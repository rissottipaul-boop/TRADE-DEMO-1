"""Локальный аудит AI: безопасные проекции, реальные usage и read-only схемы.

Здесь нет модели, API-клиента, отправки данных или торговых инструментов.
SQLite делает запись событий и резервирование ключей атомарными между процессами.
Стоимость берётся только из reported run metadata; неизвестная стоимость не равна 0.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import re
import secrets
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
MAX_TEXT = 4096
MAX_ITEMS = 100
MAX_DEPTH = 8
MAX_RUN_BYTES = 2 * 1024 * 1024
_SENSITIVE = re.compile(r"(?i)(?:^|[_\W])(?:api[_-]?key|access[_-]?token|refresh[_-]?token|token|secret|password|passwd|passphrase|authorization|cookie|credentials|private[_-]?key)(?:$|[_\W])")
_ASSIGNMENT = re.compile(r'''(?i)((?<![A-Za-z0-9_])["']?([A-Za-z_][A-Za-z0-9_.-]{0,120})["']?\s*[:=]\s*)(?:"(?:\\.|[^"\\\r\n])*"|'(?:\\.|[^'\\\r\n])*'|[^\s,;|]+)''')
_KEY_BLOCK = re.compile(r"-----BEGIN (?:[A-Z ]*PRIVATE KEY|OPENSSH PRIVATE KEY)-----[\s\S]*?(?:-----END [A-Z ]*PRIVATE KEY-----|$)")
_IDENTIFIER = re.compile(r"[A-Za-z0-9_-]{1,80}\Z")
_RUN_ID = re.compile(r"run_[A-Za-z0-9_-]{1,80}\Z")
_SAFE_FIELDS = frozenset((
    "id", "task_id", "run_id", "runtime", "model", "role", "status", "stage",
    "ok", "outcome", "quality", "count", "cursor", "has_more", "after", "limit",
    "tool", "arguments", "tools", "method", "path", "operation", "request",
    "result", "duration_ms", "error", "error_code", "code", "exit_code",
    "provenance", "source", "reference", "observed_at", "ts", "type", "events",
    "completed", "failed", "unsupported", "supported", "cost", "usd", "tokens",
    "input_tokens", "output_tokens", "cached_input_tokens", "total_tokens",
    "changed_files", "files", "checks", "name", "command", "passed", "warnings",
    "message_code", "description_length", "reason_code", "capabilities", "state",
    "result_quality", "score", "total", "correct", "rule", "verdict", "issues",
    "reason", "summary", "next_step", "evidence", "confirmed", "recovery",
))
_CONTENT_FIELDS = frozenset(("text", "prompt", "description", "content", "message", "output", "stdout", "stderr", "markdown", "query"))


def _number(value: Any, *, integer: bool = False) -> int | float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    if not 0 <= value <= 10 ** 15 or not math.isfinite(value) or (integer and not isinstance(value, int)):
        return None
    return value


def redact(value: Any, _depth: int = 0, *, max_text: int = MAX_TEXT) -> Any:
    """Ограниченная рекурсивная фильтрация; не читает окружение или файлы ключей.

    Произвольный непомеченный секрет распознать невозможно. Для журнала используйте
    safe_projection: она также исключает содержимое промптов и неизвестные поля.
    """
    if type(max_text) is not int or not 1 <= max_text <= 1048576:
        raise ValueError("max_text должен быть от 1 до 1048576")
    if _depth >= MAX_DEPTH:
        return "[DEPTH LIMIT]"
    if isinstance(value, str):
        clean = value[:max(65536, max_text)]
        input_truncated = len(clean) < len(value)
        clean = _KEY_BLOCK.sub("[REDACTED PRIVATE KEY]", clean)
        def redact_assignment(match):
            key = re.sub(r"[^a-z]", "", match[2].lower())
            sensitive = any(part in key for part in ("apikey", "token", "secret", "password", "passwd", "passphrase", "authorization", "cookie"))
            return match[1] + "[REDACTED]" if sensitive else match[0]
        clean = _ASSIGNMENT.sub(redact_assignment, clean)
        clean = re.sub(r"(?i)\bBearer\s+[^\s,;\"']+", "Bearer [REDACTED]", clean)
        clean = re.sub(r"\b(?:sk-[A-Za-z0-9_-]{8,}|gh[pousr]_[A-Za-z0-9_]{8,}|github_pat_[A-Za-z0-9_]{8,}|AIza[A-Za-z0-9_-]{15,})", "[REDACTED]", clean)
        clean = re.sub(r"\beyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\b", "[REDACTED JWT]", clean)
        return clean[:max_text] + ("[TRUNCATED]" if len(clean) > max_text or input_truncated else "")
    if isinstance(value, dict):
        result = {}
        for index, (key, child) in enumerate(value.items()):
            if index >= MAX_ITEMS:
                result["_truncated"] = True
                break
            if not isinstance(key, str):
                continue
            safe_key = redact(key, _depth + 1, max_text=max_text)
            normalized = re.sub(r"[^a-z]", "", key.lower())
            sensitive = bool(_SENSITIVE.search(key)) or any(part in normalized for part in
                ("apikey", "secret", "password", "passphrase", "authorization", "privatekey")) or normalized.endswith("token")
            result[safe_key] = "[REDACTED]" if sensitive else redact(child, _depth + 1, max_text=max_text)
        return result
    if isinstance(value, (list, tuple)):
        result = [redact(child, _depth + 1, max_text=max_text) for child in value[:MAX_ITEMS]]
        return result + (["[TRUNCATED]"] if len(value) > MAX_ITEMS else [])
    if isinstance(value, int) and not isinstance(value, bool):
        return value if abs(value) <= 10 ** 15 else "[NUMBER LIMIT]"
    if value is None or isinstance(value, bool):
        return value
    if isinstance(value, float):
        return value if math.isfinite(value) else None
    return "[UNSUPPORTED VALUE]"


def safe_projection(value: Any, _depth: int = 0) -> Any:
    """Метаданные для аудита: исключает свободный контент и неизвестные поля."""
    if _depth >= MAX_DEPTH:
        return "[DEPTH LIMIT]"
    if isinstance(value, dict):
        result = {}
        for index, (key, child) in enumerate(value.items()):
            if index >= MAX_ITEMS:
                result["_truncated"] = True
                break
            if not isinstance(key, str):
                continue
            if key in _CONTENT_FIELDS:
                if isinstance(child, str):
                    result[key + "_length"] = len(child)
            elif key in _SAFE_FIELDS:
                result[key] = safe_projection(child, _depth + 1)
        return redact(result)
    if isinstance(value, (list, tuple)):
        return [safe_projection(child, _depth + 1) for child in value[:MAX_ITEMS]]
    return redact(value)


class ToolValidationError(ValueError):
    """Вызов отвергнут до исполнения, имя и входные значения не печатаются."""


def _identifier(value: Any, *, run: bool = False) -> bool:
    return isinstance(value, str) and bool((_RUN_ID if run else _IDENTIFIER).fullmatch(value))


def validate_tool_call(name: str, arguments: dict) -> dict:
    """Закрытые read-only схемы. Не является разрешением запуска/торговли."""
    if not isinstance(name, str) or not isinstance(arguments, dict):
        raise ToolValidationError("Нужны имя инструмента и объект аргументов")
    schemas = {
        "panel.state": ({}, {}), "ai.usage": ({}, {}), "ai.evals": ({}, {}),
        "board.task": ({"task_id": "task"}, {}),
        "task.read": ({"task_id": "task"}, {}),
        "runs.list": ({}, {"limit": "limit"}),
        "run.get": ({"run_id": "run"}, {}),
        "run.result": ({"run_id": "run"}, {}),
        "run.events": ({"run_id": "run"}, {"after": "cursor", "limit": "limit"}),
        "assistant.draft": ({"description": "description"}, {}),
        "assistant.role": ({"description": "description"}, {}),
        "assistant.handoff": ({"task_id": "task"}, {"run_id": "run"}),
        "assistant.review": ({"task_id": "task"}, {"run_id": "run"}),
        "task.handoff": ({"task_id": "task"}, {"run_id": "run"}),
        "task.review": ({"task_id": "task"}, {"run_id": "run"}),
        "diff.review": ({"task_id": "task"}, {"run_id": "run"}),
        "sources.search": ({"query": "query"}, {}),
        "assistant.specialist": ({"role": "role", "tool": "tool", "arguments": "object"}, {}),
    }
    if name not in schemas:
        raise ToolValidationError("Инструмент отсутствует в read-only allowlist")
    required, optional = schemas[name]
    if not set(required).issubset(arguments) or set(arguments) - set(required) - set(optional):
        raise ToolValidationError("Неизвестные или пропущенные аргументы инструмента")
    for key, value in arguments.items():
        kind = required.get(key) or optional[key]
        valid = {
            "task": lambda: _identifier(value),
            "run": lambda: _identifier(value, run=True),
            "cursor": lambda: type(value) is int and 0 <= value <= 1000000,
            "limit": lambda: type(value) is int and 1 <= value <= 100,
            "description": lambda: isinstance(value, str) and 1 <= len(value.strip()) <= 4000 and "\x00" not in value,
            "query": lambda: isinstance(value, str) and 1 <= len(value.strip()) <= 200 and "\x00" not in value,
            "role": lambda: value in ("crypto-insight-hunter", "insight-executor", "ops-sentinel") if isinstance(value, str) else False,
            "tool": lambda: isinstance(value, str),
            "object": lambda: isinstance(value, dict),
        }[kind]()
        if not valid:
            raise ToolValidationError("Неверный тип или предел аргумента инструмента")
    if name == "assistant.specialist":
        allowed = {
            "crypto-insight-hunter": {"task.read", "sources.search"},
            "insight-executor": {"task.read", "diff.review"},
            "ops-sentinel": {"task.read"},
        }
        if arguments["tool"] not in allowed[arguments["role"]]:
            raise ToolValidationError("Инструмент недоступен выбранному специалисту")
        validate_tool_call(arguments["tool"], arguments["arguments"])
    return dict(arguments)


def error_status(code: Any) -> dict:
    """Ошибки API/рантайма видны, восстановление не повторяет неизвестный исход."""
    known = {
        "quota": ("Лимит модели исчерпан", "Зафиксировать handoff и завершение процесса; затем штатная ротация сессии"),
        "rate_limit": ("API временно ограничивает запросы", "Проверить исход запроса; повторить только подтверждённо неисполненный read-only запрос после паузы"),
        "authentication": ("Авторизация недоступна", "Человек проверяет авторизацию штатным клиентом; ключи не вводятся в панель"),
        "network": ("Нет подтверждения ответа сервера", "Сверить состояние и ID операции до повторения"),
        "unknown_outcome": ("Исход действия неизвестен", "Сверить по ID со штатным источником; повтор заблокирован до подтверждения"),
        "guard_denied": ("Guard запретил действие", "Записать needs-user; обход запрета и смена команды с тем же эффектом запрещены"),
        "tool_validation": ("Аргументы инструмента отвергнуты", "Исправить запрос по закрытой read-only схеме; исполнение не началось"),
        "runtime": ("Рантайм завершился с ошибкой", "Проверить журнал и незавершённые действия; сохранить handoff"),
    }
    safe_code = code if isinstance(code, str) and code in known else "unknown"
    message, recovery = known.get(safe_code, ("Ошибка AI или API", "Проверить штатный журнал и подтверждённое состояние; автоматический повтор выключен"))
    return {"code": safe_code, "message": message, "recovery": recovery, "automatic_retry": False}


def _database(root: Path, *, create: bool) -> sqlite3.Connection | None:
    path = Path(root).resolve() / "data" / "ai" / "observability.sqlite3"
    if not create and not path.exists():
        return None
    if create:
        path.parent.mkdir(parents=True, exist_ok=True)
        db = sqlite3.connect(path, timeout=10, isolation_level=None)
        try:
            db.execute("PRAGMA busy_timeout=10000")
            db.execute("CREATE TABLE IF NOT EXISTS actions(cursor INTEGER PRIMARY KEY AUTOINCREMENT, event TEXT NOT NULL)")
            db.execute("CREATE TABLE IF NOT EXISTS idempotency(key TEXT PRIMARY KEY, digest TEXT NOT NULL, state TEXT NOT NULL, reservation TEXT NOT NULL, result TEXT, evidence TEXT, updated_at TEXT NOT NULL)")
        except Exception:
            db.close()
            raise
    else:
        db = sqlite3.connect(path.as_uri() + "?mode=ro", uri=True, timeout=10)
    db.row_factory = sqlite3.Row
    return db


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def record_action(root: Path, *, request: Any, tool: str, arguments: Any = None,
                  result: Any = None, model: str | None = None,
                  duration_ms: int | float | None = None, outcome: str = "completed",
                  run_id: str | None = None, error: Any = None) -> dict:
    """Пишет фактический серверный вызов. Свободное тело запроса не сохраняется."""
    if outcome not in ("completed", "failed", "rejected", "unknown"):
        raise ValueError("Неизвестный исход события")
    if not isinstance(tool, str) or not re.fullmatch(r"[A-Za-z0-9_./-]{1,100}", tool):
        raise ValueError("Неверное имя функции журнала")
    if run_id is not None and not _identifier(run_id, run=True):
        raise ValueError("Неверный run_id")
    event = {
        "ts": _now(), "tool": redact(tool), "request": safe_projection(request) if isinstance(request, dict) else {"description_length": len(request) if isinstance(request, str) else 0},
        "arguments": safe_projection(arguments), "result": safe_projection(result),
        "model": redact(model) if isinstance(model, str) else None,
        "duration_ms": _number(duration_ms), "outcome": outcome, "run_id": redact(run_id),
        "error": error_status(error) if error is not None or outcome in ("failed", "unknown") else None,
        "quality": "local-server-action", "provenance": "data/ai/observability.sqlite3",
    }
    db = _database(Path(root), create=True)
    assert db is not None
    try:
        cursor = db.execute("INSERT INTO actions(event) VALUES (?)", (json.dumps(event, ensure_ascii=False, allow_nan=False),)).lastrowid
    finally:
        db.close()
    return {"cursor": cursor, **event}


def read_actions(root: Path, limit: int = 50) -> list[dict]:
    if type(limit) is not int or not 1 <= limit <= 100:
        raise ValueError("limit должен быть от 1 до 100")
    db = _database(Path(root), create=False)
    if db is None:
        return []
    try:
        rows = db.execute("SELECT cursor,event FROM actions ORDER BY cursor DESC LIMIT ?", (limit,)).fetchall()
        return [{"cursor": row["cursor"], **json.loads(row["event"])} for row in rows]
    finally:
        db.close()


def _duration(started: Any, finished: Any) -> float | None:
    try:
        if not isinstance(started, str) or not isinstance(finished, str):
            return None
        start = datetime.fromisoformat(started.replace("Z", "+00:00"))
        end = datetime.fromisoformat(finished.replace("Z", "+00:00"))
        if start.tzinfo is None or end.tzinfo is None:
            return None
        return _number(round((end - start).total_seconds() * 1000, 3))
    except (ValueError, OverflowError):
        return None


def _provenance(value: Any) -> str | None:
    return redact(value) if isinstance(value, str) and 1 <= len(value.strip()) <= MAX_TEXT else None


def usage_summary(root: Path) -> dict:
    """Reported суммы частичные: не оценивает тарифы и не считает unknown нулём."""
    root = Path(root)
    runs, alerts, models = [], [], {}
    run_paths = sorted((root / "data" / "runs").glob("run_*.json"))
    if len(run_paths) > 1000:
        alerts.append({"code": "run_summary_limit", "message": "Сводка ограничена 1000 запусками; остальные расходы неизвестны", "count": len(run_paths) - 1000})
    for path in run_paths[:1000]:
        try:
            if path.stat().st_size > MAX_RUN_BYTES:
                raise ValueError("oversize")
            raw = json.loads(path.read_text(encoding="utf-8"))
            if not isinstance(raw, dict) or raw.get("id") != path.stem:
                raise ValueError("invalid run")
        except (OSError, ValueError):
            alerts.append({"code": "invalid_run_record", "run_id": path.stem, "message": "Запись запуска не прочитана; метрики неизвестны"})
            continue
        cost = raw.get("cost") if isinstance(raw.get("cost"), dict) else {}
        tokens = raw.get("tokens") if isinstance(raw.get("tokens"), dict) else {}
        cost_source = _provenance(cost.get("provenance"))
        token_source = _provenance(tokens.get("provenance"))
        usd = _number(cost.get("usd")) if cost.get("quality") == "reported" and cost_source else None
        token_numbers = {key: _number(tokens.get(key), integer=True) for key in
                         ("input_tokens", "output_tokens", "cached_input_tokens", "total_tokens")}
        token_numbers = {key: value for key, value in token_numbers.items() if value is not None}
        if tokens.get("quality") != "reported" or not token_source:
            token_numbers = {}
        model = redact(raw.get("model")) if isinstance(raw.get("model"), str) else "unknown"
        runtime = raw.get("runtime") if raw.get("runtime") in ("claude", "codex", "gemini", "muse") else "unknown"
        duration = _duration(raw.get("started_at"), raw.get("finished_at"))
        run = {"run_id": path.stem, "runtime": runtime, "model": model,
               "usd": usd, "cost_quality": "reported" if usd is not None else "unknown",
               "cost_provenance": cost_source if usd is not None else None,
               "tokens": token_numbers or None, "tokens_quality": "reported" if token_numbers else "unknown",
               "tokens_provenance": token_source if token_numbers else None,
               "duration_ms": duration, "duration_quality": "local-timestamps" if duration is not None else "unknown"}
        runs.append(run)
        bucket = models.setdefault(model, {"model": model, "runs": 0, "reported_cost_runs": 0, "unknown_cost_runs": 0, "reported_usd": 0.0})
        bucket["runs"] += 1
        bucket["reported_cost_runs" if usd is not None else "unknown_cost_runs"] += 1
        if usd is not None:
            bucket["reported_usd"] += usd
        if raw.get("status") in ("failed", "unknown"):
            alerts.append({"run_id": path.stem, **error_status("runtime" if raw["status"] == "failed" else "unknown_outcome")})
    actions = read_actions(root)
    functions = []
    db = _database(root, create=False)
    if db is not None:
        try:
            # Считаем весь локальный журнал; API отдаёт только bounded хвост.
            rows = db.execute("""SELECT json_extract(event,'$.tool') AS tool,
                COUNT(*) AS calls,
                SUM(json_extract(event,'$.outcome') IN ('failed','unknown','rejected')) AS errors,
                SUM(COALESCE(json_extract(event,'$.duration_ms'),0)) AS duration_ms,
                SUM(json_extract(event,'$.duration_ms') IS NOT NULL) AS measured_calls
                FROM actions GROUP BY tool ORDER BY calls DESC LIMIT 100""").fetchall()
            functions = [{**dict(row), "api_cost_usd": None, "cost_quality": "unknown"} for row in rows]
            action_count = db.execute("SELECT COUNT(*) FROM actions").fetchone()[0]
            for item in functions:
                item["average_duration_ms"] = round(item["duration_ms"] / item["measured_calls"], 3) if item["measured_calls"] else None
        finally:
            db.close()
    for event in actions:
        if event.get("error"):
            alerts.append({"cursor": event["cursor"], "tool": event["tool"], **event["error"]})
    reported = [run["usd"] for run in runs if run["usd"] is not None]
    totals = {"runs": len(runs), "observed_run_records": len(run_paths), "unreadable_or_unprocessed_runs": len(run_paths) - len(runs),
              "reported_cost_runs": len(reported), "unknown_cost_runs": len(run_paths) - len(reported),
              "reported_usd": round(sum(reported), 6) if reported else None,
              "cost_quality": "reported" if run_paths and len(reported) == len(run_paths) else "partial" if reported else "unknown",
              "recorded_actions": action_count if db is not None else 0}
    for bucket in models.values():
        bucket["reported_usd"] = round(bucket["reported_usd"], 6) if bucket["reported_cost_runs"] else None
        bucket["cost_quality"] = "reported" if not bucket["unknown_cost_runs"] else "partial" if bucket["reported_cost_runs"] else "unknown"
    return {"quality": "local-observation", "provenance": ["data/runs/*.json", "data/ai/observability.sqlite3"],
            "totals": totals, "runs": runs, "models": list(models.values()), "functions": functions,
            "alerts": alerts[:100], "actions": actions, "note": "reported_usd — сумма только подтверждённых чисел; неизвестные расходы не оценены. Локальная задержка включает серверную обработку, не доказывает задержку модели."}


class IdempotencyError(ValueError):
    pass


class IdempotencyStore:
    """Durable ключи без payload. Pending/unknown никогда не разрешают повтор.

    reconcile принимает только подтверждение штатного запроса из доверенного
    серверного кода; эта функция отсутствует в инструментах модели.
    """
    def __init__(self, root: Path):
        self.root = Path(root)

    def begin(self, key: str, payload: dict) -> dict:
        if not _identifier(key) or not isinstance(payload, dict):
            raise IdempotencyError("Неверный ключ или payload")
        try:
            digest = hashlib.sha256(json.dumps(payload, sort_keys=True, ensure_ascii=False, separators=(",", ":"), allow_nan=False).encode()).hexdigest()
        except (ValueError, TypeError):
            raise IdempotencyError("Payload должен быть конечным JSON") from None
        db = _database(self.root, create=True)
        assert db is not None
        try:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT * FROM idempotency WHERE key=?", (key,)).fetchone()
            if row and row["digest"] != digest:
                raise IdempotencyError("Ключ уже принадлежит другому запросу")
            if row and row["state"] != "not_executed":
                db.commit()
                return {"execute": False, "state": row["state"], "reconciliation_required": row["state"] in ("pending", "unknown"),
                        "result": json.loads(row["result"]) if row["result"] else None}
            reservation = secrets.token_hex(24)
            db.execute("INSERT INTO idempotency(key,digest,state,reservation,updated_at) VALUES(?,?,'pending',?,?) ON CONFLICT(key) DO UPDATE SET state='pending',reservation=excluded.reservation,result=NULL,updated_at=excluded.updated_at", (key, digest, reservation, _now()))
            db.commit()
            return {"execute": True, "state": "pending", "reservation": reservation, "reconciliation_required": False}
        except Exception:
            db.rollback()
            raise
        finally:
            db.close()

    def _transition(self, key: str, reservation: str, state: str, result: Any = None) -> dict:
        if not _identifier(key) or not isinstance(reservation, str) or len(reservation) != 48:
            raise IdempotencyError("Неверное подтверждение владельца")
        db = _database(self.root, create=True)
        assert db is not None
        try:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT state,reservation FROM idempotency WHERE key=?", (key,)).fetchone()
            if not row or row["state"] != "pending" or not secrets.compare_digest(row["reservation"], reservation):
                raise IdempotencyError("Нет действующей резервации; требуется сверка")
            clean = safe_projection(result)
            db.execute("UPDATE idempotency SET state=?,result=?,updated_at=? WHERE key=?", (state, json.dumps(clean, ensure_ascii=False), _now(), key))
            db.commit()
            return {"state": state, "result": clean, "execute": False}
        except Exception:
            db.rollback()
            raise
        finally:
            db.close()

    def complete(self, key: str, reservation: str, result: Any) -> dict:
        return self._transition(key, reservation, "completed", result)

    def unknown(self, key: str, reservation: str) -> dict:
        return self._transition(key, reservation, "unknown")

    def reconcile(self, key: str, outcome: str, evidence: dict, result: Any = None) -> dict:
        required = {"source", "reference", "observed_at", "confirmed"}
        if not _identifier(key) or outcome not in ("completed", "not_executed") or not isinstance(evidence, dict) or set(evidence) != required:
            raise IdempotencyError("Нужна подтверждённая сверка исхода")
        if evidence["confirmed"] is not True or not all(isinstance(evidence[k], str) and 1 <= len(evidence[k]) <= 200 for k in ("source", "reference", "observed_at")):
            raise IdempotencyError("Недостаточно доказательств сверки")
        try:
            observed = datetime.fromisoformat(evidence["observed_at"].replace("Z", "+00:00"))
            age = (datetime.now(timezone.utc) - observed).total_seconds()
            if observed.tzinfo is None or not 0 <= age <= 300:
                raise ValueError
        except (ValueError, TypeError):
            raise IdempotencyError("Сверка устарела или время некорректно") from None
        db = _database(self.root, create=True)
        assert db is not None
        try:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT state,updated_at FROM idempotency WHERE key=?", (key,)).fetchone()
            if not row or row["state"] not in ("pending", "unknown"):
                raise IdempotencyError("Состояние не требует сверки")
            if observed < datetime.fromisoformat(row["updated_at"]):
                raise IdempotencyError("Сверка выполнена до неизвестного исхода")
            db.execute("UPDATE idempotency SET state=?,result=?,evidence=?,updated_at=? WHERE key=?", (outcome, json.dumps(safe_projection(result), ensure_ascii=False), json.dumps(safe_projection(evidence), ensure_ascii=False), _now(), key))
            db.commit()
            return {"state": outcome, "execute": False, "retry_requires_begin": outcome == "not_executed"}
        except Exception:
            db.rollback()
            raise
        finally:
            db.close()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("usage", "actions", "validate"))
    parser.add_argument("--root", type=Path, default=ROOT)
    parser.add_argument("--tool")
    parser.add_argument("--arguments", default="{}")
    options = parser.parse_args(argv)
    try:
        if options.command == "usage":
            result = usage_summary(options.root)
        elif options.command == "actions":
            result = read_actions(options.root)
        else:
            result = {"valid": True, "arguments": safe_projection(validate_tool_call(options.tool, json.loads(options.arguments)))}
    except (ValueError, OSError, sqlite3.Error):
        print(json.dumps({"ok": False, **error_status("tool_validation" if options.command == "validate" else "unknown")}, ensure_ascii=False))
        return 2
    print(json.dumps(result, ensure_ascii=False, indent=2, allow_nan=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
