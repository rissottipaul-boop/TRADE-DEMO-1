"""SQLite-хранилище состояния бота (Фаза 1).

Таблицы: orders, positions, trades, equity_curve, ws_state.
Источник правды — локальное состояние; биржа опрашивается для сверки.

Состояния ордеров — сырые состояния OKX: live | partially_filled | filled |
canceled | mmp_canceled. Конечные состояния «липкие»: запоздалое сообщение
(например, ответ REST после fill из WS) не возвращает ордер в live.
"""
import json
import logging
import sqlite3
import time
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional

log = logging.getLogger("okx.storage")

DB_PATH = Path("data/bot_state.db")
SCHEMA_VERSION = 1

OPEN_STATES = ("live", "partially_filled")
FINAL_STATES = ("filled", "canceled", "mmp_canceled")

_SCHEMA = """
CREATE TABLE IF NOT EXISTS orders (
    ord_id TEXT PRIMARY KEY,
    inst_id TEXT NOT NULL,
    side TEXT NOT NULL,
    ord_type TEXT NOT NULL,
    px REAL,
    sz REAL NOT NULL,
    state TEXT NOT NULL,
    filled_sz REAL DEFAULT 0,
    avg_px REAL DEFAULT 0,
    fee REAL DEFAULT 0,
    create_time REAL NOT NULL,
    update_time REAL NOT NULL,
    raw_json TEXT,
    cl_ord_id TEXT
);
CREATE INDEX IF NOT EXISTS idx_orders_inst ON orders(inst_id);
CREATE INDEX IF NOT EXISTS idx_orders_state ON orders(state);
CREATE INDEX IF NOT EXISTS idx_orders_cl ON orders(cl_ord_id);

CREATE TABLE IF NOT EXISTS positions (
    inst_id TEXT NOT NULL,
    pos_side TEXT NOT NULL,
    pos REAL DEFAULT 0,
    avg_px REAL DEFAULT 0,
    upl REAL DEFAULT 0,
    liq_px REAL,
    update_time REAL NOT NULL,
    PRIMARY KEY (inst_id, pos_side)
);

-- tradeId у OKX уникален в пределах инструмента, поэтому ключ составной
CREATE TABLE IF NOT EXISTS trades (
    inst_id TEXT NOT NULL,
    trade_id TEXT NOT NULL,
    ord_id TEXT,
    side TEXT NOT NULL,
    px REAL NOT NULL,
    sz REAL NOT NULL,
    fee REAL DEFAULT 0,
    fee_ccy TEXT,
    ts REAL NOT NULL,
    PRIMARY KEY (inst_id, trade_id)
);
CREATE INDEX IF NOT EXISTS idx_trades_ts ON trades(ts);
CREATE INDEX IF NOT EXISTS idx_trades_ord ON trades(ord_id);

CREATE TABLE IF NOT EXISTS equity_curve (
    ts REAL PRIMARY KEY,
    total_eq REAL NOT NULL,
    avail_eq REAL NOT NULL,
    upl REAL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS ws_state (
    key TEXT PRIMARY KEY,
    value TEXT,
    update_time REAL
);
"""


@dataclass
class OrderRecord:
    ord_id: str
    inst_id: str  # instId OKX: BTC-USDT (не CCXT-символ BTC/USDT)
    side: str  # buy | sell
    ord_type: str  # limit | market | post_only | ...
    px: Optional[float]
    sz: float
    state: str  # live | partially_filled | filled | canceled | mmp_canceled
    filled_sz: float
    avg_px: float
    fee: float  # положительное число = списанная комиссия
    create_time: float
    update_time: float
    raw_json: str
    cl_ord_id: Optional[str] = None


@dataclass
class PositionRecord:
    inst_id: str
    pos_side: str  # long | short | net
    pos: float
    avg_px: float
    upl: float  # нереализованный PnL
    liq_px: Optional[float]
    update_time: float


@dataclass
class TradeRecord:
    inst_id: str
    trade_id: str
    ord_id: Optional[str]
    side: str
    px: float
    sz: float
    fee: float  # положительное число = списанная комиссия
    fee_ccy: Optional[str]
    ts: float


