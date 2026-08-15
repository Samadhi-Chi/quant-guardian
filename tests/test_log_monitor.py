from __future__ import annotations

import unittest

from quant_guardian.domain.models import LogSignal
from quant_guardian.monitors.log_monitor import classify_lines


class LogParserTests(unittest.TestCase):
    def test_disconnect_beats_earlier_success(self) -> None:
        signal, _, _ = classify_lines(
            [
                "account initializer login success",
                "push accountdetail",
                "broker proxy disconnect: End of file",
            ]
        )
        self.assertEqual(signal, LogSignal.EXPLICIT_DISCONNECT)

    def test_later_success_clears_earlier_disconnect(self) -> None:
        signal, _, _ = classify_lines(
            [
                "broker proxy disconnect: End of file",
                "account initializer login success",
                "push accountdetail",
            ]
        )
        self.assertEqual(signal, LogSignal.POSITIVE)

    def test_generic_fatal_is_not_a_failure(self) -> None:
        signal, _, _ = classify_lines(["[FATAL] expression parse warning"])
        self.assertEqual(signal, LogSignal.NEUTRAL)

    def test_login_prompt_requires_manual(self) -> None:
        _, _, manual = classify_lines(["请输入密码和验证码"])
        self.assertTrue(manual)


if __name__ == "__main__":
    unittest.main()