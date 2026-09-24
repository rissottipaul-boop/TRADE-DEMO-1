"""Потолок плеча риск-ядра (задача RISK-LEVERAGE-CAP): от 1 до risk.MAX_LEVERAGE."""
import unittest

from src import risk


class CheckLeverageTest(unittest.TestCase):
    def test_cap_is_three(self):
        # инвариант business-plan.md §7: плечо не выше 3x
        self.assertEqual(risk.MAX_LEVERAGE, 3)

    def test_allowed(self):
        for lever in (1, 2, 2.5, 3, "3"):
            with self.subTest(lever=lever):
                self.assertEqual(risk.check_leverage(lever), (True, "ok"))

    def test_denied(self):
        for lever in (3.01, 5, "10", 0, 0.5, -2, float("nan"), float("inf"), None, "", "3x"):
            with self.subTest(lever=lever):
                ok, reason = risk.check_leverage(lever)
                self.assertFalse(ok)
                self.assertTrue(reason)

    def test_reason_names_the_cap(self):
        ok, reason = risk.check_leverage(5)
        self.assertFalse(ok)
        self.assertIn("MAX_LEVERAGE", reason)


if __name__ == "__main__":
    unittest.main()
