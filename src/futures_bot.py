"""Контрактные боты: валидация contract_grid / contract_dca (INSIGHTS-ALL-IMPL).

Контракт — insights/futures-bot-spec.md; факты — insights/futures-bots.md.
Покрывает стратегии F1–F5 (§4 инсайта). Вне области: signal-бот C6,
Smart Arbitrage C7 (нет API), инверсный грид C9, TWAP/chase/recurring C8.

Модуль НЕ ходит в сеть: строит и валидирует параметры нативных ботов,
создаёт их человек/OKX Trader через `okx --demo bot …`.
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Callable, Optional, Sequence

from src import risk

WAVE1_MAX_LEVER = 2  # волна 1: плечо ≤ 2x (потолок ядра — risk.MAX_LEVERAGE=3)
MAX_CONTRACT_MARGIN_PCT = 4.0  # Σ маржи контрактных ботов ≤ 4% equity
MAX_PLANNED_WORST_PCT = 0.8  # Σ планового худшего по SL ≤ 0.8% equity
STEP_FLOOR_MAJOR_PCT = 0.25  # §3.4: мажоры 0.25–0.40%
STEP_FLOOR_ALT_PCT = 0.35  # §3.4: альты 0.35–0.60%
MAJORS = frozenset({"BTC", "ETH"})
TAKER_SWAP_FEE = 0.0005
SLIPPAGE_BUFFER = 0.01  # буфер проскальзывания в L_SL (§3.2)
ATR_SHOCK_MULT = 3.0  # ATR(1H) > 3× среднего 30 баров — запрет
FUNDING_OVERHEAT = 0.0005  # |funding| > 0.05%/8ч — запрет направленного бота
FUNDING_WINDOW_MIN = 5  # окно funding ±5 мин — запрет создания и SO
LIQ_EDGE_MULT = 3.0  # SL→liq ≥ 3× край→SL (§3.3)
LIQ_MIN_DIST_PCT = 15.0  # и ≥ 15% цены (альты ≥ 20%)
LIQ_MIN_DIST_ALT_PCT = 20.0
SL_RATIO_MIN = 0.03  # коридор slRatio [0.03; 0.15] (M3)
SL_RATIO_MAX = 0.15

BOT_ALREADY_STOPPED = "BOT_ALREADY_STOPPED"


@dataclass(frozen=True)
class FuturesGridConfig:
    """Параметры contract_grid (F1–F3)."""

    inst_id: str  # "<BASE>-USDT-SWAP"
    direction: str  # "neutral" | "long" | "short"
    lever: int  # 1..3 (волна 1: <= 2)
    margin_usdt: float  # sz — маржа в USDT
    min_px: float
    max_px: float
    grid_num: int
    run_type: int = 2
    base_pos: Optional[bool] = None  # None = не передавать (neutral — обязательно)
    sl_trigger_px: Optional[float] = None  # long/short
    sl_ratio: Optional[float] = None  # neutral
    max_grid_qty: int = 100  # потолок биржи из grid-quantity
    owner_code: str = "trd"


@dataclass(frozen=True)
class FuturesDCAConfig:
    """Параметры contract_dca (F4–F5)."""

    inst_id: str
    direction: str  # "long" | "short"
    lever: int
    init_ord_amt: float
    safety_ord_amt: float
    max_safety_ords: int
    px_steps: float  # первый шаг в % (напр. 1.2)
    px_steps_mult: float
    vol_mult: float
    tp_pct: float
    sl_pct: float
    init_px: float  # цена первого входа (для MPD и SL↔liq)
    allow_reinvest: bool = False  # API-дефолт true — всегда гасить явно
    owner_code: str = "trd"


def base_ccy(inst_id: str) -> str:
    """Базовая валюта из inst_id."""
    return inst_id.split("-")[0].upper()


def is_alt(inst_id: str) -> bool:
    """Альт = не BTC/ETH (грубее спреды, §3.4)."""
    return base_ccy(inst_id) not in MAJORS


def liquidation_move_pct(direction: str, lever: int,
                         mmr: float = 0.004) -> float | None:
    """Движение цены от входа до ликвидации, % (формула §1.3, fee=taker 0.05%).

    long → отрицательное, short → положительное. 1x long → None (нет ликвидации).
    """
    fee = TAKER_SWAP_FEE
    if direction == "long":
        if lever <= 1:
            return None
        ratio = (1 - 1.0 / lever) / (1 - mmr - fee)
        return (ratio - 1.0) * 100.0
    if direction == "short":
        ratio = (1 + 1.0 / lever) / (1 + mmr + fee)
        return (ratio - 1.0) * 100.0
    raise ValueError(f"direction {direction!r}: жду 'long' | 'short'")


def liquidation_estimate(entry_px: float, direction: str, lever: int,
                         mmr: float = 0.004) -> float | None:
    """Оценка liqPx (§1.3). None = ликвидации практически нет (1x long)."""
    move = liquidation_move_pct(direction, lever, mmr)
    if move is None:
        return None
    return entry_px * (1 + move / 100.0)


def planned_worst_loss(fills: Sequence[tuple[float, float]],
                       sl_px: float, with_buffer: bool = True) -> float:
    """Плановый худший убыток L_SL (§3.2): Σq|e−SL| + Q×SL×(taker+буфер)."""
    qty_sum = sum(q for q, _ in fills)
    legs = sum(q * abs(e - sl_px) for q, e in fills)
    exit_rate = TAKER_SWAP_FEE + (SLIPPAGE_BUFFER if with_buffer else 0.0)
    return legs + qty_sum * sl_px * exit_rate


def grid_long_worst_pct(width_pct: float, grid_num: int, sl_drop_pct: float,
                        lever: int) -> float:
    """Оценка планового худшего long-грида, % маржи (модель M2).

    Допущения §3.2: нотионал при полном заполнении = sz×lever, равные квоты
    на уровень, SL ниже minPx на sl_drop_pct. Сверка: 10%/25/3%/2x → ≈15%.
    """
    notional_mult = float(lever)
    avg_entry_rel = 1 + (width_pct / 100.0) / 2.0  # середина диапазона
    sl_rel = 1 - sl_drop_pct / 100.0
    loss_rel = (avg_entry_rel - sl_rel) / avg_entry_rel
    exit_rel = (TAKER_SWAP_FEE + SLIPPAGE_BUFFER) * sl_rel / avg_entry_rel
    return (loss_rel + exit_rel) * notional_mult * 100.0


def dca_ladder_entries(init_px: float, px_steps_pct: float, px_mult: float,
                       n_safety: int) -> tuple[float, ...]:
    """Цены входа init + SO: шаг_in_% × multⁱ (§4 F4)."""
    prices = [init_px]
    step = px_steps_pct
    for _ in range(n_safety):
        prices.append(prices[-1] * (1 - step / 100.0))
        step *= px_mult
    return tuple(prices)


def dca_max_pullback_pct(px_steps_pct: float, px_mult: float,
                         n_safety: int) -> float:
    """MPD: просадка до последнего SO, % от init (V5: sl_pct > MPD)."""
    px = 1.0
    step = px_steps_pct
    for _ in range(n_safety):
        px *= 1 - step / 100.0
        step *= px_mult
    return (1 - px) * 100.0


def sl_ratio_for_edge(edge_loss_quote: float, margin_usdt: float) -> float:
    """slRatio = ceil(1.2 × L_edge / sz; 0.01) в коридоре [0.03; 0.15] (M3)."""
    raw = 1.2 * edge_loss_quote / margin_usdt
    stepped = math.ceil(raw / 0.01) * 0.01
    return min(SL_RATIO_MAX, max(SL_RATIO_MIN, stepped))


def _check_lever(lever: int, reasons: list[str]) -> None:
    ok, why = risk.check_leverage(lever)
    if not ok:
        reasons.append(why)
        return
    if lever > WAVE1_MAX_LEVER:
        reasons.append(f"плечо {lever}x: волна 1 — не выше {WAVE1_MAX_LEVER}x")


def _check_inst(inst_id: str, reasons: list[str]) -> None:
    if not inst_id.endswith("-USDT-SWAP"):
        reasons.append(f"{inst_id}: только *-USDT-SWAP (coin-M запрещён до C9)")


def check_stop_vs_liq(edge_px: float, sl_px: float, liq_px: float, side: str,
                      inst_id: str,
                      checker: Callable[..., bool] | None = None) -> list[str]:
    """Правило §3.3: SL строго между краем и liq; запас ≥3× и ≥15%/20%."""
    reasons: list[str] = []
    check = checker or risk.validate_stop_vs_liquidation
    if not check(edge_px, sl_px, liq_px, side):
        reasons.append(f"SL {sl_px} не между краем {edge_px} и liq {liq_px} ({side})")
        return reasons
    edge_to_sl = abs(edge_px - sl_px)
    sl_to_liq = abs(sl_px - liq_px)
    if edge_to_sl <= 0:
        reasons.append("край совпадает со SL — дистанция нулевая")
        return reasons
    if sl_to_liq < LIQ_EDGE_MULT * edge_to_sl:
        reasons.append(f"запас SL→liq {sl_to_liq:.4g} < 3× край→SL {edge_to_sl:.4g}")
    floor = LIQ_MIN_DIST_ALT_PCT if is_alt(inst_id) else LIQ_MIN_DIST_PCT
    dist_pct = sl_to_liq / sl_px * 100.0
    if dist_pct < floor:
        reasons.append(f"дистанция SL→liq {dist_pct:.1f}% < {floor:.0f}%")
    return reasons


def creation_gate(atr_1h: float, atr_avg_30: float, funding_rate: float,
                  minutes_to_funding: float, directional: bool) -> list[str]:
    """Гейты создания §3 п.10: ATR-шок, перегрев funding, окно funding."""
    blocks: list[str] = []
    if atr_1h > ATR_SHOCK_MULT * atr_avg_30:
        blocks.append(f"ATR-шок: {atr_1h:.4g} > 3× среднего {atr_avg_30:.4g}")
    if directional and abs(funding_rate) > FUNDING_OVERHEAT:
        blocks.append(f"перегрев funding {funding_rate:.4%}: направленный бот запрещён")
    if abs(minutes_to_funding) <= FUNDING_WINDOW_MIN:
        blocks.append(f"окно funding ±{FUNDING_WINDOW_MIN} мин: создание запрещено")
    return blocks


def _grid_step_pct(cfg: FuturesGridConfig) -> float:
    return math.log(cfg.max_px / cfg.min_px) / cfg.grid_num * 100.0


def validate_grid(cfg: FuturesGridConfig, equity_usdt: float,
                  existing_margins: Sequence[float] = (),
                  existing_dirs: Sequence[tuple[str, str]] = (),
                  acct_lv: int = 2,
                  liq_px: float | None = None) -> list[str]:
    """Проверки §3 для contract_grid; [] = можно создавать. Сеть не используется."""
    reasons: list[str] = []
    _check_inst(cfg.inst_id, reasons)
    if cfg.direction not in ("neutral", "long", "short"):
        reasons.append(f"direction {cfg.direction!r}: neutral | long | short")
    _check_lever(cfg.lever, reasons)
    if cfg.margin_usdt > equity_usdt * risk.MAX_BOT_INVESTMENT_PCT / 100.0:
        reasons.append(f"маржа {cfg.margin_usdt}: лимит 2% equity "
                       f"({equity_usdt * 0.02:.2f})")
    if sum(existing_margins) + cfg.margin_usdt > equity_usdt * MAX_CONTRACT_MARGIN_PCT / 100.0:
        reasons.append(f"Σ маржи контрактных ботов > {MAX_CONTRACT_MARGIN_PCT}% equity")
    if cfg.min_px <= 0 or cfg.max_px <= cfg.min_px:
        reasons.append(f"диапазон {cfg.min_px}/{cfg.max_px}: нужно 0 < min < max")
        return reasons
    top = min(100, cfg.max_grid_qty)
    if not (2 <= cfg.grid_num <= top):
        reasons.append(f"gridNum {cfg.grid_num}: нужно 2…{top}")
    floor = STEP_FLOOR_ALT_PCT if is_alt(cfg.inst_id) else STEP_FLOOR_MAJOR_PCT
    step = _grid_step_pct(cfg)
    if step < floor:
        reasons.append(f"шаг {step:.3f}% < пола {floor}% (§3.4)")
    if cfg.direction == "neutral":
        if cfg.base_pos is not None:
            reasons.append("neutral: basePos передавать нельзя (только None)")
        if cfg.sl_ratio is None:
            reasons.append("neutral: нужен sl_ratio")
    else:
        if cfg.base_pos is None:
            reasons.append(f"{cfg.direction}: basePos обязателен (явный bool)")
        if cfg.sl_trigger_px is None:
            reasons.append(f"{cfg.direction}: нужен sl_trigger_px")
    if acct_lv != 2:
        reasons.append(f"acctLv {acct_lv}: нужен 2, иначе боты вернут 51057")
    if (cfg.inst_id, cfg.direction) in existing_dirs:
        reasons.append(f"дубль: {cfg.inst_id} {cfg.direction} уже есть во флоте")
    if cfg.owner_code != "trd":
        reasons.append(f"owner {cfg.owner_code}: флот создают с кодом trd")
    if liq_px is not None and cfg.sl_trigger_px is not None and cfg.direction != "neutral":
        edge = cfg.min_px if cfg.direction == "long" else cfg.max_px
        reasons.extend(check_stop_vs_liq(edge, cfg.sl_trigger_px, liq_px,
                                         cfg.direction, cfg.inst_id))
    return reasons


def validate_dca(cfg: FuturesDCAConfig, equity_usdt: float,
                 existing_margins: Sequence[float] = (),
                 existing_dirs: Sequence[tuple[str, str]] = (),
                 acct_lv: int = 2,
                 liq_px: float | None = None) -> list[str]:
    """Проверки §3 для contract_dca; [] = можно создавать."""
    reasons: list[str] = []
    _check_inst(cfg.inst_id, reasons)
    if cfg.direction not in ("long", "short"):
        reasons.append(f"direction {cfg.direction!r}: long | short")
    _check_lever(cfg.lever, reasons)
    margin = cfg.init_ord_amt + sum(
        cfg.safety_ord_amt * (cfg.vol_mult ** i)
        for i in range(cfg.max_safety_ords))
    if margin > equity_usdt * risk.MAX_BOT_INVESTMENT_PCT / 100.0:
        reasons.append(f"маржа {margin:.2f}: лимит 2% equity")
    if sum(existing_margins) + margin > equity_usdt * MAX_CONTRACT_MARGIN_PCT / 100.0:
        reasons.append(f"Σ маржи контрактных ботов > {MAX_CONTRACT_MARGIN_PCT}% equity")
    if cfg.allow_reinvest:
        reasons.append("allow_reinvest обязан быть False (иначе раздувает объём)")
    mpd = dca_max_pullback_pct(cfg.px_steps, cfg.px_steps_mult,
                               cfg.max_safety_ords)
    if cfg.sl_pct <= mpd:
        reasons.append(f"sl_pct {cfg.sl_pct}% <= MPD {mpd:.2f}%: стоп внутри лестницы")
    if acct_lv != 2:
        reasons.append(f"acctLv {acct_lv}: нужен 2, иначе боты вернут 51057")
    if (cfg.inst_id, cfg.direction) in existing_dirs:
        reasons.append(f"дубль: {cfg.inst_id} {cfg.direction} уже есть во флоте")
    if cfg.owner_code != "trd":
        reasons.append(f"owner {cfg.owner_code}: флот создают с кодом trd")
    if liq_px is not None:
        entries = dca_ladder_entries(cfg.init_px, cfg.px_steps,
                                     cfg.px_steps_mult, cfg.max_safety_ords)
        edge = entries[-1]
        sl_px = cfg.init_px * (1 - cfg.sl_pct / 100.0)
        reasons.extend(check_stop_vs_liq(edge, sl_px, liq_px, cfg.direction,
                                         cfg.inst_id))
    return reasons


def stop_plan(algo_id: str, inst_id: str, algo_ord_type: str,
              former_sl_px: float) -> dict:
    """План стопа §5: kill шлёт stopType 2 (позиция остаётся), Sentinel
    закрывает остаток stopType 1, когда цена дошла до бывшего SL."""
    return {
        "algo_id": algo_id,
        "inst_id": inst_id,
        "algo_ord_type": algo_ord_type,
        "former_sl_px": former_sl_px,
        "close_cmd": {
            "module": "bot",
            "action": "stop",
            "algoOrdType": algo_ord_type,
            "algoId": algo_id,
            "stopType": "1",
        },
    }


def stop_is_benign(error_code: str | int, detail: str = "") -> bool | str:
    """Повторный stop 2 по no_close_position — не провал (BOT_ALREADY_STOPPED)."""
    if str(error_code) == "51291" or "no_close_position" in detail:
        return BOT_ALREADY_STOPPED
    return False
