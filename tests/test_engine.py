"""Движок (src/engine.py): флаги, kill-switch, закрытие хранилища, tdMode спота,
запрет сна Windows, паузы процесса, классы ошибок.

Задачи ENGINE-FLAG-LOG, ENGINE-KILL-REG, ENGINE-STORAGE-CLOSE, ENGINE-TDMODE,
ENGINE-KEEPAWAKE, ENGINE-PAUSE-DETECT, ENGINE-ERR-CLASS.
Без сети: настройки и биржа подменяются фейками, WS-клиенты не подключаются,
риск-ядро и хранилище — во временной директории, реестр kill-switch
восстанавливается после теста.
"""
import asyncio
import contextlib
import io
import json
import logging
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import ccxt

from src import engine as engine_mod
from src import ops, order_owner, risk
from src.connector import registered_kill_callbacks, unregister_kill_callback
from src.engine import ACCOUNT_MODE_TTL_S, RiskRejected, read_flag_text
from src.order_router import OrderRouter
from src.storage import Storage
from tests.test_connector_kill_dca import FakeBotExchange

SETTINGS = SimpleNamespace(mode="demo", domain="www.okx.com", is_demo=True,
                           api_key="k", secret="s", passphrase="p")
INST = "BTC-USDT"
PX = 50_000.0


class FakeSpotExchange:
    """CCXT okx без сети: account/config, account/balance, create_order, тикер."""

    def __init__(self, acct_lv="2", auto_loan=False, spot_borrow=False, avail=None, config_error=None):
        self.config = {"acctLv": acct_lv, "autoLoan": auto_loan, "enableSpotBorrow": spot_borrow}
        self.avail = dict(avail or {"USDT": 10_000.0, "BTC": 1.0})
        self.config_error = config_error
        self.config_reads = 0
        self.created: list[dict] = []
        self.headers: dict = {}

    def private_get_account_config(self, params=None):
        self.config_reads += 1
        if self.config_error is not None:
            raise self.config_error
        return {"code": "0", "data": [dict(self.config)]}

    def private_get_account_balance(self, params=None):
        ccy = (params or {}).get("ccy")
        details = [{"ccy": c, "availBal": str(v)} for c, v in self.avail.items() if ccy in (None, c)]
        return {"code": "0", "data": [{"totalEq": "10000", "upl": "1.5", "details": details}]}

    def create_order(self, symbol, ord_type, side, amount, price=None, params=None):
        ord_id = f"o{len(self.created) + 1}"
        self.created.append({"symbol": symbol, "type": ord_type, "side": side, "amount": amount,
                             "price": price, "params": dict(params or {})})
        return {"id": ord_id, "info": {"ordId": ord_id}}

    def fetch_ticker(self, symbol):
        return {"symbol": symbol, "ask": PX, "last": PX}


class _EngineCase(unittest.TestCase):
    """Движок на фейках: временные risk-БД, storage и файлы-флаги."""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        risk.init(self.root / "risk.db")  # свежее ядро: слот kill пуст, kill неактивен
        self.addCleanup(risk.init, self.root / "unused.db")  # отпустить файл до очистки
        before = registered_kill_callbacks()
        self.addCleanup(self._restore_registry, before)
        self.kill_flag = self.root / "KILL"
        self.stop_flag = self.root / "STOP_ENGINE"
        for name, path in (("KILL_FLAG", self.kill_flag), ("STOP_FLAG", self.stop_flag)):
            patcher = mock.patch.object(engine_mod, name, path)
            patcher.start()
            self.addCleanup(patcher.stop)

    @staticmethod
    def _restore_registry(before):
        for cb in registered_kill_callbacks():
            if cb not in before:
                unregister_kill_callback(cb)

    def make_engine(self, ex, inst_ids=(INST,)):
        with mock.patch.object(engine_mod, "load_settings", return_value=SETTINGS), \
                mock.patch.object(engine_mod, "create_exchange", return_value=ex), \
                mock.patch.object(engine_mod, "Storage", side_effect=lambda: Storage(self.root / "state.db")):
            eng = engine_mod.TradingEngine(inst_ids)
        self.addCleanup(eng.db.close)
        return eng

    @staticmethod
    def owners() -> set:
        return {getattr(cb, "__self__", None) for cb in registered_kill_callbacks()}


