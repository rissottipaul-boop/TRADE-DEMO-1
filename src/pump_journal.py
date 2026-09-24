"""Журнал рискового кармана и расчёт его остатков кодом (задача PUMP-JOURNAL). Только чтение.

    python -m src.pump_journal                  # остатки кармана перед входом
    python -m src.pump_journal --json           # машинный отчёт (для Pump Risk Taker и Ops Sentinel)
    python -m src.pump_journal --journal data/pump_journal.jsonl --pocket pump-pocket.json

Читает data/pump_journal.jsonl и pump-pocket.json (оба только чтение, в сеть не ходит) и считает
остаток бюджета кармана, дневной PnL и просадку против лимитов кармана, число открытых позиций
и их риск до стопов, а из этого — можно ли новый вход и с каким риском и размером. Допуск
движка (kill-switch, breaker'ы) сюда не входит: его показывает `python -m src.ops status`.

Журнал data/pump_journal.jsonl
------------------------------
Строка на событие: JSON-объект в одну строку, UTF-8, только дописывание (старые строки не
правятся). Общие поля всех строк:
  ts        время события, ISO с поясом, в поясе проекта +05:00 ("2026-09-24T11:05:12+05:00");
            без пояса считается +05:00, с "Z" — UTC
  event     "scan" | "entry" | "stop" | "exit" | "error"; строки других типов расчёт пропускает
  v         версия формата строки: 1. Нет поля — строка до PUMP-JOURNAL (v0): ts, event, pair,
            price, size, stop, pnl, reason — те же имена полей, читается так же
  source    кто записал: "src.pump_scanner" (scan) или "pump-risk-taker" (сделки и ошибки)
  reason    одна строка обоснования; у entry обязательна (без обоснования сделка не открывается)
Неизвестные поля не мешают; строка новее v1 читается по полям v1 (пометка в notes).

scan — пишет сканер после живого скана, поля v=1 — docstring src/pump_scanner.py. Строки scan
до PUMP-CODIFY (v0): pair, price, size, stop, pnl = null и reason с итогом скана. В расчёте scan
только считается.

entry — вход исполнен (строка после fill, а не после отправки ордера):
  pair      инструмент, "FET-USDT" (читается и inst_id)
  trade_id  ключ сделки, общий у её entry, stop, exit и error, — clOrdId входного ордера (pmp…).
            Необязателен: без него ключ сделки — pair (одна открытая позиция на пару, как в v0)
  side      "buy" (по умолчанию; карман — только спот); "sell" считается зеркально
  price     средняя цена исполнения, USDT
  size      исполненный объём, базовая валюта
  cost_usdt стоимость входа, USDT (необязательно: price × size; читается и usdt)
  fee_usdt  комиссия входа, USDT; расходом считается модуль (у OKX знак минус; читается и fee)
  stop      цена стопа при входе; null — стоп следует отдельной строкой stop
  risk_usdt риск до стопа (информационно: расчёт — по текущему стопу, см. «Расчёт»)
  ord_id, cl_ord_id  ID ордера на бирже и его clOrdId; ord_type, limit_px, td_mode,
            scan_candle_ts — по желанию (свеча сигнала — связь со строкой scan)
stop — стоп выставлен или перенесён (algo-ордер; у трейлинга — каждая перестановка):
  trade_id, pair, size
  stop      цена срабатывания стопа; null — стоп снят. Нет поля — берётся sl_trigger из orders
            (самый дальний от цены для buy — консервативно)
  orders    [{algo_id, algo_cl_ord_id, ord_type, size, sl_trigger, tp_trigger, exec}] — algo-ордера
            стопа (OCO со ступенями тейка); один ордер — можно просто algo_id, algo_cl_ord_id
exit — позиция закрыта целиком или частью (тейк, стоп, трейлинг, вручную, аварийно):
  trade_id, pair
  price     средняя цена выхода, USDT
  size      закрытый объём, базовая валюта; нет — закрыта вся позиция
  pnl       реализованный PnL этой части, USDT, за вычетом её комиссий входа и выхода. Нет pnl —
            расчёт (price − средняя цена входа) × size − комиссии с предупреждением; нет и цен —
            принимается убыток до стопа (консервативно), тоже с предупреждением
  fee_usdt  комиссия выхода, USDT (читается и fee)
  kind      "take" | "stop" | "trail" | "manual" | "error"
  ord_id, cl_ord_id, algo_id
error — ошибка: скан с кодом 2, ордер отклонён, стоп не встал, выход не исполнился:
  kind      "scan" | "order" | "stop" | "exit" | …;  code — sCode OKX или код выхода команды
  trade_id, pair, reason
  Деньги error не двигает: закрытие позиции после ошибки — отдельной строкой exit с kind
  "error". Если в error всё же есть pnl, он учитывается (убыток не теряется) с предупреждением.

Примеры (v=1; entry и stop — по первой сделке кармана 24.09 20:28, id сокращены):
  {"ts":"2026-09-24T20:28:10+05:00","event":"entry","v":1,"source":"pump-risk-taker","pair":"FET-USDT","side":"buy","price":0.21941,"size":4688,"cost_usdt":1028.59,"fee_usdt":1.0286,"stop":0.214,"risk_usdt":27.39,"ord_id":"3951…","cl_ord_id":"pmpa413…","scan_candle_ts":"2026-09-24T14:00:00Z","reason":"скан №6: FET-USDT +5.19%, объём и ликвидность пройдены; риск до стопа 27.4 ≤ 103.63"}
  {"ts":"2026-09-24T20:28:27+05:00","event":"stop","v":1,"source":"pump-risk-taker","pair":"FET-USDT","size":4688,"stop":0.214,"orders":[{"algo_id":"3951…","algo_cl_ord_id":"pmpb06e…","ord_type":"oco","size":2344,"sl_trigger":0.214,"tp_trigger":0.2249},{"algo_id":"3951…","algo_cl_ord_id":"pmp1899…","ord_type":"oco","size":2344,"sl_trigger":0.214,"tp_trigger":0.2304}],"reason":"стоп −2.47% сразу после входа, тейк 1R и 2R"}
  {"ts":"2026-09-24T21:10:03+05:00","event":"exit","v":1,"source":"pump-risk-taker","pair":"FET-USDT","price":0.2249,"size":2344,"pnl":11.1,"fee_usdt":0.53,"kind":"take","algo_id":"3951…","reason":"первая ступень тейка +2.5% (1R)"}
  {"ts":"2026-09-24T22:00:41+05:00","event":"error","v":1,"source":"pump-risk-taker","kind":"stop","code":"51008","pair":"LTC-USDT","reason":"стоп не встал: недостаточно баланса; позиция закрыта строкой exit kind=error"}

Расчёт (строки — в порядке файла, это порядок событий)
-----------------------------------------------------
- Позиция: entry открывает или наращивает её по ключу сделки, stop меняет стоп, exit уменьшает
  или закрывает. exit/stop без trade_id ищет единственную открытую позицию по pair.
- Реализованный PnL — сумма pnl строк exit (и error с pnl) за весь журнал.
- Остаток бюджета (свободно для входа) = min(budget_usdt, budget_usdt + реализованный PnL) −
  стоимость открытых позиций. Прибыль бюджет не расширяет: лимиты кармана задаёт человек.
  Стоимость входа неизвестна — принимается равной лимиту позиции.
- Дневной PnL — сумма pnl за текущие сутки. Граница суток — 00:00 UTC (05:00 +05:00), как в
  роли Pump Risk Taker («00:00 UTC для дневного») и в src/risk.py; поле day_tz в
  pump-pocket.json (например "+05:00"), если человек его добавит, заменяет её. Дневной убыток =
  max(0, −дневной PnL) против daily_loss_limit. Строка с pnl без читаемого ts: убыток идёт в
  текущие сутки, прибыль — нет.
- Просадка — от пика кривой budget_usdt + накопленный PnL (пик не ниже budget_usdt) против
  max_drawdown. Лимит достигнут хоть раз — входов нет до решения человека (задача needs-user):
  сама просадка не «отрастает».
- Риск открытой позиции = (цена входа − стоп) × размер, не меньше 0, + комиссия входа +
  комиссия выхода по стопу (по ставке входа); стопа нет — вся стоимость.
- Новый вход: риск ≤ min(max_risk_per_trade, остаток дневного лимита − риск открытых позиций,
  остаток просадки − риск открытых позиций), размер ≤ min(max_position_pct, свободный бюджет);
  входов нет, если открыто max_open_positions позиций или есть позиция без стопа.
Поле max_position_pct в pump-pocket.json исторически хранит лимит позиции в USDT (не %).

Коды выхода: 0 — расчёт выполнен, новый вход по лимитам кармана разрешён; 1 — расчёт выполнен,
новых входов нет (причины — в blocks); 2 — ошибка: pump-pocket.json или журнал не читаются,
неверные аргументы.
"""
import argparse
import json
import math
import sys
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Iterable, Optional

