"""CLI аналитики (ANALYTICS-LAB). Только чтение; коды выхода: 0 — готово, 2 — ошибка входа/данных.

  python -m src.analytics bt-draft "Как mean reversion на ETH 1H с 2023 при комиссии x1.5?" [--out cfg.json]
  python -m src.analytics bt-run cfg.json [--md out.md] [--json]        # п. 31 + 33, файл прогона в data/analytics/runs/
  python -m src.analytics bt-summary data/analytics/runs/<id>.json [--json]   # п. 33
  python -m src.analytics bt-compare A.json B.json [--json]             # п. 32
  python -m src.analytics trade-review [--trade pmp…] [--last N] [--json]     # п. 34
  python -m src.analytics hypothesis --signal rsi_below --param level=30 --horizon 24 [--spec h.json]  # п. 35
  python -m src.analytics insights "breaker drawdown HWM" [--top 5] [--json]  # п. 36
  python -m src.analytics risk-scenarios [--scenario '{"name":..,"steps":[..]}'] [--file s.json]  # п. 37
  python -m src.analytics panel-metrics [--json]                        # п. 38
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from src.analytics.common import dump_json, read_json, write_text


def _emit(args, data, text: str) -> None:
    out = dump_json(data) if getattr(args, "json", False) else text
    if getattr(args, "md", None):
        write_text(args.md, text)
    if getattr(args, "out", None) and not getattr(args, "md", None):
        write_text(args.out, out if out.endswith("\n") else out + "\n")
    print(out)


def _value(raw: str):
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return raw


def cmd_bt_draft(args) -> int:
    from src.analytics.bt_config import draft_from_text
    cfg = draft_from_text(args.question)
    text = dump_json(cfg)
    if args.out:
        write_text(args.out, text + "\n")
    print(text)
    return 0


def cmd_bt_run(args) -> int:
    from src.analytics import bt_summary
    from src.analytics.bt_config import run_config, save_run
    run = run_config(read_json(args.config), db_path=args.db)
    path = save_run(run, Path(args.runs_dir))
    text = bt_summary.render(run) + f"\nФайл прогона: `{path.as_posix()}`\n"
    _emit(args, run if args.json else None, text)
    return 0


def cmd_bt_summary(args) -> int:
    from src.analytics import bt_summary
    run = read_json(args.run)
    _emit(args, bt_summary.summarize(run), bt_summary.render(run))
    return 0


def cmd_bt_compare(args) -> int:
    from src.analytics import bt_compare
    a, b = read_json(args.a), read_json(args.b)
    cmp = bt_compare.compare(a, b)
    _emit(args, cmp, bt_compare.render(cmp, a, b))
    return 0


def cmd_trade_review(args) -> int:
    from src.analytics import trade_review
    rep = trade_review.review(Path(args.journal), Path(args.pocket), trade=args.trade, last=args.last)
    _emit(args, rep, trade_review.render(rep))
    return 0


def cmd_hypothesis(args) -> int:
    from src.analytics import hypothesis
    spec = read_json(args.spec) if args.spec else {}
    for key in ("claim", "inst", "bar", "signal", "since", "until"):
        if getattr(args, key) is not None:
            spec[key] = getattr(args, key)
    if args.horizon is not None:
        spec["horizon"] = args.horizon
    if args.stop_pct is not None:
        spec["stop_pct"] = args.stop_pct
    if args.include_holdout:
        spec["include_holdout"] = True
    if args.factor:
        spec["factors"] = list(spec.get("factors") or []) + args.factor
    params = dict(spec.get("params") or {})
    for item in args.param or []:
        k, _, v = item.partition("=")
        params[k.strip()] = _value(v.strip())
    spec["params"] = params
    res = hypothesis.analyze(spec, db_path=args.db)
    _emit(args, res, hypothesis.render(res))
    return 0


def cmd_insights(args) -> int:
    from src.analytics import insights_search
    res = insights_search.search(args.query, Path(args.dir), top=args.top, stale_days=args.stale_days)
    _emit(args, res, insights_search.render(res))
    return 0


def cmd_risk_scenarios(args) -> int:
    from src.analytics import risk_scenarios
    scns = []
    if args.file:
        data = read_json(args.file)
        scns += data if isinstance(data, list) else [data]
    for raw in args.scenario or []:
        scns.append(json.loads(raw))
    res = risk_scenarios.analyze(scns or None, db=Path(args.db))
    _emit(args, res, risk_scenarios.render(res))
    return 0


def cmd_panel_metrics(args) -> int:
    from src.analytics import panel_metrics
    res = panel_metrics.explain(Path(args.risk_db), Path(args.bot_db))
    _emit(args, res, panel_metrics.render(res))
    return 0


def build_parser() -> argparse.ArgumentParser:
    from src.analytics.common import BOT_DB, RISK_DB, RUNS_DIR
    from src.backtest.data import DB_PATH
    from src.pump_scanner import JOURNAL_PATH, POCKET_PATH
    p = argparse.ArgumentParser(prog="python -m src.analytics", description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="cmd", required=True)

    def common(sp, md=True):
        sp.add_argument("--json", action="store_true", help="машинный вывод")
        if md:
            sp.add_argument("--md", help="сохранить отчёт Markdown в файл")
        return sp

    s = sub.add_parser("bt-draft", help="п. 31: вопрос → черновик конфигурации бэктеста")
    s.add_argument("question")
    s.add_argument("--out", help="сохранить конфигурацию JSON")
    s.set_defaults(fn=cmd_bt_draft)

    s = common(sub.add_parser("bt-run", help="п. 31/33: проверить конфигурацию и прогнать бэктест"))
    s.add_argument("config")
    s.add_argument("--db", default=str(DB_PATH))
    s.add_argument("--runs-dir", default=str(RUNS_DIR))
    s.set_defaults(fn=cmd_bt_run)

    s = common(sub.add_parser("bt-summary", help="п. 33: сводка прогона"))
    s.add_argument("run")
    s.set_defaults(fn=cmd_bt_summary)

    s = common(sub.add_parser("bt-compare", help="п. 32: сравнение двух прогонов"))
    s.add_argument("a")
    s.add_argument("b")
    s.set_defaults(fn=cmd_bt_compare)

    s = common(sub.add_parser("trade-review", help="п. 34: черновик разбора закрытых сделок"))
    s.add_argument("--trade", help="trade_id (или его начало) / пара для сделок без trade_id")
    s.add_argument("--last", type=int, help="последние N закрытых сделок")
    s.add_argument("--journal", default=str(JOURNAL_PATH))
    s.add_argument("--pocket", default=str(POCKET_PATH))
    s.set_defaults(fn=cmd_trade_review)

    s = common(sub.add_parser("hypothesis", help="п. 35: поддержка/противоречия/пробелы гипотезы"))
    s.add_argument("--spec", help="JSON-спецификация (аргументы ниже её дополняют)")
    s.add_argument("--claim")
    s.add_argument("--inst")
    s.add_argument("--bar")
    s.add_argument("--signal")
    s.add_argument("--param", action="append", help="параметр сигнала key=value")
    s.add_argument("--horizon", type=int)
    s.add_argument("--since")
    s.add_argument("--until")
    s.add_argument("--stop-pct", type=float)
    s.add_argument("--factor", action="append", help="фактор гипотезы вне свечей (funding, oi, …)")
    s.add_argument("--include-holdout", action="store_true")
    s.add_argument("--db", default=str(DB_PATH))
    s.set_defaults(fn=cmd_hypothesis)

    s = common(sub.add_parser("insights", help="п. 36: поиск выводов в insights/"))
    s.add_argument("query")
    s.add_argument("--top", type=int, default=5)
    s.add_argument("--stale-days", type=int, default=30)
    s.add_argument("--dir", default="insights")
    s.set_defaults(fn=cmd_insights)

    s = common(sub.add_parser("risk-scenarios", help="п. 37: сценарии риска (лимиты не меняются)"))
    s.add_argument("--scenario", action="append", help="сценарий JSON {name, steps}")
    s.add_argument("--file", help="файл со сценарием или списком сценариев")
    s.add_argument("--db", default=str(RISK_DB))
    s.set_defaults(fn=cmd_risk_scenarios)

    s = common(sub.add_parser("panel-metrics", help="п. 38: объяснение метрик панели"))
    s.add_argument("--risk-db", default=str(RISK_DB))
    s.add_argument("--bot-db", default=str(BOT_DB))
    s.set_defaults(fn=cmd_panel_metrics)
    return p


def main(argv=None) -> int:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8")
        except (AttributeError, ValueError):
            pass
    args = build_parser().parse_args(argv)
    try:
        return args.fn(args)
    except Exception as exc:     # ошибки входа и данных — код 2 с понятным текстом
        from src.analytics.bt_config import ConfigError
        problems = getattr(exc, "problems", None) if isinstance(exc, ConfigError) else None
        print(f"ошибка: {type(exc).__name__}: {exc}", file=sys.stderr)
        for pr in problems or []:
            print(f"  - {pr}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
