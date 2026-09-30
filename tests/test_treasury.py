"""Тесты казначейства Idle Cash (insights/idle-cash-earn.md). Без сети."""
import unittest

from src.treasury import (TreasuryState, earn_capacity, expected_accrual,
                          hourly_accrual, is_demo_blocked, recall_for_margin,
                          totaleq_warning)


class AccrualTest(unittest.TestCase):
    def test_hourly(self):
        # 1000 USDT под 3.62% → 1000*0.0362/8760 ≈ 0.00413
        self.assertAlmostEqual(hourly_accrual(1000.0, 0.0362), 0.004132, places=6)

    def test_first_accrual_lag(self):
        self.assertEqual(expected_accrual(1000.0, 0.0362, 1.0), 0.0)
        self.assertAlmostEqual(expected_accrual(1000.0, 0.0362, 3.0),
                               hourly_accrual(1000.0, 0.0362))

    def test_negative_rejected(self):
        with self.assertRaises(ValueError):
            hourly_accrual(-1.0, 0.03)
        with self.assertRaises(ValueError):
            hourly_accrual(100.0, -0.01)


class ReserveTest(unittest.TestCase):
    def test_capacity_keeps_buffer(self):
        st = TreasuryState(idle_usdt=1000.0, earn_usdt=0.0,
                           reserve_buffer_usdt=300.0)
        self.assertAlmostEqual(earn_capacity(st), 700.0)

    def test_capacity_never_negative(self):
        st = TreasuryState(idle_usdt=100.0, earn_usdt=50.0,
                           reserve_buffer_usdt=300.0)
        self.assertEqual(earn_capacity(st), 0.0)

    def test_recall_for_margin(self):
        st = TreasuryState(idle_usdt=200.0, earn_usdt=500.0,
                           reserve_buffer_usdt=100.0)
        self.assertEqual(recall_for_margin(st, 150.0), 0.0)  # хватает idle
        self.assertAlmostEqual(recall_for_margin(st, 600.0), 400.0)  # недостача
        self.assertAlmostEqual(recall_for_margin(st, 5000.0), 500.0)  # всё из Earn


class DemoTest(unittest.TestCase):
    def test_demo_block_code(self):
        self.assertTrue(is_demo_blocked("50038"))
        self.assertTrue(is_demo_blocked(50038))
        self.assertFalse(is_demo_blocked("50011"))

    def test_totaleq_warning(self):
        self.assertIn("asset-valuation", totaleq_warning(100.0))


if __name__ == "__main__":
    unittest.main()
