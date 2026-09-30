"""Детерминированный памп-сканер без LLM (задача PUMP-CODIFY). Только чтение публичных данных.

    python -m src.pump_scanner --exclude ETH SOL SUI ADA TRX ETC APT BNB XLM DOT
    python -m src.pump_scanner --json                    # машинный отчёт (для агента и PUMP-BT)
    python -m src.pump_scanner --no-journal              # без строки в data/pump_journal.jsonl
    python -m src.pump_scanner --feed demo --at 2026-09-24T09:47+05:00 --pairs OKB-USDT LTC-USDT
                                                         # повтор прошлого скана (№1–6 — demo)
    python -m src.pump_scanner --no-liquidity …          # без проверки ликвидности

Методика — скилл spot-momentum-scan-validate, фаза 1 «только скан», в редакции скана №5
Pump Risk Taker (insights/pump-scan-2026-09-24.md, 09:47+05), и проверка ликвидности кандидата
(PUMP-LIQ, раздел «Ликвидность»). Кандидат сканера — не сделка: вход, сайзинг и стоп решает
Pump Risk Taker по pump-pocket.json.

Данные — только публичные GET, без ключей (других запросов opener не пропускает):
- GET /api/v5/market/tickers?instType=SPOT — универсум;
- GET /api/v5/public/instruments?instType=SPOT — пары demo (всегда с demo-заголовком);
- GET /api/v5/market/candles — свечи живого скана; /market/history-candles — повтор (--at);
- GET /api/v5/market/ticker?instId=… — текущая цена last кандидата (свежесть, PUMP-STALE);
- GET /api/v5/market/books?instId=…&sz=400 — стакан кандидатов и почти кандидатов; в ленте
  live — две книги: live (без заголовка) и demo (с заголовком, PUMP-DEMO-GUARD).

Лента (--feed, PUMP-FEED-LIVE; обоснование — insights/pump-feed.md):
  live  по умолчанию, без заголовка: все сигналы — универсум по volCcy24h, свечи, стакан;
  demo  заголовок x-simulated-trading: 1, как `okx --demo` (с CLI 1.3.0 `--demo` в
        market-командах = demo-лента) — только диагностика, проверки исполнения и повтор
        сканов №1–6, которые шли по demo-ленте.
Demo-лента — отдельный симулированный рынок: свой объём, сделки, стакан и список пар. За сутки
14.8% demo-свечей плоские (без сделок), бывают выбросы объёма ×1000 (insights/okx-api.md §10
п. 8), 8 из 45 live-пар в demo нет. Поэтому сигналы — по live, объём — к медиане.

Универсум: SPOT-пары BASE-USDT без стейблкоинов (STABLECOINS), без BTC-USDT и без баз из
--exclude. В ленте live — только пары, которые есть в demo (public/instruments с demo-заголовком,
state=live): ордер исполняется в demo-книге. Из оставшихся — топ-N (--top, по умолчанию 45) по
volCcy24h ленты, поэтому пар в скане N, если их хватает. С --pairs универсум — ровно этот список,
без tickers, исключений и сверки с demo.

Свечи 1H: в расчёт идут только подтверждённые (confirm == "1"), последние --bars штук
(по умолчанию 99 = `market candles --limit 100` сканов №4–5 минус формирующаяся). Это
сознательное отличие от calc_indicators.py скилла: тот считает по формирующейся свече и по
vol в базовой валюте. Меньше 30 закрытых свечей — «данных мало». Последняя закрытая свеча
старее, чем у остальных пар, — «свеча устарела» (пара стоит или скан перешёл через час).
Пары нет в ленте (OKX 51001 «Instrument ID does not exist» на запрос свечей) — статус no_data
с причиной, скан продолжается; другие ошибки OKX — ошибка скана (код 2).

Метрики последней закрытой свечи:
- импульс % = (close − open) / open × 100;
- vol× = volCcyQuote (индекс 7 строки свечи) / медиана и / среднее volCcyQuote 20
  предыдущих закрытых свечей; фильтр — по медиане, среднее — для сравнения;
- RSI(14) Уайлдера, MA20 (SMA close), MACD-гистограмма 12/26/9 — src/backtest/indicators.py,
  по всем --bars закрытым свечам; 24ч % — от open 24-й с конца свечи до close последней;
- score = min(vol× / vol_min × 3, 5) + min(импульс / impulse_min × 3, 5) — формула скилла,
  vol× — по медиане.

Кандидат — все пять условий (пороги — параметры CLI): импульс ≥ 1.5 %, vol× (медиана) ≥ 1.5,
50 ≤ RSI ≤ 72, close > MA20, MACD-гистограмма > 0, затем свежесть сигнала, ликвидность и
demo-цена (ниже). Кандидаты — по убыванию score; stale — сигнал устарел; illiquid — пять условий
выполнены, ликвидность — нет; demo_price — demo-книга мёртвая или отстаёт от live; «почти
кандидаты» — не выполнено ровно одно условие; у каждой пары — список невыполненных условий.

Ликвидность (PUMP-LIQ) — у кандидатов и «почти кандидатов», против лимита позиции кармана
P = max_position_pct из pump-pocket.json (только чтение; имя поля историческое, значение — сумма
в USDT, 10% бюджета). Три условия, границы включительно, пороги — параметры CLI:
  depth     глубина ask ≥ --depth-min × P: Σ px × sz уровней ask от лучшего ask до +--liq-band %
            (объём, который съест покупка); стакан — market/books, 400 уровней, лента --feed;
  spread    спред (ask − bid) / mid ≤ --spread-max %;
  turnover  оборот последней закрытой свечи (volCcyQuote) ≥ --turnover-min × P.
Кандидат, не прошедший хоть одно условие, получает статус illiquid с причинами и в кандидаты не
попадает; «почти кандидат» остаётся near, итог проверки — в его поле liquidity.

Demo-цена (PUMP-DEMO-GUARD, insights/pump-feed.md §4 п. 2) — в ленте live на живом скане:
сигнал считается по live, а ордер кармана исполняется в demo-книге. У кандидатов и почти
кандидатов берётся и demo-книга (market/books с заголовком x-simulated-trading: 1):
  demo_price   demo-стакан двусторонний и |demo-mid − live-mid| / live-mid ≤ --spread-max %
               (тот же порог; 0.5% — по суткам замеров, подстроить по журналу первой недели);
               мёртвые demo-пары отстают на 1.5–74% (ZEC 74%) или без заявок — вход дал бы
               фиктивный PnL; пары нет в demo (OKX 51001) — тоже не проходит;
  demo_depth   depth по demo-книге, как у live;
  demo_spread  spread по demo-книге, как у live.
Оборот — только по live-свече: demo-оборот артефактный. Кандидат, не прошедший demo_price,
получает статус demo_price (причина «demo-цена: …» — первой), не прошедший остальное — illiquid.
Near остаётся near. Лента demo (книга сканера и есть demo-книга) и повтор --at — без сверки
(liquidity.demo_guard = false). Другая ошибка OKX на demo-книге — ошибка скана (код 2).

Свежесть сигнала (PUMP-STALE) — у кандидатов живого скана до проверки ликвидности: если от
закрытия сигнальной свечи прошло больше --max-age-min минут (по умолчанию 15) или текущая цена
last (market/ticker ленты --feed) ниже close сигнальной свечи, кандидат получает статус stale с
причинами (сигнал устарел / цена ниже close) и стакан по нему не запрашивается. Случай скана
30.09 02:54: ICP-USDT — кандидат через 54 мин после закрытия свечи при цене −0.95% от close.
Повтор --at свежесть не проверяет: текущей цены на прошлый момент нет (freshness.enabled=false).
Порог 15 мин подобрать по журналу. Пара с котировкой
не USDT — illiquid: лимит кармана задан в USDT. Повтор --at: исторического стакана у OKX нет —
depth и spread не проверяются (пометка в notes), turnover проверяется; лимит P — из текущего
pump-pocket.json. Файла кармана нет или он не читается — проверка пропускается с предупреждением
в warnings, скан не падает. --no-liquidity — выключить проверку (повтор прошлых сканов как они
были). Ошибка запроса стакана — ошибка скана (код 2), как у свечей.

Пороги по умолчанию воспроизводят решение скана №4 (08:42+05). IMX-USDT прошёл фильтр (+2.51%,
3.98× медианы, RSI 57.8, > MA20, MACD > 0), и агент отклонил его по ликвидности: оборот свечи
1 569 USDT = 1.51 P; в демо-стакане ≈ 2.2k USDT по ask до +3.2% = 2.12 P, на лучшем уровне
≈ 0.9 USDT, «позиция съела бы половину книги»; P = 1 036.29 USDT.
  --liq-band 1      покупка на P исполняется не дальше +1% от лучшего ask — меньше минимального
                    стопа кармана (stop_loss_range, 1.5%);
  --depth-min 3     позиция — не больше трети глубины полосы: средняя цена входа — в первой трети
                    полосы, запас на изменение книги до входа. IMX: ≤ 2.2k USDT в любой полосе
                    до +3.2% < 3 P = 3.1k → illiquid (при 2 P = 2.07k он прошёл бы по полосе 3.2%);
  --turnover-min 5  позиция — не больше 20% оборота часа сигнала. IMX: 1.51 P < 5 P → illiquid и
                    в повторе без стакана; COMP-USDT того же скана (≈ 23.5k USDT/ч = 22.7 P,
                    отклонён по объёму, не по ликвидности) проходит с запасом;
  --spread-max 0.5  спред платится за круг (вход по ask, выход по bid): 0.5% — треть минимального
                    стопа 1.5%, вместе с комиссией taker 2 × 0.1% — меньше половины стопа.

Повтор (--at ВРЕМЯ --pairs …): свечи из history-candles, закрытые к моменту --at
(open + 1 ч ≤ --at; запрос after = --at − 1 ч + 1 мс). Время — ISO с поясом. Тикеров на
прошлый момент у OKX нет, поэтому --pairs обязателен. Повтор в журнал не пишет: это проверка,
а не событие кармана. --endpoint candles — диагностика расхождений повтора.

Журнал data/pump_journal.jsonl (--journal): строка на событие, UTF-8, JSON в одну строку,
только дописывание. Сканер пишет event="scan" после живого скана; вход, стоп, выход и ошибки
пишет Pump Risk Taker. Поля этих строк (entry, stop, exit, error; формат v=1), примеры и расчёт
остатков кармана по журналу — docstring src/pump_journal.py (`python -m src.pump_journal`).
Поля строки scan (формат v=1):
  ts          время скана, ISO с поясом проекта +05:00
  event       "scan"
  source      "src.pump_scanner" — детерминированный скан, не LLM
  v           версия формата строки (1)
  feed        "live" | "demo"; "demo" — строки до PUMP-FEED-LIVE и сканы с --feed demo: сигналы
              demo-ленты, для оценки правил не использовать (pump-feed.md §4 п. 4)
  bar         "1H"
  candle_ts   open time проверенной закрытой свечи, ISO UTC ("2026-09-24T03:00:00Z")
  bars        число закрытых свечей в расчёте
  thresholds  {impulse_min, vol_ratio_min, rsi_min, rsi_max}
  universe    {source: "tickers"|"pairs", tickers, usdt_pairs, non_stable, eligible, top_n,
               exclude} — числа отбора (для pairs — только source и eligible); в ленте live
              ещё demo_instruments (пар demo в state=live), not_in_demo (подходящих пар ленты
              нет в demo) и not_in_demo_top (какие из них вошли бы в топ-N без сверки);
              eligible — после сверки с demo
  pairs       проверенные пары: с ts, bars и feed дают команду повтора (--at ts --pairs …)
  counts      {scanned, candidates, near, insufficient, stale, no_data}; illiquid и rejected
              сюда не входят — число illiquid = длина списка illiquid; no_data добавлено в
              PUMP-FEED-LIVE без смены v (аддитивно)
  liquidity   настройки проверки ликвидности: {enabled, pocket, position_usdt, band_pct,
              depth_min, turnover_min, spread_max, book, book_levels, demo_guard, note};
              enabled=false — не проверялась (note — почему), book=false — без стакана
              (повтор), demo_guard — сверялась ли demo-книга (PUMP-DEMO-GUARD)
  freshness   {enabled, max_age_min, price, note} — проверка свежести (PUMP-STALE)
  candidates  [{inst_id, close, vol_quote, impulse_pct, vol_ratio_median, vol_ratio_mean,
                rsi, ma20, macd_hist, chg24_pct, score, liquidity, freshness}] — по убыванию
              score
  illiquid    то же — прошли фильтр, не прошли ликвидность (причины — liquidity.reasons)
  demo_price  то же — прошли фильтр, не прошли demo-цену (PUMP-DEMO-GUARD)
  near        то же + failed: [условие] — не хватило одного условия
  stale       то же + reasons — устаревшие: сигнал (freshness не null) или свеча (null)
  warnings    предупреждения скана (лента отстаёт, пустой универсум, нет pump-pocket.json)
  note        одна строка обоснования итога
Условия (ключи checks/failed): impulse, volume, rsi, ma20, macd.
Поле liquidity пары (null — не проверялась): {ok, checks {depth, spread, turnover}, failed,
reasons, notes, turnover_usdt, turnover_x, depth_usdt, depth_x, spread_pct, best_bid, best_ask,
ask_levels, band_covered, book_ts, live_mid, demo_bid, demo_ask, demo_mid, demo_dev_pct,
demo_depth_usdt, demo_depth_x, demo_spread_pct, demo_ask_levels, demo_band_covered,
demo_book_ts}; checks и failed — из depth, spread, turnover, demo_price, demo_depth,
demo_spread; *_x — в долях позиции P; null в checks и числах — не проверялось (повтор без
стакана, demo_* — без сверки с demo). band_covered=false — 400 уровней не покрыли полосу,
глубина — оценка снизу. Поле freshness пары (null — не проверялась): {ok, age_min, last,
vs_close_pct, reasons}. Поля liquidity и illiquid добавлены в PUMP-LIQ, demo_* и demo_price —
в PUMP-DEMO-GUARD, freshness и stale — в PUMP-STALE, всё без смены v: изменения аддитивные, а
candidates, как и прежде, — кандидаты на вход, теперь с проверенными ликвидностью, demo-ценой
и свежестью. Строка scan без поля liquidity записана до PUMP-LIQ, без liquidity.demo_guard — до
PUMP-DEMO-GUARD, без freshness — до PUMP-STALE: эти проверки в ней не делались.

Коды выхода: 0 — скан выполнен (с кандидатами или без; пары без данных в ленте — статус
no_data); 2 — ошибка данных или сети (скан не
состоялся, в журнал не пишется), а также ошибка записи журнала и неверные аргументы.
"""
import argparse
import http.client
import json
import logging
import math
import re
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
TICKER_PATH = "/api/v5/market/ticker"             # PUMP-STALE: текущая цена кандидата
INSTRUMENTS_PATH = "/api/v5/public/instruments"   # только demo: какие пары исполнимы в demo
BOOKS_PATH = "/api/v5/market/books"
BOOK_LEVELS = 400            # уровней на сторону в market/books (максимум OKX)
ENDPOINTS = {"candles": ("/api/v5/market/candles", 300),           # путь, макс. limit
             "history": ("/api/v5/market/history-candles", 100)}
