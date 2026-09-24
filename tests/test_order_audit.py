"""Аудит владельцев ордеров (ORDER-OWNER-TAG) на фейковых ответах OKX, без сети."""
import unittest
from unittest import mock

import ccxt

from src import order_audit, order_owner

NOW = 1_790_300_000_000                       # «сейчас» теста, мс
HOUR = 3_600_000
AFTER_RULE = order_owner.RULE_SINCE_MS + HOUR  # ордера после введения правила
BEFORE_RULE = order_owner.RULE_SINCE_MS - HOUR


def page(items: list[dict], params: dict, key: str) -> list[dict]:
    """Страница OKX: limit + курсор after (записи старше указанного id)."""
    limit = int((params or {}).get("limit", 100))
    after = (params or {}).get("after")
    start = 0
    if after:
        start = next(i for i, item in enumerate(items) if item[key] == after) + 1
    return items[start:start + limit]


class FakeExchange:
    """orders-pending / orders-algo-pending / orders-history(-archive) без сети."""

    def __init__(self, pending=(), algos=(), history=None, fail=()):
        self.pending = list(pending)
        self.algos = list(algos)
        self.history = history or {}  # instType -> ордера от новых к старым
        self.fail = set(fail)
        self.calls: list[tuple[str, dict]] = []

    def private_get_trade_orders_pending(self, params=None):
        self.calls.append(("pending", dict(params or {})))
        return {"data": page(self.pending, params, "ordId")}

    def private_get_trade_orders_algo_pending(self, params=None):
        types = params["ordType"].split(",")
        return {"data": page([a for a in self.algos if a["ordType"] in types], params, "algoId")}

    def _history(self, name, params):
        self.calls.append((name, dict(params)))
        if params["instType"] in self.fail:
            raise ccxt.BadRequest('okx {"code":"51000","msg":"Parameter instType error"}')
        return {"data": page(self.history.get(params["instType"], []), params, "ordId")}

    def private_get_trade_orders_history(self, params):
        return self._history("history", params)

    def private_get_trade_orders_history_archive(self, params):
        return self._history("archive", params)


def order(ord_id, c_time=AFTER_RULE, **fields) -> dict:
    base = {"ordId": ord_id, "clOrdId": "", "tag": "", "instId": "BTC-USDT", "side": "buy",
            "ordType": "limit", "sz": "0.001", "px": "30000", "state": "canceled",
            "category": "normal", "cTime": str(c_time)}
    base.update(fields)
    return base


def cl(code: str) -> str:
    return order_owner.new_cl_ord_id(code)


