"""
Technical indicator computation for DCA automation conditions.
Used to compute RSI, MACD, and Bollinger Band positions.
"""


def calc_rsi(prices, period=14):
    """
    Calculate Relative Strength Index (RSI).

    Args:
        prices: List of price values
        period: RSI period (default 14)

    Returns:
        RSI value (0-100). Returns 50 if insufficient data.
    """
    if len(prices) < period + 1:
        return 50

    gains, losses = [], []
    for i in range(1, len(prices)):
        d = prices[i] - prices[i - 1]
        gains.append(max(d, 0))
        losses.append(max(-d, 0))

    avg_gain = sum(gains[-period:]) / period
    avg_loss = sum(losses[-period:]) / period

    if avg_loss == 0:
        return 100

    rs = avg_gain / avg_loss
    return 100 - 100 / (1 + rs)


def calc_macd(prices):
    """
    Calculate MACD (Moving Average Convergence Divergence).

    Args:
        prices: List of price values

    Returns:
        Dict with keys: dif, dea, histogram
    """
    def ema(prices, period):
        if not prices:
            return []
        ema_vals = [prices[0]]
        k = 2 / (period + 1)
        for p in prices[1:]:
            ema_vals.append(p * k + ema_vals[-1] * (1 - k))
        return ema_vals

    ema12 = ema(prices, 12)
    ema26 = ema(prices, 26)
    dif = [ema12[i] - ema26[i] for i in range(len(prices))]
    dea = ema(dif, 9)
    histogram = [dif[i] - dea[i] for i in range(len(prices))]

    return {
        "dif": dif[-1] if dif else 0,
        "dea": dea[-1] if dea else 0,
        "histogram": histogram[-1] if histogram else 0,
    }


def calc_bb_position(prices, period=20):
    """
    Calculate Bollinger Band position (0-100).

    Position 0: Price at lower band
    Position 50: Price at SMA (middle)
    Position 100: Price at upper band

    Args:
        prices: List of price values
        period: Bollinger Band period (default 20)

    Returns:
        Position value (0-100). Returns 50 if insufficient data.
    """
    if len(prices) < period:
        return 50

    sma = sum(prices[-period:]) / period
    variance = sum((p - sma) ** 2 for p in prices[-period:]) / period
    std = variance ** 0.5

    if std == 0:
        return 50

    upper = sma + 2 * std
    lower = sma - 2 * std
    current_price = prices[-1]

    pos = (current_price - lower) / (upper - lower) * 100
    return max(0, min(100, pos))


def calc_atr(highs, lows, closes, period=14):
    """
    Calculate Average True Range (ATR).

    Useful for volatility assessment and stop-loss sizing.

    Args:
        highs: List of high prices
        lows: List of low prices
        closes: List of close prices
        period: ATR period (default 14)

    Returns:
        ATR value. Returns 0 if insufficient data.
    """
    if len(highs) < period or len(lows) < period or len(closes) < period:
        return 0

    true_ranges = []
    for i in range(1, len(closes)):
        tr = max(
            highs[i] - lows[i],
            abs(highs[i] - closes[i - 1]),
            abs(lows[i] - closes[i - 1]),
        )
        true_ranges.append(tr)

    if len(true_ranges) < period:
        return 0

    atr = sum(true_ranges[-period:]) / period
    return atr


def calc_stochastic(highs, lows, closes, period=14, smooth=3):
    """
    Calculate Stochastic Oscillator.

    Useful for overbought (>80) and oversold (<20) detection.

    Args:
        highs: List of high prices
        lows: List of low prices
        closes: List of close prices
        period: Lookback period (default 14)
        smooth: Smoothing period for %K (default 3)

    Returns:
        Dict with keys: k_value, d_value
    """
    if len(closes) < period:
        return {"k_value": 50, "d_value": 50}

    lowest_low = min(lows[-period:])
    highest_high = max(highs[-period:])

    if highest_high == lowest_low:
        return {"k_value": 50, "d_value": 50}

    fast_k_values = []
    for i in range(len(closes)):
        if i < period - 1:
            continue
        ll = min(lows[i - period + 1 : i + 1])
        hh = max(highs[i - period + 1 : i + 1])
        fast_k = (closes[i] - ll) / (hh - ll) * 100 if hh != ll else 50
        fast_k_values.append(fast_k)

    if len(fast_k_values) < smooth:
        return {"k_value": 50, "d_value": 50}

    k_value = sum(fast_k_values[-smooth:]) / smooth
    d_value = sum(fast_k_values[-(smooth + 1) : -1]) / smooth if len(fast_k_values) > smooth else k_value

    return {"k_value": k_value, "d_value": d_value}
