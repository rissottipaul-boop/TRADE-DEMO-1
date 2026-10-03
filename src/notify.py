"""Модуль алертов Sentinel (ALERTS-IMPL).

Отправка уведомлений Ops Sentinel в Telegram с безопасным fallback:
если токены TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID отсутствуют, событие
записывается в data/alerts.jsonl и возвращается управление без исключений.

Поддерживает дедупликацию по alert_key в пределах cooldown_s.
"""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import html
import json
import logging
import os
from pathlib import Path
import sys
import threading
import time
from typing import Optional
import urllib.error
import urllib.request

logger = logging.getLogger("notify")

INFO = "INFO"
WARNING = "WARNING"
CRITICAL = "CRITICAL"
LEVELS = (INFO, WARNING, CRITICAL)

TELEGRAM_TIMEOUT_S = 5.0
DEFAULT_COOLDOWN_S = 60

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
ALERTS_PATH = _PROJECT_ROOT / "data" / "alerts.jsonl"

_LAST_SENT: dict[str, float] = {}
_LAST_SENT_LOCK = threading.Lock()


def _alerts_file() -> Path:
    """Путь к jsonl-журналу алертов (переопределяется env ALERTS_JSONL_PATH)."""
    override = os.getenv("ALERTS_JSONL_PATH")
    if override:
        return Path(override)
    return ALERTS_PATH


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _telegram_credentials() -> tuple[str, str]:
    """Возвращает (token, chat_id); пустые строки, если не заданы."""
    token = (os.getenv("TELEGRAM_BOT_TOKEN") or "").strip()
    chat_id = (os.getenv("TELEGRAM_CHAT_ID") or "").strip()
    return token, chat_id


def _check_cooldown(alert_key: Optional[str], cooldown_s: int) -> bool:
    """True — отправка разрешена (и метка обновлена); False — пропуск по cooldown."""
    if not alert_key:
        return True
    now = time.time()
    with _LAST_SENT_LOCK:
        last = _LAST_SENT.get(alert_key)
        if last is not None and (now - last) < cooldown_s:
            return False
        _LAST_SENT[alert_key] = now
    return True


def _append_jsonl(level: str, title: str, message: str, delivered_telegram: bool) -> None:
    """Дописывает событие в alerts.jsonl; ошибки только логируются."""
    record = {
        "timestamp": _utc_now_iso(),
        "level": level,
        "title": title,
        "message": message,
        "delivered_telegram": delivered_telegram,
    }
    try:
        path = _alerts_file()
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(record, ensure_ascii=False) + "\n")
    except Exception as exc:
        logger.error("Не удалось записать алерт в %s: %s", _alerts_file(), exc)


