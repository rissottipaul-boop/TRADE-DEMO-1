"""Приёмка локального HTTP-пульта без запуска движка и Muse."""
from __future__ import annotations

from http.client import HTTPConnection
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import threading
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from src import control_panel as panel


BOARD = """# Доска

| ID | Задача | Агент | Статус | Зависит от | Критерий готовности | Заметки |
| --- | --- | --- | --- | --- | --- | --- |
| T1 | Проверить проект | Insight Executor | ready | — | Проверено | — |
| T2 | Запустить движок | Ops Sentinel | in-progress Sentinel 12:00 | T1 | Зелёный | — |
"""


class StateTests(unittest.TestCase):
    def setUp(self):
        self.temp = TemporaryDirectory()
        self.root = Path(self.temp.name)
        (self.root / "ops" / "delegations" / "outbox").mkdir(parents=True)
        (self.root / "ops" / "board.md").write_text(BOARD, encoding="utf-8")
        (self.root / "ops" / "agent-routing.json").write_text(
            json.dumps({"runtimes": {"muse": {"default_model": "model"}}}), encoding="utf-8")

    def tearDown(self):
        self.temp.cleanup()

    def test_state_reads_project_sources_and_omits_muse_output(self):
        result = {"id": "d1", "status": "done", "output_tail": "private prompt", "exit_code": 0}
        (self.root / "ops" / "delegations" / "outbox" / "d1.json").write_text(json.dumps(result))
        with patch.object(panel, "build_snapshot", return_value={"risk": {"equity": 100}}), \
             patch.object(panel, "_engine", return_value={"state": "жив"}):
            view = panel.state(self.root)
        self.assertEqual(view["board"]["counts"]["ready"], 1)
        self.assertEqual(view["runtimes"]["muse"]["default_model"], "model")
        self.assertEqual(view["delegation"]["results"][0]["status"], "done")
        self.assertNotIn("private prompt", json.dumps(view))

    def test_muse_jobs_show_stage_and_provenance_without_prompt(self):
        base = self.root / "ops" / "delegations"
        for folder in ("inbox", "processing", "done"):
            (base / folder).mkdir()
        queued = {"id": "d1", "from": "codex", "role": "insight-executor",
                  "prompt": "private inbox prompt", "created": "2026-09-30T12:00:00+05:00"}
        processing = {"id": "d2", "from": "claude", "role": "crypto-insight-hunter",
                      "prompt": "private processing prompt"}
        finished = {"id": "d3", "from": "antigravity", "role": "insight-executor",
                    "prompt": "private done prompt"}
        (base / "inbox" / "d1.json").write_text(json.dumps(queued))
        (base / "processing" / "d2.json").write_text(json.dumps(processing))
        (base / "done" / "d3.json").write_text(json.dumps(finished))
        (base / "outbox" / "d3.json").write_text(json.dumps({"id": "d3", "status": "done",
            "output_tail": "private output", "finished": "2026-09-30T12:05:00+05:00", "exit_code": 0}))
        jobs = panel._queue(self.root)["jobs"]
        self.assertEqual([job["state"] for job in jobs], ["done", "processing-unverified", "queued"])
        self.assertEqual(jobs[0]["from"], "antigravity")
        self.assertEqual(jobs[0]["provenance"], "ops/delegations/outbox/d3.json")
        self.assertEqual(jobs[0]["cost_quality"], "unknown")
        self.assertNotIn("private", json.dumps(jobs))

    def test_observed_runs_include_muse_with_file_stage_quality(self):
        base = self.root / "ops" / "delegations"
        (base / "processing").mkdir()
        (base / "processing" / "d-stage.json").write_text(json.dumps({
            "id": "d-stage", "role": "review", "prompt": "private prompt",
            "created": "2026-09-30T12:00:00+05:00"}), encoding="utf-8")
        observed = panel._observed_runs(self.root)
        self.assertEqual(len(observed), 1)
        self.assertEqual(observed[0]["id"], "d-stage")
        self.assertEqual(observed[0]["status"], "processing-unverified")
        self.assertEqual(observed[0]["state_quality"], "file-stage")
        self.assertFalse(observed[0]["managed"])
        self.assertIsNone(observed[0]["cost"]["usd"])
        self.assertNotIn("private prompt", json.dumps(observed))

    def test_observed_local_run_is_projection_without_internal_fields(self):
        panel._save_run(self.root, {
            "id": "run_T1_projection", "task_id": "T1", "role": "insight-executor",
            "runtime": "codex", "status": "completed", "prompt": "private prompt",
            "events": [{"cursor": 1, "type": "run.progress", "data": "private output"}],
            "internal_note": "private note", "cost": {"usd": None, "quality": "unknown"},
        })
        observed = panel._observed_runs(self.root)
        self.assertEqual(observed[0]["id"], "run_T1_projection")
        self.assertEqual(observed[0]["source_kind"], "panel")
        self.assertTrue(observed[0]["managed"])
        self.assertNotIn("private", json.dumps(observed))
        self.assertNotIn("events", observed[0])
        self.assertNotIn("prompt", observed[0])

    def test_only_allowlisted_actions_and_pause_flag(self):
        with self.assertRaises(ValueError):
            panel.action(self.root, "engine.reset")
        result = panel.action(self.root, "delegation.pause")
        self.assertTrue(result["ok"])
        self.assertTrue((self.root / "ops" / "delegations" / "PAUSED").exists())

    def test_task_detail_requires_unique_id_and_reports_dependencies(self):
        task = panel._task_detail(self.root, "T2")
        self.assertEqual(task["criterion"], "Зелёный")
        self.assertEqual(task["deps"], ["T1"])
        self.assertFalse(task["eligible"])
        with self.assertRaisesRegex(ValueError, "не найдена"):
            panel._task_detail(self.root, "MISSING")
        (self.root / "ops" / "board.md").write_text(BOARD + BOARD.splitlines()[-1] + "\n", encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "Дублирующийся"):
            panel._task_detail(self.root, "T2")

    def test_ambiguous_dependency_is_not_offered_as_ready(self):
        extra = "| T3 | Завершить | Insight Executor | ready | T1 | Проверено | — |\n"
        (self.root / "ops" / "board.md").write_text(BOARD + BOARD.splitlines()[-2] + "\n" + extra, encoding="utf-8")
        view = panel._board(self.root)
        self.assertIn("T1", view["duplicates"])
        self.assertNotIn("T3", view["ready_ids"])
        self.assertFalse(panel._task_detail(self.root, "T3")["eligible"])

    def test_plan_calls_read_only_script_and_filters_command_arguments(self):
        registry = {"runtimes": {"muse": {}}, "roles": {"insight-executor": {}}}
        (self.root / "ops" / "agent-routing.json").write_text(json.dumps(registry), encoding="utf-8")
        (self.root / "ops" / "agent-rotate.ps1").touch()
        raw = {"selected": "muse", "candidates": [{"agent": "muse", "model": "m",
               "available": True, "guard_status": "unverified", "arguments": ["secret prompt"]}]}
        with patch.object(panel, "_pwsh", return_value="pwsh"), \
             patch.object(panel.subprocess, "run", return_value=SimpleNamespace(
                 returncode=0, stdout=json.dumps(raw).encode(), stderr=b"")) as run:
            plan = panel._agent_plan(self.root, "T1", "insight-executor", "muse")
        args = run.call_args.args[0]
        self.assertEqual(args[4:], ["-Plan", "-Role", "insight-executor", "-TaskId", "T1", "-Agent", "muse"])
        self.assertFalse(plan["launch_enabled"])
        self.assertNotIn("secret prompt", json.dumps(plan))
        with self.assertRaisesRegex(ValueError, "Неизвестная роль"):
            panel._agent_plan(self.root, "T1", "made-up", "muse")

    def test_handoff_preserves_board_context_and_git_provenance(self):
        git = {"head": "abc123", "branch": "feature/control", "changes": [" M src/example.py"],
               "changes_truncated": False}
        events = [{"ts": "2026-09-30T12:00:00Z", "task": "T2", "agent": "muse",
                   "event": "start", "exit_code": 0}]
        with patch.object(panel, "_git_snapshot", return_value=git), \
             patch.object(panel, "_rotation", return_value=events):
            packet = panel._handoff(self.root, "T2")
        self.assertEqual(packet["git"]["head"], "abc123")
        self.assertEqual(packet["rotation"], events)
        self.assertIn("Зелёный", packet["markdown"])
        self.assertIn("T1: ready", packet["markdown"])
        self.assertIn("feature/control", packet["markdown"])
        self.assertIn("M src/example.py", packet["markdown"])
        self.assertIn("Пакет не создаёт claim", packet["markdown"])
        self.assertEqual(len(packet["board_sha256"]), 64)

    def test_handoff_redacts_secret_values_from_board_notes(self):
        board = BOARD.replace("| Зелёный | — |", "| Зелёный | api_key=private-value token: abc123 |")
        (self.root / "ops" / "board.md").write_text(board, encoding="utf-8")
        with patch.object(panel, "_git_snapshot", return_value={"head": "abc", "branch": "main",
                                                            "changes": [], "changes_truncated": False}):
            packet = panel._handoff(self.root, "T2")
        self.assertEqual(packet["redactions"], 2)
        self.assertNotIn("private-value", packet["markdown"])
        self.assertNotIn("abc123", packet["markdown"])
        self.assertIn("[СКРЫТО]", packet["markdown"])


