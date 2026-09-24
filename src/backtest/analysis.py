"""Anti-lookahead проверки (backtester-design.md §1.2) — аналоги Freqtrade
lookahead-analysis и recursive-analysis, как библиотечные функции: будущие
стратегии (MEANREV-IMPL, GRID-BT-SIM) вызывают их в своих тестах.

lookahead_check (slicing): baseline на полном ряде, затем прогоны на префиксах,
заканчивающихся на барах решений baseline. Индикаторы, решения и закрытые сделки
до конца префикса обязаны совпасть бит-в-бит. Расхождение = стратегия видела
будущее (shift(-N), глобальные агрегаты, iloc[-1] по полному ряду в prepare()).

recursive_check: прогоны с поздней датой старта. После прогрева индикаторы обязаны
совпадать (для рекуррентных EMA/RSI — с допуском rel_tol: они сходятся), а сделки —
на интервалах синхронизации (обе копии без позиции и с одинаковым состоянием
риск-ядра: серии, блокировки, счётчики, breaker'ы). Ловит зависимость от точки старта.
Расхождение состояния риск-ядра после синхронизации (путезависимый HWM breaker'а)
закрывает интервал и попадает в отчёт как risk_divergences, а не маскируется.
"""
import math
from typing import Callable, Optional, Sequence

from src.backtest.data import Bar, InstrumentSpec
from src.backtest.engine import Backtest, BacktestResult, CostModel, Strategy


def _same(a: float, b: float, rel_tol: float) -> bool:
    a_nan = a is None or (isinstance(a, float) and math.isnan(a))
    b_nan = b is None or (isinstance(b, float) and math.isnan(b))
    if a_nan or b_nan:
        return a_nan and b_nan
    if rel_tol == 0.0:
        return a == b
    return math.isclose(a, b, rel_tol=rel_tol, abs_tol=rel_tol * 1e-6)


def _run(bars: Sequence[Bar], factory: Callable[[], Strategy], spec: InstrumentSpec,
         record_state: bool = False, **kw) -> BacktestResult:
    return Backtest(bars, factory(), spec, record_state=record_state, **kw).run()


def _sample(points: list[int], max_points: int) -> list[int]:
    if len(points) <= max_points:
        return points
    step = len(points) / max_points
    return sorted({points[int(j * step)] for j in range(max_points)} | {points[-1]})


def lookahead_check(factory: Callable[[], Strategy], bars: Sequence[Bar],
                    spec: InstrumentSpec, *, max_points: int = 20, **kw) -> dict:
    base = _run(bars, factory, spec, **kw)
    idx_of = {b.ts: i for i, b in enumerate(bars)}
    points = sorted({idx_of[d.ts] for d in base.decisions if d.outcome.startswith("accepted")})
    points = [p for p in points if p < len(bars) - 1]
    n_candidates = len(points)
    points = _sample(points, max_points)
    mismatches: list[str] = []
    biased: set[str] = set()
    for k in points:
        cut_ts = bars[k].ts
        sliced = _run(bars[:k + 1], factory, spec, **kw)
        for name, vals in base.indicators.items():
            got = sliced.indicators.get(name, [])
            for j in range(k + 1):
                if j >= len(got) or not _same(vals[j], got[j], 0.0):
                    biased.add(name)
                    mismatches.append(f"k={k}: индикатор {name}[{j}] full={vals[j]!r} "
                                      f"sliced={got[j] if j < len(got) else None!r}")
                    break
        base_dec = [d for d in base.decisions if d.ts <= cut_ts and d.action != "cancel_pending"]
        cut_dec = [d for d in sliced.decisions if d.action != "cancel_pending"]
        if base_dec != cut_dec:
            first = next((j for j, (x, y) in enumerate(zip(base_dec, cut_dec)) if x != y),
                         min(len(base_dec), len(cut_dec)))
            mismatches.append(f"k={k}: решения расходятся с #{first}: "
                              f"full={base_dec[first:first + 1]} sliced={cut_dec[first:first + 1]}")
        base_tr = [t for t in base.trades if t.exit_ts <= cut_ts and t.exit_reason != "end_of_data"]
        cut_tr = [t for t in sliced.trades if t.exit_reason != "end_of_data"]
        if base_tr != cut_tr:
            mismatches.append(f"k={k}: закрытые сделки расходятся ({len(base_tr)} vs {len(cut_tr)})")
    return {"ok": not mismatches, "points": len(points), "candidates": n_candidates,
            "baseline_trades": len(base.trades),
            "biased_indicators": sorted(biased), "mismatches": mismatches[:20],
            "n_mismatches": len(mismatches)}