PUBLIC_PATHS = frozenset({TICKERS_PATH, TICKER_PATH, INSTRUMENTS_PATH, BOOKS_PATH}
                         | {p for p, _ in ENDPOINTS.values()})
FEEDS = ("live", "demo")
DEFAULT_FEED = "live"        # PUMP-FEED-LIVE: сигналы — по live-ленте (insights/pump-feed.md)
NO_DATA_CODES = frozenset({"51001"})   # Instrument ID does not exist: пары нет в ленте
USER_AGENT = "okx-pump-scanner/1.0"

POCKET_PATH = Path("pump-pocket.json")   # лимиты кармана: только чтение, меняет только человек
POCKET_FIELD = "max_position_pct"        # имя историческое: значение — лимит позиции в USDT

ALWAYS_EXCLUDED = frozenset({"BTC"})     # BTC-USDT: движок P1-72H и grid (сканы №4–5)
STABLECOINS = frozenset({
    "USDT", "USDC", "DAI", "TUSD", "USDP", "PAX", "BUSD", "GUSD", "FDUSD", "PYUSD", "USDD",
    "USDE", "USDG", "USDS", "RLUSD", "USD1", "LUSD", "FRAX", "SUSD", "USDJ", "USDK", "AUSD",
    "EURC", "EUROC", "EURT", "EURI", "EURS",
})

