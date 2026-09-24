"""Хранилище и реконсилятор на фейковой бирже (без сети)."""
import sqlite3
import tempfile
import time
import unittest
from pathlib import Path

import ccxt

from src.reconciler import Reconciler, order_record_from_okx, trade_record_from_ws_order
from src.storage import OrderRecord, Storage, TradeRecord


def okx_order(ord_id, state="live", acc="0", inst="BTC-USDT", **extra):
    # clOrdId по умолчанию с префиксом bot* — «свой» ордер (reconciler.is_own_order);
    # внешний ордер задавать явно: okx_order("1", clOrdId="lt123")
    return {
        "ordId": ord_id, "clOrdId": f"bot{ord_id}", "instId": inst, "side": "buy",
        "ordType": "limit", "px": "50000", "sz": "0.001", "state": state,
        "accFillSz": acc, "avgPx": "", "fee": "-0.000001", "cTime": "1790000000000",
        "uTime": "1790000001000", **extra,
    }


def okx_fill(trade_id, inst="BTC-USDT", bill=None):
    return {"instId": inst, "tradeId": trade_id, "ordId": "1", "side": "buy",
            "fillPx": "50000", "fillSz": "0.001", "fee": "-0.05", "feeCcy": "USDT",
            "ts": "1790000000000", "billId": bill or trade_id}


class FakeExchange:
    def __init__(self, pending=(), orders=None, fills=(), positions=()):
        self.pending = list(pending)
        self.orders = orders or {}
        self.fills = list(fills)
        self.positions = list(positions)

    def private_get_trade_orders_pending(self, params):
        items = [o for o in self.pending if o["instId"] == params.get("instId", o["instId"])]
        return {"data": items[: int(params["limit"])]}

    def private_get_trade_order(self, params):
        if params["ordId"] not in self.orders:
            raise ccxt.OrderNotFound("okx 51603 Order does not exist")
        return {"data": [self.orders[params["ordId"]]]}

    def private_get_trade_fills(self, params):
        items = self.fills
        if "after" in params:
            idx = next(i for i, f in enumerate(items) if f["billId"] == params["after"])
            items = items[idx + 1:]
        return {"data": items[: int(params["limit"])]}

    def private_get_account_positions(self, params=None):
        return {"data": self.positions}


class StorageCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.path = Path(self._tmp.name) / "state.db"
        self.db = Storage(self.path)

    def tearDown(self):
        self.db.close()
        self._tmp.cleanup()


