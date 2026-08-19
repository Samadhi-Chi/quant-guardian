from __future__ import annotations

import hashlib
import re
import secrets
from datetime import datetime
from pathlib import Path
from typing import Any

from quant_guardian.gateway.config import (
    MessagingConfig,
    default_messaging_config_path,
    load_messaging_config,
    save_messaging_config,
)
from quant_guardian.gateway.ipc import GatewayIpcError, GuardianControlClient
from quant_guardian.gateway.models import Command, CommandReply, InboundMessage
from quant_guardian.gateway.store import GatewayStore

_SPACE_RE = re.compile(r"\s+")
_PAIR_RE = re.compile(
    r"^(?:/bind|绑定)\s+(QGP-[A-F0-9]{4}-[A-F0-9]{4})$",
    re.IGNORECASE,
)
_CONFIRM_RE = re.compile(r"^确认\s+(QG-\d{4})$", re.IGNORECASE)
_CANCEL_RE = re.compile(r"^取消(?:\s+(QG-\d{4}))?$", re.IGNORECASE)
_CALLBACK_RE = re.compile(r"^qg:restart:(confirm|cancel):(QGC-[a-f0-9]{24})$")


def parse_command(text: str) -> Command:
    value = _SPACE_RE.sub(" ", str(text or "").strip())
    lowered = value.casefold()
    if not value:
        return Command("help")
    if _PAIR_RE.fullmatch(value):
        return Command("pair", _PAIR_RE.fullmatch(value).group(1).upper())
    if _CONFIRM_RE.fullmatch(value):
        return Command("confirm_restart", _CONFIRM_RE.fullmatch(value).group(1).upper())
    if _CANCEL_RE.fullmatch(value):
        match = _CANCEL_RE.fullmatch(value)
        return Command("cancel_restart", (match.group(1) or "").upper())
    aliases = {
        "/start": "help",
        "/help": "help",
        "帮助": "help",
        "/status": "status",
        "状态": "status",
        "查看状态": "status",
        "/check": "check",
        "检测": "check",
        "立即检测": "check",
        "/incidents": "incidents",
        "故障": "incidents",
        "异常": "incidents",
        "/operations": "operations",
        "操作": "operations",
        "操作记录": "operations",
        "/restart_qmt": "restart_qmt",
        "重启qmt": "restart_qmt",
        "重启 qmt": "restart_qmt",
        "重启qmt api": "restart_qmt",
        "重启 qmt api": "restart_qmt",
        "重启quantclass": "forbidden_quantclass",
        "重启 quantclass": "forbidden_quantclass",
        "重启trade system": "forbidden_quantclass",
    }
    if lowered in aliases:
        return Command(aliases[lowered])
    dangerous = ("下单", "撤单", "shell", "cmd", "powershell", "fuel", "aqua", "zeus", "rocket")
    if any(token in lowered for token in dangerous):
        return Command("forbidden")
    return Command("unknown")


def _state_name(value: str) -> str:
    return {
        "healthy": "健康",
        "warning": "需关注",
        "critical": "故障",
        "idle": "空闲",
        "unknown": "未知",
        "recovering": "恢复中",
        "starting": "启动中",
        "suspect": "待确认",
        "degraded": "降级",
        "verifying": "验证中",
        "manual_required": "需人工",
        "lockout": "已锁定",
        "paused": "已暂停",
    }.get(value, value or "未知")


