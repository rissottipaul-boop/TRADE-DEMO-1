"""Тесты собственной grid-сессии (insights/grid-strategy-design.md). Без сети."""
import unittest
from datetime import datetime, timezone

from src.grid_engine import (GridConfig, build_levels, execute_stop_accounting,
                             fee_share, is_grid_service, range_pct, step_pct,
                             stop_decision, stop_plan, validate_config)


def cfg(**kw):
    base = dict(inst_id="BTC-USDT", min_px=80225.0, max_px=88670.0,
                grid_num=35, investment_quote=1000.0)
    base.update(kw)
    return GridConfig(**base)


class ConfigTest(unittest.TestCase):
    def test_demo_range_ok(self):
        # диапазон из grid-bot-demo-run.md: 10.5%, шаг ≈0.29%... ниже пола!
        c = cfg()
        self.assertAlmostEqual(range_pct(c), 10.5, delta=0.1)
        # 0.29% < 0.32%: конфиг нативного бота наш fee-floor не проходит —
        # это ожидаемо (§5.3: поправка к нативному шагу)
        self.assertLess(step_pct(c), 0.32)
        self.assertTrue(any("пола" in r for r in validate_config(c)))

    def test_valid_config_passes(self):
        c = cfg(grid_num=30)  # шаг ≈0.34%
        self.assertGreater(step_pct(c), 0.32)
        self.assertEqual(validate_config(c), [])

    def test_bad_range_rejected(self):
        self.assertTrue(validate_config(cfg(min_px=90000.0, max_px=88670.0)))
        self.assertTrue(validate_config(cfg(min_px=85000.0, max_px=86000.0)))  # узко
        self.assertTrue(validate_config(cfg(grid_num=1)))
        self.assertTrue(validate_config(cfg(grid_num=101)))
        self.assertTrue(validate_config(cfg(run_type=1)))

    def test_fee_share_gate(self):
        c = cfg(grid_num=30)
        self.assertLessEqual(fee_share(c), 0.5)
        self.assertGreater(fee_share(c, taker_stress=True), fee_share(c))


class LevelsTest(unittest.TestCase):
    def test_geometric_levels(self):
        lv = build_levels(cfg(grid_num=30))
        self.assertEqual(len(lv), 31)
        self.assertAlmostEqual(lv[0], 80225.0)
        self.assertAlmostEqual(lv[-1], 88670.0)
        ratios = [lv[i + 1] / lv[i] for i in range(30)]
        self.assertAlmostEqual(max(ratios), min(ratios))

    def test_invalid_raises(self):
        with self.assertRaises(ValueError):
            build_levels(cfg(grid_num=1))


class StopTest(unittest.TestCase):
    def test_inside_none(self):
        self.assertEqual(stop_decision(cfg(), 84000.0, 999.0), "none")

    def test_confirming_then_trigger(self):
        self.assertEqual(stop_decision(cfg(), 80000.0, 10.0), "confirming")
        self.assertEqual(stop_decision(cfg(), 80000.0, 30.0), "trigger")

    def test_gap_immediate(self):
        c = cfg()  # шаг ≈ $241; уход на 2 шага — немедленный триггер
        step_abs = (c.max_px - c.min_px) / c.grid_num
        self.assertEqual(stop_decision(c, c.min_px - 2 * step_abs, 0.0),
                         "trigger")

    def test_plan_and_accounting_once(self):
        plan = stop_plan("пробой minPx")
        self.assertTrue(plan.cancel_level_orders)
        self.assertTrue(plan.liquidate_base_at_market)
        self.assertTrue(plan.record_pnl_once)
        self.assertEqual(plan.cooldown_hours, 4)

        calls = []

        class FakeRisk:
            def record_pnl(self, inst, pnl, closed_at):
                calls.append(("pnl", inst, pnl))
                return ["evt"]

            def block_instrument(self, inst, hours):
                calls.append(("block", inst, hours))

        evts = execute_stop_accounting(
            "BTC-USDT", 12.5, datetime.now(timezone.utc), FakeRisk())
        self.assertEqual(evts, ["evt"])
        self.assertEqual(calls.count(("pnl", "BTC-USDT", 12.5)), 1)  # один вызов
        self.assertIn(("block", "BTC-USDT", 4), calls)


class ServiceTagTest(unittest.TestCase):
    def test_is_grid_service(self):
        self.assertTrue(is_grid_service("botg9f2k31x7"))
        self.assertFalse(is_grid_service("botr9f2k31x7"))
        self.assertFalse(is_grid_service(None))
        self.assertFalse(is_grid_service(""))


if __name__ == "__main__":
    unittest.main()
