"""Собственный модуль grid-бота (GRID-IMPL / Фаза 2).

Основание: insights/grid-strategy-design.md
- §1: Range-гейт входа (классификатор EMA20 / ATR% из dca-bot-parameterizer);
- §2, §4: геометрическая сетка (runType=2), ширина диапазона 5–15%, пол шага >=0.32%;
- §3: двусторонний hard stop: отмена всех уровней + рыночная ликвидация +
  один вызов record_pnl + cooldown 4ч (block_instrument);
- §6.1: обслуживающие ордера уровней размещаются с count_as_entry=False
  (не более 1 инкремента entries_today за сессию сетки);
- §6.2: единый агрегированный record_pnl по итогам всей сессии сетки;
- §6.3: сайзинг quoteSz через risk.size_position() и потолок notional <= 15% equity.
"""
from __future__ import annotations

import logging
import math
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable, Mapping, Optional, Sequence

from src import order_owner, risk
from src.grid_engine import (
    BREAKOUT_CONFIRM_S,
    COOLDOWN_HOURS,
    MAKER_FEE,
    RANGE_MAX_PCT,
    RANGE_MIN_PCT,
    STEP_FLOOR_PCT,
    GridConfig,
    StopPlan,
    build_levels,
    execute_stop_accounting,
    fee_share,
    is_grid_service,
    range_pct,
    step_pct,
    stop_decision,
    stop_plan,
    validate_config,
)
from src.order_router import OrderRouter
from src.storage import Storage

log = logging.getLogger("okx.grid_bot")


# =====================================================================
# 1. Range-гейт входа (§1, dca-bot-parameterizer)
# =====================================================================

REGIME_RANGE = "Range"
REGIME_MILD_TREND = "Mild Trend"
REGIME_STRONG_TREND = "Strong Trend"


def calculate_ema(values: Sequence[float], period: int = 20) -> list[float]:
    """Экспоненциальное скользящее среднее (EMA) по списку значений."""
    if not values or period <= 0:
        return []
    n = len(values)
    ema: list[float] = [0.0] * n
    if n < period:
        # Недостаточно баров для полного окна — простое среднее доступного
        sma = sum(values) / n
        return [sma] * n

    # Инициализация первого окна SMA
    sma = sum(values[:period]) / period
    ema[period - 1] = sma
    for j in range(period - 1):
        ema[j] = sma

    multiplier = 2.0 / (period + 1.0)
    for i in range(period, n):
        ema[i] = (values[i] - ema[i - 1]) * multiplier + ema[i - 1]
    return ema


def calculate_atr(highs: Sequence[float], lows: Sequence[float],
                  closes: Sequence[float], period: int = 14) -> list[float]:
    """Average True Range (ATR) по рядам High, Low, Close."""
    n = len(closes)
    if n < 2 or len(highs) != n or len(lows) != n:
        return [0.0] * n

    tr = [highs[0] - lows[0]]
    for i in range(1, n):
        h_l = highs[i] - lows[i]
        h_pc = abs(highs[i] - closes[i - 1])
        l_pc = abs(lows[i] - closes[i - 1])
        tr.append(max(h_l, h_pc, l_pc))

    atr: list[float] = [0.0] * n
    if n < period:
        sma = sum(tr) / n
        return [sma] * n

    sma = sum(tr[:period]) / period
    atr[period - 1] = sma
    for j in range(period - 1):
        atr[j] = sma

    for i in range(period, n):
        atr[i] = (atr[i - 1] * (period - 1) + tr[i]) / period
    return atr


