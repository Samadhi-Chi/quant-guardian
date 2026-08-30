from __future__ import annotations

import io
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from quant_guardian.gateway.cli import STALE_LOCK_TIME_MS, main
from quant_guardian.gateway.config import MessagingConfig, save_messaging_config
from quant_guardian.gateway.supervisor import GatewaySupervisor


class StopImmediately:
    def set(self) -> None:
        return None

    def wait(self, _seconds: float) -> bool:
        return True


class GatewayProcessTests(unittest.TestCase):
    def test_cli_exits_cleanly_when_gateway_is_disabled_or_config_invalid(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "messaging.json"
            save_messaging_config(MessagingConfig(gateway_enabled=False), path)
            self.assertEqual(main(["--config", str(path)]), 0)
            path.write_text("{", encoding="utf-8")
            with patch("sys.stderr", new_callable=io.StringIO) as stderr:
                self.assertEqual(main(["--config", str(path)]), 2)
            self.assertIn("configuration error", stderr.getvalue())

    def test_cli_uses_single_instance_lock_and_stops_runtime(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "messaging.json"
            save_messaging_config(MessagingConfig(gateway_enabled=True), path)
            lock = MagicMock()
            lock.tryLock.return_value = True
            runtime = MagicMock()
            runtime.store = MagicMock()
            with (
                patch("quant_guardian.gateway.cli.QLockFile", return_value=lock),
                patch("quant_guardian.gateway.cli.GatewayRuntime", return_value=runtime),
                patch("quant_guardian.gateway.cli.threading.Event", return_value=StopImmediately()),
                patch("quant_guardian.gateway.cli.signal.signal"),
            ):
                self.assertEqual(main(["--config", str(path)]), 0)
            runtime.start.assert_called_once()
            runtime.stop.assert_called_once()
            lock.setStaleLockTime.assert_called_with(STALE_LOCK_TIME_MS)
            lock.unlock.assert_called_once()

            lock.tryLock.return_value = False
            with patch("quant_guardian.gateway.cli.QLockFile", return_value=lock):
                self.assertEqual(main(["--config", str(path)]), 0)

    def test_supervisor_source_and_frozen_commands_are_exact(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config = root / "messaging.json"
            supervisor = GatewaySupervisor(config)
            with patch.object(sys, "frozen", False, create=True):
                command = supervisor.command()
            self.assertEqual(command[0], sys.executable)
            self.assertEqual(command[1:3], ["-m", "quant_guardian.gateway.cli"])
            self.assertEqual(command[-1], str(config))

            main_exe = root / "Quant Guardian.exe"
            gateway_exe = root / "Quant Guardian Gateway.exe"
            main_exe.write_bytes(b"main")
            gateway_exe.write_bytes(b"gateway")
            with (
                patch.object(sys, "frozen", True, create=True),
                patch.object(sys, "executable", str(main_exe)),
            ):
                self.assertEqual(supervisor.command()[0], str(gateway_exe))
            gateway_exe.unlink()
            with (
                patch.object(sys, "frozen", True, create=True),
                patch.object(sys, "executable", str(main_exe)),
            ):
                with self.assertRaises(FileNotFoundError):
                    supervisor.command()

    def test_supervisor_starts_hidden_child_without_shell(self) -> None:
        supervisor = GatewaySupervisor(Path("messaging.json"))
        process = SimpleNamespace(pid=4321)
        with (
            patch.object(GatewaySupervisor, "command", return_value=["gateway.exe", "--config", "x"]),
            patch("quant_guardian.gateway.supervisor.subprocess.Popen", return_value=process) as popen,
        ):
            self.assertEqual(supervisor.start(), 4321)
        kwargs = popen.call_args.kwargs
        self.assertTrue(kwargs["close_fds"])
        self.assertNotIn("shell", kwargs)


if __name__ == "__main__":
    unittest.main()
