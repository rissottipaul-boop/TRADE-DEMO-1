"""Приёмка локальной границы AI и восстановления запуска, без внешних моделей."""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from http.client import HTTPConnection
import json
import sqlite3
from pathlib import Path
from tempfile import TemporaryDirectory
import threading
import unittest
from unittest.mock import patch

from src import control_panel as panel
from tests.test_control_panel import BOARD


class RunReservationTests(unittest.TestCase):
    def setUp(self):
        self.temp = TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        (self.root / "ops").mkdir()
        (self.root / "ops" / "board.md").write_text(BOARD, encoding="utf-8")
        (self.root / "ops" / "agent-routing.json").write_text(json.dumps({
            "roles": {"insight-executor": {}}, "runtimes": {"codex": {}}}), encoding="utf-8")

    def test_concurrent_start_spawns_once_with_persisted_intent(self):
        def spawn(*args, **kwargs):
            stored = panel._get_run(self.root, kwargs["run_id"], check_alive=False)
            self.assertEqual(stored["status"], "starting")
            self.assertIsNone(stored["pid"])
            return 12345
        with patch.object(panel, "_agent_plan", return_value={"selected": "codex", "launch_enabled": True}), \
             patch.object(panel, "_spawn_agent_process", side_effect=spawn) as invoke, \
             patch.object(panel, "process_alive", return_value=True), \
             ThreadPoolExecutor(max_workers=4) as pool:
            futures = [pool.submit(panel._start_run, self.root, "T1", "insight-executor", "codex",
                                   idempotency_key="same-key") for _ in range(4)]
            results = [future.result(timeout=5) for future in futures]
        self.assertEqual(invoke.call_count, 1)
        self.assertEqual(len({item["id"] for item in results}), 1)
        self.assertFalse((self.root / "data" / "runs" / ".start.lock").exists())

    def test_lost_spawn_response_preserves_unknown_and_blocks_new_key(self):
        with patch.object(panel, "_agent_plan", return_value={"selected": "codex", "launch_enabled": True}), \
             patch.object(panel, "_spawn_agent_process", side_effect=OSError("lost response")) as invoke:
            with self.assertRaises(OSError):
                panel._start_run(self.root, "T1", "insight-executor", "codex", idempotency_key="lost")
            records = panel._list_runs(self.root)
            self.assertEqual(len(records), 1)
            self.assertEqual(records[0]["status"], "unknown")
            replay = panel._start_run(self.root, "T1", "insight-executor", "codex", idempotency_key="lost")
            self.assertEqual(replay["id"], records[0]["id"])
            with self.assertRaisesRegex(ValueError, "Итог прежнего запуска неизвестен"):
                panel._start_run(self.root, "T1", "insight-executor", "codex", idempotency_key="other")
        self.assertEqual(invoke.call_count, 1)

    def test_restart_recovers_starting_record_as_unknown_without_spawn(self):
        panel._save_run(self.root, {"id": "run_T1_crashed", "task_id": "T1", "role": "insight-executor",
                                  "runtime": "codex", "status": "starting", "events": []})
        with patch.object(panel, "_spawn_agent_process") as spawn:
            self.assertEqual(panel._get_run(self.root, "run_T1_crashed")["status"], "unknown")
            self.assertEqual(panel._get_run(self.root, "run_T1_crashed", check_alive=False)["status"], "unknown")
            self.assertEqual(len(panel._get_run(self.root, "run_T1_crashed")["events"]), 1)
            with self.assertRaisesRegex(ValueError, "Итог прежнего запуска неизвестен"):
                panel._start_run(self.root, "T1", "insight-executor", "codex")
        spawn.assert_not_called()

    def test_abandoned_process_lock_is_not_reclaimed_by_timeout(self):
        path = panel._runs_dir(self.root) / ".start.lock"
        path.write_text('{"pid":0,"created_at":"2000-01-01"}', encoding="utf-8")
        with patch.object(panel, "_spawn_agent_process") as spawn:
            with self.assertRaisesRegex(ValueError, "нужна сверка reservation"):
                panel._start_run(self.root, "T1", "insight-executor", "codex")
        spawn.assert_not_called()
        self.assertTrue(path.exists())

    def test_relative_rotation_reference_is_resolved_against_run_root(self):
        (self.root / "logs").mkdir()
        (self.root / "logs" / "agent-rotate.log").write_text(json.dumps({
            "run_id": "run_T1_relative", "ts": "2026-10-03T10:00:00+00:00", "event": "finish", "exit_code": 0}) + "\n", encoding="utf-8")
        panel._save_run(self.root, {"id": "run_T1_relative", "task_id": "T1", "runtime": "codex",
                                  "status": "running", "rotation_log": "logs/agent-rotate.log", "events": []})
        run = panel._get_run(self.root, "run_T1_relative")
        self.assertEqual(run["status"], "completed")
        self.assertEqual(run["exit_code"], 0)

    def test_idempotent_recovery_works_after_board_becomes_done(self):
        panel._save_run(self.root, {"id": "run_T1_done", "task_id": "T1", "role": "insight-executor",
            "runtime": "codex", "status": "completed", "workspace": "checkout", "idempotency_key": "done",
            "model": None, "events": []})
        (self.root / "ops" / "board.md").write_text(BOARD.replace("| ready |", "| done |"), encoding="utf-8")
        with patch.object(panel, "_spawn_agent_process") as spawn:
            self.assertEqual(panel._start_run(self.root, "T1", "insight-executor", "codex",
                                            idempotency_key="done")["status"], "completed")
            with self.assertRaisesRegex(ValueError, "другого запуска"):
                panel._start_run(self.root, "T1", "insight-executor", "codex", idempotency_key="done", model="other")
        spawn.assert_not_called()


