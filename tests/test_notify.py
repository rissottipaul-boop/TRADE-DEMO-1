"""Тесты модуля алертов Sentinel (src/notify.py).

Проверяет:
1. Работу без токенов (запись в alerts.jsonl, delivered_telegram=False).
2. Мок Telegram (URL, payload).
3. Обработку сетевой ошибки (URLError -> False без исключений).
4. Дедупликацию (cooldown блокирует повтор с тем же alert_key).
5. Специализированные функции Sentinel.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import MagicMock, patch
import urllib.error

from src import notify


def _read_records(path: Path) -> list[dict]:
    with open(path, encoding="utf-8") as fh:
        return [json.loads(line) for line in fh if line.strip()]


def _clear_tokens(mock_env: dict) -> None:
    mock_env.pop("TELEGRAM_BOT_TOKEN", None)
    mock_env.pop("TELEGRAM_CHAT_ID", None)


class NotifyFallbackTests(unittest.TestCase):
    def setUp(self) -> None:
        notify._LAST_SENT.clear()
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.alerts = Path(self.tmp.name) / "alerts.jsonl"

    def test_no_tokens_writes_jsonl(self) -> None:
        """Без токенов: запись в jsonl, delivered_telegram=False, без исключений."""
        with patch.dict(os.environ, {}, clear=False) as env, patch.object(
            notify, "ALERTS_PATH", self.alerts
        ):
            _clear_tokens(env)
            ok = notify.send_alert(notify.INFO, "Заголовок", "Текст", alert_key="k1")
        self.assertTrue(ok)
        records = _read_records(self.alerts)
        self.assertEqual(len(records), 1)
        rec = records[0]
        self.assertEqual(rec["level"], "INFO")
        self.assertEqual(rec["title"], "Заголовок")
        self.assertEqual(rec["message"], "Текст")
        self.assertFalse(rec["delivered_telegram"])
        self.assertTrue(rec["timestamp"])

    def test_empty_tokens_treated_as_missing(self) -> None:
        """Пустые строки токенов — то же, что отсутствие."""
        with patch.dict(
            os.environ, {"TELEGRAM_BOT_TOKEN": "", "TELEGRAM_CHAT_ID": "  "}, clear=False
        ), patch.object(notify, "ALERTS_PATH", self.alerts):
            ok = notify.send_alert(notify.WARNING, "T", "M")
        self.assertTrue(ok)
        self.assertFalse(_read_records(self.alerts)[0]["delivered_telegram"])

    def test_cli_test_without_tokens_exit_zero(self) -> None:
        """CLI --test без токенов завершается кодом 0 и пишет jsonl."""
        with patch.dict(os.environ, {}, clear=False) as env, patch.object(
            notify, "ALERTS_PATH", self.alerts
        ), patch.object(notify, "_alerts_file", return_value=self.alerts):
            _clear_tokens(env)
            code = notify.main(["--test", "Тестовое сообщение"])
        self.assertEqual(code, 0)
        records = _read_records(self.alerts)
        self.assertEqual(len(records), 1)
        self.assertFalse(records[0]["delivered_telegram"])

    def test_cli_status_exit_zero(self) -> None:
        """CLI --status завершается кодом 0 и не раскрывает секреты."""
        with patch.dict(os.environ, {}, clear=False) as env, patch.object(
            notify, "ALERTS_PATH", self.alerts
        ), patch.object(notify, "_alerts_file", return_value=self.alerts):
            _clear_tokens(env)
            with patch("sys.stdout") as _:
                code = notify.main(["--status"])
        self.assertEqual(code, 0)


def _fake_ok_response() -> MagicMock:
    mock_response = MagicMock()
    mock_response.read.return_value = b'{"ok": true}'
    mock_response.__enter__.return_value = mock_response
    return mock_response


class NotifyTelegramTests(unittest.TestCase):
    ENV = {"TELEGRAM_BOT_TOKEN": "TOKEN123", "TELEGRAM_CHAT_ID": "999"}

    def setUp(self) -> None:
        notify._LAST_SENT.clear()
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.alerts = Path(self.tmp.name) / "alerts.jsonl"

    def test_telegram_mock_url_and_payload(self) -> None:
        """Мок urlopen: проверка URL и payload запроса."""
        mock_response = _fake_ok_response()
        with patch.dict(os.environ, self.ENV, clear=False), patch.object(
            notify, "ALERTS_PATH", self.alerts
        ), patch("urllib.request.urlopen", return_value=mock_response) as mock_urlopen:
            ok = notify.send_alert(notify.CRITICAL, "Заголовок", "Текст", alert_key="m1")
        self.assertTrue(ok)
        self.assertEqual(mock_urlopen.call_count, 1)
        request = mock_urlopen.call_args[0][0]
        self.assertEqual(
            request.full_url, "https://api.telegram.org/botTOKEN123/sendMessage"
        )
        payload = json.loads(request.data.decode("utf-8"))
        self.assertEqual(payload["chat_id"], "999")
        self.assertEqual(payload["parse_mode"], "HTML")
        self.assertIn("Заголовок", payload["text"])
        self.assertIn("Текст", payload["text"])
        records = _read_records(self.alerts)
        self.assertTrue(records[0]["delivered_telegram"])

    def test_network_error_returns_false(self) -> None:
        """URLError обрабатывается gracefully: возвращается False."""
        with patch.dict(os.environ, self.ENV, clear=False), patch.object(
            notify, "ALERTS_PATH", self.alerts
        ), patch(
            "urllib.request.urlopen",
            side_effect=urllib.error.URLError("boom"),
        ):
            ok = notify.send_alert(notify.WARNING, "T", "M", alert_key="net1")
        self.assertFalse(ok)
        records = _read_records(self.alerts)
        self.assertEqual(len(records), 1)
        self.assertFalse(records[0]["delivered_telegram"])

    def test_dedup_blocks_same_key(self) -> None:
        """Повтор с тем же alert_key в пределах cooldown не шлёт в Telegram."""
        mock_response = _fake_ok_response()
        with patch.dict(os.environ, self.ENV, clear=False), patch.object(
            notify, "ALERTS_PATH", self.alerts
        ), patch("urllib.request.urlopen", return_value=mock_response) as mock_urlopen:
            first = notify.send_alert(notify.INFO, "T", "M1", alert_key="dup")
            second = notify.send_alert(notify.INFO, "T", "M2", alert_key="dup")
            third = notify.send_alert(notify.INFO, "T", "M3", alert_key="other")
        self.assertTrue(first)
        self.assertTrue(second)
        self.assertTrue(third)
        self.assertEqual(mock_urlopen.call_count, 2)

    def test_dedup_expires_after_cooldown(self) -> None:
        """После истечения cooldown повтор отправляется снова."""
        mock_response = _fake_ok_response()
        with patch.dict(os.environ, self.ENV, clear=False), patch.object(
            notify, "ALERTS_PATH", self.alerts
        ), patch("urllib.request.urlopen", return_value=mock_response) as mock_urlopen:
            notify.send_alert(notify.INFO, "T", "M1", alert_key="exp", cooldown_s=60)
            notify._LAST_SENT["exp"] -= 61
            notify.send_alert(notify.INFO, "T", "M2", alert_key="exp", cooldown_s=60)
        self.assertEqual(mock_urlopen.call_count, 2)


class NotifySpecializedTests(unittest.TestCase):
    def setUp(self) -> None:
        notify._LAST_SENT.clear()
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.alerts = Path(self.tmp.name) / "alerts.jsonl"
        self.patchers = [
            patch.dict(os.environ, {}, clear=False),
            patch.object(notify, "ALERTS_PATH", self.alerts),
        ]
        self.env = self.patchers[0].start()
        self.addCleanup(self.patchers[0].stop)
        self.patchers[1].start()
        self.addCleanup(self.patchers[1].stop)
        _clear_tokens(self.env)

    def test_specialized_levels_and_content(self) -> None:
        """Специализированные функции: уровни и содержимое сообщений."""
        self.assertTrue(notify.alert_kill_switch("тестовая причина"))
        self.assertTrue(
            notify.alert_breaker("daily_loss", 150.0, 100.0, details="превышение")
        )
        self.assertTrue(notify.alert_breaker("daily_loss2", 50.0, 100.0))
        self.assertTrue(notify.alert_divergence("дрейф", inst_id="BTC-USDT"))
        self.assertTrue(notify.alert_engine_crash("segfault", pid=1234))
        self.assertTrue(notify.alert_guard_denial("codex", "place_order", reason="deny"))

        records = _read_records(self.alerts)
        self.assertEqual(len(records), 6)
        levels = [r["level"] for r in records]
        self.assertEqual(
            levels,
            [
                "CRITICAL",  # kill_switch
                "CRITICAL",  # breaker breached
                "WARNING",  # breaker near limit
                "WARNING",  # divergence
                "CRITICAL",  # engine_crash
                "WARNING",  # guard_denial
            ],
        )
        self.assertIn("тестовая причина", records[0]["message"])
        self.assertIn("daily_loss", records[1]["message"])
        self.assertIn("BTC-USDT", records[3]["title"])
        self.assertIn("1234", records[4]["message"])
        self.assertIn("codex", records[5]["message"])

    def test_invalid_level_raises(self) -> None:
        """Неизвестный уровень — ValueError."""
        with self.assertRaises(ValueError):
            notify.send_alert("NOPE", "T", "M")


if __name__ == "__main__":
    unittest.main()
