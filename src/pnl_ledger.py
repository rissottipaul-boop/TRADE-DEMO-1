"""Учёт PnL по рукавам и отчёт за день или период (задача PNL-LEDGER) — только чтение с биржи.

    python -m src.pnl_ledger                              # demo, сегодня (+05:00) -> data/reports/
    python -m src.pnl_ledger --mode live                  # live-карман -> data/live/reports/
    python -m src.pnl_ledger --date 2026-09-24            # конкретный день
    python -m src.pnl_ledger --date 2026-09-30 --days 7   # период: данные для LIVE-REVIEW
    python -m src.pnl_ledger --telegram                   # + текст сообщения (отправка — ALERTS-IMPL)

Методика — insights/pnl-ledger.md. Коротко:
1. Движения денег — account/bills (+ bills-archive глубже 7 дней). На demo 24.09
   проверено: сумма balChg по каждой валюте с начала истории равна eq торгового
   счёта; исполнения нативных ботов в bills есть (в trade/fills их нет);
   комиссия уже внутри balChg. Журнал <каталог режима>/reports/pnl_ledger.db
   хранит bills дольше биржи и досинхронизируется при каждом запуске.
2. Атрибуция по рукавам business-plan.md §2.1 — правила ops/sleeves.json:
   ордер нативного бота (ordId из sub-orders) -> рукав бота; иначе владелец по
   префиксу clOrdId (коды реестра src/order_owner.py, если он есть), затем tag;
   неопознанное — рукав default_sleeve («прочее/ручное»), не выбрасывается.
3. PnL рукава — mark-to-market его движений: V(t) = сумма по валютам
   накопленного balChg рукава x цена в USDT; PnL периода = V(t1) - V(t0).
   Разбивка: реализованный (средняя цена, с комиссиями), изменение
   нереализованного, funding (bills type 8), Earn (savings/lending-history).
4. Сверка: изменение стоимости торгового счёта = сумма PnL рукавов + переоценка
   активов вне рукавов + переводы капитала (тождество модели). Независимая
   проверка — снимки equity биржи: equity_curve движка (только чтение) и снимки
   самого ledger.

Ордеров ledger не ставит: Reader пропускает только private_get_* / public_get_*.
Базы движка (bot_state.db, risk_state.db) открываются только на чтение.
"""
import argparse
import json
import logging
import re
import sqlite3
import sys
import time
from collections import defaultdict
from contextlib import closing, contextmanager
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Optional

from .config import MODES, default_mode, load_settings, normalize_mode, state_paths

log = logging.getLogger("okx.pnl_ledger")

RULES_PATH = Path("ops/sleeves.json")
JOURNAL_NAME = "pnl_ledger.db"
DEFAULT_TZ = "+05:00"            # время проекта (ops/board.md)
PAGE = 100                       # максимум OKX для bills, sub-orders, списков ботов
DAY_MS = 86_400_000
BILLS_DEPTH_DAYS = 7             # account/bills — последние 7 дней, глубже — bills-archive (3 месяца)
BACKFILL_DAYS = 7                # глубина первой загрузки журнала
REF_MAX_DEVIATION = 0.05         # снимок equity дальше 5% от модели — другой источник, не сравниваем
PREFILTER_DEVIATION = 0.10       # грубый отсев снимков по текущим ценам, без запросов свечей
PRICE_NOW_TOLERANCE_MS = 120_000
EPS = 1e-9
USDT = "USDT"
UNALLOCATED = "unallocated"

# type из GET /api/v5/account/bills -> категория ledger. Остальные типы — "other":
# попадают в default_sleeve с предупреждением, но не выбрасываются.
BILL_CATEGORY = {
    "1": "transfer",             # перевод между счетами и аккаунтами — движение капитала
    "2": "trade",
    "7": "interest",             # проценты по займу
    "8": "funding",              # funding fee SWAP: subType 173 — расход, 174 — доход
    "12": "strategy_transfer",   # перевод в бота или из бота: пара 202 out + 200 in, в сумме 0
    "14": "trade",               # block trade
    "24": "trade",               # spread trading
    "27": "trade",               # convert
    "28": "trade",               # easy convert
    "30": "trade",               # simple trade
}
CAPITAL_CATEGORIES = frozenset({"transfer", "strategy_transfer"})
# clOrdId исполнений нативных ботов OKX: "O" + 19 цифр (demo 24.09, grid и DCA)
BOT_CL_ORD_RE = re.compile(r"^O\d{19}$")
GRID_TYPES = ("grid", "contract_grid")
DCA_TYPES = ("spot_dca", "contract_dca")
BOT_LISTS = (
    ("grid", GRID_TYPES, ("private_get_tradingbot_grid_orders_algo_pending",
                          "private_get_tradingbot_grid_orders_algo_history")),
    ("dca", DCA_TYPES, ("private_get_tradingbot_dca_ongoing_list",
                        "private_get_tradingbot_dca_history_list")),
)
BOT_GRACE_MS = 10 * 60_000
# Единица equity_curve в bot_state.db режима: demo-движок пишет totalEq торгового
# счёта (USD), live-runner — стоимость кармана из asset-valuation (USDT)
ENGINE_EQUITY_UNIT = {"demo": "USD", "live": "USDT"}
BILL_FIELDS = ("billId", "ts", "type", "subType", "ccy", "balChg", "sz", "px", "fee", "pnl",
               "interest", "instId", "instType", "ordId", "clOrdId", "tag", "tradeId",
               "execType", "notes", "from", "to")


def _f(value: Any, default: Optional[float] = 0.0) -> Optional[float]:
    """Число из строки OKX ("" и None -> default)."""
    if value in (None, ""):
        return default
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _ms(value: Any) -> int:
    return int(_f(value, 0) or 0)


def _short(exc: Exception) -> str:
    return f"{type(exc).__name__}: {exc}"[:240]


def _iso(ms: int, tz: timezone) -> str:
    return datetime.fromtimestamp(ms / 1000, tz).isoformat(timespec="seconds")


def parse_tz(text: str) -> timezone:
    """Смещение вида +05:00 -> timezone."""
    try:
        return datetime.strptime(text.strip(), "%z").tzinfo  # type: ignore[return-value]
    except ValueError:
        raise ValueError(f"--tz: ожидается смещение вида +05:00, получено {text!r}") from None


# --- Период ---

@dataclass(frozen=True)
class Period:
    start_ms: int     # начало первого дня, включительно
    end_ms: int       # конец последнего дня, не включительно
    t1_ms: int        # момент оценки: min(end, now)
    tz: timezone
    label: str        # 2026-09-24 или 2026-09-18_2026-09-24
    days: int

    @property
    def partial(self) -> bool:
        return self.t1_ms < self.end_ms


def period_bounds(day: Optional[date], days: int, tz: timezone, now_ms: int) -> Period:
    """Календарные дни в поясе tz: [day-(days-1) 00:00, day+1 00:00)."""
    if days < 1:
        raise ValueError("--days должен быть >= 1")
    last = day or datetime.fromtimestamp(now_ms / 1000, tz).date()
    first = last - timedelta(days=days - 1)
    start = int(datetime.combine(first, datetime.min.time(), tz).timestamp() * 1000)
    end = int(datetime.combine(last + timedelta(days=1), datetime.min.time(), tz).timestamp() * 1000)
    if start > now_ms:
        raise ValueError(f"период {first}..{last} ещё не начался")
    label = last.isoformat() if days == 1 else f"{first.isoformat()}_{last.isoformat()}"
    return Period(start, end, min(end, now_ms), tz, label, days)


# --- Правила атрибуции (ops/sleeves.json) ---

class RulesError(ValueError):
    """Файл правил отсутствует, не читается или невалиден."""


def _sleeve_of(value: Any) -> Any:
    return value.get("sleeve") if isinstance(value, dict) else value


def _mode_table(rules: dict, key: str, mode: str) -> dict[str, str]:
    """Общие правила "*" + правила режима (режим перекрывает общие)."""
    table = rules.get(key) or {}
    merged = {k: _sleeve_of(v) for k, v in (table.get("*") or {}).items()}
    merged.update({k: _sleeve_of(v) for k, v in (table.get(mode) or {}).items()})
    return merged


def validate_rules(rules: dict) -> list[str]:
    """Ошибки правил; пустой список — правила валидны."""
    sleeves = rules.get("sleeves")
    if not isinstance(sleeves, dict) or not sleeves:
        return ["sleeves: нужен непустой объект рукавов"]
    errors: list[str] = []

    def need(name: Any, where: str) -> None:
        if not isinstance(name, str) or name not in sleeves:
            errors.append(f"{where}: рукав {name!r} не описан в sleeves")

    need(rules.get("default_sleeve"), "default_sleeve")
    need(rules.get("earn_sleeve"), "earn_sleeve")
    funding = rules.get("funding") or {}
    need(funding.get("default_sleeve"), "funding.default_sleeve")
    for inst, value in (funding.get("by_inst") or {}).items():
        need(_sleeve_of(value), f"funding.by_inst.{inst}")
    for algo, value in (rules.get("bots") or {}).items():
        need(_sleeve_of(value), f"bots.{algo}")
    for prefix, value in (rules.get("bot_algo_cl_ord_id_prefixes") or {}).items():
        need(_sleeve_of(value), f"bot_algo_cl_ord_id_prefixes.{prefix}")
    for key in ("cl_ord_id_prefixes", "tags"):
        table = rules.get(key) or {}
        if not isinstance(table, dict):
            errors.append(f"{key}: ожидается объект")
            continue
        for scope, entries in table.items():
            if scope != "*" and scope not in MODES:
                errors.append(f"{key}.{scope}: ожидается '*' или режим {'/'.join(MODES)}")
            for name, value in (entries or {}).items():
                need(_sleeve_of(value), f"{key}.{scope}.{name}")
    return errors


