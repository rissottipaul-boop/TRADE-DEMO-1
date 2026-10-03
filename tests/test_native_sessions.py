"""Нативные журналы сессий CLI: фикстурные файлы, без настоящих рантаймов."""

import json
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from src import native_sessions as ns


def _write_jsonl(path: Path, records: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(json.dumps(r, ensure_ascii=False) for r in records) + "\n",
                    encoding="utf-8")


CLAUDE_RECORDS = [
    {"type": "queue-operation", "sessionId": "sess-claude-1",
     "timestamp": "2026-10-03T04:00:05.000Z", "operation": "enqueue"},
    {"type": "user", "sessionId": "sess-claude-1", "timestamp": "2026-10-03T04:00:06.000Z",
     "message": {"role": "user", "content": "SECRET-PROMPT-CANARY api_key=sk-very-private"}},
    {"type": "assistant", "sessionId": "sess-claude-1", "timestamp": "2026-10-03T04:01:00.000Z",
     "message": {"model": "claude-opus-5-5", "content": [{"type": "text", "text": "SECRET-OUTPUT-CANARY"}],
                 "usage": {"input_tokens": 120, "output_tokens": 45}}},
    {"type": "cost-state", "sessionId": "sess-claude-1", "timestamp": "2026-10-03T04:02:00.000Z",
     "modelUsage": {"claude-opus-5-5": {"costUSD": 0.3471, "inputTokens": 120},
                    "claude-haiku-4-5": {"costUSD": 0.0109}},
     "hasUnknownModelCost": False},
]

CODEX_RECORDS = [
    {"type": "session_meta", "timestamp": "2026-10-03T04:00:10.000Z",
     "payload": {"id": "sess-codex-1", "cwd": "", "timestamp": "2026-10-03T04:00:10.000Z",
                 "cli_version": "0.159.0"}},
    {"type": "event_msg", "timestamp": "2026-10-03T04:00:11.000Z",
     "payload": {"type": "task_started"}},
    {"type": "response_item", "timestamp": "2026-10-03T04:00:30.000Z",
     "payload": {"type": "message", "content": "SECRET-CODEX-CANARY"}},
    {"type": "event_msg", "timestamp": "2026-10-03T04:01:00.000Z",
     "payload": {"type": "token_count",
                 "info": {"total_token_usage": {"input_tokens": 900, "cached_input_tokens": 100,
                                                "output_tokens": 210, "total_tokens": 1210}}}},
    {"type": "event_msg", "timestamp": "2026-10-03T04:02:00.000Z",
     "payload": {"type": "task_complete"}},
]


class SlugTests(unittest.TestCase):
    def test_slug_matches_observed_claude_layout(self):
        # Фактический формат подтверждён на машине: разведка 03.10.2026.
        self.assertEqual(ns.claude_project_slug("c:\\TG\\BOT\\TRADE DEMO 1"),
                         "c--TG-BOT-TRADE-DEMO-1")
        self.assertEqual(
            ns.claude_project_slug("C:\\TG\\BOT\\TRADE DEMO 1\\data\\morphy-eval-package\\workspace"),
            "C--TG-BOT-TRADE-DEMO-1-data-morphy-eval-package-workspace")


