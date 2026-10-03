"""Правило E и предохранители Funding Carry без сети и ордеров."""
import sqlite3
import unittest
from unittest.mock import patch

from src.carry import (CarryConfig, CarryDataError, CarryAction, build_entry_plan, evaluate_rule,
                       liquidation_distance_pct, load_realized_series,
                       monitor_short_liquidation)
from src.funding_archive import connect


PERIOD_MS = 8 * 60 * 60 * 1000


def _rates(rate, count=90, start=1_790_000_000_000):
    return [(start + index * PERIOD_MS, str(rate)) for index in range(count)]


class RuleETest(unittest.TestCase):
    def test_enters_only_above_three_percent_annualized(self):
        decision = evaluate_rule("BTC-USDT-SWAP", _rates(0.00003), has_position=False)
        self.assertEqual(decision.action, CarryAction.ENTER)
        self.assertAlmostEqual(decision.annualized_rate, 0.03285)

    def test_does_not_enter_at_or_below_threshold(self):
        at_threshold = evaluate_rule(
            "BTC-USDT-SWAP", _rates(0.03 / 1095), has_position=False)
        below = evaluate_rule("ETH-USDT-SWAP", _rates(0.00002), has_position=False)
        self.assertEqual(at_threshold.action, CarryAction.HOLD)
        self.assertEqual(below.action, CarryAction.HOLD)

    def test_exits_below_minus_five_percent_annualized(self):
        decision = evaluate_rule("ETH-USDT-SWAP", _rates(-0.00005), has_position=True)
        self.assertEqual(decision.action, CarryAction.EXIT)
        self.assertAlmostEqual(decision.annualized_rate, -0.05475)

    def test_exit_threshold_is_strict_and_thresholds_are_configured(self):
        decision = evaluate_rule(
            "BTC-USDT-SWAP", _rates(-0.05 / 1095), has_position=True,
            config=CarryConfig(entry_apy=0.03, exit_apy=-0.05),
        )
        self.assertEqual(decision.action, CarryAction.HOLD)
        with self.assertRaises(ValueError):
            CarryConfig(entry_apy=-0.01)
        with self.assertRaises(ValueError):
            CarryConfig(exit_apy=0.0)

    def test_one_negative_period_does_not_exit(self):
        rates = _rates(0.00005)
        rates[-1] = (rates[-1][0], "-0.0001")
        decision = evaluate_rule("BTC-USDT-SWAP", rates, has_position=True)
        self.assertEqual(decision.action, CarryAction.HOLD)
        self.assertGreater(decision.annualized_rate, -0.05)

    def test_instrument_position_state_is_independent(self):
        rates = _rates(-0.00005)
        btc = evaluate_rule("BTC-USDT-SWAP", rates, has_position=True)
        eth = evaluate_rule("ETH-USDT-SWAP", _rates(0.00004), has_position=False)
        self.assertEqual(btc.action, CarryAction.EXIT)
        self.assertEqual(eth.action, CarryAction.ENTER)

    def test_only_evaluates_after_new_eight_hour_period(self):
        rates = _rates(0.00004)
        same_period = evaluate_rule(
            "BTC-USDT-SWAP", rates, has_position=False,
            last_evaluated_funding_time=rates[-1][0])
        too_soon = evaluate_rule(
            "BTC-USDT-SWAP", rates, has_position=False,
            last_evaluated_funding_time=rates[-1][0] - PERIOD_MS // 2)
        self.assertEqual(same_period.action, CarryAction.WAIT)
        self.assertEqual(too_soon.action, CarryAction.WAIT)
        self.assertEqual(same_period.funding_time, rates[-1][0])

    def test_requires_ninety_complete_realized_periods(self):
        with self.assertRaises(CarryDataError):
            evaluate_rule("BTC-USDT-SWAP", _rates(0.00004, count=89),
                          has_position=False)
        rates = _rates(0.00004)
        rates[-1] = (rates[-1][0], None)
        with self.assertRaises(CarryDataError):
            evaluate_rule("BTC-USDT-SWAP", rates, has_position=False)

    def test_rejects_unsupported_or_malformed_series(self):
        with self.assertRaises(ValueError):
            evaluate_rule("DOGE-USDT-SWAP", _rates(0.00004), has_position=False)
        rates = _rates(0.00004)
        rates[-1] = (rates[-2][0], rates[-1][1])
        with self.assertRaises(CarryDataError):
            evaluate_rule("BTC-USDT-SWAP", rates, has_position=False)
        rates = _rates(0.00004)
        rates[-1] = (rates[-2][0] + 2 * PERIOD_MS, rates[-1][1])
        with self.assertRaises(CarryDataError):
            evaluate_rule("BTC-USDT-SWAP", rates, has_position=False)


class EntryPlanTest(unittest.TestCase):
    def test_enter_signal_builds_one_x_isolated_paired_plan(self):
        decision = evaluate_rule("BTC-USDT-SWAP", _rates(0.00004), has_position=False)
        with patch("src.carry.carry_executor.build_pair", return_value=None) as build:
            build_entry_plan(
                decision, planned_hold_days=30, spot_px=80_000, equity=10_000,
                acct_lv=2, risk_mod=object(),
            )
        legs = build.call_args.args[0]
        self.assertEqual(legs.spot_inst_id, "BTC-USDT")
        self.assertEqual(legs.swap_inst_id, "BTC-USDT-SWAP")
        self.assertEqual(legs.lever, 1)
        self.assertEqual(legs.margin_mode, "isolated")
        self.assertEqual(build.call_args.kwargs["headline_apy"], decision.annualized_rate)

    def test_non_entry_signal_cannot_build_plan(self):
        decision = evaluate_rule("BTC-USDT-SWAP", _rates(0.00001), has_position=False)
        with patch("src.carry.carry_executor.build_pair") as build:
            with self.assertRaises(ValueError):
                build_entry_plan(
                    decision, planned_hold_days=30, spot_px=80_000, equity=10_000,
                    acct_lv=2,
                )
        build.assert_not_called()


class RealizedArchiveTest(unittest.TestCase):
    def test_loads_realized_rate_in_time_order(self):
        conn = connect(":memory:")
        self.addCleanup(conn.close)
        rows = _rates(0.00004)
        with conn:
            conn.executemany(
                "INSERT INTO funding_rate(inst_id,funding_time,funding_rate,realized_rate,"
                "method,formula_type,fetched_at) VALUES(?,?,?,?,?,?,?)",
                [("BTC-USDT-SWAP", ts, rate, rate, None, None, ts) for ts, rate in rows],
            )
        loaded = load_realized_series(conn, "BTC-USDT-SWAP", now_ms=rows[-1][0])
        self.assertEqual(loaded, rows)

    def test_missing_realized_rate_is_an_error_not_fallback(self):
        conn = connect(":memory:")
        self.addCleanup(conn.close)
        rows = _rates(0.00004)
        with conn:
            conn.executemany(
                "INSERT INTO funding_rate(inst_id,funding_time,funding_rate,realized_rate,"
                "method,formula_type,fetched_at) VALUES(?,?,?,?,?,?,?)",
                [("BTC-USDT-SWAP", ts, rate, None if i == 10 else rate, None, None, ts)
                 for i, (ts, rate) in enumerate(rows)],
            )
        with self.assertRaises(CarryDataError):
            load_realized_series(conn, "BTC-USDT-SWAP", now_ms=rows[-1][0])

    def test_stale_archive_is_an_error(self):
        conn = connect(":memory:")
        self.addCleanup(conn.close)
        rows = _rates(0.00004)
        with conn:
            conn.executemany(
                "INSERT INTO funding_rate(inst_id,funding_time,funding_rate,realized_rate,"
                "method,formula_type,fetched_at) VALUES(?,?,?,?,?,?,?)",
                [("BTC-USDT-SWAP", ts, rate, rate, None, None, ts) for ts, rate in rows],
            )
        with self.assertRaises(CarryDataError):
            load_realized_series(conn, "BTC-USDT-SWAP", now_ms=rows[-1][0] + PERIOD_MS * 2)


class LiquidationGuardTest(unittest.TestCase):
    def test_distance_for_short_is_from_mark_to_liquidation(self):
        self.assertAlmostEqual(liquidation_distance_pct(100.0, 199.0), 99.0)

    def test_near_liquidation_requests_exit(self):
        result = monitor_short_liquidation(mark_px=190.0, liq_px=199.0)
        self.assertTrue(result.should_exit)
        self.assertAlmostEqual(result.distance_pct, 100 * 9 / 190)

    def test_safe_distance_keeps_position(self):
        result = monitor_short_liquidation(mark_px=100.0, liq_px=199.0)
        self.assertFalse(result.should_exit)

    def test_invalid_prices_raise(self):
        for mark, liq in ((0, 100), (100, 0), (float("nan"), 200)):
            with self.subTest(mark=mark, liq=liq), self.assertRaises(ValueError):
                liquidation_distance_pct(mark, liq)
        with self.assertRaises(ValueError):
            monitor_short_liquidation(mark_px=100, liq_px=None)


if __name__ == "__main__":
    unittest.main()