class AuditTest(unittest.TestCase):
    def mixed_exchange(self) -> FakeExchange:
        history = [  # от новых к старым
            order("h1", NOW - 1 * HOUR, clOrdId=cl("rt")),
            order("h2", NOW - 2 * HOUR, clOrdId=cl("rt")),
            order("h3", NOW - 3 * HOUR, clOrdId=cl("bot")),
            order("h4", NOW - 4 * HOUR, clOrdId=cl("botsdca"), ordType="market", state="filled"),
            order("h5", NOW - 5 * HOUR, instId="OKB-USDT", side="sell", state="filled"),  # человек
            order("h6", NOW - 6 * HOUR, tag="CLI"),                                     # агент без --clOrdId
            order("h7", NOW - 7 * HOUR, clOrdId=order_owner.CCXT_BROKER_ID + "0123456789abcdef",
                  tag=order_owner.CCXT_BROKER_ID),                                   # CCXT без clOrdId
            order("h8", NOW - 8 * HOUR, clOrdId="slip1"),                               # неизвестный префикс
            order("h9", BEFORE_RULE, tag="CLI"),                                        # до правила
        ]
        return FakeExchange(
            pending=[order("p1", NOW - HOUR, clOrdId=cl("trd"), state="live", instId="LTC-USDT")],
            algos=[{"algoId": "a1", "algoClOrdId": cl("pmp"), "tag": "CLI", "ordType": "conditional",
                    "instId": "SOL-USDT", "side": "sell", "sz": "1", "state": "live",
                    "cTime": str(NOW - HOUR)}],
            history={"SPOT": history},
        )

    def run_audit(self, ex, hours=None, **kwargs):
        hours = (NOW - BEFORE_RULE) / HOUR + 1 if hours is None else hours
        return order_audit.run_audit(ex, hours, now_ms=NOW, **kwargs)

    def test_groups_by_owner_and_levels(self):
        report = self.run_audit(self.mixed_exchange())
        by_owner = report["by_owner"]
        self.assertEqual({k: v["orders"] for k, v in by_owner.items()},
                         {"rt": 2, "bot": 1, "botsdca": 1, "trd": 1, "pmp": 1})
        self.assertEqual((by_owner["trd"]["active"], by_owner["pmp"]["active"], by_owner["rt"]["active"]),
                         (1, 1, 0))
        self.assertTrue(by_owner["bot"]["own"] and not by_owner["trd"]["own"])
        self.assertEqual([r["ordId"] for r in report["unmarked"]], ["h5"])
        self.assertIn("ручной ордер человека", report["unmarked"][0]["reason"])
        self.assertEqual({r["ordId"]: r["status"] for r in report["warnings"]},
                         {"h6": "cli", "h7": "ccxt_default", "h8": "unknown_prefix"})
        self.assertEqual([r["ordId"] for r in report["legacy"]], ["h9"])
        self.assertEqual(report["summary"], {"total": 11, "ok": 6, "info": 2, "warning": 3})
        self.assertEqual(report["read"], {"pending": 1, "algo_pending": 1, "history": 9})
        self.assertEqual(order_audit.exit_code(report), 1)

    def test_only_human_orders_is_exit_0(self):
        ex = FakeExchange(history={"SPOT": [order("h1", NOW - HOUR, instId="OKB-USDT")]})
        report = self.run_audit(ex, hours=24)
        self.assertEqual((report["summary"]["info"], report["summary"]["warning"]), (1, 0))
        self.assertEqual(order_audit.exit_code(report), 0)

    def test_live_mode_unmarked_is_warning(self):
        ex = FakeExchange(history={"SPOT": [order("h1", NOW - HOUR)]})
        report = self.run_audit(ex, hours=24, mode="live")
        self.assertEqual([r["status"] for r in report["warnings"]], ["unmarked"])
        self.assertEqual(order_audit.exit_code(report), 1)

    def test_history_paginates_and_stops_at_period_start(self):
        # 250 ордеров раз в минуту от новых к старым; период 3 ч = 180 ордеров
        history = [order(f"h{i}", NOW - i * 60_000, clOrdId=cl("lt")) for i in range(250)]
        ex = FakeExchange(history={"SPOT": history})
        report = self.run_audit(ex, hours=3, inst_types=("SPOT",))
        self.assertEqual(report["by_owner"]["lt"]["orders"], 181)  # i = 0…180 включительно
        spot_calls = [p for name, p in ex.calls if name == "history"]
        self.assertEqual(len(spot_calls), 2, "третья страница не нужна — ордер старше начала периода")
        self.assertNotIn("begin", spot_calls[0], "begin меняет порядок страниц OKX — не передаём")
        self.assertEqual(spot_calls[1]["after"], "h99")

    def test_long_period_uses_archive(self):
        ex = FakeExchange(history={"SPOT": [order("h1", NOW - HOUR, clOrdId=cl("rt"))]})
        report = self.run_audit(ex, hours=10 * 24, inst_types=("SPOT",))
        self.assertEqual(report["period"]["history"], "orders-history-archive")
        self.assertEqual({name for name, _ in ex.calls if name in ("history", "archive")}, {"archive"})

    def test_read_error_is_reported_and_exit_2(self):
        ex = FakeExchange(history={"SPOT": [order("h1", NOW - HOUR, clOrdId=cl("rt"))]}, fail={"OPTION"})
        report = self.run_audit(ex, hours=24)
        self.assertEqual([e["source"] for e in report["errors"]], ["orders-history OPTION"])
        self.assertEqual(report["by_owner"]["rt"]["orders"], 1, "остальные instType прочитаны")
        self.assertEqual(order_audit.exit_code(report), 2)

    def test_same_order_counted_once(self):
        same = order("x1", NOW - HOUR, clOrdId=cl("trd"), state="live")
        ex = FakeExchange(pending=[same], history={"SPOT": [dict(same, state="canceled")]})
        report = self.run_audit(ex, hours=24)
        self.assertEqual(report["summary"]["total"], 1)
        self.assertEqual(report["by_owner"]["trd"]["active"], 1)

    def test_truncation_is_flagged(self):
        history = [order(f"h{i}", NOW - i * 1_000, clOrdId=cl("rt")) for i in range(300)]
        with mock.patch.object(order_audit, "MAX_PAGES", 2):
            report = self.run_audit(FakeExchange(history={"SPOT": history}), hours=24, inst_types=("SPOT",))
        self.assertEqual(report["truncated"], ["SPOT"])
        self.assertEqual(report["by_owner"]["rt"]["orders"], 200)

    def test_render_text(self):
        report = self.run_audit(self.mixed_exchange())
        text = order_audit.render_text(report)
        for needle in ("Свои по префиксу clOrdId", "rt ", "INFO — без метки, вероятно ручной ордер человека (1)",
                       "INFO legacy", "WARNING — ордер без метки владельца (3)", "exit 1"):
            self.assertIn(needle, text)
        self.assertIn("h9", order_audit.render_text(report, verbose=True))


if __name__ == "__main__":
    unittest.main()