class ClaudeTests(unittest.TestCase):
    def setUp(self):
        self.temp = TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.home = Path(self.temp.name)
        self.workdir = "c:\\TG\\BOT\\TRADE DEMO 1"
        self.project = self.home / ".claude" / "projects" / "c--TG-BOT-TRADE-DEMO-1"
        _write_jsonl(self.project / "sess-claude-1.jsonl", CLAUDE_RECORDS)

    def test_single_session_in_window_is_linked(self):
        link = ns.find_claude_session(self.home, self.workdir, "2026-10-03T04:00:00+00:00")
        self.assertIsNotNone(link)
        self.assertEqual(link["client"], "claude")
        self.assertEqual(link["session_id"], "sess-claude-1")
        # Регистр slug не мешает привязке.
        upper = ns.find_claude_session(self.home, "C:\\TG\\BOT\\TRADE DEMO 1",
                                       "2026-10-03T04:00:00+00:00")
        self.assertIsNotNone(upper)

    def test_ambiguous_or_out_of_window_sessions_are_not_guessed(self):
        second = [dict(CLAUDE_RECORDS[0], sessionId="sess-claude-2")]
        _write_jsonl(self.project / "sess-claude-2.jsonl", second)
        self.assertIsNone(ns.find_claude_session(self.home, self.workdir,
                                                 "2026-10-03T04:00:00+00:00"))
        # Запуск задолго после сессии: первая запись вне окна.
        (self.project / "sess-claude-2.jsonl").unlink()
        self.assertIsNone(ns.find_claude_session(
            self.home, self.workdir, "2026-10-03T08:00:00+00:00",
            finished_at="2026-10-03T08:05:00+00:00"))

    def test_ingest_extracts_metadata_and_cost_without_text(self):
        path = self.project / "sess-claude-1.jsonl"
        result = ns.claude_ingest(path)
        dump = json.dumps(result, ensure_ascii=False)
        self.assertNotIn("CANARY", dump)
        self.assertNotIn("sk-very-private", dump)
        types = [(e["type"], e["data"]["native_type"]) for e in result["events"]]
        self.assertEqual(types, [("run.progress", "user"), ("run.progress", "assistant"),
                                 ("run.cost", "cost-state")])
        assistant = result["events"][1]["data"]
        self.assertEqual(assistant["model"], "claude-opus-5-5")
        self.assertEqual(assistant["output_tokens"], 45)
        self.assertAlmostEqual(result["cost"]["usd"], 0.358, places=3)
        self.assertIn("claude-opus-5-5", result["cost"]["models"])
        # Повторный ingest с курсора ничего не дублирует.
        again = ns.claude_ingest(path, after_line=result["cursor"])
        self.assertEqual(again["events"], [])
        self.assertIsNone(again["cost"])


class CodexTests(unittest.TestCase):
    def setUp(self):
        self.temp = TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.home = Path(self.temp.name)
        self.workdir = str(Path(self.temp.name) / "repo")
        Path(self.workdir).mkdir()
        records = [dict(CODEX_RECORDS[0])] + CODEX_RECORDS[1:]
        records[0] = json.loads(json.dumps(records[0]))
        records[0]["payload"]["cwd"] = self.workdir
        self.rollout = (self.home / ".codex" / "sessions" / "2026" / "10" / "03" /
                        "rollout-2026-10-03T04-00-10-abc.jsonl")
        _write_jsonl(self.rollout, records)

    def test_session_meta_cwd_and_window_select_single_rollout(self):
        link = ns.find_codex_session(self.home, self.workdir, "2026-10-03T04:00:00+00:00")
        self.assertIsNotNone(link)
        self.assertEqual(link["session_id"], "sess-codex-1")
        other = str(Path(self.temp.name) / "other-repo")
        self.assertIsNone(ns.find_codex_session(self.home, other, "2026-10-03T04:00:00+00:00"))

    def test_ingest_returns_lifecycle_events_and_tokens_without_text(self):
        result = ns.codex_ingest(self.rollout)
        dump = json.dumps(result, ensure_ascii=False)
        self.assertNotIn("CANARY", dump)
        kinds = [e["data"]["native_type"] for e in result["events"]]
        self.assertEqual(kinds, ["session_meta", "task_started", "task_complete"])
        self.assertEqual(result["tokens"]["total_tokens"], 1210)
        self.assertEqual(result["tokens"]["output_tokens"], 210)
        # Журнал Codex подтверждённо не содержит стоимости в USD.
        self.assertIsNone(ns.ingest("codex", self.rollout).get("cost"))


class DispatchTests(unittest.TestCase):
    def test_clients_without_native_journal_get_honest_none(self):
        with TemporaryDirectory() as temp:
            home = Path(temp)
            for runtime in ("gemini", "muse"):
                self.assertIsNone(ns.find_session(home, runtime, "C:\\x",
                                                  "2026-10-03T04:00:00+00:00"))
            empty = ns.ingest("gemini", home / "missing.jsonl")
            self.assertEqual(empty["events"], [])
            self.assertIsNone(empty["cost"])


if __name__ == "__main__":
    unittest.main()
