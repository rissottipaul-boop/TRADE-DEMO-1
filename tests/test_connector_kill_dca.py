"""Kill-switch останавливает DCA-ботов (FLEET-KILL-DCA): connector.stop_dca_bots / emergency_stop.

Фейковая биржа без сети: orders-pending, grid- и DCA-боты (tradingBot API).
Фейк повторяет поведение demo OKX, проверенное 24.09 на тестовом боте
3950110382492143616: dca/stop уже остановленного бота → code 1 + sCode 51291
(«The bot doesn't exist or has already stopped»), CCXT бросает ExchangeError.

Сквозные тесты пути kill-switch: risk.kill_switch → kill_switch_dispatch →
подписчики (как engine и OrderRouter) → emergency_stop → DCA-этап, а также
путь CLI `python -m src.ops kill` (и --keep-bots). Риск-ядро — на временной
БД, data/risk_state.db не трогается.
"""
import contextlib
import io
import json
import tempfile
import unittest
from argparse import Namespace
from pathlib import Path
from unittest import mock

import ccxt

from src import ops, risk
from src.connector import (
    OKXExchange,
    emergency_stop,
    fetch_dca_bots,
    kill_switch_dispatch,
    register_kill_callback,
    registered_kill_callbacks,
    stop_dca_bots,
    unregister_kill_callback,
)
from src.order_router import OrderRouter


def no_sleep(seconds: float) -> None:
    """Пауза backoff без ожидания (тесты retry)."""


def already_stopped(algo_id: str) -> ccxt.ExchangeError:
    """Ответ demo OKX на dca/stop остановленного бота — в том виде, как его бросает CCXT."""
    return ccxt.ExchangeError(
        'okx {"code":"1","data":[{"algoClOrdId":"","algoId":"%s","sCode":"51291",'
        '"sMsg":"The bot doesn\'t exist or has already stopped","tag":""}],"msg":""}' % algo_id)


