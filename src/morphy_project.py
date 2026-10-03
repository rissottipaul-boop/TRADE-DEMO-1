"""Проекция данных проекта для Morphy. Только чтение, без торговых запросов.

Форма ответа — контракт пакета MORPHY-UI-* (§1): новые поля аддитивны, schema_version
остаётся 1. К бирже и внешней сети модуль не обращается (как и раньше, опрашиваются
только локальные Netdata и Morphy на 127.0.0.1). Базы читаются отдельным соединением
`_connect_ro` (`file:…?mode=ro`); файлы проекта не создаются и не меняются.

Разделы, добавленные для экрана проекта v2:
  equity     current (equity риск-ядра, иначе последняя точка кривой), baseline_usdt
             (внесённые деньги = lifetime.base_usdt), hwm риск-ядра и ranges 24h/48h/7d/all:
             equity_curve читается одним запросом, окна выбираются по времени от «сейчас»,
             каждое прорежено до ≤ 300 точек с первой и последней (obsidian_status.thin).
  spark      серии ≤ 40 точек: капитал за 24 ч; результат текущих риск-суток
             (граница — risk._utc_day, 00:00 UTC) как equity − day_start_equity, а без
             day_start_equity — изменение за 24 ч от первой точки с пометкой в spark_note;
             просадка за 7 суток от скользящего максимума всей кривой (как HWM, точки
             equity_curve); накопленная прибыль без внешних потоков — net_series, та же
             функция, что считает lifetime_pnl.
  positions  positions бот-БД (последняя сверка движка), слоты risk_open_risk риск-ядра и
             позиции pump-кармана по журналу (positions_view). Котировок и ctVal локально
             нет, поэтому mark и upl_pct всегда null; стоп известен только у кармана.
  events     единая лента ≤ 100 событий, новые сверху (events_view): risk_events, orders и
             trades, отказы guard (время и reason, без snippet и команд), alerts.jsonl,
             строки ops/incidents.md с датой, журнал действий пульта, журнал кармана и
             смены статусов задач. Нечитаемый источник не роняет остальные.
  sources    доступность и возраст источников для бейджей «источник недоступен».
  controls   описание действий пульта из src.morphy_actions.describe (модуль
             MORPHY-UI-CONTROLS); нет модуля — {"mode": "demo", "enabled": false, ...}.
  board.tasks[*].since/age_days/age_basis — когда задача вошла в текущий статус
             (task_ages: история git доски и даты в заметках; неизвестно — null).
"""
from __future__ import annotations

import argparse
import bisect
from contextlib import closing
from datetime import datetime, timedelta, timezone
import importlib
import importlib.util
import json
import os
from pathlib import Path
import re
import shutil
import sqlite3
import subprocess
import sys
import time
import urllib.request

from src.agent_context import STATUSES, new_decisions, read_rows
from src.control_panel import _board, _queue, _redact_data, _task_detail
from src.netdata_monitor import check_netdata_health
from src.obsidian_status import (PROJECT_TZ, _connect_ro, _f, _iso, _parse_ts, _tail_lines,
                                 build_snapshot, read_pump, thin)

ROOT = Path(__file__).resolve().parents[1]

# Скачок капитала между соседними точками больше этой доли — внешний поток
# (пополнение или вывод demo-счёта), а не результат торговли. Тот же порог
# применяет график капитала в notes/views/live.js.
EXTERNAL_FLOW_RATIO = 0.2

EQUITY_RANGES = {"24h": 24, "48h": 48, "7d": 168, "all": None}
RANGE_MAX_POINTS = 300
SPARK_MAX_POINTS = 40
SPARK_EQUITY_HOURS = 24
SPARK_DRAWDOWN_HOURS = 168

EVENTS_LIMIT = 100
EVENT_TITLE_MAX = 120
EVENT_DETAIL_MAX = 240
TAIL_BYTES = 64 * 1024              # хвост журналов, как у guard_part в obsidian_status
PUMP_TAIL_BYTES = 256 * 1024        # журнал кармана в основном из сканов: хвост длиннее

GIT_LOG_LIMIT = 60                  # коммитов доски для оценки возраста задач
GIT_TIMEOUT_S = 5
AGE_SLACK_S = 600                   # допуск часов агента относительно коммитов
AGE_MAX_WINDOW_S = 12 * 3600        # окно git шире — время смены статуса неизвестно

CONTROLS_FALLBACK = {"mode": "demo", "enabled": False, "actions": []}
POSITIONS_NOTE = ("Снимок локального состояния ≠ сверка с биржей. mark и uPnL% не выводятся: "
                  "котировки и ctVal локально не хранятся; uPnL — из последней сверки движка. "
                  "Стоп известен только у позиций кармана (журнал), расстояние до стопа — от цены входа.")


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


# --- Мелочи ---

def _iso_utc(ts) -> str | None:
    return None if ts is None else datetime.fromtimestamp(ts, timezone.utc).isoformat(timespec="seconds")


def _age(now: float, ts) -> float | None:
    return None if ts is None else _f(max(0.0, now - ts), 1)


def _num(value) -> str:
    number = _f(value, 8)
    return "?" if number is None else f"{number:.10g}"


def _clip(text: str, limit: int) -> str:
    text = " ".join(str(text).split())
    return text if len(text) <= limit else text[:limit - 1].rstrip() + "…"


def _source(name: str, fn, errors: list[str]):
    """(статус, значение): ok | missing (нет файла или таблицы) | error (тип исключения в errors).

    Текст исключения не выводится: он может содержать путь или данные источника.
    """
    try:
        return "ok", fn()
    except FileNotFoundError:
        return "missing", None
    except sqlite3.OperationalError as exc:
        if "no such table" in str(exc):
            return "missing", None
        errors.append(f"{name}: {type(exc).__name__}")
        return "error", None
    except Exception as exc:
        errors.append(f"{name}: {type(exc).__name__}")
        return "error", None


# --- Капитал: кривая, диапазоны, спарклайны ---

def read_curve(bot_db: Path, since: float | None = None) -> list[tuple[float, float]]:
    """Точки equity_curve [(ts, equity)] по возрастанию ts, с since — только ts >= since.

    Базы нет — FileNotFoundError (_connect_ro), таблицы нет — sqlite3.OperationalError.
    """
    query = "SELECT ts, total_eq FROM equity_curve"
    params: tuple = ()
    if since is not None:
        query += " WHERE ts >= ?"
        params = (since,)
    with closing(_connect_ro(Path(bot_db))) as conn:
        rows = conn.execute(query + " ORDER BY ts", params).fetchall()
    return [(ts, eq) for ts, eq in ((_f(ts, 0), _f(eq, 2)) for ts, eq in rows)
            if ts is not None and eq is not None]


