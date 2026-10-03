"""Контракт read-only проекции Morphy: утечки, деградация источников и ID задач."""
from contextlib import closing
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from src import morphy_project as mp

BOARD = """| ID | Задача | Агент | Статус | Зависит от | Критерий готовности | Заметки |
| --- | --- | --- | --- | --- | --- | --- |
| T1 | Проверить движок | Insight Executor | ready | — | Получить статус | token=private-test-value |
| T2 | Настроить канал | Human | needs-user | T1 | Доставить уведомление | password=private-password |
"""


class MorphyProjectionTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        (self.root / "ops").mkdir()
        (self.root / "ops/board.md").write_text(BOARD, encoding="utf-8")
        (self.root / "ops/agent-routing.json").write_text(json.dumps({"runtimes": {
            "codex": {"command": "codex", "default_model": "test-model", "secret": "registry-secret"}
        }}), encoding="utf-8")
        self.addCleanup(self.tmp.cleanup)

    def collect(self, snapshot=None):
        with patch.object(mp, "build_snapshot", return_value=snapshot or {}), \
             patch.object(mp, "check_netdata_health", return_value={"available": False, "error": "secret-netdata-error"}), \
             patch.object(mp, "morphy_status", return_value={}), \
             patch.object(mp, "git_summary", return_value={"available": False}):
            return mp.collect(self.root)

    def test_projection_omits_raw_log_and_unknown_fields(self):
        result = self.collect({"risk": {"equity": 100, "api_key": "risk-secret"},
                               "ops": {"watch": {"last_line": "raw-private-log"}},
                               "logs": ["raw-private-log"], "errors": ["secret-error-detail"]})
        serialized = json.dumps(result)
        for secret in ("risk-secret", "registry-secret", "raw-private-log", "secret-error-detail", "secret-netdata-error"):
            self.assertNotIn(secret, serialized)
        self.assertEqual(result["risk"]["equity"], 100)
        self.assertEqual(result["errors"], ["snapshot: partial-source-error"])

    def test_task_detail_is_redacted_and_uses_original_criterion(self):
        detail = mp.detail(self.root, "T1")
        self.assertEqual(detail["criterion"], "Получить статус")
        self.assertIn("[СКРЫТО]", detail["notes"])
        self.assertNotIn("private-test-value", json.dumps(detail))

    def test_task_id_rejects_path_or_shell_input(self):
        for task_id in ("../T1", "T1;whoami", "T1\n", "A" * 81):
            with self.subTest(task_id=task_id), self.assertRaises(ValueError):
                mp.detail(self.root, task_id)

    def test_duplicate_task_detail_fails(self):
        with (self.root / "ops/board.md").open("a", encoding="utf-8") as stream:
            stream.write("| T1 | Дубль | Human | ready | — | — | — |\n")
        with self.assertRaises(ValueError):
            mp.detail(self.root, "T1")
        self.assertEqual(self.collect()["board"]["duplicates"], ["T1"])

    def test_missing_board_does_not_hide_other_sources(self):
        (self.root / "ops/board.md").unlink()
        result = self.collect({"risk": {"equity": 123}})
        self.assertEqual(result["board"]["tasks"], [])
        self.assertEqual(result["risk"]["equity"], 123)
        self.assertTrue(any(error.startswith("board:") for error in result["errors"]))

    def test_read_only_collection_preserves_project_files(self):
        before = {p.relative_to(self.root): p.read_bytes() for p in self.root.rglob("*") if p.is_file()}
        self.collect()
        after = {p.relative_to(self.root): p.read_bytes() for p in self.root.rglob("*") if p.is_file()}
        self.assertEqual(before, after)

    def test_integration_status_does_not_claim_live_service_from_files(self):
        (self.root / "Home.md").write_text("# Пульт", encoding="utf-8")
        result = self.collect()
        integrations = {i["id"]: i for i in result["integrations"]}
        self.assertEqual(integrations["obsidian"]["status"], "files-present")
        self.assertEqual(integrations["telegram"]["status"], "needs-setup")
        self.assertEqual(integrations["netdata"]["status"], "offline")

    def test_muse_queue_never_exports_prompt_or_model_output(self):
        for folder in ("done", "outbox"):
            (self.root / "ops/delegations" / folder).mkdir(parents=True)
        record = {"id": "d20261003-000000-abcd", "status": "done", "role": "insight-executor",
                  "prompt": "private-task-prompt", "output_tail": "private-model-output", "exit_code": 0}
        for folder in ("done", "outbox"):
            (self.root / "ops/delegations" / folder / (record["id"] + ".json")).write_text(json.dumps(record), encoding="utf-8")
        result = self.collect()
        self.assertNotIn("private-task-prompt", json.dumps(result))
        self.assertNotIn("private-model-output", json.dumps(result))
        self.assertEqual(result["queue"]["jobs"][0]["state"], "done")


class LifetimePnlTests(unittest.TestCase):
    """Прибыль за всё время: внешние потоки demo-счёта в результат не входят."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = Path(self.tmp.name) / "bot_state.db"
        self.addCleanup(self.tmp.cleanup)

    def curve(self, values):
        with closing(sqlite3.connect(self.db)) as conn, conn:
            conn.execute("CREATE TABLE equity_curve (ts REAL, total_eq REAL, avail_eq REAL, upl REAL)")
            conn.executemany("INSERT INTO equity_curve (ts, total_eq) VALUES (?, ?)",
                             [(1_790_000_000 + i * 900, value) for i, value in enumerate(values)])
        return mp.lifetime_pnl(self.db)

    def test_deposit_is_excluded_from_profit(self):
        result = self.curve([5000, 5100, 100_000, 101_000])
        self.assertEqual(result["net_usdt"], 1100.0)          # 100 за первый шаг + 1000 за последний
        self.assertEqual(result["base_usdt"], 99_900.0)       # 5000 внесено + 94 900 пополнение
        self.assertEqual(result["net_pct"], round(1100 / 99_900 * 100, 2))
        self.assertEqual(result["external_flows"], 1)

    def test_withdrawal_is_excluded_from_profit(self):
        result = self.curve([100_000, 101_000, 50_000, 50_500])
        self.assertEqual(result["net_usdt"], 1500.0)
        self.assertEqual(result["external_usdt"], -51_000.0)
        self.assertEqual(result["external_flows"], 1)

    def test_loss_is_negative_and_percent_follows(self):
        result = self.curve([10_000, 9500, 9000])
        self.assertEqual(result["net_usdt"], -1000.0)
        self.assertEqual(result["base_usdt"], 10_000.0)
        self.assertEqual(result["net_pct"], -10.0)
        self.assertEqual(result["external_flows"], 0)

    def test_history_shorter_than_two_points_is_unavailable(self):
        self.assertFalse(self.curve([5000])["available"])

    def test_missing_database_is_not_an_error(self):
        self.assertEqual(mp.lifetime_pnl(Path(self.tmp.name) / "absent.db"),
                         {"available": False, "points": 0})

    def test_collect_degrades_without_equity_history(self):
        with patch.object(mp, "build_snapshot", return_value={}), \
             patch.object(mp, "check_netdata_health", return_value={}), \
             patch.object(mp, "morphy_status", return_value={}), \
             patch.object(mp, "git_summary", return_value={"available": False}):
            result = mp.collect(Path(self.tmp.name))
        self.assertFalse(result["lifetime"]["available"])
        self.assertFalse([error for error in result["errors"] if error.startswith("lifetime:")])


if __name__ == "__main__":
    unittest.main()
