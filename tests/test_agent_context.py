"""Контекст доски agent_context: синтетические доски, без сети и записи в репозиторий.

Проверяется build_context/new_decisions/main модуля src.agent_context
(сам модуль и ops/hooks/ здесь только читаются, не правятся).
Доски строятся строкой в тесте; временные файлы — только через tempfile.
"""

import contextlib
import io
import json
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock

from src import agent_context
from src.agent_context import build_context, main, new_decisions


HEADER = "| ID | Задача | Агент | Статус | Зависит от | Критерий готовности | Заметки |"
SEP = "| --- | --- | --- | --- | --- | --- | --- |"
NOW = datetime(2026, 9, 30, 7, 20, 7, tzinfo=timezone.utc)


def make_row(task_id, title="Задача", agent="-", status="ready",
             deps="\u2014", crit="критерий", notes="заметки"):
    return (f"| {task_id} | {title} | {agent} | {status} | "
            f"{deps} | {crit} | {notes} |")


def make_board(*rows):
    return "\n".join([HEADER, SEP, *rows]) + "\n"


def linenos(text, line):
    return [i for i, current in enumerate(text.splitlines(), 1) if current == line]


class AgentContextTests(unittest.TestCase):
    def test_1_full_row_verbatim_with_long_criterion_and_notes(self):
        crit = "КРИТЕРИЙ-" * 100
        notes = "ЗАМЕТКА-" * 100
        self.assertGreater(len(crit), 500)
        self.assertGreater(len(notes), 500)
        raw = make_row("X", title="Икс", crit=crit, notes=notes)
        out = build_context(make_board(raw), "X", now=NOW)
        self.assertIn(raw, out)
        self.assertIn(crit, out)
        self.assertIn(notes, out)

    def test_2_transitive_dependencies_with_statuses_and_lines(self):
        row_a = make_row("A", title="Альфа", status="ready", deps="B")
        row_b = make_row("B", title="Бета", status="blocked", deps="C")
        row_c = make_row("C", title="Гамма", status="done")
        text = make_board(row_a, row_b, row_c)
        out = build_context(text, "A", now=NOW)
        line_b = linenos(text, row_b)[0]
        line_c = linenos(text, row_c)[0]
        self.assertIn(f"- B: blocked ; Бета; строка {line_b}", out)
        self.assertIn(f"- C: done ; Гамма; строка {line_c}", out)

    def test_2b_missing_dependency_marked_absent(self):
        row_d = make_row("D", title="Дельта", status="ready", deps="MISSING")
        out = build_context(make_board(row_d), "D", now=NOW)
        self.assertIn("- MISSING: ОТСУТСТВУЕТ", out)
        self.assertIn("MISSING", out)

    def test_3_active_claims_full_lines_except_self(self):
        row_x = make_row("X", title="Икс", status="in-progress alice 12:00")
        row_p = make_row("P1", title="Пэ-первая", status="in-progress bob 13:00")
        row_d = make_row("D1", title="Дэ-готовая", status="done")
        row_r = make_row("R1", title="Эр-готовая", status="ready")
        out = build_context(make_board(row_x, row_p, row_d, row_r), "X", now=NOW)
        self.assertIn(row_p, out)
        self.assertNotIn(row_d, out)
        self.assertNotIn(row_r, out)
        self.assertEqual(out.count(row_x), 1)

    def test_4_duplicate_id_ambiguity_and_excluded_candidates(self):
        row_dup1 = make_row("DUP", title="Первая", status="ready")
        row_dup2 = make_row("DUP", title="Вторая", status="ready")
        row_need = make_row("NEED", title="Ждущая", status="ready", deps="DUP")
        row_ok = make_row("OK1", title="Чистая", status="ready")
        text = make_board(row_dup1, row_dup2, row_need, row_ok)
        out = build_context(text, None, now=NOW)
        line_dup1 = linenos(text, row_dup1)[0]
        line_dup2 = linenos(text, row_dup2)[0]
        self.assertIn("Неоднозначности", out)
        self.assertIn(f"Дубликат DUP: строки {line_dup1}, {line_dup2}", out)
        candidates = [line for line in out.splitlines() if line.startswith("Кандидаты")]
        self.assertEqual(len(candidates), 1)
        self.assertIn("OK1", candidates[0])
        self.assertNotIn("DUP", candidates[0])
        self.assertNotIn("NEED", candidates[0])

    def test_5_overview_hides_done_criteria_and_notes(self):
        row_d = make_row("D1", title="Готовая", status="done",
                         crit="CRIT-D1-УНИК", notes="NOTE-D1-УНИК")
        row_r = make_row("R1", title="Кандидат", status="ready",
                         crit="CRIT-R1-УНИК", notes="NOTE-R1-УНИК")
        row_b = make_row("B1", title="Застрявшая", status="blocked")
        out = build_context(make_board(row_d, row_r, row_b), None, now=NOW)
        self.assertNotIn("| D1 |", out)
        self.assertIn("| R1 |", out)
        self.assertIn("| B1 |", out)
        self.assertNotIn("CRIT-D1-УНИК", out)
        self.assertNotIn("NOTE-D1-УНИК", out)
        self.assertNotIn("CRIT-R1-УНИК", out)
        self.assertNotIn("NOTE-R1-УНИК", out)
        self.assertIn("done=1", out)
        candidates = [line for line in out.splitlines() if line.startswith("Кандидаты")]
        self.assertEqual(len(candidates), 1)
        self.assertIn("R1", candidates[0])

    def test_6_value_errors(self):
        valid = make_board(make_row("X", title="Икс"))
        incomplete_header = ("| ID | Задача | Агент | Статус | Зависит от "
                             "| Критерий готовности |")
        cases = {
            "unknown task": lambda: build_context(valid, "NOPE", now=NOW),
            "unknown status": lambda: build_context(
                make_board(make_row("Y", title="Игрек", status="flying")), "Y", now=NOW),
            "cell count": lambda: build_context(
                HEADER + "\n" + "| Y | Игрек | - | ready | \u2014 | к |" + "\n",
                "Y", now=NOW),
            "incomplete header": lambda: build_context(
                incomplete_header + "\n" + "| Y | Игрек | - | ready | \u2014 | к |" + "\n",
                "Y", now=NOW),
            "no table": lambda: build_context(
                "просто текст без таблицы\nвторая строка\n", "Y", now=NOW),
        }
        for name, call in cases.items():
            with self.subTest(name=name):
                self.assertRaises(ValueError, call)

    def test_7_new_decisions_frontmatter_filter(self):
        with tempfile.TemporaryDirectory(prefix="agent-context-decisions-") as tmp:
            root = Path(tmp)
            decisions = root / "notes" / "decisions"
            decisions.mkdir(parents=True)
            (decisions / "t-new.md").write_text(
                "---\ntask: T1\ndate: 2026-09-30\nstatus: новое\n---\nтело\n",
                encoding="utf-8")
            (decisions / "t-moved.md").write_text(
                "---\ntask: T2\ndate: 2026-09-30\nstatus: перенесено\n---\nтело\n",
                encoding="utf-8")
            (decisions / "t-plain.md").write_text(
                "просто заметка без frontmatter\n", encoding="utf-8")
            self.assertEqual(new_decisions(root), ["notes/decisions/t-new.md"])

    def write_board(self, root, text):
        ops = Path(root) / "ops"
        ops.mkdir(parents=True, exist_ok=True)
        (ops / "board.md").write_bytes(text.encode("utf-8"))

    def run_main(self, argv):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = main(argv)
        return code, out.getvalue(), err.getvalue()

    def test_8_main_stats_and_task(self):
        row_r = make_row("R1", title="Кандидат", status="ready")
        row_d = make_row("D1", title="Готовая", status="done")
        text = make_board(row_r, row_d)
        with tempfile.TemporaryDirectory(prefix="agent-context-main-") as tmp:
            self.write_board(tmp, text)
            with mock.patch.object(agent_context, "ROOT", Path(tmp)):
                code, stdout, _ = self.run_main(["--stats"])
                self.assertEqual(code, 0)
                payload = json.loads(stdout)
                for key in ("board_bytes", "context_bytes", "reduction_pct_bytes"):
                    self.assertIn(key, payload)
                self.assertEqual(payload["board_bytes"], len(text.encode("utf-8")))
                code, stdout, _ = self.run_main(["--task", "R1"])
                self.assertEqual(code, 0)
                self.assertIn("Полная карточка R1", stdout)
                self.assertIn(row_r, stdout)

    def test_8b_main_broken_board_returns_2(self):
        with tempfile.TemporaryDirectory(prefix="agent-context-broken-") as tmp:
            self.write_board(tmp, "мусор без таблицы\n")
            with mock.patch.object(agent_context, "ROOT", Path(tmp)):
                for argv in (["--stats"], ["--task", "R1"]):
                    with self.subTest(argv=argv):
                        code, _, stderr = self.run_main(argv)
                        self.assertEqual(code, 2)
                        self.assertIn("agent_context", stderr)


if __name__ == "__main__":
    unittest.main()
