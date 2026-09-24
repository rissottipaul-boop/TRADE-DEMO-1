"""Живой тест обрыва WebSocket на demo (задача P1-WSDROP).

Отдельные экземпляры OKXWebSocket (НЕ движок P1-72H — его процесс, сокеты
и файлы-флаги не затрагиваются):
1. public-клиент (books + tickers BTC-USDT) → принудительное закрытие сокета
   (close code 1011) → замер: детект обрыва, reconnect, новый snapshot,
   возобновление потока данных.
2. private-клиент (login + канал orders) → принудительное закрытие →
   reconnect → повторный login → повторная подписка. Верификация по логам
   клиента, БЕЗ выставления ордеров: внешний ордер зашумил бы метрику
   divergences работающего движка (его реконсилятор до рестарта — старой
   версии, см. RECON-EXT в insights/phase1-progress.md).

Ордеров скрипт не ставит. Если понадобятся — только с префиксом clOrdId
`wsd` из реестра владельцев (order_owner.WS_DROP_TEST, ORDER-OWNER-TAG).

Конфигурация клиентов — дефолтная (как у движка): ping 25с, backoff 5→60с,
чтобы замеры отражали боевое поведение. Лимиты OKX соблюдены: 2 новых
соединения + 2 reconnect'а при лимите 3 соед/сек.

Запуск: python -m src.ws_drop_demo_run   (только OKX_MODE=demo)
"""
import asyncio
import json
import logging
import time

from .config import load_settings
from .ws_client import OKXWebSocket, WSConfig, WSCredentials, ws_urls

log = logging.getLogger("ws_drop_demo")

INST_ID = "BTC-USDT"


async def wait_until(pred, timeout: float, desc: str) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if pred():
            return
        await asyncio.sleep(0.05)
    raise TimeoutError(f"таймаут ({timeout:.0f}с): {desc}")


async def run_public(settings) -> dict:
    """Обрыв public-клиента: snapshot → close(1011) → reconnect → новый snapshot."""
    urls = ws_urls(settings.domain, settings.is_demo)
    state = {"dropped": False, "tickers_before": 0, "tickers_after": 0}
    t: dict[str, float] = {}

    def on_message(data: dict) -> None:
        if data.get("arg", {}).get("channel") != "tickers":
            return
        if state["dropped"]:
            state["tickers_after"] += 1
            t.setdefault("first_msg_after", time.monotonic())
        else:
            state["tickers_before"] += 1

    client = OKXWebSocket(WSConfig(url=urls.public), on_message=on_message)
    try:
        await client.subscribe({"channel": "books", "instId": INST_ID})
        await client.subscribe({"channel": "tickers", "instId": INST_ID})
        await client.connect()
        await wait_until(lambda: client.get_book(INST_ID) is not None, 30, "первый snapshot books")
        book = client.get_book(INST_ID)
        seq_before = book.seq_id
        log.info("public: snapshot seqId=%d, bid=%s ask=%s — ждём 10с потока перед обрывом",
                 seq_before, book.best_bid(), book.best_ask())
        await asyncio.sleep(10)

        state["dropped"] = True
        t["drop"] = time.monotonic()
        log.info("public: ПРИНУДИТЕЛЬНОЕ ЗАКРЫТИЕ СОКЕТА (code=1011)")
        await client.ws.close(code=1011, reason="wsdrop-test")

        await wait_until(lambda: not client.connected, 15, "детект обрыва")
        t["detected"] = time.monotonic()
        assert client.get_book(INST_ID) is None, "стакан обязан инвалидироваться при обрыве"
        log.info("public: обрыв детектирован за %.0f мс, стакан инвалидирован",
                 (t["detected"] - t["drop"]) * 1000)

        await wait_until(lambda: client.connected and client.reconnects >= 1, 90, "reconnect")
        t["reconnected"] = time.monotonic()

        await wait_until(
            lambda: (b := client.get_book(INST_ID)) is not None and b.seq_id != seq_before,
            60, "новый snapshot после reconnect",
        )
        t["snapshot"] = time.monotonic()
        book = client.get_book(INST_ID)
        log.info("public: восстановление — reconnect за %.1fс, новый snapshot seqId=%d за %.1fс",
                 t["reconnected"] - t["drop"], book.seq_id, t["snapshot"] - t["drop"])

        await asyncio.sleep(5)  # поток данных после восстановления
        await wait_until(lambda: state["tickers_after"] > 0, 30, "tickers после reconnect")
        return {
            "seq_before": seq_before,
            "seq_after": book.seq_id,
            "detect_ms": round((t["detected"] - t["drop"]) * 1000),
            "reconnect_s": round(t["reconnected"] - t["drop"], 2),
            "new_snapshot_s": round(t["snapshot"] - t["drop"], 2),
            "first_data_s": round(t["first_msg_after"] - t["drop"], 2),
            "tickers_before_10s": state["tickers_before"],
            "tickers_after_5s": state["tickers_after"],
            "reconnects": client.reconnects,
        }
    finally:
        await client.close()


