"""Учёт PnL по рукавам (PNL-LEDGER, src/pnl_ledger.py) на фейковых ответах OKX, без сети.

Проверяются период и граница дня (+05:00), правила ops/sleeves.json,
атрибуция bills по рукавам (бот, префикс clOrdId, tag, funding, человек),
средняя цена и реализованный результат, тождество сверки, догрузка журнала
bills с архивом, чтение ботов и их ордеров, read-only к бирже и к базе
движка, отчёт md/json и текст для Telegram. Фейк отвечает как demo OKX:
данные — строками, страницы — от новых к старым, курсор after — id записи.
"""
import itertools
import json
import tempfile
import unittest
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import ccxt

from src import order_owner, pnl_ledger as pl
from src.storage import EquityRecord, Storage

ROOT = Path(__file__).resolve().parents[1]
RULES_PATH = ROOT / "ops" / "sleeves.json"
RULES = pl.load_rules(RULES_PATH)

TZ5 = timezone(timedelta(hours=5))
HOUR = 3_600_000
DAY = pl.DAY_MS
NOW_MS = int(datetime(2026, 9, 24, 11, 40, tzinfo=TZ5).timestamp() * 1000)
T0 = int(datetime(2026, 9, 24, tzinfo=TZ5).timestamp() * 1000)  # начало дня отчёта

GRID_ALGO = "3949248990629228544"   # BTC-grid из ops/sleeves.json -> native_grid
GRID_ORD = "3950000000000000001"    # его исполненный sub-order
BTC_T0, BTC_NOW = 50_500.0, 51_000.0

_ids = itertools.count(3_950_100_000_000_000_000)


def _n(value: float) -> str:
    return f"{value:.10f}".rstrip("0").rstrip(".")


def bill(ts: int, ccy: str, chg: float, type_: str = "2", **fields) -> dict:
    """Один bill account/bills: balChg уже с комиссией, fee < 0 — списание."""
    row = {"billId": str(next(_ids)), "ts": str(ts), "type": type_, "subType": "1", "ccy": ccy,
           "balChg": _n(chg), "sz": "0", "fee": "0", "instId": "", "instType": "", "ordId": "",
           "clOrdId": "", "tag": "", "tradeId": ""}
    row.update({k: (_n(v) if isinstance(v, float) else v) for k, v in fields.items()})
    return row


def fill(ts: int, side: str, qty: float, px: float, fee_base: float = 0.0, fee_quote: float = 0.0,
         inst: str = "BTC-USDT", cl: str = "", tag: str = "", ord_id: str = "") -> list[dict]:
    """Спот-исполнение — две ноги в bills (база и котируемая) с общими ordId и tradeId."""
    base, quote = inst.split("-")
    sign = 1.0 if side == "buy" else -1.0
    common = {"instId": inst, "instType": "SPOT", "ordId": ord_id or str(next(_ids)),
              "tradeId": str(next(_ids)), "clOrdId": cl, "tag": tag, "px": px}
    return [bill(ts, base, sign * qty - fee_base, sz=qty, fee=-fee_base, **common),
            bill(ts, quote, -sign * qty * px - fee_quote, sz=qty * px, fee=-fee_quote, **common)]


def scenario() -> dict:
    """День 24.09 (+05:00): капитал, DCA, нативный grid, ручной ордер, CLI, funding, незнакомый тип.

    Эталонные числа (цены BTC: 50 500 на начало дня, 51 000 сейчас):
    - dca: куплено до периода 0.1 BTC @50 000 (комиссия 0.0001 BTC), продано 0.05 @52 000
      (комиссия 2.6 USDT). V(t0) = 0.0999×50 500 − 5 000 = 44.95; V(t1) = 0.0499×51 000 −
      2 402.6 = 142.3 → PnL 97.35. Средняя 5 000/0.0999, реализовано (51 948 − 50 050.05)×0.05.
    - native_grid: куплено 0.01 @51 000, комиссия 0.00001 BTC → PnL −0.51, комиссии 0.51.
    - manual: продано 0.01 @51 500 без меток, комиссия 0.515 USDT → PnL 4.485.
    - agent_cli: tag CLI, куплено 0.002 @51 200, комиссия 0.000002 BTC → PnL −0.502.
    - funding_carry: funding −1.5 USDT. Незнакомый type 99: +0.1 USDT -> manual.
    - капитал: +10 000 USDT до периода, −1 000 USDT за период (вне рукавов).
    """
    cl_dca = order_owner.new_cl_ord_id(order_owner.DCA)
    bills = [bill(T0 - 2 * DAY, "USDT", 10_000.0, type_="1")]
    bills += fill(T0 - DAY, "buy", 0.1, 50_000.0, fee_base=0.0001, cl=cl_dca)
    bills += fill(T0 + 1 * HOUR, "sell", 0.05, 52_000.0, fee_quote=2.6, cl=cl_dca)
    bills += fill(T0 + 2 * HOUR, "buy", 0.01, 51_000.0, fee_base=0.00001, cl="O" + "7" * 19, ord_id=GRID_ORD)
    bills += fill(T0 + 3 * HOUR, "sell", 0.01, 51_500.0, fee_quote=0.515)
    bills.append(bill(T0 + 4 * HOUR, "USDT", -1.5, type_="8", subType="173",
                      instId="BTC-USDT-SWAP", instType="SWAP"))
    bills += fill(T0 + 5 * HOUR, "buy", 0.002, 51_200.0, fee_base=0.000002, tag="CLI")
    bills.append(bill(T0 + 6 * HOUR, "USDT", 0.1, type_="99"))
    bills.append(bill(T0 + 7 * HOUR, "USDT", -1_000.0, type_="1"))
    eq: dict = {}
    for row in bills:
        eq[row["ccy"]] = eq.get(row["ccy"], 0.0) + float(row["balChg"])
    grid_bot = {"algoId": GRID_ALGO, "instId": "BTC-USDT", "algoOrdType": "grid", "state": "running",
                "cTime": str(T0 - 5 * DAY), "investment": "1000", "totalPnl": "0.4",
                "gridProfit": "0.3", "floatProfit": "0.1", "arbitrageNum": "2", "algoClOrdId": ""}
    return {"bills": bills, "eq": eq, "grid_bot": grid_bot}


EQ_END = {"USDT": 6_498.085, "BTC": 0.051888}           # остатки после сценария
MODEL_T0 = 5_000 + 0.0999 * BTC_T0                       # 10 044.95
MODEL_T1 = EQ_END["USDT"] + EQ_END["BTC"] * BTC_NOW      # 9 144.373


