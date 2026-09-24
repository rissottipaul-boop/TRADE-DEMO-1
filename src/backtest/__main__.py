"""CLI бэктестера.

  python -m src.backtest download --inst BTC-USDT ETH-USDT --bar 1H --since 2022-01-01
  python -m src.backtest run --inst BTC-USDT --strategy sma_cross --fast 20 --slow 100
  python -m src.backtest baseline --inst BTC-USDT ETH-USDT --out insights/backtest-baseline.md

Снапшот прогона: --since/--until (open-время свечи в [since, until), UTC) + --no-download.
dataset_id в отчёте — хэш содержимого диапазона: тот же снапшот -> тот же id и результат.
Holdout (последние 6 мес, §2.1) run и baseline не трогают: касание — только --touch-holdout,
каждое фиксируется в data/market_data.db (holdout_touches).
Только публичные данные OKX (без ключей); ордеров не ставит, data/risk_state.db не открывает
(риск-ядро в бэктесте — in-memory, src/backtest/risk_sim.py).
"""
import argparse
import logging
import sys
import time
from datetime import datetime, timezone

from src import risk
from src.backtest import report as R
from src.backtest.analysis import lookahead_check, recursive_check
from src.backtest.data import (BAR_MS, MarketDataStore, OkxPublicClient, download_candles,
                               iso_to_ms, load_dataset, ms_to_iso)
from src.backtest.engine import Backtest, CostModel
from src.backtest.metrics import evaluate_gate
from src.backtest.strategies import BuyAndHold, SmaCross
from src.backtest.walkforward import WalkForwardConfig, holdout_start, walk_forward

log = logging.getLogger("okx.backtest")

SMA_GRID = [{"fast": f, "slow": s} for f in (10, 20, 50) for s in (50, 100, 200) if f < s]
SMA_DEFAULT = {"fast": 50, "slow": 200}
# Anti-lookahead на реальных данных: эталон + самая активная точка сетки (больше решений)
LA_PARAMS = [SMA_DEFAULT, {"fast": 10, "slow": 50}]
WF_CONFIG = WalkForwardConfig()
STRATEGIES = {"buy_hold": BuyAndHold, "sma_cross": SmaCross}
MIN_BARS = 500


def _iso_arg(ts_ms: int) -> str:
    return ms_to_iso(ts_ms).replace(" ", "T")


def _ok(flag) -> str:
    return "✅" if flag else "❌"


def _download(store, inst_ids, bar, since_ms):
    client = OkxPublicClient()
    for inst in inst_ids:
        spec, raw = client.instrument(inst)
        store.save_instrument(spec, raw=raw)
        t = time.time()
        rep = download_candles(store, client, inst, bar, since_ms)
        log.info("%s: страниц %d, новых свечей %d, всего %d (%s → %s), дыр биржи %d, %.1fс",
                 inst, rep["pages"], rep["inserted"], rep["total"], ms_to_iso(rep["first_ts"]),
                 ms_to_iso(rep["last_ts"]), rep["exchange_gaps"], time.time() - t)
    log.info("запросов к OKX: %d", client.requests)


def _resolve_until(store, inst_ids, bar, until) -> int:
    """--until или close последней свечи, общей для всех инструментов (граница снапшота)."""
    if until:
        return iso_to_ms(until)
    closes = []
    for inst in inst_ids:
        _, hi, n = store.ts_range(inst, bar)
        if not n:
            raise SystemExit(f"{inst} {bar}: нет свечей в market_data.db — сначала download")
        closes.append(hi + BAR_MS[bar])
    return min(closes)


def _load(store, inst, bar, since_ms, until_ms):
    spec = store.get_instrument(inst)
    if spec is None:
        raise SystemExit(f"нет спецификации {inst} в market_data.db — сначала download")
    ds = load_dataset(store, inst, bar, start_ms=since_ms, end_ms=until_ms)
    if len(ds.bars) < MIN_BARS:
        raise SystemExit(f"{inst}: мало данных ({len(ds.bars)} баров)")
    if ds.anomalies:
        log.warning("%s: аномалий OHLC %d: %s", inst, len(ds.anomalies), ds.anomalies[:3])
    return ds, spec


