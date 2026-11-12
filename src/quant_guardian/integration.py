"""Read-only discovery of Quantclass's execution contract.

Only approved operational fields leave the client config reader. Account IDs,
tokens, strategy parameters and robot URLs are deliberately excluded.
"""
from __future__ import annotations

import copy
import json
from dataclasses import asdict, dataclass, field
from pathlib import Path

from quant_guardian.config import AppConfig

MODE_LABELS = {
    "mini_qmt": "miniQMT",
    "planA": "Plan A · 券商特邀",
    "planX": "Plan X · 原生计划",
    "qmt": "大 QMT · 旧版桥接",
    "unknown": "未识别的接入模式",
}


@dataclass(frozen=True, slots=True)
class ClientSnapshot:
    available: bool = False
    valid: bool = True
    reason: str = "未找到客户端配置，使用 Guardian 显式配置"
    mode: str = ""
    qmt_root: str = ""
    data_root: str = ""
    automatic_live: bool | None = None
    startup_live: bool | None = None
    connection_checked: bool | None = None
    accelerated_data: bool | None = None
    strategy_keys: frozenset[str] = field(default_factory=frozenset, repr=False)


class ClientConfigReader:
    MAX_BYTES = 8 * 1024 * 1024

    def __init__(self, path: Path) -> None:
        self.path = path
        self._identity: tuple[int, int] | None = None
        self._last = ClientSnapshot()

    def read(self) -> ClientSnapshot:
        try:
            stat = self.path.stat()
        except FileNotFoundError:
            if self._identity is None:
                return ClientSnapshot()
            return self._invalid("客户端配置已消失，保留上一次有效模式，暂停自动恢复")
        except OSError:
            return self._invalid("无法读取客户端配置，暂停自动恢复")
        identity = (stat.st_mtime_ns, stat.st_size)
        if identity == self._identity:
            return self._last
        try:
            if stat.st_size > self.MAX_BYTES:
                raise ValueError("configuration too large")
            with self.path.open("rb") as stream:
                document = json.loads(stream.read(self.MAX_BYTES + 1).decode("utf-8-sig"))
            if not isinstance(document, dict):
                raise ValueError("configuration must be an object")
            real = document.get("real_market_config", {})
            settings = document.get("settings", {})
            if not isinstance(real, dict) or not isinstance(settings, dict):
                raise ValueError("invalid settings")
            raw_mode = str(real.get("qmt_mode", ""))
            mode = raw_mode if raw_mode in MODE_LABELS else "unknown" if raw_mode else ""
            def boolean(value):
                return value if isinstance(value, bool) else None
            keys: set[str] = set()
            pos = document.get("pos_mgmt")
            groups = pos.get("strategies", []) if isinstance(pos, dict) else []
            for index, group in enumerate(groups if isinstance(groups, list) else []):
                if not isinstance(group, dict):
                    continue
                group_tag = str(group.get("remark_name") or f"X{index + 1}-{group.get('name', '')}")
                for slot, strategy in enumerate(group.get("strategy_list", [])):
                    if isinstance(strategy, dict):
                        keys.add(f"{group_tag}#{slot}.{strategy.get('name', '')}")
            snapshot = ClientSnapshot(
                available=True,
                mode=mode,
                qmt_root=str(real.get("qmt_path") or ""),
                data_root=str(settings.get("all_data_path") or ""),
                automatic_live=boolean(document.get("auto_real_trading")),
                startup_live=boolean(settings.get("is_auto_launch_real_trading")),
                connection_checked=boolean(document.get("qmt_connect_ok")),
                accelerated_data=boolean(settings.get("accelerated_data_source")),
                reason="已读取客户端保存配置；连接标记只作旁证",
                strategy_keys=frozenset(keys),
            )
        except (OSError, ValueError, TypeError, UnicodeError):
            # Do not cache a half-written document as a valid new mode.
            return self._invalid("客户端配置正在写入或格式不完整，保留上一次有效结果")
        self._identity, self._last = identity, snapshot
        return snapshot

    def _invalid(self, reason: str) -> ClientSnapshot:
        from dataclasses import replace

        return replace(self._last, valid=False, reason=reason)


