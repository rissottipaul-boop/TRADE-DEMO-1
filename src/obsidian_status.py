"""Снимок состояния для пульта Obsidian (задача OBSIDIAN-STATUS). Без сети, только чтение.

    python -m src.obsidian_status                   # пишет data/obsidian/status.json
    python -m src.obsidian_status --out PATH        # в другой файл

Выход: 0 — снимок записан (даже если часть разделов не прочиталась), 2 — не удалось записать.
Запись атомарная: временный файл в том же каталоге, затем os.replace — пульт
(notes/views/live.js) никогда не видит наполовину записанный JSON. Каталог создаётся сам.
Команда работает доли секунды: пульт вызывает её при открытии и по кнопке.

Сбой раздела не роняет снимок: раздел получает null, текст ошибки — в errors.

Источники (все только чтение; пути от корня проекта, а не от текущего каталога):
  engine   data/engine.pid — жив ли процесс; счётчики и started_at — ws_state["engine_stats"]
           в data/bot_state.db (то же, что показывает `python -m src.ops status`).
           Процесс проверяется без сигналов: на Windows — OpenProcess(
           PROCESS_QUERY_LIMITED_INFORMATION) + GetExitCodeProcess (STILL_ACTIVE = 259),
           os.kill не вызывается никогда (на Windows os.kill(pid, 0) завершает процесс);
           на других ОС — os.kill(pid, 0). Дедлайн P1-72H = started_at + 72 ч.
  risk     risk.status() на data/risk_state.db и константы лимитов модуля risk. Как и
           `src.ops status`, status() в первый вызов новых суток UTC делает дневной
           rollover — тот же, что сделал бы движок; других записей нет. Нет базы — раздел null
           (пустую базу снимок не создаёт).
  flags    наличие файлов data/KILL и data/STOP_ENGINE.
  pump     src.pump_journal.pocket_status: data/pump_journal.jsonl и pump-pocket.json.
  equity_history  таблица equity_curve в data/bot_state.db за 48 ч, прорежена до ≤ 300 точек
           (первая и последняя сохраняются). Базы движка читаются отдельным соединением
           `file:…?mode=ro`: движок пишет в ту же базу, снимок в неё не пишет.
  ops      ops/live-policy.json (окно) и ops/live-pocket.json (карман live-runner);
           data/AUTOSTART_OFF (пауза автозапуска), хвост logs/autostart.log и возраст
           logs/engine.log (сторож и тишина движка); хвост data/guard.log (отказы за 24 ч).

Схема v2 (ключи читает notes/views/live.js — менять только с новой версией v;
v1 понимается панелью: секция ops тогда отсутствует):
{"v": 2, "generated_at": "<ISO +05:00>", "generated_ts": <unix>,
 "engine": {"running": bool|null, "pid": int|null, "started_at": unix|null, "uptime_h": float|null,
            "p1_deadline": "<ISO +05:00>"|null, "p1_hours_left": float|null,
            "p1_progress_pct": float|null, "reconciles", "divergences", "errors",
            "errors_exchange", "errors_internal", "pauses", "ws_public_reconnects",
            "ws_private_reconnects", "stats_saved_at": unix|null, "stats_age_s": float|null},
 "risk": {"equity", "hwm", "drawdown_pct", "day_pnl", "day_start_equity", "day_pnl_pct",
          "daily_limit_pct", "global_dd_limit_pct", "daily_breaker", "global_breaker",
          "kill_active", "equity_age_s", "entries_today", "max_entries_per_day",
          "portfolio_heat_pct", "max_heat_pct"},
 "flags": {"KILL": bool, "STOP_ENGINE": bool},
 "pump": {"entry_allowed": bool, "budget_total", "budget_free", "in_positions", "day_pnl",
          "day_limit", "drawdown", "drawdown_limit",
          "open_positions": [{"pair", "size", "cost_usdt", "stop"}], "blocks": [str]},
 "ops": {"live": {"enabled": bool|null, "until": "<ISO +05:00>"|null, "open": bool|null,
                  "hours_left": float|null},
         "live_pocket": {"exists": bool, "example": bool|null, "budget_usdt": float|null,
                         "sleeves": [str]},
         "autostart_off": bool|null,
         "watch": {"log_exists": bool, "last_line": str|null, "last_age_s": float|null},
         "engine_log_age_s": float|null,
         "guard": {"denies_24h": int|null, "errors_24h": int|null, "last_deny_at": "<ISO +05:00>"|null}},
 "equity_history": [[unix, equity], ...],
 "errors": [str]}

Пояснения к полям:
  engine.running  null — проверить процесс не удалось (причина в errors); pid есть, а
                  running=false — файл PID остался от упавшего процесса.
  engine.uptime_h только у живого процесса. Поля P1 считаются от started_at последнего
                  сохранения статистики, прогресс ограничен 100%, остаток — нулём.
  счётчики движка null — ключа нет в статистике (движок на старом коде).
  risk.day_pnl_pct = day_pnl / day_start_equity × 100 (реализованный PnL дня, как у дневного
                  лимита в record_pnl); изменение equity за день пульт считает сам по
                  equity и day_start_equity.
  pump.drawdown   текущая просадка кармана от пика, USDT (drawdown.current_usdt); блокировки
                  по максимальной просадке — в pump.blocks.
  ops.live      окно live-торговли из ops/live-policy.json: open=true — live-процессы вправе
                  торговать реальными деньгами. Файла нет — все поля null, кроме ошибок в errors.
  ops.live_pocket  ops/live-pocket.json: exists=false — live-runner не стартует (preflight);
                  example=true — лежит шаблон, а не карман человека.
  ops.autostart_off  файл data/AUTOSTART_OFF — пауза автозапуска и сторожа без удаления задач.
  ops.watch     последняя строка logs/autostart.log и её возраст: сторож пишет каждые ~5 мин;
                  log_exists=false — сторож ещё ни разу не срабатывал.
  ops.engine_log_age_s  возраст logs/engine.log: движок пишет раз в ~60 с; тишина > 180 с —
                  вероятно, остановлен (тот же порог, что notes/views/safety.js).
  ops.guard     отказы guard за 24 ч из хвоста data/guard.log (читаются последние 64 КиБ):
                  denies_24h — блокировки, errors_24h — сбои самого хука.
"""
from __future__ import annotations

