"""Журнал кармана и остатки PUMP-JOURNAL (src/pump_journal.py): без сети, на временных файлах.

Карман в тестах маленький (бюджет 1000, дневной лимит 50, просадка 300, риск на сделку 10,
позиция 100, 3 позиции, стоп 1.5–2.5%), «сейчас» — 2026-09-24 12:00+05:00, сутки UTC начались
в 05:00+05:00. Настоящие data/pump_journal.jsonl и pump-pocket.json тесты не читают.
"""
import io
import json
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

from src import pump_journal as pj

TZ5 = timezone(timedelta(hours=5))
NOW = datetime(2026, 9, 24, 12, 0, tzinfo=TZ5)
POCKET = {"pocket": "test-pocket", "mode": "demo", "budget_usdt": 1000.0,
          "daily_loss_limit": 50.0, "max_drawdown": 300.0, "max_risk_per_trade": 10.0,
          "max_position_pct": 100.0, "max_open_positions": 3, "stop_loss_range": [1.5, 2.5]}

# Строки журнала в форматах, которые реально встречаются в data/pump_journal.jsonl
SCAN_V0 = {"ts": "2026-09-24T04:11+05:00", "event": "scan", "pair": None, "price": None,
           "size": None, "stop": None, "pnl": None, "reason": "Скан №2: 0 кандидатов"}
SCAN_V1 = {"ts": "2026-09-24T09:47:00+05:00", "event": "scan", "source": "src.pump_scanner",
           "v": 1, "feed": "demo", "bar": "1H", "counts": {"scanned": 45, "candidates": 0},
           "candidates": [], "illiquid": [], "near": [], "note": "0 кандидатов"}


def setUpModule():
    """Страховка: пути по умолчанию — во временном каталоге, не проектные файлы."""
    tmp = tempfile.TemporaryDirectory()
    unittest.addModuleCleanup(tmp.cleanup)
    for name, value in (("JOURNAL_PATH", Path(tmp.name) / "journal.jsonl"),
                        ("POCKET_PATH", Path(tmp.name) / "pocket.json")):
        patcher = mock.patch.object(pj, name, value)
        patcher.start()
        unittest.addModuleCleanup(patcher.stop)


def entry(ts="2026-09-24T10:00:00+05:00", pair="FET-USDT", price=1.0, size=100.0, stop=0.98,
          **extra) -> dict:
    rec = {"ts": ts, "event": "entry", "v": 1, "source": "pump-risk-taker", "pair": pair,
           "side": "buy", "price": price, "size": size, "stop": stop, "reason": "тест"}
    rec.update(extra)
    return rec


def exit_(ts="2026-09-24T11:00:00+05:00", pair="FET-USDT", **extra) -> dict:
    rec = {"ts": ts, "event": "exit", "v": 1, "source": "pump-risk-taker", "pair": pair,
           "kind": "take", "reason": "тест"}
    rec.update(extra)
    return rec


