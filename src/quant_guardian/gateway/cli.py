from __future__ import annotations

import argparse
import signal
import sys
import threading
from datetime import datetime
from pathlib import Path

from PySide6.QtCore import QLockFile

from quant_guardian import __version__
from quant_guardian.config import app_data_dir
from quant_guardian.gateway.config import (
    default_messaging_config_path,
    load_messaging_config,
)
from quant_guardian.gateway.runtime import GatewayRuntime

STALE_LOCK_TIME_MS = 60_000


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="quant-guardian-gateway",
        description="Isolated Telegram and personal WeChat gateway for Quant Guardian",
    )
    parser.add_argument("--config", type=Path, default=default_messaging_config_path())
    parser.add_argument("--version", action="version", version=__version__)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        config = load_messaging_config(args.config)
    except (OSError, ValueError) as exc:
        print(f"Messaging configuration error: {exc}", file=sys.stderr)
        return 2
    if not config.gateway_enabled:
        return 0

    lock_path = app_data_dir() / "state" / "quant-guardian-gateway.lock"
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    lock = QLockFile(str(lock_path))
    lock.setStaleLockTime(STALE_LOCK_TIME_MS)
    if not lock.tryLock(100):
        return 0

    runtime = GatewayRuntime(config_path=args.config)
    stopping = threading.Event()

    def request_stop(*_args: object) -> None:
        stopping.set()

    signal.signal(signal.SIGINT, request_stop)
    if hasattr(signal, "SIGTERM"):
        signal.signal(signal.SIGTERM, request_stop)
    runtime.start()
    try:
        while not stopping.wait(2):
            runtime.sync_config()
            runtime.store.set_meta(
                "gateway.heartbeat", datetime.now().astimezone().isoformat()
            )
    finally:
        runtime.stop()
        lock.unlock()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
