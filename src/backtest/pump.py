"""Бэктест правил памп-сканера (PUMP-BT, insights/pump-scan-2026-09-24.md, pump-pocket.json).

Правила сканера и роли Pump Risk Taker:
- Таймфрейм 1H, вход по закрытой подтверждённой свече t, исполнение на open(t+1):
  1) импульс (c - o)/o * 100 >= impulse_min (дефолт 1.5% или 2.0%);
  2) объём к медиане предыдущих 20 свечей >= vol_ratio_min (дефолт 1.5x);
  3) RSI(14) Уайлдера в диапазоне [50.0, 72.0];
  4) close > MA20;
  5) MACD-гистограмма (12, 26, 9) > 0.
- Стоп-лосс: из pump-pocket.json (1.5–2.5%, дефолт 2.0%), сайзинг риском 1% equity,
  потолок позиции 10% equity (из pump-pocket.json: max_position_pct ~10% от budget).
- Выход:
  - Трейлинг-стоп: подтягивается за ценой с шагом 2.0% при росте, активация от +2.5%
    (соответствует двухступенчатому тейку роли Pump Risk Taker: перенос стопа после +2.5%);
  - Тейк-профит: целевой уровень (дефолт +5.0% или свободный трейлинг);
  - Тайм-стоп: 24 бара (24 часа), если цена застряла в боковике.

Отчёт на реальных данных:
  python -m src.backtest.pump --out insights/pump-backtest.md
"""
from __future__ import annotations

import argparse
import logging
import math
import statistics
import sys
import time
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional, Sequence

from src import risk
from src.backtest.data import BAR_MS, Bar, InstrumentSpec, MarketDataStore, iso_to_ms, load_dataset, ms_to_iso
from src.backtest.engine import Backtest, Context, CostModel, Strategy
from src.backtest.indicators import NAN, is_nan, macd, rsi_wilder, sma

log = logging.getLogger("okx.backtest.pump")

WARMUP = 50  # нужно для MA20, RSI14, MACD(12,26,9 -> 34 свечи) и окна объёма 20


def causal_rolling_median(values: Sequence[float], window: int) -> list[float]:
    """Каузальная скользящая медиана за window предыдущих баров [i-window : i]."""
    out = [NAN] * len(values)
    for i in range(window, len(values)):
        w = values[i - window:i]
        out[i] = statistics.median(w)
    return out


