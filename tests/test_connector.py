"""Коннектор: expTime в заголовке, clOrdId, отмена всех ордеров для kill-switch."""
import json
import unittest

from src.config import Settings
from src.connector import (
    OKXExchange,
    cancel_all_orders,
    create_exchange,
    emergency_stop,
    fetch_pending_orders,
    new_client_order_id,
)


class ExpTimeTest(unittest.TestCase):
    def test_exp_time_goes_to_header_not_body(self):
        ex = OKXExchange({"apiKey": "k", "secret": "s", "password": "p"})
        req = ex.sign("trade/order", "private", "POST", {"instId": "BTC-USDT", "sz": "1", "expTime": "123"})
        self.assertEqual(req["headers"]["expTime"], "123")
        self.assertNotIn("expTime", json.loads(req["body"]))

    def test_exp_time_in_batch_orders(self):
        # create_order в CCXT ставит одиночный ордер через trade/batch-orders (список)
        ex = OKXExchange({"apiKey": "k", "secret": "s", "password": "p"})
        orders = [{"instId": "BTC-USDT", "expTime": "200"}, {"instId": "ETH-USDT", "expTime": "100"}]
        req = ex.sign("trade/batch-orders", "private", "POST", orders)
        self.assertEqual(req["headers"]["expTime"], "100")
        self.assertTrue(all("expTime" not in o for o in json.loads(req["body"])))

    def test_create_order_sends_exp_time_header(self):
        ex = OKXExchange({"apiKey": "k", "secret": "s", "password": "p"})
        ex.markets = None
        sent = {}

        def fake_fetch(url, method="GET", headers=None, body=None):
            sent.update(url=url, headers=headers, body=body)
            return {"code": "0", "data": [{"ordId": "1", "clOrdId": "c", "sCode": "0", "sMsg": ""}]}

        ex.fetch = fake_fetch
        ex.load_markets = lambda *a, **k: None
        ex.set_markets([ex.safe_market_structure({
            "id": "BTC-USDT", "symbol": "BTC/USDT", "base": "BTC", "quote": "USDT", "type": "spot",
            "spot": True, "contract": False, "precision": {"amount": 0.00000001, "price": 0.1},
            "info": {"instType": "SPOT"},
        })])
        ex.create_order("BTC-USDT", "limit", "buy", 0.001, 50000, {"expTime": "999"})
        self.assertEqual(sent["headers"]["expTime"], "999")
        self.assertNotIn("expTime", sent["body"])

    def test_domain_applied(self):
        ex = create_exchange(Settings("demo", "k", "s", "p", "eea.okx.com"))
        self.assertTrue(ex.urls["api"]["rest"].startswith("https://{hostname}"))
        self.assertEqual(ex.hostname, "eea.okx.com")

    def test_settings_repr_hides_secrets(self):
        text = repr(Settings("demo", "KEY123", "SECRET456", "PASS789"))
        for secret in ("KEY123", "SECRET456", "PASS789"):
            self.assertNotIn(secret, text)


class ClientOrderIdTest(unittest.TestCase):
    def test_okx_constraints(self):
        ids = {new_client_order_id() for _ in range(1000)}
        self.assertEqual(len(ids), 1000)
        for i in ids:
            self.assertLessEqual(len(i), 32)
            self.assertTrue(i.isalnum())