class _FlagCheck(logging.Handler):
    """Запоминает сообщения лога и был ли файл-флаг на месте в момент записи."""

    def __init__(self, path: Path):
        super().__init__(logging.DEBUG)
        self.path = path
        self.records: list[tuple[str, bool]] = []

    def emit(self, record):
        self.records.append((record.getMessage(), self.path.exists()))


class ReadFlagTextTest(unittest.TestCase):
    """read_flag_text: кодировки PowerShell и ошибки чтения не роняют цикл флагов."""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(tmp.cleanup)
        self.path = Path(tmp.name) / "FLAG"

    def check(self, raw: bytes, expected: str):
        self.path.write_bytes(raw)
        self.assertEqual(read_flag_text(self.path), expected)

    def test_encodings(self):
        text = "ops/engine.ps1 stop 2026-09-24T20:30:00+05:00 причина"
        self.check(text.encode("utf-8"), text)
        self.check(b"\xef\xbb\xbf" + text.encode("utf-8"), text)       # UTF-8 с BOM
        self.check(text.encode("utf-16"), text)                          # `>` / Out-File в PS 5.1
        self.check(text.encode("cp1251") + b"\r\n", text)                # Set-Content в PS 5.1

    def test_whitespace_collapsed_and_long_text_truncated(self):
        self.check(b"  line1\r\n  line2 \n", "line1 line2")
        self.check(b"", "")
        self.path.write_bytes(b"x" * 2000)
        text = read_flag_text(self.path)
        self.assertEqual(len(text), engine_mod.FLAG_TEXT_LIMIT + 1)
        self.assertTrue(text.endswith("…"))

    def test_missing_file_described_not_raised(self):
        self.assertIn("не прочитан", read_flag_text(self.path))


class FlagLoopTest(_EngineCase):
    """ENGINE-FLAG-LOG: текст флага — в лог до unlink; флаги работают как раньше."""

    def run_loop(self, eng) -> _FlagCheck:
        handler = _FlagCheck(self.stop_flag)
        logger = logging.getLogger("okx.engine")
        logger.addHandler(handler)
        old_level = logger.level
        logger.setLevel(logging.DEBUG)
        try:
            eng._running = True
            asyncio.run(asyncio.wait_for(eng._flag_loop(), timeout=10))
        finally:
            logger.removeHandler(handler)
            logger.setLevel(old_level)
        return handler

    def test_stop_flag_source_logged_before_unlink(self):
        eng = self.make_engine(FakeBotExchange())
        source = "ops/engine.ps1 stop 2026-09-24T20:30:00+05:00"
        self.stop_flag.write_text(source, encoding="utf-8")
        handler = self.run_loop(eng)
        stop_logs = [(msg, existed) for msg, existed in handler.records if "STOP_ENGINE" in msg]
        self.assertEqual(len(stop_logs), 1, handler.records)
        msg, existed = stop_logs[0]
        self.assertIn(source, msg)
        self.assertTrue(existed, "источник записан в лог после удаления флага")
        self.assertFalse(self.stop_flag.exists(), "флаг STOP_ENGINE не снят")

    def test_empty_stop_flag_logged_as_unknown_source(self):
        eng = self.make_engine(FakeBotExchange())
        self.stop_flag.write_bytes(b"")
        handler = self.run_loop(eng)
        self.assertTrue(any("источник не указан" in msg for msg, _ in handler.records), handler.records)
        self.assertFalse(self.stop_flag.exists())

    def test_utf16_kill_flag_triggers_kill_switch_through_dispatcher(self):
        # раньше read_text(utf-8) на UTF-16 падал: движок выходил без kill-switch
        ex = FakeBotExchange(orders=["o1"], dca=[("s1", "spot_dca")])
        eng = self.make_engine(ex)
        self.kill_flag.write_bytes("тест ENGINE-FLAG-LOG".encode("utf-16"))
        self.stop_flag.write_text("test", encoding="utf-8")  # чтобы цикл завершился
        handler = self.run_loop(eng)
        self.assertTrue(any("KILL-флаг" in m and "тест ENGINE-FLAG-LOG" in m for m, _ in handler.records),
                        handler.records)
        self.assertFalse(self.kill_flag.exists(), "флаг KILL не снят")
        self.assertTrue(risk.status()["kill_active"])
        self.assertEqual(ex.orders, {}, "ордер не отменён")
        self.assertEqual(ex.dca, {}, "DCA-бот не остановлен")

    def test_no_flags_loop_keeps_running(self):
        eng = self.make_engine(FakeBotExchange())
        eng._running = True

        async def scenario():
            task = asyncio.create_task(eng._flag_loop())
            await asyncio.sleep(0.05)
            self.assertFalse(task.done(), "цикл флагов завершился без флага")
            eng._running = False
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

        asyncio.run(scenario())


