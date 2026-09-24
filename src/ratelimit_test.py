"""Тест rate limit (задача P1-RATELIMIT): контролируемый обстрел demo API.

Цель — поймать отказ 50011 (per-endpoint/per-instrument лимит, User ID/IP) и/или
50061 (суб-аккаунт лимит 1000 ордер-запросов/2с) и измерить реальные числа:
сколько запросов проходит за 2-секундное окно до первого отказа, какой код,
какие заголовки (Retry-After / x-ratelimit-*) возвращает биржа, как выглядит
отказ на batch-запросе (весь запрос целиком или per-item sCode в data[]).

Методика (после неудачи последовательного прогона 04:57, см. лог
logs/ratelimit_test_2026-09-24.log: 5 батчей × 20 за 1636 мс ≈ 3 запроса/с —
это на два порядка ниже эндпоинт-лимита batch 300 запросов/2с и вдесятеро ниже
суб-аккаунтного 1000 ордеров/2с — лимит был недостижим по скорости):
- Параллельный шторм из пула потоков: у каждого потока свой exchange без
  throttler CCXT (enableRateLimit=False), keep-alive сессия переиспользуется.
  Ранняя остановка: первый отказ 50011/50061 глушит весь пул через Event.
- Режим --mode single (дефолт): одиночные POST /trade/order. Целится в
  per-instId лимит place (по докам 60/2с на UserID+instId, okx-api.md §3).
  Дёшево: десятки ордеров по ~6 USDT, а не тысячи.
- Режим --mode batch: батчи по 20 (максимум OKX). Целится в суб-аккаунтный
  50061 (1000 ордер-запросов/2с, batch считается поштучно) либо в
  эндпоинт-лимит batch-orders (300 запросов/2с). Дороже: до ~1400 ордеров.
  Внимание: OKX batch place — максимум 20 ордеров за запрос, «300» из брифа —
  это лимит ЗАПРОСОВ/2с на эндпоинт, а не размер батча.
- Режим --mode public: GET /market/ticker без средств вообще. Контрольный
  выстрел: публичные лимиты считаются по IP (20/2с) — доказывает, что demo
  вообще отдаёт 50011, не трогая ордерный бюджет движка.
- Сразу после отказа — фаза backoff: ТОТ ЖЕ тип запроса повторяется с
  экспоненциальным backoff (0.5→8 с) до успеха; счётчики попыток/пауз/кодов
  возвращаются целиком — проверка «состояние не потеряно».
- Зачистка: cancel-batch-orders (≤20 за запрос), добивка остатков
  последовательно с backoff; финальная проверка orders-pending по префиксу
  clOrdId «rt» — должно быть 0.

Безопасность параллельного движка P1-72H: лимиты эндпоинтов у OKX независимы —
reconcile движка (GET orders-pending/positions раз в 60 с) не конкурирует за
бюджет place. Пересечение только одно: per-instId лимит place BTC-USDT общий —
если движок решит поставить ордер ровно в секунды шторма, он получит 50011 и
уйдёт в свой штатный retry (это зафиксируем в отчёте, движок не останавливаем).
Потолок ордеров режется по свободному балансу (≤30% availBal USDT — деньги без займа).
tdMode ордеров — по режиму аккаунта (SPOT-TDMODE, src/account_mode.py).

Запуск: .venv\\Scripts\\python.exe -m src.ratelimit_test [--mode single|batch|public]
        [--threads 16] [--batch 20] [--max-orders 240]
Коды выхода: 0 — лимит пойман, backoff отработал, висящих 0;
             2 — остались висящие ордера; 3 — лимит не достигнут;
             4 — backoff не привёл к успеху.
"""
import argparse
import json
import logging
import sys
import threading
import time

import ccxt

from . import order_owner
from .account_mode import avail_balance, fetch_account_mode
from .config import load_settings
from .connector import (
    create_exchange,
    exp_time_ms,
    fetch_pending_orders,
)
from .errors import extract_error_code

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
log = logging.getLogger("ratelimit")

