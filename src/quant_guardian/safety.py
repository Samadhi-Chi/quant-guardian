from __future__ import annotations

import hmac
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from quant_guardian.config import AppConfig, recovery_sentinel_path

SENTINEL_CONTENT = "QUANT_GUARDIAN_LIVE_RECOVERY_V1\n"


@dataclass(frozen=True, slots=True)
class SafetyStatus:
    live_actions_allowed: bool
    reason: str


class SafetyGate:
    """Requires both recovery mode and an explicit local sentinel."""

    def __init__(self, config: AppConfig, sentinel: Path | None = None) -> None:
        self.config = config
        self.sentinel = sentinel or recovery_sentinel_path()

    def status(self, now: datetime | None = None) -> SafetyStatus:
        if self.config.mode != "recover":
            return SafetyStatus(False, "配置为观察模式")
        expires_at = self.config.recovery.automatic_recovery_until
        if expires_at:
            try:
                expires = datetime.fromisoformat(expires_at)
            except ValueError:
                return SafetyStatus(False, "自动恢复授权截止时间无效")
            at = now or datetime.now().astimezone()
            if at >= expires:
                return SafetyStatus(
                    False,
                    f"临时自动恢复授权已于 {expires.astimezone():%Y-%m-%d %H:%M:%S} 到期",
                )
        if not self.sentinel.exists():
            return SafetyStatus(False, "实时恢复授权文件不存在")
        try:
            content = self.sentinel.read_text(encoding="utf-8")
        except OSError as exc:
            return SafetyStatus(False, f"无法读取实时恢复授权文件：{exc}")
        if not hmac.compare_digest(content, SENTINEL_CONTENT):
            return SafetyStatus(False, "实时恢复授权文件内容无效")
        if expires_at:
            return SafetyStatus(
                True,
                f"临时自动恢复已授权至 {expires.astimezone():%Y-%m-%d %H:%M:%S}",
            )
        return SafetyStatus(True, "恢复模式与本机授权文件均已启用")
