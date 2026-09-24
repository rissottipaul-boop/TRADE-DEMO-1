"""Реестр владельцев ордеров (ORDER-OWNER-TAG): коды, clOrdId, классификация и пути выставления.

Без сети: биржа — фейк, риск-ядро и хранилище — во временном каталоге.
"""
import json
import re
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import ccxt

import tests.test_live_runner as tlr
from src import config, live_runner, order_owner, risk
from src.connector import new_client_order_id
from src.dca_bot import DCABot
from src.order_router import OrderRouter
from src.reconciler import OWN_CLORD_PREFIX, is_own_order

SRC = Path(__file__).resolve().parents[1] / "src"
BEFORE = str(order_owner.RULE_SINCE_MS - 60_000)
AFTER = str(order_owner.RULE_SINCE_MS + 60_000)
Owner = order_owner.Owner


class RegistryTest(unittest.TestCase):
    def test_registry_is_valid(self):
        self.assertEqual(order_owner.validate_registry(), [])

    def test_validator_catches_hex_continuation_and_duplicates(self):
        bad = order_owner.OWNERS + (Owner("bota", "x", "x", "strategy"), Owner("lt", "x", "x", "test"))
        problems = order_owner.validate_registry(bad)
        self.assertTrue(any("'bota'" in p and "hex" in p for p in problems), problems)
        self.assertTrue(any("'lt'" in p and "дважды" in p for p in problems), problems)

    def test_validator_kind_rules(self):
        bad = (Owner("strx", "x", "x", "strategy"),   # код проекта не под корнем bot
               Owner("botx", "x", "x", "agent"),      # агент под корнем bot
               Owner("botlx", "x", "x", "strategy"),  # botl* — только live
               Owner("B!", "x", "x", "test"))         # недопустимые символы
        self.assertEqual(len(order_owner.validate_registry(bad)), 4)

    def test_root_matches_reconciler(self):
        # реконсилятор и live-preflight считают своими ровно корень bot*
        self.assertEqual(order_owner.OWN_ROOT, OWN_CLORD_PREFIX)
        for o in order_owner.OWNERS:
            self.assertEqual(is_own_order(order_owner.new_cl_ord_id(o.code)), o.own, o.code)

    def test_new_cl_ord_id_format_and_roundtrip(self):
        for o in order_owner.OWNERS:
            for _ in range(200):
                cl = order_owner.new_cl_ord_id(o.code)
                self.assertRegex(cl, r"^[a-z0-9]{32}$")
                self.assertIs(order_owner.owner_of(cl), o)

    def test_engine_default_prefix_is_engine(self):
        # engine.py (заморожен до конца P1-72H) ставит connector.new_client_order_id()
        for _ in range(500):
            self.assertEqual(order_owner.owner_of(new_client_order_id()).code, order_owner.ENGINE)

    def test_unregistered_sub_owner_is_not_engine(self):
        self.assertIsNone(order_owner.owner_of("botsgrid0123456789abcdef"))
        self.assertIsNone(order_owner.owner_of("botx1"))
        self.assertTrue(is_own_order("botsgrid0123"), "для реконсилятора корень bot* по-прежнему свой")

    def test_owner_of_edges(self):
        self.assertIsNone(order_owner.owner_of(""))
        self.assertIsNone(order_owner.owner_of(None))
        self.assertIsNone(order_owner.owner_of("xyz123"))
        self.assertEqual(order_owner.owner_of("trd20260924a1").code, order_owner.OKX_TRADER)

    def test_is_owned_by_is_exact_owner(self):
        self.assertTrue(order_owner.is_owned_by(order_owner.new_cl_ord_id("rt"), "rt"))
        self.assertFalse(order_owner.is_owned_by(order_owner.new_cl_ord_id("botsdca"), "bot"))
        self.assertFalse(order_owner.is_owned_by("", "rt"))

    def test_require(self):
        self.assertEqual(order_owner.require("botr", own=True).code, "botr")
        with self.assertRaises(ValueError):
            order_owner.require("zzz")
        with self.assertRaises(ValueError):
            order_owner.require("trd", own=True)
        with self.assertRaises(ValueError):
            order_owner.require("bot", own=False)
        with self.assertRaises(KeyError):
            order_owner.new_cl_ord_id("zzz")

    def test_ccxt_broker_id_matches_installed_ccxt(self):
        # сменится brokerId в новой версии CCXT — маркер «CCXT без clOrdId» надо обновить
        self.assertEqual(ccxt.okx().options["brokerId"], order_owner.CCXT_BROKER_ID)

    def test_cli_new_prints_fresh_id(self):
        with mock.patch("builtins.print") as printed:
            self.assertEqual(order_owner.main(["new", "trd"]), 0)
        self.assertEqual(order_owner.owner_of(printed.call_args.args[0]).code, "trd")
        with mock.patch("builtins.print"):
            self.assertEqual(order_owner.main(["new", "zzz"]), 2)


