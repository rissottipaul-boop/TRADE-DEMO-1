"""П. 31 — перевод вопроса в параметры бэктеста. Расчёт делает существующий бэктестер.

Поток работы агента:
  1. draft_from_text(вопрос) — черновик конфигурации: что удалось понять из текста, а всё
     остальное — дефолты с явной записью в assumptions. Модель дописывает/правит JSON.
  2. validate(cfg) — строгая проверка: инструмент, бар, даты, стратегия из реестра,
     параметры — только аргументы конструктора стратегии, издержки неотрицательны.
     Ошибка — ConfigError со списком проблем; прогон не начинается.
  3. run_config(cfg) — прогон через src.backtest.engine.Backtest (тот же движок, риск-ядро
     SimRisk и CostModel, что у `python -m src.backtest run`). Holdout (последние 182 дня
     снапшота) по умолчанию не трогается — как в CLI бэктестера; касание только при
     touch_holdout=true и записывается в data/market_data.db (holdout_touches).
     Данные — только локальный data/market_data.db: сети нет, недостающие свечи —
     ошибка с подсказкой `python -m src.backtest download`.

Конфигурация v1 (JSON):
  {"v": 1, "question": "...", "label": "...", "inst": "BTC-USDT", "bar": "1H",
   "since": "2022-01-01", "until": null, "strategy": "meanrev", "params": {...},
   "costs": {"fee_mult": 1.0, "slip_mult": 1.0}  // или taker_fee, maker_fee, slippage_bps,
                                                 // limit_fill, limit_fee
   "initial_cash": 10000, "touch_holdout": false, "assumptions": [...]}
"""
from __future__ import annotations

import hashlib
import inspect
import json
import re
import time
from collections import Counter
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

from src.analytics.common import RUNS_DIR, clean, write_text, dump_json
from src.backtest.data import BAR_MS, DB_PATH, MarketDataStore, iso_to_ms, load_dataset
from src.backtest.engine import Backtest, CostModel
from src.backtest.meanrev import MeanReversion
from src.backtest.strategies import BuyAndHold, SmaCross
from src.backtest.walkforward import WalkForwardConfig, holdout_start

CONFIG_VERSION = 1
STRATEGIES = {"buy_hold": BuyAndHold, "sma_cross": SmaCross, "meanrev": MeanReversion}
STRATEGY_TITLES = {"buy_hold": "Buy & hold в рамках риск-контура",
                   "sma_cross": "Пересечение SMA(fast)/SMA(slow)",
                   "meanrev": "Mean reversion RSI(14) + BB(20, 2), ATR-стоп"}
DEFAULTS = {"inst": "BTC-USDT", "bar": "1H", "since": "2022-01-01", "strategy": "sma_cross",
            "initial_cash": 10_000.0}
COST_KEYS = {"fee_mult", "slip_mult", "taker_fee", "maker_fee", "slippage_bps", "limit_fill",
             "limit_fee"}
MIN_BARS = 500                 # как в CLI бэктестера
TRADE_LIMIT = 300              # сколько сделок хранить в файле прогона
WF = WalkForwardConfig()
_INST = re.compile(r"^[A-Z0-9]{2,15}-[A-Z0-9]{2,10}$")


class ConfigError(ValueError):
    """Конфигурация не прошла проверку: problems — список причин."""

    def __init__(self, problems: list[str]):
        super().__init__("; ".join(problems))
        self.problems = problems


# --- 1. Черновик из вопроса ---

_ALIASES = {"btc": "BTC", "биткоин": "BTC", "биток": "BTC", "eth": "ETH", "эфир": "ETH",
            "sol": "SOL", "солан": "SOL"}
_BARS_RE = re.compile(r"(?<![\w])(1m|3m|5m|15m|30m|1h|2h|4h|6h|12h|1d)(?![\w])", re.I)
_BAR_WORDS = [(re.compile(r"4\s*-?\s*час", re.I), "4H"), (re.compile(r"15\s*-?\s*мин", re.I), "15m"),
              (re.compile(r"днев|дневк|1\s*д(ень|н)", re.I), "1D"),
              (re.compile(r"(?<!\d)час(ов|ой|ах|ам)?\b|почасов", re.I), "1H")]


def _norm_bar(text: str) -> str:
    t = text.lower()
    return t[:-1] + t[-1].upper() if t[-1] in "hd" else t


