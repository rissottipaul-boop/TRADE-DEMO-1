"""Self-test роутера ордеров src/order_router.py (задача p1-order-router).

Запуск из корня проекта: .venv\\Scripts\\python.exe -m src.order_router_selftest

Реальных запросов к бирже нет — FakeExchange. Отдельные temp-файлы БД
(data/order_router_selftest.db, data/risk_router_selftest.db), боевые
data/bot_state.db и risk_state.db не затрагиваются. Файлы удаляются после прогона.

Сценарии:
1. Нормальный ордер проходит полный конвейер и пишется в storage
   (статус дочитан через fetch_order, т.к. create_order вернул status=None).
2. При заблокированном входе (trip_breaker) ордер НЕ выставляется —
   fake не получил вызов create_order.
3. kill_switch отменяет открытые ордера через connector.emergency_stop
   (bulk cancel-batch) и помечает их canceled в storage.
4. Throttler: третий place-вызов при лимите 2/окно ждёт освобождения окна.
5. expTime и clOrdId проставляются в params, заголовок восстанавливается.
"""
import gc
import logging
import sys
import time
from pathlib import Path

from src import risk
from src.order_router import OrderRouter
from src.storage import Storage

logging.basicConfig(level=logging.CRITICAL)  # логи роутера/риска не засоряют вывод

TEST_STATE_DB = Path("data/order_router_selftest.db")
TEST_RISK_DB = Path("data/risk_router_selftest.db")
_passed = 0


def check(name: str, cond: bool, detail: str = "") -> None:
    global _passed
    if not cond:
        print(f"FAIL: {name} {detail}")
        sys.exit(1)
    _passed += 1
    print(f"OK: {name}")


class FakeExchange:
    """Mock CCXT okx: create_order/fetch_order + pending-API для emergency_stop.

    create_order возвращает status=None — имитация нюанса CCXT на свежем ордере.
    Ордер попадает в pending; cancel-batch убирает его оттуда.
    """

    def __init__(self) -> None:
        self.created: list[dict] = []
        self.cancelled: list[str] = []
        self.pending: list[dict] = []
        self.headers: dict[str, str] = {}
        self._next_id = 1

    def milliseconds(self) -> int:
        return int(time.time() * 1000)

    def create_order(self, symbol, ord_type, side, amount, price=None, params=None):
        self.created.append({"symbol": symbol, "type": ord_type, "side": side,
                             "amount": amount, "price": price, "params": params or {}})
        oid = f"fake-{self._next_id}"
        self._next_id += 1
        self.pending.append({"ordId": oid, "instId": symbol})
        return {"id": oid, "status": None, "symbol": symbol}

    def fetch_order(self, order_id, symbol=None):
        return {"id": order_id, "status": "open", "symbol": symbol}

    def cancel_order(self, order_id, symbol=None):
        self.cancelled.append(order_id)
        self.pending = [o for o in self.pending if o["ordId"] != order_id]
        return {"id": order_id, "status": "canceled", "symbol": symbol}

    # --- REST-поверхность для connector.emergency_stop ---

    def private_get_trade_orders_pending(self, params=None):
        params = params or {}
        inst = params.get("instId")
        data = [o for o in self.pending if inst is None or o["instId"] == inst]
        return {"data": data}

    def private_get_trade_orders_algo_pending(self, params=None):
        return {"data": []}

    def private_post_trade_cancel_batch_orders(self, orders):
        ids = {o["ordId"] for o in orders}
        self.cancelled.extend(sorted(ids))
        self.pending = [o for o in self.pending if o["ordId"] not in ids]
        return {"data": [{"ordId": o["ordId"], "sCode": "0", "sMsg": ""} for o in orders]}

    def private_get_tradingbot_grid_orders_algo_pending(self, params=None):
        return {"data": []}


