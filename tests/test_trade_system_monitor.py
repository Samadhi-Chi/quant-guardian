from __future__ import annotations

import json
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import patch

from quant_guardian.config import TradeSystemConfig
from quant_guardian.domain.components import ComponentState
from quant_guardian.monitors.rocket_monitor import RocketObservation
from quant_guardian.monitors.trade_system_monitor import TradeSystemMonitor


class TradeSystemMonitorTests(unittest.TestCase):
    def make_config(self, root: Path) -> TradeSystemConfig:
        return TradeSystemConfig(
            data_root=str(root),
            fuel_status_file="fuel/status.json",
            fuel_update_file="fuel/update.json",
            aqua_log_file="logs/aqua.log",
            zeus_log_file="logs/zeus.log",
            rocket_log_directory="logs/rocket",
            data_overdue_grace_seconds=60,
        )

    @staticmethod
    def write_fuel(root: Path, now: datetime) -> None:
        (root / "fuel").mkdir(parents=True)
        (root / "fuel" / "status.json").write_text(
            json.dumps(
                {
                    "stock-price": {
                        "isListed": 1,
                        "canAutoUpdate": 1,
                        "lastUpdateTime": (now - timedelta(minutes=1)).strftime("%Y-%m-%d %H:%M:%S"),
                        "nextUpdateTime": (now + timedelta(minutes=5)).strftime("%Y-%m-%d %H:%M:%S"),
                        "lastErrTime": None,
                    }
                }
            ),
            encoding="utf-8",
        )
        (root / "fuel" / "update.json").write_text(
            json.dumps({now.strftime("%Y-%m-%d %H:%M:%S"): ["stock-price"]}),
            encoding="utf-8",
        )

    def test_zeus_error_followed_by_success_exit_remains_critical(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            now = datetime.now().astimezone().replace(microsecond=0)
            self.write_fuel(root, now)
            (root / "logs").mkdir()
            (root / "logs" / "aqua.log").write_text(
                f"{now:%Y-%m-%d %H:%M:%S} - [aqua] pid 12 start\n"
                f"{now:%Y-%m-%d %H:%M:%S} - [aqua] pid 12 exit successfully\n",
                encoding="utf-8",
            )
            (root / "logs" / "zeus.log").write_text(
                f"{now:%Y-%m-%d %H:%M:%S} - [zeus] pid 34 start\n"
                f"{now:%Y-%m-%d %H:%M:%S} - [ERROR] ValueError: Usecols do not match columns\n"
                f"{now:%Y-%m-%d %H:%M:%S} - [zeus] pid 34 exit successfully\n",
                encoding="utf-8",
            )
            with patch.object(TradeSystemMonitor, "_active_processes", return_value=[]):
                observation = TradeSystemMonitor(self.make_config(root)).observe(
                    now,
                    rocket=RocketObservation(False, False, "Rocket空闲"),
                    active_window=False,
                )
            zeus = observation.selection.children[1]
            self.assertEqual(zeus.state, ComponentState.CRITICAL)
            self.assertIn("Usecols", zeus.reason)
            self.assertEqual(observation.selection.state, ComponentState.CRITICAL)
            self.assertEqual(observation.selection.metrics["engine"], "Zeus")
            self.assertEqual(observation.order.state, ComponentState.IDLE)

    def test_half_written_fuel_json_keeps_last_valid_result(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            now = datetime.now().astimezone().replace(microsecond=0)
            self.write_fuel(root, now)
            monitor = TradeSystemMonitor(self.make_config(root))
            rocket = RocketObservation(False, False, "Rocket空闲")
            with patch.object(TradeSystemMonitor, "_active_processes", return_value=[]):
                first = monitor.observe(now, rocket=rocket, active_window=False)
                (root / "fuel" / "status.json").write_text("{", encoding="utf-8")
                second = monitor.observe(now + timedelta(seconds=5), rocket=rocket, active_window=False)
            self.assertEqual(first.data.state, ComponentState.HEALTHY)
            self.assertEqual(second.data.state, ComponentState.HEALTHY)
            self.assertEqual(second.data.metrics["products"], 1)

    def test_batch_process_absence_is_idle_not_failure(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            now = datetime.now().astimezone().replace(microsecond=0)
            self.write_fuel(root, now)
            with patch.object(TradeSystemMonitor, "_active_processes", return_value=[]):
                observation = TradeSystemMonitor(self.make_config(root)).observe(
                    now,
                    rocket=RocketObservation(False, False, "Rocket空闲"),
                    active_window=False,
                )
            self.assertEqual(observation.selection.state, ComponentState.IDLE)
            self.assertEqual(observation.selection.children[0].state, ComponentState.IDLE)
            self.assertEqual(observation.selection.children[1].state, ComponentState.IDLE)
            self.assertEqual(observation.order.state, ComponentState.IDLE)
            self.assertEqual(observation.order.metrics["engine"], "Rocket")
            self.assertFalse(observation.order.children)

    def test_missing_rocket_warns_only_during_order_execution_phase(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            now = datetime.now().astimezone().replace(microsecond=0)
            self.write_fuel(root, now)
            monitor = TradeSystemMonitor(self.make_config(root))
            rocket = RocketObservation(False, False, "Rocket空闲")
            with patch.object(TradeSystemMonitor, "_active_processes", return_value=[]):
                active = monitor.observe(
                    now, rocket=rocket, active_window=True
                )
                inactive = monitor.observe(
                    now + timedelta(minutes=1),
                    rocket=rocket,
                    active_window=False,
                )
            self.assertEqual(active.order.state, ComponentState.WARNING)
            self.assertIn("下单时段", active.order.reason)
            self.assertEqual(inactive.order.state, ComponentState.IDLE)
            self.assertIn("无需运行", inactive.order.reason)

    def test_overdue_fuel_is_healthy_while_status_file_is_progressing(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            now = datetime.now().astimezone().replace(microsecond=0)
            (root / "fuel").mkdir(parents=True)
            (root / "fuel" / "status.json").write_text(
                json.dumps(
                    {
                        "stock-price": {
                            "isListed": 1,
                            "canAutoUpdate": 1,
                            "lastUpdateTime": (
                                now - timedelta(minutes=20)
                            ).strftime("%Y-%m-%d %H:%M:%S"),
                            "nextUpdateTime": (
                                now - timedelta(minutes=10)
                            ).strftime("%Y-%m-%d %H:%M:%S"),
                            "lastErrTime": None,
                        }
                    }
                ),
                encoding="utf-8",
            )
            (root / "fuel" / "update.json").write_text("{}", encoding="utf-8")
            with patch.object(
                TradeSystemMonitor,
                "_active_processes",
                side_effect=lambda names: (
                    [{"pid": 77, "name": "fuel.exe"}]
                    if "fuel.exe" in names
                    else []
                ),
            ):
                observation = TradeSystemMonitor(self.make_config(root)).observe(
                    now,
                    rocket=RocketObservation(False, False, "Rocket空闲"),
                    active_window=False,
                )
            self.assertEqual(observation.data.state, ComponentState.HEALTHY)
            self.assertTrue(observation.data.metrics["progress_fresh"])
            self.assertIn("正在追赶", observation.data.reason)

    def test_aqua_can_be_selected_without_zeus_failure_poisoning_parent(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            now = datetime.now().astimezone().replace(microsecond=0)
            self.write_fuel(root, now)
            (root / "logs").mkdir()
            (root / "logs" / "aqua.log").write_text(
                f"{now:%Y-%m-%d %H:%M:%S} - [aqua] pid 12 start\n"
                f"{now:%Y-%m-%d %H:%M:%S} - [aqua] pid 12 exit successfully\n",
                encoding="utf-8",
            )
            (root / "logs" / "zeus.log").write_text(
                f"{now:%Y-%m-%d %H:%M:%S} - [zeus] pid 34 start\n"
                f"{now:%Y-%m-%d %H:%M:%S} - [ERROR] ValueError: Usecols do not match columns\n"
                f"{now:%Y-%m-%d %H:%M:%S} - [zeus] pid 34 exit successfully\n",
                encoding="utf-8",
            )
            config = self.make_config(root)
            config.selection_engine = "aqua"
            with patch.object(TradeSystemMonitor, "_active_processes", return_value=[]):
                observation = TradeSystemMonitor(config).observe(
                    now,
                    rocket=RocketObservation(False, False, "Rocket空闲"),
                    active_window=False,
                )
            self.assertEqual(observation.selection.metrics["engine"], "Aqua")
            self.assertEqual(observation.selection.state, ComponentState.IDLE)
            self.assertEqual(observation.selection.children[1].state, ComponentState.CRITICAL)
            self.assertFalse(observation.selection.children[1].metrics["selected"])
            self.assertEqual(observation.node.state, ComponentState.HEALTHY)


if __name__ == "__main__":
    unittest.main()