import argparse
import json
import math
import os
import sqlite3
import sys
import tempfile
import time
from contextlib import closing, suppress
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Optional

SCHEMA_VERSION = 2
PROJECT_TZ = timezone(timedelta(hours=5))          # время проекта (ops/board.md)
PROJECT_ROOT = Path(__file__).resolve().parents[1]
# Каталог состояния demo — как config.data_dir("demo"); config не импортируем: он читает .env
DATA_DIR = PROJECT_ROOT / "data"
OUT_PATH = DATA_DIR / "obsidian" / "status.json"
POCKET_PATH = PROJECT_ROOT / "pump-pocket.json"     # как pump_scanner.POCKET_PATH
JOURNAL_PATH = DATA_DIR / "pump_journal.jsonl"      # как pump_scanner.JOURNAL_PATH

P1_HOURS = 72.0
HISTORY_HOURS = 48.0
HISTORY_MAX_POINTS = 300
GUARD_TAIL_BYTES = 65536
GUARD_WINDOW_S = 24 * 3600
ENGINE_COUNTERS = ("reconciles", "divergences", "errors", "errors_exchange", "errors_internal",
                   "pauses", "ws_public_reconnects", "ws_private_reconnects")

# WinAPI для проверки процесса без сигналов
PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
STILL_ACTIVE = 259
ERROR_ACCESS_DENIED = 5


# --- Мелочи ---

def _f(value: Any, nd: int) -> Optional[float]:
    """Число, округлённое до nd знаков; нечисловое и не конечное — None (JSON без NaN)."""
    if value is None or isinstance(value, bool):
        return None
    try:
        x = float(value)
    except (TypeError, ValueError):
        return None
    return round(x, nd) if math.isfinite(x) else None


def _iso(ts: Optional[float]) -> Optional[str]:
    if ts is None:
        return None
    return datetime.fromtimestamp(ts, PROJECT_TZ).isoformat(timespec="seconds")


def _err(exc: BaseException) -> str:
    return f"{type(exc).__name__}: {exc}"


def _connect_ro(path: Path) -> sqlite3.Connection:
    """Соединение только для чтения; отсутствующую базу не создаёт."""
    path = Path(path)
    if not path.exists():
        raise FileNotFoundError(f"нет базы {path}")
    return sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True, timeout=2)


# --- Процесс движка ---

def _win_kernel32():
    """Собственный экземпляр kernel32 с сигнатурами: общий ctypes.windll не трогаем."""
    import ctypes
    from ctypes import wintypes
    k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    k32.OpenProcess.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
    k32.OpenProcess.restype = wintypes.HANDLE
    k32.GetExitCodeProcess.argtypes = (wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD))
    k32.GetExitCodeProcess.restype = wintypes.BOOL
    k32.CloseHandle.argtypes = (wintypes.HANDLE,)
    k32.CloseHandle.restype = wintypes.BOOL
    return k32


