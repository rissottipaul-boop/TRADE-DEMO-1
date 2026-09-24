"""Reconnect-логика WS-клиента (задача P1-WSDROP).

Цепочка «разрыв → reconnect → login → повтор подписок → новый snapshot»
проверяется против фейкового OKX-сервера на loopback (websockets.asyncio.server):
внешней сети не нужно, внешние лимиты OKX (3 соед/сек, 480 запросов/час) не затрагиваются.
"""
import asyncio
import json
import socket
import unittest

import websockets
import websockets.asyncio.server

from src.ws_client import OKXWebSocket, OrderBook, WSConfig, WSCredentials

BOOKS_SUB = {"channel": "books", "instId": "BTC-USDT"}
TICKERS_SUB = {"channel": "tickers", "instId": "BTC-USDT"}
ORDERS_SUB = {"channel": "orders", "instType": "SPOT"}


def free_port() -> int:
    """Свободный порт на loopback (у websockets Server нет публичного .sockets)."""
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def make_config(url: str) -> WSConfig:
    """Быстрые таймауты для теста; ping 30с, чтобы heartbeat не мешал."""
    return WSConfig(
        url=url, ping_interval=30.0, reconnect_delay=0.1,
        max_reconnect_delay=0.2, login_timeout=5.0, connect_timeout=5.0,
    )


def snapshot_msg(inst_id: str, seq_id: int) -> str:
    return json.dumps({
        "arg": {"channel": "books", "instId": inst_id},
        "action": "snapshot",
        "data": [{
            "bids": [["100", "1", "0", "1"], ["99", "2", "0", "1"]],
            "asks": [["101", "1", "0", "1"], ["102", "2", "0", "1"]],
            "seqId": seq_id, "prevSeqId": -1, "checksum": 0, "ts": "1",
        }],
    })


def ticker_msg(inst_id: str, last: str) -> str:
    return json.dumps({
        "arg": {"channel": "tickers", "instId": inst_id},
        "data": [{"instId": inst_id, "last": last}],
    })


def subs_key(args: list[dict]) -> set[str]:
    return {json.dumps(a, sort_keys=True) for a in args}


async def wait_until(pred, timeout: float = 5.0, interval: float = 0.02) -> bool:
    deadline = asyncio.get_running_loop().time() + timeout
    while asyncio.get_running_loop().time() < deadline:
        if pred():
            return True
        await asyncio.sleep(interval)
    return False


class FakeOKXServer:
    """Минимальный протокол OKX WS: login, subscribe, snapshot; обрыв соединения №1.

    Соединение №1: после отправки snapshot закрывается с кодом 1011 (симуляция обрыва).
    Каждое следующее соединение шлёт snapshot со seqId = 100 * номер соединения.
    """

    def __init__(self, drop_delay: float = 0.05):
        self.drop_delay = drop_delay
        self.connections = 0
        self.logins = 0
        # подписки по каждому соединению: список списков args
        self.subscribes: list[list[dict]] = []
        self.port = 0
        self._server = None

    async def __aenter__(self):
        self.port = free_port()
        self._server = await websockets.asyncio.server.serve(self._handler, "127.0.0.1", self.port)
        return self

    async def __aexit__(self, *exc):
        self._server.close()
        await self._server.wait_closed()

    @property
    def url(self) -> str:
        return f"ws://127.0.0.1:{self.port}"

    async def _handler(self, ws) -> None:
        self.connections += 1
        conn_no = self.connections
        conn_subs: list[dict] = []
        # регистрируем список сразу (дальше он мутируется через extend):
        # иначе при живом соединении №2 тест не увидел бы его подписки
        self.subscribes.append(conn_subs)
        try:
            async for raw in ws:
                if raw == "ping":
                    await ws.send("pong")
                    continue
                data = json.loads(raw)
                op = data.get("op")
                if op == "login":
                    self.logins += 1
                    await ws.send(json.dumps({"event": "login", "code": "0", "msg": ""}))
                elif op == "subscribe":
                    conn_subs.extend(data["args"])
                    for arg in data["args"]:
                        await ws.send(json.dumps({"event": "subscribe", "arg": arg, "connId": "test"}))
                    for arg in data["args"]:
                        if arg.get("channel") == "books":
                            await ws.send(snapshot_msg(arg["instId"], seq_id=100 * conn_no))
                        elif arg.get("channel") == "tickers" and conn_no > 1:
                            # после reconnect — данные снова текут
                            await ws.send(ticker_msg(arg["instId"], "100"))
                    if conn_no == 1:
                        await asyncio.sleep(self.drop_delay)
                        await ws.close(code=1011, reason="simulated drop")
        except websockets.exceptions.ConnectionClosed:
            pass


