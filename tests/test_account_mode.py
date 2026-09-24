"""Режим аккаунта и спот-ордера (SPOT-TDMODE): src/account_mode.py, OrderRouter, live-preflight.

Сеть не используется. Запрос CCXT проверяется настоящим ccxt.okx на рынке,
заданном вручную (create_order_request без отправки). Ответы account/config и
account/balance — как у demo OKX 24.09 (insights/okx-api.md §10 п. 22).
"""
import tempfile
import unittest
from pathlib import Path

import ccxt

from src import risk
from src.account_mode import (
    MARKET_BUY_BUFFER,
    AccountMode,
    check_no_borrow,
    fetch_account_mode,
    is_spot,
    parse_account_mode,
    spot_legs,
    spot_order_params,
)
from src.connector import OKXExchange
from src.dca_bot import DCABot
from src.live_preflight import evaluate
from src.order_router import ACCOUNT_MODE_TTL_S, OrderRouter

PX = 50_000.0


class FakeOkx:
    """account/config, account/balance, create_order — без сети."""

    def __init__(self, config=None, avail=None, config_error=None):
        self.config = dict(config or {"acctLv": "1", "autoLoan": False})
        self.avail = dict(avail or {})
        self.config_error = config_error
        self.calls: list = []
        self.created: list[dict] = []
        self.headers: dict = {}

    def milliseconds(self):
        return 1_790_000_000_000

    def private_get_account_config(self, params=None):
        self.calls.append("config")
        if self.config_error is not None:
            raise self.config_error
        return {"code": "0", "data": [dict(self.config)]}

    def private_get_account_balance(self, params=None):
        ccy = (params or {}).get("ccy")
        self.calls.append(("balance", ccy))
        details = [{"ccy": ccy, "availBal": str(self.avail[ccy])}] if ccy in self.avail else []
        return {"code": "0", "data": [{"details": details}]}

    def create_order(self, symbol, ord_type, side, amount, price=None, params=None):
        self.created.append({"symbol": symbol, "type": ord_type, "side": side, "amount": amount,
                             "price": price, "params": dict(params or {})})
        return {"id": f"x{len(self.created)}", "status": "closed", "symbol": symbol}

    def fetch_order(self, order_id, symbol=None):
        return {"id": order_id, "status": "closed", "symbol": symbol}

    def fetch_ticker(self, symbol):
        return {"symbol": symbol, "last": PX}

    def fetch_balance(self):
        return {"total": {"USDT": 10_000.0}}


class ParseTest(unittest.TestCase):
    def test_td_mode_by_account_level(self):
        for lv, td in (("1", "cash"), ("2", "cash"), ("3", "cross"), ("4", "cross")):
            self.assertEqual(parse_account_mode({"acctLv": lv}).spot_td_mode, td, lv)

    def test_flags_and_borrow(self):
        mode = parse_account_mode({"acctLv": "3", "autoLoan": "true", "enableSpotBorrow": False})
        self.assertEqual((mode.auto_loan, mode.spot_borrow, mode.can_borrow), (True, False, True))
        self.assertIn("autoLoan", mode.borrow_reason)
        self.assertFalse(parse_account_mode({"acctLv": "3", "autoLoan": False}).can_borrow)
        # в режимах 1–2 autoLoan не действует; заём там — только enableSpotBorrow
        self.assertFalse(parse_account_mode({"acctLv": "1", "autoLoan": True}).can_borrow)
        self.assertTrue(parse_account_mode({"acctLv": "1", "enableSpotBorrow": "true"}).can_borrow)

    def test_unknown_level_is_an_error(self):
        for config in ({}, {"acctLv": ""}, {"acctLv": "7"}):
            with self.assertRaises(ValueError):
                parse_account_mode(config)

    def test_fetch(self):
        self.assertEqual(fetch_account_mode(FakeOkx({"acctLv": "3", "autoLoan": True})),
                         AccountMode("3", True, False))

        class Empty:
            def private_get_account_config(self):
                return {"code": "0", "data": []}
        with self.assertRaises(RuntimeError):
            fetch_account_mode(Empty())

    def test_spot_detection(self):
        self.assertEqual(spot_legs("BTC/USDT"), ("BTC", "USDT"))
        self.assertEqual(spot_legs("OKB-BTC"), ("OKB", "BTC"))
        for inst in ("BTC/USDT:USDT", "BTC-USDT-SWAP", "BTC-USD-260925", "BTC"):
            self.assertFalse(is_spot(inst), inst)

    def test_order_params(self):
        cash, cross = AccountMode("1"), AccountMode("3")
        self.assertEqual(spot_order_params(cash, "market", "buy", 0.0002, PX), {"tdMode": "cash"})
        self.assertEqual(spot_order_params(cross, "limit", "buy", 0.0002, PX), {"tdMode": "cross"})
        self.assertEqual(spot_order_params(cross, "market", "sell", 0.0002, None), {"tdMode": "cross"})
        params = spot_order_params(cross, "market", "buy", 0.0002, PX)
        self.assertEqual((params["tdMode"], params["tgtCcy"]), ("cross", "quote_ccy"))
        self.assertAlmostEqual(params["cost"], 10.0)
        with self.assertRaises(ValueError):
            spot_order_params(cross, "market", "buy", 0.0002, None)


