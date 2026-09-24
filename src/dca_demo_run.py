"""Живой демо-прогон DCA-бота на OKX Demo Trading (задача p1-dca-demo).

Запуск из корня проекта: .venv\\Scripts\\python.exe -m src.dca_demo_run

Реальный demo-коннектор (load_settings + create_exchange), 2 рыночные
покупки BTC на ~$5 с интервалом 30с, записи в каноническое хранилище
storage.py (data/bot_state.db).
Повторный запуск новых покупок НЕ делает — счётчик dca:buys_done=2
восстанавливается из ws_state (проверка восстановления состояния).

Live-режим отклоняется дважды: проверкой settings.is_demo здесь и
флагом demo=True в DCABot.

Ордера прогона метятся префиксом clOrdId `bottdca` (ORDER-OWNER-TAG,
src/order_owner.py) — их не спутать с рабочим DCA-ботом (`botsdca`).
"""
import logging
import sqlite3

from src import order_owner
from src.config import load_settings
from src.connector import create_exchange
from src.dca_bot import DCABot
from src.order_router import OrderRouter
from src.storage import DB_PATH

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
log = logging.getLogger("okx.dca_demo_run")


def dump_state_db() -> None:
    """Показать следы прогона в data/bot_state.db."""
    with sqlite3.connect(DB_PATH) as conn:
        print("\n--- SELECT orders (DCA BTC/USDT) ---")
        for row in conn.execute(
            "SELECT ord_id, inst_id, side, ord_type, px, sz, state, cl_ord_id,"
            " datetime(create_time, 'unixepoch') AS ts FROM orders"
            " WHERE inst_id = 'BTC/USDT' ORDER BY create_time"
        ):
            print(row)
        print("--- SELECT trades (DCA BTC/USDT) ---")
        for row in conn.execute(
            "SELECT inst_id, trade_id, ord_id, side, px, sz, fee,"
            " datetime(ts, 'unixepoch') AS ts FROM trades"
            " WHERE inst_id = 'BTC/USDT' ORDER BY ts"
        ):
            print(row)
        print("--- SELECT ws_state (dca:*) ---")
        for row in conn.execute(
            "SELECT key, value, datetime(update_time, 'unixepoch') FROM ws_state"
            " WHERE key LIKE 'dca:%' ORDER BY key"
        ):
            print(row)
        print("--- SELECT equity_curve (последние 5) ---")
        for row in conn.execute(
            "SELECT datetime(ts, 'unixepoch') AS ts, total_eq, avail_eq, upl"
            " FROM equity_curve ORDER BY ts DESC LIMIT 5"
        ):
            print(row)


def main() -> None:
    settings = load_settings()
    if not settings.is_demo:
        raise SystemExit("dca_demo_run: OKX_MODE не demo — запуск запрещён")
    log.info("режим: demo (x-simulated-trading), домен %s", settings.domain)

    exchange = create_exchange(settings)
    # data/bot_state.db по умолчанию
    router = OrderRouter(exchange, owner=order_owner.DCA_DEMO_RUN)
    bot = DCABot(
        exchange, router,
        inst_id="BTC/USDT",
        quote_per_buy_usdt=5.0,
        interval_sec=30,
        max_buys=2,
        demo=settings.is_demo,
        owner=order_owner.DCA_DEMO_RUN,
    )
    final = bot.run()
    log.info("финальное состояние: %s (buys_done=%d/%d)",
             final, bot._buys_done, bot.max_buys)
    dump_state_db()


if __name__ == "__main__":
    main()