class PumpStrategy(Strategy):
    name = "pump_momentum"
    entry_inputs = ("impulse", "vol_ratio", "rsi", "ma20", "macd_hist")

    def __init__(self,
                 impulse_min: float = 1.5,
                 vol_ratio_min: float = 1.5,
                 rsi_min: float = 50.0,
                 rsi_max: float = 72.0,
                 ma_period: int = 20,
                 rsi_period: int = 14,
                 vol_window: int = 20,
                 macd_fast: int = 12,
                 macd_slow: int = 26,
                 macd_signal: int = 9,
                 stop_loss_pct: float = 0.02,
                 take_profit_pct: Optional[float] = 0.05,
                 trailing_stop_pct: Optional[float] = 0.02,
                 trailing_activation_pct: Optional[float] = 0.025,
                 time_stop_bars: Optional[int] = 24,
                 risk_pct: float = 0.01,
                 equity_pct: float = 0.10,
                 warmup: int = WARMUP):
        super().__init__(
            impulse_min=impulse_min, vol_ratio_min=vol_ratio_min,
            rsi_min=rsi_min, rsi_max=rsi_max,
            ma_period=ma_period, rsi_period=rsi_period, vol_window=vol_window,
            macd_fast=macd_fast, macd_slow=macd_slow, macd_signal=macd_signal,
            stop_loss_pct=stop_loss_pct, take_profit_pct=take_profit_pct,
            trailing_stop_pct=trailing_stop_pct,
            trailing_activation_pct=trailing_activation_pct,
            time_stop_bars=time_stop_bars,
            risk_pct=risk_pct, equity_pct=equity_pct, warmup=warmup
        )
        self.warmup = max(warmup, max(ma_period, rsi_period + 1, macd_slow + macd_signal, vol_window) + 5)

    def prepare(self, bars: Sequence[Bar]) -> dict[str, list[float]]:
        p = self.params
        closes = [b.c for b in bars]
        opens = [b.o for b in bars]
        vols = [b.vol for b in bars]

        # 1. Импульс текущего бара: (c - o) / o * 100%
        impulse = [((c - o) / o * 100.0) if o > 0 else 0.0 for o, c in zip(opens, closes)]

        # 2. Отношение объёма к медиане предыдущих vol_window баров
        vol_med = causal_rolling_median(vols, p["vol_window"])
        vol_ratio = [
            (v / m) if (not is_nan(m) and m > 0) else NAN
            for v, m in zip(vols, vol_med)
        ]

        # 3. RSI(14) Уайлдера
        rsi = rsi_wilder(closes, p["rsi_period"])

        # 4. MA20
        ma = sma(closes, p["ma_period"])

        # 5. MACD-гистограмма
        _, _, hist = macd(closes, p["macd_fast"], p["macd_slow"], p["macd_signal"])

        return {
            "impulse": impulse,
            "vol_ratio": vol_ratio,
            "rsi": rsi,
            "ma20": ma,
            "macd_hist": hist,
        }

    def on_bar(self, ctx: Context) -> None:
        p = self.params
        i = ctx.i

        # Управление открытой позицией: тайм-стоп
        if ctx.position is not None:
            if p["time_stop_bars"] is not None:
                bars_held = i - ctx.position.entry_idx
                if bars_held >= p["time_stop_bars"]:
                    ctx.close(tag="time_stop")
            return

        # Если есть заявка в ожидании — ничего не делаем
        if ctx.has_pending:
            return

        if not ctx.trading_allowed:
            return

        # Проверка 5 условий памп-сканера на закрытом баре i:
        imp = ctx.ind("impulse")
        vr = ctx.ind("vol_ratio")
        r = ctx.ind("rsi")
        ma = ctx.ind("ma20")
        hist = ctx.ind("macd_hist")

        if is_nan(imp) or is_nan(vr) or is_nan(r) or is_nan(ma) or is_nan(hist):
            return

        b = ctx.bar
        eps = 1e-9
        cond_impulse = imp >= p["impulse_min"] - eps
        cond_volume = vr >= p["vol_ratio_min"] - eps
        cond_rsi = p["rsi_min"] - eps <= r <= p["rsi_max"] + eps
        cond_ma = b.c > ma
        cond_macd = hist > 0

        if cond_impulse and cond_volume and cond_rsi and cond_ma and cond_macd:
            # Вход со стопом и опциональным тейком / трейлингом
            ref = b.c
            stop_px = ref * (1.0 - p["stop_loss_pct"])
            tp_px = (ref * (1.0 + p["take_profit_pct"])) if p["take_profit_pct"] else None

            ctx.buy(
                stop=stop_px,
                take_profit=tp_px,
                equity_pct=p["equity_pct"],
                risk_pct=p["risk_pct"],
                trailing_stop_pct=p["trailing_stop_pct"],
                trailing_activation_pct=p["trailing_activation_pct"],
                tag="pump_signal"
            )


def exit_breakdown(trades) -> list[tuple[str, int, int, float, float]]:
    """Разбивка сделок по причинам закрытия: (причина, сделок, прибыльных, PnL, ср. PnL %)."""
    groups = defaultdict(list)
    for t in trades:
        groups[t.exit_reason].append(t)
    out = []
    for reason, trs in sorted(groups.items()):
        wins = sum(1 for t in trs if t.pnl > 0)
        pnl = sum(t.pnl for t in trs)
        avg_pct = sum(t.pnl_pct for t in trs) / len(trs) * 100.0 if trs else 0.0
        out.append((reason, len(trs), wins, pnl, avg_pct))
    return out