class NoBorrowTest(unittest.TestCase):
    LOAN = AccountMode("3", auto_loan=True)

    def test_without_borrow_no_network(self):
        ex = FakeOkx()
        self.assertEqual(check_no_borrow(ex, AccountMode("3"), "BTC/USDT", "buy", "limit", 1.0, PX), (True, "ok"))
        self.assertEqual(ex.calls, [])

    def test_buy_against_quote_availbal(self):
        ex = FakeOkx(avail={"USDT": 10.0})
        self.assertTrue(check_no_borrow(ex, self.LOAN, "BTC/USDT", "buy", "limit", 0.0002, PX)[0])
        ok, reason = check_no_borrow(ex, self.LOAN, "BTC/USDT", "buy", "limit", 0.0003, PX)
        self.assertFalse(ok)
        self.assertIn("заём", reason)
        self.assertIn("USDT", reason)
        self.assertEqual(ex.calls, [("balance", "USDT"), ("balance", "USDT")])

    def test_market_buy_keeps_a_buffer(self):
        ex = FakeOkx(avail={"USDT": 10.0 * (1 + MARKET_BUY_BUFFER / 2)})
        self.assertTrue(check_no_borrow(ex, self.LOAN, "BTC/USDT", "buy", "limit", 0.0002, PX)[0])
        self.assertFalse(check_no_borrow(ex, self.LOAN, "BTC/USDT", "buy", "market", 0.0002, PX)[0])
        self.assertFalse(check_no_borrow(ex, self.LOAN, "BTC/USDT", "buy", "market", 0.0002, None)[0])

    def test_sell_against_base_and_missing_currency(self):
        ex = FakeOkx(avail={"BTC": 0.0001})
        self.assertFalse(check_no_borrow(ex, self.LOAN, "BTC-USDT", "sell", "limit", 0.0002, PX)[0])
        self.assertTrue(check_no_borrow(ex, self.LOAN, "BTC-USDT", "sell", "limit", 0.0001, PX)[0])
        # валюты нет в ответе — availBal 0: любая покупка — заём
        self.assertFalse(check_no_borrow(ex, self.LOAN, "ETH/USDT", "buy", "limit", 0.01, 1_000.0)[0])