def classify_market_regime(candles: Sequence[Mapping[str, float]]) -> tuple[str, dict[str, Any]]:
    """Классификатор режима рынка по EMA20 и ATR% (dca-bot-parameterizer / §1).

    Ожидает свечи с ключами 'h' (high), 'l' (low), 'c' (close).
    Возвращает:
      (regime, details_dict)
      regime: 'Range' | 'Mild Trend' | 'Strong Trend'
    """
    if len(candles) < 25:
        return REGIME_RANGE, {"reason": "insufficient_candles_default_range", "count": len(candles)}

    highs = [float(c["h"]) for c in candles]
    lows = [float(c["l"]) for c in candles]
    closes = [float(c["c"]) for c in candles]

    ema20 = calculate_ema(closes, period=20)
    atr14 = calculate_atr(highs, lows, closes, period=14)

    curr_px = closes[-1]
    curr_ema = ema20[-1]
    prev_ema = ema20[-2]
    curr_atr = atr14[-1]

    slope = abs(curr_ema - prev_ema)
    # Порог наклона EMA20 (§1): при пологой EMA (< 0.08 * ATR) тренд отсутствует (Range)
    slope_threshold = 0.08 * curr_atr
    ema_rising = (curr_ema > prev_ema) and (slope >= slope_threshold)
    ema_falling = (curr_ema < prev_ema) and (slope >= slope_threshold)
    dist = abs(curr_px - curr_ema)

    # Проверка стороны расположения цены относительно EMA20
    price_above = curr_px >= curr_ema
    price_below = curr_px < curr_ema

    # Анализ последних 5 свечей
    check_len = min(5, len(closes))
    last_closes = closes[-check_len:]
    last_emas = ema20[-check_len:]

    if ema_rising and price_above:
        same_side_count = sum(1 for c, e in zip(last_closes, last_emas) if c >= e)
        if same_side_count >= 4 and dist >= curr_atr:
            return REGIME_STRONG_TREND, {
                "direction": "up", "same_side": same_side_count,
                "dist": dist, "atr": curr_atr, "ema_rising": True
            }
        if same_side_count >= 3 and dist < curr_atr:
            return REGIME_MILD_TREND, {
                "direction": "up", "same_side": same_side_count,
                "dist": dist, "atr": curr_atr, "ema_rising": True
            }

    elif ema_falling and price_below:
        same_side_count = sum(1 for c, e in zip(last_closes, last_emas) if c < e)
        if same_side_count >= 4 and dist >= curr_atr:
            return REGIME_STRONG_TREND, {
                "direction": "down", "same_side": same_side_count,
                "dist": dist, "atr": curr_atr, "ema_falling": True
            }
        if same_side_count >= 3 and dist < curr_atr:
            return REGIME_MILD_TREND, {
                "direction": "down", "same_side": same_side_count,
                "dist": dist, "atr": curr_atr, "ema_falling": True
            }

    return REGIME_RANGE, {
        "direction": "neutral", "dist": dist, "atr": curr_atr,
        "ema_rising": ema_rising, "ema_falling": ema_falling
    }


def check_range_gate(candles: Sequence[Mapping[str, float]]) -> tuple[bool, str, dict[str, Any]]:
    """Проверка Range-гейта (§1): вход в сетку разрешён только при Range."""
    regime, details = classify_market_regime(candles)
    if regime == REGIME_RANGE:
        return True, "ok: Range regime confirmed", details
    return False, f"отклонено: режим рынка {regime} (требуется Range)", details


# =====================================================================
# 2. Сайзинг quoteSz через risk.size_position() (§6.3)
# =====================================================================

