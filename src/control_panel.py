"""Локальная панель процессов и агентов: python -m src.control_panel.

Слушает только 127.0.0.1. Не запускает торговые CLI и не меняет guard,
autopilot, доску или решения человека. Запись — только разрешённые ops-действия.
"""
from __future__ import annotations

import argparse
from contextlib import contextmanager
from collections import Counter
from datetime import datetime, timezone
import hashlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import re
import secrets
import shutil
import sqlite3
import subprocess
import threading
import time
from urllib.parse import parse_qs, urlsplit

from src import native_sessions
from src.agent_context import build_context, dependencies, new_decisions, read_rows
from src.engine_watchdog import Paths, decide, gather
from src.netdata_monitor import check_netdata_health
from src.obsidian_status import build_snapshot, process_alive
from src.worktree_lease import (LeaseError, heartbeat, inspect_leases,
                                prepare_worktree, release_lease)
from ops.hooks.autopilot import parse_board, ready_tasks


ROOT = Path(__file__).resolve().parents[1]
ASSETS = ROOT / "ops" / "control-panel"
# Домашний каталог с состоянием CLI (патчится в тестах); ключи оттуда не читаются.
NATIVE_HOME = Path.home()
_START_LOCK = threading.RLock()
_RUN_LOCK = threading.RLock()
_INFLIGHT_STARTS: set[str] = set()
MAX_BODY = 24 * 1024
MAX_JSON = 256 * 1024
MAX_LOG_BYTES = 16 * 1024
ALLOWED_ACTIONS = {"engine.start", "engine.stop", "delegation.pause", "delegation.stop"}
RUNTIMES_CAPABILITIES = {
    "codex": ["start", "status", "events"],
    "claude": ["start", "status", "events"],
    "gemini": ["start", "status", "events"],
    "muse": ["start", "status", "events"],
}


def runtime_capabilities(root: Path = ROOT) -> dict[str, list[str]]:
    return {name: [cap for cap in caps if cap != "start" or _guard_launch_ready(root, name)]
            for name, caps in RUNTIMES_CAPABILITIES.items()}


def capabilities_provenance() -> dict:
    """Откуда взята матрица и чем подтверждён start; steer/cancel не заявлены."""
    return {
        "matrix": "статическая матрица адаптеров src/control_panel.py; "
                  "steer/cancel/resume не заявлены: протокол сессий клиентов не подтверждён",
        "start_gate": ["ops/agent-routing.json: guard_status == e2e-verified",
                       "ops/hooks/launch-e2e.json: clients.<agent>.verified == true"],
        "events_source": "data/runs/<id>.json; Claude/Codex — нативные журналы сессий "
                         "(~/.claude/projects, ~/.codex/sessions) при однозначной привязке, "
                         "иначе и для Gemini/Muse — журнал лаунчера logs/agent-rotate.log",
        "cost_source": "Claude — cost-state.modelUsage.costUSD из файла сессии (reported); "
                       "Codex — только токены, стоимости в журнале нет; "
                       "Gemini/Muse — источника нет (unknown)",
    }


def _guard_launch_ready(root: Path, agent: str) -> bool:
    """Требует подтверждение E2E из защищённого каталога, а не только реестр."""
    registry = _read_json(root / "ops" / "agent-routing.json", {})
    runtimes = registry.get("runtimes", {}) if isinstance(registry, dict) else {}
    runtime = runtimes.get(agent, {}) if isinstance(runtimes, dict) else {}
    if not isinstance(runtime, dict) or runtime.get("guard_status") != "e2e-verified":
        return False
    evidence = _read_json(root / "ops" / "hooks" / "launch-e2e.json", {})
    if not isinstance(evidence, dict) or evidence.get("schema_version") != 1:
        return False
    clients = evidence.get("clients", {})
    entry = clients.get(agent, {}) if isinstance(clients, dict) else {}
    return isinstance(entry, dict) and entry.get("verified") is True


def _guard_observation(root: Path) -> dict:
    """Только счётчики из хвоста журнала; сами команды и причины не выдаются."""
    clients = {name: {"launch_ready": _guard_launch_ready(root, name),
                      "denies_in_tail": 0, "last_deny_at": None}
               for name in RUNTIMES_CAPABILITIES}
    for line in _tail(root / "data" / "guard.log", limit=64 * 1024, lines=10000):
        try:
            event = json.loads(line)
        except (TypeError, ValueError):
            continue
        if not isinstance(event, dict) or event.get("decision") != "deny":
            continue
        client = event.get("client")
        if not isinstance(client, str) or client not in clients:
            continue
        clients[client]["denies_in_tail"] += 1
        ts = event.get("ts")
        if isinstance(ts, str) and re.fullmatch(r"[0-9TZ:+.\- ]{1,40}", ts):
            clients[client]["last_deny_at"] = ts
    return {"clients": clients, "scope": "last_65536_bytes", "provenance": "data/guard.log"}


def _read_json(path: Path, default=None):
    try:
        if path.stat().st_size > MAX_JSON:
            return default
        return json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError, UnicodeError):
        return default


def _tail(path: Path, limit: int = MAX_LOG_BYTES, lines: int = 16) -> list[str]:
    try:
        with path.open("rb") as stream:
            stream.seek(0, os.SEEK_END)
            stream.seek(max(0, stream.tell() - limit))
            data = stream.read(limit).decode("utf-8", errors="replace")
        return data.splitlines()[-lines:]
    except OSError:
        return []


def _queue(root: Path) -> dict:
    def label(value):
        return value if isinstance(value, str) and re.fullmatch(r"[a-z][a-z0-9_-]{0,39}", value) else None

    def timestamp(value):
        return value if isinstance(value, str) and re.fullmatch(r"[0-9TZ:+.\- ]{1,40}", value) else None

    base = root / "ops" / "delegations"
    folders = ("inbox", "processing", "outbox")
    counts = {name: len(list((base / name).glob("d*.json"))) for name in folders}
    results = []
    for path in sorted((base / "outbox").glob("d*.json"), reverse=True)[:12]:
        item = _read_json(path)
        if isinstance(item, dict):
            results.append({key: item.get(key) for key in ("id", "status", "exit_code", "finished", "elapsed_s")})
    jobs = {}
    # Поздняя стадия заменяет раннюю; done/ хранит исходную заявку, но не является
    # доказательством успеха. prompt и output_tail никогда не попадают в ответ.
    for folder in folders:
        for path in sorted((base / folder).glob("d*.json"), reverse=True)[:30]:
            item = _read_json(path)
            if not isinstance(item, dict) or item.get("id") != path.stem:
                continue
            request = item if folder != "outbox" else _read_json(base / "done" / path.name, {})
            if not isinstance(request, dict) or request.get("id") != path.stem:
                request = {}
            state = (item.get("status") if folder == "outbox" else
                     "processing-unverified" if folder == "processing" else "queued")
            if state not in ("done", "error", "timeout", "processing-unverified", "queued"):
                state = "unknown"
            jobs[path.stem] = {
                "id": path.stem, "runtime": "muse", "state": state,
                "role": label(request.get("role")), "from": label(request.get("from")),
                "created": timestamp(request.get("created")),
                "finished": timestamp(item.get("finished")) if folder == "outbox" else None,
                "exit_code": item.get("exit_code") if folder == "outbox" and isinstance(item.get("exit_code"), int) else None,
                "elapsed_s": item.get("elapsed_s") if folder == "outbox" and isinstance(item.get("elapsed_s"), (int, float)) else None,
                "provenance": f"ops/delegations/{folder}/{path.name}",
                "cost_usd": None, "cost_quality": "unknown",
            }
    return {"counts": counts, "paused": (base / "PAUSED").exists(),
            "runner_pid_recorded": (base / "runner.pid").exists(), "results": results,
            "jobs": sorted(jobs.values(), key=lambda job: job["id"], reverse=True)[:30]}