class RunManagerTests(unittest.TestCase):
    def setUp(self):
        self.temp = TemporaryDirectory()
        self.root = Path(self.temp.name)
        (self.root / "ops").mkdir(parents=True)
        (self.root / "ops" / "board.md").write_text(BOARD, encoding="utf-8")
        registry = {
            "runtimes": {
                "codex": {"default_model": "gpt-6-astra", "guard_status": "adapter-ready"},
                "claude": {"default_model": "opus", "guard_status": "configured"},
                "gemini": {"default_model": "pro", "guard_status": "configured"},
                "muse": {"default_model": "muse-spark-1.3", "guard_status": "configured"},
            },
            "roles": {
                "insight-executor": {"priority": ["codex", "claude", "gemini", "muse"]},
            },
        }
        (self.root / "ops" / "agent-routing.json").write_text(json.dumps(registry), encoding="utf-8")

    def tearDown(self):
        self.temp.cleanup()

    def test_runtime_capabilities_reports_matrix_for_four_clients(self):
        caps = panel.runtime_capabilities(self.root)
        # start скрыт без E2E-подтверждения; events — курсорные события локальной записи.
        self.assertEqual(set(caps["codex"]), {"status", "events"})
        self.assertEqual(set(caps["claude"]), {"status", "events"})
        self.assertEqual(set(caps["gemini"]), {"status", "events"})
        self.assertEqual(set(caps["muse"]), {"status", "events"})
        for runtime in ("codex", "claude", "gemini", "muse"):
            self.assertNotIn("steer", caps[runtime])
            self.assertNotIn("cancel", caps[runtime])
        provenance = panel.capabilities_provenance()
        self.assertIn("launch-e2e.json", json.dumps(provenance))

    def test_launch_requires_protected_e2e_evidence(self):
        registry = json.loads((self.root / "ops" / "agent-routing.json").read_text(encoding="utf-8"))
        registry["runtimes"]["codex"]["guard_status"] = "e2e-verified"
        (self.root / "ops" / "agent-routing.json").write_text(json.dumps(registry), encoding="utf-8")
        self.assertFalse(panel._guard_launch_ready(self.root, "codex"))
        hooks = self.root / "ops" / "hooks"
        hooks.mkdir()
        (hooks / "launch-e2e.json").write_text(json.dumps({"schema_version": 1,
            "clients": {"codex": {"verified": True}}}), encoding="utf-8")
        self.assertTrue(panel._guard_launch_ready(self.root, "codex"))
        self.assertIn("start", panel.runtime_capabilities(self.root)["codex"])

    def test_guard_observation_only_exposes_client_counts(self):
        log = self.root / "data" / "guard.log"
        log.parent.mkdir()
        log.write_text("\n".join([
            json.dumps({"ts": "2026-09-30T12:00:00Z", "decision": "deny",
                        "client": "codex", "snippet": "sensitive-canary"}),
            json.dumps({"ts": "2026-09-30T12:01:00Z", "decision": "deny",
                        "client": "muse", "reason": "private reason"}),
            json.dumps({"decision": "deny", "client": {"bad": "shape"}}),
        ]), encoding="utf-8")
        view = panel._guard_observation(self.root)
        self.assertEqual(view["clients"]["codex"]["denies_in_tail"], 1)
        self.assertEqual(view["clients"]["muse"]["denies_in_tail"], 1)
        self.assertFalse(view["clients"]["codex"]["launch_ready"])
        self.assertNotIn("sensitive-canary", json.dumps(view))
        self.assertNotIn("private reason", json.dumps(view))

    def test_runs_idempotent_start_and_single_active_run(self):
        alive_map = {1111: True}
        with patch.object(panel, "_agent_plan", return_value={"selected": "codex", "launch_enabled": True}), \
             patch.object(panel, "_spawn_agent_process", return_value=1111) as spawn_mock, \
             patch.object(panel, "process_alive", side_effect=lambda pid: alive_map.get(pid, False)):
            run1 = panel._start_run(self.root, "T1", "insight-executor", "codex", idempotency_key="idemp-key-1")
            self.assertEqual(run1["status"], "running")
            self.assertEqual(run1["pid"], 1111)
            self.assertEqual(spawn_mock.call_count, 1)

            # Same idempotency key returns exact same run without spawning
            run2 = panel._start_run(self.root, "T1", "insight-executor", "codex", idempotency_key="idemp-key-1")
            self.assertEqual(run2["id"], run1["id"])
            self.assertEqual(spawn_mock.call_count, 1)

            # Different call without key for active task returns existing active run
            run3 = panel._start_run(self.root, "T1", "insight-executor", "codex")
            self.assertEqual(run3["id"], run1["id"])
            self.assertEqual(spawn_mock.call_count, 1)

            # Simulate process termination (process died)
            alive_map[1111] = False
            spawn_mock.return_value = 2222
            alive_map[2222] = True

            # Unknown outcome blocks replay until the operator reconciles it.
            with self.assertRaisesRegex(ValueError, "Итог прежнего запуска неизвестен"):
                panel._start_run(self.root, "T1", "insight-executor", "codex")
            self.assertEqual(spawn_mock.call_count, 1)

            recovered = panel._get_run(self.root, run1["id"], check_alive=False)
            self.assertEqual(recovered["status"], "unknown")

    def test_launcher_finish_event_completes_run_with_provenance(self):
        (self.root / "logs").mkdir()
        panel._save_run(self.root, {"id": "run_T1_ingest", "task_id": "T1",
            "role": "insight-executor", "runtime": "codex", "status": "running", "pid": 7777,
            "events": [{"cursor": 1, "type": "run.started",
                        "ts": "2026-10-03T04:00:00+00:00", "data": {}}]})
        (self.root / "logs" / "agent-rotate.log").write_text("\n".join([
            json.dumps({"ts": "2026-10-03T04:00:01+00:00", "agent": "codex", "event": "start",
                        "exit_code": 0, "run_id": "run_T1_ingest"}),
            json.dumps({"ts": "2026-10-03T04:05:00+00:00", "agent": "codex", "event": "finish",
                        "exit_code": 0, "run_id": "run_T1_ingest"}),
            json.dumps({"ts": "2026-10-03T04:06:00+00:00", "agent": "codex", "event": "finish",
                        "exit_code": 1, "run_id": "run_T9_foreign"}),
        ]) + "\n", encoding="utf-8")
        with patch.object(panel, "process_alive", return_value=False):
            loaded = panel._get_run(self.root, "run_T1_ingest")
        self.assertEqual(loaded["status"], "completed")
        self.assertEqual(loaded["exit_code"], 0)
        # Код выхода не доказывает критерий доски — качество фиксируется явно.
        self.assertEqual(loaded["result_quality"], "exit-code-only")
        types = [event["type"] for event in loaded["events"]]
        self.assertEqual(types, ["run.started", "run.progress", "run.completed"])
        self.assertEqual(loaded["events"][-1]["data"]["provenance"], "logs/agent-rotate.log")
        # Повторное чтение не дублирует события журнала (устойчивый курсор).
        again = panel._get_run(self.root, "run_T1_ingest")
        self.assertEqual(len(again["events"]), 3)

    def test_unknown_run_recovers_from_launcher_log_and_unblocks_restart(self):
        panel._save_run(self.root, {"id": "run_T1_lost", "task_id": "T1",
            "role": "insight-executor", "runtime": "codex", "status": "unknown",
            "started_at": "2026-10-03T03:00:00+00:00", "events": []})
        with patch.object(panel, "_agent_plan", return_value={"selected": "codex", "launch_enabled": True}), \
             patch.object(panel, "_spawn_agent_process", return_value=3333) as spawn_mock, \
             patch.object(panel, "process_alive", return_value=True):
            with self.assertRaisesRegex(ValueError, "Итог прежнего запуска неизвестен"):
                panel._start_run(self.root, "T1", "insight-executor", "codex")
            spawn_mock.assert_not_called()
            # Recovery: журнал лаунчера подтверждает исход — unknown становится failed.
            (self.root / "logs").mkdir()
            (self.root / "logs" / "agent-rotate.log").write_text(json.dumps({
                "ts": "2026-10-03T03:10:00+00:00", "agent": "codex", "event": "finish",
                "exit_code": 3, "run_id": "run_T1_lost"}) + "\n", encoding="utf-8")
            recovered = panel._get_run(self.root, "run_T1_lost")
            self.assertEqual(recovered["status"], "failed")
            self.assertEqual(recovered["exit_code"], 3)
            # Сверка состоялась — повтор задачи снова разрешён.
            run = panel._start_run(self.root, "T1", "insight-executor", "codex")
            self.assertEqual(run["status"], "running")
            self.assertEqual(spawn_mock.call_count, 1)
            self.assertEqual(spawn_mock.call_args.kwargs["run_id"], run["id"])
            self.assertIsNone(spawn_mock.call_args.kwargs["workdir"])

    def test_worktree_start_creates_lease_and_launches_isolated_copy(self):
        worktree = self.root.parent / "panel-test-worktrees" / "run_wt"
        def fake_prepare(root, task_id, run_id):
            return {"schema_version": 1, "task_id": task_id, "run_id": run_id,
                    "worktree": str(worktree), "branch": f"agent/{run_id}",
                    "base_commit": "abc123", "state": "active"}
        with patch.object(panel, "_agent_plan", return_value={"selected": "codex", "launch_enabled": True}), \
             patch.object(panel, "prepare_worktree", side_effect=fake_prepare) as prepare_mock, \
             patch.object(panel, "_spawn_agent_process", return_value=5555) as spawn_mock, \
             patch.object(panel, "process_alive", return_value=True):
            run = panel._start_run(self.root, "T1", "insight-executor", "codex",
                                   workspace="worktree")
        prepare_mock.assert_called_once_with(self.root, "T1", run["id"])
        self.assertEqual(spawn_mock.call_args.kwargs["workdir"], worktree)
        self.assertEqual(spawn_mock.call_args.kwargs["run_id"], run["id"])
        self.assertEqual(run["workspace"], "worktree")
        self.assertEqual(run["lease"]["state"], "active")
        self.assertEqual(run["lease"]["base_commit"], "abc123")
        # События и стоимость читаются из журнала внутри worktree — provenance явный.
        self.assertTrue(run["rotation_log"].endswith("logs/agent-rotate.log"))
        self.assertIn("run_wt", run["rotation_log"])
        self.assertEqual(run["cost"]["quality"], "unknown")
        self.assertIn(run["rotation_log"], run["cost"]["provenance"])
        with self.assertRaisesRegex(ValueError, "Неверный workspace"):
            panel._start_run(self.root, "T1", "insight-executor", "codex", workspace="shared")

    def test_worktree_lease_conflict_blocks_start_without_spawn(self):
        from src.worktree_lease import LeaseError
        with patch.object(panel, "_agent_plan", return_value={"selected": "codex", "launch_enabled": True}), \
             patch.object(panel, "prepare_worktree",
                          side_effect=LeaseError("Задача уже имеет lease; сверьте прежний запуск")), \
             patch.object(panel, "_spawn_agent_process") as spawn_mock:
            with self.assertRaisesRegex(ValueError, "Worktree lease недоступен"):
                panel._start_run(self.root, "T1", "insight-executor", "codex",
                                 workspace="worktree")
        spawn_mock.assert_not_called()

    def test_finished_worktree_run_releases_lease_without_deleting_worktree(self):
        (self.root / "logs").mkdir()
        panel._save_run(self.root, {"id": "run_T1_wt_done", "task_id": "T1",
            "role": "insight-executor", "runtime": "codex", "status": "running", "pid": 6666,
            "workspace": "worktree",
            "lease": {"task_id": "T1", "run_id": "run_T1_wt_done", "worktree": "C:/x",
                      "branch": "agent/run_T1_wt_done", "base_commit": "abc", "state": "active"},
            "events": []})
        (self.root / "logs" / "agent-rotate.log").write_text(json.dumps({
            "ts": "2026-10-03T05:00:00+00:00", "agent": "codex", "event": "finish",
            "exit_code": 0, "run_id": "run_T1_wt_done"}) + "\n", encoding="utf-8")
        released = {"task_id": "T1", "run_id": "run_T1_wt_done", "worktree": "C:/x",
                    "branch": "agent/run_T1_wt_done", "base_commit": "abc",
                    "state": "released", "released_at": "2026-10-03T05:00:01+00:00"}
        with patch.object(panel, "process_alive", return_value=False), \
             patch.object(panel, "release_lease", return_value=released) as release_mock:
            loaded = panel._get_run(self.root, "run_T1_wt_done")
        release_mock.assert_called_once_with(self.root, "T1", "run_T1_wt_done", state="released")
        self.assertEqual(loaded["status"], "completed")
        self.assertEqual(loaded["lease"]["state"], "released")
        self.assertEqual(loaded["lease"]["released_at"], "2026-10-03T05:00:01+00:00")

    def test_steer_is_unsupported_without_confirmed_client_protocol(self):
        panel._save_run(self.root, {"id": "run_T1_steer", "task_id": "T1",
            "runtime": "codex", "status": "running", "events": []})
        result = panel._steer_run(self.root, "run_T1_steer", "сначала проверь тесты")
        self.assertFalse(result["ok"])
        self.assertEqual(result["error"], "unsupported")
        self.assertEqual(panel._get_run(self.root, "run_T1_steer")["status"], "running")
        with self.assertRaisesRegex(ValueError, "Неверная инструкция"):
            panel._steer_run(self.root, "run_T1_steer", "  ")
        with self.assertRaisesRegex(ValueError, "не найден"):
            panel._steer_run(self.root, "run_missing_x", "текст")

    def test_native_claude_session_supplies_events_and_reported_cost(self):
        home = Path(self.temp.name) / "fake-home"
        slug = panel.native_sessions.claude_project_slug(str(self.root))
        session = home / ".claude" / "projects" / slug / "sess-native-1.jsonl"
        session.parent.mkdir(parents=True)
        session.write_text("\n".join([
            json.dumps({"type": "queue-operation", "sessionId": "sess-native-1",
                        "timestamp": "2026-10-03T04:00:05.000Z"}),
            json.dumps({"type": "user", "sessionId": "sess-native-1",
                        "timestamp": "2026-10-03T04:00:06.000Z",
                        "message": {"content": "NATIVE-PROMPT-CANARY"}}),
            json.dumps({"type": "assistant", "sessionId": "sess-native-1",
                        "timestamp": "2026-10-03T04:01:00.000Z",
                        "message": {"model": "claude-opus-5-5",
                                    "content": [{"type": "text", "text": "NATIVE-OUT-CANARY"}],
                                    "usage": {"input_tokens": 10, "output_tokens": 7}}}),
            json.dumps({"type": "cost-state", "sessionId": "sess-native-1",
                        "timestamp": "2026-10-03T04:02:00.000Z",
                        "modelUsage": {"claude-opus-5-5": {"costUSD": 1.25}}}),
        ]) + "\n", encoding="utf-8")
        panel._save_run(self.root, {"id": "run_T1_native", "task_id": "T1",
            "role": "insight-executor", "runtime": "claude", "status": "running", "pid": 8888,
            "started_at": "2026-10-03T04:00:00+00:00",
            "cost": {"usd": None, "quality": "unknown", "provenance": "нет источника"},
            "events": []})
        with patch.object(panel, "NATIVE_HOME", home), \
             patch.object(panel, "process_alive", return_value=True):
            loaded = panel._get_run(self.root, "run_T1_native")
        self.assertEqual(loaded["native"]["session_id"], "sess-native-1")
        native_events = [e for e in loaded["events"]
                         if isinstance(e.get("data"), dict) and e["data"].get("quality") == "native"]
        kinds = [e["data"].get("native_type") for e in native_events]
        self.assertEqual(kinds, ["session_linked", "user", "assistant", "cost-state"])
        for event in native_events:
            self.assertEqual(event["data"].get("provenance", str(session)), str(session))
        # Стоимость — фактическая из файла сессии, с качеством reported и provenance.
        self.assertEqual(loaded["cost"]["usd"], 1.25)
        self.assertEqual(loaded["cost"]["quality"], "reported")
        self.assertEqual(loaded["cost"]["provenance"], str(session))
        # Текст prompt/вывода из нативного журнала не попадает в запись.
        self.assertNotIn("CANARY", json.dumps(loaded, ensure_ascii=False))
        # Повторное чтение не дублирует события (курсор по строкам).
        with patch.object(panel, "NATIVE_HOME", home), \
             patch.object(panel, "process_alive", return_value=True):
            again = panel._get_run(self.root, "run_T1_native")
        self.assertEqual(len(again["events"]), len(loaded["events"]))

    def test_ambiguous_native_sessions_keep_launcher_fallback(self):
        home = Path(self.temp.name) / "fake-home-2"
        slug = panel.native_sessions.claude_project_slug(str(self.root))
        project = home / ".claude" / "projects" / slug
        project.mkdir(parents=True)
        for name in ("a", "b"):
            (project / f"sess-{name}.jsonl").write_text(json.dumps({
                "type": "queue-operation", "sessionId": f"sess-{name}",
                "timestamp": "2026-10-03T04:00:05.000Z"}) + "\n", encoding="utf-8")
        panel._save_run(self.root, {"id": "run_T1_ambig", "task_id": "T1",
            "role": "insight-executor", "runtime": "claude", "status": "running", "pid": 8889,
            "started_at": "2026-10-03T04:00:00+00:00",
            "cost": {"usd": None, "quality": "unknown", "provenance": "нет источника"},
            "events": []})
        with patch.object(panel, "NATIVE_HOME", home), \
             patch.object(panel, "process_alive", return_value=True):
            loaded = panel._get_run(self.root, "run_T1_ambig")
        # Двусмысленная привязка не угадывается: события и стоимость остаются фолбэком.
        self.assertNotIn("native", loaded)
        self.assertEqual(loaded["cost"]["quality"], "unknown")

    def test_alive_worktree_run_extends_lease_heartbeat(self):
        from src.worktree_lease import heartbeat as real_heartbeat  # noqa: F401
        lease_dir = self.root / "data" / "worktree-leases"
        lease_dir.mkdir(parents=True)
        (lease_dir / "T1.json").write_text(json.dumps({
            "schema_version": 1, "task_id": "T1", "run_id": "run_T1_beat",
            "worktree": "C:/x", "branch": "agent/run_T1_beat", "base_commit": "abc",
            "state": "active", "created_at": "2026-10-03T04:00:00+00:00",
            "heartbeat_at": "2026-10-03T04:00:00+00:00"}), encoding="utf-8")
        panel._save_run(self.root, {"id": "run_T1_beat", "task_id": "T1",
            "role": "insight-executor", "runtime": "codex", "status": "running", "pid": 9001,
            "workspace": "worktree",
            "lease": {"task_id": "T1", "run_id": "run_T1_beat", "worktree": "C:/x",
                      "branch": "agent/run_T1_beat", "base_commit": "abc", "state": "active",
                      "heartbeat_at": "2026-10-03T04:00:00+00:00"},
            "events": []})
        with patch.object(panel, "process_alive", return_value=True):
            loaded = panel._get_run(self.root, "run_T1_beat")
        stored = json.loads((lease_dir / "T1.json").read_text(encoding="utf-8"))
        self.assertNotEqual(stored["heartbeat_at"], "2026-10-03T04:00:00+00:00")
        self.assertEqual(loaded["lease"]["heartbeat_at"], stored["heartbeat_at"])
        # Мёртвый процесс heartbeat не продлевает: lease честно стареет.
        with patch.object(panel, "process_alive", return_value=False):
            panel._get_run(self.root, "run_T1_beat")
        after_dead = json.loads((lease_dir / "T1.json").read_text(encoding="utf-8"))
        self.assertEqual(after_dead["heartbeat_at"], stored["heartbeat_at"])

    def test_run_cancel_never_kills_unverified_pid(self):
        run = {"id": "run_T1_test", "runtime": "codex", "task_id": "T1",
               "status": "running", "pid": 4444, "events": []}
        panel._save_run(self.root, run)
        with patch.object(panel, "process_alive", return_value=True), \
             patch.object(panel.subprocess, "run") as process_call:
            result = panel._cancel_run(self.root, run["id"])
        self.assertEqual(result["error"], "unsupported")
        self.assertEqual(panel._get_run(self.root, run["id"], check_alive=False)["status"], "running")
        process_call.assert_not_called()

    def test_idempotency_key_cannot_return_another_task_run(self):
        other = {"id": "run_T2_old", "task_id": "T2", "role": "insight-executor",
                 "runtime": "codex", "status": "completed", "idempotency_key": "same-key",
                 "started_at": "2020-01-01T00:00:00Z", "events": []}
        panel._save_run(self.root, other)
        with self.assertRaisesRegex(ValueError, "idempotency_key уже использован"):
            panel._start_run(self.root, "T1", "insight-executor", "codex", idempotency_key="same-key")

    def test_unknown_run_outside_display_limit_blocks_replay(self):
        panel._save_run(self.root, {"id": "run_T1_old", "task_id": "T1",
            "role": "insight-executor", "runtime": "codex", "status": "unknown",
            "started_at": "2020-01-01T00:00:00Z", "events": []})
        for index in range(51):
            panel._save_run(self.root, {"id": f"run_T2_{index}", "task_id": "T2",
                "status": "completed", "started_at": f"2025-01-01T00:{index:02}:00Z",
                "events": []})
        with self.assertRaisesRegex(ValueError, "Итог прежнего запуска неизвестен"):
            panel._start_run(self.root, "T1", "insight-executor", "codex")

    def test_runs_redact_secrets_in_events_and_details(self):
        run = {
            "id": "run_T1_test_redact",
            "task_id": "T1",
            "role": "insight-executor",
            "runtime": "codex",
            "status": "running",
            "events": [
                {"cursor": 1, "type": "run.progress", "data": "api_key=sk-12345678901234567890 with Bearer supersecrettoken"},
            ],
        }
        panel._save_run(self.root, run)
        loaded = panel._get_run(self.root, "run_T1_test_redact", check_alive=False)
        event_str = loaded["events"][0]["data"]
        self.assertNotIn("sk-12345678901234567890", event_str)
        self.assertNotIn("supersecrettoken", event_str)
        self.assertIn("[СКРЫТО]", event_str)

    def test_run_events_cursor_is_stable_across_pages(self):
        run = {"id": "run_T1_events", "task_id": "T1", "status": "completed",
               "events": [{"cursor": index, "type": "run.progress", "ts": f"time-{index}"}
                          for index in (1, 2, 3)]}
        panel._save_run(self.root, run)
        first = panel._run_events(self.root, run["id"], after=1, limit=1)
        self.assertEqual([e["cursor"] for e in first["events"]], [2])
        self.assertEqual(first["next_cursor"], 2)
        self.assertTrue(first["has_more"])
        second = panel._run_events(self.root, run["id"], after=first["next_cursor"])
        self.assertEqual([e["cursor"] for e in second["events"]], [3])
        self.assertFalse(second["has_more"])
        with self.assertRaisesRegex(ValueError, "Неверный курсор"):
            panel._run_events(self.root, run["id"], after=-1)