class ReconnectPrivateTest(unittest.IsolatedAsyncioTestCase):
    """Разрыв → reconnect → login → повтор подписок → новый snapshot (private-клиент)."""

    async def test_drop_reconnect_login_resubscribe_new_snapshot(self):
        received: list[dict] = []
        async with FakeOKXServer() as server:
            client = OKXWebSocket(
                make_config(server.url),
                on_message=received.append,
                credentials=WSCredentials("key", "secret", "pass"),
            )
            try:
                # подписки до connect: запоминаются, отправятся после login
                await client.subscribe(BOOKS_SUB)
                await client.subscribe(TICKERS_SUB)
                await client.subscribe(ORDERS_SUB)
                await client.connect()

                # 1) первое подключение: login + подписки + snapshot
                self.assertTrue(
                    await wait_until(lambda: (b := client.get_book("BTC-USDT")) is not None and b.seq_id == 100),
                    "нет первого snapshot",
                )
                self.assertTrue(client.connected)
                self.assertEqual(server.logins, 1)

                # 2) сервер обрывает соединение (1011): клиент замечает, стакан инвалидируется
                self.assertTrue(await wait_until(lambda: not client.connected), "клиент не заметил разрыв")
                self.assertIsNone(client.get_book("BTC-USDT"))

                # 3) reconnect → повторный login → повтор подписок → новый snapshot
                self.assertTrue(
                    await wait_until(
                        lambda: client.connected and client.reconnects >= 1
                        and (b := client.get_book("BTC-USDT")) is not None and b.seq_id == 200
                    ),
                    "нет восстановления после разрыва",
                )

                # состояние не потеряно: все три подписки на месте и повторены на новом соединении
                self.assertEqual(len(client._subscriptions), 3)
                self.assertEqual(server.logins, 2)
                self.assertEqual(server.connections, 2)
                self.assertEqual(len(server.subscribes), 2)
                expected = subs_key([BOOKS_SUB, TICKERS_SUB, ORDERS_SUB])
                for conn_subs in server.subscribes:
                    self.assertEqual(subs_key(conn_subs), expected)

                # on_message работает и после reconnect (ticker пришёл только на соединении №2)
                self.assertTrue(
                    await wait_until(lambda: any(
                        m.get("arg", {}).get("channel") == "tickers" for m in received
                    )),
                    "после reconnect данные не dispatch'атся",
                )
                self.assertGreaterEqual(client.reconnects, 1)
            finally:
                await client.close()


class ReconnectPublicTest(unittest.IsolatedAsyncioTestCase):
    """Тот же разрыв для public-клиента: reconnect без login, подписки повторяются."""

    async def test_public_reconnect_without_login(self):
        async with FakeOKXServer() as server:
            client = OKXWebSocket(make_config(server.url), on_message=lambda d: None)
            try:
                await client.subscribe(BOOKS_SUB)
                await client.connect()

                self.assertTrue(
                    await wait_until(lambda: (b := client.get_book("BTC-USDT")) is not None and b.seq_id == 100),
                    "нет первого snapshot",
                )
                self.assertTrue(await wait_until(lambda: not client.connected), "клиент не заметил разрыв")
                self.assertIsNone(client.get_book("BTC-USDT"))

                self.assertTrue(
                    await wait_until(
                        lambda: client.connected
                        and (b := client.get_book("BTC-USDT")) is not None and b.seq_id == 200
                    ),
                    "нет восстановления после разрыва",
                )
                self.assertEqual(server.logins, 0)  # без credentials login не отправляется
                self.assertEqual(len(server.subscribes), 2)
                for conn_subs in server.subscribes:
                    self.assertEqual(subs_key(conn_subs), subs_key([BOOKS_SUB]))
            finally:
                await client.close()


class NoticeReconnectTest(unittest.IsolatedAsyncioTestCase):
    """notice 64008 (плановый разрыв биржи) → немедленный reconnect без backoff."""

    async def test_notice_64008_closes_socket_and_sets_fast_reconnect(self):
        client = OKXWebSocket(make_config("ws://unused"), on_message=lambda d: None)
        closed: list[bool] = []

        class FakeWS:
            async def close(self):
                closed.append(True)

        client.ws = FakeWS()
        await client._handle_message(json.dumps({"event": "notice", "code": "64008"}))
        self.assertTrue(client._reconnect_now)
        self.assertEqual(closed, [True])


class DisconnectStateTest(unittest.TestCase):
    """_on_disconnect: стаканы инвалидированы, ресинки сброшены, подписки сохранены."""

    def test_disconnect_invalidates_books_keeps_subscriptions(self):
        client = OKXWebSocket(make_config("ws://unused"), on_message=lambda d: None)
        client._subscriptions.extend([BOOKS_SUB, ORDERS_SUB])
        book = client._books.setdefault("BTC-USDT", OrderBook())
        book.apply_snapshot([["100", "1", "0", "1"]], [["101", "1", "0", "1"]], seq_id=1)
        client._resyncing.add("BTC-USDT")
        client._connected.set()
        self.assertIsNotNone(client.get_book("BTC-USDT"))

        client._on_disconnect()

        self.assertFalse(client.connected)
        self.assertIsNone(client.get_book("BTC-USDT"))  # до нового snapshot стакан недоступен
        self.assertEqual(client._resyncing, set())
        self.assertIsNone(client.ws)
        self.assertEqual(client._subscriptions, [BOOKS_SUB, ORDERS_SUB])


if __name__ == "__main__":
    unittest.main()
