"""Режим аккаунта и спот-ордера (SPOT-TDMODE): src/account_mode.py, OrderRouter, live-preflight.
CLI проверки и смены режима (ACCT-SWITCH-CLI): status, precheck, switch — классы в конце файла.

Сеть не используется. Запрос CCXT проверяется настоящим ccxt.okx на рынке,
заданном вручную (create_order_request без отправки). Ответы account/config и
account/balance — как у demo OKX 24.09 (insights/okx-api.md §10 п. 22).
Ответы precheck — по документации OKX (на demo не проверены, okx-api.md §10 п. 24).
"""
import contextlib
import io
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import ccxt

from src import account_mode, risk
from src.account_mode import (
    MARKET_BUY_BUFFER,
    AccountMode,
    check_no_borrow,
    describe_error,
    fetch_account_mode,
    is_spot,
    parse_account_mode,
    parse_switch_precheck,
    precheck_switch,
    spot_legs,
    spot_order_params,
    switch_account_level,
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


# --- ACCT-SWITCH-CLI: status, precheck, switch ---

# data[0] precheck — формат из документации OKX (preCheckAccountLevel.md), переход 3 → 2
CLEAN_PRECHECK = {
    "acctLv": "2", "curAcctLv": "3", "posList": [], "posTierCheck": [], "riskOffsetType": "",
    "sCode": "0", "unmatchedInfoCheck": [],
    "mgnBf": {"acctAvailEq": "103773.7", "details": [], "mgnRatio": ""},
    "mgnAft": {"acctAvailEq": "", "details": [{"ccy": "USDT", "availEq": "6132.1", "mgnRatio": ""}], "mgnRatio": ""},
}
BLOCKED_PRECHECK = {
    "acctLv": "2", "curAcctLv": "3", "mgnAft": None, "mgnBf": None, "posList": [], "posTierCheck": [],
    "riskOffsetType": "", "sCode": "1",
    "unmatchedInfoCheck": [
        {"posList": [], "totalAsset": "", "type": "pending_algos"},
        {"posList": ["2005456500916518912"], "totalAsset": "", "type": "cross_margin"},
    ],
}
# Так CCXT 4.5 оформляет отказ OKX (okx.handle_errors): «okx » + тело ответа
ERR_51070 = ('okx {"code":"51070","data":[],"msg":"You do not meet the requirements for switching to '
             'this account mode. Please upgrade the account mode on the OKX website or App"}')
ERR_59132 = ('okx {"code":"59132","data":[],"msg":"Unable to switch. Please close or cancel all open orders '
             'and refer to the pre-check endpoint to stop any incompatible bots."}')


class SwitchFakeOkx:
    """account/config, precheck и set-account-level без сети; demo-заголовок — как у CCXT sandbox."""

    def __init__(self, acct_lv="3", precheck=None, applies=True, lag_reads=0, errors=None):
        self.config = {"acctLv": acct_lv, "autoLoan": True, "enableSpotBorrow": False, "posMode": "net_mode"}
        self.precheck_response = {"code": "0", "data": [dict(CLEAN_PRECHECK if precheck is None else precheck)]}
        self.applies = applies        # False: POST принят, а режим не сменился
        self.lag_reads = lag_reads    # столько чтений account/config после POST ещё видят старый режим
        self.pending = None
        self.errors = dict(errors or {})  # шаг (config | precheck | set_level) -> исключение
        self.calls: list = []
        self.headers = {"x-simulated-trading": "1"}

    def _step(self, name, call):
        self.calls.append(call)
        if name in self.errors:
            raise self.errors[name]

    def private_get_account_config(self, params=None):
        self._step("config", "config")
        if self.pending is not None:
            if self.lag_reads:
                self.lag_reads -= 1
            else:
                self.config["acctLv"], self.pending = self.pending, None
        return {"code": "0", "data": [dict(self.config)]}

    def private_get_account_set_account_switch_precheck(self, params=None):
        self._step("precheck", ("precheck", (params or {}).get("acctLv")))
        return self.precheck_response

    def private_post_account_set_account_level(self, params=None):
        level = (params or {}).get("acctLv")
        self._step("set_level", ("set_level", level))
        if self.applies:
            self.pending = level
        return {"code": "0", "data": [{"acctLv": level}]}


def run_cli(argv, exchange=None, env_mode="demo"):
    """account_mode.main без сети: (код выхода, stdout, stderr, фабрика биржи)."""
    factory = mock.Mock(return_value=exchange)
    out, err = io.StringIO(), io.StringIO()
    with mock.patch.object(account_mode, "_exchange", factory), \
            mock.patch.object(account_mode, "CONFIRM_DELAY_S", 0), \
            mock.patch.dict(os.environ, {"OKX_MODE": env_mode}), \
            mock.patch("logging.basicConfig"), \
            contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        # basicConfig отключён: иначе корневой логгер остался бы привязан к этому StringIO
        try:
            code = account_mode.main(argv)
        except SystemExit as exc:  # argparse: неверный аргумент — выход 2 до сети
            code = exc.code
    return code, out.getvalue(), err.getvalue(), factory


class SwitchPrecheckParseTest(unittest.TestCase):
    """Разбор ответа precheck: поля по документации OKX, всё непонятное — блокер."""

    def test_clean(self):
        check = parse_switch_precheck(CLEAN_PRECHECK, "2")
        self.assertTrue(check.ok)
        self.assertEqual((check.s_code, check.cur_acct_lv, check.blockers), ("0", "3", ()))
        self.assertIn("acctAvailEq 103773.7", check.margin_before)
        self.assertIn("USDT: availEq 6132.1", check.margin_after)

    def test_blockers_have_type_and_meaning(self):
        check = parse_switch_precheck(BLOCKED_PRECHECK, "2")
        self.assertFalse(check.ok)
        self.assertEqual(len(check.blockers), 2)
        self.assertIn("pending_algos: активные algo-ордера и торговые боты", check.blockers[0])
        self.assertIn("cross_margin", check.blockers[1])
        self.assertIn("позиции 2005456500916518912", check.blockers[1])

    def test_pos_list_as_strings_or_objects(self):
        # документация OKX: строки posId; модель OKX.Net: объекты {posId, lever}
        for pos_list in (["111"], [{"posId": "111", "lever": "5"}]):
            item = {"sCode": "1", "acctLv": "2", "unmatchedInfoCheck": [{"type": "all_positions", "posList": pos_list}]}
            self.assertIn("позиции 111", parse_switch_precheck(item, "2").blockers[0], pos_list)

    def test_other_codes_and_inconsistent_answers_block(self):
        tier = {"instType": "SWAP", "instFamily": "BTC-USDT", "pos": "10", "lever": "20", "maxSz": "5"}
        cases = {
            "неизвестный sCode": {"sCode": "7"},
            "нет sCode": {"unmatchedInfoCheck": []},
            "sCode 1 без перечня": {"sCode": "1"},
            "sCode 3 — нет preset плеча": {"sCode": "3", "posList": [{"posId": "9", "lever": "10"}]},
            "sCode 4 — тиры": {"sCode": "4", "posTierCheck": [tier]},
            "sCode 0, но есть блокер": dict(CLEAN_PRECHECK, unmatchedInfoCheck=[{"type": "pending_orders"}]),
            "ответ не для запрошенного режима": dict(CLEAN_PRECHECK, acctLv="3"),
        }
        for name, item in cases.items():
            check = parse_switch_precheck(item, "2")
            self.assertFalse(check.ok, name)
            self.assertTrue(check.blockers, name)
        self.assertIn("допустимо 5", parse_switch_precheck(cases["sCode 4 — тиры"], "2").blockers[0])
        self.assertEqual(parse_switch_precheck(cases["sCode 3 — нет preset плеча"], "2").positions, ("9 ×10",))

    def test_nulls_and_empty_strings_are_tolerated(self):
        item = {"sCode": "0", "acctLv": "2", "curAcctLv": "3", "mgnBf": "", "mgnAft": None,
                "posList": None, "posTierCheck": "", "unmatchedInfoCheck": None}
        check = parse_switch_precheck(item, "2")
        self.assertTrue(check.ok)
        self.assertEqual((check.margin_before, check.margin_after, check.positions), ("", "", ()))


class StatusCliTest(unittest.TestCase):
    def test_status_shows_mode_flags_and_borrow(self):
        ex = SwitchFakeOkx(acct_lv="3")
        code, out, _, factory = run_cli(["status"], ex)
        self.assertEqual(code, 0)
        for text in ("acctLv 3 (Multi-currency margin)", "autoLoan: true", "enableSpotBorrow: false",
                     "posMode: net_mode", "tdMode спота: cross", "заём спот-ордером: возможен"):
            self.assertIn(text, out)
        self.assertEqual(ex.calls, ["config"])
        factory.assert_called_once_with("demo")

    def test_status_live_is_read_only(self):
        ex = SwitchFakeOkx(acct_lv="2")
        code, out, _, factory = run_cli(["status", "--mode", "live"], ex)
        self.assertEqual(code, 0)
        self.assertIn("[live]: acctLv 2 (Spot and futures)", out)
        self.assertIn("заём спот-ордером: невозможен", out)  # autoLoan в режимах 1–2 не действует
        factory.assert_called_once_with("live")


class PrecheckCliTest(unittest.TestCase):
    def test_clean_exit_0(self):
        ex = SwitchFakeOkx()
        code, out, _, factory = run_cli(["precheck", "--acct-lv", "2"], ex)
        self.assertEqual(code, 0)
        self.assertIn("Итог: можно переключать", out)
        self.assertIn("ответ OKX:", out)  # сырой ответ: поля на demo ещё не проверены
        self.assertEqual(ex.calls, [("precheck", "2")])  # только GET precheck
        factory.assert_called_once_with("demo")

    def test_blockers_exit_1_with_list(self):
        ex = SwitchFakeOkx(precheck=BLOCKED_PRECHECK)
        code, out, _, _ = run_cli(["precheck", "--acct-lv", "2"], ex)
        self.assertEqual(code, 1)
        for text in ("блокеры (2)", "pending_algos", "cross_margin", "Итог: переключать нельзя, блокеров 2"):
            self.assertIn(text, out)
        self.assertEqual(ex.calls, [("precheck", "2")])

    def test_code_2_without_exception_exit_2(self):
        # CCXT не бросает исключение на code 2 («частичный успех») — его ловит _response_data
        ex = SwitchFakeOkx()
        ex.precheck_response = {"code": "2", "data": [], "msg": "partial"}
        code, _, err, _ = run_cli(["precheck", "--acct-lv", "2"], ex)
        self.assertEqual(code, 2)
        self.assertIn("OKX 2", err)


class SwitchCliTest(unittest.TestCase):
    def test_live_is_refused_without_network(self):
        code, _, err, factory = run_cli(["switch", "--acct-lv", "2", "--mode", "live"], SwitchFakeOkx())
        self.assertEqual(code, 2)
        self.assertIn("только человек", err)
        factory.assert_not_called()

    def test_live_settings_are_refused_without_network(self):
        # OKX_MODE=live в окружении, --mode не задан
        code, _, err, factory = run_cli(["switch", "--acct-lv", "2"], SwitchFakeOkx(), env_mode="live")
        self.assertEqual(code, 2)
        self.assertIn("Отказ", err)
        factory.assert_not_called()

    def test_client_without_demo_header_is_refused(self):
        ex = SwitchFakeOkx()
        ex.headers = {}
        code, _, err, _ = run_cli(["switch", "--acct-lv", "2"], ex)
        self.assertEqual(code, 2)
        self.assertIn("x-simulated-trading", err)
        self.assertEqual(ex.calls, [])
        with self.assertRaises(PermissionError):
            switch_account_level(ex, "2")

    def test_blockers_stop_before_post(self):
        ex = SwitchFakeOkx(precheck=BLOCKED_PRECHECK)
        code, out, _, _ = run_cli(["switch", "--acct-lv", "2"], ex)
        self.assertEqual(code, 1)
        self.assertEqual(ex.calls, ["config", ("precheck", "2")])
        self.assertIn("pending_algos", out)
        self.assertIn("POST не отправлялся", out)
        self.assertEqual(ex.config["acctLv"], "3")

    def test_success_posts_and_rereads_config(self):
        ex = SwitchFakeOkx()
        code, out, _, _ = run_cli(["switch", "--acct-lv", "2"], ex)
        self.assertEqual(code, 0, out)
        self.assertEqual(ex.calls, ["config", ("precheck", "2"), ("set_level", "2"), "config"])
        self.assertEqual(ex.config["acctLv"], "2")
        self.assertIn("account/config: acctLv 2 (Spot and futures), tdMode спота cash, заём спот-ордером невозможен",
                      out)
        self.assertIn("Итог: режим переключён на 2 (Spot and futures) и подтверждён account/config", out)

    def test_config_lag_is_waited_out(self):
        ex, sleeps = SwitchFakeOkx(lag_reads=1), []
        result = switch_account_level(ex, "2", attempts=3, delay_s=1.5, sleep=sleeps.append)
        self.assertEqual((result.status, result.exit_code, result.after.acct_lv), ("switched", 0, "2"))
        self.assertEqual(ex.calls.count("config"), 3)  # до POST + 2 после
        self.assertEqual(sleeps, [1.5])

    def test_unconfirmed_exit_1(self):
        ex = SwitchFakeOkx(applies=False)
        code, out, _, _ = run_cli(["switch", "--acct-lv", "2"], ex)
        self.assertEqual(code, 1)
        self.assertEqual(ex.calls, ["config", ("precheck", "2"), ("set_level", "2"), "config", "config", "config"])
        self.assertIn("переключение не подтверждено", out)
        sleeps = []
        result = switch_account_level(SwitchFakeOkx(applies=False), "2", attempts=3, delay_s=1.5, sleep=sleeps.append)
        self.assertEqual((result.status, result.exit_code, result.after.acct_lv), ("unconfirmed", 1, "3"))
        self.assertEqual(sleeps, [1.5, 1.5])

    def test_same_mode_skips_precheck_and_post(self):
        ex = SwitchFakeOkx(acct_lv="2")
        code, out, _, _ = run_cli(["switch", "--acct-lv", "2"], ex)
        self.assertEqual(code, 0)
        self.assertEqual(ex.calls, ["config"])
        self.assertIn("режим уже 2 (Spot and futures)", out)

    def test_invalid_level_fails_before_network(self):
        for bad in ("5", "0", "two", ""):
            code, _, err, factory = run_cli(["switch", "--acct-lv", bad], SwitchFakeOkx())
            self.assertEqual(code, 2, bad)
            self.assertIn("от 1 до 4", err)
            factory.assert_not_called()
        ex = SwitchFakeOkx()
        for bad in ("5", 0, None):
            with self.assertRaises(ValueError):
                switch_account_level(ex, bad)
            with self.assertRaises(ValueError):
                precheck_switch(ex, bad)
        self.assertEqual(ex.calls, [])

    def test_okx_error_on_precheck_exit_2(self):
        # 51070: первое включение режима — только в Web/App
        ex = SwitchFakeOkx(errors={"precheck": ccxt.ExchangeError(ERR_51070)})
        code, _, err, _ = run_cli(["switch", "--acct-lv", "2"], ex)
        self.assertEqual(code, 2)
        self.assertIn("OKX 51070", err)
        self.assertIn("Web/App", err)
        self.assertNotIn(("set_level", "2"), ex.calls)

    def test_okx_error_on_post_exit_2(self):
        ex = SwitchFakeOkx(errors={"set_level": ccxt.ExchangeError(ERR_59132)})
        code, _, err, _ = run_cli(["switch", "--acct-lv", "2"], ex)
        self.assertEqual(code, 2)
        self.assertIn("OKX 59132", err)
        self.assertIn("python -m src.account_mode status", err)  # после сбоя — проверить режим
        self.assertEqual(ex.config["acctLv"], "3")

    def test_network_error_exit_2(self):
        ex = SwitchFakeOkx(errors={"set_level": ccxt.RequestTimeout("okx POST https://www.okx.com/api/v5/account/set-account-level")})
        code, _, err, _ = run_cli(["switch", "--acct-lv", "2"], ex)
        self.assertEqual(code, 2)
        self.assertIn("RequestTimeout", err)
        self.assertIn("python -m src.account_mode status", err)


class CcxtSwitchRequestTest(unittest.TestCase):
    """Настоящий ccxt.okx собирает и подписывает запросы как в бою; HTTP подменён — сети нет."""

    class Offline(OKXExchange):
        def __init__(self, responses):
            super().__init__({"apiKey": "k", "secret": "s", "password": "p", "enableRateLimit": False})
            self.set_sandbox_mode(True)
            self.responses, self.sent = responses, []

        def fetch(self, url, method="GET", headers=None, body=None):
            request_headers = self.prepare_request_headers(headers)
            path = url.split("/api/v5/", 1)[1]
            self.sent.append((method, path, request_headers.get("x-simulated-trading"), body))
            response = self.responses[path.split("?", 1)[0]]
            self.handle_errors(200, "OK", url, method, {}, json.dumps(response), response, request_headers, body)
            return response

    def test_precheck_is_get_with_query_and_demo_header(self):
        ex = self.Offline({"account/set-account-switch-precheck": {"code": "0", "data": [CLEAN_PRECHECK]}})
        self.assertTrue(precheck_switch(ex, 2).ok)
        self.assertEqual(ex.sent, [("GET", "account/set-account-switch-precheck?acctLv=2", "1", None)])

    def test_set_level_is_post_with_json_body(self):
        ex = self.Offline({"account/set-account-level": {"code": "0", "data": [{"acctLv": "2"}]}})
        self.assertEqual(account_mode._post_account_level(ex, "2"), "2")
        method, path, demo, body = ex.sent[0]
        self.assertEqual((method, path, demo, json.loads(body)), ("POST", "account/set-account-level", "1",
                                                                  {"acctLv": "2"}))

    def test_okx_error_reaches_explain_error(self):
        ex = self.Offline({"account/set-account-switch-precheck": json.loads(ERR_51070[len("okx "):])})
        with self.assertRaises(ccxt.ExchangeError) as ctx:
            precheck_switch(ex, "2")
        text = describe_error(ctx.exception)
        self.assertIn("OKX 51070", text)
        self.assertIn("Web/App", text)


if __name__ == "__main__":
    unittest.main()
