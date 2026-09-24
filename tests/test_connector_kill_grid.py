"""Kill-switch: остановка grid-ботов (FLEET-KILL-GRID-HARDEN) — connector.stop_grid_bots / emergency_stop.

Критерий задачи: сбой списка → failed; «бот уже остановлен» (51291) — не провал;
повтор сети и 50011/50001/50013 ×2 (1 с, 2 с); пагинация orders-algo-pending по
курсору after; фильтр algo_ids. Фейк биржи — FakeBotExchange из
test_connector_kill_dca (поведение demo OKX: code 1 → исключение CCXT, code 2 —
частичный успех с sCode по каждому боту).
"""
import tempfile
import unittest
from pathlib import Path

import ccxt

from src import risk
from src.connector import (
    BOT_STOP_BATCH,
    OKXExchange,
    emergency_stop,
    fetch_grid_bots,
    kill_switch_dispatch,
    register_kill_callback,
    registered_kill_callbacks,
    stop_grid_bots,
    unregister_kill_callback,
)
from tests.test_connector_kill_dca import FakeBotExchange, no_sleep


class StopGridBotsTest(unittest.TestCase):
    def test_stops_spot_and_contract_grid_keep_assets(self):
        ex = FakeBotExchange(grids=["g1", ("c1", "contract_grid")])
        report = stop_grid_bots(ex)
        self.assertEqual(sorted(report["stopped"]), ["c1", "g1"])
        self.assertEqual((report["failed"], report["skipped"], report["errors"]), ([], [], []))
        self.assertEqual(ex.grids, {})
        by_id = {r["algoId"]: r for r in ex.grid_stop_requests}
        self.assertEqual(by_id["g1"], {"algoId": "g1", "instId": "ETH-USDT", "algoOrdType": "grid", "stopType": "2"})
        self.assertEqual(by_id["c1"]["algoOrdType"], "contract_grid")
        self.assertEqual({p["algoOrdType"] for p in ex.grid_list_requests}, {"grid", "contract_grid"})

    def test_pagination_over_page_limit(self):
        """Регрессия: читалась одна страница orders-algo-pending — боты сверх 100 переживали kill."""
        ex = FakeBotExchange(grids=[f"g{i:04d}" for i in range(150)])
        self.assertEqual(len(fetch_grid_bots(ex, "grid")), 150)
        report = stop_grid_bots(ex)
        self.assertEqual(len(report["stopped"]), 150)
        self.assertEqual(ex.grids, {})
        self.assertEqual(len(ex.grid_stop_batches), 15)
        self.assertTrue(all(len(batch) <= BOT_STOP_BATCH for batch in ex.grid_stop_batches))

    def test_list_error_is_escalated_to_failed(self):
        """Регрессия: сбой списка шёл только в errors — kill отчитывался «успехом»."""
        ex = FakeBotExchange(grids=["g1", ("c1", "contract_grid")],
                             list_raises={"grid": ccxt.AuthenticationError("50113 Invalid Sign")})
        report = stop_grid_bots(ex, sleep=no_sleep)
        self.assertEqual(report["stopped"], ["c1"])  # другой тип остановлен
        self.assertEqual(len(report["failed"]), 1)
        self.assertIn("grid", report["failed"][0])
        self.assertIn("g1", ex.grids)
        # не транзиентная ошибка — список не повторяется
        self.assertEqual([p["algoOrdType"] for p in ex.grid_list_requests].count("grid"), 1)

    def test_transient_list_error_is_retried(self):
        sleeps: list[float] = []
        ex = FakeBotExchange(grids=["g1"], list_raises={"grid": [ccxt.OnMaintenance('okx {"code":"50001"}')]})
        report = stop_grid_bots(ex, sleep=sleeps.append)
        self.assertEqual(report["stopped"], ["g1"])
        self.assertEqual(report["failed"], [])
        self.assertEqual(sleeps, [1.0])

    def test_already_stopped_in_partial_batch_is_not_failure(self):
        """Бот закончил (SL) между списком и stop: code 2, у него sCode 51291 — не провал."""
        ex = FakeBotExchange(grids=["g1", "g2"], stop_codes={"g1": "51291"})
        report = stop_grid_bots(ex, sleep=no_sleep)
        self.assertEqual(report["stopped"], ["g2"])
        self.assertEqual(report["skipped"], ["g1"])
        self.assertEqual((report["failed"], report["errors"]), ([], []))

    def test_whole_batch_already_stopped_exception_is_not_failure(self):
        """code 1 (отклонены все) CCXT бросает исключением — sCode берутся из его текста."""
        ex = FakeBotExchange(grids=["g1", "g2"], stop_codes={"g1": "51291", "g2": "51291"})
        report = stop_grid_bots(ex, sleep=no_sleep)
        self.assertEqual(sorted(report["skipped"]), ["g1", "g2"])
        self.assertEqual(report["failed"], [])

    def test_sCode_failure_in_batch_does_not_block_others(self):
        ex = FakeBotExchange(grids=["g1", "g2", "g3"], stop_codes={"g2": "51000"})
        report = stop_grid_bots(ex, sleep=no_sleep)
        self.assertEqual(sorted(report["stopped"]), ["g1", "g3"])
        self.assertEqual(report["failed"], ["g2"])
        self.assertEqual(report["errors"], [{"id": "g2", "sCode": "51000", "sMsg": "stop failed"}])
        self.assertEqual(len(ex.grid_stop_batches), 1)  # не транзиентный отказ — без повторов

    def test_transient_sCode_is_retried_only_for_that_bot(self):
        sleeps: list[float] = []
        ex = FakeBotExchange(grids=["g1", "g2"], stop_codes={"g1": ["50011"]})
        report = stop_grid_bots(ex, sleep=sleeps.append)
        self.assertEqual(sorted(report["stopped"]), ["g1", "g2"])
        self.assertEqual(report["failed"], [])
        self.assertEqual(ex.grid_stop_batches, [["g2", "g1"], ["g1"]])
        self.assertEqual(sleeps, [1.0])

    def test_network_error_retries_whole_batch(self):
        sleeps: list[float] = []
        ex = FakeBotExchange(grids=["g1", "g2"], stop_raises={"g1": [ccxt.RequestTimeout("timeout")]})
        report = stop_grid_bots(ex, sleep=sleeps.append)
        self.assertEqual(sorted(report["stopped"]), ["g1", "g2"])
        self.assertEqual(ex.grid_stop_batches, [["g2", "g1"], ["g2", "g1"]])
        self.assertEqual(sleeps, [1.0])

    def test_lost_response_then_already_stopped_is_not_failure(self):
        """stop дошёл до биржи, ответ потерян (таймаут) → повтор даёт 51291 → не провал."""
        ex = FakeBotExchange(grids=["g1"], lost_response={"g1"})
        report = stop_grid_bots(ex, sleep=no_sleep)
        self.assertEqual(report["failed"], [])
        self.assertEqual(report["skipped"], ["g1"])
        self.assertEqual(ex.grids, {})

    def test_persistent_transient_error_fails_after_two_retries(self):
        sleeps: list[float] = []
        ex = FakeBotExchange(grids=["g1"], stop_raises={"g1": ccxt.RateLimitExceeded('okx {"code":"50011"}')})
        report = stop_grid_bots(ex, sleep=sleeps.append)
        self.assertEqual(report["failed"], ["g1"])
        self.assertEqual(len(ex.grid_stop_batches), 3)  # 1 + 2 повтора
        self.assertEqual(sleeps, [1.0, 2.0])
        self.assertIn("50011", report["errors"][0]["error"])

    def test_bot_missing_in_response_is_failure(self):
        ex = FakeBotExchange(grids=["g1", "g2"])
        ex.private_post_tradingbot_grid_stop_order_algo = lambda reqs: {
            "code": "0", "data": [{"algoId": "g2", "sCode": "0", "sMsg": ""}]}
        report = stop_grid_bots(ex, sleep=no_sleep)
        self.assertEqual(report["stopped"], ["g2"])
        self.assertEqual(report["failed"], ["g1"])
        self.assertIn("не подтверждена", report["errors"][0]["error"])

    def test_bot_without_algoId_in_list_is_failure(self):
        ex = FakeBotExchange()
        ex.private_get_tradingbot_grid_orders_algo_pending = lambda params: {
            "code": "0", "data": [{"instId": "ETH-USDT"}] if params["algoOrdType"] == "grid" else []}
        report = stop_grid_bots(ex)
        self.assertEqual(len(report["failed"]), 1)
        self.assertIn("algoId", report["failed"][0])

    def test_algo_ids_filter_stops_only_selected(self):
        ex = FakeBotExchange(grids=["fleet1", "tst1", ("fleet2", "contract_grid")])
        report = stop_grid_bots(ex, algo_ids=["tst1"])
        self.assertEqual(report["stopped"], ["tst1"])
        self.assertEqual(ex.grid_stop_batches, [["tst1"]])  # в запросе — только тестовый бот
        self.assertEqual(sorted(ex.grids), ["fleet1", "fleet2"])  # флот не тронут

    def test_empty_algo_ids_stops_nothing(self):
        ex = FakeBotExchange(grids=["g1"])
        self.assertEqual(stop_grid_bots(ex, algo_ids=[])["stopped"], [])
        self.assertEqual(ex.grid_stop_batches, [])

    def test_stopping_and_no_close_position_states(self):
        ex = FakeBotExchange(grids=["run", ("stp", "grid", "stopping"),
                                    ("ncp", "contract_grid", "no_close_position")])
        report = stop_grid_bots(ex)
        self.assertEqual(report["stopped"], ["run"])
        self.assertEqual(sorted(report["skipped"]), ["ncp", "stp"])
        # закрыть оставленную позицию — только явным stopType 1
        ex = FakeBotExchange(grids=[("ncp", "contract_grid", "no_close_position")])
        self.assertEqual(stop_grid_bots(ex, stop_type="1")["stopped"], ["ncp"])
        self.assertEqual(ex.grid_stop_requests[0]["stopType"], "1")

    def test_idempotent_when_no_bots(self):
        self.assertEqual(stop_grid_bots(FakeBotExchange()),
                         {"stopped": [], "failed": [], "skipped": [], "errors": []})


