"""Mode-aware execution readiness, plan delivery and strategy handoff evidence."""
from __future__ import annotations

import csv
from datetime import datetime
from functools import lru_cache
from pathlib import Path

from quant_guardian.config import TradeSystemConfig
from quant_guardian.domain.components import ComponentNode, ComponentState
from quant_guardian.integration import MODE_LABELS, ClientSnapshot


def execution_nodes(config, client, now, *, active_window, runtime, client_running):
    readiness_state = ComponentState.HEALTHY
    readiness_reason = MODE_LABELS.get(client.mode, "未识别客户端模式")
    condition = "ready"
    if not client.valid:
        readiness_state, readiness_reason, condition = ComponentState.WARNING, client.reason, "config_unreadable"
    elif client.automatic_live is False:
        readiness_state, readiness_reason, condition = ComponentState.IDLE, "自动实盘已关闭；需要时由你在客户端开启", "operator_disabled"
    elif not client_running:
        readiness_state = ComponentState.CRITICAL if active_window else ComponentState.IDLE
        readiness_reason, condition = "未发现量化客户端进程，自动调度尚未就绪", "client_missing"
    elif client.mode == "planA" and client.connection_checked is False:
        readiness_state = ComponentState.CRITICAL if active_window else ComponentState.WARNING
        readiness_reason, condition = "客户端连接自检未通过，即使QMT可连接也不会唤醒Rocket", "client_selfcheck_failed"
    elif client.mode == "unknown":
        readiness_state, readiness_reason, condition = ComponentState.WARNING, "客户端接入模式未知，需核对新版配置", "unknown_mode"
    elif client.mode == "planX":
        readiness_reason = "Plan X 负责生成计划，QMT内部执行结果需另行核对"
    elif client.automatic_live is True:
        readiness_reason += "；自动实盘已开启；" + ("开机自动实盘已开启" if client.startup_live else "下次启动需人工开启实盘")
    readiness = ComponentNode(
        "trade_system.readiness", "执行就绪", readiness_state, readiness_reason, now,
        priority="high", metrics={
            "condition": condition, "execution_mode": client.mode,
            "automatic_live": client.automatic_live, "startup_live": client.startup_live,
            "client_connection_checked": client.connection_checked,
            "client_running": client_running,
        },
    )
    plan = _plan_node(config, client, now, active_window=active_window, runtime=runtime)
    return readiness, plan, _handoff_node(config, client, now)


def _plan_node(config, client, now, *, active_window, runtime):
    root = Path(config.data_root)
    directory = Path(config.plan_directory)
    if not directory.is_absolute():
        directory = root / directory
    # Look up exact dated outputs, never scan strategy databases or execute py.
    files = [directory / f"{prefix}_{now:%Y%m%d}{suffix}" for prefix, suffix in (
        ("plan_py36", ".py"), ("plan", ".py"), ("plan_simple", ".json"),
    )]
    outputs = []
    for path in files:
        try:
            stat = path.stat()
            if stat.st_size:
                outputs.append((path.name, stat.st_mtime))
        except OSError:
            continue
    required = client.mode == "planX"
    failed = bool(runtime.plan_error_at and (
        runtime.plan_success_at is None or runtime.plan_error_at > runtime.plan_success_at
    ))
    state = ComponentState.WARNING if failed else ComponentState.HEALTHY if outputs else ComponentState.IDLE
    reason = "原生计划生成失败；需要查看Fusion日志" if failed else (
        "今日计划已生成；尚未验证QMT内运行或成交" if outputs
        else "当前没有今日原生计划"
    )
    condition = "generation_failed" if failed else "generated_not_executed" if outputs else "no_plan"
    if required and active_window and not outputs and client.automatic_live:
        state, reason, condition = ComponentState.CRITICAL, "Plan X执行时段缺少今日计划", "missing_required_plan"
    if not required:
        reason += "；当前接入不依赖此脚本"
    return ComponentNode(
        "trade_system.plan", "原生计划交付", state, reason, now,
        metrics={
            "condition": condition, "selected": required, "required": required,
            "output_count": len(outputs), "trade_date": now.date().isoformat(),
            "latest_generated_at": datetime.fromtimestamp(max(t for _, t in outputs)).astimezone().isoformat() if outputs else "",
            "execution_confirmed": False,
        },
    )


def _handoff_node(config: TradeSystemConfig, client: ClientSnapshot, now: datetime) -> ComponentNode:
    path = Path(config.data_root) / "real_trading" / "rocket" / "data" / "账户信息" / "实盘临时调仓信号-个股-仓位记录.csv"
    state, reason, condition = ComponentState.IDLE, "暂无可用的策略交接台账", "ledger_unavailable"
    legacy_rows, legacy_shares = 0, 0
    if client.strategy_keys and path.is_file():
        try:
            stat = path.stat()
            legacy_rows, legacy_shares = _ledger_counts(
                path, stat.st_mtime_ns, stat.st_size, client.strategy_keys,
            )
            state = ComponentState.WARNING if legacy_rows else ComponentState.HEALTHY
            condition = "legacy_holdings_unassigned" if legacy_rows else "ledger_names_current"
            reason = (
                f"{legacy_rows}条非当前策略台账仍有{legacy_shares}股，需核对交接；未修改持仓"
                if legacy_rows else "台账中的非零持仓均有当前策略标签；尚未与券商逐股对账"
            )
        except (OSError, ValueError, KeyError):
            state, reason, condition = ComponentState.UNKNOWN, "台账暂不可读或字段变化，交接状态未知", "ledger_unknown"
    return ComponentNode(
        "trade_system.handoff", "策略交接", state, reason, now, priority="high",
        metrics={"condition": condition, "legacy_rows": legacy_rows, "legacy_shares": legacy_shares,
                 "broker_reconciled": False, "scope": "ledger_strategy_labels_only"},
    )


@lru_cache(maxsize=16)
def _ledger_counts(path: Path, mtime: int, size: int, strategy_keys: frozenset[str]) -> tuple[int, int]:
    if size > 8 * 1024 * 1024:
        raise ValueError("ledger exceeds read budget")
    for encoding in ("utf-8-sig", "gb18030"):
        try:
            with path.open(encoding=encoding, newline="") as stream:
                rows = list(csv.DictReader(stream))
            break
        except UnicodeError:
            continue
    else:
        raise ValueError("ledger encoding")
    if not rows or not {"策略名称", "当前持仓量"}.issubset(rows[0]):
        raise ValueError("ledger schema")
    legacy = [int(float(row["当前持仓量"] or 0)) for row in rows if row["策略名称"] not in strategy_keys]
    return sum(value > 0 for value in legacy), sum(value for value in legacy if value > 0)
