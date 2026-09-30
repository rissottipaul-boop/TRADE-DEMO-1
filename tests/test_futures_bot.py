"""Тесты контрактных ботов (insights/futures-bot-spec.md §7). Фейки, без сети."""
import unittest

from src import errors
from src.futures_bot import (BOT_ALREADY_STOPPED, FuturesDCAConfig,
                             FuturesGridConfig, check_stop_vs_liq,
                             creation_gate, dca_ladder_entries,
                             dca_max_pullback_pct, grid_long_worst_pct,
                             liquidation_estimate, liquidation_move_pct,
                             planned_worst_loss, sl_ratio_for_edge, stop_is_benign,
                             stop_plan, validate_dca, validate_grid)


def grid(**kw):
    base = dict(inst_id="BTC-USDT-SWAP", direction="long", lever=2,
                margin_usdt=100.0, min_px=75000.0, max_px=95000.0,
                grid_num=30, base_pos=True, sl_trigger_px=72750.0)
    base.update(kw)
    return FuturesGridConfig(**base)


def dca(**kw):
    base = dict(inst_id="BTC-USDT-SWAP", direction="long", lever=2,
                init_ord_amt=120.0, safety_ord_amt=100.0, max_safety_ords=5,
                px_steps=1.2, px_steps_mult=1.25, vol_mult=1.15,
                tp_pct=1.5, sl_pct=12.0, init_px=84000.0)
    base.update(kw)
    return FuturesDCAConfig(**base)


EQUITY = 100_000.0


class ValidateGridTest(unittest.TestCase):
    def test_ok(self):
        self.assertEqual(validate_grid(grid(), EQUITY), [])

    def test_v1_lever_bounds(self):
        self.assertTrue(validate_grid(grid(lever=4), EQUITY))
        self.assertTrue(validate_grid(grid(lever=0), EQUITY))

    def test_v2_wave1_cap(self):
        self.assertTrue(any("волна 1" in r
                            for r in validate_grid(grid(lever=3), EQUITY)))

    def test_v3_margin_caps(self):
        self.assertTrue(validate_grid(grid(margin_usdt=3000.0), EQUITY))  # 3%
        self.assertTrue(validate_grid(grid(), EQUITY,
                                      existing_margins=[4000.0]))  # Σ > 4%

    def test_v5_sl_required(self):
        self.assertTrue(validate_grid(grid(sl_trigger_px=None), EQUITY))
        n = grid(direction="neutral", base_pos=None, sl_trigger_px=None,
                 sl_ratio=None)
        self.assertTrue(any("sl_ratio" in r for r in validate_grid(n, EQUITY)))

    def test_v7_step_floor(self):
        g = grid(min_px=83000.0, max_px=84000.0, grid_num=100)  # шаг ~0.012%
        self.assertTrue(any("шаг" in r for r in validate_grid(g, EQUITY)))

    def test_v8_neutral_base_pos(self):
        n = grid(direction="neutral", base_pos=True, sl_trigger_px=None,
                 sl_ratio=0.05)
        self.assertTrue(any("basePos" in r for r in validate_grid(n, EQUITY)))

    def test_v10_only_usdt_swap(self):
        self.assertTrue(any("USDT-SWAP" in r
                            for r in validate_grid(grid(inst_id="BTC-USD-SWAP"),
                                                    EQUITY)))

    def test_v12_grid_num_and_range(self):
        self.assertTrue(validate_grid(grid(grid_num=1), EQUITY))
        self.assertTrue(validate_grid(grid(grid_num=50, max_grid_qty=20),
                                      EQUITY))
        self.assertTrue(validate_grid(grid(min_px=95000.0, max_px=75000.0),
                                      EQUITY))

    def test_v13_dedupe_and_owner(self):
        self.assertTrue(validate_grid(grid(), EQUITY,
                                      existing_dirs=[("BTC-USDT-SWAP", "long")]))
        self.assertTrue(validate_grid(grid(owner_code="pmp"), EQUITY))

    def test_acct_lv_gate(self):
        self.assertTrue(any("51057" in r
                            for r in validate_grid(grid(), EQUITY, acct_lv=1)))


class ValidateDCATest(unittest.TestCase):
    def test_ok(self):
        # маржа 794.24 ≤ 2% equity — поднять equity под пример M2
        self.assertEqual(validate_dca(dca(), 100_000.0), [])

    def test_v9_reinvest(self):
        self.assertTrue(any("allow_reinvest" in r
                            for r in validate_dca(dca(allow_reinvest=True),
                                                   EQUITY)))

    def test_v5_sl_inside_ladder(self):
        mpd = dca_max_pullback_pct(1.2, 1.25, 5)
        self.assertGreater(mpd, 5.0)
        bad = dca(sl_pct=round(mpd - 1.0, 2))
        self.assertTrue(any("MPD" in r for r in validate_dca(bad, EQUITY)))


