"""Бэктестер: anti-lookahead (backtester-design.md §1.2) и каузальность индикаторов.

Каждая проверка доказывается в обе стороны: честная стратегия проходит, намеренно
«подглядывающая» (shift(-1), глобальный агрегат, недостаточный прогрев EMA) —
ловится. Без второй половины зелёный тест ничего не доказывал бы.
"""
import math
import unittest
from unittest import mock

from src import risk
from src.backtest.analysis import _synced_intervals, lookahead_check, recursive_check
from src.backtest.data import Bar, InstrumentSpec
from src.backtest.engine import Strategy
from src.backtest.indicators import (atr_wilder, bollinger, crossed_above, ema, rsi_wilder,
                                     sma, typical_price)
from src.backtest.strategies import SmaCross

H = 3_600_000
T0 = 1_767_225_600_000
SPEC = InstrumentSpec("TEST-USDT", 0.01, 1e-8, 1e-5)


def sine_bars(n=1500, t0=T0):
    out, prev = [], 100.0
    for i in range(n):
        c = 100 + 10 * math.sin(i / 25.0) + 2 * math.sin(i / 4.0) + 0.3 * math.sin(i * 1.7)
        out.append(Bar(t0 + i * H, prev, max(prev, c) + 0.2, min(prev, c) - 0.2, c, 1.0))
        prev = c
    return out


class _SignalStrategy(Strategy):
    """Вход при signal>0, выход при signal<=0; signal строит подкласс в prepare()."""
    entry_inputs = ("signal",)

    def on_bar(self, ctx):
        s = ctx.ind("signal")
        if ctx.position is None:
            if ctx.trading_allowed and not ctx.has_pending and s == s and s > 0:
                ctx.buy(equity_pct=10.0)
        elif not (s > 0):
            ctx.close()


class PeekNextClose(_SignalStrategy):
    """Классический bias: shift(-1) — сигнал знает следующий close."""
    name = "peek_next"
    warmup = 1

    def prepare(self, bars):
        c = [b.c for b in bars]
        return {"signal": [c[i + 1] - c[i] for i in range(len(c) - 1)] + [float("nan")]}


class GlobalMean(_SignalStrategy):
    """Bias через глобальный агрегат: порог — среднее по ВСЕМУ ряду."""
    name = "global_mean"
    warmup = 1

    def prepare(self, bars):
        c = [b.c for b in bars]
        m = math.fsum(c) / len(c)
        return {"signal": [m - x for x in c]}


class EmaCross(_SignalStrategy):
    """Честная рекуррентная стратегия: close выше EMA(10)."""
    name = "ema_cross"

    def __init__(self, warmup=100):
        super().__init__(warmup=warmup)
        self.warmup = warmup

    def prepare(self, bars):
        c = [b.c for b in bars]
        e = ema(c, 10)
        return {"signal": [x - y if y == y else float("nan") for x, y in zip(c, e)]}


class SlicingTest(unittest.TestCase):
    def test_sma_cross_passes(self):
        res = lookahead_check(lambda: SmaCross(fast=10, slow=40), sine_bars(), SPEC,
                              max_points=12)
        self.assertTrue(res["ok"], res["mismatches"])
        self.assertGreaterEqual(res["points"], 10)
        self.assertGreater(res["baseline_trades"], 5)

    def test_shift_minus_one_detected(self):
        res = lookahead_check(PeekNextClose, sine_bars(600), SPEC, max_points=10)
        self.assertFalse(res["ok"])
        self.assertIn("signal", res["biased_indicators"])

    def test_global_aggregate_detected(self):
        res = lookahead_check(GlobalMean, sine_bars(600), SPEC, max_points=10)
        self.assertFalse(res["ok"])
        self.assertIn("signal", res["biased_indicators"])


