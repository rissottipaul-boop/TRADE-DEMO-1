"""Негативные сценарии read-only помощника и настоящие временные Git checkout."""
import json
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest import mock

from src import agent_assistant as assistant


BOARD = ("| ID | Задача | Агент | Статус | Зависит от | Критерий готовности | Заметки |\n"
         "| --- | --- | --- | --- | --- | --- | --- |\n"
         "| T1 | Исправить панель | Insight Executor | ready | — | Показывает результат и все тесты зелёные | Claim отсутствует |\n")


class AssistantFixture(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name) / "project"
        (self.root / "ops").mkdir(parents=True)
        (self.root / "ops" / "board.md").write_text(BOARD, encoding="utf-8")

    def put(self, name, text):
        path = self.root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
        return path

    def git(self, *args):
        output = subprocess.run(["git", *args], cwd=self.root, capture_output=True, check=True)
        return output.stdout.decode("utf-8").strip()

    def init_git(self):
        self.git("init", "--quiet")
        self.git("config", "user.name", "Fixture")
        self.git("config", "user.email", "fixture@example.invalid")
        self.put("src/code.py", "answer = 1\n")
        self.git("add", ".")
        self.git("commit", "--quiet", "-m", "fixture")

    def run_record(self, **fields):
        run = {"id": "run_T1_test", "task_id": "T1", "runtime": "codex", "role": "insight-executor",
               "model": "reported-model", "status": "completed", "exit_code": 0, **fields}
        self.put("data/runs/run_T1_test.json", json.dumps(run, ensure_ascii=False))
        return run


class DraftTests(AssistantFixture):
    def test_roles_explained_and_no_launch(self):
        for description, expected in (("Исправить UI панели", "insight-executor"),
                                      ("Исследовать обзор API и источники", "crypto-insight-hunter"),
                                      ("Инцидент: завис движок", "ops-sentinel")):
            role = assistant.recommend_role(description)
            self.assertEqual(role["role"], expected)
            self.assertTrue(role["reason"])
            self.assertFalse(role["launch_authorized"])
            self.assertEqual(role["confidence"], "heuristic")

    def test_draft_contains_goal_specific_criterion_and_does_not_write(self):
        board = (self.root / "ops/board.md").read_bytes()
        packet = assistant.task_draft("Панель должна показывать stage после события")
        self.assertIn("stage", packet["criterion_text"])
        self.assertGreater(len(packet["acceptance_criteria"]), 2)
        self.assertEqual(packet["source_of_truth"], "ops/board.md")
        self.assertFalse(packet["board_written"])
        self.assertIn("Критерий", packet["markdown"])
        self.assertEqual((self.root / "ops/board.md").read_bytes(), board)

    def test_protected_change_requires_human_decision(self):
        for description in ("Изменить guard", "Увеличить лимит риска", "Сброс kill-switch", "Удалить состояние .db"):
            with self.subTest(description=description):
                packet = assistant.task_draft(description)
                self.assertEqual(packet["suggested_status"], "needs-user")
                self.assertTrue(packet["requires_human_decision"])

    def test_invalid_and_oversize_description(self):
        for value in (None, "", " \n", "x\0x", "x" * 4001, {"shell": "x"}):
            with self.assertRaises(ValueError):
                assistant.task_draft(value)

    def test_draft_redacts_quoted_assignments(self):
        out = json.dumps(assistant.task_draft('Исправить OKX_API_KEY="never_publish_this"\npassword: "second_value"'), ensure_ascii=False)
        self.assertNotIn("never_publish_this", out)
        self.assertNotIn("second_value", out)


