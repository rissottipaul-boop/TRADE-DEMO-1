"""П. 32 — сравнение двух прогонов: чем отличаются версии стратегии и условия теста.

Разница результата раскладывается на то, что можно назвать по данным прогонов:
  - условия: другие данные (dataset_id, период, бар, инструмент), holdout, издержки, капитал;
  - версия стратегии: другая стратегия или другие параметры;
  - механика: число сделок, время в позиции, причины выхода, отказы риск-ядра;
  - арифметика PnL: Δnet = Δбрутто − Δиздержек (брутто = net + комиссии + проскальзывание).
Если отличаются и данные, и стратегия, эффект стратегии от эффекта рынка не отделить —
сравнение помечается как несопоставимое, и это первым пунктом в объяснении.
"""
from __future__ import annotations

from typing import Any

from src.analytics.bt_summary import summarize
from src.analytics.common import fmt, iso_ms, md_table, num

KEY_METRICS = [("net_profit_pct", "Доходность, %"), ("mdd_pct", "MDD, %"),
               ("sharpe", "Sharpe"), ("profit_factor", "Profit factor"),
               ("trades", "Сделок"), ("hit_rate_pct", "Доля прибыльных, %"),
               ("expectancy", "Expectancy, USDT"), ("fees", "Комиссии, USDT"),
               ("slippage", "Проскальзывание, USDT"), ("cost_share_pct", "Доля издержек, %"),
               ("exposure_pct", "Время в позиции, %"), ("benchmark_pct", "Рынок, %")]


def _flatten(obj: Any, prefix: str = "") -> dict[str, Any]:
    if isinstance(obj, dict):
        out: dict[str, Any] = {}
        for k, v in obj.items():
            out.update(_flatten(v, f"{prefix}.{k}" if prefix else str(k)))
        return out
    return {prefix: obj}


def config_diff(a: dict, b: dict) -> list[dict]:
    skip = {"question", "label", "assumptions", "understood"}
    fa = _flatten({k: v for k, v in a.items() if k not in skip})
    fb = _flatten({k: v for k, v in b.items() if k not in skip})
    return [{"key": k, "a": fa.get(k), "b": fb.get(k)}
            for k in sorted(set(fa) | set(fb)) if fa.get(k) != fb.get(k)]


def _delta(a: Any, b: Any):
    x, y = num(a), num(b)
    return None if x is None or y is None else y - x


