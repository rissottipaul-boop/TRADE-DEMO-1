"""Локальный архив истории funding OKX (CARRY-FUNDING-ARCHIVE).

Публичный API OKX отдаёт историю funding только за ≈ 94 дня
(insights/funding-carry.md §1). Чтобы у CARRY-IMPL и бэктестов был родной ряд
OKX глубже этого окна, архиватор регулярно дописывает новые периоды в
data/funding_history.db.

Факты API (проверены 24.09 и 03.10.2026):
- только публичный GET /api/v5/public/funding-rate-history, без ключей и без
  заголовка x-simulated-trading: demo-ряд не совпадает с live (funding-carry.md §7)
  и не архивируется — клиент ходит только на live-домен;
- ответ отсортирован от новых к старым, максимум 100 строк за вызов;
- пагинация назад — параметр `after` = fundingTime самой старой полученной строки
  (строки СТАРШЕ after); `before` окно назад не двигает;
- за пределами ≈ 94 дней API возвращает пустой массив.

Алгоритм sync: каждый запуск выкачивает всё доступное окно (≈ 3 страницы на
инструмент) и пишет его одной транзакцией через INSERT OR IGNORE по ключу
(inst_id, funding_time). Отсюда:
- повторный запуск не даёт дублей;
- любой пропуск внутри окна 94 дня (между запусками или после оборванного
  запуска) дозаполняется сам;
- оборванный запуск не оставляет «дыры» посередине: инструмент записывается
  целиком или не записывается вовсе;
- если с прошлого запуска прошло больше окна API, разрыв не восстановить —
  он попадает в warnings (exit 1).

Запуск — не реже раза в 30 дней (ops/code-map.md). Только чтение биржи, ордеров нет.
"""
from __future__ import annotations

import argparse
import json
import logging
import sqlite3
import sys
import time
from contextlib import closing
from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Iterable, Optional, Protocol

log = logging.getLogger("okx.funding_archive")

DB_PATH = Path("data/funding_history.db")
SCHEMA_VERSION = 1
ENDPOINT = "/api/v5/public/funding-rate-history"
DEFAULT_INSTRUMENTS = ("BTC-USDT-SWAP", "ETH-USDT-SWAP")
PAGE_LIMIT = 100
# Предохранитель от бесконечной пагинации: 94 дня при интервале 1 ч — 2256 строк.
MAX_PAGES = 40
# Самый длинный штатный интервал funding — 8 ч; шаг длиннее — пропуск в ряду.
MAX_STEP_MS = 8 * 3600 * 1000
# Расписание: не реже раза в 30 дней. Ряд старше этого — предупреждение.
STALE_AFTER_MS = 30 * 86_400 * 1000

_SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS funding_rate (
    inst_id       TEXT    NOT NULL,
    funding_time  INTEGER NOT NULL,  -- мс UTC, как fundingTime у OKX
    funding_rate  TEXT    NOT NULL,  -- строка OKX без потери точности
    realized_rate TEXT,
    method        TEXT,
    formula_type  TEXT,
    fetched_at    INTEGER NOT NULL,  -- мс UTC первой записи строки
    PRIMARY KEY (inst_id, funding_time)
) WITHOUT ROWID;
CREATE TABLE IF NOT EXISTS sync_run (
    run_at        INTEGER NOT NULL,
    inst_id       TEXT    NOT NULL,
    pages         INTEGER NOT NULL,
    fetched       INTEGER NOT NULL,
    inserted      INTEGER NOT NULL,
    window_oldest INTEGER,
    window_newest INTEGER,
    warning       TEXT
);
"""


class FundingArchiveError(RuntimeError):
    """Ответ API не прошёл проверку — в архив ничего не пишется."""


class PublicClient(Protocol):
    def get(self, path: str, params: dict) -> list: ...


@dataclass(frozen=True, slots=True)
class FundingRow:
    inst_id: str
    funding_time: int
    funding_rate: str
    realized_rate: Optional[str]
    method: Optional[str]
    formula_type: Optional[str]


@dataclass
class SyncResult:
    inst_id: str
    pages: int = 0
    fetched: int = 0
    inserted: int = 0
    window_oldest: Optional[int] = None
    window_newest: Optional[int] = None
    prev_newest: Optional[int] = None
    warnings: list[str] = field(default_factory=list)

    def as_dict(self) -> dict:
        return {
            "inst_id": self.inst_id, "pages": self.pages, "fetched": self.fetched,
            "inserted": self.inserted,
            "window_oldest": _iso(self.window_oldest), "window_newest": _iso(self.window_newest),
            "prev_newest": _iso(self.prev_newest), "warnings": list(self.warnings),
        }


def _iso(ms: Optional[int]) -> Optional[str]:
    if ms is None:
        return None
    return datetime.fromtimestamp(ms / 1000, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _now_ms() -> int:
    return int(time.time() * 1000)


# --- Хранилище ---

def connect(path: Path = DB_PATH) -> sqlite3.Connection:
    """Открыть (и при необходимости создать) архив. Схема только дополняется."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path))
    conn.executescript(_SCHEMA)
    row = conn.execute("SELECT value FROM meta WHERE key='schema_version'").fetchone()
    if row is None:
        conn.execute("INSERT INTO meta(key, value) VALUES('schema_version', ?)", (str(SCHEMA_VERSION),))
        conn.commit()
    elif int(row[0]) > SCHEMA_VERSION:
        conn.close()
        raise FundingArchiveError(
            f"схема архива v{row[0]} новее кода v{SCHEMA_VERSION} — обновите код, файл не трогаю")
    return conn


def newest_time(conn: sqlite3.Connection, inst_id: str) -> Optional[int]:
    row = conn.execute("SELECT MAX(funding_time) FROM funding_rate WHERE inst_id=?", (inst_id,)).fetchone()
    return row[0] if row else None


def load_series(conn: sqlite3.Connection, inst_id: str) -> list[tuple[int, str]]:
    """Ряд (funding_time, funding_rate) по возрастанию времени — для бэктестов и CARRY-IMPL."""
    return list(conn.execute(
        "SELECT funding_time, funding_rate FROM funding_rate WHERE inst_id=? ORDER BY funding_time",
        (inst_id,)))


def find_gaps(times: Iterable[int], max_step_ms: int = MAX_STEP_MS) -> list[tuple[int, int]]:
    """Пары соседних периодов с шагом длиннее штатного (пропуск в ряду)."""
    gaps: list[tuple[int, int]] = []
    prev: Optional[int] = None
    for t in times:
        if prev is not None and t - prev > max_step_ms:
            gaps.append((prev, t))
        prev = t
    return gaps


# --- Разбор ответа ---

def parse_row(raw: dict, inst_id: str) -> FundingRow:
    """Проверить строку ответа. Чужой instId или битые поля — исключение, а не пропуск."""
    if not isinstance(raw, dict):
        raise FundingArchiveError(f"{inst_id}: строка ответа не объект: {raw!r}")
    if raw.get("instId") != inst_id:
        raise FundingArchiveError(f"{inst_id}: в ответе чужой instId {raw.get('instId')!r}")
    try:
        ft = int(raw["fundingTime"])
    except (KeyError, TypeError, ValueError):
        raise FundingArchiveError(f"{inst_id}: нет или битый fundingTime: {raw!r}") from None
    if ft <= 0:
        raise FundingArchiveError(f"{inst_id}: fundingTime {ft} <= 0")
    rate = raw.get("fundingRate")
    try:
        if rate in (None, "") or not Decimal(str(rate)).is_finite():
            raise InvalidOperation
    except (InvalidOperation, ValueError):
        raise FundingArchiveError(f"{inst_id}: битый fundingRate {rate!r} на {ft}") from None
    realized = raw.get("realizedRate") or None
    return FundingRow(inst_id=inst_id, funding_time=ft, funding_rate=str(rate),
                      realized_rate=realized, method=raw.get("method") or None,
                      formula_type=raw.get("formulaType") or None)