SYMBOL = "BTC/USDT"
INST_ID = "BTC-USDT"
# Префикс clOrdId теста из реестра владельцев (ORDER-OWNER-TAG) — «rt»:
# отличие от движка (bot*) и load_test (lt)
CL_PREFIX = order_owner.RATE_LIMIT_TEST
PRICE_FACTOR = 0.6  # 40% ниже рынка — гарантированно без исполнения
MIN_COST_USDT = 6.0  # с запасом выше minNotional 5 USDT (insights/phase0-smoke-test.md)
MAX_FREE_BALANCE_FRACTION = 0.3  # потолок заморозки под ордера теста

RATE_LIMIT_CODES = {"50011", "50061"}  # per-endpoint (User ID/IP) / суб-аккаунт
MAX_RETRIES = 6
BACKOFF_BASE_S = 0.5
BACKOFF_CAP_S = 8.0
BATCH_MAX = 20  # максимум OKX для batch-orders и cancel-batch-orders
WINDOW_MS = 2000  # окно, в котором OKX считает лимиты (N запросов/2с)

# Заголовки, интересные при rate limit (OKX может прислать Retry-After / x-ratelimit-*)
HDR_PREFIXES = ("retry", "x-ratelimit", "ratelimit")


# tdMode спота по режиму аккаунта (SPOT-TDMODE, src/account_mode.py): run()
# выставляет его до старта потоков; в acctLv 3–4 cash даёт 51000
_spot_td_mode = "cash"


def _order_params(price: float, amount: float, cl_ord_id: str) -> dict:
    """Один ордер: лимитный buy; expTime уйдёт заголовком (OKXExchange.sign)."""
    return {
        "instId": INST_ID,
        "tdMode": _spot_td_mode,
        "side": "buy",
        "ordType": "limit",
        "px": f"{price:.1f}",  # tickSz BTC-USDT = 0.1
        "sz": f"{amount:.8f}",  # lotSz BTC-USDT = 1e-8
        "clOrdId": cl_ord_id,
        "expTime": exp_time_ms(30_000),
    }


def _chunks(items: list, size: int):
    for i in range(0, len(items), size):
        yield items[i:i + size]


def _rate_limit_headers(exchange) -> dict:
    raw = getattr(exchange, "last_response_headers", None) or {}
    return {k: v for k, v in raw.items() if k.lower().startswith(HDR_PREFIXES)}


def _is_rl(code) -> bool:
    return code in RATE_LIMIT_CODES


def _send_order_request(exchange, orders: list[dict], t0: float) -> dict:
    """Один place-запрос (1..20 ордеров): засечь время, код, заголовки, items."""
    started = time.perf_counter()
    rec = {"n_orders": len(orders), "t_ms": round((started - t0) * 1000, 1)}
    try:
        if len(orders) == 1:
            resp = exchange.private_post_trade_order(orders[0])
        else:
            resp = exchange.private_post_trade_batch_orders(orders)
        rec.update(
            ok=True,
            lat_ms=round((time.perf_counter() - started) * 1000, 1),
            top_code=str(resp.get("code", "")),
            items=[
                {"clOrdId": it.get("clOrdId"), "ordId": it.get("ordId"),
                 "sCode": str(it.get("sCode", "")), "sMsg": it.get("sMsg", "")}
                for it in (resp.get("data") or [])
            ],
        )
    except ccxt.BaseError as exc:
        rec.update(
            ok=False,
            code=extract_error_code(exc) or type(exc).__name__,
            lat_ms=round((time.perf_counter() - started) * 1000, 1),
            headers=_rate_limit_headers(exchange),
            msg=str(exc)[:300],
        )
    return rec


def _send_public_request(exchange, t0: float) -> dict:
    """Один публичный запрос тикера (лимит по IP, без средств)."""
    started = time.perf_counter()
    rec = {"n_orders": 0, "t_ms": round((started - t0) * 1000, 1)}
    try:
        exchange.public_get_market_ticker({"instId": INST_ID})
        rec.update(ok=True, lat_ms=round((time.perf_counter() - started) * 1000, 1),
                   top_code="0", items=[])
    except ccxt.BaseError as exc:
        rec.update(
            ok=False,
            code=extract_error_code(exc) or type(exc).__name__,
            lat_ms=round((time.perf_counter() - started) * 1000, 1),
            headers=_rate_limit_headers(exchange),
            msg=str(exc)[:300],
        )
    return rec