class FakeOKX:
    """Минимальная биржа: orders-pending с курсором after, отмена пачками."""

    def __init__(self, n_orders=0, n_algos=0, stuck=()):
        self.orders = {f"{i:05d}": {"ordId": f"{i:05d}", "instId": "BTC-USDT"} for i in range(n_orders)}
        self.algos = {f"a{i}": {"algoId": f"a{i}", "instId": "BTC-USDT", "ordType": "conditional"}
                      for i in range(n_algos)}
        self.stuck = set(stuck)
        self.batch_sizes = []

    def private_get_trade_orders_pending(self, params):
        items = sorted(self.orders.values(), key=lambda o: o["ordId"], reverse=True)
        if "after" in params:
            items = [o for o in items if o["ordId"] < params["after"]]
        return {"data": items[: int(params["limit"])]}

    def private_get_trade_orders_algo_pending(self, params):
        types = params["ordType"].split(",")
        return {"data": [a for a in self.algos.values() if a["ordType"] in types][: int(params["limit"])]}

    def _cancel(self, book, reqs, key):
        self.batch_sizes.append(len(reqs))
        data = []
        for r in reqs:
            if r[key] in self.stuck:
                data.append({key: r[key], "sCode": "51400", "sMsg": "cancel failed"})
            else:
                book.pop(r[key], None)
                data.append({key: r[key], "sCode": "0", "sMsg": ""})
        return {"code": "0", "data": data}

    def private_post_trade_cancel_batch_orders(self, reqs):
        return self._cancel(self.orders, reqs, "ordId")

    def private_post_trade_cancel_algos(self, reqs):
        return self._cancel(self.algos, reqs, "algoId")


class CancelAllTest(unittest.TestCase):
    def test_pagination(self):
        self.assertEqual(len(fetch_pending_orders(FakeOKX(n_orders=250))), 250)

    def test_cancels_orders_and_algos_in_batches(self):
        ex = FakeOKX(n_orders=45, n_algos=12)
        report = cancel_all_orders(ex, sleep=lambda s: None)
        self.assertEqual(len(report["cancelled"]), 57)
        self.assertEqual(report["failed"], [])
        self.assertTrue(all(size <= 20 for size in ex.batch_sizes))
        self.assertFalse(ex.orders or ex.algos)

    def test_idempotent_when_nothing_open(self):
        self.assertEqual(cancel_all_orders(FakeOKX(), sleep=lambda s: None),
                         {"cancelled": [], "failed": [], "errors": []})

    def test_emergency_stop_includes_grid_bots(self):
        ex = FakeOKX(n_orders=2)
        ex.bots = {"g1": {"algoId": "g1", "instId": "BTC-USDT"}, "g2": {"algoId": "g2", "instId": "ETH-USDT"}}
        stop_requests = []

        def pending_bots(params):
            return {"data": list(ex.bots.values()) if params["algoOrdType"] == "grid" else []}

        def stop_bots(reqs):
            stop_requests.extend(reqs)
            data = [{"algoId": r["algoId"], "sCode": "0" if r["algoId"] == "g1" else "50000", "sMsg": ""}
                    for r in reqs]
            return {"code": "2", "data": data}

        ex.private_get_tradingbot_grid_orders_algo_pending = pending_bots
        ex.private_post_tradingbot_grid_stop_order_algo = stop_bots

        report = emergency_stop(ex)
        self.assertEqual(report["bots_stopped"], ["g1"])
        self.assertIn("g2", report["failed"])
        self.assertEqual(sorted(report["cancelled"]), ["00000", "00001", "g1"])
        # без flatten: stopType 2 — монеты остаются на счёте
        self.assertTrue(all(r["stopType"] == "2" and r["algoOrdType"] == "grid" for r in stop_requests))

        self.assertNotIn("bots_stopped", emergency_stop(FakeOKX(), include_bots=False))

    def test_reports_stuck_orders(self):
        sleeps = []
        report = cancel_all_orders(FakeOKX(n_orders=3, stuck={"00001"}), attempts=3, sleep=sleeps.append)
        self.assertEqual(report["failed"], ["00001"])
        self.assertEqual(sorted(report["cancelled"]), ["00000", "00002"])
        self.assertEqual(sleeps, [1.0, 2.0, 4.0])  # экспоненциальный backoff
        self.assertTrue(any(e.get("sCode") == "51400" for e in report["errors"]))


if __name__ == "__main__":
    unittest.main()