def _board(root: Path) -> dict:
    path = root / "ops" / "board.md"
    text = path.read_text(encoding="utf-8-sig")
    rows = read_rows(text)
    tasks = []
    for task_id, variants in rows.items():
        for row in variants:
            task = row["task"]
            tasks.append({"id": task_id, "title": task["title"], "agent": task["agent"],
                          "status": task["status"], "status_detail": task["arg"],
                          "deps": task["deps"], "line": row["line"],
                          "duplicate": len(variants) > 1})
    counts = Counter(item["status"] for item in tasks)
    eligible = _eligible_ids(text, rows)
    return {"counts": dict(counts), "tasks": tasks, "ready_ids": sorted(eligible),
            "duplicates": sorted(key for key, variants in rows.items() if len(variants) > 1)}


def _eligible_ids(text: str, rows: dict) -> set[str]:
    ambiguous = {key for key, variants in rows.items() if len(variants) > 1}
    missing = {dep for variants in rows.values() for row in variants
               for dep in row["task"]["deps"] if dep not in rows}
    return {task_id for task_id in ready_tasks(parse_board(text))
            if not ({task_id, *dependencies(task_id, rows)} & (ambiguous | missing))}


def _task_detail(root: Path, task_id: str, text: str | None = None) -> dict:
    if not task_id or len(task_id) > 80 or any(ch not in "ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-_" for ch in task_id):
        raise ValueError("Неверный ID задачи")
    if text is None:
        text = (root / "ops" / "board.md").read_text(encoding="utf-8-sig")
    rows = read_rows(text)
    if task_id not in rows:
        raise ValueError("Задача не найдена")
    if len(rows[task_id]) != 1:
        raise ValueError("Дублирующийся ID: требуется сверка доски")
    row = rows[task_id][0]
    headings = [cell.strip().lower() for cell in row["header"].strip().strip("|").split("|")]
    cells = [cell.strip() for cell in row["raw"].strip().strip("|").split("|")]
    detail = dict(zip(headings, cells))
    task = row["task"]
    eligible = task_id in _eligible_ids(text, rows)
    return {"id": task_id, "title": task["title"], "agent": task["agent"],
            "status": task["status"], "status_detail": task["arg"],
            "deps": task["deps"], "eligible": eligible,
            "criterion": detail.get("критерий готовности", ""),
            "notes": detail.get("заметки", ""), "line": row["line"]}


def _agent_plan(root: Path, task_id: str, role: str, agent: str) -> dict:
    task = _task_detail(root, task_id)
    registry = _read_json(root / "ops" / "agent-routing.json", {})
    if not isinstance(registry, dict) or role not in registry.get("roles", {}):
        raise ValueError("Неизвестная роль")
    if agent != "auto" and agent not in registry.get("runtimes", {}):
        raise ValueError("Неизвестный рантайм")
    script = root / "ops" / "agent-rotate.ps1"
    if not script.is_file():
        raise RuntimeError("Не найден agent-rotate.ps1")
    command = [_pwsh(), "-NoProfile", "-File", str(script), "-Plan",
               "-Role", role, "-TaskId", task_id, "-Agent", agent]
    flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    result = subprocess.run(command, cwd=root, capture_output=True, timeout=20,
                            creationflags=flags)
    if result.returncode:
        raise RuntimeError("Планировщик завершился с ошибкой: " +
                           result.stderr.decode("utf-8", errors="replace")[-500:])
    try:
        try:
            output = result.stdout.decode("utf-8-sig")
        except UnicodeDecodeError:
            # Windows PowerShell CLI may mix OEM bytes with UTF-8 in one JSON stream.
            output = result.stdout.decode("cp866")
        plan = json.loads(output)
    except (UnicodeError, ValueError) as exc:
        raise RuntimeError("Планировщик вернул неверный JSON") from exc
    candidates = []
    for item in plan.get("candidates", []):
        candidates.append({key: item.get(key) for key in
                           ("agent", "model", "available", "skipped", "guard_status", "wrapper_unverified")})
    selected = plan.get("selected")
    eligible = task["eligible"]
    selected_candidate = next((c for c in candidates if c.get("agent") == selected), None)
    guard_ok = bool(selected and selected_candidate and selected_candidate.get("available")
                    and not selected_candidate.get("skipped")
                    and not selected_candidate.get("wrapper_unverified")
                    and _guard_launch_ready(root, selected))
    launch_enabled = bool(selected and eligible and guard_ok)
    reason = (
        f"Готов к запуску через {selected}" if launch_enabled else
        "Задача не готова к запуску" if not eligible else
        "Доступный CLI не найден" if not selected else
        "Интеграция guard не проверена для рантайма"
    )
    return {"task": task_id, "role": role, "requested_agent": agent,
            "selected": selected, "eligible": eligible,
            "task_status": task["status"], "candidates": candidates,
            "launch_enabled": launch_enabled, "reason": reason}


def _git_snapshot(root: Path) -> dict:
    """Метаданные текущего checkout; Git не читает содержимое изменений."""
    def read(*args: str) -> str:
        result = subprocess.run(["git", *args], cwd=root, capture_output=True, timeout=5)
        if result.returncode:
            raise RuntimeError("Git недоступен для этой рабочей копии")
        return result.stdout.decode("utf-8", errors="replace").strip()

    head = read("rev-parse", "HEAD")
    try:
        branch = read("symbolic-ref", "--quiet", "--short", "HEAD")
    except RuntimeError:
        branch = "detached"
    lines = read("-c", "core.quotePath=false", "status", "--short", "--untracked-files=normal").splitlines()
    return {"head": head, "branch": branch, "changes": lines[:100],
            "changes_truncated": len(lines) > 100}


def _redact_handoff(text: str) -> tuple[str, int]:
    patterns = [r"(?i)(\b[A-Za-z0-9_.-]*(?:api[_-]?key|token|secret|password|passphrase|authorization|cookie)[A-Za-z0-9_.-]*\s*[:=]\s*)(?:\"[^\"\r\n]*\"|'[^'\r\n]*'|[^\s|;,]+)",
                r"(?i)(\bBearer\s+)[A-Za-z0-9._~+/-]+",
                r"(?i)\b(?:sk-[A-Za-z0-9_-]{16,}|ghp_[A-Za-z0-9]{20,})\b"]
    count = 0
    for pattern in patterns:
        text, found = re.subn(pattern, lambda match: (match.group(1) if match.lastindex else "") + "[СКРЫТО]", text)
        count += found
    text, found = re.subn(r"-----BEGIN (?:[A-Z ]*PRIVATE KEY|OPENSSH PRIVATE KEY)-----[\s\S]*?(?:-----END [A-Z ]*PRIVATE KEY-----|$)",
                         "[СКРЫТО]", text)
    count += found
    return text, count


def _handoff(root: Path, task_id: str) -> dict:
    board_text = (root / "ops" / "board.md").read_text(encoding="utf-8-sig")
    task = _task_detail(root, task_id, board_text)
    board_hash = hashlib.sha256(board_text.encode("utf-8")).hexdigest()
    context = build_context(board_text, task_id, decisions=new_decisions(root))
    try:
        git = _git_snapshot(root)
    except (OSError, RuntimeError, subprocess.TimeoutExpired) as exc:
        git = {"error": f"{type(exc).__name__}: {exc}"}
    rotation = [event for event in _rotation(root) if event.get("task") == task_id]
    generated = datetime.now(timezone.utc).isoformat()
    git_lines = [f"HEAD: {git.get('head', 'недоступен')}",
                 f"Ветка: {git.get('branch', 'недоступна')}"]
    if git.get("error"):
        git_lines.append("Ошибка: " + git["error"])
    else:
        git_lines += ["Изменения (только имена, максимум 100):", *(git["changes"] or ["нет"])]
        if git["changes_truncated"]:
            git_lines.append("Список обрезан; выполните git status локально.")
    event_lines = [f"- {e.get('ts') or '?'} · {e.get('agent') or '?'} · "
                   f"{e.get('event') or '?'} · exit={e.get('exit_code')}"
                   for e in rotation] or ["- Связанных записей ротации нет"]
    markdown = "\n".join([f"# Передача задачи {task_id}",
                          f"Снимок: {generated}", f"SHA256 доски: {board_hash}",
                          "Источник: ops/board.md, git status, logs/agent-rotate.log.",
                          "Пакет не создаёт claim и не разрешает торговые действия; новый агент выполняет SYNC.",
                          "", context.rstrip(), "", "## Git", *git_lines,
                          "", "## События ротации по ID задачи", *event_lines, ""])
    markdown, redactions = _redact_handoff(markdown)
    return {"task_id": task_id, "generated_at": generated,
            "board_sha256": board_hash, "task_status": task["status"],
            "eligible": task["eligible"], "git": git, "rotation": rotation,
            "redactions": redactions, "markdown": markdown}


