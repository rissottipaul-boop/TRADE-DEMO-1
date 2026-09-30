"""Тесты copy trading (insights/copy-trader-spec.md §5). Фейки, без сети."""
import unittest
from datetime import datetime, timedelta, timezone

from src.copy_trader import (BEIJING, BuyCounter, LeadReadiness,
                             check_lead_readiness, is_banned_region,
                             is_blocked, lead_economics, monitor_subpositions,
                             profit_share_due, validate_copy_strategy,
                             validate_lead_bot_ratio, week_slice_start)


def ready(**kw):
    base = dict(strategy_gate=True, live_track_days=100,
                account_equity_usdt=1000.0, region_ok=True,
                sub_account_verified_by_human=True, kyc_done_by_human=True)
    base.update(kw)
    return LeadReadiness(**base)


class ReadinessTest(unittest.TestCase):
    def test_c1_ready(self):
        self.assertEqual(check_lead_readiness(ready()), [])

    def test_c2_floor_strict(self):
        self.assertTrue(check_lead_readiness(ready(account_equity_usdt=500.0)))
        self.assertTrue(check_lead_readiness(ready(account_equity_usdt=499.0)))
        self.assertEqual(check_lead_readiness(ready(account_equity_usdt=500.01)),
                         [])

    def test_c3_sub_account_fail_closed(self):
        out = check_lead_readiness(ready(sub_account_verified_by_human=False))
        self.assertTrue(any("суб-аккаунт" in r for r in out))

    def test_c4_region(self):
        self.assertTrue(check_lead_readiness(ready(region_ok=False)))

    def test_c5_kyc(self):
        self.assertTrue(check_lead_readiness(ready(kyc_done_by_human=False)))

    def test_track_and_gate(self):
        self.assertTrue(check_lead_readiness(ready(live_track_days=0)))
        self.assertTrue(check_lead_readiness(ready(strategy_gate=False)))


class RatioTest(unittest.TestCase):
    def test_c6_values(self):
        self.assertEqual(validate_lead_bot_ratio(0.3), 0.3)
        self.assertEqual(validate_lead_bot_ratio(0), 0)
        with self.assertRaises(ValueError):
            validate_lead_bot_ratio(0.15)
        with self.assertRaises(ValueError):
            validate_lead_bot_ratio(-0.1)


class StrategyTest(unittest.TestCase):
    def test_c7_kinds(self):
        self.assertTrue(is_blocked(validate_copy_strategy("carry")))
        self.assertFalse(is_blocked(validate_copy_strategy("spot_dca")))
        self.assertTrue(validate_copy_strategy("spot_dca"))  # предупреждение
        self.assertFalse(is_blocked(validate_copy_strategy("signal")))
        self.assertEqual(validate_copy_strategy("contract_grid"), [])
        self.assertTrue(is_blocked(validate_copy_strategy("meme-coin")))


class EconomicsTest(unittest.TestCase):
    def test_c8_basic_and_loss(self):
        self.assertAlmostEqual(lead_economics(10000, 0.15, 0.10), 150.0)
        self.assertEqual(lead_economics(10000, -0.05, 0.10), 0.0)
        self.assertEqual(lead_economics(10000, 0.0, 0.30), 0.0)

    def test_c9_table(self):
        self.assertAlmostEqual(lead_economics(80000, 0.20, 0.10), 1600.0)
        self.assertAlmostEqual(lead_economics(500000, 0.20, 0.13), 13000.0)

    def test_c14_netting(self):
        self.assertAlmostEqual(profit_share_due([200.0, -50.0, 100.0], 0.10),
                               25.0)
        self.assertEqual(profit_share_due([-10.0], 0.30), 0.0)


class CounterTest(unittest.TestCase):
    def test_c10_limit_and_rollover(self):
        now = [datetime(2026, 9, 30, 10, 0, tzinfo=timezone.utc)]
        bc = BuyCounter(_now=lambda: now[0])
        self.assertEqual(bc.register_buy(449), [])
        self.assertEqual(bc.register_buy(50), [])  # 499 — ок
        self.assertEqual(bc.register_buy(1), [])  # ровно 500 — ок
        self.assertTrue(bc.pressure()[0].startswith("WARN"))  # ≥450 — варн
        out = bc.register_buy(1)
        self.assertTrue(out and out[0].startswith("BLOCK"))
        now[0] += timedelta(days=1)  # новые пекинские сутки
        self.assertEqual(bc.register_buy(1), [])


class MonitorTest(unittest.TestCase):
    def test_c11_parsing(self):
        class FakeEx:
            def fetch_copy_subpositions(self):
                return [{"subPosId": "1", "followerId": "a",
                         "notional": 100.0, "stopPx": 90.0},
                        {"subPosId": "2", "followerId": "b",
                         "notional": 50.0, "stopPx": None}]

        out = monitor_subpositions(FakeEx())
        self.assertEqual(out["verdict"], "warn")
        self.assertAlmostEqual(out["aum_usdt"], 150.0)
        self.assertEqual(out["followers"], 2)
        self.assertEqual(out["no_stop"], ["2"])

    def test_c11_network_unknown(self):
        class Flaky:
            def fetch_copy_subpositions(self):
                raise ConnectionError("dns")

        out = monitor_subpositions(Flaky())
        self.assertEqual(out["verdict"], "unknown")


class RegionTest(unittest.TestCase):
    def test_c12_ban_list(self):
        self.assertTrue(is_banned_region("HK"))
        self.assertTrue(is_banned_region("US"))
        self.assertTrue(is_banned_region("GB"))
        self.assertFalse(is_banned_region("RU"))  # факт инсайта §1


class WeekTest(unittest.TestCase):
    def test_c13_monday_beijing(self):
        wed = datetime(2026, 9, 30, 12, 0, tzinfo=timezone.utc)  # ср
        start = week_slice_start(wed)
        self.assertEqual(start.tzinfo, BEIJING)
        self.assertEqual(start.weekday(), 0)
        self.assertEqual((start.hour, start.minute), (0, 0))


if __name__ == "__main__":
    unittest.main()