def net_series(points, ratio: float = EXTERNAL_FLOW_RATIO) -> tuple[list, float, int]:
    """Накопленная торговая прибыль по соседним точкам, без внешних потоков.

    Скачок больше `ratio` от предыдущей точки (или предыдущая ≤ 0) — пополнение или
    вывод demo-счёта, он копится отдельно. Возвращает (серия [[ts, прибыль с начала]],
    сумма внешних потоков, их число). Общая основа lifetime_pnl и spark.lifetime_net.
    """
    if not points:
        return [], 0.0, 0
    net = external = 0.0
    flows = 0
    series = [[points[0][0], 0.0]]
    for (_, before), (ts, after) in zip(points, points[1:]):
        delta = after - before
        if before <= 0 or abs(delta) > before * ratio:
            external += delta
            flows += 1
        else:
            net += delta
        series.append([ts, net])
    return series, external, flows


def lifetime_from_points(points, ratio: float = EXTERNAL_FLOW_RATIO) -> dict:
    """Итог lifetime_pnl по уже прочитанным точкам кривой."""
    if len(points) < 2:
        return {"available": False, "points": len(points)}
    series, external, flows = net_series(points, ratio)
    net = series[-1][1]
    base = points[-1][1] - net
    return {"available": True, "net_usdt": round(net, 2),
            "net_pct": round(net / base * 100, 2) if base > 0 else None,
            "base_usdt": round(base, 2), "external_usdt": round(external, 2),
            "external_flows": flows, "points": len(points),
            "since": datetime.fromtimestamp(points[0][0], timezone.utc).isoformat(),
            "equity_usdt": points[-1][1]}


def lifetime_pnl(bot_db: Path, ratio: float = EXTERNAL_FLOW_RATIO) -> dict:
    """Прибыль за всё время проекта по таблице equity_curve, без внешних потоков.

    `последняя − первая` на demo-счёте бессмысленна: пополнение 24.09 подняло
    капитал с 5 000 до ~104 000. Поэтому суммируются приращения между соседними
    точками, а скачки больше `ratio` относятся к внешним потокам и в прибыль не
    идут (net_series). База процента = капитал сейчас − прибыль, то есть внесённые деньги.
    """
    bot_db = Path(bot_db)
    if not bot_db.is_file():                 # базы нет — не ошибка, а отсутствие истории
        return {"available": False, "points": 0}
    return lifetime_from_points(read_curve(bot_db), ratio)


def _series(points, limit: int, nd: int = 2) -> list:
    """[[unix_sec, value]] после прорежения; меньше двух точек — []."""
    if len(points) < 2:
        return []
    return [[int(ts), round(value, nd)] for ts, value in thin(list(points), limit)]


def _window(points, stamps: list, start: float | None) -> list:
    return list(points) if start is None else points[bisect.bisect_left(stamps, start):]


def equity_view(points, now: float, risk: dict | None, lifetime: dict) -> dict:
    """Кривая капитала с baseline (внесённые деньги) и HWM; окна — от текущего времени."""
    stamps = [ts for ts, _ in points]
    ranges = {key: _series(_window(points, stamps, None if hours is None else now - hours * 3600),
                           RANGE_MAX_POINTS)
              for key, hours in EQUITY_RANGES.items()}
    risk = risk or {}
    current = risk.get("equity")
    if current is None and points:
        current = points[-1][1]
    return {"current": current,
            "baseline_usdt": lifetime.get("base_usdt") if lifetime.get("available") else None,
            "hwm": risk.get("hwm"), "ranges": ranges, "range_hours": dict(EQUITY_RANGES)}


def risk_day_start(now: float) -> float:
    """Начало текущих риск-суток. Граница — та же, что у дневного rollover риск-ядра."""
    from src import risk as risk_core
    day = datetime.strptime(risk_core._utc_day(now), "%Y-%m-%d")
    return day.replace(tzinfo=timezone.utc).timestamp()


def spark_view(points, now: float, risk: dict | None, day_start_ts: float | None) -> tuple[dict, str | None]:
    """Спарклайны карточек (≤ 40 точек) и пояснение, если серия посчитана приближённо."""
    notes = []
    stamps = [ts for ts, _ in points]
    last_day = _window(points, stamps, now - SPARK_EQUITY_HOURS * 3600)
    day_start_equity = _f((risk or {}).get("day_start_equity"), 2)
    if day_start_ts is not None and day_start_equity and day_start_equity > 0:
        today = _window(points, stamps, day_start_ts)
        day_pnl = _series([(ts, eq - day_start_equity) for ts, eq in today], SPARK_MAX_POINTS)
    else:
        day_pnl = _series([(ts, eq - last_day[0][1]) for ts, eq in last_day], SPARK_MAX_POINTS)
        if day_pnl:
            notes.append("day_pnl: нет day_start_equity риск-ядра — изменение капитала за 24 ч "
                         "от первой точки, не результат риск-суток")
    peak = None
    drawdown = []
    cutoff = now - SPARK_DRAWDOWN_HOURS * 3600
    for ts, eq in points:                    # максимум копится по всей истории, как HWM
        peak = eq if peak is None or eq > peak else peak
        if ts >= cutoff and peak > 0:
            drawdown.append((ts, max(0.0, (1 - eq / peak) * 100)))
    lifetime, _, _ = net_series(points)
    return ({"equity": _series(last_day, SPARK_MAX_POINTS),
             "day_pnl": day_pnl,
             "drawdown_pct": _series(drawdown, SPARK_MAX_POINTS, nd=3),
             "lifetime_net": _series(lifetime, SPARK_MAX_POINTS)},
            "; ".join(notes) or None)


# --- Позиции ---

POSITION_KEYS = ("id", "source", "symbol", "side", "size", "entry", "mark", "upl", "upl_pct",
                 "notional", "stop", "stop_distance_pct", "stop_basis", "liq_px", "opened_at",
                 "age_s", "updated_at", "risk_pct")


def _position(**fields) -> dict:
    item = dict.fromkeys(POSITION_KEYS)
    item.update(fields)
    return item


