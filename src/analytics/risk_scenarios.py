"""П. 37 — сценарный анализ риска: последствия заданных сценариев, лимиты не меняются.

Расчёт идёт тем же кодом риск-ядра, что в бою: src.backtest.risk_sim (_MemoryRiskCore —
_RiskCore без файла, модельные часы). Состояние копируется из data/risk_state.db,
открытого только на чтение, в память; реальная база, лимиты и breaker'ы не меняются.
Поэтому события сценария (daily_limit, global_breaker, instrument_blocked, system_pause) и
ответ check_entry_allowed после него — ровно то, что сделало бы риск-ядро.

Сценарий — список шагов, каждый сценарий стартует от текущего состояния:
  {"name": "...", "steps": [
     {"type": "equity_shock", "pct": -8},                       # equity × (1 + pct/100)
     {"type": "price_shock", "pct": -20, "exposure_usdt": null}, # экспозиция × pct; null —
                                                                 # потолок лимитов: 15% × 2 позиции
     {"type": "loss_streak", "n": 5, "risk_pct": 1.0, "inst": "BTC-USDT", "spacing_min": 60},
     {"type": "gap_through_stop", "risk_pct": 1.0, "mult": 3, "inst": "BTC-USDT"},
     {"type": "next_day"}]}                                      # 00:00 UTC следующих суток
Без сценариев — стандартный набор (DEFAULT_SCENARIOS).
"""
from __future__ import annotations

import time
from contextlib import closing
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

from src import risk
from src.analytics.common import RISK_DB, connect_ro, fmt, md_table
from src.backtest.risk_sim import SimRisk

STEP_TYPES = ("equity_shock", "price_shock", "loss_streak", "gap_through_stop", "next_day")
CONSEQUENCES = {
    "daily_limit": "дневной breaker: новые входы запрещены до 00:00 UTC (автосброс); выходы разрешены",
    "global_breaker": "глобальный breaker −15% от HWM: входы запрещены до ручного сброса человеком "
                      "(`python -m src.ops reset …` — только человек, задача needs-user)",
    "instrument_blocked": f"инструмент заблокирован на {risk.INST_BLOCK_HOURS} ч после "
                          f"{risk.INST_LOSS_STREAK_BLOCK} убытков подряд",
    "system_pause": f"системная пауза {risk.SYS_PAUSE_HOURS} ч после {risk.SYS_LOSS_STREAK_PAUSE} "
                    "убытков подряд по системе",
}
DEFAULT_SCENARIOS = [
    {"name": "Просадка equity −3%", "steps": [{"type": "equity_shock", "pct": -3}]},
    {"name": "Просадка до дневного лимита −6% от equity на 00:00 UTC",
     "steps": [{"type": "equity_shock", "pct": "to_daily"}, {"type": "next_day"}]},
    {"name": "Обвал −20% при максимальной экспозиции по лимитам",
     "steps": [{"type": "price_shock", "pct": -20}]},
    {"name": "Серия из 5 стопов по 1%", "steps": [{"type": "loss_streak", "n": 5, "risk_pct": 1.0}]},
    {"name": "Гэп через стоп ×3 (риск 1%)", "steps": [{"type": "gap_through_stop", "risk_pct": 1.0, "mult": 3}]},
    {"name": "Просадка до глобального breaker", "steps": [{"type": "equity_shock", "pct": "to_global"}]},
]


class ScenarioError(ValueError):
    pass


def read_state(db: Path = RISK_DB) -> dict:
    """Снимок риск-ядра только на чтение (без rollover и записи, в отличие от risk.status())."""
    with closing(connect_ro(db)) as conn:
        kv = {r["key"]: r["value"] for r in conn.execute("SELECT key, value FROM risk_kv")}
        inst = [dict(r) for r in conn.execute("SELECT * FROM risk_instruments")]
        open_risk = [dict(r) for r in conn.execute("SELECT * FROM risk_open_risk")]
        try:
            sizes = [dict(r) for r in conn.execute("SELECT * FROM risk_position_size")]
        except Exception:
            sizes = []
    return {"kv": kv, "instruments": inst, "open_risk": open_risk, "position_size": sizes}


def _f(kv: dict, key: str) -> float:
    try:
        return float(kv.get(key, 0) or 0)
    except ValueError:
        return 0.0