def price(ccy: str, ts: int):
    if ccy == "USDT":
        return 1.0
    if ccy == "BTC":
        return BTC_T0 if ts == T0 else BTC_NOW
    return None


def inputs(sc: dict, **overrides) -> pl.Inputs:
    period = pl.period_bounds(date(2026, 9, 24), 1, TZ5, NOW_MS)
    base = dict(mode="demo", period=period, now_ms=NOW_MS, bills=sc["bills"],
                order_map={GRID_ORD: GRID_ALGO}, bots={GRID_ALGO: dict(sc["grid_bot"], _family="grid")},
                eq=sc["eq"], total_eq_usd=MODEL_T1, valuation={"total_usdt": MODEL_T1,
                                                                  "details": {"trading": MODEL_T1}},
                earn=[{"ts": str(T0 + 4 * HOUR), "ccy": "USDT", "earnings": "0.25"}],
                price=price, usdt_usd=lambda ts: 1.0, owner_of=pl.registry_owner_of(),
                registry_codes=pl.registry_codes())
    base.update(overrides)
    return pl.Inputs(**base)


def sleeve(report: dict, name: str) -> dict:
    return next(r for r in report["sleeves"] if r["sleeve"] == name)


class PeriodTest(unittest.TestCase):
    def test_day_in_project_tz_is_partial_until_midnight(self):
        per = pl.period_bounds(date(2026, 9, 24), 1, TZ5, NOW_MS)
        self.assertEqual(per.start_ms, T0)
        self.assertEqual(per.end_ms, T0 + DAY)
        self.assertEqual(per.t1_ms, NOW_MS)
        self.assertTrue(per.partial)
        self.assertEqual(per.label, "2026-09-24")

    def test_week_for_live_review_is_complete(self):
        per = pl.period_bounds(date(2026, 9, 23), 7, TZ5, NOW_MS)
        self.assertEqual(per.label, "2026-09-17_2026-09-23")
        self.assertEqual(per.end_ms, T0)
        self.assertEqual(per.t1_ms, per.end_ms)
        self.assertFalse(per.partial)
        self.assertEqual(per.end_ms - per.start_ms, 7 * DAY)

    def test_default_day_is_today_in_tz(self):
        self.assertEqual(pl.period_bounds(None, 1, TZ5, NOW_MS).label, "2026-09-24")
        # 23:30 UTC 23.09 — в +05:00 уже 24.09
        late = int(datetime(2026, 9, 23, 23, 30, tzinfo=timezone.utc).timestamp() * 1000)
        self.assertEqual(pl.period_bounds(None, 1, TZ5, late).label, "2026-09-24")

    def test_invalid_period(self):
        with self.assertRaises(ValueError):
            pl.period_bounds(date(2026, 9, 24), 0, TZ5, NOW_MS)
        with self.assertRaises(ValueError):
            pl.period_bounds(date(2026, 9, 25), 1, TZ5, NOW_MS)  # ещё не начался

    def test_parse_tz(self):
        self.assertEqual(pl.parse_tz("+05:00"), TZ5)
        with self.assertRaises(ValueError):
            pl.parse_tz("Asia/Yekaterinburg")


class RulesTest(unittest.TestCase):
    def test_project_rules_are_valid(self):
        self.assertEqual(pl.validate_rules(RULES), [])
        self.assertEqual(RULES["default_sleeve"], "manual")

    def test_every_registered_owner_has_a_sleeve(self):
        """Новый код в src/order_owner.py без строки в ops/sleeves.json ушёл бы в «прочее/ручное»."""
        common = RULES["cl_ord_id_prefixes"]["*"]
        missing = [o.code for o in order_owner.OWNERS if o.code not in common]
        self.assertEqual(missing, [])

    def test_mode_tables_override_common(self):
        self.assertEqual(pl._mode_table(RULES, "cl_ord_id_prefixes", "demo")["bot"], "engine")
        self.assertEqual(pl._mode_table(RULES, "cl_ord_id_prefixes", "live")["bot"], "dca")

    def test_broken_references_are_reported(self):
        rules = json.loads(json.dumps(RULES))
        rules["bots"]["1"] = {"sleeve": "nope"}
        rules["tags"]["paper"] = {"X": "manual"}
        rules["funding"]["default_sleeve"] = "missing"
        errors = pl.validate_rules(rules)
        self.assertTrue(any("bots.1" in e for e in errors), errors)
        self.assertTrue(any("tags.paper" in e for e in errors), errors)
        self.assertTrue(any("funding.default_sleeve" in e for e in errors), errors)
        self.assertEqual(pl.validate_rules({"sleeves": {}}), ["sleeves: нужен непустой объект рукавов"])

    def test_load_rules_errors(self):
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(pl.RulesError):
                pl.load_rules(Path(tmp) / "missing.json")
            bad = Path(tmp) / "bad.json"
            bad.write_text("{", encoding="utf-8")
            with self.assertRaises(pl.RulesError):
                pl.load_rules(bad)
            bad.write_text(json.dumps({"sleeves": {"a": {}}, "default_sleeve": "b"}), encoding="utf-8")
            with self.assertRaises(pl.RulesError):
                pl.load_rules(bad)


class _Resp:
    def __init__(self, code="0"):
        self.code = code
        self.calls = 0

    def private_get_account_balance(self, params):
        self.calls += 1
        return {"code": self.code, "msg": "boom" if self.code != "0" else "", "data": [{"totalEq": "1"}]}

    def private_post_trade_order(self, params):  # pragma: no cover — вызываться не должен
        raise AssertionError("ledger поставил ордер")


class ReaderTest(unittest.TestCase):
    def test_only_get_requests(self):
        ex = _Resp()
        reader = pl.Reader(ex)
        with self.assertRaises(ValueError):
            reader.get("private_post_trade_order", {"instId": "BTC-USDT"})
        self.assertEqual(reader.calls, 0)

    def test_non_zero_code_is_error(self):
        with self.assertRaises(RuntimeError):
            pl.Reader(_Resp(code="50001")).get("private_get_account_balance")

    def test_pause_between_calls(self):
        sleeps: list[float] = []
        reader = pl.Reader(_Resp(), pause=0.1, sleep=sleeps.append)
        for _ in range(3):
            reader.get("private_get_account_balance")
        self.assertEqual(sleeps, [0.1, 0.1])

    def test_real_ccxt_client_has_every_ledger_endpoint(self):
        ex = ccxt.okx({})  # без ключей и без сети
        methods = ["private_get_account_balance", "private_get_account_bills", "private_get_account_bills_archive",
                   "private_get_asset_asset_valuation", "private_get_finance_savings_lending_history",
                   "private_get_tradingbot_grid_sub_orders", "private_get_tradingbot_dca_cycle_list",
                   "private_get_tradingbot_dca_orders", "public_get_market_tickers",
                   "public_get_market_history_candles", "public_get_market_index_tickers",
                   "public_get_market_history_index_candles"]
        methods += [m for _, _, pair in pl.BOT_LISTS for m in pair]
        for name in methods:
            self.assertTrue(callable(getattr(ex, name, None)), name)


