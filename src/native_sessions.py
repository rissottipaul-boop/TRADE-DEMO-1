"""Нативные журналы сессий CLI для событий и стоимости run с provenance.

Фактически подтверждённые источники на этой машине (разведка 03.10.2026):
- Claude Code: ~/.claude/projects/<slug-от-cwd>/<sessionId>.jsonl —
  записи с timestamp/type/sessionId, message.usage (токены) и
  cost-state.modelUsage.<model>.costUSD (числовая стоимость).
- Codex: ~/.codex/sessions/ГГГГ/ММ/ДД/rollout-*.jsonl — первая запись
  session_meta с payload.cwd/payload.id; события task_started/task_complete
  и token_count с total_token_usage (токены; стоимость в USD не пишется).
Gemini и Muse журналов сессий не ведут — для них остаётся фолбэк на журнал
лаунчера. Спекулятивные протоколы (muse serve и т.п.) здесь не кодируются.

Гигиена: из записей читаются только метаданные из белого списка (время, тип,
ID сессии, модель, числа usage/cost). Текст сообщений, инструментов и прочие
поля не копируются и не возвращаются. Файлы ключей (.credentials.json,
auth.json) не читаются.
"""
from __future__ import annotations

import json
import os
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path

# Привязка по окну времени: допуск на часы/буферизацию клиента.
MATCH_SLACK = timedelta(seconds=180)
MAX_SESSION_BYTES = 64 * 1024 * 1024
MAX_EVENTS_PER_RUN = 200


def _parse_ts(value) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return None
    return parsed.astimezone(timezone.utc)


def claude_project_slug(workdir: Path | str) -> str:
    """Каталог проекта Claude: все не-алфавитно-цифровые символы cwd → '-'.

    Подтверждено фактически: 'c:\\TG\\BOT\\TRADE DEMO 1' → 'c--TG-BOT-TRADE-DEMO-1'.
    """
    return re.sub(r"[^A-Za-z0-9]", "-", str(workdir))


def _first_record(path: Path) -> dict | None:
    try:
        if path.stat().st_size > MAX_SESSION_BYTES:
            return None
        with path.open(encoding="utf-8", errors="replace") as stream:
            line = stream.readline()
        record = json.loads(line)
    except (OSError, ValueError):
        return None
    return record if isinstance(record, dict) else None


def _in_window(candidate_ts: datetime | None, mtime: float,
               started: datetime, finished: datetime | None) -> bool:
    upper = (finished or datetime.now(timezone.utc)) + MATCH_SLACK
    lower = started - MATCH_SLACK
    anchor = candidate_ts or datetime.fromtimestamp(mtime, tz=timezone.utc)
    return lower <= anchor <= upper


def find_claude_session(home: Path, workdir: Path | str, started_at: str,
                        finished_at: str | None = None) -> dict | None:
    """Единственная сессия Claude для cwd в окне запуска; неоднозначность — None.

    Возвращает ссылку без чтения содержимого сообщений.
    """
    started = _parse_ts(started_at)
    if started is None:
        return None
    finished = _parse_ts(finished_at) if finished_at else None
    slug = claude_project_slug(workdir).lower()
    projects = home / ".claude" / "projects"
    candidates = []
    try:
        directories = [d for d in projects.iterdir() if d.is_dir() and d.name.lower() == slug]
    except OSError:
        return None
    for directory in directories:
        for path in directory.glob("*.jsonl"):
            try:
                mtime = path.stat().st_mtime
            except OSError:
                continue
            first = _first_record(path)
            first_ts = _parse_ts(first.get("timestamp")) if first else None
            if not _in_window(first_ts, mtime, started, finished):
                continue
            session_id = first.get("sessionId") if first else None
            candidates.append({"client": "claude", "path": str(path),
                               "session_id": session_id if isinstance(session_id, str) else path.stem})
    if len(candidates) != 1:
        # Ноль или несколько кандидатов: привязка не угадывается, честный фолбэк.
        return None
    return candidates[0]


def claude_ingest(path: Path | str, after_line: int = 0) -> dict:
    """Нормализованные события Claude после курсора-строки; только метаданные.

    Возвращает {"events": [...], "cursor": N, "cost": {...}|None}.
    Текст сообщений не читается в результат.
    """
    path = Path(path)
    events = []
    cost = None
    line_no = 0
    try:
        with path.open(encoding="utf-8", errors="replace") as stream:
            for line_no, line in enumerate(stream, start=1):
                if line_no <= after_line:
                    continue
                try:
                    record = json.loads(line)
                except ValueError:
                    continue
                if not isinstance(record, dict):
                    continue
                kind = record.get("type")
                ts = record.get("timestamp") if isinstance(record.get("timestamp"), str) else None
                if kind == "user":
                    events.append({"ts": ts, "type": "run.progress",
                                   "data": {"native_type": "user", "line": line_no}})
                elif kind == "assistant":
                    message = record.get("message") if isinstance(record.get("message"), dict) else {}
                    usage = message.get("usage") if isinstance(message.get("usage"), dict) else {}
                    data = {"native_type": "assistant", "line": line_no,
                            "model": message.get("model") if isinstance(message.get("model"), str) else None,
                            "input_tokens": usage.get("input_tokens") if isinstance(usage.get("input_tokens"), int) else None,
                            "output_tokens": usage.get("output_tokens") if isinstance(usage.get("output_tokens"), int) else None}
                    events.append({"ts": ts, "type": "run.progress", "data": data})
                elif kind == "cost-state":
                    usage_map = record.get("modelUsage")
                    total = 0.0
                    models = {}
                    valid = isinstance(usage_map, dict)
                    if valid:
                        for model, stats in usage_map.items():
                            value = stats.get("costUSD") if isinstance(stats, dict) else None
                            if isinstance(value, (int, float)):
                                models[str(model)] = round(float(value), 6)
                                total += float(value)
                    if valid and models:
                        cost = {"usd": round(total, 6), "models": models}
                        events.append({"ts": ts, "type": "run.cost",
                                       "data": {"native_type": "cost-state", "line": line_no,
                                                "usd": cost["usd"]}})
    except OSError:
        return {"events": [], "cursor": after_line, "cost": None}
    return {"events": events, "cursor": max(line_no, after_line), "cost": cost}


