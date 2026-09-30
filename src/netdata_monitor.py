"""Модуль интеграции и проверки системного мониторинга Netdata.

Опрашивает локальный или удаленный агент Netdata API (по умолчанию http://127.0.0.1:19999).
Собирает статус здоровья узла, алерты, системные ресурсы и версию.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys
from typing import Any
import urllib.error
import urllib.request


DEFAULT_NETDATA_URL = "http://127.0.0.1:19999"
DEFAULT_TIMEOUT = 2.0


def check_netdata_health(
    base_url: str = DEFAULT_NETDATA_URL,
    timeout: float = DEFAULT_TIMEOUT,
) -> dict[str, Any]:
    """Проверяет доступность и ключевые метрики Netdata через REST API.

    Использует эндпоинты /api/v1/info и /api/v1/alarms?all.
    Возвращает словарь с нормализованным статусом без выбрасывания исключений.
    """
    clean_url = base_url.rstrip("/")
    info_url = f"{clean_url}/api/v1/info"

    result: dict[str, Any] = {
        "available": False,
        "url": clean_url,
        "version": None,
        "os_name": None,
        "cpu_cores": None,
        "alarms_critical": 0,
        "alarms_warning": 0,
        "alarms_total": 0,
        "error": None,
    }

    try:
        req = urllib.request.Request(
            info_url,
            headers={"User-Agent": "OKX-Bot-Netdata-Monitor/1.0", "Accept": "application/json"},
        )
        with urllib.request.urlopen(req, timeout=timeout) as response:
            if response.status != 200:
                result["error"] = f"HTTP status {response.status}"
                return result
            data = json.loads(response.read().decode("utf-8"))

        result["available"] = True
        result["version"] = data.get("version")
        result["os_name"] = data.get("os_name")
        result["cpu_cores"] = data.get("cores_total")

        # Опрос активных алертов
        alarms_data = data.get("alarms", {})
        if isinstance(alarms_data, dict):
            result["alarms_critical"] = int(alarms_data.get("critical", 0))
            result["alarms_warning"] = int(alarms_data.get("warning", 0))
            result["alarms_total"] = int(alarms_data.get("normal", 0)) + result["alarms_critical"] + result["alarms_warning"]

    except urllib.error.URLError as exc:
        result["error"] = f"Connection error: {exc.reason}"
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        result["error"] = f"Malformed JSON from Netdata API: {exc}"
    except TimeoutError:
        result["error"] = "Connection timed out"
    except Exception as exc:
        result["error"] = f"{type(exc).__name__}: {exc}"

    return result


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Проверка статуса агента мониторинга Netdata")
    parser.add_argument("--url", default=DEFAULT_NETDATA_URL, help=f"URL Netdata (default: {DEFAULT_NETDATA_URL})")
    parser.add_argument("--timeout", type=float, default=DEFAULT_TIMEOUT, help="Таймаут запроса в секундах")
    parser.add_argument("--json", action="store_true", help="Вывести результат в формате JSON")
    args = parser.parse_args(argv)

    status = check_netdata_health(base_url=args.url, timeout=args.timeout)

    if args.json:
        print(json.dumps(status, ensure_ascii=False, indent=2))
    else:
        print(f"Netdata URL: {status['url']}")
        print(f"Доступность: {'Онлайн' if status['available'] else 'Офлайн'}")
        if status["available"]:
            print(f"Версия: {status['version']}")
            print(f"ОС: {status['os_name']} (Ядер: {status['cpu_cores']})")
            print(f"Алерты: Критических={status['alarms_critical']}, Предупреждений={status['alarms_warning']}")
        else:
            print(f"Причина: {status['error']}")

    return 0 if status["available"] else 1


if __name__ == "__main__":
    sys.exit(main())
