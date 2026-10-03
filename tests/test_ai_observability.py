"""Аудит, строгие read-only схемы, reported usage и durable идемпотентность."""
import json
import math
import unittest
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path
from tempfile import TemporaryDirectory

from src import ai_observability as ai


class SecretFilteringTests(unittest.TestCase):
    def test_nested_credentials_are_removed_without_losing_token_counts(self):
        value = {"OKX_API_KEY": "canary-key", "clientSecret": "canary-secret", "Authorization": "Bearer canary-auth", "data": [{"passphrase": "canary-pass"}], "input_tokens": 14}
        encoded = json.dumps(ai.redact(value))
        for secret in ("canary-key", "canary-secret", "canary-auth", "canary-pass"):
            self.assertNotIn(secret, encoded)
        self.assertEqual(ai.redact(value)["input_tokens"], 14)

    def test_free_text_quoted_json_prefixed_env_and_key_blocks(self):
        raw = 'OKX_API_KEY=plain-canary "password": "quoted canary"; Bearer auth-canary sk-12345678 ghp_abcdefgh eyJabc.abcd.abcd\n-----BEGIN RSA PRIVATE KEY-----\nprivate-canary\n-----END RSA PRIVATE KEY-----'
        clean = ai.redact(raw)
        for secret in ("plain-canary", "quoted canary", "auth-canary", "sk-12345678", "ghp_abcdefgh", "eyJabc.abcd.abcd", "private-canary"):
            self.assertNotIn(secret, clean)

    def test_escaped_json_password_value_is_removed_whole(self):
        self.assertNotIn("after-canary", ai.redact('"password": "first\\"after-canary"'))

    def test_bounded_text_items_and_depth(self):
        self.assertTrue(ai.redact("x" * 70000).endswith("[TRUNCATED]"))
        self.assertEqual(len(ai.redact(list(range(200)))), 101)
        nested = {"data": {"data": {"data": {"data": {"data": {"data": {"data": {"data": "secret"}}}}}}}}
        self.assertIn("DEPTH LIMIT", json.dumps(ai.redact(nested)))
        self.assertIsNone(ai.redact(float("nan")))

    def test_configurable_full_criterion_text_and_explicit_limit(self):
        text = "criterion " * 1200
        self.assertEqual(ai.redact(text, max_text=20000), text)
        with self.assertRaises(ValueError):
            ai.redact(text, max_text=2000000)

    def test_projection_omits_free_content_and_unknown_fields(self):
        safe = ai.safe_projection({"description": "secret without known pattern", "text": "private output", "unknown": "private data", "run_id": "run_1", "nested": {"token": "abc"}})
        self.assertEqual(safe, {"description_length": 28, "text_length": 14, "run_id": "run_1"})


