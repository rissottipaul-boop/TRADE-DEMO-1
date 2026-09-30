"""Тесты funding carry (insights/funding-carry.md). Чистая математика, без сети."""
import unittest

from src.funding_carry import (CarryLegs, assess, annualize, breakeven_days,
                               effective_apy, roundtrip_cost,
                               short_liquidation_move_pct, validate_legs)


class RoundtripTest(unittest.TestCase):
    def test_costs_match_insight_table(self):
        self.assertAlmostEqual(roundtrip_cost("taker"), 0.0030)
        self.assertAlmostEqual(roundtrip_cost("mixed"), 0.0025)
        self.assertAlmostEqual(roundtrip_cost("maker"), 0.0020)

    def test_bad_mode_raises(self):
        with self.assertRaises(ValueError):
            roundtrip_cost("wish")


class YieldTest(unittest.TestCase):
    def test_annualize_btc_window(self):
        # §2.1: mean 0.00525%/период → 5.75% годовых
        self.assertAlmostEqual(annualize(0.0000525), 0.0575, places=4)

    def test_breakeven_ranges_match_insight(self):
        # §0: BTC ≈12–19 дней, ETH ≈18–26 дней (taker верх, maker низ)
        btc_taker = breakeven_days(0.0575, "taker")
        btc_maker = breakeven_days(0.0575, "maker")
        self.assertTrue(12 <= btc_maker <= btc_taker <= 19 + 0.5)
        eth_taker = breakeven_days(0.0414, "taker")
        self.assertTrue(18 <= eth_taker <= 26 + 0.5)

    def test_breakeven_none_on_nonpositive(self):
        self.assertIsNone(breakeven_days(0.0))
        self.assertIsNone(breakeven_days(-0.01))

    def test_effective_apy_halved(self):
        # §0: поправка на x2 капитала
        self.assertAlmostEqual(effective_apy(0.0575), 0.02875)


class LegsTest(unittest.TestCase):
    def test_short_liq_1x_none(self):
        self.assertIsNone(short_liquidation_move_pct(1))

    def test_valid_pair_ok(self):
        legs = CarryLegs("BTC-USDT", "BTC-USDT-SWAP")
        self.assertEqual(validate_legs(legs, 2), [])

    def test_violations_listed(self):
        legs = CarryLegs("BTC-USDT", "BTC-USDT-SWAP", lever=2,
                         margin_mode="cross")
        reasons = validate_legs(legs, 3)
        self.assertTrue(any("1x" in r for r in reasons))
        self.assertTrue(any("isolated" in r for r in reasons))
        self.assertTrue(any("acctLv" in r for r in reasons))

    def test_unpaired_and_unsupported(self):
        self.assertTrue(validate_legs(CarryLegs("ETH-USDT", "BTC-USDT-SWAP"), 2))
        self.assertTrue(validate_legs(CarryLegs("DOGE-USDT", "DOGE-USDT-SWAP"), 2))

    def test_assess_verdict(self):
        legs = CarryLegs("BTC-USDT", "BTC-USDT-SWAP")
        v = assess(0.0575, legs, 2, planned_hold_days=30)
        self.assertEqual(v.blockers, ())
        self.assertTrue(v.meets_min_hold)
        v2 = assess(0.0575, legs, 2, planned_hold_days=5)
        self.assertFalse(v2.meets_min_hold)


if __name__ == "__main__":
    unittest.main()