def _rotation(root: Path) -> list[dict]:
    events = []
    for line in _tail(root / "logs" / "agent-rotate.log", 64 * 1024, 30):
        try:
            entry = json.loads(line)
        except ValueError:
            continue
        if isinstance(entry, dict):
            events.append({key: entry.get(key) for key in
                           ("ts", "agent", "model", "role", "task", "event", "exit_code")})
    return list(reversed(events))


def _engine(root: Path) -> dict:
    facts = gather(Paths.under(root))
    verdict = decide(facts)
    return {"state": verdict.state, "reason": verdict.reason, "pid": facts.pid,
            "owner": facts.owner, "log_age_s": round(facts.now - facts.log_mtime) if facts.log_mtime else None,
            "stop_flag": facts.stop_flag, "autostart_off": facts.off_flag, "kill_flag": facts.kill_flag}


def _processes(root: Path) -> list[dict]:
    """Инвентарь известных PID-файлов; PID сам по себе не доказывает владельца."""
    files = [root / "data" / "engine.pid", root / "data" / "live" / "runner.pid",
             root / "ops" / "delegations" / "runner.pid"]
    found = []
    for path in files:
        try:
            raw = path.read_text(encoding="utf-8-sig").strip()
            pid = int(raw.split("|", 1)[0])
        except (OSError, UnicodeError, ValueError):
            continue
        try:
            alive = process_alive(pid)
        except Exception:
            alive = None
        found.append({"name": path.relative_to(root).as_posix(), "pid": pid,
                      "alive": alive, "owner_verified": False})
    return found


def _worktrees(root: Path) -> list[dict]:
    try:
        result = subprocess.run(["git", "worktree", "list", "--porcelain"], cwd=root,
                                capture_output=True, timeout=5, check=False)
    except (OSError, subprocess.TimeoutExpired):
        return []
    if result.returncode:
        return []
    worktrees = []
    item = {}
    for line in result.stdout.decode("utf-8", errors="replace").splitlines() + [""]:
        if not line:
            if item:
                worktrees.append(item)
                item = {}
            continue
        key, _, value = line.partition(" ")
        if key in ("worktree", "HEAD", "branch", "detached"):
            item[key] = value or True
    return worktrees[:30]


def _runs_dir(root: Path) -> Path:
    base = root / "data" / "runs"
    base.mkdir(parents=True, exist_ok=True)
    return base


def _redact_data(item, _depth: int = 0):
    from src.ai_observability import redact
    if _depth >= 32:
        return "[DEPTH LIMIT]"
    if isinstance(item, str):
        return redact(item, max_text=1048576).replace("[REDACTED]", "[СКРЫТО]")
    if isinstance(item, dict):
        # Фильтр ключей общий, но длинные списки событий/задач не обрезаются
        # лимитом safe_projection: их пагинация и границы заданы самим API.
        result = {}
        for key, value in item.items():
            filtered = redact({key: None}, max_text=1048576)
            if not filtered:
                continue
            clean_key, marker = next(iter(filtered.items()))
            result[clean_key] = "[СКРЫТО]" if marker == "[REDACTED]" else _redact_data(value, _depth + 1)
        return result
    if isinstance(item, list):
        return [_redact_data(v, _depth + 1) for v in item]
    return item


def _save_run(root: Path, run: dict) -> None:
    path = _runs_dir(root) / f"{run['id']}.json"
    clean = _redact_data(run)
    temporary = path.with_name(path.name + "." + secrets.token_hex(4) + ".tmp")
    try:
        with temporary.open("w", encoding="utf-8") as stream:
            json.dump(clean, stream, ensure_ascii=False, indent=2)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def _append_event(run: dict, event_type: str, ts: str | None, data: dict) -> None:
    events = run.setdefault("events", [])
    events.append({"cursor": len(events) + 1,
                   "ts": ts or datetime.now(timezone.utc).isoformat(),
                   "type": event_type, "data": data})


def _release_run_lease(root: Path, run: dict, state: str = "released") -> None:
    """Освобождает lease завершённого run; worktree остаётся для сверки diff."""
    lease = run.get("lease")
    if not isinstance(lease, dict) or lease.get("state") not in ("active", "provisioning"):
        return
    try:
        released = release_lease(root, str(lease.get("task_id")), run["id"], state=state)
        run["lease"] = {key: released.get(key) for key in
                        ("task_id", "run_id", "worktree", "branch", "base_commit",
                         "state", "released_at")}
    except (LeaseError, ValueError, TypeError) as exc:
        lease["state"] = "release-failed"
        lease["release_error"] = f"{type(exc).__name__}: {exc}"


def _ingest_rotation_events(root: Path, run: dict) -> bool:
    """Переносит события лаунчера с run_id запуска в локальную запись.

    Источник — журнал ротации (для worktree-запусков — журнал внутри worktree).
    'finish' делает статус терминальным по коду выхода; это качество
    "exit-code-only": код выхода сам по себе не подтверждает критерий доски.
    """
    log_ref = run.get("rotation_log")
    log_path = Path(log_ref) if isinstance(log_ref, str) and log_ref else Path("logs/agent-rotate.log")
    if not log_path.is_absolute():
        log_path = root / log_path
    seen = {(event.get("data") or {}).get("source_ts")
            for event in run.get("events", []) if isinstance(event, dict)
            and isinstance(event.get("data"), dict)}
    changed = False
    for line in _tail(log_path, 256 * 1024, 4000):
        try:
            entry = json.loads(line)
        except (TypeError, ValueError):
            continue
        if not isinstance(entry, dict) or entry.get("run_id") != run.get("id"):
            continue
        ts = entry.get("ts") if isinstance(entry.get("ts"), str) else None
        if ts in seen:
            continue
        name = entry.get("event")
        code = entry.get("exit_code") if isinstance(entry.get("exit_code"), int) else None
        provenance = log_path.as_posix() if log_ref else "logs/agent-rotate.log"
        data = {"source_ts": ts, "launcher_event": name, "exit_code": code,
                "provenance": provenance}
        if name == "start":
            _append_event(run, "run.progress", ts, data)
            changed = True
        elif name == "finish":
            event_type = "run.completed" if code == 0 else "run.failed"
            _append_event(run, event_type, ts, data)
            run["status"] = "completed" if code == 0 else "failed"
            run["exit_code"] = code
            run["finished_at"] = ts or datetime.now(timezone.utc).isoformat()
            # Код выхода — не доказательство выполненного критерия доски.
            run["result_quality"] = "exit-code-only"
            _release_run_lease(root, run)
            changed = True
    return changed


