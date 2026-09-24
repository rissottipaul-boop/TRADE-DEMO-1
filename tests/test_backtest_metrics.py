"""Бэктестер: метрики §6.1, гейт §6.2, walk-forward §2 (окна, purge/embargo, holdout)."""
import math
import unittest

from src.backtest.data import Bar, InstrumentSpec
from src.backtest.engine import Backtest, Trade
from src.backtest.metrics import (DAY_MS, GateConfig, compute_metrics, daily_returns, drawdown,
                                  evaluate_gate, period_returns, sharpe_sortino, trade_stats)
from src.backtest.strategies import BuyAndHold, SmaCross
from src.backtest.walkforward import (Segment, WalkForwardConfig, concat_results, make_windows,
                                      run_segment, walk_forward)

H = 3_600_000
T0 = 1_767_225_600_000  # 2026-01-01 00:00 UTC
SPEC = InstrumentSpec("TEST-USDT", 0.01, 1e-8, 1e-5)


def trade(pnl, cost=1000.0, fees=1.0, slip=0.5, hours=5):
    return Trade("X", 0, 0, hours * H, 100, 100, 1, 1, cost, cost + pnl, fees, slip, pnl,
                 pnl / cost, "signal", hours)


def sine_bars(n, t0=T0):
    out, prev = [], 100.0
    for i in range(n):
        c = 100 + 10 * math.sin(i / 25.0) + 2 * math.sin(i / 4.0)
        out.append(Bar(t0 + i * H, prev, max(prev, c) + 0.2, min(prev, c) - 0.2, c, 1.0))
        prev = c
    return out


class MetricsTest(unittest.TestCase):
    def test_drawdown_depth_and_duration(self):
        curve = [(T0, 100.0), (T0 + DAY_MS, 120.0), (T0 + 2 * DAY_MS, 90.0),
                 (T0 + 3 * DAY_MS, 110.0), (T0 + 5 * DAY_MS, 130.0)]
        dd = drawdown(curve)
        self.assertAlmostEqual(dd["mdd_pct"], 25.0)
        self.assertEqual((dd["mdd_peak_ts"], dd["mdd_trough_ts"]), (T0 + DAY_MS, T0 + 2 * DAY_MS))
        self.assertAlmostEqual(dd["underwater_days"], 4.0)

    def test_daily_returns_and_sharpe(self):
        # точки на close 00:00 следующих суток — относятся к предыдущим суткам
        curve = [(T0, 100.0), (T0 + 12 * H, 105.0), (T0 + DAY_MS, 101.0),
                 (T0 + 2 * DAY_MS, 99.99), (T0 + 3 * DAY_MS, 101.9898)]
        rets = daily_returns(curve)
        self.assertEqual(len(rets), 3)
        for got, exp in zip(rets, [0.01, -0.01, 0.02]):
            self.assertAlmostEqual(got, exp, places=9)
        sh, so = sharpe_sortino([0.01, -0.01, 0.02])
        mean, std = 0.02 / 3, math.sqrt(sum((r - 0.02 / 3) ** 2 for r in (0.01, -0.01, 0.02)) / 2)
        self.assertAlmostEqual(sh, mean / std * math.sqrt(365))
        self.assertAlmostEqual(so, mean / math.sqrt(0.0001 / 3) * math.sqrt(365))
        self.assertEqual(sharpe_sortino([0.01]), (None, None))

    def test_trade_stats(self):
        s = trade_stats([trade(10), trade(-5), trade(-5), trade(-2), trade(30)])
        self.assertEqual((s["trades"], s["wins"], s["losses"]), (5, 2, 3))
        self.assertAlmostEqual(s["hit_rate_pct"], 40.0)
        self.assertAlmostEqual(s["profit_factor"], 40 / 12)
        self.assertAlmostEqual(s["expectancy"], 28 / 5)
        # expectancy = hit*avg_win - (1-hit)*avg_loss (определение §6.1)
        self.assertAlmostEqual(s["expectancy"], 0.4 * 20 - 0.6 * 4)
        self.assertEqual(s["max_loss_streak"], 3)
        self.assertAlmostEqual(s["costs"], 7.5)
        self.assertAlmostEqual(s["gross_pnl"], 28 + 7.5)
        self.assertAlmostEqual(s["cost_share_pct"], 7.5 / 35.5 * 100)
        self.assertIsNone(trade_stats([trade(-10)])["cost_share_pct"])  # брутто <= 0
        self.assertEqual(trade_stats([trade(5)])["profit_factor"], math.inf)

    def test_cagr_calmar(self):
        curve = [(T0, 100.0), (T0 + 100 * DAY_MS, 80.0), (T0 + 365 * DAY_MS, 200.0)]
        m = compute_metrics(curve, [], 100.0, benchmark=0.5, exposure=0.25)
        self.assertAlmostEqual(m["cagr_pct"], 100.0)
        self.assertAlmostEqual(m["mdd_pct"], 20.0)
        self.assertAlmostEqual(m["calmar"], 5.0)
        self.assertEqual((m["benchmark_pct"], m["exposure_pct"]), (50.0, 25.0))

    def test_period_returns(self):
        curve = [(T0, 100.0), (T0 + 31 * DAY_MS, 110.0), (T0 + 59 * DAY_MS, 99.0)]
        self.assertEqual([(k, round(v, 6)) for k, v in period_returns(curve, "month")],
                         [("2026-01", 10.0), ("2026-02", -10.0)])
        self.assertEqual(period_returns(curve, "year")[0][0], "2026")