class HandoffTests(AssistantFixture):
    def test_extract_run_evidence_and_event_provenance(self):
        run = self.run_record(result={"changed_files": ["src/code.py"],
                                     "checks": [{"command": "python -m unittest", "exit_code": 0}],
                                     "external_actions": [{"id": "order123", "outcome": "unknown"}],
                                     "next_step": "Сверить order123 до повтора"},
                              events=[{"type": "check.completed", "cursor": 7, "data": {"ok": True}}])
        packet = assistant.build_handoff(self.root, "T1", run=run)
        self.assertEqual(packet["changed_files"], ["src/code.py"])
        self.assertEqual(len(packet["checks"]), 2)
        self.assertIn("event:7", packet["checks"][1]["source"])
        self.assertEqual(packet["external_actions"][0]["outcome"], "unknown")
        self.assertIn("order123", packet["next_step"])
        self.assertEqual(packet["run"]["model"], "reported-model")
        self.assertIn("все тесты", packet["acceptance_criteria"])
        self.assertFalse(packet["resume_authorized"])

    def test_missing_evidence_not_inferred_from_successful_exit(self):
        self.run_record()
        packet = assistant.build_handoff(self.root, "T1", run_id="run_T1_test")
        self.assertEqual(packet["checks"], [])
        self.assertEqual(packet["changed_files"], [])
        self.assertEqual(len(packet["missing_evidence"]), 4)
        self.assertIn("SYNC", packet["next_step"])

    def test_no_run_and_different_task_rejected(self):
        self.assertIsNone(assistant.build_handoff(self.root, "T1")["run"])
        with self.assertRaises(ValueError):
            assistant.build_handoff(self.root, "T1", run={"id": "run_T2_x", "task_id": "T2"})

    def test_path_injection_unknown_and_duplicate_board_rejected(self):
        for task in ("../T1", "T1;echo", "MISSING"):
            with self.assertRaises(ValueError):
                assistant.build_handoff(self.root, task)
        self.put("ops/board.md", BOARD + BOARD.splitlines()[-1] + "\n")
        with self.assertRaises(ValueError):
            assistant.build_handoff(self.root, "T1")

    def test_run_path_injection_and_wrong_record_id_rejected(self):
        with self.assertRaises(ValueError):
            assistant.build_handoff(self.root, "T1", run_id="../env")
        self.run_record(id="run_OTHER")
        with self.assertRaises(ValueError):
            assistant.build_handoff(self.root, "T1", run_id="run_T1_test")

    def test_handoff_no_secrets_in_structured_data_or_markdown(self):
        run = self.run_record(result={"checks": [{"command": "token=unpublishable", "secret": "x-private"}],
                                     "next_step": "Bearer this_is_private"})
        out = json.dumps(assistant.build_handoff(self.root, "T1", run=run), ensure_ascii=False)
        for value in ("unpublishable", "x-private", "this_is_private"):
            self.assertNotIn(value, out)

    def test_long_board_criterion_preserved_with_shared_redaction(self):
        criterion = "Полный критерий " * 1000
        self.put("ops/board.md", BOARD.replace("Показывает результат и все тесты зелёные", criterion))
        packet = assistant.build_handoff(self.root, "T1")
        self.assertEqual(packet["acceptance_criteria"], criterion.strip())
        self.assertIn(criterion.strip(), packet["markdown"])

    def test_handoff_event_limit_visible_before_continuation(self):
        run = self.run_record(events=[{"type": "external.action", "cursor": i, "data": {"id": str(i)}} for i in range(120)])
        packet = assistant.build_handoff(self.root, "T1", run=run)
        self.assertEqual(len(packet["external_actions"]), 100)
        self.assertTrue(any("предел 100" in value for value in packet["missing_evidence"]))