def _is_contract(inst_id: str) -> bool:
    """BTC-USDT — спот или маржа; -SWAP, фьючерсы и опционы — контракты."""
    return len(str(inst_id).split("-")) != 2


def read_engine_positions(bot_db: Path) -> list[tuple]:
    """Открытые позиции из таблицы positions (src/storage.py); закрытые хранятся с pos = 0."""
    with closing(_connect_ro(bot_db)) as conn:
        return conn.execute("SELECT inst_id, pos_side, pos, avg_px, upl, liq_px, update_time "
                            "FROM positions WHERE pos != 0 ORDER BY inst_id, pos_side").fetchall()


def read_risk_slots(risk_db: Path) -> list[dict]:
    """Слоты risk_open_risk и остаток из risk_position_size (src/risk.py, ROUTER-EXIT)."""
    with closing(_connect_ro(risk_db)) as conn:
        slots = conn.execute("SELECT inst_id, side, risk_pct, opened_at FROM risk_open_risk "
                             "ORDER BY opened_at").fetchall()
        try:
            sizes = {(inst, side, opened): sz for inst, side, opened, sz in conn.execute(
                "SELECT inst_id, side, opened_at, sz FROM risk_position_size")}
        except sqlite3.OperationalError:     # база на коде до ROUTER-EXIT
            sizes = {}
    return [{"inst_id": inst, "side": side, "risk_pct": pct, "opened_at": opened,
             "sz": sizes.get((inst, side, opened))} for inst, side, pct, opened in slots]


def positions_view(data_dir: Path, now: float, pump_list: list, pump_state: str,
                   errors: list[str]) -> dict:
    """Что держит бот: движок (сверка), риск-ядро (слоты) и pump-карман (журнал).

    Слот риск-ядра по тому же инструменту и стороне, что позиция движка, не дублируется:
    он даёт позиции дату входа (opened_at) и risk_pct. mark не выводится — честно его
    из локальных данных не получить (котировки и ctVal не хранятся).
    """
    data_dir = Path(data_dir)
    engine_state, engine_rows = _source("positions.engine",
                                        lambda: read_engine_positions(data_dir / "bot_state.db"), errors)
    risk_state, slots = _source("positions.risk", lambda: read_risk_slots(data_dir / "risk_state.db"), errors)
    slots_by_inst = {slot["inst_id"]: slot for slot in slots or []}
    items: list[dict] = []
    updated: list[float] = []
    for inst_id, pos_side, pos, avg_px, upl, liq_px, update_time in engine_rows or []:
        size = _f(pos, 8)
        if not size:
            continue
        if pos_side in ("long", "short"):
            side = pos_side
        elif pos_side == "net":              # net-режим: направление — знак позиции
            side = "long" if size > 0 else "short"
        else:
            side = "net"
        slot = slots_by_inst.get(inst_id)
        if slot and {"buy": "long", "sell": "short"}.get(str(slot["side"]).lower()) == side:
            slots_by_inst.pop(inst_id)
        else:
            slot = None
        opened = _f(slot["opened_at"], 3) if slot else None
        stamp = _f(update_time, 3)
        if stamp is not None:
            updated.append(stamp)
        items.append(_position(id=f"engine:{inst_id}:{side}", source="engine", symbol=inst_id,
                               side=side, size=abs(size), entry=_f(avg_px, 8) or None,
                               upl=_f(upl, 2), liq_px=_f(liq_px, 8) or None,
                               opened_at=_iso_utc(opened), age_s=_age(now, opened),
                               updated_at=_iso_utc(stamp),
                               risk_pct=_f(slot["risk_pct"], 3) if slot else None))
    for slot in slots_by_inst.values():
        inst_id = slot["inst_id"]
        raw_side = str(slot["side"]).lower()
        if raw_side == "buy" and not _is_contract(inst_id):
            side = "spot"
        else:
            side = {"buy": "long", "sell": "short"}.get(raw_side, "net")
        opened = _f(slot["opened_at"], 3)
        items.append(_position(id=f"risk:{inst_id}:{side}", source="risk", symbol=inst_id, side=side,
                               size=_f(slot["sz"], 8), opened_at=_iso_utc(opened),
                               age_s=_age(now, opened), risk_pct=_f(slot["risk_pct"], 3)))
    for record in pump_list or []:
        if not isinstance(record, dict):
            continue
        pair = record.get("pair")
        size = _f(record.get("size"), 8)
        cost = _f(record.get("cost_usdt"), 2)
        entry = _f(record.get("price"), 8) or None
        if entry is None and cost and size:
            entry = _f(cost / size, 8)       # средняя цена входа по стоимости из журнала
        raw_side = str(record.get("side") or "buy").lower()
        stop = _f(record.get("stop"), 8)
        distance = None
        if entry and stop is not None:
            # > 0 — до стопа против позиции; < 0 — стоп уже за входом в сторону прибыли
            distance = (entry - stop) / entry * 100 if raw_side == "buy" else (stop - entry) / entry * 100
        opened = _parse_ts(record.get("opened"))
        items.append(_position(id=f"pump:{record.get('trade_id') or pair}", source="pump",
                               symbol=pair, side="spot" if raw_side == "buy" else "short",
                               size=size, entry=entry, notional=cost, stop=stop,
                               stop_distance_pct=_f(distance, 3),
                               stop_basis="entry" if distance is not None else None,
                               opened_at=_iso_utc(opened), age_s=_age(now, opened)))
    upls = [item["upl"] for item in items if item["upl"] is not None]
    states = {"engine": engine_state, "risk": risk_state, "pump": pump_state}
    return {"ok": any(state == "ok" for state in states.values()), "count": len(items),
            "total_upl": round(sum(upls), 2) if upls else None, "note": POSITIONS_NOTE,
            "items": items, "sources": states,
            "age_s": _f(now - max(updated), 1) if updated else None}


# --- Лента событий ---

