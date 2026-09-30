"""Передача свежего кандидата скана в PUMP-SCAN (PUMP-SCHED-HANDOFF, src/pump_handoff.py): без сети.

Фикстура журнала — строка scan 30.09 06:02 с кандидатом ZRO-USDT (формат v=1 сканера, поля
сокращены до используемых), фикстура доски — таблица в формате ops/board.md. Настоящие
data/pump_journal.jsonl и ops/board.md тесты не читают и не пишут: всё во временном каталоге.
"""
import importlib.util
import io
import json
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

from src import pump_handoff as ph

ROOT = Path(__file__).resolve().parent.parent
_spec = importlib.util.spec_from_file_location("autopilot_for_handoff",
                                               ROOT / "ops" / "hooks" / "autopilot.py")
autopilot = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(autopilot)

TZ = timezone(timedelta(hours=5))
FRESH_NOW = datetime(2026, 9, 30, 6, 2, 30, tzinfo=TZ)   # 2.5 мин после закрытия свечи 05:00–06:00
STALE_NOW = datetime(2026, 9, 30, 7, 25, tzinfo=TZ)      # как пришёл LLM-цикл 30.09

ZRO = {"inst_id": "ZRO-USDT", "close": 1.7499, "vol_quote": 289988.66, "impulse_pct": 4.8723,
       "vol_ratio_median": 2.8853, "vol_ratio_mean": 1.5003, "rsi": 66.75, "ma20": 1.60871,
       "macd_hist": 0.0125592, "chg24_pct": 13.652, "score": 10.0,
       "liquidity": {"ok": True},
       "freshness": {"ok": True, "age_min": 2.3, "last": 1.7592, "vs_close_pct": 0.53,
                     "reasons": []}}


def scan_row(ts="2026-09-30T06:02:00+05:00", candle_ts="2026-09-30T00:00:00Z", cands=(ZRO,),
             feed="live", freshness=None, source="src.pump_scanner"):
    return {"ts": ts, "event": "scan", "source": source, "v": 1, "feed": feed, "bar": "1H",
            "candle_ts": candle_ts, "bars": 99,
            "freshness": freshness or {"enabled": True, "max_age_min": 15.0,
                                       "price": "market/ticker last", "note": None},
            "counts": {"scanned": 45, "candidates": len(cands), "near": 2, "insufficient": 0,
                       "stale": 0, "no_data": 0},
            "candidates": list(cands), "illiquid": [], "demo_price": [], "near": [], "stale": [],
            "warnings": [], "note": "…"}


HEADER = ("| ID | Задача | Агент | Статус | Зависит от | Критерий готовности | Заметки |\n"
          "| --- | --- | --- | --- | --- | --- | --- |\n")
OTHER = "| PUMP-STALE | Правило свежести | Insight Executor | done | — | Тест | Сделано |\n"


def board_text(status="scheduled 2026-09-30T08:04+05:00", notes="Регулярная. Прошлый цикл 07:25."):
    return ("# Доска\n\n## Активные\n\n" + HEADER + OTHER +
            f"| PUMP-SCAN | Скан рынка на пампы (demo) | Pump Risk Taker | {status} | — | "
            f"Отчёт скана | {notes} |\n"
            "| PUMP-SCHED-HANDOFF | Передача | Insight Executor | in-progress x 08:12 | PUMP-STALE "
            "| Критерий | Заметка |\n\n## Архив\n\n" + HEADER + OTHER)