def _ingest_native_events(root: Path, run: dict) -> bool:
    """События и стоимость из нативного журнала сессии клиента (Claude/Codex).

    Привязка только при единственном кандидате по cwd и окну времени; иначе
    запись остаётся на фолбэке журнала лаунчера. Текст сообщений не копируется.
    """
    runtime = run.get("runtime")
    if runtime not in ("claude", "codex"):
        return False
    native = run.get("native")
    if isinstance(native, dict) and native.get("finalized"):
        return False
    changed = False
    if not isinstance(native, dict) or not native.get("path"):
        lease = run.get("lease") if isinstance(run.get("lease"), dict) else {}
        workdir = lease.get("worktree") or str(root)
        try:
            link = native_sessions.find_session(NATIVE_HOME, runtime, workdir,
                                                run.get("started_at") or "",
                                                run.get("finished_at"))
        except Exception:
            link = None
        if not link:
            return False
        native = {"client": link["client"], "session_id": link["session_id"],
                  "path": link["path"], "cursor": 0, "finalized": False}
        run["native"] = native
        _append_event(run, "run.progress", None,
                      {"native_type": "session_linked", "quality": "native",
                       "session_id": link["session_id"], "provenance": link["path"]})
        changed = True
    try:
        result = native_sessions.ingest(native["client"], native["path"],
                                        native.get("cursor") or 0)
    except Exception:
        return changed
    if result["cursor"] != native.get("cursor"):
        native["cursor"] = result["cursor"]
        changed = True
    native_count = sum(1 for event in run.get("events", [])
                       if isinstance(event, dict) and isinstance(event.get("data"), dict)
                       and event["data"].get("quality") == "native")
    for event in result.get("events", []):
        if native_count >= native_sessions.MAX_EVENTS_PER_RUN:
            if not native.get("truncated"):
                native["truncated"] = True
                _append_event(run, "run.progress", None,
                              {"native_type": "native_truncated", "quality": "native",
                               "detail": "Достигнут предел нативных событий записи; "
                                         "полный журнал — в файле сессии",
                               "provenance": native["path"]})
                changed = True
            break
        _append_event(run, event["type"], event.get("ts"),
                      {**event["data"], "quality": "native", "provenance": native["path"]})
        native_count += 1
        changed = True
    cost = result.get("cost")
    if isinstance(cost, dict) and isinstance(cost.get("usd"), (int, float)):
        run["cost"] = {"usd": cost["usd"], "quality": "reported",
                       "models": cost.get("models"), "provenance": native["path"]}
        changed = True
    tokens = result.get("tokens")
    if isinstance(tokens, dict) and tokens:
        run["tokens"] = {**tokens, "quality": "reported", "provenance": native["path"]}
        changed = True
    if run.get("status") in ("completed", "failed", "cancelled"):
        native["finalized"] = True
        changed = True
    return changed


def _heartbeat_run_lease(root: Path, run: dict) -> bool:
    """Продлевает heartbeat активного lease, пока процесс запуска жив."""
    lease = run.get("lease")
    if not isinstance(lease, dict) or lease.get("state") != "active":
        return False
    try:
        record = heartbeat(root, str(lease.get("task_id")), run["id"], min_interval_s=60)
    except (LeaseError, ValueError):
        return False
    if record.get("heartbeat_at") != lease.get("heartbeat_at"):
        lease["heartbeat_at"] = record.get("heartbeat_at")
        return True
    return False


def _get_run(root: Path, run_id: str, check_alive: bool = True) -> dict | None:
    # Параллельный polling не должен перетирать курсор или дублировать события.
    with _RUN_LOCK:
        return _get_run_locked(root, run_id, check_alive)


def _ingest_run_report(root: Path, run: dict) -> bool:
    """Читает отчёт агента из подтверждённого checkout; reported не означает приёмку."""
    from src.agent_assistant import _review_checkout
    from src.agent_run_report import load_report
    try:
        checkout, _ = _review_checkout(root, run)
        envelope = load_report(checkout, run["id"], run["task_id"])
    except (OSError, ValueError, TypeError, KeyError, RuntimeError):
        problem = "Источник отчёта не подтверждён; требуется сверка привязки, схемы и worktree"
        if run.get("result_report_error") == problem:
            return False
        run["result_report_error"] = problem
        return True
    if envelope is None:
        return False
    digest = hashlib.sha256(json.dumps(envelope, sort_keys=True, ensure_ascii=False).encode("utf-8")).hexdigest()
    if run.get("result_report_sha256") == digest:
        return False
    run["result"] = envelope["result"]
    run["result_report_sha256"] = digest
    run["result_report_provenance"] = {
        "source": envelope["provenance"], "checkout": str(checkout),
        "reported_at": envelope["reported_at"], "quality": "reported", "acceptance_verified": False,
    }
    run.pop("result_report_error", None)
    _append_event(run, "run.report", envelope["reported_at"], {"source": envelope["provenance"], "quality": "reported"})
    return True


def _get_run_locked(root: Path, run_id: str, check_alive: bool = True) -> dict | None:
    if not run_id or not re.fullmatch(r"run_[A-Za-z0-9_-]{1,80}", run_id):
        return None
    path = _runs_dir(root) / f"{run_id}.json"
    if not path.is_file():
        return None
    run = _read_json(path)
    if not isinstance(run, dict) or run.get("id") != run_id:
        return None
    changed = False
    if check_alive and run.get("status") == "starting" and run_id not in _INFLIGHT_STARTS:
        run["status"] = "unknown"
        _append_event(run, "run.unconfirmed", None,
                      {"reason": "Найдена незавершённая запись запуска; перед повтором нужна сверка"})
        changed = True
    if check_alive and run.get("status") in ("running", "unknown"):
        # Recovery: журнал лаунчера может подтвердить итог и после рестарта панели.
        changed = _ingest_rotation_events(root, run) or changed
        # Нативный журнал сессии дополняет события и стоимость с provenance.
        changed = _ingest_native_events(root, run) or changed
    if check_alive and run.get("status") == "running" and run.get("pid"):
        pid = run["pid"]
        alive = False
        try:
            alive = process_alive(pid)
        except Exception:
            alive = False
        if not alive:
            now = datetime.now(timezone.utc).isoformat()
            run["status"] = "unknown"
            run["finished_at"] = now
            _append_event(run, "run.unconfirmed", now,
                          {"reason": "Процесс не найден; итог исполнения требуется сверить"})
            changed = True
        else:
            changed = _heartbeat_run_lease(root, run) or changed
    changed = _ingest_run_report(root, run) or changed
    if changed:
        _save_run(root, run)
    return run


def _list_runs(root: Path, limit: int | None = 50) -> list[dict]:
    runs_dir = _runs_dir(root)
    results = []
    for path in runs_dir.glob("run_*.json"):
        run = _get_run(root, path.stem, check_alive=True)
        if run:
            results.append(run)
    results.sort(key=lambda r: r.get("started_at") or "", reverse=True)
    return results[:limit] if limit is not None else results


def _observed_runs(root: Path, limit: int = 50) -> list[dict]:
    """Общий список без превращения файловой стадии Muse в подтверждённый run."""
    records = []
    for run in _list_runs(root, limit=None):
        # Список — краткая проекция. События и будущие поля локальной записи
        # остаются в /api/run/events, а не попадают в общий снимок панели.
        visible = {key: run.get(key) for key in (
            "id", "task_id", "role", "runtime", "model", "status", "pid",
            "started_at", "finished_at", "exit_code", "cost", "provenance",
            "workspace", "result_quality", "tokens")}
        records.append({**visible, "source_kind": "panel", "managed": True,
                        "state_quality": "pid-only" if run.get("status") == "running" else "local-record"})
    for job in _queue(root)["jobs"]:
        records.append({
            "id": job["id"], "runtime": "muse", "role": job.get("role"),
            "task_id": None, "status": job["state"], "pid": None,
            "started_at": job.get("created"), "finished_at": job.get("finished"),
            "elapsed_s": job.get("elapsed_s"), "exit_code": job.get("exit_code"),
            "cost": {"usd": job.get("cost_usd"), "quality": job.get("cost_quality", "unknown")},
            "provenance": job.get("provenance"), "source_kind": "muse-queue",
            "managed": False, "state_quality": "file-stage",
        })
    records.sort(key=lambda item: (item.get("started_at") or "", item["id"]), reverse=True)
    return records[:limit]


def _run_events(root: Path, run_id: str, after: int = 0, limit: int = 100) -> dict:
    if type(after) is not int or after < 0 or after > 1_000_000_000:
        raise ValueError("Неверный курсор события")
    if type(limit) is not int or not 1 <= limit <= 100:
        raise ValueError("Неверный предел событий")
    run = _get_run(root, run_id, check_alive=True)
    if not run:
        raise ValueError("Локальный запуск не найден")
    events = [event for event in run.get("events", [])
              if isinstance(event, dict) and isinstance(event.get("cursor"), int)
              and event["cursor"] > after]
    events.sort(key=lambda event: event["cursor"])
    page = events[:limit]
    return {"run_id": run_id, "events": page,
            "next_cursor": page[-1]["cursor"] if page else after,
            "has_more": len(events) > limit,
            "status": run.get("status"), "provenance": run.get("provenance")}


