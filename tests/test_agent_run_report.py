"""Настоящий CLI -> atomic artifact -> strict consumer, без записи в проект."""
from concurrent.futures import ThreadPoolExecutor
import json
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

from src import agent_run_report as producer


RUN = "run_T1_report"
TASK = "T1"


def report():
    return {"summary": "Исправлены события панели; нужна приёмка diff.", "changed_files": ["src/example.py"],
            "checks": [{"command": "python -m unittest tests.test_example", "exit_code": 0,
                        "status": "passed", "artifact_ref": "logs/test-example.log"}],
            "external_actions": [{"id": "order123", "kind": "order", "status": "unknown"}],
            "next_step": "Сверить order123 со штатным источником перед повтором."}


class ReportFixture(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name) / "checkout"
        self.root.mkdir()

    @property
    def destination(self):
        return self.root / "data/run-results" / f"{RUN}.json"


class RunReportTests(ReportFixture):
    def test_real_write_load_reported_provenance_and_unknown_action(self):
        packet = producer.write_report(self.root, RUN, TASK, report())
        loaded = producer.load_report(self.root, RUN, TASK)
        self.assertEqual(packet, loaded)
        self.assertEqual(loaded["quality"], "reported")
        self.assertFalse(loaded["acceptance_verified"])
        self.assertEqual(loaded["result"]["external_actions"][0]["status"], "unknown")
        self.assertEqual(loaded["provenance"], f"data/run-results/{RUN}.json")
        self.assertTrue(self.destination.is_file())
        self.assertEqual(list(self.destination.parent.glob("*.tmp")), [])

    def test_update_same_binding_and_refuse_other_task(self):
        producer.write_report(self.root, RUN, TASK, report())
        second = report()
        second["summary"] = "Второй отчёт того же запуска"
        producer.write_report(self.root, RUN, TASK, second)
        before = self.destination.read_bytes()
        with self.assertRaises(ValueError):
            producer.write_report(self.root, RUN, "OTHER", report())
        self.assertEqual(before, self.destination.read_bytes())
        self.assertEqual(producer.load_report(self.root, RUN, TASK)["result"]["summary"], second["summary"])

    def test_parallel_first_writers_cannot_overwrite_foreign_binding(self):
        def write(task):
            try:
                producer.write_report(self.root, RUN, task, report())
                return task
            except ValueError:
                return None
        with ThreadPoolExecutor(max_workers=2) as executor:
            results = list(executor.map(write, ["T1", "T2"]))
        accepted = [task for task in results if task]
        self.assertEqual(len(accepted), 1)
        self.assertEqual(producer.load_report(self.root, RUN, accepted[0])["task_id"], accepted[0])

    def test_schema_missing_unknown_or_provider_fields_no_output(self):
        invalid = [[], {"summary": "x"}, {**report(), "runtime": "codex"},
                   {**report(), "model": "fake-provider"}, {**report(), "output": "outside.json"},
                   {**report(), "acceptance_verified": True}]
        for value in invalid:
            with self.subTest(value_type=type(value).__name__):
                with self.assertRaises(ValueError):
                    producer.write_report(self.root, RUN, TASK, value)
                self.assertFalse((self.root / "data").exists())

    def test_invalid_status_type_and_inconsistent_checks_rejected(self):
        invalid_checks = [{"command": "test", "exit_code": True, "status": "passed"},
                          {"command": "test", "exit_code": 1, "status": "passed"},
                          {"command": "test", "exit_code": 0, "status": "failed"},
                          {"command": "test", "exit_code": 0, "status": "not-run"},
                          {"command": "test", "exit_code": None, "status": "verified"},
                          {"command": "test", "exit_code": 0, "status": "passed", "shell": True}]
        for check in invalid_checks:
            value = report()
            value["checks"] = [check]
            with self.assertRaises(ValueError):
                producer.write_report(self.root, RUN, TASK, value)
        self.assertFalse(self.destination.exists())

    def test_valid_unknown_and_not_run_checks(self):
        value = report()
        value["checks"] = [{"command": "pending test", "exit_code": None, "status": "unknown"},
                           {"command": "future test", "exit_code": None, "status": "not-run"}]
        producer.write_report(self.root, RUN, TASK, value)
        self.assertEqual(len(producer.load_report(self.root, RUN, TASK)["result"]["checks"]), 2)

    def test_path_traversal_secret_query_absolute_paths_rejected(self):
        for path in ("../outside.py", "C:/secret.py", "/outside.py", ".env", ".env.local", "keys/credentials.json",
                     "src/code.py?token=private", "src/code.py#secret", "src//code.py", "src/./code.py", ".git/config", "private.pem"):
            value = report()
            value["changed_files"] = [path]
            with self.subTest(path=path), self.assertRaises(ValueError):
                producer.write_report(self.root, RUN, TASK, value)
        self.assertFalse((self.root / "data").exists())

    def test_artifact_ref_is_not_arbitrary_input_path(self):
        value = report()
        value["checks"][0]["artifact_ref"] = "../../private.json"
        with self.assertRaises(ValueError):
            producer.write_report(self.root, RUN, TASK, value)

    def test_limits_empty_description_duplicate_files(self):
        values = []
        for field, replacement in (("summary", "x" * 2001), ("next_step", ""),
                                   ("changed_files", ["src/x.py"] * 51),
                                   ("changed_files", ["src/x.py", "src/x.py"]),
                                   ("checks", [report()["checks"][0]] * 51)):
            value = report()
            value[field] = replacement
            values.append(value)
        for value in values:
            with self.assertRaises(ValueError):
                producer.write_report(self.root, RUN, TASK, value)
        self.assertFalse(self.destination.exists())

    def test_secrets_filtered_before_persistence_and_return(self):
        value = report()
        value["summary"] = 'OKX_API_KEY="never-persist-this"; Bearer secret-bearer-value'
        value["checks"][0]["command"] = 'python check.py --config \'{"password":"canary_password"}\''
        value["next_step"] = '-----BEGIN PRIVATE KEY-----\nprivate-pem-canary\n-----END PRIVATE KEY-----'
        packet = producer.write_report(self.root, RUN, TASK, value)
        encoded = json.dumps(packet, ensure_ascii=False) + self.destination.read_text(encoding="utf-8")
        for secret in ("never-persist-this", "secret-bearer-value", "canary_password", "private-pem-canary"):
            self.assertNotIn(secret, encoded)
        self.assertIn("REDACTED", encoded)
        self.assertEqual(producer.load_report(self.root, RUN, TASK)["quality"], "reported")

    def test_external_action_secrets_unsafe_ids_and_fields_rejected(self):
        for action in ({"id": "id?token=x", "kind": "order", "status": "unknown"},
                       {"id": "sk-test-canary-secret-123456789", "kind": "order", "status": "unknown"},
                       {"id": "id", "kind": "order", "status": "confirmed-success"},
                       {"id": "id", "kind": "order", "status": "unknown", "retry": True}):
            value = report()
            value["external_actions"] = [action]
            with self.assertRaises(ValueError):
                producer.write_report(self.root, RUN, TASK, value)

    def test_loader_binding_schema_quality_and_timestamp_checks(self):
        packet = producer.write_report(self.root, RUN, TASK, report())
        for field, replacement in (("run_id", "run_other"), ("task_id", "OTHER"), ("quality", "verified"),
                                   ("acceptance_verified", True), ("schema_version", True),
                                   ("reported_at", "2026-10-03"), ("provenance", "../elsewhere.json")):
            value = {**packet, field: replacement}
            self.destination.write_text(json.dumps(value), encoding="utf-8")
            with self.subTest(field=field), self.assertRaises(ValueError):
                producer.load_report(self.root, RUN, TASK)
        self.destination.write_text(json.dumps({**packet, "model": "fake"}), encoding="utf-8")
        with self.assertRaises(ValueError):
            producer.load_report(self.root, RUN, TASK)

    def test_loader_missing_oversize_and_invalid_json(self):
        self.assertIsNone(producer.load_report(self.root, RUN, TASK))
        self.destination.parent.mkdir(parents=True)
        for raw in (b"{" * (producer.MAX_BYTES + 1), b"{", b'{"a":1,"a":2}', b'{"a":NaN}'):
            self.destination.write_bytes(raw)
            with self.assertRaises(ValueError):
                producer.load_report(self.root, RUN, TASK)

    def test_atomic_failure_preserves_existing_report_and_cleans_own_temp(self):
        producer.write_report(self.root, RUN, TASK, report())
        before = self.destination.read_bytes()
        with mock.patch.object(producer.os, "replace", side_effect=OSError("simulated atomic install failure")):
            with self.assertRaises(OSError):
                producer.write_report(self.root, RUN, TASK, {**report(), "summary": "second"})
        self.assertEqual(before, self.destination.read_bytes())
        self.assertEqual(list(self.destination.parent.glob("*.tmp")), [])

    def test_linked_storage_or_root_refused_before_writing(self):
        outside = Path(self.temp.name) / "outside"
        outside.mkdir()
        link = self.root / "data"
        try:
            link.symlink_to(outside, target_is_directory=True)
        except OSError:
            self.skipTest("Windows symlink privilege недоступен")
        with self.assertRaises(ValueError):
            producer.write_report(self.root, RUN, TASK, report())
        self.assertEqual(list(outside.iterdir()), [])
        root_link = Path(self.temp.name) / "root-link"
        root_link.symlink_to(self.root, target_is_directory=True)
        with self.assertRaises(ValueError):
            producer.load_report(root_link, RUN, TASK)