class Fixture(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        self.journal = self.dir / "pump_journal.jsonl"
        self.board = self.dir / "board.md"

    def tearDown(self):
        self.tmp.cleanup()

    def write_journal(self, *rows, extra_lines=()):
        lines = [json.dumps(r, ensure_ascii=False, separators=(",", ":")) for r in rows]
        self.journal.write_text("\n".join([*lines, *extra_lines]) + "\n", encoding="utf-8")

    def write_board(self, text=None):
        self.board.write_bytes((text if text is not None else board_text()).encode("utf-8"))

    def run_main(self, now, *extra):
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = ph.main(["--journal", str(self.journal), "--board", str(self.board),
                            "--now", now.isoformat(), "--json", *extra])
        return code, (json.loads(out.getvalue()) if out.getvalue() else None), err.getvalue()

    def row(self, text=None, task="PUMP-SCAN"):
        text = self.board.read_text(encoding="utf-8") if text is None else text
        return autopilot.parse_board(text)[task]

    def pump_line(self):
        return [ln for ln in self.board.read_text(encoding="utf-8").split("\n")
                if ln.startswith("| PUMP-SCAN |")][0]


class DecideTest(Fixture):
    def test_fresh_candidate(self):
        dec = ph.decide(scan_row(), FRESH_NOW)
        self.assertTrue(dec.handoff)
        self.assertEqual([p["inst_id"] for p in dec.pairs], ["ZRO-USDT"])
        self.assertEqual(dec.deadline, "06:15")
        self.assertEqual(dec.pairs[0]["age_min"], 2.5)

    def test_window_edge(self):
        at_edge = datetime(2026, 9, 30, 6, 15, tzinfo=TZ)
        self.assertTrue(ph.decide(scan_row(), at_edge).handoff)
        self.assertFalse(ph.decide(scan_row(), at_edge + timedelta(seconds=1)).handoff)

    def test_window_from_scan_row(self):
        row = scan_row(freshness={"enabled": True, "max_age_min": 5.0})
        self.assertFalse(ph.decide(row, datetime(2026, 9, 30, 6, 6, tzinfo=TZ)).handoff)

    def test_stale_zero_demo_and_not_ok(self):
        self.assertFalse(ph.decide(scan_row(), STALE_NOW).handoff)
        self.assertIn("устарели", ph.decide(scan_row(), STALE_NOW).reason)
        self.assertFalse(ph.decide(scan_row(cands=()), FRESH_NOW).handoff)
        self.assertFalse(ph.decide(scan_row(feed="demo"), FRESH_NOW).handoff)
        bad = dict(ZRO, freshness={"ok": False, "reasons": ["цена ниже close"]})
        self.assertFalse(ph.decide(scan_row(cands=(bad,)), FRESH_NOW).handoff)
        self.assertFalse(ph.decide(None, FRESH_NOW).handoff)

    def test_last_scan_of_scanner_only(self):
        older = scan_row()
        newer = scan_row(ts="2026-09-30T06:03:00+05:00", cands=())
        foreign = dict(scan_row(ts="2026-09-30T06:04:00+05:00"), source="pump-risk-taker")
        entry = {"ts": "2026-09-30T06:05:00+05:00", "event": "entry", "pair": "ZRO-USDT"}
        self.write_journal(older, newer, foreign, entry, extra_lines=["{битая строка"])
        self.assertEqual(ph.last_scan(self.journal)["ts"], "2026-09-30T06:03:00+05:00")
        self.assertIsNone(ph.last_scan(self.dir / "нет.jsonl"))


class BoardTest(Fixture):
    def test_scheduled_becomes_ready_with_pair(self):
        self.write_journal(scan_row())
        self.write_board()
        before = board_text().split("\n")
        code, res, _ = self.run_main(FRESH_NOW)
        self.assertEqual(code, 0)
        self.assertTrue(res["handoff"])
        self.assertTrue(res["board_changed"])
        after = self.board.read_text(encoding="utf-8").split("\n")
        changed = [i for i, (a, b) in enumerate(zip(before, after)) if a != b]
        self.assertEqual(len(before), len(after))
        self.assertEqual(len(changed), 1)                     # одна строка — PUMP-SCAN
        task = self.row()
        self.assertEqual(task["status"], "ready")
        self.assertIn("PUMP-SCAN", autopilot.ready_tasks(autopilot.parse_board(
            self.board.read_text(encoding="utf-8"))))
        line = self.pump_line()
        self.assertIn("ZRO-USDT", line)
        self.assertIn("до 06:15", line)
        self.assertIn("было «scheduled 2026-09-30T08:04+05:00»", line)
        self.assertIn("Регулярная. Прошлый цикл 07:25.", line)
        self.assertEqual(line.count("|"), 8)                  # формат строки не сломан
        cells = [c.strip() for c in line.strip("|").split("|")]
        self.assertEqual(cells[:3], ["PUMP-SCAN", "Скан рынка на пампы (demo)", "Pump Risk Taker"])
        self.assertEqual(cells[4:6], ["—", "Отчёт скана"])
        self.assertEqual(self.row(task="PUMP-SCHED-HANDOFF")["status"], "in-progress")

    def test_idempotent(self):
        self.write_journal(scan_row())
        self.write_board()
        self.run_main(FRESH_NOW)
        once = self.board.read_bytes()
        code, res, _ = self.run_main(FRESH_NOW + timedelta(minutes=1))
        self.assertEqual(code, 0)
        self.assertFalse(res["board_changed"])
        self.assertIn("уже записана", res["board"])
        self.assertEqual(self.board.read_bytes(), once)

    def test_stale_or_empty_scan_leaves_board(self):
        self.write_board()
        original = self.board.read_bytes()
        for row, now in ((scan_row(), STALE_NOW), (scan_row(cands=()), FRESH_NOW)):
            self.write_journal(row)
            code, res, _ = self.run_main(now)
            self.assertEqual(code, 0)
            self.assertFalse(res["handoff"])
            self.assertEqual(self.board.read_bytes(), original)

    def test_busy_or_human_status_not_touched(self):
        self.write_journal(scan_row())
        for status in ("in-progress Pump Risk Taker 06:01", "blocked нет кармана",
                       "needs-user", "done"):
            self.write_board(board_text(status=status))
            original = self.board.read_bytes()
            code, res, _ = self.run_main(FRESH_NOW)
            self.assertEqual(code, 0)
            self.assertTrue(res["handoff"])
            self.assertFalse(res["board_changed"])
            self.assertIn("не трогаю", res["board"])
            self.assertEqual(self.board.read_bytes(), original)

    def test_ready_gets_note_and_old_mark_replaced(self):
        self.write_board()
        self.write_journal(scan_row())
        self.run_main(FRESH_NOW)
        second = scan_row(ts="2026-09-30T07:02:00+05:00", candle_ts="2026-09-30T01:00:00Z",
                          cands=(dict(ZRO, inst_id="TRUMP-USDT"),))
        self.write_journal(second)
        code, res, _ = self.run_main(datetime(2026, 9, 30, 7, 3, tzinfo=TZ))
        self.assertTrue(res["board_changed"])
        line = self.pump_line()
        self.assertEqual(self.row()["status"], "ready")
        self.assertEqual(line.count("**Передача"), 1)
        self.assertIn("TRUMP-USDT", line)
        self.assertNotIn("ZRO-USDT", line)
        self.assertNotIn("было «", line)
        self.assertIn("Регулярная. Прошлый цикл 07:25.", line)

    def test_dry_run(self):
        self.write_journal(scan_row())
        self.write_board()
        original = self.board.read_bytes()
        code, res, _ = self.run_main(FRESH_NOW, "--dry-run")
        self.assertEqual(code, 0)
        self.assertTrue(res["handoff"])
        self.assertFalse(res["board_changed"])
        self.assertIn("сухой прогон", res["board"])
        self.assertEqual(self.board.read_bytes(), original)

    def test_broken_row_or_duplicate_exit_2(self):
        self.write_journal(scan_row())
        for text in (board_text(notes="есть | лишняя черта"),
                     board_text() + OTHER.replace("PUMP-STALE", "PUMP-SCAN"),   # дубль в архиве
                     board_text().replace("| PUMP-SCAN |", "| PUMP-SCAN-X |")):
            self.write_board(text)
            original = self.board.read_bytes()
            code, res, err = self.run_main(FRESH_NOW)
            self.assertEqual(code, 2, err)
            self.assertIsNone(res)
            self.assertIn("Передача не выполнена", err)
            self.assertEqual(self.board.read_bytes(), original)

    def test_concurrent_edit_is_kept(self):
        """Доску правят между чтением и записью: правка другого агента сохраняется,
        передача применяется поверх неё."""
        self.write_journal(scan_row())
        self.write_board()
        real_mkstemp = ph.tempfile.mkstemp
        calls = []

        def racing_mkstemp(*a, **kw):
            if not calls:
                text = self.board.read_text(encoding="utf-8").replace(
                    "| PUMP-STALE | Правило свежести | Insight Executor | done | — | Тест | Сделано |",
                    "| PUMP-STALE | Правило свежести | Insight Executor | done | — | Тест | Сделано, принято |",
                    1)
                self.board.write_bytes(text.encode("utf-8"))
            calls.append(1)
            return real_mkstemp(*a, **kw)

        with mock.patch.object(ph.tempfile, "mkstemp", side_effect=racing_mkstemp):
            code, res, _ = self.run_main(FRESH_NOW)
        self.assertEqual(code, 0)
        self.assertEqual(len(calls), 2)
        text = self.board.read_text(encoding="utf-8")
        self.assertIn("Сделано, принято", text)
        self.assertEqual(self.row()["status"], "ready")
        self.assertEqual(list(self.dir.glob(".board-*.tmp")), [])

    def test_crlf_and_backticked_status(self):
        self.write_journal(scan_row())
        self.write_board(board_text(status="`scheduled 2026-09-30T08:04+05:00`").replace("\n", "\r\n"))
        code, res, _ = self.run_main(FRESH_NOW)
        self.assertEqual(code, 0)
        self.assertTrue(res["board_changed"])
        raw = self.board.read_bytes()
        self.assertEqual(raw.count(b"\r\n"), board_text().count("\n"))
        self.assertEqual(self.row(raw.decode("utf-8"))["status"], "ready")


class ScheduleScriptTest(unittest.TestCase):
    def test_pump_scan_ps1_calls_handoff_after_scan(self):
        text = (ROOT / "ops" / "pump_scan.ps1").read_text(encoding="utf-8")
        self.assertIn("src.pump_handoff", text)
        self.assertLess(text.index("-m src.pump_scanner"), text.index("-m src.pump_handoff"))


if __name__ == "__main__":
    unittest.main()