def compare(run_a: dict, run_b: dict) -> dict:
    ca, cb = run_a["config"], run_b["config"]
    da, db = run_a["dataset"], run_b["dataset"]
    ma, mb = run_a["metrics"], run_b["metrics"]
    diff = config_diff(ca, cb)
    same_data = da.get("dataset_id") == db.get("dataset_id") and \
        da.get("used_first_ts") == db.get("used_first_ts") and \
        da.get("used_last_ts") == db.get("used_last_ts")
    same_costs = run_a.get("costs") == run_b.get("costs")
    same_strategy = ca.get("strategy") == cb.get("strategy")
    same_params = (run_a.get("engine") or {}).get("params_effective") == \
        (run_b.get("engine") or {}).get("params_effective")
    explain: list[str] = []
    if not same_data and not (same_strategy and same_params):
        explain.append("НЕСОПОСТАВИМО напрямую: отличаются и данные, и стратегия — эффект "
                       "стратегии смешан с эффектом рынка. Прогоните обе версии на одном периоде.")
    if not same_data:
        parts = []
        for key, name in (("inst", "инструмент"), ("bar", "бар")):
            if da.get(key) != db.get(key):
                parts.append(f"{name} {da.get(key)} → {db.get(key)}")
        if (da.get("used_first_ts"), da.get("used_last_ts")) != (db.get("used_first_ts"), db.get("used_last_ts")):
            parts.append(f"период {iso_ms(da.get('used_first_ts'))}…{iso_ms(da.get('used_last_ts'))} → "
                         f"{iso_ms(db.get('used_first_ts'))}…{iso_ms(db.get('used_last_ts'))}")
        if da.get("dataset_id") != db.get("dataset_id"):
            parts.append(f"dataset_id {da.get('dataset_id')} → {db.get('dataset_id')}")
        bench = _delta(ma.get("benchmark_pct"), mb.get("benchmark_pct"))
        explain.append("Условия: другие данные (" + "; ".join(parts) + ")" +
                       (f"; рынок за период изменился на {fmt(bench)} п.п." if bench else ""))
    hold_a = (da.get("holdout") or {}).get("excluded", True)
    hold_b = (db.get("holdout") or {}).get("excluded", True)
    if hold_a != hold_b:
        explain.append("Условия: holdout исключён в одном прогоне и включён в другом — "
                       "периоды разной длины, второй включает «отложенные» данные")
    if not same_costs:
        ka, kb = run_a.get("costs") or {}, run_b.get("costs") or {}
        changed = [f"{k} {ka.get(k)} → {kb.get(k)}" for k in sorted(set(ka) | set(kb))
                   if ka.get(k) != kb.get(k)]
        explain.append("Условия: другие издержки (" + ", ".join(changed) + ")")
    if ca.get("initial_cash") != cb.get("initial_cash"):
        explain.append("Условия: другой стартовый капитал — проценты сопоставимы, USDT нет "
                       "(округление объёма к lotSz тоже меняется)")
    if not same_strategy:
        explain.append(f"Версия: другая стратегия {ca.get('strategy')} → {cb.get('strategy')}")
    elif not same_params:
        pa = (run_a.get("engine") or {}).get("params_effective") or {}
        pb = (run_b.get("engine") or {}).get("params_effective") or {}
        changed = [f"{k} {pa.get(k)} → {pb.get(k)}" for k in sorted(set(pa) | set(pb))
                   if pa.get(k) != pb.get(k)]
        explain.append("Версия: другие параметры (" + ", ".join(changed) + ")")
    d_net = _delta(ma.get("net_profit"), mb.get("net_profit"))
    d_gross = _delta(ma.get("gross_pnl"), mb.get("gross_pnl"))
    d_costs = _delta(ma.get("costs"), mb.get("costs"))
    if d_net is not None and d_gross is not None and d_costs is not None:
        explain.append(f"Арифметика PnL сделок: Δбрутто {fmt(d_gross)} − Δиздержек "
                       f"{fmt(d_costs)} = {fmt(d_gross - d_costs)} USDT; Δчистая прибыль "
                       f"по кривой {fmt(d_net)} USDT")
        if d_costs and d_net and abs(d_costs) >= 0.5 * abs(d_net):
            explain.append("Издержки объясняют не меньше половины разницы результата")
    d_tr = _delta(ma.get("trades"), mb.get("trades"))
    if d_tr:
        explain.append(f"Механика: сделок {fmt(ma.get('trades'))} → {fmt(mb.get('trades'))}; "
                       f"время в позиции {fmt(ma.get('exposure_pct'))}% → {fmt(mb.get('exposure_pct'))}%")
    ra, rb = run_a.get("exit_reasons") or {}, run_b.get("exit_reasons") or {}
    if ra != rb:
        explain.append("Механика: причины выхода " + ", ".join(
            f"{k} {ra.get(k, 0)} → {rb.get(k, 0)}" for k in sorted(set(ra) | set(rb))
            if ra.get(k, 0) != rb.get(k, 0)))
    ja, jb = run_a.get("rejections") or {}, run_b.get("rejections") or {}
    if ja != jb:
        explain.append("Риск-ядро: отказы входа " + ", ".join(
            f"{k} {ja.get(k, 0)} → {jb.get(k, 0)}" for k in sorted(set(ja) | set(jb))))
    if same_data and same_costs and same_strategy and same_params:
        explain.append("Конфигурации эквивалентны: разница должна быть нулевой (детерминированный "
                       "движок); ненулевая — повод проверить версию кода бэктестера")
    metrics = [{"key": k, "name": name, "a": ma.get(k), "b": mb.get(k),
                "delta": _delta(ma.get(k), mb.get(k))} for k, name in KEY_METRICS]
    return {"a": summarize(run_a)["run_id"], "b": summarize(run_b)["run_id"],
            "comparable": same_data and same_costs, "same_data": same_data,
            "same_costs": same_costs, "same_strategy": same_strategy, "same_params": same_params,
            "config_diff": diff, "metrics": metrics, "explanation": explain}


def render(cmp: dict, run_a: dict, run_b: dict) -> str:
    la = run_a["config"].get("label") or cmp["a"]
    lb = run_b["config"].get("label") or cmp["b"]
    lines = [f"# Сравнение прогонов: A `{cmp['a']}` и B `{cmp['b']}`", "",
             f"- **A:** {la}", f"- **B:** {lb}",
             f"- **Сопоставимость:** {'одни данные и издержки' if cmp['comparable'] else 'условия теста различаются'}",
             "", "## Метрики", "",
             md_table(["Метрика", "A", "B", "B − A"],
                      [[r["name"], fmt(r["a"]), fmt(r["b"]), fmt(r["delta"])] for r in cmp["metrics"]]),
             "", "## Чем объясняется разница", ""]
    lines += [f"- {x}" for x in cmp["explanation"]] or ["- отличий не найдено"]
    if cmp["config_diff"]:
        lines += ["", "## Отличия конфигурации", "",
                  md_table(["Поле", "A", "B"], [[d["key"], d["a"], d["b"]] for d in cmp["config_diff"]])]
    return "\n".join(lines) + "\n"
