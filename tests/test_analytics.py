"""Unit-тесты пакета src.analytics (п. 31–38, ANALYTICS-LAB).

Тесты работают без сети, без приватных ключей, без изменения реальных файлов состояния.
"""
from __future__ import annotations

import json
import sqlite3
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

from src.analytics import (bt_compare, bt_config, bt_summary, hypothesis,
                           insights_search, panel_metrics, risk_scenarios,
                           trade_review)
from src.analytics.common import clean, connect_ro, fmt, md_table, num, unclean
from src.backtest.data import Bar, InstrumentSpec, MarketDataStore
from src.backtest.engine import CostModel


class TestAnalyticsCommon(unittest.TestCase):
    def test_clean_unclean(self):
        self.assertEqual(clean(float("nan")), "nan")
        self.assertEqual(clean(float("inf")), "inf")
        self.assertEqual(clean(float("-inf")), "-inf")
        self.assertEqual(clean({"a": (1, 2)}), {"a": [1, 2]})
        self.assertEqual(unclean("inf"), float("inf"))
        self.assertEqual(unclean("-inf"), float("-inf"))

    def test_num_and_fmt(self):
        self.assertIsNone(num(None))
        self.assertIsNone(num("invalid"))
        self.assertIsNone(num(True))
        self.assertEqual(num("123.45"), 123.45)
        self.assertEqual(fmt(None), "—")
        self.assertEqual(fmt(True), "да")
        self.assertEqual(fmt(1234.56, 1), "1 234.6")

    def test_md_table(self):
        tbl = md_table(["A", "B"], [["x", "y"], ["1", "2"]])
        self.assertIn("| A | B |", tbl)
        self.assertIn("| x | y |", tbl)


class TestBtConfig(unittest.TestCase):
    def test_draft_from_text_variants(self):
        q1 = "Как mean reversion на ETH 1H с 2023-05-01 при комиссии x1.5 и RSI < 25?"
        d1 = bt_config.draft_from_text(q1)
        self.assertEqual(d1["inst"], "ETH-USDT")
        self.assertEqual(d1["bar"], "1H")
        self.assertEqual(d1["since"], "2023-05-01")
        self.assertEqual(d1["strategy"], "meanrev")
        self.assertEqual(d1["params"].get("rsi_entry"), 25.0)
        self.assertEqual(d1["costs"].get("fee_mult"), 1.5)
        self.assertTrue(len(d1["understood"]) >= 4)

        q2 = "SMA 10/50 на BTC 4 часа за последние 6 месяцев без комиссий"
        d2 = bt_config.draft_from_text(q2)
        self.assertEqual(d2["inst"], "BTC-USDT")
        self.assertEqual(d2["bar"], "4H")
        self.assertEqual(d2["strategy"], "sma_cross")
        self.assertEqual(d2["params"], {"fast": 10, "slow": 50})
        self.assertEqual(d2["costs"].get("fee_mult"), 0.0)

    def test_validate_success_and_errors(self):
        valid = {"inst": "BTC-USDT", "bar": "1H", "since": "2022-01-01",
                 "strategy": "sma_cross", "params": {"fast": 20, "slow": 100},
                 "costs": {"fee_mult": 1.0}}
        norm = bt_config.validate(valid)
        self.assertEqual(norm["inst"], "BTC-USDT")
        self.assertEqual(norm["initial_cash"], 10000.0)

        with self.assertRaises(bt_config.ConfigError) as ctx:
            bt_config.validate({"inst": "INVALID", "bar": "99H", "strategy": "unknown",
                                "params": {"not_a_param": 1}, "since": "not-a-date"})
        self.assertTrue(len(ctx.exception.problems) >= 4)

        # Invalid SMA fast >= slow
        with self.assertRaises(bt_config.ConfigError):
            bt_config.validate({"inst": "BTC-USDT", "bar": "1H", "since": "2022-01-01",
                                "strategy": "sma_cross", "params": {"fast": 100, "slow": 20}})


