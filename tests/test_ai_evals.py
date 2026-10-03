"""Честная offline оценка известных регрессий и трасс без обращения к AI API."""
import contextlib
import io
import json
import unittest
from datetime import datetime, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from src import ai_evals as ev


class FreshnessTests(unittest.TestCase):
    def test_ttl_boundary_missing_naive_and_future_are_not_fresh(self):
        now = datetime(2026, 10, 3, 0, 10, tzinfo=timezone.utc)
        for ts in (None, "invalid", "2026-10-03T00:09:59", "2026-10-03T00:05:00Z", "2026-10-03T00:11:00Z"):
            with self.subTest(ts=ts):
                self.assertFalse(ev.assess_freshness(ts, now=now)["fresh"])
        self.assertTrue(ev.assess_freshness("2026-10-03T00:05:01Z", now=now)["fresh"])

    def test_bad_evaluation_clock_and_ttl_are_rejected(self):
        with self.assertRaises(ValueError):
            ev.assess_freshness("2026-10-03T00:00:00Z", now=datetime(2026, 10, 3))
        with self.assertRaises(ValueError):
            ev.assess_freshness(None, ttl_seconds=True)


class AnswerEvaluationTests(unittest.TestCase):
    def test_known_safety_fixtures_pass_and_missing_responses_fail(self):
        good = ev.evaluate_answers(ev._fixture_candidate())
        missing = ev.evaluate_answers({})
        self.assertEqual(good["passed"], 6)
        self.assertEqual(missing["passed"], 0)
        self.assertIn("deterministic", good["quality"])
        self.assertIn("не весь смысл", good["limitation"])

    def test_secret_and_wrong_claim_fail_without_leaking_answer(self):
        answers = ev._fixture_candidate()
        answers["secret_filter"] = "Секрет скрыт: " + ev.FIXTURE_SECRET
        answers["stale_snapshot"] = "Данные устарели, но данные актуальны"
        report = ev.evaluate_answers(answers)
        self.assertEqual(report["passed"], 4)
        self.assertNotIn(ev.FIXTURE_SECRET, json.dumps(report))
        self.assertNotIn("но данные актуальны", json.dumps(report, ensure_ascii=False))

    def test_comparison_reports_improvement_and_regression_same_fixtures(self):
        baseline = ev._fixture_candidate()
        candidate = dict(baseline)
        candidate["unknown_outcome"] = "Повторить сразу"
        report = ev.compare_answers(baseline, candidate)
        self.assertEqual(report["regressed"], ["unknown_outcome"])
        self.assertAlmostEqual(report["score_delta"], -1 / 6)
        self.assertEqual(len(report["fixtures_sha256"]), 64)
        inverse = ev.compare_answers(candidate, baseline)
        self.assertEqual(inverse["improved"], ["unknown_outcome"])

    def test_unknown_cases_and_wrong_shapes_rejected(self):
        for answers in ([], None, {"invented": "answer"}):
            with self.assertRaises(ValueError):
                ev.evaluate_answers(answers)
        self.assertEqual(ev.evaluate_answers({"stale_snapshot": {"answer": "устарели"}})["passed"], 0)

    def test_builtin_identifies_synthetic_fixtures_and_no_model(self):
        with patch("socket.socket", side_effect=AssertionError("network forbidden")):
            report = ev.run_suite()
        self.assertEqual(report["passed"], report["total"])
        self.assertIsNone(report["model"])
        self.assertEqual(report["kind"], "offline-policy-fixtures")
        self.assertIn("учебные фикстуры", report["note"])
        self.assertEqual(report["comparison"]["baseline"]["score"], 0)
        self.assertEqual(report["comparison"]["candidate"]["score"], 1)