class FakeBotExchange:
    """Минимальная OKX: обычные ордера, grid-боты (grid / contract_grid) и DCA-боты (spot_dca / contract_dca).

    grids — algoId (spot grid) или кортеж (algoId, algoOrdType[, state]);
    dca — кортежи (algoId, algoOrdType[, state]);
    stop_codes — algoId → sCode ответа stop без исключения (не "0" — отказ по
                 боту) или список sCode по одному на вызов (дальше — "0");
    stop_raises — algoId → исключение на каждый вызов stop или список
                  исключений (по одному на вызов, дальше — обычная обработка);
                  у grid исключение бросает вся пачка;
    list_raises — algoOrdType → то же для списков ботов;
    lost_response — algoId, у которых первый stop доходит до биржи (бот
                    остановлен), а ответ теряется (RequestTimeout).
    Как на demo OKX: остановленный или несуществующий бот → sCode 51291;
    code 1 (вся пачка/бот отклонены) — исключение CCXT с телом ответа,
    code 2 (частичный успех) — обычный ответ.
    """

    def __init__(self, dca=(), grids=(), orders=(), stop_codes=None, stop_raises=None,
                 list_raises=None, lost_response=()):
        self.orders = {o: {"ordId": o, "instId": "BTC-USDT"} for o in orders}
        self.grids = {}
        for item in grids:
            algo_id, algo_type, *rest = (item, "grid") if isinstance(item, str) else item
            self.grids[algo_id] = {"algoId": algo_id, "algoOrdType": algo_type, "instId": "ETH-USDT",
                                   "state": rest[0] if rest else "running"}
        self.dca = {}
        for item in dca:
            algo_id, algo_type, *rest = item
            self.dca[algo_id] = {"algoId": algo_id, "algoOrdType": algo_type, "instId": "LTC-USDT",
                                 "state": rest[0] if rest else "running"}
        self.stop_codes = stop_codes or {}
        self.stop_raises = stop_raises or {}
        self.list_raises = list_raises or {}
        self.lost_response = set(lost_response)
        self.grid_stop_requests: list[dict] = []
        self.grid_stop_batches: list[list[str]] = []
        self.grid_list_requests: list[dict] = []
        self.dca_stop_requests: list[dict] = []
        self.dca_list_requests: list[dict] = []

    @staticmethod
    def _maybe_raise(table: dict, key: str) -> None:
        exc = table.get(key)
        if isinstance(exc, list):
            if exc:
                raise exc.pop(0)
        elif exc is not None:
            raise exc

    def _next_code(self, algo_id: str) -> str:
        code = self.stop_codes.get(algo_id, "0")
        if isinstance(code, list):
            return code.pop(0) if code else "0"
        return code

    @staticmethod
    def _page(bots: dict, algo_type: str, params: dict) -> list[dict]:
        items = sorted((b for b in bots.values() if b["algoOrdType"] == algo_type),
                       key=lambda b: b["algoId"], reverse=True)
        if "after" in params:
            items = [b for b in items if b["algoId"] < params["after"]]
        return items[: int(params.get("limit", 100))]

    # --- обычные и algo-ордера (cancel_all_orders) ---
    def private_get_trade_orders_pending(self, params):
        return {"data": list(self.orders.values())}

    def private_get_trade_orders_algo_pending(self, params):
        return {"data": []}

    def private_post_trade_cancel_batch_orders(self, reqs):
        for r in reqs:
            self.orders.pop(r["ordId"], None)
        return {"code": "0", "data": [{"ordId": r["ordId"], "sCode": "0", "sMsg": ""} for r in reqs]}

    # --- grid ---
    def private_get_tradingbot_grid_orders_algo_pending(self, params):
        self.grid_list_requests.append(dict(params))
        algo_type = params["algoOrdType"]
        self._maybe_raise(self.list_raises, algo_type)
        return {"code": "0", "data": self._page(self.grids, algo_type, params)}

    def private_post_tradingbot_grid_stop_order_algo(self, reqs):
        self.grid_stop_requests.extend(reqs)
        self.grid_stop_batches.append([r["algoId"] for r in reqs])
        for r in reqs:  # исключение на всю пачку
            self._maybe_raise(self.stop_raises, r["algoId"])
        data, lost = [], False
        for r in reqs:
            algo_id = r["algoId"]
            if algo_id not in self.grids:
                code = "51291"
            elif algo_id in self.lost_response:
                self.lost_response.discard(algo_id)
                self.grids.pop(algo_id)
                code, lost = "0", True
            else:
                code = self._next_code(algo_id)
                if code == "0":
                    self.grids.pop(algo_id)
            data.append({"algoId": algo_id, "sCode": code, "sMsg": "" if code == "0" else "stop failed"})
        if lost:
            raise ccxt.RequestTimeout("okx POST tradingBot/grid/stop-order-algo: read timeout")
        bad = [d for d in data if d["sCode"] != "0"]
        if bad and len(bad) == len(data):  # code 1: CCXT бросает исключение с телом ответа
            raise ccxt.ExchangeError("okx " + json.dumps({"code": "1", "data": data, "msg": ""}))
        return {"code": "2" if bad else "0", "data": data}

    # --- DCA ---
    def private_get_tradingbot_dca_ongoing_list(self, params):
        self.dca_list_requests.append(dict(params))
        algo_type = params["algoOrdType"]
        self._maybe_raise(self.list_raises, algo_type)
        return {"code": "0", "data": self._page(self.dca, algo_type, params)}

    def private_post_tradingbot_dca_stop(self, params):
        self.dca_stop_requests.append(dict(params))
        algo_id = params["algoId"]
        self._maybe_raise(self.stop_raises, algo_id)
        if algo_id not in self.dca:
            raise already_stopped(algo_id)
        if algo_id in self.lost_response:
            self.lost_response.discard(algo_id)
            self.dca.pop(algo_id)
            raise ccxt.RequestTimeout("okx POST tradingBot/dca/stop: read timeout")
        code = self._next_code(algo_id)
        if code == "0":
            self.dca.pop(algo_id, None)
        return {"code": "0" if code == "0" else "1",
                "data": [{"algoId": algo_id, "sCode": code, "sMsg": "" if code == "0" else "stop failed"}]}


