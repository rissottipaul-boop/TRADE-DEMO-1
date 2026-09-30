"""Funding Carry execution layer (M1) поверх src/funding_carry.py.

Чистая математика (комиссии, breakeven, гейты ног) переиспользуется из
src/funding_carry.py как есть; этот модуль добавляет исполнение:
- дельта-нейтральный сайзинг пары (спот long + своп short 1x isolated);
- пре-трейд гейты: funding_carry.validate_legs/assess + risk.check_entry_allowed;
- сайзинг через risk.size_position; выставление через OrderRouter;
- dry_run=True по умолчанию (сборка + валидация, без выставления);
- чистые функции: partial-fill политика и сигнал выхода по funding.

Метка владельца (ORDER-OWNER-TAG, AGENTS.md §6): `botcar` (CARRY,
kind=strategy), зарегистрирован в src/order_owner.py. OrderRouter вызывает
order_owner.require(owner, own=True) и бросает ValueError на незарегистрированном
коде, поэтому OWNER берётся из реестра, а не выдумывается.

Только demo (AGENTS.md §2): модуль не читает секреты и не меняет лимиты риска.
Сети нет: funding-ставка и цены приходят параметрами от вызывателя.
"""
from __future__ import annotations

import logging
import math
from dataclasses import dataclass
from typing import Any, Optional

from . import funding_carry, order_owner
from .funding_carry import CarryLegs, CarryVerdict

log = logging.getLogger("okx.carry_executor")

OWNER = order_owner.CARRY  # "botcar" — Funding Carry (kind=strategy)

SPOT_SIDE = "buy"   # спот-нога: long (cash, без плеча)
SWAP_SIDE = "sell"  # своп-нога: short 1x isolated

PERIODS_PER_DAY = 3  # расчёты funding каждые 8ч


@dataclass(frozen=True)
class LegSpec:
    """Одна нога пары к выставлению."""

    inst_id: str
    side: str
    ord_type: str
    px: Optional[float]  # None только для market
    sz: float


@dataclass(frozen=True)
class CarryPlan:
    """Собранная пара: гейты + сайзинг. Выставления не было."""

    legs: CarryLegs
    spot: LegSpec
    swap: LegSpec
    verdict: CarryVerdict
    spot_notional: float
    swap_notional: float
    sizing_warnings: tuple[str, ...] = ()
    gates: tuple[str, ...] = ()  # непусто = выставлять нельзя
    owner: str = OWNER

    @property
    def placeable(self) -> bool:
        return not self.gates


def synthetic_stop(entry: float, risk_pct: float, position_cap_pct: float) -> float:
    """Синтетический стоп для сайзинга через risk.size_position.

    У carry нет направленного стопа, поэтому дистанция подбирается так, чтобы
    риск-подразумеваемый размер в точности равнялся потолку позиции
    (risk.MAX_POSITION_PCT): потолок срабатывает детерминированно, а не
    случайно. Нотионал ноги ограничен потолком от equity.
    """
    if entry <= 0:
        raise ValueError(f"entry {entry!r}: нужна положительная цена")
    if risk_pct <= 0 or position_cap_pct <= 0:
        raise ValueError("risk_pct и position_cap_pct обязаны быть > 0")
    return entry * (1.0 - risk_pct / position_cap_pct)


def _floor_to_lot(size: float, lot_sz: float) -> float:
    if lot_sz > 0:
        # +1e-9: 0.01/1e-5 в float = 999.999… → floor обязан дать 1000, не 999
        # (тот же приём epsilon, что ceil(… − 1e-9) в src/backtest/risk_sim.py).
        return math.floor(size / lot_sz + 1e-9) * lot_sz
    return size