from .pump_scanner import JOURNAL_PATH, POCKET_PATH, PROJECT_TZ, PocketError

FORMAT_VERSION = 1
SOURCE = "pump-risk-taker"            # source строк сделок и ошибок
EVENTS = ("scan", "entry", "stop", "exit", "error")
DAY_TZ = timezone.utc                 # граница суток 00:00 UTC: роль Pump Risk Taker и src/risk.py
POCKET_LIMITS = ("budget_usdt", "daily_loss_limit", "max_drawdown", "max_risk_per_trade",
                 "max_position_pct", "max_open_positions")
EPS = 1e-9


class JournalError(RuntimeError):
    """Журнал не читается (не файл, нет доступа)."""


# --- Разбор значений ---

def parse_tz(text: str) -> timezone:
    """Смещение вида +05:00 (или Z) -> timezone."""
    raw = str(text).strip()
    if raw.upper() in ("Z", "UTC"):
        return timezone.utc
    try:
        tz = datetime.strptime(raw, "%z").tzinfo
    except ValueError:
        raise ValueError(f"ожидается смещение вида +05:00, получено {text!r}") from None
    return tz  # type: ignore[return-value]


def parse_ts(value) -> Optional[datetime]:
    """ts строки журнала -> aware datetime; без пояса — +05:00 проекта; не читается — None."""
    if not isinstance(value, str) or not value.strip():
        return None
    text = value.strip()
    if text[-1] in "Zz":
        text = text[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(text)
    except ValueError:
        return None
    return dt if dt.tzinfo is not None else dt.replace(tzinfo=PROJECT_TZ)


def _num(value) -> Optional[float]:
    """Число из JSON (OKX отдаёт строки): конечное float или None."""
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        x = float(value)
    elif isinstance(value, str):
        try:
            x = float(value.strip())
        except ValueError:
            return None
    else:
        return None
    return x if math.isfinite(x) else None


def _text(value) -> Optional[str]:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _iso(dt: Optional[datetime]) -> Optional[str]:
    return dt.astimezone(PROJECT_TZ).isoformat(timespec="seconds") if dt else None


# --- Карман ---

@dataclass(frozen=True)
class Pocket:
    """Лимиты кармана из pump-pocket.json (только чтение)."""
    name: str
    mode: Optional[str]
    budget: float
    daily_loss_limit: float
    max_drawdown: float
    max_risk_per_trade: float
    max_position: float              # max_position_pct: имя историческое, значение — USDT
    max_open_positions: int
    stop_range: Optional[tuple[float, float]]   # диапазон стопа, % от цены входа
    day_tz: timezone
    day_tz_source: str               # "pocket" — задан полем day_tz, "default" — 00:00 UTC
    path: str


def read_pocket(path: Path) -> Pocket:
    """Читает pump-pocket.json. Нет файла, не JSON, нет лимита — PocketError."""
    name = Path(path).as_posix()
    try:
        text = Path(path).read_text(encoding="utf-8-sig")     # BOM от PowerShell — не ошибка
    except FileNotFoundError:
        raise PocketError(f"{name} не найден") from None
    except (OSError, UnicodeDecodeError) as exc:
        raise PocketError(f"{name} не читается: {exc}") from None
    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        raise PocketError(f"{name} не читается: не JSON ({exc.msg}, строка {exc.lineno})") from None
    if not isinstance(data, dict):
        raise PocketError(f"{name}: ожидается объект JSON")
    limits = {}
    for key in POCKET_LIMITS:
        value = data.get(key)
        if isinstance(value, bool) or not isinstance(value, (int, float)) \
                or not 0 < value < math.inf:
            raise PocketError(f"{name}: {key} = {value!r} — нужно число > 0")
        limits[key] = float(value)
    stop_range = None
    raw = data.get("stop_loss_range")
    if isinstance(raw, (list, tuple)) and len(raw) == 2:
        lo, hi = _num(raw[0]), _num(raw[1])
        if lo is not None and hi is not None and 0 < lo <= hi:
            stop_range = (lo, hi)
    day_tz, tz_source = DAY_TZ, "default"
    if data.get("day_tz") is not None:
        try:
            day_tz, tz_source = parse_tz(data["day_tz"]), "pocket"
        except ValueError as exc:
            raise PocketError(f"{name}: day_tz — {exc}") from None
    return Pocket(name=str(data.get("pocket") or "pump-pocket"), mode=_text(data.get("mode")),
                  budget=limits["budget_usdt"], daily_loss_limit=limits["daily_loss_limit"],
                  max_drawdown=limits["max_drawdown"],
                  max_risk_per_trade=limits["max_risk_per_trade"],
                  max_position=limits["max_position_pct"],
                  max_open_positions=int(limits["max_open_positions"]),
                  stop_range=stop_range, day_tz=day_tz, day_tz_source=tz_source, path=name)


# --- Журнал ---

@dataclass
class Journal:
    path: str
    exists: bool
    records: list[tuple[int, dict]]     # (номер строки в файле, объект)
    bad: list[str]                      # нечитаемые строки: «строка N: причина»


def read_journal(path: Path) -> Journal:
    """Читает JSONL. Нет файла — пустой журнал; битая строка — в bad, расчёт продолжается."""
    path = Path(path)
    name = path.as_posix()
    if not path.exists():
        return Journal(name, False, [], [])
    try:
        raw = path.read_bytes()
    except OSError as exc:
        raise JournalError(f"{name} не читается: {exc}") from None
    text = raw.decode("utf-8-sig", errors="replace")
    records, bad = [], []
    for n, line in enumerate(text.splitlines(), 1):
        if not line.strip():
            continue
        try:
            rec = json.loads(line)
        except json.JSONDecodeError as exc:
            bad.append(f"строка {n}: не JSON ({exc.msg})")
            continue
        if not isinstance(rec, dict):
            bad.append(f"строка {n}: не объект JSON")
            continue
        records.append((n, rec))
    return Journal(name, True, records, bad)


# --- Расчёт ---

@dataclass
class Position:
    key: str
    trade_id: Optional[str]
    pair: Optional[str]
    side: str                       # "buy" | "sell"
    size: Optional[float]           # остаток, базовая валюта
    cost: float                     # стоимость остатка по цене входа, USDT
    cost_known: bool
    stop: Optional[float]
    opened: Optional[str]
    line: int
    fee: float = 0.0                # комиссия входа остатка, USDT

    @property
    def fee_rate(self) -> float:
        return self.fee / self.cost if self.cost_known and self.cost > EPS else 0.0

    @property
    def price(self) -> Optional[float]:
        if self.size and self.size > EPS and self.cost_known:
            return self.cost / self.size
        return None

    def risk(self) -> float:
        """Убыток при срабатывании стопа с комиссиями входа и выхода, USDT; без стопа или без
        цен — вся стоимость."""
        price = self.price
        if self.stop is None or price is None or self.size is None:
            return self.cost
        per_unit = price - self.stop if self.side == "buy" else self.stop - price
        return max(0.0, per_unit * self.size) + self.fee + self.fee_rate * self.stop * self.size

    def public(self) -> dict:
        price = self.price
        stop_pct = None
        if price and self.stop is not None:
            stop_pct = (self.stop - price) / price * 100 * (1 if self.side == "buy" else -1)
        return {"trade_id": self.trade_id, "pair": self.pair, "side": self.side,
                "size": self.size, "price": _r(price, 8), "cost_usdt": _r(self.cost, 2),
                "cost_known": self.cost_known, "stop": self.stop, "stop_pct": _r(stop_pct, 2),
                "risk_usdt": _r(self.risk(), 2), "opened": self.opened, "line": self.line}


def _r(x: Optional[float], nd: int) -> Optional[float]:
    return None if x is None else round(x + 0.0, nd)


def _first_num(rec: dict, *keys: str) -> Optional[float]:
    """Первое числовое значение из полей keys (канон и алиасы)."""
    for key in keys:
        x = _num(rec.get(key))
        if x is not None:
            return x
    return None


def stop_of(rec: dict, side: str) -> tuple[bool, Optional[float]]:
    """Стоп строки stop: (задан ли, цена). Поле stop (null — стоп снят) или sl_trigger из orders:
    самый дальний от цены (для buy — меньший) — консервативно."""
    if "stop" in rec:
        return True, _num(rec.get("stop"))
    orders = rec.get("orders")
    triggers = [x for x in (_num(o.get("sl_trigger")) for o in orders if isinstance(o, dict))
                if x is not None] if isinstance(orders, list) else []
    if not triggers:
        return False, None
    return True, (min(triggers) if side == "buy" else max(triggers))


def _find(positions: dict[str, Position], key: Optional[str],
          pair: Optional[str]) -> Optional[Position]:
    if key and key in positions:
        return positions[key]
    if pair:
        same = [p for p in positions.values() if p.pair == pair]
        if len(same) == 1:
            return same[0]
    return None


def day_bounds(now: datetime, tz: timezone) -> tuple[datetime, datetime]:
    """Текущие сутки в поясе tz: [00:00, следующие 00:00)."""
    local = now.astimezone(tz)
    start = datetime(local.year, local.month, local.day, tzinfo=tz)
    return start, start + timedelta(days=1)


def summarize(records: Iterable[tuple[int, dict]], pocket: Pocket, now: datetime,
              bad: Optional[list[str]] = None) -> dict:
    """Остатки кармана по строкам журнала (в порядке файла) на момент now."""
    day_start, day_end = day_bounds(now, pocket.day_tz)
    warnings = [f"нечитаемая строка журнала — {b}" for b in (bad or [])]
    notes: list[str] = []
    counts = {e: 0 for e in EVENTS}
    other: dict[str, int] = {}
    newer: set = set()
    positions: dict[str, Position] = {}
    realized = 0.0
    peak = pocket.budget
    dd_max = 0.0
    day = {"pnl": 0.0, "entries": 0, "exits": 0, "errors": 0}

    def book_pnl(pnl: float, ts: Optional[datetime], n: int) -> None:
        nonlocal realized, peak, dd_max
        realized += pnl
        equity = pocket.budget + realized
        peak = max(peak, equity)
        dd_max = max(dd_max, peak - equity)
        if ts is None:
            if pnl < 0:
                day["pnl"] += pnl
            warnings.append(f"строка {n}: ts не читается — убыток отнесён к текущим суткам, "
                            f"прибыль нет")
        elif ts >= day_start:
            day["pnl"] += pnl

    for n, rec in records:
        event = rec.get("event")
        if event not in EVENTS:
            label = str(event)
            other[label] = other.get(label, 0) + 1
            continue
        counts[event] += 1
        v = _num(rec.get("v"))
        if v is not None and v > FORMAT_VERSION:
            newer.add(rec.get("v"))
        if event == "scan":
            continue
        ts = parse_ts(rec.get("ts"))
        today = ts is not None and ts >= day_start
        trade_id = _text(rec.get("trade_id"))
        pair = _text(rec.get("pair")) or _text(rec.get("inst_id"))
        if pair:
            pair = pair.upper()

        if event == "entry":
            if today:
                day["entries"] += 1
            key = trade_id or pair
            if key is None:
                key = f"строка {n}"
                warnings.append(f"строка {n}: entry без trade_id и pair — позиция учтена "
                                f"безымянной, закрыть её строкой exit нельзя")
            side = "sell" if str(rec.get("side") or "buy").lower() == "sell" else "buy"
            price, size = _num(rec.get("price")), _num(rec.get("size"))
            usdt = _first_num(rec, "cost_usdt", "usdt")
            fee = abs(_first_num(rec, "fee_usdt", "fee") or 0.0)
            if size is None and usdt is not None and price:
                size = usdt / price
            cost = usdt if usdt is not None else (price * size if price is not None
                                                  and size is not None else None)
            known = cost is not None and cost > 0
            if not known:
                cost = pocket.max_position
                warnings.append(f"строка {n}: стоимость входа {key} неизвестна — принята равной "
                                f"лимиту позиции {pocket.max_position:.2f} USDT")
            if not _text(rec.get("reason")):
                warnings.append(f"строка {n}: entry {key} без обоснования reason")
            stop = _num(rec.get("stop"))
            pos = positions.get(key)
            if pos is None:
                pos = Position(key, trade_id, pair, side, size, cost, known, stop,
                               _iso(ts) or _text(rec.get("ts")), n, fee)
                positions[key] = pos
            else:                                   # добор в ту же сделку
                pos.size = None if pos.size is None or size is None else pos.size + size
                pos.cost += cost
                pos.fee += fee
                pos.cost_known = pos.cost_known and known
                if stop is not None:
                    pos.stop = stop
            if pocket.stop_range and stop is not None and price:
                pct = abs(price - stop) / price * 100
                lo, hi = pocket.stop_range
                if pct < lo - 1e-6 or pct > hi + 1e-6:
                    warnings.append(f"строка {n}: стоп {key} {pct:.2f}% вне диапазона кармана "
                                    f"{lo:g}–{hi:g}%")

        elif event == "stop":
            pos = _find(positions, trade_id or pair, pair)
            if pos is None:
                warnings.append(f"строка {n}: stop для {trade_id or pair or '?'} — открытой "
                                f"позиции нет")
                continue
            given, stop = stop_of(rec, pos.side)
            if not given:
                warnings.append(f"строка {n}: stop {pos.pair or pos.key} без цены стопа (stop или "
                                f"orders[].sl_trigger) — стоп не изменён")
                continue
            pos.stop = stop

        elif event == "exit":
            if today:
                day["exits"] += 1
            pos = _find(positions, trade_id or pair, pair)
            pnl = _num(rec.get("pnl"))
            price, size = _num(rec.get("price")), _num(rec.get("size"))
            fee = abs(_first_num(rec, "fee_usdt", "fee") or 0.0)
            label = trade_id or pair or "?"
            if pos is None:
                if pnl is None:
                    warnings.append(f"строка {n}: exit {label} без открытой позиции и без pnl — "
                                    f"в PnL не учтён")
                else:
                    warnings.append(f"строка {n}: exit {label} без открытой позиции — pnl "
                                    f"учтён")
                    book_pnl(pnl, ts, n)
                continue
            if size is None or pos.size is None or pos.size <= EPS:
                frac = 1.0
                if size is not None and pos.size is None:
                    warnings.append(f"строка {n}: размер позиции {pos.key} неизвестен — exit "
                                    f"закрывает её целиком")
            else:
                if size > pos.size * (1 + 1e-6) + EPS:
                    warnings.append(f"строка {n}: exit {pos.key} закрывает {size:g} при открытых "
                                    f"{pos.size:g}")
                frac = min(1.0, size / pos.size)
            closed = size if size is not None else pos.size
            if pnl is None:
                entry_price = pos.price
                if price is not None and entry_price is not None and closed is not None:
                    sign = 1 if pos.side == "buy" else -1
                    pnl = sign * (price - entry_price) * closed - pos.fee * frac - fee
                    warnings.append(f"строка {n}: exit {pos.key} без pnl — рассчитан по ценам: "
                                    f"{pnl:+.2f} USDT")
                else:
                    pnl = -pos.risk() * frac
                    warnings.append(f"строка {n}: exit {pos.key} без pnl и цен — принят убыток до "
                                    f"стопа {pnl:+.2f} USDT")
            book_pnl(pnl, ts, n)
            if frac >= 1 - 1e-9:
                del positions[pos.key]
            else:
                pos.cost *= 1 - frac
                pos.fee *= 1 - frac
                pos.size = None if pos.size is None else pos.size - closed

        elif event == "error":
            if today:
                day["errors"] += 1
            pnl = _num(rec.get("pnl"))
            if pnl is not None and abs(pnl) > EPS:
                warnings.append(f"строка {n}: pnl {pnl:+.2f} в строке error учтён; закрытие "
                                f"позиции пишите строкой exit")
                book_pnl(pnl, ts, n)

    if newer:
        notes.append(f"строки версии {', '.join(sorted(map(str, newer)))} новее схемы "
                     f"v{FORMAT_VERSION}: прочитаны по полям v{FORMAT_VERSION}")
    if other:
        notes.append("строки других типов пропущены: " +
                     ", ".join(f"{k} ({v})" for k, v in sorted(other.items())))

    in_positions = sum(p.cost for p in positions.values())
    open_risk = sum(p.risk() for p in positions.values())
    equity = pocket.budget + realized
    free = min(pocket.budget, equity) - in_positions
    day_loss = max(0.0, -day["pnl"])
    day_left = pocket.daily_loss_limit - day_loss
    dd_now = peak - equity
    dd_left = pocket.max_drawdown - dd_now
    risk_room = min(pocket.max_risk_per_trade, day_left - open_risk, dd_left - open_risk)
    pos_room = min(pocket.max_position, free)
    reset = _iso(day_end)

    blocks = []
    if day_left <= EPS:
        blocks.append(f"дневной лимит убытка исчерпан: {day_loss:.2f}/"
                      f"{pocket.daily_loss_limit:.2f} USDT — входов нет до {reset}")
    if dd_max >= pocket.max_drawdown - EPS:
        blocks.append(f"просадка кармана достигала лимита: {dd_max:.2f}/"
                      f"{pocket.max_drawdown:.2f} USDT — входов нет до решения человека "
                      f"(задача needs-user)")
    if len(positions) >= pocket.max_open_positions:
        blocks.append(f"открыто позиций {len(positions)}/{pocket.max_open_positions}")
    naked = [p.pair or p.key for p in positions.values() if p.stop is None]
    if naked:
        blocks.append("позиция без стопа: " + ", ".join(naked) + " — сначала стоп")
    if risk_room <= EPS and day_left > EPS and dd_max < pocket.max_drawdown - EPS:
        blocks.append(f"риск открытых позиций до стопов {open_risk:.2f} USDT съедает остаток "
                      f"лимита (дневного {day_left:.2f}, просадки {dd_left:.2f})")
    if pos_room <= EPS:
        blocks.append(f"свободного бюджета нет: {free:.2f} USDT")

    return {
        "generated_at": _iso(now),
        "pocket": {"name": pocket.name, "path": pocket.path, "mode": pocket.mode},
        "limits": {"budget_usdt": pocket.budget, "daily_loss_limit": pocket.daily_loss_limit,
                   "max_drawdown": pocket.max_drawdown,
                   "max_risk_per_trade": pocket.max_risk_per_trade,
                   "max_position_usdt": pocket.max_position,
                   "max_open_positions": pocket.max_open_positions,
                   "stop_loss_range": list(pocket.stop_range) if pocket.stop_range else None},
        "budget": {"budget_usdt": pocket.budget, "realized_pnl": _r(realized, 2),
                   "equity_usdt": _r(equity, 2), "in_positions_usdt": _r(in_positions, 2),
                   "free_usdt": _r(free, 2)},
        "day": {"tz": day_start.isoformat()[-6:],
                "tz_source": pocket.day_tz_source,
                "start": _iso(day_start), "end": reset, "pnl": _r(day["pnl"], 2),
                "loss_used": _r(day_loss, 2), "loss_limit": pocket.daily_loss_limit,
                "loss_left": _r(day_left, 2), "entries": day["entries"],
                "exits": day["exits"], "errors": day["errors"]},
        "drawdown": {"peak_usdt": _r(peak, 2), "current_usdt": _r(dd_now, 2),
                     "max_usdt": _r(dd_max, 2), "limit_usdt": pocket.max_drawdown,
                     "left_usdt": _r(dd_left, 2)},
        "positions": {"open": len(positions), "max": pocket.max_open_positions,
                      "risk_usdt": _r(open_risk, 2),
                      "list": [p.public() for p in positions.values()]},
        "entry": {"allowed": not blocks, "risk_max_usdt": _r(max(0.0, risk_room), 2),
                  "position_max_usdt": _r(max(0.0, pos_room), 2), "blocks": blocks},
        "counts": dict(counts, other=sum(other.values()), bad=len(bad or [])),
        "warnings": warnings,
        "notes": notes,
    }


def pocket_status(journal: Path = JOURNAL_PATH, pocket: Path = POCKET_PATH,
                  now: Optional[datetime] = None) -> dict:
    """Остатки кармана по файлам. PocketError / JournalError — не читается карман или журнал."""
    now = now or datetime.now(timezone.utc)
    pk = read_pocket(pocket)
    jr = read_journal(journal)
    report = summarize(jr.records, pk, now, jr.bad)
    report["journal"] = {"path": jr.path, "exists": jr.exists, "lines": len(jr.records) + len(jr.bad)}
    if not jr.exists:
        report["notes"].append(f"журнала {jr.path} нет — сделок не было")
    return report


# --- Текстовый отчёт ---

def render_text(r: dict) -> str:
    b, d, dd, pos, e, c = (r["budget"], r["day"], r["drawdown"], r["positions"], r["entry"],
                           r["counts"])
    mode = f" ({r['pocket']['mode']})" if r["pocket"]["mode"] else ""
    boundary = (f"сутки с 00:00 {d['tz']}" if d["tz_source"] == "pocket"
                else "сутки UTC")
    lines = [
        f"Карман {r['pocket']['name']}{mode} по журналу {r['journal']['path']} — "
        f"{r['generated_at']}",
        f"Строк: {r['journal']['lines']} (scan {c['scan']}, entry {c['entry']}, stop {c['stop']}, "
        f"exit {c['exit']}, error {c['error']}, другие {c['other']}, битые {c['bad']})",
        f"Бюджет: свободно {b['free_usdt']:.2f} из {b['budget_usdt']:.2f} USDT · в позициях "
        f"{b['in_positions_usdt']:.2f} · реализовано всего {b['realized_pnl']:+.2f}",
        f"День ({boundary}, {d['start'][11:16]}–{d['end'][11:16]} +05:00): PnL {d['pnl']:+.2f} · "
        f"убыток {d['loss_used']:.2f}/{d['loss_limit']:.2f} · запас {d['loss_left']:.2f} · "
        f"входов {d['entries']}, выходов {d['exits']}, ошибок {d['errors']}",
        f"Просадка: {dd['current_usdt']:.2f}/{dd['limit_usdt']:.2f} USDT от пика "
        f"{dd['peak_usdt']:.2f} · максимум {dd['max_usdt']:.2f} · запас {dd['left_usdt']:.2f}",
        f"Позиции: {pos['open']}/{pos['max']} · риск до стопов {pos['risk_usdt']:.2f} USDT",
    ]
    for p in pos["list"]:
        stop = "без стопа" if p["stop"] is None else (
            f"стоп {p['stop']:g}" + (f" ({p['stop_pct']:+.2f}%)" if p["stop_pct"] is not None
                                     else ""))
        size = "?" if p["size"] is None else f"{p['size']:g}"
        price = "?" if p["price"] is None else f"{p['price']:g}"
        lines.append(f"  {p['pair'] or '?'} {p['trade_id'] or ''} {size} @ {price} = "
                     f"{p['cost_usdt']:.2f} USDT · {stop} · риск {p['risk_usdt']:.2f}")
    if e["allowed"]:
        lines.append(f"Вход: разрешён — риск на сделку до {e['risk_max_usdt']:.2f} USDT, позиция "
                     f"до {e['position_max_usdt']:.2f} USDT (допуск движка — python -m src.ops "
                     f"status)")
    else:
        lines.append("Вход: запрещён")
        lines += [f"  - {x}" for x in e["blocks"]]
    if r["warnings"]:
        lines.append("Предупреждения:")
        lines += [f"  - {x}" for x in r["warnings"]]
    lines += [f"Заметка: {x}" for x in r["notes"]]
    return "\n".join(lines)


# --- CLI ---

def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="python -m src.pump_journal",
        description="Остатки рискового кармана по журналу: бюджет, дневной PnL, просадка против "
                    "pump-pocket.json. Только чтение. Коды выхода: 0 — вход разрешён, 1 — "
                    "входов нет, 2 — ошибка чтения.")
    p.add_argument("--journal", type=Path, default=JOURNAL_PATH,
                   help=f"журнал кармана (по умолчанию {JOURNAL_PATH.as_posix()})")
    p.add_argument("--pocket", type=Path, default=POCKET_PATH,
                   help=f"лимиты кармана (по умолчанию {POCKET_PATH.as_posix()})")
    p.add_argument("--json", action="store_true", help="отчёт JSON")
    return p


def main(argv: Optional[list[str]] = None, now: Optional[datetime] = None) -> int:
    """CLI. now подставляют тесты (фиксированное время)."""
    args = build_parser().parse_args(argv)
    try:
        report = pocket_status(args.journal, args.pocket, now)
    except (PocketError, JournalError) as exc:
        print(f"Остатки кармана не посчитаны: {exc}", file=sys.stderr)
        return 2
    print(json.dumps(report, ensure_ascii=False, indent=2) if args.json else render_text(report))
    return 0 if report["entry"]["allowed"] else 1


if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):   # −, ·, × в консоли Windows с cp1251
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8")
    sys.exit(main())