class ToolSchemaTests(unittest.TestCase):
    def test_allowed_calls_return_normalized_arguments(self):
        args = {"run_id": "run_fixture", "after": 0, "limit": 100}
        self.assertEqual(ai.validate_tool_call("run.events", args), args)
        self.assertIsNot(ai.validate_tool_call("run.events", args), args)

    def test_write_trade_shell_and_unknown_tools_fail_closed(self):
        for tool in ("order.place", "run.start", "run.cancel", "shell", "ops.reset", "anything"):
            with self.subTest(tool=tool), self.assertRaises(ai.ToolValidationError):
                ai.validate_tool_call(tool, {})

    def test_unknown_missing_arguments_and_bools_rejected(self):
        cases = [("panel.state", {"token": "secret"}), ("board.task", {}), ("run.events", {"run_id": "run_1", "after": True}), ("runs.list", {"limit": False}), ("runs.list", {"limit": 101}), ("assistant.draft", {"description": " "}), ("assistant.role", {"description": "x" * 4001})]
        for tool, args in cases:
            with self.subTest(tool=tool, args=args), self.assertRaises(ai.ToolValidationError):
                ai.validate_tool_call(tool, args)

    def test_identifier_paths_and_wrong_types_rejected(self):
        for task in ("../other", "A/B", "A\\B", "x" * 81, 12, None):
            with self.subTest(task=task), self.assertRaises(ai.ToolValidationError):
                ai.validate_tool_call("board.task", {"task_id": task})
        for args in ([], None, "{}"):
            with self.assertRaises(ai.ToolValidationError):
                ai.validate_tool_call("panel.state", args)

    def test_specialists_cannot_escalate_or_recurse(self):
        ai.validate_tool_call("assistant.specialist", {"role": "crypto-insight-hunter", "tool": "sources.search", "arguments": {"query": "OKX docs"}})
        cases = [("ops-sentinel", "sources.search", {"query": "docs"}), ("crypto-insight-hunter", "diff.review", {"task_id": "T1"}), ("insight-executor", "assistant.specialist", {}), ("ops-sentinel", "task.read", {"task_id": "T1", "command": "write"})]
        for role, tool, args in cases:
            with self.subTest(role=role, tool=tool), self.assertRaises(ai.ToolValidationError):
                ai.validate_tool_call("assistant.specialist", {"role": role, "tool": tool, "arguments": args})

    def test_handoff_aliases_share_exact_schema(self):
        for name in ("task.handoff", "task.review", "assistant.handoff", "assistant.review", "diff.review"):
            self.assertEqual(ai.validate_tool_call(name, {"task_id": "T1", "run_id": "run_1"}), {"task_id": "T1", "run_id": "run_1"})


