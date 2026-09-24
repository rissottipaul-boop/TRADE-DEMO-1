"""Торговый движок Фазы 1: WS (public + private) + reconciler + хранилище + риск.

Запуск: python -m src.engine
Kill-switch: создать файл data/KILL (текст внутри — причина) или
`python -m src.ops kill`. Движок отменит все ордера, включая algo, и
заблокирует входы до ручного сброса (`python -m src.ops reset kill`).
Нативные grid-боты OKX тоже останавливаются (монеты остаются на счёте).
Штатная остановка: файл data/STOP_ENGINE (или `ops/engine.ps1 stop`).

Блокирующие вызовы CCXT выполняются в потоках (asyncio.to_thread): троттлер
CCXT спит через time.sleep и иначе останавливал бы event loop вместе с WS-пингами.
"""
import asyncio
import json
import logging
import time
from pathlib import Path
from typing import Optional

import ccxt

from . import risk
from .config import load_settings
from .connector import (
    check_time_sync,
    create_exchange,
    emergency_stop,
    exp_time_ms,
    new_client_order_id,
)
from .reconciler import Reconciler, order_record_from_okx, to_float, trade_record_from_ws_order
from .storage import EquityRecord, OrderRecord, Storage
from .ws_client import OKXWebSocket, WSConfig, WSCredentials, ws_urls

log = logging.getLogger("okx.engine")

KILL_FLAG = Path("data/KILL")
STOP_FLAG = Path("data/STOP_ENGINE")
ORDER_TTL_MS = 5000  # expTime: биржа отбросит place, если он дошёл позже


class RiskRejected(Exception):
    """Ордер отклонён риск-ядром."""