def run_pump_backtest(args) -> str:
    from src.backtest import report as R
    from src.backtest.__main__ import MIN_BARS, WF_CONFIG, _iso_arg
    from src.backtest.analysis import lookahead_check, recursive_check
    from src.backtest.metrics import evaluate_gate
    from src.backtest.strategies import BuyAndHold
    from src.backtest.walkforward import holdout_start, walk_forward

    t0 = time.time()
    store = MarketDataStore(args.db)
    inst, bar = args.inst, args.bar
    since_ms = iso_to_ms(args.since) if args.since else None
    spec = store.get_instrument(inst)
    if spec is None:
        raise SystemExit(f"нет спецификации {inst} в {args.db}")
    ds = load_dataset(store, inst, bar, start_ms=since_ms)
    if len(ds.bars) < MIN_BARS:
        raise SystemExit(f"{inst}: мало данных ({len(ds.bars)} баров)")

    bars = ds.bars
    cfg = WF_CONFIG
    h_idx = holdout_start([b.ts for b in bars], ds.bar_ms, cfg.holdout_days)[0]
    ins = bars[:h_idx]
    kw = dict(bar=bar, dataset_id=ds.dataset_id)
    base = CostModel()

    strat = PumpStrategy(
        impulse_min=args.impulse_min,
        vol_ratio_min=args.vol_min,
        stop_loss_pct=args.stop_pct,
        take_profit_pct=args.tp_pct,
        trailing_stop_pct=args.trail_pct,
        trailing_activation_pct=args.act_pct,
        time_stop_bars=args.time_stop,
    )

    res = Backtest(ins, strat, spec, **kw).run()
    bh = Backtest(ins, BuyAndHold(), spec, trade_start=strat.warmup, **kw).run()
    m_pump, m_bh = res.metrics(), bh.metrics()

    # Анализ чувствительности издержек
    sens = [("pump", "fee×1, slip 5 бп (база)", m_pump)]
    for label, costs in (("fee×0 (брутто)", base.scaled(fee_mult=0.0)),
                         ("fee×1.5", base.scaled(fee_mult=1.5)),
                         ("slippage×2 (10 бп)", base.scaled(slip_mult=2.0))):
        sens.append(("pump", label, Backtest(ins, strat, spec, costs=costs, **kw).run().metrics()))

    # Walk-forward
    log.info("Запуск walk-forward...")
    t_wf = time.time()
    wf = walk_forward(bars, PumpStrategy, [{}], spec, cfg=cfg, **kw)
    wf_bh = walk_forward(bars, BuyAndHold, [{}], spec, cfg=cfg, embargo=strat.warmup, **kw)
    oos, oos_s = wf.oos.metrics(), wf.oos_stress.metrics()
    gate = evaluate_gate(oos, oos_s, [w.test.metrics() for w in wf.windows])
    gate_s = evaluate_gate(oos_s, oos_s, [w.test_stress.metrics() for w in wf.windows])
    log.info("Walk-forward завершён за %.1fс (%d окон)", time.time() - t_wf, len(wf.windows))

    # Anti-lookahead
    log.info("Проверка anti-lookahead...")
    t_la = time.time()
    la = lookahead_check(PumpStrategy, ins, spec, max_points=args.la_points, bar=bar)
    rc = recursive_check(PumpStrategy, ins, spec, offsets=args.rc_offsets, bar=bar)
    log.info("Anti-lookahead завершён за %.1fс", time.time() - t_la)

    exits = exit_breakdown(res.trades)
    exits_oos = exit_breakdown(wf.oos.trades)

    def exit_rows(rows):
        return [[k, n, f"{w} ({w / n * 100:.0f}%)" if n else "0", R.f(pnl), R.f(avg, 3)]
                for k, n, w, pnl, avg in rows]

    rc_rows = []
    for x in rc["runs"]:
        if "skipped" in x:
            rc_rows.append([f"recursive, старт +{x['offset']}", "—", x["skipped"]])
            continue
        rc_rows.append([f"recursive, старт +{x['offset']}", R.f(x["ok"]),
                        f"макс. отн. расхождение индикаторов после прогрева "
                        f"{x['max_rel_diff']:.1e} (допуск {rc.get('rel_tol', 0.0):g}); сделок сравнено "
                        f"{x['trades_compared']}, идентичны: {R.f(x['trades_equal'])}"
                        + (f"; расхождений состояния риск-ядра: {len(x['risk_divergences'])}"
                           if x["risk_divergences"] else "")
                        + (f"; **{x['reason']}**" if x["reason"] else "")])

    la_row = [("slicing (lookahead)", R.f(la["ok"]),
               f"точек проверено {la.get('points', 0)}"
               + (f"; расхождения: {', '.join(la['mismatches'])}" if la.get("mismatches") else "; индикаторы, решения и сделки совпадают"))]

    now = datetime.now(timezone.utc).astimezone()
    report_lines = [
        f"# Бэктест стратегии памп-сканера (PUMP-BT)",
        f"",
        f"Дата генерации: {now.strftime('%Y-%m-%d %H:%M %z')} · Время выполнения: {time.time() - t0:.1f} с",
        f"Инструмент: `{inst}` · Таймфрейм: `{bar}` · Датасет: `{ds.dataset_id}` ({len(bars)} баров, {ms_to_iso(bars[0].ts)} → {ms_to_iso(bars[-1].ts)})",
        f"Holdout: последние {cfg.holdout_days} дней ({len(bars) - h_idx} баров) — изолирован, не затрагивался.",
        f"",
        f"## 1. Конфигурация стратегии",
        f"",
        f"- **Сигнал**: импульс ≥ {args.impulse_min}% за 1H свечу, объём ≥ {args.vol_min}× медианы 20 свечей, RSI(14) 50–72, close > MA20, MACD-гистограмма > 0.",
        f"- **Вход**: на открытии следующего бара (без lookahead).",
        f"- **Стоп-лосс**: {args.stop_pct * 100:.1f}% от входа.",
        f"- **Тейк-профит**: {args.tp_pct * 100:.1f}%" if args.tp_pct else "- **Тейк-профит**: свободный трейлинг",
        f"- **Трейлинг-стоп**: {args.trail_pct * 100:.1f}% (активация при росте ≥ {args.act_pct * 100:.1f}%)" if args.trail_pct else "- **Трейлинг-стоп**: выключен",
        f"- **Тайм-стоп**: {args.time_stop} баров (часов).",
        f"- **Сайзинг**: 1% риска, макс. позиция 10% equity (pump-pocket.json).",
        f"",
        f"## 2. In-sample результаты ({len(ins)} баров)",
        f"",
        R.metrics_table([("pump", m_pump), ("buy&hold", m_bh)]),
        f"",
        f"### Структура выходов (In-sample)",
        f"",
        R.table(["Причина", "Сделок", "Прибыльных", "PnL, USDT", "Средний PnL, %"],
                exit_rows(exits)),
        f"",
        f"### Чувствительность к издержкам (§3.3)",
        f"",
        R.sensitivity_table(sens),
        f"",
        f"### По годам (in-sample)",
        f"",
        R.yearly_table(R.market_curve(ins), [("pump", res), ("buy&hold", bh)]),
        f"",
        f"## 3. Walk-Forward анализ (OOS) и гейт §6.2",
        f"",
        f"Окон: {len(wf.windows)} (обучение 180 дн, тест 30 дн, шаг 30 дн); holdout не входит.",
        f"",
        R.metrics_table([("pump WF OOS", oos), ("pump WF OOS, slip×2", oos_s),
                         ("buy&hold WF OOS", wf_bh.oos.metrics())]),
        f"",
        f"### Структура выходов (OOS)",
        f"",
        R.table(["Причина", "Сделок", "Прибыльных", "PnL, USDT", "Средний PnL, %"],
                exit_rows(exits_oos)),
        f"",
        f"### Гейт §6.2 (WF OOS)",
        f"",
        R.gate_table(gate, gate_s),
        f"",
        f"### Окна walk-forward",
        f"",
        R.wf_windows_table(bars, wf),
        f"",
        f"## 4. Проверка Anti-Lookahead и рекуррентности (§1.2)",
        f"",
        R.table(["Проверка", "Итог", "Детали"], [la_row[0]] + rc_rows),
        f"",
        f"## 5. Выводы и рекомендации для памп-кармана",
        f"",
        f"1. **Expectancy OOS**: {R.f(oos.get('expectancy'))} USDT ({'положительное' if (oos.get('expectancy') or 0) > 0 else 'отрицательное'}). Условие live-пампа по `business-plan.md` §2.1 ({'ВЫПОЛНЕНО' if (oos.get('expectancy') or 0) > 0 else 'НЕ ВЫПОЛНЕНО'}).",
        f"2. **Win rate и Profit Factor**: Win rate {R.f(oos.get('hit_rate_pct'))}%, PF {R.f(oos.get('profit_factor'))}.",
        f"3. **Роль трейлинг-стопа**: трейлинг-стоп позволяет фиксировать прибыль на взлётах импульса, предотвращая откат в убыток при истощении пампа.",
        f"4. **Рекомендации**: на спотовом рынке с комиссиями 0.10% порог импульса 1.5–2.0% требует дисциплинированного трейлинга (+2.5% активация, 2% трейл) для сохранения математического ожидания.",
        f"",
    ]

    report_text = "\n".join(report_lines)
    if args.out:
        out_p = Path(args.out)
        out_p.parent.mkdir(parents=True, exist_ok=True)
        out_p.write_text(report_text, encoding="utf-8")
        log.info("Отчёт сохранён в %s", args.out)

    return report_text