def _alive_windows(pid: int, kernel32=None, get_last_error: Optional[Callable[[], int]] = None) -> bool:
    """Жив ли процесс на Windows: только запрос информации, процесс не затрагивается."""
    import ctypes
    from ctypes import wintypes
    if kernel32 is None:
        kernel32 = _win_kernel32()
        get_last_error = ctypes.get_last_error
    handle = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if not handle:
        # Нет доступа — процесс существует (чужой сеанс); иначе (87) — такого PID нет
        return (get_last_error() if get_last_error else 0) == ERROR_ACCESS_DENIED
    try:
        code = wintypes.DWORD()
        if not kernel32.GetExitCodeProcess(handle, ctypes.byref(code)):
            raise OSError(f"GetExitCodeProcess({pid}) не удался, код "
                          f"{get_last_error() if get_last_error else '?'}")
        return code.value == STILL_ACTIVE
    finally:
        kernel32.CloseHandle(handle)


def process_alive(pid: int, *, os_name: Optional[str] = None, kernel32=None,
                  get_last_error: Optional[Callable[[], int]] = None) -> bool:
    """Жив ли процесс с этим PID. На Windows os.kill не вызывается никогда."""
    if pid <= 0:
        return False
    if (os_name or os.name) == "nt":
        return _alive_windows(pid, kernel32, get_last_error)
    try:
        os.kill(pid, 0)          # только не на Windows: там это завершение процесса
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def read_pid(path: Path) -> int:
    text = Path(path).read_text(encoding="utf-8-sig").strip()
    pid = int(text)
    if pid <= 0:
        raise ValueError(f"некорректный PID {text!r} в {path}")
    return pid


# --- Разделы ---

def read_engine_stats(bot_db: Path) -> tuple[Optional[dict], Optional[float]]:
    """ws_state["engine_stats"] и время его записи; строки нет — (None, None)."""
    with closing(_connect_ro(bot_db)) as conn:
        row = conn.execute("SELECT value, update_time FROM ws_state WHERE key='engine_stats'").fetchone()
    if row is None:
        return None, None
    stats = json.loads(row[0])
    if not isinstance(stats, dict):
        raise ValueError(f"engine_stats не объект: {type(stats).__name__}")
    return stats, row[1]


def engine_section(data_dir: Path, now: float, errors: list[str],
                   alive: Callable[[int], bool] = process_alive) -> dict:
    """Процесс и счётчики движка. Сбой одной части не прячет другую: ошибка — в errors."""
    pid: Optional[int] = None
    running: Optional[bool] = None
    pid_path = data_dir / "engine.pid"
    try:
        if pid_path.exists():
            pid = read_pid(pid_path)
            running = bool(alive(pid))
        else:
            running = False        # ops/engine.ps1 stop удаляет файл PID
    except Exception as exc:
        errors.append(f"engine.pid: {_err(exc)}")

    stats: dict = {}
    saved_at: Optional[float] = None
    try:
        raw, row_time = read_engine_stats(data_dir / "bot_state.db")
        if raw is not None:
            stats = raw
            saved_at = _f(raw.get("saved_at"), 3) or _f(row_time, 3)
    except Exception as exc:
        errors.append(f"engine.stats: {_err(exc)}")

    started_at = _f(stats.get("started_at"), 3)
    view: dict[str, Any] = {"running": running, "pid": pid, "started_at": started_at,
                            "uptime_h": None, "p1_deadline": None, "p1_hours_left": None,
                            "p1_progress_pct": None}
    if started_at is not None:
        elapsed = now - started_at
        deadline = started_at + P1_HOURS * 3600
        if running:
            view["uptime_h"] = _f(max(0.0, elapsed) / 3600, 2)
        view["p1_deadline"] = _iso(deadline)
        view["p1_hours_left"] = _f(max(0.0, deadline - now) / 3600, 2)
        view["p1_progress_pct"] = _f(min(100.0, max(0.0, elapsed / (P1_HOURS * 3600) * 100)), 1)
    for key in ENGINE_COUNTERS:
        value = stats.get(key)
        view[key] = int(value) if isinstance(value, (int, float)) and not isinstance(value, bool) else None
    view["stats_saved_at"] = saved_at
    view["stats_age_s"] = _f(now - saved_at, 1) if saved_at is not None else None
    return view