class TradingEngine:
    def __init__(self, inst_ids: tuple[str, ...] = ("BTC-USDT",), inst_type: str = "SPOT"):
        self.settings = load_settings()
        self.ex = create_exchange(self.settings)
        self.db = Storage()
        self.inst_ids = tuple(inst_ids)
        self.inst_type = inst_type
        self.reconciler = Reconciler(self.ex, self.db, inst_type=inst_type)
        risk.set_order_canceller(self._cancel_all_for_kill_switch)

        urls = ws_urls(self.settings.domain, self.settings.is_demo)
        self.ws_public = OKXWebSocket(WSConfig(url=urls.public), on_message=self._on_public_message)
        self.ws_private = OKXWebSocket(
            WSConfig(url=urls.private),
            on_message=self._on_private_message,
            credentials=WSCredentials(
                self.settings.api_key, self.settings.secret, self.settings.passphrase
            ),
        )
        # Клиент business-эндпоинта создаётся лениво: каналы candle* на public
        # отклоняются ошибкой 60018 (insights/okx-api.md §4)
        self.ws_business: Optional[OKXWebSocket] = None

        self._running = False
        self._reconcile_interval = 60.0  # сек; заодно продлевает жизнь live-ключа (14 дней)
        self._equity_interval = 300.0
        self._tickers: dict[str, dict] = {}
        self.stats = {"started_at": None, "reconciles": 0, "divergences": 0, "errors": 0}

    # --- Жизненный цикл ---

    async def start(self) -> None:
        self.stats["started_at"] = time.time()
        log.info("Starting engine (mode=%s, domain=%s, inst=%s)",
                 self.settings.mode, self.settings.domain, ",".join(self.inst_ids))

        drift = await asyncio.to_thread(check_time_sync, self.ex)
        log.info("Time drift: %+d ms", drift)
        await asyncio.to_thread(self.ex.load_markets)

        await self._reconcile()
        await self._update_equity()

        await self.ws_public.connect()
        for inst_id in self.inst_ids:
            await self.subscribe_market_data("books", inst_id)
            await self.subscribe_market_data("tickers", inst_id)
        await self.ws_private.connect()
        await self.ws_private.subscribe({"channel": "orders", "instType": self.inst_type})

        self._running = True
        log.info("Engine started")
        tasks = [asyncio.create_task(c) for c in (self._reconcile_loop(), self._equity_loop(), self._flag_loop())]
        try:
            # выход — по флагу STOP_ENGINE или если какой-то цикл упал непойманным исключением
            done, _ = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
            for t in done:
                if not t.cancelled() and t.exception():
                    raise t.exception()
        finally:
            for t in tasks:
                t.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)

    async def stop(self) -> None:
        self._running = False
        await self.ws_public.close()
        if self.ws_business is not None:
            await self.ws_business.close()
        await self.ws_private.close()
        self._save_stats()
        log.info("Engine stopped. Stats: %s", self.stats)

    async def subscribe_market_data(self, channel: str, inst_id: str) -> None:
        """Подписка на публичный канал маркет-данных.

        Каналы candle* идут через ws_urls(...).business — на public-эндпоинте
        OKX отклоняет их ошибкой 60018; остальное — на public.
        """
        args = {"channel": channel, "instId": inst_id}
        if channel.startswith("candle"):
            if self.ws_business is None:
                urls = ws_urls(self.settings.domain, self.settings.is_demo)
                self.ws_business = OKXWebSocket(
                    WSConfig(url=urls.business), on_message=self._on_public_message
                )
                await self.ws_business.connect()
            await self.ws_business.subscribe(args)
        else:
            await self.ws_public.subscribe(args)

    # --- WS-обработчики ---

    def _on_public_message(self, data: dict) -> None:
        arg = data.get("arg", {})
        if arg.get("channel") == "tickers":
            for t in data["data"]:
                self._tickers[t["instId"]] = t

    def _on_private_message(self, data: dict) -> None:
        if data.get("arg", {}).get("channel") != "orders":
            return
        for o in data["data"]:
            record = order_record_from_okx(o)
            self.db.upsert_order(record)
            trade = trade_record_from_ws_order(o)
            if trade:
                self.db.insert_trade(trade)
            log.info("Order %s %s %s: %s (filled %s/%s)",
                     record.ord_id, record.side, record.inst_id, record.state,
                     record.filled_sz, record.sz)

    # --- Периодические задачи ---

    async def _reconcile_loop(self) -> None:
        while self._running:
            await asyncio.sleep(self._reconcile_interval)
            try:
                await self._reconcile()
            except Exception as e:
                self.stats["errors"] += 1
                log.error("Reconcile error: %s", e)

    async def _reconcile(self) -> None:
        """Сверка состояния с биржей; расхождения = WS что-то пропустил."""
        divergences = 0
        for inst_id in self.inst_ids:
            orders = await asyncio.to_thread(self.reconciler.sync_orders, inst_id)
            trades = await asyncio.to_thread(self.reconciler.sync_trades, inst_id)
            divergences += orders["missing_on_exchange"] + orders["missing_locally"] + orders["changed"]
            log.info("Reconcile %s: orders=%s trades=%s", inst_id, orders, trades)
        if self.inst_type != "SPOT":
            positions = await asyncio.to_thread(self.reconciler.sync_positions)
            log.info("Positions: %s", positions)

        self.stats["reconciles"] += 1
        if divergences and self._running:  # стартовая сверка — не рассинхрон
            self.stats["divergences"] += divergences
            log.warning("РАССИНХРОН WS/REST: %d (всего %d)", divergences, self.stats["divergences"])
        self._save_stats()

    def _save_stats(self) -> None:
        self.db.set_ws_state("engine_stats", {
            **self.stats,
            "ws_public_reconnects": self.ws_public.reconnects,
            "ws_business_reconnects": self.ws_business.reconnects if self.ws_business else 0,
            "ws_private_reconnects": self.ws_private.reconnects,
            "saved_at": time.time(),
        })

    async def _equity_loop(self) -> None:
        while self._running:
            await asyncio.sleep(self._equity_interval)
            try:
                await self._update_equity()
            except Exception as e:
                self.stats["errors"] += 1
                log.error("Equity error: %s", e)

    async def _update_equity(self) -> None:
        """Equity = totalEq аккаунта (USD), а не только USDT-баланс."""
        resp = await asyncio.to_thread(self.ex.private_get_account_balance)
        account = (resp.get("data") or [{}])[0]
        total = to_float(account.get("totalEq"))
        usdt = next((d for d in account.get("details", []) if d.get("ccy") == "USDT"), {})
        self.db.record_equity(EquityRecord(
            ts=time.time(),
            total_eq=total,
            avail_eq=to_float(usdt.get("availBal")),
            upl=to_float(account.get("upl")),
        ))
        events = risk.update_equity(total)
        if events:
            log.critical("RISK BREAKER по equity %.2f: %s — новые входы заблокированы", total, events)

    async def _flag_loop(self) -> None:
        """Файлы-флаги: data/KILL — аварийная остановка торговли, data/STOP_ENGINE — штатный выход."""
        while self._running:
            if KILL_FLAG.exists():
                reason = KILL_FLAG.read_text(encoding="utf-8").strip() or "file flag"
                KILL_FLAG.unlink(missing_ok=True)
                report = await asyncio.to_thread(risk.kill_switch, False, f"flag: {reason}")
                log.critical("KILL-SWITCH: отменено %d, не отменено %d",
                             len(report["cancelled"]), len(report["failed"]))
            if STOP_FLAG.exists():
                STOP_FLAG.unlink(missing_ok=True)
                log.info("STOP_ENGINE: штатная остановка по флагу")
                return
            await asyncio.sleep(2)

    def _cancel_all_for_kill_switch(self, flatten: bool) -> dict:
        if flatten:
            log.error("flatten не реализован: позиции закрываются по стопам или вручную")
        return emergency_stop(self.ex)

    # --- Ордера ---

    async def place_order(
        self,
        inst_id: str,
        side: str,
        ord_type: str,
        sz: float,
        px: Optional[float] = None,
        *,
        entry: Optional[bool] = None,
    ) -> OrderRecord:
        """Ставит ордер: риск-проверка входа → clOrdId + expTime → запись в БД.

        entry — открытие/наращивание позиции (проверяется риск-ядром). По
        умолчанию для спота buy = вход, sell = выход: выходы breaker'ами не
        блокируются, иначе при срабатывании защиты нельзя было бы закрыться.
        Учёт открытой позиции (risk.register_entry) — на стороне стратегии,
        после фактического исполнения.
        """
        if ord_type not in ("limit", "market", "post_only"):
            raise ValueError(f"Unsupported ord_type: {ord_type}")
        if ord_type != "market" and not px:
            raise ValueError("px обязателен для limit/post_only")

        if entry is None:
            entry = side == "buy"
        if entry:
            allowed, reason = risk.check_entry_allowed(inst_id, side)
            if not allowed:
                raise RiskRejected(reason)
            await self._check_notional(inst_id, sz, px)

        cl_ord_id = new_client_order_id()
        params = {"clOrdId": cl_ord_id, "expTime": exp_time_ms(ORDER_TTL_MS)}
        ccxt_type = "limit" if ord_type == "post_only" else ord_type
        if ord_type == "post_only":
            params["postOnly"] = True

        try:
            order = await asyncio.to_thread(
                self.ex.create_order, inst_id, ccxt_type, side, sz, px, params
            )
            ord_id, raw = order["id"], order.get("info", {})
        except ccxt.NetworkError:
            # ответ потерян, но ордер мог встать; после expTime исход уже окончательный
            await asyncio.sleep(ORDER_TTL_MS / 1000)
            raw = await asyncio.to_thread(self._find_order_by_cl_id, inst_id, cl_ord_id)
            if raw is None:
                raise
            ord_id = raw["ordId"]
            log.warning("Ответ place потерян, ордер найден по clOrdId %s → %s", cl_ord_id, ord_id)

        now = time.time()
        record = OrderRecord(
            ord_id=ord_id, inst_id=inst_id, side=side, ord_type=ord_type, px=px, sz=sz,
            state="live", filled_sz=0.0, avg_px=0.0, fee=0.0, create_time=now,
            update_time=now, raw_json=json.dumps(raw, ensure_ascii=False, default=str),
            cl_ord_id=cl_ord_id,
        )
        # если WS уже прислал fill, upsert не откатит состояние (см. storage.py)
        self.db.upsert_order(record)
        log.info("Order placed: %s %s %s @ %s (ordId=%s)", side, sz, inst_id, px, ord_id)
        return record

    def _find_order_by_cl_id(self, inst_id: str, cl_ord_id: str) -> Optional[dict]:
        try:
            data = self.ex.private_get_trade_order({"instId": inst_id, "clOrdId": cl_ord_id}).get("data")
        except ccxt.OrderNotFound:
            return None
        return data[0] if data else None

    async def _check_notional(self, inst_id: str, sz: float, px: Optional[float]) -> None:
        """Последний рубеж от ошибок единиц (rookie-airbag): notional ≤ потолка позиции."""
        equity = risk.status()["equity"]
        if equity <= 0:
            raise RiskRejected("equity неизвестен — вход запрещён до первого обновления баланса")
        price = px or to_float(self._tickers.get(inst_id, {}).get("askPx"), None)
        if price is None:
            ticker = await asyncio.to_thread(self.ex.fetch_ticker, inst_id)
            price = ticker["ask"] or ticker["last"]
        notional = sz * price
        limit = equity * risk.MAX_POSITION_PCT / 100
        if notional > limit:
            raise RiskRejected(
                f"notional {notional:.2f} > {risk.MAX_POSITION_PCT}% equity ({limit:.2f})"
            )


async def main() -> None:
    engine = TradingEngine()
    try:
        await engine.start()
    finally:
        await engine.stop()


if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        log.info("Остановлено пользователем")
