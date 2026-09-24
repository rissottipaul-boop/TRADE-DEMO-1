"""Детерминированный памп-сканер без LLM (задача PUMP-CODIFY). Только чтение публичных данных.

    python -m src.pump_scanner --exclude ETH SOL SUI ADA TRX ETC APT BNB XLM DOT
    python -m src.pump_scanner --json                    # машинный отчёт (для агента и PUMP-BT)
    python -m src.pump_scanner --no-journal              # без строки в data/pump_journal.jsonl
    python -m src.pump_scanner --at 2026-09-24T09:47+05:00 --pairs OKB-USDT LTC-USDT
                                                         # повтор прошлого скана

Методика — скилл spot-momentum-scan-validate, фаза 1 «только скан», в редакции скана №5
Pump Risk Taker (insights/pump-scan-2026-09-24.md, 09:47+05). Кандидат сканера — не сделка:
вход, сайзинг, стоп и проверку ликвидности решает Pump Risk Taker по pump-pocket.json.

Данные — только публичные GET, без ключей (других запросов opener не пропускает):
- GET /api/v5/market/tickers?instType=SPOT — универсум;
- GET /api/v5/market/candles — свечи живого скана; /market/history-candles — повтор (--at).
Лента (--feed): demo (по умолчанию) — заголовок x-simulated-trading: 1, как `okx --demo`
в сканах №1–5; live — без заголовка. Demo-свечи строятся из сделок demo: бывают плоские бары
без сделок и выбросы объёма ×1000 (insights/okx-api.md §10 п.8), поэтому объём — к медиане.

Универсум: SPOT-пары BASE-USDT без стейблкоинов (STABLECOINS), без BTC-USDT и без баз из
--exclude; топ-N (--top, по умолчанию 45) по volCcy24h. С --pairs универсум — ровно этот
список, без tickers и без исключений.

Свечи 1H: в расчёт идут только подтверждённые (confirm == "1"), последние --bars штук
(по умолчанию 99 = `market candles --limit 100` сканов №4–5 минус формирующаяся). Это
сознательное отличие от calc_indicators.py скилла: тот считает по формирующейся свече и по
vol в базовой валюте. Меньше 30 закрытых свечей — «данных мало». Последняя закрытая свеча
старее, чем у остальных пар, — «свеча устарела» (пара стоит или скан перешёл через час).

Метрики последней закрытой свечи:
- импульс % = (close − open) / open × 100;
- vol× = volCcyQuote (индекс 7 строки свечи) / медиана и / среднее volCcyQuote 20
  предыдущих закрытых свечей; фильтр — по медиане, среднее — для сравнения;
- RSI(14) Уайлдера, MA20 (SMA close), MACD-гистограмма 12/26/9 — src/backtest/indicators.py,
  по всем --bars закрытым свечам; 24ч % — от open 24-й с конца свечи до close последней;
- score = min(vol× / vol_min × 3, 5) + min(импульс / impulse_min × 3, 5) — формула скилла,
  vol× — по медиане.

Кандидат — все пять условий (пороги — параметры CLI): импульс ≥ 1.5 %, vol× (медиана) ≥ 1.5,
50 ≤ RSI ≤ 72, close > MA20, MACD-гистограмма > 0. Кандидаты — по убыванию score; «почти
кандидаты» — не выполнено ровно одно условие; у каждой пары — список невыполненных условий.

Повтор (--at ВРЕМЯ --pairs …): свечи из history-candles, закрытые к моменту --at
(open + 1 ч ≤ --at; запрос after = --at − 1 ч + 1 мс). Время — ISO с поясом. Тикеров на
прошлый момент у OKX нет, поэтому --pairs обязателен. Повтор в журнал не пишет: это проверка,
а не событие кармана. --endpoint candles — диагностика расхождений повтора.

Журнал data/pump_journal.jsonl (--journal): строка на событие, UTF-8, JSON в одну строку,
только дописывание. Сканер пишет event="scan" после живого скана; вход, стоп, выход и ошибки
пишет Pump Risk Taker. Поля строки scan (формат v=1):
  ts          время скана, ISO с поясом проекта +05:00
  event       "scan"
  source      "src.pump_scanner" — детерминированный скан, не LLM
  v           версия формата строки (1)
  feed        "demo" | "live"
  bar         "1H"
  candle_ts   open time проверенной закрытой свечи, ISO UTC ("2026-09-24T03:00:00Z")
  bars        число закрытых свечей в расчёте
  thresholds  {impulse_min, vol_ratio_min, rsi_min, rsi_max}
  universe    {source: "tickers"|"pairs", tickers, usdt_pairs, non_stable, eligible, top_n,
               exclude} — числа отбора (для pairs — только source и eligible)
  pairs       проверенные пары: с ts, bars и feed дают команду повтора (--at ts --pairs …)
  counts      {scanned, candidates, near, insufficient, stale}
  candidates  [{inst_id, close, vol_quote, impulse_pct, vol_ratio_median, vol_ratio_mean,
                rsi, ma20, macd_hist, chg24_pct, score}] — по убыванию score
  near        то же + failed: [условие] — не хватило одного условия
  warnings    предупреждения скана (лента отстаёт, пустой универсум)
  note        одна строка обоснования итога
Условия (ключи checks/failed): impulse, volume, rsi, ma20, macd.

Коды выхода: 0 — скан выполнен (с кандидатами или без); 2 — ошибка данных или сети (скан не
состоялся, в журнал не пишется), а также ошибка записи журнала и неверные аргументы.
"""
import argparse
import http.client
import json
import logging
import math
import statistics
import sys
import time
import urllib.parse
import urllib.request
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable, Iterable, Optional, Sequence