def snapshot(state: dict, now: float) -> dict:
    kv = state["kv"]
    eq, hwm, day0 = _f(kv, "equity"), _f(kv, "hwm"), _f(kv, "day_start_equity")
    upd = _f(kv, "equity_updated_at")
    daily_floor = day0 * (1 - risk.DAILY_LOSS_LIMIT_PCT / 100) if day0 > 0 else None
    global_floor = hwm * (1 - risk.GLOBAL_DD_LIMIT_PCT / 100) if hwm > 0 else None
    return {"equity": eq, "hwm": hwm, "day_start_equity": day0, "day_pnl": _f(kv, "day_pnl"),
            "drawdown_pct": (eq / hwm - 1) * 100 if hwm > 0 else 0.0,
            "equity_age_s": now - upd if upd else None,
            "equity_stale": not upd or abs(now - upd) > risk.EQUITY_MAX_AGE_S,
            "daily_floor": daily_floor, "to_daily_usdt": eq - daily_floor if daily_floor else None,
            "global_floor": global_floor, "to_global_usdt": eq - global_floor if global_floor else None,
            "global_loss_streak": int(_f(kv, "global_loss_streak")),
            "entries_today": int(_f(kv, "entries_today")),
            "breakers": {k: bool(_f(kv, k)) for k in ("daily_breaker", "global_breaker", "kill_active")},
            "open_risk": state["open_risk"]}


def _seed(sim: SimRisk, state: dict, now: float) -> None:
    with sim.core._conn() as conn:
        for k, v in state["kv"].items():
            conn.execute("INSERT OR REPLACE INTO risk_kv VALUES (?, ?)", (k, str(v)))
        for r in state["instruments"]:
            conn.execute("INSERT OR REPLACE INTO risk_instruments (inst_id, loss_streak, blocked_until) "
                         "VALUES (?, ?, ?)", (r["inst_id"], r.get("loss_streak") or 0, r.get("blocked_until")))
        for r in state["open_risk"]:
            conn.execute("INSERT OR REPLACE INTO risk_open_risk VALUES (?, ?, ?, ?)",
                         (r["inst_id"], r["side"], r["risk_pct"], r["opened_at"]))
    sim.now = now


def run_scenario(scn: dict, state: dict, now: Optional[float] = None) -> dict:
    now = time.time() if now is None else now
    steps = scn.get("steps") or []
    if not steps:
        raise ScenarioError(f"сценарий {scn.get('name')!r}: нет шагов")
    sim = SimRisk("BTC-USDT")
    log: list[dict] = []
    try:
        _seed(sim, state, now)
        with sim.activate():
            core = sim.core
            for st in steps:
                kind = st.get("type")
                if kind not in STEP_TYPES:
                    raise ScenarioError(f"шаг {kind!r}: доступно {', '.join(STEP_TYPES)}")
                eq = core._get("equity")
                events: list[str] = []
                if kind == "equity_shock":
                    pct = st.get("pct")
                    if pct in ("to_global", "to_daily"):
                        if pct == "to_global":
                            target = core._get("hwm") * (1 - risk.GLOBAL_DD_LIMIT_PCT / 100) - 0.01
                        else:
                            target = core._get("day_start_equity") * (1 - risk.DAILY_LOSS_LIMIT_PCT / 100) - 0.01
                        pct = (target / eq - 1) * 100 if eq > 0 else 0.0
                    new = eq * (1 + float(pct) / 100)
                    events += core.update_equity(new)
                    detail = f"equity {fmt(eq)} → {fmt(new)} ({float(pct):+.2f}%)"
                elif kind == "price_shock":
                    expo = st.get("exposure_usdt")
                    note = ""
                    if expo is None:
                        expo = eq * risk.MAX_POSITION_PCT * risk.MAX_OPEN_POSITIONS / 100
                        note = (f" (экспозиция — потолок лимитов {risk.MAX_POSITION_PCT:g}% × "
                                f"{risk.MAX_OPEN_POSITIONS} позиции)")
                    delta = float(expo) * float(st["pct"]) / 100
                    events += core.update_equity(eq + delta)
                    detail = f"экспозиция {fmt(expo)} × {st['pct']}% = {fmt(delta)} USDT{note}"
                elif kind in ("loss_streak", "gap_through_stop"):
                    n = int(st.get("n", 1)) if kind == "loss_streak" else 1
                    rp = float(st.get("risk_pct", risk.DEFAULT_RISK_PCT))
                    mult = float(st.get("mult", 1)) if kind == "gap_through_stop" else 1.0
                    inst = st.get("inst", "BTC-USDT")
                    spacing = float(st.get("spacing_min", 60)) * 60
                    total = 0.0
                    for k in range(n):
                        if k:
                            sim.now += spacing        # следующий стоп через spacing_min
                        e0 = core._get("equity")
                        pnl = -e0 * rp * mult / 100
                        total += pnl
                        events += core.record_pnl(inst, pnl, datetime.fromtimestamp(sim.now, tz=timezone.utc))
                        events += core.update_equity(e0 + pnl)
                    detail = (f"{n} убыт. по {rp:g}%" + (f" × {mult:g} (гэп)" if mult != 1 else "") +
                              f" на {inst}: {fmt(total)} USDT")
                else:
                    day = datetime.fromtimestamp(sim.now, tz=timezone.utc).date() + timedelta(days=1)
                    sim.now = datetime(day.year, day.month, day.day, tzinfo=timezone.utc).timestamp() + 1
                    events += core.update_equity(core._get("equity"))
                    detail = f"переход на {day.isoformat()} 00:00 UTC (дневной rollover)"
                step_ok, step_reason = core.check_entry_allowed("BTC-USDT", "buy")
                log.append({"step": kind, "detail": detail, "equity": core._get("equity"),
                            "events": events, "entry_allowed": step_ok,
                            "entry_reason": "" if step_ok else step_reason})
            ok, reason = core.check_entry_allowed("BTC-USDT", "buy")
            st_after = core.status()
    finally:
        sim.close()
    all_events = [e for s in log for e in s["events"]]
    return {"name": scn.get("name") or "сценарий", "steps": log,
            "final": {"equity": st_after["equity"], "hwm": st_after["hwm"],
                      "drawdown_pct": st_after["drawdown_pct"], "day_pnl": st_after["day_pnl"],
                      "daily_breaker": st_after["daily_breaker"],
                      "global_breaker": st_after["global_breaker"],
                      "system_pause_until": st_after["system_pause_until"],
                      "blocked": [r["inst_id"] for r in st_after["instruments"]
                                  if (r.get("blocked_until") or 0) > sim.now]},
            "entry_allowed": ok, "entry_reason": reason,
            "consequences": [CONSEQUENCES[e] for e in dict.fromkeys(all_events) if e in CONSEQUENCES]}


