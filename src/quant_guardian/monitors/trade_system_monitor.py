from __future__ import annotations

import json
import re
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import psutil

from quant_guardian.config import TradeSystemConfig
from quant_guardian.domain.components import (
    ComponentNode,
    ComponentState,
    aggregate_state,
)
from quant_guardian.monitors.rocket_monitor import RocketObservation

_TIMESTAMP = re.compile(r"^(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})(?:,\d+)?")
_START = re.compile(r"\[(fuel|aqua|zeus|rocket)\].*\bpid\s+\d+\s+start", re.I)
_EXIT = re.compile(r"\[(fuel|aqua|zeus|rocket)\].*\bpid\s+\d+\s+exit successfully", re.I)


def _parse_timestamp(line: str) -> datetime | None:
    match = _TIMESTAMP.match(line)
    if not match:
        return None
    try:
        return datetime.strptime(match.group(1), "%Y-%m-%d %H:%M:%S").astimezone()
    except ValueError:
        return None


def _short_error(line: str) -> str:
    text = line.split(" - ", 1)[-1].strip()
    text = re.sub(r"\s+", " ", text)
    if "Usecols do not match columns" in text:
        return "策略所需数据字段缺失（Usecols不匹配）"
    return text[:180] or "任务日志记录了异常"


@dataclass(slots=True)
class _LogRuntime:
    offset: int = 0
    file_identity: tuple[int, int] | None = None
    carry: str = ""
    run_started_at: datetime | None = None
    run_error: bool = False
    run_error_summary: str = ""
    last_result: str = "unknown"
    last_result_at: datetime | None = None
    last_error_summary: str = ""


class IncrementalTaskLog:
    def __init__(self, path: Path, *, tail_bytes: int = 262_144) -> None:
        self.path = path
        self.tail_bytes = max(16_384, tail_bytes)
        self.runtime = _LogRuntime()

    def _read_new_text(self) -> str:
        try:
            stat = self.path.stat()
        except OSError:
            return ""
        identity = (int(getattr(stat, "st_ino", 0)), int(stat.st_ctime_ns))
        rotated = (
            self.runtime.file_identity is not None
            and (identity != self.runtime.file_identity or stat.st_size < self.runtime.offset)
        )
        if self.runtime.file_identity is None or rotated:
            start = max(0, stat.st_size - self.tail_bytes)
        else:
            start = self.runtime.offset
            if stat.st_size - start > self.tail_bytes:
                start = stat.st_size - self.tail_bytes
        try:
            with self.path.open("rb") as stream:
                stream.seek(start)
                raw = stream.read(self.tail_bytes)
                self.runtime.offset = stream.tell()
        except OSError:
            return ""
        self.runtime.file_identity = identity
        return raw.decode("utf-8-sig", errors="replace")

    def observe(self) -> _LogRuntime:
        text = self._read_new_text()
        if not text:
            return self.runtime
        combined = self.runtime.carry + text
        lines = combined.splitlines(keepends=True)
        if lines and not lines[-1].endswith(("\n", "\r")):
            self.runtime.carry = lines.pop()
        else:
            self.runtime.carry = ""
        for raw in lines:
            line = raw.rstrip("\r\n")
            at = _parse_timestamp(line)
            if _START.search(line):
                self.runtime.run_started_at = at
                self.runtime.run_error = False
                self.runtime.run_error_summary = ""
                continue
            if "[ERROR]" in line or "Traceback" in line or "ValueError:" in line:
                self.runtime.run_error = True
                if "[ERROR]" in line or not self.runtime.run_error_summary:
                    self.runtime.run_error_summary = _short_error(line)
                if at:
                    self.runtime.last_result_at = at
                continue
            if _EXIT.search(line):
                self.runtime.last_result_at = at or self.runtime.last_result_at
                if self.runtime.run_error:
                    self.runtime.last_result = "failed"
                    self.runtime.last_error_summary = (
                        self.runtime.run_error_summary or "任务运行期间出现异常"
                    )
                else:
                    self.runtime.last_result = "success"
                    self.runtime.last_error_summary = ""
                self.runtime.run_started_at = None
                self.runtime.run_error = False
                self.runtime.run_error_summary = ""
        return self.runtime


@dataclass(frozen=True, slots=True)
class TradeSystemObservation:
    node: ComponentNode
    data: ComponentNode
    selection: ComponentNode
    order: ComponentNode
    evidence: dict[str, Any] = field(default_factory=dict)


