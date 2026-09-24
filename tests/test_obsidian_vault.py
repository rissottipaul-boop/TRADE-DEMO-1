"""Obsidian-хранилище (AGENTS.md §9): настройки, ссылки в документах, шаблоны и запросы пульта."""
import importlib.util
import json
import os
import re
import unittest
from pathlib import Path
from urllib.parse import unquote

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("autopilot", ROOT / "ops" / "hooks" / "autopilot.py")
autopilot = importlib.util.module_from_spec(spec)
spec.loader.exec_module(autopilot)

DASHBOARD = ROOT / "obsidian" / "dashboard.md"
STATUSES = ("needs-user", "in-progress", "blocked", "scheduled", "ready", "done")

SKIP_DIRS = {"data", "logs", "venv", "__pycache__", "node_modules"}
AGENT_DOCS = (".github/agents/", ".claude/agents/")  # роли агентов: Obsidian их не видит, но это документы
HUMAN_ONLY = ("notes/", "obsidian/templates/")  # заметки человека и шаблоны с {{плейсхолдерами}}

CODE_BLOCK = re.compile(r"^[ \t]*(`{3,}|~{3,})[^\n]*\n.*?^[ \t]*\1[ \t]*$", re.M | re.S)
CODE_SPAN = re.compile(r"`[^`\n]*`")
MD_LINK = re.compile(r"\[[^\]\n]*\]\(([^)\s]+)")
QUERY_BLOCK = re.compile(r"^```query\n(.*?)\n```", re.M | re.S)

SAMPLE_BOARD = """
| ID | Задача | Агент | Статус | Зависит от | Критерий готовности | Заметки |
| --- | --- | --- | --- | --- | --- | --- |
| Q1 | вопрос | Human | needs-user | — | x | Вопрос: да или нет? |
| Q2 | ответ дан | Human | ready | — | x | **Ответ человека 2026-09-24 16:05:** да. Была needs-user |
| W1 | в работе | Insight Executor | in-progress Insight Executor 16:10 | — | x | |
| B1 | стоп | OKX Trader | blocked | — | x | ждёт in-progress соседа |
| S1 | проверка | Ops Sentinel | scheduled 2026-09-24T10:30+05:00 | — | x | |
| D1 | готово | Insight Executor | done | — | x | была ready |
"""


def project_docs() -> list[str]:
    """Markdown-документы проекта (пути от корня): всё видимое в Obsidian плюс роли агентов."""
    docs = []
    for dirpath, dirnames, filenames in os.walk(ROOT):
        rel = Path(dirpath).relative_to(ROOT).as_posix()
        prefix = "" if rel == "." else rel + "/"
        dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS and
                       (not d.startswith(".") or d in (".github", ".claude"))]
        for name in filenames:
            path = prefix + name
            if not name.endswith(".md") or path.startswith(HUMAN_ONLY):
                continue
            if path.startswith((".github/", ".claude/")) and not path.startswith(AGENT_DOCS):
                continue
            docs.append(path)
    return sorted(docs)


def prose(path: str) -> str:
    """Текст документа без блоков и фрагментов кода — там ссылки не живут."""
    text = (ROOT / path).read_text(encoding="utf-8")
    return CODE_SPAN.sub("", CODE_BLOCK.sub("", text))


def dashboard_line_regexes(path_filter: str) -> list[re.Pattern]:
    """Регулярки `line:/…/` из query-блоков пульта с данным `path:`."""
    found = []
    for query in QUERY_BLOCK.findall(DASHBOARD.read_text(encoding="utf-8")):
        if path_filter in query:
            found.append(re.compile(re.search(r"line:/(.+)/\s*$", query.strip()).group(1)))
    return found


def statuses_in(pattern: re.Pattern) -> set[str]:
    return {s for s in STATUSES if re.search(rf"(?<![\w-]){re.escape(s)}(?![\w-])", pattern.pattern)}


def row_id(line: str) -> str:
    return line.strip().strip("|").split("|")[0].strip("`* ")


class VaultSettingsTest(unittest.TestCase):
    def test_links_stay_portable(self):
        app = json.loads((ROOT / ".obsidian" / "app.json").read_text(encoding="utf-8"))
        self.assertIs(app["useMarkdownLinks"], True, "Obsidian должен писать markdown-ссылки, а не [[вики]]")
        self.assertEqual(app["newLinkFormat"], "relative")
        # Если папки нет, Obsidian молча кладёт новые заметки в корень репозитория
        self.assertTrue((ROOT / app["newFileFolderPath"]).is_dir())

    def test_templates_folder(self):
        options = json.loads((ROOT / ".obsidian" / "templates.json").read_text(encoding="utf-8"))
        folder = ROOT / options["folder"]
        for name in ("board-task.md", "board-answer.md"):
            self.assertTrue((folder / name).is_file(), name)

    def test_personal_obsidian_state_not_committed(self):
        rules = (ROOT / ".gitignore").read_text(encoding="utf-8").splitlines()
        for rule in (".obsidian/*", "!.obsidian/app.json", "!.obsidian/templates.json"):
            self.assertIn(rule, rules)


