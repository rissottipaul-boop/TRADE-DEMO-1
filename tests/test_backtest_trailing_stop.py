import unittest

from src.backtest.data import Bar, InstrumentSpec
from src.backtest.engine import Backtest, Context, CostModel, Strategy


class DummyTrailStrategy(Strategy):
    name = "dummy_trail"

    def __init__(self, stop: float = 98.0, trailing_stop_pct: float = 0.02,
                 trailing_activation_pct: float = None):
        super().__init__(stop=stop, trailing_stop_pct=trailing_stop_pct,
                         trailing_activation_pct=trailing_activation_pct)
        self.warmup = 0

    def prepare(self, bars):
        return {}

    def on_bar(self, ctx: Context) -> None:
        if ctx.i == 0 and ctx.position is None and not ctx.has_pending:
            ctx.buy(stop=self.params["stop"],
                    trailing_stop_pct=self.params["trailing_stop_pct"],
                    trailing_activation_pct=self.params["trailing_activation_pct"],
                    equity_pct=0.1)


class TestBacktestTrailingStop(unittest.TestCase):
    def setUp(self):
        self.spec = InstrumentSpec("TEST-USDT", tick_sz=0.01, lot_sz=0.001, min_sz=0.001)
        self.zero_costs = CostModel(taker_fee=0.0, maker_fee=0.0, slippage_bps=0.0)

    def test_trailing_stop_ratchet_and_exit(self):
        bars = [
            Bar(ts=1000, o=100.0, h=101.0, l=99.0, c=100.0, vol=10.0),  # bar 0: signal buy
            Bar(ts=2000, o=100.0, h=105.0, l=99.5, c=104.0, vol=10.0),  # bar 1: entry at 100, high 105 -> stop = 105 * 0.98 = 102.9
            Bar(ts=3000, o=104.0, h=104.5, l=102.0, c=103.0, vol=10.0), # bar 2: low 102.0 <= 102.9 -> trailing stop hit
        ]
        strat = DummyTrailStrategy(stop=98.0, trailing_stop_pct=0.02)
        bt = Backtest(bars, strat, self.spec, costs=self.zero_costs, initial_cash=10000.0)
        res = bt.run()
        self.assertEqual(len(res.trades), 1)
        t = res.trades[0]
        self.assertEqual(t.exit_reason, "trailing_stop")
        self.assertAlmostEqual(t.entry_px, 100.0)
        self.assertAlmostEqual(t.exit_px, 102.9)
        self.assertGreater(t.pnl, 0)

    def test_trailing_stop_activation_pct(self):
        # Activation at +5%: price must reach 105 to activate trail
        bars = [
            Bar(ts=1000, o=100.0, h=101.0, l=99.0, c=100.0, vol=10.0),  # bar 0: signal
            Bar(ts=2000, o=100.0, h=103.0, l=99.5, c=102.0, vol=10.0),  # bar 1: high 103 (+3%), not activated -> stop still 98.0
            Bar(ts=3000, o=102.0, h=102.5, l=98.5, c=100.0, vol=10.0),  # bar 2: low 98.5 > 98.0 -> alive
            Bar(ts=4000, o=100.0, h=106.0, l=99.5, c=105.0, vol=10.0),  # bar 3: high 106 (+6% >= +5%) -> activated, stop = 106*0.98 = 103.88
            Bar(ts=5000, o=105.0, h=105.0, l=103.0, c=103.5, vol=10.0), # bar 4: low 103.0 <= 103.88 -> hit trailing stop
        ]
        strat = DummyTrailStrategy(stop=98.0, trailing_stop_pct=0.02, trailing_activation_pct=0.05)
        bt = Backtest(bars, strat, self.spec, costs=self.zero_costs, initial_cash=10000.0)
        res = bt.run()
        self.assertEqual(len(res.trades), 1)
        t = res.trades[0]
        self.assertEqual(t.exit_reason, "trailing_stop")
        self.assertAlmostEqual(t.exit_px, 103.88)

    def test_low_before_high_protective_order(self):
        # On bar 2, price gaps down to 97.0 (below stop 98.0) and then reaches high 110.0
        # "Low раньше high": stop should execute at open/stop without seeing 110.0
        bars = [
            Bar(ts=1000, o=100.0, h=100.0, l=99.0, c=100.0, vol=10.0),
            Bar(ts=2000, o=100.0, h=100.0, l=99.5, c=100.0, vol=10.0),
            Bar(ts=3000, o=97.0, h=110.0, l=96.0, c=108.0, vol=10.0),
        ]
        strat = DummyTrailStrategy(stop=98.0, trailing_stop_pct=0.02)
        bt = Backtest(bars, strat, self.spec, costs=self.zero_costs, initial_cash=10000.0)
        res = bt.run()
        self.assertEqual(len(res.trades), 1)
        t = res.trades[0]
        self.assertEqual(t.exit_reason, "stop_loss")
        self.assertAlmostEqual(t.exit_px, 97.0)


if __name__ == "__main__":
    unittest.main()
