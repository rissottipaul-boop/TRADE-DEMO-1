"""DCA-бот на споте BTC-USDT (задача p1-dca-demo, roadmap Фаза 2 п.2).

Первый торговый сценарий проекта. Цель — обкатка пайплайна
коннектор → риск → ордер-роутер → состояние, НЕ прибыль.

Машина состояний: IDLE → BUYING → COOLDOWN → (BUYING | DONE | PAUSED).
Состояние и счётчик покупок персистятся в каноническом хранилище storage.py
(таблица ws_state, ключи dca:state / dca:buys_done) — бот переживает
перезапуск: повторный старт с тем же max_buys новых покупок не делает.

Бот принимает готовый exchange-объект; ключи и .env он не читает.
Live (demo=False) — только через src.live_runner: с лимитом рукава
max_total_usdt и проверкой периметра before_buy (окно ops/live-policy.json,
карман ops/live-pocket.json, kill-флаг) перед КАЖДОЙ покупкой; без них
конструктор отказывает (insights/business-plan.md §3, блокер №2).

Метка владельца (ORDER-OWNER-TAG, src/order_owner.py): покупки уходят с
префиксом clOrdId `botsdca` в demo и `botldca` в live — live-владелец в demo
и demo-владелец в live конструктор не принимает.

Equity для риск-ядра по умолчанию — USDT-баланс (так было на demo, где
покупки на $5 теряются в 100k). В live runner передаёт equity_fn со стоимостью
всего кармана: иначе каждая покупка выглядела бы просадкой, и после траты 15%
кармана сработал бы глобальный breaker.

Особенность интеграции с риск-ядром: risk.check_entry_allowed запрещает
повторный вход по инструменту, по которому числится открытый риск
(«хедж запрещён»), а OrderRouter.place_order регистрирует вход после
каждой покупки. Спотовый DCA усредняет ОДНУ позицию без плеча и стопа,
поэтому после фиксации сделки слот risk_open_risk освобождается через
risk.register_spot_buy(inst_id) — явный API доливки спот-позиции:
в отличие от record_pnl он не трогает серии убытков, day_pnl и equity,
а брейкеры сработают при реальной просадке equity от HWM.
"""
import logging
import time
from typing import Any, Callable, Optional

from . import order_owner, risk
from .connector import okx_call
from .order_router import OrderRouter
from .storage import EquityRecord, Storage, TradeRecord

log = logging.getLogger("okx.dca_bot")

# Эмпирический практический минимум стоимости ордера BTC-USDT (phase0-smoke-test:
# ~$0.85 отклонён с 51020, ~$5 прошёл). С запасом держим не ниже $1.
DEFAULT_MIN_NOTIONAL_USDT = 1.0