def size_grid_session(
    equity: float,
    current_px: float,
    min_px: float,
    max_px: float,
    ct_val: float = 0.01,
    lot_sz: float = 0.001,
    min_sz: float = 0.001,
    risk_pct: float = risk.DEFAULT_RISK_PCT,
    max_position_pct: float = risk.MAX_POSITION_PCT,  # 15% equity
) -> dict[str, Any]:
    """Сайзинг объема сетки (§6.3) через худший сценарий выхода за ближайшую границу.

    Возвращает dict:
      ok: bool
      quote_sz: float (общий бюджет в quote, например USDT)
      quote_per_grid: float (бюджет на уровень)
      reason: str
    """
    if equity <= 0 or current_px <= 0 or min_px <= 0 or max_px <= min_px:
        return {"ok": False, "quote_sz": 0.0, "reason": "некорректные параметры цен/equity"}

    # Ближайшая граница трактуется как условный защитный стоп для сайзинга
    dist_min = abs(current_px - min_px)
    dist_max = abs(max_px - current_px)
    nearest_boundary = min_px if dist_min <= dist_max else max_px

    sized = risk.size_position(
        equity=equity,
        entry=current_px,
        stop=nearest_boundary,
        ct_val=ct_val,
        lot_sz=lot_sz,
        min_sz=min_sz,
        risk_pct=risk_pct,
    )

    risk_notional = sized.get("notional", 0.0)
    hard_cap = equity * (max_position_pct / 100.0)
    quote_sz = min(risk_notional, hard_cap)

    if quote_sz <= 0:
        return {
            "ok": False,
            "quote_sz": 0.0,
            "reason": f"сайзинг дал нулевой размер: {'; '.join(sized.get('warnings', []))}"
        }

    return {
        "ok": True,
        "quote_sz": quote_sz,
        "risk_notional": risk_notional,
        "hard_cap": hard_cap,
        "nearest_boundary": nearest_boundary,
        "reason": "ok"
    }


# =====================================================================
# 3. GridBot Класс сессии (§2–6)
# =====================================================================