class LocalAuditTests(unittest.TestCase):
    def setUp(self):
        self.tmp = TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)

    def test_read_only_views_do_not_create_database(self):
        self.assertEqual(ai.read_actions(self.root), [])
        self.assertEqual(ai.usage_summary(self.root)["totals"]["cost_quality"], "unknown")
        self.assertFalse((self.root / "data").exists())

    def test_real_actions_safe_payload_latency_and_error_recovery(self):
        event = ai.record_action(self.root, request={"method": "POST", "path": "/api/assistant/tool", "prompt": "raw-request-canary"}, tool="assistant.draft", arguments={"description": "raw-description-canary"}, result={"status": "ready", "markdown": "raw-response-canary"}, model="model-fixture", duration_ms=12.5, outcome="failed", error="network")
        self.assertEqual(event["cursor"], 1)
        raw_bytes = (self.root / "data" / "ai" / "observability.sqlite3").read_bytes()
        for secret in (b"raw-request-canary", b"raw-description-canary", b"raw-response-canary"):
            self.assertNotIn(secret, raw_bytes)
        summary = ai.usage_summary(self.root)
        self.assertEqual(summary["functions"][0]["average_duration_ms"], 12.5)
        self.assertEqual(summary["totals"]["recorded_actions"], 1)
        self.assertFalse(summary["alerts"][0]["automatic_retry"])
        self.assertEqual(ai.read_actions(self.root)[0]["arguments"], {"description_length": 22})

    def test_concurrent_writers_keep_unique_cursors_and_counts(self):
        def write(index):
            return ai.record_action(self.root, request="local request", tool="panel.state", result={"count": index}, duration_ms=index)["cursor"]
        with ThreadPoolExecutor(max_workers=6) as pool:
            cursors = list(pool.map(write, range(24)))
        self.assertEqual(len(set(cursors)), 24)
        self.assertEqual(ai.usage_summary(self.root)["totals"]["recorded_actions"], 24)
        self.assertEqual(len(ai.read_actions(self.root, 5)), 5)

    def test_journal_invalid_metadata_rejected(self):
        with self.assertRaises(ValueError):
            ai.record_action(self.root, request="x", tool="tool", outcome="done")
        with self.assertRaises(ValueError):
            ai.record_action(self.root, request="x", tool="api_key=secret")
        with self.assertRaises(ValueError):
            ai.read_actions(self.root, True)

    def _write_run(self, run_id, **data):
        path = self.root / "data" / "runs" / (run_id + ".json")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"id": run_id, **data}), encoding="utf-8")

    def test_reported_cost_partial_unknown_not_zero_and_provenance(self):
        self._write_run("run_claude", runtime="claude", model="opus-fixture", cost={"usd": .35, "quality": "reported", "provenance": "native-fixture"}, tokens={"input_tokens": 120, "output_tokens": 20, "quality": "reported", "provenance": "native-fixture"}, started_at="2026-10-03T00:00:00Z", finished_at="2026-10-03T00:00:02Z")
        self._write_run("run_codex", runtime="codex", cost={"usd": 0, "quality": "unknown"}, status="unknown")
        summary = ai.usage_summary(self.root)
        self.assertEqual(summary["totals"]["reported_usd"], .35)
        self.assertEqual(summary["totals"]["cost_quality"], "partial")
        self.assertEqual(summary["totals"]["unknown_cost_runs"], 1)
        claude = next(run for run in summary["runs"] if run["run_id"] == "run_claude")
        codex = next(run for run in summary["runs"] if run["run_id"] == "run_codex")
        self.assertIsNone(codex["usd"])
        self.assertEqual(claude["duration_ms"], 2000)
        self.assertEqual(claude["tokens"]["input_tokens"], 120)
        self.assertEqual(summary["alerts"][0]["code"], "unknown_outcome")

    def test_bad_numbers_provenance_and_naive_clock_remain_unknown(self):
        self._write_run("run_bad", cost={"usd": True, "quality": "reported", "provenance": "fixture"}, tokens={"input_tokens": -1, "total_tokens": True, "quality": "reported", "provenance": "fixture"}, started_at="2026-10-03T00:00:00", finished_at="2026-10-03T00:00:01")
        self._write_run("run_no_provenance", cost={"usd": 1.5, "quality": "reported"})
        summary = ai.usage_summary(self.root)
        self.assertIsNone(summary["totals"]["reported_usd"])
        self.assertTrue(all(run["tokens"] is None for run in summary["runs"]))
        self.assertTrue(all(run["duration_ms"] is None for run in summary["runs"]))

    def test_bad_run_file_is_alert_and_never_false_good_metric(self):
        self._write_run("run_corrupt", cost={"usd": 9, "quality": "reported", "provenance": "fixture"})
        (self.root / "data" / "runs" / "run_corrupt.json").write_text("{", encoding="utf-8")
        summary = ai.usage_summary(self.root)
        self.assertIsNone(summary["totals"]["reported_usd"])
        self.assertEqual(summary["alerts"][0]["code"], "invalid_run_record")

    def test_malformed_and_huge_metadata_are_unknown_and_bounded(self):
        costs = (None, [], "private-canary", {"usd": "9" * 50000, "quality": "reported", "provenance": "fixture"}, {"usd": 10 ** 400, "quality": "reported", "provenance": "fixture"}, {"usd": math.inf, "quality": "reported", "provenance": "fixture"}, {"usd": 5, "quality": "reported", "provenance": {"api_key": "private-canary"}})
        for index, cost in enumerate(costs):
            self._write_run("run_bad" + str(index), cost=cost, tokens=["private-canary"], model="x" * 70000)
        summary = ai.usage_summary(self.root)
        self.assertIsNone(summary["totals"]["reported_usd"])
        self.assertEqual(summary["totals"]["unknown_cost_runs"], 7)
        self.assertTrue(all(len(run["model"]) <= 4110 for run in summary["runs"]))
        self.assertNotIn("private-canary", json.dumps(summary))
        json.dumps(summary, allow_nan=False)

    def test_corrupt_record_keeps_total_cost_quality_partial(self):
        self._write_run("run_reported", cost={"usd": .35, "quality": "reported", "provenance": "fixture"})
        self._write_run("run_corrupt")
        (self.root / "data" / "runs" / "run_corrupt.json").write_text("{", encoding="utf-8")
        summary = ai.usage_summary(self.root)
        self.assertEqual(summary["totals"]["cost_quality"], "partial")
        self.assertEqual(summary["totals"]["unknown_cost_runs"], 1)

    def test_arbitrary_result_objects_and_huge_numbers_have_safe_projection(self):
        event = ai.record_action(self.root, request={"unknown": object()}, tool="panel.state", result={"status": object(), "unknown": {"token": "private-canary"}}, duration_ms=10 ** 400)
        self.assertEqual(event["result"], {"status": "[UNSUPPORTED VALUE]"})
        self.assertIsNone(event["duration_ms"])
        self.assertNotIn("private-canary", json.dumps(event))


