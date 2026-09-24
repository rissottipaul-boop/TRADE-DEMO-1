# K-Line Indicators Reference

## Complete Indicator Catalog

### Core Indicators (Required)

- **MA7, MA25, MA50**: Simple Moving Averages for trend identification
- **MACD (12,26,9)**: DIF, DEA, Histogram, crossover signals (DIF > DEA = bullish)
- **RSI6, RSI14**: Relative Strength Index (Wilder smoothing), ranges 0-100
  - < 30: Oversold (bullish signal)
  - 30-45: Bullish zone
  - 55-70: Bearish zone
  - > 70: Overbought (bearish signal)
- **BB (20,2)**: Bollinger Bands with %B position (0-100)
  - %B < 20%: Oversold region
  - %B > 80%: Overbought region
- **ATR14**: Average True Range for volatility measurement
- **Vol Ratio**: Current volume / 20-period average volume (>1.5 = strong signal confirmation)
- **Momentum 5, 10**: Close - Close[n] for directional strength

### Extended Indicators

**Trend & Momentum:**
- Stochastic (K%, D%, J%)
- CCI (Commodity Channel Index)
- Williams %R
- ADX (Average Directional Index) + DI+/DI-
- Ichimoku (Tenkan-sen, Kijun-sen, Senkou A/B, Chikou Span)
- SAR (Parabolic SAR)
- SuperTrend
- TRIX (Triple EMA)
- DPO (Detrended Price Oscillator)
- KST (Know Sure Thing)
- Aroon Oscillator (Aroon Up, Aroon Down)

**Volume & Order Flow:**
- OBV (On-Balance Volume)
- MFI (Money Flow Index)
- CMF (Chaikin Money Flow)
- AD (Accumulation/Distribution)
- PVT (Price-Volume Trend)
- Force Index
- Vortex (VI+ / VI-)
- Ultimate Oscillator
- RVI (Relative Vigor Index)

**Volatility:**
- Keltner Channels
- Donchian Channels
- Mass Index
- Elder Ray (Bull Power, Bear Power)

**Price Levels:**
- Pivot Points (Standard, Fibonacci, Woodie)
- Fibonacci Retracements/Extensions

**Advanced:**
- Heikin-Ashi Candlesticks
- VWAP (Volume-Weighted Average Price)

### Alpha Factors (101 + 191)

**Alpha 101**: ~40 time-series factors + ~60 cross-sectional factors
- Momentum-based: delta, rank operations, time-series rank
- Mean reversion: correlation with volume, RSI extremes
- Volatility-adjusted: standard deviation, variance ratios
- Price structure: open-close relationships, candle patterns

**Alpha 191**: ~80 time-series + ~90 cross-sectional factors (extended set)
- Advanced momentum dynamics
- Volume-price combinations
- Price structure decomposition
- Decay-weighted operations
- Cross-period relationships

## Indicator Calculation Details

### Signal Scoring (Single Period)

Each period generates a score (-5 to +5 range, then normalized to 0-100):

- Price > MA7/25/50: +1 per MA (3 points max)
- MACD just crossed up (+3) vs. continuing (+1)
- RSI < 30: +2, RSI 30-45: +1
- RSI > 70: -2, RSI 55-70: -1
- BB %B < 20%: +2 (oversold), > 80%: -2 (overbought)
- Volume ratio > 1.5: +1 (confirmation)
- Momentum (5,10) positive: +1

### Multi-Period Aggregation

- **Primary timeframe** (e.g., 1H for crypto): 60% weight
- **Upper level** (4H): ±10% confidence adjustment
- **Two levels up** (1D): ±5% confidence adjustment

Result: 0-100 composite score per period, then multi-period weighted average

## Candlestick Patterns (30+)

**Bullish:**
- Hammer
- Inverted Hammer
- Engulfing (bullish)
- Piercing
- Morning Star
- Three White Soldiers
- Kicking (by lower shadow)

**Bearish:**
- Hanging Man
- Shooting Star
- Engulfing (bearish)
- Dark Cloud Cover
- Evening Star
- Three Black Crows
- Kicking (by upper shadow)

**Continuation/Reversal:**
- Harami
- Harami Cross
- Spinning Top
- Doji variants (Dragonfly, Gravestone)
- Belt Hold
- Tri-star

## Divergence Detection

**Types:**
- **Bullish Divergence**: Price makes lower low, indicator makes higher low (reversal signal)
- **Bearish Divergence**: Price makes higher high, indicator makes lower high (reversal signal)

**Indicators Supporting Divergence:**
- RSI (most common)
- MACD (histogram)
- OBV
- Stochastic

**Scoring:**
- Regular divergence: +1 or -1 signal
- Hidden divergence: ±0.5 signal
- Strength grade: Weak (< 5 periods apart), Normal (5-10), Strong (10+ periods apart)

## Support & Resistance

**Automatic Detection:**
- Historical price clusters (2+ touches within 1% range)
- Previous highs/lows (swing points)
- Trend line breaks
- Market structure: HH/HL (uptrend), LH/LL (downtrend), LS/LS (accumulation)

**Output:**
- Level price, touch count, last touch date
- Break probability (based on volume, closes)
- Distance to current price (% away)
