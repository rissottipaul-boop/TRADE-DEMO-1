"""Бэктестер: движок исполнения, комиссии/PnL, стопы, риск-контур (BT-IMPL).

Все данные синтетические, без сети. Ожидаемые значения посчитаны вручную по
правилам backtester-design.md §1.1 (исполнение по open(t+1), «low раньше high»)
и §3 (taker 0.10% на каждой стороне, проскальзывание 5 бп, tickSz/lotSz).
"""
import math
import sqlite3
import unittest
from unittest import mock

from src import risk
from src.backtest.data import Bar, InstrumentSpec
from src.backtest.engine import Backtest, CostModel, Strategy
from src.backtest.strategies import BuyAndHold, SmaCross

H = 3_600_000
T0 = 1_767_225_600_000  # 2026-01-01 00:00 UTC
SPEC = InstrumentSpec("TEST-USDT", tick_sz=0.01, lot_sz=1e-8, min_sz=1e-5)


def bars_from(ohlc, t0=T0):
    return [Bar(t0 + i * H, o, h, l, c, 1.0) for i, (o, h, l, c) in enumerate(ohlc)]


def flat(n, px=100.0, t0=T0):
    return bars_from([(px, px, px, px)] * n, t0)


class Scripted(Strategy):
    """Покупает на барах buy_at, закрывает на close_at (решение на close бара)."""
    name = "scripted"

    def __init__(self, buy_at=(), close_at=(), equity_pct=10.0, stop=None, take_profit=None,
                 warmup=0):
        super().__init__()
        self.buy_at, self.close_at = set(buy_at), set(close_at)
        self.equity_pct, self.stop, self.tp = equity_pct, stop, take_profit
        self.warmup = warmup
        self.results = {}

    def on_bar(self, ctx):
        if ctx.i in self.buy_at:
            self.results[ctx.i] = ctx.buy(stop=self.stop, take_profit=self.tp,
                                          equity_pct=self.equity_pct)
        if ctx.i in self.close_at:
            ctx.close("scripted")


class ChurnStrategy(Strategy):
    """Вход на каждом баре без позиции, выход на следующем: на плоском рынке каждая
    сделка убыточна ровно на издержки — проверка серий убытков риск-ядра."""
    name = "churn"

    def on_bar(self, ctx):
        if ctx.position is None and not ctx.has_pending:
            ctx.buy(equity_pct=10.0)
        elif ctx.position is not None and not ctx.has_pending:
            ctx.close("churn")


class FeesAndPnlTest(unittest.TestCase):
    def test_round_trip_exact_accounting(self):
        """Покупка по open(1)=100, продажа по open(2)=110: всё посчитано вручную."""
        bars = bars_from([(100, 101, 99, 100), (100, 111, 99, 110),
                          (110, 111, 109, 110), (110, 111, 109, 110)])
        r = Backtest(bars, Scripted(buy_at=[0], close_at=[1]), SPEC).run()
        self.assertEqual(len(r.trades), 1)
        t = r.trades[0]
        # вход: 10% от 10000 по ref close 100 -> 10 ед.; exec = ceil_tick(100*1.0005)=100.05
        self.assertEqual(t.qty, 10.0)
        self.assertAlmostEqual(t.entry_px, 100.05, places=10)
        self.assertAlmostEqual(t.entry_cost, 1000.5, places=9)
        # комиссия покупки в базе: 10*0.001 = 0.01 ед. -> на руках 9.99
        self.assertAlmostEqual(t.qty_sold, 9.99, places=10)
        # выход: floor_tick(110*0.9995=109.945) = 109.94
        self.assertAlmostEqual(t.exit_px, 109.94, places=10)
        gross = 9.99 * 109.94
        exit_fee = gross * 0.001
        self.assertAlmostEqual(t.proceeds, gross - exit_fee, places=9)
        self.assertAlmostEqual(t.fees, 0.01 * 100.05 + exit_fee, places=9)
        self.assertAlmostEqual(t.slippage, 10 * 0.05 + 9.99 * 0.06, places=9)
        self.assertAlmostEqual(t.pnl, gross - exit_fee - 1000.5, places=9)
        self.assertAlmostEqual(t.pnl, 96.7022994, places=6)
        self.assertAlmostEqual(t.pnl_pct, t.pnl / 1000.5, places=12)
        self.assertEqual(t.exit_reason, "signal")
        self.assertAlmostEqual(r.final_equity, 10_000 + t.pnl, places=9)

    def test_fee_multipliers_and_zero_cost(self):
        """fee×0 и slippage×0: PnL = чистое изменение цены."""
        bars = bars_from([(100, 101, 99, 100), (100, 111, 99, 110),
                          (110, 111, 109, 110), (110, 111, 109, 110)])
        zero = CostModel().scaled(fee_mult=0.0, slip_mult=0.0)
        r = Backtest(bars, Scripted(buy_at=[0], close_at=[1]), SPEC, costs=zero).run()
        self.assertAlmostEqual(r.trades[0].pnl, 10 * (110 - 100), places=9)
        self.assertEqual(r.trades[0].fees, 0.0)
        heavy = CostModel().scaled(fee_mult=1.5)
        r15 = Backtest(bars, Scripted(buy_at=[0], close_at=[1]), SPEC, costs=heavy).run()
        r10 = Backtest(bars, Scripted(buy_at=[0], close_at=[1]), SPEC).run()
        self.assertLess(r15.trades[0].pnl, r10.trades[0].pnl)

    def test_equity_identity_with_dust(self):
        """final = initial + Σpnl + пыль×close: пыль от комиссии в базе и floor к lotSz."""
        spec = InstrumentSpec("TEST-USDT", 0.01, 0.001, 0.001)  # крупный лот -> заметная пыль
        bars = SmaCrossData.sine(800)
        r = Backtest(bars, SmaCross(fast=5, slow=20), spec).run()
        self.assertGreater(len(r.trades), 3)
        self.assertGreater(r.dust_qty, 0.0)
        expected = r.initial_cash + math.fsum(t.pnl for t in r.trades) + r.dust_qty * bars[-1].c
        self.assertAlmostEqual(r.final_equity, expected, places=6)

    def test_cost_model_validation(self):
        with self.assertRaises(ValueError):
            CostModel(limit_fill="maybe")
        with self.assertRaises(ValueError):
            CostModel(slippage_bps=-1)


