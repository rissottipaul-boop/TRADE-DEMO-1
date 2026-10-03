"""П. 35 — поиск противоречий в гипотезе: что поддерживает сигнал, что противоречит, чего нет.

Гипотеза формализуется как событие на закрытии бара + горизонт удержания. Расчёт —
event study по локальным свечам data/market_data.db (без сети, датасет не регистрируется):
вход по open(i+1) — как в бэктестере, выход по close(i+horizon); net = gross − круг
издержек CostModel (2 × (taker + проскальзывание)). Holdout (последние 182 дня) по
умолчанию исключён — его можно потратить только осознанно (--include-holdout).

Спецификация (JSON или аргументы CLI):
  {"claim": "RSI<30 на BTC 1H даёт отскок за 24 часа", "inst": "BTC-USDT", "bar": "1H",
   "signal": "rsi_below", "params": {"level": 30}, "horizon": 24, "since": "2022-01-01",
   "until": null, "stop_pct": 2.0, "factors": ["funding"], "include_holdout": false}
Сигналы: rsi_below/rsi_above (n, level, cross), sma_cross_up/sma_cross_down (fast, slow),
breakout (n), bb_lower (n, k), impulse (pct, vol_ratio, vol_n).
factors — на что ещё опирается гипотеза; чего нет в market_data.db (funding, OI, стакан,
новости, ончейн) — уходит в «чего не хватает».
"""
from __future__ import annotations

import math
import statistics
from datetime import datetime, timezone
from typing import Any, Optional

from src.analytics.common import fmt, iso_ms, md_table
from src.backtest.data import BAR_MS, DB_PATH, MarketDataStore, iso_to_ms, load_dataset
from src.backtest.engine import CostModel
from src.backtest.indicators import bollinger, is_nan, rsi_wilder, sma
from src.backtest.walkforward import holdout_start

MIN_EVENTS = 30
LOCAL_FACTORS = {"price", "volume", "rsi", "sma", "bb", "ohlc", "цена", "объём", "объем"}
SIGNALS = {
    "rsi_below": {"n": 14, "level": 30.0, "cross": True},
    "rsi_above": {"n": 14, "level": 70.0, "cross": True},
    "sma_cross_up": {"fast": 20, "slow": 100},
    "sma_cross_down": {"fast": 20, "slow": 100},
    "breakout": {"n": 20},
    "bb_lower": {"n": 20, "k": 2.0},
    "impulse": {"pct": 1.5, "vol_ratio": 1.5, "vol_n": 20},
}


class HypothesisError(ValueError):
    pass


def signal_mask(name: str, params: dict, bars) -> tuple[list[bool], int]:
    """Флаги события на закрытии каждого бара и прогрев (баров без сигнала)."""
    if name not in SIGNALS:
        raise HypothesisError(f"signal={name!r}: доступно {', '.join(SIGNALS)}")
    p = {**SIGNALS[name], **(params or {})}
    unknown = set(params or {}) - set(SIGNALS[name])
    if unknown:
        raise HypothesisError(f"params {sorted(unknown)}: у {name} есть {sorted(SIGNALS[name])}")
    c = [b.c for b in bars]
    n = len(bars)
    mask = [False] * n
    if name in ("rsi_below", "rsi_above"):
        r = rsi_wilder(c, int(p["n"]))
        lvl = float(p["level"])
        below = name == "rsi_below"
        for i in range(1, n):
            if is_nan(r[i]) or is_nan(r[i - 1]):
                continue
            now = r[i] < lvl if below else r[i] > lvl
            before = r[i - 1] < lvl if below else r[i - 1] > lvl
            mask[i] = now and (not before or not p["cross"])
        return mask, int(p["n"]) * 2
    if name in ("sma_cross_up", "sma_cross_down"):
        if not 0 < int(p["fast"]) < int(p["slow"]):
            raise HypothesisError("нужно 0 < fast < slow")
        f, s = sma(c, int(p["fast"])), sma(c, int(p["slow"]))
        for i in range(1, n):
            if any(is_nan(x) for x in (f[i], s[i], f[i - 1], s[i - 1])):
                continue
            up = f[i - 1] <= s[i - 1] and f[i] > s[i]
            down = f[i - 1] >= s[i - 1] and f[i] < s[i]
            mask[i] = up if name == "sma_cross_up" else down
        return mask, int(p["slow"])
    if name == "breakout":
        k = int(p["n"])
        for i in range(k, n):
            mask[i] = c[i] > max(b.h for b in bars[i - k:i])
        return mask, k
    if name == "bb_lower":
        mid, up, lo = bollinger(c, int(p["n"]), float(p["k"]))
        for i in range(n):
            mask[i] = not is_nan(lo[i]) and c[i] < lo[i]
        return mask, int(p["n"])
    vols = [b.vol for b in bars]
    vs = sma(vols, int(p["vol_n"]))
    for i in range(1, n):
        if is_nan(vs[i - 1]) or vs[i - 1] <= 0:
            continue
        chg = (c[i] / c[i - 1] - 1) * 100
        mask[i] = chg >= float(p["pct"]) and vols[i] / vs[i - 1] >= float(p["vol_ratio"])
    return mask, int(p["vol_n"]) + 1