RISK_EVENTS = {
    "kill_switch": ("kill", "crit", "Включён kill-switch"),
    "breaker_reset_kill": ("kill", "warn", "Kill-switch снят вручную"),
    "breaker_trip_daily": ("breaker", "crit", "Сработал дневной breaker"),
    "breaker_trip_global": ("breaker", "crit", "Сработал глобальный breaker"),
    "breaker_reset_daily": ("breaker", "warn", "Дневной breaker снят вручную"),
    "breaker_reset_global": ("breaker", "warn", "Глобальный breaker снят вручную"),
    "daily_breaker_auto_reset": ("breaker", "info", "Дневной breaker снят в 00:00 UTC"),
    "system_pause": ("breaker", "warn", "Системная пауза после серии убытков"),
    "instrument_blocked": ("breaker", "warn", "Инструмент заблокирован"),
    "instrument_unblocked": ("breaker", "info", "Блокировка инструмента истекла"),
    "pnl": ("fill", "info", "Закрыта сделка"),
    "position_closed": ("fill", "info", "Позиция закрыта"),
    "position_reduced": ("fill", "info", "Позиция уменьшена"),
    "spot_buy": ("order", "info", "Доливка спот-позиции"),
    "exit_release_skipped": ("order", "warn", "Выход не освободил слот риска"),
    "stop_beyond_liquidation": ("stop", "warn", "Стоп за ликвидацией — вход отклонён"),
    "equity_invalid": ("engine", "warn", "Нечисловое equity отброшено"),
}
SIDES_RU = {"buy": "покупка", "sell": "продажа"}
OWNER_PREFIXES = ("botl", "bot", "trd", "pmp", "sen", "iex", "hnt", "smk", "lt", "rt")
SEVERITY_BY_LEVEL = {"CRITICAL": "crit", "ERROR": "crit", "WARNING": "warn", "WARN": "warn"}
ACTION_OUTCOMES = {"ok": "выполнено", "failed": "сбой", "rejected": "отклонено"}
TASK_EVENTS = {"done": ("info", "Задача закрыта"), "in-progress": ("info", "Задача взята в работу"),
               "needs-user": ("warn", "Нужно решение человека"), "blocked": ("warn", "Задача заблокирована")}
_LABEL = re.compile(r"^[a-z][a-z0-9._-]{0,40}$")
_INCIDENT_TS = re.compile(r"(\d{4})-(\d{2})-(\d{2})[ T](\d{1,2}):(\d{2})"
                          r"(?:\s*\(?\s*(?:UTC)?\s*([+-])(\d{2}):?(\d{2})\s*\)?)?")


def _event(ts: float, kind: str, severity: str, title, detail, source: str) -> dict:
    """Событие ленты: заголовок ≤ 120, деталь ≤ 240 символов; сначала redaction, потом обрезка."""
    return {"_ts": ts, "ts": _iso_utc(ts), "kind": kind, "severity": severity,
            "title": _clip(_redact_data(str(title)), EVENT_TITLE_MAX),
            "detail": _clip(_redact_data(str(detail)), EVENT_DETAIL_MAX) if detail not in (None, "") else None,
            "source": source}


def _jsonl_tail(path: Path, max_bytes: int = TAIL_BYTES):
    """Объекты из последних max_bytes файла; файла нет — FileNotFoundError."""
    path = Path(path)
    if not path.is_file():
        raise FileNotFoundError(path.name)
    for line in _tail_lines(path, max_bytes):
        try:
            record = json.loads(line)
        except ValueError:
            continue
        if isinstance(record, dict):
            yield record


def _owner(cl_ord_id) -> str | None:
    """Код владельца по префиксу clOrdId (AGENTS.md §6); сам clOrdId не выводится."""
    if isinstance(cl_ord_id, str):
        for prefix in OWNER_PREFIXES:
            if cl_ord_id.startswith(prefix):
                return prefix
    return None


def _events_risk(risk_db: Path) -> list[dict]:
    """risk_events (src/risk.py): kill-switch, breaker'ы, закрытия сделок, блокировки."""
    with closing(_connect_ro(risk_db)) as conn:
        rows = conn.execute("SELECT ts, event, inst_id, detail FROM risk_events "
                            "ORDER BY ts DESC LIMIT ?", (EVENTS_LIMIT,)).fetchall()
    events = []
    for ts, name, inst_id, detail in rows:
        stamp = _f(ts, 3)
        if stamp is None:
            continue
        name = str(name)
        kind, severity, title = RISK_EVENTS.get(
            name, ("engine", "info", f"Событие риск-ядра: {name if _LABEL.match(name) else '?'}"))
        if inst_id:
            title = f"{title}: {inst_id}"
        events.append(_event(stamp, kind, severity, title, detail, "risk_events"))
    return events


def _events_orders(bot_db: Path) -> list[dict]:
    """orders и trades без ордера в базе (src/storage.py); raw_json и clOrdId не выводятся."""
    with closing(_connect_ro(bot_db)) as conn:
        orders = conn.execute("SELECT inst_id, side, ord_type, px, sz, state, filled_sz, avg_px, "
                              "create_time, update_time, cl_ord_id FROM orders "
                              "ORDER BY update_time DESC LIMIT ?", (EVENTS_LIMIT,)).fetchall()
        try:
            trades = conn.execute("SELECT t.inst_id, t.side, t.px, t.sz, t.ts FROM trades AS t "
                                  "WHERE NOT EXISTS (SELECT 1 FROM orders AS o WHERE o.ord_id = t.ord_id) "
                                  "ORDER BY t.ts DESC LIMIT ?", (EVENTS_LIMIT,)).fetchall()
        except sqlite3.OperationalError as exc:
            if "no such table" not in str(exc):
                raise
            trades = []
    events = []
    for inst_id, side, ord_type, px, sz, state, filled, avg_px, created, updated, cl_ord_id in orders:
        what = f"{SIDES_RU.get(side, side)} {inst_id}"
        owner = _owner(cl_ord_id)
        placed = f"{ord_type}, {_num(sz)} @ {_num(px) if px else 'рынок'}"
        placed += f", владелец {owner}" if owner else ""
        created, updated = _f(created, 3), _f(updated, 3)
        if created is not None:
            events.append(_event(created, "order", "info", f"Ордер выставлен: {what}", placed, "orders"))
        if updated is None:
            continue
        if state == "filled":
            events.append(_event(updated, "fill", "info", f"Ордер исполнен: {what}",
                                 f"{_num(filled)} @ {_num(avg_px)}", "orders"))
        elif state == "partially_filled":
            events.append(_event(updated, "fill", "info", f"Частичное исполнение: {what}",
                                 f"{_num(filled)} из {_num(sz)} @ {_num(avg_px)}", "orders"))
        elif state in ("canceled", "mmp_canceled"):
            events.append(_event(updated, "order", "info", f"Ордер отменён: {what}", placed, "orders"))
    for inst_id, side, px, sz, ts in trades:
        stamp = _f(ts, 3)
        if stamp is not None:
            events.append(_event(stamp, "fill", "info",
                                 f"Исполнение без ордера в базе: {SIDES_RU.get(side, side)} {inst_id}",
                                 f"{_num(sz)} @ {_num(px)}", "trades"))
    return events