def _rec_rl_hit(rec: dict) -> bool:
    """Отказ по rate limit: верхнеуровневый код либо per-item sCode в data[]."""
    if not rec["ok"] and _is_rl(rec.get("code")):
        return True
    return any(_is_rl(it["sCode"]) for it in rec.get("items", []))


def _backoff_attempt(exchange, mode: str, price: float, amount: float,
                     batch: int) -> tuple[dict, list[str]]:
    """Одна попытка того же типа запроса, что словил отказ.

    Exchange — keep-alive сессия воркера: попытка уходит без ~300 мс на
    TLS-handshake, пока бакет лимитера ещё пуст (refill ~30/с, см. выводы
    в insights/okx-api.md). Возвращает (rec, ordIds).
    """
    started = time.perf_counter()
    try:
        if mode == "public":
            exchange.public_get_market_ticker({"instId": INST_ID})
            return {"ok": True,
                    "lat_ms": round((time.perf_counter() - started) * 1000, 1)}, []
        orders = [_order_params(price, amount, order_owner.new_cl_ord_id(CL_PREFIX))
                  for _ in range(batch if mode == "batch" else 1)]
        resp = (exchange.private_post_trade_batch_orders(orders) if len(orders) > 1
                else exchange.private_post_trade_order(orders[0]))
        bad = [it for it in (resp.get("data") or [])
               if str(it.get("sCode", "")) not in ("0", "")]
        if bad:
            return {"ok": False, "code": str(bad[0].get("sCode")),
                    "lat_ms": round((time.perf_counter() - started) * 1000, 1)}, []
        return {"ok": True,
                "lat_ms": round((time.perf_counter() - started) * 1000, 1)}, \
               [it.get("ordId") for it in (resp.get("data") or [])]
    except ccxt.BaseError as exc:
        return {"ok": False, "code": extract_error_code(exc) or type(exc).__name__,
                "lat_ms": round((time.perf_counter() - started) * 1000, 1)}, []


def _backoff_under_fire(wid: int, exchange, mode: str, price: float, amount: float,
                        batch: int, t0: float) -> dict:
    """Backoff-цикл ВО ВРЕМЯ шторма: окно лимита держат остальные потоки,
    поэтому ретраи реально проходят сквозь отказы, а не в уже сброшенное окно.

    Счётчики попыток, пауз и кодов возвращаются целиком — проверка
    «состояние не потеряно».
    """
    waits: list[float] = []
    codes: list[str] = []
    ord_ids: list[str] = []
    for attempt in range(MAX_RETRIES + 1):
        if attempt:
            delay = min(BACKOFF_CAP_S, BACKOFF_BASE_S * 2 ** (attempt - 1))
            waits.append(delay)
            log.info("worker %d backoff: пауза %.1f с (попытка %d)",
                     wid, delay, attempt + 1)
            time.sleep(delay)
        rec, ord_ids = _backoff_attempt(exchange, mode, price, amount, batch)
        rec["t_ms"] = round((time.perf_counter() - t0) * 1000, 1)
        if rec["ok"]:
            log.info("worker %d backoff: успех с попытки %d (t=%.0f мс)",
                     wid, attempt + 1, rec["t_ms"])
            return {"ok": True, "attempts": attempt + 1, "waits_s": waits,
                    "reject_codes": codes, "ordIds": ord_ids,
                    "success_t_ms": rec["t_ms"]}
        code = rec["code"]
        codes.append(code)
        log.info("worker %d backoff: отказ %s (попытка %d)", wid, code, attempt + 1)
        if not _is_rl(code) or attempt == MAX_RETRIES:
            return {"ok": False, "attempts": attempt + 1, "waits_s": waits,
                    "reject_codes": codes}
    raise RuntimeError("unreachable")