class KillRegistrationTest(_EngineCase):
    """ENGINE-KILL-REG: движок — подписчик реестра, в слоте risk — диспетчер."""

    def test_engine_subscribes_via_registry(self):
        eng = self.make_engine(FakeBotExchange())
        self.assertIn(eng, self.owners())

    def test_engine_created_after_router_keeps_router_subscription(self):
        # раньше движок, созданный после роутера, перехватывал слот risk
        router = OrderRouter(FakeBotExchange(orders=["r1"]), db_path=self.root / "router.db")
        self.addCleanup(router.storage.close)
        self.addCleanup(router.close)
        eng = self.make_engine(FakeBotExchange(orders=["e1"]))
        report = risk.kill_switch(flatten=False, by="test ENGINE-KILL-REG")
        self.assertEqual(report["cancelled"], ["e1", "r1"])
        self.assertEqual(report["failed"], [])
        self.assertIn(eng, self.owners())
        self.assertIn(router, self.owners())

    def test_router_created_after_engine_both_called(self):
        eng = self.make_engine(FakeBotExchange(orders=["e1"]))
        router = OrderRouter(FakeBotExchange(orders=["r1"]), db_path=self.root / "router.db")
        self.addCleanup(router.storage.close)
        self.addCleanup(router.close)
        report = risk.kill_switch(flatten=False, by="test ENGINE-KILL-REG")
        self.assertEqual(report["cancelled"], ["e1", "r1"])
        self.assertIn(eng, self.owners())

    def test_two_engines_idempotent_and_independent(self):
        e1 = self.make_engine(FakeBotExchange(orders=["a"]))
        e2 = self.make_engine(FakeBotExchange(orders=["b"]))
        report = risk.kill_switch(flatten=False, by="test")
        self.assertEqual(report["cancelled"], ["a", "b"])
        self.assertIn(e1, self.owners())
        self.assertIn(e2, self.owners())

    def test_stop_unregisters_engine(self):
        eng = self.make_engine(FakeBotExchange())
        asyncio.run(eng.stop())
        self.assertNotIn(eng, self.owners())