from .backtest.data import OKX_REST, OkxApiError, OkxPublicClient
from .backtest.indicators import is_nan, macd, rsi_wilder, sma

log = logging.getLogger("okx.pump_scanner")

SOURCE = "src.pump_scanner"
FORMAT_VERSION = 1
PROJECT_TZ = timezone(timedelta(hours=5))    # время проекта (ops/board.md)
JOURNAL_PATH = Path("data/pump_journal.jsonl")

BAR = "1H"
BAR_MS = 3_600_000
QUOTE = "USDT"
DEFAULT_TOP_N = 45           # скан №5
DEFAULT_BARS = 99            # `okx market candles --limit 100` минус формирующаяся
MIN_BARS = 30                # меньше — «данных мало» (скилл)
MA_PERIOD = 20
RSI_PERIOD = 14
VOL_WINDOW = 20
MACD_PERIODS = (12, 26, 9)
MACD_WARMUP = MACD_PERIODS[1] + MACD_PERIODS[2] - 1   # 34 свечи до первой гистограммы
MAX_BARS = 1000
MAX_PAGES = 20
EPS = 1e-9                   # допуск сравнения с порогами (ошибка округления float)

TICKERS_PATH = "/api/v5/market/tickers"
ENDPOINTS = {"candles": ("/api/v5/market/candles", 300),           # путь, макс. limit
             "history": ("/api/v5/market/history-candles", 100)}
PUBLIC_PATHS = frozenset({TICKERS_PATH} | {p for p, _ in ENDPOINTS.values()})
USER_AGENT = "okx-pump-scanner/1.0"

ALWAYS_EXCLUDED = frozenset({"BTC"})     # BTC-USDT: движок P1-72H и grid (сканы №4–5)
STABLECOINS = frozenset({
    "USDT", "USDC", "DAI", "TUSD", "USDP", "PAX", "BUSD", "GUSD", "FDUSD", "PYUSD", "USDD",
    "USDE", "USDG", "USDS", "RLUSD", "USD1", "LUSD", "FRAX", "SUSD", "USDJ", "USDK", "AUSD",
    "EURC", "EUROC", "EURT", "EURI", "EURS",
})

CONDITIONS = ("impulse", "volume", "rsi", "ma20", "macd")
STATUS_ORDER = ("candidate", "near", "rejected", "stale", "insufficient")


class ScanError(RuntimeError):
    """Скан не может быть выполнен: неверные данные биржи или запрещённый запрос."""


# Ошибки, при которых скан не состоялся (код выхода 2). HTTPError/URLError/TimeoutError — OSError.
SCAN_ERRORS = (ScanError, OkxApiError, OSError, http.client.HTTPException, json.JSONDecodeError)


@dataclass(frozen=True)
class Thresholds:
    impulse_min: float = 1.5     # импульс свечи, %
    vol_ratio_min: float = 1.5   # объём к медиане 20 предыдущих свечей
    rsi_min: float = 50.0
    rsi_max: float = 72.0


@dataclass(frozen=True)
class Candle:
    ts: int                      # open time, мс UTC
    o: float
    h: float
    l: float
    c: float
    vol_quote: float             # volCcyQuote — оборот в котируемой валюте (USDT)
    confirm: bool


# --- Время ---

def iso_local(ms: Optional[int]) -> Optional[str]:
    if ms is None:
        return None
    return datetime.fromtimestamp(ms / 1000, PROJECT_TZ).isoformat(timespec="seconds")


