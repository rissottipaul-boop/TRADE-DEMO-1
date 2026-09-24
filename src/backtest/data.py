"""Рыночные данные бэктестера (insights/backtester-design.md §4).

- Хранилище: отдельная SQLite data/market_data.db (не bot_state.db): рыночные
  данные — append-only массив со своим жизненным циклом, бэктест читает read-only.
- Источник: публичный REST OKX GET /api/v5/market/history-candles (без ключей).
  Факты проверены 2026-09-24: limit=300 -> 300 строк; первая строка ответа —
  формирующаяся свеча с confirm=0; after=ts -> записи СТАРШЕ ts.
- Троттлинг: 1 запрос / 0.25 с = 8 зап/2с — ниже бюджета задачи (10/2с) и лимита
  OKX (20/2с на IP): параллельно работают движок и другие агенты.
- Целостность (§4.3): только confirm=1 и только свечи, закрытые к моменту загрузки;
  INSERT OR IGNORE (подтверждённая свеча финальна, строки не переписываются);
  gap-check; dataset_id — хэш содержимого диапазона для воспроизводимости.
"""
import hashlib
import json
import logging
import math
import sqlite3
import time
import urllib.error
import urllib.parse
import urllib.request
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal
from functools import lru_cache
from pathlib import Path
from typing import Callable, Iterator, Optional, Sequence

log = logging.getLogger("okx.backtest.data")

DB_PATH = Path("data/market_data.db")
SCHEMA_VERSION = 1
OKX_REST = "https://www.okx.com"
PAGE_LIMIT = 300
MIN_REQUEST_INTERVAL = 0.25
RETRYABLE_CODES = {"50011", "50013", "50026"}  # rate limit / system busy / system error

BAR_MS = {
    "1m": 60_000, "3m": 180_000, "5m": 300_000, "15m": 900_000, "30m": 1_800_000,
    "1H": 3_600_000, "2H": 7_200_000, "4H": 14_400_000,
    "6H": 21_600_000, "6Hutc": 21_600_000, "12H": 43_200_000, "12Hutc": 43_200_000,
    "1D": 86_400_000, "1Dutc": 86_400_000,
}


# --- Базовые типы ---

@dataclass(frozen=True, slots=True)
class Bar:
    """Подтверждённая свеча: ts — время ОТКРЫТИЯ в мс UTC (как у OKX)."""
    ts: int
    o: float
    h: float
    l: float
    c: float
    vol: float = 0.0


@dataclass(frozen=True)
class InstrumentSpec:
    """Торговые шаги инструмента (GET /public/instruments), §3.2."""
    inst_id: str
    tick_sz: float
    lot_sz: float
    min_sz: float


@dataclass(frozen=True)
class Gap:
    """Дыра в ряду: первая и последняя отсутствующие свечи (open time) и их число."""
    start_ts: int
    end_ts: int
    missing: int


@dataclass
class Dataset:
    inst_id: str
    bar: str
    bars: list[Bar]
    gaps: list[Gap] = field(default_factory=list)
    anomalies: list[str] = field(default_factory=list)
    dataset_id: str = ""

    @property
    def bar_ms(self) -> int:
        return BAR_MS[self.bar]

    @property
    def first_ts(self) -> Optional[int]:
        return self.bars[0].ts if self.bars else None

    @property
    def last_ts(self) -> Optional[int]:
        return self.bars[-1].ts if self.bars else None


# --- Округления к шагам биржи (§3.2) ---

@lru_cache(maxsize=64)
def _step_decimals(step: float) -> int:
    return max(0, -Decimal(repr(step)).normalize().as_tuple().exponent)


def floor_to_step(x: float, step: float) -> float:
    if step <= 0:
        return x
    return round(math.floor(x / step + 1e-9) * step, _step_decimals(step))


def ceil_to_step(x: float, step: float) -> float:
    if step <= 0:
        return x
    return round(math.ceil(x / step - 1e-9) * step, _step_decimals(step))


def round_to_step(x: float, step: float) -> float:
    if step <= 0:
        return x
    return round(round(x / step) * step, _step_decimals(step))