def draft_from_text(question: str, now: Optional[datetime] = None) -> dict:
    """Черновик конфигурации по тексту вопроса. Не угаданное — дефолт + запись в assumptions."""
    now = now or datetime.now(timezone.utc)
    q = question.strip()
    low = q.lower()
    cfg: dict[str, Any] = {"v": CONFIG_VERSION, "question": q, "label": "", "params": {},
                           "costs": {"fee_mult": 1.0, "slip_mult": 1.0}, "touch_holdout": False,
                           "until": None}
    assumptions: list[str] = []
    understood: list[str] = []

    m = re.search(r"\b([A-Za-z0-9]{2,10})[-/]?(USDT|USDC)\b", q, re.I)
    if m:
        cfg["inst"] = f"{m.group(1).upper()}-{m.group(2).upper()}"
        understood.append(f"инструмент {cfg['inst']}")
    else:
        base = next((v for k, v in _ALIASES.items() if re.search(rf"\b{k}", low)), None)
        if base:
            cfg["inst"] = f"{base}-USDT"
            understood.append(f"инструмент {cfg['inst']} (по названию монеты)")
        else:
            cfg["inst"] = DEFAULTS["inst"]
            assumptions.append(f"инструмент не указан — {DEFAULTS['inst']}")

    m = _BARS_RE.search(q)
    bar = _norm_bar(m.group(1)) if m else next((b for rx, b in _BAR_WORDS if rx.search(q)), None)
    if bar and bar in BAR_MS:
        cfg["bar"] = bar
        understood.append(f"таймфрейм {bar}")
    else:
        cfg["bar"] = DEFAULTS["bar"]
        assumptions.append(f"таймфрейм не указан — {DEFAULTS['bar']}")

    dates = re.findall(r"\b(20\d\d-\d\d-\d\d)\b", q)
    rel = re.search(r"последни[йех]\s+(\d+)?\s*(год|года|лет|месяц|месяца|месяцев|дн|день|дня|дней)", low)
    year = re.search(r"\bс\s+(20\d\d)\b", low)
    if dates:
        cfg["since"] = dates[0]
        if len(dates) > 1:
            cfg["until"] = dates[1]
        understood.append("период " + " → ".join(dates[:2]))
    elif rel:
        n = int(rel.group(1) or 1)
        unit = rel.group(2)
        days = 365 * n if unit.startswith(("год", "лет")) else 30 * n if unit.startswith("месяц") else n
        cfg["since"] = (now - timedelta(days=days)).strftime("%Y-%m-%d")
        understood.append(f"период — последние {days} дн. (с {cfg['since']})")
        assumptions.append("holdout (последние 182 дня снапшота) исключается: при коротком "
                           "периоде в прогон может не попасть ничего — проверьте bars_used")
    elif year:
        cfg["since"] = f"{year.group(1)}-01-01"
        understood.append(f"период с {cfg['since']}")
    else:
        cfg["since"] = DEFAULTS["since"]
        assumptions.append(f"период не указан — с {DEFAULTS['since']} до последней свечи в базе")

    if re.search(r"mean.?rev|возврат.{0,6}средн|боллиндж|bollinger|\brsi\b|перепрод", low):
        cfg["strategy"] = "meanrev"
    elif re.search(r"buy.?(and|&|n)?.?hold|купить и держать|\bхолд", low):
        cfg["strategy"] = "buy_hold"
    elif re.search(r"\bsma\b|скользящ|пересечени|\bma\s*\d", low):
        cfg["strategy"] = "sma_cross"
    else:
        cfg["strategy"] = DEFAULTS["strategy"]
        assumptions.append(f"стратегия не распознана — {DEFAULTS['strategy']} (база сравнения)")
    if "strategy" in cfg and not any(a.startswith("стратегия") for a in assumptions):
        understood.append(f"стратегия {cfg['strategy']}")

    p = cfg["params"]
    m = re.search(r"(?:sma|ma)\s*(\d{1,3})\s*[/x×,]\s*(\d{1,3})", low)
    if m and cfg["strategy"] == "sma_cross":
        p["fast"], p["slow"] = int(m.group(1)), int(m.group(2))
        understood.append(f"SMA {p['fast']}/{p['slow']}")
    m = re.search(r"rsi\D{0,12}(?:ниже|<|меньше|под)\s*(\d{1,2})", low)
    if m and cfg["strategy"] == "meanrev":
        p["rsi_entry"] = float(m.group(1))
        understood.append(f"вход RSI < {m.group(1)}")
    m = re.search(r"стоп\D{0,10}(\d+(?:[.,]\d+)?)\s*(atr|атр)", low)
    if m and cfg["strategy"] == "meanrev":
        p["stop_atr_mult"] = float(m.group(1).replace(",", "."))
        understood.append(f"стоп {p['stop_atr_mult']} ATR")
    m = re.search(r"стоп\D{0,10}(\d+(?:[.,]\d+)?)\s*%", low)
    if m and cfg["strategy"] == "sma_cross":
        p["stop_pct"] = float(m.group(1).replace(",", "."))
        understood.append(f"стоп {p['stop_pct']}%")

    costs = cfg["costs"]
    if re.search(r"без\s+комисси", low):
        costs["fee_mult"] = 0.0
        understood.append("комиссии ×0")
    m = re.search(r"комисси\w*\D{0,8}[x×]\s*(\d+(?:[.,]\d+)?)", low)
    if m:
        costs["fee_mult"] = float(m.group(1).replace(",", "."))
        understood.append(f"комиссии ×{costs['fee_mult']}")
    m = re.search(r"проскальзыван\w*\D{0,8}[x×]\s*(\d+(?:[.,]\d+)?)", low)
    if m:
        costs["slip_mult"] = float(m.group(1).replace(",", "."))
        understood.append(f"проскальзывание ×{costs['slip_mult']}")
    if costs == {"fee_mult": 1.0, "slip_mult": 1.0}:
        assumptions.append("издержки базовые: taker 0.10%, maker 0.08%, проскальзывание 5 бп/сторону")

    m = re.search(r"(\d[\d\s]{2,9})\s*(usdt|\$|долл)", low)
    if m:
        cfg["initial_cash"] = float(m.group(1).replace(" ", ""))
        understood.append(f"стартовый капитал {cfg['initial_cash']:.0f} USDT")
    else:
        cfg["initial_cash"] = DEFAULTS["initial_cash"]
        assumptions.append("стартовый капитал 10 000 USDT (на проценты не влияет, влияет на "
                           "округление объёма к lotSz)")
    if re.search(r"holdout|отложенн", low):
        assumptions.append("holdout упомянут в вопросе, но касание включается только вручную "
                           "(touch_holdout=true) и фиксируется в market_data.db")
    if not p:
        assumptions.append("параметры стратегии — дефолты конструктора")
    cfg["label"] = f"{cfg['strategy']} {cfg['inst']} {cfg['bar']}"
    cfg["understood"] = understood
    cfg["assumptions"] = assumptions
    return cfg


