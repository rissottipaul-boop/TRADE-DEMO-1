"""Markdown-отчёт бэктеста: метрики §6.1, чувствительность §3.3, walk-forward §2, гейт §6.2."""
import math
from collections import Counter
from typing import Optional, Sequence

from src.backtest.data import Dataset, InstrumentSpec, ms_to_iso
from src.backtest.metrics import period_returns, trades_by_period

MONTHS = ["01", "02", "03", "04", "05", "06", "07", "08", "09", "10", "11", "12"]


def f(v, nd: int = 2) -> str:
    if v is None:
        return "—"
    if isinstance(v, bool):
        return "да" if v else "нет"
    if isinstance(v, float) and math.isinf(v):
        return "∞" if v > 0 else "−∞"
    if isinstance(v, int):
        return str(v)
    return f"{v:,.{nd}f}".replace(",", " ")


def d(ts: Optional[int]) -> str:
    return ms_to_iso(ts)[:10] if ts else "—"


def table(header: Sequence[str], rows: Sequence[Sequence]) -> str:
    out = ["| " + " | ".join(header) + " |", "| " + " | ".join("---" for _ in header) + " |"]
    out += ["| " + " | ".join(str(c) for c in r) + " |" for r in rows]
    return "\n".join(out)


METRIC_ROWS = [
    ("Капитал: старт → финал, USDT", lambda m: f"{f(m['initial'])} → {f(m['final'])}"),
    ("Net profit, USDT", lambda m: f(m["net_profit"])),
    ("Net profit, %", lambda m: f(m["net_profit_pct"])),
    ("CAGR, %", lambda m: f(m["cagr_pct"])),
    ("MDD (wallet), %", lambda m: f(m["mdd_pct"])),
    ("MDD: пик → дно", lambda m: f"{d(m['mdd_peak_ts'])} → {d(m['mdd_trough_ts'])}"),
    ("Макс. «под водой», дн.", lambda m: f(m["underwater_days"], 1)),
    ("Sharpe (дневн., ×√365)", lambda m: f(m["sharpe"])),
    ("Sortino", lambda m: f(m["sortino"])),
    ("Calmar", lambda m: f(m["calmar"])),
    ("Сделок (закрытых)", lambda m: f(m["trades"])),
    ("Hit rate, %", lambda m: f(m["hit_rate_pct"], 1)),
    ("Expectancy, USDT / сделку", lambda m: f(m["expectancy"])),
    ("Expectancy, % / сделку", lambda m: f(m["expectancy_pct"], 3)),
    ("Profit factor", lambda m: f(m["profit_factor"])),
    ("Длительность avg / min / max, ч",
     lambda m: f"{f(m['avg_duration_h'], 1)} / {f(m['min_duration_h'], 0)} / {f(m['max_duration_h'], 0)}"),
    ("Макс. серия убытков", lambda m: f(m["max_loss_streak"])),
    ("Комиссии, USDT", lambda m: f(m["fees"])),
    ("Проскальзывание, USDT", lambda m: f(m["slippage"])),
    ("Доля издержек в брутто-PnL, %", lambda m: f(m["cost_share_pct"], 1)
     if m["cost_share_pct"] is not None else "— (брутто ≤ 0)"),
    ("Рынок за период (buy&hold 100%), %", lambda m: f(m["benchmark_pct"])),
    ("Экспозиция (бары в позиции), %", lambda m: f(m["exposure_pct"], 1)),
]


def metrics_table(runs: Sequence[tuple[str, dict]]) -> str:
    header = ["Метрика"] + [name for name, _ in runs]
    rows = [[label] + [fn(m) for _, m in runs] for label, fn in METRIC_ROWS]
    return table(header, rows)