class StopDcaBotsTest(unittest.TestCase):
    def test_stops_spot_and_contract_with_keep_assets(self):
        ex = FakeBotExchange(dca=[("s1", "spot_dca"), ("s2", "spot_dca"), ("c1", "contract_dca")])
        report = stop_dca_bots(ex)
        self.assertEqual(sorted(report["stopped"]), ["c1", "s1", "s2"])
        self.assertEqual(report["failed"], [])
        self.assertEqual(report["errors"], [])
        self.assertEqual(ex.dca, {})
        # по одному боту на запрос, algoOrdType — тип бота, stopType 2 (без flatten)
        by_id = {r["algoId"]: r for r in ex.dca_stop_requests}
        self.assertEqual(by_id["s1"], {"algoId": "s1", "algoOrdType": "spot_dca", "stopType": "2"})
        self.assertEqual(by_id["c1"], {"algoId": "c1", "algoOrdType": "contract_dca", "stopType": "2"})
        self.assertEqual({p["algoOrdType"] for p in ex.dca_list_requests}, {"spot_dca", "contract_dca"})

    def test_sCode_failure_of_one_bot_does_not_block_others(self):
        ex = FakeBotExchange(dca=[("s1", "spot_dca"), ("s2", "spot_dca"), ("c1", "contract_dca")],
                             stop_codes={"s2": "51000"})
        report = stop_dca_bots(ex, sleep=no_sleep)
        self.assertEqual(sorted(report["stopped"]), ["c1", "s1"])
        self.assertEqual(report["failed"], ["s2"])
        self.assertEqual(report["errors"], [{"id": "s2", "sCode": "51000", "sMsg": "stop failed"}])
        # отказ биржи по боту — не транзиентная ошибка: без повторов
        self.assertEqual([r["algoId"] for r in ex.dca_stop_requests].count("s2"), 1)

    def test_exception_on_one_bot_does_not_block_others(self):
        ex = FakeBotExchange(dca=[("s1", "spot_dca"), ("s2", "spot_dca"), ("c1", "contract_dca")],
                             stop_raises={"s2": ccxt.NetworkError("timeout")})
        report = stop_dca_bots(ex, sleep=no_sleep)
        self.assertEqual(sorted(report["stopped"]), ["c1", "s1"])
        self.assertEqual(report["failed"], ["s2"])
        self.assertEqual(report["errors"][0]["id"], "s2")
        self.assertIn("timeout", report["errors"][0]["error"])

    def test_list_error_of_one_type_does_not_block_other_type(self):
        ex = FakeBotExchange(dca=[("s1", "spot_dca"), ("c1", "contract_dca")],
                             list_raises={"spot_dca": ccxt.ExchangeError("51000 Parameter algoOrdType error")})
        report = stop_dca_bots(ex, sleep=no_sleep)
        self.assertEqual(report["stopped"], ["c1"])
        self.assertEqual(report["errors"][0]["algoOrdType"], "spot_dca")

    def test_list_error_is_escalated_to_failed(self):
        """Регрессия: сбой ongoing-list шёл только в errors — kill отчитывался «успехом»,
        а все боты этого типа продолжали торговать (risk.kill_switch errors не читает)."""
        ex = FakeBotExchange(dca=[("s1", "spot_dca")],
                             list_raises={"spot_dca": ccxt.AuthenticationError("50113 Invalid Sign")})
        report = stop_dca_bots(ex, sleep=no_sleep)
        self.assertEqual(len(report["failed"]), 1)
        self.assertIn("spot_dca", report["failed"][0])
        self.assertIn("s1", ex.dca)
        # не транзиентная ошибка — список не повторяется
        self.assertEqual([p["algoOrdType"] for p in ex.dca_list_requests].count("spot_dca"), 1)

    def test_transient_list_error_is_retried(self):
        sleeps: list[float] = []
        ex = FakeBotExchange(dca=[("s1", "spot_dca")],
                             list_raises={"spot_dca": [ccxt.NetworkError("connection reset")]})
        report = stop_dca_bots(ex, sleep=sleeps.append)
        self.assertEqual(report["stopped"], ["s1"])
        self.assertEqual(report["failed"], [])
        self.assertEqual(sleeps, [1.0])

    def test_already_stopped_51291_is_not_failure(self):
        """Регрессия: бот закончил (SL) между ongoing-list и stop → 51291 давал ложный failed."""
        ex = FakeBotExchange(dca=[("s1", "spot_dca"), ("s2", "spot_dca")],
                             stop_raises={"s1": already_stopped("s1")})
        report = stop_dca_bots(ex, sleep=no_sleep)
        self.assertEqual(report["stopped"], ["s2"])
        self.assertEqual(report["skipped"], ["s1"])
        self.assertEqual(report["failed"], [])
        self.assertEqual(report["errors"], [])

    def test_already_stopped_51291_in_sCode_is_not_failure(self):
        ex = FakeBotExchange(dca=[("s1", "spot_dca")], stop_codes={"s1": "51291"})
        report = stop_dca_bots(ex, sleep=no_sleep)
        self.assertEqual(report["skipped"], ["s1"])
        self.assertEqual(report["failed"], [])

    def test_transient_stop_error_is_retried(self):
        sleeps: list[float] = []
        ex = FakeBotExchange(dca=[("s1", "spot_dca")],
                             stop_raises={"s1": [ccxt.RateLimitExceeded('okx {"code":"50011"}')]})
        report = stop_dca_bots(ex, sleep=sleeps.append)
        self.assertEqual(report["stopped"], ["s1"])
        self.assertEqual(report["failed"], [])
        self.assertEqual([r["algoId"] for r in ex.dca_stop_requests], ["s1", "s1"])
        self.assertEqual(sleeps, [1.0])

    def test_lost_response_then_51291_is_not_failure(self):
        """stop дошёл до биржи, ответ потерян (таймаут) → повтор даёт 51291 → бот остановлен."""
        ex = FakeBotExchange(dca=[("s1", "spot_dca")], lost_response={"s1"})
        report = stop_dca_bots(ex, sleep=no_sleep)
        self.assertEqual(report["failed"], [])
        self.assertEqual(report["skipped"], ["s1"])
        self.assertEqual(ex.dca, {})

    def test_persistent_transient_error_fails_after_retries(self):
        sleeps: list[float] = []
        ex = FakeBotExchange(dca=[("s1", "spot_dca"), ("s2", "spot_dca")],
                             stop_raises={"s1": ccxt.RequestTimeout("timeout")})
        report = stop_dca_bots(ex, attempts=2, backoff_s=1.0, sleep=sleeps.append)
        self.assertEqual(report["stopped"], ["s2"])
        self.assertEqual(report["failed"], ["s1"])
        self.assertEqual(len(report["errors"]), 1)
        # s2 остановлен в первом проходе, до пауз; s1 — 1 + 2 повтора с backoff 1, 2 с
        self.assertEqual([r["algoId"] for r in ex.dca_stop_requests], ["s2", "s1", "s1", "s1"])
        self.assertEqual(sleeps, [1.0, 2.0])

    def test_algo_ids_filter_stops_only_selected(self):
        ex = FakeBotExchange(dca=[("fleet1", "spot_dca"), ("tst1", "spot_dca"), ("fleet2", "contract_dca")])
        report = stop_dca_bots(ex, algo_ids={"tst1"})
        self.assertEqual(report["stopped"], ["tst1"])
        self.assertEqual([r["algoId"] for r in ex.dca_stop_requests], ["tst1"])
        self.assertEqual(sorted(ex.dca), ["fleet1", "fleet2"])  # флот не тронут

    def test_pagination_over_page_limit(self):
        ex = FakeBotExchange(dca=[(f"s{i:04d}", "spot_dca") for i in range(150)])
        self.assertEqual(len(fetch_dca_bots(ex, "spot_dca")), 150)
        self.assertEqual(len(stop_dca_bots(ex)["stopped"]), 150)

    def test_already_stopping_states(self):
        ex = FakeBotExchange(dca=[("run", "spot_dca"), ("stp", "spot_dca", "stopping"),
                                  ("ncp", "contract_dca", "no_close_position")])
        report = stop_dca_bots(ex)
        self.assertEqual(report["stopped"], ["run"])
        self.assertEqual(sorted(report["skipped"]), ["ncp", "stp"])
        # закрытие оставленной позиции — только явным stopType 1
        ex = FakeBotExchange(dca=[("stp", "spot_dca", "stopping"), ("ncp", "contract_dca", "no_close_position")])
        report = stop_dca_bots(ex, stop_type="1")
        self.assertEqual(report["stopped"], ["ncp"])
        self.assertEqual(ex.dca_stop_requests, [{"algoId": "ncp", "algoOrdType": "contract_dca", "stopType": "1"}])

    def test_idempotent_when_no_bots(self):
        self.assertEqual(stop_dca_bots(FakeBotExchange()),
                         {"stopped": [], "failed": [], "skipped": [], "errors": []})