# --- 2. Проверка ---

def strategy_params(name: str) -> dict[str, Any]:
    """Допустимые параметры стратегии и их дефолты (аргументы конструктора)."""
    sig = inspect.signature(STRATEGIES[name].__init__)
    return {k: (None if v.default is inspect.Parameter.empty else v.default)
            for k, v in sig.parameters.items() if k != "self" and v.kind is v.POSITIONAL_OR_KEYWORD}


def _check_type(value: Any, default: Any) -> bool:
    if default is None or value is None:
        return True
    if isinstance(default, bool):
        return isinstance(value, bool)
    if isinstance(default, (int, float)):
        return isinstance(value, (int, float)) and not isinstance(value, bool)
    if isinstance(default, (tuple, list)):
        return isinstance(value, (tuple, list))
    return isinstance(value, type(default))


def cost_model(costs: dict) -> CostModel:
    base = CostModel()
    explicit = {k: costs[k] for k in ("taker_fee", "maker_fee", "slippage_bps", "limit_fill",
                                      "limit_fee") if k in costs}
    if explicit:
        base = CostModel(**{**{"taker_fee": base.taker_fee, "maker_fee": base.maker_fee,
                               "slippage_bps": base.slippage_bps, "limit_fill": base.limit_fill,
                               "limit_fee": base.limit_fee}, **explicit})
    return base.scaled(fee_mult=float(costs.get("fee_mult", 1.0)),
                       slip_mult=float(costs.get("slip_mult", 1.0)))


