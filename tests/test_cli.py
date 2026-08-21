from __future__ import annotations

import unittest
from pathlib import Path
from unittest.mock import patch

from quant_guardian.cli import main


class CliTests(unittest.TestCase):
    def test_ui_smoke_does_not_start_the_messaging_gateway(self) -> None:
        with patch("quant_guardian.ui.app.run_gui", return_value=0) as run_gui:
            self.assertEqual(main(["--ui-smoke"]), 0)

        self.assertFalse(run_gui.call_args.kwargs["start_monitoring"])
        self.assertFalse(run_gui.call_args.kwargs["start_gateway"])
        self.assertEqual(run_gui.call_args.kwargs["auto_quit_ms"], 800)

    def test_ui_smoke_uses_a_temporary_runtime_and_removes_it_afterward(self) -> None:
        observed: dict[str, Path] = {}

        def run_desktop(args, _config, *, first_run, runtime_root):
            observed["runtime_root"] = runtime_root
            observed["config"] = args.config
            self.assertTrue(first_run)
            self.assertTrue(runtime_root.is_dir())
            self.assertTrue(args.config.is_file())
            self.assertEqual(args.config, runtime_root / "config" / "config.json")
            return 0

        with patch("quant_guardian.cli._run_desktop", side_effect=run_desktop):
            self.assertEqual(main(["--ui-smoke"]), 0)

        self.assertFalse(observed["runtime_root"].exists())
        self.assertFalse(observed["config"].exists())


if __name__ == "__main__":
    unittest.main()
