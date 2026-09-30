"""Локальная панель процессов и агентов: python -m src.control_panel.

Слушает только 127.0.0.1. Не запускает торговые CLI и не меняет guard,
autopilot, доску или решения человека. Запись — только разрешённые ops-действия.
"""
from __future__ import annotations

import argparse
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
import subprocess
import threading
from urllib.parse import parse_qs, urlsplit

from src.agent_context import build_context, dependencies, new_decisions, read_rows
from src.engine_watchdog import Paths, decide, gather
from src.netdata_monitor import check_netdata_health
from src.obsidian_status import build_snapshot, process_alive
from ops.hooks.autopilot import parse_board, ready_tasks


ROOT = Path(__file__).resolve().parents[1]
ASSETS = ROOT / "ops" / "control-panel"
MAX_BODY = 4096
MAX_JSON = 256 * 1024
MAX_LOG_BYTES = 16 * 1024
ALLOWED_ACTIONS = {"engine.start", "engine.stop", "delegation.pause", "delegation.stop"}
RUNTIMES_CAPABILITIES = {
    "codex": ["start", "status"],
    "claude": ["start", "status"],
    "gemini": ["start", "status"],
    "muse": ["start", "status", "events"],
}


def runtime_capabilities(root: Path = ROOT) -> dict[str, list[str]]:
    return {name: [cap for cap in caps if cap != "start" or _guard_launch_ready(root, name)]
            for name, caps in RUNTIMES_CAPABILITIES.items()}


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
    patterns = [r"(?i)(\b(?:api[_-]?key|token|secret|password)\b\s*[:=]\s*)[^\s|;,]+",
                r"(?i)(\bBearer\s+)[A-Za-z0-9._~+/-]+",
                r"(?i)\b(?:sk-[A-Za-z0-9_-]{16,}|ghp_[A-Za-z0-9]{20,})\b"]
    count = 0
    for pattern in patterns:
        text, found = re.subn(pattern, lambda match: (match.group(1) if match.lastindex else "") + "[СКРЫТО]", text)
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


def _redact_data(item):
    if isinstance(item, str):
        text, _ = _redact_handoff(item)
        return text
    if isinstance(item, dict):
        return {k: _redact_data(v) for k, v in item.items()}
    if isinstance(item, list):
        return [_redact_data(v) for v in item]
    return item