def validate(cfg: dict) -> dict:
    """Нормализованная копия конфигурации или ConfigError со всеми проблемами сразу."""
    problems: list[str] = []
    if not isinstance(cfg, dict):
        raise ConfigError(["конфигурация — не объект JSON"])
    out = dict(cfg)
    if out.get("v", CONFIG_VERSION) != CONFIG_VERSION:
        problems.append(f"v={out.get('v')!r}: поддерживается только {CONFIG_VERSION}")
    inst = str(out.get("inst", "")).upper()
    if not _INST.match(inst):
        problems.append(f"inst={out.get('inst')!r}: ожидается вида BTC-USDT")
    out["inst"] = inst
    if out.get("bar") not in BAR_MS:
        problems.append(f"bar={out.get('bar')!r}: допустимо {', '.join(BAR_MS)}")
    since_ms = until_ms = None
    for key in ("since", "until"):
        val = out.get(key)
        if val in (None, "") and key == "until":
            out[key] = None
            continue
        try:
            ms = iso_to_ms(str(val))
        except (ValueError, TypeError):
            problems.append(f"{key}={val!r}: ожидается дата ISO (2024-01-01 или 2024-01-01T00:00)")
            continue
        if key == "since":
            since_ms = ms
        else:
            until_ms = ms
    if since_ms is not None and until_ms is not None and until_ms <= since_ms:
        problems.append("until должен быть позже since")
    name = out.get("strategy")
    if name not in STRATEGIES:
        problems.append(f"strategy={name!r}: доступно {', '.join(STRATEGIES)}")
    else:
        allowed = strategy_params(name)
        params = out.get("params") or {}
        if not isinstance(params, dict):
            problems.append("params — не объект")
            params = {}
        for k, v in params.items():
            if k not in allowed:
                problems.append(f"params.{k}: у {name} нет такого параметра "
                                f"(есть: {', '.join(allowed)})")
            elif not _check_type(v, allowed[k]):
                problems.append(f"params.{k}={v!r}: тип не совпадает с дефолтом {allowed[k]!r}")
        out["params"] = params
        if not problems:
            try:
                STRATEGIES[name](**params)
            except (ValueError, TypeError) as exc:
                problems.append(f"params: стратегия отклонила параметры — {exc}")
    costs = out.get("costs") or {}
    if not isinstance(costs, dict):
        problems.append("costs — не объект")
        costs = {}
    unknown = set(costs) - COST_KEYS
    if unknown:
        problems.append(f"costs: неизвестные поля {sorted(unknown)}")
    else:
        try:
            for k in ("fee_mult", "slip_mult"):
                if float(costs.get(k, 1.0)) < 0:
                    problems.append(f"costs.{k} < 0")
            cost_model(costs)
        except (ValueError, TypeError) as exc:
            problems.append(f"costs: {exc}")
    out["costs"] = costs
    cash = out.get("initial_cash", DEFAULTS["initial_cash"])
    if isinstance(cash, bool) or not isinstance(cash, (int, float)) or cash <= 0:
        problems.append(f"initial_cash={cash!r}: нужно число > 0")
    out["initial_cash"] = float(cash) if isinstance(cash, (int, float)) else cash
    if not isinstance(out.get("touch_holdout", False), bool):
        problems.append("touch_holdout — только true/false")
    out["touch_holdout"] = bool(out.get("touch_holdout", False))
    if problems:
        raise ConfigError(problems)
    out["v"] = CONFIG_VERSION
    return out


# --- 3. Прогон ---