class StopClosesStorageTest(_EngineCase):
    """ENGINE-STORAGE-CLOSE: stop() закрывает WS, сохраняет статистику, затем db.close()."""

    def test_stop_order_ws_stats_then_close(self):
        eng = self.make_engine(FakeBotExchange())
        calls: list[str] = []
        eng.ws_public.close = mock.AsyncMock(side_effect=lambda: calls.append("ws_public"))
        eng.ws_private.close = mock.AsyncMock(side_effect=lambda: calls.append("ws_private"))
        real_save, real_close = eng._save_stats, eng.db.close
        eng._save_stats = lambda: (calls.append("save_stats"), real_save())
        eng.db.close = lambda: (calls.append("db_close"), real_close())
        asyncio.run(eng.stop())
        self.assertEqual(calls, ["ws_public", "ws_private", "save_stats", "db_close"])
        self.assertIsNotNone(Storage(self.root / "state.db").get_ws_state("engine_stats"))

    def test_storage_closed_even_if_ws_close_fails(self):
        eng = self.make_engine(FakeBotExchange())
        eng.ws_public.close = mock.AsyncMock(side_effect=RuntimeError("ws упал"))
        with mock.patch.object(eng.db, "close", wraps=eng.db.close) as close:
            with self.assertRaises(RuntimeError):
                asyncio.run(eng.stop())
        close.assert_called_once()
        self.assertNotIn(eng, self.owners())

    def test_stop_real_ws_clients_and_idempotent_close(self):
        eng = self.make_engine(FakeBotExchange())
        asyncio.run(eng.stop())  # WS не подключались — close() без сети
        eng.db.close()  # повторное закрытие безопасно


class EquityFeedTest(_EngineCase):
    """Фид equity не изменился: totalEq → storage и risk.update_equity."""

    def test_update_equity_feeds_risk_and_storage(self):
        eng = self.make_engine(FakeSpotExchange())
        asyncio.run(eng._update_equity())
        self.assertEqual(risk.status()["equity"], 10_000.0)
        rows = eng.db.get_equity_history()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["avail_eq"], 10_000.0)