def _in_sample(ds, cfg: WalkForwardConfig) -> int:
    """Индекс первого бара holdout: всё левее — in-sample."""
    return holdout_start([b.ts for b in ds.bars], ds.bar_ms, cfg.holdout_days)[0]


def cmd_download(args):
    _download(MarketDataStore(args.db), args.inst, args.bar, iso_to_ms(args.since))


def cmd_run(args):
    store = MarketDataStore(args.db)
    inst = args.inst[0]
    until_ms = _resolve_until(store, [inst], args.bar, args.until)
    ds, spec = _load(store, inst, args.bar, iso_to_ms(args.since), until_ms)
    params = {"fast": args.fast, "slow": args.slow} if args.strategy == "sma_cross" else {}
    bars = ds.bars
    if args.touch_holdout:
        n = store.log_holdout_touch(ds.dataset_id, args.strategy, params)
        scope = f"с holdout (касание №{n} записано)"
    else:
        bars = bars[:_in_sample(ds, WF_CONFIG)]
        scope = "без holdout"
    res = Backtest(bars, STRATEGIES[args.strategy](**params), spec, bar=args.bar,
                   dataset_id=ds.dataset_id).run()
    print(f"{inst} {args.bar}: dataset_id={ds.dataset_id}, бары {ms_to_iso(bars[0].ts)} → "
          f"{ms_to_iso(bars[-1].ts)} UTC, {scope}")
    print(R.metrics_table([(f"{args.strategy} {params or ''}", res.metrics())]))


def _lookahead_rows(checks) -> list[list[str]]:
    rows = []
    for p, la, rc in checks:
        name = f"SMA {p['fast']}/{p['slow']}"
        rows.append([f"slicing, {name}", _ok(la["ok"]),
                     f"точек усечения {la['points']} из {la['candidates']} решений, сделок "
                     f"baseline {la['baseline_trades']}, расхождений {la['n_mismatches']}"
                     + (f": {la['mismatches'][:2]}" if la["mismatches"] else "")])
        for x in rc["runs"]:
            if "skipped" in x:
                rows.append([f"recursive, {name}, старт +{x['offset']} баров", "—", x["skipped"]])
                continue
            ind = ("совпали бит-в-бит" if not x["indicators_mismatch"]
                   else f"расходятся: {x['indicators_mismatch']}")
            text = (f"индикаторы после прогрева {rc['warmup']}: {ind}; синхронизация "
                    f"{R.d(x['sync_ts'])}; сделок сравнено {x['trades_compared']}, "
                    f"идентичны: {R.f(x['trades_equal'])}")
            div = x["risk_divergences"]
            if div:
                fields = sorted({fl for e in div for fl in e["fields"]})
                text += (f"; расхождений состояния риск-ядра: {len(div)} (с {R.d(div[0]['ts'])}, "
                         f"поля: {', '.join(fields)}) — путезависимый лимит, сделки после "
                         "расхождения не сравниваются до новой синхронизации")
            if x["reason"]:
                text += f"; **{x['reason']}**"
            rows.append([f"recursive, {name}, старт +{x['offset']} баров", _ok(x["ok"]), text])
    return rows


