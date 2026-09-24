"""Mean-reversion RSI/BB, long-only спот (MEANREV-IMPL, insights/meanrev-strategy-design.md).

Правила дизайна (стартовые дефолты, калибровка — MEANREV-CALIB):
- вход (§3) на закрытии подтверждённого бара t, исполнение по open(t+1):
  RSI(14) Уайлдера пересекает 30 снизу вверх, close(t) <= BB-middle(t), close(t) > close(t-1),
  volume(t) > 0. BB(20, 2) строится от typical price (H+L+C)/3, stdev с ddof=0;
- стоп (§5): close(t) − 1.5 × ATR(14)(t), фиксируется на сигнальном баре и больше не
  двигается; размер — risk.size_position через бэктестер (1% риска, потолок позиции 15%
  equity — риск-ядро обрезает размер, а не отклоняет вход);
- выход, приоритет (§6.3): стоп (внутри бара, движок) → minimal ROI → тайм-стоп → сигнал
  RSI(14) пересекает 70 снизу вверх, close(t) >= BB-middle(t), close(t) < close(t-1).
  ROI и сигнал решаются на close(t), исполнение по open(t+1);
- ROI-таблица (§6.2): минут в сделке → требуемая доходность (0: 2%, 240: 1.2%, 720: 0.6%,
  1440: 0%). Доходность — нетто: вход с комиссией и проскальзыванием, выход минус
  оценочная taker-комиссия (roi_fee). Тайм-стоп 24ч: сделка, не взявшая ROI за 1440 минут,
  закрывается при любом знаке PnL (§6.2 п.2 дизайна).

Отчёт на реальных данных: python -m src.backtest.meanrev --out insights/meanrev-backtest.md
(офлайн, данные — data/market_data.db; ордеров нет, src/engine.py не трогается).
"""
from __future__ import annotations

import argparse
import logging
import sys
import time
from collections import defaultdict
from datetime import datetime, timezone
from typing import Optional, Sequence

from src import risk
from src.backtest.data import Bar
from src.backtest.engine import Context, CostModel, Strategy
from src.backtest.indicators import (atr_wilder, bollinger, crossed_above, is_nan, rsi_wilder,
                                     typical_price)

# (минут в сделке, требуемая доходность %) — §6.2 дизайна
DEFAULT_ROI: tuple[tuple[int, float], ...] = ((0, 2.0), (240, 1.2), (720, 0.6), (1440, 0.0))
TIME_STOP_MIN = 1440
WARMUP = 200  # §2.3 дизайна: startup_candle_count Freqtrade, запас над формальным 2×BB(20)


def roi_required(table: Sequence[tuple[int, float]], minutes: float) -> Optional[float]:
    """Требуемая доходность (%) после minutes в сделке: строка с наибольшим ключом <= minutes."""
    req = None
    for m, pct in sorted(table):
        if minutes >= m:
            req = pct
    return req


def entry_signal(rsi: Sequence[float], mid: Sequence[float], bars: Sequence[Bar], i: int,
                 level: float = 30.0) -> bool:
    """§3: crossed_above(RSI, level) и close <= mid и close > close[-1] и volume > 0."""
    if i < 1 or is_nan(mid[i]):
        return False
    b, prev = bars[i], bars[i - 1]
    return (crossed_above(rsi, level, i) and b.c <= mid[i] and b.c > prev.c and b.vol > 0)


def exit_signal(rsi: Sequence[float], mid: Sequence[float], bars: Sequence[Bar], i: int,
                level: float = 70.0, price_guard: bool = True) -> bool:
    """§4: crossed_above(RSI, level) и close >= mid и close < close[-1].

    Внимание: для RSI Уайлдера от close пересечение вверх возможно только на баре с
    close > close[-1], поэтому с price_guard условие не выполняется никогда (см. тест
    test_meanrev.ExitSignalTest и отчёт). price_guard=False — вариант без этого guard'а.
    """
    if i < 1 or is_nan(mid[i]):
        return False
    b, prev = bars[i], bars[i - 1]
    guard = b.c < prev.c if price_guard else True
    return crossed_above(rsi, level, i) and b.c >= mid[i] and guard