def _spawn_agent_process(root: Path, agent: str, role: str, task_id: str, model: str | None = None,
                         run_id: str | None = None, workdir: Path | None = None) -> int:
    base = Path(workdir) if workdir else root
    script = base / "ops" / "agent-rotate.ps1"
    if not script.is_file():
        raise RuntimeError("Не найден agent-rotate.ps1")
    command = [_pwsh(), "-NoProfile", "-File", str(script), "-Agent", agent, "-Role", role, "-TaskId", task_id, "-Headless"]
    if model:
        command.extend(["-Model", model])
    if run_id:
        command.extend(["-RunId", run_id])
        report_instruction = (
            f"Работай только над задачей {task_id}. Выполни SYNC, CLAIM, DO, VERIFY и RECORD по AGENTS.md. "
            "До завершения сохрани JSON-отчёт: summary, changed_files, checks "
            "(command, exit_code, status passed/failed/unknown/not-run, artifact_ref при наличии), "
            "external_actions (id, kind, status; незавершённые — unknown), next_step. "
            "Не включай секреты; не объявляй непроверенные действия завершёнными. "
            f"Передай JSON через stdin команде .venv/Scripts/python.exe -m src.agent_run_report "
            f"--run-id {run_id} --task-id {task_id} либо используй --input относительный-report.json. "
            "Отчёт не заменяет доску и проверку критерия."
        )
        command.extend(["-Prompt", report_instruction])
    flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    proc = subprocess.Popen(command, cwd=base, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, creationflags=flags)
    return proc.pid


@contextmanager
def _start_reservation(root: Path):
    """Межпоточный и межпроцессный запрет второго запуска до записи результата.

    Оставшийся после аварии файл не снимается по таймеру: его исход требуется
    сверить, поскольку процесс мог успеть стартовать до потери ответа.
    """
    with _START_LOCK:
        path = _runs_dir(root) / ".start.lock"
        try:
            descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        except FileExistsError as exc:
            raise ValueError("Запуск уже выполняется или его исход неизвестен; нужна сверка reservation") from exc
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
                json.dump({"pid": os.getpid(), "created_at": datetime.now(timezone.utc).isoformat()}, stream)
                stream.flush()
                os.fsync(stream.fileno())
            yield
        finally:
            path.unlink(missing_ok=True)


def _start_run(root: Path, task_id: str, role: str, agent: str = "auto",
               idempotency_key: str | None = None, model: str | None = None,
               workspace: str = "checkout") -> dict:
    with _start_reservation(root):
        return _start_run_reserved(root, task_id, role, agent, idempotency_key, model, workspace)