class ActualCLITests(ReportFixture):
    def setUp(self):
        super().setUp()
        package = self.root / "src"
        package.mkdir()
        (package / "__init__.py").write_text("", encoding="utf-8")
        source = Path(producer.__file__).parent
        shutil.copy2(source / "agent_run_report.py", package / "agent_run_report.py")
        shutil.copy2(source / "ai_observability.py", package / "ai_observability.py")

    def cli(self, raw=None, input_path=None, extra=()):
        argv = [sys.executable, "-m", "src.agent_run_report", "--run-id", RUN, "--task-id", TASK,
                "--input", input_path or "-", *extra]
        return subprocess.run(argv, cwd=self.root, input=raw, capture_output=True, timeout=15)

    def test_actual_stdin_cli_artifact_loaded_by_real_consumer(self):
        process = self.cli(json.dumps(report(), ensure_ascii=False).encode("utf-8"))
        self.assertEqual(process.returncode, 0, process.stderr.decode("utf-8", errors="replace"))
        acknowledgement = json.loads(process.stdout)
        self.assertTrue(acknowledgement["ok"])
        loaded = producer.load_report(self.root, RUN, TASK)
        self.assertEqual(loaded["result"]["changed_files"], ["src/example.py"])
        self.assertEqual(loaded["result"]["checks"][0]["status"], "passed")
        self.assertEqual(loaded["result"]["external_actions"][0]["status"], "unknown")
        self.assertEqual(loaded["quality"], "reported")
        self.assertFalse(loaded["acceptance_verified"])

    def test_actual_file_cli_and_no_provider_payload_or_raw_secrets_stdout(self):
        value = report()
        value["summary"] = "token=private-cli-canary"
        (self.root / "report.json").write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")
        process = self.cli(input_path="report.json")
        self.assertEqual(process.returncode, 0)
        self.assertNotIn(b"private-cli-canary", process.stdout)
        self.assertNotIn("private-cli-canary", self.destination.read_text(encoding="utf-8"))

    def test_malformed_duplicate_nonfinite_unknown_and_oversize_cli_no_output(self):
        for raw in (b"{", b'{"summary":"one","summary":"two"}', b'{"summary":NaN}',
                    json.dumps({**report(), "model": "fake"}).encode("utf-8"), b"x" * (producer.MAX_BYTES + 1)):
            process = self.cli(raw)
            self.assertEqual(process.returncode, 2)
            self.assertFalse(json.loads(process.stdout)["ok"])
            self.assertFalse(self.destination.exists())

    def test_fixed_output_path_rejects_external_input_and_output_option(self):
        outside = Path(self.temp.name) / "private.json"
        outside.write_text(json.dumps(report()), encoding="utf-8")
        process = self.cli(input_path=str(outside))
        self.assertEqual(process.returncode, 2)
        self.assertFalse(self.destination.exists())
        process = self.cli(json.dumps(report()).encode(), extra=("--output", "elsewhere.json"))
        self.assertEqual(process.returncode, 2)
        self.assertFalse((self.root / "elsewhere.json").exists())


if __name__ == "__main__":
    unittest.main()