class GridApiPresenceTest(unittest.TestCase):
    """Без tradingBot/grid у клиента grid-этап kill невозможен — у реального клиента методы обязаны быть."""

    def test_real_client_has_grid_api(self):
        ex = OKXExchange({})  # без ключей и без сети
        for name in ("private_get_tradingbot_grid_orders_algo_pending",
                     "private_post_tradingbot_grid_stop_order_algo"):
            self.assertTrue(callable(getattr(ex, name, None)), name)

    def test_real_ccxt_client_without_grid_api_is_failure(self):
        ex = OKXExchange({})
        ex.private_post_tradingbot_grid_stop_order_algo = None  # как ccxt без tradingBot/grid
        report = stop_grid_bots(ex)
        self.assertEqual(len(report["failed"]), 1)
        self.assertIn("tradingBot/grid", report["failed"][0])


class EmergencyStopGridTest(unittest.TestCase):
    def test_grid_list_failure_reaches_report_and_dca_still_stopped(self):
        ex = FakeBotExchange(grids=["g1"], dca=[("s1", "spot_dca")],
                             list_raises={"grid": ccxt.AuthenticationError("50113 Invalid Sign")})
        report = emergency_stop(ex, sleep=no_sleep)
        self.assertEqual(report["bots_stopped"], ["s1"])
        self.assertTrue(any("grid" in str(f) for f in report["failed"]), report["failed"])

    def test_transient_grid_stop_is_retried(self):
        sleeps: list[float] = []
        ex = FakeBotExchange(grids=["g1"], stop_raises={"g1": [ccxt.RequestTimeout("timeout")]})
        report = emergency_stop(ex, sleep=sleeps.append)
        self.assertEqual(report["bots_stopped"], ["g1"])
        self.assertEqual(report["failed"], [])
        self.assertEqual(sleeps, [1.0])