class DiffTests(AssistantFixture):
    def setUp(self):
        super().setUp()
        self.init_git()

    def test_real_staged_unstaged_untracked_and_read_only(self):
        self.put("src/code.py", "answer = 2\n")
        self.git("add", "src/code.py")
        self.put("src/code.py", "answer = 3\n")
        self.put("src/new.py", "new = True\n")
        before = self.git("status", "--porcelain")
        index = (self.root / ".git/index").read_bytes()
        packet = assistant.review_diff(self.root, "T1")
        entries = {(f["kind"], f["path"]): f for f in packet["files"]}
        self.assertIn("+answer = 2", entries[("staged", "src/code.py")]["patch"])
        self.assertIn("+answer = 3", entries[("unstaged", "src/code.py")]["patch"])
        self.assertIn("new = True", entries[("untracked", "src/new.py")]["patch"])
        self.assertEqual(packet["verdict"], "requires-human-review")
        self.assertFalse(packet["accepted"])
        self.assertEqual(packet["semantic_acceptance"], "not-evaluated")
        self.assertEqual(before, self.git("status", "--porcelain"))
        self.assertEqual(index, (self.root / ".git/index").read_bytes())

    def test_secret_files_omitted_and_other_assignments_redacted(self):
        self.put(".env", "API_KEY=private_env_value\n")
        self.put("src/example.py", 'TOKEN = "private_example_value"\n')
        packet = assistant.review_diff(self.root, "T1")
        out = json.dumps(packet, ensure_ascii=False)
        self.assertNotIn("private_env_value", out)
        self.assertNotIn("private_example_value", out)
        self.assertFalse(packet["complete"])
        self.assertTrue(any(f["code"] == "secret-file" for f in packet["findings"]))
        self.assertTrue(any(f["code"] == "possible-secret" for f in packet["findings"]))

    def test_protected_path_and_added_risk_bypass_flagged(self):
        self.put("ops/hooks/proposal.py", "guard_enabled = False\n")
        self.put("src/risk.py", "disable_risk = True\n")
        packet = assistant.review_diff(self.root, "T1")
        codes = {f["code"] for f in packet["findings"]}
        self.assertIn("guard-autopilot-change", codes)
        self.assertIn("risk-change-needs-review", codes)
        self.assertIn("risk-bypass-suspected", codes)
        finding = next(f for f in packet["findings"] if f["code"] == "risk-bypass-suspected")
        self.assertEqual(finding["added_line"], 1)

    def test_binary_and_extension_not_read(self):
        self.put("state.db", "private-binary-data")
        packet = assistant.review_diff(self.root, "T1")
        self.assertNotIn("private-binary-data", json.dumps(packet))
        self.assertFalse(packet["complete"])

    def test_limits_explicit_and_no_automatic_acceptance(self):
        self.put("src/large.py", "line\n" * 10000)
        with mock.patch.object(assistant, "MAX_FILE_BYTES", 128):
            packet = assistant.review_diff(self.root, "T1")
        self.assertTrue(packet["files"][0]["truncated"])
        self.assertLessEqual(packet["bytes_examined"], 128)
        self.assertFalse(packet["complete"])
        self.assertFalse(packet["accepted"])

    def test_file_limit_explicit(self):
        for i in range(5):
            self.put(f"src/new{i}.py", "x = 1")
        with mock.patch.object(assistant, "MAX_FILES", 2):
            packet = assistant.review_diff(self.root, "T1")
        self.assertEqual(len(packet["files"]), 2)
        self.assertFalse(packet["complete"])

    def test_git_output_buffer_is_bounded(self):
        self.put("src/large.py", "line\n" * 10000)
        self.git("add", "src/large.py")
        output, truncated = assistant._git(self.root, ["diff", "--cached", "--no-ext-diff", "--no-textconv"], 100)
        self.assertEqual(len(output), 100)
        self.assertTrue(truncated)

    def test_external_diff_and_textconv_are_disabled(self):
        self.git("config", "diff.external", "nonexistent-forbidden-command")
        self.git("config", "diff.bad.textconv", "nonexistent-forbidden-textconv")
        self.put(".gitattributes", "*.py diff=bad\n")
        self.put("src/code.py", "answer = 7\n")
        packet = assistant.review_diff(self.root, "T1")
        self.assertTrue(any("answer = 7" in (entry["patch"] or "") for entry in packet["files"]))

    def test_empty_diff_does_not_prove_task_complete(self):
        packet = assistant.review_diff(self.root, "T1")
        self.assertEqual(packet["files"], [])
        self.assertFalse(packet["accepted"])
        self.assertTrue(any("не доказательство" in line for line in packet["limitations"]))

    def test_forged_worktree_cwd_rejected_before_read(self):
        lease = {"schema_version": 1, "task_id": "T1", "run_id": "run_T1_test", "worktree": str(self.root),
                 "branch": "agent/run_T1_test", "base_commit": self.git("rev-parse", "HEAD")}
        self.put("data/worktree-leases/T1.json", json.dumps(lease))
        self.run_record(workspace="worktree", lease=lease)
        with self.assertRaises(ValueError):
            assistant.review_diff(self.root, "T1", "run_T1_test")

    def test_registered_worktree_committed_staged_untracked_review(self):
        run_id = "run_T1_test"
        destination = self.root.parent / (self.root.name + "-agent-worktrees") / run_id
        destination.parent.mkdir()
        base = self.git("rev-parse", "HEAD")
        self.git("worktree", "add", "-b", f"agent/{run_id}", str(destination), base)
        (destination / "src/code.py").write_text("answer = 11\n", encoding="utf-8")
        subprocess.run(["git", "add", "src/code.py"], cwd=destination, check=True, capture_output=True)
        subprocess.run(["git", "commit", "--quiet", "-m", "worktree result"], cwd=destination, check=True, capture_output=True)
        (destination / "src/new.py").write_text("new = 12\n", encoding="utf-8")
        lease = {"schema_version": 1, "task_id": "T1", "run_id": run_id, "worktree": str(destination),
                 "branch": f"agent/{run_id}", "base_commit": base}
        self.put("data/worktree-leases/T1.json", json.dumps(lease))
        self.run_record(workspace="worktree", lease=lease)
        packet = assistant.review_diff(self.root, "T1", run_id)
        self.assertEqual(packet["scope"], "verified-worktree")
        self.assertTrue(any(f["kind"] == "committed" and "answer = 11" in f["patch"] for f in packet["files"]))
        self.assertTrue(any(f["kind"] == "untracked" and f["path"] == "src/new.py" for f in packet["files"]))