class GateTest(unittest.TestCase):
    def test_v11_blocks(self):
        self.assertTrue(creation_gate(3.5, 1.0, 0.0, 60.0, True))  # ATR-шок
        self.assertTrue(creation_gate(1.0, 1.0, 0.001, 60.0, True))  # funding
        self.assertTrue(creation_gate(1.0, 1.0, 0.0, 3.0, True))  # окно ±5 мин

    def test_v11_ok_and_neutral_funding(self):
        self.assertEqual(creation_gate(1.0, 1.0, 0.0, 60.0, True), [])
        # нейтральному перегрев funding не блокирует
        self.assertEqual(creation_gate(1.0, 1.0, 0.001, 60.0, False), [])


class LiqMathTest(unittest.TestCase):
    def test_m1_table(self):
        self.assertAlmostEqual(liquidation_move_pct("long", 2), -49.8, delta=0.15)
        self.assertAlmostEqual(liquidation_move_pct("short", 2), 49.3, delta=0.15)
        self.assertIsNone(liquidation_move_pct("long", 1))
        self.assertAlmostEqual(liquidation_estimate(84000.0, "short", 2),
                               84000.0 * 1.493, delta=50.0)

    def test_m1_bad_direction(self):
        with self.assertRaises(ValueError):
            liquidation_move_pct("neutral", 2)


class WorstLossTest(unittest.TestCase):
    def test_literal_formula(self):
        fills = [(1.0, 100.0), (1.0, 102.0)]
        # |100-97|+|102-97| + 2*97*(0.0005+0.01) = 8 + 2.037
        self.assertAlmostEqual(planned_worst_loss(fills, 97.0), 10.037, places=3)

    def test_m2_grid_model(self):
        pct = grid_long_worst_pct(10.0, 25, 3.0, 2)
        self.assertTrue(13.0 <= pct <= 18.0, f"модель дала {pct:.1f}%")

    def test_m3_sl_ratio(self):
        self.assertAlmostEqual(sl_ratio_for_edge(49.0, 1000.0), 0.06)
        self.assertEqual(sl_ratio_for_edge(1.0, 100000.0), 0.03)  # пол
        self.assertEqual(sl_ratio_for_edge(500.0, 1000.0), 0.15)  # потолок


class StopVsLiqTest(unittest.TestCase):
    def test_ok_long(self):
        # край 75000, SL 72750, liq ~37200: запас 35.5k ≥ 3×2.25k и ≥15%
        liq = liquidation_estimate(85000.0, "long", 2)
        self.assertEqual(check_stop_vs_liq(75000.0, 72750.0, liq, "long",
                                           "BTC-USDT-SWAP",
                                           checker=lambda e, s, l, d: l < s < e),
                         [])

    def test_sl_beyond_liq(self):
        out = check_stop_vs_liq(75000.0, 30000.0, 37200.0, "long",
                                "BTC-USDT-SWAP",
                                checker=lambda e, s, l, d: l < s < e)
        self.assertTrue(out)

    def test_alt_floor_20(self):
        # край 100, SL 97, liq 81.5: запас 15.5 ≥ 3×3, дистанция 16% —
        # мажору (пол 15%) ок, альту (пол 20%) мало
        ok_major = check_stop_vs_liq(100.0, 97.0, 81.5, "long", "BTC-USDT-SWAP",
                                     checker=lambda e, s, l, d: True)
        bad_alt = check_stop_vs_liq(100.0, 97.0, 81.5, "long", "DOGE-USDT-SWAP",
                                    checker=lambda e, s, l, d: True)
        self.assertEqual(ok_major, [])
        self.assertTrue(any("20%" in r for r in bad_alt))


class StopPlanTest(unittest.TestCase):
    def test_m4_plan(self):
        plan = stop_plan("39547", "BTC-USDT-SWAP", "contract_grid", 72750.0)
        self.assertEqual(plan["close_cmd"]["stopType"], "1")
        self.assertEqual(plan["former_sl_px"], 72750.0)

    def test_m4_benign_repeat(self):
        self.assertEqual(stop_is_benign("51291"), BOT_ALREADY_STOPPED)
        self.assertEqual(stop_is_benign("0", "no_close_position"),
                         BOT_ALREADY_STOPPED)
        self.assertFalse(stop_is_benign("50011"))


class BotCodesTest(unittest.TestCase):
    def test_m5_all_mapped(self):
        for code in ("51057", "51055", "51070", "51340", "51399", "51370",
                     "51381", "51348", "51344", "51349", "51343", "51398",
                     "51380", "51313", "51065", "51291", "50016"):
            with self.subTest(code=code):
                self.assertIn(code, errors.ERROR_MAP)

    def test_dca_ladder_shape(self):
        entries = dca_ladder_entries(84000.0, 1.2, 1.25, 5)
        self.assertEqual(len(entries), 6)
        self.assertTrue(all(b < a for a, b in zip(entries, entries[1:])))


if __name__ == "__main__":
    unittest.main()