def main() -> None:
    for path in (TEST_STATE_DB, TEST_RISK_DB):
        if path.exists():
            path.unlink()
    risk.init(TEST_RISK_DB)
    risk.update_equity(10000.0)

    fake = FakeExchange()
    router = OrderRouter(fake, db_path=TEST_STATE_DB)

    # --- 1. Нормальный ордер: полный конвейер ---
    # equity 10000, риск 1% = 100; стоп 2000 ниже -> 5 контрактов,
    # потолок 15% equity: floor(1500/(80000*0.01)) = 1 контракт.
    res = router.place_order(
        "BTC-USDT-SWAP", "buy", "limit", px=80000.0,
        stop_px=78000.0, liq_price=70000.0,
        equity=10000.0, ct_val=0.01, lot_sz=0.01, min_sz=0.01,
    )
    check("1a. ордер принят", res["ok"], str(res))
    check("1b. размер = 1 контракт (потолок 15% equity)", res["sz"] == 1, str(res))
    check("1c. fake получил ровно один create_order", len(fake.created) == 1)
    call = fake.created[0]
    check("1d. параметры вызова корректны",
          call["symbol"] == "BTC-USDT-SWAP" and call["side"] == "buy"
          and call["type"] == "limit" and call["price"] == 80000.0, str(call))
    check("1e. статус дочитан через fetch_order (open, не None)",
          res["status"] == "open", str(res))
    storage = Storage(TEST_STATE_DB)
    rows = storage.get_open_orders()
    check("1f. ордер записан в storage в состоянии live",
          len(rows) == 1 and rows[0]["state"] == "live"
          and rows[0]["ord_id"] == "fake-1" and rows[0]["cl_ord_id"], str(rows))
    st = risk.status()
    check("1g. risk.register_entry отработал (entries_today=1, heat>0)",
          st["entries_today"] == 1 and st["portfolio_heat_pct"] > 0, str(st))

    # --- 5. expTime: params + заголовок, заголовок восстановлен ---
    check("5a. expTime ушёл в params create_order",
          "expTime" in call["params"] and call["params"]["expTime"].isdigit(),
          str(call["params"]))
    check("5c. clOrdId ушёл в params create_order",
          "clOrdId" in call["params"] and len(call["params"]["clOrdId"]) <= 32,
          str(call["params"]))
    check("5b. заголовок expTime восстановлен после вызова",
          "expTime" not in fake.headers, str(fake.headers))

    # --- 2. Заблокированный вход: ордер НЕ выставляется ---
    risk.trip_breaker("selftest: глобальный breaker", scope="global")
    res2 = router.place_order(
        "ETH-USDT-SWAP", "buy", "limit", px=3000.0,
        stop_px=2900.0, liq_price=2500.0,
        equity=10000.0, ct_val=0.1, lot_sz=0.1, min_sz=0.1,
    )
    check("2a. ордер отклонён риск-слоем",
          not res2["ok"] and res2["stage"] == "check_entry_allowed", str(res2))
    check("2b. fake НЕ получил новых вызовов create_order", len(fake.created) == 1)
    risk.reset_breaker("global", by="selftest")

    # --- 3. kill_switch отменяет открытые ордера через emergency_stop ---
    report = risk.kill_switch(flatten=False, by="selftest")
    check("3a. kill_switch отменил ордер bulk-отменой (cancel-batch)",
          fake.cancelled == ["fake-1"] and report["cancelled"] == ["fake-1"]
          and not report["failed"], str(report))
    rows = storage.get_open_orders()
    check("3b. открытых ордеров в storage не осталось", rows == [], str(rows))
    allowed, reason = risk.check_entry_allowed("BTC-USDT-SWAP", "buy")
    check("3c. после kill_switch вход заблокирован", not allowed, reason)
    risk.reset_breaker("kill", by="selftest")

    # --- 4. Throttler: лимит 2 за окно 0.4с, третий вызов ждёт ---
    throttled = OrderRouter(fake, db_path=TEST_STATE_DB,
                            throttle_limit=2, throttle_window_sec=0.4)
    start = time.monotonic()
    throttled.throttler.acquire("X-USDT")
    throttled.throttler.acquire("X-USDT")
    throttled.throttler.acquire("X-USDT")
    elapsed = time.monotonic() - start
    check("4a. третий вызов дождался окна (~0.4с)", elapsed >= 0.35, f"{elapsed:.3f}с")
    start = time.monotonic()
    throttled.throttler.acquire("Y-USDT")  # другой инструмент — без ожидания
    elapsed = time.monotonic() - start
    check("4b. лимит считается per-инструмент", elapsed < 0.2, f"{elapsed:.3f}с")

    # Storage закрывает соединения после каждой операции, но risk может
    # держать своё — даём GC шанс освободить файлы перед удалением
    del storage, router, throttled
    gc.collect()
    for path in (TEST_STATE_DB, TEST_RISK_DB):
        for _ in range(5):
            try:
                path.unlink(missing_ok=True)
                break
            except PermissionError:
                gc.collect()
                time.sleep(0.1)
    print(f"\nSELF-TEST PASSED: {_passed} проверок")


if __name__ == "__main__":
    main()