class ClassifierTest(unittest.TestCase):
    def classify(self, row: dict, mode: str = "demo", bots=None, order_map=None) -> pl.Attr:
        c = pl.Classifier(RULES, mode, order_map or {}, bots or {}, pl.registry_owner_of())
        return c(row)

    def test_owner_prefix_longest_wins(self):
        dca = bill(T0, "BTC", 0.1, clOrdId=order_owner.new_cl_ord_id(order_owner.DCA))
        self.assertEqual(self.classify(dca), pl.Attr("trade", "dca", "clOrdId:botsdca"))
        engine = bill(T0, "BTC", 0.1, clOrdId=order_owner.new_cl_ord_id(order_owner.ENGINE))
        self.assertEqual(self.classify(engine).sleeve, "engine")
        self.assertEqual(self.classify(engine, mode="live").sleeve, "dca")  # в live bot* ставит только runner
        test = bill(T0, "BTC", 0.1, clOrdId=order_owner.new_cl_ord_id(order_owner.LOAD_TEST))
        self.assertEqual(self.classify(test).sleeve, "tests")

    def test_tags_and_unmarked(self):
        cli = bill(T0, "BTC", 0.1, tag="CLI")
        self.assertEqual(self.classify(cli), pl.Attr("trade", "agent_cli", "tag:CLI"))
        live_cli = self.classify(cli, mode="live")  # в live таблица tags пуста
        self.assertEqual((live_cli.sleeve, live_cli.source), ("manual", "unknown:CLI"))
        ccxt_default = bill(T0, "BTC", 0.1, clOrdId=order_owner.CCXT_BROKER_ID + "0123456789abcdef",
                            tag=order_owner.CCXT_BROKER_ID)
        self.assertEqual(self.classify(ccxt_default).sleeve, "tests")
        human = self.classify(bill(T0, "BTC", 0.1))
        self.assertEqual((human.sleeve, human.source), ("manual", "unmarked"))
        self.assertIn("человека", human.note)

    def test_capital_and_funding(self):
        self.assertEqual(self.classify(bill(T0, "USDT", 5.0, type_="1")).sleeve, pl.UNALLOCATED)
        self.assertEqual(self.classify(bill(T0, "USDT", -5.0, type_="12")).category, "strategy_transfer")
        funding = self.classify(bill(T0, "USDT", -1.0, type_="8", instId="ETH-USDT-SWAP"))
        self.assertEqual((funding.category, funding.sleeve, funding.source),
                         ("funding", "funding_carry", "funding:ETH-USDT-SWAP"))
        rules = json.loads(json.dumps(RULES))
        rules["funding"]["by_inst"] = {"ETH-USDT-SWAP": "manual"}
        c = pl.Classifier(rules, "demo", {}, {})
        self.assertEqual(c(bill(T0, "USDT", -1.0, type_="8", instId="ETH-USDT-SWAP")).sleeve, "manual")

    def test_loan_interest_is_not_a_human_order(self):
        attr = self.classify(bill(T0, "USDT", -0.3, type_="7"))
        self.assertEqual((attr.category, attr.sleeve, attr.source), ("interest", "manual", "interest:USDT"))
        self.assertNotIn("человека", attr.note)

    def test_unknown_bill_type_is_kept(self):
        c = pl.Classifier(RULES, "demo", {}, {})
        attr = c(bill(T0, "USDT", 0.1, type_="99", subType="5"))
        self.assertEqual((attr.category, attr.sleeve, attr.source), ("other", "manual", "type:99"))
        self.assertEqual(dict(c.unknown_types), {"99/5": 1})

    def test_bot_orders(self):
        bots = {GRID_ALGO: {"instId": "BTC-USDT"},
                "fleet1": {"instId": "ETH-USDT", "algoClOrdId": "flt0123"},
                "anon": {"instId": "SOL-USDT"}}
        order_map = {"o1": GRID_ALGO, "o2": "fleet1", "o3": "anon"}
        c = pl.Classifier(RULES, "demo", order_map, bots)
        self.assertEqual(c(bill(T0, "BTC", 0.01, ordId="o1")), pl.Attr("trade", "native_grid", f"bot:{GRID_ALGO}"))
        self.assertEqual(c(bill(T0, "ETH", 0.01, ordId="o2")).sleeve, "demo_fleet")
        self.assertEqual(c(bill(T0, "SOL", 0.01, ordId="o3")).sleeve, "manual")
        self.assertEqual(c.unmapped_bots, {"anon"})

    def test_bot_fill_without_order_map_by_inst_and_lifetime(self):
        cl = "O" + "1" * 19
        one = {"b1": {"instId": "LTC-USDT", "cTime": str(T0 - HOUR), "state": "running"}}
        c = pl.Classifier(RULES, "demo", {}, one)
        attr = c(bill(T0, "LTC", 0.1, instId="LTC-USDT", clOrdId=cl))
        self.assertEqual((attr.source, attr.sleeve), ("bot:b1", "manual"))  # бот без правила
        self.assertEqual(c.heuristic_bills, 1)
        two = dict(one, b2={"instId": "LTC-USDT", "cTime": str(T0 - HOUR), "state": "running"})
        c = pl.Classifier(RULES, "demo", {}, two)
        self.assertEqual(c(bill(T0, "LTC", 0.1, instId="LTC-USDT", clOrdId=cl)).source, "bot:?")
        self.assertEqual(c.orphan_bot_bills, 1)
        # бот остановлен задолго до исполнения — не его
        stopped = {"b3": {"instId": "LTC-USDT", "cTime": str(T0 - 5 * HOUR), "stopTime": str(T0 - 2 * HOUR)}}
        c = pl.Classifier(RULES, "demo", {}, stopped)
        self.assertEqual(c(bill(T0, "LTC", 0.1, instId="LTC-USDT", clOrdId=cl)).source, "bot:?")

    def test_bot_end(self):
        self.assertEqual(pl.bot_end_ms({"stopTime": "5"}), 5.0)
        self.assertEqual(pl.bot_end_ms({"state": "stopped", "uTime": "7"}), 7.0)
        self.assertEqual(pl.bot_end_ms({"state": "running", "uTime": "7"}), float("inf"))