class DocLinksTest(unittest.TestCase):
    def test_no_wikilinks(self):
        offenders = [p for p in project_docs() if "[[" in prose(p)]
        self.assertEqual(offenders, [], "вики-ссылки не понимают GitHub и агенты (AGENTS.md §9)")

    def test_relative_links_resolve_inside_vault(self):
        problems = []
        for path in project_docs():
            for target in MD_LINK.findall(prose(path)):
                if re.match(r"[a-z][a-z0-9+.-]*:", target, re.I) or target.startswith(("#", "<")):
                    continue
                file_part = unquote(target.split("#", 1)[0])
                if not file_part:
                    continue
                dest = ((ROOT / path).parent / file_part).resolve()
                if not dest.exists():
                    problems.append(f"{path}: нет файла {target}")
                    continue
                try:
                    parts = dest.relative_to(ROOT.resolve()).parts
                except ValueError:
                    problems.append(f"{path}: ссылка за пределы репозитория {target}")
                    continue
                if not path.startswith(AGENT_DOCS) and any(p.startswith(".") for p in parts):
                    problems.append(f"{path}: Obsidian не видит скрытые папки — путь пишется кодом: {target}")
        self.assertEqual(problems, [])


class BoardTemplatesTest(unittest.TestCase):
    HEADER = ("| ID | Задача | Агент | Статус | Зависит от | Критерий готовности | Заметки |\n"
              "| --- | --- | --- | --- | --- | --- | --- |\n")

    def test_task_template_is_one_ready_row(self):
        row = (ROOT / "obsidian" / "templates" / "board-task.md").read_text(encoding="utf-8")
        self.assertEqual(row.count("\n"), 1)
        self.assertEqual(row.count("|"), 8, "7 колонок доски")
        tasks = autopilot.parse_board(self.HEADER + row)
        self.assertEqual([t["status"] for t in tasks.values()], ["ready"])
        self.assertEqual([t["deps"] for t in tasks.values()], [[]])

    def test_answer_template_fits_in_cell(self):
        snippet = (ROOT / "obsidian" / "templates" / "board-answer.md").read_text(encoding="utf-8")
        # Перевод строки разорвёт строку таблицы, а «|» сдвинет колонки
        self.assertNotIn("\n", snippet)
        self.assertNotIn("|", snippet)
        self.assertTrue(snippet.startswith("**Ответ человека "))


class DashboardQueriesTest(unittest.TestCase):
    def assert_board_queries_match(self, board: str):
        tasks = autopilot.parse_board(board)
        for pattern in dashboard_line_regexes('path:"ops/board.md"'):
            wanted = statuses_in(pattern)
            matched = {row_id(line) for line in board.splitlines() if pattern.search(line)}
            expected = {tid for tid, t in tasks.items() if t["status"] in wanted}
            self.assertEqual(matched, expected, f"{pattern.pattern} ↔ статусы {sorted(wanted)}")

    def test_board_queries_follow_status_column(self):
        covered = set().union(*map(statuses_in, dashboard_line_regexes('path:"ops/board.md"')))
        self.assertIn("needs-user", covered)
        self.assert_board_queries_match(SAMPLE_BOARD)
        self.assert_board_queries_match((ROOT / "ops" / "board.md").read_text(encoding="utf-8"))

    def test_incident_query_finds_every_row(self):
        (pattern,) = dashboard_line_regexes('path:"ops/incidents.md"')
        rows = [line for line in (ROOT / "ops" / "incidents.md").read_text(encoding="utf-8").splitlines()
                if line.startswith("|") and "Время" not in line and not set(line) <= set("|-: ")]
        self.assertTrue(rows)
        self.assertEqual([r[:40] for r in rows if not pattern.search(r)], [])

    def test_insight_status_query_matches_headers(self):
        (pattern,) = dashboard_line_regexes("path:insights/")
        lines = [line for path in sorted((ROOT / "insights").glob("*.md"))
                 for line in path.read_text(encoding="utf-8").splitlines() if "**Статус:**" in line]
        self.assertTrue(lines)
        self.assertEqual([line[:60] for line in lines if not pattern.search(line)], [])


if __name__ == "__main__":
    unittest.main()