def data_section(datasets: Sequence[tuple[Dataset, InstrumentSpec]]) -> str:
    rows = []
    for ds, spec in datasets:
        missing = sum(g.missing for g in ds.gaps)
        zero_vol = sum(1 for b in ds.bars if not b.vol)
        rows.append([ds.inst_id, f"`{ds.dataset_id}`", f"{ms_to_iso(ds.first_ts)} → "
                     f"{ms_to_iso(ds.last_ts)}", f(len(ds.bars)),
                     f"{len(ds.gaps)} ({missing} свечей)", zero_vol, len(ds.anomalies),
                     f"{spec.tick_sz:g} / {spec.lot_sz:g} / {spec.min_sz:g}"])
    bar = datasets[0][0].bar if datasets else "1H"
    out = [table(["Инструмент", "dataset_id", "Диапазон (open, UTC)", f"Баров {bar}",
                  "Дыр (пропущено)", "Баров vol=0 (не дыры)", "Аномалий OHLC",
                  "tickSz / lotSz / minSz"], rows)]
    for ds, _ in datasets:
        if ds.gaps:
            items = ", ".join(f"{ms_to_iso(g.start_ts)}…{ms_to_iso(g.end_ts)} ({g.missing})"
                              for g in ds.gaps[:12])
            more = f" и ещё {len(ds.gaps) - 12}" if len(ds.gaps) > 12 else ""
            out.append(f"\nДыры {ds.inst_id} (биржа не вернула свечи): {items}{more}.")
        if ds.anomalies:
            out.append(f"\nАномалии {ds.inst_id}: " + "; ".join(ds.anomalies[:10]))
    return "\n".join(out)


def sensitivity_table(rows: Sequence[tuple[str, str, dict]]) -> str:
    return table(["Прогон", "Сценарий издержек", "Net %", "Sharpe", "PF", "Expectancy USDT",
                  "Издержки USDT", "Доля издержек %"],
                 [[run, sc, f(m["net_profit_pct"]), f(m["sharpe"]), f(m["profit_factor"]),
                   f(m["expectancy"]), f(m["costs"]),
                   f(m["cost_share_pct"], 1)] for run, sc, m in rows])


def market_curve(bars, start_idx: int = 0) -> list[tuple[int, float]]:
    """Псевдо-кривая рынка (buy&hold 100% без издержек) для помесячного сравнения."""
    b0 = bars[start_idx]
    step = bars[1].ts - bars[0].ts if len(bars) > 1 else 0
    return [(b0.ts, b0.o)] + [(b.ts + step, b.c) for b in bars[start_idx:]]


def yearly_table(market, runs: Sequence[tuple[str, object]]) -> str:
    mk = dict(period_returns(market, "year"))
    per = [(name, dict(period_returns(r.curve, "year")), trades_by_period(r.trades, "year"))
           for name, r in runs]
    header = ["Год", "Рынок %"]
    for name, _, _ in per:
        header += [f"{name} %", f"{name}: сделок / PnL USDT"]
    rows = []
    for y in sorted(mk):
        row = [y, f(mk[y])]
        for _, rets, tr in per:
            n, pnl = tr.get(y, (0, 0.0))
            row += [f(rets.get(y)), f"{n} / {f(pnl)}"]
        rows.append(row)
    return table(header, rows)


def monthly_matrix(curve) -> str:
    rets = dict(period_returns(curve, "month"))
    years = sorted({k[:4] for k in rets})
    rows = []
    for y in years:
        vals = [rets.get(f"{y}-{m}") for m in MONTHS]
        total = 1.0
        for v in vals:
            if v is not None:
                total *= 1 + v / 100.0
        rows.append([y] + [f(v, 1) for v in vals] + [f((total - 1) * 100.0, 1)])
    return table(["Год"] + MONTHS + ["Год, %"], rows)


def _score(v) -> str:
    """Оценка окна: -inf — метрика не определена (нет дневной вариации equity)."""
    return "—" if v == float("-inf") else f(v)