CONDITIONS = ("impulse", "volume", "rsi", "ma20", "macd")
LIQ_CONDITIONS = ("depth", "spread", "turnover")
# PUMP-DEMO-GUARD: сверка с demo-книгой, где исполняется ордер кармана (лента live)
DEMO_CONDITIONS = ("demo_price", "demo_depth", "demo_spread")
CHECK_ORDER = ("demo_price",) + LIQ_CONDITIONS + DEMO_CONDITIONS[1:]   # порядок failed/reasons
LIQ_FIELDS = ("turnover_usdt", "turnover_x", "depth_usdt", "depth_x", "spread_pct", "best_bid",
              "best_ask", "ask_levels", "band_covered", "book_ts",
              "live_mid", "demo_bid", "demo_ask", "demo_mid", "demo_dev_pct", "demo_depth_usdt",
              "demo_depth_x", "demo_spread_pct", "demo_ask_levels", "demo_band_covered",
              "demo_book_ts")
DEFAULT_MAX_AGE_MIN = 15.0   # PUMP-STALE: сигнал старше — stale (скан 30.09 02:54, ICP через 54 мин)
STATUS_ORDER = ("candidate", "illiquid", "demo_price", "near", "rejected", "stale",
                "insufficient", "no_data")
SCORED = ("candidate", "illiquid", "demo_price", "near")   # сортировка по score


class ScanError(RuntimeError):
    """Скан не может быть выполнен: неверные данные биржи или запрещённый запрос."""


class PocketError(ValueError):
    """pump-pocket.json нет или он не читается: проверка ликвидности пропускается."""


# Ошибки, при которых скан не состоялся (код выхода 2). HTTPError/URLError/TimeoutError — OSError.
SCAN_ERRORS = (ScanError, OkxApiError, OSError, http.client.HTTPException, json.JSONDecodeError)


@dataclass(frozen=True)
class Thresholds:
    impulse_min: float = 1.5     # импульс свечи, %
    vol_ratio_min: float = 1.5   # объём к медиане 20 предыдущих свечей
    rsi_min: float = 50.0
    rsi_max: float = 72.0


@dataclass(frozen=True)
class LiquidityRules:
    """Пороги ликвидности против позиции кармана P (обоснование — в docstring модуля, скан №4)."""
    band_pct: float = 1.0        # полоса от лучшего ask, %: глубина, которую съест покупка
    depth_min: float = 3.0       # глубина ask в полосе ≥ depth_min × P
    turnover_min: float = 5.0    # оборот последней закрытой свечи ≥ turnover_min × P
    spread_max: float = 0.5      # спред ≤ spread_max, % от mid


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
                            f"публичные эндпоинты market/* и public/instruments")
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


def fetch_tradable(demo_client) -> set[str]:
    """Пары SPOT, которые есть в demo и торгуются (state=live): public/instruments demo-ленты."""
    data = demo_client.get(INSTRUMENTS_PATH, {"instType": "SPOT"})
    if not data:
        raise ScanError("пустой ответ demo public/instruments")
    live = {str(x.get("instId", "")).upper() for x in data
            if isinstance(x, dict) and x.get("state") == "live"}
    if not live:
        raise ScanError("в demo public/instruments нет пар SPOT в state=live")
    return live


def select_universe(tickers: Sequence[dict], top_n: int = DEFAULT_TOP_N,
                    exclude: Iterable[str] = (),
                    tradable: Optional[set[str]] = None) -> tuple[list[str], dict]:
    """USDT-пары без стейблов, BTC и --exclude; топ-N по volCcy24h (при равенстве — по имени).
    tradable — пары demo (лента live): сначала сверка с demo, затем топ-N из оставшихся."""
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
            "non_stable": non_stable, "top_n": top_n, "exclude": sorted(excl | ALWAYS_EXCLUDED)}
    if tradable is not None:
        missing = [inst for _, inst in eligible if inst not in tradable]
        info.update(demo_instruments=len(tradable), not_in_demo=len(missing),
                    not_in_demo_top=[i for _, i in eligible[:top_n] if i not in tradable])
        eligible = [(v, inst) for v, inst in eligible if inst in tradable]
    info["eligible"] = len(eligible)
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