def _events_guard(log_path: Path) -> list[dict]:
    """Только отказы guard: время и reason. snippet, tool и команды не читаются в ответ."""
    events = []
    for record in _jsonl_tail(log_path):
        if record.get("decision") != "deny":
            continue
        ts = _parse_ts(record.get("ts"))
        reason = record.get("reason")
        if ts is not None:
            events.append(_event(ts, "guard", "warn", "Guard отклонил вызов",
                                 reason if isinstance(reason, str) else None, "guard.log"))
    return events[-EVENTS_LIMIT:]


def _events_alerts(alerts_path: Path) -> list[dict]:
    """data/alerts.jsonl (src/notify.py): timestamp, level, title, message."""
    events = []
    for record in _jsonl_tail(alerts_path):
        ts = _parse_ts(record.get("timestamp") or record.get("ts"))
        if ts is None:
            continue
        level = str(record.get("level") or "").upper()
        events.append(_event(ts, "alert", SEVERITY_BY_LEVEL.get(level, "info"),
                             record.get("title") or "Алерт", record.get("message"), "alerts.jsonl"))
    return events[-EVENTS_LIMIT:]


def _incident_title(body: str) -> str:
    bold = re.search(r"\*\*(.+?)\*\*", body)
    text = bold.group(1) if bold else body
    return re.sub(r"[*`]", "", text)


def _events_incidents(path: Path) -> list[dict]:
    """Строки ops/incidents.md с датой в первой ячейке (или заголовки «## …» с датой)."""
    text = Path(path).read_text(encoding="utf-8-sig")
    events = []
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith("|"):
            cells = [cell.strip() for cell in stripped.strip("|").split("|")]
            if len(cells) < 2:
                continue
            match, body = _INCIDENT_TS.match(cells[0]), cells[1]
        elif stripped.startswith("## "):
            body = stripped[3:]
            match = _INCIDENT_TS.search(body)
        else:
            continue
        if not match:
            continue
        year, month, day, hour, minute, sign, off_h, off_m = match.groups()
        tz = PROJECT_TZ                       # время журнала — по поясу проекта
        if sign:
            offset = timedelta(hours=int(off_h), minutes=int(off_m))
            tz = timezone(offset if sign == "+" else -offset)
        try:
            ts = datetime(int(year), int(month), int(day), int(hour), int(minute), tzinfo=tz).timestamp()
        except ValueError:
            continue
        head = body[:200]
        severity = ("crit" if "КРИТИЧНО" in head else
                    "info" if head.lower().startswith("info") else "warn")
        events.append(_event(ts, "incident", severity, _incident_title(body), None, "incidents.md"))
    return events[:EVENTS_LIMIT]


def _events_actions(log_path: Path) -> list[dict]:
    """logs/control-panel-actions.jsonl: {ts, action, outcome, via} без токена и вывода."""
    events = []
    for record in _jsonl_tail(log_path):
        ts = _parse_ts(record.get("ts"))
        if ts is None:
            continue
        action = record.get("action")
        action = action if isinstance(action, str) and _LABEL.match(action) else "?"
        outcome = record.get("outcome")
        via = record.get("via")
        severity = ("warn" if outcome == "failed" or
                    (outcome == "ok" and action.startswith(("kill.", "breaker.", "orders."))) else "info")
        events.append(_event(ts, "action", severity,
                             f"Действие пульта: {action} — {ACTION_OUTCOMES.get(outcome, 'неизвестно')}",
                             f"через {via}" if isinstance(via, str) and _LABEL.match(via) else None,
                             "control-panel-actions.jsonl"))
    return events[-EVENTS_LIMIT:]


def _events_pump(journal_path: Path) -> list[dict]:
    """Вход, стоп, выход и ошибки pump-кармана из хвоста data/pump_journal.jsonl."""
    from src.pump_journal import parse_ts as journal_ts
    events = []
    for record in _jsonl_tail(journal_path, PUMP_TAIL_BYTES):
        event = record.get("event")
        if event not in ("entry", "stop", "exit", "error"):
            continue
        moment = journal_ts(record.get("ts"))
        if moment is None:
            continue
        ts = moment.timestamp()
        pair = record.get("pair") or record.get("inst_id") or "?"
        reason = record.get("reason") if isinstance(record.get("reason"), str) else ""
        if event == "entry":
            detail = f"{_num(record.get('size'))} @ {_num(record.get('price'))}, стоп {_num(record.get('stop'))}. {reason}"
            events.append(_event(ts, "order", "info", f"Карман: вход {pair}", detail, "pump_journal.jsonl"))
        elif event == "stop":
            events.append(_event(ts, "order", "info", f"Карман: выставлен стоп {pair}", reason,
                                 "pump_journal.jsonl"))
        elif event == "exit":
            by_stop = record.get("kind") in ("stop", "sl", "stop_loss")
            pnl = _f(record.get("pnl"), 2)
            detail = f"{_num(record.get('size'))} @ {_num(record.get('price'))}"
            detail += f", PnL {pnl:+.2f} USDT" if pnl is not None else ""
            events.append(_event(ts, "stop" if by_stop else "fill", "warn" if by_stop else "info",
                                 f"Карман: {'сработал стоп' if by_stop else 'выход'} {pair}",
                                 f"{detail}. {reason}", "pump_journal.jsonl"))
        else:
            events.append(_event(ts, "alert", "warn", f"Карман: ошибка {pair}", reason, "pump_journal.jsonl"))
    return events[-EVENTS_LIMIT:]


def _events_tasks(tasks: list[dict], ages: dict) -> list[dict]:
    """Смены статуса задач с известным временем (task_ages); время — оценка, см. detail."""
    events = []
    for task in tasks:
        since, basis = ages.get(task.get("id"), (None, None))
        label = TASK_EVENTS.get(task.get("status"))
        if since is None or label is None or task.get("duplicate"):
            continue
        note = "время из заметок задачи" if basis == "notes-date" else "время — оценка по истории git доски"
        events.append(_event(since, "task", label[0], f"{label[1]}: {task['id']}",
                             f"{task.get('title') or ''}; {note}", "board.md"))
    return events


