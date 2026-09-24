"""Единая точка выставления ордеров (задача p1-order-router).

Все ордеры проекта идут ТОЛЬКО через OrderRouter. Прямые вызовы
exchange.create_order вне этого модуля запрещены.

Последовательность place_order:
1. risk.check_entry_allowed  — отказ риск-слоя => ордер НЕ выставляется;
2. risk.size_position (если sz не задан явно) или валидация переданного sz;
3. risk.validate_stop_vs_liquidation (если заданы stop_px и liq_price);
3b. спот — режим аккаунта (SPOT-TDMODE, src/account_mode.py): tdMode по acctLv
   (1–2 → cash, 3–4 → cross; режим перечитывается раз в 5 мин). Если заём
   возможен (autoLoan в режимах 3–4 или enableSpotBorrow), ордер больше
   availBal отклоняется до биржи. Режим или баланс не прочитаны — отказ;
4. throttler: не более 20 place-запросов / 2 сек на инструмент
   (поверх CCXT enableRateLimit — тот про общий REST, этот про ордера);
5. выставление через okx_call с expTime-дедлайном (защита от зависших
   запросов; OKX отбрасывает запрос после дедлайна — аналог recvWindow)
   и clOrdId с префиксом владельца (ORDER-OWNER-TAG, src/order_owner.py):
   владелец роутера по умолчанию — `botr`, стратегия передаёт свой
   (`owner=` в конструкторе или в place_order); допустим только корень
   bot* — ордера роутера пишутся в storage и сверяются реконсилятором;
6. fetch_order — CCXT на свежесозданном ордере может вернуть status=None,
   дочитываем реальный статус перед записью в БД;
7. storage.upsert_order + risk.register_entry.

Kill-switch: при инициализации роутер ПОДПИСЫВАЕТСЯ на kill-switch через
реестр connector.register_kill_callback, а в risk-модуль регистрируется
единый диспетчер connector.kill_switch_dispatch — он вызывает ВСЕХ
подписчиков, поэтому при одновременной работе engine и OrderRouter
регистрации не перезаписывают друг друга (задача KILL-CALLBACK-OWNER).
Единая точка отмены — connector.emergency_stop (bulk-отмена обычных и
algo-ордеров + остановка grid- и DCA-ботов); после отмены локальные открытые
ордера в storage помечаются canceled.

Известное ограничение: engine.py регистрирует в risk свой колбэк напрямую
(файл не меняем — работает движок). Если engine создаётся ПОСЛЕ роутера,
он перезапишет слот risk своим колбэком. Это безопасно: колбэк engine —
тот же connector.emergency_stop, а локальную пометку canceled восстановит
реконсиляция. Рекомендуемый порядок в общем процессе: сначала engine,
затем OrderRouter — тогда в risk стоит диспетчер и вызываются все.

Демо-принцип: роутер принимает готовый exchange-объект (см. connector.py),
сам ключи и .env не читает.

Выход из позиции (ROUTER-EXIT): place_exit_order — отдельный метод, НЕ вход.
В отличие от place_order он не проходит risk.check_entry_allowed (хедж, лимит
входов/день, breaker'ы, свежесть equity — по регламенту в аварии выходы
обязаны проходить, AGENTS.md §2), а ищет позицию в risk.open_position — том
же слоте risk_open_risk, что занимает register_entry при входе. Нет записи —
выход отклонён (метка «выход» сама по себе не должна открывать позицию,
о которой риск-ядро не знает); сторона обязана быть противоположна стороне
позиции, sz — не больше её остатка (иначе отказ: так выход не может открыть
или нарастить позицию под своим именем); для деривативов (не спот) —
reduceOnly=True вторым рубежом на стороне биржи. Проверки ордера, а не
входа (tdMode/запрет займа по режиму аккаунта, throttler, expTime, clOrdId
с меткой владельца), выход проходит наравне со входом. По исполнении выход
освобождает слот через risk.release_position, а не занимает его через
risk.register_entry.
"""
import json
import logging
import math
import threading
import time
from collections import deque
from pathlib import Path
from typing import Any, Optional

