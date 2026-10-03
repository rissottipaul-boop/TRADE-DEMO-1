"""Проекция данных проекта для Morphy. Только чтение, без торговых запросов."""
from __future__ import annotations

import argparse
from contextlib import closing
from datetime import datetime, timezone
import json
from pathlib import Path
import re
import shutil
import subprocess
import sys
import urllib.request

from src.agent_context import new_decisions
from src.control_panel import _board, _queue, _redact_data, _task_detail
from src.netdata_monitor import check_netdata_health
from src.obsidian_status import _connect_ro, _f, build_snapshot

ROOT = Path(__file__).resolve().parents[1]

# Скачок капитала между соседними точками больше этой доли — внешний поток
# (пополнение или вывод demo-счёта), а не результат торговли. Тот же порог
# применяет график капитала в notes/views/live.js.
EXTERNAL_FLOW_RATIO = 0.2


def pick(value, keys):
    if not isinstance(value, dict):
        return None
    return {key: value.get(key) for key in keys}


def read_json(path: Path, fallback):
    try:
        return json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError):
        return fallback


def recent_documents(root: Path, folder: str, limit: int = 12) -> list[dict]:
    records = []
    for path in sorted((root / folder).glob("*.md"), key=lambda p: p.stat().st_mtime, reverse=True)[:limit]:
        text = path.read_text(encoding="utf-8-sig")
        heading = re.search(r"^#\s+(.+)$", text, re.M)
        status = re.search(r"(?:^status:\s*|\*\*Статус:\*\*\s*)([^\n]+)", text, re.M)
        records.append({"path": path.relative_to(root).as_posix(),
                        "title": heading.group(1) if heading else path.stem,
                        "status": status.group(1)[:160] if status else None,
                        "updated_at": datetime.fromtimestamp(path.stat().st_mtime, timezone.utc).isoformat()})
    return _redact_data(records)


def incidents(root: Path) -> list[dict]:
    """Только заголовки инцидентов; сырые журналы в веб-ответ не входят."""
    try:
        text = (root / "ops" / "incidents.md").read_text(encoding="utf-8-sig")
    except OSError:
        return []
    headings = re.findall(r"^##\s+(.+)$", text, re.M)
    return _redact_data([{"title": title, "source": "ops/incidents.md"} for title in headings[:12]])


