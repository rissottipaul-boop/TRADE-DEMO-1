"""Тесты контроллера Idle Cash Earn (M3). Моки/данные, ноль сети."""
import unittest

from src.treasury import TreasuryState
from src.treasury_exec import (MIN_SWEEP_USDT, BalanceSnapshot, SimulatedEarnLedger,
                               attribute_yield, free_cash, plan_redeem, plan_sweep,
                               should_simulate, sweep_cycle,
                               treasury_state_from_snapshot)


def snap(avail=1000.0, frozen=0.0, equity=1000.0, ccy="USDT"):
    return BalanceSnapshot(ccy=ccy, avail_bal=avail, frozen_bal=frozen, equity=equity)


class FreeCashTest(unittest.TestCase):
    def test_reserve_pct_applied(self):
        # 1000 avail, 0 frozen, 1000 equity, 30% резерв → 700
        self.assertAlmostEqual(free_cash(snap(), 0.30), 700.0)

    def test_frozen_reduces(self):
        # equity - frozen = 800 ограничивает сверху: min(1000, 800) - 300 = 500
        self.assertAlmostEqual(free_cash(snap(frozen=200.0), 0.30), 500.0)

    def test_never_negative(self):
        self.assertEqual(free_cash(snap(avail=100.0, equity=1000.0), 0.30), 0.0)

    def test_zero_reserve(self):
        self.assertAlmostEqual(free_cash(snap(), 0.0), 1000.0)

    def test_bad_inputs_rejected(self):
        with self.assertRaises(ValueError):
            free_cash(snap(), -0.1)
        with self.assertRaises(ValueError):
            free_cash(snap(), 1.5)
        with self.assertRaises(ValueError):
            BalanceSnapshot(ccy="USDT", avail_bal=-1.0, frozen_bal=0.0, equity=1.0)


class SweepPlanTest(unittest.TestCase):
    def test_purchase_above_threshold(self):
        plan = plan_sweep(700.0)
        self.assertEqual(plan.action, "purchase")
        self.assertAlmostEqual(plan.amount_usdt, 700.0)
        self.assertIn("asset-valuation", plan.reason)  # totaleq-предупреждение §4

    def test_hold_below_threshold(self):
        plan = plan_sweep(MIN_SWEEP_USDT - 0.01)
        self.assertEqual(plan.action, "hold")
        self.assertEqual(plan.amount_usdt, 0.0)

    def test_boundary_exact(self):
        self.assertEqual(plan_sweep(MIN_SWEEP_USDT).action, "purchase")

    def test_negative_rejected(self):
        with self.assertRaises(ValueError):
            plan_sweep(-5.0)


class RedeemPlanTest(unittest.TestCase):
    def test_none_when_idle_covers(self):
        st = TreasuryState(idle_usdt=500.0, earn_usdt=500.0, reserve_buffer_usdt=0.0)
        plan = plan_redeem(st, 150.0)
        self.assertEqual((plan.action, plan.amount_usdt), ("none", 0.0))

    def test_redeem_shortfall(self):
        st = TreasuryState(idle_usdt=200.0, earn_usdt=500.0, reserve_buffer_usdt=100.0)
        plan = plan_redeem(st, 600.0)
        self.assertEqual(plan.action, "redeem")
        self.assertAlmostEqual(plan.amount_usdt, 400.0)


class AttributionTest(unittest.TestCase):
    def test_cash_earn_sleeve(self):
        rec = attribute_yield(1000.0, 0.0362, 3.0)
        self.assertEqual(rec.sleeve, "cash_earn")
        self.assertTrue(rec.simulated)
        # 3ч − лаг 2ч = 1 оплачиваемый час
        self.assertAlmostEqual(rec.accrued_usdt, 1000.0 * 0.0362 / 8760.0, places=8)

    def test_lag_zero(self):
        self.assertEqual(attribute_yield(1000.0, 0.03, 1.0).accrued_usdt, 0.0)


class SimulatedLedgerTest(unittest.TestCase):
    def test_purchase_redeem_accrue_marked_simulated(self):
        ledger = SimulatedEarnLedger()
        r1 = ledger.purchase("USDT", 700.0)
        r2 = ledger.accrue("USDT", 0.0362, 3.0)
        r3 = ledger.redeem("USDT", 100.0)
        for rec in (r1, r2, r3):
            self.assertTrue(rec["simulated"])
        self.assertTrue(all(r["simulated"] for r in ledger.records))
        self.assertEqual(len(ledger.records), 3)

    def test_accrue_math(self):
        ledger = SimulatedEarnLedger()
        ledger.purchase("USDT", 1000.0)
        rec = ledger.accrue("USDT", 0.0362, 3.0)
        self.assertAlmostEqual(rec["amount"], 1000.0 * 0.0362 / 8760.0, places=8)

    def test_redeem_over_balance_rejected(self):
        ledger = SimulatedEarnLedger()
        ledger.purchase("USDT", 50.0)
        with self.assertRaises(ValueError):
            ledger.redeem("USDT", 51.0)

    def test_bad_amounts_rejected(self):
        ledger = SimulatedEarnLedger()
        with self.assertRaises(ValueError):
            ledger.purchase("USDT", 0.0)
        with self.assertRaises(ValueError):
            ledger.redeem("USDT", -1.0)

    def test_should_simulate_50038(self):
        self.assertTrue(should_simulate("50038"))
        self.assertTrue(should_simulate(50038))
        self.assertFalse(should_simulate("0"))


class SweepCycleTest(unittest.TestCase):
    def test_cycle_purchases(self):
        ledger = SimulatedEarnLedger()
        plan, rec = sweep_cycle(snap(), ledger)
        self.assertEqual(plan.action, "purchase")
        self.assertIsNotNone(rec)
        assert rec is not None
        self.assertTrue(rec["simulated"])
        self.assertAlmostEqual(rec["amount"], 700.0)

    def test_cycle_holds(self):
        ledger = SimulatedEarnLedger()
        plan, rec = sweep_cycle(snap(avail=50.0, equity=50.0), ledger)
        self.assertEqual(plan.action, "hold")
        self.assertIsNone(rec)
        self.assertEqual(ledger.records, [])

    def test_state_bridge(self):
        st = treasury_state_from_snapshot(snap(), 100.0)
        self.assertAlmostEqual(st.idle_usdt, 700.0)
        self.assertAlmostEqual(st.earn_usdt, 100.0)


if __name__ == "__main__":
    unittest.main()