class DCABot:
    """Простейший DCA: раз в interval_sec рыночная покупка на quote_per_buy_usdt."""

    STATE_IDLE = "IDLE"
    STATE_BUYING = "BUYING"
    STATE_COOLDOWN = "COOLDOWN"
    STATE_DONE = "DONE"
    STATE_PAUSED = "PAUSED"

    def __init__(
        self,
        exchange: Any,
        router: OrderRouter,
        *,
        inst_id: str = "BTC/USDT",          # unified-формат CCXT
        quote_per_buy_usdt: float = 5.0,
        interval_sec: float = 60.0,
        max_buys: int = 10,
        min_notional_usdt: float = DEFAULT_MIN_NOTIONAL_USDT,
        demo: bool = True,
        storage: Optional[Storage] = None,
        max_total_usdt: Optional[float] = None,
        before_buy: Optional[Callable[[], tuple[bool, str]]] = None,
        equity_fn: Optional[Callable[[], Optional[float]]] = None,
        sleep: Callable[[float], None] = time.sleep,
        owner: Optional[str] = None,  # код владельца clOrdId; None -> botsdca (demo) / botldca (live)
    ) -> None:
        if not demo and (not max_total_usdt or max_total_usdt <= 0 or before_buy is None):
            raise RuntimeError(
                "DCABot в live — только через src.live_runner: нужен лимит рукава "
                "max_total_usdt и проверка периметра before_buy. Live-запуск запрещён."
            )
        code = owner or (order_owner.DCA if demo else order_owner.LIVE_DCA)
        if order_owner.require(code, own=True).live == demo:
            raise RuntimeError(
                f"DCABot: владелец {code!r} не для режима {'demo' if demo else 'live'} — "
                f"live-ордера метятся только {order_owner.LIVE_FAMILY}* (src/order_owner.py)"
            )
        self.owner = code
        self.exchange = exchange
        self.router = router
        self.inst_id = inst_id
        self.interval_sec = float(interval_sec)
        self.max_buys = int(max_buys)
        # Хранилище общее с роутером (канон — storage.py)
        self.storage = storage or router.storage
        # minNotional считается по стоимости (51020) — берём с запасом x1.2
        if quote_per_buy_usdt < min_notional_usdt:
            bumped = min_notional_usdt * 1.2
            log.warning("quote_per_buy_usdt=%.2f ниже minNotional %.2f — поднято до %.2f",
                        quote_per_buy_usdt, min_notional_usdt, bumped)
            quote_per_buy_usdt = bumped
        self.quote_per_buy_usdt = float(quote_per_buy_usdt)
        self.max_total_usdt = max_total_usdt
        if max_total_usdt:
            # Лимит рукава в покупках: больше max_total_usdt не потратить никогда
            cap_buys = int(max_total_usdt // self.quote_per_buy_usdt)
            if cap_buys < self.max_buys:
                log.info("max_buys %d -> %d по лимиту рукава %.2f USDT",
                         self.max_buys, cap_buys, max_total_usdt)
                self.max_buys = cap_buys
        self.before_buy = before_buy
        self._equity_fn = equity_fn or self._fetch_equity
        self._sleep = sleep

        # Восстановление персистентного состояния (переживает рестарт)
        saved_state = self.storage.get_ws_state("dca:state")
        saved_buys = self.storage.get_ws_state("dca:buys_done")
        self._buys_done = int(float(saved_buys)) if saved_buys is not None else 0
        self._state = str(saved_state) if saved_state is not None else self.STATE_IDLE
        if self._state == self.STATE_DONE and self._buys_done < self.max_buys:
            # max_buys подняли после завершения — разрешаем докупить
            self._set_state(self.STATE_COOLDOWN)
        log.info("DCABot восстановлен: state=%s buys_done=%d/%d",
                 self._state, self._buys_done, self.max_buys)

    # --- Персистентность машины состояний ---

    def _set_state(self, new_state: str) -> None:
        if new_state != self._state:
            log.info("DCA: %s -> %s", self._state, new_state)
        self._state = new_state
        self.storage.set_ws_state("dca:state", new_state)

    def _save_counter(self) -> None:
        self.storage.set_ws_state("dca:buys_done", str(self._buys_done))

    # --- Рыночные данные ---

    def _fetch_equity(self) -> Optional[float]:
        """Equity в USDT из баланса (CCXT unified: balance['total']['USDT'])."""
        try:
            balance = okx_call(self.exchange.fetch_balance)
        except Exception as exc:
            log.warning("fetch_balance не удался (%s) — snapshot equity пропущен", exc)
            return None
        total = (balance.get("total") or {}).get("USDT")
        if total is None:
            total = (balance.get("USDT") or {}).get("total")
        return float(total) if total is not None else None

    def _fetch_price(self) -> float:
        ticker = okx_call(self.exchange.fetch_ticker, self.inst_id)
        price = float(ticker["last"])
        if price <= 0:
            raise ValueError(f"некорректная цена {self.inst_id}: {price}")
        return price

    # --- Один шаг покупки ---

    def _buy_once(self) -> bool:
        """Рыночная покупка на quote_per_buy_usdt. False = ошибка, бот на паузу."""
        price = self._fetch_price()
        sz = self.quote_per_buy_usdt / price
        log.info("покупка %d/%d: %s market buy sz=%.8f (~%.2f USDT @ %.1f)",
                 self._buys_done + 1, self.max_buys, self.inst_id, sz,
                 sz * price, price)
        result = self.router.place_order(
            self.inst_id, "buy", "market", px=price, sz=sz, owner=self.owner,
        )
        if not result.get("ok"):
            log.warning("place_order отклонён (stage=%s): %s",
                        result.get("stage"), result.get("reason"))
            return False
        ord_id = str(result.get("exchange_order_id") or result["order_id"])
        self.storage.insert_trade(TradeRecord(
            inst_id=self.inst_id,
            trade_id=ord_id,  # market-ордер: fill привязан к ordId; идемпотентно по PK
            ord_id=ord_id,
            side="buy",
            px=float(result.get("px") or price),
            sz=float(result["sz"]),
            fee=0.0,
            fee_ccy="USDT",
            ts=time.time(),
        ))
        # Освобождаем слот risk_open_risk: спотовый DCA усредняет одну позицию,
        # иначе риск-ядро заблокирует следующую покупку как «хедж».
        # register_spot_buy — НЕ record_pnl: покупка не закрытие сделки,
        # она не должна обнулять серии убытков системы.
        risk.register_spot_buy(self.inst_id)
        return True

    # --- Главный цикл ---

    def run(self) -> str:
        """Крутить машину состояний до DONE/PAUSED. Возвращает финальное состояние."""
        self._set_state(self.STATE_IDLE)
        try:
            while True:
                if self._buys_done >= self.max_buys:
                    log.info("max_buys=%d достигнут (buys_done=%d) — стоп",
                             self.max_buys, self._buys_done)
                    self._set_state(self.STATE_DONE)
                    break

                # 1. Снимок equity (обязателен каждую итерацию)
                equity = self._equity_fn()
                if equity is not None:
                    self.storage.record_equity(EquityRecord(
                        ts=time.time(), total_eq=equity, avail_eq=equity, upl=0.0,
                    ))
                    risk.update_equity(equity)
                    log.info("equity snapshot: %.2f USDT", equity)

                # 2. Допуск риск-слоя (kill_switch / breakers / лимиты)
                allowed, reason = risk.check_entry_allowed(self.inst_id, "buy")
                if not allowed:
                    if "kill_switch" in reason:
                        log.error("KILL-SWITCH активен — бот остановлен: %s", reason)
                    else:
                        log.warning("риск-слой отказал во входе — пауза: %s", reason)
                    self._set_state(self.STATE_PAUSED)
                    break

                # 2b. Периметр live (окно, карман, kill-флаг) — перед КАЖДОЙ покупкой
                if self.before_buy is not None:
                    allowed, reason = self.before_buy()
                    if not allowed:
                        log.warning("периметр отказал во входе — пауза: %s", reason)
                        self._set_state(self.STATE_PAUSED)
                        break

                # 3. Покупка
                self._set_state(self.STATE_BUYING)
                if not self._buy_once():
                    self._set_state(self.STATE_PAUSED)
                    break
                self._buys_done += 1
                self._save_counter()

                # 4. Завершение или cooldown
                if self._buys_done >= self.max_buys:
                    log.info("max_buys=%d достигнут — стоп", self.max_buys)
                    self._set_state(self.STATE_DONE)
                    break
                self._set_state(self.STATE_COOLDOWN)
                log.info("cooldown %.0fс до следующей покупки", self.interval_sec)
                self._sleep(self.interval_sec)
        except KeyboardInterrupt:
            log.warning("прервано оператором (Ctrl+C) — пауза")
            self._set_state(self.STATE_PAUSED)
        log.info("DCA-цикл завершён: state=%s buys_done=%d/%d",
                 self._state, self._buys_done, self.max_buys)
        return self._state