class Files(unittest.TestCase):
    """Временные журнал и карман; run() — CLI, report() — функция."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.dir = Path(self._tmp.name)
        self.journal = self.dir / "data" / "pump_journal.jsonl"
        self.pocket = self.dir / "pump-pocket.json"
        self.write_pocket()

    def write_pocket(self, **changes):
        data = dict(POCKET, **changes)
        self.pocket.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")

    def write_journal(self, *records, raw: str = ""):
        self.journal.parent.mkdir(parents=True, exist_ok=True)
        lines = [json.dumps(r, ensure_ascii=False) for r in records]
        self.journal.write_text("\n".join(lines) + ("\n" if lines else "") + raw,
                                encoding="utf-8")

    def report(self, now=NOW) -> dict:
        return pj.pocket_status(self.journal, self.pocket, now)

    def run_cli(self, *args, now=NOW) -> tuple[int, str, str]:
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = pj.main(["--journal", str(self.journal), "--pocket", str(self.pocket),
                            *args], now=now)
        return code, out.getvalue(), err.getvalue()


class LegacyAndEmpty(Files):
    def test_legacy_scan_lines_full_budget(self):
        """Строки scan v0 (без v) и v1 сканера читаются; сделок нет — карман целый."""
        self.write_journal(SCAN_V0, SCAN_V1)
        r = self.report()
        self.assertEqual(r["counts"]["scan"], 2)
        self.assertEqual(r["budget"]["free_usdt"], 1000.0)
        self.assertEqual(r["day"]["loss_left"], 50.0)
        self.assertEqual(r["drawdown"]["current_usdt"], 0.0)
        self.assertEqual(r["positions"]["open"], 0)
        self.assertTrue(r["entry"]["allowed"])
        self.assertEqual(r["entry"]["risk_max_usdt"], 10.0)
        self.assertEqual(r["entry"]["position_max_usdt"], 100.0)
        self.assertEqual(r["warnings"], [])

    def test_missing_journal_is_empty(self):
        code, out, _ = self.run_cli()
        self.assertEqual(code, 0)
        self.assertIn("сделок не было", out)
        self.assertIn("свободно 1000.00 из 1000.00", out)

    def test_unknown_types_fields_and_newer_version_do_not_break(self):
        self.write_journal(SCAN_V0, {"ts": "2026-09-24T10:00+05:00", "event": "note", "x": 1},
                           {"event": "heartbeat"},
                           entry(v=2, extra_field={"a": 1}, stop=0.98))
        r = self.report()
        self.assertEqual(r["counts"]["other"], 2)
        self.assertEqual(r["positions"]["open"], 1)
        self.assertTrue(any("heartbeat (1)" in x and "note (1)" in x for x in r["notes"]))
        self.assertTrue(any("новее схемы v1" in x for x in r["notes"]))

    def test_bad_lines_warn_and_calc_continues(self):
        """Битая строка и недописанная последняя (сканер пишет параллельно) — предупреждения."""
        self.write_journal(SCAN_V0, exit_(pnl=-5.0), raw='[1, 2]\n{"ts": "2026-09-24T11:')
        r = self.report()
        self.assertEqual(r["counts"]["bad"], 2)
        self.assertEqual(r["budget"]["realized_pnl"], -5.0)
        self.assertEqual(len([w for w in r["warnings"] if "нечитаемая строка" in w]), 2)

    def test_bom_in_pocket_and_journal(self):
        self.pocket.write_text(json.dumps(POCKET), encoding="utf-8-sig")
        self.journal.parent.mkdir(parents=True)
        self.journal.write_text(json.dumps(SCAN_V0) + "\n", encoding="utf-8-sig")
        r = self.report()
        self.assertEqual(r["counts"]["scan"], 1)
        self.assertEqual(r["counts"]["bad"], 0)


class Positions(Files):
    def test_real_prt_entry_and_oco_stop(self):
        """Формат первой сделки кармана 24.09: cost_usdt, fee_usdt, стоп OCO в orders, без
        trade_id — ключ pair. Риск = до стопа + комиссии входа и выхода (как считал агент)."""
        self.write_pocket(max_position_pct=1036.29, max_risk_per_trade=103.63,
                          budget_usdt=10362.91, daily_loss_limit=518.15, max_drawdown=3108.87)
        self.write_journal(
            {"ts": "2026-09-24T20:28:10+05:00", "event": "entry", "pair": "FET-USDT",
             "price": 0.21941, "size": 4688, "stop": 0.214, "pnl": None, "side": "buy",
             "cost_usdt": 1028.59, "fee_usdt": 1.0286, "risk_usdt": 27.39, "reason": "скан №6"},
            {"ts": "2026-09-24T20:28:27+05:00", "event": "stop", "pair": "FET-USDT",
             "price": 0.214, "size": 4688, "stop": 0.214, "pnl": None,
             "orders": [{"sl_trigger": 0.214, "tp_trigger": 0.2249, "size": 2344},
                        {"sl_trigger": 0.214, "tp_trigger": 0.2304, "size": 2344}]})
        r = self.report(now=datetime(2026, 9, 24, 20, 30, tzinfo=TZ5))
        pos = r["positions"]["list"][0]
        self.assertEqual(pos["pair"], "FET-USDT")
        self.assertAlmostEqual(pos["risk_usdt"], 27.39, places=2)
        self.assertAlmostEqual(pos["stop_pct"], -2.47, places=2)
        self.assertAlmostEqual(r["budget"]["free_usdt"], 10362.91 - 1028.59, places=2)
        self.assertTrue(r["entry"]["allowed"])
        self.assertEqual(r["entry"]["risk_max_usdt"], 103.63)
        self.assertEqual(r["warnings"], [])

    def test_stop_from_orders_most_conservative(self):
        """Нет поля stop — берётся sl_trigger из orders, для buy — меньший."""
        self.write_journal(entry(stop=None), {"ts": "2026-09-24T10:01+05:00", "event": "stop",
                                              "pair": "FET-USDT",
                                              "orders": [{"sl_trigger": 0.985},
                                                         {"sl_trigger": 0.98}]})
        pos = self.report()["positions"]["list"][0]
        self.assertEqual(pos["stop"], 0.98)
        self.assertAlmostEqual(pos["risk_usdt"], 2.0)

    def test_no_stop_blocks_and_risk_is_full_cost(self):
        self.write_journal(entry(stop=None))
        code, out, _ = self.run_cli()
        self.assertEqual(code, 1)
        self.assertIn("позиция без стопа: FET-USDT", out)
        r = self.report()
        self.assertEqual(r["positions"]["risk_usdt"], 100.0)

    def test_stop_removed_by_null(self):
        self.write_journal(entry(), {"ts": "2026-09-24T10:05+05:00", "event": "stop",
                                     "pair": "FET-USDT", "stop": None})
        self.assertFalse(self.report()["entry"]["allowed"])

    def test_trailing_stop_above_entry_zero_risk(self):
        self.write_journal(entry(), {"ts": "2026-09-24T10:30+05:00", "event": "stop",
                                     "pair": "FET-USDT", "stop": 1.01})
        self.assertEqual(self.report()["positions"]["risk_usdt"], 0.0)

    def test_max_open_positions(self):
        self.write_journal(entry(pair="A-USDT"), entry(pair="B-USDT"), entry(pair="C-USDT"))
        r = self.report()
        self.assertEqual(r["positions"]["open"], 3)
        self.assertIn("открыто позиций 3/3", r["entry"]["blocks"])

    def test_unknown_cost_assumed_max_position(self):
        self.write_journal(entry(price=None, size=None))
        r = self.report()
        self.assertEqual(r["budget"]["in_positions_usdt"], 100.0)
        self.assertTrue(any("стоимость входа" in w for w in r["warnings"]))

    def test_stop_out_of_pocket_range_warns(self):
        self.write_journal(entry(stop=0.95))
        self.assertTrue(any("5.00% вне диапазона кармана 1.5–2.5%" in w
                            for w in self.report()["warnings"]))

    def test_entry_without_reason_warns(self):
        self.write_journal(entry(reason=""))
        self.assertTrue(any("без обоснования" in w for w in self.report()["warnings"]))


class Pnl(Files):
    def test_partial_and_full_exit(self):
        """Два тейка: 50 + 50, pnl из журнала; позиция закрыта, PnL в текущих сутках."""
        self.write_journal(entry(trade_id="pmp1"),
                           exit_(trade_id="pmp1", price=1.025, size=50, pnl=1.2),
                           exit_(trade_id="pmp1", price=1.05, size=50, pnl=2.4))
        r = self.report()
        self.assertEqual(r["positions"]["open"], 0)
        self.assertAlmostEqual(r["budget"]["realized_pnl"], 3.6)
        self.assertAlmostEqual(r["day"]["pnl"], 3.6)
        self.assertEqual(r["day"]["exits"], 2)
        self.assertEqual(r["budget"]["free_usdt"], 1000.0)      # прибыль бюджет не расширяет
        self.assertAlmostEqual(r["budget"]["equity_usdt"], 1003.6)

    def test_partial_exit_keeps_rest(self):
        self.write_journal(entry(fee_usdt=0.1), exit_(price=1.025, size=40, pnl=0.9))
        pos = self.report()["positions"]["list"][0]
        self.assertAlmostEqual(pos["size"], 60)
        self.assertAlmostEqual(pos["cost_usdt"], 60.0)
        # 60 × (1 − 0.98) + комиссия входа 0.06 + выхода 0.001 × 0.98 × 60
        self.assertAlmostEqual(pos["risk_usdt"], round(1.2 + 0.06 + 0.0588, 2))

    def test_exit_without_pnl_computed_from_prices(self):
        self.write_journal(entry(fee_usdt=-0.1), exit_(price=0.98, fee_usdt=-0.098))
        r = self.report()
        self.assertAlmostEqual(r["budget"]["realized_pnl"], -2.198, places=2)   # −2 − 0.1 − 0.098
        self.assertTrue(any("рассчитан по ценам" in w for w in r["warnings"]))

    def test_exit_without_pnl_and_prices_assumes_stop_loss(self):
        self.write_journal(entry(), exit_())
        r = self.report()
        self.assertAlmostEqual(r["budget"]["realized_pnl"], -2.0)
        self.assertTrue(any("принят убыток до стопа" in w for w in r["warnings"]))

    def test_exit_by_pair_finds_trade_id_position(self):
        self.write_journal(entry(trade_id="pmpX"), exit_(pnl=-1.0))
        r = self.report()
        self.assertEqual(r["positions"]["open"], 0)
        self.assertEqual(r["warnings"], [])

    def test_exit_ambiguous_pair_counts_pnl_with_warning(self):
        self.write_journal(entry(trade_id="t1"), entry(trade_id="t2"), exit_(pnl=-1.0))
        r = self.report()
        self.assertEqual(r["positions"]["open"], 2)
        self.assertEqual(r["budget"]["realized_pnl"], -1.0)
        self.assertTrue(any("без открытой позиции" in w for w in r["warnings"]))

    def test_error_line_moves_money_only_with_pnl(self):
        self.write_journal({"ts": "2026-09-24T10:00+05:00", "event": "error", "kind": "scan",
                            "code": 2, "reason": "скан не состоялся"},
                           {"ts": "2026-09-24T10:05+05:00", "event": "error", "kind": "stop",
                            "pair": "LTC-USDT", "pnl": -3.0, "reason": "стоп не встал"})
        r = self.report()
        self.assertEqual(r["day"]["errors"], 2)
        self.assertEqual(r["budget"]["realized_pnl"], -3.0)
        self.assertTrue(any("в строке error учтён" in w for w in r["warnings"]))


class DayWindow(Files):
    def test_day_is_utc_by_default(self):
        """04:30+05 — вчерашние сутки UTC, 05:00+05 — уже сегодняшние (роль и src/risk.py)."""
        self.write_journal(exit_(ts="2026-09-24T04:30:00+05:00", pnl=-20.0),
                           exit_(ts="2026-09-24T05:00:00+05:00", pnl=-7.0))
        r = self.report()
        self.assertEqual(r["day"]["tz"], "+00:00")
        self.assertEqual(r["day"]["start"], "2026-09-24T05:00:00+05:00")
        self.assertEqual(r["day"]["end"], "2026-09-25T05:00:00+05:00")
        self.assertAlmostEqual(r["day"]["pnl"], -7.0)
        self.assertAlmostEqual(r["budget"]["realized_pnl"], -27.0)

    def test_day_tz_from_pocket(self):
        self.write_pocket(day_tz="+05:00")
        self.write_journal(exit_(ts="2026-09-24T04:30:00+05:00", pnl=-20.0))
        r = self.report()
        self.assertEqual(r["day"]["tz"], "+05:00")
        self.assertEqual(r["day"]["tz_source"], "pocket")
        self.assertAlmostEqual(r["day"]["pnl"], -20.0)

    def test_ts_formats(self):
        """Z, без секунд, без пояса (= +05:00) — всё читается."""
        self.write_journal(exit_(ts="2026-09-24T01:00:00Z", pnl=-1.0),
                           exit_(ts="2026-09-24T06:00", pnl=-2.0),
                           exit_(ts="2026-09-24T04:00", pnl=-4.0))
        self.assertAlmostEqual(self.report()["day"]["pnl"], -3.0)

    def test_unreadable_ts_loss_counts_today_profit_not(self):
        self.write_journal(exit_(ts="вчера", pnl=-5.0), exit_(ts=None, pnl=8.0))
        r = self.report()
        self.assertAlmostEqual(r["day"]["pnl"], -5.0)
        self.assertAlmostEqual(r["budget"]["realized_pnl"], 3.0)

    def test_daily_limit_exhausted_blocks_until_utc_midnight(self):
        self.write_journal(exit_(pnl=-30.0), exit_(pnl=-20.0))
        code, out, _ = self.run_cli()
        self.assertEqual(code, 1)
        self.assertIn("дневной лимит убытка исчерпан: 50.00/50.00 USDT", out)
        self.assertIn("до 2026-09-25T05:00:00+05:00", out)
        # на следующие сутки UTC лимит снова свободен
        code, _, _ = self.run_cli(now=datetime(2026, 9, 25, 5, 0, tzinfo=TZ5))
        self.assertEqual(code, 0)

    def test_open_risk_eats_day_left(self):
        """Остаток дневного лимита 5, риск открытой позиции 2 → на новый вход риск 3."""
        self.write_journal(exit_(pnl=-45.0), entry())
        r = self.report()
        self.assertTrue(r["entry"]["allowed"])
        self.assertAlmostEqual(r["entry"]["risk_max_usdt"], 3.0)
        self.write_journal(exit_(pnl=-49.0), entry(stop=0.97))
        r = self.report()
        self.assertFalse(r["entry"]["allowed"])
        self.assertTrue(any("съедает остаток" in b for b in r["entry"]["blocks"]))


class Drawdown(Files):
    def test_drawdown_from_peak(self):
        self.write_journal(exit_(ts="2026-09-20T10:00+05:00", pnl=100.0),
                           exit_(ts="2026-09-21T10:00+05:00", pnl=-40.0))
        dd = self.report()["drawdown"]
        self.assertAlmostEqual(dd["peak_usdt"], 1100.0)
        self.assertAlmostEqual(dd["current_usdt"], 40.0)
        self.assertAlmostEqual(dd["left_usdt"], 260.0)

    def test_drawdown_limit_latches(self):
        """Просадка достигла лимита хоть раз — входов нет и после отскока (решает человек)."""
        self.write_journal(exit_(ts="2026-09-20T10:00+05:00", pnl=-300.0),
                           exit_(ts="2026-09-21T10:00+05:00", pnl=+200.0))
        code, out, _ = self.run_cli()
        self.assertEqual(code, 1)
        self.assertIn("needs-user", out)
        r = self.report()
        self.assertAlmostEqual(r["drawdown"]["current_usdt"], 100.0)
        self.assertAlmostEqual(r["drawdown"]["max_usdt"], 300.0)

    def test_losses_shrink_free_budget(self):
        self.write_journal(exit_(ts="2026-09-20T10:00+05:00", pnl=-950.0))
        r = self.report()
        self.assertAlmostEqual(r["budget"]["free_usdt"], 50.0)
        self.assertAlmostEqual(r["entry"]["position_max_usdt"], 50.0)


class Cli(Files):
    def test_json_report(self):
        self.write_journal(SCAN_V0, entry())
        code, out, _ = self.run_cli("--json")
        self.assertEqual(code, 0)
        r = json.loads(out)
        self.assertEqual(r["journal"]["lines"], 2)
        self.assertEqual(r["pocket"]["mode"], "demo")
        self.assertEqual(r["positions"]["list"][0]["pair"], "FET-USDT")

    def test_text_report(self):
        self.write_journal(entry())
        code, out, _ = self.run_cli()
        self.assertEqual(code, 0)
        self.assertIn("Позиции: 1/3", out)
        self.assertIn("День (сутки UTC, 05:00–05:00 +05:00)", out)
        self.assertIn("Вход: разрешён", out)

    def test_pocket_errors_exit_2(self):
        self.pocket.unlink()
        code, _, err = self.run_cli()
        self.assertEqual(code, 2)
        self.assertIn("не найден", err)
        self.write_pocket(daily_loss_limit=None)
        self.assertEqual(self.run_cli()[0], 2)
        self.write_pocket(max_drawdown=True)
        self.assertEqual(self.run_cli()[0], 2)
        self.write_pocket(day_tz="полдень")
        self.assertEqual(self.run_cli()[0], 2)
        self.pocket.write_text("{не json", encoding="utf-8")
        self.assertEqual(self.run_cli()[0], 2)

    def test_journal_is_directory_exit_2(self):
        self.journal.mkdir(parents=True)
        self.assertEqual(self.run_cli()[0], 2)

    def test_files_are_not_modified(self):
        self.write_journal(SCAN_V0, entry(), exit_(pnl=1.0))
        before = (self.journal.read_bytes(), self.pocket.read_bytes())
        self.run_cli()
        self.run_cli("--json")
        self.assertEqual((self.journal.read_bytes(), self.pocket.read_bytes()), before)


if __name__ == "__main__":
    unittest.main()