class CcxtRequestTest(unittest.TestCase):
    """Какой запрос соберёт CCXT 4.5 из параметров spot_order_params (без сети)."""

    def setUp(self):
        self.ex = OKXExchange({})
        self.ex.set_markets([{
            "id": "BTC-USDT", "symbol": "BTC/USDT", "base": "BTC", "quote": "USDT", "baseId": "BTC",
            "quoteId": "USDT", "settle": None, "type": "spot", "spot": True, "margin": True, "swap": False,
            "future": False, "option": False, "contract": False, "linear": None, "inverse": None,
            "active": True, "precision": {"amount": 1e-08, "price": 0.1},
            "limits": {"amount": {"min": 1e-05}, "price": {}, "cost": {}}, "info": {}}])

    def request(self, acct_lv: str, ord_type: str, side: str) -> dict:
        params = spot_order_params(AccountMode(acct_lv), ord_type, side, 0.0002, PX)
        return self.ex.create_order_request("BTC/USDT", ord_type, side, 0.0002, PX,
                                            {**params, "clOrdId": "botrtest"})

    def test_cash_market_buy_in_base(self):
        req = self.request("1", "market", "buy")
        self.assertEqual((req["tdMode"], req["tgtCcy"], req["sz"]), ("cash", "base_ccy", "0.0002"))

    def test_cross_market_buy_sends_quote_amount(self):
        req = self.request("3", "market", "buy")
        self.assertEqual((req["tdMode"], req["tgtCcy"], req["sz"]), ("cross", "quote_ccy", "10"))
        self.assertNotIn("cost", req)

    def test_cross_limit_and_sell_in_base(self):
        buy = self.request("3", "limit", "buy")
        self.assertEqual((buy["tdMode"], buy["sz"], buy["px"]), ("cross", "0.0002", "50000"))
        self.assertNotIn("tgtCcy", buy)
        sell = self.request("3", "market", "sell")
        self.assertEqual((sell["tdMode"], sell["sz"]), ("cross", "0.0002"))

    def test_ccxt_pitfall_without_explicit_cost(self):
        """Без cost CCXT (createMarketBuyOrderRequiresPrice=False) шлёт количество базы как сумму."""
        self.assertFalse(self.ex.options.get("createMarketBuyOrderRequiresPrice"))
        req = self.ex.create_order_request("BTC/USDT", "market", "buy", 0.0002, PX,
                                           {"tdMode": "cross", "tgtCcy": "quote_ccy"})
        self.assertEqual(req["sz"], "0")


