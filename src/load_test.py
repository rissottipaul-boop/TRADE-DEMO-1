"""Нагрузочный тест (задача P1-LOAD): серия place/cancel на demo.

Сценарий: N циклов «выставить лимитный buy на BTC-USDT на 40%+ ниже рынка
→ отменить». Ордер гарантированно не исполняется (цена далеко от рынка,
стоимость выше minNotional — см. insights/phase0-smoke-test.md).

Замеры:
- доля ошибок place и cancel (по кодам OKX, errors.py);
- задержки успешных вызовов place/cancel (p50/p95), мс;
- число отказов по rate limit (50011/50061) — backoff + повтор,
  засчитывается в статистику, тест не падает (insights/okx-api.md §3).

После серии: висящих ордеров НАШИХ (clOrdId с префиксом) быть не должно;
остатки отменяются и фиксируются в отчёте. Чужие ордера (работающий
движок P1-72H, grid-боты) НЕ трогаем.

Запуск: .venv\\Scripts\\python.exe -m src.load_test [--cycles 100]
"""
import argparse
import json
import logging
import sys
import time
from collections import Counter
from dataclasses import dataclass, field

import ccxt

from . import order_owner
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
log = logging.getLogger("loadtest")

SYMBOL = "BTC/USDT"
INST_ID = "BTC-USDT"
# Префикс clOrdId теста из реестра владельцев (ORDER-OWNER-TAG) — «lt»
CL_PREFIX = order_owner.LOAD_TEST
PRICE_FACTOR = 0.6  # 40% ниже рынка — гарантированно без исполнения
MIN_COST_USDT = 6.0  # с запасом выше minNotional 5 USDT (см. phase0-smoke-test.md)

RATE_LIMIT_CODES = {"50011", "50061"}  # лимит эндпоинта / лимит суб-аккаунта
MAX_RETRIES = 6
BACKOFF_BASE_S = 0.5
BACKOFF_CAP_S = 8.0


@dataclass
class Stats:
    cycles_ok: int = 0
    place_lat_ms: list[float] = field(default_factory=list)
    cancel_lat_ms: list[float] = field(default_factory=list)
    place_errors: Counter = field(default_factory=Counter)
    cancel_errors: Counter = field(default_factory=Counter)
    rate_limit_hits: Counter = field(default_factory=Counter)  # code -> число отказов
    retries: int = 0  # суммарно повторных попыток (все причины)
    leftover_cancelled: list[str] = field(default_factory=list)


def _percentile(sorted_vals: list[float], pct: float) -> float:
    """Перцентиль методом ближайшего ранга; sorted_vals уже отсортирован."""
    if not sorted_vals:
        return 0.0
    idx = min(len(sorted_vals) - 1, max(0, round(pct / 100 * len(sorted_vals)) - 1))
    return sorted_vals[idx]


def _is_rate_limit(exc: BaseException) -> str | None:
    """Код rate-limit отказа (50011/50061) или None."""
    code = extract_error_code(exc)
    if code in RATE_LIMIT_CODES:
        return code
    if isinstance(exc, (ccxt.RateLimitExceeded, ccxt.DDoSProtection)):
        return code or "rateLimit"
    return None


def _call_with_backoff(func, stats: Stats, op: str):
    """Вызов CCXT с backoff при rate limit.

    Возвращает (результат, задержка_мс_успешной_попытки).
    Rate-limit отказы идут в stats.rate_limit_hits и НЕ считаются ошибкой
    цикла; прочие ошибки после исчерпания попыток — перевыбрасываются.
    """
    for attempt in range(MAX_RETRIES + 1):
        if attempt:
            delay = min(BACKOFF_CAP_S, BACKOFF_BASE_S * 2 ** (attempt - 1))
            stats.retries += 1
            time.sleep(delay)
        started = time.perf_counter()
        try:
            result = func()
            return result, (time.perf_counter() - started) * 1000
        except ccxt.BaseError as exc:
            rl_code = _is_rate_limit(exc)
            if rl_code is not None and attempt < MAX_RETRIES:
                stats.rate_limit_hits[rl_code] += 1
                log.info("%s: rate limit %s, backoff (попытка %d)", op, rl_code, attempt + 1)
                continue
            raise
    raise RuntimeError("unreachable")


def _remaining_our_orders(exchange) -> list[dict]:
    """Висящие ордера ТЕСТА (по префиксу clOrdId) на инструменте."""
    pending = fetch_pending_orders(exchange, INST_ID)
    return [o for o in pending if order_owner.is_owned_by(o.get("clOrdId"), CL_PREFIX)]


