"""Тесты интеграции CockroachDB Cloud (src/cloud_db.py)."""

import os
import unittest
from unittest.mock import MagicMock, patch

from src.cloud_db import CloudDB, cloud_db_status, get_connection_string


class TestCloudDB(unittest.TestCase):

    def test_connection_string_from_url(self):
        with patch.dict(os.environ, {"COCKROACH_DATABASE_URL": "postgresql://user:pass@host:26257/db?sslmode=require"}):
            self.assertEqual(get_connection_string(), "postgresql://user:pass@host:26257/db?sslmode=require")

    def test_connection_string_from_components(self):
        env = {
            "COCKROACH_HOST": "legion-pi.cockroachlabs.cloud",
            "COCKROACH_USER": "paul",
            "COCKROACH_PASSWORD": "secret_password",
            "COCKROACH_DATABASE": "defaultdb",
            "COCKROACH_PORT": "26257",
            "COCKROACH_SSLMODE": "require",
        }
        with patch.dict(os.environ, env, clear=True), patch("os.path.exists", return_value=False):
            expected = "postgresql://paul:secret_password@legion-pi.cockroachlabs.cloud:26257/defaultdb?sslmode=require"
            self.assertEqual(get_connection_string(), expected)

    def test_not_configured_when_no_env(self):
        with patch.dict(os.environ, {}, clear=True):
            with patch("os.path.exists", return_value=False):
                self.assertIsNone(get_connection_string())
                db = CloudDB()
                self.assertFalse(db.is_configured)
                health = db.check_health()
                self.assertFalse(health["ok"])
                self.assertFalse(health["configured"])

    def test_cloud_db_status_not_configured(self):
        with patch.dict(os.environ, {}, clear=True):
            with patch("os.path.exists", return_value=False):
                st = cloud_db_status()
                self.assertFalse(st["enabled"])
                self.assertEqual(st["status"], "not_configured")

    @patch("src.cloud_db.psycopg2.connect")
    def test_check_health_success(self, mock_connect):
        mock_conn = MagicMock()
        mock_cur = MagicMock()
        mock_connect.return_value.__enter__.return_value = mock_conn
        mock_conn.cursor.return_value.__enter__.return_value = mock_cur
        mock_cur.fetchone.return_value = ("CockroachDB CCL v24.1", "defaultdb", "paul")

        db = CloudDB("postgresql://paul:pass@host:26257/defaultdb?sslmode=require")
        res = db.check_health()
        self.assertTrue(res["ok"])
        self.assertEqual(res["database"], "defaultdb")
        self.assertEqual(res["user"], "paul")

    @patch("src.cloud_db.psycopg2.connect")
    def test_check_health_failure_is_resilient(self, mock_connect):
        mock_connect.side_effect = Exception("Connection timed out")

        db = CloudDB("postgresql://paul:pass@host:26257/defaultdb?sslmode=require")
        res = db.check_health()
        self.assertFalse(res["ok"])
        self.assertIn("Connection timed out", res["error"])

    @patch("src.cloud_db.psycopg2.connect")
    def test_init_schema_success(self, mock_connect):
        mock_conn = MagicMock()
        mock_cur = MagicMock()
        mock_connect.return_value.__enter__.return_value = mock_conn
        mock_conn.cursor.return_value.__enter__.return_value = mock_cur

        db = CloudDB("postgresql://paul:pass@host:26257/defaultdb?sslmode=require")
        res = db.init_schema()
        self.assertTrue(res["ok"])
        self.assertTrue(mock_cur.execute.called)

    @patch("src.cloud_db.psycopg2.connect")
    def test_sync_run(self, mock_connect):
        mock_conn = MagicMock()
        mock_cur = MagicMock()
        mock_connect.return_value.__enter__.return_value = mock_conn
        mock_conn.cursor.return_value.__enter__.return_value = mock_cur

        db = CloudDB("postgresql://paul:pass@host:26257/defaultdb?sslmode=require")
        run_data = {
            "id": "run_test_123",
            "task_id": "T1",
            "role": "insight-executor",
            "runtime": "codex",
            "model": "gpt-6-astra",
            "status": "running",
            "pid": 1234,
            "started_at": "2026-09-30T19:00:00Z",
        }
        self.assertTrue(db.sync_run(run_data))
        self.assertTrue(mock_cur.execute.called)

    @patch("src.cloud_db.psycopg2.connect")
    def test_sync_equity_snapshot(self, mock_connect):
        mock_conn = MagicMock()
        mock_cur = MagicMock()
        mock_connect.return_value.__enter__.return_value = mock_conn
        mock_conn.cursor.return_value.__enter__.return_value = mock_cur

        db = CloudDB("postgresql://paul:pass@host:26257/defaultdb?sslmode=require")
        eq_data = {
            "equity": 108613.0,
            "hwm": 110414.0,
            "drawdown_pct": -1.63,
            "day_pnl": 0.0,
        }
        self.assertTrue(db.sync_equity_snapshot(eq_data))
        self.assertTrue(mock_cur.execute.called)


if __name__ == "__main__":
    unittest.main()
