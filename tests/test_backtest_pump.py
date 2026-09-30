import unittest

from src.backtest.data import Bar, InstrumentSpec
from src.backtest.engine import Backtest, CostModel
from src.backtest.pump import PumpStrategy, causal_rolling_median


class TestBacktestPump(unittest.TestCase):
    def setUp(self):
        self.spec = InstrumentSpec("TEST-USDT", tick_sz=0.01, lot_sz=0.001, min_sz=0.001)
        self.zero_costs = CostModel(taker_fee=0.0, maker_fee=0.0, slippage_bps=0.0)

    def test_causal_rolling_median(self):
        vals = [10.0] * 30
        med = causal_rolling_median(vals, 20)
        self.assertEqual(len(med), 30)
        # first 20 bars should be nan
        import math
        for i in range(20):
            self.assertTrue(math.isnan(med[i]))
        for i in range(20, 30):
            self.assertEqual(med[i], 10.0)

    def test_pump_strategy_prepare_shapes(self):
        # 60 synthetic bars
        bars = [Bar(ts=1000 + i * 3600_000, o=100.0 + i, h=102.0 + i, l=99.0 + i, c=101.0 + i, vol=100.0)
                for i in range(60)]
        strat = PumpStrategy()
        ind = strat.prepare(bars)
        self.assertIn("impulse", ind)
        self.assertIn("vol_ratio", ind)
        self.assertIn("rsi", ind)
        self.assertIn("ma20", ind)
        self.assertIn("macd_hist", ind)
        for k, v in ind.items():
            self.assertEqual(len(v), 60)

    def test_pump_signal_and_execution(self):
        # Generate bars that satisfy all 5 conditions on bar 40
        # 1) impulse >= 2.0%
        # 2) vol_ratio >= 1.5x (previous 20 vols = 10.0, current vol = 30.0)
        # 3) RSI in [50, 72]
        # 4) close > MA20
        # 5) MACD hist > 0
        import math
        bars = []
        for i in range(54):
            c = 100.0 + math.sin(i * 0.5) * 1.5
            o = 100.0 + math.sin((i - 0.5) * 0.5) * 1.5
            bars.append(Bar(ts=1000 + i * 3600_000, o=o, h=max(o, c) + 0.3, l=min(o, c) - 0.3, c=c, vol=10.0))

        last_c = bars[-1].c
        bars.append(Bar(ts=1000 + 54 * 3600_000, o=last_c, h=last_c + 0.3, l=last_c - 0.3, c=last_c, vol=10.0))
        b54_c = bars[-1].c
        # bar 55: pump bar (+2.1% impulse, vol 40 vs median 10, RSI ~71.5, c > ma20, macd > 0)
        bars.append(Bar(ts=1000 + 55 * 3600_000, o=b54_c, h=b54_c * 1.025, l=b54_c * 0.998, c=b54_c * 1.021, vol=40.0))
        bars.append(Bar(ts=1000 + 56 * 3600_000, o=103.6, h=110.0, l=103.0, c=108.0, vol=15.0))
        for i in range(57, 80):
            bars.append(Bar(ts=1000 + i * 3600_000, o=108.0, h=108.5, l=105.0, c=106.0, vol=15.0))

        strat = PumpStrategy(impulse_min=2.0, vol_ratio_min=1.5, stop_loss_pct=0.02,
                             take_profit_pct=0.10, trailing_stop_pct=0.02, trailing_activation_pct=0.025,
                             time_stop_bars=10)
        bt = Backtest(bars, strat, self.spec, costs=self.zero_costs, initial_cash=10000.0)
        res = bt.run()
        # Should have executed a trade
        self.assertGreaterEqual(len(res.trades), 1)
        t = res.trades[0]
        self.assertEqual(t.tag, "pump_signal")
        self.assertGreater(t.pnl, 0)

    def test_pump_rejection_when_impulse_fails(self):
        # All bars flat impulse (< 0.5%)
        bars = [Bar(ts=1000 + i * 3600_000, o=100.0, h=100.2, l=99.8, c=100.1, vol=10.0) for i in range(50)]
        strat = PumpStrategy(impulse_min=2.0)
        bt = Backtest(bars, strat, self.spec, costs=self.zero_costs, initial_cash=10000.0)
        res = bt.run()
        self.assertEqual(len(res.trades), 0)


if __name__ == "__main__":
    unittest.main()