class RouterTest(unittest.TestCase):
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

    def test_spot_account_uses_cash(self):
        ex = FakeOkx({"acctLv": "1"})
        result = self.router(ex).place_order("BTC/USDT", "buy", "limit", px=PX, sz=0.0002)
        self.assertTrue(result["ok"], result)
        params = ex.created[0]["params"]
        self.assertEqual(params["tdMode"], "cash")
        self.assertNotIn("tgtCcy", params)
        self.assertEqual(ex.calls, ["config"])

    def test_margin_account_market_buy(self):
        ex = FakeOkx({"acctLv": "3", "autoLoan": False})
        result = self.router(ex).place_order("BTC/USDT", "buy", "market", px=PX, sz=0.0002)
        self.assertTrue(result["ok"], result)
        params = ex.created[0]["params"]
        self.assertEqual((params["tdMode"], params["tgtCcy"]), ("cross", "quote_ccy"))
        self.assertAlmostEqual(params["cost"], 10.0)
        self.assertTrue(params["clOrdId"].startswith("botr"))
        self.assertEqual(ex.calls, ["config"])  # займа нет — баланс не нужен

    def test_auto_loan_blocks_order_beyond_availbal(self):
        ex = FakeOkx({"acctLv": "3", "autoLoan": True}, avail={"USDT": 5.0})
        result = self.router(ex).place_order("BTC/USDT", "buy", "limit", px=PX, sz=0.0002)
        self.assertFalse(result["ok"])
        self.assertEqual(result["stage"], "account_mode")
        self.assertIn("заём", result["reason"])
        self.assertEqual(ex.created, [])
        self.assertEqual(risk.status()["entries_today"], 0)  # вход не зарегистрирован

    def test_auto_loan_allows_order_within_availbal(self):
        ex = FakeOkx({"acctLv": "3", "autoLoan": True}, avail={"USDT": 100.0})
        result = self.router(ex).place_order("BTC/USDT", "buy", "limit", px=PX, sz=0.0002)
        self.assertTrue(result["ok"], result)
        self.assertEqual(ex.calls, ["config", ("balance", "USDT")])

    def test_unreadable_mode_blocks_order(self):
        ex = FakeOkx(config_error=ccxt.RequestTimeout("timeout"))
        result = self.router(ex).place_order("BTC/USDT", "buy", "limit", px=PX, sz=0.0002)
        self.assertEqual((result["ok"], result["stage"]), (False, "account_mode"))
        self.assertEqual(ex.created, [])

    def test_mode_is_cached_and_reread_after_ttl(self):
        ex = FakeOkx({"acctLv": "1"})
        router = self.router(ex)

        def buy() -> dict:
            result = router.place_order("BTC/USDT", "buy", "limit", px=PX, sz=0.0002)
            risk.register_spot_buy("BTC/USDT")  # как DCA: слот позиции свободен для следующей покупки
            return result

        self.assertTrue(buy()["ok"])
        self.assertTrue(buy()["ok"])
        self.assertEqual(ex.calls.count("config"), 1)
        ex.config = {"acctLv": "3", "autoLoan": False}  # человек сменил режим
        router._account_mode_at -= ACCOUNT_MODE_TTL_S + 1
        with self.assertLogs("okx.order_router", "WARNING") as logs:
            self.assertTrue(buy()["ok"])
        self.assertIn("режим аккаунта сменился", " ".join(logs.output))
        self.assertEqual(ex.calls.count("config"), 2)
        self.assertEqual([c["params"]["tdMode"] for c in ex.created], ["cash", "cash", "cross"])

    def test_explicit_mode_is_fixed(self):
        ex = FakeOkx({"acctLv": "1"})
        router = self.router(ex, account_mode=AccountMode("3"))
        router._account_mode_at -= ACCOUNT_MODE_TTL_S + 1
        router.place_order("BTC/USDT", "buy", "limit", px=PX, sz=0.0002)
        self.assertEqual(ex.calls, [])
        self.assertEqual(ex.created[0]["params"]["tdMode"], "cross")

    def test_non_spot_is_untouched(self):
        ex = FakeOkx({"acctLv": "3"})
        result = self.router(ex).place_order("BTC-USDT-SWAP", "buy", "limit", px=PX, sz=1.0)
        self.assertTrue(result["ok"], result)
        self.assertNotIn("tdMode", ex.created[0]["params"])
        self.assertEqual(ex.calls, [])

    def test_dca_market_buy_in_margin_account_spends_quote(self):
        ex = FakeOkx({"acctLv": "3", "autoLoan": False})
        router = self.router(ex)
        bot = DCABot(ex, router, quote_per_buy_usdt=5.0, max_buys=1, interval_sec=0, demo=True,
                     storage=router.storage, sleep=lambda s: None)
        self.assertEqual(bot.run(), DCABot.STATE_DONE)
        params = ex.created[0]["params"]
        self.assertEqual((params["tdMode"], params["tgtCcy"]), ("cross", "quote_ccy"))
        self.assertAlmostEqual(params["cost"], 5.0)


class PreflightTest(unittest.TestCase):
    def spot_borrow(self, mode: str, config: dict) -> str:
        checks = evaluate(mode, account_config=dict({"perm": "read_only,trade", "uid": "1", "mainUid": "2",
                                                     "ip": "203.0.113.7", "posMode": "net_mode"}, **config),
                          equity_usdt=None, equity_source="", drift_ms=0, drift_error=None,
                          pocket=None, pocket_error=None, policy=None, foreign_orders=0)
        return next(c.status for c in checks if c.name == "spot_borrow")

    def test_auto_loan_fails_live(self):
        loan = {"acctLv": "3", "autoLoan": "true"}
        self.assertEqual(self.spot_borrow("live", loan), "fail")
        self.assertEqual(self.spot_borrow("demo", loan), "warn")
        self.assertEqual(self.spot_borrow("live", {"acctLv": "1", "enableSpotBorrow": True}), "fail")

    def test_no_borrow_is_ok(self):
        self.assertEqual(self.spot_borrow("live", {"acctLv": "1"}), "ok")
        self.assertEqual(self.spot_borrow("live", {"acctLv": "3", "autoLoan": "false"}), "ok")

    def test_unknown_mode(self):
        self.assertEqual(self.spot_borrow("live", {"acctLv": ""}), "fail")
        self.assertEqual(self.spot_borrow("demo", {"acctLv": ""}), "info")


if __name__ == "__main__":
    unittest.main()