def ms_to_iso(ts_ms: int) -> str:
    return datetime.fromtimestamp(ts_ms / 1000, tz=timezone.utc).strftime("%Y-%m-%d %H:%M")


def iso_to_ms(value: str) -> int:
    """'2022-01-01' или '2022-01-01T00:00' (UTC) -> мс."""
    dt = datetime.fromisoformat(value)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return int(dt.timestamp() * 1000)


# --- Контроль целостности (§4.3) ---

def check_gaps(ts_list: Sequence[int], bar_ms: int) -> list[Gap]:
    """Дыры в ряду open-time с шагом бара.

    Свеча без сделок (vol=0, OHLC = прошлый close) — НЕ дыра: OKX её отдаёт.
    Невозрастающий ряд — ошибка данных, а не дыра: ValueError.
    """
    gaps: list[Gap] = []
    for prev, cur in zip(ts_list, ts_list[1:]):
        step = cur - prev
        if step <= 0:
            raise ValueError(f"ряд свечей не строго возрастает: {prev} -> {cur}")
        if step > bar_ms:
            gaps.append(Gap(prev + bar_ms, cur - bar_ms, step // bar_ms - 1))
    return gaps


def check_bars(bars: Sequence[Bar], bar_ms: int) -> tuple[list[Gap], list[str]]:
    """Gap-check + аномалии OHLC (невалидные цены, невыровненный шаг)."""
    anomalies: list[str] = []
    for b in bars:
        if min(b.o, b.h, b.l, b.c) <= 0:
            anomalies.append(f"{ms_to_iso(b.ts)}: неположительная цена")
        elif b.l > min(b.o, b.c) or b.h < max(b.o, b.c):
            anomalies.append(f"{ms_to_iso(b.ts)}: нарушено l <= o,c <= h")
    for prev, cur in zip(bars, bars[1:]):
        if (cur.ts - prev.ts) % bar_ms:
            anomalies.append(f"{ms_to_iso(cur.ts)}: шаг {cur.ts - prev.ts} мс не кратен бару")
    return check_gaps([b.ts for b in bars], bar_ms), anomalies


def dataset_hash(inst_id: str, bar: str, bars: Sequence[Bar]) -> str:
    """Хэш содержимого диапазона: тот же снапшот -> тот же id (воспроизводимость)."""
    h = hashlib.sha256(f"{inst_id}|{bar}".encode())
    for b in bars:
        h.update(f"|{b.ts},{b.o!r},{b.h!r},{b.l!r},{b.c!r},{b.vol!r}".encode())
    return h.hexdigest()[:16]


# --- Хранилище ---

_SCHEMA = """
CREATE TABLE IF NOT EXISTS candles (
    inst_id TEXT NOT NULL,
    bar     TEXT NOT NULL,
    ts      INTEGER NOT NULL,           -- open time, ms UTC
    o REAL NOT NULL, h REAL NOT NULL, l REAL NOT NULL, c REAL NOT NULL,
    vol REAL, vol_ccy REAL, vol_ccy_quote REAL,
    confirm INTEGER NOT NULL,           -- пишутся только 1 (финальные)
    raw TEXT,                           -- исходная строка биржи (аудит расхождений)
    PRIMARY KEY (inst_id, bar, ts)      -- PK уже индекс по (inst_id, bar, ts)
);
CREATE TABLE IF NOT EXISTS instruments (
    inst_id TEXT PRIMARY KEY,
    inst_type TEXT,
    tick_sz REAL NOT NULL,
    lot_sz REAL NOT NULL,
    min_sz REAL NOT NULL,
    raw TEXT,
    fetched_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS datasets (
    dataset_id TEXT PRIMARY KEY,
    inst_id TEXT NOT NULL,
    bar TEXT NOT NULL,
    first_ts INTEGER,
    last_ts INTEGER,
    n_bars INTEGER NOT NULL,
    gaps_json TEXT,
    source TEXT,
    created_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS holdout_touches (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    dataset_id TEXT NOT NULL,
    strategy TEXT NOT NULL,
    params_json TEXT,
    touched_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS schema_meta (
    key TEXT PRIMARY KEY,
    value TEXT
);
"""


class MarketDataStore:
    """SQLite рыночных данных (стиль src/storage.py: явный PK, сырые значения)."""

    def __init__(self, db_path: Path | str = DB_PATH):
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        with self._conn() as conn:
            conn.executescript(_SCHEMA)
            conn.execute("INSERT OR IGNORE INTO schema_meta VALUES ('schema_version', ?)",
                         (str(SCHEMA_VERSION),))

    @contextmanager
    def _conn(self) -> Iterator[sqlite3.Connection]:
        conn = sqlite3.connect(self.db_path)
        try:
            yield conn
            conn.commit()
        finally:
            conn.close()

    def upsert_candles(self, inst_id: str, bar: str, rows: Sequence[Sequence[str]],
                       now_ms: Optional[int] = None) -> int:
        """Пишет только подтверждённые и уже закрытые свечи. Возвращает число новых строк."""
        bar_ms = BAR_MS[bar]
        now_ms = int(time.time() * 1000) if now_ms is None else now_ms
        values = []
        for r in rows:
            if len(r) < 9 or str(r[8]) != "1":
                continue  # confirm=0 — формирующаяся свеча, в датасет не попадает
            ts = int(r[0])
            if ts + bar_ms > now_ms:
                continue  # верхний край: только завершённые к моменту загрузки
            values.append((inst_id, bar, ts, float(r[1]), float(r[2]), float(r[3]),
                           float(r[4]), float(r[5]), float(r[6]), float(r[7]), 1,
                           json.dumps(list(r))))
        if not values:
            return 0
        with self._conn() as conn:
            before = conn.total_changes
            conn.executemany(
                "INSERT OR IGNORE INTO candles VALUES (?,?,?,?,?,?,?,?,?,?,?,?)", values)
            return conn.total_changes - before

    def ts_range(self, inst_id: str, bar: str) -> tuple[Optional[int], Optional[int], int]:
        with self._conn() as conn:
            row = conn.execute(
                "SELECT MIN(ts), MAX(ts), COUNT(*) FROM candles WHERE inst_id=? AND bar=?",
                (inst_id, bar)).fetchone()
        return row[0], row[1], int(row[2])

    def load_ts(self, inst_id: str, bar: str) -> list[int]:
        with self._conn() as conn:
            rows = conn.execute("SELECT ts FROM candles WHERE inst_id=? AND bar=? ORDER BY ts",
                                (inst_id, bar)).fetchall()
        return [int(r[0]) for r in rows]

    def load_bars(self, inst_id: str, bar: str, start_ms: Optional[int] = None,
                  end_ms: Optional[int] = None) -> list[Bar]:
        """Свечи с open time в [start_ms, end_ms), по возрастанию, только confirm=1."""
        sql = "SELECT ts, o, h, l, c, vol FROM candles WHERE inst_id=? AND bar=? AND confirm=1"
        args: list = [inst_id, bar]
        if start_ms is not None:
            sql += " AND ts >= ?"
            args.append(start_ms)
        if end_ms is not None:
            sql += " AND ts < ?"
            args.append(end_ms)
        with self._conn() as conn:
            rows = conn.execute(sql + " ORDER BY ts", args).fetchall()
        return [Bar(int(r[0]), r[1], r[2], r[3], r[4], r[5] or 0.0) for r in rows]

    def save_instrument(self, spec: InstrumentSpec, inst_type: str = "SPOT",
                        raw: Optional[dict] = None) -> None:
        with self._conn() as conn:
            conn.execute("INSERT OR REPLACE INTO instruments VALUES (?,?,?,?,?,?,?)",
                         (spec.inst_id, inst_type, spec.tick_sz, spec.lot_sz, spec.min_sz,
                          json.dumps(raw or {}), time.time()))

    def get_instrument(self, inst_id: str) -> Optional[InstrumentSpec]:
        with self._conn() as conn:
            row = conn.execute("SELECT tick_sz, lot_sz, min_sz FROM instruments WHERE inst_id=?",
                               (inst_id,)).fetchone()
        return InstrumentSpec(inst_id, row[0], row[1], row[2]) if row else None

    def register_dataset(self, ds: Dataset, source: str = "okx:history-candles") -> None:
        with self._conn() as conn:
            conn.execute(
                "INSERT OR IGNORE INTO datasets VALUES (?,?,?,?,?,?,?,?,?)",
                (ds.dataset_id, ds.inst_id, ds.bar, ds.first_ts, ds.last_ts, len(ds.bars),
                 json.dumps([g.__dict__ for g in ds.gaps]), source, time.time()))

    def log_holdout_touch(self, dataset_id: str, strategy: str, params: dict) -> int:
        """Фиксирует касание holdout (§2.1: одно касание). Возвращает число касаний."""
        with self._conn() as conn:
            conn.execute("INSERT INTO holdout_touches (dataset_id, strategy, params_json, "
                         "touched_at) VALUES (?,?,?,?)",
                         (dataset_id, strategy, json.dumps(params, sort_keys=True), time.time()))
            row = conn.execute("SELECT COUNT(*) FROM holdout_touches WHERE dataset_id=? AND "
                               "strategy=?", (dataset_id, strategy)).fetchone()
        return int(row[0])

    def holdout_touch_counts(self, inst_id: Optional[str] = None) -> dict[str, int]:
        """Касания holdout по стратегиям (все снапшоты инструмента) — для отчёта."""
        sql = ("SELECT h.strategy, COUNT(*) FROM holdout_touches h LEFT JOIN datasets d "
               "ON d.dataset_id = h.dataset_id")
        args: tuple = ()
        if inst_id is not None:
            sql += " WHERE d.inst_id = ?"
            args = (inst_id,)
        with self._conn() as conn:
            rows = conn.execute(sql + " GROUP BY h.strategy ORDER BY h.strategy", args).fetchall()
        return {r[0]: int(r[1]) for r in rows}


def load_dataset(store: MarketDataStore, inst_id: str, bar: str,
                 start_ms: Optional[int] = None, end_ms: Optional[int] = None,
                 register: bool = True) -> Dataset:
    """Загрузка диапазона + gap-check + dataset_id (регистрация снапшота)."""
    bars = store.load_bars(inst_id, bar, start_ms, end_ms)
    gaps, anomalies = check_bars(bars, BAR_MS[bar])
    ds = Dataset(inst_id, bar, bars, gaps, anomalies, dataset_hash(inst_id, bar, bars))
    if register and bars:
        store.register_dataset(ds)
    if gaps:
        log.warning("%s %s: %d дыр(ы), пропущено свечей: %d", inst_id, bar, len(gaps),
                    sum(g.missing for g in gaps))
    return ds


# --- Публичный REST OKX ---

class OkxApiError(RuntimeError):
    """Ответ OKX с code != '0' (не глушится: вызывающий получает исключение)."""


class OkxPublicClient:
    """Минимальный клиент публичных эндпоинтов (stdlib, без ключей и без demo-заголовка:
    рыночные данные demo = live-книга, demo-slippage.md §5)."""

    def __init__(self, base_url: str = OKX_REST, min_interval: float = MIN_REQUEST_INTERVAL,
                 timeout: float = 15.0, max_retries: int = 4,
                 sleep: Callable[[float], None] = time.sleep,
                 clock: Callable[[], float] = time.monotonic,
                 opener: Optional[Callable[[str, float], bytes]] = None):
        self.base_url = base_url.rstrip("/")
        self.min_interval = min_interval
        self.timeout = timeout
        self.max_retries = max_retries
        self._sleep = sleep
        self._clock = clock
        self._opener = opener or self._urlopen
        self._last_request: Optional[float] = None
        self.requests = 0

    @staticmethod
    def _urlopen(url: str, timeout: float) -> bytes:
        req = urllib.request.Request(url, headers={"User-Agent": "okx-backtest/1.0"})
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.read()

    def _throttle(self) -> None:
        if self._last_request is not None:
            wait = self.min_interval - (self._clock() - self._last_request)
            if wait > 0:
                self._sleep(wait)
        self._last_request = self._clock()

    def get(self, path: str, params: dict) -> list:
        url = f"{self.base_url}{path}?{urllib.parse.urlencode(params)}"
        attempt = 0
        while True:
            self._throttle()
            self.requests += 1
            try:
                payload = json.loads(self._opener(url, self.timeout).decode())
            except urllib.error.HTTPError as exc:
                if exc.code == 429 and attempt < self.max_retries:
                    attempt += 1
                    log.warning("HTTP 429 %s — пауза %.1fс", path, 2.0 * attempt)
                    self._sleep(2.0 * attempt)
                    continue
                raise
            except (urllib.error.URLError, TimeoutError) as exc:
                if attempt < self.max_retries:
                    attempt += 1
                    log.warning("сеть %s: %s — повтор %d", path, exc, attempt)
                    self._sleep(1.0 * attempt)
                    continue
                raise
            code = str(payload.get("code"))
            if code == "0":
                return payload.get("data") or []
            if code in RETRYABLE_CODES and attempt < self.max_retries:
                attempt += 1
                log.warning("OKX code=%s %s — пауза %.1fс", code, payload.get("msg"), 2.0 * attempt)
                self._sleep(2.0 * attempt)
                continue
            raise OkxApiError(f"{path}: code={code} msg={payload.get('msg')!r}")

    def history_candles(self, inst_id: str, bar: str, after: Optional[int] = None,
                        limit: int = PAGE_LIMIT) -> list[list[str]]:
        params = {"instId": inst_id, "bar": bar, "limit": str(limit)}
        if after is not None:
            params["after"] = str(after)
        return self.get("/api/v5/market/history-candles", params)

    def instrument(self, inst_id: str, inst_type: str = "SPOT") -> tuple[InstrumentSpec, dict]:
        data = self.get("/api/v5/public/instruments", {"instType": inst_type, "instId": inst_id})
        if not data:
            raise OkxApiError(f"инструмент {inst_id} не найден")
        x = data[0]
        return InstrumentSpec(inst_id, float(x["tickSz"]), float(x["lotSz"]),
                              float(x["minSz"])), x


def download_candles(store: MarketDataStore, client: OkxPublicClient, inst_id: str, bar: str,
                     since_ms: int, now_ms: Optional[int] = None,
                     max_pages: int = 100_000) -> dict:
    """Докачка свечей в прошлое пагинацией after (§4.1). Идемпотентна.

    Фаза 1: от текущего момента назад до перекрытия с уже сохранённым максимумом.
    Фаза 2: история старше сохранённого минимума — до since_ms.
    """
    now_ms = int(time.time() * 1000) if now_ms is None else now_ms
    lo, hi, _ = store.ts_range(inst_id, bar)
    report = {"inst_id": inst_id, "bar": bar, "pages": 0, "inserted": 0}

    def page_back(after: Optional[int], stop_at: int) -> None:
        while report["pages"] < max_pages:
            rows = client.history_candles(inst_id, bar, after=after)
            report["pages"] += 1
            if not rows:
                return
            report["inserted"] += store.upsert_candles(inst_id, bar, rows, now_ms=now_ms)
            oldest = min(int(r[0]) for r in rows)
            if oldest <= stop_at:
                return
            if after is not None and oldest >= after:
                log.warning("пагинация %s не продвинулась (after=%s)", inst_id, after)
                return
            after = oldest

    page_back(None, hi if hi is not None else since_ms)
    if lo is not None and lo > since_ms:
        page_back(lo, since_ms)
    # Фаза 3: дозаполнение дыр (прерванная прошлая загрузка оставляет разрыв между
    # «новым верхом» и старым максимумом). Реальные дыры биржи данных не вернут —
    # страница уйдёт старше дыры, и цикл остановится (1 запрос на дыру).
    unresolved = []
    for gap in check_gaps(store.load_ts(inst_id, bar), BAR_MS[bar]):
        before = report["inserted"]
        page_back(gap.end_ts + BAR_MS[bar], gap.start_ts)
        if report["inserted"] == before:
            unresolved.append(gap)
    lo2, hi2, n2 = store.ts_range(inst_id, bar)
    report.update(first_ts=lo2, last_ts=hi2, total=n2, exchange_gaps=len(unresolved))
    return report