class TradeSystemMonitor:
    def __init__(self, config: TradeSystemConfig) -> None:
        self.config = config
        self.root = Path(config.data_root)
        self._last_products: dict[str, Any] | None = None
        self._last_updates: dict[str, Any] | None = None
        self._aqua_log = IncrementalTaskLog(
            self._resolve(config.aqua_log_file),
            tail_bytes=config.task_log_tail_bytes,
        )
        self._zeus_log = IncrementalTaskLog(
            self._resolve(config.zeus_log_file),
            tail_bytes=config.task_log_tail_bytes,
        )

    def _resolve(self, value: str) -> Path:
        path = Path(value)
        return path if path.is_absolute() else self.root / path

    @staticmethod
    def _active_processes(names: list[str]) -> list[dict[str, Any]]:
        expected = {name.casefold() for name in names}
        values: list[dict[str, Any]] = []
        if not expected:
            return values
        for process in psutil.process_iter(["pid", "name", "cmdline"]):
            try:
                name = str(process.info.get("name") or "")
                cmdline = " ".join(process.info.get("cmdline") or [])
            except (psutil.Error, OSError):
                continue
            if name.casefold() in expected or any(
                token in cmdline.casefold() for token in expected if token != "python.exe"
            ):
                values.append({"pid": process.pid, "name": name or "unknown"})
        return values

    @staticmethod
    def _read_json(path: Path) -> dict[str, Any] | None:
        try:
            value = json.loads(path.read_text(encoding="utf-8-sig"))
        except (OSError, ValueError, json.JSONDecodeError):
            return None
        return value if isinstance(value, dict) else None

    @staticmethod
    def _parse_local(value: object) -> datetime | None:
        if not value:
            return None
        try:
            return datetime.strptime(str(value), "%Y-%m-%d %H:%M:%S").astimezone()
        except ValueError:
            return None

    def _observe_data(self, now: datetime) -> ComponentNode:
        status_path = self._resolve(self.config.fuel_status_file)
        update_path = self._resolve(self.config.fuel_update_file)
        active = self._active_processes(self.config.fuel_process_names)
        products = self._read_json(status_path)
        if products is not None:
            self._last_products = products
        else:
            products = self._last_products
        updates = self._read_json(update_path)
        if updates is not None:
            self._last_updates = updates
        else:
            updates = self._last_updates
        if products is None:
            return ComponentNode(
                id="trade_system.data",
                name="数据内核",
                state=ComponentState.WARNING,
                reason="Fuel状态文件暂时不可读，保留上一次有效结论",
                observed_at=now,
                priority="high",
                metrics={"active_processes": active, "status_file": str(status_path)},
            )
        errors: list[tuple[str, datetime | None]] = []
        latest_update: datetime | None = None
        latest_next: datetime | None = None
        overdue_products: list[str] = []
        enabled = 0
        for name, value in products.items():
            if not isinstance(value, dict):
                continue
            eligible = bool(value.get("isListed", 1) and value.get("canAutoUpdate", 1))
            if eligible:
                enabled += 1
            updated = self._parse_local(value.get("lastUpdateTime"))
            next_update = self._parse_local(value.get("nextUpdateTime"))
            errored = self._parse_local(value.get("lastErrTime"))
            if updated and (latest_update is None or updated > latest_update):
                latest_update = updated
            if next_update and (latest_next is None or next_update > latest_next):
                latest_next = next_update
            if (
                eligible
                and active
                and next_update
                and now
                > next_update
                + timedelta(seconds=self.config.data_overdue_grace_seconds)
                and (updated is None or updated < next_update)
            ):
                overdue_products.append(str(name))
            if errored and (updated is None or errored >= updated):
                errors.append((str(name), errored))
        try:
            file_at = datetime.fromtimestamp(status_path.stat().st_mtime).astimezone()
        except OSError:
            file_at = None
        recorded_updates = [
            self._parse_local(key)
            for key in (updates or {})
            if isinstance(key, str)
        ]
        latest_recorded_update = max(
            (value for value in recorded_updates if value is not None),
            default=None,
        )
        stale = bool(file_at and now - file_at > timedelta(hours=36))
        progress_age_seconds = (
            max(0, int((now - file_at).total_seconds())) if file_at else None
        )
        progress_fresh = bool(
            active
            and progress_age_seconds is not None
            and progress_age_seconds <= self.config.data_overdue_grace_seconds
        )
        state = (
            ComponentState.CRITICAL
            if errors
            else ComponentState.WARNING
            if stale or (overdue_products and not progress_fresh)
            else ComponentState.HEALTHY
        )
        if errors:
            reason = f"{len(errors)}项数据产品最近一次更新失败"
        elif overdue_products and progress_fresh:
            reason = f"Fuel正在追赶更新，{len(overdue_products)}项数据待处理"
        elif overdue_products:
            reason = (
                f"Fuel更新进度停滞，{len(overdue_products)}项数据"
                "已超过计划更新时间"
            )
        elif active:
            reason = f"Fuel正在更新，已跟踪{enabled}项数据产品"
        elif stale:
            reason = "数据状态超过36小时未刷新"
        else:
            reason = f"最近一次数据状态正常，共{enabled}项产品"
        return ComponentNode(
            id="trade_system.data",
            name="数据内核",
            state=state,
            reason=reason,
            observed_at=now,
            priority="high",
            metrics={
                "engine": "Fuel",
                "active_processes": active,
                "products": enabled,
                "errors": len(errors),
                "error_products": [name for name, _at in errors[:8]],
                "last_update": latest_update.isoformat() if latest_update else "",
                "next_update": latest_next.isoformat() if latest_next else "",
                "overdue_products": overdue_products[:8],
                "progress_fresh": progress_fresh,
                "progress_age_seconds": progress_age_seconds,
                "overdue_seconds": (
                    max(0, int((now - latest_next).total_seconds()))
                    if latest_next and now > latest_next
                    else 0
                ),
                "latest_recorded_update": (
                    latest_recorded_update.isoformat()
                    if latest_recorded_update
                    else ""
                ),
                "status_file_modified": file_at.isoformat() if file_at else "",
                "update_file": str(update_path),
            },
        )

    def _task_node(
        self,
        *,
        node_id: str,
        name: str,
        engine: str,
        process_names: list[str],
        log: IncrementalTaskLog,
        now: datetime,
        priority: str,
        failure_is_critical: bool,
    ) -> ComponentNode:
        active = self._active_processes(process_names)
        runtime = log.observe()
        if active:
            state = ComponentState.HEALTHY
            reason = f"{engine}任务正在运行"
        elif runtime.last_result == "failed":
            state = (
                ComponentState.CRITICAL
                if failure_is_critical
                and runtime.last_result_at
                and now - runtime.last_result_at < timedelta(hours=36)
                else ComponentState.WARNING
            )
            reason = runtime.last_error_summary or f"{engine}最近一次任务失败"
        elif runtime.last_result == "success":
            state = ComponentState.IDLE
            reason = f"{engine}当前空闲，最近一次任务成功"
        else:
            state = ComponentState.IDLE
            reason = f"{engine}当前无运行任务"
        return ComponentNode(
            id=node_id,
            name=name,
            state=state,
            reason=reason,
            observed_at=now,
            priority=priority,
            metrics={
                "engine": engine,
                "active_processes": active,
                "last_result": runtime.last_result,
                "last_result_at": (
                    runtime.last_result_at.isoformat()
                    if runtime.last_result_at
                    else ""
                ),
                "log_file": str(log.path),
            },
        )

    def observe(
        self,
        now: datetime,
        *,
        rocket: RocketObservation,
        active_window: bool,
    ) -> TradeSystemObservation:
        if not self.config.enabled:
            idle = ComponentNode(
                id="trade_system",
                name="Trade System",
                state=ComponentState.IDLE,
                reason="Trade System监控已关闭",
                observed_at=now,
            )
            return TradeSystemObservation(idle, idle, idle, idle)
        data = self._observe_data(now)
        aqua = self._task_node(
            node_id="trade_system.selection.aqua",
            name="选股引擎 · Aqua",
            engine="Aqua",
            process_names=self.config.aqua_process_names,
            log=self._aqua_log,
            now=now,
            priority="high",
            failure_is_critical=True,
        )
        zeus = self._task_node(
            node_id="trade_system.selection.zeus",
            name="选股引擎 · Zeus",
            engine="Zeus",
            process_names=self.config.zeus_process_names,
            log=self._zeus_log,
            now=now,
            priority="high",
            failure_is_critical=True,
        )
        selected_engine = str(self.config.selection_engine).casefold()
        aqua = replace(
            aqua,
            metrics={**aqua.metrics, "selected": selected_engine == "aqua"},
        )
        zeus = replace(
            zeus,
            metrics={**zeus.metrics, "selected": selected_engine == "zeus"},
        )
        selected = aqua if selected_engine == "aqua" else zeus
        selection = ComponentNode(
            id="trade_system.selection",
            name="选股内核",
            state=selected.state,
            reason=f"当前使用{selected.metrics['engine']}：{selected.reason}",
            observed_at=now,
            priority="high",
            metrics={
                "engine": selected.metrics["engine"],
                "selected_engine": selected_engine,
                "available_engines": ["Aqua", "Zeus"],
            },
            children=(aqua, zeus),
        )
        if rocket.active:
            rocket_state = (
                ComponentState.CRITICAL
                if rocket.error_burst
                else ComponentState.HEALTHY
            )
        elif active_window:
            rocket_state = ComponentState.WARNING
        else:
            rocket_state = ComponentState.IDLE
        rocket_reason = (
            rocket.reason
            if rocket.active
            else "下单时段未检测到Rocket进程"
            if active_window
            else "Rocket当前空闲，当前时段无需运行"
        )
        order = ComponentNode(
            id="trade_system.order",
            name="下单内核",
            state=rocket_state,
            reason=rocket_reason,
            observed_at=now,
            priority="high",
            metrics={
                "engine": "Rocket",
                "active": rocket.active,
                "error_burst": rocket.error_burst,
                "log_age_seconds": rocket.log_age_seconds,
            },
        )
        children = (data, selection, order)
        parent = ComponentNode(
            id="trade_system",
            name="Trade System",
            state=aggregate_state(children),
            reason=(
                "数据、选股或下单链路需要处理"
                if any(
                    child.state in {ComponentState.CRITICAL, ComponentState.WARNING}
                    for child in children
                )
                else "数据、选股与下单内核状态已汇总"
            ),
            observed_at=now,
            children=children,
        )
        return TradeSystemObservation(
            parent,
            data,
            selection,
            order,
            evidence={
                "data_root": str(self.root),
                "selection_engine": selected_engine,
                "zeus_log": str(self._zeus_log.path),
                "aqua_log": str(self._aqua_log.path),
            },
        )