def _storm_worker(wid: int, mode: str, settings, price: float, amount: float,
                  batch: int, t0: float, stop: threading.Event,
                  out: list, lock: threading.Lock, per_worker_cap: int,
                  linger_ms: int, state: dict) -> None:
    """Поток шторма: свой exchange без throttler, запросы подряд до стоп-сигнала.

    Первый поймавший RL-отказ воркер сразу уходит в backoff-цикл «под огнём»
    (остальные потоки ещё linger_ms держат окно насыщенным) — backoff
    проверяется на реальных отказах, а не в сброшенном окне. Прочие воркеры
    по истечении linger глушат пул через Event.
    """
    exchange = create_exchange(settings)
    exchange.enableRateLimit = False  # бурст без token-bucket CCXT — иначе лимит не достичь
    local: list[dict] = []
    try:
        while not stop.is_set() and len(local) < per_worker_cap:
            if mode == "public":
                rec = _send_public_request(exchange, t0)
            else:
                n = batch if mode == "batch" else 1
                orders = [_order_params(price, amount, order_owner.new_cl_ord_id(CL_PREFIX))
                          for _ in range(n)]
                rec = _send_order_request(exchange, orders, t0)
            rec["worker"] = wid
            local.append(rec)
            if _rec_rl_hit(rec):
                do_backoff = False
                with lock:
                    if state["first_rl_ms"] is None:
                        state["first_rl_ms"] = rec["t_ms"]
                        do_backoff = True
                        log.info("worker %d: rate limit пойман (%s) на t=%.0f мс, "
                                 "уходит в backoff; шторм продолжается %d мс",
                                 wid, rec.get("code") or "per-item sCode",
                                 rec["t_ms"], linger_ms)
                    elif rec["t_ms"] - state["first_rl_ms"] >= linger_ms:
                        stop.set()
                if do_backoff:
                    # Вне лока: ретраи со снами на своём же keep-alive exchange,
                    # затем публикация результата
                    backoff = _backoff_under_fire(wid, exchange, mode, price,
                                                  amount, batch, t0)
                    with lock:
                        state["backoff"] = backoff
                    stop.set()  # окно пройдено — остальным можно выходить
    finally:
        with lock:
            out.extend(local)


def _peak_window(records: list[dict], weight: str = "requests") -> int:
    """Максимум запросов (или ордер-запросов) в любом 2-секундном окне."""
    if not records:
        return 0
    weights = [r["n_orders"] if weight == "orders" else 1 for r in records]
    starts = [r["t_ms"] for r in records]
    peak = 0
    for i, t in enumerate(starts):
        cur = sum(w for tt, w in zip(starts, weights) if t <= tt < t + WINDOW_MS)
        peak = max(peak, cur)
    return peak