class RecursiveTest(unittest.TestCase):
    def test_sma_cross_exact_and_trades_identical(self):
        res = recursive_check(lambda: SmaCross(fast=10, slow=40), sine_bars(), SPEC,
                              offsets=(97, 250, 600))
        self.assertTrue(res["ok"], res["runs"])
        for run in res["runs"]:
            self.assertEqual(run["indicators_mismatch"], {})
            self.assertTrue(run["trades_equal"])
            self.assertGreater(run["trades_compared"], 3)

    def test_ema_converges_with_enough_warmup(self):
        res = recursive_check(lambda: EmaCross(warmup=150), sine_bars(), SPEC,
                              offsets=(120, 400), rel_tol=1e-6)
        self.assertTrue(res["ok"], res["runs"])

    def test_ema_start_dependence_detected_with_short_warmup(self):
        res = recursive_check(lambda: EmaCross(warmup=3), sine_bars(), SPEC,
                              offsets=(120,), rel_tol=1e-6)
        self.assertFalse(res["ok"])
        self.assertIn("signal", res["runs"][0]["indicators_mismatch"])

    def test_global_aggregate_detected(self):
        res = recursive_check(GlobalMean, sine_bars(600), SPEC, offsets=(100,))
        self.assertFalse(res["ok"])

    def test_start_index_dependence_detected(self):
        """Зависимость от точки старта не через индикаторы (номер бара среза): при частых
        входах копии не синхронизируются, при редких — сделки расходятся внутри интервала."""
        for period in (7, 97):
            res = recursive_check(lambda period=period: EveryNthBar(period), sine_bars(1500),
                                  SPEC, offsets=(100, 400))
            self.assertFalse(res["ok"], period)
            self.assertTrue(all(r["reason"] for r in res["runs"]), res["runs"])
        self.assertTrue(any(r["trades_equal"] is False for r in res["runs"]), res["runs"])

    def test_empty_comparison_is_not_a_pass(self):
        """Стратегия без сделок: сравнивать нечего — «не проверено», а не зелёный."""
        res = recursive_check(lambda: EveryNthBar(10 ** 9), sine_bars(600), SPEC, offsets=(100,))
        self.assertFalse(res["ok"])
        self.assertIn("не проверено", res["runs"][0]["reason"])

    def test_synced_intervals_close_on_risk_divergence(self):
        """Путезависимый breaker (HWM от старта прогона) закрывает интервал сравнения и
        попадает в отчёт; после новой синхронизации сравнение продолжается."""
        same = (0, 0, 0.0, False, False, False, (), ())
        tripped = (0, 0, 0.0, False, True, False, (), ())

        def st(i, flat, gate):
            return (T0 + i * H, flat, gate)

        late = [st(0, False, same), st(1, True, same), st(2, True, same), st(3, True, same),
                st(4, True, tripped), st(5, True, tripped)]
        base = [st(-2, True, same), st(-1, True, same)] + [
            st(0, False, same), st(1, True, same), st(2, True, same), st(3, True, tripped),
            st(4, True, tripped), st(5, True, tripped)]
        intervals, divergences = _synced_intervals(base, late, offset=2, warm=0)
        self.assertEqual(intervals, [(T0 + H, T0 + 3 * H), (T0 + 4 * H, None)])
        self.assertEqual(divergences, [{"ts": T0 + 3 * H, "fields": ["global_breaker"]}])

    def test_breaker_divergence_does_not_fail_honest_strategy(self):
        """Жёсткий breaker (только тест) у копий с разным HWM: проверка честной стратегии
        не краснеет от путезависимого лимита — только от расхождения сделок/индикаторов."""
        with mock.patch.object(risk, "GLOBAL_DD_LIMIT_PCT", 3.0):  # ужесточение, только тест
            res = recursive_check(lambda: SmaCross(fast=10, slow=40), sine_bars(), SPEC,
                                  offsets=(97, 250, 600))
        self.assertTrue(res["ok"], res["runs"])


class EveryNthBar(Strategy):
    """Скрытая зависимость от точки старта: вход на каждом period-м баре среза, выход через 3."""
    name = "every_nth"

    def __init__(self, period=7):
        super().__init__(period=period)

    def on_bar(self, ctx):
        if ctx.position is None:
            period = self.params["period"]
            if ctx.trading_allowed and not ctx.has_pending and ctx.i % period == period - 1:
                ctx.buy(equity_pct=10.0)
        elif not ctx.has_pending and ctx.i - ctx.position.entry_idx >= 3:
            ctx.close()


class IndicatorCausalityTest(unittest.TestCase):
    """out[i] зависит только от входа [:i+1]: значения на префиксе = значения на полном."""

    def setUp(self):
        bars = sine_bars(400)
        self.h = [b.h for b in bars]
        self.l = [b.l for b in bars]
        self.c = [b.c for b in bars]

    def _assert_prefix_invariant(self, fn):
        full = fn(len(self.c))
        for k in (30, 77, 200, 399):
            part = fn(k)
            for a, b in zip(full[:k], part):
                self.assertTrue((a != a and b != b) or a == b, (k, a, b))

    def test_prefix_invariance(self):
        self._assert_prefix_invariant(lambda k: sma(self.c[:k], 20))
        self._assert_prefix_invariant(lambda k: ema(self.c[:k], 20))
        self._assert_prefix_invariant(lambda k: rsi_wilder(self.c[:k], 14))
        self._assert_prefix_invariant(lambda k: atr_wilder(self.h[:k], self.l[:k], self.c[:k], 14))
        self._assert_prefix_invariant(
            lambda k: bollinger(typical_price(self.h[:k], self.l[:k], self.c[:k]), 20)[1])

    def test_known_values(self):
        self.assertEqual(sma([1, 2, 3, 4], 2)[1:], [1.5, 2.5, 3.5])
        self.assertTrue(math.isnan(sma([1, 2, 3], 2)[0]))
        e = ema([1, 2, 3, 4], 3)
        self.assertEqual(e[2], 2.0)                       # затравка SMA(3)
        self.assertAlmostEqual(e[3], 0.5 * 4 + 0.5 * 2.0)  # alpha = 2/(3+1)
        up = rsi_wilder([float(i) for i in range(30)], 14)
        self.assertTrue(math.isnan(up[13]))
        self.assertEqual(up[14], 100.0)
        self.assertEqual(rsi_wilder([5.0] * 20, 14)[-1], 50.0)
        # RSI при равных по модулю ростах и падениях -> 50
        zig = [100.0 + (i % 2) for i in range(40)]
        self.assertAlmostEqual(rsi_wilder(zig, 14)[-1], 50.0, delta=4.0)
        atr = atr_wilder([11.0] * 20, [9.0] * 20, [10.0] * 20, 14)
        self.assertEqual(atr[14], 2.0)
        mid, upper, lower = bollinger([7.0] * 25, 20)
        self.assertEqual((mid[-1], upper[-1], lower[-1]), (7.0, 7.0, 7.0))
        self.assertTrue(crossed_above([1, 3], [2, 2], 1))
        self.assertFalse(crossed_above([3, 3], [2, 2], 1))


if __name__ == "__main__":
    unittest.main()