def _save_run(root: Path, run: dict) -> None:
    path = _runs_dir(root) / f"{run['id']}.json"
    clean = _redact_data(run)
    temporary = path.with_name(path.name + "." + secrets.token_hex(4) + ".tmp")
    try:
        temporary.write_text(json.dumps(clean, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def _get_run(root: Path, run_id: str, check_alive: bool = True) -> dict | None:
    if not run_id or not re.fullmatch(r"run_[A-Za-z0-9_-]{1,80}", run_id):
        return None
    path = _runs_dir(root) / f"{run_id}.json"
    if not path.is_file():
        return None
    run = _read_json(path)
    if not isinstance(run, dict) or run.get("id") != run_id:
        return None
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
            events = run.setdefault("events", [])
            events.append({
                "cursor": len(events) + 1,
                "ts": now,
                "type": "run.unconfirmed",
                "data": {"reason": "Процесс не найден; итог исполнения требуется сверить"},
            })
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


def _spawn_agent_process(root: Path, agent: str, role: str, task_id: str, model: str | None = None) -> int:
    script = root / "ops" / "agent-rotate.ps1"
    if not script.is_file():
        raise RuntimeError("Не найден agent-rotate.ps1")
    command = [_pwsh(), "-NoProfile", "-File", str(script), "-Agent", agent, "-Role", role, "-TaskId", task_id, "-Headless"]
    if model:
        command.extend(["-Model", model])
    flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    proc = subprocess.Popen(command, cwd=root, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, creationflags=flags)
    return proc.pid


def _start_run(root: Path, task_id: str, role: str, agent: str = "auto",
               idempotency_key: str | None = None, model: str | None = None) -> dict:
    if idempotency_key is not None and (not isinstance(idempotency_key, str) or
            not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", idempotency_key)):
        raise ValueError("Неверный idempotency_key")
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

    # 1. Проверка по idempotency_key
    all_runs = _list_runs(root, limit=None)
    if idempotency_key:
        for existing in all_runs:
            if existing.get("idempotency_key") == idempotency_key:
                if (existing.get("task_id"), existing.get("role"), existing.get("runtime")) != (task_id, role, agent):
                    raise ValueError("idempotency_key уже использован для другого запуска")
                return existing

    # 2. Проверка активного запуска по task_id
    for existing in all_runs:
        if existing.get("task_id") == task_id and existing.get("status") == "running":
            return existing
        if existing.get("task_id") == task_id and existing.get("status") == "unknown":
            raise ValueError("Итог прежнего запуска неизвестен; нужна сверка перед повтором")

    # 3. Запуск нового процесса
    plan = _agent_plan(root, task_id, role, agent)
    if not plan.get("launch_enabled") or plan.get("selected") != agent:
        raise ValueError("Запуск недоступен: " + str(plan.get("reason") or "guard/CLI не проверены"))
    pid = _spawn_agent_process(root, agent, role, task_id, model=model)
    now = datetime.now(timezone.utc).isoformat()
    rand_suffix = secrets.token_hex(4)
    run_id = f"run_{task_id}_{int(datetime.now(timezone.utc).timestamp())}_{rand_suffix}"
    run = {
        "id": run_id,
        "task_id": task_id,
        "role": role,
        "runtime": agent,
        "model": model,
        "status": "running",
        "pid": pid,
        "idempotency_key": idempotency_key,
        "started_at": now,
        "finished_at": None,
        "exit_code": None,
        "events": [
            {
                "cursor": 1,
                "ts": now,
                "type": "run.started",
                "data": {"task_id": task_id, "role": role, "runtime": agent, "pid": pid},
            }
        ],
        "cost": {"usd": None, "quality": "unknown"},
        "provenance": f"data/runs/{run_id}.json",
    }
    _save_run(root, run)
    return run


def _cancel_run(root: Path, run_id: str) -> dict:
    run = _get_run(root, run_id, check_alive=True)
    if not run:
        raise ValueError(f"Запуск {run_id} не найден")

    return {"ok": False, "error": "unsupported",
            "detail": "Отмена ждёт идентификатор сессии и подтверждение клиента; PID не завершался",
            "run": run}


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
            "runs": part("runs", lambda: _list_runs(root, 20)),
            "engine": part("engine", lambda: _engine(root)),
            "processes": part("processes", lambda: _processes(root)),
            "worktrees": part("worktrees", lambda: _worktrees(root)),
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
        self._send(code, json.dumps(payload, ensure_ascii=False).encode("utf-8"), "application/json; charset=utf-8")

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
        if parsed.path == "/api/state":
            return self._json(200, state(self.server.root))
        if parsed.path == "/api/task":
            try:
                task_id = parse_qs(parsed.query).get("id", [""])[0]
                return self._json(200, _task_detail(self.server.root, task_id))
            except (OSError, ValueError) as exc:
                return self._json(400, {"error": str(exc)})
        if parsed.path == "/api/capabilities":
            return self._json(200, {"capabilities": runtime_capabilities(self.server.root)})
        if parsed.path == "/api/runs":
            return self._json(200, {"runs": _list_runs(self.server.root)})
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
                task_id = parse_qs(parsed.query).get("id", [""])[0]
                return self._json(200, _handoff(self.server.root, task_id))
            except (OSError, ValueError) as exc:
                return self._json(400, {"error": str(exc)})
        return self._json(404, {"error": "Не найдено"})

    def do_POST(self):
        if not self._host_ok() or not self._origin_ok() or not self._authorized():
            return self._json(403, {"error": "Доступ запрещён"})
        parsed = urlsplit(self.path)
        if parsed.path not in ("/api/action", "/api/agent/plan", "/api/runs", "/api/run/cancel", "/api/runs/cancel"):
            return self._json(404, {"error": "Не найдено"})
        try:
            length = int(self.headers.get("Content-Length", "0"))
            if not 0 < length <= MAX_BODY:
                return self._json(413, {"error": "Неверный размер запроса"})
            payload = json.loads(self.rfile.read(length))
            if not isinstance(payload, dict):
                return self._json(400, {"error": "Неверный запрос"})
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
                    if not isinstance(task_id, str) or not isinstance(role, str) or not isinstance(agent, str):
                        return self._json(400, {"error": "Неверные параметры запуска"})
                    run = _start_run(self.server.root, task_id, role, agent,
                                     idempotency_key=idempotency_key, model=model)
                    return self._json(200, {"ok": True, "run": run})
                except ValueError as exc:
                    return self._json(400, {"error": str(exc)})
                except (OSError, RuntimeError, subprocess.TimeoutExpired) as exc:
                    return self._json(502, {"error": f"{type(exc).__name__}: {exc}"})
            if parsed.path in ("/api/run/cancel", "/api/runs/cancel"):
                try:
                    run_id = payload.get("run_id") or parse_qs(parsed.query).get("id", [""])[0]
                    if not isinstance(run_id, str) or not run_id:
                        return self._json(400, {"error": "Не указан run_id"})
                    res = _cancel_run(self.server.root, run_id)
                    code = 200 if res.get("ok") else 400
                    return self._json(code, res)
                except ValueError as exc:
                    return self._json(404 if "не найден" in str(exc) else 400, {"error": str(exc)})
                except (OSError, RuntimeError) as exc:
                    return self._json(502, {"error": f"{type(exc).__name__}: {exc}"})
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