def okx_code(exc: Exception) -> Optional[str]:
    """Код OKX из OkxApiError ("…: code=51001 msg=…"); None — не ответ OKX с кодом."""
    m = re.search(r"\bcode=(\w+)", str(exc)) if isinstance(exc, OkxApiError) else None
    return m.group(1) if m else None


def no_data_row(inst_id: str, feed: str, exc: Exception) -> dict:
    """Строка пары, которой нет в ленте (OKX 51001): скан продолжается без неё."""
    return {"inst_id": inst_id, "bars": 0, "candle_ts": None, "status": "no_data",
            "metrics": None, "checks": {}, "failed": [], "score": None,
            "reasons": [f"нет в ленте {feed}: OKX {okx_code(exc)} (Instrument ID does not exist)"]}


def scan_pair(client, inst_id: str, th: Thresholds, *, n_bars: int, at_ms: Optional[int],
              endpoint: str, feed: str) -> dict:
    """Свечи пары и её строка отчёта; пары нет в ленте (51001) — статус no_data."""
    try:
        candles = fetch_closed_candles(client, inst_id, n_bars, at_ms=at_ms, endpoint=endpoint)
    except OkxApiError as exc:
        if okx_code(exc) in NO_DATA_CODES:
            return no_data_row(inst_id, feed, exc)
        raise
    return analyze_pair(inst_id, candles, th)


def _order_key(row: dict) -> tuple:
    rank = STATUS_ORDER.index(row["status"])
    if row["status"] in SCORED:
        return rank, -row["score"], row["inst_id"]
    if row["status"] == "rejected":
        imp = row["metrics"]["impulse_pct"]
        return rank, -(imp if imp is not None else -math.inf), row["inst_id"]
    return rank, 0.0, row["inst_id"]


def finalize(rows: list[dict], expected_ts: Optional[int] = None) -> tuple[Optional[int], list[str]]:
    """Отметка «свеча устарела» и сортировка. Возвращает (свеча скана, предупреждения)."""
    ref_ts = max((r["candle_ts"] for r in rows if r["candle_ts"] is not None), default=None)
    for r in rows:
        if r["status"] not in ("insufficient", "no_data") and r["candle_ts"] < ref_ts:
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


# --- Ликвидность (PUMP-LIQ) ---

def read_position_limit(path: Path) -> float:
    """Лимит позиции кармана P, USDT: поле max_position_pct из pump-pocket.json. Только чтение."""
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
    value = data.get(POCKET_FIELD) if isinstance(data, dict) else None
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not 0 < value < math.inf:
        raise PocketError(f"{name}: {POCKET_FIELD} = {value!r} — нужно число USDT > 0")
    return float(value)


def _levels(raw, inst_id: str) -> list[tuple[float, float]]:
    """Уровни стакана [px, sz, …] -> [(px, sz)]; уровни с нулевой ценой или размером — мимо."""
    out = []
    for lvl in raw or ():
        try:
            px, sz = float(lvl[0]), float(lvl[1])
        except (TypeError, ValueError, IndexError, KeyError):
            raise ScanError(f"{inst_id}: уровень стакана не [px, sz, …]: {str(lvl)[:80]}") from None
        if 0 < px < math.inf and 0 < sz < math.inf:
            out.append((px, sz))
    return out


def parse_book(data: Sequence, inst_id: str, levels: int = BOOK_LEVELS) -> dict:
    """Ответ market/books -> {asks по возрастанию цены, bids по убыванию, ts, levels}.
    Для SPOT sz — в базовой валюте, px × sz — в котируемой. Пустой ответ — пустой стакан."""
    snap = data[0] if data else {}
    if not isinstance(snap, dict):
        raise ScanError(f"{inst_id}: ответ market/books — не объект: {str(snap)[:120]}")
    ts = str(snap.get("ts") or "")
    return {"asks": sorted(_levels(snap.get("asks"), inst_id)),
            "bids": sorted(_levels(snap.get("bids"), inst_id), reverse=True),
            "ts": int(ts) if ts.isdigit() else None, "levels": levels}


def fetch_book(client, inst_id: str, levels: int = BOOK_LEVELS) -> dict:
    """Снимок стакана: публичный GET market/books (лента — та же, что у свечей)."""
    data = client.get(BOOKS_PATH, {"instId": inst_id, "sz": str(levels)})
    return parse_book(data, inst_id, levels)


def _at_least(value: float, bound: float) -> bool:
    """value ≥ bound с допуском на округление float (относительным: суммы в USDT)."""
    return value >= bound - EPS * max(1.0, abs(bound))


def _usdt(x: float) -> str:
    return f"{x:,.0f}".replace(",", " ") if abs(x) >= 100 else f"{x:.2f}"


def _book_checks(book: dict, position: float, rules: LiquidityRules) -> tuple[dict, dict, dict]:
    """depth и spread одной книги против позиции P: (checks, тексты причин, поля отчёта)."""
    asks, bids = book["asks"], book["bids"]
    ask = asks[0][0] if asks else None
    bid = bids[0][0] if bids else None
    need = rules.depth_min * position
    band = [(px, sz) for px, sz in asks if (px - ask) / ask * 100 <= rules.band_pct + EPS]
    depth = math.fsum(px * sz for px, sz in band)
    covered = len(band) < len(asks) or len(asks) < book["levels"]
    spread = (ask - bid) / ((ask + bid) / 2) * 100 if asks and bids else None
    checks = {"depth": _at_least(depth, need),
              "spread": spread is not None and -EPS <= spread <= rules.spread_max + EPS}
    text = {"depth": ("стакан пуст: нет асков" if not asks else
                      f"глубина ask до +{rules.band_pct:g}%: {_usdt(depth)} USDT < "
                      f"{rules.depth_min:g} × {position:g} = {_usdt(need)} USDT"
                      + ("" if covered else
                         f" (оценка снизу: {len(asks)} уровней не покрыли полосу)"))}
    if spread is None:
        text["spread"] = "спред: нет " + ("бидов" if asks else "асков" if bids else "заявок")
    elif spread < -EPS:
        text["spread"] = f"стакан пересечён: bid {bid:g} > ask {ask:g}"
    else:
        text["spread"] = f"спред {spread:.2f}% > {rules.spread_max:g}%"
    fields = dict(depth_usdt=_sig(depth), depth_x=_r(depth / position, 4),
                  spread_pct=_r(spread, 4), best_bid=bid, best_ask=ask, ask_levels=len(band),
                  band_covered=covered, book_ts=iso_local(book["ts"]))
    return checks, text, fields


