from __future__ import annotations

import os
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QModelIndex, Qt
from PySide6.QtGui import QColor
from PySide6.QtWidgets import QApplication, QDialog, QLabel

from quant_guardian.config import AppConfig
from quant_guardian.domain.models import GuardianState
from quant_guardian.ui.dialogs import (
    FirstRunDialog,
    QuantclassRestartConfirmDialog,
    RestartConfirmDialog,
)
from quant_guardian.ui.event_model import EventTableModel, OperationTableModel
from quant_guardian.ui.widgets import (
    HealthSample,
    HealthTimelineWidget,
    LatencyChartWidget,
    TaskOutcomeChartWidget,
    compute_trend_metrics,
    filter_samples,
    sample_from_document,
)

BASE = datetime(2026, 8, 15, 9, 30, tzinfo=timezone(timedelta(hours=8)))


class UiComponentTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.application = QApplication.instance() or QApplication([])

    def test_restart_dialogs_render_both_risk_branches(self) -> None:
        dialogs = [
            RestartConfirmDialog("观察模式"),
            QuantclassRestartConfirmDialog(
                r"C:\Apps\Quantclass\quantclass.exe", rocket_active=True
            ),
            QuantclassRestartConfirmDialog(
                r"C:\Apps\Quantclass\quantclass.exe", rocket_active=False
            ),
        ]
        for dialog in dialogs:
            self.addCleanup(dialog.deleteLater)
            dialog.show()
            self.application.processEvents()
            self.assertFalse(dialog.grab().isNull())
        rendered = " ".join(
            label.text()
            for dialog in dialogs
            for label in dialog.findChildren(QLabel)
        )
        self.assertIn("Rocket当前处于活动状态", rendered)
        self.assertIn("Fuel、Zeus与Rocket进程不会", rendered)

    def test_first_run_navigation_saves_observe_mode_without_sentinel(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.json"
            config = AppConfig()
            dialog = FirstRunDialog(config, path)
            self.addCleanup(dialog.deleteLater)
            self.assertEqual(dialog.stack.currentIndex(), 0)
            dialog.go_back()
            self.assertEqual(dialog.stack.currentIndex(), 0)
            dialog.go_next()
            self.assertEqual(dialog.stack.currentIndex(), 1)
            dialog.launcher_edit.setText(r"C:\QMT\bin.x64\XtItClient.exe")
            dialog.go_next()
            self.assertEqual(dialog.stack.currentIndex(), 2)
            dialog.go_back()
            self.assertEqual(dialog.stack.currentIndex(), 1)
            dialog.go_next()
            dialog.go_next()
            self.assertEqual(dialog.result(), QDialog.DialogCode.Accepted)
            self.assertTrue(path.is_file())
            self.assertEqual(config.mode, "observe")
            self.assertFalse((Path(directory) / "RECOVERY_ENABLED").exists())

    def test_first_run_save_error_remains_open(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            dialog = FirstRunDialog(
                AppConfig(), Path(directory) / "config.json"
            )
            self.addCleanup(dialog.deleteLater)
            dialog.stack.setCurrentIndex(2)
            with patch(
                "quant_guardian.ui.dialogs.save_config",
                side_effect=OSError("read only"),
            ), patch(
                "quant_guardian.ui.dialogs.QMessageBox.critical"
            ) as critical:
                dialog.go_next()
            self.assertEqual(dialog.result(), 0)
            critical.assert_called_once()

    def test_event_table_model_all_roles_and_append(self) -> None:
        model = EventTableModel()
        events = [
            {
                "time": "2026-08-15T09:30:00+08:00",
                "component_id": component,
                "event_type": "test",
                "severity": severity,
                "summary": f"{severity} summary",
            }
            for component, severity in (
                ("qmt_api.process", "critical"),
                ("trade_system.data", "warning"),
                ("quant_guardian", "info"),
                ("other", "custom"),
            )
        ]
        model.set_events(events[:1])
        model.append_events([])
        model.append_events(events[1:])
        self.assertEqual(model.rowCount(), 4)
        self.assertEqual(model.columnCount(), 5)
        self.assertEqual(model.rowCount(model.index(0, 0)), 0)
        self.assertEqual(model.columnCount(model.index(0, 0)), 0)
        self.assertEqual(
            model.headerData(
                0, Qt.Orientation.Horizontal, Qt.ItemDataRole.DisplayRole
            ),
            "时间",
        )
        self.assertIsNone(
            model.headerData(
                0, Qt.Orientation.Vertical, Qt.ItemDataRole.DisplayRole
            )
        )
        self.assertEqual(
            model.data(model.index(0, 1), Qt.ItemDataRole.DisplayRole),
            "QMT API",
        )
        self.assertEqual(
            model.data(model.index(1, 1), Qt.ItemDataRole.DisplayRole),
            "Trade System",
        )
        self.assertEqual(
            model.data(model.index(2, 1), Qt.ItemDataRole.DisplayRole),
            "Guardian",
        )
        self.assertIsInstance(
            model.data(model.index(0, 3), Qt.ItemDataRole.ForegroundRole),
            QColor,
        )
        self.assertEqual(
            model.data(model.index(0, 0), Qt.ItemDataRole.UserRole),
            events[0],
        )
        self.assertIsNone(model.event_at(-1))
        self.assertIsNone(model.data(QModelIndex(), Qt.ItemDataRole.DisplayRole))

    def test_operation_table_model_formats_durations_and_statuses(self) -> None:
        model = OperationTableModel()
        operations = [
            {
                "started_at": "2026-08-15T09:30:00+08:00",
                "operation_type": operation_type,
                "initiator": initiator,
                "status": status,
                "duration_ms": duration,
                "summary": status,
            }
            for operation_type, initiator, status, duration in (
                ("qmt_restart", "automatic", "succeeded", 500),
                ("manual_check", "manual", "failed", 2_500),
                ("recovery_control", "watchdog", "blocked", 120_000),
                ("custom", "custom", "cancelled", None),
            )
        ]
        model.set_operations(operations[:1])
        model.append_operations([])
        model.append_operations(operations[1:])
        self.assertEqual(model.rowCount(), 4)
        self.assertEqual(model.columnCount(), 6)
        self.assertEqual(
            model.data(model.index(0, 4), Qt.ItemDataRole.DisplayRole), "500 ms"
        )
        self.assertEqual(
            model.data(model.index(1, 4), Qt.ItemDataRole.DisplayRole), "2.5 秒"
        )
        self.assertEqual(
            model.data(model.index(2, 4), Qt.ItemDataRole.DisplayRole), "2.0 分"
        )
        self.assertEqual(
            model.data(model.index(3, 4), Qt.ItemDataRole.DisplayRole), "—"
        )
        for row in range(4):
            self.assertIsInstance(
                model.data(
                    model.index(row, 3), Qt.ItemDataRole.ForegroundRole
                ),
                QColor,
            )
        self.assertEqual(
            model.data(model.index(0, 0), Qt.ItemDataRole.UserRole),
            operations[0],
        )
        self.assertIsNone(model.operation_at(99))
        self.assertIsNone(model.data(QModelIndex(), Qt.ItemDataRole.DisplayRole))

    def make_samples(self) -> list[HealthSample]:
        return [
            HealthSample(
                at=BASE + timedelta(minutes=index * 20),
                state=state,
                qmt_ok=qmt,
                trade_ok=trade,
                latency_ms=latency,
                data_state=data,
                selection_state=selection,
                order_state=order,
            )
            for index, (state, qmt, trade, latency, data, selection, order) in enumerate(
                (
                    (
                        GuardianState.HEALTHY,
                        True,
                        True,
                        20.0,
                        "healthy",
                        "idle",
                        "healthy",
                    ),
                    (
                        GuardianState.SUSPECT,
                        None,
                        False,
                        90.0,
                        "warning",
                        "critical",
                        "unknown",
                    ),
                    (
                        GuardianState.DEGRADED,
                        False,
                        False,
                        150.0,
                        "critical",
                        "recovering",
                        "idle",
                    ),
                    (
                        GuardianState.VERIFYING,
                        True,
                        True,
                        35.0,
                        "healthy",
                        "healthy",
                        "healthy",
                    ),
                )
            )
        ]

    def test_chart_widgets_render_empty_single_multiple_dark_and_markers(self) -> None:
        samples = self.make_samples()
        widgets = [
            HealthTimelineWidget(),
            HealthTimelineWidget(compact=True),
            LatencyChartWidget(),
            TaskOutcomeChartWidget(),
        ]
        for widget in widgets:
            self.addCleanup(widget.deleteLater)
            widget.resize(760, 230)
            widget.show()
            self.application.processEvents()
            self.assertFalse(widget.grab().isNull())
            widget.set_samples(samples[:1])
            widget.set_range("today")
            widget.set_dark(True)
            if isinstance(widget, HealthTimelineWidget):
                widget.set_operation_markers(
                    [
                        {
                            "started_at": samples[0].at.isoformat(),
                            "status": "succeeded",
                        },
                        {
                            "started_at": samples[1].at.isoformat(),
                            "status": "failed",
                        },
                        {
                            "started_at": samples[2].at.isoformat(),
                            "status": "blocked",
                        },
                        {"started_at": "invalid", "status": "other"},
                    ]
                )
            self.application.processEvents()
            self.assertFalse(widget.grab().isNull())
            widget.set_samples(samples)
            widget.set_range("7d")
            self.application.processEvents()
            self.assertFalse(widget.grab().isNull())

    def test_persisted_sample_parsing_filtering_and_empty_metrics(self) -> None:
        self.assertIsNone(sample_from_document({"observed_at": "bad"}))
        document = {
            "observed_at": BASE.isoformat(),
            "state": "healthy",
            "probe": {"latency_ms": 42},
            "components": {
                "qmt_api": {"state": "recovering"},
                "trade_system": {
                    "state": "idle",
                    "children": [
                        {"id": "trade_system.data", "state": "healthy"},
                        {"id": "trade_system.backtest", "state": "idle"},
                        {"id": "trade_system.order", "state": "unknown"},
                    ],
                },
            },
        }
        sample = sample_from_document(document)
        self.assertIsNotNone(sample)
        self.assertIsNone(sample.qmt_ok)
        self.assertTrue(sample.trade_ok)
        self.assertEqual(sample.selection_state, "idle")
        self.assertEqual(filter_samples([], "1h"), [])
        self.assertEqual(len(filter_samples(self.make_samples(), "today")), 4)
        self.assertEqual(
            compute_trend_metrics([], "1h"),
            ("—", "—", "—", "—", "—", "等待数据"),
        )


if __name__ == "__main__":
    unittest.main()
