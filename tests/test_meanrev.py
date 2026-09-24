"""Mean-reversion RSI/BB (MEANREV-IMPL): сигналы, стоп и сайзинг, ROI-таблица, тайм-стоп,
anti-lookahead. Данные синтетические, без сети.

Ожидаемые значения считаются в тестах независимо от кода стратегии: формулы дизайна
insights/meanrev-strategy-design.md §3–§6 и risk.size_position (1% риска, потолок 15%).
"""
import math
import random
import unittest

from src import risk
from src.backtest.analysis import lookahead_check, recursive_check
from src.backtest.data import Bar, InstrumentSpec, ceil_to_step, floor_to_step, round_to_step
from src.backtest.engine import Backtest, CostModel
from src.backtest.indicators import crossed_above, rsi_wilder
from src.backtest.meanrev import (DEFAULT_ROI, MeanReversion, entry_signal, exit_breakdown,
                                  exit_signal, roi_required)

H = 3_600_000
T0 = 1_767_225_600_000  # 2026-01-01 00:00 UTC
SPEC = InstrumentSpec("TEST-USDT", tick_sz=0.01, lot_sz=1e-8, min_sz=1e-5)
NAN = float("nan")
COSTS = CostModel()


def ar_bars(n, seed=1, vol=0.01, t0=T0):
    """Цена 100·exp(x), x — AR(1): колеблется вокруг 100, не уходит в ноль."""
    rng = random.Random(seed)
    out, x, prev = [], 0.0, 100.0
    for i in range(n):
        x = 0.97 * x + rng.gauss(0.0, vol)
        c = round(100.0 * math.exp(x), 2)
        h = round(max(prev, c) * (1 + abs(rng.gauss(0.0, vol)) / 2), 2)
        lo = round(min(prev, c) * (1 - abs(rng.gauss(0.0, vol)) / 2), 2)
        out.append(Bar(t0 + i * H, prev, h, lo, c, 1.0))
        prev = c
    return out


def bars(rows, t0=T0):
    return [Bar(t0 + i * H, o, h, lo, c, v) for i, (o, h, lo, c, v) in enumerate(rows)]


def first_signal(vol=0.01, seed=1, warmup=50, n=800):
    """Префикс ряда, оканчивающийся первым сигналом входа после прогрева, и ATR на нём."""
    src = ar_bars(n, seed, vol)
    ind = MeanReversion(warmup=warmup).prepare(src)
    for i in range(warmup, len(src)):
        if entry_signal(ind["rsi"], ind["bb_mid"], src, i) and ind["atr"][i] == ind["atr"][i]:
            return src[:i + 1], ind["atr"][i]
    raise AssertionError("в синтетическом ряду нет сигнала входа")


def extend(prefix, rows):
    """Продолжение ряда барами (o, h, l, c) после префикса."""
    t = prefix[-1].ts + H
    return prefix + [Bar(t + k * H, o, h, lo, c, 1.0) for k, (o, h, lo, c) in enumerate(rows)]


def run(bar_list, **params):
    params.setdefault("warmup", 50)
    return Backtest(bar_list, MeanReversion(**params), SPEC).run()


class RoiTableTest(unittest.TestCase):
    def test_design_table(self):
        self.assertEqual(DEFAULT_ROI, ((0, 2.0), (240, 1.2), (720, 0.6), (1440, 0.0)))

    def test_decay_by_minutes(self):
        cases = [(0, 2.0), (60, 2.0), (239.9, 2.0), (240, 1.2), (719, 1.2), (720, 0.6),
                 (1439, 0.6), (1440, 0.0), (5000, 0.0)]
        for minutes, want in cases:
            self.assertEqual(roi_required(DEFAULT_ROI, minutes), want, minutes)

    def test_unsorted_table_and_before_first_row(self):
        self.assertEqual(roi_required(((240, 1.0), (0, 3.0)), 300), 1.0)
        self.assertIsNone(roi_required(((60, 1.0),), 30))


class EntrySignalTest(unittest.TestCase):
    """§3: crossed_above(RSI, 30) и close <= BB-middle и close > close[-1] и volume > 0."""

    def check(self, rsi, closes, mid=100.0, vol=1.0):
        b = bars([(c, c, c, c, vol) for c in closes])
        return entry_signal(rsi, [mid] * len(closes), b, len(closes) - 1)

    def test_all_conditions(self):
        self.assertTrue(self.check([NAN, 29.0, 31.0], [99.0, 98.0, 99.0]))
        self.assertTrue(self.check([NAN, 30.0, 30.1], [99.0, 98.0, 100.0]))  # 30 → выше, close = mid

    def test_each_condition_breaks_signal(self):
        self.assertFalse(self.check([NAN, 31.0, 32.0], [99.0, 98.0, 99.0]))   # нет пересечения
        self.assertFalse(self.check([NAN, 29.0, 30.0], [99.0, 98.0, 99.0]))   # 30 не выше 30
        self.assertFalse(self.check([NAN, 29.0, 31.0], [99.0, 98.0, 101.0]))  # close > middle
        self.assertFalse(self.check([NAN, 29.0, 31.0], [99.0, 99.5, 99.0]))   # close не растёт
        self.assertFalse(self.check([NAN, 29.0, 31.0], [99.0, 98.0, 99.0], vol=0.0))  # vol = 0
        self.assertFalse(self.check([NAN, 29.0, 31.0], [99.0, 98.0, 99.0], mid=NAN))  # прогрев