def _instrument_section(store, inst, args, since_ms, until_ms):
    ds, spec = _load(store, inst, args.bar, since_ms, until_ms)
    bars, bar, cfg = ds.bars, args.bar, WF_CONFIG
    kw = dict(bar=bar, dataset_id=ds.dataset_id)
    h_idx = _in_sample(ds, cfg)
    ins = bars[:h_idx]  # holdout не участвует ни в одном прогоне без --touch-holdout
    if len(ins) < MIN_BARS:
        raise SystemExit(f"{inst}: in-sample до holdout слишком короткий ({len(ins)} баров)")
    base = CostModel()
    scenarios = [("fee×0 (брутто по комиссиям)", base.scaled(fee_mult=0.0)),
                 ("fee×1.5", base.scaled(fee_mult=1.5)),
                 ("slippage×2 (10 бп, стресс)", base.scaled(slip_mult=2.0))]
    sma_name = f"SMA {SMA_DEFAULT['fast']}/{SMA_DEFAULT['slow']}"
    strategies = (("buy&hold", BuyAndHold), (sma_name, lambda: SmaCross(**SMA_DEFAULT)))

    t = time.time()
    bh = Backtest(ins, BuyAndHold(), spec, **kw).run()
    sma = Backtest(ins, SmaCross(**SMA_DEFAULT), spec, **kw).run()
    m_bh, m_sma = bh.metrics(), sma.metrics()
    sens = [("buy&hold", "fee×1, slip 5 бп (база)", m_bh),
            (sma_name, "fee×1, slip 5 бп (база)", m_sma)]
    for label, costs in scenarios:
        for name, make in strategies:
            sens.append((name, label, Backtest(ins, make(), spec, costs=costs, **kw)
                         .run().metrics()))
    log.info("%s: in-sample и чувствительность %.1fс", inst, time.time() - t)

    t = time.time()
    checks = []
    for p in LA_PARAMS:
        la = lookahead_check(lambda p=p: SmaCross(**p), ins, spec, max_points=args.la_points,
                             bar=bar)
        rc = recursive_check(lambda p=p: SmaCross(**p), ins, spec, offsets=args.rc_offsets,
                             bar=bar)
        checks.append((p, la, rc))
    la_ok = all(la["ok"] for _, la, _ in checks)
    rc_ok = all(rc["ok"] for _, _, rc in checks)
    log.info("%s: anti-lookahead проверки %.1fс", inst, time.time() - t)

    t = time.time()
    wf = walk_forward(bars, SmaCross, SMA_GRID, spec, cfg=cfg, touch_holdout=args.touch_holdout,
                      **kw)
    embargo = max(SmaCross(**p).warmup for p in SMA_GRID)
    wf_bh = walk_forward(bars, BuyAndHold, [{}], spec, cfg=cfg, touch_holdout=args.touch_holdout,
                         embargo=embargo, **kw)
    log.info("%s: walk-forward %.1fс (%d окон)", inst, time.time() - t, len(wf.windows))
    if not wf.windows:
        raise SystemExit(f"{inst}: нет ни одного окна walk-forward — мало истории до holdout")
    touches = {}
    if args.touch_holdout:
        for w in (wf, wf_bh):
            if w.holdout_result is not None:
                touches[w.strategy] = store.log_holdout_touch(ds.dataset_id, w.strategy,
                                                              w.holdout_params or {})
    oos, oos_s = wf.oos.metrics(), wf.oos_stress.metrics()
    gate = evaluate_gate(oos, oos_s, [w.test.metrics() for w in wf.windows])
    gate_s = evaluate_gate(oos_s, oos_s, [w.test_stress.metrics() for w in wf.windows])
    bh_oos, bh_oos_s = wf_bh.oos.metrics(), wf_bh.oos_stress.metrics()
    gate_bh = evaluate_gate(bh_oos, bh_oos_s, [w.test.metrics() for w in wf_bh.windows])
    gate_bh_s = evaluate_gate(bh_oos_s, bh_oos_s,
                              [w.test_stress.metrics() for w in wf_bh.windows])

    implied = (m_bh["net_profit_pct"] / m_bh["benchmark_pct"]
               if m_bh["benchmark_pct"] else None)
    h_start = ms_to_iso(bars[h_idx].ts) if h_idx < len(bars) else "—"
    journal = store.holdout_touch_counts(inst)
    journal_txt = ("журнал `holdout_touches` по инструменту: " + (", ".join(
        f"{k} — {v}" for k, v in journal.items()) if journal else "пуст") + ".")
    L = [f"## {inst}", ""]
    L += ["### In-sample: метрики §6.1", "",
          f"In-sample: {ms_to_iso(ins[0].ts)} → {ms_to_iso(ins[-1].ts + ds.bar_ms)} UTC "
          f"({len(ins)} баров). Торговля B&H — с {ms_to_iso(bh.trade_start_ts)}, SMA — с "
          f"{ms_to_iso(sma.trade_start_ts)} (прогрев {SmaCross(**SMA_DEFAULT).warmup} баров). "
          f"Holdout {cfg.holdout_days} д (с {h_start}) — "
          + ("прогнан (--touch-holdout), см. ниже" if args.touch_holdout else
             "**не тронут** ни одним прогоном этого отчёта") + f"; {journal_txt}", "",
          R.metrics_table([("buy&hold (15% equity)", m_bh), (sma_name, m_sma)]), "",
          f"Контур buy&hold: net / рынок = {R.f(implied, 3)} (ожидание ≈ 0.15 — потолок позиции "
          "`risk.MAX_POSITION_PCT`, минус издержки входа/выхода; позиция не ребалансируется, "
          "поэтому её доля и MDD растут вместе с ценой).", ""]
    L += ["### Чувствительность к издержкам (§3.3)", "", R.sensitivity_table(sens), ""]
    L += ["### Breakdown по годам (in-sample)", "",
          R.yearly_table(R.market_curve(ins), [("buy&hold", bh), (sma_name, sma)]), "",
          f"### Помесячно: {sma_name}, доходность equity %", "", R.monthly_matrix(sma.curve), ""]
    L += ["### Walk-forward SMA-cross (§2)", "",
          f"Окна rolling train {cfg.train_days} / valid {cfg.valid_days} / test {cfg.test_days} д, "
          f"шаг {cfg.step_days} д, окон {len(wf.windows)}; holdout — последние "
          f"{cfg.holdout_days} д (с {h_start}) в окна не входит; embargo {embargo} баров "
          "(прогрев перед каждым сегментом), purge — позиция закрывается в конце сегмента. "
          f"Сетка ({len(SMA_GRID)}): " + ", ".join(f"{p['fast']}/{p['slow']}" for p in SMA_GRID)
          + f". Objective: {cfg.objective} на train, выбор из top-{cfg.top_k} по valid, test — "
          f"один прогон выбранного набора. **Число испытаний (trials): {wf.trials}** (§2.2 п.2; "
          "поправка на множественные сравнения не применялась).", "",
          R.wf_windows_table(bars, wf), "",
          "OOS-конкатенация test-окон (equity сцеплена по доходностям окон; holdout не входит):", "",
          R.metrics_table([("SMA-cross WF OOS", oos), ("SMA-cross WF OOS, slip×2", oos_s),
                           ("buy&hold WF OOS", bh_oos), ("buy&hold WF OOS, slip×2", bh_oos_s)]),
          ""]
    L += ["### Гейт приёмки §6.2: SMA-cross (WF OOS)", "", R.gate_table(gate, gate_s), "",
          "### Гейт §6.2: buy&hold (WF OOS, для сравнения)", "", R.gate_table(gate_bh, gate_bh_s),
          ""]
    if args.touch_holdout and wf.holdout_result is not None:
        L += ["### Holdout (последние 6 мес, одно касание)", "",
              f"Параметры — выбор последнего окна: {wf.holdout_params}. Касание записано в "
              f"`holdout_touches` (sma_cross: {touches.get('sma_cross')}, "
              f"buy_hold: {touches.get('buy_hold')}).", "",
              R.metrics_table([("SMA-cross holdout", wf.holdout_result.metrics()),
                               ("SMA-cross holdout, slip×2", wf.holdout_stress.metrics()),
                               ("buy&hold holdout", wf_bh.holdout_result.metrics())]), ""]
    L += ["### Anti-lookahead на реальных данных (§1.2)", "",
          f"Ряд — весь in-sample ({len(ins)} баров). Slicing: прогоны на префиксах, "
          "оканчивающихся на барах решений полного прогона; индикаторы, решения и закрытые сделки "
          "обязаны совпасть бит-в-бит. Recursive: прогоны с поздним стартом; после прогрева "
          "индикаторы совпадают, сделки — с первой точки синхронизации (обе копии без позиции, "
          "одинаковое состояние риск-ядра).", "",
          R.table(["Проверка", "Итог", "Детали"], _lookahead_rows(checks)), ""]
    wf_tests = [w.test for w in wf.windows]
    L += ["### Риск-контур: отказы и события", "",
          R.rejections_summary([("buy&hold in-sample", [bh]), (f"{sma_name} in-sample", [sma]),
                                ("SMA-cross WF OOS (все test-окна)", wf_tests),
                                ("buy&hold WF OOS (все test-окна)",
                                 [w.test for w in wf_bh.windows])]), ""]
    trips = []  # (прогон, время, просадка %, отклонено входов breaker'ом)
    for name, results in (("buy&hold in-sample", [bh]), (f"{sma_name} in-sample", [sma]),
                          ("SMA-cross WF OOS", wf_tests), ("buy&hold WF OOS",
                                                           [w.test for w in wf_bh.windows])):
        for r in results:
            trips += [(name, ts, dd, R.breaker_rejections(r)) for ts, dd in R.breaker_trips(r)]
    rc_div = [(p, x["offset"], e) for p, _, rc in checks for x in rc["runs"]
              for e in x.get("risk_divergences", [])]
    summary = {"inst": inst, "dataset_id": ds.dataset_id, "bh": m_bh, "sma": m_sma, "oos": oos,
               "oos_stress": oos_s, "bh_oos": bh_oos, "gate": gate, "gate_stress": gate_s,
               "gate_bh": gate_bh, "lookahead": la_ok, "recursive": rc_ok, "rc_divergences": rc_div,
               "windows": len(wf.windows), "trials": wf.trials, "breaker_trips": trips,
               "holdout_start": h_start}
    return (ds, spec), "\n".join(L), summary