class DcaApiPresenceTest(unittest.TestCase):
    """Без tradingBot/dca у клиента DCA-этап kill невозможен — у реального клиента методы обязаны быть."""

    def test_real_client_has_dca_api(self):
        ex = OKXExchange({})  # без ключей и без сети
        for name in ("private_get_tradingbot_dca_ongoing_list", "private_post_tradingbot_dca_stop"):
            self.assertTrue(callable(getattr(ex, name, None)), name)

    def test_real_ccxt_client_without_dca_api_is_failure(self):
        ex = OKXExchange({})
        ex.private_get_tradingbot_dca_ongoing_list = None  # как ccxt без tradingBot/dca
        report = stop_dca_bots(ex)
        self.assertEqual(len(report["failed"]), 1)
        self.assertIn("tradingBot/dca", report["failed"][0])


class EmergencyStopBotsTest(unittest.TestCase):
    def test_stops_orders_grid_and_dca(self):
        ex = FakeBotExchange(orders=["o1"], grids=["g1"], dca=[("s1", "spot_dca"), ("c1", "contract_dca")])
        report = emergency_stop(ex)
        self.assertEqual(sorted(report["bots_stopped"]), ["c1", "g1", "s1"])
        self.assertEqual(sorted(report["cancelled"]), ["c1", "g1", "o1", "s1"])
        self.assertEqual(report["failed"], [])
        self.assertFalse(ex.orders or ex.grids or ex.dca)
        self.assertTrue(all(r["stopType"] == "2" for r in ex.grid_stop_requests + ex.dca_stop_requests))

    def test_one_failed_bot_does_not_block_others(self):
        ex = FakeBotExchange(grids=["g1"], dca=[("s1", "spot_dca"), ("s2", "spot_dca"), ("c1", "contract_dca")],
                             stop_raises={"s1": ccxt.RequestTimeout("timeout")})
        report = emergency_stop(ex, sleep=no_sleep)
        self.assertEqual(sorted(report["bots_stopped"]), ["c1", "g1", "s2"])
        self.assertEqual(report["failed"], ["s1"])
        self.assertEqual(sorted(ex.dca), ["s1"])

    def test_grid_crash_does_not_block_dca(self):
        ex = FakeBotExchange(dca=[("s1", "spot_dca")])
        # непредвиденный ответ grid-списка (нет algoId) — KeyError внутри stop_grid_bots
        ex.private_get_tradingbot_grid_orders_algo_pending = lambda params: {"data": [{"instId": "ETH-USDT"}]}
        report = emergency_stop(ex)
        self.assertEqual(report["bots_stopped"], ["s1"])
        self.assertTrue(any("grid" in str(f) for f in report["failed"]), report["failed"])

    def test_cancel_failure_does_not_block_bots(self):
        ex = FakeBotExchange(grids=["g1"], dca=[("s1", "spot_dca")])

        def broken_pending(params):
            raise ccxt.NetworkError("orders-pending timeout")

        ex.private_get_trade_orders_pending = broken_pending
        report = emergency_stop(ex)
        self.assertEqual(sorted(report["bots_stopped"]), ["g1", "s1"])
        self.assertTrue(any("отмена ордеров" in str(f) for f in report["failed"]), report["failed"])

    def test_dca_list_failure_reaches_kill_report(self):
        ex = FakeBotExchange(grids=["g1"], dca=[("s1", "spot_dca")],
                             list_raises={"spot_dca": ccxt.AuthenticationError("50113 Invalid Sign")})
        report = emergency_stop(ex, sleep=no_sleep)
        self.assertEqual(report["bots_stopped"], ["g1"])
        self.assertTrue(any("spot_dca" in str(f) for f in report["failed"]), report["failed"])

    def test_include_bots_false_skips_dca(self):
        ex = FakeBotExchange(dca=[("s1", "spot_dca")])
        report = emergency_stop(ex, include_bots=False)
        self.assertNotIn("bots_stopped", report)
        self.assertEqual(ex.dca_list_requests, [])
        self.assertIn("s1", ex.dca)

    def test_exchange_without_dca_api_still_stops_grid(self):
        """Фейки других тестов без DCA-методов: остановка grid и отмена ордеров не ломаются."""

        class NoDca(FakeBotExchange):
            private_get_tradingbot_dca_ongoing_list = None  # вызов → TypeError

        ex = NoDca(orders=["o1"], grids=["g1"])
        report = emergency_stop(ex)
        self.assertEqual(report["bots_stopped"], ["g1"])
        self.assertEqual(sorted(report["cancelled"]), ["g1", "o1"])
        self.assertEqual(report["failed"], [])
        self.assertEqual({e.get("algoOrdType") for e in report["errors"]}, {"spot_dca", "contract_dca"})