class AiApiTests(unittest.TestCase):
    def setUp(self):
        self.temp = TemporaryDirectory()
        self.root = Path(self.temp.name)
        (self.root / "ops").mkdir()
        (self.root / "ops" / "board.md").write_text(BOARD, encoding="utf-8")
        self.server = panel.PanelServer(("127.0.0.1", 0), root=self.root, token="test-secret")
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.headers = {"X-Control-Token": "test-secret", "Content-Type": "application/json"}

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)
        self.temp.cleanup()

    def request(self, method, path, payload=None, headers=None):
        connection = HTTPConnection("127.0.0.1", self.server.server_port, timeout=8)
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8") if payload is not None else None
        connection.request(method, path, body=body, headers=self.headers if headers is None else headers)
        response = connection.getresponse()
        status, content = response.status, response.read().decode("utf-8")
        connection.close()
        return status, json.loads(content)

    def save_run(self, **fields):
        run = {"id": "run_T1_result", "task_id": "T1", "role": "insight-executor", "runtime": "codex",
               "status": "completed", "exit_code": 0, "events": []}
        run.update(fields)
        panel._save_run(self.root, run)
        return run

    def test_assistant_api_auth_host_and_origin_boundary(self):
        for path in ("/api/ai/observability", "/api/ai/evals", "/api/run/result?id=run_T1_result", "/api/run/context?id=run_T1_result"):
            self.assertEqual(self.request("GET", path, headers={})[0], 403)
        payload = {"description": "Исправить тесты"}
        self.assertEqual(self.request("POST", "/api/assistant/draft", payload, headers={})[0], 403)
        self.assertEqual(self.request("POST", "/api/assistant/draft", payload,
                                      headers={**self.headers, "Origin": "https://foreign.example"})[0], 403)
        self.assertEqual(self.request("GET", "/api/ai/evals",
                                      headers={**self.headers, "Host": "foreign.example"})[0], 403)

    def test_draft_role_and_journal_are_read_only_and_secret_safe(self):
        from src.ai_observability import read_actions
        code, draft = self.request("POST", "/api/assistant/draft", {
            "description": "Исправить код. api_key=SECRET_CANARY"})
        self.assertEqual(code, 200)
        self.assertEqual(draft["recommended_role"]["role"], "insight-executor")
        self.assertTrue(draft["acceptance_criteria"])
        self.assertFalse(draft["board_written"])
        self.assertNotIn("SECRET_CANARY", json.dumps(draft))
        self.assertEqual((self.root / "ops" / "board.md").read_text(encoding="utf-8"), BOARD)
        actions = read_actions(self.root)
        self.assertTrue(actions)
        self.assertNotIn("SECRET_CANARY", json.dumps(actions))
        self.assertNotIn("Исправить код", json.dumps(actions))
        code, role = self.request("POST", "/api/assistant/tool", {
            "name": "assistant.role", "arguments": {"description": "Исследовать API и источники"}})
        self.assertEqual(code, 200)
        self.assertEqual(role["role"], "crypto-insight-hunter")

    def test_tools_reject_execution_and_extra_nested_fields_before_dispatch(self):
        with patch.object(panel, "_start_run") as spawn, patch.object(panel, "action") as action:
            for payload in (
                {"name": "run.start", "arguments": {"task_id": "T1"}},
                {"name": "board.task", "arguments": {"task_id": "T1", "command": "ops reset"}},
                {"name": "assistant.draft", "arguments": {"description": 12}},
                {"name": "assistant.specialist", "arguments": {"role": "ops-sentinel", "tool": "ops.reset", "arguments": {}}},
            ):
                self.assertEqual(self.request("POST", "/api/assistant/tool", payload)[0], 400)
            self.assertEqual(self.request("POST", "/api/assistant/draft", {"description": "x", "write_board": True})[0], 400)
            self.assertEqual(self.request("POST", "/api/agent/plan", {
                "task_id": "T1", "role": "insight-executor", "agent": "codex", "command": "bad"})[0], 400)
        spawn.assert_not_called()
        action.assert_not_called()

    def test_result_and_context_retain_evidence_without_accepting_task(self):
        run = self.save_run(result={"summary": "Проверки закончены", "changed_files": ["src/example.py"],
                    "checks": [{"command": "python -m unittest", "exit_code": 0}],
                    "external_actions": [{"id": "operation-1", "status": "unknown"}], "next_step": "Сверить operation-1"})
        code, result = self.request("GET", "/api/run/result?id=" + run["id"])
        self.assertEqual(code, 200)
        self.assertEqual(result["summary_quality"], "reported")
        self.assertEqual(result["changed_files"], ["src/example.py"])
        self.assertEqual(result["external_actions"][0]["status"], "unknown")
        self.assertFalse(result["acceptance_verified"])
        code, context = self.request("GET", "/api/run/context?id=" + run["id"])
        self.assertEqual(code, 200)
        self.assertEqual(context["kind"], "recovered-handoff")
        self.assertFalse(context["session_resumed"])
        self.assertEqual(context["handoff"]["checks"][0]["exit_code"], 0)
        self.assertIn("Проверено", context["markdown"])
        self.assertIn("python -m unittest", context["markdown"])
        self.assertIn("operation-1", context["markdown"])
        self.assertIn("Сверить operation-1", context["markdown"])

    def test_shared_response_filter_hides_sensitive_keys_and_preserves_long_lists(self):
        payload = {"events": [{"secret": "LITERAL_CANARY", "source": "api_key=ANOTHER_CANARY"}],
                   "files": list(range(150)), "cost": {"OKX_API_KEY": "PREFIX_CANARY"}}
        filtered = panel._redact_data(payload)
        self.assertNotIn("CANARY", json.dumps(filtered))
        self.assertEqual(len(filtered["files"]), 150)
        self.assertEqual(filtered["events"][0]["secret"], "[СКРЫТО]")

    def test_legacy_run_api_filters_old_unsanitized_records(self):
        path = panel._runs_dir(self.root) / "run_T1_legacy.json"
        path.write_text(json.dumps({"id": "run_T1_legacy", "status": "completed", "events": [],
                                   "metadata": {"OKX_API_KEY": "OLD_CANARY"}}), encoding="utf-8")
        code, response = self.request("GET", "/api/run?id=run_T1_legacy")
        self.assertEqual(code, 200)
        self.assertNotIn("OLD_CANARY", json.dumps(response))
        self.assertEqual(response["metadata"]["OKX_API_KEY"], "[СКРЫТО]")

    def test_exit_zero_without_report_stays_unverified(self):
        run = self.save_run(result_quality="exit-code-only")
        code, result = self.request("GET", "/api/run/result?id=" + run["id"])
        self.assertEqual(code, 200)
        self.assertEqual(result["summary_quality"], "lifecycle-only")
        self.assertFalse(result["acceptance_verified"])
        self.assertTrue(result["missing_evidence"])

    def test_resume_cancel_and_steer_are_unsupported_without_session_protocol(self):
        run = self.save_run()
        with patch.object(panel, "_spawn_agent_process") as spawn, patch.object(panel.subprocess, "run") as process:
            for route, extra in (("resume", {}), ("cancel", {}), ("steer", {"text": "Проверь тесты"})):
                code, result = self.request("POST", "/api/run/" + route, {"run_id": run["id"], **extra})
                self.assertEqual(code, 400)
                self.assertEqual(result["error"], "unsupported")
        spawn.assert_not_called()
        process.assert_not_called()
        self.assertEqual(panel._get_run(self.root, run["id"], False)["status"], "completed")

    def test_coding_start_defaults_to_worktree_and_blocks_main_checkout(self):
        with patch.object(panel, "_start_run", return_value={"id": "run_T1_started", "status": "running"}) as start:
            code, _ = self.request("POST", "/api/runs", {"task_id": "T1", "role": "insight-executor"})
            self.assertEqual(code, 200)
            self.assertEqual(start.call_args.kwargs["workspace"], "worktree")
            self.assertEqual(self.request("POST", "/api/runs", {
                "task_id": "T1", "role": "insight-executor", "workspace": "checkout"})[0], 400)
            self.assertEqual(self.request("POST", "/api/runs", {
                "task_id": "T1", "role": "insight-executor", "unexpected": True})[0], 400)
        self.assertEqual(start.call_count, 1)

    def test_unknown_replay_response_requires_reconciliation(self):
        with patch.object(panel, "_start_run", return_value={"id": "run_T1_unknown", "status": "unknown"}) as start:
            code, result = self.request("POST", "/api/runs", {"task_id": "T1", "role": "insight-executor"})
        self.assertEqual(code, 409)
        self.assertFalse(result["ok"])
        self.assertEqual(result["error"], "needs_reconciliation")
        self.assertEqual(start.call_count, 1)

    def test_invalid_start_optional_types_are_rejected_before_start(self):
        with patch.object(panel, "_start_run") as start:
            for field, value in (("model", {}), ("model", "--unsafe"), ("idempotency_key", 123)):
                self.assertEqual(self.request("POST", "/api/runs", {
                    "task_id": "T1", "role": "insight-executor", field: value})[0], 400)
        start.assert_not_called()

    def test_local_ai_failure_has_bounded_safe_recovery_response(self):
        with patch.object(panel, "_assistant_tool", side_effect=sqlite3.OperationalError("SECRET_ERROR_CANARY")):
            code, response = self.request("GET", "/api/ai/observability")
        self.assertEqual(code, 503)
        self.assertNotIn("SECRET_ERROR_CANARY", json.dumps(response))
        self.assertIn("повторите чтение", response["error"])

    def test_observability_and_evals_are_actual_local_results(self):
        code, usage = self.request("GET", "/api/ai/observability")
        self.assertEqual(code, 200)
        self.assertIn("provenance", usage)
        self.assertIn("totals", usage)
        code, report = self.request("GET", "/api/ai/evals")
        self.assertEqual(code, 200)
        self.assertIn("provenance", report)

    def test_real_report_artifact_supplies_result_and_context_without_acceptance(self):
        from src.agent_run_report import write_report
        self.save_run()
        report = {"summary": "Проверен локальный инструмент", "changed_files": ["src/example.py"],
                  "checks": [{"command": "python -m unittest tests.test_example", "exit_code": 0, "status": "passed"}],
                  "external_actions": [{"id": "ord_123", "kind": "demo-order", "status": "unknown"}],
                  "next_step": "Сверить ord_123 перед повтором"}
        write_report(self.root, "run_T1_result", "T1", report)
        code, result = self.request("GET", "/api/run/result?id=run_T1_result")
        self.assertEqual(code, 200)
        self.assertEqual(result["summary"], report["summary"])
        self.assertEqual(result["summary_quality"], "reported")
        self.assertFalse(result["acceptance_verified"])
        self.assertEqual(result["external_actions"][0]["status"], "unknown")
        self.assertEqual(result["report_source"]["quality"], "reported")
        code, context = self.request("GET", "/api/run/context?id=run_T1_result")
        self.assertEqual(code, 200)
        self.assertIn("ord_123", context["markdown"])
        self.assertIn("tests.test_example", context["markdown"])
        self.assertFalse(context["session_resumed"])
        run = panel._get_run(self.root, "run_T1_result", check_alive=False)
        self.assertEqual(sum(event["type"] == "run.report" for event in run["events"]), 1)

    def test_invalid_report_cannot_inject_or_confirm_result(self):
        self.save_run()
        directory = self.root / "data" / "run-results"
        directory.mkdir()
        (directory / "run_T1_result.json").write_text(json.dumps({
            "schema_version": 1, "run_id": "run_T1_result", "task_id": "OTHER",
            "quality": "verified", "acceptance_verified": True,
            "result": {"summary": "FORGED_RESULT_CANARY"}}), encoding="utf-8")
        code, result = self.request("GET", "/api/run/result?id=run_T1_result")
        self.assertEqual(code, 200)
        self.assertNotIn("FORGED_RESULT_CANARY", json.dumps(result))
        self.assertFalse(result["acceptance_verified"])
        self.assertIn("сверка", result["report_warning"])

    def test_launcher_requests_bound_structured_report(self):
        script = self.root / "ops" / "agent-rotate.ps1"
        script.write_text("# test launcher", encoding="utf-8")
        with patch.object(panel, "_pwsh", return_value="powershell"), patch.object(panel.subprocess, "Popen") as spawn:
            spawn.return_value.pid = 123
            panel._spawn_agent_process(self.root, "codex", "insight-executor", "T1", run_id="run_T1_report")
        args = spawn.call_args.args[0]
        instruction = args[args.index("-Prompt") + 1]
        self.assertIn("--run-id run_T1_report --task-id T1", instruction)
        self.assertIn("src.agent_run_report", instruction)
        self.assertIn("unknown", instruction)


if __name__ == "__main__":
    unittest.main()