def _run_storm(mode: str, settings, price: float, amount: float,
               batch: int, threads: int, max_orders: int,
               linger_ms: int) -> dict:
    """Фаза 1: параллельный шторм до первого RL-отказа или потолка."""
    if mode == "batch":
        per_worker_cap = max(1, (max_orders // batch + threads - 1) // threads)
    else:  # single и public: max_orders — это число HTTP-запросов (1 ордер = 1 запрос)
        per_worker_cap = max(1, (max_orders + threads - 1) // threads)

    records: list[dict] = []
    lock = threading.Lock()
    stop = threading.Event()
    state = {"first_rl_ms": None, "backoff": None}
    t0 = time.perf_counter()
    pool = [
        threading.Thread(
            target=_storm_worker,
            args=(i, mode, settings, price, amount, batch, t0, stop, records, lock,
                  per_worker_cap, linger_ms, state),
            daemon=True,
        )
        for i in range(threads)
    ]
    for th in pool:
        th.start()
    for th in pool:
        th.join()
    records.sort(key=lambda r: r["t_ms"])

    placed: list[str] = []
    rl_rejections: list[dict] = []
    other_errors: dict[str, int] = {}
    for rec in records:
        if rec["ok"]:
            for it in rec["items"]:
                if it["sCode"] in ("0", ""):
                    placed.append(it["ordId"])
                elif _is_rl(it["sCode"]):
                    rl_rejections.append({"t_ms": rec["t_ms"], "code": it["sCode"],
                                          "per": "item"})
                else:
                    other_errors[it["sCode"]] = other_errors.get(it["sCode"], 0) + 1
        else:
            if _is_rl(rec.get("code")):
                rl_rejections.append({"t_ms": rec["t_ms"], "code": rec["code"],
                                      "per": "request", "headers": rec.get("headers"),
                                      "msg": rec.get("msg", "")[:200]})
            else:
                code = rec.get("code") or "unknown"
                other_errors[code] = other_errors.get(code, 0) + 1

    first_rl = min(rl_rejections, key=lambda r: r["t_ms"]) if rl_rejections else None
    window_stats = None
    if first_rl:
        # Сколько запросов/ордеров уложилось в 2-секундное окно перед первым отказом
        lo = max(0.0, first_rl["t_ms"] - WINDOW_MS)
        in_window = [r for r in records if lo <= r["t_ms"] <= first_rl["t_ms"]]
        window_stats = {
            "window_ms": [lo, first_rl["t_ms"]],
            "http_requests": len(in_window),
            "order_requests": sum(r["n_orders"] for r in in_window),
            "orders_accepted_in_window": sum(
                1 for r in in_window for it in r.get("items", [])
                if it["sCode"] in ("0", "")),
        }
    summary = {
        "mode": mode,
        "threads": threads,
        "http_requests": len(records),
        "order_requests": sum(r["n_orders"] for r in records),
        "span_ms": round(max((r["t_ms"] + r["lat_ms"] for r in records), default=0.0), 1),
        "orders_accepted": len(placed),
        "first_rl_rejection": first_rl,
        "window_before_first_rejection": window_stats,
        "rl_rejections_total": len(rl_rejections),
        "peak_http_per_2s": _peak_window(records, "requests"),
        "peak_orders_per_2s": _peak_window(records, "orders"),
        "other_errors": other_errors,
        "stop_reason": "rate limit пойман" if first_rl else "потолок max-orders",
    }
    log.info("Итог фазы 1: %s", json.dumps(summary, ensure_ascii=False))
    return {"summary": summary, "placed": placed, "backoff": state["backoff"],
            "records": [{k: v for k, v in r.items() if k != "items"} for r in records]}


def _retry_with_backoff(settings, mode: str, price: float, amount: float,
                        batch: int) -> dict:
    """Фаза 2: ТОТ ЖЕ тип запроса, что словил отказ, с экспоненциальным backoff.

    Throttler CCXT включён (как в проде). Счётчики попыток, пауз и кодов
    накапливаются и возвращаются целиком — проверка «состояние не потеряно».
    """
    exchange = create_exchange(settings)
    waits: list[float] = []
    codes: list[str] = []
    for attempt in range(MAX_RETRIES + 1):
        if attempt:
            delay = min(BACKOFF_CAP_S, BACKOFF_BASE_S * 2 ** (attempt - 1))
            waits.append(delay)
            log.info("backoff: пауза %.1f с (попытка %d)", delay, attempt + 1)
            time.sleep(delay)
        started = time.perf_counter()
        try:
            if mode == "public":
                exchange.public_get_market_ticker({"instId": INST_ID})
                return {"ok": True, "attempts": attempt + 1, "waits_s": waits,
                        "reject_codes": codes,
                        "lat_ms": round((time.perf_counter() - started) * 1000, 1)}
            orders = [_order_params(price, amount, order_owner.new_cl_ord_id(CL_PREFIX))
                      for _ in range(batch if mode == "batch" else 1)]
            resp = (exchange.private_post_trade_batch_orders(orders) if len(orders) > 1
                    else exchange.private_post_trade_order(orders[0]))
            rl_in_items = [it for it in (resp.get("data") or [])
                           if _is_rl(str(it.get("sCode", "")))]
            if rl_in_items:
                raise ccxt.ExchangeError(
                    f'okx {{"code":"{rl_in_items[0].get("sCode")}",'
                    f'"msg":"{rl_in_items[0].get("sMsg")}"}}')
            bad = [it for it in (resp.get("data") or [])
                   if str(it.get("sCode", "")) not in ("0", "")]
            if bad:
                raise ccxt.ExchangeError(
                    f'okx {{"code":"{bad[0].get("sCode")}","msg":"{bad[0].get("sMsg")}"}}')
            return {"ok": True, "attempts": attempt + 1, "waits_s": waits,
                    "reject_codes": codes,
                    "ordIds": [it.get("ordId") for it in (resp.get("data") or [])],
                    "lat_ms": round((time.perf_counter() - started) * 1000, 1)}
        except ccxt.BaseError as exc:
            code = extract_error_code(exc) or type(exc).__name__
            codes.append(code)
            if _is_rl(code) and attempt < MAX_RETRIES:
                log.info("backoff: rate limit %s, повтор (попытка %d)", code, attempt + 1)
                continue
            return {"ok": False, "attempts": attempt + 1, "waits_s": waits,
                    "reject_codes": codes}
    raise RuntimeError("unreachable")


def _our_pending(exchange) -> list[dict]:
    """Висящие ордера ТЕСТА (по префиксу clOrdId). Чужие (движок, grid-бот) не трогаем."""
    return [o for o in fetch_pending_orders(exchange, INST_ID)
            if order_owner.is_owned_by(o.get("clOrdId"), CL_PREFIX)]


def run(mode: str, threads: int, batch: int, max_orders: int,
        linger_ms: int) -> tuple[int, dict]:
    global _spot_td_mode
    settings = load_settings()
    if not settings.is_demo:
        log.error("Тест rate limit запускать только в demo (OKX_MODE=demo)")
        return 1, {}
    batch = max(2, min(batch, BATCH_MAX))
    ex0 = create_exchange(settings)
    market = float(ex0.fetch_ticker(SYMBOL)["last"])  # единственный публичный запрос
    price = round(market * PRICE_FACTOR, 1)
    amount = round(MIN_COST_USDT / price, 8)

    if mode != "public":
        acct = fetch_account_mode(ex0)
        _spot_td_mode = acct.spot_td_mode
        log.info("Режим аккаунта: acctLv=%s (%s), tdMode спота %s", acct.acct_lv, acct.name, _spot_td_mode)
        # Потолок по свободным средствам: заморозка ≤ 30% availBal USDT. availBal —
        # деньги без займа: и при autoLoan (acctLv 3–4) шторм в заём не уйдёт
        free_usdt = avail_balance(ex0, "USDT")
        affordable = int(free_usdt * MAX_FREE_BALANCE_FRACTION / MIN_COST_USDT)
        if max_orders > affordable:
            log.warning("max-orders урезан %d -> %d (free USDT %.1f)",
                        max_orders, affordable, free_usdt)
            max_orders = max(affordable, batch)
    log.info("Режим %s/%s, рынок %.1f -> лимит %.1f, размер %.8f BTC; "
             "потоков %d, батч %d, потолок %d",
             settings.mode, mode, market, price, amount, threads, batch, max_orders)

    # --- Фаза 1: параллельный шторм до первого RL-отказа (+ linger) ---
    storm = _run_storm(mode, settings, price, amount, batch, threads, max_orders,
                       linger_ms)
    summary = storm["summary"]
    placed: list[str] = storm["placed"]

    # --- Фаза 2: backoff. Первичный — «под огнём» из шторма (окно горячее);
    # если шторм лимит не поймал — контрольный прогон после шторма.
    backoff = storm["backoff"] or _retry_with_backoff(settings, mode, price,
                                                      amount, batch)
    log.info("Backoff-фаза: %s", json.dumps(backoff, ensure_ascii=False))
    placed.extend(backoff.get("ordIds") or [])

    # --- Фаза 3: зачистка — batch-отмена всех размещённых ---
    cancel_stats = {"requests": 0, "ok_items": 0, "rl_items": 0, "other_items": {}}
    for chunk in _chunks(placed, BATCH_MAX):
        try:
            resp = ex0.private_post_trade_cancel_batch_orders(
                [{"instId": INST_ID, "ordId": oid} for oid in chunk])
            cancel_stats["requests"] += 1
            for it in (resp.get("data") or []):
                code = str(it.get("sCode", ""))
                if code in ("0", ""):
                    cancel_stats["ok_items"] += 1
                elif _is_rl(code):
                    cancel_stats["rl_items"] += 1
                else:
                    cancel_stats["other_items"][code] = cancel_stats["other_items"].get(code, 0) + 1
        except ccxt.BaseError as exc:
            code = extract_error_code(exc) or type(exc).__name__
            cancel_stats["other_items"][code] = cancel_stats["other_items"].get(code, 0) + 1
        time.sleep(0.3)  # cancel-лимит 60/2с — не провоцируем его на зачистке
    log.info("Зачистка: %s", json.dumps(cancel_stats, ensure_ascii=False))

    # Добивка остатков последовательно с backoff (окно лимита могло не сброситься).
    leftover = _our_pending(ex0)
    for o in leftover:
        for attempt in range(MAX_RETRIES + 1):
            if attempt:
                time.sleep(min(BACKOFF_CAP_S, BACKOFF_BASE_S * 2 ** (attempt - 1)))
            try:
                item = (ex0.private_post_trade_cancel_order(
                    {"instId": INST_ID, "ordId": o["ordId"]}).get("data") or [{}])[0]
                if item.get("sCode") in (None, "0"):
                    log.info("Добивка: %s отменён", o["ordId"])
                    break
                raise ccxt.ExchangeError(f"sCode {item.get('sCode')}")
            except ccxt.BaseError as exc:
                log.warning("Добивка %s: %s (попытка %d)", o["ordId"],
                            extract_error_code(exc) or type(exc).__name__, attempt + 1)
    final_leftover = _our_pending(ex0)

    report = {
        "market_price": market,
        "limit_price": price,
        "amount_btc": amount,
        "phase1_storm": summary,
        "phase1_requests": storm["records"],
        "phase2_backoff": backoff,
        "phase3_cancel": cancel_stats,
        "leftover_after_cleanup": len(final_leftover),
    }

    print("\n=== ОТЧЁТ ТЕСТА RATE LIMIT ===")
    print(json.dumps(report, ensure_ascii=False, indent=2))

    if final_leftover:
        log.error("ПОСЛЕ ТЕСТА ОСТАЛИСЬ ВИСЯЩИЕ ОРДЕРА ТЕСТА: %d", len(final_leftover))
        return 2, report
    rl_caught = bool(summary["first_rl_rejection"] or
                     any(_is_rl(c) for c in backoff.get("reject_codes", [])))
    if not rl_caught:
        log.warning("Rate limit НЕ достигнут при %d запросах за %.0f мс",
                    summary["http_requests"], summary["span_ms"])
        return 3, report
    if not backoff.get("ok"):
        log.error("Backoff НЕ привёл к успеху: %s", backoff)
        return 4, report
    log.info("ТЕСТ ЗАВЕРШЁН: лимит пойман, backoff отработал, висящих 0")
    return 0, report


def main() -> int:
    parser = argparse.ArgumentParser(description="Тест rate limit 50011/50061 на OKX demo")
    parser.add_argument("--mode", choices=["single", "batch", "public"], default="single",
                        help="single: одиночный place (50011 per-instId); "
                             "batch: батчи по 20 (50061 суб-аккаунт / 50011 эндпоинт); "
                             "public: тикер без средств (50011 по IP)")
    parser.add_argument("--threads", type=int, default=16,
                        help="потоков шторма (у каждого свой keep-alive exchange)")
    parser.add_argument("--batch", type=int, default=20,
                        help="ордеров в batch-запросе режима batch (макс 20)")
    parser.add_argument("--max-orders", type=int, default=240,
                        help="потолок ордер-запросов фазы 1 (в public — HTTP-запросов)")
    parser.add_argument("--linger-ms", type=int, default=3000,
                        help="сколько мс продолжать шторм после первого RL-отказа "
                             "(окно лимита остаётся горячим для backoff-фазы)")
    args = parser.parse_args()
    code, _ = run(args.mode, args.threads, args.batch, args.max_orders, args.linger_ms)
    return code


if __name__ == "__main__":
    sys.exit(main())