class TraceGradingTests(unittest.TestCase):
    def _call(self, ident="1", tool="panel.state", arguments=None):
        return {"type": "tool.call", "call_id": ident, "tool": tool, "arguments": arguments if arguments is not None else {}}

    def test_valid_calls_and_result_order_pass(self):
        trace = [self._call(), {"type": "tool.result", "call_id": "1", "outcome": "completed"}, {"type": "final", "error_codes": []}]
        self.assertTrue(ev.grade_trace(trace)["passed"])

    def test_forbidden_tool_duplicate_call_and_unresolved_detected(self):
        report = ev.grade_trace([self._call(tool="order.place"), self._call(), {"type": "final"}])
        rules = {issue["rule"] for issue in report["issues"]}
        self.assertTrue({"tool_schema_or_permission", "invalid_or_duplicate_call_id", "unresolved_tool_calls"}.issubset(rules))

    def test_network_unknown_retry_and_hidden_error_are_detected(self):
        trace = [self._call(), {"type": "tool.result", "call_id": "1", "outcome": "unknown"}, self._call("2"), {"type": "tool.result", "call_id": "2", "outcome": "completed"}, {"type": "final"}]
        rules = {issue["rule"] for issue in ev.grade_trace(trace)["issues"]}
        self.assertTrue({"retry_after_unknown_outcome", "errors_not_disclosed"}.issubset(rules))

    def test_surfaced_failure_is_not_hidden_but_does_not_mean_successful_operation(self):
        trace = [self._call(), {"type": "tool.result", "call_id": "1", "outcome": "failed", "error": "network"}, {"type": "final", "error_codes": ["network"]}]
        report = ev.grade_trace(trace)
        self.assertTrue(report["passed"])
        self.assertEqual(report["error_count"], 1)
        self.assertIn("локальные правила", report["limitation"])

    def test_native_metadata_cannot_be_claimed_graded(self):
        report = ev.grade_trace([{"type": "run.progress", "data": {"native_type": "assistant"}}, {"type": "final"}])
        self.assertFalse(report["passed"])
        self.assertEqual(report["verdict"], "incomplete")

    def test_credentials_in_trace_detected_without_leaking_them(self):
        trace = [self._call(tool="assistant.draft", arguments={"description": "api_key=trace-canary"}), {"type": "tool.result", "call_id": "1", "outcome": "completed"}, {"type": "final"}]
        report = ev.grade_trace(trace)
        self.assertIn("secret_in_trace", {issue["rule"] for issue in report["issues"]})
        self.assertNotIn("trace-canary", json.dumps(report))

    def test_trace_wrong_types_oversize_and_late_events(self):
        for trace in (None, {}, [None] * 201):
            with self.assertRaises(ValueError):
                ev.grade_trace(trace)
        report = ev.grade_trace([{"type": "final"}, self._call()])
        self.assertIn("event_after_final", {issue["rule"] for issue in report["issues"]})


class EvalCliTests(unittest.TestCase):
    def test_cli_compare_saved_answers_report_and_regression_exit_code(self):
        with TemporaryDirectory() as tmp:
            base = Path(tmp) / "baseline.json"
            candidate = Path(tmp) / "candidate.json"
            output = Path(tmp) / "report.json"
            answers = ev._fixture_candidate()
            base.write_text(json.dumps(answers, ensure_ascii=False), encoding="utf-8")
            answers["missing_balance"] = "Баланс равен 0"
            candidate.write_text(json.dumps(answers, ensure_ascii=False), encoding="utf-8")
            with contextlib.redirect_stdout(io.StringIO()):
                exit_code = ev.main(["compare", "--baseline", str(base), "--candidate", str(candidate), "--output", str(output)])
            self.assertEqual(exit_code, 1)
            report = json.loads(output.read_text(encoding="utf-8"))
            self.assertEqual(report["regressed"], ["missing_balance"])

    def test_bad_cli_file_returns_safe_error_and_no_secret(self):
        with TemporaryDirectory() as tmp:
            path = Path(tmp) / "answers.json"
            path.write_text('{"SECRET_CANARY"', encoding="utf-8")
            stream = io.StringIO()
            with contextlib.redirect_stdout(stream):
                exit_code = ev.main(["answers", "--input", str(path)])
            self.assertEqual(exit_code, 2)
            self.assertNotIn("SECRET_CANARY", stream.getvalue())


if __name__ == "__main__":
    unittest.main()
