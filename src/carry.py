"""Сигнальный слой Funding Carry (правило E из funding-carry.md).

Данные для сигнала берутся только из live-архива realizedRate. Построение пары
переиспользует carry_executor и риск-ядро; исполнение деривативов здесь не
обходится через сырой API.
"""
from __future__ import annotations

import math
import sqlite3
import time
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from enum import Enum
from typing import Any, Sequence

from . import carry_executor
from .funding_carry import CarryLegs

PERIODS_PER_YEAR = 1095
LOOKBACK_PERIODS = 90
ENTRY_APY = 0.03
EXIT_APY = -0.05
DECISION_INTERVAL_MS = 8 * 60 * 60 * 1000
MAX_RATE_AGE_MS = DECISION_INTERVAL_MS
MIN_LIQUIDATION_DISTANCE_PCT = 10.0
SUPPORTED_INSTRUMENTS = {
    "BTC-USDT-SWAP": "BTC-USDT",
    "ETH-USDT-SWAP": "ETH-USDT",
}


class CarryDataError(ValueError):
    """Архив funding не позволяет принять безопасное решение."""


class CarryAction(str, Enum):
    WAIT = "wait"
    HOLD = "hold"
    ENTER = "enter"
    EXIT = "exit"


@dataclass(frozen=True)
class CarryConfig:
    """Пороги правила E, централизованные в конфигурации стратегии."""

    entry_apy: float = ENTRY_APY
    exit_apy: float = EXIT_APY

    def __post_init__(self) -> None:
        if not isinstance(self.entry_apy, (int, float)) or not math.isfinite(self.entry_apy) \
                or self.entry_apy <= 0:
            raise ValueError("entry_apy должен быть положительным конечным числом")
        if not isinstance(self.exit_apy, (int, float)) or not math.isfinite(self.exit_apy) \
                or self.exit_apy >= 0:
            raise ValueError("exit_apy должен быть отрицательным конечным числом")


DEFAULT_CONFIG = CarryConfig()


@dataclass(frozen=True)
class CarryDecision:
    inst_id: str
    action: CarryAction
    annualized_rate: float | None
    funding_time: int | None
    periods: int
    reason: str


@dataclass(frozen=True)
class LiquidationMonitor:
    distance_pct: float
    should_exit: bool
    reason: str


def _validate_instrument(inst_id: str) -> None:
    if inst_id not in SUPPORTED_INSTRUMENTS:
        raise ValueError(
            f"{inst_id!r}: carry поддерживает только {sorted(SUPPORTED_INSTRUMENTS)}")


def load_realized_series(
    conn: sqlite3.Connection,
    inst_id: str,
    *,
    now_ms: int | None = None,
) -> list[tuple[int, str]]:
    """Загрузить 90 свежих live realizedRate; fundingRate не является fallback."""
    _validate_instrument(inst_id)
    now_ms = int(time.time() * 1000) if now_ms is None else now_ms
    if not isinstance(now_ms, int) or now_ms <= 0:
        raise ValueError(f"now_ms {now_ms!r}: ожидается положительный timestamp в мс")

    rows = list(conn.execute(
        "SELECT funding_time, realized_rate FROM funding_rate "
        "WHERE inst_id=? ORDER BY funding_time DESC LIMIT ?",
        (inst_id, LOOKBACK_PERIODS),
    ))
    rows.reverse()
    if len(rows) != LOOKBACK_PERIODS:
        raise CarryDataError(
            f"{inst_id}: для правила E нужно {LOOKBACK_PERIODS} периодов realizedRate, "
            f"в архиве {len(rows)}")
    if any(rate is None or rate == "" for _, rate in rows):
        raise CarryDataError(f"{inst_id}: в 90-периодном окне отсутствует realizedRate")
    if now_ms < rows[-1][0] or now_ms - rows[-1][0] > MAX_RATE_AGE_MS:
        raise CarryDataError(
            f"{inst_id}: последний live funding-период устарел или находится в будущем")
    return [(int(timestamp), str(rate)) for timestamp, rate in rows]


def _annualized_mean(series: Sequence[tuple[int, str | float]]) -> float:
    if len(series) < LOOKBACK_PERIODS:
        raise CarryDataError(
            f"для правила E нужно {LOOKBACK_PERIODS} периодов, получено {len(series)}")
    window = series[-LOOKBACK_PERIODS:]
    timestamps: list[int] = []
    rates: list[Decimal] = []
    for timestamp, raw_rate in window:
        if not isinstance(timestamp, int) or timestamp <= 0:
            raise CarryDataError(f"битый funding_time: {timestamp!r}")
        try:
            rate = Decimal(str(raw_rate))
        except (InvalidOperation, ValueError):
            raise CarryDataError(f"битый realizedRate: {raw_rate!r}") from None
        if not rate.is_finite():
            raise CarryDataError(f"нечисловой realizedRate: {raw_rate!r}")
        timestamps.append(timestamp)
        rates.append(rate)
    for previous, current in zip(timestamps, timestamps[1:]):
        if current <= previous:
            raise CarryDataError("funding_time должен строго возрастать без дублей")
        if current - previous > DECISION_INTERVAL_MS:
            raise CarryDataError(
                f"пропуск live funding-периода: шаг {current - previous} мс")
    mean_rate = sum(rates, Decimal(0)) / Decimal(LOOKBACK_PERIODS)
    return float(mean_rate * PERIODS_PER_YEAR)