def pair_sizes(*, equity: float, spot_px: float, spot_lot_sz: float = 0.0,
               spot_min_sz: float = 0.0, swap_ct_val: float,
               swap_lot_sz: float = 0.0, swap_min_sz: float = 0.0,
               risk_pct: float | None = None, risk_mod: Any = None) -> dict:
    """Дельта-нейтральные размеры ног. Чистая функция (риск-модуль инжектится).

    Спот сайзится через risk.size_position по канону «контракт = 1 лот»
    (src/backtest/risk_sim.py: size_position считает целые контракты, и ct_val=1
    округлил бы BTC до целых монет → 0): ct_val=spot_lot_sz, lot_sz=1,
    min_sz=ceil(spot_min_sz/лот). Своп-контракты выводятся из спот-размера
    (spot_sz / ctVal) и округляются вниз к lotSz; спот подгоняется обратно —
    базовые количества совпадают точно, нейтральность не зависит от базиса цен.
    Возвращает {spot_sz, swap_contracts, spot_notional, warnings}.
    Нулевой размер — не исключение, а warnings + нули: гейт решает build_pair.
    """
    if risk_mod is None:
        from . import risk as risk_mod  # локально: импорт без БД, БД трогает _c()
    if risk_pct is None:
        risk_pct = risk_mod.DEFAULT_RISK_PCT
    cap_pct = risk_mod.MAX_POSITION_PCT
    stop = synthetic_stop(spot_px, risk_pct, cap_pct)
    unit = spot_lot_sz if spot_lot_sz > 0 else 1.0
    min_lots = math.ceil(spot_min_sz / unit - 1e-9) if spot_min_sz > 0 else 0.0
    sized = risk_mod.size_position(equity=equity, entry=spot_px, stop=stop,
                                   ct_val=unit, lot_sz=1.0,
                                   min_sz=min_lots, risk_pct=risk_pct)
    warnings = list(sized["warnings"])
    spot_sz = float(sized["size"]) * unit
    if swap_ct_val <= 0:
        raise ValueError(f"swap_ct_val {swap_ct_val!r}: обязан быть > 0")
    swap_contracts = _floor_to_lot(spot_sz / swap_ct_val, swap_lot_sz)
    # Точная нейтральность: спот = контракты × ctVal (а не наоборот).
    spot_sz = _floor_to_lot(swap_contracts * swap_ct_val, spot_lot_sz)
    if spot_sz < spot_min_sz:
        warnings.append(f"спот {spot_sz:g} < min_sz {spot_min_sz:g}: вход невозможен")
        spot_sz = 0.0
    if swap_contracts < swap_min_sz:
        warnings.append(f"своп {swap_contracts:g} < min_sz {swap_min_sz:g}: вход невозможен")
        swap_contracts = 0.0
    if spot_sz == 0.0 or swap_contracts == 0.0:
        spot_sz, swap_contracts = 0.0, 0.0  # пара целиком или ничего
    return {"spot_sz": spot_sz, "swap_contracts": swap_contracts,
            "spot_notional": spot_sz * spot_px, "warnings": warnings}