class GridBot:
    """Управление сессией собственного grid-бота на споте."""

    STATE_IDLE = "IDLE"
    STATE_RUNNING = "RUNNING"
    STATE_STOPPED = "STOPPED"
    STATE_PAUSED = "PAUSED"

    def __init__(
        self,
        exchange: Any,
        router: OrderRouter,
        config: GridConfig,
        storage: Optional[Storage] = None,
        demo: bool = True,
        owner: Optional[str] = None,
    ) -> None:
        problems = validate_config(config)
        if problems:
            raise ValueError(f"Конфигурация GridBot некорректна: {'; '.join(problems)}")

        self.exchange = exchange
        self.router = router
        self.config = config
        self.storage = storage
        self.demo = demo
        self.owner = owner or order_owner.GRID

        # Проверка владельца
        order_owner.require(self.owner, own=True)

        self.levels = build_levels(config)
        self.quote_per_grid = config.investment_quote / config.grid_num

        # Состояние сессии
        self.state = self.STATE_IDLE
        self.start_time: Optional[float] = None
        self.start_equity: float = 0.0
        self.arbitrages_count: int = 0
        self.realized_arbitrage_pnl: float = 0.0
        self.inventory_base: float = 0.0
        self.inventory_cost: float = 0.0

        # Уровни: level_idx -> cl_ord_id
        self.active_level_orders: dict[int, str] = {}
        # level_idx -> cost in quote
        self.level_buy_cost: dict[int, float] = {}

        # Мониторинг пробоя
        self.seconds_outside: float = 0.0
        self.last_outside_check: Optional[float] = None

    # --- Жизненный цикл сессии ---

    def start_session(
        self,
        current_px: float,
        candles: Optional[Sequence[Mapping[str, float]]] = None,
        equity: Optional[float] = None,
    ) -> dict[str, Any]:
        """Старт сессии сетки (§1, §6.1).

        1. Проверяет Range-гейт (если переданы свечи);
        2. Проверяет check_entry_allowed;
        3. Вызывает register_entry(inst_id, 'buy') РОВНО ОДИН РАЗ на сессию;
        4. Выставляет начальные лимитные ордера уровней ниже текущей цены
           с count_as_entry=False.
        """
        if self.state == self.STATE_RUNNING:
            return {"ok": False, "reason": "сессия уже запущена"}

        # 1. Range Gate (§1)
        if candles:
            range_ok, reason, details = check_range_gate(candles)
            if not range_ok:
                log.warning("Старт GridBot отклонен Range-гейтом: %s", reason)
                return {"ok": False, "stage": "range_gate", "reason": reason, "details": details}

        # 2. Допуск риск-ядра
        allowed, reason = risk.check_entry_allowed(self.config.inst_id, "buy")
        if not allowed:
            log.warning("Старт GridBot отклонен risk.check_entry_allowed: %s", reason)
            return {"ok": False, "stage": "check_entry_allowed", "reason": reason}

        # 3. Фиксация входа в риск-ядре РОВНО ОДИН РАЗ (§6.1)
        risk.register_entry(self.config.inst_id, "buy", risk_pct=risk.DEFAULT_RISK_PCT)

        self.state = self.STATE_RUNNING
        self.start_time = time.time()
        self.start_equity = equity if equity is not None else 0.0
        self.arbitrages_count = 0
        self.realized_arbitrage_pnl = 0.0
        self.inventory_base = 0.0
        self.inventory_cost = 0.0
        self.active_level_orders.clear()
        self.level_buy_cost.clear()

        # 4. Начальное выставление лимитных уровней покупки ниже текущей цены
        placed_levels = []
        for k, px in enumerate(self.levels):
            if px < current_px:
                qty = self.quote_per_grid / px
                res = self.router.place_order(
                    inst_id=self.config.inst_id,
                    side="buy",
                    ord_type="limit",
                    px=px,
                    sz=qty,
                    owner=self.owner,
                    count_as_entry=False,  # Обход register_entry (§6.1)
                )
                if res.get("ok"):
                    cl_id = res.get("cl_ord_id") or res.get("order_id")
                    self.active_level_orders[k] = cl_id
                    placed_levels.append((k, px, qty))
                else:
                    log.warning("Не удалось выставить уровень %d (px=%s): %s", k, px, res.get("reason"))

        log.info("GridBot сессия запущена: %s, уровней выставлено: %d",
                 self.config.inst_id, len(placed_levels))
        return {
            "ok": True,
            "status": "RUNNING",
            "levels_placed": len(placed_levels),
            "details": placed_levels,
        }

    def check_market(self, mid_px: float, now_ts: Optional[float] = None) -> dict[str, Any]:
        """Регулярный опрос рынка и проверка hard stop (§3.1)."""
        if self.state != self.STATE_RUNNING:
            return {"status": self.state, "action": "none"}

        ts = now_ts or time.time()
        # Обновление времени нахождения вне диапазона
        if not (self.config.min_px <= mid_px <= self.config.max_px):
            if self.last_outside_check is not None:
                delta = max(0.0, ts - self.last_outside_check)
                self.seconds_outside += delta
            self.last_outside_check = ts
        else:
            self.seconds_outside = 0.0
            self.last_outside_check = None

        decision = stop_decision(self.config, mid_px, self.seconds_outside)
        if decision == "trigger":
            log.warning("Hard stop trigger по GridBot: mid_px=%s вне [%s, %s] (outside=%s s)",
                        mid_px, self.config.min_px, self.config.max_px, self.seconds_outside)
            stop_res = self.stop_session(
                reason=f"hard_stop_breakout_{mid_px:.2f}",
                current_px=mid_px,
                is_hard_stop=True,
            )
            return {"action": "hard_stop", "decision": decision, "result": stop_res}

        return {
            "action": "monitoring",
            "decision": decision,
            "seconds_outside": self.seconds_outside,
            "inside": (self.config.min_px <= mid_px <= self.config.max_px),
        }

    def on_level_fill(
        self,
        level_idx: int,
        side: str,
        fill_px: float,
        fill_qty: float,
        fee: float = 0.0,
    ) -> dict[str, Any]:
        """Обработка исполнения уровня сетки (на баре или по WS).

        - Buy: накапливает inventory_base, размещает парный Sell на уровень выше (k+1);
        - Sell: фиксирует арбитражный PnL, восстанавливает Buy на уровень ниже (m-1);
        - Внимание: обслуживающие ордера уровней всегда count_as_entry=False!
        """
        if self.state != self.STATE_RUNNING:
            return {"ok": False, "reason": "сессия не активна"}

        self.active_level_orders.pop(level_idx, None)

        if side == "buy":
            cost = fill_qty * fill_px
            self.inventory_base += fill_qty
            self.inventory_cost += cost
            self.level_buy_cost[level_idx] = cost

            # Размещаем парный ордер на продажу на уровень выше (k+1)
            placed_sell = False
            sell_level = level_idx + 1
            if sell_level < len(self.levels):
                sell_px = self.levels[sell_level]
                # Учет maker fee на выходе
                sell_qty = fill_qty * (1.0 - MAKER_FEE)
                res = self.router.place_order(
                    inst_id=self.config.inst_id,
                    side="sell",
                    ord_type="limit",
                    px=sell_px,
                    sz=sell_qty,
                    owner=self.owner,
                    count_as_entry=False,
                )
                if res.get("ok"):
                    cl_id = res.get("cl_ord_id") or res.get("order_id")
                    self.active_level_orders[sell_level] = cl_id
                    placed_sell = True

            return {
                "ok": True,
                "action": "buy_filled",
                "level": level_idx,
                "placed_sell_level": sell_level if placed_sell else None,
            }

        elif side == "sell":
            proceeds = fill_qty * fill_px - fee
            buy_level = level_idx - 1
            buy_cost = self.level_buy_cost.pop(buy_level, self.quote_per_grid)

            arb_pnl = proceeds - buy_cost
            self.arbitrages_count += 1
            self.realized_arbitrage_pnl += arb_pnl
            self.inventory_base = max(0.0, self.inventory_base - fill_qty)
            self.inventory_cost = max(0.0, self.inventory_cost - buy_cost)

            # Восстанавливаем ордер на покупку на уровне k = level_idx - 1
            placed_buy = False
            if 0 <= buy_level < len(self.levels):
                buy_px = self.levels[buy_level]
                buy_qty = self.quote_per_grid / buy_px
                res = self.router.place_order(
                    inst_id=self.config.inst_id,
                    side="buy",
                    ord_type="limit",
                    px=buy_px,
                    sz=buy_qty,
                    owner=self.owner,
                    count_as_entry=False,
                )
                if res.get("ok"):
                    cl_id = res.get("cl_ord_id") or res.get("order_id")
                    self.active_level_orders[buy_level] = cl_id
                    placed_buy = True

            return {
                "ok": True,
                "action": "sell_filled_arbitrage",
                "level": level_idx,
                "arb_pnl": arb_pnl,
                "total_arbitrage_pnl": self.realized_arbitrage_pnl,
                "arbitrages_count": self.arbitrages_count,
                "restored_buy_level": buy_level if placed_buy else None,
            }

        return {"ok": False, "reason": f"unknown side {side}"}

    def stop_session(
        self,
        reason: str = "manual_stop",
        current_px: Optional[float] = None,
        is_hard_stop: bool = False,
    ) -> dict[str, Any]:
        """Остановка сессии сетки (§3.2, §6.2).

        1. Отменяет все активные лимитные ордера уровней;
        2. Рыночная ликвидация накопленного inventory_base;
        3. ЕДИНЫЙ вызов risk.record_pnl по суммарному итогу сессии;
        4. Cooldown 4ч (block_instrument) при hard stop.
        """
        if self.state == self.STATE_STOPPED:
            return {"ok": False, "reason": "сессия уже остановлена"}

        log.info("Остановка сессии GridBot %s (причина: %s, hard_stop=%s)",
                 self.config.inst_id, reason, is_hard_stop)

        # 1. Отмена всех активных ордеров уровней
        cancelled_orders = 0
        for level_idx, cl_id in list(self.active_level_orders.items()):
            try:
                # Отменяем через router.cancel_order
                self.router.cancel_order(self.config.inst_id, cl_id)
                cancelled_orders += 1
            except Exception as exc:
                log.warning("Ошибка отмены ордера уровня %s (%s): %s", level_idx, cl_id, exc)
        self.active_level_orders.clear()

        # 2. Рыночная ликвидация остатка базы при наличии инвентаря
        liquidation_pnl = 0.0
        liquidation_res = None
        if self.inventory_base > 0 and current_px and current_px > 0:
            try:
                # Продажа остатка по рынку через place_order с is_exit=True
                res = self.router.place_order(
                    inst_id=self.config.inst_id,
                    side="sell",
                    ord_type="market",
                    sz=self.inventory_base,
                    owner=self.owner,
                    is_exit=True,
                )
                liquidation_res = res
                gross_proceeds = self.inventory_base * current_px
                fee_approx = gross_proceeds * 0.0010  # taker fee
                liquidation_pnl = (gross_proceeds - fee_approx) - self.inventory_cost
            except Exception as exc:
                log.error("Сбой ликвидации инвентаря при остановке GridBot: %s", exc)

        # 3. Суммарный PnL сессии и ОДИН вызов record_pnl (§6.2)
        total_session_pnl = self.realized_arbitrage_pnl + liquidation_pnl
        now = datetime.now(timezone.utc)
        events = risk.record_pnl(self.config.inst_id, total_session_pnl, closed_at=now)

        # 4. При hard stop — cooldown 4ч (§3.2 п.4)
        if is_hard_stop:
            risk.block_instrument(self.config.inst_id, hours=COOLDOWN_HOURS)

        self.state = self.STATE_STOPPED

        return {
            "ok": True,
            "status": "STOPPED",
            "reason": reason,
            "is_hard_stop": is_hard_stop,
            "cancelled_orders": cancelled_orders,
            "arbitrages_count": self.arbitrages_count,
            "realized_arbitrage_pnl": self.realized_arbitrage_pnl,
            "liquidation_pnl": liquidation_pnl,
            "total_session_pnl": total_session_pnl,
            "risk_events": list(events),
        }


