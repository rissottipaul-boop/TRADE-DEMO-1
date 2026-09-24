"""Каузальные индикаторы бэктестера (backtester-design.md §1.1 п.4).

Контракт: out[i] зависит только от values[:i+1] — никаких глобальных агрегатов,
shift(-N) и заглядывания вперёд. Недостаточно истории -> NaN (NaN-guard движка
не даст открыть сделку на таком баре). Проверяется slicing/recursive-тестами.

Оконные суммы — через math.fsum по окну (точное округление, не зависит от точки
старта): SMA/BB бит-в-бит совпадают на прогонах с разной датой старта.
EMA/RSI/ATR рекуррентны (сглаживание Уайлдера) — сходятся после прогрева, поэтому
recursive-проверка для них идёт с допуском (как recursive-analysis Freqtrade).

Потребители: SMA-cross (базовый прогон), mean-reversion (RSI(14) Уайлдера, BB(20,2) от
typical price, ATR(14) — insights/meanrev-strategy-design.md §2, §5).
"""
import math
from typing import Sequence

NAN = float("nan")


def is_nan(x) -> bool:
    return x is None or (isinstance(x, float) and math.isnan(x))


def sma(values: Sequence[float], n: int) -> list[float]:
    if n <= 0:
        raise ValueError("n > 0")
    out = [NAN] * len(values)
    for i in range(n - 1, len(values)):
        out[i] = math.fsum(values[i - n + 1:i + 1]) / n
    return out


def ema(values: Sequence[float], n: int) -> list[float]:
    """EMA с затравкой SMA(n) на баре n-1, alpha = 2/(n+1)."""
    if n <= 0:
        raise ValueError("n > 0")
    out = [NAN] * len(values)
    if len(values) < n:
        return out
    alpha = 2.0 / (n + 1)
    e = math.fsum(values[:n]) / n
    out[n - 1] = e
    for i in range(n, len(values)):
        e = alpha * values[i] + (1 - alpha) * e
        out[i] = e
    return out


def _rsi_value(avg_gain: float, avg_loss: float) -> float:
    if avg_loss == 0:
        return 50.0 if avg_gain == 0 else 100.0
    return 100.0 - 100.0 / (1.0 + avg_gain / avg_loss)


def rsi_wilder(closes: Sequence[float], n: int = 14) -> list[float]:
    """RSI Уайлдера (1978): затравка — средние n изменений, далее avg=(avg*(n-1)+x)/n.

    Первое значение — на баре n (как у TA-Lib). Плоский ряд -> 50.
    """
    out = [NAN] * len(closes)
    if len(closes) <= n:
        return out
    gains = [max(closes[i] - closes[i - 1], 0.0) for i in range(1, n + 1)]
    losses = [max(closes[i - 1] - closes[i], 0.0) for i in range(1, n + 1)]
    avg_g = math.fsum(gains) / n
    avg_l = math.fsum(losses) / n
    out[n] = _rsi_value(avg_g, avg_l)
    for i in range(n + 1, len(closes)):
        ch = closes[i] - closes[i - 1]
        avg_g = (avg_g * (n - 1) + max(ch, 0.0)) / n
        avg_l = (avg_l * (n - 1) + max(-ch, 0.0)) / n
        out[i] = _rsi_value(avg_g, avg_l)
    return out


def true_range(highs: Sequence[float], lows: Sequence[float],
               closes: Sequence[float]) -> list[float]:
    out = [NAN] * len(closes)
    if closes:
        out[0] = highs[0] - lows[0]
    for i in range(1, len(closes)):
        pc = closes[i - 1]
        out[i] = max(highs[i] - lows[i], abs(highs[i] - pc), abs(lows[i] - pc))
    return out


def atr_wilder(highs: Sequence[float], lows: Sequence[float], closes: Sequence[float],
               n: int = 14) -> list[float]:
    """ATR Уайлдера: затравка — среднее TR[1..n] на баре n (как TA-Lib), далее сглаживание."""
    tr = true_range(highs, lows, closes)
    out = [NAN] * len(closes)
    if len(closes) <= n:
        return out
    a = math.fsum(tr[1:n + 1]) / n
    out[n] = a
    for i in range(n + 1, len(closes)):
        a = (a * (n - 1) + tr[i]) / n
        out[i] = a
    return out


def rolling_std(values: Sequence[float], n: int, ddof: int = 0) -> list[float]:
    out = [NAN] * len(values)
    if n - ddof <= 0:
        raise ValueError("n - ddof > 0")
    for i in range(n - 1, len(values)):
        w = values[i - n + 1:i + 1]
        m = math.fsum(w) / n
        out[i] = math.sqrt(math.fsum((x - m) ** 2 for x in w) / (n - ddof))
    return out


def bollinger(values: Sequence[float], n: int = 20, k: float = 2.0,
              ddof: int = 0) -> tuple[list[float], list[float], list[float]]:
    """(middle, upper, lower). ddof=0 — определение Боллинджера и TA-Lib;
    qtpylib (шаблон Freqtrade) — pandas.rolling.std, ddof=1: сверить при MEANREV-IMPL."""
    mid = sma(values, n)
    sd = rolling_std(values, n, ddof)
    upper = [m + k * s if not (is_nan(m) or is_nan(s)) else NAN for m, s in zip(mid, sd)]
    lower = [m - k * s if not (is_nan(m) or is_nan(s)) else NAN for m, s in zip(mid, sd)]
    return mid, upper, lower


def typical_price(highs: Sequence[float], lows: Sequence[float],
                  closes: Sequence[float]) -> list[float]:
    return [(h + l + c) / 3.0 for h, l, c in zip(highs, lows, closes)]


def crossed_above(a: Sequence[float], b: Sequence[float] | float, i: int) -> bool:
    """a пересекла b снизу вверх на баре i: a[i-1] <= b[i-1] и a[i] > b[i]."""
    if i < 1:
        return False
    b0, b1 = (b, b) if isinstance(b, (int, float)) else (b[i], b[i - 1])
    vals = (a[i], a[i - 1], b0, b1)
    if any(is_nan(v) for v in vals):
        return False
    return a[i - 1] <= b1 and a[i] > b0


def crossed_below(a: Sequence[float], b: Sequence[float] | float, i: int) -> bool:
    if i < 1:
        return False
    b0, b1 = (b, b) if isinstance(b, (int, float)) else (b[i], b[i - 1])
    vals = (a[i], a[i - 1], b0, b1)
    if any(is_nan(v) for v in vals):
        return False
    return a[i - 1] >= b1 and a[i] < b0
