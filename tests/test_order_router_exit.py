"""Выход из позиции через OrderRouter — не вход (ROUTER-EXIT).

Выход не проходит risk.check_entry_allowed (kill-switch, breaker'ы, свежесть
equity, лимит входов, хедж), но и не может открыть или нарастить позицию:
позиция по inst_id должна быть в риск-ядре, сторона — противоположная, размер —
не больше остатка, у контрактов — reduceOnly. По исполнению выход освобождает
слот риска. Сеть не используется: FakeOkx, temp-БД риска и storage.
"""
import tempfile
import time
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock

from src import risk
from src.order_router import OrderRouter

PX = 3_000.0
SPOT = "ETH/USDT"
SWAP = "ETH-USDT-SWAP"


class FakeOkx:
    """create_order и fetch_order без сети; исполнение ордера задаёт тест.

    fill="closed" — fetch_order показывает ордер исполненным целиком, "open" —
    висящим без исполнения. set_state задаёт статус и исполнение конкретного ордера.
    """

    def __init__(self, fill="closed", config=None, avail=None):
        self.fill = fill
        self.config = dict(config or {"acctLv": "1", "autoLoan": False})
        self.avail = dict(avail or {})
        self.created: list[dict] = []
        self.states: dict[str, tuple[str, float]] = {}
        self.fetch_error: Exception | None = None
        self.headers: dict = {}

    def milliseconds(self):
        return int(time.time() * 1000)

    def private_get_account_config(self, params=None):
        return {"code": "0", "data": [dict(self.config)]}

    def private_get_account_balance(self, params=None):
        ccy = (params or {}).get("ccy")
        details = [{"ccy": ccy, "availBal": str(self.avail[ccy])}] if ccy in self.avail else []
        return {"code": "0", "data": [{"details": details}]}

    def create_order(self, symbol, ord_type, side, amount, price=None, params=None):
        oid = f"x{len(self.created) + 1}"
        self.created.append({"id": oid, "symbol": symbol, "type": ord_type, "side": side,
                             "amount": amount, "price": price, "params": dict(params or {})})
        return {"id": oid, "status": None, "symbol": symbol}

    def fetch_order(self, order_id, symbol=None, params=None):
        if self.fetch_error is not None:
            raise self.fetch_error
        amount = next(o["amount"] for o in self.created if o["id"] == order_id)
        status, filled = self.states.get(
            order_id, ("closed", amount) if self.fill == "closed" else ("open", 0.0))
        return {"id": order_id, "status": status, "filled": filled, "symbol": symbol}

    def set_state(self, order_id, status, filled):
        self.states[order_id] = (status, filled)

    # --- пустая поверхность connector.emergency_stop (kill-switch) ---

    def private_get_trade_orders_pending(self, params=None):
        return {"code": "0", "data": []}

    def private_get_trade_orders_algo_pending(self, params=None):
        return {"code": "0", "data": []}

    def private_get_tradingbot_grid_orders_algo_pending(self, params=None):
        return {"code": "0", "data": []}

    def private_get_tradingbot_dca_ongoing_list(self, params=None):
        return {"code": "0", "data": []}


class RouterExitTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.root = Path(self._tmp.name)
        risk.init(self.root / "risk.db")
        risk.update_equity(10_000)
        self._routers: list[OrderRouter] = []

    def tearDown(self):
        for router in self._routers:
            router.close()
        risk.init(self.root / "unused.db")  # отпустить файл до очистки
        self._tmp.cleanup()

    def router(self, ex, **kwargs) -> OrderRouter:
        router = OrderRouter(ex, db_path=self.root / f"bot{len(self._routers)}.db", **kwargs)
        self._routers.append(router)
        return router

    def buy(self, router, inst_id=SPOT, sz=0.5) -> dict:
        result = router.place_order(inst_id, "buy", "limit", px=PX, sz=sz)
        self.assertTrue(result["ok"], result)
        return result

    def assert_denied(self, result, stage):
        self.assertFalse(result["ok"], result)
        self.assertEqual(result["stage"], stage, result)

    # --- критерий доски ---

    def test_sell_after_buy_passes_and_frees_slot(self):
        ex = FakeOkx()
        router = self.router(ex)
        self.buy(router)
        entries = risk.status()["entries_today"]
        # раньше: «по ETH/USDT уже есть открытая позиция (хедж запрещён)»
        result = router.place_exit_order(SPOT, "sell", "limit", px=PX * 1.01, sz=0.5)
        self.assertTrue(result["ok"], result)
        self.assertTrue(result["exit"])
        self.assertTrue(result["closed"])
        self.assertEqual(ex.created[-1]["side"], "sell")
        self.assertEqual(ex.created[-1]["params"]["tdMode"], "cash")
        self.assertTrue(ex.created[-1]["params"]["clOrdId"].startswith("botr"))
        st = risk.status()
        self.assertEqual(st["open_risk"], [])  # слот освобождён
        self.assertEqual(st["entries_today"], entries)  # выход — не вход
        self.assertIsNone(risk.open_position(SPOT))
        self.buy(router)  # по инструменту снова возможен вход

    def test_exit_passes_under_global_breaker_entry_denied(self):
        ex = FakeOkx()
        router = self.router(ex)
        self.buy(router)
        risk.trip_breaker("тест", scope="global")
        self.assert_denied(router.place_order("BTC/USDT", "buy", "limit", px=PX, sz=0.001),
                           "check_entry_allowed")
        result = router.place_order(SPOT, "sell", "market", sz=0.5, is_exit=True)
        self.assertTrue(result["ok"], result)
        self.assertIsNone(risk.open_position(SPOT))

    def test_exit_passes_with_stale_equity_entry_denied(self):
        ex = FakeOkx()
        router = self.router(ex)
        self.buy(router)
        later = time.time() + risk.EQUITY_MAX_AGE_S + 60
        with mock.patch.object(risk, "_utc_now", lambda: later):
            denied = router.place_order("BTC/USDT", "buy", "limit", px=PX, sz=0.001)
            self.assert_denied(denied, "check_entry_allowed")
            self.assertIn("equity устарела", denied["reason"])
            result = router.place_exit_order(SPOT, "sell", "limit", px=PX)
        self.assertTrue(result["ok"], result)
        self.assertEqual(result["sz"], 0.5)  # sz=None — весь остаток
        self.assertIsNone(risk.open_position(SPOT))

    def test_exit_passes_under_kill_switch_and_daily_breaker(self):
        ex = FakeOkx()
        router = self.router(ex)
        self.buy(router, SPOT)
        self.buy(router, SWAP, sz=2.0)
        risk.trip_breaker("тест", scope="daily")
        risk.kill_switch(by="тест")
        self.assertTrue(risk.status()["kill_active"])
        self.assert_denied(router.place_order("BTC/USDT", "buy", "limit", px=PX, sz=0.001),
                           "check_entry_allowed")
        self.assertTrue(router.place_exit_order(SPOT, "sell", "market")["ok"])
        self.assertTrue(router.place_exit_order(SWAP, "sell", "market")["ok"])
        self.assertEqual(risk.status()["open_risk"], [])

    # --- выход не открывает и не наращивает позицию ---

    def test_exit_without_position_is_denied(self):
        ex = FakeOkx()
        router = self.router(ex)
        result = router.place_exit_order(SPOT, "sell", "market", sz=0.1)
        self.assert_denied(result, "check_exit_allowed")
        self.assertIn("нет открытой позиции", result["reason"])
        self.assertEqual(ex.created, [])

    def test_exit_on_position_side_is_denied(self):
        ex = FakeOkx()
        router = self.router(ex)
        self.buy(router)
        result = router.place_order(SPOT, "buy", "limit", px=PX, sz=0.1, is_exit=True)
        self.assert_denied(result, "check_exit_allowed")
        self.assertEqual(len(ex.created), 1)  # только вход

    def test_exit_above_remaining_is_denied(self):
        ex = FakeOkx()
        router = self.router(ex)
        self.buy(router)
        result = router.place_exit_order(SPOT, "sell", "limit", px=PX, sz=0.6)
        self.assert_denied(result, "check_exit_allowed")
        self.assertIn("больше остатка", result["reason"])
        self.assertEqual(len(ex.created), 1)

    def test_entry_without_size_cannot_be_exited(self):
        """Вход движка или грида (register_entry без размера): остаток неизвестен."""
        ex = FakeOkx()
        router = self.router(ex)
        risk.register_entry(SPOT, "buy")
        result = router.place_exit_order(SPOT, "sell", "market", sz=0.1)
        self.assert_denied(result, "check_exit_allowed")
        self.assertIn("остаток", result["reason"])
        self.assertEqual(ex.created, [])

    def test_swap_exit_is_reduce_only(self):
        ex = FakeOkx()
        router = self.router(ex)
        self.buy(router, SWAP, sz=2.0)
        self.assertNotIn("reduceOnly", ex.created[0]["params"])  # вход — без reduceOnly
        result = router.place_exit_order(SWAP, "sell", "market", sz=2.0)
        self.assertTrue(result["ok"], result)
        self.assertIs(ex.created[-1]["params"]["reduceOnly"], True)

    def test_spot_exit_keeps_borrow_check(self):
        """Выход проходит проверки ордера как вход: заём в режиме autoLoan отклонён."""
        ex = FakeOkx(config={"acctLv": "3", "autoLoan": True}, avail={"USDT": 10_000.0, "ETH": 0.2})
        router = self.router(ex)
        self.buy(router)
        result = router.place_exit_order(SPOT, "sell", "market", sz=0.5)
        self.assert_denied(result, "account_mode")
        self.assertIn("заём", result["reason"])
        self.assertEqual(len(ex.created), 1)
        self.assertIsNotNone(risk.open_position(SPOT))

    def test_exit_validates_params_and_owner(self):
        ex = FakeOkx()
        router = self.router(ex)
        self.buy(router)
        self.assert_denied(router.place_exit_order(SPOT, "sell", "limit", sz=0.1), "exit_params")
        self.assert_denied(router.place_exit_order(SPOT, "sell", "market", sz=-1), "exit_params")
        self.assert_denied(router.place_exit_order(SPOT, "hold", "market"), "exit_params")
        for owner in ("zzz", "trd"):  # не зарегистрирован; не bot* — ордер агента, не кода
            with self.assertRaises(ValueError):
                router.place_exit_order(SPOT, "sell", "market", owner=owner)
        self.assertEqual(len(ex.created), 1)
        result = router.place_exit_order(SPOT, "sell", "market", owner="botsdca")
        self.assertTrue(result["ok"], result)
        self.assertTrue(ex.created[-1]["params"]["clOrdId"].startswith("botsdca"))
        self.assertIn("expTime", ex.created[-1]["params"])

    # --- исполнение и слот ---

    def test_partial_exit_keeps_slot_until_rest_is_sold(self):
        ex = FakeOkx()
        router = self.router(ex)
        self.buy(router)
        heat = risk.status()["portfolio_heat_pct"]
        result = router.place_exit_order(SPOT, "sell", "market", sz=0.2)
        self.assertTrue(result["ok"], result)
        self.assertFalse(result["closed"])
        self.assertAlmostEqual(result["remaining"], 0.3)
        self.assertEqual(risk.status()["portfolio_heat_pct"], heat)  # слот держится
        self.assert_denied(router.place_order(SPOT, "buy", "limit", px=PX, sz=0.1),
                           "check_entry_allowed")
        self.assert_denied(router.place_exit_order(SPOT, "sell", "market", sz=0.4),
                           "check_exit_allowed")
        self.assertTrue(router.place_exit_order(SPOT, "sell", "market")["closed"])
        self.assertEqual(risk.status()["portfolio_heat_pct"], 0.0)

    def test_unfilled_exit_reserves_remaining_until_settled(self):
        ex = FakeOkx(fill="open")
        router = self.router(ex)
        self.buy(router)
        first = router.place_exit_order(SPOT, "sell", "limit", px=PX * 1.02)
        self.assertTrue(first["ok"], first)
        self.assertTrue(first["pending"])
        self.assertIsNotNone(risk.open_position(SPOT))  # слот — по исполнению
        # весь остаток уже в неисполненном выходе
        self.assert_denied(router.place_exit_order(SPOT, "sell", "market", sz=0.1), "exit_pending")
        ex.set_state(first["exchange_order_id"], "canceled", 0.2)  # отменён с частичным исполнением
        reports = router.settle_exits()
        self.assertEqual(reports[0]["filled"], 0.2)
        self.assertFalse(reports[0]["pending"])
        self.assertEqual(router.pending_exits(), [])
        self.assertAlmostEqual(risk.open_position(SPOT)["sz"], 0.3)
        second = router.place_exit_order(SPOT, "sell", "market")
        self.assertTrue(second["ok"], second)
        self.assertAlmostEqual(second["sz"], 0.3)
        ex.set_state(second["exchange_order_id"], "closed", 0.3)
        router.settle_exits(SPOT)
        self.assertIsNone(risk.open_position(SPOT))

    def test_unreadable_exit_stays_reserved(self):
        ex = FakeOkx(fill="open")
        router = self.router(ex)
        self.buy(router)
        first = router.place_exit_order(SPOT, "sell", "limit", px=PX * 1.02, sz=0.3)
        ex.fetch_error = RuntimeError("timeout")
        self.assertIn("error", router.settle_exits()[0])
        self.assertEqual(len(router.pending_exits()), 1)
        # свободно 0.2: выход на 0.3 отклонён ещё до биржи
        self.assert_denied(router.place_exit_order(SPOT, "sell", "market", sz=0.3), "exit_pending")
        ex.fetch_error = None
        ex.set_state(first["exchange_order_id"], "closed", 0.3)
        self.assertTrue(router.place_exit_order(SPOT, "sell", "market", sz=0.2)["ok"])
        ex.set_state(ex.created[-1]["id"], "closed", 0.2)
        router.settle_exits()
        self.assertIsNone(risk.open_position(SPOT))