class ExecutionModelTest(unittest.TestCase):
    def test_entry_at_next_open_not_signal_close(self):
        bars = bars_from([(100, 101, 94, 95), (97, 98, 96, 97), (97, 98, 96, 97)])
        r = Backtest(bars, Scripted(buy_at=[0]), SPEC).run()
        buy = r.fills[0]
        self.assertEqual(buy.ts, bars[1].ts)
        self.assertEqual(buy.ref_px, bars[1].o)       # open(t+1)=97, а не close(t)=95
        self.assertAlmostEqual(buy.exec_px, 97.05, places=10)  # ceil(97*1.0005=97.0485)

    def test_stop_before_take_profit_in_same_bar(self):
        """Бар задевает и стоп, и тейк -> стоп (low раньше high, худший сценарий)."""
        bars = bars_from([(100, 100, 100, 100), (100, 100.5, 99.5, 100),
                          (100, 106, 94, 100), (100, 100, 100, 100)])
        r = Backtest(bars, Scripted(buy_at=[0], stop=95.0, take_profit=105.0), SPEC).run()
        t = r.trades[0]
        self.assertEqual(t.exit_reason, "stop_loss")
        self.assertEqual(t.exit_ts, bars[2].ts)
        self.assertAlmostEqual(t.exit_px, 94.95, places=10)  # floor(95*0.9995=94.9525)

    def test_stop_gap_fills_at_open(self):
        """Open ниже стопа: исполнение от open (хуже стопа), не по цене стопа."""
        bars = bars_from([(100, 100, 100, 100), (100, 100.5, 99.5, 100),
                          (90, 91, 89, 90), (90, 90, 90, 90)])
        r = Backtest(bars, Scripted(buy_at=[0], stop=95.0), SPEC).run()
        t = r.trades[0]
        self.assertEqual(t.exit_reason, "stop_loss")
        self.assertAlmostEqual(t.exit_px, 89.95, places=10)  # floor(90*0.9995=89.955)

    def test_stop_checked_on_entry_bar(self):
        bars = bars_from([(100, 100, 100, 100), (100, 100.5, 94, 99), (99, 99, 99, 99)])
        r = Backtest(bars, Scripted(buy_at=[0], stop=95.0), SPEC).run()
        self.assertEqual(r.trades[0].exit_reason, "stop_loss")
        self.assertEqual(r.trades[0].bars_held, 0)

    def test_take_profit_through_vs_touch(self):
        """through (дефолт): касание тейка — не исполнение; прошла на 1 тик — исполнение."""
        touch_bar = [(100, 100, 100, 100), (100, 100.5, 99.5, 100),
                     (100, 105.0, 99.5, 100), (100, 100, 100, 100)]
        r = Backtest(bars_from(touch_bar), Scripted(buy_at=[0], take_profit=105.0), SPEC).run()
        self.assertEqual(r.trades[0].exit_reason, "end_of_data")
        through_bar = list(touch_bar)
        through_bar[2] = (100, 105.01, 99.5, 100)
        r = Backtest(bars_from(through_bar), Scripted(buy_at=[0], take_profit=105.0), SPEC).run()
        t = r.trades[0]
        self.assertEqual(t.exit_reason, "take_profit")
        self.assertEqual(t.exit_px, 105.0)
        fill = r.fills[-1]
        self.assertEqual(fill.liquidity, "taker")          # консервативный дефолт §3.1
        self.assertAlmostEqual(fill.fee, fill.qty * 105.0 * 0.001, places=10)
        self.assertEqual(fill.slippage, 0.0)
        touch = CostModel(limit_fill="touch", limit_fee="maker")
        r = Backtest(bars_from(touch_bar), Scripted(buy_at=[0], take_profit=105.0), SPEC,
                     costs=touch).run()
        self.assertEqual(r.trades[0].exit_reason, "take_profit")
        self.assertEqual(r.fills[-1].liquidity, "maker")
        self.assertAlmostEqual(r.fills[-1].fee, r.fills[-1].qty * 105.0 * 0.0008, places=10)

    def test_end_of_data_forced_close_and_pending_dropped(self):
        bars = flat(5)
        r = Backtest(bars, Scripted(buy_at=[1]), SPEC).run()
        self.assertEqual(r.trades[0].exit_reason, "end_of_data")
        self.assertEqual(r.trades[0].exit_ts, bars[-1].ts + H)
        self.assertAlmostEqual(r.trades[0].exit_px, 99.95, places=10)
        r = Backtest(bars, Scripted(buy_at=[4]), SPEC).run()   # сигнал на последнем баре
        self.assertEqual(r.trades, [])
        self.assertEqual(r.unfilled_at_end, 1)
        self.assertEqual(r.final_equity, 10_000.0)

    def test_warmup_blocks_entries(self):
        s = Scripted(buy_at=range(20), warmup=10)
        r = Backtest(flat(30), s, SPEC).run()
        self.assertEqual(r.rejections.get("warmup"), 10)
        self.assertEqual(r.trades[0].entry_signal_ts, T0 + 10 * H)
        self.assertEqual(r.curve[0], (T0 + 10 * H, 10_000.0))

    def test_nan_guard(self):
        class NanAt5(Scripted):
            def prepare(self, bars):
                return {"x": [float("nan") if i == 5 else 1.0 for i in range(len(bars))]}

        r = Backtest(flat(10), NanAt5(buy_at=[5]), SPEC).run()
        self.assertEqual(r.trades, [])
        self.assertEqual(r.rejections, {"nan_guard": 1})

    def test_history_view_blocks_future(self):
        test = self

        class Peeker(Strategy):
            def prepare(self, bars):
                return {"x": [float(i) for i in range(len(bars))]}

            def on_bar(self, ctx):
                test.assertEqual(ctx.bars[-1], ctx.bar)
                test.assertEqual(len(ctx.bars), ctx.i + 1)
                test.assertEqual(ctx.series("x")[ctx.i], float(ctx.i))
                with test.assertRaises(IndexError):
                    ctx.bars[ctx.i + 1]
                with test.assertRaises(IndexError):
                    ctx.series("x")[ctx.i + 1]
                with test.assertRaises(ValueError):
                    ctx.ind("x", lag=-1)

        Backtest(flat(5), Peeker(), SPEC).run()

    def test_strategy_exception_propagates(self):
        class Boom(Strategy):
            def on_bar(self, ctx):
                raise ZeroDivisionError("стратегия упала")

        with self.assertRaises(ZeroDivisionError):
            Backtest(flat(3), Boom(), SPEC).run()

    def test_deterministic(self):
        bars = SmaCrossData.sine(1500)
        a = Backtest(bars, SmaCross(fast=10, slow=40), SPEC).run()
        b = Backtest(bars, SmaCross(fast=10, slow=40), SPEC).run()
        self.assertEqual(a.trades, b.trades)
        self.assertEqual(a.curve, b.curve)
        self.assertEqual(a.decisions, b.decisions)


