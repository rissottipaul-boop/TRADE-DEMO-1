"""Self-test DCA-бота src/dca_bot.py (задача p1-dca-demo).

Запуск из корня проекта: .venv\\Scripts\\python.exe -m src.dca_selftest

Реальных запросов к бирже нет — FakeExchange. Отдельные temp-файлы БД
(data/dca_selftest.db, data/risk_dca_selftest.db), боевые data/bot_state.db и
risk_state.db не затрагиваются. Файлы удаляются после прогона.

Сценарии:
1. Бот с max_buys=1 делает ровно 1 покупку (ордер + trade + snapshot equity).
2. Пересоздание DCABot между покупками: счётчик продолжается (1 -> 2 -> 3),
   новых покупок сверх max_buys нет.
3. После max_buys=3 бот останавливается: повторный run() не покупает.
4. Отказ риск-слоя (trip_breaker) -> бот в PAUSED, ордеров нет.
"""
import gc
import logging
import sqlite3
import sys
import time
from pathlib import Path

from src import risk
from src.dca_bot import DCABot
from src.order_router import OrderRouter
from src.storage import Storage

logging.basicConfig(level=logging.CRITICAL)  # логи бота/роутера/риска не засоряют вывод

TEST_STATE_DB = Path("data/dca_selftest.db")
TEST_RISK_DB = Path("data/risk_dca_selftest.db")
_passed = 0


def check(name: str, cond: bool, detail: str = "") -> None:
    global _passed
    if not cond:
        print(f"FAIL: {name} {detail}")
        sys.exit(1)
    _passed += 1
    print(f"OK: {name}")


class FakeExchange:
    """Mock CCXT okx: fetch_ticker/fetch_balance/create_order без сети."""

    def __init__(self) -> None:
        self.created: list[dict] = []
        self.headers: dict[str, str] = {}
        self._next_id = 1
        self.price = 100000.0
        self.usdt_total = 100.0

    def milliseconds(self) -> int:
        return int(time.time() * 1000)

    def fetch_ticker(self, symbol):
        return {"symbol": symbol, "last": self.price}

    def fetch_balance(self):
        return {"USDT": {"free": self.usdt_total, "used": 0.0, "total": self.usdt_total},
                "total": {"USDT": self.usdt_total}}

    def create_order(self, symbol, ord_type, side, amount, price=None, params=None):
        self.created.append({"symbol": symbol, "type": ord_type, "side": side,
                             "amount": amount, "price": price, "params": params or {}})
        oid = f"fake-dca-{self._next_id}"
        self._next_id += 1
        return {"id": oid, "status": "closed", "symbol": symbol}

    def fetch_order(self, order_id, symbol=None):
        return {"id": order_id, "status": "closed", "symbol": symbol}

    def private_get_account_config(self, params=None):  # режим аккаунта для tdMode (SPOT-TDMODE)
        return {"code": "0", "data": [{"acctLv": "1", "autoLoan": False}]}


def make_bot(exchange: FakeExchange, max_buys: int) -> DCABot:
    router = OrderRouter(exchange, db_path=TEST_STATE_DB)
    return DCABot(
        exchange, router,
        quote_per_buy_usdt=5.0, interval_sec=1, max_buys=max_buys,
        demo=True, storage=router.storage,
    )


def main() -> None:
    for path in (TEST_STATE_DB, TEST_RISK_DB):
        if path.exists():
            path.unlink()
    risk.init(TEST_RISK_DB)
    exchange = FakeExchange()

    # 1. Первая покупка
    bot = make_bot(exchange, max_buys=1)
    final = bot.run()
    check("1 покупка при max_buys=1", len(exchange.created) == 1,
          f"created={len(exchange.created)}")
    check("бот завершился в DONE", final == DCABot.STATE_DONE, final)
    order = exchange.created[0]
    check("ордер market buy BTC/USDT", order["symbol"] == "BTC/USDT"
          and order["type"] == "market" and order["side"] == "buy", str(order))
    check("sz = quote/price", abs(order["amount"] - 5.0 / 100000.0) < 1e-12,
          f"amount={order['amount']}")

    # 2. Пересоздание бота: счётчик продолжается
    bot2 = make_bot(exchange, max_buys=2)
    check("счётчик восстановлен (1/2)", bot2._buys_done == 1, str(bot2._buys_done))
    bot2.run()
    check("всего 2 покупки", len(exchange.created) == 2, str(len(exchange.created)))
    bot3 = make_bot(exchange, max_buys=3)
    check("счётчик восстановлен (2/3)", bot3._buys_done == 2, str(bot3._buys_done))
    final3 = bot3.run()
    check("всего 3 покупки", len(exchange.created) == 3, str(len(exchange.created)))
    check("бот завершился в DONE после 3", final3 == DCABot.STATE_DONE, final3)

    # 3. Персистентность в БД
    conn = sqlite3.connect(TEST_STATE_DB)
    orders = conn.execute(
        "SELECT inst_id, side, ord_type, state FROM orders").fetchall()
    trades = conn.execute("SELECT inst_id, side, px, sz FROM trades").fetchall()
    snapshots = conn.execute("SELECT COUNT(*) FROM equity_curve").fetchone()[0]
    conn.close()
    check("3 ордера в storage", len(orders) == 3, str(orders))
    check("3 сделки в storage", len(trades) == 3, str(trades))
    check("equity-снимков >= 3", snapshots >= 3, str(snapshots))
    storage = Storage(TEST_STATE_DB)
    check("ws_state: dca:buys_done=3",
          storage.get_ws_state("dca:buys_done") == "3")
    check("ws_state: dca:state=DONE",
          storage.get_ws_state("dca:state") == DCABot.STATE_DONE)

    # 3b. RISK-DCA-SLOT: покупки освобождают слот risk_open_risk и не трогают
    # серии убытков (register_spot_buy вместо хака record_pnl(inst, 0))
    st = risk.status()
    check("слот open_risk пуст после покупок", st["open_risk"] == [], str(st["open_risk"]))
    check("покупки не тронули серию убытков", st["global_loss_streak"] == 0,
          str(st["global_loss_streak"]))
    check("покупки посчитаны в entries_today (3)", st["entries_today"] == 3,
          str(st["entries_today"]))

    # 4. Повторный запуск с тем же max_buys: новых покупок нет
    bot4 = make_bot(exchange, max_buys=3)
    final4 = bot4.run()
    check("перезапуск без новых покупок", len(exchange.created) == 3,
          str(len(exchange.created)))
    check("перезапуск завершился в DONE", final4 == DCABot.STATE_DONE, final4)

    # 5. Отказ риск-слоя -> пауза без ордеров
    risk.trip_breaker("selftest", scope="global")
    bot5 = make_bot(exchange, max_buys=5)
    final5 = bot5.run()
    check("при breaker бот в PAUSED", final5 == DCABot.STATE_PAUSED, final5)
    check("при breaker ордеров нет", len(exchange.created) == 3,
          str(len(exchange.created)))
    risk.reset_breaker("global", by="selftest")

    # Уборка temp-БД
    del bot, bot2, bot3, bot4, bot5, storage
    gc.collect()
    for path in (TEST_STATE_DB, TEST_RISK_DB):
        try:
            path.unlink()
        except FileNotFoundError:
            pass
    print(f"\nDCA SELF-TEST ПРОЙДЕН ({_passed} проверок), temp-БД удалены")


if __name__ == "__main__":
    main()