def _conclusions(summaries) -> list[str]:
    out = []
    for s in summaries:
        g, o = s["gate"], s["oos"]
        verdict = "пройден" if g["passed"] else "**не пройден** (пороги " + ", ".join(
            R.failed_checks(g)) + ")"
        out.append(f"- **{s['inst']}:** SMA-cross WF OOS — гейт §6.2 {verdict}: net "
                   f"{R.f(o['net_profit_pct'])}% при рынке {R.f(o['benchmark_pct'])}%, Sharpe "
                   f"{R.f(o['sharpe'])}, PF {R.f(o['profit_factor'])}, сделок {o['trades']}, "
                   f"MDD {R.f(o['mdd_pct'])}%. Buy&hold WF OOS — гейт "
                   + ("пройден" if s["gate_bh"]["passed"] else "не пройден (пороги "
                      + ", ".join(R.failed_checks(s["gate_bh"])) + ")")
                   + f", net {R.f(s['bh_oos']['net_profit_pct'])}%.")
    passed = [s["inst"] for s in summaries if s["gate"]["passed"]]
    out.append("- Итог: " + ("SMA-cross прошёл гейт на " + ", ".join(passed)
                             + " — но это контрольная стратегия, в paper не идёт без отдельного "
                               "решения" if passed else
                             "ни одна базовая стратегия гейт §6.2 не прошла — в paper не идут. "
                             "Ожидаемо для проверки контура: цель прогона — рабочий и честный "
                             "бэктестер, а не прибыльная стратегия."))
    la = all(s["lookahead"] for s in summaries)
    rc = all(s["recursive"] for s in summaries)
    line = f"- Anti-lookahead на реальных данных: slicing {_ok(la)}, recursive {_ok(rc)}."
    div = [(s["inst"], p, off, e) for s in summaries for p, off, e in s["rc_divergences"]]
    if div:
        fields = sorted({fl for *_, e in div for fl in e["fields"]})
        line += (f" В recursive {len(div)} эпизод(а) расхождения состояния риск-ядра после "
                 f"синхронизации (поля: {', '.join(fields)}): HWM breaker'а считается от старта "
                 "прогона, поэтому копии с разной датой старта тормозят в разное время. Это не "
                 "lookahead — сделки до расхождения идентичны; такие интервалы не сравниваются "
                 "(`analysis._synced_intervals`).")
    out.append(line)
    trips = [(s["inst"],) + t for s in summaries for t in s["breaker_trips"]]
    early = [t for t in trips if t[3] < risk.GLOBAL_DD_LIMIT_PCT]
    if early:
        items = "; ".join(f"{inst} {name} — {R.d(ts)} при просадке {R.f(dd)}%, после него "
                          f"отклонено входов: {n}" for inst, name, ts, dd, n in early)
        wf_hit = any(name.endswith("WF OOS") for _, name, *_ in early)
        out.append(f"- Риск-ядро: global_breaker срабатывал раньше номинала "
                   f"−{risk.GLOBAL_DD_LIMIT_PCT:g}%: {items}. Причина в `src/risk.py`: "
                   "`record_pnl` прибавляет PnL к equity, которую `update_equity` уже переоценил "
                   "по рынку (двойной учёт, HWM завышается). Бэктест повторяет боевое поведение "
                   "(паритет), ошибка — в безопасную сторону, но in-sample результаты этих "
                   "прогонов после даты срабатывания занижены (торговля остановлена). "
                   + ("Затронуты и test-окна walk-forward — гейт считать с оговоркой."
                      if wf_hit else "На WF OOS и гейт не влияет: в test-окнах breaker не "
                                     "срабатывал."))
    return out