class SpotTdModeTest(_EngineCase):
    """ENGINE-TDMODE: tdMode спота по acctLv и запрет скрытого займа, как в OrderRouter."""

    def setUp(self):
        super().setUp()
        risk.update_equity(10_000)  # свежая equity: вход разрешён, notional ≤ 15%

    def place(self, eng, side="buy", ord_type="limit", sz=0.001, px=PX, inst=INST, **kw):
        return asyncio.run(eng.place_order(inst, side, ord_type, sz, px, **kw))

    def test_cash_mode_limit_buy(self):
        ex = FakeSpotExchange(acct_lv="2")
        record = self.place(self.make_engine(ex))
        params = ex.created[0]["params"]
        self.assertEqual(params["tdMode"], "cash")
        self.assertNotIn("tgtCcy", params)
        self.assertIn("expTime", params)
        self.assertEqual(params["clOrdId"], record.cl_ord_id)
        self.assertEqual(order_owner.owner_of(record.cl_ord_id).code, order_owner.ENGINE)

    def test_cash_mode_level_1(self):
        ex = FakeSpotExchange(acct_lv="1")
        self.place(self.make_engine(ex), ord_type="market", px=None)
        self.assertEqual(ex.created[0]["params"]["tdMode"], "cash")

    def test_cross_mode_limit_buy(self):
        ex = FakeSpotExchange(acct_lv="3")
        self.place(self.make_engine(ex))
        params = ex.created[0]["params"]
        self.assertEqual(params["tdMode"], "cross")
        self.assertNotIn("tgtCcy", params)

    def test_cross_market_buy_sends_quote_amount(self):
        ex = FakeSpotExchange(acct_lv="4")
        self.place(self.make_engine(ex), ord_type="market")
        params = ex.created[0]["params"]
        self.assertEqual((params["tdMode"], params["tgtCcy"]), ("cross", "quote_ccy"))
        self.assertAlmostEqual(params["cost"], 0.001 * PX)

    def test_cross_market_buy_without_px_rejected(self):
        ex = FakeSpotExchange(acct_lv="3")
        with self.assertRaisesRegex(RiskRejected, "cross"):
            self.place(self.make_engine(ex), ord_type="market", px=None)
        self.assertEqual(ex.created, [])

    def test_auto_loan_buy_over_avail_rejected(self):
        ex = FakeSpotExchange(acct_lv="3", auto_loan=True, avail={"USDT": 10.0, "BTC": 0.0})
        with self.assertRaisesRegex(RiskRejected, "заём"):
            self.place(self.make_engine(ex))  # 0.001 × 50 000 = 50 USDT > 10
        self.assertEqual(ex.created, [])

    def test_auto_loan_buy_within_avail_passes(self):
        ex = FakeSpotExchange(acct_lv="3", auto_loan=True, avail={"USDT": 100.0})
        self.place(self.make_engine(ex))
        self.assertEqual(ex.created[0]["params"]["tdMode"], "cross")

    def test_spot_borrow_exit_sell_over_base_rejected(self):
        # выход тоже не может взять заём: продажа больше availBal базы
        ex = FakeSpotExchange(acct_lv="2", spot_borrow=True, avail={"BTC": 0.5})
        with self.assertRaisesRegex(RiskRejected, "заём"):
            self.place(self.make_engine(ex), side="sell", sz=1.0)
        self.assertEqual(ex.created, [])

    def test_exit_sell_gets_td_mode_even_when_kill_active(self):
        ex = FakeSpotExchange(acct_lv="2")
        eng = self.make_engine(ex)
        with mock.patch.object(engine_mod, "emergency_stop", return_value={"cancelled": [], "failed": []}):
            risk.kill_switch(flatten=False, by="test")  # входы заблокированы, выходы — нет
        with self.assertRaises(RiskRejected):
            self.place(eng)
        self.place(eng, side="sell")
        self.assertEqual([c["side"] for c in ex.created], ["sell"])
        self.assertEqual(ex.created[0]["params"]["tdMode"], "cash")

    def test_unreadable_mode_rejects_before_exchange(self):
        ex = FakeSpotExchange(config_error=ccxt.NetworkError("timeout"))
        with self.assertRaisesRegex(RiskRejected, "режим аккаунта не прочитан"):
            self.place(self.make_engine(ex))
        self.assertEqual(ex.created, [])

    def test_unknown_acct_lv_rejects(self):
        ex = FakeSpotExchange(acct_lv="9")
        with self.assertRaisesRegex(RiskRejected, "режим аккаунта не прочитан"):
            self.place(self.make_engine(ex))
        self.assertEqual(ex.created, [])

    def test_mode_cached_and_reread_after_ttl(self):
        ex = FakeSpotExchange(acct_lv="2")
        eng = self.make_engine(ex)
        self.place(eng)
        self.place(eng)
        self.assertEqual(ex.config_reads, 1)
        ex.config["acctLv"] = "3"  # человек сменил режим на ходу
        eng._account_mode_at -= ACCOUNT_MODE_TTL_S + 1
        with self.assertLogs("okx.engine", level="WARNING") as cm:
            self.place(eng)
        self.assertEqual(ex.config_reads, 2)
        self.assertEqual([c["params"]["tdMode"] for c in ex.created], ["cash", "cash", "cross"])
        self.assertTrue(any("сменился" in line for line in cm.output), cm.output)

    def test_non_spot_not_touched(self):
        ex = FakeSpotExchange(acct_lv="3")
        self.place(self.make_engine(ex), inst="BTC-USDT-SWAP", side="sell", entry=False)
        self.assertNotIn("tdMode", ex.created[0]["params"])
        self.assertEqual(ex.config_reads, 0)

    def test_start_logs_mode_and_survives_read_failure(self):
        eng = self.make_engine(FakeSpotExchange(acct_lv="2"))
        with self.assertLogs("okx.engine", level="INFO") as cm:
            asyncio.run(eng._log_account_mode())
        self.assertTrue(any("acctLv 2" in line and "cash" in line for line in cm.output), cm.output)

        bad = self.make_engine(FakeSpotExchange(config_error=ccxt.NetworkError("timeout")))
        with self.assertLogs("okx.engine", level="WARNING") as cm:
            asyncio.run(bad._log_account_mode())  # не бросает: сверка и equity работают без режима
        self.assertTrue(any("не прочитан" in line for line in cm.output), cm.output)


class FakeKernel32:
    """kernel32 без ОС: SetThreadExecutionState запоминает флаги."""

    def __init__(self, result: int = 0x80000000):
        self.calls: list[int] = []
        self.result = result

    def SetThreadExecutionState(self, flags):  # noqa: N802 — имя функции WinAPI
        self.calls.append(flags)
        return self.result