from . import order_owner, risk
from .account_mode import AccountMode, check_no_borrow, fetch_account_mode, is_spot, spot_order_params
from .connector import (
    emergency_stop,
    kill_switch_dispatch,
    okx_call,
    register_kill_callback,
    unregister_kill_callback,
)
from .storage import DB_PATH as DEFAULT_DB_PATH
from .storage import OrderRecord, Storage

log = logging.getLogger("okx.order_router")

# Маппинг unified-статусов CCXT в сырые состояния OKX (канон storage.py)
_CCXT_TO_OKX_STATE = {
    "open": "live",
    "closed": "filled",
    "canceled": "canceled",
    "expired": "canceled",
    "rejected": "canceled",
}

# Лимит place-запросов OKX: 20/2с (spot) — берём самый строгий (insights/okx-api.md §3)
PLACE_LIMIT = 20
PLACE_WINDOW_SEC = 2.0
# TTL ордерного запроса по умолчанию (expTime), мс
DEFAULT_EXP_TTL_MS = 10_000
# Режим аккаунта перечитывается не чаще раза в 5 мин: человек может сменить его
# на ходу (incidents.md 24.09 10:10), а account/config — 5 запросов / 2 с
ACCOUNT_MODE_TTL_S = 300.0

# CCXT unified side -> сторона позиции для risk.validate_stop_vs_liquidation
_SIDE_TO_POSITION = {"buy": "long", "sell": "short"}

# Выход обязан идти противоположной стороной относительно зарегистрированной
# позиции (ROUTER-EXIT) — иначе это не выход, а вход/наращивание под чужой меткой
_OPPOSITE_SIDE = {"buy": "sell", "sell": "buy"}


class _PlaceThrottler:
    """Счётчик place-запросов с скользящим окном на инструмент.

    При исчерпании лимита блокирует вызывающий поток до освобождения окна
    (не роняет ордер — задерживает; отказ — прерогатива риск-слоя).
    """

    def __init__(self, limit: int = PLACE_LIMIT, window_sec: float = PLACE_WINDOW_SEC):
        self.limit = limit
        self.window_sec = window_sec
        self._hits: dict[str, deque[float]] = {}
        self._lock = threading.Lock()

    def acquire(self, inst_id: str) -> None:
        while True:
            with self._lock:
                now = time.monotonic()
                dq = self._hits.setdefault(inst_id, deque())
                while dq and now - dq[0] >= self.window_sec:
                    dq.popleft()
                if len(dq) < self.limit:
                    dq.append(now)
                    return
                sleep_for = self.window_sec - (now - dq[0])
            log.warning("throttler: лимит %d/%.1fс по %s исчерпан, пауза %.2fс",
                        self.limit, self.window_sec, inst_id, sleep_for)
            time.sleep(max(sleep_for, 0.01))


