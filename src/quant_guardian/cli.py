from __future__ import annotations

import argparse
import json
import signal
import sys
import tempfile
import time
from pathlib import Path

from quant_guardian import __version__
from quant_guardian.config import default_config_path, load_config, save_config
from quant_guardian.service import GuardianService
from quant_guardian.simulation import run_simulation


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="quant-guardian",
        description="Safety-first QMT health monitor and controlled recovery",
    )
    parser.add_argument(
        "--config",
        type=Path,
        default=default_config_path(),
        help="Path to the JSON configuration file",
    )
    parser.add_argument("--headless", action="store_true", help="Run without Qt")
    parser.add_argument("--once", action="store_true", help="Run one health check")
    parser.add_argument(
        "--simulate",
        action="store_true",
        help="Run an offline state-machine demonstration",
    )
    parser.add_argument("--ui-smoke", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--version", action="version", version=__version__)
    return parser


def _load_or_create(path: Path):
    if not path.exists():
        config = load_config(None)
        save_config(config, path)
        return config
    return load_config(path)


def run_headless(service: GuardianService, once: bool) -> int:
    if once:
        print(json.dumps(service.run_once().to_dict(), ensure_ascii=False, indent=2))
        service.stop()
        return 0

    stopping = False

    def request_stop(*_args) -> None:
        nonlocal stopping
        stopping = True

    signal.signal(signal.SIGINT, request_stop)
    if hasattr(signal, "SIGTERM"):
        signal.signal(signal.SIGTERM, request_stop)
    service.start()
    last_time = None
    try:
        while not stopping:
            status = service.status
            if status.observed_at != last_time:
                print(json.dumps(status.to_dict(), ensure_ascii=False), flush=True)
                last_time = status.observed_at
            time.sleep(0.5)
    finally:
        service.stop()
    return 0


def _run_desktop(
    args: argparse.Namespace,
    config,
    *,
    first_run: bool,
    runtime_root: Path | None = None,
) -> int:
    try:
        from quant_guardian.ui.app import run_gui
    except ImportError as exc:
        print(
            "PySide6 is required for the desktop UI. "
            "Run scripts/bootstrap.ps1 with Python 3.11 first. "
            f"Details: {exc}",
            file=sys.stderr,
        )
        return 3
    return run_gui(
        config,
        args.config,
        start_monitoring=not args.ui_smoke,
        start_gateway=not args.ui_smoke,
        auto_quit_ms=800 if args.ui_smoke else None,
        show_onboarding=first_run and not args.ui_smoke,
        runtime_root=runtime_root,
    )


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.simulate:
        print(json.dumps(run_simulation(), ensure_ascii=False, indent=2))
        return 0

    if args.ui_smoke:
        with tempfile.TemporaryDirectory(prefix="quant-guardian-ui-smoke-") as directory:
            runtime_root = Path(directory)
            args.config = runtime_root / "config" / "config.json"
            config = _load_or_create(args.config)
            return _run_desktop(
                args,
                config,
                first_run=True,
                runtime_root=runtime_root,
            )

    first_run = not args.config.exists()
    try:
        config = _load_or_create(args.config)
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        print(f"Configuration error: {exc}", file=sys.stderr)
        return 2

    if args.headless or args.once:
        return run_headless(GuardianService(config), args.once)

    return _run_desktop(args, config, first_run=first_run)


if __name__ == "__main__":
    raise SystemExit(main())