class TestBtSummaryAndCompare(unittest.TestCase):
    def _make_dummy_run(self, run_id="run-1", strategy="sma_cross", net_profit=100.0):
        return {
            "run_id": run_id,
            "config": {"strategy": strategy, "label": f"{strategy} test", "params": {},
                       "costs": {"fee_mult": 1.0, "slip_mult": 1.0}, "touch_holdout": False},
            "dataset": {"inst": "BTC-USDT", "bar": "1H", "dataset_id": "ds-123",
                        "first_ts": 1640995200000, "last_ts": 1672531200000,
                        "used_first_ts": 1640995200000, "used_last_ts": 1672531200000,
                        "bars_total": 8760, "bars_used": 7000, "gaps": 0, "anomalies": 0,
                        "holdout": {"days": 182, "excluded": True}},
            "costs": {"taker_fee": 0.001, "maker_fee": 0.0008, "slippage_bps": 5.0,
                      "limit_fill": "through", "limit_fee": "taker"},
            "metrics": {"initial": 10000.0, "final": 10000.0 + net_profit,
                        "net_profit": net_profit, "net_profit_pct": net_profit / 100.0,
                        "days": 365.0, "cagr_pct": 5.0, "mdd_pct": 8.0, "underwater_days": 20.0,
                        "sharpe": 1.2, "profit_factor": 1.5, "trades": 50,
                        "hit_rate_pct": 55.0, "expectancy": 2.0, "fees": 40.0,
                        "slippage": 15.0, "costs": 55.0, "gross_pnl": net_profit + 55.0,
                        "cost_share_pct": 35.0, "exposure_pct": 25.0, "benchmark_pct": 10.0,
                        "max_loss_streak": 3},
            "exit_reasons": {"signal": 45, "stop_loss": 5},
            "rejections": {},
        }

    def test_bt_summary(self):
        run = self._make_dummy_run()
        s = bt_summary.summarize(run)
        self.assertEqual(s["return_pct"], 1.0)
        self.assertEqual(s["trades"], 50)
        rendered = bt_summary.render(run)
        self.assertIn("# Сводка прогона", rendered)
        self.assertIn("Доходность, %", rendered)
        self.assertIn("holdout исключён", rendered)

    def test_bt_compare(self):
        run_a = self._make_dummy_run("run-A", "sma_cross", 100.0)
        run_b = self._make_dummy_run("run-B", "meanrev", 200.0)
        cmp = bt_compare.compare(run_a, run_b)
        self.assertTrue(cmp["comparable"])
        self.assertFalse(cmp["same_strategy"])
        self.assertEqual(len(cmp["metrics"]), len(bt_compare.KEY_METRICS))
        rendered = bt_compare.render(cmp, run_a, run_b)
        self.assertIn("# Сравнение прогонов", rendered)
        self.assertIn("Арифметика PnL", rendered)


class TestTradeReview(unittest.TestCase):
    def test_collect_trades_and_review(self):
        records = [
            (1, {"event": "scan", "pair": "FET-USDT"}),
            (2, {"event": "entry", "trade_id": "pmp123", "pair": "FET-USDT", "side": "buy",
                 "price": 1.0, "size": 1000.0, "cost_usdt": 1000.0, "fee_usdt": 1.0,
                 "stop": 0.98, "risk_usdt": 22.0, "cl_ord_id": "pmp_entry_1",
                 "ts": "2026-09-24T10:00:00+05:00", "reason": "impulse test"}),
            (3, {"event": "stop", "trade_id": "pmp123", "pair": "FET-USDT", "size": 1000.0,
                 "stop": 0.98, "orders": [{"algo_cl_ord_id": "pmp_stop_1"}],
                 "ts": "2026-09-24T10:02:00+05:00"}),
            (4, {"event": "exit", "trade_id": "pmp123", "pair": "FET-USDT", "price": 1.05,
                 "size": 1000.0, "pnl": 48.0, "fee_usdt": 1.0, "kind": "take",
                 "ts": "2026-09-24T11:00:00+05:00"}),
        ]
        trades = trade_review.collect_trades(records)
        self.assertEqual(len(trades), 1)
        t = trades[0]
        self.assertTrue(t.closed)

        pocket = trade_review.read_pocket(Path("pump-pocket.json"))
        rev = trade_review.review_trade(t, pocket)
        self.assertEqual(rev["pnl_total"], 48.0)
        self.assertEqual(rev["stop_initial"], 0.98)
        self.assertTrue(all(c["status"] in ("✅", "⚠️", "—") for c in rev["checks"]))

        rendered = trade_review.render({"pocket_error": None, "bad_lines": [],
                                        "reviews": [rev], "open_trades": []})
        self.assertIn("## FET-USDT — `pmp123`", rendered)
        self.assertIn("Обоснование входа", rendered)


