"""Казначейство простаивающего USDT: Simple Earn Flexible (INSIGHTS-IMPL-TEAMWORK).

Основание — insights/idle-cash-earn.md (validated):
- Simple Earn Flexible: выкуп мгновенный 24/7, теряется только процент
  за неполный текущий час; лаг до первого начисления ~2 часа (§3);
- средства на отдельном счёте `earn`: НЕ доступны как маржа и НЕ входят
  в `totalEq` торгового счёта — перед использованием под маржу нужен redeem (§4);
- Trading Account Auto Earn доступен только VIP1+ — не наш случай (§0);
- в demo счётные Earn-эндпоинты блокированы кодом 50038, справочные работают (§1);
- ставка USDT плавающая ≈ 3–3.6% годовых (§2) — передаётся параметром,
  модуль её из сети не тянет.

Модуль не ходит в сеть и ордеров не ставит: считает начисления и планирует
резерв/выкуп. Исполнение purchase/redeem — человек или OKX Trader через CLI.
"""
from __future__ import annotations

from dataclasses import dataclass

DEMO_BLOCKED_CODE = "50038"  # «unavailable in demo trading» для Earn-счётных вызовов
HOURS_PER_YEAR = 365.0 * 24.0
FIRST_ACCRUAL_LAG_HOURS = 2.0  # лаг подписка → первое начисление (§3)
SIMPLE_EARN_IN_TOTALEQ = False  # §4: earn-счёт вне totalEq


@dataclass(frozen=True)
class TreasuryState:
    """Снимок казначейства на момент решения."""

    idle_usdt: float  # свободный USDT торгового счёта
    earn_usdt: float  # уже в Simple Earn Flexible
    reserve_buffer_usdt: float  # неснижаемый резерв под маржу/просадку


def hourly_accrual(principal_usdt: float, annual_rate: float) -> float:
    """Начисление за один полный час лендинга."""
    if principal_usdt < 0 or annual_rate < 0:
        raise ValueError("principal и ставка — неотрицательные")
    return principal_usdt * annual_rate / HOURS_PER_YEAR


def expected_accrual(principal_usdt: float, annual_rate: float,
                     hours: float) -> float:
    """Ожидаемое начисление за hours часов с учётом лага ~2ч (§3)."""
    billable = max(0.0, hours - FIRST_ACCRUAL_LAG_HOURS)
    return hourly_accrual(principal_usdt, annual_rate) * billable


def earn_capacity(state: TreasuryState) -> float:
    """Сколько можно отправить в Earn, не трогая резерв. Никогда не отрицательно."""
    return max(0.0, state.idle_usdt - state.reserve_buffer_usdt)


def recall_for_margin(state: TreasuryState, margin_need_usdt: float) -> float:
    """Сколько выкупить из Earn под маржинальную нужду (мгновенно, §3)."""
    if margin_need_usdt <= 0:
        return 0.0
    shortfall = max(0.0, margin_need_usdt - state.idle_usdt)
    return min(state.earn_usdt, shortfall)


def is_demo_blocked(error_code: str | int) -> bool:
    """Код 50038 = Earn недоступен в demo (§1)."""
    return str(error_code) == DEMO_BLOCKED_CODE


def totaleq_warning(amount_usdt: float) -> str:
    """Напоминание §4: перевод в Earn снижает totalEq — preflight считает по asset-valuation."""
    return (f"перевод {amount_usdt:.2f} USDT в Simple Earn выводит их из totalEq "
            "(EQUITY-TOTAL: equity считать по asset-valuation, не по totalEq)")