def _stats(xs: list[float]) -> dict:
    if not xs:
        return {"n": 0, "mean": None, "median": None, "hit": None, "t": None}
    mean = statistics.fmean(xs)
    sd = statistics.stdev(xs) if len(xs) > 1 else 0.0
    t = mean / (sd / math.sqrt(len(xs))) if sd > 0 else None
    return {"n": len(xs), "mean": mean, "median": statistics.median(xs),
            "hit": sum(1 for x in xs if x > 0) / len(xs) * 100, "t": t}


def analyze(spec: dict, db_path=DB_PATH) -> dict:
    inst = str(spec.get("inst", "BTC-USDT")).upper()
    bar = spec.get("bar", "1H")
    if bar not in BAR_MS:
        raise HypothesisError(f"bar={bar!r}: допустимо {', '.join(BAR_MS)}")
    horizon = int(spec.get("horizon", 24))
    if horizon < 1:
        raise HypothesisError("horizon ≥ 1 бара")
    store = MarketDataStore(db_path)
    if not store.ts_range(inst, bar)[2]:
        raise HypothesisError(f"нет свечей {inst} {bar} — `python -m src.backtest download "
                              f"--inst {inst} --bar {bar} --since 2022-01-01`")
    since = iso_to_ms(spec.get("since") or "2022-01-01")
    until = iso_to_ms(spec["until"]) if spec.get("until") else None
    ds = load_dataset(store, inst, bar, start_ms=since, end_ms=until, register=False)
    bars = ds.bars
    h_idx, h_ts = holdout_start([b.ts for b in bars], ds.bar_ms, 182)
    include_holdout = bool(spec.get("include_holdout", False))
    if not include_holdout:
        bars = bars[:h_idx]
    if len(bars) < horizon + 50:
        raise HypothesisError(f"мало баров ({len(bars)}) для горизонта {horizon}")
    mask, warm = signal_mask(spec.get("signal", "rsi_below"), spec.get("params") or {}, bars)
    costs = CostModel()
    rt_cost = 2 * (costs.taker_fee + costs.slip) * 100     # % за круг
    stop_pct = spec.get("stop_pct")
    no_overlap = spec.get("no_overlap", True)
    sma200 = sma([b.c for b in bars], 200)
    events = []
    last_exit = -1
    for i in range(warm, len(bars) - horizon):
        if not mask[i] or (no_overlap and i <= last_exit):
            continue
        entry = bars[i + 1].o
        exit_px = bars[i + horizon].c
        window = bars[i + 1:i + horizon + 1]
        mae = (min(b.l for b in window) / entry - 1) * 100
        mfe = (max(b.h for b in window) / entry - 1) * 100
        gross = (exit_px / entry - 1) * 100
        regime = None if is_nan(sma200[i]) else ("выше SMA200" if bars[i].c > sma200[i] else "ниже SMA200")
        events.append({"ts": bars[i].ts, "gross": gross, "net": gross - rt_cost, "mae": mae,
                       "mfe": mfe, "regime": regime,
                       "year": datetime.fromtimestamp(bars[i].ts / 1000, tz=timezone.utc).year})
        last_exit = i + horizon
    base = [(bars[j + horizon].c / bars[j + 1].o - 1) * 100 for j in range(warm, len(bars) - horizon)]
    net = [e["net"] for e in events]
    st = _stats(net)
    st_gross = _stats([e["gross"] for e in events])
    base_mean = statistics.fmean(base) if base else None
    half = len(events) // 2
    halves = [_stats(net[:half]), _stats(net[half:])] if len(events) >= 2 else []
    years: dict[int, list[float]] = {}
    regimes: dict[str, list[float]] = {}
    for e in events:
        years.setdefault(e["year"], []).append(e["net"])
        if e["regime"]:
            regimes.setdefault(e["regime"], []).append(e["net"])
    by_year = {y: _stats(v) for y, v in sorted(years.items())}
    by_regime = {k: _stats(v) for k, v in regimes.items()}
    stop_hit = (sum(1 for e in events if e["mae"] <= -float(stop_pct)) / len(events) * 100
                if stop_pct and events else None)

    support, contra, missing, context = [], [], [], []
    if st["n"]:
        (support if st["mean"] > 0 else contra).append(
            f"средний net-результат события {fmt(st['mean'], 3)}% за {horizon} бар. "
            f"(после издержек круга {rt_cost:.2f}%), медиана {fmt(st['median'], 3)}%")
        (support if st["hit"] > 50 else contra).append(f"доля прибыльных событий {fmt(st['hit'], 1)}%")
        if base_mean is not None:
            edge = st_gross["mean"] - base_mean
            (support if edge > 0 else contra).append(
                f"превышение над фоном (любой бар, тот же горизонт, брутто {fmt(base_mean, 3)}%): "
                f"{fmt(edge, 3)} п.п.")
        if st["t"] is not None:
            (support if st["t"] >= 2 else contra if st["t"] <= 0 else missing).append(
                f"t-статистика среднего {fmt(st['t'])} " +
                ("(≥ 2 — устойчиво)" if st["t"] >= 2 else "(< 2 — неотличимо от шума)"))
        neg_years = [str(y) for y, s in by_year.items() if s["n"] and s["mean"] <= 0]
        pos_years = [str(y) for y, s in by_year.items() if s["n"] and s["mean"] > 0]
        if pos_years:
            support.append(f"плюс по годам: {', '.join(pos_years)}")
        if neg_years:
            contra.append(f"минус по годам: {', '.join(neg_years)}")
        if len(halves) == 2 and halves[0]["mean"] is not None and halves[1]["mean"] is not None:
            if (halves[0]["mean"] > 0) != (halves[1]["mean"] > 0):
                contra.append(f"знак меняется между половинами выборки: {fmt(halves[0]['mean'], 3)}% → "
                              f"{fmt(halves[1]['mean'], 3)}%")
            else:
                (support if halves[0]["mean"] > 0 else contra).append(
                    f"обе половины выборки одного знака: {fmt(halves[0]['mean'], 3)}% и {fmt(halves[1]['mean'], 3)}%")
        for k, s in by_regime.items():
            if s["n"] >= 5:
                (support if s["mean"] > 0 else contra).append(
                    f"режим {k}: {s['n']} соб., средний net {fmt(s['mean'], 3)}%")
        if stop_hit is not None:
            context.append(
                f"стоп −{stop_pct}% был бы задет внутри горизонта в {fmt(stop_hit, 1)}% событий "
                f"(средний MAE {fmt(statistics.fmean(e['mae'] for e in events), 2)}%, "
                f"средний MFE {fmt(statistics.fmean(e['mfe'] for e in events), 2)}%)")
    if st["n"] < MIN_EVENTS:
        missing.append(f"мало событий: {st['n']} (< {MIN_EVENTS}) — выводы статистически слабые")
    if not include_holdout:
        missing.append(f"holdout с {iso_ms(h_ts)} не проверен — независимая проверка ещё доступна")
    if ds.gaps:
        missing.append(f"дыры в данных: {len(ds.gaps)} (пропущено свечей {sum(g.missing for g in ds.gaps)})")
    for f in spec.get("factors") or []:
        if str(f).lower() not in LOCAL_FACTORS:
            missing.append(f"фактор «{f}» в гипотезе, но его нет в market_data.db — не проверен "
                           "(funding: `python -m src.funding_archive status`)")
    missing.append("один инструмент и одна постановка: нет проверки на других парах и таймфреймах")
    missing.append("event study без риск-ядра и сайзинга: для решения нужен бэктест (bt-draft → bt-run)")
    verdict = ("сигнал поддержан данными" if st["n"] >= MIN_EVENTS and st["mean"] and st["mean"] > 0
               and (st["t"] or 0) >= 2 else
               "данные противоречат" if st["n"] >= MIN_EVENTS and st["mean"] is not None and st["mean"] <= 0
               else "недостаточно данных для вывода")
    return {"claim": spec.get("claim"), "inst": inst, "bar": bar, "signal": spec.get("signal"),
            "params": {**SIGNALS.get(spec.get("signal"), {}), **(spec.get("params") or {})},
            "horizon": horizon, "period": [bars[0].ts, bars[-1].ts], "bars": len(bars),
            "dataset_id": ds.dataset_id, "roundtrip_cost_pct": rt_cost, "events": st["n"],
            "net": st, "gross": st_gross, "baseline_gross_mean": base_mean, "halves": halves,
            "by_year": by_year, "by_regime": by_regime, "stop_hit_pct": stop_hit,
            "supports": support, "contradicts": contra, "missing": missing, "context": context, "verdict": verdict}