async def run_private(settings) -> dict:
    """Обрыв private-клиента: login + orders → close(1011) → reconnect → login → resubscribe.

    Верификация по логам клиента (WS login OK / subscribe confirmed) —
    ордера не выставляются, чтобы не зашумлять divergences работающего движка.
    """
    urls = ws_urls(settings.domain, settings.is_demo)
    logs: list[str] = []

    class Capture(logging.Handler):
        def emit(self, record):
            logs.append(record.getMessage())

    ws_logger = logging.getLogger("okx.ws")
    handler = Capture()
    ws_logger.addHandler(handler)
    try:
        client = OKXWebSocket(
            WSConfig(url=urls.private),
            on_message=lambda d: None,
            credentials=WSCredentials(settings.api_key, settings.secret, settings.passphrase),
        )
        try:
            await client.subscribe({"channel": "orders", "instType": "SPOT"})
            await client.connect()
            await wait_until(lambda: client.connected, 20, "private connect")
            await wait_until(lambda: any("WS login OK" in m for m in logs), 10, "первый login")
            log.info("private: login OK, подписка orders подтверждена — обрыв")
            logs.clear()

            t_drop = time.monotonic()
            await client.ws.close(code=1011, reason="wsdrop-test")

            await wait_until(lambda: not client.connected, 15, "private: детект обрыва")
            await wait_until(lambda: client.connected and client.reconnects >= 1, 90, "private reconnect")
            t_reconnected = time.monotonic()

            await wait_until(lambda: any("WS login OK" in m for m in logs), 15, "повторный login")
            t_login = time.monotonic()
            await wait_until(
                lambda: any("subscribe confirmed" in m and "orders" in m for m in logs),
                15, "повторная подписка orders",
            )
            t_resub = time.monotonic()
            log.info("private: восстановление — reconnect %.1fс, login %.1fс, resubscribe %.1fс",
                     t_reconnected - t_drop, t_login - t_drop, t_resub - t_drop)
            return {
                "reconnect_s": round(t_reconnected - t_drop, 2),
                "login_s": round(t_login - t_drop, 2),
                "orders_resubscribed_s": round(t_resub - t_drop, 2),
                "reconnects": client.reconnects,
            }
        finally:
            await client.close()
    finally:
        ws_logger.removeHandler(handler)


async def main() -> None:
    settings = load_settings()
    if not settings.is_demo:
        raise SystemExit("Тест только на demo (OKX_MODE=demo)")
    log.info("Режим %s, домен %s. Движок P1-72H не затрагивается: отдельные соединения, без ордеров",
             settings.mode, settings.domain)

    pub = await run_public(settings)
    log.info("PUBLIC итог: %s", json.dumps(pub, ensure_ascii=False))
    prv = await run_private(settings)
    log.info("PRIVATE итог: %s", json.dumps(prv, ensure_ascii=False))

    print("\n=== P1-WSDROP live demo ===")
    print(json.dumps({"public": pub, "private": prv}, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        log.info("Остановлено пользователем")
