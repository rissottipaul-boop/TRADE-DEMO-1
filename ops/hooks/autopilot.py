"""Stop-хук автопилота оркестратора: не даёт остановиться, пока есть работа.

Когда агент собирается завершить ход, хук читает доску ops/board.md. Если есть
готовые задачи (status=ready с выполненными зависимостями, или scheduled, чьё
время наступило) — возвращает decision=block с перечнем задач, и агент
продолжает работу. Лимит продолжений на сессию и выключатель — в
ops/autopilot.json (его меняет только человек: guard защищает файл).

Агент останавливается сам, когда готовых задач нет или всё оставшееся ждёт
человека (needs-user) — это штатный выход, а не сбой.
"""
import json
import re
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

ROOT = Path(__file__).resolve().parents[2]
BOARD = ROOT / "ops" / "board.md"
CONFIG = ROOT / "ops" / "autopilot.json"
STATE = ROOT / "data" / "autopilot_state.json"
SESSION_TTL_S = 2 * 24 * 3600


def parse_board(text: str) -> dict[str, dict]:
    """Таблицы доски → {ID: {"status", "arg", "deps", "agent", "title"}}."""
    tasks: dict[str, dict] = {}
    columns: Optional[dict[str, int]] = None
    for line in text.splitlines():
        if not line.strip().startswith("|"):
            columns = None if not line.strip() else columns
            continue
        cells = [c.strip() for c in line.strip().strip("|").split("|")]
        lowered = [c.lower() for c in cells]
        if "id" in lowered and "статус" in lowered:
            columns = {name: lowered.index(name) for name in lowered}
            continue
        if columns is None or set(line.replace("|", "").strip()) <= set("-: "):
            continue
        task_id = cells[columns["id"]].strip("`* ")
        if not task_id:
            continue
        status_cell = cells[columns["статус"]].strip("` ") if columns["статус"] < len(cells) else ""
        parts = status_cell.split(maxsplit=1)
        deps_cell = cells[columns["зависит от"]] if "зависит от" in columns and columns["зависит от"] < len(cells) else ""
        tasks[task_id] = {
            "status": parts[0].lower() if parts else "",
            "arg": parts[1].strip("` ") if len(parts) > 1 else "",
            "deps": [d.strip("` ") for d in re.split(r"[,;\s]+", deps_cell) if re.match(r"[`]?[A-Z0-9]", d)],
            "agent": cells[columns["агент"]] if "агент" in columns and columns["агент"] < len(cells) else "",
            "title": cells[columns["задача"]] if "задача" in columns and columns["задача"] < len(cells) else "",
        }
    return tasks


def _due(arg: str, now: datetime) -> bool:
    try:
        when = datetime.fromisoformat(arg.split()[0])
    except (ValueError, IndexError):
        return False
    if when.tzinfo is None:
        when = when.replace(tzinfo=timezone.utc)
    return now >= when


def ready_tasks(tasks: dict[str, dict], now: Optional[datetime] = None) -> list[str]:
    now = now or datetime.now(timezone.utc)
    result = []
    for task_id, task in tasks.items():
        actionable = task["status"] == "ready" or (task["status"] == "scheduled" and _due(task["arg"], now))
        deps_done = all(tasks.get(dep, {}).get("status") == "done" for dep in task["deps"])
        if actionable and deps_done:
            result.append(task_id)
    return result


def decide(payload: dict, board_text: str, config: dict, state: dict,
           now: Optional[datetime] = None) -> tuple[Optional[str], dict]:
    """(reason для продолжения или None, новое состояние)."""
    if config.get("enabled") is not True:
        return None, state
    ready = ready_tasks(parse_board(board_text), now)
    if not ready:
        return None, state

    sessions = {sid: s for sid, s in state.get("sessions", {}).items()
                if time.time() - s.get("updated", 0) < SESSION_TTL_S}
    session_id = str(payload.get("session_id") or "unknown")
    count = sessions.get(session_id, {}).get("count", 0)
    limit = int(config.get("max_continuations", 10))
    if count >= limit:
        return None, {"sessions": sessions}
    sessions[session_id] = {"count": count + 1, "updated": time.time()}

    shown = ", ".join(ready[:6]) + (" …" if len(ready) > 6 else "")
    reason = (
        f"[автопилот {count + 1}/{limit}] На доске ops/board.md готово задач: {len(ready)} ({shown}). "
        "Не завершай работу: возьми следующую по приоритету, отметь claim, делегируй профильному "
        "агенту, проверь артефакт по критерию готовности и обнови доску (AGENTS.md, «Цикл работы»). "
        "Завершить можно, только если готовых задач не осталось или все оставшиеся ждут человека "
        "(needs-user) — тогда переведи их в needs-user с одним конкретным вопросом."
    )
    return reason, {"sessions": sessions}


def _read_json(path: Path, default: dict) -> dict:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return default


def main() -> int:
    try:
        payload = json.loads(sys.stdin.buffer.read().decode("utf-8") or "{}")
        board = BOARD.read_text(encoding="utf-8") if BOARD.exists() else ""
        reason, state = decide(payload, board, _read_json(CONFIG, {}), _read_json(STATE, {}))
        if reason:
            STATE.parent.mkdir(parents=True, exist_ok=True)
            STATE.write_text(json.dumps(state, ensure_ascii=False), encoding="utf-8")
            out = {"decision": "block", "reason": reason,
                   "hookSpecificOutput": {"hookEventName": "Stop", "decision": "block", "reason": reason}}
            sys.stdout.write(json.dumps(out))  # ASCII-JSON: не зависит от кодировки консоли
    except Exception:  # сбой автопилота не должен ломать сессию — агент просто остановится
        return 0
    return 0


if __name__ == "__main__":
    sys.exit(main())