class ClassifyTest(unittest.TestCase):
    @staticmethod
    def c(human_trades=True, **order):
        order.setdefault("cTime", AFTER)
        return order_owner.classify(order, human_trades=human_trades)

    def test_owned_by_cl_ord_id(self):
        att = self.c(clOrdId=order_owner.new_cl_ord_id("trd"), tag="CLI")
        self.assertEqual((att.status, att.level, att.owner.code, att.marker), ("owned", "ok", "trd", "clOrdId"))

    def test_owned_by_algo_cl_ord_id(self):
        # ордер, порождённый стопом агента: clOrdId пуст, algoClOrdId с меткой
        att = self.c(clOrdId="", algoClOrdId=order_owner.new_cl_ord_id("pmp"), tag="CLI")
        self.assertEqual((att.status, att.owner.code, att.marker), ("owned", "pmp", "algoClOrdId"))

    def test_unmarked_is_info_on_demo(self):
        att = self.c(clOrdId="", tag="")
        self.assertEqual((att.status, att.level, att.legacy), ("unmarked", "info", False))
        self.assertIn("ручной ордер человека", att.reason)
        self.assertEqual(self.c(clOrdId="", tag="", cTime=BEFORE).level, "info")

    def test_unmarked_is_warning_in_live(self):
        att = self.c(human_trades=False, clOrdId="", tag="")
        self.assertEqual((att.status, att.level), ("unmarked", "warning"))

    def test_cli_and_mcp_without_prefix_are_warnings(self):
        self.assertEqual((self.c(tag="CLI").status, self.c(tag="CLI").level), ("cli", "warning"))
        self.assertEqual((self.c(tag="MCP").status, self.c(tag="MCP").level), ("mcp", "warning"))

    def test_ccxt_default_is_warning(self):
        broker = order_owner.CCXT_BROKER_ID
        att = self.c(clOrdId=broker + "0123456789abcdef", tag=broker)
        self.assertEqual((att.status, att.level, att.marker), ("ccxt_default", "warning", "clOrdId"))
        self.assertEqual(self.c(tag=broker).status, "ccxt_default")

    def test_unknown_prefix_and_tag_are_warnings(self):
        att = self.c(clOrdId="slip1")
        self.assertEqual((att.status, att.level), ("unknown_prefix", "warning"))
        self.assertIn("подвладелец", self.c(clOrdId="botsgrid01").reason)
        self.assertEqual(self.c(tag="xyz").status, "unknown_tag")

    def test_tag_equal_to_owner_code(self):
        att = self.c(tag="pmp")  # например, --aiBuilderCode pmp
        self.assertEqual((att.status, att.owner.code, att.marker), ("owned", "pmp", "tag"))

    def test_system_category_is_warning(self):
        att = self.c(clOrdId="", tag="", category="full_liquidation")
        self.assertEqual((att.status, att.level), ("system", "warning"))
        self.assertEqual(self.c(clOrdId="", tag="", category="twap").status, "unmarked")

    def test_legacy_before_rule_is_info(self):
        for order in ({"tag": "CLI"}, {"clOrdId": order_owner.CCXT_BROKER_ID + "ab"}, {"clOrdId": "slip1"}):
            att = self.c(cTime=BEFORE, **order)
            self.assertEqual((att.level, att.legacy), ("info", True), order)
            self.assertIn("до правила", att.reason)

    def test_missing_ctime_is_not_legacy(self):
        att = order_owner.classify({"tag": "CLI"})
        self.assertEqual((att.level, att.legacy), ("warning", False))


