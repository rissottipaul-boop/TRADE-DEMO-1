"""Тесты live-обвязки mean-reversion (src/mean_reversion.py). Без сети/биржи."""
import math
import tempfile
import unittest
from pathlib import Path

from src import risk
from src.backtest.data import Bar
from src.mean_reversion import (LivePosition, check_entry, check_exit,
                                compute_indicators, stop_price, suggest_size)


def flat_bars(n, px=100.0, vol=10.0):
    return [Bar(ts=i * 3600, o=px, h=px, l=px, c=px, vol=vol)
            for i in range(n)]


class IndicatorsTest(unittest.TestCase):
    def test_warmup_required(self):
        with self.assertRaises(ValueError):
            compute_indicators(flat_bars(50))

    def test_shapes_and_finite(self):
        bars = flat_bars(250)
        ind = compute_indicators(bars)
        self.assertEqual(len(ind.rsi), 250)
        self.assertEqual(len(ind.bb_mid), 250)
        self.assertEqual(len(ind.atr), 250)
        self.assertTrue(all(math.isfinite(v) for v in ind.bb_mid[-5:]))


class EntryTest(unittest.TestCase):
    def test_flat_series_no_entry(self):
        bars = flat_bars(250)
        ind = compute_indicators(bars)
        self.assertFalse(check_entry(bars, ind))


class StopTest(unittest.TestCase):
    def test_stop_price_math(self):
        self.assertAlmostEqual(stop_price(100.0, 2.0), 97.0)


class ExitTest(unittest.TestCase):
    def setUp(self):
        bars = flat_bars(250, px=100.0)
        self.bars = bars
        self.ind = compute_indicators(bars)

    def test_stop_first_priority(self):
        pos = LivePosition(entry_px=100.0, stop_px=97.0, entry_minute=0.0)
        bar = Bar(ts=0, o=98.0, h=99.0, l=96.0, c=103.0, vol=5.0)
        self.assertEqual(check_exit(pos, bar, 10.0, self.bars, self.ind), "stop")

    def test_roi(self):
        pos = LivePosition(entry_px=100.0, stop_px=90.0, entry_minute=0.0)
        bar = Bar(ts=0, o=102.0, h=104.0, l=101.0, c=103.0, vol=5.0)
        self.assertEqual(check_exit(pos, bar, 10.0, self.bars, self.ind), "roi")

    def test_time_stop(self):
        pos = LivePosition(entry_px=100.0, stop_px=90.0, entry_minute=0.0)
        bar = Bar(ts=0, o=100.0, h=100.5, l=99.5, c=100.0, vol=5.0)
        self.assertEqual(check_exit(pos, bar, 1500.0, self.bars, self.ind),
                         "time_stop")

    def test_hold(self):
        pos = LivePosition(entry_px=100.0, stop_px=90.0, entry_minute=0.0)
        bar = Bar(ts=0, o=100.0, h=100.5, l=99.5, c=100.5, vol=5.0)
        self.assertIsNone(check_exit(pos, bar, 10.0, self.bars, self.ind))


class SizeTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        risk.init(Path(self._tmp.name) / "risk.db")

    def tearDown(self):
        risk.init(Path(self._tmp.name) / "unused.db")
        self._tmp.cleanup()

    def test_suggest_size_via_core(self):
        out = suggest_size(10_000.0, 100.0, 97.0, 1.0, 0.001, 0.001)
        self.assertGreater(out["size"], 0.0)
        self.assertIn("notional", out)


if __name__ == "__main__":
    unittest.main()
