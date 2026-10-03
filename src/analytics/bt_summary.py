"""П. 33 — сводка по результатам прогона: доходность, просадка, издержки, сделки, ограничения.

Вход — запись прогона из bt_config.run_config (файл data/analytics/runs/<run_id>.json).
Метрики — те же, что считает src.backtest.metrics.compute_metrics (определения там).
Проверка по порогам гейта (GateConfig) — информационная: гейт приёмки в проекте
считается только на OOS walk-forward (`python -m src.backtest baseline`), одиночный
прогон его не заменяет — это всегда выносится в ограничения.
"""
from __future__ import annotations

from typing import Any

from src.analytics.common import fmt, iso_ms, md_table, num, unclean
from src.backtest.metrics import GateConfig

GATE = GateConfig()
LIQUID = {"BTC-USDT", "ETH-USDT"}      # где 5 бп проскальзывания подтверждены demo-slippage.md


def limitations(run: dict) -> list[str]:
    """Ограничения набора данных и постановки, которые надо назвать вместе с цифрами."""
    out: list[str] = []
    ds, m, cfg = run["dataset"], run["metrics"], run["config"]
    days = num(m.get("days")) or 0.0
    trades = int(m.get("trades") or 0)
    hold = ds.get("holdout") or {}
    if hold.get("excluded", True):
        out.append(f"holdout исключён: последние {hold.get('days', 182)} дн. снапшота "
                   f"(с {iso_ms(hold.get('start_ts'))}) не тестировались — результат in-sample")
    else:
        out.append(f"holdout затронут (касание №{hold.get('touch_no')}): повторные касания "
                   "обесценивают holdout как независимую проверку")
    out.append("одиночный прогон без walk-forward: гейт приёмки (backtester-design.md §6.2) "
               "не оценивался, параметры могли быть подогнаны под период")
    if days < 365:
        out.append(f"короткий период: {days:.0f} дн. (< 1 года) — мало рыночных режимов")
    if trades < GATE.min_trades:
        out.append(f"мало сделок: {trades} (< {GATE.min_trades} для гейта) — доля случайности высока")
    if ds.get("gaps"):
        out.append(f"дыры в данных: {ds['gaps']} (пропущено свечей {ds.get('missing_bars')})")
    if ds.get("anomalies"):
        out.append(f"аномалии OHLC: {ds['anomalies']} (примеры: {ds.get('anomaly_examples')})")
    if ds.get("inst") not in LIQUID:
        out.append(f"{ds.get('inst')}: фиксированные 5 бп проскальзывания подтверждены только "
                   "для BTC/ETH; для альтов нужна spread-модель (CostModel, demo-slippage.md §8)")
    reasons = run.get("exit_reasons") or {}
    if reasons.get("end_of_data"):
        out.append("позиция закрыта принудительно в конце данных (end_of_data) — последняя "
                   "сделка не по правилам стратегии")
    if run.get("rejections"):
        rej = ", ".join(f"{k}: {v}" for k, v in sorted(run["rejections"].items()))
        out.append(f"риск-ядро отклоняло входы ({rej}) — результат зависит от лимитов src/risk.py")
        if any("глобальный breaker" in k for k in run["rejections"]):
            out.append("в прогоне сработал глобальный breaker −15% от HWM: ручной сброс в бэктесте "
                       "не моделируется, после срабатывания входов нет до конца данных — часть "
                       "периода стратегия простаивала")
    if trades and m.get("cost_share_pct") is None:
        out.append("доля издержек не определена: брутто-PnL сделок ≤ 0 (убыточно и без издержек)")
    costs = cfg.get("costs") or {}
    if float(costs.get("fee_mult", 1.0)) == 0 or float(costs.get("slip_mult", 1.0)) == 0:
        out.append("издержки обнулены — результат брутто, в торговлю не переносится")
    out.append("модель v1: спот long-only, одна позиция, один инструмент; исполнение по open "
               "следующего бара; стоп внутри бара — худший сценарий")
    return out


def _gate_rows(m: dict) -> list[list[Any]]:
    def mark(ok):
        return "—" if ok is None else ("✅" if ok else "❌")
    pf = unclean(m.get("profit_factor"))
    rows = [
        ("Expectancy, USDT", m.get("expectancy"), f"> {GATE.min_expectancy}",
         None if m.get("expectancy") is None else m["expectancy"] > GATE.min_expectancy),
        ("Profit factor", pf, f">= {GATE.min_profit_factor}",
         None if pf is None else pf >= GATE.min_profit_factor),
        ("Sharpe", m.get("sharpe"), f">= {GATE.min_sharpe}",
         None if m.get("sharpe") is None else m["sharpe"] >= GATE.min_sharpe),
        ("MDD, %", m.get("mdd_pct"), f"<= {GATE.max_mdd_pct}",
         None if m.get("mdd_pct") is None else m["mdd_pct"] <= GATE.max_mdd_pct),
        ("Сделок", m.get("trades"), f">= {GATE.min_trades}",
         None if m.get("trades") is None else m["trades"] >= GATE.min_trades),
        ("Доля издержек, %", m.get("cost_share_pct"), f"< {GATE.max_cost_share_pct}",
         None if m.get("cost_share_pct") is None else m["cost_share_pct"] < GATE.max_cost_share_pct),
        ("Макс. серия убытков", m.get("max_loss_streak"), f"<= {GATE.max_loss_streak}",
         None if m.get("max_loss_streak") is None else m["max_loss_streak"] <= GATE.max_loss_streak),
    ]
    return [[name, fmt(v), thr, mark(ok)] for name, v, thr, ok in rows]


