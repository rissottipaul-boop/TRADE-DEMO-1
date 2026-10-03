"""П. 38 — объяснение метрик панели: equity, HWM, baseline и просадка по формулам проекта.

Ничего не пересчитывается «своей» формулой: значения берутся из тех же источников и
функций, что у панелей (Morphy-экран src/morphy_project.py, Obsidian, `python -m src.ops status`):
  equity     risk_kv.equity — последний update_equity риск-ядра (totalEq биржи, движок раз в 300 с);
             без него — последняя точка equity_curve (data/bot_state.db).
  HWM        risk_kv.hwm — максимум equity за всё время, только растёт; ведёт update_equity.
             Пополнение demo-счёта тоже поднимает HWM (внешние потоки риск-ядро не различает).
  просадка   (equity / HWM − 1) × 100 — карточка риска и глобальный breaker (−15%).
             Спарклайн 7 сут.: (1 − eq / пик) × 100, пик — скользящий максимум точек
             equity_curve с начала истории; может отличаться от карточки (дискретные точки).
  baseline   «внесённые деньги» = equity по кривой − накопленная торговая прибыль
             (net_series: приращения соседних точек, скачок > 20% — внешний поток, не прибыль).
  день       equity − day_start_equity (equity на 00:00 UTC); дневной лимит −6% от неё.
Обе базы открываются только на чтение; функции кривой импортируются из src.morphy_project.
"""
from __future__ import annotations

import time
from contextlib import closing
from pathlib import Path
from typing import Optional

from src import risk
from src.analytics.common import BOT_DB, RISK_DB, connect_ro, fmt, iso_s


def _kv(db: Path) -> dict:
    with closing(connect_ro(db)) as conn:
        return {r["key"]: float(r["value"]) for r in conn.execute("SELECT key, value FROM risk_kv")
                if _is_float(r["value"])}


def _is_float(v) -> bool:
    try:
        float(v)
        return True
    except (TypeError, ValueError):
        return False