def load_rules(path: Path = RULES_PATH) -> dict:
    try:
        rules = json.loads(Path(path).read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise RulesError(f"нет файла правил {path}") from None
    except (OSError, ValueError) as exc:
        raise RulesError(f"{path} не читается: {exc}") from None
    if not isinstance(rules, dict):
        raise RulesError(f"{path}: ожидается JSON-объект")
    errors = validate_rules(rules)
    if errors:
        raise RulesError("; ".join(errors))
    return rules


def registry_owner_of() -> Optional[Callable[[str], Optional[str]]]:
    """Код владельца по clOrdId из реестра ORDER-OWNER-TAG (src/order_owner.py).

    Связь мягкая: модуля нет или у него другой API — атрибуция идёт по
    префиксам ops/sleeves.json (самый длинный префикс).
    """
    try:
        from . import order_owner  # noqa: PLC0415 — необязательная зависимость
    except ImportError:
        return None
    owner_of = getattr(order_owner, "owner_of", None)
    if not callable(owner_of):
        return None

    def code_of(cl_ord_id: str) -> Optional[str]:
        try:
            return getattr(owner_of(cl_ord_id), "code", None)
        except Exception:  # noqa: BLE001 — сбой реестра не должен ломать отчёт
            return None

    return code_of


def registry_codes() -> list[str]:
    try:
        from . import order_owner  # noqa: PLC0415
    except ImportError:
        return []
    return [getattr(o, "code", "") for o in getattr(order_owner, "OWNERS", ()) if getattr(o, "code", "")]


# --- Журнал (SQLite в каталоге отчётов) ---

_JOURNAL_SCHEMA = """
CREATE TABLE IF NOT EXISTS bills (bill_id TEXT PRIMARY KEY, ts INTEGER NOT NULL, raw TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS idx_bills_ts ON bills(ts);
CREATE TABLE IF NOT EXISTS bot_orders (ord_id TEXT PRIMARY KEY, algo_id TEXT NOT NULL,
                                       algo_ord_type TEXT, inst_id TEXT);
CREATE TABLE IF NOT EXISTS bots (algo_id TEXT PRIMARY KEY, raw TEXT NOT NULL, seen_ms INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS snapshots (ts INTEGER PRIMARY KEY, trading_usdt REAL, valuation_usdt REAL,
                                      total_eq_usd REAL, usdt_usd REAL);
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT);
"""


class Journal:
    """Локальная копия bills, ордеров ботов и снимков equity. Биржа хранит bills
    7 дней (archive — 3 месяца); журнал — сколько нужно. Запись идемпотентна."""

    def __init__(self, path: Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._conn() as conn:
            conn.executescript(_JOURNAL_SCHEMA)

    @contextmanager
    def _conn(self):
        conn = sqlite3.connect(self.path, timeout=10)
        try:
            yield conn
            conn.commit()
        finally:
            conn.close()

    def add_bills(self, bills: list[dict]) -> int:
        rows = [(str(b["billId"]), _ms(b.get("ts")),
                 json.dumps({k: b[k] for k in BILL_FIELDS if k in b}, ensure_ascii=False))
                for b in bills if b.get("billId")]
        with self._conn() as conn:
            before = conn.total_changes
            conn.executemany("INSERT OR IGNORE INTO bills VALUES (?, ?, ?)", rows)
            return conn.total_changes - before

    def bills(self, until_ms: Optional[int] = None) -> list[dict]:
        query = "SELECT raw FROM bills"
        params: tuple = ()
        if until_ms is not None:
            query += " WHERE ts <= ?"
            params = (until_ms,)
        query += " ORDER BY ts, CAST(bill_id AS INTEGER)"
        with self._conn() as conn:
            return [json.loads(r[0]) for r in conn.execute(query, params)]

    def oldest_bill_id(self, min_ts: int = 0) -> Optional[str]:
        with self._conn() as conn:
            row = conn.execute("SELECT bill_id FROM bills WHERE ts >= ? ORDER BY ts, CAST(bill_id AS INTEGER)"
                               " LIMIT 1", (min_ts,)).fetchone()
        return row[0] if row else None

    def add_bot_orders(self, rows: list[tuple]) -> int:
        with self._conn() as conn:
            before = conn.total_changes
            conn.executemany("INSERT OR IGNORE INTO bot_orders VALUES (?, ?, ?, ?)", rows)
            return conn.total_changes - before

    def bot_order_map(self) -> dict[str, str]:
        with self._conn() as conn:
            return {r[0]: r[1] for r in conn.execute("SELECT ord_id, algo_id FROM bot_orders")}

    def upsert_bots(self, bots: list[dict], seen_ms: int) -> None:
        with self._conn() as conn:
            conn.executemany("INSERT OR REPLACE INTO bots VALUES (?, ?, ?)",
                             [(b["algoId"], json.dumps(b, ensure_ascii=False), seen_ms)
                              for b in bots if b.get("algoId")])

    def bots(self) -> dict[str, dict]:
        with self._conn() as conn:
            return {r[0]: json.loads(r[1]) for r in conn.execute("SELECT algo_id, raw FROM bots")}

    def add_snapshot(self, ts: int, trading_usdt: Optional[float], valuation_usdt: Optional[float],
                     total_eq_usd: Optional[float], usdt_usd: Optional[float]) -> None:
        with self._conn() as conn:
            conn.execute("INSERT OR REPLACE INTO snapshots VALUES (?, ?, ?, ?, ?)",
                         (ts, trading_usdt, valuation_usdt, total_eq_usd, usdt_usd))

    def snapshots(self, since_ms: int, until_ms: int) -> list[tuple[int, float]]:
        """(ts, стоимость торгового счёта в USDT) — снимки прошлых запусков ledger."""
        with self._conn() as conn:
            return [(int(r[0]), float(r[1])) for r in conn.execute(
                "SELECT ts, trading_usdt FROM snapshots WHERE ts >= ? AND ts <= ? AND trading_usdt IS NOT NULL"
                " ORDER BY ts", (since_ms, until_ms))]

    def meta(self, key: str) -> Optional[str]:
        with self._conn() as conn:
            row = conn.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
        return row[0] if row else None

    def meta_int(self, key: str) -> Optional[int]:
        value = self.meta(key)
        return int(value) if value not in (None, "") else None

    def set_meta(self, key: str, value: Any) -> None:
        with self._conn() as conn:
            conn.execute("INSERT OR REPLACE INTO meta VALUES (?, ?)", (key, str(value)))


# --- Чтение биржи: только GET ---

class Reader:
    """Вызовы OKX только private_get_* / public_get_* с паузой (лимит tradingBot — 20 за 2 с)."""

    def __init__(self, exchange: Any, pause: float = 0.0, sleep: Callable[[float], None] = time.sleep):
        self.exchange = exchange
        self.pause = pause
        self.sleep = sleep
        self.calls = 0

    def get(self, method: str, params: Optional[dict] = None) -> list:
        if not method.startswith(("private_get_", "public_get_")):
            raise ValueError(f"ledger читает биржу только GET-запросами, а не {method}")
        if self.calls and self.pause:
            self.sleep(self.pause)
        self.calls += 1
        resp = getattr(self.exchange, method)(dict(params or {}))
        code = str((resp or {}).get("code", "0") or "0")
        if code != "0":
            raise RuntimeError(f"{method}: OKX code {code} {resp.get('msg', '')}".strip())
        return list((resp or {}).get("data") or [])


def sync_bills(reader: Reader, journal: Journal, since_ms: int, now_ms: int) -> dict:
    """Догрузить bills в журнал: сначала новые (до уже покрытого), затем историю
    старше покрытия до since_ms. Покрытие [bills_cov_from, bills_cov_to] — в meta."""
    stats = {"pages": 0, "archive_pages": 0, "fetched": 0, "new": 0}
    cov_from, cov_to = journal.meta_int("bills_cov_from"), journal.meta_int("bills_cov_to")
    newest: list[Optional[int]] = [cov_to]

    def walk(after: Optional[str], stop_ts: int, overlap_ts: Optional[int]) -> tuple[Optional[int], str]:
        archive = False
        oldest: Optional[int] = None
        while True:
            params = {"limit": str(PAGE)}
            if after:
                params["after"] = after
            method = "private_get_account_bills_archive" if archive else "private_get_account_bills"
            page = reader.get(method, params)
            stats["archive_pages" if archive else "pages"] += 1
            stats["fetched"] += len(page)
            stats["new"] += journal.add_bills(page)
            if page:
                top = _ms(page[0].get("ts"))
                newest[0] = top if newest[0] is None else max(newest[0], top)
                oldest, after = _ms(page[-1].get("ts")), page[-1]["billId"]
                if oldest < stop_ts:
                    return oldest, "since"
                if overlap_ts is not None and oldest <= overlap_ts:
                    return oldest, "overlap"
            if len(page) < PAGE:
                if not archive and stop_ts < now_ms - BILLS_DEPTH_DAYS * DAY_MS:
                    archive = True  # глубже 7 дней — bills-archive с того же курсора
                    continue
                return oldest, "end"

    oldest, why = walk(None, since_ms, cov_to)
    if why == "overlap" and cov_from is not None:
        new_from = cov_from
    elif why == "end":
        new_from = min(since_ms, oldest if oldest is not None else now_ms)  # старше bills нет
    else:
        new_from = oldest if oldest is not None else now_ms
    if since_ms < new_from:
        cursor = journal.oldest_bill_id(new_from)
        oldest_b, why_b = walk(cursor, since_ms, None)
        if why_b == "end":
            new_from = min(since_ms, oldest_b if oldest_b is not None else new_from)
        elif oldest_b is not None:
            new_from = min(new_from, oldest_b)
    journal.set_meta("bills_cov_from", new_from)
    journal.set_meta("bills_cov_to", newest[0] if newest[0] is not None else now_ms)
    stats["covered_from_ms"] = new_from
    return stats


STOPPED_BOT_STATES = frozenset({"stopped", "no_close_position"})


def bot_end_ms(bot: dict) -> float:
    """Конец жизни бота (мс): stopTime; у остановленного без stopTime — uTime; иначе бесконечность."""
    stop = _ms(bot.get("stopTime"))
    if stop:
        return float(stop)
    if bot.get("state") in STOPPED_BOT_STATES and _ms(bot.get("uTime")):
        return float(_ms(bot.get("uTime")))
    return float("inf")


def _grid_orders(reader: Reader, bot: dict, since_ms: int, known: dict[str, str]) -> list[tuple]:
    rows: list[tuple] = []
    after: Optional[str] = None
    while True:
        params = {"algoOrdType": bot.get("algoOrdType") or "grid", "algoId": bot["algoId"],
                  "type": "filled", "limit": str(PAGE)}
        if after:
            params["after"] = after
        page = reader.get("private_get_tradingbot_grid_sub_orders", params)
        fresh = [o for o in page if o.get("ordId") and o["ordId"] not in known]
        rows += [(o["ordId"], bot["algoId"], bot.get("algoOrdType"), o.get("instId") or bot.get("instId"))
                 for o in page if o.get("ordId")]
        if len(page) < PAGE or not fresh:
            return rows  # конец списка или дальше только уже известные
        if min(_ms(o.get("uTime") or o.get("cTime")) for o in page) < since_ms:
            return rows
        after = page[-1]["ordId"]


def _dca_orders(reader: Reader, bot: dict, since_ms: int) -> list[tuple]:
    base = {"algoOrdType": bot.get("algoOrdType") or "spot_dca", "algoId": bot["algoId"]}
    cycles: list[dict] = []
    after: Optional[str] = None
    while True:
        params = dict(base, limit=str(PAGE))
        if after:
            params["after"] = after
        page = reader.get("private_get_tradingbot_dca_cycle_list", params)
        cycles += page
        if len(page) < PAGE or not page[-1].get("cycleId"):
            break
        after = page[-1]["cycleId"]
    rows: list[tuple] = []
    for cycle in cycles:
        end = _ms(cycle.get("endTime"))
        if end and end < since_ms:
            continue
        after = None
        while True:
            params = dict(base, cycleId=cycle.get("cycleId"), limit=str(PAGE))
            if after:
                params["after"] = after
            page = reader.get("private_get_tradingbot_dca_orders", params)
            rows += [(o["ordId"], bot["algoId"], bot.get("algoOrdType"), o.get("instId") or bot.get("instId"))
                     for o in page if o.get("ordId")]
            if len(page) < PAGE:
                break
            after = page[-1]["ordId"]
    return rows


def sync_bots(reader: Reader, journal: Journal, since_ms: int, now_ms: int) -> dict:
    """Нативные боты (grid/DCA, активные и история) и их ордера -> журнал.

    Ордер бота (ordId) — ключ атрибуции его исполнений в bills.
    Ошибка одного списка или бота — в errors, остальные синхронизируются.
    """
    stats: dict[str, Any] = {"bots": 0, "orders_new": 0, "errors": []}
    found: dict[str, dict] = {}
    for family, types, methods in BOT_LISTS:
        for algo_type in types:
            for method in methods:
                after: Optional[str] = None
                try:
                    while True:
                        params = {"algoOrdType": algo_type, "limit": str(PAGE)}
                        if after:
                            params["after"] = after
                        page = reader.get(method, params)
                        for bot in page:
                            if bot.get("algoId"):
                                found[bot["algoId"]] = dict(bot, algoOrdType=bot.get("algoOrdType") or algo_type,
                                                            _family=family)
                        if len(page) < PAGE or bot_end_ms(page[-1]) < since_ms:
                            break
                        after = page[-1]["algoId"]
                except Exception as exc:  # noqa: BLE001 — фиксируем и идём дальше
                    stats["errors"].append(f"{method} {algo_type}: {_short(exc)}")
    journal.upsert_bots(list(found.values()), now_ms)
    known = journal.bot_order_map()
    for bot in found.values():
        if bot_end_ms(bot) < since_ms:
            continue
        try:
            rows = (_grid_orders(reader, bot, since_ms, known) if bot["_family"] == "grid"
                    else _dca_orders(reader, bot, since_ms))
            stats["orders_new"] += journal.add_bot_orders(rows)
        except Exception as exc:  # noqa: BLE001
            stats["errors"].append(f"ордера бота {bot['algoId']}: {_short(exc)}")
    stats["bots"] = len(found)
    return stats


def fetch_account(reader: Reader) -> dict:
    row = (reader.get("private_get_account_balance") or [{}])[0]
    details = row.get("details") or []
    return {"total_eq_usd": _f(row.get("totalEq"), None),
            "eq": {d["ccy"]: _f(d.get("eq")) for d in details if d.get("ccy")}}


def fetch_valuation(reader: Reader) -> dict:
    """asset-valuation в USDT: totalBal и details (trading/funding/earn/classic)."""
    row = (reader.get("private_get_asset_asset_valuation", {"ccy": USDT}) or [{}])[0]
    return {"total_usdt": _f(row.get("totalBal"), None),
            "details": {k: _f(v) for k, v in (row.get("details") or {}).items()}}


def fetch_earn(reader: Reader, since_ms: int, until_ms: int) -> list[dict]:
    """Начисления Simple Earn (flexible) — finance/savings/lending-history, от новых к старым."""
    rows: list[dict] = []
    after: Optional[str] = None
    while True:
        params = {"limit": str(PAGE)}
        if after:
            params["after"] = after
        page = reader.get("private_get_finance_savings_lending_history", params)
        rows += [r for r in page if since_ms <= _ms(r.get("ts")) <= until_ms]
        if len(page) < PAGE or min(_ms(r.get("ts")) for r in page) < since_ms:
            return rows
        after = str(page[-1].get("ts"))


def candle_price(reader: Reader, inst_id: str, ts_ms: int, index: bool = False) -> Optional[float]:
    """Цена в момент ts по минутной свече (интерполяция open -> close внутри минуты)."""
    minute = ts_ms - ts_ms % 60_000
    method = "public_get_market_history_index_candles" if index else "public_get_market_history_candles"
    rows = reader.get(method, {"instId": inst_id, "bar": "1m", "after": str(minute + 60_000), "limit": "1"})
    if not rows:
        return None
    candle_ts, open_px, close_px = _ms(rows[0][0]), _f(rows[0][1]), _f(rows[0][4])
    if not open_px or open_px <= 0:
        return None
    if candle_ts != minute:  # свечи этой минуты нет — закрытие последней до неё
        return close_px or None
    return open_px + (close_px - open_px) * (ts_ms - minute) / 60_000


class PriceBook:
    """Цены валют в USDT: около now — тикеры SPOT (один запрос), в прошлом — минутные свечи."""

    def __init__(self, reader: Reader, now_ms: int, warnings: list[str]):
        self.reader = reader
        self.now_ms = now_ms
        self.warnings = warnings
        self._tickers: Optional[dict[str, float]] = None
        self._cache: dict[tuple[str, int], Optional[float]] = {}
        self.missing: set[str] = set()

    def tickers(self) -> dict[str, float]:
        if self._tickers is None:
            try:
                self._tickers = {t["instId"]: float(t["last"])
                                 for t in self.reader.get("public_get_market_tickers", {"instType": "SPOT"})
                                 if (_f(t.get("last")) or 0) > 0}
            except Exception as exc:  # noqa: BLE001
                self._tickers = {}
                self.warnings.append(f"тикеры SPOT недоступны: {_short(exc)}")
        return self._tickers

    def __call__(self, ccy: str, ts_ms: int) -> Optional[float]:
        if ccy == USDT:
            return 1.0
        if abs(ts_ms - self.now_ms) <= PRICE_NOW_TOLERANCE_MS:
            key = (ccy, -1)
            if key not in self._cache:
                self._cache[key] = self._now(ccy)
        else:
            key = (ccy, ts_ms // 60_000)
            if key not in self._cache:
                self._cache[key] = self._past(ccy, ts_ms)
        if self._cache[key] is None:
            self.missing.add(ccy)
        return self._cache[key]

    def _now(self, ccy: str) -> Optional[float]:
        tickers = self.tickers()
        if tickers.get(f"{ccy}-USDT"):
            return tickers[f"{ccy}-USDT"]
        for via in ("USDC", "BTC", "ETH"):
            cross, via_px = tickers.get(f"{ccy}-{via}"), tickers.get(f"{via}-USDT")
            if cross and via_px:
                return cross * via_px
        return None

    def _candle(self, inst_id: str, ts_ms: int) -> Optional[float]:
        try:
            return candle_price(self.reader, inst_id, ts_ms)
        except Exception:  # noqa: BLE001 — пары может не быть (51001): пробуем следующую
            return None

    def _past(self, ccy: str, ts_ms: int) -> Optional[float]:
        direct = self._candle(f"{ccy}-USDT", ts_ms)
        if direct:
            return direct
        for via in ("BTC", "ETH"):
            cross = self._candle(f"{ccy}-{via}", ts_ms)
            via_px = self(via, ts_ms) if cross else None
            if cross and via_px:
                return cross * via_px
        return None


class UsdRate:
    """USD за 1 USDT (индекс USDT-USD): totalEq биржи — в USD, отчёт — в USDT."""

    def __init__(self, reader: Reader, now_ms: int, warnings: list[str]):
        self.reader, self.now_ms, self.warnings = reader, now_ms, warnings
        self._cache: dict[int, Optional[float]] = {}

    def __call__(self, ts_ms: int) -> Optional[float]:
        key = -1 if abs(ts_ms - self.now_ms) <= PRICE_NOW_TOLERANCE_MS else ts_ms // 60_000
        if key not in self._cache:
            try:
                if key == -1:
                    rows = self.reader.get("public_get_market_index_tickers", {"instId": "USDT-USD"})
                    self._cache[key] = _f(rows[0].get("idxPx"), None) if rows else None
                else:
                    self._cache[key] = candle_price(self.reader, "USDT-USD", ts_ms, index=True)
            except Exception as exc:  # noqa: BLE001
                self._cache[key] = None
                self.warnings.append(f"индекс USDT-USD недоступен: {_short(exc)}")
        return self._cache[key]


def read_equity_curve(db_path: Path, since_ms: int, until_ms: int) -> list[tuple[int, float]]:
    """Снимки equity_curve из bot_state.db режима — ТОЛЬКО чтение (mode=ro)."""
    path = Path(db_path)
    if not path.exists():
        return []
    uri = path.resolve().as_uri() + "?mode=ro"
    with closing(sqlite3.connect(uri, uri=True, timeout=5)) as conn:
        rows = conn.execute("SELECT ts, total_eq FROM equity_curve WHERE ts >= ? AND ts <= ? ORDER BY ts",
                            (since_ms / 1000, until_ms / 1000)).fetchall()
    return [(int(ts * 1000), float(eq)) for ts, eq in rows if eq is not None]


# --- Атрибуция ---

@dataclass(frozen=True)
class Attr:
    category: str   # trade | funding | interest | other | transfer | strategy_transfer
    sleeve: str     # рукав; у переводов капитала — UNALLOCATED
    source: str     # bot:<algoId> | clOrdId:<код> | tag:<tag> | funding:<instId> | unmarked | ...
    note: str = ""


class Classifier:
    """Правило рукава для каждого bill (порядок — в ops/sleeves.json, поле note)."""

    def __init__(self, rules: dict, mode: str, order_map: dict[str, str], bots: dict[str, dict],
                 owner_of: Optional[Callable[[str], Optional[str]]] = None):
        self.default = rules["default_sleeve"]
        self.bot_rules = {k: _sleeve_of(v) for k, v in (rules.get("bots") or {}).items()}
        self.bot_prefixes = {k: _sleeve_of(v) for k, v in (rules.get("bot_algo_cl_ord_id_prefixes") or {}).items()}
        self.prefixes = _mode_table(rules, "cl_ord_id_prefixes", mode)
        self.tags = _mode_table(rules, "tags", mode)
        funding = rules.get("funding") or {}
        self.funding_default = funding.get("default_sleeve")
        self.funding_by_inst = {k: _sleeve_of(v) for k, v in (funding.get("by_inst") or {}).items()}
        self.order_map = order_map
        self.bots = bots
        self.owner_of = owner_of
        self.unmapped_bots: set[str] = set()
        self.unknown_types: dict[str, int] = defaultdict(int)
        self.heuristic_bills = 0
        self.orphan_bot_bills = 0

    @staticmethod
    def _longest(value: str, table: dict[str, str]) -> Optional[str]:
        best = None
        for prefix in table:
            if value.startswith(prefix) and (best is None or len(prefix) > len(best)):
                best = prefix
        return best

    def owner_code(self, cl_ord_id: str) -> Optional[str]:
        if self.owner_of is not None:
            code = self.owner_of(cl_ord_id)
            if code and code in self.prefixes:
                return code
        return self._longest(cl_ord_id, self.prefixes)

    def bot_sleeve(self, algo_id: str) -> str:
        if algo_id in self.bot_rules:
            return self.bot_rules[algo_id]
        algo_cl = (self.bots.get(algo_id) or {}).get("algoClOrdId") or ""
        prefix = self._longest(algo_cl, self.bot_prefixes) if algo_cl else None
        if prefix:
            return self.bot_prefixes[prefix]
        code = self.owner_code(algo_cl) if algo_cl else None
        if code:
            return self.prefixes[code]
        self.unmapped_bots.add(algo_id)
        return self.default

    def bot_by_inst_time(self, bill: dict) -> Optional[str]:
        inst, ts = bill.get("instId"), _ms(bill.get("ts"))
        hits = [a for a, b in self.bots.items()
                if b.get("instId") == inst and _ms(b.get("cTime")) - BOT_GRACE_MS <= ts <= bot_end_ms(b) + BOT_GRACE_MS]
        return hits[0] if len(hits) == 1 else None

    def __call__(self, bill: dict) -> Attr:
        bill_type = str(bill.get("type") or "")
        category = BILL_CATEGORY.get(bill_type, "other")
        if category in CAPITAL_CATEGORIES:
            return Attr(category, UNALLOCATED, f"capital:{category}")
        if category == "funding":
            inst = bill.get("instId") or ""
            return Attr(category, self.funding_by_inst.get(inst, self.funding_default), f"funding:{inst or '?'}")
        if category == "interest":  # заём не привязан к ордеру: рукав не узнать, но это и не «ордер человека»
            return Attr(category, self.default, f"interest:{bill.get('ccy') or '?'}", "проценты по займу")
        if category == "other":
            self.unknown_types[f"{bill_type}/{bill.get('subType')}"] += 1
        cl = bill.get("clOrdId") or ""
        ord_id = bill.get("ordId") or ""
        algo = self.order_map.get(ord_id) if ord_id else None
        note = ""
        if algo is None and BOT_CL_ORD_RE.match(cl):
            algo = self.bot_by_inst_time(bill)
            if algo is None:
                self.orphan_bot_bills += 1
                return Attr(category, self.default, "bot:?", "исполнение нативного бота без algoId")
            self.heuristic_bills += 1
            note = "бот опознан по instId и времени жизни"
        if algo:
            return Attr(category, self.bot_sleeve(algo), f"bot:{algo}", note)
        if cl:
            code = self.owner_code(cl)
            if code:
                return Attr(category, self.prefixes[code], f"clOrdId:{code}")
        tag = bill.get("tag") or ""
        if tag and tag in self.tags:
            return Attr(category, self.tags[tag], f"tag:{tag}")
        if category == "other":
            return Attr(category, self.default, f"type:{bill_type}", "тип bill не распознан")
        if not cl and not tag:
            return Attr(category, self.default, "unmarked", "без clOrdId и tag — вероятно, ордер человека")
        return Attr(category, self.default, f"unknown:{cl[:8] or tag}", "владелец не опознан")


# --- Позиция по средней цене ---

@dataclass
class Position:
    """Спот-позиция рукава по средней цене. qty — база (после комиссий), cost — qty x средняя
    цена в котируемой валюте (с тем же знаком, что qty: шорт относительно старта)."""
    qty: float = 0.0
    cost: float = 0.0

    def apply(self, dq: float, dcash: float) -> float:
        """dq, dcash — balChg базовой и котируемой ноги исполнения (комиссии внутри).
        Возвращает реализованный результат в котируемой валюте."""
        if abs(dq) < EPS:
            return dcash
        px = -dcash / dq
        if abs(self.qty) < EPS or (self.qty > 0) == (dq > 0):
            self.qty += dq
            self.cost += dq * px
            return 0.0
        avg = self.cost / self.qty
        closing_qty = min(abs(dq), abs(self.qty))
        sign = 1.0 if self.qty > 0 else -1.0
        realized = (px - avg) * closing_qty * sign
        self.qty -= sign * closing_qty
        self.cost -= sign * closing_qty * avg
        rest = abs(dq) - closing_qty
        if rest > EPS:  # переворот позиции: остаток открывает новую по цене исполнения
            self.qty = -sign * rest
            self.cost = self.qty * px
        if abs(self.qty) < EPS:
            self.qty, self.cost = 0.0, 0.0
        return realized


def _dd() -> defaultdict:
    return defaultdict(float)


@dataclass
class Book:
    """Движения одного источника внутри рукава (бот, префикс clOrdId, tag...)."""
    sleeve: str
    source: str
    h_t0: defaultdict = field(default_factory=_dd)      # накоплено до начала периода
    h_t1: defaultdict = field(default_factory=_dd)      # накоплено до t1
    fees: defaultdict = field(default_factory=_dd)      # за период, + = уплачено
    funding: defaultdict = field(default_factory=_dd)
    interest: defaultdict = field(default_factory=_dd)
    other: defaultdict = field(default_factory=_dd)
    realized: defaultdict = field(default_factory=_dd)  # за период, по котируемой валюте
    volume: defaultdict = field(default_factory=_dd)    # оборот за период, по котируемой валюте
    earn: defaultdict = field(default_factory=_dd)
    trades: int = 0
    positions: dict = field(default_factory=dict)       # instId -> (base, quote, Position)
    notes: set = field(default_factory=set)


def _spot_legs(inst_id: str, inst_type: str) -> Optional[tuple[str, str]]:
    parts = (inst_id or "").split("-")
    if len(parts) == 2 and (inst_type in ("SPOT", "") and all(parts)):
        return parts[0], parts[1]
    return None


# --- Отчёт ---

@dataclass
class Inputs:
    mode: str
    period: Period
    now_ms: int
    bills: list[dict]
    order_map: dict[str, str]
    bots: dict[str, dict]
    eq: dict[str, float]
    total_eq_usd: Optional[float]
    valuation: Optional[dict]
    earn: Optional[list[dict]]
    price: Callable[[str, int], Optional[float]]
    usdt_usd: Callable[[int], Optional[float]]
    engine_curve: list[tuple[int, float]] = field(default_factory=list)
    own_snapshots: list[tuple[int, float]] = field(default_factory=list)
    owner_of: Optional[Callable[[str], Optional[str]]] = None
    registry_codes: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)


def _value(amounts: dict[str, float], price: Callable[[str], Optional[float]]) -> float:
    return sum(qty * (price(ccy) or 0.0) for ccy, qty in amounts.items() if abs(qty) > EPS)


def _r(x: Optional[float], nd: int = 6) -> Optional[float]:
    return None if x is None else round(x, nd)


def _nonzero(amounts: dict[str, float], nd: int = 10) -> dict[str, float]:
    return {c: round(q, nd) for c, q in sorted(amounts.items()) if abs(q) > 1e-10}


def build_report(inp: Inputs, rules: dict) -> dict:
    """Чистый расчёт: журнал + балансы + цены -> отчёт (dict, сериализуется в JSON)."""
    per, t0, t1 = inp.period, inp.period.start_ms, inp.period.t1_ms
    warnings, notes = list(inp.warnings), list(inp.notes)
    classify = Classifier(rules, inp.mode, inp.order_map, inp.bots, inp.owner_of)
    bills = [b for b in inp.bills if _ms(b.get("ts")) <= inp.now_ms]
    attrs = [classify(b) for b in bills]

    # стартовые остатки до начала журнала: eq сейчас минус все движения журнала
    sums: defaultdict = _dd()
    for bill in bills:
        sums[bill.get("ccy") or "?"] += _f(bill.get("balChg")) or 0.0
    opening = {c: inp.eq.get(c, 0.0) - sums.get(c, 0.0) for c in set(sums) | set(inp.eq)}
    opening = {c: q for c, q in opening.items() if abs(q) > 1e-9 * max(1.0, abs(inp.eq.get(c, 0.0)))}

    books: dict[tuple[str, str], Book] = {}
    u_t0, u_t1 = _dd(), _dd()
    for ccy, qty in opening.items():
        u_t0[ccy] += qty
        u_t1[ccy] += qty
    capital = {"transfer": _dd(), "strategy_transfer": _dd()}
    fills: dict[tuple, dict] = {}
    for bill, attr in zip(bills, attrs):
        ts = _ms(bill.get("ts"))
        if ts > t1:
            continue
        ccy, chg, in_period = bill.get("ccy") or "?", _f(bill.get("balChg")) or 0.0, ts >= t0
        if attr.category in CAPITAL_CATEGORIES:
            u_t1[ccy] += chg
            if in_period:
                capital[attr.category][ccy] += chg
            else:
                u_t0[ccy] += chg
            continue
        book = books.setdefault((attr.sleeve, attr.source), Book(attr.sleeve, attr.source))
        if attr.note:
            book.notes.add(attr.note)
        book.h_t1[ccy] += chg
        if not in_period:
            book.h_t0[ccy] += chg
        else:
            fee = _f(bill.get("fee")) or 0.0
            if fee:
                book.fees[ccy] -= fee  # OKX: отрицательный fee — списание
            if attr.category in ("funding", "interest", "other"):
                getattr(book, attr.category)[ccy] += chg
        if attr.category == "trade":
            trade_id = bill.get("tradeId") or ""
            key = ((bill.get("instId"), trade_id, bill.get("ordId")) if trade_id not in ("", "0")
                   else ("bill", bill.get("billId")))
            fill = fills.setdefault(key, {"book": book, "ts": ts, "inst": bill.get("instId") or "",
                                          "inst_type": bill.get("instType") or "", "legs": _dd(),
                                          "gross": {}, "in_period": in_period})
            fill["legs"][ccy] += chg
            fill["gross"][ccy] = abs(_f(bill.get("sz")) or 0.0)

    for fill in sorted(fills.values(), key=lambda f: f["ts"]):
        book: Book = fill["book"]
        legs = fill["legs"]
        pair = _spot_legs(fill["inst"], fill["inst_type"])
        if fill["in_period"]:
            book.trades += 1
        if pair and pair[0] in legs and pair[1] in legs:
            base, quote = pair
            entry = book.positions.setdefault(fill["inst"], (base, quote, Position()))
            realized = entry[2].apply(legs[base], legs[quote])
            if fill["in_period"]:
                book.realized[quote] += realized
                book.volume[quote] += fill["gross"].get(quote, 0.0)
        elif fill["in_period"]:
            for ccy, chg in legs.items():  # не спот или неполная пара ног: результат — кэш
                book.realized[ccy] += chg

    # Earn — отдельный счёт: начисления идут в рукав earn_sleeve (в bills торгового счёта их нет)
    earn_by_ccy: defaultdict = _dd()
    for row in inp.earn or []:
        ts = _ms(row.get("ts"))
        if t0 <= ts <= t1:
            earn_by_ccy[row.get("ccy") or "?"] += _f(row.get("earnings")) or 0.0
    if earn_by_ccy:
        book = books.setdefault((rules["earn_sleeve"], "earn:simple"), Book(rules["earn_sleeve"], "earn:simple"))
        for ccy, qty in earn_by_ccy.items():
            book.earn[ccy] += qty

    def p1(ccy: str) -> Optional[float]:
        return inp.price(ccy, t1)

    def p0(ccy: str) -> Optional[float]:
        return inp.price(ccy, t0)

    # --- рукава ---
    sleeve_rows: dict[str, dict] = {}
    source_rows: dict[str, list[dict]] = defaultdict(list)
    source_value: dict[str, float] = {}
    for (sleeve, source), book in books.items():
        v_t1, v_t0 = _value(book.h_t1, p1), _value(book.h_t0, p0)
        trading_pnl = v_t1 - v_t0
        earn = _value(book.earn, p1)
        funding, interest, other = _value(book.funding, p1), _value(book.interest, p1), _value(book.other, p1)
        realized = _value(book.realized, p1)
        unreal_end = sum(pos.qty * (p1(base) or 0.0) - pos.cost * (p1(quote) or 0.0)
                         for base, quote, pos in book.positions.values())
        # вложено в код-позиции (у ботов вложенное — investment из bot API, ниже)
        cost_long = 0.0 if source.startswith("bot:") else sum(
            pos.cost * (p1(quote) or 0.0) for _, quote, pos in book.positions.values() if pos.qty > 0)
        row = {
            "pnl_usdt": trading_pnl + earn, "trading_pnl_usdt": trading_pnl, "realized_usdt": realized,
            "unrealized_change_usdt": trading_pnl - realized - funding - interest - other,
            "unrealized_end_usdt": unreal_end, "fees_usdt": _value(book.fees, p1), "funding_usdt": funding,
            "earn_usdt": earn, "interest_usdt": interest, "other_usdt": other, "trades": book.trades,
            "volume_usdt": _value(book.volume, p1), "cum_pnl_usdt": v_t1, "cost_long_usdt": cost_long,
        }
        source_value[source] = v_t1
        source_rows[sleeve].append({"source": source, "pnl_usdt": _r(row["pnl_usdt"]), "trades": book.trades,
                                    "fees_usdt": _r(row["fees_usdt"]), "cum_pnl_usdt": _r(v_t1),
                                    "fees_by_ccy": _nonzero(book.fees), "notes": sorted(book.notes)})
        agg = sleeve_rows.setdefault(sleeve, defaultdict(float))
        for key, value in row.items():
            agg[key] += value
        fees_by_ccy = agg.setdefault("_fees_by_ccy", _dd())  # type: ignore[arg-type]
        for ccy, qty in book.fees.items():
            fees_by_ccy[ccy] += qty

    # --- боты: сверка с PnL, который считает сама биржа ---
    bot_rows: list[dict] = []
    for algo, bot in sorted(inp.bots.items(), key=lambda kv: _ms(kv[1].get("cTime"))):
        if _ms(bot.get("cTime")) > t1 or bot_end_ms(bot) < t0:
            continue
        family = bot.get("_family") or ("dca" if "dca" in str(bot.get("algoOrdType")) else "grid")
        inst = bot.get("instId") or ""
        quote = inst.split("-")[1] if "-" in inst else USDT
        pnl_ccy = bot.get("investmentCcy") or quote if family == "dca" else quote
        invest_qty = _f(bot.get("investmentAmt") if family == "dca" else bot.get("investment")) or 0.0
        invest = invest_qty * (p1(pnl_ccy) or 0.0)
        okx_pnl = (_f(bot.get("totalPnl")) or 0.0) * (p1(pnl_ccy) or 0.0)
        sleeve = classify.bot_sleeve(algo)
        ledger_pnl = source_value.get(f"bot:{algo}", 0.0)
        bot_rows.append({
            "algoId": algo, "sleeve": sleeve, "algoOrdType": bot.get("algoOrdType"), "instId": inst,
            "algoClOrdId": bot.get("algoClOrdId") or "", "state": bot.get("state"),
            "created": _iso(_ms(bot.get("cTime")), per.tz) if bot.get("cTime") else None,
            "investment_usdt": _r(invest), "okx_total_pnl_usdt": _r(okx_pnl), "ledger_pnl_usdt": _r(ledger_pnl),
            "diff_usdt": _r(ledger_pnl - okx_pnl), "pnl_ccy": pnl_ccy,
            "grid_profit": _f(bot.get("gridProfit"), None), "float_profit": _f(bot.get("floatProfit"), None),
            "arbitrage_num": _f(bot.get("arbitrageNum"), None),
        })

    sleeves_out = []
    totals = defaultdict(float)
    for sleeve, meta in rules["sleeves"].items():
        agg = sleeve_rows.get(sleeve)
        active = agg is not None
        agg = agg or defaultdict(float)
        fees_by_ccy = agg.pop("_fees_by_ccy", {}) if active else {}
        invested = agg.get("cost_long_usdt", 0.0) + sum(r["investment_usdt"] or 0.0 for r in bot_rows
                                                        if r["sleeve"] == sleeve and r["state"] == "running")
        row = {"sleeve": sleeve, "title": meta.get("title", sleeve), "plan": bool(meta.get("plan")),
               "active": active}
        for key in ("pnl_usdt", "trading_pnl_usdt", "realized_usdt", "unrealized_change_usdt",
                    "unrealized_end_usdt", "fees_usdt", "funding_usdt", "earn_usdt", "interest_usdt",
                    "other_usdt", "volume_usdt", "cum_pnl_usdt"):
            row[key] = _r(agg.get(key, 0.0))
            if key in ("pnl_usdt", "trading_pnl_usdt", "realized_usdt", "fees_usdt", "funding_usdt",
                       "earn_usdt", "interest_usdt", "other_usdt", "volume_usdt"):
                totals[key] += agg.get(key, 0.0)
        row["trades"] = int(agg.get("trades", 0))
        totals["trades"] += row["trades"]
        row["invested_usdt"] = _r(invested)
        row["fees_by_ccy"] = _nonzero(fees_by_ccy)
        row["sources"] = sorted(source_rows.get(sleeve, []), key=lambda s: -abs(s["pnl_usdt"] or 0.0))
        sleeves_out.append(row)
    for sleeve in sleeve_rows:
        if sleeve not in rules["sleeves"]:  # защита: правило сослалось на неописанный рукав
            warnings.append(f"рукав {sleeve!r} из правил не описан в sleeves — строки нет в таблице")

    # --- вне рукавов и сверка ---
    h0_all, h1_all = _dd(), _dd()
    for book in books.values():
        for ccy, qty in book.h_t0.items():
            h0_all[ccy] += qty
        for ccy, qty in book.h_t1.items():
            h1_all[ccy] += qty
    reval = sum(qty * ((p1(ccy) or 0.0) - (p0(ccy) or 0.0)) for ccy, qty in u_t0.items() if abs(qty) > EPS)
    flows = {c: u_t1.get(c, 0.0) - u_t0.get(c, 0.0) for c in set(u_t0) | set(u_t1)}
    capital_value = _value(flows, p1)
    bal_t0 = {c: u_t0.get(c, 0.0) + h0_all.get(c, 0.0) for c in set(u_t0) | set(h0_all)}
    bal_t1 = {c: u_t1.get(c, 0.0) + h1_all.get(c, 0.0) for c in set(u_t1) | set(h1_all)}
    model_t0, model_t1 = _value(bal_t0, p0), _value(bal_t1, p1)
    sleeves_trading = sum(r["trading_pnl_usdt"] or 0.0 for r in sleeves_out)
    identity_error = (model_t1 - model_t0) - (sleeves_trading + reval + capital_value)
    if abs(identity_error) > 0.01:
        warnings.append(f"тождество модели нарушено на {identity_error:.4f} USDT — проверить цены валют")
    strategy_net = _nonzero(capital["strategy_transfer"], 8)
    if strategy_net:
        notes.append(f"переводы в/из ботов за период в сумме не 0: {strategy_net} — вероятно, граница периода")

    exchange_check = _exchange_check(inp, bills, opening, model_t1, per, warnings, notes)
    level_check = None
    trading_usdt = (inp.valuation or {}).get("details", {}).get("trading") if inp.valuation else None
    current = abs(t1 - inp.now_ms) <= PRICE_NOW_TOLERANCE_MS
    if current and trading_usdt:
        level_check = {"model_usdt": _r(model_t1, 4), "exchange_trading_usdt": _r(trading_usdt, 4),
                       "diff_usdt": _r(model_t1 - trading_usdt, 4),
                       "diff_pct": _r((model_t1 - trading_usdt) / trading_usdt * 100, 5)}

    # полнота истории: стартовые остатки ~0 — журнал покрывает счёт с первого bill
    if opening:
        notes.append("остатки до начала журнала (движения старше истории bills): "
                     + ", ".join(f"{c} {q:.8g}" for c, q in sorted(opening.items())))
    if classify.unmapped_bots:
        warnings.append("боты без правила в ops/sleeves.json -> «" + rules["sleeves"][classify.default]["title"]
                        + "»: " + ", ".join(sorted(classify.unmapped_bots)))
    if classify.orphan_bot_bills:
        warnings.append(f"исполнений нативных ботов без algoId: {classify.orphan_bot_bills} bills "
                        "(sub-orders не дали ordId) — в «прочее/ручное»")
    if classify.heuristic_bills:
        notes.append(f"{classify.heuristic_bills} bills ботов опознаны по instId и времени (нет ordId в sub-orders)")
    if classify.unknown_types:
        warnings.append("нераспознанные типы bills (type/subType: число) -> «прочее/ручное»: "
                        + ", ".join(f"{k}: {v}" for k, v in sorted(classify.unknown_types.items())))
    interest_ccy: defaultdict = _dd()
    for book in books.values():
        for ccy, qty in book.interest.items():
            interest_ccy[ccy] += qty
    if _nonzero(interest_ccy):
        warnings.append("проценты по займу за период (bills type 7): " + _holdings(_nonzero(interest_ccy))
                        + " — на счёте был заём (autoLoan): проверить availBal и режим аккаунта (SPOT-TDMODE)")
    missing = sorted(getattr(inp.price, "missing", set()))
    if missing:
        warnings.append(f"нет цены в USDT для {', '.join(missing)} — стоимость этих валют принята 0")
    for code in inp.registry_codes:
        if code not in classify.prefixes:
            notes.append(f"владелец {code!r} из src/order_owner.py без рукава в ops/sleeves.json")

    earn_note = None
    if inp.earn is None:
        earn_note = "источник Earn недоступен (finance/savings/lending-history)"
    elif not inp.earn and inp.mode == "demo":
        earn_note = "в demo Simple Earn недоступен (savings/balance -> 50038), начислений нет"
    unalloc_title = "Вне рукавов (стартовые активы, кэш)"
    report = {
        "mode": inp.mode,
        "period": {"label": per.label, "start": _iso(t0, per.tz), "end": _iso(per.end_ms, per.tz),
                   "t1": _iso(t1, per.tz), "days": per.days, "partial": per.partial,
                   "tz": _iso(t0, per.tz)[-6:]},
        "generated_at": _iso(inp.now_ms, per.tz),
        "currency": USDT,
        "sleeves": sleeves_out,
        "totals": {k: (_r(v) if k != "trades" else int(v)) for k, v in totals.items()},
        "unallocated": {
            "title": unalloc_title,
            "holdings_end": _nonzero(u_t1),
            "value_end_usdt": _r(_value(u_t1, p1)),
            "revaluation_usdt": _r(reval),
            "capital_flows_usdt": _r(capital_value),
            "transfers": _nonzero(capital["transfer"]),
            "strategy_transfers_net": strategy_net,
        },
        "bots": bot_rows,
        "equity": {
            "model_start_usdt": _r(model_t0, 4), "model_end_usdt": _r(model_t1, 4),
            "model_delta_usdt": _r(model_t1 - model_t0, 4),
            "exchange": {"trading_usdt": _r(trading_usdt, 4),
                         "valuation_usdt": _r((inp.valuation or {}).get("total_usdt"), 4),
                         "total_eq_usd": _r(inp.total_eq_usd, 4),
                         "details_usdt": (inp.valuation or {}).get("details") or {}},
            "level_check": level_check,
            "check": exchange_check,
        },
        "reconciliation": {
            "components_usdt": {"sleeves_trading_pnl": _r(sleeves_trading), "unallocated_revaluation": _r(reval),
                                "capital_flows": _r(capital_value)},
            "model_delta_usdt": _r(model_t1 - model_t0),
            "identity_error_usdt": _r(identity_error, 8),
            "opening_balances": _nonzero(opening),
            "history_from_account_start": not opening,
        },
        "earn": {"records": len(inp.earn or []), "by_ccy": _nonzero(earn_by_ccy), "note": earn_note},
        "warnings": warnings,
        "notes": notes,
        "sources": {"bills_total": len(bills), "bills_in_period": sum(1 for b in bills if t0 <= _ms(b.get("ts")) <= t1),
                    "bots": len(bot_rows), "bot_orders": len(inp.order_map)},
    }
    return report


def _exchange_check(inp: Inputs, bills: list[dict], opening: dict[str, float], model_t1: float,
                    per: Period, warnings: list[str], notes: list[str]) -> Optional[dict]:
    """Независимая проверка модели по снимкам equity биржи внутри периода.

    Эталон A — самый ранний снимок, согласованный с моделью (<= 5%); конец B —
    asset-valuation trading сейчас (текущий период) или последний снимок до t1.
    Невязка = Δ биржи − Δ модели на [A, B]: цены (индекс против last), время снимков.
    """
    t0, t1 = per.start_ms, per.t1_ms
    unit = ENGINE_EQUITY_UNIT.get(inp.mode, "USD")
    cands = [(ts, v, "ledger", USDT) for ts, v in inp.own_snapshots if t0 <= ts <= t1]
    cands += [(ts, v, "equity_curve движка", unit) for ts, v in inp.engine_curve if t0 <= ts <= t1]
    cands.sort()
    if not cands:
        notes.append("снимков equity биржи внутри периода нет — проверка только уровнем на конец периода")
        return None
    ordered = sorted(bills, key=lambda b: _ms(b.get("ts")))

    def balances_at(ts_ms: int) -> dict[str, float]:
        bal = defaultdict(float, opening)
        for bill in ordered:
            if _ms(bill.get("ts")) > ts_ms:
                break
            bal[bill.get("ccy") or "?"] += _f(bill.get("balChg")) or 0.0
        return bal

    def to_usdt(value: float, unit_: str, ts_ms: int) -> Optional[float]:
        if unit_ == USDT:
            return value
        rate = inp.usdt_usd(ts_ms)
        return value / rate if rate else None

    now_prices = lambda ccy: inp.price(ccy, inp.now_ms)  # noqa: E731
    skipped = 0
    ref = None
    for ts, value, source, unit_ in cands:
        bal = balances_at(ts)
        approx = _value(bal, now_prices)
        if not approx or abs(value - approx) / approx > PREFILTER_DEVIATION:
            skipped += 1
            continue
        model = _value(bal, lambda ccy, _ts=ts: inp.price(ccy, _ts))
        usdt = to_usdt(value, unit_, ts)
        if usdt is None or not model or abs(usdt - model) / model > REF_MAX_DEVIATION:
            skipped += 1
            continue
        ref = {"ts": _iso(ts, per.tz), "source": source, "exchange_usdt": usdt, "model_usdt": model, "_ms": ts}
        break
    if skipped:
        notes.append(f"пропущено снимков equity, несопоставимых с моделью (> {REF_MAX_DEVIATION:.0%}): {skipped}")
    if ref is None:
        return None
    end = None
    trading = ((inp.valuation or {}).get("details") or {}).get("trading")
    if abs(t1 - inp.now_ms) <= PRICE_NOW_TOLERANCE_MS and trading:
        end = {"ts": _iso(inp.now_ms, per.tz), "source": "asset-valuation trading", "exchange_usdt": trading,
               "model_usdt": model_t1}
    else:
        for ts, value, source, unit_ in reversed(cands):
            if ts <= ref["_ms"]:
                break
            usdt = to_usdt(value, unit_, ts)
            model = _value(balances_at(ts), lambda ccy, _ts=ts: inp.price(ccy, _ts))
            if usdt and model and abs(usdt - model) / model <= REF_MAX_DEVIATION:
                end = {"ts": _iso(ts, per.tz), "source": source, "exchange_usdt": usdt, "model_usdt": model}
                break
    if end is None:
        return None
    d_exchange = end["exchange_usdt"] - ref["exchange_usdt"]
    d_model = end["model_usdt"] - ref["model_usdt"]
    residual = d_exchange - d_model
    base = end["exchange_usdt"] or 1.0
    ref.pop("_ms")
    path = _equity_path([v for ts, v, s, u in cands if ts >= _ms_from_iso(ref["ts"]) and s == ref["source"]])
    return {"from": {k: (_r(v, 4) if isinstance(v, float) else v) for k, v in ref.items()},
            "to": {k: (_r(v, 4) if isinstance(v, float) else v) for k, v in end.items()},
            "exchange_delta_usdt": _r(d_exchange, 4), "model_delta_usdt": _r(d_model, 4),
            "residual_usdt": _r(residual, 4), "residual_pct": _r(residual / base * 100, 5),
            "equity_path": path}


def _ms_from_iso(text: str) -> int:
    return int(datetime.fromisoformat(text).timestamp() * 1000)


def _equity_path(values: list[float]) -> Optional[dict]:
    """Максимальная просадка по ряду снимков одного источника (для LIVE-REVIEW §6.3)."""
    if len(values) < 2:
        return None
    peak, max_dd = values[0], 0.0
    for value in values:
        peak = max(peak, value)
        if peak > 0:
            max_dd = max(max_dd, (peak - value) / peak)
    return {"snapshots": len(values), "min": _r(min(values), 4), "max": _r(max(values), 4),
            "max_drawdown_pct": _r(max_dd * 100, 4)}


# --- Представление ---

def _money(x: Optional[float], signed: bool = True) -> str:
    if x is None:
        return "—"
    text = f"{x:+,.2f}" if signed else f"{x:,.2f}"
    return text.replace(",", " ")


def render_markdown(report: dict) -> str:
    per = report["period"]
    eq = report["equity"]
    rec = report["reconciliation"]
    un = report["unallocated"]
    lines = [
        f"# PnL по рукавам — {report['mode']}, {per['label']}",
        "",
        f"- **Период:** {per['start']} — {per['t1']}" + (" (неполный: день ещё идёт)" if per["partial"] else ""),
        f"- **Сформирован:** {report['generated_at']}, валюта отчёта — USDT, цены — OKX SPOT "
        "(сейчас — тикеры, в прошлом — минутные свечи)",
        f"- **Источники:** bills {report['sources']['bills_total']} (за период {report['sources']['bills_in_period']}),"
        f" ботов {report['sources']['bots']}, ордеров ботов в журнале {report['sources']['bot_orders']}",
        "- **Методика:** insights/pnl-ledger.md — PnL рукава = mark-to-market его движений в bills; "
        "комиссии уже внутри PnL (колонка — справочно)",
        "",
        "## Рукава",
        "",
        "| Рукав | PnL | реализ. | Δ нереализ. | комиссии | funding | earn | сделок | оборот | вложено |",
        "| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |",
    ]
    for row in report["sleeves"]:
        if not row["active"] and not row["plan"]:
            continue
        lines.append(f"| {row['title']}{'' if row['active'] else ' (нет движений)'} | {_money(row['pnl_usdt'])} "
                     f"| {_money(row['realized_usdt'])} | {_money(row['unrealized_change_usdt'])} "
                     f"| {_money(row['fees_usdt'], False)} | {_money(row['funding_usdt'])} "
                     f"| {_money(row['earn_usdt'])} | {row['trades']} | {_money(row['volume_usdt'], False)} "
                     f"| {_money(row['invested_usdt'], False)} |")
    tot = report["totals"]
    lines += [
        f"| **Итого рукава** | **{_money(tot.get('pnl_usdt'))}** | {_money(tot.get('realized_usdt'))} | — "
        f"| {_money(tot.get('fees_usdt'), False)} | {_money(tot.get('funding_usdt'))} "
        f"| {_money(tot.get('earn_usdt'))} | {tot.get('trades', 0)} | {_money(tot.get('volume_usdt'), False)} | — |",
        f"| {un['title']} (переоценка) | {_money(un['revaluation_usdt'])} | — | — | — | — | — | — | — "
        f"| {_money(un['value_end_usdt'], False)} |",
        "",
        "## Сверка с equity",
        "",
        "| Компонент | USDT |",
        "| --- | --- |",
        f"| PnL рукавов на торговом счёте | {_money(rec['components_usdt']['sleeves_trading_pnl'])} |",
        f"| Переоценка активов вне рукавов ({_holdings(un['holdings_end'])}) "
        f"| {_money(rec['components_usdt']['unallocated_revaluation'])} |",
        f"| Переводы капитала | {_money(rec['components_usdt']['capital_flows'])} |",
        f"| **Δ стоимости торгового счёта (модель)** | **{_money(rec['model_delta_usdt'])}** |",
        f"| Тождество модели, ошибка | {rec['identity_error_usdt']} |",
        "",
        f"Стоимость торгового счёта по модели: {_money(eq['model_start_usdt'], False)} на начало → "
        f"{_money(eq['model_end_usdt'], False)} на конец.",
    ]
    ex = eq["exchange"]
    if eq.get("level_check"):
        lc = eq["level_check"]
        lines.append(f"Биржа сейчас: asset-valuation trading {_money(ex['trading_usdt'], False)} USDT "
                     f"(totalEq {_money(ex['total_eq_usd'], False)} USD); модель − биржа = "
                     f"{_money(lc['diff_usdt'])} ({lc['diff_pct']}%).")
    check = eq.get("check")
    if check:
        lines += ["", f"**Проверка по снимкам биржи** {check['from']['ts']} ({check['from']['source']}) → "
                      f"{check['to']['ts']} ({check['to']['source']}): Δ биржи {_money(check['exchange_delta_usdt'])}, "
                      f"Δ модели {_money(check['model_delta_usdt'])}, невязка {_money(check['residual_usdt'])} "
                      f"({check['residual_pct']}% equity)."]
        if check.get("equity_path"):
            path = check["equity_path"]
            lines.append(f"Макс. просадка по снимкам: {path['max_drawdown_pct']}% ({path['snapshots']} снимков).")
    lines.append(f"История bills с начала счёта: {'да' if rec['history_from_account_start'] else 'нет'}"
                 + ("" if rec["history_from_account_start"] else f" (остатки до журнала: {_holdings(rec['opening_balances'])})"))
    if report["bots"]:
        lines += ["", "## Нативные боты", "",
                  "| algoId | рукав | пара | тип | состояние | вложено | PnL OKX | PnL ledger | Δ |",
                  "| --- | --- | --- | --- | --- | --- | --- | --- | --- |"]
        titles = {r["sleeve"]: r["title"] for r in report["sleeves"]}
        for bot in report["bots"]:
            lines.append(f"| `{bot['algoId']}` | {titles.get(bot['sleeve'], bot['sleeve'])} | {bot['instId']} "
                         f"| {bot['algoOrdType']} | {bot['state']} | {_money(bot['investment_usdt'], False)} "
                         f"| {_money(bot['okx_total_pnl_usdt'])} | {_money(bot['ledger_pnl_usdt'])} "
                         f"| {_money(bot['diff_usdt'])} |")
    lines += ["", "## Источники внутри рукавов", ""]
    for row in report["sleeves"]:
        if row["sources"]:
            parts = [f"`{s['source']}` {_money(s['pnl_usdt'])} ({s['trades']})" for s in row["sources"][:12]]
            more = f" и ещё {len(row['sources']) - 12}" if len(row["sources"]) > 12 else ""
            lines.append(f"- **{row['title']}:** " + ", ".join(parts) + more)
    if report["earn"]["note"]:
        lines += ["", f"Earn: {report['earn']['note']}."]
    if report["warnings"]:
        lines += ["", "## Предупреждения", ""] + [f"- {w}" for w in report["warnings"]]
    if report["notes"]:
        lines += ["", "## Заметки", ""] + [f"- {n}" for n in report["notes"]]
    return "\n".join(lines) + "\n"


def _holdings(amounts: dict[str, float]) -> str:
    if not amounts:
        return "нет"
    return ", ".join(f"{c} {q:.6g}" for c, q in sorted(amounts.items(), key=lambda kv: kv[0]))


def telegram_text(report: dict, limit: int = 4000) -> str:
    """Текст ежедневного сообщения в Telegram. Отправки здесь нет — её сделает
    ALERTS-IMPL (src/notify.py), когда появятся TELEGRAM_BOT_TOKEN и chat id."""
    per, tot = report["period"], report["totals"]
    lines = [f"PnL {report['mode']} {per['label']} ({per['start'][11:16]}–{per['t1'][11:16]} {per['tz']}"
             + (", неполный день)" if per["partial"] else ")"),
             f"Рукава: {_money(tot.get('pnl_usdt'))} USDT; комиссии {_money(tot.get('fees_usdt'), False)}; "
             f"funding {_money(tot.get('funding_usdt'))}; earn {_money(tot.get('earn_usdt'))}; "
             f"сделок {tot.get('trades', 0)}"]
    for row in sorted((r for r in report["sleeves"] if r["active"]), key=lambda r: -abs(r["pnl_usdt"] or 0)):
        lines.append(f"- {row['title']}: {_money(row['pnl_usdt'])} (сделок {row['trades']})")
    un, rec = report["unallocated"], report["reconciliation"]
    lines.append(f"Вне рукавов, переоценка: {_money(un['revaluation_usdt'])}")
    lines.append(f"Счёт (модель): {_money(report['equity']['model_start_usdt'], False)} -> "
                 f"{_money(report['equity']['model_end_usdt'], False)} (Δ {_money(rec['model_delta_usdt'])})")
    check = report["equity"].get("check")
    if check:
        lines.append(f"Сверка с биржей: невязка {_money(check['residual_usdt'])} ({check['residual_pct']}%)")
    if report["warnings"]:
        lines.append(f"Предупреждений: {len(report['warnings'])} — см. отчёт pnl_{per['label']}.md")
    text = "\n".join(lines)
    return text if len(text) <= limit else text[:limit - 1] + "…"


def report_paths(reports_dir: Path, label: str) -> tuple[Path, Path]:
    return Path(reports_dir) / f"pnl_{label}.md", Path(reports_dir) / f"pnl_{label}.json"


def write_report(report: dict, reports_dir: Path) -> tuple[Path, Path]:
    md_path, json_path = report_paths(reports_dir, report["period"]["label"])
    md_path.parent.mkdir(parents=True, exist_ok=True)
    report["files"] = {"md": str(md_path), "json": str(json_path)}
    md_path.write_text(render_markdown(report), encoding="utf-8")
    json_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    return md_path, json_path


# --- Прогон ---

def run(mode: Optional[str] = None, day: Optional[date] = None, days: int = 1, tz: str = DEFAULT_TZ,
        rules_path: Path = RULES_PATH, exchange: Any = None, now_ms: Optional[int] = None,
        save: bool = True, reports_dir: Optional[Path] = None, engine_db: Optional[Path] = None,
        pause: float = 0.1, backfill_days: int = BACKFILL_DAYS, owner_of: Any = "auto") -> dict:
    """Синхронизировать журнал, построить отчёт, записать md + json. Только чтение с биржи."""
    mode = normalize_mode(mode or default_mode())
    rules = load_rules(rules_path)
    tzinfo = parse_tz(tz)
    paths = state_paths(mode)
    reports_dir = Path(reports_dir) if reports_dir is not None else paths.root / "reports"
    engine_db = Path(engine_db) if engine_db is not None else paths.bot_db
    if exchange is None:
        from .connector import create_exchange  # noqa: PLC0415 — сеть нужна только CLI
        exchange = create_exchange(load_settings(mode))
    reader = Reader(exchange, pause)
    warnings: list[str] = []
    notes: list[str] = []

    # 1. Баланс торгового счёта: его момент — момент оценки (bills позже него не учитываются)
    account = fetch_account(reader)
    now_ms = int(time.time() * 1000) if now_ms is None else int(now_ms)
    period = period_bounds(day, days, tzinfo, now_ms)
    try:
        valuation: Optional[dict] = fetch_valuation(reader)
    except Exception as exc:  # noqa: BLE001
        valuation = None
        warnings.append(f"asset-valuation недоступен: {_short(exc)}")

    journal = Journal(reports_dir / JOURNAL_NAME)
    since_ms = min(period.start_ms, now_ms - backfill_days * DAY_MS)
    bills_stats = sync_bills(reader, journal, since_ms, now_ms)
    bots_stats = sync_bots(reader, journal, since_ms, now_ms)
    warnings += [f"синхронизация ботов: {e}" for e in bots_stats.pop("errors")]
    try:
        earn: Optional[list[dict]] = fetch_earn(reader, period.start_ms, period.t1_ms)
    except Exception as exc:  # noqa: BLE001
        earn = None
        (notes if mode == "demo" else warnings).append(f"Earn: {_short(exc)}")
    try:
        engine_curve = read_equity_curve(engine_db, period.start_ms, period.t1_ms)
    except (sqlite3.Error, OSError) as exc:
        engine_curve = []
        notes.append(f"equity_curve {engine_db} не прочитан: {_short(exc)}")

    prices = PriceBook(reader, now_ms, warnings)
    usd = UsdRate(reader, now_ms, warnings)
    resolver = registry_owner_of() if owner_of == "auto" else owner_of
    inputs = Inputs(
        mode=mode, period=period, now_ms=now_ms, bills=journal.bills(until_ms=now_ms),
        order_map=journal.bot_order_map(), bots=journal.bots(), eq=account["eq"],
        total_eq_usd=account["total_eq_usd"], valuation=valuation, earn=earn, price=prices, usdt_usd=usd,
        engine_curve=engine_curve, own_snapshots=journal.snapshots(period.start_ms, period.t1_ms),
        owner_of=resolver, registry_codes=registry_codes() if owner_of == "auto" else [],
        warnings=warnings, notes=notes)
    report = build_report(inputs, rules)
    report["sources"].update({"bills_sync": bills_stats, "bots_sync": bots_stats, "api_calls": reader.calls,
                              "journal": str(journal.path)})
    # снимок equity — эталон для сверки следующих отчётов
    if valuation and abs(period.t1_ms - now_ms) <= PRICE_NOW_TOLERANCE_MS:
        journal.add_snapshot(now_ms, (valuation.get("details") or {}).get("trading"), valuation.get("total_usdt"),
                             account["total_eq_usd"], usd(now_ms))
    if save:
        write_report(report, reports_dir)
    return report


def summary_text(report: dict) -> str:
    tot, rec = report["totals"], report["reconciliation"]
    check = report["equity"].get("check") or {}
    lines = [f"PnL-ledger {report['mode']} {report['period']['label']}: рукава {_money(tot.get('pnl_usdt'))} USDT, "
             f"комиссии {_money(tot.get('fees_usdt'), False)}, сделок {tot.get('trades', 0)}; "
             f"вне рукавов {_money(report['unallocated']['revaluation_usdt'])}; "
             f"Δ счёта (модель) {_money(rec['model_delta_usdt'])}"
             + (f"; невязка с биржей {_money(check.get('residual_usdt'))}" if check else "")]
    for row in report["sleeves"]:
        if row["active"]:
            lines.append(f"  {row['sleeve']:<14} {_money(row['pnl_usdt']):>12}  сделок {row['trades']:>4}  "
                         f"комиссии {_money(row['fees_usdt'], False)}")
    for warning in report["warnings"]:
        lines.append(f"  ! {warning}")
    if report.get("files"):
        lines.append(f"  файлы: {report['files']['md']}, {report['files']['json']}")
    return "\n".join(lines)


def main(argv=None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    parser = argparse.ArgumentParser(prog="python -m src.pnl_ledger", description=__doc__.splitlines()[0])
    parser.add_argument("--mode", choices=MODES, default=None, help="demo|live (по умолчанию OKX_MODE, иначе demo)")
    parser.add_argument("--date", type=date.fromisoformat, default=None, help="последний день периода, YYYY-MM-DD")
    parser.add_argument("--days", type=int, default=1, help="длина периода в днях (7 — для LIVE-REVIEW)")
    parser.add_argument("--tz", default=DEFAULT_TZ, help="пояс границ дня, по умолчанию +05:00")
    parser.add_argument("--rules", type=Path, default=RULES_PATH, help="правила атрибуции")
    parser.add_argument("--telegram", action="store_true", help="напечатать текст сообщения для Telegram")
    parser.add_argument("--no-save", action="store_true", help="не писать файлы отчёта (журнал обновляется)")
    args = parser.parse_args(argv)
    try:
        report = run(mode=args.mode, day=args.date, days=args.days, tz=args.tz, rules_path=args.rules,
                     save=not args.no_save)
    except (RulesError, ValueError, RuntimeError) as exc:
        log.error("отчёт не построен: %s", exc)
        return 2
    print(summary_text(report))
    if args.telegram:
        print("--- telegram ---")
        print(telegram_text(report))
    return 1 if report["warnings"] else 0


if __name__ == "__main__":
    sys.exit(main())