class PositionTest(unittest.TestCase):
    def test_average_price_and_partial_close(self):
        pos = pl.Position()
        self.assertEqual(pos.apply(1.0, -100.0), 0.0)
        self.assertEqual(pos.apply(1.0, -200.0), 0.0)
        self.assertAlmostEqual(pos.cost / pos.qty, 150.0)
        self.assertAlmostEqual(pos.apply(-1.0, 180.0), 30.0)
        self.assertAlmostEqual((pos.qty, pos.cost), (1.0, 150.0))
        self.assertAlmostEqual(pos.apply(-1.0, 140.0), -10.0)
        self.assertEqual((pos.qty, pos.cost), (0.0, 0.0))

    def test_flip_and_short_close(self):
        pos = pl.Position()
        pos.apply(1.0, -100.0)
        self.assertAlmostEqual(pos.apply(-3.0, 330.0), 10.0)   # закрыл 1 по 110, остаток 2 — шорт по 110
        self.assertAlmostEqual((pos.qty, pos.cost), (-2.0, -220.0))
        self.assertAlmostEqual(pos.apply(2.0, -180.0), 40.0)   # откуп шорта по 90
        self.assertEqual((pos.qty, pos.cost), (0.0, 0.0))

    def test_cash_only_leg(self):
        self.assertEqual(pl.Position().apply(0.0, -0.5), -0.5)