class KillPathReachesDcaTest(unittest.TestCase):
    """Сквозной путь kill-switch до DCA-этапа. Риск-ядро — временная БД, реестр — с откатом."""

    def setUp(self):
        self._before = registered_kill_callbacks()
        self._tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.risk_db = Path(self._tmp.name) / "risk.db"
        risk.init(self.risk_db)  # свежее ядро: canceller=None, kill неактивен

    def tearDown(self):
        for cb in registered_kill_callbacks():
            if cb not in self._before:
                unregister_kill_callback(cb)
        risk.init(Path(self._tmp.name) / "unused.db")  # отпустить файл до очистки
        self._tmp.cleanup()

    def test_dispatch_to_engine_like_subscriber_stops_dca(self):
        ex = FakeBotExchange(orders=["o1"], grids=["g1"], dca=[("s1", "spot_dca"), ("c1", "contract_dca")])
        register_kill_callback(lambda flatten: emergency_stop(ex))  # как engine._cancel_all_for_kill_switch
        risk.set_order_canceller(kill_switch_dispatch)
        report = risk.kill_switch(flatten=False, by="test FLEET-KILL-DCA")
        self.assertEqual(sorted(report["cancelled"]), ["c1", "g1", "o1", "s1"])
        self.assertEqual(report["failed"], [])
        self.assertEqual(ex.dca, {})
        self.assertEqual({r["stopType"] for r in ex.dca_stop_requests}, {"2"})
        self.assertTrue(risk.status()["kill_active"])

    def test_dispatch_to_order_router_stops_dca(self):
        ex = FakeBotExchange(dca=[("s1", "spot_dca")])
        router = OrderRouter(ex, db_path=Path(self._tmp.name) / "state.db")  # подписка + диспетчер в risk
        try:
            report = risk.kill_switch(flatten=False, by="test FLEET-KILL-DCA")
        finally:
            router.close()
        self.assertIn("s1", report["cancelled"])
        self.assertEqual(report["failed"], [])
        self.assertEqual(ex.dca, {})

    def test_kill_report_escalates_dca_list_failure(self):
        ex = FakeBotExchange(dca=[("s1", "spot_dca")],
                             list_raises={"spot_dca": ccxt.AuthenticationError("50113 Invalid Sign")})
        register_kill_callback(lambda flatten: emergency_stop(ex, sleep=no_sleep))
        risk.set_order_canceller(kill_switch_dispatch)
        report = risk.kill_switch(flatten=False, by="test FLEET-KILL-DCA")
        self.assertTrue(any("spot_dca" in str(f) for f in report["failed"]), report)
        self.assertIn("s1", ex.dca)  # бот не остановлен — и отчёт kill это показывает

    def _ops_kill(self, ex, keep_bots: bool = False) -> int:
        """`python -m src.ops kill` без сети и без data/risk_state.db (patch.object падает,
        если имени нет, — до вызова cmd_kill)."""
        args = Namespace(mode="demo", reason="test FLEET-KILL-DCA", keep_bots=keep_bots)
        with mock.patch.object(ops, "_init_risk", side_effect=lambda mode: risk.init(self.risk_db)), \
                mock.patch.object(ops, "_exchange", return_value=ex), \
                contextlib.redirect_stdout(io.StringIO()):
            return ops.cmd_kill(args)

    def test_ops_kill_stops_dca(self):
        ex = FakeBotExchange(grids=["g1"], dca=[("s1", "spot_dca"), ("c1", "contract_dca")])
        self.assertEqual(self._ops_kill(ex), 0)
        self.assertEqual(ex.dca, {})
        self.assertEqual(ex.grids, {})
        self.assertTrue(risk.status()["kill_active"])

    def test_ops_kill_keep_bots_leaves_dca(self):
        ex = FakeBotExchange(grids=["g1"], dca=[("s1", "spot_dca")])
        self.assertEqual(self._ops_kill(ex, keep_bots=True), 0)
        self.assertIn("s1", ex.dca)
        self.assertIn("g1", ex.grids)
        self.assertEqual(ex.dca_list_requests, [])


if __name__ == "__main__":
    unittest.main()