class FakeExchange:
    """CCXT okx без сети: create_order / fetch_order / тикер / баланс."""

    def __init__(self):
        self.created: list[dict] = []
        self.headers: dict = {}

    def milliseconds(self):
        return 1_790_000_000_000

    def create_order(self, symbol, ord_type, side, amount, price=None, params=None):
        self.created.append({"symbol": symbol, "type": ord_type, "side": side,
                             "amount": amount, "params": dict(params or {})})
        return {"id": f"x{len(self.created)}", "status": "closed", "symbol": symbol}

    def fetch_order(self, order_id, symbol=None):
        return {"id": order_id, "status": "closed", "symbol": symbol}

    def fetch_ticker(self, symbol):
        return {"symbol": symbol, "last": 50_000.0}

    def fetch_balance(self):
        return {"total": {"USDT": 10_000.0}}


class PlacementPathsTest(unittest.TestCase):
    """OrderRouter, DCABot и live_runner ставят префикс своего владельца."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.root = Path(self._tmp.name)
        risk.init(self.root / "risk.db")
        risk.update_equity(10_000)
        self.ex = FakeExchange()
        self._routers: list[OrderRouter] = []

    def tearDown(self):
        for router in self._routers:
            router.close()
        risk.init(self.root / "unused.db")  # отпустить файл до очистки
        self._tmp.cleanup()

    def router(self, **kwargs) -> OrderRouter:
        router = OrderRouter(self.ex, db_path=self.root / "bot.db", **kwargs)
        self._routers.append(router)
        return router

    def last_owner(self) -> str:
        return order_owner.owner_of(self.ex.created[-1]["params"]["clOrdId"]).code

    def test_router_default_owner_is_botr(self):
        result = self.router().place_order("BTC/USDT", "buy", "limit", px=30_000.0, sz=0.001)
        self.assertTrue(result["ok"], result)
        self.assertEqual((self.last_owner(), result["owner"]), (order_owner.ROUTER, order_owner.ROUTER))
        self.assertTrue(is_own_order(result["cl_ord_id"]), "ордер роутера — свой для реконсилятора")
        row = self._routers[0].storage.get_order(result["order_id"])
        self.assertEqual(row["cl_ord_id"], result["cl_ord_id"], "в storage тот же clOrdId с меткой")

    def test_router_owner_from_ctor_and_call(self):
        router = self.router(owner=order_owner.DCA)
        router.place_order("BTC/USDT", "buy", "limit", px=30_000.0, sz=0.001)
        self.assertEqual(self.last_owner(), order_owner.DCA)
        router.place_order("ETH/USDT", "buy", "limit", px=1_000.0, sz=0.01, owner=order_owner.ROUTER)
        self.assertEqual(self.last_owner(), order_owner.ROUTER)

    def test_router_rejects_foreign_owner_before_exchange_and_risk(self):
        router = self.router()
        for bad in ("trd", "lt", "zzz"):
            with self.assertRaises(ValueError):
                router.place_order("BTC/USDT", "buy", "limit", px=30_000.0, sz=0.001, owner=bad)
        with self.assertRaises(ValueError):
            self.router(owner="rt")
        self.assertEqual(self.ex.created, [])
        self.assertEqual(risk.status()["entries_today"], 0)

    def test_dca_bot_demo_marks_botsdca(self):
        router = self.router()
        bot = DCABot(self.ex, router, max_buys=1, interval_sec=0, demo=True,
                     storage=router.storage, sleep=lambda s: None)
        self.assertEqual(bot.run(), DCABot.STATE_DONE)
        self.assertEqual(self.last_owner(), order_owner.DCA)

    def test_dca_bot_owner_must_match_mode(self):
        router = self.router()
        gate = lambda: (True, "ok")  # noqa: E731
        with self.assertRaises(RuntimeError):
            DCABot(self.ex, router, demo=True, owner=order_owner.LIVE_DCA, storage=router.storage)
        with self.assertRaises(RuntimeError):
            DCABot(self.ex, router, demo=False, max_total_usdt=100, before_buy=gate,
                   owner=order_owner.DCA, storage=router.storage)
        with self.assertRaises(ValueError):
            DCABot(self.ex, router, demo=True, owner=order_owner.OKX_TRADER, storage=router.storage)
        live_bot = DCABot(self.ex, router, demo=False, max_total_usdt=100, before_buy=gate,
                          storage=router.storage)
        self.assertEqual(live_bot.owner, order_owner.LIVE_DCA)
        demo_run = DCABot(self.ex, router, demo=True, owner=order_owner.DCA_DEMO_RUN, storage=router.storage)
        self.assertEqual(demo_run.owner, order_owner.DCA_DEMO_RUN)


class LiveRunnerOwnerTest(unittest.TestCase):
    """Live-runner метит покупки botldca (фикстуры — tests/test_live_runner.py)."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.root = Path(self._tmp.name)
        patcher = mock.patch.object(config, "DATA_ROOT", self.root / "data")
        patcher.start()
        self.addCleanup(patcher.stop)
        self.pocket_path = self.root / "live-pocket.json"
        self.policy_path = self.root / "live-policy.json"
        self.pocket_path.write_text(json.dumps(tlr.POCKET), encoding="utf-8")
        self.policy_path.write_text(json.dumps(tlr.OPEN_POLICY), encoding="utf-8")
        self.ex = tlr.FakeExchange()
        self.clock = tlr.FakeClock()

    def tearDown(self):
        risk.init(self.root / "unused.db")
        self._tmp.cleanup()

    def test_live_buy_is_marked_botldca(self):
        code = live_runner.run(once=True, exchange=self.ex, pocket_path=self.pocket_path,
                               policy_path=self.policy_path, sleep=self.clock.sleep,
                               clock=self.clock, now=lambda: tlr.NOW)
        self.assertEqual(code, 0)
        owner = order_owner.owner_of(self.ex.created[0]["params"]["clOrdId"])
        self.assertEqual(owner.code, order_owner.LIVE_DCA)
        self.assertTrue(owner.live and owner.own)