class BuildReportTest(unittest.TestCase):
    def setUp(self):
        self.sc = scenario()
        self.report = pl.build_report(inputs(self.sc), RULES)

    def test_sleeve_pnl_breakdown(self):
        dca = sleeve(self.report, "dca")
        avg = 5_000 / 0.0999
        realized = (2_597.4 / 0.05 - avg) * 0.05
        self.assertAlmostEqual(dca["pnl_usdt"], 97.35, places=5)
        self.assertAlmostEqual(dca["realized_usdt"], realized, places=5)
        self.assertAlmostEqual(dca["unrealized_change_usdt"], 97.35 - realized, places=5)
        self.assertAlmostEqual(dca["unrealized_end_usdt"], 0.0499 * BTC_NOW - (5_000 - 0.05 * avg), places=5)
        self.assertAlmostEqual(dca["fees_usdt"], 2.6, places=6)
        self.assertEqual(dca["fees_by_ccy"], {"USDT": 2.6})  # комиссия покупки — до периода
        self.assertEqual(dca["trades"], 1)
        self.assertAlmostEqual(dca["volume_usdt"], 2_600.0, places=6)
        self.assertAlmostEqual(dca["invested_usdt"], 5_000 - 0.05 * avg, places=5)
        self.assertAlmostEqual(dca["cum_pnl_usdt"], 142.3, places=5)

        grid = sleeve(self.report, "native_grid")
        self.assertAlmostEqual(grid["pnl_usdt"], -0.51, places=6)
        self.assertAlmostEqual(grid["fees_usdt"], 0.51, places=6)
        self.assertAlmostEqual(grid["invested_usdt"], 1_000.0)  # investment работающего бота
        self.assertAlmostEqual(sleeve(self.report, "manual")["pnl_usdt"], 4.485 + 0.1, places=6)
        self.assertAlmostEqual(sleeve(self.report, "agent_cli")["pnl_usdt"], -0.502, places=6)
        funding = sleeve(self.report, "funding_carry")
        self.assertAlmostEqual(funding["funding_usdt"], -1.5)
        self.assertAlmostEqual(funding["pnl_usdt"], -1.5)
        self.assertAlmostEqual(funding["unrealized_change_usdt"], 0.0)
        earn = sleeve(self.report, "cash_earn")
        self.assertAlmostEqual(earn["earn_usdt"], 0.25)
        self.assertAlmostEqual(earn["pnl_usdt"], 0.25)
        self.assertFalse(sleeve(self.report, "pump")["active"])

    def test_totals_and_reconciliation_identity(self):
        tot = self.report["totals"]
        self.assertAlmostEqual(tot["pnl_usdt"], 99.423 + 0.25, places=5)
        self.assertAlmostEqual(tot["fees_usdt"], 2.6 + 0.51 + 0.515 + 0.102, places=5)
        self.assertEqual(tot["trades"], 4)
        rec = self.report["reconciliation"]
        self.assertAlmostEqual(rec["components_usdt"]["sleeves_trading_pnl"], 99.423, places=5)
        self.assertAlmostEqual(rec["components_usdt"]["capital_flows"], -1_000.0)
        self.assertAlmostEqual(rec["components_usdt"]["unallocated_revaluation"], 0.0)
        self.assertAlmostEqual(rec["model_delta_usdt"], MODEL_T1 - MODEL_T0, places=5)
        self.assertLess(abs(rec["identity_error_usdt"]), 1e-5)
        self.assertTrue(rec["history_from_account_start"])
        eq = self.report["equity"]
        self.assertAlmostEqual(eq["model_start_usdt"], MODEL_T0, places=3)
        self.assertAlmostEqual(eq["model_end_usdt"], MODEL_T1, places=3)
        self.assertAlmostEqual(eq["level_check"]["diff_usdt"], 0.0, places=3)
        un = self.report["unallocated"]
        self.assertEqual(un["holdings_end"], {"USDT": 9_000.0})
        self.assertEqual(un["transfers"], {"USDT": -1_000.0})

    def test_bot_row_compares_with_okx_pnl(self):
        (row,) = self.report["bots"]
        self.assertEqual((row["algoId"], row["sleeve"], row["state"]), (GRID_ALGO, "native_grid", "running"))
        self.assertAlmostEqual(row["investment_usdt"], 1_000.0)
        self.assertAlmostEqual(row["okx_total_pnl_usdt"], 0.4)
        self.assertAlmostEqual(row["ledger_pnl_usdt"], -0.51, places=6)
        self.assertAlmostEqual(row["diff_usdt"], -0.91, places=6)

    def test_warnings_and_notes(self):
        warnings = " | ".join(self.report["warnings"])
        self.assertIn("нераспознанные типы bills", warnings)
        self.assertIn("99/1: 1", warnings)
        self.assertEqual(len(self.report["warnings"]), 1, self.report["warnings"])
        self.assertTrue(any("снимков equity биржи внутри периода нет" in n for n in self.report["notes"]))
        self.assertFalse(any("без рукава" in n for n in self.report["notes"]))

    def test_opening_balances_when_journal_is_shorter_than_history(self):
        sc = self.sc
        eq = dict(sc["eq"], ETH=2.0, USDT=sc["eq"]["USDT"] + 500.0)  # движения старше журнала
        prices = lambda ccy, ts: {"ETH": 2_000.0 if ts == T0 else 2_100.0}.get(ccy) or price(ccy, ts)  # noqa: E731
        report = pl.build_report(inputs(sc, eq=eq, price=prices), RULES)
        rec = report["reconciliation"]
        self.assertFalse(rec["history_from_account_start"])
        self.assertEqual(rec["opening_balances"], {"ETH": 2.0, "USDT": 500.0})
        self.assertAlmostEqual(rec["components_usdt"]["unallocated_revaluation"], 200.0)  # 2 ETH × +100
        self.assertLess(abs(rec["identity_error_usdt"]), 1e-5)
        self.assertTrue(any("остатки до начала журнала" in n for n in report["notes"]))

    def test_missing_price_is_a_warning(self):
        sc = self.sc
        extra = fill(T0 + HOUR, "buy", 1.0, 2.0, inst="XYZ-USDT", tag="CLI")
        report = pl.build_report(inputs(sc, bills=sc["bills"] + extra,
                                        eq=dict(sc["eq"], XYZ=1.0, USDT=sc["eq"]["USDT"] - 2.0),
                                        price=pl_price_with_missing()), RULES)
        self.assertTrue(any("нет цены в USDT для XYZ" in w for w in report["warnings"]), report["warnings"])

    def test_loan_interest_in_period_is_a_warning(self):
        sc = self.sc
        interest = [bill(T0 - DAY, "USDT", -0.2, type_="7"), bill(T0 + HOUR, "USDT", -0.3, type_="7")]
        report = pl.build_report(inputs(sc, bills=sc["bills"] + interest,
                                        eq=dict(sc["eq"], USDT=sc["eq"]["USDT"] - 0.5)), RULES)
        manual = sleeve(report, "manual")
        self.assertAlmostEqual(manual["interest_usdt"], -0.3)  # до периода — не в счёт
        self.assertIn("interest:USDT", [s["source"] for s in manual["sources"]])
        warning = next(w for w in report["warnings"] if w.startswith("проценты по займу"))
        self.assertIn("USDT -0.3", warning)
        self.assertLess(abs(report["reconciliation"]["identity_error_usdt"]), 1e-5)
        # без процентов за период предупреждения нет
        report = pl.build_report(inputs(sc, bills=sc["bills"] + interest[:1],
                                        eq=dict(sc["eq"], USDT=sc["eq"]["USDT"] - 0.2)), RULES)
        self.assertFalse(any(w.startswith("проценты по займу") for w in report["warnings"]))

    def test_bills_after_now_are_ignored(self):
        late = bill(NOW_MS + HOUR, "USDT", 777.0, type_="1")
        report = pl.build_report(inputs(self.sc, bills=self.sc["bills"] + [late]), RULES)
        self.assertEqual(report["unallocated"]["transfers"], {"USDT": -1_000.0})

    def test_exchange_check_by_snapshots(self):
        snap_ts = T0 + 30 * 60_000  # до первой сделки дня: на счёте 5 000 USDT и 0.0999 BTC
        snap = 5_000 + 0.0999 * BTC_NOW
        report = pl.build_report(inputs(self.sc, engine_curve=[(snap_ts, snap)]), RULES)
        check = report["equity"]["check"]
        self.assertEqual(check["from"]["source"], "equity_curve движка")
        self.assertEqual(check["to"]["source"], "asset-valuation trading")
        self.assertAlmostEqual(check["residual_usdt"], 0.0, places=3)
        # биржа на 5 USDT выше модели — невязка это показывает
        val = {"total_usdt": MODEL_T1 + 5, "details": {"trading": MODEL_T1 + 5}}
        report = pl.build_report(inputs(self.sc, engine_curve=[(snap_ts, snap)], valuation=val), RULES)
        self.assertAlmostEqual(report["equity"]["check"]["residual_usdt"], 5.0, places=3)
        # снимок другого масштаба (другой счёт, другая валюта) — не эталон
        report = pl.build_report(inputs(self.sc, engine_curve=[(snap_ts, snap * 3)]), RULES)
        self.assertIsNone(report["equity"]["check"])
        self.assertTrue(any("несопоставимых" in n for n in report["notes"]))

    def test_equity_path_drawdown(self):
        path = pl._equity_path([100.0, 110.0, 99.0, 105.0])
        self.assertEqual(path["snapshots"], 4)
        self.assertAlmostEqual(path["max_drawdown_pct"], 10.0)
        self.assertIsNone(pl._equity_path([100.0]))


def pl_price_with_missing():
    """Цена как в сценарии, но XYZ цены нет — как PriceBook с атрибутом missing."""
    def fn(ccy, ts):
        if ccy == "XYZ":
            fn.missing.add(ccy)
            return None
        return price(ccy, ts)
    fn.missing = set()
    return fn


class RenderTest(unittest.TestCase):
    def setUp(self):
        self.report = pl.build_report(inputs(scenario()), RULES)

    def test_markdown(self):
        md = pl.render_markdown(self.report)
        self.assertTrue(md.startswith("# PnL по рукавам — demo, 2026-09-24\n"))
        for part in ("## Рукава", "| DCA BTC/ETH | +97.35 |", "## Сверка с equity", "## Нативные боты",
                     f"`{GRID_ALGO}`", "## Предупреждения", "(неполный: день ещё идёт)",
                     "| **Δ стоимости торгового счёта (модель)** | **-900.58** |"):
            self.assertIn(part, md)
        self.assertNotIn("Демо-флот", md)  # рукав вне плана и без движений не показывается

    def test_telegram_text(self):
        text = pl.telegram_text(self.report)
        first, second = text.splitlines()[:2]
        self.assertEqual(first, "PnL demo 2026-09-24 (00:00–11:40 +05:00, неполный день)")
        self.assertTrue(second.startswith("Рукава: +99.67 USDT; комиссии 3.73;"), second)
        self.assertIn("Предупреждений: 1", text)
        short = pl.telegram_text(self.report, limit=60)
        self.assertEqual(len(short), 60)
        self.assertTrue(short.endswith("…"))

    def test_summary_and_files(self):
        with tempfile.TemporaryDirectory() as tmp:
            md_path, json_path = pl.write_report(self.report, Path(tmp) / "reports")
            self.assertEqual(md_path.name, "pnl_2026-09-24.md")
            saved = json.loads(json_path.read_text(encoding="utf-8"))
            self.assertEqual(saved["period"]["label"], "2026-09-24")
            self.assertEqual(saved["files"]["md"], str(md_path))
        summary = pl.summary_text(self.report)
        self.assertIn("рукава +99.67 USDT", summary)
        self.assertIn("! нераспознанные типы bills", summary)