class RiskExitCoreTest(unittest.TestCase):
    """API риск-ядра для выхода: check_exit_allowed и release_position."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        risk.init(Path(self._tmp.name) / "risk.db")
        risk.update_equity(10_000)

    def tearDown(self):
        risk.init(Path(self._tmp.name) / "unused.db")
        self._tmp.cleanup()

    def open(self, inst_id=SPOT, side="buy", sz=1.0):
        risk.register_entry(inst_id, side)
        self.assertTrue(risk.register_entry_size(inst_id, side, sz))
        return risk.open_position(inst_id)

    def test_short_position_exits_only_by_buy(self):
        self.open(SWAP, "sell", 3.0)
        self.assertFalse(risk.check_exit_allowed(SWAP, "sell")[0])
        allowed, _, pos = risk.check_exit_allowed(SWAP, "buy", 3.0)
        self.assertTrue(allowed)
        self.assertEqual(pos["exit_sz"], 3.0)

    def test_entry_size_needs_matching_slot(self):
        self.assertFalse(risk.register_entry_size(SPOT, "buy", 1.0))  # слота нет
        risk.register_entry(SPOT, "buy")
        self.assertFalse(risk.register_entry_size(SPOT, "sell", 1.0))  # не та сторона
        self.assertFalse(risk.register_entry_size(SPOT, "buy", float("nan")))
        self.assertIsNone(risk.open_position(SPOT)["sz"])

    def test_late_fill_of_old_exit_does_not_touch_new_position(self):
        old = self.open(sz=1.0)
        risk.record_pnl(SPOT, 5.0, datetime.now(timezone.utc))  # позиция закрыта стратегией
        new = self.open(sz=2.0)
        self.assertNotEqual(old["pos_id"], new["pos_id"])
        result = risk.release_position(SPOT, 1.0, old["pos_id"])
        self.assertEqual(result["released"], 0.0)
        self.assertEqual(risk.open_position(SPOT)["sz"], 2.0)

    def test_record_pnl_after_release_keeps_streaks(self):
        """Освобождение слота — не закрытие сделки: серии ведёт record_pnl."""
        pos = self.open(sz=1.0)
        self.assertTrue(risk.release_position(SPOT, 1.0, pos["pos_id"])["closed"])
        self.assertEqual(risk.status()["global_loss_streak"], 0)
        risk.record_pnl(SPOT, -10.0, datetime.now(timezone.utc))
        self.assertEqual(risk.status()["global_loss_streak"], 1)

    def test_stale_size_after_spot_buy_is_ignored(self):
        """register_spot_buy (DCA) удаляет слот: прежний размер для выхода недействителен."""
        self.open(sz=1.0)
        risk.register_spot_buy(SPOT)
        self.assertFalse(risk.check_exit_allowed(SPOT, "sell")[0])
        risk.register_entry(SPOT, "buy")  # новый слот без размера
        allowed, reason, _ = risk.check_exit_allowed(SPOT, "sell", 0.5)
        self.assertFalse(allowed)
        self.assertIn("остаток", reason)


if __name__ == "__main__":
    unittest.main()