def _mid(book: dict) -> Optional[float]:
    """Середина двустороннего стакана; None — нет асков или бидов."""
    if not book["asks"] or not book["bids"]:
        return None
    return (book["asks"][0][0] + book["bids"][0][0]) / 2


def check_demo_price(demo_book: dict, live_book: dict,
                     rules: LiquidityRules = LiquidityRules()) -> tuple[bool, str, dict]:
    """PUMP-DEMO-GUARD: demo-стакан двусторонний и |demo-mid − live-mid| / live-mid ≤
    --spread-max %. demo_book["missing"] — пары нет в demo (OKX 51001). Возвращает
    (прошла ли, причина «demo-цена: …», поля отчёта live_mid, demo_mid, demo_dev_pct)."""
    live_mid, demo_mid = _mid(live_book), _mid(demo_book)
    fields = dict(live_mid=_sig(live_mid), demo_mid=_sig(demo_mid), demo_dev_pct=None)
    if demo_book.get("missing"):
        return False, f"demo-цена: {demo_book['missing']}", fields
    if demo_mid is None:
        side = ("бидов" if demo_book["asks"] else "асков" if demo_book["bids"] else "заявок")
        return False, f"demo-цена: demo-стакан не двусторонний (нет {side})", fields
    if live_mid is None:
        return False, "demo-цена: live-стакан не двусторонний — demo-цену сверить не с чем", fields
    dev = abs(demo_mid - live_mid) / live_mid * 100
    fields["demo_dev_pct"] = _r(dev, 4)
    ok = dev <= rules.spread_max + EPS
    return ok, (f"demo-цена: demo-mid {demo_mid:g} отклоняется от live-mid {live_mid:g} на "
                f"{dev:.2f}% > {rules.spread_max:g}%"), fields


def check_liquidity(vol_quote: Optional[float], book: Optional[dict], position: float,
                    rules: LiquidityRules = LiquidityRules(), quote: str = QUOTE,
                    demo_book: Optional[dict] = None) -> dict:
    """Ликвидность пары против позиции кармана P (USDT). book=None — стакан не проверялся
    (повтор --at): depth и spread = None, решение — по обороту свечи.
    demo_book (PUMP-DEMO-GUARD, лента live) — demo-книга, где исполнится ордер: demo-цена
    (demo_price), depth и spread ещё и по ней (demo_depth, demo_spread); оборот — только по
    live-свече. demo_book=None — сверка не делалась, demo_* = None."""
    out = dict.fromkeys(LIQ_FIELDS)
    all_checks = LIQ_CONDITIONS + DEMO_CONDITIONS
    if quote != QUOTE:
        return dict(ok=False, checks=dict.fromkeys(all_checks), failed=[], notes=[],
                    reasons=[f"котировка {quote or '—'}: лимит позиции кармана — в {QUOTE}, "
                             f"ликвидность не проверить"], **out)
    vq = vol_quote or 0.0
    need_turnover = rules.turnover_min * position
    checks: dict[str, Optional[bool]] = dict.fromkeys(all_checks)
    checks["turnover"] = _at_least(vq, need_turnover)
    text = {"turnover": f"оборот свечи {_usdt(vq)} USDT < {rules.turnover_min:g} × "
                        f"{position:g} = {_usdt(need_turnover)} USDT"}
    out.update(turnover_usdt=_sig(vq), turnover_x=_r(vq / position, 4))
    notes = []
    if book is None:
        notes.append("стакан не проверен: повтор --at, исторического стакана у OKX нет")
    else:
        live_checks, live_text, live_fields = _book_checks(book, position, rules)
        checks.update(live_checks)
        text.update(live_text)
        out.update(live_fields)
        if demo_book is not None:
            d_checks, d_text, d_fields = _book_checks(demo_book, position, rules)
            checks["demo_depth"], checks["demo_spread"] = d_checks["depth"], d_checks["spread"]
            text["demo_depth"], text["demo_spread"] = (f"demo: {d_text['depth']}",
                                                       f"demo: {d_text['spread']}")
            out.update(demo_bid=d_fields["best_bid"], demo_ask=d_fields["best_ask"],
                       demo_depth_usdt=d_fields["depth_usdt"], demo_depth_x=d_fields["depth_x"],
                       demo_spread_pct=d_fields["spread_pct"],
                       demo_ask_levels=d_fields["ask_levels"],
                       demo_band_covered=d_fields["band_covered"],
                       demo_book_ts=d_fields["book_ts"])
            checks["demo_price"], text["demo_price"], price_fields = check_demo_price(
                demo_book, book, rules)
            out.update(price_fields)
    failed = [k for k in CHECK_ORDER if checks[k] is False]
    return dict(ok=not failed, checks=checks, failed=failed,
                reasons=[text[k] for k in failed], notes=notes, **out)


def fetch_demo_book(demo_client, inst_id: str, levels: int = BOOK_LEVELS) -> dict:
    """Demo-книга пары; пары нет в demo (51001) — пустой стакан с пометкой missing."""
    try:
        return fetch_book(demo_client, inst_id, levels)
    except OkxApiError as exc:
        if okx_code(exc) not in NO_DATA_CODES:
            raise
        return {"asks": [], "bids": [], "ts": None, "levels": levels,
                "missing": f"пары нет в demo (OKX {okx_code(exc)})"}


def apply_liquidity(client, rows: list[dict], pocket: Optional[Path],
                    rules: LiquidityRules = LiquidityRules(), replay: bool = False,
                    demo_client=None, demo_guard: bool = False) -> tuple[dict, list[str]]:
    """Проверка ликвидности кандидатов и почти кандидатов (строки после finalize). Кандидат,
    не прошедший её, -> illiquid; не прошедший demo-цену (demo_guard) -> demo_price.
    pocket=None — проверка выключена (--no-liquidity). demo_guard — сверка с demo-книгой
    (лента live, живой скан), нужен demo_client. Возвращает (блок liquidity, предупреждения)."""
    info = {"enabled": False, "pocket": None if pocket is None else Path(pocket).as_posix(),
            "position_usdt": None, **asdict(rules), "book": False, "book_levels": BOOK_LEVELS,
            "demo_guard": False, "note": None}
    if pocket is None:
        info["note"] = "выключена флагом --no-liquidity"
        return info, []
    try:
        position = read_position_limit(pocket)
    except PocketError as exc:
        info["note"] = str(exc)
        return info, [f"ликвидность не проверена: {exc} — кандидаты не проверены по стакану, "
                      f"обороту и demo-цене"]
    demo_guard = demo_guard and not replay
    if demo_guard and demo_client is None:
        raise ValueError("проверка demo-цены: нужен demo_client (demo-книга)")
    info.update(enabled=True, position_usdt=position, book=not replay, demo_guard=demo_guard)
    if replay:
        info["note"] = "повтор --at: исторического стакана у OKX нет — проверен только оборот свечи"
    for r in rows:
        if r["status"] not in ("candidate", "near"):
            continue
        quote = r["inst_id"].partition("-")[2]
        book = demo_book = None
        if not replay and quote == QUOTE:
            book = fetch_book(client, r["inst_id"])
            if demo_guard:
                demo_book = fetch_demo_book(demo_client, r["inst_id"])
        liq = check_liquidity(r["metrics"]["vol_quote"], book, position, rules, quote, demo_book)
        r["liquidity"] = liq
        if r["status"] == "candidate" and not liq["ok"]:
            status = "demo_price" if "demo_price" in liq["failed"] else "illiquid"
            r["status"], r["reasons"] = status, list(liq["reasons"])
    return info, []