class JournalTest(unittest.TestCase):
    def test_bills_are_idempotent_and_ordered(self):
        with tempfile.TemporaryDirectory() as tmp:
            journal = pl.Journal(Path(tmp) / "j.db")
            rows = [bill(T0 + 2, "USDT", 1.0), bill(T0 + 1, "USDT", 2.0)]
            rows[0]["junk"] = "не сохраняется"
            self.assertEqual(journal.add_bills(rows), 2)
            self.assertEqual(journal.add_bills(rows), 0)
            saved = journal.bills()
            self.assertEqual([b["balChg"] for b in saved], ["2", "1"])
            self.assertNotIn("junk", saved[1])
            self.assertEqual(len(journal.bills(until_ms=T0 + 1)), 1)
            self.assertEqual(journal.oldest_bill_id(T0 + 2), rows[0]["billId"])
            journal.set_meta("bills_cov_from", 5)
            self.assertEqual(journal.meta_int("bills_cov_from"), 5)
            self.assertIsNone(journal.meta_int("nope"))
            journal.add_snapshot(T0, 100.0, 120.0, 99.9, 1.0)
            journal.add_snapshot(T0 + HOUR, None, 120.0, 99.9, 1.0)
            self.assertEqual(journal.snapshots(T0, T0 + DAY), [(T0, 100.0)])


class FakeOkx:
    """GET-эндпоинты OKX, которые читает ledger. Любой другой вызов — ошибка теста."""

    def __init__(self, bills: list[dict], now_ms: int, grid_bots=(), dca_bots=(), grid_orders=None,
                 dca_cycles=None, dca_orders=None, balance=None, valuation=None, fail=()):
        self.all_bills = sorted(bills, key=lambda b: int(b["billId"]), reverse=True)
        self.now_ms = now_ms
        self.grid_bots, self.dca_bots = list(grid_bots), list(dca_bots)
        self.grid_orders = grid_orders or {}
        self.dca_cycles = dca_cycles or {}
        self.dca_orders = dca_orders or {}
        self.balance = balance or {}
        self.valuation = valuation or {}
        self.fail = set(fail)
        self.calls: list[str] = []

    def __getattr__(self, name):
        raise AttributeError(f"ledger вызвал {name} — такого эндпоинта у фейка нет")

    def _log(self, name: str) -> None:
        self.calls.append(name)
        if name in self.fail:
            raise ccxt.ExchangeError(f'okx {{"code":"50001","msg":"{name} down"}}')

    @staticmethod
    def _page(items: list[dict], params: dict, key: str) -> dict:
        limit = int(params.get("limit", 100))
        after = params.get("after")
        if after:
            items = [i for i in items if int(i[key]) < int(after)]
        return {"code": "0", "data": items[:limit]}

    # --- счёт ---
    def private_get_account_balance(self, params):
        self._log("balance")
        return {"code": "0", "data": [{"totalEq": _n(self.balance.get("total_eq", 0.0)),
                                       "details": [{"ccy": c, "eq": _n(q)}
                                                   for c, q in self.balance.get("eq", {}).items()]}]}

    def private_get_asset_asset_valuation(self, params):
        self._log("valuation")
        return {"code": "0", "data": [{"totalBal": _n(self.valuation.get("total", 0.0)),
                                       "details": {"trading": _n(self.valuation.get("trading", 0.0)),
                                                   "funding": "0", "earn": "0", "classic": "0"}}]}

    def private_get_account_bills(self, params):
        self._log("bills")
        recent = [b for b in self.all_bills if int(b["ts"]) >= self.now_ms - pl.BILLS_DEPTH_DAYS * DAY]
        return self._page(recent, params, "billId")

    def private_get_account_bills_archive(self, params):
        self._log("bills_archive")
        return self._page(self.all_bills, params, "billId")

    def private_get_finance_savings_lending_history(self, params):
        self._log("earn")
        raise ccxt.ExchangeError('okx {"code":"50038","msg":"This feature is unavailable in demo trading"}')

    # --- боты ---
    def _bots(self, name: str, bots: list[dict], params: dict, running: bool) -> dict:
        self._log(name)
        chosen = [b for b in bots if b["algoOrdType"] == params["algoOrdType"]
                  and (b["state"] == "running") == running]
        return self._page(chosen, params, "algoId")

    def private_get_tradingbot_grid_orders_algo_pending(self, params):
        return self._bots("grid_pending", self.grid_bots, params, True)

    def private_get_tradingbot_grid_orders_algo_history(self, params):
        return self._bots("grid_history", self.grid_bots, params, False)

    def private_get_tradingbot_dca_ongoing_list(self, params):
        return self._bots("dca_ongoing", self.dca_bots, params, True)

    def private_get_tradingbot_dca_history_list(self, params):
        return self._bots("dca_history", self.dca_bots, params, False)

    def private_get_tradingbot_grid_sub_orders(self, params):
        self._log("grid_sub_orders")
        assert params["type"] == "filled", params
        return self._page(self.grid_orders.get(params["algoId"], []), params, "ordId")

    def private_get_tradingbot_dca_cycle_list(self, params):
        self._log("dca_cycles")
        return self._page(self.dca_cycles.get(params["algoId"], []), params, "cycleId")

    def private_get_tradingbot_dca_orders(self, params):
        self._log("dca_orders")
        return self._page(self.dca_orders.get((params["algoId"], params["cycleId"]), []), params, "ordId")

    # --- цены ---
    def public_get_market_tickers(self, params):
        self._log("tickers")
        return {"code": "0", "data": [{"instId": "BTC-USDT", "last": _n(BTC_NOW)},
                                      {"instId": "ETH-BTC", "last": "0.05"}]}

    def public_get_market_history_candles(self, params):
        self._log("candles")
        minute = int(params["after"]) - 60_000
        if params["instId"] != "BTC-USDT":
            raise ccxt.BadRequest('okx {"code":"51001","msg":"Instrument ID does not exist"}')
        px = _n(BTC_T0 if minute == T0 else BTC_NOW)
        return {"code": "0", "data": [[str(minute), px, px, px, px, "1"]]}

    def public_get_market_index_tickers(self, params):
        self._log("index_tickers")
        return {"code": "0", "data": [{"instId": "USDT-USD", "idxPx": "1"}]}

    def public_get_market_history_index_candles(self, params):
        self._log("index_candles")
        minute = int(params["after"]) - 60_000
        return {"code": "0", "data": [[str(minute), "1", "1", "1", "1"]]}