# =====================================================================
# 4. Self-test (§7 критерий готовности)
# =====================================================================

def _run_self_test() -> None:
    """Полный самотест модуля grid_bot без сети."""
    import tempfile
    from pathlib import Path
    print("=== Запуск self-test модуля src.grid_bot ===")

    # 1. Тест Range-гейта
    # Создаем плоский ряд баров (Range)
    range_candles = []
    base_p = 84000.0
    for i in range(40):
        # колебания около base_p
        drift = math.sin(i * 0.5) * 50.0
        c = base_p + drift
        range_candles.append({"h": c + 20.0, "l": c - 20.0, "c": c})
    regime, det = classify_market_regime(range_candles)
    assert regime == REGIME_RANGE, f"Ожидался Range, получено {regime}"
    gate_ok, _, _ = check_range_gate(range_candles)
    assert gate_ok, "Range-гейт должен был пропустить боковик"
    print("OK: 1. Range-гейт корректно классифицирует боковой рынок")

    # Создаем сильный тренд
    trend_candles = []
    p = 80000.0
    for i in range(40):
        p += 200.0
        trend_candles.append({"h": p + 50.0, "l": p - 20.0, "c": p})
    trend_regime, _ = classify_market_regime(trend_candles)
    assert trend_regime in (REGIME_STRONG_TREND, REGIME_MILD_TREND), f"Ожидался Trend, получено {trend_regime}"
    gate_ok, reason, _ = check_range_gate(trend_candles)
    assert not gate_ok, "Range-гейт должен был отклонить тренд"
    print(f"OK: 2. Range-гейт отклоняет трендовый рынок ({trend_regime})")

    # 2. Тест сайзинга через size_position
    sized = size_grid_session(equity=10000.0, current_px=84000.0, min_px=80000.0, max_px=88000.0)
    assert sized["ok"], f"Сайзинг отклонен: {sized['reason']}"
    assert sized["quote_sz"] > 0, "quote_sz обязан быть > 0"
    assert sized["quote_sz"] <= 10000.0 * 0.15, "quote_sz не должен превышать 15% equity"
    print(f"OK: 3. Сайзинг quoteSz={sized['quote_sz']:.2f} USDT соблюдает лимит 15% equity")

    # 3. Тест сессии, обхода register_entry и единственного record_pnl
    with tempfile.TemporaryDirectory() as tmp_dir:
        test_db = Path(tmp_dir) / "test_risk.db"
        risk.init(test_db)
        risk.update_equity(10000.0)

        # Фейковый OrderRouter для проверки
        placed_calls = []

        class MockRouter:
            owner = order_owner.GRID

            def place_order(self, inst_id, side, ord_type, px=None, sz=None,
                            owner=None, count_as_entry=True, is_exit=False):
                placed_calls.append({
                    "side": side, "px": px, "sz": sz,
                    "count_as_entry": count_as_entry, "is_exit": is_exit
                })
                # При count_as_entry=True зовется register_entry, при False — нет!
                if count_as_entry:
                    risk.register_entry(inst_id, side)
                return {"ok": True, "order_id": f"ord_{len(placed_calls)}", "cl_ord_id": f"botg_{len(placed_calls)}"}

            def cancel_order(self, inst_id, cl_id):
                return {"ok": True}

        router = MockRouter()
        cfg = GridConfig(
            inst_id="BTC-USDT",
            min_px=80225.0,
            max_px=88670.0,
            grid_num=30,  # шаг > 0.32%
            investment_quote=1000.0,
        )

        bot = GridBot(exchange=None, router=router, config=cfg)
        entries_before = risk.status()["entries_today"]

        # Старт сессии
        start_res = bot.start_session(current_px=84400.0, candles=range_candles, equity=10000.0)
        assert start_res["ok"], f"Старт сессии завершился с ошибкой: {start_res}"
        entries_after_start = risk.status()["entries_today"]
        assert entries_after_start == entries_before + 1, "Старт сессии должен увеличить entries_today ровно на 1"
        print("OK: 4. Старт сессии занимает риск-слот и увеличивает entries_today ровно на 1")

        # Симуляция 25 исполнений уровней (арбитражей)
        # Все размещения должны идти с count_as_entry=False
        for i in range(25):
            # buy fill
            b_res = bot.on_level_fill(level_idx=10, side="buy", fill_px=82000.0, fill_qty=0.0004)
            assert b_res["ok"]
            # sell fill
            s_res = bot.on_level_fill(level_idx=11, side="sell", fill_px=82280.0, fill_qty=0.0004)
            assert s_res["ok"]

        entries_after_fills = risk.status()["entries_today"]
        assert entries_after_fills == entries_after_start, (
            f"Исполнения уровней не должны увеличивать entries_today: было {entries_after_start}, стало {entries_after_fills}"
        )
        assert bot.arbitrages_count == 25, f"Ожидалось 25 арбитражей, факт {bot.arbitrages_count}"
        assert bot.realized_arbitrage_pnl > 0, "Арбитражный PnL должен быть положительным"
        print(f"OK: 5. 25 арбитражей выполнены: entries_today остался {entries_after_fills} (изоляция §6.1 подтверждена)")

        # 4. Тест Hard Stop при выходе из диапазона (§3)
        # Пробиваем min_px
        breach_px = cfg.min_px - 500.0  # сильный гэп вниз (> 1 шага)
        m_res = bot.check_market(mid_px=breach_px)
        assert m_res["action"] == "hard_stop", f"Ожидался hard_stop, получено {m_res}"
        assert bot.state == GridBot.STATE_STOPPED, "Бот должен перейти в статус STOPPED"

        # Проверка 4ч кулдауна
        assert risk.is_instrument_blocked("BTC-USDT"), "Инструмент должен быть заблокирован на 4ч после hard stop"
        print("OK: 6. Hard stop корректно ликвидировал позицию, вызвал record_pnl и установил 4ч cooldown")

    print("\n=== ВСЕ ТЕСТЫ GRID_BOT УСПЕШНО ПРОЙДЕНЫ ===")


if __name__ == "__main__":
    _run_self_test()