class StorageTest(StorageCase):
    def test_final_state_is_sticky(self):
        # WS прислал fill раньше, чем вернулся ответ REST на place (state=live)
        self.db.upsert_order(order_record_from_okx(okx_order("1", state="filled", acc="0.001")))
        late = order_record_from_okx(okx_order("1", state="live", acc="0"))
        self.db.upsert_order(late)
        row = self.db.get_order("1")
        self.assertEqual(row["state"], "filled")
        self.assertEqual(row["filled_sz"], 0.001)
        self.assertEqual(self.db.get_open_orders(), [])

    def test_trades_idempotent_and_keyed_by_instrument(self):
        t = TradeRecord("BTC-USDT", "42", "1", "buy", 1.0, 1.0, 0.1, "USDT", time.time())
        self.assertTrue(self.db.insert_trade(t))
        self.assertFalse(self.db.insert_trade(t))
        # tradeId у OKX уникален только в пределах инструмента
        self.assertTrue(self.db.insert_trade(TradeRecord("ETH-USDT", "42", "2", "buy", 1, 1, 0, "USDT", time.time())))

    def test_close_is_idempotent_and_usable_as_context_manager(self):
        # close() безопасен при повторном вызове и не мешает штатной работе
        # (используется в tearDown этого файла) — задача STORAGE-CLOSE.
        self.db.close()
        self.db.close()  # повторный вызов не должен падать

        path = Path(self._tmp.name) / "ctx.db"
        with Storage(path) as db:
            db.upsert_order(order_record_from_okx(okx_order("ctx1")))
            self.assertIsNotNone(db.get_order("ctx1"))
        # после выхода из "with" WAL-файлы слиты в основной, соединений не осталось
        self.assertFalse((path.parent / f"{path.name}-wal").exists())

    def test_migration_from_v0(self):
        path = Path(self._tmp.name) / "old.db"
        conn = sqlite3.connect(path)
        conn.executescript("""
            CREATE TABLE orders (ord_id TEXT PRIMARY KEY, inst_id TEXT NOT NULL, side TEXT NOT NULL,
                ord_type TEXT NOT NULL, px REAL, sz REAL NOT NULL, state TEXT NOT NULL,
                filled_sz REAL DEFAULT 0, avg_px REAL DEFAULT 0, fee REAL DEFAULT 0,
                create_time REAL NOT NULL, update_time REAL NOT NULL, raw_json TEXT);
            CREATE TABLE trades (trade_id TEXT PRIMARY KEY, ord_id TEXT, inst_id TEXT NOT NULL,
                side TEXT NOT NULL, px REAL NOT NULL, sz REAL NOT NULL, fee REAL DEFAULT 0, ts REAL NOT NULL);
            INSERT INTO orders VALUES ('1','BTC-USDT','buy','limit',1,1,'live',0,0,0,1,1,'{}');
            INSERT INTO trades VALUES ('t1','1','BTC-USDT','buy',1,1,0,1);
        """)
        conn.commit()
        conn.close()

        db = Storage(path)
        self.assertEqual(len(db.get_open_orders("BTC-USDT")), 1)
        self.assertEqual(db.get_trades()[0]["trade_id"], "t1")
        db.close()
        # Внимание: "with sqlite3.connect(...) as c" НЕ закрывает соединение —
        # __exit__ у sqlite3.Connection делает только commit/rollback, поэтому
        # без явного c.close() соединение утекает до сборки мусора интерпретатором
        # (источник ResourceWarning, задача STORAGE-CLOSE).
        c = sqlite3.connect(path)
        try:
            self.assertEqual(c.execute("PRAGMA user_version").fetchone()[0], 1)
            self.assertIn("cl_ord_id", {r[1] for r in c.execute("PRAGMA table_info(orders)")})
        finally:
            c.close()