def wf_windows_table(bars, wf) -> str:
    rows = []
    for w in wf.windows:
        m = w.test.metrics()
        win = w.window
        p = ", ".join(f"{k}={v}" for k, v in w.params.items() if k in ("fast", "slow")) or "—"
        rows.append([win.k, d(bars[win.train.start].ts), d(bars[win.test.start].ts),
                     d(bars[win.test.end - 1].ts), p, _score(w.train_score[0]),
                     _score(w.valid_score[0]), f(m["net_profit_pct"]), f(m["benchmark_pct"]),
                     m["trades"], f(m["mdd_pct"])])
    return table(["#", "train с", "test с", "test по", "Выбрано", "Sharpe train",
                  "Sharpe valid", "Test net %", "Рынок %", "Сделок", "MDD %"], rows)


def _gate_value(v) -> str:
    return str(v) if isinstance(v, int) and not isinstance(v, bool) else f(v)


def failed_checks(gate: dict) -> list[str]:
    return [c["id"] for c in gate["checks"] if not c["ok"]]


def gate_table(gate: dict, stress: Optional[dict] = None) -> str:
    """Гейт §6.2; stress — те же пороги на OOS при slip×2 (§3.3: до калибровки все
    приёмочные метрики считаются и при удвоенном проскальзывании), информативно."""
    header = ["#", "Порог §6.2", "Значение", "Условие", "Итог"]
    if stress:
        header += ["slip×2: значение", "slip×2: итог"]
    rows = []
    for i, c in enumerate(gate["checks"]):
        row = [c["id"], c["name"], _gate_value(c["value"]), c["threshold"],
               "✅" if c["ok"] else "❌"]
        if stress:
            s = stress["checks"][i]
            row += [_gate_value(s["value"]), "✅" if s["ok"] else "❌"]
        rows.append(row)
    if gate["passed"]:
        verdict = "**ПРОЙДЕН**"
    else:
        verdict = ("**НЕ ПРОЙДЕН** (провалены пороги " + ", ".join(failed_checks(gate))
                   + ") — стратегия в доработку, не в paper")
    text = table(header, rows) + f"\n\nГейт: {verdict}."
    if stress:
        text += (" Все пороги при slip×2: " + ("пройдены." if stress["passed"] else
                 "не пройдены (" + ", ".join(failed_checks(stress)) + ")."))
    return text


def breaker_trips(r) -> list[tuple[int, float]]:
    """[(время, просадка %)]: просадка кривой wallet от её HWM на последней точке до
    срабатывания global_breaker.

    Если она меньше порога risk.GLOBAL_DD_LIMIT_PCT, breaker сработал раньше номинала:
    record_pnl прибавляет PnL к equity, уже учтённой update_equity по рынку (двойной учёт).
    """
    out = []
    for t, e in r.risk_events:
        if e != "global_breaker":
            continue
        ts_ms = int(t * 1000)
        pts = [eq for ts, eq in r.curve if ts <= ts_ms]
        if pts:
            out.append((ts_ms, (1.0 - pts[-1] / max(pts)) * 100.0))
    return out


def breaker_rejections(r) -> int:
    """Входы, отклонённые глобальным breaker'ом (после его срабатывания)."""
    return sum(v for k, v in r.rejections.items() if "breaker" in k and "HWM" in k)


def rejections_summary(groups: Sequence[tuple[str, Sequence[object]]]) -> str:
    """groups: [(имя, [результаты прогонов])] — для walk-forward агрегируются все test-окна."""
    rows = []
    for name, results in groups:
        rej: Counter = Counter()
        ev: Counter = Counter()
        trips: list[tuple[int, float]] = []
        for r in results:
            rej.update({k: v for k, v in r.rejections.items() if k != "warmup"})
            ev.update(e for _, e in r.risk_events)
            trips += breaker_trips(r)
        rows.append([name, "; ".join(f"{k} — {v}" for k, v in rej.most_common()) or "—",
                     "; ".join(f"{k} — {v}" for k, v in ev.most_common()) or "—",
                     ", ".join(f"{d(ts)}: {f(x)}" for ts, x in trips) or "—"])
    return table(["Прогон", "Отказы во входе (причина — число)", "События риск-ядра",
                  "global_breaker: просадка кривой в момент срабатывания, %"], rows)