def read_risk(risk_db: Path) -> tuple[dict, dict]:
    """risk.status() и лимиты риск-ядра. Модуль risk только читается, база не создаётся."""
    if not Path(risk_db).exists():
        raise FileNotFoundError(f"нет базы риска {risk_db}")
    from . import risk
    risk.init(risk_db)
    limits = {"daily_limit_pct": risk.DAILY_LOSS_LIMIT_PCT,
              "global_dd_limit_pct": risk.GLOBAL_DD_LIMIT_PCT,
              "max_entries_per_day": risk.MAX_ENTRIES_PER_DAY,
              "max_heat_pct": risk.MAX_PORTFOLIO_HEAT_PCT}
    return risk.status(), limits


def risk_section(status: dict, limits: dict) -> dict:
    day_pnl = _f(status.get("day_pnl"), 2)
    day_start = _f(status.get("day_start_equity"), 2)
    day_pct = day_pnl / day_start * 100 if day_pnl is not None and day_start else None
    entries = status.get("entries_today")
    return {
        "equity": _f(status.get("equity"), 2),
        "hwm": _f(status.get("hwm"), 2),
        "drawdown_pct": _f(status.get("drawdown_pct"), 3),
        "day_pnl": day_pnl,
        "day_start_equity": day_start,
        "day_pnl_pct": _f(day_pct, 3),
        "daily_limit_pct": _f(limits.get("daily_limit_pct"), 3),
        "global_dd_limit_pct": _f(limits.get("global_dd_limit_pct"), 3),
        "daily_breaker": bool(status.get("daily_breaker")),
        "global_breaker": bool(status.get("global_breaker")),
        "kill_active": bool(status.get("kill_active")),
        "equity_age_s": _f(status.get("equity_age_s"), 1),
        "entries_today": int(entries) if entries is not None else None,
        "max_entries_per_day": limits.get("max_entries_per_day"),
        "portfolio_heat_pct": _f(status.get("portfolio_heat_pct"), 3),
        "max_heat_pct": _f(limits.get("max_heat_pct"), 3),
    }


def flags_section(data_dir: Path) -> dict:
    return {"KILL": (data_dir / "KILL").exists(), "STOP_ENGINE": (data_dir / "STOP_ENGINE").exists()}


def read_pump(journal: Path, pocket: Path, now: datetime) -> dict:
    """Отчёт src.pump_journal (журнал и карман только читаются)."""
    from .pump_journal import pocket_status
    return pocket_status(journal=journal, pocket=pocket, now=now)


def pump_section(report: dict) -> dict:
    budget = report.get("budget") or {}
    day = report.get("day") or {}
    dd = report.get("drawdown") or {}
    entry = report.get("entry") or {}
    positions = (report.get("positions") or {}).get("list") or []
    return {
        "entry_allowed": bool(entry.get("allowed")),
        "budget_total": budget.get("budget_usdt"),
        "budget_free": budget.get("free_usdt"),
        "in_positions": budget.get("in_positions_usdt"),
        "day_pnl": day.get("pnl"),
        "day_limit": day.get("loss_limit"),
        "drawdown": dd.get("current_usdt"),
        "drawdown_limit": dd.get("limit_usdt"),
        "open_positions": [{"pair": p.get("pair"), "size": p.get("size"),
                            "cost_usdt": p.get("cost_usdt"), "stop": p.get("stop")}
                           for p in positions],
        "blocks": [str(b) for b in entry.get("blocks") or []],
    }


