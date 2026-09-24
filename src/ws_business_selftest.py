r"""Самопроверка приёма свечей через business-эндпоинт (задача ARCH-DEDUP).

Канон: свечи candle* идут только через ws_urls(domain, demo).business —
на public-эндпоинте OKX отклоняет их ошибкой 60018 (insights/okx-api.md §4).
Клиент — канонический OKXWebSocket из src/ws_client.py.

Запуск из корня проекта: .venv\Scripts\python.exe -m src.ws_business_selftest
Реальная сеть, demo-WS. Ждём до 60 сек первое сообщение candle1m по BTC-USDT.
"""
import asyncio
import sys
import time

from src.config import load_settings
from src.ws_client import OKXWebSocket, WSConfig, ws_urls

TIMEOUT_SEC = 60


async def main() -> int:
    settings = load_settings()
    urls = ws_urls(settings.domain, settings.is_demo)
    if not urls.business.endswith("/business"):
        print(f"FAIL: business URL некорректен: {urls.business}")
        return 1
    print(f"business endpoint: {urls.business}")

    received: list[dict] = []

    def on_message(data: dict) -> None:
        if data.get("arg", {}).get("channel") == "candle1m" and "data" in data:
            received.append(data)

    ws = OKXWebSocket(WSConfig(url=urls.business), on_message=on_message)
    await ws.connect()
    if not ws.connected:
        print("FAIL: не удалось подключиться к business-эндпоинту")
        await ws.close()
        return 1
    await ws.subscribe({"channel": "candle1m", "instId": "BTC-USDT"})

    deadline = time.time() + TIMEOUT_SEC
    while time.time() < deadline and not received:
        await asyncio.sleep(1)
    await ws.close()

    if not received:
        print(f"FAIL: за {TIMEOUT_SEC} сек не пришло ни одного candle1m")
        return 1
    candle = received[0]["data"][0]
    print(f"OK: candle1m получена: instId={received[0]['arg']['instId']} "
          f"o={candle[1]} h={candle[2]} l={candle[3]} c={candle[4]}")
    print("WS BUSINESS SELF-TEST ПРОЙДЕН")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