def events_view(root: Path, tasks: list[dict], ages: dict, errors: list[str]) -> tuple[dict, dict]:
    """Единая лента ≤ 100 событий, новые сверху, и статусы источников.

    Отсутствующий файл или таблица — источник просто не прочитан; исключение — имя
    источника в events.errors и тип исключения в общем errors. ok=false, только если не
    прочитан ни один источник.
    """
    root = Path(root)
    data_dir = root / "data"
    readers = (
        ("risk_events", lambda: _events_risk(data_dir / "risk_state.db")),
        ("orders", lambda: _events_orders(data_dir / "bot_state.db")),
        ("guard", lambda: _events_guard(data_dir / "guard.log")),
        ("alerts", lambda: _events_alerts(data_dir / "alerts.jsonl")),
        ("incidents", lambda: _events_incidents(root / "ops" / "incidents.md")),
        ("actions", lambda: _events_actions(root / "logs" / "control-panel-actions.jsonl")),
        ("pump", lambda: _events_pump(data_dir / "pump_journal.jsonl")),
    )
    items: list[dict] = []
    read: list[str] = []
    failed: list[str] = []
    states: dict[str, str] = {}
    for name, reader in readers:
        state, found = _source(f"events.{name}", reader, errors)
        states[name] = state
        if state == "ok":
            read.append(name)
            items.extend(found)
        elif state == "error":
            failed.append(name)
    if tasks:
        read.append("board")
        items.extend(_events_tasks(tasks, ages))
    items.sort(key=lambda item: item["_ts"], reverse=True)
    items = items[:EVENTS_LIMIT]
    for item in items:
        item.pop("_ts")
    return {"ok": bool(read), "sources": read, "errors": failed, "items": items}, states


# --- Возраст задач ---

_ROW_ID = re.compile(r"^[A-Z0-9][A-Z0-9_-]{0,79}$")
_CLAIM_TIME = re.compile(r"(?<![\d:])(\d{1,2}):(\d{2})\s*$")
_NOTE_DATETIME = re.compile(
    r"(?<![\d.])(\d{1,2})\.(\d{1,2})(?:\.(\d{4}))?,?\s+(?:в\s+)?(\d{1,2}):(\d{2})(?![\d:])"
    r"|(?<!\d)(\d{4})-(\d{2})-(\d{2})[ T](\d{1,2}):(\d{2})(?![\d:])")
_TIME_ONLY = re.compile(r"(\d{1,2}):(\d{2})(?![\d:])")
_DONE_MARK = re.compile(r"готово", re.I)
_AMBIGUOUS = object()
_ABSENT = object()


def _status_cells(lines) -> dict:
    """ID → нормализованная ячейка статуса по строкам таблиц доски (терпимо к старым версиям)."""
    found: dict = {}
    for line in lines:
        if not line.lstrip().startswith("|"):
            continue
        cells = [cell.strip() for cell in line.strip().strip("|").split("|")]
        if len(cells) < 7:
            continue
        task_id, status = cells[0], " ".join(cells[3].split())
        if not _ROW_ID.match(task_id) or status.split(" ", 1)[0] not in STATUSES:
            continue
        previous = found.get(task_id, _ABSENT)
        if previous is _ABSENT:
            found[task_id] = status
        elif previous is not _AMBIGUOUS and previous != status:
            found[task_id] = _AMBIGUOUS
    return found


def _parse_board_log(text: str) -> list[tuple[float, dict, dict]]:
    """Вывод `git log -p -U0` доски → [(время коммита, строки «до», строки «после»)], новые первыми."""
    commits: list[dict] = []
    for line in text.splitlines():
        if line.startswith("@@COMMIT@@"):
            stamp = _f(line[len("@@COMMIT@@"):].strip(), 0)
            commits.append({"ts": stamp, "-": [], "+": []})
        elif commits and line[:1] in "+-" and line[1:].lstrip().startswith("|"):
            commits[-1][line[0]].append(line[1:])
    return [(c["ts"], _status_cells(c["-"]), _status_cells(c["+"])) for c in commits if c["ts"] is not None]


