"""WebSocket-клиент OKX для Фазы 1.

Практики из insights/okx-api.md + проверено на живом API 2026-09-24:
- ping/pong <30 сек (иначе разрыв)
- notice 64008 (плановый разрыв через 60 сек) → переподключаемся сразу
- reconnect: login (private) + повтор подписок; стакан инвалидируется до нового snapshot
- action (snapshot/update) лежит на ВЕРХНЕМ уровне сообщения, не внутри data[]
- целостность стакана — цепочка seqId/prevSeqId: разрыв цепочки → ресинк.
  checksum в канале books сейчас = 0 и на demo, и на live; если биржа снова
  начнёт его слать — проверяем по ИСХОДНЫМ строкам цен/объёмов
- лимит 480 subscribe/login запросов в час на соединение
- лимит 3 новых соединения/сек на IP
"""
import asyncio
import base64
import hashlib
import hmac
import json
import logging
import time
import zlib
from dataclasses import dataclass, field
from typing import Any, Callable, NamedTuple, Optional

import websockets
# websockets 17: `websockets.exceptions` — не ленивый атрибут пакета;
# без явного импорта доступ к нему падает с AttributeError, если никто
# другой (напр. websockets.asyncio.server) не импортировал его раньше.
# Нужен в except-клаузах subscribe/unsubscribe при вызове до connect().
import websockets.exceptions

log = logging.getLogger("okx.ws")


@dataclass
class WSConfig:
    url: str
    ping_interval: float = 25.0  # <30 сек по докам OKX
    reconnect_delay: float = 5.0
    max_reconnect_delay: float = 60.0
    login_timeout: float = 10.0
    connect_timeout: float = 15.0


@dataclass(frozen=True)
class WSCredentials:
    api_key: str = field(repr=False)
    secret: str = field(repr=False)
    passphrase: str = field(repr=False)


def login_args(creds: WSCredentials, ts: Optional[str] = None) -> dict:
    """Аргументы op=login: sign = Base64(HMAC_SHA256(secret, ts + 'GET' + '/users/self/verify'))."""
    ts = ts or str(int(time.time()))
    prehash = f"{ts}GET/users/self/verify"
    sign = base64.b64encode(
        hmac.new(creds.secret.encode(), prehash.encode(), hashlib.sha256).digest()
    ).decode()
    return {"apiKey": creds.api_key, "passphrase": creds.passphrase, "timestamp": ts, "sign": sign}


def okx_checksum(bids: list[tuple[str, str]], asks: list[tuple[str, str]]) -> int:
    """CRC32 (signed int32) от "bid1px:bid1sz:ask1px:ask1sz:..." по 25 уровням.

    Строки — ровно как прислала биржа: "0.10" и "0.1" дают разный checksum.
    """
    parts = []
    for i in range(25):
        if i < len(bids):
            parts.append(f"{bids[i][0]}:{bids[i][1]}")
        if i < len(asks):
            parts.append(f"{asks[i][0]}:{asks[i][1]}")
    value = zlib.crc32(":".join(parts).encode()) & 0xFFFFFFFF
    return value - 0x100000000 if value > 0x7FFFFFFF else value