class MeanReversion(Strategy):
    name = "meanrev"
    entry_inputs = ("rsi", "bb_mid", "atr")

    def __init__(self, rsi_n: int = 14, rsi_entry: float = 30.0, rsi_exit: float = 70.0,
                 bb_n: int = 20, bb_k: float = 2.0, bb_ddof: int = 0, atr_n: int = 14,
                 stop_atr_mult: float = 1.5,
                 roi: Sequence[tuple[int, float]] = DEFAULT_ROI,
                 time_stop_min: Optional[float] = TIME_STOP_MIN,
                 risk_pct: float = risk.DEFAULT_RISK_PCT,
                 roi_fee: float = CostModel().taker_fee,
                 exit_price_guard: bool = True, warmup: int = WARMUP):
        if stop_atr_mult <= 0:
            raise ValueError("stop_atr_mult > 0")
        super().__init__(rsi_n=rsi_n, rsi_entry=rsi_entry, rsi_exit=rsi_exit, bb_n=bb_n,
                         bb_k=bb_k, bb_ddof=bb_ddof, atr_n=atr_n, stop_atr_mult=stop_atr_mult,
                         roi=tuple(tuple(r) for r in roi), time_stop_min=time_stop_min,
                         risk_pct=risk_pct, roi_fee=roi_fee,
                         exit_price_guard=exit_price_guard, warmup=warmup)
        # не меньше формального минимума §1.1 п.3 бэктестера: 2 × максимальное окно
        self.warmup = max(warmup, 2 * max(bb_n, rsi_n + 1, atr_n + 1))

    def prepare(self, bars: Sequence[Bar]) -> dict[str, list[float]]:
        p = self.params
        h, lo, c = [b.h for b in bars], [b.l for b in bars], [b.c for b in bars]
        mid, upper, lower = bollinger(typical_price(h, lo, c), p["bb_n"], p["bb_k"],
                                      p["bb_ddof"])
        return {"rsi": rsi_wilder(c, p["rsi_n"]), "bb_mid": mid, "bb_upper": upper,
                "bb_lower": lower, "atr": atr_wilder(h, lo, c, p["atr_n"])}

    def net_return(self, ctx: Context) -> float:
        """Нетто-доходность позиции при выходе по close(t) с taker-комиссией (доля)."""
        pos = ctx.position
        return pos.qty * ctx.bar.c * (1.0 - self.params["roi_fee"]) / pos.entry_cost - 1.0

    def on_bar(self, ctx: Context) -> None:
        p, i = self.params, ctx.i
        if ctx.has_pending:
            return
        rsi, mid = ctx.series("rsi"), ctx.series("bb_mid")
        pos = ctx.position
        if pos is not None:
            minutes = (ctx.time_ms - pos.entry_ts) / 60_000
            req = roi_required(p["roi"], minutes)
            if req is not None and self.net_return(ctx) >= req / 100.0:
                ctx.close("roi")
            elif p["time_stop_min"] is not None and minutes >= p["time_stop_min"]:
                ctx.close("time_stop")
            elif exit_signal(rsi, mid, ctx.bars, i, p["rsi_exit"], p["exit_price_guard"]):
                ctx.close("rsi_exit")
            return
        if not ctx.trading_allowed or not entry_signal(rsi, mid, ctx.bars, i, p["rsi_entry"]):
            return
        atr = ctx.ind("atr")
        ctx.buy(stop=ctx.bar.c - p["stop_atr_mult"] * atr, risk_pct=p["risk_pct"],
                tag="rsi_bb_long")


# --- Отчёт на реальных данных ---

EXIT_ORDER = ("stop_loss", "roi", "time_stop", "rsi_exit", "end_of_data")


def exit_breakdown(trades) -> list[tuple[str, int, int, float, float]]:
    """[(причина, сделок, прибыльных, PnL USDT, средний PnL %)] по причинам выхода."""
    acc: dict[str, list] = defaultdict(list)
    for t in trades:
        acc[t.exit_tag if t.exit_reason == "signal" else t.exit_reason].append(t)
    keys = [k for k in EXIT_ORDER if k in acc] + sorted(k for k in acc if k not in EXIT_ORDER)
    return [(k, len(acc[k]), sum(1 for t in acc[k] if t.pnl > 0),
             sum(t.pnl for t in acc[k]), sum(t.pnl_pct for t in acc[k]) / len(acc[k]) * 100.0)
            for k in keys]