class HttpTests(unittest.TestCase):
    def setUp(self):
        self.temp = TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.server = panel.PanelServer(("127.0.0.1", 0), root=self.root, token="test-secret")
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)
        self.temp.cleanup()

    def request(self, method, path, body=None, headers=None):
        connection = HTTPConnection("127.0.0.1", self.server.server_port, timeout=3)
        connection.request(method, path, body=body, headers=headers or {})
        response = connection.getresponse()
        content = response.read().decode("utf-8")
        status = response.status
        connection.close()
        return status, content

    def test_page_requires_token_and_serves_local_ui(self):
        self.assertEqual(self.request("GET", "/")[0], 403)
        code, body = self.request("GET", "/?token=test-secret")
        self.assertEqual(code, 200)
        self.assertIn("Все процессы", body)
        self.assertIn("test-secret", body)
        self.assertEqual(self.request("GET", "/app.js")[0], 200)

    def test_api_rejects_missing_token_foreign_origin_and_unknown_action(self):
        self.assertEqual(self.request("GET", "/api/state")[0], 403)
        headers = {"X-Control-Token": "test-secret", "Content-Type": "application/json"}
        self.assertEqual(self.request("POST", "/api/action", b'{"action":"engine.reset"}', headers)[0], 400)
        foreign = dict(headers, Origin="https://example.com")
        self.assertEqual(self.request("POST", "/api/action", b'{"action":"engine.stop"}', foreign)[0], 403)
        bad_host = dict(headers, Host="evil.example")
        self.assertEqual(self.request("GET", "/api/state", headers=bad_host)[0], 403)

    def test_http_action_calls_only_allowlisted_operation(self):
        headers = {"X-Control-Token": "test-secret", "Content-Type": "application/json"}
        with patch.object(panel, "action", return_value={"ok": True, "output": "done"}) as invoke:
            code, body = self.request("POST", "/api/action", b'{"action":"engine.stop"}', headers)
        self.assertEqual(code, 200)
        self.assertEqual(json.loads(body)["output"], "done")
        invoke.assert_called_once_with(self.root, "engine.stop")
        audit = (self.root / "logs" / "control-panel-actions.jsonl").read_text(encoding="utf-8")
        self.assertIn('"action": "engine.stop"', audit)
        self.assertNotIn("test-secret", audit)

    def test_task_and_plan_api_require_token_and_validate_request(self):
        (self.root / "ops").mkdir()
        (self.root / "ops" / "board.md").write_text(BOARD, encoding="utf-8")
        headers = {"X-Control-Token": "test-secret", "Content-Type": "application/json"}
        self.assertEqual(self.request("GET", "/api/task?id=T1")[0], 403)
        code, body = self.request("GET", "/api/task?id=T1", headers=headers)
        self.assertEqual(code, 200)
        self.assertEqual(json.loads(body)["criterion"], "Проверено")
        self.assertEqual(self.request("GET", "/api/task?id=BAD", headers=headers)[0], 400)
        self.assertEqual(self.request("POST", "/api/agent/plan", b'{}', headers)[0], 400)
        with patch.object(panel, "_agent_plan", return_value={"launch_enabled": False}) as invoke:
            code, body = self.request("POST", "/api/agent/plan",
                                      b'{"task_id":"T1","role":"insight-executor","agent":"auto"}', headers)
        self.assertEqual(code, 200)
        self.assertFalse(json.loads(body)["launch_enabled"])
        invoke.assert_called_once_with(self.root, "T1", "insight-executor", "auto")
        self.assertEqual(self.request("GET", "/api/handoff?id=T1")[0], 403)
        code, body = self.request("GET", "/api/handoff?id=T1", headers=headers)
        self.assertEqual(code, 200)
        self.assertIn("Проверено", json.loads(body)["markdown"])
        self.assertEqual(self.request("GET", "/api/handoff?id=BAD", headers=headers)[0], 400)

    def test_runs_and_capabilities_http_api(self):
        headers = {"X-Control-Token": "test-secret", "Content-Type": "application/json"}
        # Capabilities
        code, body = self.request("GET", "/api/capabilities", headers=headers)
        self.assertEqual(code, 200)
        caps = json.loads(body)["capabilities"]
        self.assertIn("codex", caps)
        self.assertNotIn("cancel", caps["codex"])
        self.assertNotIn("start", caps["codex"])

        # Runs listing
        code, body = self.request("GET", "/api/runs", headers=headers)
        self.assertEqual(code, 200)
        self.assertIsInstance(json.loads(body)["runs"], list)

        # Start run via POST /api/runs
        fake_run = {"id": "run_T1_http", "status": "running", "task_id": "T1"}
        with patch.object(panel, "_start_run", return_value=fake_run) as start_mock:
            code, body = self.request("POST", "/api/runs",
                                      b'{"task_id":"T1","role":"insight-executor","agent":"codex"}', headers)
        self.assertEqual(code, 200)
        self.assertEqual(json.loads(body)["run"]["id"], "run_T1_http")
        start_mock.assert_called_once_with(self.root, "T1", "insight-executor", "codex",
                                           idempotency_key=None, model=None, workspace="checkout")

        # Get run by ID
        with patch.object(panel, "_get_run", return_value=fake_run):
            code, body = self.request("GET", "/api/run?id=run_T1_http", headers=headers)
        self.assertEqual(code, 200)
        self.assertEqual(json.loads(body)["id"], "run_T1_http")

        # Cancel run via POST /api/run/cancel
        cancel_res = {"ok": True, "status": "cancelled", "run": fake_run}
        with patch.object(panel, "_cancel_run", return_value=cancel_res) as cancel_mock:
            code, body = self.request("POST", "/api/run/cancel", b'{"run_id":"run_T1_http"}', headers)
        self.assertEqual(code, 200)
        self.assertTrue(json.loads(body)["ok"])
        cancel_mock.assert_called_once_with(self.root, "run_T1_http")

    def test_steer_http_returns_unsupported_without_capability(self):
        panel._save_run(self.root, {"id": "run_T1_http_steer", "task_id": "T1",
            "runtime": "codex", "status": "running", "events": []})
        headers = {"X-Control-Token": "test-secret", "Content-Type": "application/json"}
        self.assertEqual(self.request("POST", "/api/run/steer", b'{"text":"x"}')[0], 403)
        code, body = self.request("POST", "/api/run/steer",
                                  b'{"run_id":"run_T1_http_steer","text":"pause"}', headers)
        self.assertEqual(code, 400)
        payload = json.loads(body)
        self.assertEqual(payload["error"], "unsupported")
        self.assertEqual(self.request("POST", "/api/run/steer", b'{"text":"x"}', headers)[0], 400)
        self.assertEqual(self.request("POST", "/api/run/steer",
                                      b'{"run_id":"run_missing_x","text":"x"}', headers)[0], 404)

    def test_runs_http_includes_muse_without_prompt_or_output(self):
        outbox = self.root / "ops" / "delegations" / "outbox"
        outbox.mkdir(parents=True)
        (outbox / "d-safe.json").write_text(json.dumps({"id": "d-safe", "status": "done",
            "role": "review", "created": "2026-09-30T12:00:00Z", "exit_code": 0,
            "prompt": "private prompt", "output_tail": "private output"}), encoding="utf-8")
        code, body = self.request("GET", "/api/runs", headers={"X-Control-Token": "test-secret"})
        self.assertEqual(code, 200)
        runs = json.loads(body)["runs"]
        self.assertEqual(len(runs), 1)
        self.assertEqual(runs[0]["id"], "d-safe")
        self.assertFalse(runs[0]["managed"])
        self.assertEqual(runs[0]["state_quality"], "file-stage")
        self.assertNotIn("private", body)

    def test_events_http_requires_token_and_valid_cursor(self):
        panel._save_run(self.root, {"id": "run_T1_http_events", "status": "completed",
            "events": [{"cursor": 1, "type": "run.started", "ts": "2026-09-30T12:00:00Z"}]})
        self.assertEqual(self.request("GET", "/api/run/events?id=run_T1_http_events")[0], 403)
        headers = {"X-Control-Token": "test-secret"}
        code, body = self.request("GET", "/api/run/events?id=run_T1_http_events&after=0", headers=headers)
        self.assertEqual(code, 200)
        self.assertEqual(json.loads(body)["next_cursor"], 1)
        self.assertEqual(self.request("GET", "/api/run/events?id=run_T1_http_events&after=-1", headers=headers)[0], 400)
        self.assertEqual(self.request("GET", "/api/run/events?id=d-muse", headers=headers)[0], 404)

    def test_worktree_leases_http_is_read_only_and_authenticated(self):
        self.assertEqual(self.request("GET", "/api/worktree-leases")[0], 403)
        with patch.object(panel, "inspect_leases", return_value=[{
            "task_id": "T1", "run_id": "run_T1_abc", "registered": False,
            "owner_verified": False}]) as inspect:
            code, body = self.request("GET", "/api/worktree-leases",
                                      headers={"X-Control-Token": "test-secret"})
        self.assertEqual(code, 200)
        self.assertEqual(json.loads(body)["leases"][0]["task_id"], "T1")
        inspect.assert_called_once_with(self.root)

    def test_direct_start_cannot_bypass_plan_guard(self):
        (self.root / "ops").mkdir()
        (self.root / "ops" / "board.md").write_text(BOARD, encoding="utf-8")
        (self.root / "ops" / "agent-routing.json").write_text(json.dumps({
            "runtimes": {"codex": {"guard_status": "adapter-required"}},
            "roles": {"insight-executor": {}}}), encoding="utf-8")
        headers = {"X-Control-Token": "test-secret", "Content-Type": "application/json"}
        with patch.object(panel, "_agent_plan", return_value={"selected": "codex",
                "launch_enabled": False, "reason": "guard не проверен"}), \
             patch.object(panel, "_spawn_agent_process") as spawn:
            code, body = self.request("POST", "/api/runs",
                b'{"task_id":"T1","role":"insight-executor","agent":"codex"}', headers)
        self.assertEqual(code, 400)
        self.assertIn("guard", json.loads(body)["error"])
        spawn.assert_not_called()


if __name__ == "__main__":
    unittest.main()
