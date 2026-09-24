"""Smoke-тест Фазы 0: подключение, время, баланс, place/cancel в демо.

Запуск: python -m src.smoke_test
Критерий выхода Фазы 0 (roadmap.md): ордер ставится/отменяется в демо,
ошибки распознаются и логируются.

Ордер метится префиксом clOrdId `smk` (ORDER-OWNER-TAG, src/order_owner.py):
без своего clOrdId CCXT подставил бы brokerId `6b9ad…` в clOrdId и tag,
и ордер было бы не отличить от прочего кода через CCXT.
"""
import json
import logging
import sys
import time

from . import order_owner
from .config import load_settings
from .connector import check_time_sync, create_exchange, okx_call
from .storage import OrderRecord, Storage

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
log = logging.getLogger("smoke")

SYMBOL = "BTC/USDT"
# Далеко от рынка, чтобы не исполниться; стоимость > минимальной (см. phase0-smoke-test.md)
TEST_PRICE = 50000.0
TEST_AMOUNT = 0.0001


def main() -> int:
    settings = load_settings()
    log.info("Режим: %s (domain %s)", settings.mode, settings.domain)
    if not settings.is_demo:
        log.error("Smoke-тест запускать только в demo (OKX_MODE=demo)")
        return 1

    db = Storage()
    ex = create_exchange(settings)

    drift = check_time_sync(ex)
    log.info("Дрейф часов: %+d мс", drift)

    balance = ex.fetch_balance()
    usdt_free = balance.get("USDT", {}).get("free")
    log.info("Баланс USDT (free): %s", usdt_free)

    cl_ord_id = order_owner.new_cl_ord_id(order_owner.SMOKE_TEST)
    order = okx_call(ex.create_limit_buy_order, SYMBOL, TEST_AMOUNT, TEST_PRICE,
                     {"clOrdId": cl_ord_id})
    log.info("Ордер размещён: id=%s status=%s clOrdId=%s", order["id"], order["status"], cl_ord_id)

    inst_id = SYMBOL.replace("/", "-")
    now = time.time()
    record = OrderRecord(
        ord_id=str(order["id"]), inst_id=inst_id, side="buy", ord_type="limit",
        px=TEST_PRICE, sz=TEST_AMOUNT,
        state="live" if (order["status"] or "open") == "open" else "filled",
        filled_sz=0.0, avg_px=0.0, fee=0.0, create_time=now, update_time=now,
        raw_json=json.dumps(order, ensure_ascii=False, default=str),
        cl_ord_id=cl_ord_id,
    )
    db.upsert_order(record)
    log.info("Ордер записан в БД: ord_id=%s", record.ord_id)

    cancelled = okx_call(ex.cancel_order, order["id"], SYMBOL)
    log.info("Ордер отменён: id=%s status=%s", cancelled["id"], cancelled["status"])

    record.state = "canceled"
    record.update_time = time.time()
    db.upsert_order(record)
    log.info("Статус в БД обновлён: ord_id=%s -> canceled", record.ord_id)

    log.info("SMOKE-ТЕСТ ПРОЙДЕН")
    return 0


if __name__ == "__main__":
    sys.exit(main())