def build_pair(legs: CarryLegs, *, headline_apy: float, planned_hold_days: float,
               spot_px: float, swap_px: float | None = None, equity: float,
               acct_lv: int, exec_mode: str = "taker", ord_type: str = "limit",
               spot_lot_sz: float = 0.0, spot_min_sz: float = 0.0,
               swap_ct_val: float = 0.0, swap_lot_sz: float = 0.0,
               swap_min_sz: float = 0.0, risk_pct: float | None = None,
               risk_mod: Any = None) -> CarryPlan:
    """Собрать и провалидировать пару. Выставления и сети нет.

    Гейты по порядку: funding_carry.assess (ноги + экономика breakeven vs
    планового удержания), risk.check_entry_allowed обеих ног, сайзинг пары.
    risk_mod инжектится для тестов (дефолт — реальный src.risk, локальный SQLite).
    """
    if risk_mod is None:
        from . import risk as risk_mod
    if risk_pct is None:
        risk_pct = risk_mod.DEFAULT_RISK_PCT
    if ord_type not in ("limit", "market"):
        raise ValueError(f"ord_type {ord_type!r}: нужен limit или market")
    if swap_px is None:
        swap_px = spot_px
    if spot_px <= 0 or swap_px <= 0:
        raise ValueError("spot_px и swap_px обязаны быть > 0")

    verdict = funding_carry.assess(headline_apy, legs, acct_lv,
                                   planned_hold_days, exec_mode)
    gates: list[str] = list(verdict.blockers)
    if not verdict.blockers and not verdict.meets_min_hold:
        gates.append(
            f"экономика не сходится: breakeven {verdict.breakeven_days} дн "
            f"(мин. {funding_carry.MIN_HOLD_DAYS} дн) > плановых {planned_hold_days} дн")

    for inst_id, side in ((legs.spot_inst_id, SPOT_SIDE), (legs.swap_inst_id, SWAP_SIDE)):
        allowed, reason = risk_mod.check_entry_allowed(inst_id, side)
        if not allowed:
            gates.append(f"risk {inst_id} {side}: {reason}")

    sizing = pair_sizes(equity=equity, spot_px=spot_px, spot_lot_sz=spot_lot_sz,
                        spot_min_sz=spot_min_sz, swap_ct_val=swap_ct_val,
                        swap_lot_sz=swap_lot_sz, swap_min_sz=swap_min_sz,
                        risk_pct=risk_pct, risk_mod=risk_mod)
    if sizing["spot_sz"] <= 0 or sizing["swap_contracts"] <= 0:
        gates.append("сайзинг дал нулевой размер: " + "; ".join(sizing["warnings"]))

    return CarryPlan(
        legs=legs,
        spot=LegSpec(legs.spot_inst_id, SPOT_SIDE, ord_type,
                     spot_px if ord_type == "limit" else None, sizing["spot_sz"]),
        swap=LegSpec(legs.swap_inst_id, SWAP_SIDE, ord_type,
                     swap_px if ord_type == "limit" else None, sizing["swap_contracts"]),
        verdict=verdict,
        spot_notional=sizing["spot_notional"],
        swap_notional=sizing["swap_contracts"] * swap_ct_val * swap_px,
        sizing_warnings=tuple(sizing["warnings"]),
        gates=tuple(gates),
    )


def place_pair(plan: CarryPlan, router: Any, *, dry_run: bool = True) -> dict:
    """Выставить пару через OrderRouter. По умолчанию dry_run: только валидация.

    Реальное выставление — только явным отдельным вызовом place_pair(plan,
    router, dry_run=False) и только в demo (роутер принимает готовый exchange;
    вызыватель отвечает за demo-коннектор). Порядок: сначала спот, затем своп;
    при отказе второй ноги прикладывается hedge-or-exit решение.
    """
    if dry_run:
        log.info("carry dry_run: пара %s/%s валидна=%s, выставления нет",
                 plan.legs.spot_inst_id, plan.legs.swap_inst_id, plan.placeable)
        return {"ok": True, "dry_run": True, "placeable": plan.placeable,
                "gates": list(plan.gates), "orders": [], "plan": plan}
    if not plan.placeable:
        return {"ok": False, "dry_run": False, "reason": "план заблокирован гейтами",
                "gates": list(plan.gates), "orders": []}
    orders: list[dict] = []
    spot_res = router.place_order(plan.spot.inst_id, plan.spot.side, plan.spot.ord_type,
                                  px=plan.spot.px, sz=plan.spot.sz, owner=OWNER)
    orders.append({"leg": "spot", **spot_res})
    if not spot_res.get("ok"):
        log.warning("carry: спот-нога отклонена (%s), своп не ставим",
                    spot_res.get("reason"))
        return {"ok": False, "dry_run": False, "orders": orders,
                "decision": decide_partial_fill(spot_fill=0.0, swap_fill=0.0)}
    swap_res = router.place_order(plan.swap.inst_id, plan.swap.side, plan.swap.ord_type,
                                  px=plan.swap.px, sz=plan.swap.sz, owner=OWNER)
    orders.append({"leg": "swap", **swap_res})
    if not swap_res.get("ok"):
        log.warning("carry: своп-нога отклонена (%s) при выставленном споте",
                    swap_res.get("reason"))
        return {"ok": False, "dry_run": False, "orders": orders,
                "decision": decide_partial_fill(spot_fill=1.0, swap_fill=0.0)}
    return {"ok": True, "dry_run": False, "orders": orders, "plan": plan}