# --- Свежесть сигнала (PUMP-STALE) ---

def fetch_last(client, inst_id: str) -> tuple[float, Optional[int]]:
    """Текущая цена пары: last из GET market/ticker (лента клиента) и её ts, мс."""
    data = client.get(TICKER_PATH, {"instId": inst_id})
    snap = data[0] if data else None
    if not isinstance(snap, dict):
        raise ScanError(f"{inst_id}: пустой ответ market/ticker")
    last = _num(snap.get("last"), f"{inst_id} last")
    if not last > 0:
        raise ScanError(f"{inst_id}: market/ticker без цены last: {str(snap)[:120]}")
    ts = str(snap.get("ts") or "")
    return last, int(ts) if ts.isdigit() else None


def check_freshness(candle_ts: int, close: float, last: float, now_ms: int,
                    max_age_min: float = DEFAULT_MAX_AGE_MIN) -> dict:
    """Сигнал свеж, если от закрытия сигнальной свечи прошло ≤ max_age_min минут и текущая
    цена last не ниже её close. Возвращает {ok, age_min, last, vs_close_pct, reasons}."""
    age_min = (now_ms - (candle_ts + BAR_MS)) / 60_000
    vs_close = (last - close) / close * 100
    reasons = []
    if age_min > max_age_min + EPS:
        reasons.append(f"сигнал устарел: {age_min:.0f} мин после закрытия свечи "
                       f"> {max_age_min:g} мин")
    if last < close * (1 - EPS):
        reasons.append(f"цена ниже close сигнальной свечи: {last:g} < {close:g} "
                       f"({vs_close:+.2f}%)")
    return {"ok": not reasons, "age_min": _r(age_min, 1), "last": last,
            "vs_close_pct": _r(vs_close, 4), "reasons": reasons}


def apply_freshness(client, rows: list[dict], now_ms: int,
                    max_age_min: float = DEFAULT_MAX_AGE_MIN,
                    replay: bool = False) -> dict:
    """PUMP-STALE: кандидат со старым сигналом или ценой ниже close сигнальной свечи -> stale
    с причиной. Только живой скан: при повторе --at текущей цены на прошлый момент нет.
    Возвращает блок freshness отчёта."""
    info = {"enabled": not replay, "max_age_min": max_age_min, "price": "market/ticker last",
            "note": None}
    if replay:
        info["note"] = "повтор --at: свежесть не проверялась — текущей цены на прошлый момент нет"
        return info
    for r in rows:
        if r["status"] != "candidate":
            continue
        last, _ = fetch_last(client, r["inst_id"])
        fresh = check_freshness(r["candle_ts"], r["metrics"]["close"], last, now_ms, max_age_min)
        r["freshness"] = fresh
        if not fresh["ok"]:
            r["status"], r["reasons"] = "stale", list(fresh["reasons"])
    return info


# --- Скан ---

def run_scan(client, *, pairs: Optional[Sequence[str]] = None, top_n: int = DEFAULT_TOP_N,
             exclude: Iterable[str] = (), at_ms: Optional[int] = None,
             n_bars: int = DEFAULT_BARS, thresholds: Thresholds = Thresholds(),
             feed: str = DEFAULT_FEED, endpoint: Optional[str] = None,
             now_ms: Optional[int] = None, pocket: Optional[Path] = None,
             liq_rules: LiquidityRules = LiquidityRules(), demo_client=None,
             max_age_min: float = DEFAULT_MAX_AGE_MIN) -> dict:
    """Полный скан (только публичные GET через client.get). Ошибки данных не глушатся.
    client — лента feed; demo_client — demo-лента: public/instruments (feed=live без pairs:
    универсум сверяется с demo) и demo-книга кандидатов (PUMP-DEMO-GUARD: feed=live, живой скан
    с проверкой ликвидности). pocket — путь файла кармана для проверки ликвидности; None —
    без проверки. max_age_min — свежесть сигнала (PUMP-STALE)."""
    now_ms = int(time.time() * 1000) if now_ms is None else now_ms
    endpoint = endpoint or ("history" if at_ms is not None else "candles")
    if pairs:
        scan_pairs = list(dict.fromkeys(pairs))
        universe = {"source": "pairs", "eligible": len(scan_pairs)}
    else:
        tickers = client.get(TICKERS_PATH, {"instType": "SPOT"})
        if not tickers:
            raise ScanError("пустой ответ market/tickers")
        tradable = None
        if feed == "live":
            if demo_client is None:
                raise ValueError("лента live: нужен demo_client — универсум сверяется с demo")
            tradable = fetch_tradable(demo_client)
        scan_pairs, universe = select_universe(tickers, top_n, exclude, tradable)
    rows = [scan_pair(client, inst, thresholds, n_bars=n_bars, at_ms=at_ms, endpoint=endpoint,
                      feed=feed) for inst in scan_pairs]
    ref_ms = at_ms if at_ms is not None else now_ms
    expected_ts = ref_ms // BAR_MS * BAR_MS - BAR_MS
    candle_ts, warnings = finalize(rows, expected_ts)
    freshness = apply_freshness(client, rows, ref_ms, max_age_min, replay=at_ms is not None)
    liquidity, liq_warnings = apply_liquidity(client, rows, pocket, liq_rules,
                                              replay=at_ms is not None, demo_client=demo_client,
                                              demo_guard=feed == "live")
    warnings += liq_warnings
    rows.sort(key=_order_key)                       # illiquid — сразу за кандидатами
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
        "liquidity": liquidity,
        "freshness": freshness,
        "universe": universe,
        "pairs": scan_pairs,
        "counts": {"scanned": len(rows), "candidates": len(by_status["candidate"]),
                   "near": len(by_status["near"]), "insufficient": len(by_status["insufficient"]),
                   "stale": len(by_status["stale"]), "no_data": len(by_status["no_data"])},
        "candidates": by_status["candidate"],
        "illiquid": by_status["illiquid"],
        "demo_price": by_status["demo_price"],
        "near": by_status["near"],
        "insufficient": by_status["insufficient"],
        "stale": by_status["stale"],
        "no_data": by_status["no_data"],
        "warnings": warnings,
        "requests": sum(getattr(c, "requests", 0) or 0
                        for c in {id(c): c for c in (client, demo_client) if c is not None}.values()),
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
        "liquidity": row.get("liquidity"),
        "freshness": row.get("freshness"),
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
    if report["illiquid"]:
        text += "; неликвидны: " + ", ".join(
            f"{i} — {res[i]['reasons'][0]}" for i in report["illiquid"][:3])
    if report["demo_price"]:
        text += "; " + ", ".join(
            f"{i} — {res[i]['reasons'][0]}" for i in report["demo_price"][:3])
    if not report["candidates"] and report["near"]:
        text += "; почти: " + ", ".join(
            f"{i} — {res[i]['reasons'][0]}" for i in report["near"][:3])
    if report["insufficient"]:
        text += f"; данных мало: {', '.join(report['insufficient'])}"
    if report["stale"]:
        text += "; устарело: " + ", ".join(
            f"{i} — {res[i]['reasons'][0]}" for i in report["stale"][:3])
    if report["no_data"]:
        text += f"; нет в ленте {report['feed']}: {', '.join(report['no_data'])}"
    if report["candidates"] and not report["liquidity"]["enabled"]:
        text += "; ликвидность не проверена"
    return text