def _trade_key(t) -> tuple:
    return (t.entry_signal_ts, t.entry_ts, t.exit_ts, t.exit_reason, t.entry_px, t.exit_px)


def recursive_check(factory: Callable[[], Strategy], bars: Sequence[Bar],
                    spec: InstrumentSpec, *, offsets: Sequence[int] = (250, 500, 1000),
                    rel_tol: float = 0.0, **kw) -> dict:
    base = _run(bars, factory, spec, record_state=True, **kw)
    warm = factory().warmup
    runs = []
    ok = True
    for s in offsets:
        if s + warm >= len(bars) - 1:
            runs.append({"offset": s, "skipped": "мало данных"})
            continue
        r = _run(bars[s:], factory, spec, record_state=True, **kw)
        ind_bad: dict[str, dict] = {}
        max_rel = 0.0
        for name, vals in base.indicators.items():
            got = r.indicators[name]
            for j in range(warm, len(got)):
                a, b = vals[s + j], got[j]
                if not _same(a, b, rel_tol):
                    ind_bad.setdefault(name, {"first_idx": s + j, "full": a, "late": b})
                if isinstance(a, float) and isinstance(b, float) and a and not (
                        math.isnan(a) or math.isnan(b)):
                    max_rel = max(max_rel, abs(a - b) / abs(a))
        intervals, divergences = _synced_intervals(base.states, r.states, s, warm)
        trades_equal = None
        n_cmp = 0
        for lo, hi in intervals:
            def inside(t, lo=lo, hi=hi) -> bool:
                return t.entry_signal_ts > lo and (hi is None or t.entry_signal_ts < hi)
            tb = [_trade_key(t) for t in base.trades if inside(t)]
            tr = [_trade_key(t) for t in r.trades if inside(t)]
            trades_equal = (trades_equal is not False) and tb == tr
            n_cmp += len(tb)
        # Пустое сравнение — не доказательство: без синхронизации или без сделок прогон
        # «не проверен» (так же выглядит и стратегия, чьи копии из-за скрытой зависимости
        # от старта ни разу не сходятся в одно состояние).
        if ind_bad:
            reason = "индикаторы расходятся после прогрева"
        elif trades_equal is False:
            reason = "сделки расходятся при одинаковом состоянии риск-ядра"
        elif not intervals:
            reason = "копии ни разу не синхронизировались — сделки не проверены"
        elif n_cmp == 0:
            reason = "в интервалах синхронизации нет сделок — не проверено"
        else:
            reason = None
        run_ok = reason is None
        ok &= run_ok
        runs.append({"offset": s, "ok": run_ok, "reason": reason, "indicators_mismatch": ind_bad,
                     "max_rel_diff": max_rel, "sync_ts": intervals[0][0] if intervals else None,
                     "trades_compared": n_cmp, "trades_equal": trades_equal,
                     "risk_divergences": divergences})
    return {"ok": ok, "warmup": warm, "rel_tol": rel_tol, "runs": runs}


GATING_FIELDS = ("entries_today", "global_loss_streak", "system_pause_until", "daily_breaker",
                 "global_breaker", "kill_active", "instruments", "open_risk")


def _synced_intervals(base_states, late_states, offset: int, warm: int):
    """Интервалы, где копии сравнимы: от синхронизации (обе без позиции и заявки, состояние
    риск-ядра одинаково) до повторного расхождения этого состояния.

    Повторное расхождение при совпавших индикаторах и сделках возможно только от
    путезависимых лимитов риск-ядра: HWM global_breaker считается от старта прогона,
    поэтому breaker срабатывает у копий в разное время. Это не lookahead — сделки после
    такого расхождения не сравниваются до новой синхронизации, эпизод (время и поля
    состояния) идёт в отчёт. Решение о входе на баре расхождения уже зависит от нового
    состояния, поэтому интервал для входов — (sync, divergence), граница исключена.
    """
    intervals, divergences = [], []
    start = None
    for j in range(warm, len(late_states)):
        ts, flat_r, gate_r = late_states[j]
        _, flat_b, gate_b = base_states[offset + j]
        if start is None:
            if flat_r and flat_b and gate_r == gate_b:
                start = ts
        elif gate_r != gate_b:
            intervals.append((start, ts))
            divergences.append({"ts": ts, "fields": [GATING_FIELDS[k] for k in range(len(gate_r))
                                                     if gate_r[k] != gate_b[k]]})
            start = None
    if start is not None:
        intervals.append((start, None))
    return intervals, divergences