def spread_bills(count: int, newest_ts: int, step_ms: int) -> list[dict]:
    """count bills от старых к новым с шагом step_ms, последний — в newest_ts."""
    return [bill(newest_ts - (count - 1 - i) * step_ms, "USDT", 1.0) for i in range(count)]


class SyncBillsTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.journal = pl.Journal(Path(self._tmp.name) / "j.db")

    def tearDown(self):
        self._tmp.cleanup()

    def test_first_sync_pages_until_end_of_history(self):
        rows = spread_bills(250, NOW_MS - HOUR, 10 * 60_000)  # ~42 ч истории
        ex = FakeOkx(rows, NOW_MS)
        since = NOW_MS - 7 * DAY
        stats = pl.sync_bills(pl.Reader(ex), self.journal, since, NOW_MS)
        self.assertEqual((stats["pages"], stats["archive_pages"], stats["fetched"], stats["new"]), (3, 0, 250, 250))
        self.assertEqual(self.journal.meta_int("bills_cov_from"), since)  # старше bills нет — покрыто с since
        self.assertEqual(self.journal.meta_int("bills_cov_to"), NOW_MS - HOUR)

    def test_incremental_sync_stops_at_known_bills(self):
        rows = spread_bills(250, NOW_MS - HOUR, 10 * 60_000)
        since = NOW_MS - 7 * DAY
        pl.sync_bills(pl.Reader(FakeOkx(rows, NOW_MS)), self.journal, since, NOW_MS)
        later = NOW_MS + HOUR
        fresh = spread_bills(30, later - 60_000, 60_000)
        ex = FakeOkx(rows + fresh, later)
        stats = pl.sync_bills(pl.Reader(ex), self.journal, later - 7 * DAY, later)
        self.assertEqual((stats["pages"], stats["new"]), (1, 30))
        self.assertEqual(self.journal.meta_int("bills_cov_from"), since)
        self.assertEqual(self.journal.meta_int("bills_cov_to"), later - 60_000)
        self.assertEqual(len(self.journal.bills()), 280)

    def test_deeper_period_goes_to_archive(self):
        old = spread_bills(20, NOW_MS - 8 * DAY, HOUR)      # старше 7 дней — только в bills-archive
        recent = spread_bills(40, NOW_MS - HOUR, HOUR)
        ex = FakeOkx(old + recent, NOW_MS)
        since = NOW_MS - 10 * DAY
        stats = pl.sync_bills(pl.Reader(ex), self.journal, since, NOW_MS)
        self.assertEqual(stats["new"], 60)
        self.assertGreaterEqual(stats["archive_pages"], 1)
        self.assertIn("bills_archive", ex.calls)
        self.assertEqual(self.journal.meta_int("bills_cov_from"), since)

    def test_backfill_older_than_coverage(self):
        rows = spread_bills(250, NOW_MS - HOUR, HOUR)       # bill в час; за 7 дней — 168 штук
        pl.sync_bills(pl.Reader(FakeOkx(rows, NOW_MS)), self.journal, NOW_MS - 2 * DAY, NOW_MS)
        self.assertEqual(len(self.journal.bills()), 100)    # одна страница уже старше since
        self.assertEqual(self.journal.meta_int("bills_cov_from"), NOW_MS - 100 * HOUR)
        # отчёт за неделю: журнал догружает хвост старше покрытия с курсора, без повторов
        ex = FakeOkx(rows, NOW_MS)
        stats = pl.sync_bills(pl.Reader(ex), self.journal, NOW_MS - 7 * DAY, NOW_MS)
        self.assertEqual(stats["new"], 68)
        self.assertEqual(len(self.journal.bills()), 168)
        self.assertEqual(self.journal.meta_int("bills_cov_from"), NOW_MS - 7 * DAY)
        self.assertNotIn("bills_archive", ex.calls)  # глубже 7 дней не нужно


class SyncBotsTest(unittest.TestCase):
    def test_bots_and_their_orders(self):
        grid = {"algoId": "11", "algoOrdType": "grid", "state": "running", "instId": "ETH-USDT",
                "cTime": str(T0 - DAY)}
        old_grid = {"algoId": "10", "algoOrdType": "grid", "state": "stopped", "instId": "SOL-USDT",
                    "cTime": str(T0 - 30 * DAY), "stopTime": str(T0 - 20 * DAY)}
        dca = {"algoId": "21", "algoOrdType": "spot_dca", "state": "running", "instId": "BNB-USDT",
               "cTime": str(T0 - DAY)}
        ex = FakeOkx([], NOW_MS, grid_bots=[grid, old_grid], dca_bots=[dca],
                     grid_orders={"11": [{"ordId": "902", "instId": "ETH-USDT", "uTime": str(T0)},
                                         {"ordId": "901", "instId": "ETH-USDT", "uTime": str(T0 - HOUR)}],
                                  "10": [{"ordId": "800"}]},
                     dca_cycles={"21": [{"cycleId": "5", "endTime": ""}]},
                     dca_orders={("21", "5"): [{"ordId": "702", "instId": "BNB-USDT"},
                                               {"ordId": "701", "instId": "BNB-USDT"}]},
                     fail={"dca_history"})
        with tempfile.TemporaryDirectory() as tmp:
            journal = pl.Journal(Path(tmp) / "j.db")
            stats = pl.sync_bots(pl.Reader(ex), journal, T0 - 7 * DAY, NOW_MS)
            self.assertEqual(stats["bots"], 3)
            self.assertEqual(stats["orders_new"], 4)
            self.assertEqual(len(stats["errors"]), 2, stats["errors"])  # dca history: spot и contract
            self.assertEqual(journal.bot_order_map(), {"901": "11", "902": "11", "701": "21", "702": "21"})
            self.assertEqual(journal.bots()["11"]["_family"], "grid")
            self.assertEqual(journal.bots()["21"]["_family"], "dca")
            # бот, закончивший до периода, ордера не запрашивает
            self.assertEqual(ex.calls.count("grid_sub_orders"), 1)