def analyze(scenarios: Optional[list[dict]] = None, db: Path = RISK_DB,
            now: Optional[float] = None) -> dict:
    now = time.time() if now is None else now
    state = read_state(db)
    return {"source": str(db), "now": now, "limits": {
                "daily_loss_limit_pct": risk.DAILY_LOSS_LIMIT_PCT,
                "global_dd_limit_pct": risk.GLOBAL_DD_LIMIT_PCT,
                "inst_loss_streak_block": risk.INST_LOSS_STREAK_BLOCK,
                "sys_loss_streak_pause": risk.SYS_LOSS_STREAK_PAUSE,
                "max_position_pct": risk.MAX_POSITION_PCT,
                "max_open_positions": risk.MAX_OPEN_POSITIONS},
            "snapshot": snapshot(state, now),
            "results": [run_scenario(s, state, now) for s in (scenarios or DEFAULT_SCENARIOS)],
            "note": "Расчёт на копии состояния в памяти. Лимиты, breaker'ы и data/risk_state.db не "
                    "менялись; ослабление лимитов — только решение человека (AGENTS.md §2)."}


def render(r: dict) -> str:
    s = r["snapshot"]
    lim = r["limits"]
    lines = ["# Сценарный анализ риска", "", f"> {r['note']}", "",
             "## Текущее состояние (data/risk_state.db, только чтение)", "",
             md_table(["Показатель", "Значение"], [
                 ["Equity, USDT", fmt(s["equity"])], ["HWM, USDT", fmt(s["hwm"])],
                 ["Просадка от HWM, %", fmt(s["drawdown_pct"])],
                 ["Equity на 00:00 UTC, USDT", fmt(s["day_start_equity"])],
                 [f"До дневного лимита −{lim['daily_loss_limit_pct']:g}%, USDT", fmt(s["to_daily_usdt"])],
                 [f"До глобального breaker −{lim['global_dd_limit_pct']:g}%, USDT", fmt(s["to_global_usdt"])],
                 ["Серия убытков по системе", s["global_loss_streak"]],
                 ["Breaker'ы / kill", ", ".join(k for k, v in s["breakers"].items() if v) or "нет"],
                 ["Возраст equity, с", fmt(s["equity_age_s"], 0) + (" — устарела" if s["equity_stale"] else "")],
             ]), ""]
    for res in r["results"]:
        f = res["final"]
        lines += [f"## {res['name']}", ""]
        lines += [f"- {st['detail']} → equity {fmt(st['equity'])}"
                  + (f"; события: {', '.join(st['events'])}" if st["events"] else "")
                  + ("" if st.get("entry_allowed", True) else f"; вход запрещён: {st['entry_reason']}")
                  for st in res["steps"]]
        lines += [f"- **Итог:** equity {fmt(f['equity'])}, просадка от HWM {fmt(f['drawdown_pct'])}%, "
                  f"дневной PnL сделок {fmt(f['day_pnl'])} USDT",
                  f"- **Новый вход:** {'разрешён' if res['entry_allowed'] else 'запрещён — ' + res['entry_reason']}"]
        lines += [f"- **Последствие:** {c}" for c in res["consequences"]] or ["- Лимиты не задеты"]
        lines.append("")
    return "\n".join(lines) + "\n"