def _start_run_reserved(root: Path, task_id: str, role: str, agent: str = "auto",
               idempotency_key: str | None = None, model: str | None = None,
               workspace: str = "checkout") -> dict:
    if idempotency_key is not None and (not isinstance(idempotency_key, str) or
            not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", idempotency_key)):
        raise ValueError("Неверный idempotency_key")
    if workspace not in ("checkout", "worktree"):
        raise ValueError("Неверный workspace: допустимы checkout и worktree")
    if model is not None and (not isinstance(model, str) or
                              not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.:/-]{0,99}", model)):
        raise ValueError("Неверная модель")
    requested_runtime = agent
    # Повтор уже принятого intent не зависит от нового статуса карточки/CLI.
    all_runs = _list_runs(root, limit=None)
    if idempotency_key:
        for existing in all_runs:
            if existing.get("idempotency_key") == idempotency_key:
                if (existing.get("task_id"), existing.get("role"),
                    existing.get("requested_runtime", existing.get("runtime")),
                    existing.get("model"), existing.get("workspace", "checkout")) != (task_id, role, agent, model, workspace):
                    raise ValueError("idempotency_key уже использован для другого запуска")
                return existing
    task = _task_detail(root, task_id)
    if not task.get("eligible"):
        raise ValueError(f"Задача {task_id} не готова к запуску")
    registry = _read_json(root / "ops" / "agent-routing.json", {})
    if not isinstance(registry, dict) or role not in registry.get("roles", {}):
        raise ValueError("Неизвестная роль")

    if agent == "auto":
        plan = _agent_plan(root, task_id, role, "auto")
        agent = plan.get("selected")
        if not agent:
            raise ValueError("Не удалось автоматически выбрать доступный рантайм")
    elif agent not in registry.get("runtimes", {}):
        raise ValueError("Неизвестный рантайм")

    caps = RUNTIMES_CAPABILITIES.get(agent, [])
    if "start" not in caps:
        raise ValueError(f"Рантайм {agent} не поддерживает операцию start")

    # Проверка активного запуска по task_id, включая записи вне UI-лимита.
    for existing in all_runs:
        if existing.get("task_id") == task_id and existing.get("status") in ("running", "starting"):
            return existing
        if existing.get("task_id") == task_id and existing.get("status") == "unknown":
            raise ValueError("Итог прежнего запуска неизвестен; нужна сверка перед повтором")

    # 3. Запуск нового процесса
    plan = _agent_plan(root, task_id, role, agent)
    if not plan.get("launch_enabled") or plan.get("selected") != agent:
        raise ValueError("Запуск недоступен: " + str(plan.get("reason") or "guard/CLI не проверены"))
    now = datetime.now(timezone.utc).isoformat()
    rand_suffix = secrets.token_hex(4)
    run_id = f"run_{task_id[:48]}_{int(datetime.now(timezone.utc).timestamp())}_{rand_suffix}"
    lease = None
    workdir = None
    if workspace == "worktree":
        # Пишущий запуск получает изолированный checkout; занятая задача — отказ.
        try:
            lease = prepare_worktree(root, task_id, run_id)
        except LeaseError as exc:
            raise ValueError(f"Worktree lease недоступен: {exc}") from exc
        workdir = Path(lease["worktree"])
    rotation_log = ((workdir / "logs" / "agent-rotate.log").as_posix()
                    if workdir else "logs/agent-rotate.log")
    run = {
        "id": run_id,
        "task_id": task_id,
        "role": role,
        "runtime": agent,
        "requested_runtime": requested_runtime,
        "model": model,
        "status": "starting",
        "pid": None,
        "idempotency_key": idempotency_key,
        "started_at": now,
        "finished_at": None,
        "exit_code": None,
        "workspace": workspace,
        "lease": ({key: lease.get(key) for key in
                   ("task_id", "run_id", "worktree", "branch", "base_commit", "state")}
                  if lease else None),
        "rotation_log": rotation_log,
        "events": [
            {
                "cursor": 1,
                "ts": now,
                "type": "run.prepared",
                "data": {"task_id": task_id, "role": role, "runtime": agent,
                         "workspace": workspace,
                         "worktree": lease["worktree"] if lease else None},
            }
        ],
        # Стоимость — вычисляемая проекция; источника данных пока нет, и это фиксируется явно.
        "cost": {"usd": None, "quality": "unknown",
                 "provenance": "клиент не сообщает стоимость; журнал " + rotation_log +
                               " содержит только события start/finish"},
        "provenance": f"data/runs/{run_id}.json",
    }
    # Сначала durable intent: даже при потере ответа после Popen повтор закрыт.
    _INFLIGHT_STARTS.add(run_id)
    try:
        _save_run(root, run)
        pid = _spawn_agent_process(root, agent, role, task_id, model=model,
                                   run_id=run_id, workdir=workdir)
        run.update(status="running", pid=pid)
        _append_event(run, "run.started", None, {"pid": pid, "runtime": agent,
                                                  "workspace": workspace})
        _save_run(root, run)
    except Exception:
        run["status"] = "unknown"
        _append_event(run, "run.unconfirmed", None,
                      {"reason": "Ответ операции запуска не подтверждён; перед повтором нужна сверка"})
        try:
            _save_run(root, run)
        except OSError:
            # Ранее сохранённый intent остаётся starting и при чтении станет unknown.
            pass
        raise
    finally:
        _INFLIGHT_STARTS.discard(run_id)
    return run


def _cancel_run(root: Path, run_id: str) -> dict:
    run = _get_run(root, run_id, check_alive=True)
    if not run:
        raise ValueError(f"Запуск {run_id} не найден")
    caps = runtime_capabilities(root).get(run.get("runtime"), [])
    if "cancel" not in caps:
        return {"ok": False, "error": "unsupported",
                "detail": "Клиент не заявил поддержку cancel: отмена ждёт протокол сессии "
                          "и подтверждение конечного состояния; PID не завершался",
                "run": run}
    # Ни один адаптер пока не подтвердил cancel; ветка станет достижимой
    # только после включения capability в матрице с доказательством.
    raise RuntimeError("Подтверждённый протокол отмены не подключён")


def _steer_run(root: Path, run_id: str, text: str) -> dict:
    if not isinstance(text, str) or not text.strip() or len(text) > 2000:
        raise ValueError("Неверная инструкция steer")
    run = _get_run(root, run_id, check_alive=True)
    if not run:
        raise ValueError(f"Запуск {run_id} не найден")
    caps = runtime_capabilities(root).get(run.get("runtime"), [])
    if "steer" not in caps:
        return {"ok": False, "error": "unsupported",
                "detail": "Клиент не заявил поддержку steer: передача инструкции требует "
                          "подтверждённый протокол сессии; инструкция не сохранялась",
                "run": run}
    raise RuntimeError("Подтверждённый протокол steer не подключён")


def _run_context(root: Path, run_id: str) -> dict:
    """Восстановленный пакет для SYNC; чтение контекста не продолжает сессию."""
    from src.agent_assistant import build_handoff
    run = _get_run(root, run_id)
    if not run:
        raise ValueError("Локальный запуск не найден")
    handoff = build_handoff(root, run["task_id"], run=run, run_id=run_id)
    return _redact_data({"run_id": run_id, "status": run.get("status"),
                         "kind": "recovered-handoff", "session_resumed": False,
                         "native_session_id": run["native"].get("session_id") if isinstance(run.get("native"), dict) else None,
                         "handoff": handoff,
                         "markdown": handoff["markdown"],
                         "project_context_markdown": _handoff(root, run["task_id"])["markdown"],
                         "detail": "Пакет сохраняет цель и доказательства; новый исполнитель делает SYNC и CLAIM"})


def _run_result(root: Path, run_id: str) -> dict:
    from src.agent_assistant import build_handoff
    run = _get_run(root, run_id)
    if not run:
        raise ValueError("Локальный запуск не найден")
    handoff = build_handoff(root, run["task_id"], run=run, run_id=run_id)
    result = run.get("result") if isinstance(run.get("result"), dict) else {}
    summary = result.get("summary")
    summary_quality = "reported"
    if not isinstance(summary, str) or not summary.strip():
        summary_quality = "lifecycle-only"
        status = run.get("status")
        summary = (f"Процесс завершён: код {run.get('exit_code')}. Выполнение критерия задачи требует проверки."
                   if status in ("completed", "failed") else
                   "Исход запуска неизвестен; перед повтором требуется сверка."
                   if status == "unknown" else f"Состояние запуска: {status}. Итоговый отчёт не получен.")
    return _redact_data({"run_id": run_id, "task_id": run.get("task_id"),
                         "status": run.get("status"), "summary": summary[:2000],
                         "summary_quality": summary_quality,
                         "result_quality": run.get("result_quality", "unverified"),
                         "acceptance_verified": False,
                         "changed_files": handoff.get("changed_files", []),
                         "checks": handoff.get("checks", []),
                         "external_actions": handoff.get("external_actions", []),
                         "missing_evidence": handoff.get("missing_evidence", []),
                         "next_step": handoff.get("next_step"),
                         "provenance": handoff.get("provenance"),
                         "report_source": run.get("result_report_provenance"),
                         "report_warning": run.get("result_report_error"),
                         "continuation": {"resume_supported": "resume" in runtime_capabilities(root).get(run.get("runtime"), []),
                                          "context_url": "/api/run/context?id=" + run_id,
                                          "kind": "recovered-handoff"}})


def _resume_run(root: Path, run_id: str) -> dict:
    run = _get_run(root, run_id)
    if not run:
        raise ValueError("Локальный запуск не найден")
    if "resume" not in runtime_capabilities(root).get(run.get("runtime"), []):
        return {"ok": False, "error": "unsupported", "run_id": run_id,
                "detail": "Адаптер не подтвердил протокол resume; можно восстановить пакет передачи без запуска сессии",
                "context_url": "/api/run/context?id=" + run_id,
                "session_resumed": False}
    raise RuntimeError("Подтверждённый протокол resume не подключён")


def _assistant_tool(root: Path, name: str, arguments: dict) -> dict:
    """Серверная граница AI: схема проверяется до любого чтения/инструмента."""
    from src import agent_assistant, ai_evals, ai_observability
    started = time.monotonic()
    args = {}
    validated = False
    try:
        args = ai_observability.validate_tool_call(name, arguments)
        validated = True
        if name == "panel.state":
            result = state(root)
        elif name in ("board.task", "task.read"):
            result = _task_detail(root, args["task_id"])
        elif name == "runs.list":
            result = {"runs": _observed_runs(root, args.get("limit", 50))}
        elif name == "run.get":
            run = _get_run(root, args["run_id"])
            if not run:
                raise ValueError("Локальный запуск не найден")
            # Ассистент не получает произвольные внутренние поля записи.
            result = {key: run.get(key) for key in ("id", "task_id", "role", "runtime", "model",
                      "status", "started_at", "finished_at", "exit_code", "cost", "tokens", "provenance")}
        elif name == "run.events":
            result = _run_events(root, args["run_id"], args.get("after", 0), args.get("limit", 100))
        elif name == "run.result":
            result = _run_result(root, args["run_id"])
        elif name == "ai.usage":
            result = ai_observability.usage_summary(root)
        elif name == "ai.evals":
            result = ai_evals.run_suite()
        elif name == "assistant.draft":
            result = agent_assistant.task_draft(args["description"])
        elif name == "assistant.role":
            result = agent_assistant.recommend_role(args["description"])
        elif name in ("assistant.handoff", "task.handoff"):
            run = _get_run(root, args["run_id"]) if args.get("run_id") else None
            if args.get("run_id") and (not run or run.get("task_id") != args["task_id"]):
                raise ValueError("Запуск не найден или относится к другой задаче")
            result = agent_assistant.build_handoff(root, args["task_id"], run=run, run_id=args.get("run_id"))
        elif name in ("assistant.review", "task.review", "diff.review"):
            if args.get("run_id"):
                run = _get_run(root, args["run_id"])
                if not run or run.get("task_id") != args["task_id"]:
                    raise ValueError("Запуск не найден или относится к другой задаче")
            result = agent_assistant.review_diff(root, args["task_id"], run_id=args.get("run_id"))
        elif name == "assistant.specialist":
            result = agent_assistant.call_specialist(root, args["role"], args["tool"], args["arguments"])
        elif name == "sources.search":
            result = agent_assistant.call_specialist(root, "crypto-insight-hunter", name, args)
        else:
            raise ValueError("Инструмент не подключён")
        result = _redact_data(result)
    except Exception as exc:
        ai_observability.record_action(root, request="local-panel", tool=name if validated else "tool.rejected", arguments=args if validated else {},
                                      result=None, duration_ms=(time.monotonic() - started) * 1000,
                                      outcome="failed" if validated else "rejected", error=type(exc).__name__ if validated else "tool_validation")
        raise
    ai_observability.record_action(root, request="local-panel", tool=name, arguments=args,
                                  result=result, duration_ms=(time.monotonic() - started) * 1000,
                                  outcome="completed", run_id=args.get("run_id"))
    return result


def _strict_payload(payload: dict, required: set[str], optional: set[str] = frozenset()) -> None:
    if not required <= payload.keys() or payload.keys() - required - optional:
        raise ValueError("Неверные поля запроса")


def state(root: Path = ROOT) -> dict:
    """Только чтение. Ошибка одного источника не скрывает остальные разделы."""
    errors = []

    def part(name, fn):
        try:
            return fn()
        except Exception as exc:
            errors.append(f"{name}: {type(exc).__name__}: {exc}")
            return None

    registry = part("runtimes", lambda: _read_json(root / "ops" / "agent-routing.json", {}))
    snap = part("snapshot", lambda: build_snapshot(project_root=root, data_dir=root / "data",
                                                     pocket_path=root / "pump-pocket.json",
                                                     journal_path=root / "data" / "pump_journal.jsonl"))
    return {"generated_at": datetime.now(timezone.utc).isoformat(),
            "board": part("board", lambda: _board(root)),
            "runtimes": registry.get("runtimes", {}) if isinstance(registry, dict) else {},
            "roles": registry.get("roles", {}) if isinstance(registry, dict) else {},
            "capabilities": runtime_capabilities(root),
            "guard_observation": part("guard_observation", lambda: _guard_observation(root)),
            "runs": part("runs", lambda: _observed_runs(root, 30)),
            "engine": part("engine", lambda: _engine(root)),
            "processes": part("processes", lambda: _processes(root)),
            "worktrees": part("worktrees", lambda: _worktrees(root)),
            "worktree_leases": part("worktree_leases", lambda: inspect_leases(root)),
            "snapshot": snap, "delegation": part("delegation", lambda: _queue(root)),
            "netdata": part("netdata", lambda: check_netdata_health(timeout=1.0)),
            "rotation": part("rotation", lambda: _rotation(root)),
            "logs": {name: _tail(root / "logs" / f"{name}.log") for name in ("engine", "delegation")},
            "errors": errors}



def _pwsh() -> str:
    command = shutil.which("pwsh")
    if not command:
        raise RuntimeError("Нужен PowerShell 7 (pwsh)")
    return command


def _script(root: Path, name: str, action: str, timeout: int = 60) -> dict:
    script = root / "ops" / name
    if not script.is_file():
        raise RuntimeError(f"Не найден {name}")
    flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    completed = subprocess.run([_pwsh(), "-NoProfile", "-File", str(script), action],
                               cwd=root, capture_output=True, timeout=timeout,
                               creationflags=flags)
    output = (completed.stdout + completed.stderr).decode("utf-8", errors="replace")[-2000:]
    return {"ok": completed.returncode == 0, "exit_code": completed.returncode, "output": output}


def action(root: Path, name: str) -> dict:
    if name not in ALLOWED_ACTIONS:
        raise ValueError("Действие не поддерживается")
    if name == "delegation.pause":
        path = root / "ops" / "delegations" / "PAUSED"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.touch(exist_ok=True)
        return {"ok": True, "output": "Очередь Muse поставлена на паузу"}
    group, operation = name.split(".")
    if group == "engine":
        return _script(root, "engine.ps1", operation, 120)
    return _script(root, "delegate.ps1", operation, 60)


def _audit(root: Path, name: str, outcome: str) -> None:
    """Короткий локальный журнал действий без токена, prompt и вывода команд."""
    try:
        path = root / "logs" / "control-panel-actions.jsonl"
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as stream:
            stream.write(json.dumps({"ts": datetime.now(timezone.utc).isoformat(),
                                     "action": name, "outcome": outcome}, ensure_ascii=False) + "\n")
    except OSError:
        pass


class PanelServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, address, root: Path = ROOT, token: str | None = None):
        self.root = Path(root)
        self.token = token or secrets.token_urlsafe(32)
        self.action_lock = threading.Lock()
        super().__init__(address, PanelHandler)