# --- Журнал ---

JOURNAL_ROW_KEYS = ("inst_id", "close", "vol_quote", "impulse_pct", "vol_ratio_median",
                    "vol_ratio_mean", "rsi", "ma20", "macd_hist", "chg24_pct", "score",
                    "liquidity", "freshness")


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
        "liquidity": report["liquidity"],
        "freshness": report["freshness"],
        "universe": report["universe"],
        "pairs": report["pairs"],
        "counts": report["counts"],
        "candidates": [brief(i) for i in report["candidates"]],
        "illiquid": [brief(i) for i in report["illiquid"]],
        "demo_price": [brief(i) for i in report["demo_price"]],
        "near": [dict(brief(i), failed=res[i]["failed"]) for i in report["near"]],
        "stale": [dict(brief(i), reasons=res[i]["reasons"]) for i in report["stale"]],
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


def _times(x: Optional[float]) -> str:
    return "—" if x is None else (f"{x:.0f}" if abs(x) >= 100 else f"{x:.2f}")


def _liq_header(liq: dict) -> str:
    if not liq["enabled"]:
        return f"Ликвидность: не проверяется — {liq['note']}"
    parts = [f"позиция P = {liq['position_usdt']:g} USDT ({POCKET_FIELD} из {liq['pocket']})"]
    if liq["book"]:
        parts += [f"глубина ask до +{liq['band_pct']:g}% ≥ {liq['depth_min']:g}×P",
                  f"спред ≤ {liq['spread_max']:g}%"]
    parts.append(f"оборот свечи ≥ {liq['turnover_min']:g}×P")
    if liq.get("demo_guard"):
        parts.append(f"demo-книга: двусторонняя, |demo-mid − live-mid| ≤ {liq['spread_max']:g}%, "
                     f"глубина и спред — как у live")
    return "Ликвидность: " + " · ".join(parts) + (f" ({liq['note']})" if liq["note"] else "")