@dataclass
class OrderBook:
    """Локальный стакан: snapshot + дельты по цепочке seqId/prevSeqId.

    Уровни хранятся как цена(float) → (исходная строка цены, исходная строка объёма).
    """

    bids: dict[float, tuple[str, str]] = field(default_factory=dict)
    asks: dict[float, tuple[str, str]] = field(default_factory=dict)
    seq_id: int = -1
    last_update: float = 0.0
    valid: bool = False

    def apply_snapshot(self, bids: list, asks: list, seq_id: int, checksum: int = 0) -> bool:
        self.bids = {float(px): (px, sz) for px, sz, *_ in bids}
        self.asks = {float(px): (px, sz) for px, sz, *_ in asks}
        self.seq_id = seq_id
        self.last_update = time.time()
        self.valid = self._checksum_ok(checksum)
        return self.valid

    def apply_update(self, bids: list, asks: list, seq_id: int, prev_seq_id: int, checksum: int = 0) -> bool:
        """Применяет дельту. False — книга рассинхронизирована, нужен новый snapshot.

        prevSeqId обязан совпасть с seqId предыдущего сообщения. Это покрывает и
        heartbeat без изменений (seqId == prevSeqId), и сброс seqId после
        техработ (новый seqId меньше старого, но prevSeqId всё равно совпадает).
        """
        if not self.valid:
            return False
        if prev_seq_id != self.seq_id:
            log.warning("Разрыв цепочки seqId: ждали prevSeqId=%d, пришёл %d", self.seq_id, prev_seq_id)
            self.valid = False
            return False

        for levels, side in ((bids, self.bids), (asks, self.asks)):
            for px, sz, *_ in levels:
                key = float(px)
                if float(sz) == 0:
                    side.pop(key, None)
                else:
                    side[key] = (px, sz)

        self.seq_id = seq_id
        self.last_update = time.time()
        self.valid = self._checksum_ok(checksum)
        return self.valid

    def invalidate(self) -> None:
        self.valid = False

    def _checksum_ok(self, expected: int) -> bool:
        if expected == 0:  # биржа сейчас не присылает checksum (0 и на demo, и на live)
            return True
        computed = okx_checksum(self.top_bids(25), self.top_asks(25))
        if computed != expected:
            log.warning("Checksum mismatch: computed=%d expected=%d", computed, expected)
            return False
        return True

    def top_bids(self, n: int) -> list[tuple[str, str]]:
        return [self.bids[k] for k in sorted(self.bids, reverse=True)[:n]]

    def top_asks(self, n: int) -> list[tuple[str, str]]:
        return [self.asks[k] for k in sorted(self.asks)[:n]]

    def best_bid(self) -> Optional[float]:
        return max(self.bids) if self.bids else None

    def best_ask(self) -> Optional[float]:
        return min(self.asks) if self.asks else None


class WSUrls(NamedTuple):
    public: str
    private: str
    business: str


# REST-домен → WS-хосты (live, demo). Проверены: www.okx.com (2026-09-24).
# EEA/US — по докам OKX, сверить при смене региона.
_WS_HOSTS = {
    "www.okx.com": ("ws.okx.com", "wspap.okx.com"),
    "openapi.okx.com": ("ws.okx.com", "wspap.okx.com"),
    "eea.okx.com": ("wseea.okx.com", "wseeapap.okx.com"),
    "us.okx.com": ("wsus.okx.com", "wsuspap.okx.com"),
    "app.okx.com": ("wsus.okx.com", "wsuspap.okx.com"),
}


def ws_urls(domain: str, demo: bool) -> WSUrls:
    live_host, demo_host = _WS_HOSTS.get(domain, _WS_HOSTS["www.okx.com"])
    if domain not in _WS_HOSTS:
        log.warning("Неизвестный домен %s — WS по умолчанию global", domain)
    base = f"wss://{demo_host if demo else live_host}:8443/ws/v5"
    return WSUrls(f"{base}/public", f"{base}/private", f"{base}/business")


