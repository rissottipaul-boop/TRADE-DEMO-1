"""Реконсиляция состояния: сверка локального хранилища с биржей (Фаза 1).

Принцип: локальное состояние — источник правды; биржа опрашивается для
обнаружения расхождений (пропущенные fill'ы, внешние отмены).

Свои ордера отличаем от внешних по префиксу clOrdId (OWN_CLORD_PREFIX):
движок и стратегии ставят ордера через connector.new_client_order_id()
(префикс «bot»), а нагрузочные тесты (lt*), ручные и прочие ордера без
нашего префикса — внешние. Внешние ордера не считаются расхождением и не
подбираются в БД реконсилятором: иначе их внешняя отмена давала бы ложный
missing_on_exchange (см. insights/phase1-progress.md, RECON-EXT).

Работаем с сырыми ответами OKX (instId, state, accFillSz), а не с
CCXT-унифицированными (symbol BTC/USDT, status open/closed): так ключи и
состояния в БД совпадают с тем, что приходит по WS-каналу orders.
"""
import json
import logging
import time
from collections import Counter
from typing import Any, Optional

import ccxt

from .connector import fetch_pending_orders
from .storage import OrderRecord, PositionRecord, Storage, TradeRecord

log = logging.getLogger("okx.reconciler")

FILLS_PAGE = 100

# Префикс clOrdId своих ордеров — значение по умолчанию connector.new_client_order_id()
OWN_CLORD_PREFIX = "bot"


def is_own_order(cl_ord_id: Optional[str]) -> bool:
    """Свой ордер — выставлен движком/стратегиями (clOrdId с префиксом OWN_CLORD_PREFIX).

    Пустой clOrdId (ручные ордера) — внешний: все наши потоки выставления
    всегда проставляют свой clOrdId (он же ключ восстановления после таймаута).
    """
    return bool(cl_ord_id) and cl_ord_id.startswith(OWN_CLORD_PREFIX)


def to_float(value: Any, default: Optional[float] = 0.0) -> Optional[float]:
    """Число из строки OKX ("" и None → default)."""
    if value in (None, ""):
        return default
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _ts(ms: Any) -> Optional[float]:
    value = to_float(ms, None)
    return value / 1000 if value else None


def order_record_from_okx(o: dict) -> OrderRecord:
    """Сырой ордер OKX (REST orders-pending / trade/order, WS orders) → OrderRecord."""
    now = time.time()
    return OrderRecord(
        ord_id=o["ordId"],
        inst_id=o["instId"],
        side=o["side"],
        ord_type=o["ordType"],
        px=to_float(o.get("px"), None),
        sz=to_float(o.get("sz")),
        state=o["state"],
        filled_sz=to_float(o.get("accFillSz")),
        avg_px=to_float(o.get("avgPx")),
        fee=-to_float(o.get("fee")),  # OKX: отрицательный fee = списание
        create_time=_ts(o.get("cTime")) or now,
        update_time=_ts(o.get("uTime")) or now,
        raw_json=json.dumps(o, ensure_ascii=False),
        cl_ord_id=o.get("clOrdId") or None,
    )


def trade_record_from_fill(f: dict) -> TradeRecord:
    """Сделка из REST /trade/fills."""
    return TradeRecord(
        inst_id=f["instId"],
        trade_id=f["tradeId"],
        ord_id=f.get("ordId") or None,
        side=f["side"],
        px=to_float(f.get("fillPx")),
        sz=to_float(f.get("fillSz")),
        fee=-to_float(f.get("fee")),
        fee_ccy=f.get("feeCcy") or None,
        ts=_ts(f.get("ts")) or time.time(),
    )


def trade_record_from_ws_order(o: dict) -> Optional[TradeRecord]:
    """Сделка из WS-сообщения канала orders (если оно вызвано исполнением)."""
    if not o.get("tradeId") or not to_float(o.get("fillSz")):
        return None
    return TradeRecord(
        inst_id=o["instId"],
        trade_id=o["tradeId"],
        ord_id=o["ordId"],
        side=o["side"],
        px=to_float(o.get("fillPx")),
        sz=to_float(o.get("fillSz")),
        fee=-to_float(o.get("fillFee")),
        fee_ccy=o.get("fillFeeCcy") or None,
        ts=_ts(o.get("fillTime")) or time.time(),
    )