class KeepAwakeTest(_EngineCase):
    """ENGINE-KEEPAWAKE: запрос при старте, снятие в stop(), на других ОС — ничего."""

    ES_DISPLAY_REQUIRED = 0x00000002

    def run_start_stop(self, kernel32):
        eng = self.make_engine(FakeBotExchange())
        with mock.patch.object(engine_mod, "_kernel32", return_value=kernel32), \
                mock.patch.object(engine_mod, "check_time_sync", side_effect=RuntimeError("стоп теста")), \
                self.assertLogs("okx.engine", level="INFO") as cm:
            with self.assertRaisesRegex(RuntimeError, "стоп теста"):
                asyncio.run(eng.start())  # запрет сна — до первого обращения к бирже
            asyncio.run(eng.stop())  # как main(): stop() в finally
        return eng, cm.output

    def test_start_requests_system_and_stop_releases(self):
        k32 = FakeKernel32()
        eng, logs = self.run_start_stop(k32)
        self.assertEqual(k32.calls, [engine_mod.ES_CONTINUOUS | engine_mod.ES_SYSTEM_REQUIRED,
                                     engine_mod.ES_CONTINUOUS])
        self.assertEqual(k32.calls[0], 0x80000001)
        self.assertFalse(k32.calls[0] & self.ES_DISPLAY_REQUIRED, "экран должен гаснуть")
        self.assertTrue(any("Запрет сна Windows включён" in line for line in logs), logs)
        self.assertTrue(any("Запрет сна Windows снят" in line for line in logs), logs)
        self.assertFalse(eng._keep_awake)

    def test_not_windows_does_nothing(self):
        eng, logs = self.run_start_stop(None)
        self.assertFalse(any("сна" in line for line in logs), logs)
        self.assertFalse(eng._keep_awake)

    def test_failed_request_warns_and_is_not_released(self):
        k32 = FakeKernel32(result=0)
        _, logs = self.run_start_stop(k32)
        self.assertEqual(k32.calls, [0x80000001])  # снимать нечего
        self.assertTrue(any("WARNING" in line and "не включён" in line for line in logs), logs)

    def test_platform_switch(self):
        with mock.patch.object(engine_mod.sys, "platform", "linux"):
            self.assertIsNone(engine_mod._kernel32())
            self.assertIsNone(engine_mod.set_thread_execution_state(engine_mod.ES_CONTINUOUS))

    @unittest.skipUnless(sys.platform == "win32", "только Windows")
    def test_real_kernel32_signature(self):
        # настоящий вызов в потоке теста: поставить и сразу снять
        self.assertTrue(engine_mod.set_thread_execution_state(0x80000001))
        self.assertTrue(engine_mod.set_thread_execution_state(engine_mod.ES_CONTINUOUS))


class _Clock:
    def __init__(self, times):
        self.times = list(times)

    def __call__(self) -> float:
        return self.times.pop(0)


class PauseDetectTest(_EngineCase):
    """ENGINE-PAUSE-DETECT: между сверками больше двух интервалов — WARNING и pauses += 1."""

    def run_reconcile_loop(self, eng, times, cycles):
        eng._clock = _Clock(times)
        eng._reconcile_interval = 0.001  # настоящие сны короткие, время задают часы
        done = []

        async def fake_reconcile():
            done.append(1)
            if len(done) >= cycles:
                eng._running = False

        eng._reconcile = fake_reconcile
        eng._running = True
        asyncio.run(asyncio.wait_for(eng._reconcile_loop(), timeout=10))

    def test_pause_on_fake_clock(self):
        eng = self.make_engine(FakeBotExchange())
        with self.assertLogs("okx.engine", level="WARNING") as cm:
            # старт 1000; +0.001 — норма; +500 — пауза; +0.001 — норма
            self.run_reconcile_loop(eng, [1000.0, 1000.001, 1500.001, 1500.002], cycles=3)
        self.assertEqual(eng.stats["pauses"], 1)
        pause_logs = [line for line in cm.output if "пауза процесса" in line]
        self.assertEqual(len(pause_logs), 1, cm.output)
        self.assertIn("пауза процесса 500 с", pause_logs[0])
        eng._save_stats()
        self.assertEqual(eng.db.get_ws_state("engine_stats")["pauses"], 1)

    def test_threshold_is_two_intervals(self):
        eng = self.make_engine(FakeBotExchange())
        eng._reconcile_interval = 60.0
        eng._check_pause(120.0)  # ровно два интервала — ещё не пауза
        self.assertEqual(eng.stats["pauses"], 0)
        with self.assertLogs("okx.engine", level="WARNING"):
            eng._check_pause(121.0)
        self.assertEqual(eng.stats["pauses"], 1)

    def test_wall_clock_by_default(self):
        eng = self.make_engine(FakeBotExchange())
        self.assertIs(eng._clock, engine_mod.time.time)


