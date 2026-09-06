from __future__ import annotations

import json
import os
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
            fusion_log_file="logs/fusion.log",
            selection_status_directory="status",
            rocket_log_directory="logs/rocket",
            data_overdue_grace_seconds=60,
            data_stall_confirmation_seconds=300,
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

    @staticmethod
    def selection_child(observation, engine: str):
        return next(
            child
            for child in observation.selection.children
            if child.metrics.get("engine") == engine
        )

    @staticmethod
    def write_task_status(
        root: Path,
        engine: str,
        *,
        started_at: datetime,
        ended_at: datetime | None,
        running: bool,
    ) -> Path:
        status_directory = root / "status"
        status_directory.mkdir(parents=True, exist_ok=True)
        path = status_directory / f"{engine}-stats-{started_at:%Y-%m-%d}.json"
        path.write_text(
            json.dumps(
                {
                    "start_time": started_at.strftime("%Y-%m-%d %H:%M:%S"),
                    "end_time": (
                        ended_at.strftime("%Y-%m-%d %H:%M:%S")
                        if ended_at
                        else None
                    ),
                    "is_running": running,
                    "command": "select",
                    "stats": [
                        {
                            "tag": "SELECT_CLOSE",
                            "time": [
                                started_at.strftime("%Y-%m-%d %H:%M:%S"),
                                (
                                    ended_at.strftime("%Y-%m-%d %H:%M:%S")
                                    if ended_at
                                    else None
                                ),
                            ],
                            "messages": [],
                        }
                    ],
                }
            ),
            encoding="utf-8",
        )
        return path

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
            zeus = self.selection_child(observation, "Zeus")
            self.assertEqual(zeus.state, ComponentState.CRITICAL)
            self.assertIn("Usecols", zeus.reason)
            self.assertEqual(observation.selection.state, ComponentState.CRITICAL)
            self.assertEqual(observation.selection.metrics["engine"], "Zeus")
            self.assertEqual(observation.order.state, ComponentState.IDLE)

    def test_auto_detects_running_fusion_over_stale_zeus(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            now = datetime.now().astimezone().replace(microsecond=0)
            self.write_fuel(root, now)
            (root / "logs").mkdir()
            (root / "logs" / "zeus.log").write_text(
                f"{now - timedelta(hours=2):%Y-%m-%d %H:%M:%S} - "
                "[zeus] pid 12 exit successfully\n",
                encoding="utf-8",
            )
            (root / "logs" / "fusion.log").write_text(
                f"{now:%Y-%m-%d %H:%M:%S} - [fusion] pid 34 start\n",
                encoding="utf-8",
            )
            self.write_task_status(
                root,
                "fusion",
                started_at=now,
                ended_at=None,
                running=True,
            )
            config = self.make_config(root)
            config.selection_engine = "auto"

            def active_processes(names: list[str]) -> list[dict[str, object]]:
                if "fusion.exe" in names:
                    return [{"pid": 34, "name": "fusion.exe", "command": "select trading"}]
                return []

            with patch.object(
                TradeSystemMonitor,
                "_active_processes",
                side_effect=active_processes,
            ):
                observation = TradeSystemMonitor(config).observe(
                    now,
                    rocket=RocketObservation(False, False, "Rocket空闲"),
                    active_window=False,
                )
            fusion = self.selection_child(observation, "Fusion")
            zeus = self.selection_child(observation, "Zeus")
            self.assertEqual(observation.selection.state, ComponentState.HEALTHY)
            self.assertEqual(observation.selection.metrics["engine"], "Fusion")
            self.assertEqual(
                observation.selection.metrics["detection_source"],
                "running_process",
            )
            self.assertTrue(fusion.metrics["selected"])
            self.assertFalse(zeus.metrics["selected"])

    def test_auto_detects_completed_fusion_from_status_file(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            now = datetime.now().astimezone().replace(microsecond=0)
            self.write_fuel(root, now)
            ended_at = now - timedelta(minutes=1)
            self.write_task_status(
                root,
                "fusion",
                started_at=now - timedelta(minutes=12),
                ended_at=ended_at,
                running=False,
            )
            config = self.make_config(root)
            config.selection_engine = "auto"
            with patch.object(TradeSystemMonitor, "_active_processes", return_value=[]):
                observation = TradeSystemMonitor(config).observe(
                    now,
                    rocket=RocketObservation(False, False, "Rocket空闲"),
                    active_window=False,
                )
            fusion = self.selection_child(observation, "Fusion")
            self.assertEqual(observation.selection.state, ComponentState.IDLE)
            self.assertEqual(observation.selection.metrics["engine"], "Fusion")
            self.assertEqual(fusion.metrics["last_result"], "success")
            self.assertEqual(
                observation.selection.metrics["detection_source"],
                "latest_status_or_log",
            )

    def test_half_written_fusion_status_keeps_last_valid_result(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            now = datetime.now().astimezone().replace(microsecond=0)
            self.write_fuel(root, now)
            status_path = self.write_task_status(
                root,
                "fusion",
                started_at=now - timedelta(minutes=10),
                ended_at=now - timedelta(minutes=1),
                running=False,
            )
            config = self.make_config(root)
            config.selection_engine = "auto"
            monitor = TradeSystemMonitor(config)
            rocket = RocketObservation(False, False, "Rocket空闲")
            with patch.object(TradeSystemMonitor, "_active_processes", return_value=[]):
                first = monitor.observe(now, rocket=rocket, active_window=False)
                status_path.write_text("{", encoding="utf-8")
                second = monitor.observe(
                    now + timedelta(seconds=5),
                    rocket=rocket,
                    active_window=False,
                )
            self.assertEqual(first.selection.metrics["engine"], "Fusion")
            self.assertEqual(second.selection.metrics["engine"], "Fusion")
            self.assertEqual(
                self.selection_child(second, "Fusion").metrics["last_result"],
                "success",
            )

    def test_recent_running_status_without_process_is_warning(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            now = datetime.now().astimezone().replace(microsecond=0)
            self.write_fuel(root, now)
            self.write_task_status(
                root,
                "fusion",
                started_at=now - timedelta(minutes=10),
                ended_at=None,
                running=True,
            )
            config = self.make_config(root)
            config.selection_engine = "auto"
            with patch.object(TradeSystemMonitor, "_active_processes", return_value=[]):
                observation = TradeSystemMonitor(config).observe(
                    now,
                    rocket=RocketObservation(False, False, "Rocket空闲"),
                    active_window=False,
                )
            self.assertEqual(observation.selection.state, ComponentState.WARNING)
            self.assertIn("未检测到对应进程", observation.selection.reason)

    def test_explicit_legacy_engine_overrides_fusion_auto_detection(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            now = datetime.now().astimezone().replace(microsecond=0)
            self.write_fuel(root, now)
            (root / "logs").mkdir()
            (root / "logs" / "fusion.log").write_text(
                f"{now:%Y-%m-%d %H:%M:%S} - [fusion] pid 34 start\n",
                encoding="utf-8",
            )
            config = self.make_config(root)
            config.selection_engine = "zeus"

            def active_processes(names: list[str]) -> list[dict[str, object]]:
                if "fusion.exe" in names:
                    return [{"pid": 34, "name": "fusion.exe", "command": "select trading"}]
                return []

            with patch.object(
                TradeSystemMonitor,
                "_active_processes",
                side_effect=active_processes,
            ):
                observation = TradeSystemMonitor(config).observe(
                    now,
                    rocket=RocketObservation(False, False, "Rocket空闲"),
                    active_window=False,
                )
            self.assertEqual(observation.selection.metrics["engine"], "Zeus")
            self.assertEqual(
                observation.selection.metrics["detection_source"], "configured"
            )
            self.assertFalse(self.selection_child(observation, "Fusion").metrics["selected"])

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
            self.assertTrue(
                all(
                    child.state is ComponentState.IDLE
                    for child in observation.selection.children
                )
            )
            self.assertEqual(len(observation.selection.children), 3)
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

    def test_min_data_process_is_not_treated_as_full_data_update(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            now = datetime.now().astimezone().replace(microsecond=0)
            self.write_fuel(root, now)
            with patch.object(
                TradeSystemMonitor,
                "_active_processes",
                side_effect=lambda names: (
                    [
                        {
                            "pid": 77,
                            "name": "fuel.exe",
                            "command": "min_data",
                        }
                    ]
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
            self.assertEqual(observation.data.metrics["active_processes"], [])
            self.assertEqual(
                observation.data.metrics["all_fuel_processes"][0]["command"],
                "min_data",
            )
            self.assertIn("其他任务", observation.data.reason)

    def test_fuel_stall_warns_only_after_confirmation_window(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            now = datetime.now().astimezone().replace(microsecond=0)
            (root / "fuel").mkdir(parents=True)
            status_path = root / "fuel" / "status.json"
            update_path = root / "fuel" / "update.json"

            def write_status(next_update: datetime) -> None:
                status_path.write_text(
                    json.dumps(
                        {
                            "stock-price": {
                                "isListed": 1,
                                "canAutoUpdate": 1,
                                "lastUpdateTime": (
                                    now - timedelta(minutes=20)
                                ).strftime("%Y-%m-%d %H:%M:%S"),
                                "nextUpdateTime": next_update.strftime(
                                    "%Y-%m-%d %H:%M:%S"
                                ),
                                "lastErrTime": None,
                            }
                        }
                    ),
                    encoding="utf-8",
                )
                old_timestamp = (now - timedelta(minutes=10)).timestamp()
                os.utime(status_path, (old_timestamp, old_timestamp))

            update_path.write_text("{}", encoding="utf-8")
            monitor = TradeSystemMonitor(self.make_config(root))
            rocket = RocketObservation(False, False, "Rocket空闲")
            with patch.object(TradeSystemMonitor, "_active_processes", return_value=[]):
                write_status(now - timedelta(minutes=2))
                confirming = monitor.observe(
                    now, rocket=rocket, active_window=False
                )
                write_status(now - timedelta(minutes=10))
                stalled = monitor.observe(
                    now, rocket=rocket, active_window=False
                )
            self.assertEqual(confirming.data.state, ComponentState.HEALTHY)
            self.assertFalse(confirming.data.metrics["stalled_products"])
            self.assertIn("等待更新确认", confirming.data.reason)
            self.assertEqual(stalled.data.state, ComponentState.WARNING)
            self.assertTrue(stalled.data.metrics["stalled_products"])
            self.assertIn("超过确认窗口", stalled.data.reason)

    def test_scheduled_fuel_pause_uses_fresh_minute_data_as_health_evidence(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            now = datetime.now().astimezone().replace(
                hour=13, minute=30, second=0, microsecond=0
            )
            (root / "fuel" / "log").mkdir(parents=True)
            status_path = root / "fuel" / "status.json"
            status_path.write_text(
                json.dumps(
                    {
                        "stock-price": {
                            "isListed": 1,
                            "canAutoUpdate": 1,
                            "lastUpdateTime": (now - timedelta(minutes=40)).strftime(
                                "%Y-%m-%d %H:%M:%S"
                            ),
                            "nextUpdateTime": (now - timedelta(minutes=30)).strftime(
                                "%Y-%m-%d %H:%M:%S"
                            ),
                            "lastErrTime": None,
                        }
                    }
                ),
                encoding="utf-8",
            )
            old_timestamp = (now - timedelta(minutes=30)).timestamp()
            os.utime(status_path, (old_timestamp, old_timestamp))
            (root / "fuel" / "update.json").write_text("{}", encoding="utf-8")
            log_path = root / "fuel" / "log" / f"{now:%Y-%m-%d}_日志.log"
            log_path.write_text(
                f"INFO:root:{now - timedelta(minutes=6):%H:%M:%S} --> "
                "[command in]: C:\\data\\fuel\\fuel.exe min_data\n"
                f"INFO:root:{now - timedelta(minutes=5):%H:%M:%S} --> "
                "[加速数据源] 本轮完成，成功 1/1 个 hm\n"
                f"INFO:root:{now - timedelta(seconds=8):%H:%M:%S} --> "
                "[command in]: C:\\data\\fuel\\fuel.exe all_data\n"
                f"INFO:root:{now - timedelta(seconds=8):%H:%M:%S} --> "
                "在交易时间，不再更新数据。如需更新，可手动增量更新。\n",
                encoding="utf-8",
            )
            config = self.make_config(root)
            config.fuel_log_directory = "fuel/log"
            monitor = TradeSystemMonitor(config)
            rocket = RocketObservation(False, False, "Rocket空闲")
            with patch.object(TradeSystemMonitor, "_active_processes", return_value=[]):
                paused = monitor.observe(now, rocket=rocket, active_window=True)
                with log_path.open("a", encoding="utf-8") as stream:
                    stream.write(
                        f"INFO:root:{now + timedelta(minutes=1):%H:%M:%S} --> "
                        "[command in]: C:\\data\\fuel\\fuel.exe all_data\n"
                    )
                resumed = monitor.observe(
                    now + timedelta(minutes=1), rocket=rocket, active_window=True
                )
            self.assertEqual(paused.data.state, ComponentState.HEALTHY)
            self.assertTrue(paused.data.metrics["full_update_paused"])
            self.assertTrue(paused.data.metrics["min_data_fresh"])
            self.assertTrue(paused.data.metrics["stalled_products"])
            self.assertIn("分钟数据正常", paused.data.reason)
            self.assertEqual(resumed.data.state, ComponentState.WARNING)
            self.assertFalse(resumed.data.metrics["full_update_paused"])

    def test_scheduled_fuel_pause_warns_when_minute_data_heartbeat_is_stale(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            now = datetime.now().astimezone().replace(
                hour=13, minute=30, second=0, microsecond=0
            )
            self.write_fuel(root, now)
            (root / "fuel" / "log").mkdir()
            (root / "fuel" / "log" / f"{now:%Y-%m-%d}_日志.log").write_text(
                f"INFO:root:{now - timedelta(minutes=20):%H:%M:%S} --> "
                "[command in]: C:\\data\\fuel\\fuel.exe min_data\n"
                f"INFO:root:{now - timedelta(minutes=19):%H:%M:%S} --> "
                "[加速数据源] 本轮完成，成功 1/1 个 hm\n"
                f"INFO:root:{now - timedelta(seconds=5):%H:%M:%S} --> "
                "[command in]: C:\\data\\fuel\\fuel.exe all_data\n"
                f"INFO:root:{now - timedelta(seconds=5):%H:%M:%S} --> "
                "在交易时间，不再更新数据。\n",
                encoding="utf-8",
            )
            config = self.make_config(root)
            config.fuel_log_directory = "fuel/log"
            with patch.object(TradeSystemMonitor, "_active_processes", return_value=[]):
                observation = TradeSystemMonitor(config).observe(
                    now,
                    rocket=RocketObservation(False, False, "Rocket空闲"),
                    active_window=True,
                )
            self.assertEqual(observation.data.state, ComponentState.WARNING)
            self.assertTrue(observation.data.metrics["full_update_paused"])
            self.assertFalse(observation.data.metrics["min_data_fresh"])
            self.assertIn("分钟数据心跳已过期", observation.data.reason)

    def test_long_running_minute_round_is_fresh_before_it_writes_success(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            now = datetime.now().astimezone().replace(
                hour=13, minute=30, second=0, microsecond=0
            )
            self.write_fuel(root, now)
            (root / "fuel" / "log").mkdir()
            (root / "fuel" / "log" / f"{now:%Y-%m-%d}_日志.log").write_text(
                f"INFO:root:{now - timedelta(minutes=20):%H:%M:%S} --> "
                "[command in]: C:\\data\\fuel\\fuel.exe min_data\n"
                f"INFO:root:{now - timedelta(minutes=19):%H:%M:%S} --> "
                "[加速数据源] 本轮完成，成功 1/1 个 hm\n"
                f"INFO:root:{now - timedelta(minutes=8):%H:%M:%S} --> "
                "[command in]: C:\\data\\fuel\\fuel.exe min_data\n"
                f"INFO:root:{now - timedelta(seconds=5):%H:%M:%S} --> "
                "[command in]: C:\\data\\fuel\\fuel.exe all_data\n"
                f"INFO:root:{now - timedelta(seconds=5):%H:%M:%S} --> "
                "在交易时间，不再更新数据。\n",
                encoding="utf-8",
            )
            config = self.make_config(root)
            config.fuel_log_directory = "fuel/log"

            def active_processes(names: list[str]) -> list[dict[str, object]]:
                if "fuel.exe" in names:
                    return [
                        {
                            "pid": 77,
                            "name": "fuel.exe",
                            "command": "min_data",
                        }
                    ]
                return []

            with patch.object(
                TradeSystemMonitor,
                "_active_processes",
                side_effect=active_processes,
            ):
                observation = TradeSystemMonitor(config).observe(
                    now,
                    rocket=RocketObservation(False, False, "Rocket空闲"),
                    active_window=True,
                )
            self.assertEqual(observation.data.state, ComponentState.HEALTHY)
            self.assertTrue(observation.data.metrics["full_update_paused"])
            self.assertFalse(observation.data.metrics["min_data_success_fresh"])
            self.assertTrue(observation.data.metrics["min_data_scheduler_fresh"])
            self.assertTrue(observation.data.metrics["min_data_running"])
            self.assertTrue(observation.data.metrics["min_data_fresh"])
            self.assertIn("分钟数据正在更新", observation.data.reason)

    def test_minute_data_is_not_required_after_its_observed_session(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            now = datetime.now().astimezone().replace(
                hour=15, minute=15, second=0, microsecond=0
            )
            self.write_fuel(root, now)
            (root / "fuel" / "log").mkdir()
            (root / "fuel" / "log" / f"{now:%Y-%m-%d}_日志.log").write_text(
                f"INFO:root:{now.replace(hour=15, minute=1):%H:%M:%S} --> "
                "[command in]: C:\\data\\fuel\\fuel.exe min_data\n"
                f"INFO:root:{now.replace(hour=15, minute=2):%H:%M:%S} --> "
                "[加速数据源] 本轮完成，成功 1/1 个 hm\n"
                f"INFO:root:{now - timedelta(seconds=5):%H:%M:%S} --> "
                "[command in]: C:\\data\\fuel\\fuel.exe all_data\n"
                f"INFO:root:{now - timedelta(seconds=5):%H:%M:%S} --> "
                "在交易时间，不再更新数据。\n",
                encoding="utf-8",
            )
            config = self.make_config(root)
            config.fuel_log_directory = "fuel/log"
            with patch.object(TradeSystemMonitor, "_active_processes", return_value=[]):
                observation = TradeSystemMonitor(config).observe(
                    now,
                    rocket=RocketObservation(False, False, "Rocket空闲"),
                    active_window=True,
                )
            self.assertEqual(observation.data.state, ComponentState.HEALTHY)
            self.assertTrue(observation.data.metrics["full_update_paused"])
            self.assertFalse(observation.data.metrics["min_data_expected"])
            self.assertFalse(observation.data.metrics["min_data_fresh"])
            self.assertTrue(observation.data.metrics["min_data_health_ok"])
            self.assertIn("分钟数据无需运行", observation.data.reason)

    def test_full_update_process_is_healthy_while_stale_products_catch_up(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            now = datetime.now().astimezone().replace(
                hour=16, minute=1, second=0, microsecond=0
            )
            (root / "fuel").mkdir(parents=True)
            status_path = root / "fuel" / "status.json"
            status_path.write_text(
                json.dumps(
                    {
                        "stock-price": {
                            "isListed": 1,
                            "canAutoUpdate": 1,
                            "lastUpdateTime": (now - timedelta(hours=1)).strftime(
                                "%Y-%m-%d %H:%M:%S"
                            ),
                            "nextUpdateTime": (now - timedelta(minutes=30)).strftime(
                                "%Y-%m-%d %H:%M:%S"
                            ),
                            "lastErrTime": None,
                        }
                    }
                ),
                encoding="utf-8",
            )
            old_timestamp = (now - timedelta(minutes=30)).timestamp()
            os.utime(status_path, (old_timestamp, old_timestamp))
            (root / "fuel" / "update.json").write_text("{}", encoding="utf-8")

            def active_processes(names: list[str]) -> list[dict[str, object]]:
                if "fuel.exe" in names:
                    return [
                        {
                            "pid": 88,
                            "name": "fuel.exe",
                            "command": "all_data",
                        }
                    ]
                return []

            with patch.object(
                TradeSystemMonitor,
                "_active_processes",
                side_effect=active_processes,
            ):
                observation = TradeSystemMonitor(self.make_config(root)).observe(
                    now,
                    rocket=RocketObservation(False, False, "Rocket空闲"),
                    active_window=True,
                )
            self.assertEqual(observation.data.state, ComponentState.HEALTHY)
            self.assertEqual(
                observation.data.metrics["condition"],
                "fuel_full_update_running",
            )
            self.assertIn("执行全量更新", observation.data.reason)

    def test_active_rocket_with_stale_business_heartbeat_is_warning(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            now = datetime.now().astimezone().replace(microsecond=0)
            self.write_fuel(root, now)
            rocket = RocketObservation(
                True,
                False,
                "Rocket进程存在，但业务心跳已过期",
                300,
                False,
                300,
                "explicit_business_success",
            )
            with patch.object(TradeSystemMonitor, "_active_processes", return_value=[]):
                observation = TradeSystemMonitor(self.make_config(root)).observe(
                    now,
                    rocket=rocket,
                    active_window=True,
                )
            self.assertEqual(observation.order.state, ComponentState.WARNING)
            self.assertFalse(observation.order.metrics["business_healthy"])
            self.assertIn("心跳已过期", observation.order.reason)

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
            zeus = self.selection_child(observation, "Zeus")
            self.assertEqual(zeus.state, ComponentState.CRITICAL)
            self.assertFalse(zeus.metrics["selected"])
            self.assertEqual(observation.node.state, ComponentState.HEALTHY)


if __name__ == "__main__":
    unittest.main()