class IdempotencyTests(unittest.TestCase):
    def setUp(self):
        self.tmp = TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.store = ai.IdempotencyStore(self.root)

    def test_only_one_concurrent_reservation_is_allowed(self):
        with ThreadPoolExecutor(max_workers=6) as pool:
            results = list(pool.map(lambda _: ai.IdempotencyStore(self.root).begin("key1", {"task_id": "T1"}), range(18)))
        self.assertEqual(sum(result["execute"] for result in results), 1)
        self.assertTrue(all(result["reconciliation_required"] for result in results if not result["execute"]))

    def test_completed_replay_is_durable_and_never_reexecutes(self):
        start = self.store.begin("key1", {"task_id": "T1"})
        self.store.complete("key1", start["reservation"], {"run_id": "run_1", "status": "running"})
        replay = ai.IdempotencyStore(self.root).begin("key1", {"task_id": "T1"})
        self.assertFalse(replay["execute"])
        self.assertEqual(replay["result"]["run_id"], "run_1")

    def test_payload_conflict_and_wrong_owner_fail_closed(self):
        self.store.begin("key1", {"task_id": "T1"})
        with self.assertRaises(ai.IdempotencyError):
            self.store.begin("key1", {"task_id": "T2"})
        with self.assertRaises(ai.IdempotencyError):
            self.store.complete("key1", "0" * 48, {})

    def test_unknown_cannot_retry_without_fresh_query_evidence(self):
        start = self.store.begin("key1", {"task_id": "T1"})
        self.store.unknown("key1", start["reservation"])
        self.assertFalse(self.store.begin("key1", {"task_id": "T1"})["execute"])
        with self.assertRaises(ai.IdempotencyError):
            self.store.reconcile("key1", "not_executed", {})
        with self.assertRaises(ai.IdempotencyError):
            self.store.reconcile("key1", "not_executed", {"source": "fixture", "reference": "lookup", "observed_at": (datetime.now(timezone.utc) - timedelta(minutes=10)).isoformat(), "confirmed": True})
        evidence = {"source": "fixture-read-only-query", "reference": "request-id-1", "observed_at": datetime.now(timezone.utc).isoformat(), "confirmed": True}
        self.store.reconcile("key1", "not_executed", evidence)
        replacement = self.store.begin("key1", {"task_id": "T1"})
        self.assertTrue(replacement["execute"])
        self.assertNotEqual(start["reservation"], replacement["reservation"])
        with self.assertRaises(ai.IdempotencyError):
            self.store.complete("key1", start["reservation"], {})

    def test_confirmed_completed_reconciliation_returns_result(self):
        start = self.store.begin("key1", {"task_id": "T1"})
        self.store.unknown("key1", start["reservation"])
        evidence = {"source": "fixture-query", "reference": "run_1", "observed_at": datetime.now(timezone.utc).isoformat(), "confirmed": True}
        self.store.reconcile("key1", "completed", evidence, {"run_id": "run_1"})
        replay = self.store.begin("key1", {"task_id": "T1"})
        self.assertEqual(replay["state"], "completed")
        self.assertFalse(replay["execute"])
        self.assertEqual(replay["result"]["run_id"], "run_1")

    def test_payload_never_saved_and_result_free_content_omitted(self):
        start = self.store.begin("key1", {"token": "payload-canary"})
        self.store.complete("key1", start["reservation"], {"prompt": "result-canary", "status": "completed"})
        replay = self.store.begin("key1", {"token": "payload-canary"})
        self.assertNotIn("result-canary", json.dumps(replay))
        raw = (self.root / "data" / "ai" / "observability.sqlite3").read_bytes()
        self.assertNotIn(b"payload-canary", raw)
        self.assertNotIn(b"result-canary", raw)


if __name__ == "__main__":
    unittest.main()