def summarize(run: dict) -> dict:
    """Ключевые цифры прогона в одном словаре (для --json и для сравнения)."""
    m, ds = run["metrics"], run["dataset"]
    return {
        "run_id": run.get("run_id"), "label": run["config"].get("label"),
        "strategy": run["config"].get("strategy"), "params": run["config"].get("params"),
        "inst": ds.get("inst"), "bar": ds.get("bar"), "dataset_id": ds.get("dataset_id"),
        "period": [ds.get("used_first_ts"), ds.get("used_last_ts")],
        "return_pct": m.get("net_profit_pct"), "net_profit": m.get("net_profit"),
        "cagr_pct": m.get("cagr_pct"), "benchmark_pct": m.get("benchmark_pct"),
        "mdd_pct": m.get("mdd_pct"), "underwater_days": m.get("underwater_days"),
        "fees": m.get("fees"), "slippage": m.get("slippage"), "costs": m.get("costs"),
        "cost_share_pct": m.get("cost_share_pct"), "trades": m.get("trades"),
        "hit_rate_pct": m.get("hit_rate_pct"), "profit_factor": m.get("profit_factor"),
        "expectancy": m.get("expectancy"), "sharpe": m.get("sharpe"),
        "exposure_pct": m.get("exposure_pct"),
        "limitations": run.get("limitations") or limitations(run),
    }


def render(run: dict) -> str:
    m, ds, cfg = run["metrics"], run["dataset"], run["config"]
    s = summarize(run)
    lines = [f"# Сводка прогона {run.get('run_id')}", ""]
    if cfg.get("question"):
        lines += [f"**Вопрос:** {cfg['question']}", ""]
    lines += [
        f"- **Стратегия:** `{cfg['strategy']}` {cfg.get('params') or '(дефолты)'}; "
        f"эффективные параметры: {run.get('engine', {}).get('params_effective')}",
        f"- **Данные:** {ds['inst']} {ds['bar']}, dataset_id `{ds['dataset_id']}`, "
        f"{iso_ms(ds.get('used_first_ts'))} → {iso_ms(ds.get('used_last_ts'))} "
        f"({ds.get('bars_used')} баров из {ds.get('bars_total')})",
        f"- **Издержки:** taker {fmt(run['costs']['taker_fee'] * 100, 3)}%, maker "
        f"{fmt(run['costs']['maker_fee'] * 100, 3)}%, проскальзывание "
        f"{fmt(run['costs']['slippage_bps'], 1)} бп/сторону",
        f"- **Стартовый капитал:** {fmt(m.get('initial'))} USDT", "",
        "## Результат", "",
        md_table(["Метрика", "Значение"], [
            ["Доходность, %", fmt(s["return_pct"])],
            ["Чистая прибыль, USDT", fmt(s["net_profit"])],
            ["CAGR, %", fmt(s["cagr_pct"])],
            ["Рынок за период (benchmark), %", fmt(s["benchmark_pct"])],
            ["Макс. просадка (от HWM кривой), %", fmt(s["mdd_pct"])],
            ["Дней «под водой» (макс.)", fmt(s["underwater_days"], 1)],
            ["Комиссии, USDT", fmt(s["fees"])],
            ["Проскальзывание, USDT", fmt(s["slippage"])],
            ["Доля издержек в брутто-PnL, %", fmt(s["cost_share_pct"])],
            ["Сделок", fmt(s["trades"])],
            ["Доля прибыльных, %", fmt(s["hit_rate_pct"])],
            ["Profit factor", fmt(s["profit_factor"])],
            ["Expectancy, USDT/сделка", fmt(s["expectancy"])],
            ["Sharpe (дневной, √365)", fmt(s["sharpe"])],
            ["Время в позиции, %", fmt(s["exposure_pct"])],
        ]), "",
    ]
    if run.get("exit_reasons"):
        lines += ["**Причины выхода:** " + ", ".join(f"{k} — {v}" for k, v in
                                                     sorted(run["exit_reasons"].items())), ""]
    lines += ["## Пороги гейта (информативно, не приёмка)", "", md_table(
        ["Проверка", "Значение", "Порог", ""], _gate_rows(m)), "",
        "## Ограничения", ""]
    lines += [f"- {x}" for x in s["limitations"]]
    return "\n".join(lines) + "\n"