class OKXWebSocket:
    """WebSocket-клиент с авто-reconnect, heartbeat и (опционально) login."""

    def __init__(
        self,
        config: WSConfig,
        on_message: Callable[[dict], Any],
        credentials: Optional[WSCredentials] = None,
    ):
        self.config = config
        self.on_message = on_message
        self.credentials = credentials
        self.ws = None
        self._running = False
        self._task: Optional[asyncio.Task] = None
        self._connected = asyncio.Event()
        self._subscriptions: list[dict] = []
        self._books: dict[str, OrderBook] = {}  # instId -> OrderBook
        self._resyncing: set[str] = set()
        self._reconnect_now = False
        self._reconnect_delay = config.reconnect_delay
        self.reconnects = 0  # для мониторинга

    @property
    def connected(self) -> bool:
        return self._connected.is_set()

    async def connect(self) -> None:
        """Запускает фоновый цикл и ждёт первого подключения (с таймаутом)."""
        self._running = True
        self._task = asyncio.create_task(self._connect_loop())
        try:
            await asyncio.wait_for(self._connected.wait(), self.config.connect_timeout)
        except asyncio.TimeoutError:
            log.warning("WS %s: нет подключения за %.0fс, продолжаем попытки в фоне",
                        self.config.url, self.config.connect_timeout)

    async def _connect_loop(self) -> None:
        """Цикл подключения с reconnect и экспоненциальным backoff."""
        while self._running:
            try:
                async with websockets.connect(
                    self.config.url, ping_interval=None, close_timeout=5
                ) as ws:
                    self.ws = ws
                    log.info("WS connected: %s", self.config.url)
                    if self.credentials:
                        await self._login(ws)
                    if self._subscriptions:
                        await self._send_op("subscribe", self._subscriptions)
                    self._reconnect_delay = self.config.reconnect_delay
                    self._connected.set()

                    tasks = [
                        asyncio.create_task(self._heartbeat_loop(ws)),
                        asyncio.create_task(self._receive_loop(ws)),
                    ]
                    try:
                        done, _ = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
                        for t in done:
                            if not t.cancelled() and t.exception():
                                log.warning("WS разрыв (%s): %r", self.config.url, t.exception())
                    finally:
                        for t in tasks:
                            t.cancel()
                        await asyncio.gather(*tasks, return_exceptions=True)
            except asyncio.CancelledError:
                raise
            except Exception as e:
                log.error("WS error (%s): %s", self.config.url, e)
            finally:
                self._on_disconnect()

            if not self._running:
                break
            self.reconnects += 1
            if self._reconnect_now:
                self._reconnect_now = False
                continue
            log.info("WS reconnect in %.1fs", self._reconnect_delay)
            await asyncio.sleep(self._reconnect_delay)
            self._reconnect_delay = min(self._reconnect_delay * 2, self.config.max_reconnect_delay)

    def _on_disconnect(self) -> None:
        self.ws = None
        self._connected.clear()
        self._resyncing.clear()
        for book in self._books.values():
            book.invalidate()  # до нового snapshot стаканом пользоваться нельзя

    async def _login(self, ws) -> None:
        await ws.send(json.dumps({"op": "login", "args": [login_args(self.credentials)]}))
        deadline = time.monotonic() + self.config.login_timeout
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise RuntimeError("WS login: нет ответа")
            data = json.loads(await asyncio.wait_for(ws.recv(), remaining))
            if data.get("event") == "login" and data.get("code") == "0":
                log.info("WS login OK")
                return
            if data.get("event") in ("login", "error"):
                # 60009 — ошибка логина; 50119 — ключ из другого региона
                raise RuntimeError(f"WS login failed: code={data.get('code')} msg={data.get('msg')}")

    async def _heartbeat_loop(self, ws) -> None:
        """Шлёт ping каждые ping_interval секунд."""
        while True:
            await asyncio.sleep(self.config.ping_interval)
            await ws.send("ping")

    async def _receive_loop(self, ws) -> None:
        """Читает сообщения; тишина дольше двух интервалов пинга — reconnect."""
        timeout = self.config.ping_interval * 2
        while True:
            try:
                msg = await asyncio.wait_for(ws.recv(), timeout=timeout)
            except asyncio.TimeoutError:
                log.warning("No message in %.0fs, reconnecting", timeout)
                return
            await self._handle_message(msg)

    async def _handle_message(self, raw: str) -> None:
        if raw == "pong":
            return

        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            log.warning("Non-JSON message: %s", raw[:200])
            return

        event = data.get("event")
        if event == "notice" and data.get("code") == "64008":
            log.warning("Планируемый разрыв (64008) — переподключаемся сейчас")
            self._reconnect_now = True
            if self.ws:
                await self.ws.close()
            return
        if event == "error":
            log.error("WS error event: code=%s msg=%s", data.get("code"), data.get("msg"))
            return
        if event in ("subscribe", "unsubscribe"):
            log.info("%s confirmed: %s", event, data.get("arg"))
            return
        if event in ("channel-conn-count", "channel-conn-count-error"):
            log.warning("WS %s: %s", event, data)
            return

        if "arg" in data and "data" in data:
            arg = data["arg"]
            if arg.get("channel") == "books" and arg.get("instId"):
                self._handle_book_update(arg["instId"], data)
            else:
                self._dispatch(data)

    def _dispatch(self, data: dict) -> None:
        """on_message может быть sync или async; его исключения не рвут соединение."""
        try:
            result = self.on_message(data)
            if asyncio.iscoroutine(result):
                asyncio.create_task(result)
        except Exception:
            log.exception("on_message упал на канале %s", data.get("arg", {}).get("channel"))

    def _handle_book_update(self, inst_id: str, data: dict) -> None:
        book = self._books.setdefault(inst_id, OrderBook())
        action = data.get("action")
        for item in data["data"]:
            bids, asks = item.get("bids", []), item.get("asks", [])
            seq_id = int(item.get("seqId", -1))
            checksum = int(item.get("checksum", 0) or 0)

            if action == "snapshot":
                self._resyncing.discard(inst_id)
                if book.apply_snapshot(bids, asks, seq_id, checksum):
                    log.info("Book snapshot: %s, bids=%d, asks=%d", inst_id, len(bids), len(asks))
                else:
                    self._schedule_resync(inst_id)
            elif not book.apply_update(bids, asks, seq_id, int(item.get("prevSeqId", -1)), checksum):
                self._schedule_resync(inst_id)
                return

    def _schedule_resync(self, inst_id: str) -> None:
        if inst_id in self._resyncing:
            return  # ресинк уже идёт — ждём snapshot
        self._resyncing.add(inst_id)
        log.warning("Стакан %s рассинхронизирован — запрашиваем snapshot", inst_id)
        asyncio.create_task(self._resync_book(inst_id))

    async def _resync_book(self, inst_id: str) -> None:
        """Переподписка на books: биржа пришлёт свежий snapshot."""
        args = [{"channel": "books", "instId": inst_id}]
        try:
            await self._send_op("unsubscribe", args)
            await asyncio.sleep(0.5)
            await self._send_op("subscribe", args)
        except Exception as e:
            self._resyncing.discard(inst_id)
            log.error("Ресинк %s не удался: %s", inst_id, e)

    async def subscribe(self, args: dict) -> None:
        """Подписка; запоминается и повторяется после reconnect."""
        if args not in self._subscriptions:
            self._subscriptions.append(args)
        try:
            await self._send_op("subscribe", [args])
        except (ConnectionError, websockets.exceptions.ConnectionClosed):
            pass  # подписка уйдёт после reconnect

    async def unsubscribe(self, args: dict) -> None:
        if args in self._subscriptions:
            self._subscriptions.remove(args)
        try:
            await self._send_op("unsubscribe", [args])
        except (ConnectionError, websockets.exceptions.ConnectionClosed):
            pass

    async def _send_op(self, op: str, args: list[dict]) -> None:
        if not self.ws:
            raise ConnectionError("WS не подключён")
        await self.ws.send(json.dumps({"op": op, "args": args}))
        log.info("%s sent: %s", op, args)

    def get_book(self, inst_id: str) -> Optional[OrderBook]:
        """Стакан или None, если он не синхронизирован (нет snapshot / разрыв)."""
        book = self._books.get(inst_id)
        return book if book and book.valid else None

    async def close(self) -> None:
        self._running = False
        if self.ws:
            await self.ws.close()
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass


# URL для демо и live (global). Для других регионов — ws_urls(domain, demo)
WS_PUBLIC_DEMO = "wss://wspap.okx.com:8443/ws/v5/public"
WS_PRIVATE_DEMO = "wss://wspap.okx.com:8443/ws/v5/private"
WS_PUBLIC_LIVE = "wss://ws.okx.com:8443/ws/v5/public"
WS_PRIVATE_LIVE = "wss://ws.okx.com:8443/ws/v5/private"