def format_status(document: dict[str, Any]) -> str:
    components = document.get("components") if isinstance(document.get("components"), dict) else {}
    qmt = components.get("qmt_api") if isinstance(components.get("qmt_api"), dict) else {}
    trade = (
        components.get("trade_system") if isinstance(components.get("trade_system"), dict) else {}
    )
    schedule = document.get("schedule") if isinstance(document.get("schedule"), dict) else {}
    attention = document.get("attention") if isinstance(document.get("attention"), dict) else {}
    lines = [
        f"Quant Guardian：{_state_name(str(document.get('state') or 'unknown'))}",
        f"QMT API：{_state_name(str(qmt.get('state') or 'unknown'))}",
        f"Trade System：{_state_name(str(trade.get('state') or 'unknown'))}",
    ]
    message = str(attention.get("message") or document.get("reason") or "")
    if message:
        lines.append(f"提示：{message[:180]}")
    next_check = str(schedule.get("next_check_at") or "")
    if next_check:
        try:
            next_value = datetime.fromisoformat(next_check).astimezone().strftime("%m-%d %H:%M:%S")
        except ValueError:
            next_value = next_check[:19]
        lines.append(f"下次检测：{next_value}")
    return "\n".join(lines)


class CommandProcessor:
    """Deterministic command router.  No free-form agent or shell is involved."""

    def __init__(
        self,
        *,
        store: GatewayStore,
        client: GuardianControlClient,
        config_path: Path | None = None,
    ) -> None:
        self.store = store
        self.client = client
        self.config_path = config_path or default_messaging_config_path()

    def _config(self) -> MessagingConfig:
        return load_messaging_config(self.config_path)

    @staticmethod
    def _allowed(config: MessagingConfig, message: InboundMessage) -> bool:
        if message.chat_type != "private":
            return False
        channel = config.telegram if message.channel == "telegram" else config.weixin
        allowed = {str(value) for value in channel.allowed_user_ids}
        if message.sender_id not in allowed:
            return False
        return not channel.home_chat_id or message.chat_id == channel.home_chat_id

    def process(self, message: InboundMessage) -> CommandReply:
        if message.chat_type != "private":
            return CommandReply("群聊控制已永久禁用。", command_name="rejected", outcome="blocked")
        if message.callback_data:
            return self._process_callback(message)
        command = parse_command(message.text)
        if command.name == "pair":
            return self._pair(message, command.argument)
        config = self._config()
        if not self._allowed(config, message):
            return CommandReply(
                "此私聊尚未绑定。请在 Quant Guardian 的“设置 → 消息通道”生成配对码。",
                command_name=command.name,
                outcome="blocked",
            )
        if command.name == "help":
            return CommandReply(
                "可用命令：\n"
                "状态 / /status\n检测 / /check\n故障 / /incidents\n"
                "操作 / /operations\n重启 QMT / /restart_qmt\n\n"
                "只允许控制 QMT；Quantclass、Fuel、Aqua、Zeus、Rocket、下单和撤单均不开放。",
                command_name="help",
            )
        if command.name in {"forbidden", "forbidden_quantclass"}:
            return CommandReply(
                "该操作不在远程控制范围内。远程端只提供只读查询和经二次确认的 QMT 受控重启。",
                command_name=command.name,
                outcome="blocked",
            )
        if command.name == "unknown":
            return CommandReply(
                "无法识别该命令。发送“帮助”查看固定命令列表。", command_name="unknown"
            )
        if command.name == "confirm_restart":
            return self._confirm_by_code(message, command.argument)
        if command.name == "cancel_restart":
            return self._cancel_by_code(message, command.argument)
        if command.name == "restart_qmt":
            return self._request_restart(message, config)
        return self._read_command(message, command.name)

    def _pair(self, message: InboundMessage, code: str) -> CommandReply:
        challenge, reason = self.store.consume_pairing(
            channel=message.channel,
            sender_id=message.sender_id,
            chat_id=message.chat_id,
            code=code,
        )
        if challenge is None:
            return CommandReply(reason, command_name="pair", outcome="blocked")
        config = self._config()
        channel_config = config.telegram if message.channel == "telegram" else config.weixin
        channel_config.enabled = True
        channel_config.allowed_user_ids = [message.sender_id]
        channel_config.home_chat_id = message.chat_id
        config.gateway_enabled = True
        save_messaging_config(config, self.config_path)
        request_id = challenge.request_id
        self.store.record_command(
            request_id=request_id,
            channel=message.channel,
            sender_id=message.sender_id,
            chat_id=message.chat_id,
            command="pair",
            status="succeeded",
            reason="private chat paired",
        )
        return CommandReply(
            "绑定成功。当前私聊已成为唯一授权会话；群聊仍保持禁用。发送“状态”进行首次验证。",
            command_name="pair",
        )

    def _request_id(self) -> str:
        return f"QGR-{datetime.now().astimezone():%Y%m%d}-{secrets.token_hex(6)}"

    def _read_command(self, message: InboundMessage, command: str) -> CommandReply:
        action = command
        params: dict[str, Any] = {}
        if command == "check":
            params["source"] = "all"
        request_id = self._request_id()
        try:
            result = self.client.request(
                action,
                channel=message.channel,
                sender_id=message.sender_id,
                chat_id=message.chat_id,
                params=params,
                request_id=request_id,
            )
            if command in {"status", "check"}:
                reply = format_status(dict(result.get("status") or {}))
            elif command == "incidents":
                incidents = list(result.get("incidents") or [])
                if not incidents:
                    reply = "最近24小时没有需要播报的故障事件。"
                else:
                    lines = ["最近故障："]
                    for item in incidents[:8]:
                        lines.append(
                            f"• {str(item.get('time') or '')[5:16]} "
                            f"[{item.get('severity')}] {item.get('summary') or item.get('event_type')}"
                        )
                    reply = "\n".join(lines)
            else:
                operations = list(result.get("operations") or [])
                if not operations:
                    reply = "暂无操作记录。"
                else:
                    lines = ["最近操作："]
                    for item in operations[:8]:
                        lines.append(
                            f"• {str(item.get('started_at') or '')[5:16]} "
                            f"{item.get('operation_type')} · {item.get('status')} · {item.get('initiator')}"
                        )
                    reply = "\n".join(lines)
            outcome = "succeeded"
        except GatewayIpcError as exc:
            reply = f"命令未完成：{exc}"
            outcome = "failed"
        self.store.record_command(
            request_id=request_id,
            channel=message.channel,
            sender_id=message.sender_id,
            chat_id=message.chat_id,
            command=command,
            status=outcome,
            reason=reply[:500],
        )
        return CommandReply(reply, command_name=command, outcome=outcome)

    def _request_restart(self, message: InboundMessage, config: MessagingConfig) -> CommandReply:
        remote = config.remote_control
        if not remote.enabled or not remote.qmt_restart_enabled:
            return CommandReply(
                "远程 QMT 重启尚未在本机授权。只读状态和检测仍可使用。",
                command_name="restart_qmt",
                outcome="blocked",
            )
        try:
            status_result = self.client.request(
                "status",
                channel=message.channel,
                sender_id=message.sender_id,
                chat_id=message.chat_id,
                request_id=self._request_id(),
            )
            status_line = format_status(dict(status_result.get("status") or {})).splitlines()[1]
        except (GatewayIpcError, IndexError):
            status_line = "QMT API：当前状态暂不可读取"
        challenge = self.store.create_challenge(
            channel=message.channel,
            sender_id=message.sender_id,
            chat_id=message.chat_id,
            action="restart_qmt",
            ttl_seconds=remote.confirmation_ttl_seconds,
            require_code=message.channel == "weixin",
        )
        self.store.record_command(
            request_id=challenge.request_id,
            channel=message.channel,
            sender_id=message.sender_id,
            chat_id=message.chat_id,
            command="restart_qmt",
            status="awaiting_confirmation",
            reason="waiting for second-factor confirmation",
            completed=False,
        )
        base = (
            "即将执行 QMT 受控重启\n"
            f"{status_line}\n"
            "范围：仅 QMT；不会重启 Quantclass，也不会操作 Fuel/Aqua/Zeus/Rocket。\n"
            f"确认有效期：{remote.confirmation_ttl_seconds} 秒。"
        )
        if message.channel == "telegram":
            buttons = (
                (
                    ("确认重启 QMT", f"qg:restart:confirm:{challenge.challenge_id}"),
                    ("取消", f"qg:restart:cancel:{challenge.challenge_id}"),
                ),
            )
            return CommandReply(base, buttons, "restart_qmt", "awaiting_confirmation")
        return CommandReply(
            f"{base}\n请输入：确认 {challenge.code}",
            command_name="restart_qmt",
            outcome="awaiting_confirmation",
        )

    def _process_callback(self, message: InboundMessage) -> CommandReply:
        config = self._config()
        if message.channel != "telegram" or not self._allowed(config, message):
            return CommandReply("该按钮请求无权执行。", command_name="callback", outcome="blocked")
        match = _CALLBACK_RE.fullmatch(message.callback_data)
        if not match:
            return CommandReply("按钮已失效。", command_name="callback", outcome="blocked")
        verb, challenge_id = match.groups()
        if verb == "cancel":
            changed = self.store.cancel_challenge(
                challenge_id,
                channel=message.channel,
                sender_id=message.sender_id,
            )
            return CommandReply(
                "已取消 QMT 重启。" if changed else "该确认已使用或已失效。",
                command_name="restart_qmt",
                outcome="cancelled" if changed else "blocked",
            )
        challenge = self.store.find_challenge(
            channel=message.channel,
            sender_id=message.sender_id,
            challenge_id=challenge_id,
        )
        if challenge is None:
            return CommandReply("确认请求不存在。", command_name="restart_qmt", outcome="blocked")
        return self._execute_restart(message, challenge.challenge_id, "", challenge.request_id)

    def _confirm_by_code(self, message: InboundMessage, code: str) -> CommandReply:
        challenge = self.store.find_challenge(
            channel=message.channel,
            sender_id=message.sender_id,
            code=code,
        )
        if challenge is None:
            return CommandReply(
                "确认码不存在或已失效。", command_name="restart_qmt", outcome="blocked"
            )
        return self._execute_restart(message, challenge.challenge_id, code, challenge.request_id)

    def _cancel_by_code(self, message: InboundMessage, code: str) -> CommandReply:
        challenge = (
            self.store.find_challenge(
                channel=message.channel,
                sender_id=message.sender_id,
                code=code,
            )
            if code
            else None
        )
        if challenge is None:
            return CommandReply(
                "请提供当前确认码，例如：取消 QG-4821。",
                command_name="restart_qmt",
                outcome="blocked",
            )
        changed = self.store.cancel_challenge(
            challenge.challenge_id,
            channel=message.channel,
            sender_id=message.sender_id,
        )
        return CommandReply(
            "已取消 QMT 重启。" if changed else "确认已使用或已失效。",
            command_name="restart_qmt",
            outcome="cancelled" if changed else "blocked",
        )

    def _execute_restart(
        self,
        message: InboundMessage,
        challenge_id: str,
        code: str,
        request_id: str,
    ) -> CommandReply:
        try:
            result = self.client.request(
                "confirm_restart_qmt",
                channel=message.channel,
                sender_id=message.sender_id,
                chat_id=message.chat_id,
                params={"challenge_id": challenge_id, "code": code},
                request_id=request_id,
            )
            operation_id = str(result.get("operation_id") or "")
            reply = str(result.get("reason") or "QMT受控重启已启动。")
            if operation_id:
                reply += f"\n操作编号：{operation_id}"
            result_status = str(result.get("status") or "accepted")
            outcome = (
                "blocked"
                if result_status == "blocked"
                else "failed" if result_status == "failed" else "succeeded"
            )
        except GatewayIpcError as exc:
            reply = f"QMT重启未执行：{exc}"
            operation_id = ""
            outcome = "blocked"
        self.store.record_command(
            request_id=request_id,
            channel=message.channel,
            sender_id=message.sender_id,
            chat_id=message.chat_id,
            command="restart_qmt",
            status=outcome,
            reason=reply,
            operation_id=operation_id,
        )
        return CommandReply(reply, command_name="restart_qmt", outcome=outcome)


def inbound_text_hash(text: str) -> str:
    return hashlib.sha256(str(text or "").encode("utf-8")).hexdigest()