@dataclass(frozen=True)
class PartialFillDecision:
    """Hedge-or-exit решение при рассинхроне ног (чистая функция)."""

    action: str  # hold_pair | abort_pair | exit_spot | exit_swap | wait
    reason: str


def decide_partial_fill(*, spot_fill: float, swap_fill: float) -> PartialFillDecision:
    """Политика частичных заливок: доли исполнения ног [0,1] → решение.

    Любой дисбаланс закрывает перевесившую ногу: голая направленная позиция —
    не carry. Равные частичные заливки ждут сеттла и повторной оценки.
    """
    for name, value in (("spot_fill", spot_fill), ("swap_fill", swap_fill)):
        if not isinstance(value, (int, float)) or not math.isfinite(value) \
                or not 0.0 <= value <= 1.0:
            raise ValueError(f"{name} {value!r}: нужна доля исполнения [0,1]")
    full = lambda f: math.isclose(f, 1.0, abs_tol=1e-9)  # noqa: E731
    empty = lambda f: math.isclose(f, 0.0, abs_tol=1e-9)  # noqa: E731
    if full(spot_fill) and full(swap_fill):
        return PartialFillDecision("hold_pair", "обе ноги залиты — держать пару")
    if empty(spot_fill) and empty(swap_fill):
        return PartialFillDecision("abort_pair",
                                   "ничего не залито — отменить оба ордера, позиции нет")
    if math.isclose(spot_fill, swap_fill, abs_tol=1e-9):
        return PartialFillDecision("wait",
                                   "заливки равны, но неполны — ждать сеттла и переоценить")
    if spot_fill > swap_fill:
        return PartialFillDecision(
            "exit_spot",
            f"голый спот-лонг (спот {spot_fill:g}, своп {swap_fill:g}): "
            "немедленно закрыть спот-ногу, нейтральность потеряна")
    return PartialFillDecision(
        "exit_swap",
        f"голый своп-шорт (своп {swap_fill:g}, спот {spot_fill:g}): "
        "немедленно закрыть своп-ногу, нейтральность потеряна")


@dataclass(frozen=True)
class FundingExit:
    """Сигнал выхода по funding (чистая функция)."""

    exit: bool
    reason: str


def funding_exit_signal(*, recent_mean_per_period: float, exec_mode: str = "taker",
                        planned_hold_days: float) -> FundingExit:
    """Выходить ли: ставка отрицательна или ниже амортизации комиссий.

    recent_mean_per_period — сглаженная средняя ставка периода (вызыватель
    усредняет окно сам, напр. 7 дней: одиночный период шумит). Порог амортизации —
    round-trip издержки, разложенные на плановые периоды удержания.
    """
    if not isinstance(recent_mean_per_period, (int, float)) \
            or not math.isfinite(recent_mean_per_period):
        raise ValueError(f"ставка {recent_mean_per_period!r}: нужно конечное число")
    if planned_hold_days <= 0:
        raise ValueError(f"planned_hold_days {planned_hold_days!r}: нужен > 0")
    if recent_mean_per_period < 0:
        return FundingExit(True, f"funding {recent_mean_per_period:g}/период отрицательный: "
                                 "шорт-нога платит — carry убыточен, выходить")
    amort = funding_carry.roundtrip_cost(exec_mode) / (planned_hold_days * PERIODS_PER_DAY)
    if recent_mean_per_period < amort:
        return FundingExit(
            True, f"funding {recent_mean_per_period:g}/период ниже амортизации комиссий "
                  f"{amort:g}/период ({exec_mode}, {planned_hold_days:g} дн) — выходить")
    return FundingExit(False, f"funding {recent_mean_per_period:g}/период покрывает "
                              f"амортизацию {amort:g}/период — держать")