@dataclass(frozen=True, slots=True)
class RuntimeProfile:
    mode: str
    adapter: str
    config: AppConfig = field(compare=False, repr=False)
    automatic_recovery_supported: bool = True
    reason: str = ""

    @property
    def label(self) -> str:
        return MODE_LABELS.get(self.mode, MODE_LABELS["unknown"])

    @property
    def fingerprint(self) -> tuple[str, ...]:
        return (
            self.mode, self.config.qmt.launcher, self.config.qmt.userdata_directory,
            self.config.probe.python_executable, self.config.probe.xtquant_parent,
            self.config.trade_system.data_root,
            self.config.trade_system.quantclass_config,
            str(self.config.probe.enabled), str(self.config.probe.timeout_seconds),
            self.config.probe.account_id_protected,
            json.dumps(asdict(self.config.trade_system), sort_keys=True),
        )


def resolve_runtime(config: AppConfig, client: ClientSnapshot) -> RuntimeProfile:
    """Build an effective config without rewriting either application's config."""
    effective = copy.deepcopy(config)
    if client.data_root and config.trade_system.follow_client_data_root:
        effective.trade_system.data_root = client.data_root
        rocket_log = Path(config.trade_system.rocket_log_directory)
        effective.rocket.log_directory = str(
            rocket_log if rocket_log.is_absolute() else Path(client.data_root) / rocket_log
        )
    mode = config.qmt.access_mode
    if mode == "auto":
        mode = client.mode or (
            "mini_qmt" if Path(config.qmt.userdata_directory).name.casefold() == "userdata_mini"
            else "unknown"
        )
    effective.qmt.access_mode = mode
    if mode == "mini_qmt":
        if client.qmt_root and config.qmt.access_mode == "auto":
            root = Path(client.qmt_root)
            if root.name.casefold() in {"userdata", "userdata_mini"}:
                root = root.parent
            effective.qmt.launcher = str(root / "bin.x64" / "XtItClient.exe")
            effective.qmt.working_directory = str(root / "config" / "tradingtime")
            effective.qmt.userdata_directory = str(root / "userdata_mini")
            effective.qmt.log_directory = str(root / "userdata_mini" / "log")
        effective.qmt.main_process_name = "XtMiniQmt.exe"
        effective.qmt.process_names = ["XtMiniQmt.exe", "miniquote.exe"]
        return RuntimeProfile(mode, "xtquant", effective)
    if mode not in {"planA", "planX", "qmt"}:
        return RuntimeProfile("unknown", "unavailable", effective, False, "接入模式未知，需人工核对")
    root = Path(client.qmt_root) if client.qmt_root else Path(config.qmt.launcher).parent.parent
    if root.name.casefold() in {"userdata", "userdata_mini"}:
        root = root.parent
    effective.qmt.launcher = str(root / "bin.x64" / "XtItClient.exe")
    effective.qmt.working_directory = str(root / "config" / "tradingtime")
    effective.qmt.userdata_directory = str(root / "userdata")
    effective.qmt.log_directory = str(root / "userdata" / "log")
    effective.qmt.main_process_name = "XtItClient.exe"
    effective.qmt.process_names = ["XtItClient.exe", "miniquote.exe"]
    if mode == "planA":
        effective.probe.python_executable = str(root / "bin.x64" / "pythonw.exe")
        effective.probe.xtquant_parent = str(root / "bin.x64" / "Lib" / "site-packages")
        return RuntimeProfile(mode, "broker_runtime", effective)
    return RuntimeProfile(
        mode, "manual_plan" if mode == "planX" else "unavailable", effective, False,
        "原生策略执行状态需人工核对；自动重启可能中断策略" if mode == "planX"
        else "旧版桥接没有已验证的只读协议，需人工核对",
    )
