"""П. 34 — автоматический черновик разбора закрытых сделок по фактам журнала и правилам.

Источники (только чтение, сети нет):
  data/pump_journal.jsonl  — факты сделок памп-кармана: entry / stop / exit / error (схема v=1,
                             docstring src/pump_journal.py); читается тем же read_journal;
  pump-pocket.json         — зафиксированные лимиты кармана (read_pocket);
  роль Pump Risk Taker     — правила «вход только со стопом сразу после входа», метка `pmp`,
                             обоснование входа обязательно, свежесть сигнала ≤ 15 мин.
Сделка закрыта, когда сумма size строк exit покрыла объём входа (или exit без size).
Черновик не сверяется с биржей: перед выводами агент сверяет ID ордеров штатными командами.

Проверки правил — ✅ выполнено, ❌ нарушено, ⚠️ отклонение без нарушения лимита, «—» нет данных.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional

from src.analytics.common import fmt, md_table, num
from src.pump_journal import parse_ts, read_journal, read_pocket
from src.pump_scanner import JOURNAL_PATH, POCKET_PATH, PROJECT_TZ, PocketError

STOP_DELAY_MAX_MIN = 10.0     # «сразу» после входа: стоп не позже 10 минут
SIGNAL_MAX_AGE_MIN = 15.0     # PUMP-STALE: --max-age-min сканера
OWNER_PREFIX = "pmp"
EPS = 1e-9


@dataclass
class TradeFacts:
    key: str
    pair: str
    entry: dict
    entry_line: int
    stops: list[tuple[int, dict]] = field(default_factory=list)
    exits: list[tuple[int, dict]] = field(default_factory=list)
    errors: list[tuple[int, dict]] = field(default_factory=list)

    @property
    def size(self) -> float:
        return num(self.entry.get("size")) or 0.0

    @property
    def closed_size(self) -> float:
        return sum(num(e.get("size")) or 0.0 for _, e in self.exits)

    @property
    def closed(self) -> bool:
        if any(e.get("size") is None for _, e in self.exits):
            return bool(self.exits)
        return bool(self.exits) and self.closed_size >= self.size * (1 - 1e-6) - EPS


def collect_trades(records: list[tuple[int, dict]]) -> list[TradeFacts]:
    """Сделки по журналу в порядке файла (как src.pump_journal): ключ — trade_id, иначе pair."""
    trades: list[TradeFacts] = []
    open_by_key: dict[str, TradeFacts] = {}
    for line, rec in records:
        ev = rec.get("event")
        pair = str(rec.get("pair") or rec.get("inst_id") or "")
        tid = rec.get("trade_id")
        if ev == "entry":
            key = str(tid or pair)
            if key in open_by_key and not open_by_key[key].closed:
                open_by_key[key].entry.setdefault("_adds", []).append(line)   # доливка
                continue
            t = TradeFacts(key=key, pair=pair, entry=dict(rec), entry_line=line)
            trades.append(t)
            open_by_key[key] = t
            continue
        if ev not in ("stop", "exit", "error"):
            continue
        t = open_by_key.get(str(tid)) if tid else None
        if t is None and pair:
            cands = [x for x in open_by_key.values() if x.pair == pair and not x.closed]
            t = cands[0] if len(cands) == 1 else None
        if t is None:
            continue
        {"stop": t.stops, "exit": t.exits, "error": t.errors}[ev].append((line, rec))
    return trades


def _minutes(a: Optional[datetime], b: Optional[datetime]) -> Optional[float]:
    return None if a is None or b is None else (b - a).total_seconds() / 60.0


def review_trade(t: TradeFacts, pocket) -> dict:
    e = t.entry
    side = str(e.get("side") or "buy")
    entry_ts = parse_ts(e.get("ts"))
    price = num(e.get("price"))
    size = t.size
    cost = num(e.get("cost_usdt")) or num(e.get("usdt")) or (price * size if price else None)
    fee_in = abs(num(e.get("fee_usdt")) or num(e.get("fee")) or 0.0)
    stop0 = num(e.get("stop"))
    stop_src = "entry"
    first_stop_ts = entry_ts if stop0 is not None else None
    stops_sorted = sorted(t.stops, key=lambda x: parse_ts(x[1].get("ts")) or datetime.max.replace(tzinfo=PROJECT_TZ))
    if stop0 is None and stops_sorted:
        stop0 = num(stops_sorted[0][1].get("stop"))
        first_stop_ts = parse_ts(stops_sorted[0][1].get("ts"))
        stop_src = f"stop (строка {stops_sorted[0][0]})"
    algo_stop_ts = parse_ts(stops_sorted[0][1].get("ts")) if stops_sorted else None
    risk = num(e.get("risk_usdt"))
    if risk is None and price and stop0:
        risk = abs(price - stop0) * size + fee_in * 2
    exits = sorted(t.exits, key=lambda x: parse_ts(x[1].get("ts")) or datetime.max.replace(tzinfo=PROJECT_TZ))
    pnl_known = [num(x.get("pnl")) for _, x in exits]
    pnl_total = sum(p for p in pnl_known if p is not None)
    last_exit_ts = parse_ts(exits[-1][1].get("ts")) if exits else None
    exit_rows = []
    for line, x in exits:
        px = num(x.get("price"))
        move = (px / price - 1) * 100 * (1 if side == "buy" else -1) if px and price else None
        slip = None
        if x.get("kind") == "stop" and px and stop0:
            slip = (stop0 - px) / stop0 * 100 * (1 if side == "buy" else -1)
        exit_rows.append({"line": line, "ts": x.get("ts"), "kind": x.get("kind"), "price": px,
                          "size": num(x.get("size")), "pnl": num(x.get("pnl")),
                          "move_pct": move, "stop_slip_pct": slip, "reason": x.get("reason")})
    checks = []

    def add(name, status, detail):
        checks.append({"rule": name, "status": status, "detail": detail})

    add("Обоснование входа (reason обязательно, схема журнала)",
        "✅" if str(e.get("reason") or "").strip() else "❌",
        (str(e.get("reason"))[:160] + "…") if len(str(e.get("reason") or "")) > 160 else (e.get("reason") or "нет"))
    if stop0 is None:
        add("Стоп сразу после входа (роль Pump Risk Taker)", "❌", "стоп не найден ни во входе, ни строкой stop")
    else:
        delay = _minutes(entry_ts, algo_stop_ts) if algo_stop_ts else None
        if delay is None:
            add("Стоп сразу после входа (роль Pump Risk Taker)", "⚠️",
                f"цена стопа {stop0} указана во входе, строки stop с algo-ордером нет")
        else:
            add("Стоп сразу после входа (роль Pump Risk Taker)",
                "✅" if delay <= STOP_DELAY_MAX_MIN else "❌",
                f"algo-стоп через {delay:.1f} мин после входа (порог {STOP_DELAY_MAX_MIN:.0f} мин)")
    if stop0 is not None and price and pocket and pocket.stop_range:
        dist = abs(price - stop0) / price * 100
        lo, hi = pocket.stop_range
        add("Дистанция стопа в stop_loss_range кармана",
            "✅" if lo - 1e-6 <= dist <= hi + 1e-6 else "❌", f"{dist:.2f}% (диапазон {lo}–{hi}%)")
    else:
        add("Дистанция стопа в stop_loss_range кармана", "—", "нет стопа, цены или диапазона")
    if risk is not None and pocket:
        add("Риск до стопа ≤ max_risk_per_trade", "✅" if risk <= pocket.max_risk_per_trade + EPS else "❌",
            f"{fmt(risk)} USDT ≤ {fmt(pocket.max_risk_per_trade)}")
    else:
        add("Риск до стопа ≤ max_risk_per_trade", "—", "риск не определён")
    if cost is not None and pocket:
        over = cost - pocket.max_position
        status = "✅" if over <= EPS else ("⚠️" if over <= pocket.max_position * 0.01 else "❌")
        add("Стоимость позиции ≤ лимита позиции (max_position_pct, USDT)", status,
            f"{fmt(cost)} USDT против {fmt(pocket.max_position)}" +
            (f" — превышение {fmt(over)} USDT (проскальзывание исполнения)" if over > EPS else ""))
    ids = [e.get("cl_ord_id")] + [o.get("algo_cl_ord_id") for _, s in t.stops for o in (s.get("orders") or [])]
    ids = [i for i in ids if i]
    if ids:
        bad = [i for i in ids if not str(i).startswith(OWNER_PREFIX)]
        add("Метка владельца pmp у ордеров (AGENTS.md §6)", "❌" if bad else "✅",
            f"{len(ids) - len(bad)}/{len(ids)} с префиксом pmp" + (f"; без метки: {bad}" if bad else ""))
    else:
        add("Метка владельца pmp у ордеров (AGENTS.md §6)", "—", "clOrdId в журнале не записан")
    scan_ts = parse_ts(e.get("scan_candle_ts"))
    if scan_ts and entry_ts:
        age = _minutes(scan_ts + timedelta(hours=1), entry_ts)
        if age < 0:
            add("Возраст сигнала при входе (PUMP-STALE ≤ 15 мин)", "⚠️",
                f"scan_candle_ts {e.get('scan_candle_ts')} + 1 ч позже входа на {-age:.0f} мин: в журнал, "
                "вероятно, записано время закрытия свечи вместо открытия (у сканера candle_ts — "
                "открытие) — поправить запись или проверить скан")
        else:
            add("Возраст сигнала при входе (PUMP-STALE ≤ 15 мин)", "✅" if age <= SIGNAL_MAX_AGE_MIN else "⚠️",
                f"{age:.0f} мин от закрытия сигнальной 1H-свечи")
    missing_pnl = sum(1 for p in pnl_known if p is None)
    add("Полнота строк exit (kind и pnl)", "✅" if not missing_pnl and all(r["kind"] for r in exit_rows) else "⚠️",
        f"exit: {len(exit_rows)}, без pnl: {missing_pnl}")
    if t.errors:
        add("Ошибки по сделке", "⚠️", "; ".join(f"строка {l}: {r.get('kind')} {r.get('code')} {r.get('reason')}"
                                              for l, r in t.errors))
    r_mult = pnl_total / risk if risk else None
    questions = []
    kinds = [r["kind"] for r in exit_rows]
    if kinds and all(k == "stop" for k in kinds):
        questions.append("Сделка целиком закрыта стопом: подтвердился ли импульс после входа? "
                         "Сверить свечи после входа с условиями скана")
    if "take" in kinds and kinds[-1] == "stop":
        questions.append("После первой ступени тейка остаток закрыт исходным стопом: правило "
                         "переноса стопа в безубыток в роли не зафиксировано — вопрос для "
                         "Crypto Insight Hunter (статистика по журналу), не правка лимитов")
    slips = [r["stop_slip_pct"] for r in exit_rows if r["stop_slip_pct"] is not None]
    if slips and max(slips) > 0.1:
        questions.append(f"Проскальзывание стопа до {max(slips):.2f}% — учесть в риске до стопа")
    if any("⚠️" == c["status"] and c["rule"].startswith("Возраст") for c in checks):
        questions.append("Вход позже окна свежести скана: была ли повторная проверка цены перед входом?")
    return {
        "key": t.key, "pair": t.pair, "side": side, "closed": t.closed,
        "entry_line": t.entry_line, "entry_ts": e.get("ts"), "entry_price": price, "size": size,
        "cost_usdt": cost, "fee_in": fee_in, "stop_initial": stop0, "stop_source": stop_src,
        "stop_moves": len(t.stops), "risk_usdt": risk, "pnl_total": pnl_total,
        "r_multiple": r_mult, "hold_min": _minutes(entry_ts, last_exit_ts),
        "exits": exit_rows, "checks": checks, "questions": questions,
        "first_stop_ts": first_stop_ts.isoformat() if first_stop_ts else None,
    }


def review(journal: Path = JOURNAL_PATH, pocket_path: Path = POCKET_PATH,
           trade: Optional[str] = None, last: Optional[int] = None) -> dict:
    j = read_journal(Path(journal))
    try:
        pocket = read_pocket(Path(pocket_path))
        pocket_err = None
    except PocketError as exc:
        pocket, pocket_err = None, str(exc)
    trades = collect_trades(j.records)
    closed = [t for t in trades if t.closed]
    if trade:
        closed = [t for t in closed if t.key == trade or t.key.startswith(trade)]
    if last:
        closed = closed[-last:]
    return {"journal": j.path, "journal_exists": j.exists, "bad_lines": j.bad,
            "pocket": pocket.path if pocket else None, "pocket_error": pocket_err,
            "open_trades": [t.key for t in trades if not t.closed],
            "reviews": [review_trade(t, pocket) for t in closed]}


def render(rep: dict) -> str:
    lines = ["# Черновик разбора закрытых сделок", "",
             "> Черновик собран кодом по `data/pump_journal.jsonl` и `pump-pocket.json`. Сверка с "
             "биржей не выполнялась: перед выводами сверить ID ордеров (`okx --demo spot get`).", ""]
    if rep["pocket_error"]:
        lines += [f"**Карман не прочитан:** {rep['pocket_error']} — проверки лимитов пропущены", ""]
    if rep["bad_lines"]:
        lines += [f"**Нечитаемые строки журнала:** {len(rep['bad_lines'])}", ""]
    if not rep["reviews"]:
        lines += ["Закрытых сделок по фильтру нет."]
    for r in rep["reviews"]:
        lines += [f"## {r['pair']} — `{r['key']}`", "",
                  f"- **Вход:** {r['entry_ts']} по {fmt(r['entry_price'], 6)}, объём {fmt(r['size'], 4)}, "
                  f"стоимость {fmt(r['cost_usdt'])} USDT, комиссия {fmt(r['fee_in'], 4)}",
                  f"- **Стоп:** {fmt(r['stop_initial'], 6)} (источник: {r['stop_source']}), строк stop: {r['stop_moves']}",
                  f"- **Риск до стопа:** {fmt(r['risk_usdt'])} USDT; **итог:** {fmt(r['pnl_total'])} USDT "
                  f"({fmt(r['r_multiple'])} R); удержание {fmt(r['hold_min'], 0)} мин", "",
                  md_table(["Выход", "Тип", "Цена", "Объём", "PnL", "Ход от входа, %", "Проск. стопа, %"],
                           [[x["ts"], x["kind"], fmt(x["price"], 6), fmt(x["size"], 4), fmt(x["pnl"]),
                             fmt(x["move_pct"]), fmt(x["stop_slip_pct"])] for x in r["exits"]]), "",
                  md_table(["Правило", "", "Факт"], [[c["rule"], c["status"], c["detail"]] for c in r["checks"]]), ""]
        if r["questions"]:
            lines += ["**Вопросы для разбора:**", ""] + [f"- {q}" for q in r["questions"]] + [""]
    if rep["open_trades"]:
        lines += [f"Открытые (не разбираются): {', '.join(rep['open_trades'])}"]
    return "\n".join(lines) + "\n"