# Прямые вызовы выставления у exchange-объекта CCXT (обычные, batch и algo)
PLACE_CALL = re.compile(
    r"\.(create_order|create_(?:limit|market)_(?:buy_|sell_)?order|"
    r"private_post_trade_(?:order_algo|order|batch_orders))\b")


class StaticPathsTest(unittest.TestCase):
    def test_every_direct_order_path_sets_owner_prefix(self):
        """Каждый модуль src/, который сам ставит ордера, берёт clOrdId из реестра.

        Исключение — engine.py (заморожен до конца P1-72H): его
        connector.new_client_order_id() по умолчанию и есть владелец «bot».
        """
        placing, offenders = [], []
        for path in sorted(SRC.glob("*.py")):
            text = path.read_text(encoding="utf-8")
            if not PLACE_CALL.search(text):
                continue
            placing.append(path.name)
            if path.name == "engine.py":
                ok = "new_client_order_id()" in text and "clOrdId" in text
            else:
                ok = "order_owner" in text and "clOrdId" in text
            if not ok:
                offenders.append(path.name)
        self.assertEqual(offenders, [], f"пути выставления без метки владельца: {offenders}")
        self.assertLessEqual({"engine.py", "load_test.py", "order_router.py", "ratelimit_test.py",
                              "smoke_test.py"}, set(placing), placing)


if __name__ == "__main__":
    unittest.main()
