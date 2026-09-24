"""DCABot отдаёт риск-ядру totalEq счёта, как движок (задача DCA-EQUITY-SRC).

В demo движок (engine._update_equity) и DCABot пишут в один data/risk_state.db.
Раньше бот отдавал риск-ядру USDT-баланс: HWM ≈ 104 000 от движка (totalEq) и
16 600 USDT от бота давали глобальный breaker на «просадке» −84%, а снять его
может только человек. Теперь оба процесса отдают одну величину — totalEq из
GET /api/v5/account/balance. Если equity не прочитан, бот update_equity не зовёт:
вход решает свежесть equity от другого фида (insights/risk-core.md §6).

Фид движка здесь — его собственный код (TradingEngine._update_equity на фейковой
бирже), engine.py не меняется. Без сети; часы риск-ядра — фейковые (risk._utc_now).
"""
import asyncio
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import ccxt

from src import risk
from src.dca_bot import DCABot
from src.engine import TradingEngine
from src.order_router import OrderRouter
from src.storage import Storage

T0 = datetime(2026, 9, 24, 6, 0, tzinfo=timezone.utc).timestamp()  # середина суток UTC
ENGINE_HWM = 104_000.0  # totalEq demo-счёта (USD) — HWM движка в общем состоянии
USDT_ONLY = 16_600.0    # USDT-баланс того же счёта — прежний источник бота
PX = 60_000.0
_MISSING = object()


class FakeOkx:
    """Demo-счёт без сети: account/balance (totalEq, USD) и USDT-баланс CCXT."""

    def __init__(self, total_eq: object = "104000", balance_error: Exception | None = None,
                 balance_resp: dict | None = None) -> None:
        self.total_eq = total_eq
        self.balance_error = balance_error
        self.balance_resp = balance_resp
        self.created: list[dict] = []
        self.headers: dict = {}
        self.fetch_balance_calls = 0

    def milliseconds(self) -> int:
        return 1_790_000_000_000

    def private_get_account_balance(self, params=None):
        if self.balance_error is not None:
            raise self.balance_error
        if self.balance_resp is not None:
            return self.balance_resp
        row = {"upl": "0", "details": [{"ccy": "USDT", "availBal": str(USDT_ONLY), "eq": str(USDT_ONLY)}]}
        if self.total_eq is not _MISSING:
            row["totalEq"] = self.total_eq
        return {"code": "0", "msg": "", "data": [row]}

    def fetch_balance(self):  # только USDT: риск-ядру не источник
        self.fetch_balance_calls += 1
        return {"USDT": {"free": USDT_ONLY, "used": 0.0, "total": USDT_ONLY}, "total": {"USDT": USDT_ONLY}}

    def fetch_ticker(self, symbol):
        return {"symbol": symbol, "last": PX}

    def create_order(self, symbol, ord_type, side, amount, price=None, params=None):
        self.created.append({"symbol": symbol, "type": ord_type, "side": side,
                             "amount": amount, "params": dict(params or {})})
        return {"id": f"x{len(self.created)}", "status": "closed", "symbol": symbol}

    def fetch_order(self, order_id, symbol=None):
        return {"id": order_id, "status": "closed", "symbol": symbol}

    def private_get_account_config(self, params=None):  # режим аккаунта для tdMode (SPOT-TDMODE)
        return {"code": "0", "data": [{"acctLv": "1", "autoLoan": False}]}


class _Clock:
    """Часы риск-ядра: время двигает тест."""

    def __init__(self, t: float) -> None:
        self.t = t

    def __call__(self) -> float:
        return self.t