def _liq_table(report: dict) -> list[str]:
    """Таблица ликвидности проверенных пар (кандидаты, illiquid, почти кандидаты)."""
    checked = [r for r in report["results"] if r.get("liquidity")]
    if not checked:
        return []
    lines = ["", f"Ликвидность против позиции P = {report['liquidity']['position_usdt']:g} USDT:",
             f"{'Пара':<14}{'Глуб.ask':>9}{'×P':>7}{'Спред%':>8}{'Оборот':>8}{'×P':>7}"
             f"{'demo×P':>8}{'demoСпр%':>9}{'Откл.%':>8}  Итог"]
    for r in checked:
        q = r["liquidity"]
        if not q["ok"]:
            verdict = "нет: " + "; ".join(q["reasons"])
        else:
            verdict = "да" + (" (по обороту: стакан не проверен)" if q["notes"] else "")
        lines.append(f"{r['inst_id']:<14}{_compact(q['depth_usdt']):>9}{_times(q['depth_x']):>7}"
                     f"{_fmt(q['spread_pct'], '.2f'):>8}{_compact(q['turnover_usdt']):>8}"
                     f"{_times(q['turnover_x']):>7}{_times(q.get('demo_depth_x')):>8}"
                     f"{_fmt(q.get('demo_spread_pct'), '.2f'):>9}"
                     f"{_fmt(q.get('demo_dev_pct'), '.2f'):>8}  {verdict}")
    return lines


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
        demo = ""
        if "demo_instruments" in u:
            demo = (f" → есть в demo (state=live, {u['demo_instruments']} пар): "
                    f"{u['eligible']}")
            if u["not_in_demo_top"]:
                demo += f" [из топ-{u['top_n']} нет в demo: {', '.join(u['not_in_demo_top'])}]"
        live_eligible = u["eligible"] + u.get("not_in_demo", 0)
        uni = (f"tickers SPOT {u['tickers']} → USDT-пар {u['usdt_pairs']} → без стейблов "
               f"{u['non_stable']} → без {', '.join(u['exclude'])}: {live_eligible}{demo} → "
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
        _liq_header(report["liquidity"]),
        "",
        f"{'Пара':<14}{'Имп.%':>7}{'vol×мед':>9}{'vol×ср':>8}{'RSI':>6}{'>MA20':>6}"
        f"{'MACD>0':>7}{'24ч%':>8}{'Оборот':>8}{'score':>7}  Итог",
    ]
    for r in report["results"]:
        c = r["checks"]
        if r["status"] == "candidate":
            verdict = "КАНДИДАТ"
        elif r["status"] == "illiquid":
            verdict = "НЕЛИКВИДЕН: " + "; ".join(r["reasons"])
        elif r["status"] == "demo_price":
            verdict = "DEMO-ЦЕНА: " + "; ".join(r["reasons"])
        elif r["status"] == "stale" and r.get("freshness"):
            verdict = "СИГНАЛ УСТАРЕЛ: " + "; ".join(r["reasons"])
        elif r["status"] == "near":
            verdict = "почти: " + r["reasons"][0]
            if r.get("liquidity") and not r["liquidity"]["ok"]:
                verdict += (" · demo-цена" if "demo_price" in r["liquidity"]["failed"]
                            else " · неликвиден")
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
    lines += _liq_table(report)
    counts = report["counts"]
    liq = report["liquidity"]
    lines += [
        "",
        f"Кандидатов: {counts['candidates']} из {counts['scanned']}"
        + (" — " + ", ".join(report["candidates"]) if report["candidates"] else "."),
    ]
    if liq["enabled"]:
        lines.append("Неликвидны (фильтр пройден, ликвидность — нет): "
                     + (", ".join(report["illiquid"]) if report["illiquid"] else "нет"))
    if liq.get("demo_guard"):
        lines.append("Demo-цена не прошла (фильтр пройден, demo-книга мёртвая или отстаёт): "
                     + (", ".join(report["demo_price"]) if report["demo_price"] else "нет"))
    lines.append("Почти кандидаты (не хватило одного условия): "
                 + (", ".join(report["near"]) if report["near"] else "нет"))
    if report["insufficient"]:
        lines.append(f"Данных мало (< {MIN_BARS} закрытых свечей): {', '.join(report['insufficient'])}")
    fr = report["freshness"]
    signal_stale = [r["inst_id"] for r in report["results"]
                    if r["status"] == "stale" and r.get("freshness")]
    candle_stale = [i for i in report["stale"] if i not in signal_stale]
    if signal_stale:
        lines.append(f"Сигнал устарел (> {fr['max_age_min']:g} мин после закрытия свечи или "
                     f"цена ниже её close): {', '.join(signal_stale)}")
    if candle_stale:
        lines.append(f"Свеча устарела: {', '.join(candle_stale)}")
    if report["no_data"]:
        lines.append(f"Нет в ленте {report['feed']} (OKX 51001): {', '.join(report['no_data'])}")
    for w in report["warnings"]:
        lines.append(f"ВНИМАНИЕ: {w}")
    journal = report.get("journal")
    if report.get("journal_error"):
        lines.append(f"Журнал: НЕ ЗАПИСАН — {report['journal_error']}")
    else:
        lines.append("Журнал: " + (f"+1 строка scan → {journal}" if journal else
                                   "не пишется (повтор или --no-journal)"))
    if not liq["enabled"]:
        lines.append("Кандидат — не сделка: вход, размер, стоп и ликвидность (сканер её не "
                     "проверял) решает Pump Risk Taker по pump-pocket.json.")
    else:
        lines.append("Кандидат — не сделка: вход, размер и стоп решает Pump Risk Taker по "
                     "pump-pocket.json; " + (
                         "ликвидность (стакан, спред, оборот) проверена на момент скана."
                         if liq["book"] else
                         "ликвидность проверена только по обороту свечи (стакана на прошлый "
                         "момент нет)."))
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
                    "закрытые 1H-свечи, фильтр скана №5, ликвидность кандидата против лимита "
                    "позиции pump-pocket.json (PUMP-LIQ). Ордеров не ставит.",
        epilog="Примеры:\n"
               "  python -m src.pump_scanner --exclude ETH SOL SUI ADA TRX ETC APT BNB XLM DOT\n"
               "  python -m src.pump_scanner --feed demo --at 2026-09-24T09:47+05:00 "
               "--pairs OKB-USDT LTC-USDT\n"
               "  python -m src.pump_scanner --feed demo --at 2026-09-24T08:42+05:00 "
               "--pairs IMX-USDT COMP-USDT\n"
               "Сканы №1–6 (24.09) шли по demo-ленте: их повтор — с --feed demo.\n"
               "Коды выхода: 0 — скан выполнен; 2 — ошибка данных или сети.",
        formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--feed", choices=FEEDS, default=DEFAULT_FEED,
                   help="лента рыночных данных: live (по умолчанию; универсум сверяется с парами "
                        "demo) или demo (диагностика и повтор сканов №1–6, insights/pump-feed.md)")
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
    liq = LiquidityRules
    p.add_argument("--liq-band", type=_positive, default=liq.band_pct, metavar="PCT",
                   help=f"ликвидность: полоса от лучшего ask для глубины стакана, %% "
                        f"(по умолчанию {liq.band_pct:g})")
    p.add_argument("--depth-min", type=_positive, default=liq.depth_min, metavar="K",
                   help=f"ликвидность: глубина ask в полосе ≥ K × позиция кармана "
                        f"(по умолчанию {liq.depth_min:g})")
    p.add_argument("--turnover-min", type=_positive, default=liq.turnover_min, metavar="M",
                   help=f"ликвидность: оборот последней закрытой свечи ≥ M × позиция "
                        f"(по умолчанию {liq.turnover_min:g})")
    p.add_argument("--spread-max", type=_positive, default=liq.spread_max, metavar="PCT",
                   help=f"ликвидность: спред ≤ PCT %% от mid по live- и demo-книге; тот же порог "
                        f"— отклонение demo-mid от live-mid (PUMP-DEMO-GUARD; по умолчанию "
                        f"{liq.spread_max:g})")
    p.add_argument("--max-age-min", type=_positive, default=DEFAULT_MAX_AGE_MIN, metavar="MIN",
                   help=f"свежесть сигнала (PUMP-STALE): кандидат, у которого от закрытия "
                        f"сигнальной свечи прошло больше MIN минут или текущая цена ниже её "
                        f"close, — stale (по умолчанию {DEFAULT_MAX_AGE_MIN:g}; только живой скан)")
    p.add_argument("--no-liquidity", action="store_true",
                   help=f"не проверять ликвидность (повтор прошлых сканов как они были); "
                        f"позиция — {POCKET_FIELD} из {POCKET_PATH.as_posix()}")
    p.add_argument("--endpoint", choices=tuple(ENDPOINTS), default=None,
                   help="эндпоинт свечей: по умолчанию candles для живого скана и history для "
                        "--at; для диагностики расхождений повтора")
    p.add_argument("--json", action="store_true", help="отчёт JSON")
    p.add_argument("--no-journal", action="store_true", help="не писать строку scan в журнал")
    p.add_argument("--journal", type=Path, default=JOURNAL_PATH,
                   help=f"путь журнала (по умолчанию {JOURNAL_PATH.as_posix()})")
    return p


def main(argv: Optional[list[str]] = None, client=None,
         now_ms: Optional[int] = None, demo_client=None) -> int:
    """CLI. client, demo_client и now_ms подставляют тесты (фейковая биржа, фиксированное
    время). client — лента --feed; demo_client — demo-лента для public/instruments и
    demo-книги кандидатов (PUMP-DEMO-GUARD)."""
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
    liq_rules = LiquidityRules(args.liq_band, args.depth_min, args.turnover_min, args.spread_max)
    client = client if client is not None else make_client(args.feed)
    if demo_client is None:
        demo_client = client if args.feed == "demo" else make_client("demo")
    try:
        report = run_scan(client, pairs=pairs, top_n=args.top, exclude=args.exclude,
                          at_ms=args.at, n_bars=args.bars, thresholds=thresholds,
                          feed=args.feed, endpoint=args.endpoint, now_ms=now_ms,
                          pocket=None if args.no_liquidity else POCKET_PATH,
                          liq_rules=liq_rules, demo_client=demo_client,
                          max_age_min=args.max_age_min)
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
            report["journal_error"] = f"{args.journal.as_posix()}: {exc}"
            print(f"Журнал не записан ({args.journal}): {exc}", file=sys.stderr)
            code = 2
    print(json.dumps(report, ensure_ascii=False, indent=2) if args.json else render_text(report))
    return code


if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):   # ≥, ×, → в консоли Windows с cp1251
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8")
    logging.basicConfig(level=logging.WARNING,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    sys.exit(main())