def run(cycles: int) -> tuple[int, Stats, dict]:
    settings = load_settings()
    if not settings.is_demo:
        log.error("Нагрузочный тест запускать только в demo (OKX_MODE=demo)")
        return 1, Stats(), {}
    log.info("Режим: %s (domain %s), циклов: %d", settings.mode, settings.domain, cycles)

    ex = create_exchange(settings)
    stats = Stats()

    # Текущая цена — из демо-стакана: ордера ставятся в демо, сравнивать
    # надо с демо-котировкой, иначе «далеко от рынка» не гарантировано.
    ticker = ex.fetch_ticker(SYMBOL)
    market = float(ticker["last"])
    price = round(market * PRICE_FACTOR, 1)  # tickSz BTC-USDT = 0.1
    amount = round(MIN_COST_USDT / price, 8)  # стоимость >= 6 USDT
    log.info("Рынок %.1f -> лимит %.1f (-40%%), размер %.8f BTC (~%.1f USDT)",
             market, price, amount, price * amount)

    started_all = time.perf_counter()
    for i in range(1, cycles + 1):
        cl_ord_id = order_owner.new_cl_ord_id(CL_PREFIX)
        order_id = None
        try:
            order, lat = _call_with_backoff(
                lambda: ex.create_limit_buy_order(
                    SYMBOL, amount, price,
                    params={"clOrdId": cl_ord_id, "expTime": exp_time_ms(30_000)},
                ),
                stats, "place",
            )
            stats.place_lat_ms.append(lat)
            order_id = order["id"]
        except ccxt.BaseError as exc:
            code = extract_error_code(exc) or type(exc).__name__
            stats.place_errors[code] += 1
            log.warning("цикл %d: place ошибка %s: %s", i, code, str(exc)[:200])
            continue  # ордер не создан (либо неизвестен) — цикл завершён

        try:
            _, lat = _call_with_backoff(
                lambda: ex.cancel_order(order_id, SYMBOL),
                stats, "cancel",
            )
            stats.cancel_lat_ms.append(lat)
        except ccxt.BaseError as exc:
            code = extract_error_code(exc) or type(exc).__name__
            stats.cancel_errors[code] += 1
            log.warning("цикл %d: cancel ошибка %s: %s", i, code, str(exc)[:200])

        stats.cycles_ok += 1
        if i % 20 == 0:
            log.info("прогресс: %d/%d циклов, rate-limit отказов: %d",
                     i, cycles, sum(stats.rate_limit_hits.values()))
    duration_s = time.perf_counter() - started_all

    # Финальная проверка: наших висящих ордеров быть не должно.
    # Чужие (движок P1-72H) не трогаем.
    leftover = _remaining_our_orders(ex)
    if leftover:
        log.warning("Остались висящие ордера теста: %d — отменяем", len(leftover))
    for o in leftover:
        try:
            _call_with_backoff(
                lambda o=o: ex.cancel_order(o["ordId"], SYMBOL, params={"clOrdId": o.get("clOrdId")}),
                stats, "cleanup-cancel",
            )
            stats.leftover_cancelled.append(o["ordId"])
        except ccxt.BaseError as exc:
            code = extract_error_code(exc) or type(exc).__name__
            stats.cancel_errors[code] += 1
            log.error("Не удалось отменить остаток %s: %s", o["ordId"], code)
    final_leftover = _remaining_our_orders(ex)

    place_lats = sorted(stats.place_lat_ms)
    cancel_lats = sorted(stats.cancel_lat_ms)
    report = {
        "symbol": SYMBOL,
        "market_price": market,
        "limit_price": price,
        "amount_btc": amount,
        "cycles_requested": cycles,
        "cycles_ok": stats.cycles_ok,
        "duration_s": round(duration_s, 1),
        "throughput_cycles_per_s": round(stats.cycles_ok / duration_s, 3) if duration_s else 0,
        "place": {
            "ok": len(place_lats),
            "errors": dict(stats.place_errors),
            "p50_ms": round(_percentile(place_lats, 50), 1),
            "p95_ms": round(_percentile(place_lats, 95), 1),
        },
        "cancel": {
            "ok": len(cancel_lats),
            "errors": dict(stats.cancel_errors),
            "p50_ms": round(_percentile(cancel_lats, 50), 1),
            "p95_ms": round(_percentile(cancel_lats, 95), 1),
        },
        "rate_limit_hits": dict(stats.rate_limit_hits),
        "retries": stats.retries,
        "leftover_after_cleanup": len(final_leftover),
        "leftover_cancelled_ids": stats.leftover_cancelled,
    }

    print("\n=== ОТЧЁТ НАГРУЗОЧНОГО ТЕСТА ===")
    print(json.dumps(report, ensure_ascii=False, indent=2))

    if final_leftover:
        log.error("ПОСЛЕ ТЕСТА ОСТАЛИСЬ ВИСЯЩИЕ ОРДЕРА ТЕСТА: %d", len(final_leftover))
        return 2, stats, report
    error_share = (sum(stats.place_errors.values()) + sum(stats.cancel_errors.values())) / max(1, 2 * cycles)
    log.info("ТЕСТ ЗАВЕРШЁН: доля ошибок %.1f%%, висящих ордеров 0", error_share * 100)
    return 0, stats, report


def main() -> int:
    parser = argparse.ArgumentParser(description="Нагрузочный тест place/cancel на OKX demo")
    parser.add_argument("--cycles", type=int, default=100, help="число циклов place+cancel (>=100)")
    args = parser.parse_args()
    code, _, _ = run(max(1, args.cycles))
    return code


if __name__ == "__main__":
    sys.exit(main())