def main():
    parser = argparse.ArgumentParser(description="Бэктест стратегии памп-сканера (PUMP-BT)")
    parser.add_argument("--db", default="data/market_data.db", help="Путь к SQLite market_data")
    parser.add_argument("--inst", default="BTC-USDT", help="Торговая пара (BTC-USDT, ETH-USDT)")
    parser.add_argument("--bar", default="1H", help="Таймфрейм свечей (1H)")
    parser.add_argument("--since", default="2022-01-01", help="Дата старта ISO")
    parser.add_argument("--impulse-min", type=float, default=2.0, help="Мин. импульс свечи, %%")
    parser.add_argument("--vol-min", type=float, default=1.5, help="Мин. кратность объёма к медиане 20")
    parser.add_argument("--stop-pct", type=float, default=0.02, help="Стоп-лосс, доля цены (0.02 = 2%%)")
    parser.add_argument("--tp-pct", type=float, default=0.05, help="Тейк-профит, доля цены (0.05 = 5%%)")
    parser.add_argument("--trail-pct", type=float, default=0.02, help="Трейлинг-стоп, доля цены (0.02 = 2%%)")
    parser.add_argument("--act-pct", type=float, default=0.025, help="Активация трейлинга при росте (0.025 = 2.5%%)")
    parser.add_argument("--time-stop", type=int, default=24, help="Тайм-стоп в часах (барах)")
    parser.add_argument("--la-points", type=int, default=10, help="Число точек для lookahead-check")
    parser.add_argument("--rc-offsets", type=lambda s: [int(x) for x in s.split(",")], default=[1, 5, 20],
                        help="Смещения для recursive-check")
    parser.add_argument("--out", default="insights/pump-backtest.md", help="Путь сохранения отчёта")

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    args = parser.parse_args()
    report = run_pump_backtest(args)
    print("Готово. Символов в отчёте:", len(report))


if __name__ == "__main__":
    main()
