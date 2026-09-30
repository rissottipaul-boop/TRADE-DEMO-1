"""Funding carry: спот long + perp short 1x, экономика и гейты (INSIGHTS-IMPL-TEAMWORK).

Основание — insights/funding-carry.md (validated):
- издержки round-trip 4 ног: 0.20% (весь maker) … 0.30% (весь taker) от нотионала (§4);
- порог рентабельности BTC ≈ 12–19 дней, ETH ≈ 18–26 дней (§0);
- шорт 1x isolated ликвидируется при росте ≈ +99% (§0);
- скрытая находка: маржа шорта 1x = 100% нотионала, доходность на весь
  вложенный капитал ≈ вдвое ниже заголовочной (§0);
- funding в demo не совпадает с live — годится проверять код, не экономику (§0).

Модуль не ходит в сеть: считает экономику по переданным ставкам.
Создание позиций — вне модуля (нужны ACCT-MODE-DERIV и demo-проверка кода).
"""
from __future__ import annotations

from dataclasses import dataclass

# Комиссии базового тира OKX, funding-carry.md §4.
MAKER_SPOT = 0.0008
TAKER_SPOT = 0.0010
MAKER_SWAP = 0.0002
TAKER_SWAP = 0.0005

FUNDING_PERIODS_PER_YEAR = 1095  # 3 периода по 8ч в сутки
CAPITAL_EFFICIENCY = 0.5  # поправка §0: x2 капитала на 1x-шорт
SUPPORTED_SWAPS = frozenset({"BTC-USDT-SWAP", "ETH-USDT-SWAP"})
MIN_HOLD_DAYS = 14  # вердикт §0: не раньше 2–4 недель удержания на самоокупаемость


@dataclass(frozen=True)
class CarryLegs:
    """Пара ног carry: спот long + perp short 1x isolated."""

    spot_inst_id: str  # "BTC-USDT"
    swap_inst_id: str  # "BTC-USDT-SWAP"
    lever: int = 1
    margin_mode: str = "isolated"


def roundtrip_cost(exec_mode: str = "taker") -> float:
    """Издержки round-trip 4 ног как доля нотионала (§4).

    taker → 0.0030, mixed → 0.0025, maker → 0.0020.
    """
    costs = {
        "taker": (TAKER_SPOT + TAKER_SWAP) * 2,
        "mixed": TAKER_SPOT + TAKER_SWAP + MAKER_SPOT + MAKER_SWAP,
        "maker": (MAKER_SPOT + MAKER_SWAP) * 2,
    }
    try:
        return costs[exec_mode]
    except KeyError:
        raise ValueError(f"exec_mode {exec_mode!r}: жду 'taker' | 'mixed' | 'maker'")


def annualize(mean_per_period: float) -> float:
    """Средняя ставка периода → годовая (×1095, §2.1)."""
    return mean_per_period * FUNDING_PERIODS_PER_YEAR


def breakeven_days(annual_rate: float, exec_mode: str = "taker") -> float | None:
    """Дней удержания до окупаемости издержек; None при неположительной ставке."""
    if annual_rate <= 0:
        return None
    return roundtrip_cost(exec_mode) / annual_rate * 365.0


def effective_apy(headline_apy: float) -> float:
    """Реальная доходность на весь вложенный капитал (§0: ×0.5)."""
    return headline_apy * CAPITAL_EFFICIENCY


def short_liquidation_move_pct(lever: int = 1, mmr: float = 0.004) -> float | None:
    """Рост цены от входа до ликвидации шорт-ноги, % (§0: 1x → ≈+99%).

    None при 1x и ниже — практически недостижимо на горизонте недель/месяцев.
    """
    if lever <= 1:
        return None
    return (1.0 / lever - mmr) * 100.0


def validate_legs(legs: CarryLegs, acct_lv: int) -> list[str]:
    """Гейты создания carry-пары; [] = можно открывать."""
    reasons: list[str] = []
    if legs.swap_inst_id not in SUPPORTED_SWAPS:
        reasons.append(f"swap {legs.swap_inst_id}: валидированы только {sorted(SUPPORTED_SWAPS)}")
    base = legs.swap_inst_id.removesuffix("-SWAP")
    if legs.spot_inst_id != base:
        reasons.append(f"ноги не парные: {legs.spot_inst_id} vs {legs.swap_inst_id}")
    if legs.lever != 1:
        reasons.append(f"плечо шорта {legs.lever}x: carry валидирован только на 1x")
    if legs.margin_mode != "isolated":
        reasons.append(f"маржа {legs.margin_mode}: только isolated (§1.2 futures-bots)")
    if acct_lv != 2:
        reasons.append(f"acctLv {acct_lv}: деривативы требуют acctLv 2 (ACCT-MODE-DERIV)")
    return reasons


@dataclass(frozen=True)
class CarryVerdict:
    """Итог оценки carry-позиции."""

    headline_apy: float
    effective_apy: float
    breakeven_days: float | None
    meets_min_hold: bool
    blockers: tuple[str, ...] = ()


def assess(headline_apy: float, legs: CarryLegs, acct_lv: int,
           planned_hold_days: float, exec_mode: str = "taker") -> CarryVerdict:
    """Сводная оценка: окупаемость + гейты. Позиций не открывает."""
    blockers = validate_legs(legs, acct_lv)
    days = breakeven_days(headline_apy, exec_mode)
    meets = days is not None and planned_hold_days >= max(days, MIN_HOLD_DAYS)
    return CarryVerdict(
        headline_apy=headline_apy,
        effective_apy=effective_apy(headline_apy),
        breakeven_days=days,
        meets_min_hold=meets,
        blockers=tuple(blockers),
    )