class SpecialistTests(AssistantFixture):
    def test_research_returns_unverified_claims_and_source_links(self):
        self.put("insights/example.md", "# Сессии API\nСессии API описаны в https://example.invalid/docs\n")
        out = assistant.call_specialist(self.root, "crypto-insight-hunter", "sources.search", {"query": "сессии"})
        self.assertTrue(out["facts"])
        self.assertEqual(out["facts"][0]["quality"], "document-claim-unverified")
        self.assertEqual(out["facts"][0]["freshness"], "not-verified")
        self.assertEqual(out["sources"][0]["links"], ["https://example.invalid/docs"])
        self.assertFalse(out["execution_authorized"])

    def test_empty_search_has_no_invented_facts(self):
        out = assistant.call_specialist(self.root, "crypto-insight-hunter", "sources.search", {"query": "несуществующий"})
        self.assertEqual(out["facts"], [])
        self.assertEqual(out["sources"], [])

    def test_research_cannot_review_or_read_arbitrary_path(self):
        for tool, args in (("shell", {"command": "whoami"}), ("diff.review", {"task_id": "T1"}),
                           ("sources.search", {"query": "API", "path": ".env"}),
                           ("sources.search", {"query": "API", "max_files": 99999})):
            with self.assertRaises(ValueError):
                assistant.call_specialist(self.root, "crypto-insight-hunter", tool, args)

    def test_unknown_role_recursive_trade_and_write_tools_denied(self):
        for role, tool in (("unknown", "task.read"), ("insight-executor", "assistant.specialist"),
                           ("ops-sentinel", "engine.stop"), ("crypto-insight-hunter", "orders.place"),
                           ("insight-executor", "board.write")):
            with self.assertRaises(ValueError):
                assistant.call_specialist(self.root, role, tool, {"task_id": "T1"})

    def test_task_read_exact_schema_and_board_source(self):
        out = assistant.call_specialist(self.root, "ops-sentinel", "task.read", {"task_id": "T1"})
        self.assertEqual(out["facts"][0]["status"], "ready")
        self.assertEqual(out["sources"], ["ops/board.md:3"])
        with self.assertRaises(ValueError):
            assistant.call_specialist(self.root, "ops-sentinel", "task.read", {})

    def test_safe_path_traversal_and_symlink_rejected(self):
        with self.assertRaises(ValueError):
            assistant._safe_path(self.root, "../../.env")
        target = Path(self.temp.name) / "outside.md"
        target.write_text("private", encoding="utf-8")
        link = self.root / "link.md"
        try:
            link.symlink_to(target)
        except OSError:
            self.skipTest("Windows не разрешает создание symlink текущему пользователю")
        with self.assertRaises(ValueError):
            assistant._read(self.root, "link.md", 100)


if __name__ == "__main__":
    unittest.main()
