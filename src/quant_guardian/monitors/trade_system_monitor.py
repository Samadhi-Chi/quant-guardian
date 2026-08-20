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
_FUEL_TIME = re.compile(r"^[A-Z]+:root:(\d{2}:\d{2}:\d{2})\s+-->")
_FUEL_LOG_DATE = re.compile(r"(\d{4}-\d{2}-\d{2})")
_FUEL_MIN_SUCCESS = re.compile(r"本轮完成，成功\s+(\d+)/(\d+)")
_FUEL_ALL_DATA_COMMAND = "fuel.exe all_data"
_FUEL_MIN_DATA_COMMAND = "fuel.exe min_data"
_FUEL_SCHEDULED_PAUSE = "在交易时间，不再更新数据"


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


@dataclass(slots=True)
class _FuelLogRuntime:
    path: Path | None = None
    offset: int = 0
    file_identity: tuple[int, int] | None = None
    carry: str = ""
    last_activity_at: datetime | None = None
    last_all_data_started_at: datetime | None = None
    last_scheduled_pause_at: datetime | None = None
    last_min_data_started_at: datetime | None = None
    last_min_data_success_at: datetime | None = None


class IncrementalFuelLog:
    """Read only the newest Fuel log tail and retain scheduling evidence."""

    def __init__(self, directory: Path, *, tail_bytes: int = 262_144) -> None:
        self.directory = directory
        self.tail_bytes = max(16_384, tail_bytes)
        self.runtime = _FuelLogRuntime()

    def _latest_path(self) -> Path | None:
        try:
            candidates = [
                path
                for path in self.directory.iterdir()
                if path.is_file() and path.suffix.casefold() == ".log"
            ]
        except OSError:
            return None
        if not candidates:
            return None
        try:
            return max(candidates, key=lambda path: path.stat().st_mtime_ns)
        except OSError:
            return None

    def _read_new_text(self) -> tuple[str, Path | None]:
        path = self._latest_path()
        if path is None:
            return "", None
        try:
            stat = path.stat()
        except OSError:
            return "", path
        identity = (int(getattr(stat, "st_ino", 0)), int(stat.st_ctime_ns))
        rotated = (
            self.runtime.path != path
            or self.runtime.file_identity is None
            or identity != self.runtime.file_identity
            or stat.st_size < self.runtime.offset
        )
        start = max(0, stat.st_size - self.tail_bytes) if rotated else self.runtime.offset
        try:
            with path.open("rb") as stream:
                stream.seek(start)
                raw = stream.read(self.tail_bytes)
                offset = stream.tell()
        except OSError:
            return "", path
        if rotated:
            self.runtime.carry = ""
            if start:
                newline = raw.find(b"\n")
                raw = raw[newline + 1 :] if newline >= 0 else b""
        self.runtime.path = path
        self.runtime.file_identity = identity
        self.runtime.offset = offset
        return raw.decode("utf-8-sig", errors="replace"), path

    @staticmethod
    def _line_time(path: Path, line: str) -> datetime | None:
        time_match = _FUEL_TIME.match(line)
        if not time_match:
            return None
        date_match = _FUEL_LOG_DATE.search(path.name)
        try:
            day = (
                date_match.group(1)
                if date_match
                else datetime.fromtimestamp(path.stat().st_mtime).strftime("%Y-%m-%d")
            )
            return datetime.strptime(
                f"{day} {time_match.group(1)}", "%Y-%m-%d %H:%M:%S"
            ).astimezone()
        except (OSError, ValueError):
            return None

    def observe(self) -> _FuelLogRuntime:
        text, path = self._read_new_text()
        if not text or path is None:
            return self.runtime
        combined = self.runtime.carry + text
        lines = combined.splitlines(keepends=True)
        if lines and not lines[-1].endswith(("\n", "\r")):
            self.runtime.carry = lines.pop()
        else:
            self.runtime.carry = ""
        for raw in lines:
            line = raw.rstrip("\r\n")
            at = self._line_time(path, line)
            if at is None:
                continue
            self.runtime.last_activity_at = at
            lowered = line.casefold()
            if _FUEL_ALL_DATA_COMMAND in lowered:
                self.runtime.last_all_data_started_at = at
            elif _FUEL_MIN_DATA_COMMAND in lowered:
                self.runtime.last_min_data_started_at = at
            if _FUEL_SCHEDULED_PAUSE in line:
                self.runtime.last_scheduled_pause_at = at
            success = _FUEL_MIN_SUCCESS.search(line)
            if success and int(success.group(1)) > 0 and success.group(1) == success.group(2):
                self.runtime.last_min_data_success_at = at
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
        self._fuel_log = IncrementalFuelLog(
            self._resolve(config.fuel_log_directory),
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
                raw_cmdline = [
                    str(item) for item in (process.info.get("cmdline") or [])
                ]
                cmdline = " ".join(raw_cmdline)
            except (psutil.Error, OSError):
                continue
            if name.casefold() in expected or any(
                token in cmdline.casefold() for token in expected if token != "python.exe"
            ):
                command = ""
                if name.casefold() == "fuel.exe":
                    executable_index = next(
                        (
                            index
                            for index, item in enumerate(raw_cmdline)
                            if Path(item).name.casefold() == "fuel.exe"
                        ),
                        0,
                    )
                    arguments = raw_cmdline[executable_index + 1 :]
                    command = arguments[0].casefold() if arguments else ""
                values.append(
                    {
                        "pid": process.pid,
                        "name": name or "unknown",
                        "command": command,
                    }
                )
        return values

    def _active_fuel_updates(self) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        all_active = self._active_processes(self.config.fuel_process_names)
        expected_commands = {
            value.casefold() for value in self.config.fuel_update_commands if value
        }
        updates = [
            value
            for value in all_active
            if not value.get("command")
            or str(value.get("command")).casefold() in expected_commands
        ]
        return updates, all_active

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
        active, all_fuel = self._active_fuel_updates()
        fuel_log = self._fuel_log.observe()
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
                metrics={
                    "active_processes": active,
                    "all_fuel_processes": all_fuel,
                    "status_file": str(status_path),
                },
            )
        errors: list[tuple[str, datetime | None]] = []
        latest_update: datetime | None = None
        latest_next: datetime | None = None
        overdue_products: list[str] = []
        stalled_products: list[str] = []
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
                and next_update
                and now
                > next_update
                + timedelta(seconds=self.config.data_overdue_grace_seconds)
                and (updated is None or updated < next_update)
            ):
                overdue_products.append(str(name))
                if now > next_update + timedelta(
                    seconds=(
                        self.config.data_overdue_grace_seconds
                        + self.config.data_stall_confirmation_seconds
                    )
                ):
                    stalled_products.append(str(name))
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
            progress_age_seconds is not None
            and progress_age_seconds
            <= max(
                self.config.data_overdue_grace_seconds,
                self.config.data_stall_confirmation_seconds,
            )
        )
        scheduled_pause_age_seconds = (
            max(0, int((now - fuel_log.last_scheduled_pause_at).total_seconds()))
            if fuel_log.last_scheduled_pause_at
            else None
        )
        full_update_paused = bool(
            fuel_log.last_scheduled_pause_at
            and fuel_log.last_all_data_started_at
            and fuel_log.last_scheduled_pause_at >= fuel_log.last_all_data_started_at
            and scheduled_pause_age_seconds is not None
            and scheduled_pause_age_seconds
            <= self.config.fuel_pause_heartbeat_stale_seconds
        )
        min_data_age_seconds = (
            max(0, int((now - fuel_log.last_min_data_success_at).total_seconds()))
            if fuel_log.last_min_data_success_at
            else None
        )
        min_data_success_fresh = bool(
            min_data_age_seconds is not None
            and min_data_age_seconds <= self.config.fuel_min_data_stale_seconds
        )
        min_data_start_age_seconds = (
            max(0, int((now - fuel_log.last_min_data_started_at).total_seconds()))
            if fuel_log.last_min_data_started_at
            else None
        )
        min_data_scheduler_fresh = bool(
            min_data_start_age_seconds is not None
            and min_data_start_age_seconds
            <= self.config.fuel_min_data_stale_seconds
        )
        min_data_processes = [
            value
            for value in all_fuel
            if str(value.get("command") or "").casefold() == "min_data"
        ]
        min_data_in_progress = bool(
            min_data_processes
            or (
                fuel_log.last_min_data_started_at
                and (
                    fuel_log.last_min_data_success_at is None
                    or fuel_log.last_min_data_started_at
                    > fuel_log.last_min_data_success_at
                )
                and now - fuel_log.last_min_data_started_at <= timedelta(minutes=3)
            )
        )
        # Fuel launches min_data on a schedule.  A round may legitimately take
        # several minutes, and overlapping launches then exit with "round is
        # already running" without another successful-write summary.  Treat the
        # scheduled launch and the exact min_data process as heartbeat evidence;
        # otherwise an active round is falsely reported as stale merely because
        # it has not finished writing yet.
        min_data_fresh = bool(
            min_data_success_fresh
            or min_data_scheduler_fresh
            or min_data_processes
        )
        min_data_expected = bool(
            fuel_log.last_min_data_started_at or min_data_processes
        )
        state = (
            ComponentState.CRITICAL
            if errors
            else ComponentState.WARNING
            if stale
            or (
                full_update_paused
                and min_data_expected
                and not min_data_fresh
                and not min_data_in_progress
            )
            or (not full_update_paused and stalled_products and not progress_fresh)
            else ComponentState.HEALTHY
        )
        if errors:
            reason = f"{len(errors)}项数据产品最近一次更新失败"
            condition = "fuel_product_error"
        elif stale:
            reason = "数据状态超过36小时未刷新"
            condition = "fuel_status_stale"
        elif full_update_paused and min_data_in_progress:
            reason = "盘中分钟数据正在更新，全量更新按计划暂停"
            condition = "fuel_scheduled_pause_minute_running"
        elif full_update_paused and min_data_fresh:
            reason = "盘中分钟数据正常，全量更新按计划暂停"
            condition = "fuel_scheduled_pause_minute_healthy"
        elif full_update_paused and min_data_expected:
            reason = "盘中分钟数据心跳已过期，全量更新仍处于计划暂停"
            condition = "fuel_min_data_stale"
        elif full_update_paused:
            reason = "盘中全量更新按计划暂停"
            condition = "fuel_scheduled_pause"
        elif overdue_products and progress_fresh:
            reason = f"Fuel正在追赶更新，{len(overdue_products)}项数据待处理"
            condition = "fuel_catching_up"
        elif overdue_products and not stalled_products:
            reason = f"{len(overdue_products)}项数据已到更新时间，正在等待更新确认"
            condition = "fuel_awaiting_confirmation"
        elif stalled_products:
            reason = (
                f"Fuel更新进度停滞，{len(stalled_products)}项数据"
                "已超过确认窗口"
            )
            condition = "fuel_stalled"
        elif active:
            reason = f"Fuel正在执行全量更新，已跟踪{enabled}项数据产品"
            condition = "fuel_full_update_running"
        elif all_fuel:
            commands = sorted(
                {str(value.get("command") or "unknown") for value in all_fuel}
            )
            reason = f"Fuel正在执行其他任务（{', '.join(commands)}），数据状态正常"
            condition = "fuel_other_task_running"
        else:
            reason = f"最近一次数据状态正常，共{enabled}项产品"
            condition = "fuel_healthy"
        return ComponentNode(
            id="trade_system.data",
            name="数据内核",
            state=state,
            reason=reason,
            observed_at=now,
            priority="high",
            metrics={
                "engine": "Fuel",
                "condition": condition,
                "active_processes": active,
                "all_fuel_processes": all_fuel,
                "products": enabled,
                "errors": len(errors),
                "error_products": [name for name, _at in errors[:8]],
                "last_update": latest_update.isoformat() if latest_update else "",
                "next_update": latest_next.isoformat() if latest_next else "",
                "overdue_products": overdue_products[:8],
                "stalled_products": stalled_products[:8],
                "progress_fresh": progress_fresh,
                "progress_age_seconds": progress_age_seconds,
                "full_update_paused": full_update_paused,
                "scheduled_pause_age_seconds": scheduled_pause_age_seconds,
                "min_data_expected": min_data_expected,
                "min_data_fresh": min_data_fresh,
                "min_data_success_fresh": min_data_success_fresh,
                "min_data_scheduler_fresh": min_data_scheduler_fresh,
                "min_data_running": bool(min_data_processes),
                "min_data_processes": min_data_processes,
                "min_data_age_seconds": min_data_age_seconds,
                "min_data_start_age_seconds": min_data_start_age_seconds,
                "min_data_last_started": (
                    fuel_log.last_min_data_started_at.isoformat()
                    if fuel_log.last_min_data_started_at
                    else ""
                ),
                "min_data_last_success": (
                    fuel_log.last_min_data_success_at.isoformat()
                    if fuel_log.last_min_data_success_at
                    else ""
                ),
                "fuel_log_file": str(fuel_log.path) if fuel_log.path else "",
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
                if rocket.business_healthy
                else ComponentState.WARNING
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
        rocket_condition = (
            "rocket_error_burst"
            if rocket.active and rocket.error_burst
            else "rocket_healthy"
            if rocket.active and rocket.business_healthy
            else "rocket_business_unhealthy"
            if rocket.active
            else "rocket_missing_active_window"
            if active_window
            else "rocket_idle"
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
                "condition": rocket_condition,
                "active": rocket.active,
                "error_burst": rocket.error_burst,
                "log_age_seconds": rocket.log_age_seconds,
                "business_healthy": rocket.business_healthy,
                "business_age_seconds": rocket.business_age_seconds,
                "heartbeat_source": rocket.heartbeat_source,
                "business_health_known": rocket.business_health_known,
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