def cmd_baseline(args):
    store = MarketDataStore(args.db)
    since_ms = iso_to_ms(args.since)
    if not args.no_download:
        _download(store, args.inst, args.bar, since_ms)
    until_ms = _resolve_until(store, args.inst, args.bar, args.until)
    sections, datasets, summaries = [], [], []
    t0 = time.time()
    for inst in args.inst:
        dsspec, text, summary = _instrument_section(store, inst, args, since_ms, until_ms)
        datasets.append(dsspec)
        sections.append(text)
        summaries.append(summary)
    now = datetime.now(timezone.utc).astimezone()
    th = " --touch-holdout" if args.touch_holdout else ""
    repro = (f".venv\\Scripts\\python.exe -m src.backtest baseline --inst {' '.join(args.inst)} "
             f"--bar {args.bar} --since {args.since} --until {_iso_arg(until_ms)} --no-download "
             f"--la-points {args.la_points} --rc-offsets "
             f"{','.join(str(x) for x in args.rc_offsets)}{th} --out {args.out or '-'}")
    cfg = WF_CONFIG
    head = [
        f"# Базовый прогон бэктестера: buy&hold и SMA-cross ({', '.join(args.inst)}, {args.bar})",
        "",
        f"- **Дата:** {now:%Y-%m-%d %H:%M %z}",
        "- **Задача:** BT-IMPL (проверка контура бэктестера, `insights/backtester-design.md`)",
        "- **Статус:** validated — отчёт сгенерирован кодом (`src/backtest`), воспроизводим на тех "
        "же dataset_id",
        f"- **Воспроизведение:** `{repro}` — тот же снапшот `data/market_data.db` даёт те же "
        "dataset_id и цифры (движок детерминирован)",
        f"- **Время прогона:** {time.time() - t0:.0f} с", "",
        "> Buy&hold и SMA-cross — **проверка контура**, не кандидаты в paper. Не финансовый совет.",
        "",
        "## Вывод", "", *_conclusions(summaries), "",
        "## Данные", "",
        f"Источник — публичный REST OKX `GET /api/v5/market/history-candles` (без ключей), только "
        f"`confirm=1`; снапшот — open-время в [{ms_to_iso(since_ms)}, {ms_to_iso(until_ms)}) UTC. "
        "Gap-check — шаг ровно 1 бар; бар с vol=0 (биржа подставляет прошлый close) дырой не "
        "считается.", "",
        R.data_section(datasets), "",
        "## Модель прогона", "",
        "- Капитал 10 000 USDT, спот long-only, один инструмент на прогон.",
        "- Сигнал на close подтверждённой свечи `t` → рыночное исполнение по open `t+1`; "
        "стоп/тейк внутри бара «low раньше high».",
        "- Издержки: taker 0.10% на каждой стороне (комиссия покупки — в базе, продажи — в USDT), "
        "проскальзывание 5 бп/сторону (дефолт подтверждён `demo-slippage.md` §5, §7.4); "
        "цена → tickSz, объём → floor lotSz. Стресс — slip×2 (10 бп); чувствительность fee×0/×1.5.",
        "- Риск-ядро `src/risk.py` (тот же код, модельное время, in-memory SQLite — "
        "`data/risk_state.db` не открывается): check_entry_allowed, size_position, потолок "
        "позиции 15% equity, лимит 10 входов/сутки, блок инструмента после 3 убытков (24ч), "
        "пауза после 5 убытков (24ч), breaker'ы −6%/−15%.",
        "- Sharpe/Sortino — по дневным доходностям wallet balance, ×√365, rf=0.",
        f"- Walk-forward: rolling {cfg.train_days}/{cfg.valid_days}/{cfg.test_days} д, шаг "
        f"{cfg.step_days} д, holdout {cfg.holdout_days} д "
        + ("прогнан (--touch-holdout)." if args.touch_holdout else
           "зарезервирован под финальную приёмку кандидата (не тронут)."), "",
        "## Сводка", "",
        R.table(["Инструмент", "B&H net %", "Рынок %",
                 f"SMA {SMA_DEFAULT['fast']}/{SMA_DEFAULT['slow']} net %", "SMA Sharpe",
                 "WF OOS net %", "WF OOS Sharpe", "WF OOS сделок", "Гейт SMA", "Гейт B&H",
                 "slicing", "recursive"],
                [[s["inst"], R.f(s["bh"]["net_profit_pct"]), R.f(s["bh"]["benchmark_pct"]),
                  R.f(s["sma"]["net_profit_pct"]), R.f(s["sma"]["sharpe"]),
                  R.f(s["oos"]["net_profit_pct"]), R.f(s["oos"]["sharpe"]), s["oos"]["trades"],
                  _ok(s["gate"]["passed"]), _ok(s["gate_bh"]["passed"]), _ok(s["lookahead"]),
                  _ok(s["recursive"])] for s in summaries]), ""]
    doc = "\n".join(head + sections) + "\n"
    if args.out:
        with open(args.out, "w", encoding="utf-8") as fh:
            fh.write(doc)
        log.info("отчёт записан: %s", args.out)
    else:
        sys.stdout.write(doc)
    return summaries