def fetch_window(client: PublicClient, inst_id: str, limit: int = PAGE_LIMIT,
                 max_pages: int = MAX_PAGES) -> tuple[list[FundingRow], int]:
    """Всё доступное окно истории: от новых к старым через `after`, до пустой страницы.

    Возвращает (строки без дублей по времени, число запросов).
    """
    rows: dict[int, FundingRow] = {}
    after: Optional[int] = None
    pages = 0
    while True:
        if pages >= max_pages:
            raise FundingArchiveError(
                f"{inst_id}: пагинация не закончилась за {max_pages} страниц — проверьте ответ API")
        params = {"instId": inst_id, "limit": str(limit)}
        if after is not None:
            params["after"] = str(after)
        data = client.get(ENDPOINT, params)
        pages += 1
        if not data:
            break
        page = [parse_row(raw, inst_id) for raw in data]
        oldest = min(r.funding_time for r in page)
        if after is not None and oldest >= after:
            # Курсор не сдвинулся назад — иначе зациклимся (так ведёт себя `before`).
            raise FundingArchiveError(
                f"{inst_id}: страница после after={after} не старше курсора (oldest={oldest})")
        for r in page:
            rows.setdefault(r.funding_time, r)
        after = oldest
    return sorted(rows.values(), key=lambda r: r.funding_time), pages


# --- Синхронизация ---

def sync_instrument(conn: sqlite3.Connection, client: PublicClient, inst_id: str,
                    now_ms: Optional[int] = None) -> SyncResult:
    now_ms = _now_ms() if now_ms is None else now_ms
    res = SyncResult(inst_id=inst_id, prev_newest=newest_time(conn, inst_id))
    rows, res.pages = fetch_window(client, inst_id)
    res.fetched = len(rows)
    if rows:
        res.window_oldest = rows[0].funding_time
        res.window_newest = rows[-1].funding_time
    if res.prev_newest is not None:
        if not rows:
            res.warnings.append(f"{inst_id}: API вернул пустое окно — архив не пополнен")
        elif res.window_oldest > res.prev_newest + MAX_STEP_MS:
            res.warnings.append(
                f"{inst_id}: невосстановимый разрыв {_iso(res.prev_newest)} → {_iso(res.window_oldest)}: "
                f"с прошлого запуска прошло больше окна API (≈ 94 дня)")
    with conn:  # одна транзакция на инструмент: всё или ничего
        before = conn.total_changes
        conn.executemany(
            "INSERT OR IGNORE INTO funding_rate(inst_id, funding_time, funding_rate, realized_rate,"
            " method, formula_type, fetched_at) VALUES(?,?,?,?,?,?,?)",
            [(r.inst_id, r.funding_time, r.funding_rate, r.realized_rate, r.method, r.formula_type, now_ms)
             for r in rows])
        res.inserted = conn.total_changes - before
        conn.execute(
            "INSERT INTO sync_run(run_at, inst_id, pages, fetched, inserted, window_oldest, window_newest,"
            " warning) VALUES(?,?,?,?,?,?,?,?)",
            (now_ms, inst_id, res.pages, res.fetched, res.inserted, res.window_oldest, res.window_newest,
             "; ".join(res.warnings) or None))
    return res


def sync(conn: sqlite3.Connection, client: PublicClient,
         instruments: Iterable[str] = DEFAULT_INSTRUMENTS,
         now_ms: Optional[int] = None) -> list[SyncResult]:
    return [sync_instrument(conn, client, inst, now_ms=now_ms) for inst in instruments]


