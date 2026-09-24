# 3-Pillar Analysis Framework

## Overview

The K-Line Indicator Engine uses a three-pillar framework to provide comprehensive market analysis. Each pillar answers a specific question about market conditions, and their weighted combination produces an overall market assessment.

## Pillar 1: Macro Cycle (30% weight)

**Question**: Where are we in the market cycle?

### Data Sources

- **Rainbow Chart**: Log regression bands (1-9) mapping BTC to cycle stages
- **AHR999**: (Price / 200D SMA) × (Price / Fitted Growth Value)
- **MVRV (Market Value / Realized Value)**: Long-term holder profit/loss ratio
- **Fear & Greed Index**: Sentiment aggregator from multiple sources
- **BTC Dominance**: BTC market cap % (high = risk-off, low = altseason)
- **BTC/GDP Ratio**: Long-term valuation metric
- **ETF Capital Flows**: Institutional money flows (positive/negative)
- **CAPE Ratio**: Cyclically-Adjusted PE for broader markets
- **Buffett Indicator**: Total market cap / GDP (macro overvaluation signal)

### Market Stage Classification

| Score | Stage | Action |
|-------|-------|--------|
| 0-20 | Deep Value / Capitulation | **Maximum accumulation** |
| 20-40 | Recovery / Early Bull | **Gradual accumulation** |
| 40-60 | Mid-Cycle / Neutral | **Selective positioning** |
| 60-80 | Late Bull / Overheating | **Risk reduction** |
| 80-100 | Mania / Distribution | **Maximum caution / Hedge** |

### Rainbow Chart Bands

1. **Fire Sale** — Extreme discount (DCA aggressively)
2. **BUY!** — Strong discount
3. **Accumulate** — Moderate discount
4. **Still Cheap** — Slight discount
5. **Hold** — Fair value
6. **Is This a Bubble?** — Warming up
7. **FOMO** — Euphoria building
8. **Sell** — Extreme euphoria
9. **Maximum Bubble** — Parabolic peak

**Formula**: Fitted Band = 10^(a × days_since_genesis + b)
*Calculated using log-linear regression on BTC's full daily history*

## Pillar 2: Price-Volume Factors (40% weight)

**Question**: What do price and volume signals tell us?

### Components

- **Core Indicators** (Step 1): MA trends, MACD crossovers, RSI extremes, BB position, ATR, momentum, volume confirmation
- **Extended Indicators** (Step 2): Stochastic, CCI, Williams%R, ADX, Ichimoku, SAR, SuperTrend, VWAP, OBV, MFI, Pivot points, Fibonacci, Heikin-Ashi, Keltner, Donchian, CMF, TRIX, DPO, KST, Aroon, Elder Ray, Mass Index, Vortex, Ultimate Oscillator, RVI, Force Index
- **Candlestick Patterns** (Step 2): 30+ patterns (Hammer, Engulfing, Morning Star, Harami, Doji, etc.)
- **Divergences** (Step 2): RSI, MACD, OBV top/bottom divergence detection
- **Support/Resistance** (Step 2): Automatic level identification from price clusters, swing points, structure
- **Alpha Factors** (Step 2): ~40 time-series (Alpha 101) + ~80 extended (Alpha 191) price-volume relationships

### Multi-Period Aggregation

Signals are computed across **5 timeframes** (5m, 15m, 1H, 4H, 1D):
- Primary frame: 60% weight
- One level up: ±10% confidence
- Two levels up: ±5% confidence

**Result**: Normalized 0-100 composite score reflecting price-volume consensus

## Pillar 3: Derivatives (30% weight)

**Question**: What are smart money and derivative markets pricing in?

### Data Sources

- **Funding Rate Depth**: OKX perpetual funding rate (current + 30-day trend)
  - Positive = Longs paying shorts (overheating)
  - Negative = Shorts paying longs (oversold)
- **Basis / Carry**: Futures premium/discount vs. spot (contango = optimism)
- **DVOL (Deribit Volatility Index)**: Market's 30-day volatility expectation
- **BVOL (Realized Volatility)**: Recent actual 30-day volatility
- **Option Skew**: IV distribution (calls vs. puts) — predicts expected direction
- **Open Interest**: Total $ value of open contracts (trend = direction strength)
- **Long/Short Ratio**: Directional positioning (from CoinGlass, OKX)
- **Liquidation Heat Map**: Cluster analysis of cascading liquidations

### Interpretation

| Metric | Bullish | Bearish |
|--------|---------|---------|
| **Funding Rate** | Negative (shorts paying) | Positive (longs paying) |
| **Basis** | Positive contango | Negative backwardation |
| **Open Interest Trend** | Rising with price | Rising with decline |
| **Liquidation Clusters** | Shorts being liquidated | Longs being liquidated |
| **Option Skew** | Call skew (upside bias) | Put skew (downside bias) |

## Combined Score Calculation

### Formula

**Composite Score** = (Pillar1_Score × 0.30) + (Pillar2_Score × 0.40) + (Pillar3_Score × 0.30)

Each pillar ranges 0-100, so result is 0-100.

### Interpretation Zones

- **0-20**: Deep value (maximum accumulation opportunity)
- **20-40**: Recovery phase (gradual risk-on)
- **40-60**: Mid-cycle (selective positioning, neutral bias)
- **60-80**: Late bull (reduce leverage, take profits)
- **80-100**: Mania phase (extreme caution, hedge long positions)

## Typical Market Cycle Sequence

1. **Capitulation (0-20)**: Fear prevails, capitulation volume, lows cluster, funding negative
   → **Action**: Aggressive accumulation

2. **Recovery (20-40)**: Volume increases, support holds, funding turns positive
   → **Action**: Continue accumulation, reduce hedges

3. **Trending (40-60)**: Strong price-volume confirmation, macro improving, OI rising
   → **Action**: Hold full allocation, selective adds

4. **Overheating (60-80)**: Extreme sentiment, divergences forming, retail FOMO
   → **Action**: Reduce leverage, establish hedges, take profits on strength

5. **Mania (80-100)**: Parabolic moves, retail euphoria, max liquidation risk
   → **Action**: Severe risk reduction, full hedges, short opposing bets

## Usage in Trading Decisions

The 3-Pillar score informs:
- **Position sizing**: Higher score = smaller positions (risk management)
- **Leverage**: Higher score = lower leverage (mania risk)
- **Entry timing**: Lower score = more aggressive entries
- **Exit strategy**: Higher score = tighter stops, aggressive profit-taking
- **Instrument selection**: Higher score = spot only, lower score = contracts allowed

For automated trading, only positions with **composite score between 30-70** and **profit factor ≥ 1.5:1** are recommended.
