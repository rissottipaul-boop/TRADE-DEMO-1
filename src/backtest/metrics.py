"""Метрики отчёта (backtester-design.md §6.1) и гейт приёмки (§6.2).

Определения совместимы с summary-метриками Freqtrade и риск-ядром:
- equity — wallet balance по close каждого бара (включая нереализованный PnL);
- Sharpe/Sortino — годовые по ДНЕВНЫМ доходностям wallet balance (UTC-сутки,
  annualization √365, безрисковая ставка 0); Sortino — downside deviation по всем дням;
- MDD — от high-water mark по кривой equity, % и длительность «под водой»;
- expectancy = средний net PnL сделки (= hit·avg_win − (1−hit)·avg_loss);
- издержки = комиссии + смоделированное проскальзывание; брутто = net + издержки.
Проценты — в процентных пунктах (12.3 = 12.3%).
"""
import math
from dataclasses import dataclass, fields
from datetime import datetime, timezone
from typing import Optional, Sequence

DAY_MS = 86_400_000


def _utc(ts_ms: int) -> datetime:
    return datetime.fromtimestamp(ts_ms / 1000, tz=timezone.utc)


def daily_returns(curve: Sequence[tuple[int, float]]) -> list[float]:
    """Доходности по последнему значению equity в каждых UTC-сутках.

    curve[0] — стартовая точка (капитал до торговли); точка с ts ровно 00:00 —
    close предыдущих суток, поэтому ключ суток — (ts - 1) // DAY_MS.
    """
    if len(curve) < 2:
        return []
    last: dict[int, float] = {}
    for ts, eq in curve[1:]:
        last[(ts - 1) // DAY_MS] = eq
    prev = curve[0][1]
    out = []
    for key in sorted(last):
        eq = last[key]
        out.append(eq / prev - 1.0 if prev > 0 else 0.0)
        prev = eq
    return out


def sharpe_sortino(rets: Sequence[float], periods: int = 365) -> tuple[Optional[float],
                                                                       Optional[float]]:
    n = len(rets)
    if n < 2:
        return None, None
    mean = math.fsum(rets) / n
    std = math.sqrt(math.fsum((r - mean) ** 2 for r in rets) / (n - 1))
    down = math.sqrt(math.fsum(min(r, 0.0) ** 2 for r in rets) / n)
    k = math.sqrt(periods)
    return (mean / std * k if std > 0 else None), (mean / down * k if down > 0 else None)


def drawdown(curve: Sequence[tuple[int, float]]) -> dict:
    """MDD (%), пик/дно максимальной просадки и макс. длительность «под водой» (дни)."""
    out = {"mdd_pct": 0.0, "mdd_peak_ts": None, "mdd_trough_ts": None, "underwater_days": 0.0}
    if not curve:
        return out
    peak, peak_ts = curve[0][1], curve[0][0]
    uw_start: Optional[int] = None
    max_uw = 0
    for ts, eq in curve:
        if eq >= peak:
            if uw_start is not None:
                max_uw = max(max_uw, ts - uw_start)
                uw_start = None
            peak, peak_ts = eq, ts
            continue
        if uw_start is None:
            uw_start = peak_ts
        dd = (1.0 - eq / peak) * 100.0 if peak > 0 else 0.0
        if dd > out["mdd_pct"]:
            out.update(mdd_pct=dd, mdd_peak_ts=peak_ts, mdd_trough_ts=ts)
    if uw_start is not None:
        max_uw = max(max_uw, curve[-1][0] - uw_start)
    out["underwater_days"] = max_uw / DAY_MS
    return out


def trade_stats(trades: Sequence) -> dict:
    n = len(trades)
    pnls = [t.pnl for t in trades]
    wins = [p for p in pnls if p > 0]
    losses = [p for p in pnls if p < 0]
    streak = max_streak = 0
    for p in pnls:
        streak = streak + 1 if p < 0 else 0
        max_streak = max(max_streak, streak)
    sum_w, sum_l = math.fsum(wins), -math.fsum(losses)
    fees = math.fsum(t.fees for t in trades)
    slip = math.fsum(t.slippage for t in trades)
    net = math.fsum(pnls)
    gross = net + fees + slip
    dur = [t.duration_h for t in trades]
    return {
        "trades": n,
        "wins": len(wins),
        "losses": len(losses),
        "hit_rate_pct": len(wins) / n * 100.0 if n else None,
        "avg_win": sum_w / len(wins) if wins else None,
        "avg_loss": sum_l / len(losses) if losses else None,
        "expectancy": net / n if n else None,
        "expectancy_pct": math.fsum(t.pnl_pct for t in trades) / n * 100.0 if n else None,
        "profit_factor": (sum_w / sum_l) if sum_l > 0 else (math.inf if sum_w > 0 else None),
        "avg_duration_h": math.fsum(dur) / n if n else None,
        "min_duration_h": min(dur) if dur else None,
        "max_duration_h": max(dur) if dur else None,
        "max_loss_streak": max_streak,
        "trade_pnl": net,
        "fees": fees,
        "slippage": slip,
        "costs": fees + slip,
        "gross_pnl": gross,
        "cost_share_pct": (fees + slip) / gross * 100.0 if gross > 0 else None,
    }


def compute_metrics(curve: Sequence[tuple[int, float]], trades: Sequence, initial: float, *,
                    benchmark: Optional[float] = None,
                    exposure: Optional[float] = None) -> dict:
    final = curve[-1][1] if curve else initial
    days = (curve[-1][0] - curve[0][0]) / DAY_MS if len(curve) >= 2 else 0.0
    years = days / 365.0
    if final <= 0:
        cagr: Optional[float] = -100.0
    elif years > 0:
        cagr = ((final / initial) ** (1.0 / years) - 1.0) * 100.0
    else:
        cagr = None
    dd = drawdown(curve)
    sharpe, sortino = sharpe_sortino(daily_returns(curve))
    m = {
        "initial": initial,
        "final": final,
        "net_profit": final - initial,
        "net_profit_pct": (final / initial - 1.0) * 100.0,
        "days": days,
        "cagr_pct": cagr,
        **dd,
        "sharpe": sharpe,
        "sortino": sortino,
        "calmar": (cagr / dd["mdd_pct"]) if cagr is not None and dd["mdd_pct"] > 0 else None,
        "benchmark_pct": benchmark * 100.0 if benchmark is not None else None,
        "exposure_pct": exposure * 100.0 if exposure is not None else None,
    }
    m.update(trade_stats(trades))
    return m


def period_returns(curve: Sequence[tuple[int, float]], period: str = "month") -> list[tuple[str, float]]:
    """Доходность equity по календарным месяцам/годам UTC: [(метка, %)]."""
    if len(curve) < 2:
        return []
    fmt = "%Y-%m" if period == "month" else "%Y"
    last: dict[str, float] = {}
    for ts, eq in curve[1:]:
        last[_utc(ts - 1).strftime(fmt)] = eq
    prev = curve[0][1]
    out = []
    for key in sorted(last):
        out.append((key, (last[key] / prev - 1.0) * 100.0))
        prev = last[key]
    return out


def trades_by_period(trades: Sequence, period: str = "year") -> dict[str, tuple[int, float]]:
    fmt = "%Y-%m" if period == "month" else "%Y"
    out: dict[str, tuple[int, float]] = {}
    for t in trades:
        key = _utc(t.exit_ts).strftime(fmt)
        n, pnl = out.get(key, (0, 0.0))
        out[key] = (n + 1, pnl + t.pnl)
    return out


# --- Гейт приёмки §6.2 (пороги — конфиг, не константы кода) ---

@dataclass(frozen=True)
class GateConfig:
    min_expectancy: float = 0.0            # строго больше, при удвоенном проскальзывании
    min_profit_factor: float = 1.3
    min_sharpe: float = 0.5
    max_mdd_pct: float = 10.0
    min_trades: int = 100
    min_trades_per_window: int = 30        # для оконных выводов (информативно)
    min_profitable_windows_pct: float = 60.0
    max_cost_share_pct: float = 50.0       # строго меньше
    max_loss_streak: int = 7

    def as_dict(self) -> dict:
        return {f.name: getattr(self, f.name) for f in fields(self)}


def evaluate_gate(oos: dict, oos_stress: dict, window_tests: Sequence[dict],
                  cfg: GateConfig = GateConfig()) -> dict:
    """Все 8 порогов на OOS-конкатенации walk-forward; любой провал -> не в paper."""
    def ok(value, cond) -> bool:
        return value is not None and cond(value)

    n_win = len(window_tests)
    prof = sum(1 for w in window_tests if w["net_profit"] > 0)
    prof_pct = prof / n_win * 100.0 if n_win else None
    thin = sum(1 for w in window_tests if w["trades"] < cfg.min_trades_per_window)
    checks = [
        ("1", "Expectancy > 0 (slippage ×2)", oos_stress.get("expectancy"),
         f"> {cfg.min_expectancy}", ok(oos_stress.get("expectancy"), lambda v: v > cfg.min_expectancy)),
        ("2", "Profit factor", oos.get("profit_factor"), f">= {cfg.min_profit_factor}",
         ok(oos.get("profit_factor"), lambda v: v >= cfg.min_profit_factor)),
        ("3", "Sharpe OOS", oos.get("sharpe"), f">= {cfg.min_sharpe}",
         ok(oos.get("sharpe"), lambda v: v >= cfg.min_sharpe)),
        ("4", "MDD OOS (wallet), %", oos.get("mdd_pct"), f"<= {cfg.max_mdd_pct}",
         ok(oos.get("mdd_pct"), lambda v: v <= cfg.max_mdd_pct)),
        ("5", f"Закрытых сделок OOS (окон < {cfg.min_trades_per_window} сделок: {thin}/{n_win})",
         oos.get("trades"), f">= {cfg.min_trades}",
         ok(oos.get("trades"), lambda v: v >= cfg.min_trades)),
        ("6", "Прибыльных test-окон, %", prof_pct, f">= {cfg.min_profitable_windows_pct}",
         ok(prof_pct, lambda v: v >= cfg.min_profitable_windows_pct)),
        ("7", "Доля издержек в брутто-PnL, %", oos.get("cost_share_pct"),
         f"< {cfg.max_cost_share_pct}",
         ok(oos.get("cost_share_pct"), lambda v: v < cfg.max_cost_share_pct)),
        ("8", "Макс. серия убытков OOS", oos.get("max_loss_streak"), f"<= {cfg.max_loss_streak}",
         ok(oos.get("max_loss_streak"), lambda v: v <= cfg.max_loss_streak)),
    ]
    rows = [{"id": c[0], "name": c[1], "value": c[2], "threshold": c[3], "ok": c[4]}
            for c in checks]
    return {"passed": all(r["ok"] for r in rows), "checks": rows,
            "profitable_windows": prof, "windows": n_win, "thin_windows": thin}