@dataclass
class EquityRecord:
    ts: float
    total_eq: float
    avail_eq: float
    upl: float


class Storage:
    def __init__(self, db_path: Path = DB_PATH):
        db_path = Path(db_path)
        db_path.parent.mkdir(parents=True, exist_ok=True)
        self.db_path = db_path
        self._closed = False
        self._init_db()

    @contextmanager
    def _conn(self):
        conn = sqlite3.connect(self.db_path, timeout=10)
        conn.row_factory = sqlite3.Row
        try:
            yield conn
            conn.commit()
        finally:
            conn.close()

    def close(self) -> None:
        """Явное закрытие хранилища.

        Storage не держит постоянного sqlite3-соединения — каждая операция
        открывает своё через _conn() и закрывает его в finally, поэтому
        штатная работа (в т.ч. движок) не приводит к утечкам дескрипторов.
        close() даёт явную точку завершения (симметрично OrderRouter.close())
        и делает WAL-чекпойнт: сливает journal в основной файл и снимает
        файловые локи -wal/-shm — это важно для тестов с temp-БД на Windows,
        которые удаляют временную директорию сразу после использования
        Storage (задача STORAGE-CLOSE, из KILL-CALLBACK-OWNER).

        Идемпотентен: повторный вызов безопасен и ничего не делает.
        """
        if self._closed:
            return
        try:
            with self._conn() as conn:
                conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        except sqlite3.Error:
            log.warning("close(): WAL-чекпойнт для %s не выполнен", self.db_path, exc_info=True)
        finally:
            self._closed = True

    def __enter__(self) -> "Storage":
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.close()

    def _init_db(self) -> None:
        with self._conn() as conn:
            # WAL: читатели (дашборд, CLI) не блокируют запись движка
            conn.execute("PRAGMA journal_mode=WAL")
            version = conn.execute("PRAGMA user_version").fetchone()[0]
            tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            if version == 0 and "orders" in tables:
                self._migrate_v0_to_v1(conn)
            conn.executescript(_SCHEMA)
            conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
        log.info("DB initialized: %s", self.db_path)

    @staticmethod
    def _migrate_v0_to_v1(conn: sqlite3.Connection) -> None:
        """v0 → v1: orders.cl_ord_id; trades с ключом (inst_id, trade_id) и fee_ccy."""
        log.info("Миграция БД v0 → v1")
        cols = {r["name"] for r in conn.execute("PRAGMA table_info(orders)")}
        if "cl_ord_id" not in cols:
            conn.execute("ALTER TABLE orders ADD COLUMN cl_ord_id TEXT")
        trade_cols = {r["name"] for r in conn.execute("PRAGMA table_info(trades)")}
        if trade_cols and "fee_ccy" not in trade_cols:
            conn.execute("ALTER TABLE trades RENAME TO trades_v0")
            conn.executescript(_SCHEMA)
            conn.execute("""
                INSERT OR IGNORE INTO trades (inst_id, trade_id, ord_id, side, px, sz, fee, ts)
                SELECT inst_id, trade_id, ord_id, side, px, sz, fee, ts FROM trades_v0
            """)
            conn.execute("DROP TABLE trades_v0")

    # --- Orders ---

    def upsert_order(self, order: OrderRecord) -> None:
        final = ", ".join(f"'{s}'" for s in FINAL_STATES)
        with self._conn() as conn:
            conn.execute(f"""
                INSERT INTO orders (ord_id, inst_id, side, ord_type, px, sz, state,
                    filled_sz, avg_px, fee, create_time, update_time, raw_json, cl_ord_id)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(ord_id) DO UPDATE SET
                    state = CASE WHEN orders.state IN ({final}) THEN orders.state
                                 ELSE excluded.state END,
                    px = COALESCE(excluded.px, orders.px),
                    sz = excluded.sz,
                    filled_sz = MAX(orders.filled_sz, excluded.filled_sz),
                    avg_px = CASE WHEN excluded.filled_sz >= orders.filled_sz
                                  THEN excluded.avg_px ELSE orders.avg_px END,
                    fee = CASE WHEN excluded.filled_sz >= orders.filled_sz
                               THEN excluded.fee ELSE orders.fee END,
                    update_time = excluded.update_time,
                    raw_json = excluded.raw_json,
                    cl_ord_id = COALESCE(orders.cl_ord_id, excluded.cl_ord_id)
            """, (
                order.ord_id, order.inst_id, order.side, order.ord_type,
                order.px, order.sz, order.state, order.filled_sz,
                order.avg_px, order.fee, order.create_time, order.update_time,
                order.raw_json, order.cl_ord_id,
            ))

    def get_order(self, ord_id: str) -> Optional[sqlite3.Row]:
        with self._conn() as conn:
            return conn.execute("SELECT * FROM orders WHERE ord_id=?", (ord_id,)).fetchone()

    def get_open_orders(self, inst_id: Optional[str] = None) -> list[sqlite3.Row]:
        placeholders = ", ".join("?" for _ in OPEN_STATES)
        query = f"SELECT * FROM orders WHERE state IN ({placeholders})"
        params: tuple = OPEN_STATES
        if inst_id:
            query += " AND inst_id=?"
            params += (inst_id,)
        with self._conn() as conn:
            return conn.execute(query, params).fetchall()

    # --- Trades ---

    def insert_trade(self, trade: TradeRecord) -> bool:
        """Идемпотентно; True — сделка новая."""
        with self._conn() as conn:
            cur = conn.execute("""
                INSERT OR IGNORE INTO trades (inst_id, trade_id, ord_id, side, px, sz, fee, fee_ccy, ts)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (trade.inst_id, trade.trade_id, trade.ord_id, trade.side, trade.px,
                  trade.sz, trade.fee, trade.fee_ccy, trade.ts))
            return cur.rowcount > 0

    def get_trades(self, inst_id: Optional[str] = None, limit: int = 1000) -> list[sqlite3.Row]:
        with self._conn() as conn:
            if inst_id:
                return conn.execute(
                    "SELECT * FROM trades WHERE inst_id=? ORDER BY ts DESC LIMIT ?", (inst_id, limit)
                ).fetchall()
            return conn.execute("SELECT * FROM trades ORDER BY ts DESC LIMIT ?", (limit,)).fetchall()

    # --- Positions ---

    def upsert_position(self, pos: PositionRecord) -> None:
        with self._conn() as conn:
            conn.execute("""
                INSERT INTO positions VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(inst_id, pos_side) DO UPDATE SET
                    pos=excluded.pos,
                    avg_px=excluded.avg_px,
                    upl=excluded.upl,
                    liq_px=excluded.liq_px,
                    update_time=excluded.update_time
            """, (pos.inst_id, pos.pos_side, pos.pos, pos.avg_px, pos.upl, pos.liq_px, pos.update_time))

    def get_positions(self) -> list[sqlite3.Row]:
        with self._conn() as conn:
            return conn.execute("SELECT * FROM positions WHERE pos != 0").fetchall()

    # --- Equity ---

    def record_equity(self, eq: EquityRecord) -> None:
        with self._conn() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO equity_curve VALUES (?, ?, ?, ?)",
                (eq.ts, eq.total_eq, eq.avail_eq, eq.upl)
            )

    def get_equity_history(self, limit: int = 1000) -> list[sqlite3.Row]:
        with self._conn() as conn:
            return conn.execute(
                "SELECT * FROM equity_curve ORDER BY ts DESC LIMIT ?", (limit,)
            ).fetchall()

    # --- WS state (для восстановления после рестарта) ---

    def set_ws_state(self, key: str, value: Any) -> None:
        with self._conn() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO ws_state VALUES (?, ?, ?)",
                (key, json.dumps(value), time.time())
            )

    def get_ws_state(self, key: str) -> Optional[Any]:
        with self._conn() as conn:
            row = conn.execute("SELECT value FROM ws_state WHERE key=?", (key,)).fetchone()
            return json.loads(row["value"]) if row else None