def config_hash(cfg: dict) -> str:
    keys = ("inst", "bar", "since", "until", "strategy", "params", "costs", "initial_cash",
            "touch_holdout")
    blob = json.dumps({k: cfg.get(k) for k in keys}, sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()[:12]


def _trade_row(t) -> dict:
    return {"entry_ts": t.entry_ts, "exit_ts": t.exit_ts, "entry_px": t.entry_px,
            "exit_px": t.exit_px, "pnl": round(t.pnl, 4), "pnl_pct": round(t.pnl_pct * 100, 4),
            "fees": round(t.fees, 4), "slippage": round(t.slippage, 4),
            "exit_reason": t.exit_reason, "bars_held": t.bars_held, "tag": t.tag,
            "exit_tag": t.exit_tag}


def run_config(cfg: dict, db_path=DB_PATH) -> dict:
    """Проверка + прогон существующим Backtest. Возвращает запись прогона (dict для JSON)."""
    cfg = validate(cfg)
    store = MarketDataStore(db_path)
    inst, bar = cfg["inst"], cfg["bar"]
    spec = store.get_instrument(inst)
    lo, hi, n = store.ts_range(inst, bar)
    if spec is None or not n:
        raise ConfigError([f"нет данных {inst} {bar} в {db_path} — сначала "
                           f"`python -m src.backtest download --inst {inst} --bar {bar} "
                           f"--since {cfg['since']}` (публичные свечи, без ключей)"])
    since_ms = iso_to_ms(cfg["since"])
    until_ms = iso_to_ms(cfg["until"]) if cfg["until"] else hi + BAR_MS[bar]
    ds = load_dataset(store, inst, bar, start_ms=since_ms, end_ms=until_ms)
    if len(ds.bars) < MIN_BARS:
        raise ConfigError([f"{inst} {bar}: в периоде {len(ds.bars)} баров, нужно ≥ {MIN_BARS}"])
    h_idx, h_ts = holdout_start([b.ts for b in ds.bars], ds.bar_ms, WF.holdout_days)
    touches = None
    if cfg["touch_holdout"]:
        bars = ds.bars
        touches = store.log_holdout_touch(ds.dataset_id, cfg["strategy"], cfg["params"])
    else:
        bars = ds.bars[:h_idx]
    if len(bars) < MIN_BARS:
        raise ConfigError([f"без holdout остаётся {len(bars)} баров (< {MIN_BARS}): расширьте "
                           "период (since раньше) — holdout = последние 182 дня снапшота"])
    strategy = STRATEGIES[cfg["strategy"]](**cfg["params"])
    costs = cost_model(cfg["costs"])
    t0 = time.time()
    res = Backtest(bars, strategy, spec, bar=bar, costs=costs, initial_cash=cfg["initial_cash"],
                   dataset_id=ds.dataset_id).run()
    elapsed = time.time() - t0
    metrics = res.metrics()
    breaker = [{"ts": ts, "event": ev} for ts, ev in res.risk_events
               if "breaker" in ev or "pause" in ev or "block" in ev][:50]
    run = {
        "v": CONFIG_VERSION,
        "kind": "backtest-run",
        "run_id": f"{config_hash(cfg)}-{ds.dataset_id[:8]}",
        "created_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "config": cfg,
        "engine": {"module": "src.backtest.engine.Backtest", "strategy_name": res.strategy,
                   "params_effective": res.params, "warmup_bars": strategy.warmup,
                   "elapsed_s": round(elapsed, 2)},
        "dataset": {"inst": inst, "bar": bar, "dataset_id": ds.dataset_id,
                    "first_ts": ds.first_ts, "last_ts": ds.last_ts, "bars_total": len(ds.bars),
                    "bars_used": len(bars), "used_first_ts": bars[0].ts, "used_last_ts": bars[-1].ts,
                    "gaps": len(ds.gaps), "missing_bars": sum(g.missing for g in ds.gaps),
                    "anomalies": len(ds.anomalies), "anomaly_examples": ds.anomalies[:3],
                    "holdout": {"days": WF.holdout_days, "start_ts": h_ts,
                                "excluded": not cfg["touch_holdout"], "touch_no": touches}},
        "costs": {"taker_fee": round(costs.taker_fee, 10), "maker_fee": round(costs.maker_fee, 10),
                  "slippage_bps": round(costs.slippage_bps, 10), "limit_fill": costs.limit_fill,
                  "limit_fee": costs.limit_fee},
        "metrics": metrics,
        "exit_reasons": dict(Counter(t.exit_reason for t in res.trades)),
        "rejections": res.rejections,
        "risk_events": len(res.risk_events),
        "risk_events_sample": breaker,
        "unfilled_at_end": res.unfilled_at_end,
        "trades_total": len(res.trades),
        "trades": [_trade_row(t) for t in res.trades[-TRADE_LIMIT:]],
        "trades_truncated": max(0, len(res.trades) - TRADE_LIMIT),
    }
    from src.analytics.bt_summary import limitations   # ограничения пишутся в файл прогона
    run["limitations"] = limitations(run)
    return clean(run)


def save_run(run: dict, runs_dir=RUNS_DIR):
    return write_text(runs_dir / f"{run['run_id']}.json", dump_json(run) + "\n")