class PanelHandler(BaseHTTPRequestHandler):
    server: PanelServer

    def log_message(self, format_string, *args):
        # URL с токеном не должен попадать в access log.
        pass

    def _host_ok(self) -> bool:
        host = self.headers.get("Host", "")
        expected = f"127.0.0.1:{self.server.server_port}"
        return host == expected

    def _origin_ok(self) -> bool:
        origin = self.headers.get("Origin")
        return not origin or origin == f"http://127.0.0.1:{self.server.server_port}"

    def _authorized(self) -> bool:
        given = self.headers.get("X-Control-Token", "")
        return secrets.compare_digest(given, self.server.token)

    def _send(self, code: int, body: bytes, content_type: str):
        self.send_response(code)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Content-Security-Policy", "default-src 'self'; script-src 'self'; style-src 'self'; connect-src 'self'; base-uri 'none'; frame-ancestors 'none'")
        self.end_headers()
        self.wfile.write(body)

    def _json(self, code: int, payload: dict):
        self._send(code, json.dumps(_redact_data(payload), ensure_ascii=False).encode("utf-8"), "application/json; charset=utf-8")

    def _ai_response(self, name: str, arguments: dict):
        try:
            return self._json(200, _assistant_tool(self.server.root, name, arguments))
        except ValueError as exc:
            return self._json(404 if "не найден" in str(exc) else 400, {"error": str(exc)})
        except (OSError, RuntimeError, sqlite3.Error, subprocess.TimeoutExpired):
            return self._json(503, {"error": "Локальный помощник временно недоступен; проверьте состояние и повторите чтение"})

    def do_GET(self):
        if not self._host_ok():
            return self._json(403, {"error": "Недопустимый Host"})
        parsed = urlsplit(self.path)
        if parsed.path == "/health":
            return self._json(200, {"ok": True})
        if parsed.path == "/":
            query_token = parse_qs(parsed.query).get("token", [""])[0]
            if not secrets.compare_digest(query_token, self.server.token):
                return self._json(403, {"error": "Неверный токен"})
            page = (ASSETS / "index.html").read_text(encoding="utf-8")
            page = page.replace("__CONTROL_TOKEN__", self.server.token)
            return self._send(200, page.encode("utf-8"), "text/html; charset=utf-8")
        if parsed.path in ("/app.css", "/app.js"):
            path = ASSETS / parsed.path.removeprefix("/")
            kind = "text/css" if path.suffix == ".css" else "text/javascript"
            return self._send(200, path.read_bytes(), kind + "; charset=utf-8")
        if not self._authorized():
            return self._json(403, {"error": "Неверный токен"})
        if not self._origin_ok():
            return self._json(403, {"error": "Недопустимый Origin"})
        if parsed.path == "/api/ai/observability":
            return self._ai_response("ai.usage", {})
        if parsed.path == "/api/ai/evals":
            return self._ai_response("ai.evals", {})
        if parsed.path == "/api/run/result":
            return self._ai_response("run.result", {"run_id": parse_qs(parsed.query).get("id", [""])[0]})
        if parsed.path == "/api/run/context":
            try:
                return self._json(200, _run_context(self.server.root, parse_qs(parsed.query).get("id", [""])[0]))
            except (OSError, ValueError) as exc:
                return self._json(404 if "не найден" in str(exc) else 400, {"error": str(exc)})
        if parsed.path == "/api/state":
            return self._json(200, state(self.server.root))
        if parsed.path == "/api/task":
            try:
                task_id = parse_qs(parsed.query).get("id", [""])[0]
                return self._json(200, _task_detail(self.server.root, task_id))
            except (OSError, ValueError) as exc:
                return self._json(400, {"error": str(exc)})
        if parsed.path == "/api/capabilities":
            return self._json(200, {"capabilities": runtime_capabilities(self.server.root),
                                    "provenance": capabilities_provenance()})
        if parsed.path == "/api/runs":
            return self._json(200, {"runs": _observed_runs(self.server.root)})
        if parsed.path == "/api/worktree-leases":
            return self._json(200, {"leases": inspect_leases(self.server.root)})
        if parsed.path == "/api/run/events":
            try:
                query = parse_qs(parsed.query)
                run_id = query.get("id", [""])[0]
                cursor = query.get("after", ["0"])[0]
                limit = query.get("limit", ["100"])[0]
                if not re.fullmatch(r"[0-9]{1,10}", cursor):
                    raise ValueError("Неверный курсор события")
                if not re.fullmatch(r"[0-9]{1,3}", limit):
                    raise ValueError("Неверный предел событий")
                return self._json(200, _run_events(self.server.root, run_id, int(cursor), int(limit)))
            except ValueError as exc:
                return self._json(404 if "не найден" in str(exc) else 400, {"error": str(exc)})
        if parsed.path == "/api/run":
            try:
                run_id = parse_qs(parsed.query).get("id", [""])[0]
                run = _get_run(self.server.root, run_id, check_alive=True)
                if not run:
                    return self._json(404, {"error": "Запуск не найден"})
                return self._json(200, run)
            except (OSError, ValueError) as exc:
                return self._json(400, {"error": str(exc)})
        if parsed.path == "/api/handoff":
            try:
                query = parse_qs(parsed.query)
                task_id = query.get("id", query.get("task_id", [""]))[0]
                result = _handoff(self.server.root, task_id)
                args = {"task_id": task_id}
                if query.get("run_id"):
                    args["run_id"] = query["run_id"][0]
                result["structured"] = _assistant_tool(self.server.root, "assistant.handoff", args)
                return self._json(200, result)
            except (OSError, ValueError) as exc:
                return self._json(400, {"error": str(exc)})
        return self._json(404, {"error": "Не найдено"})

    def do_POST(self):
        if not self._host_ok() or not self._origin_ok() or not self._authorized():
            return self._json(403, {"error": "Доступ запрещён"})
        parsed = urlsplit(self.path)
        if parsed.path not in ("/api/action", "/api/agent/plan", "/api/runs",
                               "/api/run/cancel", "/api/runs/cancel", "/api/run/steer",
                               "/api/run/resume", "/api/assistant/draft", "/api/assistant/review",
                               "/api/assistant/tool"):
            return self._json(404, {"error": "Не найдено"})
        try:
            length = int(self.headers.get("Content-Length", "0"))
            if not 0 < length <= MAX_BODY:
                return self._json(413, {"error": "Неверный размер запроса"})
            payload = json.loads(self.rfile.read(length))
            if not isinstance(payload, dict):
                return self._json(400, {"error": "Неверный запрос"})
            schemas = {
                "/api/action": ({"action"}, set()),
                "/api/agent/plan": ({"task_id", "role", "agent"}, set()),
                "/api/runs": ({"task_id", "role"}, {"agent", "idempotency_key", "model", "workspace"}),
                "/api/run/cancel": ({"run_id"}, set()),
                "/api/runs/cancel": ({"run_id"}, set()),
                "/api/run/steer": ({"run_id", "text"}, set()),
                "/api/run/resume": ({"run_id"}, set()),
                "/api/assistant/draft": ({"description"}, set()),
                "/api/assistant/review": ({"task_id"}, {"run_id"}),
                "/api/assistant/tool": ({"name", "arguments"}, set()),
            }
            _strict_payload(payload, *schemas[parsed.path])
            if parsed.path == "/api/assistant/draft":
                return self._ai_response("assistant.draft", payload)
            if parsed.path == "/api/assistant/review":
                return self._ai_response("assistant.review", payload)
            if parsed.path == "/api/assistant/tool":
                if not isinstance(payload["name"], str) or not isinstance(payload["arguments"], dict):
                    raise ValueError("Неверный вызов инструмента")
                return self._ai_response(payload["name"], payload["arguments"])
            if parsed.path == "/api/agent/plan":
                try:
                    if not all(isinstance(payload.get(key), str) for key in ("task_id", "role", "agent")):
                        return self._json(400, {"error": "Неверные параметры плана"})
                    return self._json(200, _agent_plan(self.server.root, payload["task_id"],
                                                       payload["role"], payload["agent"]))
                except ValueError as exc:
                    return self._json(400, {"error": str(exc)})
                except (OSError, RuntimeError, subprocess.TimeoutExpired) as exc:
                    return self._json(502, {"error": f"{type(exc).__name__}: {exc}"})
            if parsed.path == "/api/runs":
                try:
                    task_id = payload.get("task_id")
                    role = payload.get("role")
                    agent = payload.get("agent", "auto")
                    idempotency_key = payload.get("idempotency_key")
                    model = payload.get("model")
                    workspace = payload.get("workspace", "worktree" if role == "insight-executor" else "checkout")
                    if not isinstance(task_id, str) or not isinstance(role, str) or not isinstance(agent, str) \
                            or not isinstance(workspace, str):
                        return self._json(400, {"error": "Неверные параметры запуска"})
                    if idempotency_key is not None and (not isinstance(idempotency_key, str) or
                            not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", idempotency_key)):
                        return self._json(400, {"error": "Неверный idempotency_key"})
                    if model is not None and (not isinstance(model, str) or
                            not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.:/-]{0,99}", model)):
                        return self._json(400, {"error": "Неверная модель"})
                    if role == "insight-executor" and workspace != "worktree":
                        return self._json(400, {"error": "Кодовая задача требует отдельный worktree; запись агента в основной checkout не разрешена"})
                    run = _start_run(self.server.root, task_id, role, agent,
                                     idempotency_key=idempotency_key, model=model,
                                     workspace=workspace)
                    if run.get("status") == "unknown":
                        return self._json(409, {"ok": False, "error": "needs_reconciliation", "run": run})
                    return self._json(200, {"ok": True, "run": run})
                except ValueError as exc:
                    return self._json(400, {"error": str(exc)})
                except (OSError, RuntimeError, subprocess.TimeoutExpired) as exc:
                    return self._json(502, {"error": f"{type(exc).__name__}: {exc}"})
            if parsed.path in ("/api/run/cancel", "/api/runs/cancel"):
                try:
                    run_id = payload["run_id"]
                    if not isinstance(run_id, str) or not re.fullmatch(r"run_[A-Za-z0-9_-]{1,80}", run_id):
                        return self._json(400, {"error": "Неверный run_id"})
                    res = _cancel_run(self.server.root, run_id)
                    code = 200 if res.get("ok") else 400
                    return self._json(code, res)
                except ValueError as exc:
                    return self._json(404 if "не найден" in str(exc) else 400, {"error": str(exc)})
                except (OSError, RuntimeError) as exc:
                    return self._json(502, {"error": f"{type(exc).__name__}: {exc}"})
            if parsed.path == "/api/run/steer":
                try:
                    run_id = payload.get("run_id")
                    text = payload.get("text")
                    if not isinstance(run_id, str) or not re.fullmatch(r"run_[A-Za-z0-9_-]{1,80}", run_id):
                        return self._json(400, {"error": "Неверный run_id"})
                    res = _steer_run(self.server.root, run_id, text if isinstance(text, str) else "")
                    code = 200 if res.get("ok") else 400
                    return self._json(code, res)
                except ValueError as exc:
                    return self._json(404 if "не найден" in str(exc) else 400, {"error": str(exc)})
                except (OSError, RuntimeError) as exc:
                    return self._json(502, {"error": f"{type(exc).__name__}: {exc}"})
            if parsed.path == "/api/run/resume":
                try:
                    run_id = payload["run_id"]
                    if not isinstance(run_id, str) or not re.fullmatch(r"run_[A-Za-z0-9_-]{1,80}", run_id):
                        raise ValueError("Неверный run_id")
                    return self._json(400, _resume_run(self.server.root, run_id))
                except ValueError as exc:
                    return self._json(404 if "не найден" in str(exc) else 400, {"error": str(exc)})
                except (OSError, RuntimeError) as exc:
                    return self._json(502, {"error": type(exc).__name__ + ": протокол resume недоступен"})
            if not isinstance(payload.get("action"), str):
                return self._json(400, {"error": "Неверный запрос"})
            name = payload["action"]
            if name not in ALLOWED_ACTIONS:
                return self._json(400, {"error": "Действие не поддерживается"})
            if not self.server.action_lock.acquire(blocking=False):
                return self._json(409, {"error": "Другое действие ещё выполняется"})
            try:
                try:
                    result = action(self.server.root, name)
                except Exception:
                    _audit(self.server.root, name, "error")
                    raise
                _audit(self.server.root, name, "ok" if result.get("ok") else "failed")
            finally:
                self.server.action_lock.release()
            code = 200 if result.get("ok") else 502
            return self._json(code, result)
        except (ValueError, UnicodeError):
            return self._json(400, {"error": "Неверный JSON"})
        except (OSError, RuntimeError, subprocess.TimeoutExpired) as exc:
            return self._json(502, {"error": f"{type(exc).__name__}: {exc}"})


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--token", type=str, default=None, help="Фиксированный токен панели")
    args = parser.parse_args(argv)
    with PanelServer(("127.0.0.1", args.port), token=args.token) as server:
        print(f"Панель: http://127.0.0.1:{server.server_port}/?token={server.token}", flush=True)
        print("Только localhost. Ctrl+C — остановка панели.", flush=True)
        try:
            server.serve_forever(poll_interval=0.3)
        except KeyboardInterrupt:
            pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