class TestHypothesis(unittest.TestCase):
    def test_signal_mask(self):
        # 100 bars with increasing then decreasing close
        bars = [Bar(ts=i * 3600000, o=100.0 + i, h=102.0 + i, l=99.0 + i, c=101.0 + i, vol=100.0)
                for i in range(100)]
        mask, warm = hypothesis.signal_mask("sma_cross_up", {"fast": 5, "slow": 20}, bars)
        self.assertEqual(len(mask), 100)
        self.assertEqual(warm, 20)

    def test_hypothesis_render(self):
        res = {
            "claim": "Test claim", "inst": "BTC-USDT", "bar": "1H", "signal": "rsi_below",
            "params": {"n": 14, "level": 30.0}, "horizon": 24, "period": [1640995200000, 1672531200000],
            "bars": 5000, "dataset_id": "test_ds", "roundtrip_cost_pct": 0.3, "events": 40,
            "supports": ["доля прибыльных событий 55%"],
            "contradicts": ["минус в 2022 году"],
            "context": ["средний MAE -1.5%"],
            "missing": ["holdout не проверен"],
            "by_year": {2022: {"n": 40, "mean": 0.5, "hit": 55.0}},
            "verdict": "сигнал поддержан данными",
        }
        rendered = hypothesis.render(res)
        self.assertIn("# Проверка гипотезы: Test claim", rendered)
        self.assertIn("## Контекст и движение внутри горизонта", rendered)
        self.assertIn("## Чего не хватает", rendered)


class TestInsightsSearch(unittest.TestCase):
    def test_search_insights(self):
        res = insights_search.search("риск breaker drawdown", top=3)
        self.assertTrue(res["total"] > 0)
        self.assertTrue(len(res["hits"]) <= 3)
        h = res["hits"][0]
        self.assertIn("insights/", h["file"])
        self.assertTrue(h["date"] is not None)
        rendered = insights_search.render(res)
        self.assertIn("# Инсайты по запросу", rendered)


class TestRiskScenarios(unittest.TestCase):
    def test_scenarios_on_temp_db(self):
        with tempfile.TemporaryDirectory() as td:
            db_path = Path(td) / "risk_test.db"
            conn = sqlite3.connect(db_path)
            conn.executescript("""
                CREATE TABLE risk_kv (key TEXT PRIMARY KEY, value TEXT);
                CREATE TABLE risk_instruments (inst_id TEXT PRIMARY KEY, loss_streak INT, blocked_until REAL);
                CREATE TABLE risk_open_risk (inst_id TEXT PRIMARY KEY, side TEXT, risk_pct REAL, opened_at REAL);
                INSERT INTO risk_kv VALUES ('equity', '100000.0');
                INSERT INTO risk_kv VALUES ('hwm', '100000.0');
                INSERT INTO risk_kv VALUES ('day_start_equity', '100000.0');
                INSERT INTO risk_kv VALUES ('day_date', '2026-10-03');
                INSERT INTO risk_kv VALUES ('equity_updated_at', '1791012253.0');
                INSERT INTO risk_kv VALUES ('day_pnl', '0.0');
                INSERT INTO risk_kv VALUES ('global_loss_streak', '0');
                INSERT INTO risk_kv VALUES ('entries_today', '0');
            """)
            conn.commit()
            conn.close()

            scns = [
                {"name": "Шок −3%", "steps": [{"type": "equity_shock", "pct": -3.0}]},
                {"name": "Шок до глобального breaker", "steps": [{"type": "equity_shock", "pct": "to_global"}]},
            ]
            res = risk_scenarios.analyze(scns, db=db_path, now=1791012253.0)
            self.assertEqual(len(res["results"]), 2)
            r1, r2 = res["results"]
            self.assertTrue(r1["entry_allowed"])
            self.assertFalse(r2["entry_allowed"])
            self.assertTrue(r2["final"]["global_breaker"])

            rendered = risk_scenarios.render(res)
            self.assertIn("# Сценарный анализ риска", rendered)
            self.assertIn("Шок −3%", rendered)


class TestPanelMetrics(unittest.TestCase):
    def test_explain_metrics(self):
        res = panel_metrics.explain()
        self.assertTrue(len(res["metrics"]) >= 5)
        keys = {m["key"] for m in res["metrics"]}
        self.assertTrue({"equity", "hwm", "drawdown_pct", "baseline_usdt"}.issubset(keys))
        rendered = panel_metrics.render(res)
        self.assertIn("# Метрики панели", rendered)
        self.assertIn("HWM (high-water mark)", rendered)


if __name__ == "__main__":
    unittest.main()