class _HttpError(Exception):
    """Как requests.HTTPError: статус в response.status_code."""

    def __init__(self, status: int):
        super().__init__(f"HTTP {status}")
        self.response = SimpleNamespace(status_code=status)


def _chained(exc: BaseException, cause: BaseException) -> BaseException:
    exc.__cause__ = cause
    return exc


URL = "https://www.okx.com/api/v5/account/balance"


class ClassifyErrorTest(unittest.TestCase):
    """ENGINE-ERR-CLASS: классы ошибок по коду OKX, HTTP-статусу и классу CCXT."""

    EXCHANGE = [
        ccxt.OnMaintenance('okx {"code":"50001","msg":"Service temporarily unavailable","data":[]}'),
        ccxt.ExchangeNotAvailable('okx {"code":"50013","msg":"Systems are busy","data":[]}'),
        ccxt.RequestTimeout('okx {"code":"50004","msg":"Endpoint request timeout","data":[]}'),
        ccxt.ExchangeNotAvailable('okx {"code":"50026","msg":"System error","data":[]}'),
        ccxt.ExchangeNotAvailable(f"okx GET {URL} 503 Service Unavailable <html>ray 12345</html>"),
        ccxt.ExchangeNotAvailable(f"okx POST {URL} 502 Bad Gateway "),
        ccxt.RequestTimeout(f"okx GET {URL} 504 Gateway Timeout "),
        _chained(ccxt.ExchangeError(f"okx GET {URL}"), _HttpError(505)),  # 5xx без маппинга CCXT
        ccxt.RequestTimeout(f"okx GET {URL}"),  # requests Timeout
        ccxt.NetworkError(f"okx GET {URL}"),  # обрыв соединения
        TimeoutError("read timeout"),
        ConnectionResetError(10054, "connection reset"),
    ]
    INTERNAL = [
        ccxt.RateLimitExceeded('okx {"code":"50011","msg":"Too Many Requests","data":[]}'),
        ccxt.RateLimitExceeded(f"okx GET {URL} 429 Too Many Requests "),
        ccxt.AuthenticationError('okx {"code":"50113","msg":"Invalid Sign","data":[]}'),
        ccxt.InvalidNonce('okx {"code":"50102","msg":"Timestamp request expired","data":[]}'),
        ccxt.ExchangeNotAvailable(f"okx GET {URL} 403 Forbidden "),  # CCXT маппит 4xx в NotAvailable
        ccxt.BadRequest('okx {"code":"51000","msg":"Parameter tdMode error","data":[]}'),
        ccxt.ExchangeError(f"okx GET {URL}"),  # SSL, редиректы — без статуса
        KeyError("totalEq"),
        ValueError("could not convert string to float"),
        RuntimeError("Дрейф часов 40000 мс"),
    ]

    def test_exchange_side(self):
        for exc in self.EXCHANGE:
            with self.subTest(exc=repr(exc)):
                self.assertEqual(engine_mod.classify_error(exc), "exchange")

    def test_internal(self):
        for exc in self.INTERNAL:
            with self.subTest(exc=repr(exc)):
                self.assertEqual(engine_mod.classify_error(exc), "internal")


