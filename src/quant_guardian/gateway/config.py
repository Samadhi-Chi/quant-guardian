from __future__ import annotations

import json
import urllib.parse
from dataclasses import asdict, dataclass, field, fields, is_dataclass
from pathlib import Path
from typing import Any

from quant_guardian.config import app_data_dir


def is_trusted_weixin_base_url(value: str) -> bool:
    try:
        parsed = urllib.parse.urlsplit(str(value).strip())
        port = parsed.port
    except ValueError:
        return False
    host = (parsed.hostname or "").casefold()
    return bool(
        parsed.scheme.casefold() == "https"
        and host.endswith(".weixin.qq.com")
        and not parsed.username
        and not parsed.password
        and port in {None, 443}
        and parsed.path in {"", "/"}
        and not parsed.query
        and not parsed.fragment
    )


@dataclass(slots=True)
class TelegramGatewayConfig:
    enabled: bool = False
    allowed_user_ids: list[str] = field(default_factory=list)
    home_chat_id: str = ""
    poll_timeout_seconds: int = 25


@dataclass(slots=True)
class WeixinGatewayConfig:
    enabled: bool = False
    account_id: str = ""
    base_url: str = "https://ilinkai.weixin.qq.com"
    allowed_user_ids: list[str] = field(default_factory=list)
    home_chat_id: str = ""
    poll_timeout_seconds: int = 35
    group_enabled: bool = False


@dataclass(slots=True)
class BroadcastConfig:
    enabled: bool = True
    minimum_severity: str = "warning"
    health_events: bool = True
    recovery_events: bool = True
    operation_events: bool = True
    guardian_events: bool = True
    include_healthy_recovery: bool = True


@dataclass(slots=True)
class RemoteControlConfig:
    enabled: bool = False
    allow_status: bool = True
    allow_check: bool = True
    allow_incidents: bool = True
    allow_operations: bool = True
    qmt_restart_enabled: bool = True
    quantclass_restart_enabled: bool = False
    confirmation_ttl_seconds: int = 60
    pairing_ttl_seconds: int = 300
    max_commands_per_minute: int = 8
    max_restart_requests_per_hour: int = 2


@dataclass(slots=True)
class MessagingConfig:
    schema_version: int = 1
    gateway_enabled: bool = False
    autostart: bool = True
    telegram: TelegramGatewayConfig = field(default_factory=TelegramGatewayConfig)
    weixin: WeixinGatewayConfig = field(default_factory=WeixinGatewayConfig)
    broadcast: BroadcastConfig = field(default_factory=BroadcastConfig)
    remote_control: RemoteControlConfig = field(default_factory=RemoteControlConfig)

    def validate(self) -> list[str]:
        errors: list[str] = []
        if self.schema_version != 1:
            errors.append(f"unsupported messaging schema_version: {self.schema_version}")
        if self.broadcast.minimum_severity not in {"info", "warning", "critical"}:
            errors.append("minimum_severity must be info, warning, or critical")
        if not 5 <= self.telegram.poll_timeout_seconds <= 50:
            errors.append("telegram poll_timeout_seconds must be between 5 and 50")
        if not 5 <= self.weixin.poll_timeout_seconds <= 50:
            errors.append("weixin poll_timeout_seconds must be between 5 and 50")
        if not is_trusted_weixin_base_url(self.weixin.base_url):
            errors.append("weixin.base_url must be an HTTPS weixin.qq.com endpoint")
        if self.weixin.group_enabled:
            errors.append("personal WeChat group control is permanently disabled")
        if self.remote_control.quantclass_restart_enabled:
            errors.append("remote Quantclass restart is not supported")
        if not 30 <= self.remote_control.confirmation_ttl_seconds <= 300:
            errors.append("confirmation_ttl_seconds must be between 30 and 300")
        if not 60 <= self.remote_control.pairing_ttl_seconds <= 900:
            errors.append("pairing_ttl_seconds must be between 60 and 900")
        if not 1 <= self.remote_control.max_commands_per_minute <= 60:
            errors.append("max_commands_per_minute must be between 1 and 60")
        if not 1 <= self.remote_control.max_restart_requests_per_hour <= 10:
            errors.append("max_restart_requests_per_hour must be between 1 and 10")
        for name, values in (
            ("telegram.allowed_user_ids", self.telegram.allowed_user_ids),
            ("weixin.allowed_user_ids", self.weixin.allowed_user_ids),
        ):
            if any(not str(value).strip() for value in values):
                errors.append(f"{name} cannot contain blank values")
            if len({str(value).strip() for value in values if str(value).strip()}) > 1:
                errors.append(f"{name} supports exactly one private owner")
        for name, channel in (
            ("telegram", self.telegram),
            ("weixin", self.weixin),
        ):
            allowed = {str(value).strip() for value in channel.allowed_user_ids}
            home = str(channel.home_chat_id).strip()
            if bool(allowed) != bool(home) or (home and home not in allowed):
                errors.append(f"{name} owner and home_chat_id must identify the same private chat")
        return errors