class ExitSignalTest(unittest.TestCase):
    """§4: crossed_above(RSI, 70) и close >= BB-middle и close < close[-1]."""

    def test_rule_on_given_series(self):
        b = bars([(c, c, c, c, 1.0) for c in (104.0, 105.0, 104.0)])
        self.assertTrue(exit_signal([NAN, 69.0, 71.0], [100.0] * 3, b, 2))
        self.assertFalse(exit_signal([NAN, 69.0, 71.0], [106.0] * 3, b, 2))  # close < middle
        up = bars([(c, c, c, c, 1.0) for c in (104.0, 105.0, 106.0)])
        self.assertFalse(exit_signal([NAN, 69.0, 71.0], [100.0] * 3, up, 2))
        self.assertTrue(exit_signal([NAN, 69.0, 71.0], [100.0] * 3, up, 2, price_guard=False))

    def test_guard_unreachable_for_wilder_rsi(self):
        """RSI Уайлдера от close растёт только на баре с close > close[-1]: пересечение 70
        вверх и close(t) < close(t−1) несовместимы, выход по сигналу §4 не срабатывает."""
        crosses = 0
        for seed in range(20):
            c = [b.c for b in ar_bars(2000, seed, 0.01)]
            rsi = rsi_wilder(c, 14)
            for i in range(15, len(c)):
                if crossed_above(rsi, 70.0, i):
                    crosses += 1
                    self.assertGreater(c[i], c[i - 1])
                if crossed_above(rsi, 30.0, i):
                    self.assertGreater(c[i], c[i - 1])  # guard входа избыточен
        self.assertGreater(crosses, 100)

    def test_no_rsi_exits_in_backtest(self):
        res = run(ar_bars(3000, 7, 0.01))
        self.assertGreater(len(res.trades), 10)
        self.assertEqual([t for t in res.trades if t.exit_tag == "rsi_exit"], [])

    def test_rsi_exit_wired_without_guard(self):
        """Без guard'а и без ROI/тайм-стопа выход §4 срабатывает — параметр не мёртвый."""
        b = ar_bars(3000, 7, 0.01)
        res = run(b, exit_price_guard=False, roi=((0, 1000.0),), time_stop_min=None)
        self.assertTrue([t for t in res.trades if t.exit_tag == "rsi_exit"])


class StopAndSizingTest(unittest.TestCase):
    def expected_qty(self, close, stop, equity=10_000.0):
        """risk.size_position в лотах: 1% риска, потолок 15% notional, floor к lotSz."""
        lot = SPEC.lot_sz
        by_risk = math.floor(equity * risk.DEFAULT_RISK_PCT / 100.0 / ((close - stop) * lot))
        by_cap = math.floor(equity * risk.MAX_POSITION_PCT / 100.0 / (close * lot))
        return floor_to_step(min(by_risk, by_cap) * lot, lot), by_risk > by_cap

    def test_entry_next_open_stop_and_capped_size(self):
        prefix, atr = first_signal(vol=0.01, seed=1)
        c = prefix[-1].c
        stop = round_to_step(c - 1.5 * atr, SPEC.tick_sz)
        # бар 1: low на тик выше стопа — стоп не срабатывает; бар 2: касание стопа
        res = run(extend(prefix, [(c, c, stop + 0.01, stop + 0.05),
                                  (stop + 0.05, stop + 0.1, stop - 1.0, stop - 0.5)]))
        self.assertEqual(len(res.trades), 1)
        t = res.trades[0]
        self.assertEqual(t.entry_signal_ts, prefix[-1].ts)
        self.assertEqual(t.entry_ts, prefix[-1].ts + H)  # open(t+1)
        self.assertEqual(t.entry_px, ceil_to_step(c * (1 + COSTS.slip), SPEC.tick_sz))
        self.assertEqual(t.exit_reason, "stop_loss")
        self.assertEqual(t.bars_held, 1)
        self.assertEqual(t.exit_px, floor_to_step(stop * (1 - COSTS.slip), SPEC.tick_sz))
        qty, capped = self.expected_qty(c, stop)
        self.assertTrue(capped, "на vol 1%/бар 1.5×ATR даёт позицию выше потолка 15%")
        self.assertAlmostEqual(t.qty, qty, places=8)
        self.assertLessEqual(t.qty * c, 10_000.0 * risk.MAX_POSITION_PCT / 100.0)

    def test_size_by_risk_when_cap_not_binding(self):
        prefix, atr = first_signal(vol=0.06, seed=3)
        c = prefix[-1].c
        stop = round_to_step(c - 1.5 * atr, SPEC.tick_sz)
        res = run(extend(prefix, [(c, c * 1.001, stop - 0.5, stop - 0.2)]))
        t = res.trades[0]
        qty, capped = self.expected_qty(c, stop)
        self.assertFalse(capped, "высокая волатильность: сайзинг риском, без потолка")
        self.assertAlmostEqual(t.qty, qty, places=8)
        self.assertAlmostEqual(t.qty * (c - stop), 100.0, delta=(c - stop) * SPEC.lot_sz + 1e-6)
        self.assertEqual(t.exit_reason, "stop_loss")

    def test_rejects_stop_multiplier_not_positive(self):
        with self.assertRaises(ValueError):
            MeanReversion(stop_atr_mult=0)