class GateTest(unittest.TestCase):
    GOOD = {"expectancy": 2.0, "profit_factor": 1.6, "sharpe": 0.9, "mdd_pct": 6.0,
            "trades": 150, "cost_share_pct": 30.0, "max_loss_streak": 5}

    def test_all_pass(self):
        windows = [{"net_profit": 1.0, "trades": 40}] * 7 + [{"net_profit": -1.0, "trades": 35}] * 3
        g = evaluate_gate(self.GOOD, self.GOOD, windows)
        self.assertTrue(g["passed"], g["checks"])
        self.assertEqual(len(g["checks"]), 8)

    def test_each_threshold_can_fail(self):
        windows = [{"net_profit": 1.0, "trades": 40}] * 10
        cases = {"2": {"profit_factor": 1.29}, "3": {"sharpe": 0.49}, "4": {"mdd_pct": 10.01},
                 "5": {"trades": 99}, "7": {"cost_share_pct": 50.0}, "8": {"max_loss_streak": 8}}
        for cid, patch in cases.items():
            g = evaluate_gate({**self.GOOD, **patch}, self.GOOD, windows)
            failed = [c["id"] for c in g["checks"] if not c["ok"]]
            self.assertEqual(failed, [cid], (cid, g["checks"]))
        g = evaluate_gate(self.GOOD, {**self.GOOD, "expectancy": 0.0}, windows)
        self.assertEqual([c["id"] for c in g["checks"] if not c["ok"]], ["1"])
        g = evaluate_gate(self.GOOD, self.GOOD, [{"net_profit": 1.0, "trades": 40}] * 5
                          + [{"net_profit": -1.0, "trades": 40}] * 5)
        self.assertEqual([c["id"] for c in g["checks"] if not c["ok"]], ["6"])
        self.assertFalse(evaluate_gate({**self.GOOD, "sharpe": None}, self.GOOD, windows)["passed"])

    def test_thresholds_are_config(self):
        g = evaluate_gate({**self.GOOD, "sharpe": 0.3}, self.GOOD,
                          [{"net_profit": 1.0, "trades": 40}] * 10, GateConfig(min_sharpe=0.2))
        self.assertTrue(g["passed"])