def default_messaging_config_path() -> Path:
    return app_data_dir() / "config" / "messaging.json"


def gateway_database_path() -> Path:
    return app_data_dir() / "state" / "gateway.db"


def gateway_secret_path() -> Path:
    return app_data_dir() / "secrets" / "messaging-secrets.json"


def remote_control_sentinel_path() -> Path:
    return app_data_dir() / "state" / "REMOTE_CONTROL_ENABLED"


REMOTE_CONTROL_SENTINEL_CONTENT = "QUANT_GUARDIAN_REMOTE_CONTROL_V1\n"


def _merge(instance: Any, document: dict[str, Any]) -> None:
    known = {item.name for item in fields(instance)}
    for key, value in document.items():
        if key not in known:
            continue
        current = getattr(instance, key)
        if is_dataclass(current) and isinstance(value, dict):
            _merge(current, value)
        elif isinstance(current, list) and isinstance(value, list):
            setattr(instance, key, [str(item).strip() for item in value if str(item).strip()])
        else:
            setattr(instance, key, value)


def load_messaging_config(path: Path | None = None) -> MessagingConfig:
    target = path or default_messaging_config_path()
    config = MessagingConfig()
    if target.exists():
        raw = json.loads(target.read_text(encoding="utf-8-sig"))
        if not isinstance(raw, dict):
            raise ValueError("messaging configuration root must be an object")
        _merge(config, raw)
    config.weixin.group_enabled = False
    config.remote_control.quantclass_restart_enabled = False
    errors = config.validate()
    if errors:
        raise ValueError("invalid messaging configuration: " + "; ".join(errors))
    return config


def save_messaging_config(
    config: MessagingConfig,
    path: Path | None = None,
) -> Path:
    config.schema_version = 1
    config.weixin.group_enabled = False
    config.remote_control.quantclass_restart_enabled = False
    config.telegram.allowed_user_ids = sorted(
        {str(value).strip() for value in config.telegram.allowed_user_ids if str(value).strip()}
    )
    config.weixin.allowed_user_ids = sorted(
        {str(value).strip() for value in config.weixin.allowed_user_ids if str(value).strip()}
    )
    errors = config.validate()
    if errors:
        raise ValueError("invalid messaging configuration: " + "; ".join(errors))
    target = path or default_messaging_config_path()
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_suffix(target.suffix + ".tmp")
    temporary.write_text(
        json.dumps(asdict(config), ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    temporary.replace(target)
    return target


def set_remote_control_authorized(enabled: bool, path: Path | None = None) -> Path:
    target = path or remote_control_sentinel_path()
    target.parent.mkdir(parents=True, exist_ok=True)
    if enabled:
        temporary = target.with_suffix(".tmp")
        temporary.write_text(REMOTE_CONTROL_SENTINEL_CONTENT, encoding="utf-8")
        temporary.replace(target)
    elif target.exists():
        target.unlink()
    return target


def remote_control_authorized(path: Path | None = None) -> tuple[bool, str]:
    target = path or remote_control_sentinel_path()
    if not target.exists():
        return False, "本机远程控制授权文件不存在"
    try:
        content = target.read_text(encoding="utf-8")
    except OSError as exc:
        return False, f"无法读取远程控制授权文件：{exc}"
    if content != REMOTE_CONTROL_SENTINEL_CONTENT:
        return False, "本机远程控制授权文件内容无效"
    return True, "本机远程控制授权已启用"