class DemoEquitySourceTest(unittest.TestCase):
    """Движок и DCABot на одном риск-состоянии и одном bot_state.db, как в demo."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        root = Path(self._tmp.name)
        self.clock = _Clock(T0)
        patcher = mock.patch.object(risk, "_utc_now", self.clock)
        patcher.start()
        self.addCleanup(patcher.stop)
        risk.init(root / "risk_state.db")
        self.storage = Storage(root / "bot_state.db")  # общий с движком, как data/bot_state.db

    def tearDown(self):
        self.storage.close()
        risk.init(Path(self._tmp.name) / "unused.db")  # отпустить файл до очистки
        self._tmp.cleanup()

    def tick(self, seconds: float) -> None:
        self.clock.t += seconds

    def engine_feed(self, ex: FakeOkx) -> None:
        """Фид движка — его собственный код, engine.py не меняется."""
        asyncio.run(TradingEngine._update_equity(SimpleNamespace(ex=ex, db=self.storage)))

    def bot(self, ex: FakeOkx) -> DCABot:
        router = OrderRouter(ex, storage=self.storage)
        self.addCleanup(router.close)
        return DCABot(ex, router, max_buys=1, interval_sec=0, demo=True,
                      storage=self.storage, sleep=lambda s: None)

    def run_quiet(self, bot: DCABot) -> str:
        """run(), где предупреждения ожидаемы (баланс не прочитан, отказ риск-слоя)."""
        with self.assertLogs(level="WARNING"):
            return bot.run()

    # --- Критерий: движок (HWM ≈ 104k) и DCABot на одном состоянии ---

    def test_engine_hwm_and_dca_bot_on_same_state_no_breaker(self):
        ex = FakeOkx(total_eq="104000")
        self.engine_feed(ex)
        self.assertEqual(risk.status()["hwm"], ENGINE_HWM)
        self.tick(60)
        ex.total_eq = "103950.5"  # счёт чуть сдвинулся с рынком
        final = self.bot(ex).run()
        st = risk.status()
        self.assertEqual(final, DCABot.STATE_DONE, "бот купил: ложного breaker'а нет")
        self.assertEqual(len(ex.created), 1)
        self.assertFalse(st["global_breaker"], "было: USDT 16 600 против HWM 104 000 — «−84%»")
        self.assertFalse(st["daily_breaker"])
        self.assertEqual(st["hwm"], ENGINE_HWM)
        self.assertEqual(st["equity"], 103_950.5, "бот отдал totalEq, а не USDT-баланс")
        self.assertEqual(st["equity_updated_at"], self.clock.t, "свежесть продлил фид бота")
        self.assertEqual(ex.fetch_balance_calls, 0, "USDT-баланс риск-ядру не источник")

    def test_bot_feeds_exactly_engine_value(self):
        ex = FakeOkx(total_eq="104123.45")
        self.engine_feed(ex)
        engine_equity = risk.status()["equity"]
        bot = self.bot(ex)
        self.assertEqual(bot._fetch_equity(), engine_equity)
        self.assertEqual(bot.run(), DCABot.STATE_DONE)
        self.assertEqual(risk.status()["equity"], engine_equity)
        # equity_curve bot_state.db: снимки движка и бота в одной единице (totalEq, USD)
        self.assertEqual({row["total_eq"] for row in self.storage.get_equity_history()}, {engine_equity})

    def test_real_drawdown_of_total_eq_still_trips_breaker(self):
        ex = FakeOkx(total_eq="104000")
        self.engine_feed(ex)
        self.tick(60)
        ex.total_eq = "88000"  # −15.4% от HWM: настоящая просадка счёта
        final = self.run_quiet(self.bot(ex))
        self.assertEqual(final, DCABot.STATE_PAUSED)
        self.assertEqual(ex.created, [])
        self.assertTrue(risk.status()["global_breaker"], "защита по фиду бота работает")

    # --- Equity не прочитан: update_equity не зовём, решает свежесть другого фида ---

    def test_unread_equity_is_none(self):
        exchange_error = ccxt.ExchangeError('okx {"code":"50001","msg":"Service temporarily unavailable","data":[]}')
        cases = {
            "сеть": FakeOkx(balance_error=ccxt.NetworkError("timeout")),
            "ошибка OKX": FakeOkx(balance_error=exchange_error),
            "code != 0 без исключения": FakeOkx(balance_resp={"code": "50001", "msg": "busy", "data": []}),
            "пустой data": FakeOkx(balance_resp={"code": "0", "data": []}),
            "нет totalEq": FakeOkx(total_eq=_MISSING),
            "пустая строка": FakeOkx(total_eq=""),
            "не число": FakeOkx(total_eq="abc"),
            "NaN": FakeOkx(total_eq="NaN"),
            "inf": FakeOkx(total_eq="inf"),
            "ноль": FakeOkx(total_eq="0"),
            "минус": FakeOkx(total_eq="-1"),
        }
        for name, ex in cases.items():
            with self.subTest(name), self.assertLogs(level="WARNING"):
                self.assertIsNone(self.bot(ex)._fetch_equity())

    def test_unread_equity_fresh_engine_feed_allows_buy(self):
        self.engine_feed(FakeOkx(total_eq="104000"))
        self.tick(60)  # движок обновил equity минуту назад
        ex = FakeOkx(balance_error=ccxt.NetworkError("timeout"))
        final = self.run_quiet(self.bot(ex))
        st = risk.status()
        self.assertEqual(final, DCABot.STATE_DONE)
        self.assertEqual(len(ex.created), 1)
        self.assertEqual((st["equity"], st["equity_updated_at"]), (ENGINE_HWM, T0),
                         "update_equity не вызывался: equity и отметка — от движка")
        self.assertFalse(st["global_breaker"])
        self.assertEqual(len(self.storage.get_equity_history()), 1, "снимок бота не записан")

    def test_unread_equity_stale_feed_pauses_without_orders(self):
        self.engine_feed(FakeOkx(total_eq="104000"))
        self.tick(risk.EQUITY_MAX_AGE_S + 1)  # движок молчит дольше 10 мин
        ex = FakeOkx(total_eq="")
        final = self.run_quiet(self.bot(ex))
        self.assertEqual(final, DCABot.STATE_PAUSED)
        self.assertEqual(ex.created, [])
        self.assertFalse(risk.status()["global_breaker"])


if __name__ == "__main__":
    unittest.main()