def _same_path(left, right) -> bool:
    try:
        return Path(str(left)).resolve() == Path(str(right)).resolve()
    except OSError:
        return False


def find_codex_session(home: Path, workdir: Path | str, started_at: str,
                       finished_at: str | None = None) -> dict | None:
    """Единственный rollout Codex с payload.cwd == workdir в окне запуска."""
    started = _parse_ts(started_at)
    if started is None:
        return None
    finished = _parse_ts(finished_at) if finished_at else None
    sessions = home / ".codex" / "sessions"
    candidates = []
    try:
        paths = list(sessions.rglob("rollout-*.jsonl"))
    except OSError:
        return None
    for path in paths:
        try:
            mtime = path.stat().st_mtime
        except OSError:
            continue
        # Грубый фильтр по mtime до чтения первой строки.
        if not _in_window(None, mtime, started - timedelta(hours=12), finished):
            continue
        first = _first_record(path)
        if not first or first.get("type") != "session_meta":
            continue
        payload = first.get("payload") if isinstance(first.get("payload"), dict) else {}
        if not _same_path(payload.get("cwd"), workdir):
            continue
        first_ts = _parse_ts(payload.get("timestamp")) or _parse_ts(first.get("timestamp"))
        if not _in_window(first_ts, mtime, started, finished):
            continue
        session_id = payload.get("id") or payload.get("session_id")
        candidates.append({"client": "codex", "path": str(path),
                           "session_id": session_id if isinstance(session_id, str) else path.stem})
    if len(candidates) != 1:
        return None
    return candidates[0]


def codex_ingest(path: Path | str, after_line: int = 0) -> dict:
    """События Codex rollout после курсора; usage токенов — без стоимости.

    Журнал Codex подтверждённо не содержит USD, поэтому cost остаётся None,
    а токены возвращаются отдельно с provenance reported.
    """
    path = Path(path)
    events = []
    tokens = None
    line_no = 0
    try:
        with path.open(encoding="utf-8", errors="replace") as stream:
            for line_no, line in enumerate(stream, start=1):
                if line_no <= after_line:
                    continue
                try:
                    record = json.loads(line)
                except ValueError:
                    continue
                if not isinstance(record, dict):
                    continue
                ts = record.get("timestamp") if isinstance(record.get("timestamp"), str) else None
                payload = record.get("payload") if isinstance(record.get("payload"), dict) else {}
                kind = payload.get("type")
                if record.get("type") == "session_meta":
                    events.append({"ts": ts, "type": "run.progress",
                                   "data": {"native_type": "session_meta", "line": line_no}})
                elif record.get("type") == "event_msg" and kind in ("task_started", "task_complete", "turn_aborted"):
                    events.append({"ts": ts, "type": "run.progress",
                                   "data": {"native_type": kind, "line": line_no}})
                elif record.get("type") == "event_msg" and kind == "token_count":
                    info = payload.get("info") if isinstance(payload.get("info"), dict) else {}
                    total = info.get("total_token_usage") if isinstance(info.get("total_token_usage"), dict) else {}
                    extracted = {key: total.get(key) for key in
                                 ("input_tokens", "cached_input_tokens", "output_tokens", "total_tokens")
                                 if isinstance(total.get(key), int)}
                    if extracted:
                        tokens = extracted
    except OSError:
        return {"events": [], "cursor": after_line, "tokens": None}
    return {"events": events, "cursor": max(line_no, after_line), "tokens": tokens}


def find_session(home: Path, runtime: str, workdir: Path | str, started_at: str,
                 finished_at: str | None = None) -> dict | None:
    """Диспетчер по рантайму; клиенты без нативного журнала получают None."""
    if runtime == "claude":
        return find_claude_session(home, workdir, started_at, finished_at)
    if runtime == "codex":
        return find_codex_session(home, workdir, started_at, finished_at)
    # Gemini и Muse на этой машине журналов сессий не ведут (разведка 03.10.2026).
    return None


def ingest(client: str, path: Path | str, after_line: int = 0) -> dict:
    if client == "claude":
        result = claude_ingest(path, after_line)
        result.setdefault("tokens", None)
        return result
    if client == "codex":
        result = codex_ingest(path, after_line)
        result.setdefault("cost", None)
        return result
    return {"events": [], "cursor": after_line, "cost": None, "tokens": None}


if __name__ == "__main__":
    # Самотест-обзор: показывает только количество кандидатов для текущего каталога.
    root = Path(os.getcwd())
    now = datetime.now(timezone.utc).isoformat()
    for client in ("claude", "codex"):
        link = find_session(Path.home(), client, root, now)
        print(f"{client}: {'найдена сессия' if link else 'однозначной сессии нет'}")