def iso_utc(ms: Optional[int]) -> Optional[str]:
    if ms is None:
        return None
    return datetime.fromtimestamp(ms / 1000, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_at(text: str) -> int:
    """ISO-время с поясом -> мс UTC. Без пояса — ошибка: 09:47 по +05:00 и по UTC — разные свечи."""
    value = text.strip()
    if value[-1:] in ("Z", "z"):
        value = value[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(value)
    except ValueError:
        raise argparse.ArgumentTypeError(f"не ISO-время: {text!r}") from None
    if dt.tzinfo is None:
        raise argparse.ArgumentTypeError(
            f"укажите часовой пояс: {text}+05:00 или {text}Z")
    return int(dt.timestamp() * 1000)


# --- Публичный REST (только GET из PUBLIC_PATHS) ---

def make_opener(feed: str) -> Callable[[str, float], bytes]:
    """Opener для OkxPublicClient: GET без ключей; demo — заголовок x-simulated-trading: 1."""
    headers = {"User-Agent": USER_AGENT, "Accept": "application/json"}
    if feed == "demo":
        headers["x-simulated-trading"] = "1"

    def opener(url: str, timeout: float) -> bytes:
        parts = urllib.parse.urlsplit(url)
        if f"{parts.scheme}://{parts.netloc}" != OKX_REST or parts.path not in PUBLIC_PATHS:
            raise ScanError(f"запрещённый запрос: {parts.path} — сканер читает только "
                            f"публичные market-эндпоинты")
        req = urllib.request.Request(url, headers=headers, method="GET")
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.read()

    return opener


def make_client(feed: str) -> OkxPublicClient:
    return OkxPublicClient(opener=make_opener(feed))


# --- Разбор данных ---

def _num(value, what: str) -> float:
    if value in (None, ""):
        return 0.0
    try:
        return float(value)
    except (TypeError, ValueError):
        raise ScanError(f"нечисловое поле {what}: {value!r}") from None


def parse_candle(row: Sequence, inst_id: str) -> Candle:
    """Строка OKX [ts, o, h, l, c, vol, volCcy, volCcyQuote, confirm] -> Candle."""
    if not isinstance(row, (list, tuple)) or len(row) < 9:
        raise ScanError(f"{inst_id}: строка свечи без volCcyQuote/confirm: {str(row)[:120]}")
    try:
        return Candle(int(row[0]), float(row[1]), float(row[2]), float(row[3]), float(row[4]),
                      _num(row[7], "volCcyQuote"), str(row[8]) == "1")
    except (TypeError, ValueError):
        raise ScanError(f"{inst_id}: нечисловое поле свечи: {str(row)[:120]}") from None


def closed_candles(rows: Iterable[Sequence], inst_id: str,
                   at_ms: Optional[int] = None) -> list[Candle]:
    """Только confirm == "1" (и при --at — закрытые к моменту at), по возрастанию времени."""
    out: dict[int, Candle] = {}
    for row in rows:
        c = parse_candle(row, inst_id)
        if c.confirm and (at_ms is None or c.ts + BAR_MS <= at_ms):
            out[c.ts] = c
    return [out[ts] for ts in sorted(out)]


def fetch_closed_candles(client, inst_id: str, n_bars: int = DEFAULT_BARS, *,
                         at_ms: Optional[int] = None, endpoint: str = "candles") -> list[Candle]:
    """Последние n_bars закрытых свечей (пагинация after — к более старым)."""
    path, page_limit = ENDPOINTS[endpoint]
    after = None if at_ms is None else at_ms - BAR_MS + 1   # open + бар <= at
    got: dict[int, Candle] = {}
    for _ in range(MAX_PAGES):
        limit = max(1, min(page_limit, n_bars - len(got) + 1))   # +1 — формирующаяся
        params = {"instId": inst_id, "bar": BAR, "limit": str(limit)}
        if after is not None:
            params["after"] = str(after)
        rows = client.get(path, params)
        if not rows:
            break
        for c in closed_candles(rows, inst_id, at_ms):
            got[c.ts] = c
        oldest = min(int(r[0]) for r in rows)
        if len(got) >= n_bars or len(rows) < limit or (after is not None and oldest >= after):
            break
        after = oldest
    return [got[ts] for ts in sorted(got)][-n_bars:]


def normalize_bases(items: Iterable[str]) -> set[str]:
    """'eth', 'SOL-USDT', 'ada,trx' -> {'ETH', 'SOL', 'ADA', 'TRX'}."""
    out = set()
    for item in items or ():
        for part in str(item).replace(",", " ").split():
            base = part.strip().upper().split("-")[0]
            if base:
                out.add(base)
    return out


def normalize_pairs(items: Iterable[str]) -> list[str]:
    out: list[str] = []
    for item in items or ():
        for part in str(item).replace(",", " ").split():
            inst = part.strip().upper()
            if inst.count("-") != 1 or inst.startswith("-") or inst.endswith("-"):
                raise argparse.ArgumentTypeError(f"пара вида BASE-USDT, получено {part!r}")
            if inst not in out:
                out.append(inst)
    return out


def select_universe(tickers: Sequence[dict], top_n: int = DEFAULT_TOP_N,
                    exclude: Iterable[str] = ()) -> tuple[list[str], dict]:
    """USDT-пары без стейблов, BTC и --exclude; топ-N по volCcy24h (при равенстве — по имени)."""
    excl = normalize_bases(exclude)
    usdt = non_stable = 0
    eligible: list[tuple[float, str]] = []
    for t in tickers:
        inst = str(t.get("instId", "")).upper()
        base, _, quote = inst.partition("-")
        if quote != QUOTE or not base:
            continue
        usdt += 1
        if base in STABLECOINS:
            continue
        non_stable += 1
        if base in ALWAYS_EXCLUDED or base in excl:
            continue
        eligible.append((_num(t.get("volCcy24h"), f"{inst} volCcy24h"), inst))
    eligible.sort(key=lambda x: (-x[0], x[1]))
    info = {"source": "tickers", "tickers": len(tickers), "usdt_pairs": usdt,
            "non_stable": non_stable, "eligible": len(eligible), "top_n": top_n,
            "exclude": sorted(excl | ALWAYS_EXCLUDED)}
    return [inst for _, inst in eligible[:top_n]], info


# --- Метрики и фильтр ---

def compute_metrics(candles: Sequence[Candle]) -> dict:
    """Метрики последней закрытой свечи (нужно >= VOL_WINDOW + 1 свечей)."""
    if len(candles) < VOL_WINDOW + 1:
        raise ValueError(f"нужно >= {VOL_WINDOW + 1} свечей")
    last = candles[-1]
    closes = [c.c for c in candles]
    window = [c.vol_quote for c in candles[-VOL_WINDOW - 1:-1]]
    median = statistics.median(window)
    mean = math.fsum(window) / len(window)
    rsi = rsi_wilder(closes, RSI_PERIOD)[-1]
    ma = sma(closes, MA_PERIOD)[-1]
    hist = macd(closes, *MACD_PERIODS)[2][-1]
    ref = candles[-24].o if len(candles) >= 24 else None
    return {
        "open": last.o,
        "close": last.c,
        "vol_quote": last.vol_quote,
        "impulse_pct": (last.c - last.o) / last.o * 100.0 if last.o > 0 else None,
        "vol_median": median,
        "vol_mean": mean,
        "vol_ratio_median": last.vol_quote / median if median > 0 else None,
        "vol_ratio_mean": last.vol_quote / mean if mean > 0 else None,
        "rsi": None if is_nan(rsi) else rsi,
        "ma20": None if is_nan(ma) else ma,
        "macd_hist": None if is_nan(hist) else hist,
        "chg24_pct": (last.c - ref) / ref * 100.0 if ref else None,
    }


def _fmt(x: Optional[float], spec: str) -> str:
    return "—" if x is None else format(x, spec)


def evaluate(m: dict, th: Thresholds) -> tuple[dict[str, bool], list[str]]:
    """Пять условий кандидата -> (checks, причины невыполненных — по-русски)."""
    imp, vr, rsi = m.get("impulse_pct"), m.get("vol_ratio_median"), m.get("rsi")
    ma, close, hist = m.get("ma20"), m.get("close"), m.get("macd_hist")
    checks = {
        "impulse": imp is not None and imp >= th.impulse_min - EPS,
        "volume": vr is not None and vr >= th.vol_ratio_min - EPS,
        "rsi": rsi is not None and th.rsi_min - EPS <= rsi <= th.rsi_max + EPS,
        "ma20": ma is not None and close is not None and close > ma,
        "macd": hist is not None and hist > 0,
    }
    text = {
        "impulse": f"импульс {_fmt(imp, '+.2f')}% < {th.impulse_min:g}%",
        "volume": (f"объём {vr:.2f}× медианы < {th.vol_ratio_min:g}×" if vr is not None
                   else "объём: медиана 20 свечей = 0"),
        "rsi": (f"RSI {rsi:.1f} вне {th.rsi_min:g}–{th.rsi_max:g}" if rsi is not None
                else "RSI: нет данных"),
        "ma20": (f"close {close:.6g} ≤ MA20 {ma:.6g}" if ma is not None and close is not None
                 else "MA20: нет данных"),
        "macd": (f"MACD-гистограмма {hist:.3g} ≤ 0" if hist is not None
                 else f"MACD: нет данных (< {MACD_WARMUP} свечей)"),
    }
    return checks, [text[k] for k in CONDITIONS if not checks[k]]


def signal_score(m: dict, th: Thresholds) -> float:
    """Формула скилла (по 5 баллов максимум на объём и импульс); объём — к медиане."""
    score = 0.0
    vr, imp = m.get("vol_ratio_median"), m.get("impulse_pct")
    if vr:
        score += min(vr / th.vol_ratio_min * 3, 5)
    if imp is not None:
        score += min(imp / th.impulse_min * 3, 5)
    return score


def analyze_pair(inst_id: str, candles: Sequence[Candle], th: Thresholds) -> dict:
    """Строка отчёта по паре; статус (кроме stale) — candidate / near / rejected / insufficient."""
    row = {"inst_id": inst_id, "bars": len(candles),
           "candle_ts": candles[-1].ts if candles else None}
    if len(candles) < MIN_BARS:
        row.update(status="insufficient", metrics=None, checks={}, failed=[], score=None,
                   reasons=[f"данных мало: {len(candles)} закрытых свечей < {MIN_BARS}"])
        return row
    m = compute_metrics(candles)
    checks, reasons = evaluate(m, th)
    failed = [k for k in CONDITIONS if not checks[k]]
    status = "candidate" if not failed else ("near" if len(failed) == 1 else "rejected")
    row.update(status=status, metrics=m, checks=checks, failed=failed, reasons=reasons,
               score=signal_score(m, th))
    return row


def _order_key(row: dict) -> tuple:
    rank = STATUS_ORDER.index(row["status"])
    if row["status"] in ("candidate", "near"):
        return rank, -row["score"], row["inst_id"]
    if row["status"] == "rejected":
        imp = row["metrics"]["impulse_pct"]
        return rank, -(imp if imp is not None else -math.inf), row["inst_id"]
    return rank, 0.0, row["inst_id"]


def finalize(rows: list[dict], expected_ts: Optional[int] = None) -> tuple[Optional[int], list[str]]:
    """Отметка «свеча устарела» и сортировка. Возвращает (свеча скана, предупреждения)."""
    ref_ts = max((r["candle_ts"] for r in rows if r["candle_ts"] is not None), default=None)
    for r in rows:
        if r["status"] != "insufficient" and r["candle_ts"] < ref_ts:
            r["status"] = "stale"
            r["reasons"] = [f"свеча устарела: последняя закрытая {iso_utc(r['candle_ts'])}"]
    rows.sort(key=_order_key)
    warnings = []
    if not rows:
        warnings.append("универсум пуст: нет пар для скана")
    elif ref_ts is None:
        warnings.append("ни у одной пары нет закрытых свечей")
    elif expected_ts is not None and ref_ts < expected_ts:
        warnings.append(f"последняя закрытая свеча {iso_utc(ref_ts)} старее ожидаемой "
                        f"{iso_utc(expected_ts)}: биржа ещё не подтвердила свечу или лента стоит")
    return ref_ts, warnings


# --- Скан ---

def run_scan(client, *, pairs: Optional[Sequence[str]] = None, top_n: int = DEFAULT_TOP_N,
             exclude: Iterable[str] = (), at_ms: Optional[int] = None,
             n_bars: int = DEFAULT_BARS, thresholds: Thresholds = Thresholds(),
             feed: str = "demo", endpoint: Optional[str] = None,
             now_ms: Optional[int] = None) -> dict:
    """Полный скан (только публичные GET через client.get). Ошибки данных не глушатся."""
    now_ms = int(time.time() * 1000) if now_ms is None else now_ms
    endpoint = endpoint or ("history" if at_ms is not None else "candles")
    if pairs:
        scan_pairs = list(dict.fromkeys(pairs))
        universe = {"source": "pairs", "eligible": len(scan_pairs)}
    else:
        tickers = client.get(TICKERS_PATH, {"instType": "SPOT"})
        if not tickers:
            raise ScanError("пустой ответ market/tickers")
        scan_pairs, universe = select_universe(tickers, top_n, exclude)
    rows = [analyze_pair(inst, fetch_closed_candles(client, inst, n_bars, at_ms=at_ms,
                                                    endpoint=endpoint), thresholds)
            for inst in scan_pairs]
    ref_ms = at_ms if at_ms is not None else now_ms
    expected_ts = ref_ms // BAR_MS * BAR_MS - BAR_MS
    candle_ts, warnings = finalize(rows, expected_ts)
    by_status = {s: [r["inst_id"] for r in rows if r["status"] == s] for s in STATUS_ORDER}
    report = {
        "scanner": SOURCE,
        "v": FORMAT_VERSION,
        "ts": iso_local(now_ms),
        "mode": "replay" if at_ms is not None else "live",
        "at": iso_local(at_ms),
        "feed": feed,
        "endpoint": ENDPOINTS[endpoint][0],
        "bar": BAR,
        "bars": n_bars,
        "candle_ts": iso_utc(candle_ts),
        "thresholds": asdict(thresholds),
        "universe": universe,
        "pairs": scan_pairs,
        "counts": {"scanned": len(rows), "candidates": len(by_status["candidate"]),
                   "near": len(by_status["near"]), "insufficient": len(by_status["insufficient"]),
                   "stale": len(by_status["stale"])},
        "candidates": by_status["candidate"],
        "near": by_status["near"],
        "insufficient": by_status["insufficient"],
        "stale": by_status["stale"],
        "warnings": warnings,
        "requests": getattr(client, "requests", None),
        "results": [public_row(r) for r in rows],
    }
    report["note"] = scan_note(report)
    return report


def _r(x: Optional[float], nd: int) -> Optional[float]:
    return None if x is None else round(x, nd)


def _sig(x: Optional[float], digits: int = 8) -> Optional[float]:
    return None if x is None else float(f"{x:.{digits}g}")


def public_row(row: dict) -> dict:
    """Строка для JSON и журнала: округлённые метрики, без внутренних полей."""
    m = row.get("metrics") or {}
    return {
        "inst_id": row["inst_id"],
        "status": row["status"],
        "bars": row["bars"],
        "candle_ts": iso_utc(row["candle_ts"]),
        "close": m.get("close"),
        "vol_quote": _sig(m.get("vol_quote")),
        "impulse_pct": _r(m.get("impulse_pct"), 4),
        "vol_ratio_median": _r(m.get("vol_ratio_median"), 4),
        "vol_ratio_mean": _r(m.get("vol_ratio_mean"), 4),
        "rsi": _r(m.get("rsi"), 2),
        "ma20": _sig(m.get("ma20")),
        "macd_hist": _sig(m.get("macd_hist"), 6),
        "chg24_pct": _r(m.get("chg24_pct"), 4),
        "score": _r(row.get("score"), 4),
        "checks": row["checks"],
        "failed": row["failed"],
        "reasons": row["reasons"],
    }


def scan_note(report: dict) -> str:
    """Одна строка обоснования итога скана."""
    res = {r["inst_id"]: r for r in report["results"]}
    n = report["counts"]["scanned"]
    if report["candidates"]:
        top = [res[i] for i in report["candidates"][:3]]
        text = f"Кандидатов {len(report['candidates'])} из {n}: " + ", ".join(
            f"{r['inst_id']} (score {r['score']:.2f}, {r['impulse_pct']:+.2f}%, "
            f"объём {r['vol_ratio_median']:.1f}×, RSI {r['rsi']:.1f})" for r in top)
    else:
        text = f"Кандидатов 0 из {n}"
        if report["near"]:
            text += "; почти: " + ", ".join(
                f"{i} — {res[i]['reasons'][0]}" for i in report["near"][:3])
    if report["insufficient"]:
        text += f"; данных мало: {', '.join(report['insufficient'])}"
    if report["stale"]:
        text += f"; свеча устарела: {', '.join(report['stale'])}"
    return text


# --- Журнал ---

JOURNAL_ROW_KEYS = ("inst_id", "close", "vol_quote", "impulse_pct", "vol_ratio_median",
                    "vol_ratio_mean", "rsi", "ma20", "macd_hist", "chg24_pct", "score")


def journal_record(report: dict) -> dict:
    """Строка event="scan" для data/pump_journal.jsonl (поля — в docstring модуля)."""
    res = {r["inst_id"]: r for r in report["results"]}

    def brief(inst_id: str) -> dict:
        return {k: res[inst_id][k] for k in JOURNAL_ROW_KEYS}

    return {
        "ts": report["ts"],
        "event": "scan",
        "source": SOURCE,
        "v": FORMAT_VERSION,
        "feed": report["feed"],
        "bar": report["bar"],
        "candle_ts": report["candle_ts"],
        "bars": report["bars"],
        "thresholds": report["thresholds"],
        "universe": report["universe"],
        "pairs": report["pairs"],
        "counts": report["counts"],
        "candidates": [brief(i) for i in report["candidates"]],
        "near": [dict(brief(i), failed=res[i]["failed"]) for i in report["near"]],
        "warnings": report["warnings"],
        "note": report["note"],
    }


def append_journal(path: Path, record: dict) -> None:
    """Дописывает одну строку JSON (UTF-8, LF). Существующие строки не трогает."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    line = json.dumps(record, ensure_ascii=False, separators=(",", ":"))
    with open(path, "a", encoding="utf-8", newline="\n") as fh:
        fh.write(line + "\n")


# --- Текстовый отчёт ---

def _compact(x: Optional[float]) -> str:
    if x is None:
        return "—"
    for div, suffix in ((1e9, "B"), (1e6, "M"), (1e3, "k")):
        if abs(x) >= div:
            return f"{x / div:.1f}{suffix}"
    return f"{x:.0f}"


def _yes(flag: Optional[bool]) -> str:
    return "—" if flag is None else ("да" if flag else "нет")


def render_text(report: dict) -> str:
    th = report["thresholds"]
    u = report["universe"]
    head = "повтор на " + report["at"] if report["mode"] == "replay" else "живой скан"
    candle = "—"
    if report["candle_ts"]:
        start = datetime.strptime(report["candle_ts"], "%Y-%m-%dT%H:%M:%SZ").replace(
            tzinfo=timezone.utc)
        end = start + timedelta(milliseconds=BAR_MS)
        loc = start.astimezone(PROJECT_TZ), end.astimezone(PROJECT_TZ)
        candle = (f"{start:%Y-%m-%d %H:%M}–{end:%H:%M} UTC "
                  f"({loc[0]:%H:%M}–{loc[1]:%H:%M} +05:00)")
    if u["source"] == "tickers":
        uni = (f"tickers SPOT {u['tickers']} → USDT-пар {u['usdt_pairs']} → без стейблов "
               f"{u['non_stable']} → без {', '.join(u['exclude'])}: {u['eligible']} → "
               f"топ-{u['top_n']} по volCcy24h")
    else:
        uni = f"--pairs ({u['eligible']})"
    lines = [
        f"Памп-скан без LLM ({SOURCE}): лента {report['feed']}, {report['bar']}, {head}",
        f"Время скана {report['ts']}; свеча {candle}; закрытых свечей в расчёте {report['bars']}; "
        f"{report['endpoint']}",
        f"Универсум: {uni}",
        f"Фильтр: импульс ≥ {th['impulse_min']:g}% · объём ≥ {th['vol_ratio_min']:g}× медианы "
        f"20 свечей · RSI(14) {th['rsi_min']:g}–{th['rsi_max']:g} · close > MA20 · "
        f"MACD-гист.(12/26/9) > 0",
        "",
        f"{'Пара':<14}{'Имп.%':>7}{'vol×мед':>9}{'vol×ср':>8}{'RSI':>6}{'>MA20':>6}"
        f"{'MACD>0':>7}{'24ч%':>8}{'Оборот':>8}{'score':>7}  Итог",
    ]
    for r in report["results"]:
        c = r["checks"]
        if r["status"] == "candidate":
            verdict = "КАНДИДАТ"
        elif r["status"] == "near":
            verdict = "почти: " + r["reasons"][0]
        elif r["status"] == "rejected":
            verdict = "нет: " + "; ".join(r["reasons"])
        else:
            verdict = r["reasons"][0]
        lines.append(
            f"{r['inst_id']:<14}{_fmt(r['impulse_pct'], '+.2f'):>7}"
            f"{_fmt(r['vol_ratio_median'], '.2f'):>9}{_fmt(r['vol_ratio_mean'], '.2f'):>8}"
            f"{_fmt(r['rsi'], '.1f'):>6}{_yes(c.get('ma20')):>6}{_yes(c.get('macd')):>7}"
            f"{_fmt(r['chg24_pct'], '+.2f'):>8}{_compact(r['vol_quote']):>8}"
            f"{_fmt(r['score'], '.2f'):>7}  {verdict}")
    counts = report["counts"]
    lines += [
        "",
        f"Кандидатов: {counts['candidates']} из {counts['scanned']}"
        + (" — " + ", ".join(report["candidates"]) if report["candidates"] else "."),
        "Почти кандидаты (не хватило одного условия): "
        + (", ".join(report["near"]) if report["near"] else "нет"),
    ]
    if report["insufficient"]:
        lines.append(f"Данных мало (< {MIN_BARS} закрытых свечей): {', '.join(report['insufficient'])}")
    if report["stale"]:
        lines.append(f"Свеча устарела: {', '.join(report['stale'])}")
    for w in report["warnings"]:
        lines.append(f"ВНИМАНИЕ: {w}")
    journal = report.get("journal")
    lines.append("Журнал: " + (f"+1 строка scan → {journal}" if journal else
                               "не пишется (повтор или --no-journal)"))
    lines.append("Кандидат — не сделка: вход, размер, стоп и ликвидность решает Pump Risk Taker "
                 "по pump-pocket.json.")
    return "\n".join(lines)


# --- CLI ---

def _positive(text: str) -> float:
    value = float(text)
    if not value > 0:
        raise argparse.ArgumentTypeError("нужно число > 0")
    return value


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="python -m src.pump_scanner",
        description="Детерминированный памп-сканер (PUMP-CODIFY): публичные данные OKX, "
                    "закрытые 1H-свечи, фильтр скана №5. Ордеров не ставит.",
        epilog="Примеры:\n"
               "  python -m src.pump_scanner --exclude ETH SOL SUI ADA TRX ETC APT BNB XLM DOT\n"
               "  python -m src.pump_scanner --at 2026-09-24T09:47+05:00 --pairs OKB-USDT LTC-USDT\n"
               "Коды выхода: 0 — скан выполнен; 2 — ошибка данных или сети.",
        formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--feed", choices=("demo", "live"), default="demo",
                   help="лента рыночных данных: demo (по умолчанию, как okx --demo в сканах №1–5) "
                        "или live")
    p.add_argument("--top", type=int, default=DEFAULT_TOP_N,
                   help=f"топ-N USDT-пар по volCcy24h (по умолчанию {DEFAULT_TOP_N})")
    p.add_argument("--exclude", nargs="*", default=[], metavar="BASE",
                   help="исключить базы (пары флота): ETH SOL … или ETH-USDT; только для "
                        "отбора из tickers")
    p.add_argument("--pairs", nargs="+", default=None, metavar="PAIR",
                   help="сканировать ровно эти пары (без tickers и исключений)")
    p.add_argument("--at", type=parse_at, default=None, metavar="ISO",
                   help="повтор: свечи, закрытые к моменту (ISO с поясом, напр. "
                        "2026-09-24T09:47+05:00); требует --pairs; в журнал не пишет")
    p.add_argument("--bars", type=int, default=DEFAULT_BARS,
                   help=f"закрытых свечей в расчёте ({MACD_WARMUP}–{MAX_BARS}, по умолчанию "
                        f"{DEFAULT_BARS})")
    p.add_argument("--impulse-min", type=_positive, default=Thresholds.impulse_min,
                   help="минимальный импульс свечи, %% (по умолчанию 1.5)")
    p.add_argument("--vol-min", type=_positive, default=Thresholds.vol_ratio_min,
                   help="минимальный объём к медиане 20 свечей (по умолчанию 1.5)")
    p.add_argument("--rsi-min", type=float, default=Thresholds.rsi_min,
                   help="нижняя граница RSI(14) (по умолчанию 50)")
    p.add_argument("--rsi-max", type=float, default=Thresholds.rsi_max,
                   help="верхняя граница RSI(14) (по умолчанию 72)")
    p.add_argument("--endpoint", choices=tuple(ENDPOINTS), default=None,
                   help="эндпоинт свечей: по умолчанию candles для живого скана и history для "
                        "--at; для диагностики расхождений повтора")
    p.add_argument("--json", action="store_true", help="отчёт JSON")
    p.add_argument("--no-journal", action="store_true", help="не писать строку scan в журнал")
    p.add_argument("--journal", type=Path, default=JOURNAL_PATH,
                   help=f"путь журнала (по умолчанию {JOURNAL_PATH.as_posix()})")
    return p


def main(argv: Optional[list[str]] = None, client=None,
         now_ms: Optional[int] = None) -> int:
    logging.basicConfig(level=logging.WARNING,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    parser = build_parser()
    args = parser.parse_args(argv)
    now_ms = int(time.time() * 1000) if now_ms is None else now_ms
    try:
        pairs = normalize_pairs(args.pairs) if args.pairs else None
    except argparse.ArgumentTypeError as exc:
        parser.error(f"--pairs: {exc}")
    if args.at is not None and not pairs:
        parser.error("--at требует --pairs: тикеров на прошлый момент у OKX нет")
    if args.at is not None and args.at > now_ms:
        parser.error("--at в будущем")
    if not MACD_WARMUP <= args.bars <= MAX_BARS:
        parser.error(f"--bars: от {MACD_WARMUP} до {MAX_BARS}")
    if args.top < 1:
        parser.error("--top: нужно >= 1")
    if not 0 <= args.rsi_min <= args.rsi_max <= 100:
        parser.error("RSI: нужно 0 <= --rsi-min <= --rsi-max <= 100")
    thresholds = Thresholds(args.impulse_min, args.vol_min, args.rsi_min, args.rsi_max)
    client = client if client is not None else make_client(args.feed)
    try:
        report = run_scan(client, pairs=pairs, top_n=args.top, exclude=args.exclude,
                          at_ms=args.at, n_bars=args.bars, thresholds=thresholds,
                          feed=args.feed, endpoint=args.endpoint, now_ms=now_ms)
    except SCAN_ERRORS as exc:
        print(f"Скан не выполнен (ошибка данных или сети): {type(exc).__name__}: "
              f"{str(exc)[:300]}", file=sys.stderr)
        return 2
    write_journal = report["mode"] == "live" and not args.no_journal
    report["journal"] = args.journal.as_posix() if write_journal else None
    code = 0
    if write_journal:
        try:
            append_journal(args.journal, journal_record(report))
        except OSError as exc:
            report["journal"] = None
            print(f"Журнал не записан ({args.journal}): {exc}", file=sys.stderr)
            code = 2
    print(json.dumps(report, ensure_ascii=False, indent=2) if args.json else render_text(report))
    return code


if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8")
    sys.exit(main())