def sizing_summary(result, bars: Sequence[Bar]) -> dict:
    """Влияние потолка позиции: доля входов, где 1% риска при стопе k×ATR требовал бы
    notional > risk.MAX_POSITION_PCT, и медиана фактического риска (% equity)."""
    idx = {b.ts: i for i, b in enumerate(bars)}
    atr, p = result.indicators["atr"], result.params
    capped, eff = 0, []
    for t in result.trades:
        i = idx[t.entry_signal_ts]
        dist_pct = p["stop_atr_mult"] * atr[i] / bars[i].c * 100.0
        want = p["risk_pct"] / dist_pct * 100.0  # notional % equity без потолка
        if want > risk.MAX_POSITION_PCT:
            capped += 1
        eff.append(min(want, risk.MAX_POSITION_PCT) * dist_pct / 100.0)
    eff.sort()
    n = len(eff)
    return {"entries": n, "capped": capped,
            "median_risk_pct": (eff[n // 2] if n % 2 else (eff[n // 2 - 1] + eff[n // 2]) / 2)
            if n else None}


def _report(args) -> str:
    from src.backtest import report as R
    from src.backtest.__main__ import MIN_BARS, WF_CONFIG, _iso_arg, _resolve_until
    from src.backtest.analysis import lookahead_check, recursive_check
    from src.backtest.data import MarketDataStore, iso_to_ms, load_dataset, ms_to_iso
    from src.backtest.engine import Backtest
    from src.backtest.metrics import evaluate_gate
    from src.backtest.strategies import BuyAndHold
    from src.backtest.walkforward import holdout_start, walk_forward

    t0 = time.time()
    store = MarketDataStore(args.db)
    inst, bar = args.inst, args.bar
    since_ms = iso_to_ms(args.since)
    until_ms = _resolve_until(store, [inst], bar, args.until)
    spec = store.get_instrument(inst)
    if spec is None:
        raise SystemExit(f"нет спецификации {inst} в {args.db} — сначала download")
    ds = load_dataset(store, inst, bar, start_ms=since_ms, end_ms=until_ms)
    if len(ds.bars) < MIN_BARS:
        raise SystemExit(f"{inst}: мало данных ({len(ds.bars)} баров)")
    bars, cfg = ds.bars, WF_CONFIG
    h_idx = holdout_start([b.ts for b in bars], ds.bar_ms, cfg.holdout_days)[0]
    ins = bars[:h_idx]
    kw = dict(bar=bar, dataset_id=ds.dataset_id)
    base = CostModel()

    mr = Backtest(ins, MeanReversion(), spec, **kw).run()
    bh = Backtest(ins, BuyAndHold(), spec, trade_start=MeanReversion().warmup, **kw).run()
    no_guard = Backtest(ins, MeanReversion(exit_price_guard=False), spec, **kw).run()
    m_mr, m_bh, m_ng = mr.metrics(), bh.metrics(), no_guard.metrics()
    sens = [("meanrev", "fee×1, slip 5 бп (база)", m_mr)]
    for label, costs in (("fee×0 (брутто по комиссиям)", base.scaled(fee_mult=0.0)),
                         ("fee×1.5", base.scaled(fee_mult=1.5)),
                         ("slippage×2 (10 бп, стресс)", base.scaled(slip_mult=2.0))):
        sens.append(("meanrev", label, Backtest(ins, MeanReversion(), spec, costs=costs, **kw)
                     .run().metrics()))
    log.info("in-sample и чувствительность: %.1fс", time.time() - t0)

    t = time.time()
    wf = walk_forward(bars, MeanReversion, [{}], spec, cfg=cfg, **kw)
    wf_bh = walk_forward(bars, BuyAndHold, [{}], spec, cfg=cfg, embargo=MeanReversion().warmup,
                         **kw)
    if not wf.windows:
        raise SystemExit("нет ни одного окна walk-forward — мало истории до holdout")
    oos, oos_s = wf.oos.metrics(), wf.oos_stress.metrics()
    gate = evaluate_gate(oos, oos_s, [w.test.metrics() for w in wf.windows])
    gate_s = evaluate_gate(oos_s, oos_s, [w.test_stress.metrics() for w in wf.windows])
    bh_oos = wf_bh.oos.metrics()
    log.info("walk-forward: %.1fс (%d окон)", time.time() - t, len(wf.windows))

    t = time.time()
    la = lookahead_check(MeanReversion, ins, spec, max_points=args.la_points, bar=bar)
    rc = recursive_check(MeanReversion, ins, spec, offsets=args.rc_offsets,
                         rel_tol=args.rc_tol, bar=bar)
    log.info("anti-lookahead: %.1fс", time.time() - t)

    sz = sizing_summary(mr, ins)
    wf_tests = [w.test for w in wf.windows]
    exits = exit_breakdown(mr.trades)
    exits_oos = exit_breakdown(wf.oos.trades)
    trips = R.breaker_trips(mr)
    rsi_exit_n = sum(n for k, n, *_ in exits if k == "rsi_exit")
    ng_exits = {k: n for k, n, *_ in exit_breakdown(no_guard.trades)}
    failed = ", ".join(R.failed_checks(gate)) or "—"
    now = datetime.now(timezone.utc).astimezone()
    repro = (f".venv\\Scripts\\python.exe -m src.backtest.meanrev --inst {inst} --bar {bar} "
             f"--since {args.since} --until {_iso_arg(until_ms)} --la-points {args.la_points} "
             f"--rc-offsets {','.join(str(x) for x in args.rc_offsets)} --out {args.out or '-'}")

    def exit_rows(rows):
        return [[k, n, f"{w} ({w / n * 100:.0f}%)", R.f(pnl), R.f(avg, 3)]
                for k, n, w, pnl, avg in rows]

    rc_rows = []
    for x in rc["runs"]:
        if "skipped" in x:
            rc_rows.append([f"recursive, старт +{x['offset']}", "—", x["skipped"]])
            continue
        rc_rows.append([f"recursive, старт +{x['offset']}", R.f(x["ok"]),
                        f"макс. отн. расхождение индикаторов после прогрева "
                        f"{x['max_rel_diff']:.1e} (допуск {rc['rel_tol']:g}); сделок сравнено "
                        f"{x['trades_compared']}, идентичны: {R.f(x['trades_equal'])}"
                        + (f"; расхождений состояния риск-ядра: {len(x['risk_divergences'])}"
                           if x["risk_divergences"] else "")
                        + (f"; **{x['reason']}**" if x["reason"] else "")])

    L = [f"# Бэктест mean-reversion RSI/BB: {inst} {bar}", "",
         f"- **Дата:** {now:%Y-%m-%d %H:%M %z}",
         "- **Задача:** MEANREV-IMPL (дизайн `insights/meanrev-strategy-design.md`, метрики "
         "`insights/backtester-design.md` §6.1, гейт §6.2)",
         "- **Статус:** validated — отчёт сгенерирован кодом (`src/backtest/meanrev.py`), "
         "воспроизводим на том же dataset_id",
         f"- **Воспроизведение:** `{repro}`",
         f"- **Данные:** `data/market_data.db`, dataset_id `{ds.dataset_id}`, "
         f"{ms_to_iso(bars[0].ts)} → {ms_to_iso(bars[-1].ts)} UTC ({len(bars)} баров); "
         f"in-sample — до {ms_to_iso(bars[h_idx].ts) if h_idx < len(bars) else '—'} "
         f"({len(ins)} баров), holdout {cfg.holdout_days} д **не тронут**",
         f"- **Время прогона:** {time.time() - t0:.0f} с", "",
         "> Дефолтные параметры дизайна, без калибровки. Не финансовый совет.", "",
         "## Вывод", "",
         f"- **Гейт §6.2 на WF OOS: {'пройден' if gate['passed'] else 'не пройден'}** "
         f"(проваленные пороги: {failed}). OOS: net {R.f(oos['net_profit_pct'])}% при рынке "
         f"{R.f(oos['benchmark_pct'])}%, PF {R.f(oos['profit_factor'])}, Sharpe "
         f"{R.f(oos['sharpe'])}, Sortino {R.f(oos['sortino'])}, MDD {R.f(oos['mdd_pct'])}%, "
         f"сделок {oos['trades']}, hit rate {R.f(oos['hit_rate_pct'], 1)}%, expectancy "
         f"{R.f(oos['expectancy_pct'], 3)}% на сделку.",
         f"- In-sample (без holdout): net {R.f(m_mr['net_profit_pct'])}%, PF "
         f"{R.f(m_mr['profit_factor'])}, Sharpe {R.f(m_mr['sharpe'])}, MDD {R.f(m_mr['mdd_pct'])}%, "
         f"сделок {m_mr['trades']}, hit rate {R.f(m_mr['hit_rate_pct'], 1)}%, доля издержек в "
         "брутто-PnL " + (f"{R.f(m_mr['cost_share_pct'], 1)}%"
                          if m_mr["cost_share_pct"] is not None else "не определена (брутто ≤ 0)")
         + f". Buy&hold с тем же потолком 15%: net {R.f(m_bh['net_profit_pct'])}%.",
         "- Хуже внешних ориентиров §8 дизайна (Sharpe ~0–0.2, PF ~1.0, win rate 55–71%): "
         f"edge отрицателен и до комиссий — при fee×0 in-sample PF {R.f(sens[1][2]['profit_factor'])}. "
         "Асимметрия обратная ожидаемой: стоп 1.5×ATR срабатывает чаще, чем ROI 2% "
         "(см. «Причины выхода»). Первый шаг калибровки — MEANREV-CALIB.",
         f"- Сигнал выхода по RSI (§4) сработал {rsi_exit_n} раз: для RSI Уайлдера от close "
         "пересечение 70 снизу вверх бывает только на растущем close, а guard требует "
         "close(t) < close(t−1) — условие недостижимо (доказано тестом "
         "`tests/test_meanrev.py::ExitSignalTest`). Выходы — только стоп, ROI и тайм-стоп. "
         "На входе тот же guard close(t) > close(t−1) избыточен: пересечение 30 вверх его уже "
         "означает. Прогон без guard'а на выходе (`exit_price_guard=False`): выходов по RSI "
         f"{ng_exits.get('rsi_exit', 0)}, net {R.f(m_ng['net_profit_pct'])}%, PF "
         f"{R.f(m_ng['profit_factor'])}"
         + (" — то же, что с guard'ом: ROI-таблица закрывает сделку раньше, чем RSI доходит до "
            "70, так что при этих ROI сигнал §4 на результат не влияет."
            if not ng_exits.get("rsi_exit") else ".")
         + " Вопрос guard'а — в MEANREV-TEMA-CHECK (guard по TEMA(9) не вырожден).",
         f"- Сайзинг: у {sz['capped']} из {sz['entries']} входов 1% риска при стопе 1.5×ATR(14) "
         "требовал позицию больше потолка 15% equity — риск-ядро обрезало размер; медиана "
         f"фактического риска {R.f(sz['median_risk_pct'], 3)}% equity на сделку. Дизайн §5.2 "
         "говорил «вход отклоняется», `size_position()` обрезает — риск при этом только ниже.",
         *([f"- Риск-ядро: global_breaker (−{risk.GLOBAL_DD_LIMIT_PCT:g}% от HWM) сработал в "
            "in-sample " + ", ".join(f"{R.d(ts)} при просадке {R.f(dd)}%" for ts, dd in trips)
            + f", после него отклонено входов: {R.breaker_rejections(mr)}. Это реальная "
            "просадка кривой (после RISK-PNL-DOUBLE), не двойной учёт: in-sample после этой "
            "даты не торгует, поэтому net in-sample и строки чувствительности упираются в "
            "≈ −15%. "
            + ("WF OOS не затронут: каждое test-окно — отдельный прогон, breaker там не "
               "срабатывал." if not any(R.breaker_trips(w) for w in wf_tests) else
               "WF OOS тоже затронут — см. «Риск-контур».")] if trips else []), "",
         "## Модель прогона", "",
         "- Капитал 10 000 USDT, спот long-only, одна позиция. Сигнал на close подтверждённой "
         "свечи t → рыночное исполнение по open(t+1); стоп внутри бара «low раньше high».",
         "- Издержки: taker 0.10% на сторону, проскальзывание 5 бп/сторону; стресс — 10 бп.",
         "- Риск-ядро `src/risk.py` в модельном времени (in-memory): check_entry_allowed, "
         "size_position (1% риска, потолок 15%), лимиты входов, серии убытков, breaker'ы; "
         "equity и HWM ведёт только update_equity на каждом баре.",
         f"- Стратегия: RSI(14) Уайлдера 30/70, BB(20, 2) от typical price (ddof=0), стоп "
         f"close(t) − 1.5×ATR(14), ROI {dict(DEFAULT_ROI)} (минуты → %, нетто), тайм-стоп "
         f"{TIME_STOP_MIN} мин, прогрев {MeanReversion().warmup} баров.", "",
         "## In-sample: метрики §6.1", "",
         R.metrics_table([("meanrev", m_mr), ("buy&hold (15% equity)", m_bh)]), "",
         "### Причины выхода (in-sample)", "",
         R.table(["Причина", "Сделок", "Прибыльных", "PnL, USDT", "Средний PnL, %"],
                 exit_rows(exits)), "",
         "### Чувствительность к издержкам (§3.3)", "", R.sensitivity_table(sens), "",
         "### По годам (in-sample)", "",
         R.yearly_table(R.market_curve(ins), [("meanrev", mr), ("buy&hold", bh)]), "",
         "## Walk-forward (§2) и гейт §6.2", "",
         f"Окна rolling {cfg.train_days}/{cfg.valid_days}/{cfg.test_days} д, шаг "
         f"{cfg.step_days} д, окон {len(wf.windows)}; параметры фиксированы (сетка из одного "
         f"набора — оптимизации нет, trials = {wf.trials}); embargo {MeanReversion().warmup} "
         "баров; holdout не входит.", "",
         R.metrics_table([("meanrev WF OOS", oos), ("meanrev WF OOS, slip×2", oos_s),
                          ("buy&hold WF OOS", bh_oos)]), "",
         "### Причины выхода (WF OOS)", "",
         R.table(["Причина", "Сделок", "Прибыльных", "PnL, USDT", "Средний PnL, %"],
                 exit_rows(exits_oos)), "",
         "### Гейт §6.2 (WF OOS)", "", R.gate_table(gate, gate_s), "",
         "### Окна walk-forward", "", R.wf_windows_table(bars, wf), "",
         "## Anti-lookahead на реальных данных (§1.2)", "",
         R.table(["Проверка", "Итог", "Детали"],
                 [["slicing", R.f(la["ok"]),
                   f"точек усечения {la['points']} из {la['candidates']} решений, сделок "
                   f"{la['baseline_trades']}, расхождений {la['n_mismatches']}"
                   + (f": {la['mismatches'][:2]}" if la["mismatches"] else "")]] + rc_rows), "",
         "RSI и ATR рекуррентны (Уайлдер): копия с поздним стартом сходится к полному прогону "
         "экспоненциально, поэтому recursive-проверка индикаторов — с допуском, как у "
         "recursive-analysis Freqtrade. Расхождение состояния риск-ядра в recursive — "
         "путезависимый HWM breaker'а (копии стартуют с разной даты), не lookahead: такие "
         "интервалы сделок не сравниваются (`analysis._synced_intervals`).", "",
         "## Риск-контур: отказы", "",
         R.rejections_summary([("meanrev in-sample", [mr]), ("meanrev WF OOS", wf_tests)]), ""]
    return "\n".join(L) + "\n"


log = logging.getLogger("okx.backtest")


def _offsets(value: str) -> tuple[int, ...]:
    out = tuple(int(x) for x in value.split(",") if x.strip())
    if not out or min(out) <= 0:
        raise argparse.ArgumentTypeError("смещения — положительные целые")
    return out


def main(argv=None) -> None:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8")
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    p = argparse.ArgumentParser(prog="python -m src.backtest.meanrev",
                                description="Отчёт бэктеста mean-reversion RSI/BB (офлайн)")
    p.add_argument("--db", default="data/market_data.db")
    p.add_argument("--inst", default="BTC-USDT")
    p.add_argument("--bar", default="1H")
    p.add_argument("--since", default="2022-01-01")
    p.add_argument("--until", default=None)
    p.add_argument("--la-points", type=int, default=10)
    p.add_argument("--rc-offsets", type=_offsets, default=(500, 2000))
    p.add_argument("--rc-tol", type=float, default=1e-6)
    p.add_argument("--out", default=None)
    args = p.parse_args(argv)
    doc = _report(args)
    if args.out:
        with open(args.out, "w", encoding="utf-8") as fh:
            fh.write(doc)
        log.info("отчёт записан: %s", args.out)
    else:
        sys.stdout.write(doc)


if __name__ == "__main__":
    main()
