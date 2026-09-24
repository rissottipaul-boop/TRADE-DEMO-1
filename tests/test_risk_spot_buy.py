"""Риск-ядро: API доливки спот-позиции register_spot_buy (задача RISK-DCA-SLOT).

Ключевой инвариант: покупки DCA (register_entry + register_spot_buy) НЕ обнуляют
глобальную серию убытков — 5 убытков подряд другой стратегии обязаны поставить
системную паузу даже при работающем DCA. Регрессия на старый хак
record_pnl(inst, 0): ветка pnl >= 0 сбрасывала global_loss_streak.
"""
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

from src import risk

SPOT_INST = "BTC/USDT"        # инструмент DCA (спот)
STRAT_INST = "ETH-USDT-SWAP"  # инструмент «другой стратегии»


class SpotBuyApiTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        risk.init(Path(self._tmp.name) / "risk.db")
        risk.update_equity(10_000)

    def tearDown(self):
        risk.init(Path(self._tmp.name) / "unused.db")  # отпустить файл до очистки
        self._tmp.cleanup()

    @staticmethod
    def _dca_buy() -> None:
        """Симуляция одной покупки DCA: роутер регистрирует вход, бот отпускает слот."""
        risk.register_entry(SPOT_INST, "buy")
        risk.register_spot_buy(SPOT_INST)

    def test_system_pause_with_running_dca(self):
        """5 убытков подряд другой стратегии при работающем DCA -> системная пауза."""
        now = datetime.now(tz=timezone.utc)
        pause_at = None
        for i in range(5):
            self._dca_buy()  # DCA покупает между убытками
            events = risk.record_pnl(STRAT_INST, -5.0, now)
            if "system_pause" in events:
                pause_at = i + 1
        self.assertEqual(pause_at, 5, "пауза обязана сработать ровно на 5-м убытке")
        allowed, reason = risk.check_entry_allowed("SOL-USDT-SWAP", "buy")
        self.assertFalse(allowed)
        self.assertIn("пауза", reason)

    def test_spot_buy_does_not_reset_loss_streak(self):
        """Покупка DCA между убытками не сбрасывает глобальную серию."""
        now = datetime.now(tz=timezone.utc)
        risk.record_pnl(STRAT_INST, -5.0, now)
        self._dca_buy()
        risk.record_pnl(STRAT_INST, -5.0, now)
        self.assertEqual(risk.status()["global_loss_streak"], 2)

    def test_spot_buy_frees_risk_slot(self):
        """После покупки слот open_risk свободен — следующая покупка не «хедж»."""
        risk.register_entry(SPOT_INST, "buy")
        risk.register_spot_buy(SPOT_INST)
        self.assertEqual(risk.status()["open_risk"], [])
        allowed, reason = risk.check_entry_allowed(SPOT_INST, "buy")
        self.assertTrue(allowed, reason)

    def test_spot_buy_does_not_touch_pnl_and_counters(self):
        """Доливка не меняет equity/day_pnl и не дублирует entries_today."""
        before = risk.status()
        risk.register_entry(SPOT_INST, "buy")
        risk.register_spot_buy(SPOT_INST)
        after = risk.status()
        self.assertEqual(after["equity"], before["equity"])
        self.assertEqual(after["day_pnl"], before["day_pnl"])
        # entries_today вырос ровно на 1 (от register_entry), без дабл-каунта
        self.assertEqual(after["entries_today"], before["entries_today"] + 1)

    def test_spot_buy_without_slot_is_noop(self):
        """Идемпотентно: доливка без открытого слота не падает и ничего не ломает."""
        risk.register_spot_buy(SPOT_INST)
        self.assertEqual(risk.status()["open_risk"], [])


if __name__ == "__main__":
    unittest.main()