class KillSwitchGridTest(unittest.TestCase):
    """risk.kill_switch → kill_switch_dispatch → emergency_stop. Риск-ядро — временная БД."""

    def setUp(self):
        self._before = registered_kill_callbacks()
        self._tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        risk.init(Path(self._tmp.name) / "risk.db")

    def tearDown(self):
        for cb in registered_kill_callbacks():
            if cb not in self._before:
                unregister_kill_callback(cb)
        risk.init(Path(self._tmp.name) / "unused.db")  # отпустить файл до очистки
        self._tmp.cleanup()

    def _kill(self, ex) -> dict:
        register_kill_callback(lambda flatten: emergency_stop(ex, sleep=no_sleep))
        risk.set_order_canceller(kill_switch_dispatch)
        return risk.kill_switch(flatten=False, by="test FLEET-KILL-GRID-HARDEN")

    def test_kill_report_escalates_grid_list_failure(self):
        ex = FakeBotExchange(grids=["g1"], list_raises={"grid": ccxt.ExchangeError("51000 Parameter error")})
        report = self._kill(ex)
        self.assertTrue(any("grid" in str(f) for f in report["failed"]), report)
        self.assertIn("g1", ex.grids)  # не остановлен — и отчёт kill это показывает

    def test_kill_stops_grid_fleet_beyond_one_page(self):
        ex = FakeBotExchange(grids=[f"g{i:04d}" for i in range(120)])
        report = self._kill(ex)
        self.assertEqual(len(report["cancelled"]), 120)
        self.assertEqual(report["failed"], [])
        self.assertEqual(ex.grids, {})


if __name__ == "__main__":
    unittest.main()