class ReconcilerTest(StorageCase):
    def reconcile(self, ex):
        return Reconciler(ex, self.db).sync_orders("BTC-USDT")

    def test_remote_order_converges(self):
        # свой ордер (префикс bot*) с биржи, неизвестный локально — рассинхрон, подбирается
        ex = FakeExchange(pending=[okx_order("1")])
        first = self.reconcile(ex)
        self.assertEqual(first["missing_locally"], 1)
        second = self.reconcile(ex)
        self.assertEqual((second["missing_locally"], second["missing_on_exchange"], second["changed"]), (0, 0, 0))
        self.assertEqual(self.db.get_order("1")["inst_id"], "BTC-USDT")

    def test_external_order_is_not_divergence(self):
        # внешний ордер (без нашего префикса clOrdId: нагрузочный тест lt*, ручной) —
        # не рассинхрон, в БД не подбирается
        ex = FakeExchange(pending=[okx_order("1", clOrdId="lt999")])
        first = self.reconcile(ex)
        self.assertEqual(first["external_orders"], 1)
        self.assertEqual((first["missing_locally"], first["missing_on_exchange"], first["changed"]), (0, 0, 0))
        self.assertIsNone(self.db.get_order("1"))
        # внешний ордер отменён внешне между сверками — тоже не рассинхрон
        ex.pending = []
        second = self.reconcile(ex)
        self.assertEqual((second["external_orders"], second["missing_on_exchange"],
                          second["missing_locally"], second["changed"]), (0, 0, 0, 0))
        self.assertEqual(second["resolved"], {})

    def test_external_order_from_ws_vanishes_without_divergence(self):
        # внешний ордер попал в БД по WS (чужой clOrdId), потом исчез с биржи —
        # не missing_on_exchange (случай divergences:1 нагрузочного теста)
        self.db.upsert_order(order_record_from_okx(okx_order("1", clOrdId="lt777")))
        result = self.reconcile(FakeExchange())
        self.assertEqual((result["missing_on_exchange"], result["resolved"]), (0, {}))

    def test_manual_order_without_cl_ord_id_is_external(self):
        # ручной ордер с пустым clOrdId — внешний
        ex = FakeExchange(pending=[okx_order("1", clOrdId="")])
        result = self.reconcile(ex)
        self.assertEqual((result["external_orders"], result["missing_locally"]), (1, 0))

    def test_vanished_filled_order_is_not_marked_canceled(self):
        self.db.upsert_order(order_record_from_okx(okx_order("1")))
        ex = FakeExchange(pending=[], orders={"1": okx_order("1", state="filled", acc="0.001", avgPx="49990")})
        result = self.reconcile(ex)
        self.assertEqual(result["resolved"], {"filled": 1})
        row = self.db.get_order("1")
        self.assertEqual((row["state"], row["filled_sz"], row["avg_px"]), ("filled", 0.001, 49990))

    def test_vanished_unknown_order_is_canceled(self):
        self.db.upsert_order(order_record_from_okx(okx_order("1")))
        self.assertEqual(self.reconcile(FakeExchange())["resolved"], {"canceled": 1})
        self.assertEqual(self.db.get_order("1")["state"], "canceled")

    def test_partial_fill_counts_as_divergence(self):
        self.db.upsert_order(order_record_from_okx(okx_order("1")))
        ex = FakeExchange(pending=[okx_order("1", state="partially_filled", acc="0.0005")])
        self.assertEqual(self.reconcile(ex)["changed"], 1)
        self.assertEqual(self.db.get_order("1")["state"], "partially_filled")

    def test_sync_trades_paginates_until_known(self):
        ex = FakeExchange(fills=[okx_fill(str(i)) for i in range(150)])
        rec = Reconciler(ex, self.db)
        self.assertEqual(rec.sync_trades("BTC-USDT")["new_trades"], 150)
        self.assertEqual(rec.sync_trades("BTC-USDT")["new_trades"], 0)
        self.assertAlmostEqual(self.db.get_trades(limit=1)[0]["fee"], 0.05)  # комиссия положительна

    def test_positions_closed(self):
        pos = {"instId": "BTC-USDT-SWAP", "posSide": "net", "pos": "2", "avgPx": "50000", "upl": "1", "liqPx": ""}
        rec = Reconciler(FakeExchange(positions=[pos]), self.db)
        self.assertEqual(rec.sync_positions()["remote_positions"], 1)
        rec.ex.positions = []
        self.assertEqual(rec.sync_positions()["closed"], 1)
        self.assertEqual(self.db.get_positions(), [])


class ParsingTest(unittest.TestCase):
    def test_ws_fill_becomes_trade(self):
        msg = okx_order("1", state="filled", acc="0.001", tradeId="77", fillPx="50000",
                        fillSz="0.001", fillFee="-0.0000001", fillFeeCcy="BTC", fillTime="1790000002000")
        trade = trade_record_from_ws_order(msg)
        self.assertEqual((trade.trade_id, trade.fee_ccy, trade.ts), ("77", "BTC", 1790000002.0))

    def test_ws_non_fill_update_has_no_trade(self):
        self.assertIsNone(trade_record_from_ws_order(okx_order("1", tradeId="", fillSz="0")))

    def test_market_order_without_px(self):
        self.assertIsNone(order_record_from_okx(okx_order("1", px="")).px)


if __name__ == "__main__":
    unittest.main()
