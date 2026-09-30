"""Изолированные рабочие копии для agent runs; lease не доказывает живую сессию."""

from __future__ import annotations

import json
import os
import re
import secrets
import subprocess
from datetime import datetime, timezone
from pathlib import Path


class LeaseError(RuntimeError):
    pass


def _git(root: Path, *args: str, timeout: int = 10) -> str:
    try:
        result = subprocess.run(["git", *args], cwd=root, capture_output=True,
                                timeout=timeout, check=False)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise LeaseError("Git недоступен или не ответил вовремя") from exc
    if result.returncode:
        raise LeaseError("Git отклонил операцию с рабочей копией")
    return result.stdout.decode("utf-8", errors="replace").strip()


def _lease_path(root: Path, task_id: str) -> Path:
    if not re.fullmatch(r"[A-Z0-9][A-Z0-9_-]{0,79}", task_id):
        raise ValueError("Неверный ID задачи")
    return root / "data" / "worktree-leases" / f"{task_id}.json"


def _write(path: Path, record: dict) -> None:
    temporary = path.with_name(path.name + "." + secrets.token_hex(4) + ".tmp")
    try:
        temporary.write_text(json.dumps(record, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def prepare_worktree(root: Path, task_id: str, run_id: str,
                     parent: Path | None = None) -> dict:
    """Атомарно резервирует задачу и создаёт worktree от чистого HEAD.

    При неопределённом результате git lease сохраняется для ручной сверки.
    Функция не запускает агента и не выдаёт торговых разрешений.
    """
    root = root.resolve()
    lease_path = _lease_path(root, task_id)
    if not re.fullmatch(r"run_[A-Za-z0-9_-]{1,80}", run_id):
        raise ValueError("Неверный ID запуска")
    if Path(_git(root, "rev-parse", "--show-toplevel")).resolve() != root:
        raise LeaseError("Ожидается корень Git-репозитория")
    if _git(root, "status", "--porcelain", "--untracked-files=normal"):
        raise LeaseError("Рабочая копия изменена; сначала зафиксируйте handoff")
    base_commit = _git(root, "rev-parse", "HEAD")
    parent = (parent or root.parent / f"{root.name}-agent-worktrees").resolve()
    if parent == root or root in parent.parents:
        raise LeaseError("Каталог worktree должен находиться вне основного checkout")
    destination = parent / run_id
    if destination.exists():
        raise LeaseError("Каталог запуска уже существует")
    branch = f"agent/{run_id}"
    now = datetime.now(timezone.utc).isoformat()
    record = {"schema_version": 1, "task_id": task_id, "run_id": run_id,
              "worktree": str(destination), "branch": branch,
              "base_commit": base_commit, "state": "provisioning",
              "created_at": now, "heartbeat_at": now}
    lease_path.parent.mkdir(parents=True, exist_ok=True)
    try:
        with lease_path.open("x", encoding="utf-8") as stream:
            json.dump(record, stream, ensure_ascii=False)
    except FileExistsError as exc:
        raise LeaseError("Задача уже имеет lease; сверьте прежний запуск") from exc
    try:
        parent.mkdir(parents=True, exist_ok=True)
        _git(root, "worktree", "add", "-b", branch, str(destination), base_commit,
             timeout=30)
    except (LeaseError, OSError):
        record["state"] = "unknown"
        _write(lease_path, record)
        raise
    record["state"] = "active"
    _write(lease_path, record)
    return record


def heartbeat(root: Path, task_id: str, run_id: str) -> dict:
    path = _lease_path(root.resolve(), task_id)
    try:
        record = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise LeaseError("Lease недоступен; требуется сверка") from exc
    if record.get("run_id") != run_id or record.get("state") != "active":
        raise LeaseError("Lease принадлежит другому запуску или не активен")
    record["heartbeat_at"] = datetime.now(timezone.utc).isoformat()
    _write(path, record)
    return record


def inspect_leases(root: Path, stale_after_s: int = 300) -> list[dict]:
    """Показывает запись и регистрацию worktree, не угадывая владельца процесса."""
    root = root.resolve()
    try:
        registered = {Path(line[9:]).resolve() for line in
                      _git(root, "worktree", "list", "--porcelain").splitlines()
                      if line.startswith("worktree ")}
    except LeaseError:
        registered = set()
    result = []
    for path in sorted((root / "data" / "worktree-leases").glob("*.json"))[:100]:
        try:
            if path.stat().st_size > 16 * 1024:
                continue
            record = json.loads(path.read_text(encoding="utf-8"))
            if not isinstance(record, dict) or record.get("schema_version") != 1:
                continue
            worktree = record.get("worktree")
            if not isinstance(worktree, str):
                continue
            item = {key: record.get(key) for key in
                    ("task_id", "run_id", "worktree", "branch", "base_commit",
                     "state", "created_at", "heartbeat_at")}
            item["registered"] = Path(worktree).resolve() in registered
            item["owner_verified"] = False
            try:
                beat = datetime.fromisoformat(record["heartbeat_at"])
                if beat.tzinfo is None:
                    raise ValueError("heartbeat without timezone")
                age = max(0, (datetime.now(timezone.utc) - beat).total_seconds())
                item["heartbeat_age_s"] = round(age)
                item["stale"] = record.get("state") == "active" and age > stale_after_s
            except (KeyError, TypeError, ValueError):
                item["heartbeat_age_s"] = None
                item["stale"] = record.get("state") == "active"
            result.append(item)
        except (OSError, ValueError):
            continue
    return result
