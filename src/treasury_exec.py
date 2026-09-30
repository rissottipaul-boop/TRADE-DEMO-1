"""Казначейский контроллер Idle Cash Earn: планы sweep/redeem + demo-адаптер (M3).

Основание — insights/idle-cash-earn.md (validated) и математика src/treasury.py:
- free cash считается ЧИСТОЙ функцией переданного снапшота баланса
  (availBal/frozenBal/equity + резервный %) — модуль в сеть не ходит;
- Simple Earn Flexible покидает totalEq (§4) — планы предупреждают об этом;
- demo блокирует счётные Earn-эндпоинты кодом 50038 (§1) — адаптер вместо
  реальных вызовов ведёт симулированный in-memory леджер, каждая запись
  помечена simulated=True.

Модуль НЕ делает сетевых вызовов, НЕ пишет в data/*.db и sleeves.json,
ордеров не ставит. Исполнение на live — человек/OKX Trader через CLI.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from src.treasury import (FIRST_ACCRUAL_LAG_HOURS, TreasuryState, earn_capacity,
                          expected_accrual, is_demo_blocked, recall_for_margin,
                          totaleq_warning)

MIN_SWEEP_USDT = 100.0  # минимальный размер sweep в Earn
DEFAULT_RESERVE_PCT = 0.30  # неснижаемый резерв: 30% equity под маржу/просадку
CASH_EARN_SLEEVE = "cash_earn"


@dataclass(frozen=True)
class BalanceSnapshot:
    """Переданный вызывающим снимок баланса (уже получен, сети нет)."""

    ccy: str  # "USDT" / "USDC"
    avail_bal: float  # доступный баланс
    frozen_bal: float  # заморожено в ордерах
    equity: float  # equity валюты на торговом счёте

    def __post_init__(self) -> None:
        for name in ("avail_bal", "frozen_bal", "equity"):
            if getattr(self, name) < 0:
                raise ValueError(f"{name} — неотрицательное")


def free_cash(snapshot: BalanceSnapshot, reserve_pct: float = DEFAULT_RESERVE_PCT) -> float:
    """Свободный кэш под sweep: чистая функция снапшота + резервного %.

    unlocked = min(availBal, equity - frozenBal) — консервативная оценка
    средств вне обеспечения и ордеров; минус резерв reserve_pct * equity.
    Никогда не отрицательно. Сети нет.
    """
    if not 0.0 <= reserve_pct <= 1.0:
        raise ValueError("reserve_pct в диапазоне [0, 1]")
    unlocked = min(snapshot.avail_bal, max(0.0, snapshot.equity - snapshot.frozen_bal))
    reserve = snapshot.equity * reserve_pct
    return max(0.0, unlocked - reserve)


def treasury_state_from_snapshot(snapshot: BalanceSnapshot, earn_usdt: float,
                                reserve_pct: float = DEFAULT_RESERVE_PCT) -> TreasuryState:
    """Мост к src/treasury.py: снапшот → TreasuryState (REUSE математики)."""
    if earn_usdt < 0:
        raise ValueError("earn_usdt — неотрицательное")
    return TreasuryState(idle_usdt=free_cash(snapshot, reserve_pct),
                         earn_usdt=earn_usdt,
                         reserve_buffer_usdt=0.0)  # резерв уже учтён в free_cash


@dataclass(frozen=True)
class SweepPlan:
    """План размещения: данные, не исполнение."""

    action: str  # "purchase" | "hold"
    amount_usdt: float
    ccy: str
    reason: str


def plan_sweep(free_cash_usdt: float, ccy: str = "USDT",
               min_sweep: float = MIN_SWEEP_USDT) -> SweepPlan:
    """FreeCash >= min_sweep → purchase-план, иначе hold. Только данные."""
    if free_cash_usdt < 0:
        raise ValueError("free_cash_usdt — неотрицательное")
    if min_sweep <= 0:
        raise ValueError("min_sweep — положительное")
    if free_cash_usdt >= min_sweep:
        return SweepPlan(action="purchase", amount_usdt=free_cash_usdt, ccy=ccy,
                         reason=f"free {free_cash_usdt:.2f} >= min {min_sweep:.2f}; "
                                + totaleq_warning(free_cash_usdt))
    return SweepPlan(action="hold", amount_usdt=0.0, ccy=ccy,
                     reason=f"free {free_cash_usdt:.2f} < min {min_sweep:.2f} — ниже порога")


@dataclass(frozen=True)
class RedeemPlan:
    """План возврата средств под маржинальную нужду: данные, не исполнение."""

    action: str  # "redeem" | "none"
    amount_usdt: float
    reason: str


def plan_redeem(state: TreasuryState, margin_need_usdt: float) -> RedeemPlan:
    """Сколько выкупить из Earn под нужду маржи (мгновенно, §3). Только данные."""
    amount = recall_for_margin(state, margin_need_usdt)
    if amount > 0:
        return RedeemPlan(action="redeem", amount_usdt=amount,
                          reason=f"margin need {margin_need_usdt:.2f}: выкуп {amount:.2f} "
                                 "USDT из Earn (instant redemption)")
    return RedeemPlan(action="none", amount_usdt=0.0,
                      reason=f"margin need {margin_need_usdt:.2f} покрыта idle — выкуп не нужен")


@dataclass(frozen=True)
class YieldAttribution:
    """Атрибуция доходности рукава cash_earn: только данные, леджер не пишется."""

    sleeve: str
    ccy: str
    principal_usdt: float
    annual_rate: float
    hours: float
    accrued_usdt: float
    simulated: bool


def attribute_yield(principal_usdt: float, annual_rate: float, hours: float,
                    ccy: str = "USDT", simulated: bool = True) -> YieldAttribution:
    """Начисление рукава cash_earn с лагом ~2ч (REUSE expected_accrual)."""
    return YieldAttribution(sleeve=CASH_EARN_SLEEVE, ccy=ccy,
                            principal_usdt=principal_usdt, annual_rate=annual_rate,
                            hours=hours,
                            accrued_usdt=expected_accrual(principal_usdt, annual_rate, hours),
                            simulated=simulated)


@dataclass
class SimulatedEarnLedger:
    """Demo-адаптер: in-memory леджер вместо заблокированных (50038) Earn-вызовов.

    Каждая запись помечена simulated=True. Реальных Earn-вызовов нет.
    """

    balances: dict[str, float] = field(default_factory=dict)
    accrued_total: dict[str, float] = field(default_factory=dict)
    records: list[dict[str, Any]] = field(default_factory=list)

    def _record(self, kind: str, ccy: str, amount: float, **extra: Any) -> dict[str, Any]:
        rec: dict[str, Any] = {"kind": kind, "ccy": ccy, "amount": round(amount, 8),
                               "simulated": True}
        rec.update(extra)
        self.records.append(rec)
        return rec

    def purchase(self, ccy: str, amount: float) -> dict[str, Any]:
        """Симуляция earn savings purchase (реальный вызов в demo дал бы 50038)."""
        if amount <= 0:
            raise ValueError("purchase amount — положительное")
        self.balances[ccy] = self.balances.get(ccy, 0.0) + amount
        return self._record("purchase", ccy, amount,
                            balance=round(self.balances[ccy], 8),
                            warning=totaleq_warning(amount))

    def redeem(self, ccy: str, amount: float) -> dict[str, Any]:
        """Симуляция мгновенного выкупа (real-world: instant redemption, §3)."""
        if amount <= 0:
            raise ValueError("redeem amount — положительное")
        have = self.balances.get(ccy, 0.0)
        if amount > have + 1e-9:
            raise ValueError(f"недостаточно в симуляции: {have:.2f} < {amount:.2f} {ccy}")
        self.balances[ccy] = have - amount
        return self._record("redeem", ccy, amount,
                            balance=round(self.balances[ccy], 8))

    def accrue(self, ccy: str, annual_rate: float, hours: float) -> dict[str, Any]:
        """Симуляция начисления за hours часов с лагом ~2ч (REUSE expected_accrual)."""
        if annual_rate < 0 or hours < 0:
            raise ValueError("ставка и часы — неотрицательные")
        principal = self.balances.get(ccy, 0.0)
        gain = expected_accrual(principal, annual_rate, hours)
        self.balances[ccy] = principal + gain
        self.accrued_total[ccy] = self.accrued_total.get(ccy, 0.0) + gain
        return self._record("accrue", ccy, gain, principal=round(principal, 8),
                            annual_rate=annual_rate, hours=hours,
                            lag_hours=FIRST_ACCRUAL_LAG_HOURS,
                            balance=round(self.balances[ccy], 8))


def should_simulate(error_code: str | int) -> bool:
    """sCode 50038 → demo блокирует Earn: использовать симуляцию (REUSE is_demo_blocked)."""
    return is_demo_blocked(error_code)


def sweep_cycle(snapshot: BalanceSnapshot, ledger: SimulatedEarnLedger,
                earn_usdt: float = 0.0,
                reserve_pct: float = DEFAULT_RESERVE_PCT) -> tuple[SweepPlan, dict[str, Any] | None]:
    """Связка: free cash → план → симулированное исполнение. Сети нет.

    Возвращает (план, запись леджера или None при hold).
    """
    state = treasury_state_from_snapshot(snapshot, earn_usdt, reserve_pct)
    plan = plan_sweep(earn_capacity(state), ccy=snapshot.ccy)
    if plan.action != "purchase":
        return plan, None
    return plan, ledger.purchase(plan.ccy, plan.amount_usdt)