class Reconciler:
    def __init__(self, exchange: Any, storage: Storage, inst_type: str = "SPOT"):
        self.ex = exchange
        self.db = storage
        self.inst_type = inst_type
        self.last_sync = 0.0

    def sync_orders(self, inst_id: str) -> dict:
        """Сверяет открытые ордера: локальные vs биржевые.

        Участвуют только свои ордера (is_own_order по clOrdId) — и локальные,
        и биржевые. Внешние ордера (без префикса OWN_CLORD_PREFIX) попадают
        только в метрику external_orders и в БД не подбираются.

        Возвращает счётчики; ненулевые missing_* / changed — это рассинхрон
        (WS что-то пропустил), метрика для критерия выхода Фазы 1.
        """
        local_open = {
            r["ord_id"]: r for r in self.db.get_open_orders(inst_id)
            if is_own_order(r["cl_ord_id"])
        }
        remote_all = {o["ordId"]: o for o in fetch_pending_orders(self.ex, inst_id)}
        remote_open = {k: o for k, o in remote_all.items() if is_own_order(o.get("clOrdId"))}
        external_orders = len(remote_all) - len(remote_open)

        missing_locally = set(remote_open) - set(local_open)
        missing_on_exchange = set(local_open) - set(remote_all)
        changed = 0

        for ord_id, o in remote_open.items():
            record = order_record_from_okx(o)
            local = local_open.get(ord_id)
            if local is not None and (local["state"] != record.state or local["filled_sz"] != record.filled_sz):
                changed += 1
            self.db.upsert_order(record)

        if external_orders:
            log.info("Внешних ордеров на бирже (без префикса %s*): %d — не рассинхрон",
                     OWN_CLORD_PREFIX, external_orders)
        if missing_locally:
            log.warning("Свои ордера есть на бирже, но нет локально: %s — добавлены", sorted(missing_locally))
        if changed:
            log.warning("У %d ордеров устарели состояние/исполнение — обновлены", changed)

        resolved: Counter = Counter()
        for ord_id in missing_on_exchange:
            resolved[self._resolve_final_state(inst_id, ord_id, local_open[ord_id])] += 1
        if missing_on_exchange:
            log.warning("Ордера закрылись без WS-уведомления: %s", dict(resolved))

        self.last_sync = time.time()
        return {
            "local_open": len(local_open),
            "remote_open": len(remote_open),
            "external_orders": external_orders,
            "missing_on_exchange": len(missing_on_exchange),
            "missing_locally": len(missing_locally),
            "changed": changed,
            "resolved": dict(resolved),
            "synced_at": self.last_sync,
        }

    def _resolve_final_state(self, inst_id: str, ord_id: str, row: Any) -> str:
        """Ордер пропал из активных: узнаём у биржи, исполнен он или отменён."""
        try:
            data = self.ex.private_get_trade_order({"instId": inst_id, "ordId": ord_id}).get("data") or []
        except ccxt.OrderNotFound:  # 51603
            data = []
        if data:
            record = order_record_from_okx(data[0])
            self.db.upsert_order(record)
            return record.state

        # OKX хранит отменённые без исполнения ордера ~2 часа — не найден = отменён
        log.warning("Ордер %s не найден на бирже — помечаем canceled", ord_id)
        self.db.upsert_order(OrderRecord(
            ord_id=ord_id, inst_id=row["inst_id"], side=row["side"], ord_type=row["ord_type"],
            px=row["px"], sz=row["sz"], state="canceled", filled_sz=row["filled_sz"],
            avg_px=row["avg_px"], fee=row["fee"], create_time=row["create_time"],
            update_time=time.time(), raw_json=row["raw_json"], cl_ord_id=row["cl_ord_id"],
        ))
        return "canceled"

    def sync_trades(self, inst_id: str) -> dict:
        """Догружает сделки из /trade/fills (последние 3 дня), идемпотентно."""
        new = 0
        after: Optional[str] = None
        while True:
            params = {"instType": self.inst_type, "instId": inst_id, "limit": str(FILLS_PAGE)}
            if after:
                params["after"] = after
            page = self.ex.private_get_trade_fills(params).get("data") or []
            page_new = sum(self.db.insert_trade(trade_record_from_fill(f)) for f in page)
            new += page_new
            # страница без новых сделок — дальше только уже известные
            if len(page) < FILLS_PAGE or page_new == 0:
                break
            after = page[-1]["billId"]
        if new:
            log.info("Новых сделок %s: %d", inst_id, new)
        return {"new_trades": new}

    def sync_positions(self) -> dict:
        """Сверяет позиции (маржа/деривативы) с биржей."""
        remote = self.ex.private_get_account_positions().get("data") or []
        local = {f"{r['inst_id']}:{r['pos_side']}": r for r in self.db.get_positions()}

        remote_keys = set()
        for p in remote:
            pos = to_float(p.get("pos"))
            if not pos:
                continue
            record = PositionRecord(
                inst_id=p["instId"],
                pos_side=p.get("posSide") or "net",
                pos=pos,
                avg_px=to_float(p.get("avgPx")),
                upl=to_float(p.get("upl")),
                liq_px=to_float(p.get("liqPx"), None),
                update_time=time.time(),
            )
            remote_keys.add(f"{record.inst_id}:{record.pos_side}")
            self.db.upsert_position(record)

        closed = set(local) - remote_keys
        if closed:
            log.info("Закрытые позиции: %s", sorted(closed))
        for key in closed:
            inst_id, pos_side = key.rsplit(":", 1)
            self.db.upsert_position(PositionRecord(
                inst_id=inst_id, pos_side=pos_side, pos=0, avg_px=0, upl=0,
                liq_px=None, update_time=time.time(),
            ))

        return {"remote_positions": len(remote_keys), "local_positions": len(local), "closed": len(closed)}