class RiskIntegrationTest(unittest.TestCase):
    def test_sizing_by_stop_uses_risk_core(self):
        """Стоп 50%: риск 1% = 100 USDT / 50 на единицу = 2 ед. (под потолком 15%)."""
        s = Scripted(buy_at=[0], stop=50.0, equity_pct=None)
        r = Backtest(flat(4), s, SPEC).run()
        self.assertEqual(r.fills[0].qty, 2.0)

    def test_sizing_capped_by_position_limit(self):
        """Стоп 2%: риск даёт 50 ед. (5000 USDT), потолок 15% equity -> 15 ед."""
        r = Backtest(flat(4), Scripted(buy_at=[0], stop=98.0, equity_pct=None), SPEC).run()
        self.assertEqual(r.fills[0].qty, 15.0)
        r = Backtest(flat(4), Scripted(buy_at=[0], equity_pct=50.0), SPEC).run()
        self.assertEqual(r.fills[0].qty, 15.0)  # equity_pct не ослабляет потолок risk.py

    def test_buy_requires_sizing_input(self):
        with self.assertRaises(ValueError):
            Backtest(flat(3), Scripted(buy_at=[0], equity_pct=None), SPEC).run()

    def test_loss_streak_blocks_instrument_in_model_time(self):
        """3 убытка подряд -> блок инструмента 24ч МОДЕЛЬНОГО времени, затем входы снова."""
        bars = flat(24 * 4)
        r = Backtest(bars, ChurnStrategy(), SPEC).run()
        self.assertTrue(all(t.pnl < 0 for t in r.trades))
        blocked = [k for k in r.rejections if "заблокирован" in k]
        self.assertTrue(blocked, r.rejections)
        third_exit = r.trades[2].exit_ts
        fourth_signal = r.trades[3].entry_signal_ts
        self.assertGreaterEqual(fourth_signal + H - third_exit, 24 * H)
        self.assertLess(fourth_signal + H - third_exit, 26 * H)
        self.assertIn((third_exit / 1000.0, "instrument_blocked"), r.risk_events)

    def test_entries_per_day_limit(self):
        """Лимит 10 входов/сутки UTC: без убытков (рост цены) блокирует именно он."""
        ohlc = [(100 + i, 100 + i, 100 + i, 100 + i) for i in range(48)]
        r = Backtest(bars_from(ohlc), ChurnStrategy(), SPEC,
                     costs=CostModel().scaled(0.0, 0.0)).run()
        per_day = {}
        for t in r.trades:
            day = t.entry_ts // (24 * H)
            per_day[day] = per_day.get(day, 0) + 1
        self.assertLessEqual(max(per_day.values()), risk.MAX_ENTRIES_PER_DAY)
        self.assertTrue(any("входов в день" in k for k in r.rejections), r.rejections)

    def test_global_risk_state_untouched(self):
        """Прогон не трогает глобальное ядро и возвращает настоящие часы."""
        clock, core = risk._utc_now, risk._core
        Backtest(flat(50), ChurnStrategy(), SPEC).run()
        self.assertIs(risk._utc_now, clock)
        self.assertIs(risk._core, core)

    def test_no_file_backed_sqlite(self):
        """Риск-ядро прогона — только in-memory: data/risk_state.db не открывается."""
        orig, calls = sqlite3.connect, []

        def spy(database, *a, **k):
            calls.append(str(database))
            return orig(database, *a, **k)

        with mock.patch("sqlite3.connect", side_effect=spy):
            r = Backtest(flat(24 * 3), ChurnStrategy(), SPEC).run()
        self.assertTrue(r.trades)
        self.assertEqual(set(calls), {":memory:"})

    def test_buy_and_hold_uses_position_cap(self):
        bars = bars_from([(100, 100, 100, 100)] + [(100, 200, 100, 200)] * 3)
        r = Backtest(bars, BuyAndHold(), SPEC, costs=CostModel().scaled(0.0, 0.0)).run()
        # 15% капитала -> 15 ед. по 100; к концу цена 200: +1500 USDT
        self.assertAlmostEqual(r.final_equity, 11_500.0, places=6)
        self.assertEqual(r.trades[0].exit_reason, "end_of_data")


class SmaCrossData:
    @staticmethod
    def sine(n, t0=T0):
        out, prev = [], 100.0
        for i in range(n):
            c = 100 + 10 * math.sin(i / 25.0) + 2 * math.sin(i / 4.0)
            o = prev
            out.append(Bar(t0 + i * H, o, max(o, c) + 0.2, min(o, c) - 0.2, c, 1.0))
            prev = c
        return out


if __name__ == "__main__":
    unittest.main()