class WalkForwardTest(unittest.TestCase):
    CFG = WalkForwardConfig(train_days=20, valid_days=5, test_days=5, step_days=5,
                            holdout_days=10, top_k=2)
    GRID = [{"fast": 5, "slow": 20}, {"fast": 10, "slow": 30}, {"fast": 8, "slow": 40}]

    def setUp(self):
        self.bars = sine_bars(24 * 80)

    def test_windows_layout(self):
        ts = [b.ts for b in self.bars]
        windows, holdout = make_windows(ts, H, self.CFG, origin_idx=80)
        self.assertEqual(ts[holdout.start], T0 + 70 * DAY_MS)
        self.assertEqual(len(holdout), 24 * 10)
        self.assertGreater(len(windows), 3)
        for w in windows:
            self.assertEqual(w.train.end, w.valid.start)
            self.assertEqual(w.valid.start + 5 * 24, w.test.start)
            self.assertEqual(len(w.train), 20 * 24)
            self.assertLessEqual(w.test.end, holdout.start)   # test не заходит в holdout
        for a, b in zip(windows, windows[1:]):
            self.assertEqual(b.train.start - a.train.start, 5 * 24)
            self.assertEqual(a.test.end, b.test.start)         # test-окна стыкуются

    def test_purge_embargo_and_holdout(self):
        wf = walk_forward(self.bars, SmaCross, self.GRID, SPEC, cfg=self.CFG)
        self.assertEqual(wf.trials, len(self.GRID) * len(wf.windows))
        holdout_ts = self.bars[wf.holdout.start].ts
        n_trades = 0
        for w in wf.windows:
            start_ts = self.bars[w.window.test.start].ts
            end_close = self.bars[w.window.test.end - 1].ts + H
            for t in w.test.trades:
                n_trades += 1
                self.assertGreaterEqual(t.entry_signal_ts, start_ts)   # embargo: не в прогреве
                self.assertLessEqual(t.exit_ts, end_close)              # purge: не за границей
                self.assertLess(t.exit_ts, holdout_ts + 1)
            self.assertEqual(w.test.trade_start_ts, start_ts)
        self.assertGreater(n_trades, 5)
        self.assertIsNone(wf.holdout_result)                           # holdout не тронут
        wf2 = walk_forward(self.bars, SmaCross, self.GRID, SPEC, cfg=self.CFG,
                           touch_holdout=True)
        self.assertEqual(wf2.holdout_result.trade_start_ts, holdout_ts)
        self.assertEqual(wf2.holdout_params, wf2.windows[-1].params)

    def test_selection_uses_only_train_and_valid(self):
        wf = walk_forward(self.bars, SmaCross, self.GRID, SPEC, cfg=self.CFG)
        w = wf.windows[0].window
        kw = dict(bar="1H", costs=wf.windows[0].test.costs, initial_cash=10_000.0, embargo=80)

        def score(seg, p):
            m = run_segment(self.bars, seg, SmaCross(**p), SPEC, **kw).metrics()
            return (m["sharpe"] if m["sharpe"] is not None else float("-inf"),
                    m["net_profit_pct"])

        ranked = sorted(range(len(self.GRID)), key=lambda i: (score(w.train, self.GRID[i]), -i),
                        reverse=True)[:2]
        best = max(ranked, key=lambda i: (score(w.valid, self.GRID[i]), -i))
        self.assertEqual(wf.windows[0].params, self.GRID[best])

    def test_stress_costs_worse(self):
        wf = walk_forward(self.bars, SmaCross, self.GRID[:1], SPEC, cfg=self.CFG)
        self.assertLess(wf.oos_stress.final_equity, wf.oos.final_equity)

    def test_concat_chains_returns(self):
        a = Backtest(self.bars[:500], BuyAndHold(), SPEC).run()
        b = Backtest(self.bars[500:1000], BuyAndHold(), SPEC).run()
        c = concat_results([a, b], 10_000.0)
        exp = 10_000 * (a.final_equity / 10_000) * (b.final_equity / 10_000)
        self.assertAlmostEqual(c.final_equity, exp, places=6)
        self.assertEqual(len(c.curve), len(a.curve) + len(b.curve) - 1)
        self.assertAlmostEqual(c.benchmark_return,
                               (1 + a.benchmark_return) * (1 + b.benchmark_return) - 1)
        self.assertAlmostEqual(c.trades[1].pnl, b.trades[0].pnl * a.final_equity / 10_000)


if __name__ == "__main__":
    unittest.main()