class ErrorCountersTest(_EngineCase):
    """ENGINE-ERR-CLASS: счётчики в циклах, errors = сумма, всё в engine_stats."""

    def run_equity_loop(self, eng, errors):
        eng._equity_interval = 0.001
        queue = list(errors)

        async def fake_update():
            if not queue:
                eng._running = False
                return
            raise queue.pop(0)

        eng._update_equity = fake_update
        eng._running = True
        asyncio.run(asyncio.wait_for(eng._equity_loop(), timeout=10))

    def test_equity_loop_splits_counters(self):
        eng = self.make_engine(FakeBotExchange())
        with self.assertLogs("okx.engine", level="ERROR") as cm:
            self.run_equity_loop(eng, [
                ccxt.RequestTimeout(f"okx GET {URL}"),
                ccxt.RateLimitExceeded('okx {"code":"50011","msg":"Too Many Requests"}'),
                KeyError("totalEq"),
            ])
        self.assertEqual((eng.stats["errors_exchange"], eng.stats["errors_internal"], eng.stats["errors"]),
                         (1, 2, 3))
        self.assertTrue(any("Equity error (exchange)" in line for line in cm.output), cm.output)
        eng._save_stats()
        saved = eng.db.get_ws_state("engine_stats")
        self.assertEqual((saved["errors_exchange"], saved["errors_internal"], saved["errors"], saved["pauses"]),
                         (1, 2, 3, 0))

    def test_reconcile_loop_counts_exchange_error(self):
        eng = self.make_engine(FakeBotExchange())
        eng._reconcile_interval = 0.001
        calls = []

        async def fake_reconcile():
            calls.append(1)
            if len(calls) >= 2:
                eng._running = False
            raise ccxt.ExchangeNotAvailable(f"okx GET {URL} 503 Service Unavailable ")

        eng._reconcile = fake_reconcile
        eng._running = True
        with self.assertLogs("okx.engine", level="ERROR"):
            asyncio.run(asyncio.wait_for(eng._reconcile_loop(), timeout=10))
        self.assertEqual((eng.stats["errors_exchange"], eng.stats["errors_internal"], eng.stats["errors"]),
                         (2, 0, 2))


class OpsStatusEngineTest(unittest.TestCase):
    """`python -m src.ops status`: errors_exchange, errors_internal, pauses у движка."""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name)
        self.addCleanup(risk.init, root / "unused.db")
        self.paths = SimpleNamespace(risk_db=root / "risk.db", bot_db=root / "bot.db")
        patcher = mock.patch.object(ops, "state_paths", return_value=self.paths)
        patcher.start()
        self.addCleanup(patcher.stop)

    def status(self, stats):
        if stats is not None:
            Storage(self.paths.bot_db).set_ws_state("engine_stats", stats)
        out = io.StringIO()
        # basicConfig из ops.main не должен вешать обработчик на root для остальных тестов
        with contextlib.redirect_stdout(out), mock.patch("logging.basicConfig"):
            self.assertEqual(ops.main(["status", "--mode", "demo"]), 0)
        return json.loads(out.getvalue())["engine"]

    def test_new_counters_shown(self):
        engine = self.status({"errors": 3, "errors_exchange": 1, "errors_internal": 2, "pauses": 1})
        self.assertEqual((engine["errors_exchange"], engine["errors_internal"], engine["errors"],
                          engine["pauses"]), (1, 2, 3, 1))

    def test_old_engine_stats_show_null(self):
        engine = self.status({"errors": 5, "reconciles": 10})
        self.assertEqual(engine["errors"], 5)
        self.assertIsNone(engine["errors_exchange"])
        self.assertIsNone(engine["errors_internal"])
        self.assertIsNone(engine["pauses"])

    def test_no_engine_stats(self):
        self.assertIsNone(self.status(None))


if __name__ == "__main__":
    unittest.main()