def board_history(root: Path, limit: int = GIT_LOG_LIMIT) -> dict | None:
    """Статусы доски в HEAD и диффы последних `limit` коммитов ops/board.md; git нет — None.

    Только чтение: `git show` и `git log` с GIT_OPTIONAL_LOCKS=0 (индекс не обновляется).
    """
    if not shutil.which("git"):
        return None
    env = dict(os.environ, GIT_OPTIONAL_LOCKS="0", GIT_TERMINAL_PROMPT="0")

    def git(*args):
        result = subprocess.run(["git", "-C", str(root), *args], capture_output=True, text=True,
                                encoding="utf-8", errors="replace", timeout=GIT_TIMEOUT_S, env=env,
                                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        return result.stdout if result.returncode == 0 else None
    try:
        head = git("show", "--no-color", "HEAD:./ops/board.md")
        if head is None:
            return None
        log = git("log", "--first-parent", "--diff-merges=first-parent", f"-n{int(limit)}", "-p", "-U0",
                  "--no-color", "--no-ext-diff", "--no-textconv", "--no-renames",
                  "--format=@@COMMIT@@%ct", "--", "ops/board.md")
    except (OSError, subprocess.TimeoutExpired):
        return None
    if log is None:
        return None
    return {"head": _status_cells(head.splitlines()), "commits": _parse_board_log(log)}


def status_windows(current: dict, history: dict, edited_at: float) -> dict:
    """ID → (lo, hi): текущий статус появился позже lo (None — неизвестно) и не позже hi.

    Идём от HEAD к старым коммитам доски, пока ячейка статуса совпадает с текущей;
    коммиты, где правились только заметки, пропускаются. Отличие рабочей копии от
    HEAD — смена после последнего коммита доски, hi = время записи файла. Расхождение
    истории (дубли, удалённая строка) — None: возраст неизвестен.
    """
    head, commits = history["head"], history["commits"]
    newest = commits[0][0] if commits else None
    windows: dict = {}
    pending: dict = {}
    for task_id, cell in current.items():
        before = head.get(task_id, _ABSENT)
        if before is _AMBIGUOUS:
            windows[task_id] = None
        elif before != cell:
            windows[task_id] = (newest, max(newest, edited_at)) if newest is not None else None
        else:
            pending[task_id] = cell
    for index, (stamp, minus, plus) in enumerate(commits):
        if not pending:
            break
        older = commits[index + 1][0] if index + 1 < len(commits) else None
        for task_id in [key for key in pending if key in minus or key in plus]:
            cell = pending.pop(task_id)
            after, before = plus.get(task_id, _ABSENT), minus.get(task_id, _ABSENT)
            if after is _AMBIGUOUS or before is _AMBIGUOUS or after != cell:
                windows[task_id] = None
            elif before == cell:
                pending[task_id] = cell
            else:
                windows[task_id] = (older, stamp)
    for task_id in pending:                  # статус старше просмотренной истории
        windows[task_id] = (None, commits[-1][0]) if commits else None
    return windows


def _times_in_window(hour: int, minute: int, lo: float, hi: float) -> list[float]:
    """Моменты ЧЧ:ММ (пояс проекта) в интервале (lo, hi]."""
    if not (0 <= hour <= 23 and 0 <= minute <= 59) or hi - lo > 3 * 86400:
        return []
    day = datetime.fromtimestamp(lo, PROJECT_TZ).date() - timedelta(days=1)
    last = datetime.fromtimestamp(hi, PROJECT_TZ).date() + timedelta(days=1)
    found = []
    while day <= last:
        ts = datetime(day.year, day.month, day.day, hour, minute, tzinfo=PROJECT_TZ).timestamp()
        if lo < ts <= hi:
            found.append(ts)
        day += timedelta(days=1)
    return found


def _datetime_at(match: re.Match, now: float) -> float | None:
    """Дата-время из совпадения _NOTE_DATETIME; ДД.ММ без года — ближайший год не позже now."""
    groups = match.groups()
    try:
        if groups[0] is not None:
            day, month, year, hour, minute = groups[:5]
            years = [int(year)] if year else [datetime.fromtimestamp(now, PROJECT_TZ).year - k for k in (0, 1)]
            for candidate in years:
                ts = datetime(candidate, int(month), int(day), int(hour), int(minute), tzinfo=PROJECT_TZ).timestamp()
                if ts <= now + AGE_SLACK_S or year:
                    return ts
            return None
        year, month, day, hour, minute = groups[5:]
        return datetime(int(year), int(month), int(day), int(hour), int(minute), tzinfo=PROJECT_TZ).timestamp()
    except ValueError:
        return None


def _done_times(notes: str, now: float, lower: float | None, upper: float) -> list[float]:
    """Моменты из отметок «Готово ДД.ММ ЧЧ:ММ» / «Готово ЧЧ:ММ» (время — только внутри окна < 24 ч)."""
    found = []
    for mark in _DONE_MARK.finditer(notes):
        tail = notes[mark.end():mark.end() + 40].lstrip(" *:—-")
        full = _NOTE_DATETIME.match(tail)
        if full:
            ts = _datetime_at(full, now)
            if ts is not None:
                found.append(ts)
            continue
        short = _TIME_ONLY.match(tail)
        if short and lower is not None and upper - lower < 86400:
            candidates = _times_in_window(int(short.group(1)), int(short.group(2)), lower, upper)
            if len(candidates) == 1:
                found.append(candidates[0])
    return found


def since_for(cell: str, notes: str, window, now: float) -> tuple[float | None, str | None]:
    """Лучшая оценка момента входа в текущий статус: (unix, "git-history"|"notes-date") или (None, None).

    1. in-progress с ЧЧ:ММ в ячейке (claim): единственный такой момент в окне git → git-history;
       иначе дата «ДД.ММ ЧЧ:ММ» с тем же временем из заметок → notes-date.
    2. done: «Готово ДД.ММ ЧЧ:ММ» в заметках, совместимое с окном git → notes-date.
    3. Окно git не шире 12 ч → его верхняя граница (момент не позже) → git-history.
    Иначе (None, None): возраст не выдумывается.
    """
    status = cell.split(" ", 1)[0] if cell else ""
    lo, hi = window if window else (None, None)
    lower = lo - AGE_SLACK_S if lo is not None else None
    upper = (hi if hi is not None else now) + AGE_SLACK_S

    def inside(ts: float) -> bool:
        return ts <= min(upper, now + AGE_SLACK_S) and (lower is None or ts > lower)

    if status == "in-progress":
        claim = _CLAIM_TIME.search(cell)
        if claim:
            hour, minute = int(claim.group(1)), int(claim.group(2))
            if lo is not None and hi is not None and hi - lo < 86400:
                found = _times_in_window(hour, minute, lower, upper)
                if len(found) == 1 and found[0] <= now + AGE_SLACK_S:
                    return found[0], "git-history"
            dated = [ts for match in _NOTE_DATETIME.finditer(notes or "")
                     if (ts := _datetime_at(match, now)) is not None and inside(ts)
                     and datetime.fromtimestamp(ts, PROJECT_TZ).strftime("%H:%M") == f"{hour:02d}:{minute:02d}"]
            if dated:
                return max(dated), "notes-date"
    elif status == "done":
        done = [ts for ts in _done_times(notes or "", now, lower, upper) if inside(ts)]
        if done:
            return max(done), "notes-date"
    if lo is not None and hi is not None and hi - lo <= AGE_MAX_WINDOW_S and hi <= now + AGE_SLACK_S:
        return hi, "git-history"
    return None, None


def task_ages(root: Path, tasks: list[dict], now: float, history=_ABSENT) -> dict:
    """ID → (unix, basis): когда задача вошла в текущий статус (since_for).

    Источники: ячейка статуса и заметки текущей доски, история git ops/board.md
    (один `git show` и один `git log -p -U0` не больше GIT_LOG_LIMIT коммитов, таймаут
    GIT_TIMEOUT_S). Коммиты доски редкие, поэтому окно git само по себе даёт время
    только с точностью до 12 ч; точнее — время claim и отметки «Готово ДД.ММ ЧЧ:ММ».
    Дубли ID и строки, сдвинувшиеся между чтениями доски, — (None, None).
    """
    path = Path(root) / "ops" / "board.md"
    text = path.read_text(encoding="utf-8-sig")
    edited_at = min(path.stat().st_mtime, now)
    current: dict = {}
    for task_id, variants in read_rows(text).items():
        if len(variants) != 1:
            continue
        row = variants[0]
        headings = [cell.strip().lower() for cell in row["header"].strip().strip("|").split("|")]
        cells = [cell.strip() for cell in row["raw"].strip().strip("|").split("|")]
        detail = dict(zip(headings, cells))
        current[task_id] = (" ".join(detail.get("статус", "").split()), detail.get("заметки", ""), row["line"])
    if history is _ABSENT:
        history = board_history(Path(root))
    windows = status_windows({key: value[0] for key, value in current.items()}, history, edited_at) \
        if history else {}
    ages: dict = {}
    for task in tasks:
        entry = current.get(task.get("id"))
        if entry is None or task.get("duplicate") or entry[2] != task.get("line"):
            continue
        ages[task["id"]] = since_for(entry[0], entry[1], windows.get(task["id"]), now)
    return ages


# --- Управление ---

def _load_actions():
    """Модуль действий пульта (MORPHY-UI-CONTROLS) или None, если его ещё нет."""
    if importlib.util.find_spec("src.morphy_actions") is None:
        return None
    return importlib.import_module("src.morphy_actions")


def controls_view(root: Path, risk, engine, flags) -> dict:
    """Описание действий пульта; доступность считает src.morphy_actions по флагам риска и движка."""
    module = _load_actions()
    if module is None:
        return dict(CONTROLS_FALLBACK, actions=[])
    described = module.describe(root, risk=risk, engine=engine, flags=flags, mode="demo")
    if not isinstance(described, dict):
        raise TypeError("describe() вернул не объект")
    return described


def morphy_status() -> dict:
    """Публичные флаги настройки; профили и credentials не читаются."""
    try:
        with urllib.request.urlopen("http://127.0.0.1:7480/api/onboard/status", timeout=1) as response:
            return pick(json.load(response), ("portalConfigured", "provider", "model", "tunnelMode")) or {}
    except (OSError, ValueError):
        return {}


def collect(root: Path = ROOT, *, now: float | None = None) -> dict:
    root = Path(root)
    now = time.time() if now is None else now
    errors: list[str] = []

    def part(name, fn, fallback):
        try:
            return fn()
        except Exception as exc:
            # Содержимое исключения может включать путь/данные источника.
            errors.append(f"{name}: {type(exc).__name__}")
            return fallback

    captured: dict = {}

    def pump_reader(journal, pocket, moment):
        # Полный отчёт кармана нужен позициям (цена и время входа); снимок хранит урезанный.
        captured["pump"] = read_pump(journal, pocket, moment)
        return captured["pump"]

    board = part("board", lambda: _board(root), {"tasks": [], "counts": {}, "ready_ids": [], "duplicates": []})
    snapshot = part("snapshot", lambda: build_snapshot(project_root=root, data_dir=root / "data",
                         pocket_path=root / "pump-pocket.json", journal_path=root / "data/pump_journal.jsonl",
                         pump_reader=pump_reader), {})
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
                "equity_age_s", "entries_today", "max_entries_per_day", "portfolio_heat_pct", "max_heat_pct",
                "day_start_equity"))
    engine = pick(snapshot.get("engine"), ("running", "uptime_h", "reconciles", "divergences",
                "errors_exchange", "errors_internal", "stats_age_s", "ws_public_reconnects", "ws_private_reconnects"))
    flags = pick(snapshot.get("flags"), ("KILL", "STOP_ENGINE"))
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

    # Капитал: одна выборка equity_curve на диапазоны, спарклайны и прибыль за всё время.
    data_dir = root / "data"
    curve_state, points = _source("equity", lambda: read_curve(data_dir / "bot_state.db"), errors)
    points = points or []
    lifetime = lifetime_from_points(points)
    day_start_ts = part("risk_day", lambda: risk_day_start(now), None)
    equity = part("equity_view", lambda: equity_view(points, now, risk, lifetime),
                  {"current": None, "baseline_usdt": None, "hwm": None,
                   "ranges": {key: [] for key in EQUITY_RANGES}, "range_hours": dict(EQUITY_RANGES)})
    spark, spark_note = part("spark", lambda: spark_view(points, now, risk, day_start_ts),
                             ({"equity": [], "day_pnl": [], "drawdown_pct": [], "lifetime_net": []}, None))

    if isinstance(captured.get("pump"), dict):
        pump_list, pump_state = (captured["pump"].get("positions") or {}).get("list") or [], "ok"
    elif isinstance(snapshot.get("pump"), dict):
        pump_list, pump_state = snapshot["pump"].get("open_positions") or [], "ok"
    else:
        pump_list, pump_state = [], "missing"
    positions = part("positions", lambda: positions_view(data_dir, now, pump_list, pump_state, errors),
                     {"ok": False, "count": 0, "total_upl": None, "note": POSITIONS_NOTE, "items": [],
                      "sources": {}, "age_s": None})
    positions_age = positions.pop("age_s", None)

    ages = part("ages", lambda: task_ages(root, board["tasks"], now), {}) if board["tasks"] else {}
    for task in board["tasks"]:
        since, basis = ages.get(task["id"], (None, None)) if not task.get("duplicate") else (None, None)
        task["since"] = _iso(since)
        task["age_days"] = round(max(0.0, now - since) / 86400, 2) if since is not None else None
        task["age_basis"] = basis
    events, event_states = part("events", lambda: events_view(root, board["tasks"], ages, errors),
                                ({"ok": False, "sources": [], "errors": ["events"], "items": []}, {}))
    controls = part("controls", lambda: controls_view(root, risk, engine, flags),
                    dict(CONTROLS_FALLBACK, actions=[]))
    sources = {
        "risk": {"ok": risk is not None, "age_s": (risk or {}).get("equity_age_s")},
        "engine": {"ok": engine is not None, "age_s": (engine or {}).get("stats_age_s")},
        "equity": {"ok": curve_state == "ok", "points": len(points),
                   "age_s": _f(now - points[-1][0], 1) if points else None},
        "positions": {"ok": positions["ok"], "age_s": positions_age},
        "events": {"ok": events["ok"]},
        "guard": {"ok": event_states.get("guard") == "ok"},
    }
    result = {
        "schema_version": 1, "generated_at": datetime.now(timezone.utc).isoformat(),
        "mode": "demo", "data_quality": "local-snapshot", "refresh_seconds": 15,
        "board": board, "claims": claims, "runtimes": runtimes, "queue": queue,
        "engine": engine, "risk": risk, "flags": flags,
        "pump": pick(snapshot.get("pump"), ("entry_allowed", "budget_total", "budget_free", "in_positions",
                    "day_pnl", "day_limit", "drawdown", "drawdown_limit", "open_positions")),
        "equity_history": snapshot.get("equity_history") or [],
        "lifetime": lifetime,
        "equity": equity, "spark": spark, "spark_note": spark_note,
        "positions": positions, "events": events, "sources": sources, "controls": controls,
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