def _parse_ts(text: Any) -> Optional[float]:
    """ISO-строка с зоной → unix; без зоны или не разобралась — None (снимок не роняет)."""
    if not isinstance(text, str) or not text.strip():
        return None
    try:
        dt = datetime.fromisoformat(text.strip().replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is None:
        return None
    return dt.timestamp()


def _tail_lines(path: Path, max_bytes: int = GUARD_TAIL_BYTES) -> list[str]:
    """Последние строки файла, не больше max_bytes с конца; файла нет — []."""
    try:
        size = Path(path).stat().st_size
    except OSError:
        return []
    with open(path, "rb") as fh:
        fh.seek(max(0, size - max_bytes))
        return fh.read().decode("utf-8", errors="replace").splitlines()


def live_part(root: Path, now: float) -> dict:
    """Окно live-торговли из ops/live-policy.json. Файла нет — все поля null."""
    view: dict[str, Any] = {"enabled": None, "until": None, "open": None, "hours_left": None}
    policy = json.loads((Path(root) / "ops" / "live-policy.json").read_text(encoding="utf-8-sig"))
    if not isinstance(policy, dict):
        raise ValueError("live-policy.json не объект")
    enabled = policy.get("live_enabled")
    view["enabled"] = bool(enabled) if enabled is not None else None
    until_ts = _parse_ts(policy.get("enabled_until"))
    view["until"] = _iso(until_ts)
    if view["enabled"] is not None and until_ts is not None:
        view["open"] = bool(view["enabled"]) and until_ts > now
        view["hours_left"] = _f(max(0.0, until_ts - now) / 3600, 1)
    return view


def live_pocket_part(root: Path) -> dict:
    """Карман live-runner: есть ли файл, шаблон ли, бюджет и рукава (только чтение)."""
    view: dict[str, Any] = {"exists": False, "example": None, "budget_usdt": None, "sleeves": []}
    pocket_path = Path(root) / "ops" / "live-pocket.json"
    if not pocket_path.exists():
        return view
    pocket = json.loads(pocket_path.read_text(encoding="utf-8-sig"))
    if not isinstance(pocket, dict):
        raise ValueError("live-pocket.json не объект")
    view["exists"] = True
    view["example"] = bool(pocket.get("example", False))
    view["budget_usdt"] = _f(pocket.get("budget_usdt"), 2)
    sleeves = pocket.get("sleeves")
    view["sleeves"] = sorted(sleeves) if isinstance(sleeves, dict) else []
    return view


def watch_part(root: Path, data_dir: Path, now: float) -> tuple[Any, Any, dict]:
    """Пауза автозапуска, последняя строка сторожа и возраст лога движка."""
    root, data_dir = Path(root), Path(data_dir)
    off: Optional[bool] = (data_dir / "AUTOSTART_OFF").exists()
    watch: dict[str, Any] = {"log_exists": False, "last_line": None, "last_age_s": None}
    log_path = root / "logs" / "autostart.log"
    if log_path.exists():
        watch["log_exists"] = True
        lines = [ln.strip() for ln in _tail_lines(log_path, 8192) if ln.strip()]
        if lines:
            last = lines[-1][:220]
            watch["last_line"] = last
            ts = _parse_ts(last.split(" ", 1)[0])
            watch["last_age_s"] = _f(now - ts, 1) if ts is not None else None
    engine_age: Optional[float] = None
    try:
        engine_age = _f(now - (root / "logs" / "engine.log").stat().st_mtime, 1)
    except OSError:
        engine_age = None
    return off, watch, engine_age


def guard_part(data_dir: Path, now: float) -> dict:
    """Отказы guard за 24 ч из хвоста data/guard.log; лога нет — счётчики null."""
    view: dict[str, Any] = {"denies_24h": None, "errors_24h": None, "last_deny_at": None}
    log_path = Path(data_dir) / "guard.log"
    if not log_path.exists():
        return view
    denies = errs = 0
    last_deny: Optional[float] = None
    for line in _tail_lines(log_path):
        try:
            rec = json.loads(line)
        except ValueError:
            continue
        if not isinstance(rec, dict):
            continue
        ts = _parse_ts(rec.get("ts"))
        if ts is None or now - ts > GUARD_WINDOW_S:
            continue
        if rec.get("decision") == "deny":
            denies += 1
            last_deny = ts if last_deny is None or ts > last_deny else last_deny
        elif rec.get("decision") == "error":
            errs += 1
    view["denies_24h"] = denies
    view["errors_24h"] = errs
    view["last_deny_at"] = _iso(last_deny)
    return view


def ops_section(project_root: Path, data_dir: Path, now: float, errors: list[str]) -> dict:
    """Дежурство для пульта: live-окно, карман, сторож, guard. Часть сбоила — null и errors."""
    root = Path(project_root)
    view: dict[str, Any] = {}
    try:
        view["live"] = live_part(root, now)
    except Exception as exc:
        errors.append(f"ops.live: {_err(exc)}")
        view["live"] = {"enabled": None, "until": None, "open": None, "hours_left": None}
    try:
        view["live_pocket"] = live_pocket_part(root)
    except Exception as exc:
        errors.append(f"ops.live_pocket: {_err(exc)}")
        view["live_pocket"] = {"exists": None, "example": None, "budget_usdt": None, "sleeves": []}
    try:
        off, watch, engine_age = watch_part(root, data_dir, now)
        view["autostart_off"] = off
        view["watch"] = watch
        view["engine_log_age_s"] = engine_age
    except Exception as exc:
        errors.append(f"ops.watch: {_err(exc)}")
        view["autostart_off"] = None
        view["watch"] = {"log_exists": None, "last_line": None, "last_age_s": None}
        view["engine_log_age_s"] = None
    try:
        view["guard"] = guard_part(data_dir, now)
    except Exception as exc:
        errors.append(f"ops.guard: {_err(exc)}")
        view["guard"] = {"denies_24h": None, "errors_24h": None, "last_deny_at": None}
    return view


def thin(points: list, limit: int = HISTORY_MAX_POINTS) -> list:
    """Равномерное прорежение до limit точек; первая и последняя сохраняются."""
    n = len(points)
    if n <= limit:
        return list(points)
    if limit <= 1:
        return points[-1:] if limit == 1 else []
    step = (n - 1) / (limit - 1)
    return [points[round(i * step)] for i in range(limit)]


def read_equity_history(bot_db: Path, now: float, hours: float = HISTORY_HOURS,
                        limit: int = HISTORY_MAX_POINTS) -> list:
    with closing(_connect_ro(bot_db)) as conn:
        rows = conn.execute("SELECT ts, total_eq FROM equity_curve WHERE ts >= ? ORDER BY ts",
                            (now - hours * 3600,)).fetchall()
    points = [[int(ts), _f(eq, 2)] for ts, eq in rows if _f(eq, 2) is not None]
    return thin(points, limit)


# --- Снимок ---

def _section(name: str, fn: Callable[[], Any], errors: list[str]) -> Any:
    try:
        return fn()
    except Exception as exc:
        errors.append(f"{name}: {_err(exc)}")
        return None


def build_snapshot(now: Optional[float] = None, *, data_dir: Path = DATA_DIR,
                   pocket_path: Path = POCKET_PATH, journal_path: Path = JOURNAL_PATH,
                   project_root: Path = PROJECT_ROOT,
                   alive: Callable[[int], bool] = process_alive,
                   risk_reader: Callable[[Path], tuple[dict, dict]] = read_risk,
                   pump_reader: Callable[[Path, Path, datetime], dict] = read_pump) -> dict:
    """Снимок схемы v2. Не бросает: сбой раздела — null и строка в errors."""
    now = time.time() if now is None else now
    data_dir = Path(data_dir)
    errors: list[str] = []
    bot_db = data_dir / "bot_state.db"
    snap: dict[str, Any] = {"v": SCHEMA_VERSION, "generated_at": _iso(now), "generated_ts": int(now)}
    snap["engine"] = _section("engine", lambda: engine_section(data_dir, now, errors, alive), errors)
    snap["risk"] = _section("risk", lambda: risk_section(*risk_reader(data_dir / "risk_state.db")), errors)
    snap["flags"] = _section("flags", lambda: flags_section(data_dir), errors)
    snap["pump"] = _section("pump", lambda: pump_section(
        pump_reader(journal_path, pocket_path, datetime.fromtimestamp(now, timezone.utc))), errors)
    snap["ops"] = _section("ops", lambda: ops_section(project_root, data_dir, now, errors), errors)
    snap["equity_history"] = _section("equity_history", lambda: read_equity_history(bot_db, now), errors)
    snap["errors"] = errors
    return snap


def write_atomic(path: Path, data: dict, retries: int = 5) -> None:
    """JSON во временный файл рядом, затем os.replace; временный файл при сбое удаляется."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as fh:
            json.dump(data, fh, ensure_ascii=False, separators=(",", ":"), allow_nan=False)
            fh.write("\n")
        for attempt in range(retries):
            try:
                os.replace(tmp, path)
                return
            except PermissionError:
                # Windows: файл-приёмник на миг открыт читателем без FILE_SHARE_DELETE
                if attempt == retries - 1:
                    raise
                time.sleep(0.05)
    except BaseException:
        with suppress(OSError):
            os.unlink(tmp)
        raise


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m src.obsidian_status",
                                     description=__doc__.splitlines()[0])
    parser.add_argument("--out", type=Path, default=OUT_PATH,
                        help=f"куда писать снимок (по умолчанию {OUT_PATH})")
    args = parser.parse_args(argv)
    try:
        snap = build_snapshot()
        write_atomic(args.out, snap)
    except Exception as exc:
        print(f"снимок не записан: {args.out}: {_err(exc)}", file=sys.stderr)
        return 2
    print(f"снимок записан: {args.out} (разделов с ошибкой: {len(snap['errors'])})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