def _send_telegram(token: str, chat_id: str, level: str, title: str, message: str) -> bool:
    """Отправка сообщения в Telegram через urllib. Ошибки не пробрасываются."""
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    text = f"<b>[{level}] {html.escape(title)}</b>\n{html.escape(message)}"
    payload = json.dumps(
        {"chat_id": chat_id, "text": text, "parse_mode": "HTML"},
        ensure_ascii=False,
    ).encode("utf-8")
    request = urllib.request.Request(
        url,
        data=payload,
        headers={"Content-Type": "application/json", "User-Agent": "OKX-Bot-Notify/1.0"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=TELEGRAM_TIMEOUT_S) as response:
            body = response.read().decode("utf-8", errors="replace")
        try:
            data = json.loads(body) if body else {}
        except json.JSONDecodeError:
            return True  # ответ получен, тело не JSON — считаем доставленным
        if isinstance(data, dict) and "ok" in data:
            return bool(data["ok"])
        return True
    except urllib.error.URLError as exc:
        logger.warning("Telegram недоступен (URLError): %s", exc)
    except TimeoutError:
        logger.warning("Telegram недоступен: таймаут %s с", TELEGRAM_TIMEOUT_S)
    except Exception as exc:
        logger.warning("Ошибка отправки Telegram: %s: %s", type(exc).__name__, exc)
    return False


def send_alert(
    level: str,
    title: str,
    message: str,
    alert_key: Optional[str] = None,
    cooldown_s: int = DEFAULT_COOLDOWN_S,
) -> bool:
    """Отправляет алерт Sentinel.

    Возвращает True, если событие обработано (доставлено в Telegram или
    записано в локальный журнал), False — при сетевом сбое Telegram.
    Повтор с тем же alert_key в пределах cooldown_s пропускает отправку
    в Telegram. Никогда не бросает исключения в вызывающий код.
    """
    normalized = (level or "").upper()
    if normalized not in LEVELS:
        raise ValueError(f"Неизвестный уровень алерта: {level!r} (ожидается {LEVELS})")

    if not _check_cooldown(alert_key, cooldown_s):
        logger.debug("Пропуск повторного алерта %r в пределах cooldown %s с", alert_key, cooldown_s)
        return True

    token, chat_id = _telegram_credentials()
    if not token or not chat_id:
        logger.info("[%s] %s: %s", normalized, title, message)
        _append_jsonl(normalized, title, message, delivered_telegram=False)
        return True

    delivered = _send_telegram(token, chat_id, normalized, title, message)
    if not delivered:
        logger.warning("[%s] %s: %s (Telegram не доставил)", normalized, title, message)
    _append_jsonl(normalized, title, message, delivered_telegram=delivered)
    return delivered


def alert_kill_switch(reason: str, triggered_by: str = "risk_core") -> bool:
    """CRITICAL-алерт о срабатывании kill-switch."""
    return send_alert(
        CRITICAL,
        "Kill-switch активирован",
        f"Причина: {reason} (инициатор: {triggered_by})",
        alert_key="kill_switch",
    )


def alert_breaker(
    breaker_name: str,
    current_value: float,
    limit_value: float,
    details: str = "",
) -> bool:
    """Алерт о срабатывании/приближении брейкера.

    Превышение лимита — CRITICAL, приближение без превышения — WARNING.
    """
    breached = current_value >= limit_value
    level = CRITICAL if breached else WARNING
    state = "превышен" if breached else "близок к лимиту"
    text = f"Брейкер {breaker_name} {state}: {current_value} при лимите {limit_value}"
    if details:
        text += f". {details}"
    return send_alert(
        level,
        f"Брейкер {breaker_name}: {state}",
        text,
        alert_key=f"breaker:{breaker_name}",
    )


def alert_divergence(details: str, inst_id: str = "") -> bool:
    """WARNING-алерт о расхождении данных/позиций."""
    suffix = f" [{inst_id}]" if inst_id else ""
    return send_alert(
        WARNING,
        f"Расхождение данных{suffix}",
        details,
        alert_key=f"divergence:{inst_id or 'all'}",
    )


def alert_engine_crash(error_message: str, pid: Optional[int] = None) -> bool:
    """CRITICAL-алерт о падении движка."""
    text = f"Движок аварийно завершён: {error_message}"
    if pid is not None:
        text += f" (pid={pid})"
    return send_alert(
        CRITICAL,
        "Падение движка",
        text,
        alert_key="engine_crash",
    )


def alert_guard_denial(client: str, command: str, reason: str = "") -> bool:
    """WARNING-алерт об отказе guard."""
    text = f"Guard отклонил команду {command!r} клиента {client}"
    if reason:
        text += f": {reason}"
    return send_alert(
        WARNING,
        "Отказ guard",
        text,
        alert_key=f"guard_denial:{client}:{command}",
    )


def _status_payload() -> dict:
    token, chat_id = _telegram_credentials()
    path = _alerts_file()
    count: Optional[int] = None
    try:
        if path.exists():
            with open(path, encoding="utf-8") as fh:
                count = sum(1 for _ in fh)
        else:
            count = 0
    except Exception as exc:
        logger.warning("Не удалось прочитать %s: %s", path, exc)
    return {
        "telegram_configured": bool(token and chat_id),
        "alerts_file": str(path),
        "alerts_recorded": count,
    }


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="Алерты Sentinel: Telegram + data/alerts.jsonl")
    parser.add_argument("--test", metavar="TEXT", default=None, help="Отправить тестовый алерт")
    parser.add_argument("--status", action="store_true", help="Показать состояние модуля алертов")
    args = parser.parse_args(argv)

    if args.status:
        status = _status_payload()
        print(f"Telegram: {'настроен' if status['telegram_configured'] else 'не настроен (fallback в jsonl)'}")
        print(f"Журнал: {status['alerts_file']}")
        print(f"Записано алертов: {status['alerts_recorded']}")
        return 0

    if args.test is not None:
        ok = send_alert(INFO, "Тестовый алерт", args.test, alert_key="cli_test")
        print("Тестовый алерт обработан" if ok else "Тестовый алерт не доставлен в Telegram (см. журнал)")
        return 0

    parser.print_help()
    return 2


if __name__ == "__main__":
    sys.exit(main())