class OrderRouter:
    """Единая точка выставления и аварийной отмены ордеров."""

    def __init__(
        self,
        exchange: Any,
        storage: Optional[Storage] = None,
        db_path: Path | str = DEFAULT_DB_PATH,
        exp_ttl_ms: int = DEFAULT_EXP_TTL_MS,
        throttle_limit: int = PLACE_LIMIT,
        throttle_window_sec: float = PLACE_WINDOW_SEC,
        owner: str = order_owner.ROUTER,
        account_mode: Optional[AccountMode] = None,  # None -> читать account/config (TTL)
    ) -> None:
        # Владелец clOrdId по умолчанию (ORDER-OWNER-TAG); ошибка конфигурации —
        # ValueError до любого обращения к бирже
        self.owner = order_owner.require(owner, own=True).code
        self.exchange = exchange
        # Режим аккаунта для спота (SPOT-TDMODE): переданный — фиксирован
        self._account_mode = account_mode
        self._account_mode_fixed = account_mode is not None
        self._account_mode_at = 0.0
        # Хранилище — канонический Storage (storage.py); db_path — для тестов
        self.storage = storage or Storage(db_path)
        self.db_path = self.storage.db_path
        self.exp_ttl_ms = exp_ttl_ms
        self.throttler = _PlaceThrottler(throttle_limit, throttle_window_sec)
        # Привязка kill-switch: роутер — подписчик реестра connector
        # (KILL-CALLBACK-OWNER); в risk регистрируется единый диспетчер,
        # который вызывает всех подписчиков, — регистрация из нескольких
        # мест (engine, роутер) больше не перезаписывает друг друга.
        register_kill_callback(self._cancel_all_orders)
        risk.set_order_canceller(kill_switch_dispatch)

    def close(self) -> None:
        """Отписать роутер от kill-switch (тесты, пересоздание роутера)."""
        unregister_kill_callback(self._cancel_all_orders)

    # --- Выставление ---

    def place_order(
        self,
        inst_id: str,
        side: str,                       # "buy" | "sell" (CCXT unified)
        ord_type: str,                   # "limit" | "market"
        px: Optional[float] = None,
        sz: Optional[float] = None,      # явный размер; None -> сайзинг через risk
        stop_px: Optional[float] = None,
        liq_price: Optional[float] = None,
        equity: Optional[float] = None,  # обязателен для сайзинга
        ct_val: float = 1.0,
        lot_sz: float = 0.0,
        min_sz: float = 0.0,
        risk_pct: float = risk.DEFAULT_RISK_PCT,
        owner: Optional[str] = None,     # код владельца clOrdId; None -> владелец роутера
    ) -> dict:
        """Выставить ордер через риск-слой. Возвращает dict с ok=True/False.

        При ok=False ордер на биржу НЕ уходил; причина в полях stage/reason.
        Исключения биржи (ccxt) не глушатся — okx_call уже залогировал sCode/sMsg.
        Незарегистрированный или не bot* владелец — ValueError до риск-проверки.
        """
        owner_code = order_owner.require(owner, own=True).code if owner else self.owner

        # 1. Допуск риск-слоя
        allowed, reason = risk.check_entry_allowed(inst_id, side)
        if not allowed:
            log.warning("place_order ОТКЛОНЁН risk: %s %s %s — %s",
                        inst_id, side, ord_type, reason)
            return {"ok": False, "stage": "check_entry_allowed", "reason": reason}

        # 2. Размер позиции
        if sz is None:
            if equity is None or px is None or stop_px is None:
                reason = "для сайзинга нужны equity, px и stop_px (или передайте sz явно)"
                log.warning("place_order ОТКЛОНЁН: %s", reason)
                return {"ok": False, "stage": "size_position", "reason": reason}
            sized = risk.size_position(equity=equity, entry=px, stop=stop_px,
                                       ct_val=ct_val, lot_sz=lot_sz, min_sz=min_sz,
                                       risk_pct=risk_pct)
            for warning in sized["warnings"]:
                log.warning("size_position: %s", warning)
            if sized["size"] <= 0:
                reason = "сайзинг дал нулевой размер: " + "; ".join(sized["warnings"])
                log.warning("place_order ОТКЛОНЁН: %s", reason)
                return {"ok": False, "stage": "size_position", "reason": reason}
            sz = sized["size"]
        else:
            if sz <= 0 or (min_sz and sz < min_sz):
                reason = f"некорректный sz={sz} (min_sz={min_sz})"
                log.warning("place_order ОТКЛОНЁН: %s", reason)
                return {"ok": False, "stage": "size_validation", "reason": reason}

        # 3. Стоп обязан срабатывать раньше ликвидации
        if stop_px is not None and px is not None:
            if liq_price is None:
                log.warning("stop_px задан без liq_price — проверка стоп/ликвидация пропущена")
            elif not risk.validate_stop_vs_liquidation(
                    px, stop_px, liq_price, _SIDE_TO_POSITION.get(side, side)):
                reason = (f"стоп {stop_px} за пределами ликвидации {liq_price} "
                          f"(entry {px}, side {side})")
                log.warning("place_order ОТКЛОНЁН: %s", reason)
                return {"ok": False, "stage": "stop_vs_liquidation", "reason": reason}

        # 3b. Спот: tdMode по режиму аккаунта и запрет скрытого займа (SPOT-TDMODE)
        mode_params, reason = self._spot_params(inst_id, side, ord_type, sz, px)
        if mode_params is None:
            log.warning("place_order ОТКЛОНЁН: %s", reason)
            return {"ok": False, "stage": "account_mode", "reason": reason}

        # 4. Throttler ордерных запросов (поверх CCXT enableRateLimit)
        self.throttler.acquire(inst_id)

        # 5. Выставление с expTime-дедлайном и своим clOrdId: префикс — метка
        #    владельца, clOrdId — ключ восстановления, если ответ на place потерялся
        cl_ord_id = order_owner.new_cl_ord_id(owner_code)
        order = self._create_order_with_exp(inst_id, ord_type, side, sz, px, cl_ord_id, mode_params)
        exchange_order_id = order.get("id")
        log.info("ордер выставлен: %s %s %s sz=%s px=%s id=%s clOrdId=%s (владелец %s)",
                 inst_id, side, ord_type, sz, px, exchange_order_id, cl_ord_id, owner_code)

        # 6. CCXT на свежем ордере может вернуть status=None — дочитываем
        status = order.get("status")
        if not status and exchange_order_id:
            try:
                fetched = okx_call(self.exchange.fetch_order, exchange_order_id, inst_id)
                status = fetched.get("status")
            except Exception as exc:
                log.warning("fetch_order после place не удался (%s) — пишем state=live", exc)
        okx_state = _CCXT_TO_OKX_STATE.get(status or "", "live")

        # 7. Фиксация в storage и риск-ядре (ord_id = ordId биржи, иначе clOrdId)
        now = time.time()
        ord_id = str(exchange_order_id or cl_ord_id)
        self.storage.upsert_order(OrderRecord(
            ord_id=ord_id, inst_id=inst_id, side=side, ord_type=ord_type,
            px=px, sz=sz, state=okx_state, filled_sz=0.0, avg_px=0.0, fee=0.0,
            create_time=now, update_time=now,
            raw_json=json.dumps(order, ensure_ascii=False, default=str),
            cl_ord_id=cl_ord_id,
        ))
        risk.register_entry(inst_id, side, risk_pct)
        return {
            "ok": True,
            "order_id": ord_id,
            "exchange_order_id": exchange_order_id,
            "cl_ord_id": cl_ord_id,
            "owner": owner_code,
            "status": status or "open",
            "inst_id": inst_id,
            "side": side,
            "ord_type": ord_type,
            "px": px,
            "sz": sz,
        }

    def account_mode(self) -> AccountMode:
        """Режим аккаунта: переданный в конструктор или account/config (кэш ACCOUNT_MODE_TTL_S)."""
        now = time.monotonic()
        stale = now - self._account_mode_at > ACCOUNT_MODE_TTL_S
        if self._account_mode is None or (stale and not self._account_mode_fixed):
            mode = okx_call(fetch_account_mode, self.exchange)
            if self._account_mode is not None and mode != self._account_mode:
                log.warning("режим аккаунта сменился: %s -> %s", self._account_mode, mode)
            self._account_mode, self._account_mode_at = mode, now
        return self._account_mode

    def _spot_params(self, inst_id: str, side: str, ord_type: str, sz: float,
                     px: Optional[float]) -> tuple[Optional[dict], str]:
        """Параметры create_order для спота или (None, причина отказа).

        Не спот — без изменений ({}): деривативы роутер пока не ставит.
        """
        if not is_spot(inst_id):
            return {}, ""
        try:
            mode = self.account_mode()
        except Exception as exc:  # без режима tdMode не выбрать — ордер не ставим
            return None, f"режим аккаунта не прочитан ({exc}) — tdMode спота не выбрать"
        try:
            ok, reason = check_no_borrow(self.exchange, mode, inst_id, side, ord_type, sz, px)
        except Exception as exc:
            return None, f"availBal не прочитан ({exc}) — заём не исключить"
        if not ok:
            return None, reason
        try:
            return spot_order_params(mode, ord_type, side, sz, px), ""
        except ValueError as exc:
            return None, str(exc)

    def _create_order_with_exp(self, inst_id: str, ord_type: str, side: str,
                               sz: float, px: Optional[float], cl_ord_id: str,
                               extra_params: Optional[dict] = None) -> dict:
        """create_order с expTime: дедлайн и в теле запроса (params), и в заголовке.

        OKX принимает expTime (unix ms) как параметр place/amend — после дедлайна
        сервер отбрасывает запрос. Заголовок проставляем дополнительно и
        восстанавливаем после вызова, чтобы не тянуть его в неордерные запросы.
        extra_params — tdMode/tgtCcy спота (SPOT-TDMODE).
        """
        now_ms = (self.exchange.milliseconds()
                  if hasattr(self.exchange, "milliseconds") else int(time.time() * 1000))
        deadline = str(now_ms + self.exp_ttl_ms)
        headers = getattr(self.exchange, "headers", None)
        prev = headers.get("expTime") if isinstance(headers, dict) else None
        if isinstance(headers, dict):
            headers["expTime"] = deadline
        try:
            return okx_call(self.exchange.create_order,
                            inst_id, ord_type, side, sz, px,
                            {**(extra_params or {}), "expTime": deadline, "clOrdId": cl_ord_id})
        finally:
            if isinstance(headers, dict):
                if prev is None:
                    headers.pop("expTime", None)
                else:
                    headers["expTime"] = prev

    # --- Kill-switch: отмена всех открытых ордеров ---

    def _cancel_all_orders(self, flatten: bool = False) -> dict:
        """Подписчик реестра kill-switch connector (вызывается диспетчером
        kill_switch_dispatch). Единая точка отмены — connector.emergency_stop:
        bulk-отмена обычных и algo-ордеров + остановка grid- и DCA-ботов; после неё
        локальные открытые ордера в storage помечаются canceled.

        Контракт risk.set_order_canceller: вернуть {cancelled: [...], failed: [...]}.
        flatten (закрытие позиций market-ордерами) здесь не реализован — роутер
        только отменяет ордера; закрытие позиций — отдельная задача движка.
        """
        if flatten:
            log.warning("kill_switch(flatten=True): flatten не реализован, только отмена ордеров")
        report = emergency_stop(self.exchange)
        # Локально canceled помечаем только подтверждённо отменённые на бирже;
        # неотменённые остаются live — их увидит реконсиляция
        cancelled_ids = set(report.get("cancelled", []))
        for row in self.storage.get_open_orders():
            if row["ord_id"] in cancelled_ids or row["cl_ord_id"] in cancelled_ids:
                self._mark_canceled(row)
        return report

    def _mark_canceled(self, row: Any) -> None:
        """Пометить локальный ордер отменённым (липкие финалы storage не откатит)."""
        self.storage.upsert_order(OrderRecord(
            ord_id=row["ord_id"], inst_id=row["inst_id"], side=row["side"],
            ord_type=row["ord_type"], px=row["px"], sz=row["sz"],
            state="canceled", filled_sz=row["filled_sz"], avg_px=row["avg_px"],
            fee=row["fee"], create_time=row["create_time"], update_time=time.time(),
            raw_json=row["raw_json"], cl_ord_id=row["cl_ord_id"],
        ))
