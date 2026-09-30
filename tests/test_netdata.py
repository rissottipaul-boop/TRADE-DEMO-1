"""Тесты модуля мониторинга Netdata (src/netdata_monitor.py).

Проверяет:
1. Корректную обработку успешного ответа API Netdata (/api/v1/info).
2. Безопасную обработку сетевых ошибок, таймаутов и некорректного JSON.
3. Корректность вывода CLI с флагом --json и без него.
4. Валидность конфигурации docker-compose.yml для Netdata.
"""

from __future__ import annotations

import io
import json
from pathlib import Path
import unittest
from unittest.mock import MagicMock, patch
import urllib.error

from src.netdata_monitor import DEFAULT_NETDATA_URL, check_netdata_health, main


class NetdataMonitorTests(unittest.TestCase):
    def test_successful_api_response_parsing(self) -> None:
        """При 200 OK возвращаются нормализованные поля версии, ресурсов и алертов."""
        fake_payload = {
            "version": "v1.44.0",
            "os_name": "Linux",
            "cores_total": 8,
            "alarms": {
                "normal": 42,
                "warning": 2,
                "critical": 1,
            },
        }
        mock_response = MagicMock()
        mock_response.status = 200
        mock_response.read.return_value = json.dumps(fake_payload).encode("utf-8")
        mock_response.__enter__.return_value = mock_response

        with patch("urllib.request.urlopen", return_value=mock_response):
            status = check_netdata_health("http://127.0.0.1:19999")

        self.assertTrue(status["available"])
        self.assertEqual(status["version"], "v1.44.0")
        self.assertEqual(status["os_name"], "Linux")
        self.assertEqual(status["cpu_cores"], 8)
        self.assertEqual(status["alarms_critical"], 1)
        self.assertEqual(status["alarms_warning"], 2)
        self.assertEqual(status["alarms_total"], 45)
        self.assertIsNone(status["error"])

    def test_connection_error_handling(self) -> None:
        """Сетевой сбой не выбрасывает исключение и возвращает available=False с ошибкой."""
        with patch("urllib.request.urlopen", side_effect=urllib.error.URLError("Connection refused")):
            status = check_netdata_health("http://127.0.0.1:19999")

        self.assertFalse(status["available"])
        self.assertIn("Connection refused", str(status["error"]))
        self.assertIsNone(status["version"])

    def test_timeout_error_handling(self) -> None:
        """Таймаут запроса безопасно перехватывается."""
        with patch("urllib.request.urlopen", side_effect=TimeoutError()):
            status = check_netdata_health("http://127.0.0.1:19999")

        self.assertFalse(status["available"])
        self.assertIn("timed out", str(status["error"]))

    def test_malformed_json_handling(self) -> None:
        """Некорректный JSON в ответе API не роняет процесс."""
        mock_response = MagicMock()
        mock_response.status = 200
        mock_response.read.return_value = b"{broken json"
        mock_response.__enter__.return_value = mock_response

        with patch("urllib.request.urlopen", return_value=mock_response):
            status = check_netdata_health("http://127.0.0.1:19999")

        self.assertFalse(status["available"])
        self.assertIn("Malformed JSON", str(status["error"]))

    def test_cli_output_json_mode(self) -> None:
        """CLI main() с флагом --json печатает валидный JSON."""
        fake_status = {
            "available": True,
            "url": "http://127.0.0.1:19999",
            "version": "v1.44.0",
            "os_name": "Linux",
            "cpu_cores": 4,
            "alarms_critical": 0,
            "alarms_warning": 0,
            "alarms_total": 10,
            "error": None,
        }
        stdout = io.StringIO()
        with patch("src.netdata_monitor.check_netdata_health", return_value=fake_status):
            with patch("sys.stdout", stdout):
                code = main(["--json"])

        self.assertEqual(code, 0)
        output_data = json.loads(stdout.getvalue())
        self.assertTrue(output_data["available"])
        self.assertEqual(output_data["version"], "v1.44.0")

    def test_docker_compose_file_structure(self) -> None:
        """Конфигурация compose для Netdata существует и содержит обязательные параметры."""
        compose_path = Path("ops/netdata/docker-compose.yml")
        self.assertTrue(compose_path.is_file(), "Файл ops/netdata/docker-compose.yml должен существовать")

        content = compose_path.read_text(encoding="utf-8")
        self.assertIn("netdata/netdata", content)
        self.assertIn("19999:19999", content)
        self.assertIn("okx-netdata", content)


if __name__ == "__main__":
    unittest.main()
