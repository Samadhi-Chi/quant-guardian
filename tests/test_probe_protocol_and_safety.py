from __future__ import annotations

import unittest
from pathlib import Path

from quant_guardian.probe.protocol import ProbeRequest, ProbeResponse


class ProbeProtocolTests(unittest.TestCase):
    def test_request_and_response_round_trip(self) -> None:
        request = ProbeRequest(
            operation="health",
            userdata_directory="D:/qmt/userdata_mini",
            xtquant_parent="C:/xtquant-parent",
            session_id=1234,
        )
        parsed = ProbeRequest.from_json(request.to_json())
        self.assertEqual(parsed.request_id, request.request_id)
        response = ProbeResponse(
            request_id=request.request_id,
            ok=True,
            status="healthy",
            reason="ok",
        )
        self.assertTrue(ProbeResponse.from_json(response.to_json()).ok)

    def test_readonly_adapter_has_no_trade_write_calls(self) -> None:
        path = (
            Path(__file__).resolve().parents[1]
            / "src"
            / "quant_guardian"
            / "probe"
            / "readonly_xtquant.py"
        )
        source = path.read_text(encoding="utf-8")
        forbidden = (
            ".order_stock(",
            ".cancel_order_stock(",
            ".order_stock_async(",
            ".cancel_order_stock_async(",
            ".smt_appointment_order(",
            ".repay(",
        )
        for symbol in forbidden:
            self.assertNotIn(symbol, source)

    def test_calendar_request_round_trip_is_read_only(self) -> None:
        request = ProbeRequest(
            operation="calendar",
            userdata_directory="D:/qmt/userdata_mini",
            xtquant_parent="C:/xtquant-parent",
            session_id=4321,
            market="SH",
            start_date="20260101",
            end_date="20261231",
        )
        parsed = ProbeRequest.from_json(request.to_json())
        self.assertEqual(parsed.operation, "calendar")
        self.assertEqual(parsed.market, "SH")
        self.assertEqual(parsed.start_date, "20260101")
        self.assertEqual(parsed.end_date, "20261231")


if __name__ == "__main__":
    unittest.main()