def render(r: dict) -> str:
    lines = [f"# Проверка гипотезы: {r.get('claim') or r['signal']}", "",
             f"- **Постановка:** {r['inst']} {r['bar']}, сигнал `{r['signal']}` {r['params']}, "
             f"горизонт {r['horizon']} бар., вход open(i+1), выход close(i+h)",
             f"- **Данные:** {iso_ms(r['period'][0])} → {iso_ms(r['period'][1])}, {r['bars']} баров, "
             f"dataset_id `{r['dataset_id']}`; издержки круга {r['roundtrip_cost_pct']:.2f}%",
             f"- **Событий:** {r['events']}; **итог:** {r['verdict']}", "",
             "## Поддерживает", ""] + [f"- {x}" for x in r["supports"] or ["—"]]
    lines += ["", "## Противоречит", ""] + [f"- {x}" for x in r["contradicts"] or ["—"]]
    if r.get("context"):
        lines += ["", "## Контекст и движение внутри горизонта", ""] + [f"- {x}" for x in r["context"]]
    lines += ["", "## Чего не хватает", ""] + [f"- {x}" for x in r["missing"]]
    lines += ["", "## По годам", "",
              md_table(["Год", "Событий", "Средний net, %", "Доля плюс, %"],
                       [[y, s["n"], fmt(s["mean"], 3), fmt(s["hit"], 1)] for y, s in r["by_year"].items()])]
    return "\n".join(lines) + "\n"