def git_summary(root: Path) -> dict:
    def git(*args):
        result = subprocess.run(["git", "-C", str(root), *args], capture_output=True,
                                text=True, encoding="utf-8", errors="replace", timeout=6,
                                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        if result.returncode:
            return None
        return result.stdout.strip()
    if not shutil.which("git"):
        return {"available": False}
    try:
        branch = git("branch", "--show-current")
        commit = git("rev-parse", "--short", "HEAD")
        status = git("status", "--porcelain", "--untracked-files=normal")
        # URL remote не читается: он может содержать учётные данные.
        return {"available": commit is not None, "branch": branch, "commit": commit,
                "changed_files": len(status.splitlines()) if status else 0,
                "remote_count": len((git("remote") or "").splitlines()),
                "github_cli_available": bool(shutil.which("gh"))}
    except (OSError, subprocess.TimeoutExpired):
        return {"available": False}


def project_modules(root: Path) -> list[dict]:
    inventory = (
        ("market", "Рыночные данные", "Свечи, скан рынка и архив funding", ("src/pump_scanner.py", "src/funding_archive.py")),
        ("strategies", "Стратегии", "Grid, Mean Reversion и Funding Carry", ("src/grid_engine.py", "src/mean_reversion.py", "src/funding_carry.py")),
        ("native-bots", "Боты OKX", "Фьючерсные боты и Copy Trading", ("src/futures_bot.py", "src/copy_trader.py")),
        ("treasury", "Казначейство", "Simple Earn и управление ликвидностью", ("src/treasury.py", "src/treasury_exec.py")),
        ("pnl", "Учёт прибыли", "PnL, сделки и атрибуция", ("src/pnl_ledger.py", "src/order_audit.py")),
        ("backtest", "Бэктесты", "Исторические прогоны стратегий", ("src/backtest/__main__.py",)),
        ("vps", "VPS", "Скрипты развёртывания и резервирования", ("ops/deploy-vps.ps1", "ops/deploy-vps.sh")),
    )
    return [{"id": key, "name": name, "description": desc, "files": list(files),
             "present": all((root / file).is_file() for file in files),
             "status": "code-present" if all((root / file).is_file() for file in files) else "incomplete"}
            for key, name, desc, files in inventory]


def lifetime_pnl(bot_db: Path, ratio: float = EXTERNAL_FLOW_RATIO) -> dict:
    """Прибыль за всё время проекта по таблице equity_curve, без внешних потоков.

    `последняя − первая` на demo-счёте бессмысленна: пополнение 24.09 подняло
    капитал с 5 000 до ~104 000. Поэтому суммируются приращения между соседними
    точками, а скачки больше `ratio` относятся к внешним потокам и в прибыль не
    идут. База процента = капитал сейчас − прибыль, то есть внесённые деньги.
    """
    bot_db = Path(bot_db)
    if not bot_db.is_file():                 # базы нет — не ошибка, а отсутствие истории
        return {"available": False, "points": 0}
    with closing(_connect_ro(bot_db)) as conn:
        rows = conn.execute("SELECT ts, total_eq FROM equity_curve ORDER BY ts").fetchall()
    points = [(ts, eq) for ts, eq in ((_f(ts, 0), _f(eq, 2)) for ts, eq in rows)
              if ts is not None and eq is not None]
    if len(points) < 2:
        return {"available": False, "points": len(points)}
    net = external = 0.0
    flows = 0
    for (_, before), (_, after) in zip(points, points[1:]):
        delta = after - before
        if before <= 0 or abs(delta) > before * ratio:
            external += delta
            flows += 1
            continue
        net += delta
    base = points[-1][1] - net
    return {"available": True, "net_usdt": round(net, 2),
            "net_pct": round(net / base * 100, 2) if base > 0 else None,
            "base_usdt": round(base, 2), "external_usdt": round(external, 2),
            "external_flows": flows, "points": len(points),
            "since": datetime.fromtimestamp(points[0][0], timezone.utc).isoformat(),
            "equity_usdt": points[-1][1]}


def morphy_status() -> dict:
    """Публичные флаги настройки; профили и credentials не читаются."""
    try:
        with urllib.request.urlopen("http://127.0.0.1:7480/api/onboard/status", timeout=1) as response:
            return pick(json.load(response), ("portalConfigured", "provider", "model", "tunnelMode")) or {}
    except (OSError, ValueError):
        return {}


def collect(root: Path = ROOT) -> dict:
    root = Path(root)
    errors: list[str] = []

    def part(name, fn, fallback):
        try:
            return fn()
        except Exception as exc:
            # Содержимое исключения может включать путь/данные источника.
            errors.append(f"{name}: {type(exc).__name__}")
            return fallback

    board = part("board", lambda: _board(root), {"tasks": [], "counts": {}, "ready_ids": [], "duplicates": []})
    snapshot = part("snapshot", lambda: build_snapshot(project_root=root, data_dir=root / "data",
                         pocket_path=root / "pump-pocket.json", journal_path=root / "data/pump_journal.jsonl"), {})
    registry = read_json(root / "ops" / "agent-routing.json", {})
    queue = part("queue", lambda: _queue(root), {"counts": {}, "jobs": [], "paused": None})
    netdata = part("netdata", lambda: check_netdata_health(timeout=1), {})
    morphy = part("morphy", morphy_status, {})
    claims = [task for task in board["tasks"] if task["status"] == "in-progress"]
    runtimes = []
    for name in ("claude", "codex", "gemini", "muse"):
        runtime = registry.get("runtimes", {}).get(name, {}) if isinstance(registry, dict) else {}
        command = runtime.get("command", name)
        runtimes.append({"id": name, "model": runtime.get("default_model"),
                         "guard_status": runtime.get("guard_status", "unknown"),
                         "cli_available": bool(shutil.which(command)) if isinstance(command, str) else False})
    ops = snapshot.get("ops") or {}
    risk = pick(snapshot.get("risk"), ("equity", "hwm", "drawdown_pct", "day_pnl", "day_pnl_pct",
                "daily_limit_pct", "global_dd_limit_pct", "daily_breaker", "global_breaker", "kill_active",
                "equity_age_s", "entries_today", "max_entries_per_day", "portfolio_heat_pct", "max_heat_pct"))
    engine = pick(snapshot.get("engine"), ("running", "uptime_h", "reconciles", "divergences",
                "errors_exchange", "errors_internal", "stats_age_s", "ws_public_reconnects", "ws_private_reconnects"))
    telegram_tasks = [t for t in board["tasks"] if t["id"] in ("ALERTS-TG", "ALERTS-IMPL")]
    telegram_ready = len(telegram_tasks) == 2 and all(t["status"] == "done" for t in telegram_tasks)
    integrations = [
        {"id": "morphy", "name": "Morphy", "group": "Агенты", "status": "online" if morphy.get("portalConfigured") else "needs-setup",
         "detail": f"AI: {morphy.get('provider') or 'не настроен'}; модель: {morphy.get('model') or 'не указана'}", "source": "Публичные флаги настройки Morphy"},
        {"id": "okx", "name": "OKX", "group": "Торговля", "status": "observed" if risk else "unavailable",
         "detail": "Состояние локального движка и риск-ядра; не запрос к бирже", "source": "data/risk_state.db"},
        {"id": "muse-queue", "name": "Очередь Muse", "group": "Агенты", "status": "paused" if queue.get("paused") else "observed",
         "detail": "Файловая очередь и результаты; processing не подтверждает живой процесс", "source": "ops/delegations/"},
        {"id": "netdata", "name": "Netdata", "group": "Система", "status": "online" if netdata.get("available") else "offline",
         "detail": "Мониторинг системы", "source": "127.0.0.1:19999", "url": "http://127.0.0.1:19999"},
        {"id": "obsidian", "name": "Obsidian", "group": "Проект", "status": "files-present" if (root / "Home.md").is_file() else "unavailable",
         "detail": "Заметки, исследования и решения человека", "source": "Home.md и notes/"},
        {"id": "telegram", "name": "Telegram", "group": "Уведомления", "status": "board-complete" if telegram_ready else "needs-setup",
         "detail": "Статус задач настройки; доставка сообщений не проверялась", "source": "ALERTS-TG / ALERTS-IMPL"},
        {"id": "autostart", "name": "Сторож и автозапуск", "group": "Система", "status": "paused" if ops.get("autostart_off") else "observed",
         "detail": "Состояние локальных флагов; регистрация расписания не меняется", "source": "ops/autostart.ps1"},
        {"id": "skills", "name": "Skills и роли", "group": "Агенты", "status": "files-present",
         "detail": f"Skills: {len(list((root / '.agents/skills').glob('*/SKILL.md')))}; роли: {len(list((root / '.github/agents').glob('*.agent.md')))}", "source": ".agents/skills/ и .github/agents/"},
    ]
    integrations.extend({"id": runtime["id"], "name": runtime["id"].capitalize(), "group": "Агенты",
                         "status": "cli-present" if runtime["cli_available"] else "unavailable",
                         "detail": f"Модель в реестре: {runtime['model'] or 'не указана'}; доступность сессии не проверялась",
                         "source": "ops/agent-routing.json"} for runtime in runtimes)
    result = {
        "schema_version": 1, "generated_at": datetime.now(timezone.utc).isoformat(),
        "mode": "demo", "data_quality": "local-snapshot", "refresh_seconds": 15,
        "board": board, "claims": claims, "runtimes": runtimes, "queue": queue,
        "engine": engine, "risk": risk, "flags": pick(snapshot.get("flags"), ("KILL", "STOP_ENGINE")),
        "pump": pick(snapshot.get("pump"), ("entry_allowed", "budget_total", "budget_free", "in_positions",
                    "day_pnl", "day_limit", "drawdown", "drawdown_limit", "open_positions")),
        "equity_history": snapshot.get("equity_history") or [],
        "lifetime": part("lifetime", lambda: lifetime_pnl(root / "data" / "bot_state.db"),
                         {"available": False}),
        "ops": {"live": pick(ops.get("live"), ("enabled", "until", "open", "hours_left")),
                "guard": pick(ops.get("guard"), ("denies_24h", "errors_24h", "last_deny_at")),
                "engine_log_age_s": ops.get("engine_log_age_s"), "autostart_off": ops.get("autostart_off")},
        "netdata": pick(netdata, ("available", "version", "cpu_cores", "alarms_critical", "alarms_warning")),
        "integrations": integrations, "modules": project_modules(root),
        "git": part("git", lambda: git_summary(root), {"available": False}),
        "insights": part("insights", lambda: recent_documents(root, "insights"), []),
        "incidents": part("incidents", lambda: incidents(root), []),
        "pending_decisions": part("decisions", lambda: new_decisions(root), []),
        "errors": errors + ["snapshot: partial-source-error"] * len(snapshot.get("errors") or []),
    }
    return _redact_data(result)


def detail(root: Path, task_id: str) -> dict:
    return _redact_data(_task_detail(Path(root), task_id))


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--task")
    args = parser.parse_args(argv)
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    try:
        payload = detail(ROOT, args.task) if args.task else collect(ROOT)
        print(json.dumps(payload, ensure_ascii=False, allow_nan=False))
        return 0
    except Exception as exc:
        print(json.dumps({"error": type(exc).__name__}))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