def _offsets(value: str) -> tuple[int, ...]:
    try:
        out = tuple(int(x) for x in value.split(",") if x.strip())
    except ValueError:
        raise argparse.ArgumentTypeError(f"ожидается список целых через запятую: {value!r}")
    if not out or min(out) <= 0:
        raise argparse.ArgumentTypeError("смещения recursive-проверки — положительные числа")
    return out


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="python -m src.backtest", description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--db", default="data/market_data.db")
    sub = p.add_subparsers(dest="cmd", required=True)
    handlers = {"download": cmd_download, "run": cmd_run, "baseline": cmd_baseline}
    for name, fn in handlers.items():
        s = sub.add_parser(name)
        s.set_defaults(func=fn)
        s.add_argument("--inst", nargs="+", default=["BTC-USDT"])
        s.add_argument("--bar", default="1H", choices=sorted(BAR_MS))
        s.add_argument("--since", default="2022-01-01", help="начало снапшота, UTC")
        if name == "download":
            continue
        s.add_argument("--until", default=None,
                       help="конец снапшота, UTC, open-время < until (по умолчанию — close "
                            "последней свечи в базе)")
        s.add_argument("--touch-holdout", action="store_true",
                       help="включить holdout (одно касание, фиксируется в БД)")
        if name == "run":
            s.add_argument("--strategy", choices=sorted(STRATEGIES), default="sma_cross")
            s.add_argument("--fast", type=int, default=SMA_DEFAULT["fast"])
            s.add_argument("--slow", type=int, default=SMA_DEFAULT["slow"])
        else:
            s.add_argument("--out", default=None)
            s.add_argument("--no-download", action="store_true")
            s.add_argument("--la-points", type=int, default=20,
                           help="точек усечения slicing-проверки на параметр")
            s.add_argument("--rc-offsets", type=_offsets, default=(500, 2000, 8760),
                           help="смещения старта recursive-проверки, баров")
    return p


def main(argv=None):
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8")
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    args = build_parser().parse_args(argv)
    args.func(args)


if __name__ == "__main__":
    main()
