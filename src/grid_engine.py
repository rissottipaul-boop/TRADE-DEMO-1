"""Собственная grid-сессия: уровни, fee-floor, hard stop (INSIGHTS-ALL-IMPL).

Основание — insights/grid-strategy-design.md:
- вход только в режиме Range (§1); ширина диапазона 5–15% (§2);
- пол шага 0.32% от экономики комиссий, стресс-ориентир 0.40% (§5.3);
- hard stop при выходе за [minPx, maxPx]: отмена уровней + рыночная
  ликвидация (аналог stopType=1) + один record_pnl + cooldown 4ч (§3);
- подтверждение пробоя ≥30 с по mid-цене, немедленно при уходе за границу
  больше чем на 1 шаг (§3.1);
- обслуживающие ордера сетки идут в обход register_entry: не более
  1 инкремента entries_today за сессию (§6.1).

Модуль чистый (без сети и ордеров): строит уровни, решает стоп, возвращает
план действий. Исполнение — вызывающий код (GRID-IMPL src/grid_bot.py).
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any

STEP_FLOOR_PCT = 0.32  # §5.3: maker round-trip 0.16% / 0.5
STRESS_STEP_PCT = 0.40  # §5.3: taker round-trip 0.20% / 0.5, ориентир
RANGE_MIN_PCT = 5.0
RANGE_MAX_PCT = 15.0
GRID_NUM_MIN = 2
GRID_NUM_MAX = 100
GRID_NUM_SOFT_MIN = 10  # §4.2: меньше — грубая сетка, расширить диапазон
BREAKOUT_CONFIRM_S = 30  # §3.1 подтверждение по mid-цене
COOLDOWN_HOURS = 4  # §3.2 п.4
MAKER_FEE = 0.0008  # resting limit (§5.1)

GRID_SERVICE_TAG = "botg"  # префикс clOrdId обслуживающих ордеров сетки


@dataclass(frozen=True)
class GridConfig:
    """Конфигурация одной grid-сессии (спот, geometric runType=2)."""

    inst_id: str
    min_px: float
    max_px: float
    grid_num: int
    investment_quote: float
    run_type: int = 2


@dataclass(frozen=True)
class StopPlan:
    """План hard stop §3.2: действия исполняет вызывающий код по порядку."""

    reason: str
    cancel_level_orders: bool = True
    liquidate_base_at_market: bool = True
    record_pnl_once: bool = True
    cooldown_hours: int = COOLDOWN_HOURS


def range_pct(cfg: GridConfig) -> float:
    """Ширина диапазона в % (§4.1)."""
    return (cfg.max_px - cfg.min_px) / cfg.min_px * 100.0


def step_pct(cfg: GridConfig) -> float:
    """Шаг уровня в % (§4.1): ln(max/min)/N×100."""
    return math.log(cfg.max_px / cfg.min_px) / cfg.grid_num * 100.0


def fee_share(cfg: GridConfig, taker_stress: bool = False) -> float:
    """Доля комиссий в валовой прибыли арбитража; требование ≤ 0.5 (§5.3)."""
    roundtrip = (0.0010 * 2) if taker_stress else (MAKER_FEE * 2)
    return roundtrip / (step_pct(cfg) / 100.0)


def validate_config(cfg: GridConfig) -> list[str]:
    """Проверка конфига; [] = можно стартовать. Fee-floor — до прогона (§8)."""
    reasons: list[str] = []
    if cfg.min_px <= 0 or cfg.max_px <= cfg.min_px:
        reasons.append(f"диапазон {cfg.min_px}/{cfg.max_px}: нужно 0 < min < max")
        return reasons
    if not (GRID_NUM_MIN <= cfg.grid_num <= GRID_NUM_MAX):
        reasons.append(f"gridNum {cfg.grid_num}: биржа требует 2…100")
    if cfg.grid_num < GRID_NUM_SOFT_MIN:
        reasons.append(f"gridNum {cfg.grid_num} < 10: сетка грубая — расширить диапазон (§4.2)")
    rp = range_pct(cfg)
    if not (RANGE_MIN_PCT <= rp <= RANGE_MAX_PCT):
        reasons.append(f"ширина {rp:.2f}%: коридор 5–15% (§2)")
    if cfg.run_type != 2:
        reasons.append(f"runType {cfg.run_type}: дизайн требует geometric (2)")
    if step_pct(cfg) < STEP_FLOOR_PCT:
        reasons.append(f"шаг {step_pct(cfg):.3f}% < пола {STEP_FLOOR_PCT}% (§5.3)")
    if cfg.investment_quote <= 0:
        reasons.append("investment_quote должен быть положительным")
    return reasons


def build_levels(cfg: GridConfig) -> tuple[float, ...]:
    """Цены уровней сетки (geometric, N+1 граница). Raises ValueError."""
    problems = validate_config(cfg)
    if problems:
        raise ValueError("; ".join(problems))
    ratio = (cfg.max_px / cfg.min_px) ** (1.0 / cfg.grid_num)
    return tuple(cfg.min_px * (ratio ** k) for k in range(cfg.grid_num + 1))


def stop_decision(cfg: GridConfig, mid_px: float,
                  seconds_outside: float) -> str:
    """Решение hard stop §3.1: 'none' | 'confirming' | 'trigger'.

    seconds_outside — сколько секунд mid-цена непрерывно вне [minPx, maxPx].
    Немедленный триггер — уход за границу больше чем на 1 шаг сетки.
    """
    if cfg.min_px <= mid_px <= cfg.max_px:
        return "none"
    step_abs = (cfg.max_px - cfg.min_px) / cfg.grid_num
    outside_by = cfg.min_px - mid_px if mid_px < cfg.min_px else mid_px - cfg.max_px
    if outside_by > step_abs:
        return "trigger"
    if seconds_outside >= BREAKOUT_CONFIRM_S:
        return "trigger"
    return "confirming"


def stop_plan(reason: str) -> StopPlan:
    """План действий §3.2 по сработавшему стопу."""
    return StopPlan(reason=reason)


def execute_stop_accounting(inst_id: str, session_pnl: float,
                            closed_at: Any, risk_mod: Any) -> list[str]:
    """Учёт стопа §3.2 пп.3–4: ОДИН record_pnl на сессию + cooldown 4ч.

    risk_mod — модуль риска (в проде src.risk, в тестах фейк).
    Возвращает события record_pnl.
    """
    events = risk_mod.record_pnl(inst_id, session_pnl, closed_at)
    risk_mod.block_instrument(inst_id, COOLDOWN_HOURS)
    return list(events)


def is_grid_service(cl_ord_id: str | None) -> bool:
    """Обслуживающий ордер сетки (§6.1): идёт в обход register_entry."""
    return bool(cl_ord_id) and str(cl_ord_id).startswith(GRID_SERVICE_TAG)