def explain(risk_db: Path = RISK_DB, bot_db: Path = BOT_DB, now: Optional[float] = None) -> dict:
    from src.morphy_project import (EXTERNAL_FLOW_RATIO, SPARK_DRAWDOWN_HOURS,
                                    lifetime_from_points, read_curve)
    now = time.time() if now is None else now
    notes: list[str] = []
    try:
        kv = _kv(risk_db)
    except FileNotFoundError:
        kv = {}
        notes.append(f"{risk_db} нет — значения риск-ядра недоступны")
    try:
        points = read_curve(Path(bot_db))
    except FileNotFoundError:
        points = []
        notes.append(f"{bot_db} нет — кривой equity нет")
    equity = kv.get("equity")
    eq_src = "risk_kv.equity (update_equity)"
    if equity is None and points:
        equity, eq_src = points[-1][1], "последняя точка equity_curve"
    hwm = kv.get("hwm")
    day0 = kv.get("day_start_equity")
    upd = kv.get("equity_updated_at")
    dd = (equity / hwm - 1) * 100 if equity is not None and hwm else None
    global_floor = hwm * (1 - risk.GLOBAL_DD_LIMIT_PCT / 100) if hwm else None
    daily_floor = day0 * (1 - risk.DAILY_LOSS_LIMIT_PCT / 100) if day0 else None
    life = lifetime_from_points(points, EXTERNAL_FLOW_RATIO)
    curve_peak = max((eq for _, eq in points), default=None)
    cutoff = now - SPARK_DRAWDOWN_HOURS * 3600
    peak, spark_now, spark_max = None, None, 0.0
    for ts, eq in points:
        peak = eq if peak is None or eq > peak else peak
        if ts >= cutoff and peak > 0:
            spark_now = max(0.0, (1 - eq / peak) * 100)
            spark_max = max(spark_max, spark_now)
    if hwm and curve_peak and abs(hwm - curve_peak) / hwm > 0.001:
        notes.append(f"HWM риск-ядра {fmt(hwm)} ≠ пик кривой {fmt(curve_peak)}: кривая пишется "
                     "дискретно (equity_curve), а HWM обновляется на каждом update_equity — "
                     "поэтому просадка в карточке и в спарклайне может различаться")
    if upd and now - upd > risk.EQUITY_MAX_AGE_S:
        notes.append(f"equity устарела ({now - upd:.0f} с > {risk.EQUITY_MAX_AGE_S} с): риск-ядро "
                     "запрещает входы, цифры панели — прошлое состояние")
    if life.get("available") and life.get("external_flows"):
        notes.append(f"внешних потоков: {life['external_flows']} на {fmt(life['external_usdt'])} USDT — "
                     "они подняли и HWM, и baseline; в прибыль не входят")
    metrics = [
        {"key": "equity", "name": "Equity (капитал)", "value": equity, "source": eq_src,
         "formula": "totalEq биржи по рынку (PnL открытых позиций уже внутри)",
         "detail": f"обновлено {iso_s(upd)}" if upd else None},
        {"key": "hwm", "name": "HWM (high-water mark)", "value": hwm, "source": "risk_kv.hwm",
         "formula": "HWM = max(HWM, equity) на каждом update_equity; не снижается, сброс не предусмотрен",
         "detail": f"пик точек equity_curve {fmt(curve_peak)}"},
        {"key": "drawdown_pct", "name": "Просадка от HWM, %", "value": dd,
         "source": "risk.status() / карточка риска",
         "formula": "(equity / HWM − 1) × 100",
         "detail": (f"глобальный breaker при equity ≤ HWM × {1 - risk.GLOBAL_DD_LIMIT_PCT / 100:.2f} = "
                    f"{fmt(global_floor)}; запас {fmt(equity - global_floor if equity and global_floor else None)} USDT")},
        {"key": "spark_drawdown_pct", "name": "Просадка на спарклайне (7 сут.), %", "value": spark_now,
         "source": "equity_curve, spark_view",
         "formula": "(1 − eq / скользящий пик кривой) × 100, точки за 168 ч",
         "detail": f"максимум за окно {fmt(spark_max, 3)}%"},
        {"key": "baseline_usdt", "name": "Baseline (внесённые деньги)", "value": life.get("base_usdt"),
         "source": "lifetime_from_points(equity_curve)",
         "formula": f"последняя точка кривой − Σ приращений без скачков > {EXTERNAL_FLOW_RATIO:.0%}",
         "detail": f"прибыль за всё время {fmt(life.get('net_usdt'))} USDT ({fmt(life.get('net_pct'))}%) "
                   f"с {life.get('since', '—')}"},
        {"key": "day_result", "name": "Результат риск-суток, USDT",
         "value": (equity - day0) if equity is not None and day0 else None,
         "source": "equity − risk_kv.day_start_equity",
         "formula": "equity − equity на 00:00 UTC",
         "detail": f"дневной breaker при equity ≤ {fmt(daily_floor)} (−{risk.DAILY_LOSS_LIMIT_PCT:g}%)"},
    ]
    return {"now": now, "metrics": metrics, "notes": notes, "points": len(points),
            "caveat": "Снимок локального состояния ≠ сверка с биржей (AGENTS.md §3)."}


def render(r: dict) -> str:
    lines = ["# Метрики панели: что означают и как считаются", "", f"> {r['caveat']}", ""]
    for m in r["metrics"]:
        lines += [f"## {m['name']}: {fmt(m['value'])}", "",
                  f"- **Формула:** {m['formula']}", f"- **Источник:** {m['source']}"]
        if m.get("detail"):
            lines.append(f"- {m['detail']}")
        lines.append("")
    if r["notes"]:
        lines += ["## Почему цифры могут расходиться", ""] + [f"- {n}" for n in r["notes"]]
    return "\n".join(lines) + "\n"