class RoiAndTimeStopTest(unittest.TestCase):
    def setUp(self):
        self.prefix, atr = first_signal(vol=0.01, seed=1)
        self.c = self.prefix[-1].c
        self.stop = self.c - 1.5 * atr

    def flat(self, px, n):
        lo = max(px * 0.9995, self.stop + 0.05)
        return [(px, px * 1.0005, lo, px)] * n

    def test_roi_first_row_exit_next_open(self):
        c = self.c
        up = c * 1.03  # нетто ≈ +2.7% уже на первом баре → строка «0 мин: 2%»
        res = run(extend(self.prefix, [(c, up, c * 0.9995, up)] + self.flat(up, 3)))
        t = res.trades[0]
        self.assertEqual((t.exit_reason, t.exit_tag, t.bars_held), ("signal", "roi", 1))
        self.assertEqual(t.exit_ts, t.entry_ts + H)
        self.assertEqual(t.exit_px, floor_to_step(up * (1 - COSTS.slip), SPEC.tick_sz))

    def test_roi_decays_to_720_minutes(self):
        c = self.c
        px = c * 1.0105  # нетто ≈ +0.8%: ниже 2% и 1.2%, выше 0.6% — выход на 720 мин
        res = run(extend(self.prefix, [(c, px, max(c * 0.9995, self.stop + 0.05), px)]
                         + self.flat(px, 30)))
        t = res.trades[0]
        self.assertEqual(t.exit_tag, "roi")
        self.assertEqual(t.bars_held, 12)
        self.assertEqual(t.exit_ts - t.entry_ts, 720 * 60_000)

    def test_time_stop_24h_at_any_pnl(self):
        px = self.c * 0.998  # в минусе, стоп не задет: ни одна строка ROI не выполнена
        res = run(extend(self.prefix, self.flat(px, 40)))
        t = res.trades[0]
        self.assertEqual((t.exit_reason, t.exit_tag), ("signal", "time_stop"))
        self.assertEqual(t.bars_held, 24)
        self.assertEqual(t.exit_ts - t.entry_ts, 1440 * 60_000)
        self.assertLess(t.pnl, 0)

    def test_time_stop_disabled(self):
        px = self.c * 0.998
        res = run(extend(self.prefix, self.flat(px, 40)), time_stop_min=None)
        self.assertEqual(res.trades[0].exit_reason, "end_of_data")

    def test_exit_breakdown(self):
        c = self.c
        up = c * 1.03
        res = run(extend(self.prefix, [(c, up, c * 0.9995, up)] + self.flat(up, 3)))
        rows = exit_breakdown(res.trades)
        self.assertEqual([r[:3] for r in rows], [("roi", 1, 1)])


class AntiLookaheadTest(unittest.TestCase):
    def test_warmup_default_and_no_decision_before_it(self):
        self.assertEqual(MeanReversion().warmup, 200)
        self.assertEqual(MeanReversion(bb_n=150, warmup=0).warmup, 300)
        b = ar_bars(1200, 5, 0.01)
        res = Backtest(b, MeanReversion(), SPEC).run()
        self.assertTrue(res.decisions)
        self.assertTrue(all(d.ts >= b[200].ts for d in res.decisions))

    def test_slicing(self):
        b = ar_bars(1500, 11, 0.01)
        la = lookahead_check(lambda: MeanReversion(warmup=50), b, SPEC, max_points=8)
        self.assertGreater(la["baseline_trades"], 5)
        self.assertTrue(la["ok"], la["mismatches"])

    def test_recursive_with_tolerance(self):
        b = ar_bars(2500, 13, 0.01)
        rc = recursive_check(MeanReversion, b, SPEC, offsets=(300,), rel_tol=1e-6)
        run0 = rc["runs"][0]
        self.assertTrue(rc["ok"], run0)
        self.assertGreater(run0["trades_compared"], 0)


if __name__ == "__main__":
    unittest.main()