def status(conn: sqlite3.Connection, instruments: Iterable[str] = DEFAULT_INSTRUMENTS,
           now_ms: Optional[int] = None) -> list[dict]:
    """Покрытие архива: число периодов, диапазон, пропуски, свежесть."""
    now_ms = _now_ms() if now_ms is None else now_ms
    out = []
    for inst in instruments:
        times = [t for t, _ in load_series(conn, inst)]
        gaps = find_gaps(times)
        warnings = [f"{inst}: пропуск {_iso(a)} → {_iso(b)}" for a, b in gaps]
        if not times:
            warnings.append(f"{inst}: архив пуст — запустите sync")
        elif now_ms - times[-1] > STALE_AFTER_MS:
            warnings.append(f"{inst}: последний период {_iso(times[-1])} старше 30 дней — sync пропущен")
        last_run = conn.execute("SELECT MAX(run_at) FROM sync_run WHERE inst_id=?", (inst,)).fetchone()[0]
        out.append({
            "inst_id": inst, "periods": len(times),
            "oldest": _iso(times[0]) if times else None, "newest": _iso(times[-1]) if times else None,
            "days": round((times[-1] - times[0]) / 86_400_000, 2) if times else 0.0,
            "gaps": len(gaps), "last_run": _iso(last_run), "warnings": warnings,
        })
    return out


# --- CLI ---

def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="python -m src.funding_archive",
        description="Архив истории funding OKX (live, публичный API, без ключей).")
    p.add_argument("command", nargs="?", default="sync", choices=("sync", "status"),
                   help="sync — дописать доступное окно API (по умолчанию); status — покрытие архива")
    p.add_argument("--inst", action="append", metavar="INST_ID",
                   help="инструмент (можно несколько раз); по умолчанию BTC- и ETH-USDT-SWAP")
    p.add_argument("--db", type=Path, default=DB_PATH, help=f"файл архива (по умолчанию {DB_PATH})")
    p.add_argument("--json", action="store_true", help="вывод в JSON")
    return p


def main(argv: Optional[list[str]] = None, client: Optional[PublicClient] = None,
         now_ms: Optional[int] = None) -> int:
    """Коды выхода: 0 — норма, 1 — есть предупреждения, 2 — ошибка сети, API или архива."""
    args = build_parser().parse_args(argv)
    instruments = tuple(args.inst) if args.inst else DEFAULT_INSTRUMENTS
    try:
        with closing(connect(args.db)) as conn:
            if args.command == "sync":
                if client is None:
                    # Live-домен и без demo-заголовка: demo-ряд не архивируем (funding-carry.md §7).
                    from src.backtest.data import OkxPublicClient
                    client = OkxPublicClient()
                results = [r.as_dict() for r in sync(conn, client, instruments, now_ms=now_ms)]
            results_status = status(conn, instruments, now_ms=now_ms)
    except Exception as exc:  # сеть, OKX code != 0, битый ответ, схема — всё в exit 2
        print(f"funding_archive: ошибка: {exc}", file=sys.stderr)
        return 2
    payload = {"command": args.command, "db": str(args.db), "status": results_status}
    if args.command == "sync":
        payload["sync"] = results
    warnings = [w for s in results_status for w in s["warnings"]]
    if args.command == "sync":
        warnings = [w for r in results for w in r["warnings"]] + warnings
    payload["warnings"] = warnings
    if args.json:
        print(json.dumps(payload, ensure_ascii=False, indent=2))
    else:
        if args.command == "sync":
            for r in results:
                print(f"{r['inst_id']}: страниц {r['pages']}, получено {r['fetched']}, новых {r['inserted']}, "
                      f"окно API {r['window_oldest']} … {r['window_newest']}")
        for s in results_status:
            print(f"{s['inst_id']}: в архиве {s['periods']} периодов, {s['oldest']} … {s['newest']} "
                  f"({s['days']} дн), пропусков {s['gaps']}, последний sync {s['last_run']}")
        for w in warnings:
            print(f"ПРЕДУПРЕЖДЕНИЕ: {w}")
    return 1 if warnings else 0


if __name__ == "__main__":
    logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(name)s: %(message)s")
    sys.exit(main())