class PriceBookTest(unittest.TestCase):
    def test_now_prices_from_one_tickers_request(self):
        ex = FakeOkx([], NOW_MS)
        warnings: list[str] = []
        prices = pl.PriceBook(pl.Reader(ex), NOW_MS, warnings)
        self.assertEqual(prices("BTC", NOW_MS), BTC_NOW)
        self.assertAlmostEqual(prices("ETH", NOW_MS - 1000), 0.05 * BTC_NOW)  # кросс через BTC
        self.assertEqual(prices("USDT", 0), 1.0)
        self.assertIsNone(prices("XYZ", NOW_MS))
        self.assertEqual(prices.missing, {"XYZ"})
        self.assertEqual(ex.calls.count("tickers"), 1)
        self.assertEqual(warnings, [])

    def test_past_prices_from_minute_candles(self):
        ex = FakeOkx([], NOW_MS)
        prices = pl.PriceBook(pl.Reader(ex), NOW_MS, [])
        self.assertEqual(prices("BTC", T0), BTC_T0)
        self.assertEqual(prices("BTC", T0 + 30_000), BTC_T0)  # та же минута — из кэша
        self.assertEqual(ex.calls.count("candles"), 1)
        self.assertIsNone(prices("ETH", T0))  # ETH-USDT и ETH-BTC в фейке без свечей
        self.assertIn("ETH", prices.missing)

    def test_candle_interpolation_inside_minute(self):
        class Candles:
            def public_get_market_history_candles(self, params):
                minute = int(params["after"]) - 60_000
                return {"code": "0", "data": [[str(minute), "100", "170", "90", "160", "1"]]}
        self.assertAlmostEqual(pl.candle_price(pl.Reader(Candles()), "BTC-USDT", T0 + 30_000), 130.0)

    def test_usdt_usd_rate(self):
        ex = FakeOkx([], NOW_MS)
        rate = pl.UsdRate(pl.Reader(ex), NOW_MS, [])
        self.assertEqual(rate(NOW_MS), 1.0)
        self.assertEqual(rate(T0), 1.0)
        self.assertEqual((ex.calls.count("index_tickers"), ex.calls.count("index_candles")), (1, 1))


class EngineCurveTest(unittest.TestCase):
    def test_read_only_from_engine_db(self):
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
            path = Path(tmp) / "bot_state.db"
            with Storage(path) as storage:  # WAL, как у движка
                storage.record_equity(EquityRecord(ts=(T0 + HOUR) / 1000, total_eq=100.0, avail_eq=50.0, upl=0))
                storage.record_equity(EquityRecord(ts=(T0 - HOUR) / 1000, total_eq=90.0, avail_eq=50.0, upl=0))
                self.assertEqual(pl.read_equity_curve(path, T0, NOW_MS), [(T0 + HOUR, 100.0)])
            missing = Path(tmp) / "none.db"
            self.assertEqual(pl.read_equity_curve(missing, T0, NOW_MS), [])
            self.assertFalse(missing.exists())  # отсутствующую базу не создаёт


class RunTest(unittest.TestCase):
    """run() целиком: синхронизация журнала, отчёт, файлы — на фейковой бирже demo."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.dir = Path(self._tmp.name)
        sc = scenario()
        self.bot = sc["grid_bot"]
        self.ex = FakeOkx(sc["bills"], NOW_MS, grid_bots=[self.bot],
                          grid_orders={GRID_ALGO: [{"ordId": GRID_ORD, "instId": "BTC-USDT",
                                                    "uTime": str(T0 + 2 * HOUR)}]},
                          balance={"total_eq": MODEL_T1, "eq": sc["eq"]},
                          valuation={"total": MODEL_T1, "trading": MODEL_T1})

    def tearDown(self):
        self._tmp.cleanup()

    def run_ledger(self, now_ms: int = NOW_MS) -> dict:
        self.ex.now_ms = now_ms
        return pl.run(mode="demo", day=date(2026, 9, 24), rules_path=RULES_PATH, exchange=self.ex,
                      now_ms=now_ms, reports_dir=self.dir / "reports", engine_db=self.dir / "bot_state.db",
                      pause=0)

    def test_report_files_and_read_only_calls(self):
        report = self.run_ledger()
        self.assertTrue((self.dir / "reports" / "pnl_2026-09-24.md").exists())
        self.assertTrue((self.dir / "reports" / "pnl_2026-09-24.json").exists())
        self.assertFalse((self.dir / "bot_state.db").exists())
        self.assertAlmostEqual(report["totals"]["pnl_usdt"], 99.423, places=4)
        self.assertAlmostEqual(sleeve(report, "dca")["pnl_usdt"], 97.35, places=4)
        self.assertEqual(report["bots"][0]["ledger_pnl_usdt"], -0.51)
        self.assertLess(abs(report["reconciliation"]["identity_error_usdt"]), 1e-5)
        self.assertAlmostEqual(report["equity"]["level_check"]["diff_usdt"], 0.0, places=3)
        self.assertEqual(report["earn"]["note"], "источник Earn недоступен (finance/savings/lending-history)")
        self.assertTrue(any(n.startswith("Earn: ExchangeError") for n in report["notes"]), report["notes"])
        self.assertEqual(report["sources"]["bills_sync"]["new"], len(self.ex.all_bills))
        self.assertEqual(report["sources"]["bot_orders"], 1)
        self.assertNotIn("earn", " ".join(report["warnings"]))  # в demo Earn — заметка, не предупреждение

    def test_second_run_reuses_journal_and_checks_against_snapshot(self):
        self.run_ledger()
        report = self.run_ledger(NOW_MS + HOUR)
        self.assertEqual(report["sources"]["bills_sync"]["new"], 0)
        self.assertEqual(report["sources"]["bills_total"], len(self.ex.all_bills))
        check = report["equity"]["check"]
        self.assertIsNotNone(check)
        self.assertEqual(check["from"]["source"], "ledger")  # снимок первого запуска
        self.assertAlmostEqual(check["residual_usdt"], 0.0, places=3)

    def test_main_without_rules_fails_before_exchange(self):
        self.assertEqual(pl.main(["--rules", str(self.dir / "missing.json"), "--no-save"]), 2)


if __name__ == "__main__":
    unittest.main()
