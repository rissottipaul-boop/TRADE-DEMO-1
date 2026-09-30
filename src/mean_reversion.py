"""Mean-reversion RSI/BB для live/demo-сигналов (INSIGHTS-IMPL-TEAMWORK).

Сигнальная логика — та же, что в бэктесте (insights/meanrev-strategy-design.md,
реализация src/backtest/meanrev.py): RSI(14) crossed 30/70, BB(20,2) от typical
price, ATR-стоп ×1.5, ROI-таблица, тайм-стоп 24ч. Дубли signal-функций нет —
модуль переиспользует их и добавляет live-обвязку: индикаторы по буферу свечей,
цену стопа, сайзинг через risk.size_position, приоритет выходов §6.3.

Честное предупреждение из insights/meanrev-backtest.md: дефолты гейт §6.2
НЕ прошли (WF OOS net −17.96%, PF 0.54, edge отрицателен до комиссий).
Модуль готовит сигналы для demo-проверки калибровок (MEANREV-CALIB),
а не для торговли «как есть». Ордеров не ставит.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Sequence

from src import risk
from src.backtest import meanrev as bt
from src.backtest.data import Bar
from src.backtest.indicators import (atr_wilder, bollinger, rsi_wilder,
                                     typical_price)

# Дефолты дизайна §3–§6 (калибровка — MEANREV-CALIB).
RSI_N = 14
RSI_ENTRY = 30.0
RSI_EXIT = 70.0
BB_N = 20
BB_DEV = 2.0
ATR_N = 14
ATR_MULT = 1.5
ROI_TABLE = bt.DEFAULT_ROI
TIME_STOP_MIN = bt.TIME_STOP_MIN
WARMUP = bt.WARMUP

__all__ = ["RSI_N", "RSI_ENTRY", "RSI_EXIT", "BB_N", "BB_DEV", "ATR_N",
           "ATR_MULT", "ROI_TABLE", "TIME_STOP_MIN", "WARMUP",
           "Indicators", "LivePosition", "compute_indicators",
           "check_entry", "stop_price", "suggest_size", "check_exit"]


@dataclass(frozen=True)
class Indicators:
    """Индикаторы по буферу свечей (индекс i — сигнальный бар)."""

    rsi: tuple[float, ...]
    bb_mid: tuple[float, ...]
    atr: tuple[float, ...]


@dataclass(frozen=True)
class LivePosition:
    """Открытая demo/live-позиция стратегии."""

    entry_px: float
    stop_px: float
    entry_minute: float  # минуты (монотонные, те же единицы, что у now_minute)
    fee_pct: float = 0.10  # taker оценка выхода для нетто-ROI


def compute_indicators(bars: Sequence[Bar]) -> Indicators:
    """RSI/ATR Уайлдера и BB-mid по typical price. Нужно ≥ WARMUP баров."""
    if len(bars) < WARMUP:
        raise ValueError(f"нужно ≥ {WARMUP} баров прогрева, есть {len(bars)}")
    closes = [b.c for b in bars]
    highs = [b.h for b in bars]
    lows = [b.l for b in bars]
    tp = typical_price(highs, lows, closes)
    _, mid, _ = bollinger(tp, BB_N, BB_DEV)
    return Indicators(rsi=tuple(rsi_wilder(closes, RSI_N)),
                      bb_mid=tuple(mid),
                      atr=tuple(atr_wilder(highs, lows, closes, ATR_N)))


def check_entry(bars: Sequence[Bar], ind: Indicators, i: int = -1) -> bool:
    """§3 дизайна через backtest.entry_signal (дефолты RSI 30)."""
    idx = len(bars) + i if i < 0 else i
    return bt.entry_signal(ind.rsi, ind.bb_mid, bars, idx, RSI_ENTRY)


def stop_price(entry_close: float, atr_value: float,
               mult: float = ATR_MULT) -> float:
    """§5: close(t) − mult × ATR(14), фиксируется на сигнальном баре."""
    return entry_close - mult * atr_value


def suggest_size(equity: float, entry: float, stop: float, ct_val: float,
                 lot_sz: float, min_sz: float,
                 risk_pct: float = risk.DEFAULT_RISK_PCT) -> dict:
    """Сайзинг через риск-ядро (обрезает до потолка 15%, не отклоняет вход)."""
    return risk.size_position(equity, entry, stop, ct_val, lot_sz, min_sz,
                              risk_pct)


def check_exit(pos: LivePosition, bar: Bar, now_minute: float,
               bars: Sequence[Bar], ind: Indicators, i: int = -1) -> str | None:
    """Приоритет §6.3: 'stop' → 'roi' → 'time_stop' → 'signal'; None = держать.

    Стоп — внутрибарный («low раньше high»): low <= stop_px.
    ROI — нетто: вход с комиссией уже в entry_px учётом fee_pct, выход минус fee.
    """
    idx = len(bars) + i if i < 0 else i
    if bar.l <= pos.stop_px:
        return "stop"
    minutes = now_minute - pos.entry_minute
    req = bt.roi_required(ROI_TABLE, minutes)
    net_pct = (bar.c * (1 - pos.fee_pct / 100.0) / pos.entry_px - 1.0) * 100.0
    if req is not None and net_pct >= req:
        return "roi"
    if minutes >= TIME_STOP_MIN:
        return "time_stop"
    if bt.exit_signal(ind.rsi, ind.bb_mid, bars, idx, RSI_EXIT):
        return "signal"
    return None
