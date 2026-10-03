"""Риск-ядро: учёт funding SWAP в record_pnl (задача RISK-FUNDING-PNL).

Контракт (уточнение 12:48): funding за время позиции входит в day_pnl
и серии убытков как часть итога сделки (pnl + funding), а equity и HWM
не меняет — там funding уже учтён через баланс биржи (update_equity).
"""
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

from src import risk

INST = "ETH-USDT-SWAP"


class FundingPnlTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        risk.init(Path(self._tmp.name) / "risk.db")
        risk.update_equity(10_000)

    def tearDown(self):
        risk.init(Path(self._tmp.name) / "unused.db")  # отпустить файл до очистки
        self._tmp.cleanup()

    def test_funding_changes_day_pnl_not_equity(self):
        """funding входит в day_pnl, equity не двигается."""
        now = datetime.now(tz=timezone.utc)
        before = risk.status()
        risk.record_pnl(INST, -5.0, now, funding=8.0)  # итог +3.0
        st = risk.status()
        self.assertAlmostEqual(st["day_pnl"] - before["day_pnl"], 3.0)
        self.assertAlmostEqual(st["equity"], before["equity"])
        self.assertAlmostEqual(st["hwm"], before["hwm"])

    def test_positive_funding_resets_loss_streak(self):
        """Положительный итог с funding сбрасывает серию убытков."""
        now = datetime.now(tz=timezone.utc)
        risk.record_pnl(INST, -5.0, now)
        self.assertEqual(risk.status()["global_loss_streak"], 1)
        risk.record_pnl(INST, -5.0, now, funding=8.0)  # итог +3.0
        self.assertEqual(risk.status()["global_loss_streak"], 0)

    def test_negative_funding_extends_loss_streak(self):
        """Отрицательный funding превращает прибыльную сделку в убыток серии."""
        now = datetime.now(tz=timezone.utc)
        risk.record_pnl(INST, 2.0, now, funding=-5.0)  # итог -3.0
        st = risk.status()
        self.assertEqual(st["global_loss_streak"], 1)
        self.assertAlmostEqual(st["day_pnl"], -3.0)

    def test_default_funding_zero_keeps_old_behavior(self):
        """Без funding поведение прежнее: день и серии только по pnl."""
        now = datetime.now(tz=timezone.utc)
        risk.record_pnl(INST, -7.0, now)
        st = risk.status()
        self.assertAlmostEqual(st["day_pnl"], -7.0)
        self.assertEqual(st["global_loss_streak"], 1)
        self.assertAlmostEqual(st["equity"], 10_000)


if __name__ == "__main__":
    unittest.main()