def evaluate_rule(
    inst_id: str,
    realized_rates: Sequence[tuple[int, str | float]],
    *,
    has_position: bool,
    last_evaluated_funding_time: int | None = None,
    config: CarryConfig = DEFAULT_CONFIG,
) -> CarryDecision:
    """Оценить E на новом периоде: вход >3%, выход <−5% годовых.

    Вызывающая сторона передаёт закрытый live-период. Повторная оценка до
    следующего 8-часового периода возвращает WAIT, не переиспользуя сигнал.
    """
    _validate_instrument(inst_id)
    annualized_rate = _annualized_mean(realized_rates)
    funding_time = realized_rates[-1][0]
    if last_evaluated_funding_time is not None:
        if not isinstance(last_evaluated_funding_time, int) or last_evaluated_funding_time <= 0:
            raise ValueError("last_evaluated_funding_time должен быть положительным timestamp")
        if funding_time - last_evaluated_funding_time < DECISION_INTERVAL_MS:
            return CarryDecision(
                inst_id, CarryAction.WAIT, annualized_rate, funding_time,
                LOOKBACK_PERIODS, "нового периода для решения раз в 8 часов нет",
            )

    if has_position and annualized_rate < config.exit_apy:
        action = CarryAction.EXIT
        reason = f"A90 {annualized_rate:.4%} ниже порога выхода {config.exit_apy:.0%}"
    elif not has_position and annualized_rate > config.entry_apy:
        action = CarryAction.ENTER
        reason = f"A90 {annualized_rate:.4%} выше порога входа {config.entry_apy:.0%}"
    else:
        action = CarryAction.HOLD
        threshold = config.exit_apy if has_position else config.entry_apy
        reason = f"A90 {annualized_rate:.4%} не пересёк порог {threshold:.0%}"
    return CarryDecision(
        inst_id, action, annualized_rate, funding_time, LOOKBACK_PERIODS, reason)


def build_entry_plan(
    decision: CarryDecision,
    *,
    planned_hold_days: float,
    spot_px: float,
    swap_px: float | None = None,
    equity: float,
    acct_lv: int,
    **sizing_options: Any,
) -> carry_executor.CarryPlan:
    """Собрать риск-гейтнутый план пары для сигнала ENTER, без выставления ордеров."""
    if decision.action is not CarryAction.ENTER or decision.annualized_rate is None:
        raise ValueError("план входа допустим только для сигнала ENTER с рассчитанным A90")
    spot_inst_id = SUPPORTED_INSTRUMENTS[decision.inst_id]
    return carry_executor.build_pair(
        CarryLegs(spot_inst_id=spot_inst_id, swap_inst_id=decision.inst_id,
                  lever=1, margin_mode="isolated"),
        headline_apy=decision.annualized_rate,
        planned_hold_days=planned_hold_days,
        spot_px=spot_px,
        swap_px=swap_px,
        equity=equity,
        acct_lv=acct_lv,
        **sizing_options,
    )


def liquidation_distance_pct(mark_px: float, liq_px: float) -> float:
    """Дистанция от mark до фактической цены ликвидации короткой ноги, в %."""
    if not all(isinstance(value, (int, float)) and math.isfinite(value) and value > 0
               for value in (mark_px, liq_px)):
        raise ValueError("mark_px и liq_px должны быть положительными конечными числами")
    return (liq_px - mark_px) / mark_px * 100.0


def monitor_short_liquidation(
    *,
    mark_px: float,
    liq_px: float | None,
    min_distance_pct: float = MIN_LIQUIDATION_DISTANCE_PCT,
) -> LiquidationMonitor:
    """Fail-closed монитор: при неизвестной/малой дистанции просит закрыть шорт."""
    if liq_px is None:
        raise ValueError("биржа не сообщила liqPx: дистанцию до ликвидации проверить нельзя")
    if not isinstance(min_distance_pct, (int, float)) or not math.isfinite(min_distance_pct) \
            or min_distance_pct <= 0:
        raise ValueError("min_distance_pct должен быть положительным конечным числом")
    distance = liquidation_distance_pct(mark_px, liq_px)
    should_exit = distance <= min_distance_pct
    reason = (
        f"дистанция до ликвидации {distance:.2f}% ≤ {min_distance_pct:.2f}%"
        if should_exit else
        f"дистанция до ликвидации {distance:.2f}% выше порога {min_distance_pct:.2f}%"
    )
    return LiquidationMonitor(distance, should_exit, reason)
