"""Риск-ядро: breakers срабатывают от equity с биржи, а не только от record_pnl."""
import tempfile
import unittest
from pathlib import Path

from src import risk


class EquityBreakerTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        risk.init(Path(self._tmp.name) / "risk.db")

    def tearDown(self):
        risk.init(Path(self._tmp.name) / "unused.db")  # отпустить файл до очистки
        self._tmp.cleanup()

    def test_small_drop_allowed(self):
        risk.update_equity(10_000)
        self.assertEqual(risk.update_equity(9_500), [])
        self.assertTrue(risk.check_entry_allowed("BTC-USDT", "buy")[0])

    def test_daily_limit_from_unrealized_loss(self):
        risk.update_equity(10_000)
        self.assertEqual(risk.update_equity(9_390), ["daily_limit"])  # −6.1% без закрытых сделок
        allowed, reason = risk.check_entry_allowed("BTC-USDT", "buy")
        self.assertFalse(allowed)
        self.assertIn("дневной", reason)

    def test_global_breaker_from_drawdown(self):
        risk.update_equity(10_000)
        risk.update_equity(12_000)  # новый HWM
        events = risk.update_equity(10_150)  # −15.4% от HWM
        self.assertIn("global_breaker", events)
        self.assertTrue(risk.status()["global_breaker"])
        # повторное обновление не дублирует событие (идемпотентно)
        self.assertNotIn("global_breaker", risk.update_equity(10_100))


if __name__ == "__main__":
    unittest.main()
