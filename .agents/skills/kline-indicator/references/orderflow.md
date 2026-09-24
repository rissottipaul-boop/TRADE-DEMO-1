# Order Flow Analysis Reference

## Overview

Order flow analysis examines the actual buying and selling activity in the market to understand who is accumulating, distributing, or in distress. This reveals information invisible to standard technical indicators.

## Core Metrics

### Delta Analysis

**Definition**: Buy volume - Sell volume at each price level or over a time period

**Calculation**:
- For each trade: classify as BUY if price > previous trade, SELL if price < previous trade, HOLD if equal
- Aggregate over period: Delta = sum(BUY volume) - sum(SELL volume)

**Interpretation**:
- **Positive Delta**: More buying than selling (accumulation phase)
- **Negative Delta**: More selling than buying (distribution phase)
- **Delta Divergence**: Price rising but delta falling = weakening uptrend (bearish)

### CVD (Cumulative Volume Delta)

**Definition**: Running sum of Delta over multiple periods

**Interpretation**:
- **Rising CVD**: Persistent buying pressure (institutional accumulation)
- **Falling CVD**: Persistent selling pressure (smart money exit)
- **CVD Divergence**: CVD making higher lows while price makes lower lows (bullish reversal)

## Order Book Analysis

### Depth Distribution

**Data**: Real-time limit order book at various price levels (typically ±20 levels from mid-price)

**Metrics**:
- **Bid/Ask Imbalance**: Sum of bid volume vs. sum of ask volume
  - Imbalance > 1.2x = bullish (more buying intent)
  - Imbalance < 0.8x = bearish (more selling intent)
- **Resistance Clusters**: High ask volume at price levels (walls)
- **Support Clusters**: High bid volume at price levels (walls)

**Trading Implications**:
- Large ask walls at resistance = potential rejection
- Large bid walls at support = potential bounce
- Disappearing walls = liquidity removed (often before big moves)

### Wall Detection

**Large Single Wall** (when single level contains 5%+ of all buy/sell at that side):
- At resistance: Potential rejection point or accumulation level
- At support: Potential bounce point or profit-taking level
- Moving away from price: Liquidity removal (preparation for move)

## Trade Flow Analysis

### Aggressive Buy/Sell Ratio

**Definition**: Trades executed at ask / Trades executed at bid

**Interpretation**:
- **Ratio > 1**: Aggressive buyers (market-taker dominance), bullish
- **Ratio < 1**: Aggressive sellers, bearish
- **Spikes > 2**: One-sided flow, often precedes move in that direction

### Large Trade Detection

**Definition**: Individual trades > X% of 1-minute average volume

**Alert Conditions**:
- Multiple large buys in sequence = institutional buying, bullish
- Multiple large sells in sequence = institutional selling, bearish
- Large buy rejected by price (not followed by rally) = distribution
- Large sell soaked up (not followed by decline) = accumulation

### Main Order Takers

**Definition**: Aggressive parties continuously buying or selling through the order book

**Measurement**:
- Count 5-minute aggressive buy/sell ratio
- Compare to historical average
- Sustained ratio > 1.3x = main taker accumulating
- Sustained ratio < 0.7x = main taker distributing

## Volume Analysis

### Volume Distribution

**Metrics**:
- **Volume at Price Levels**: Where did trading occur? (price-volume profile)
- **Volume Trend**: Increasing/decreasing volume during move confirmation
- **Volume Concentration**: % of trading in top 5 price levels (high = institutional activity)

**Interpretation**:
- Volume profile with single peak = clear structure/support/resistance
- Volume profile with flat distribution = no clear institutional interest
- Rising volume on breakout = real breakout, not fake

### Relative Volume Analysis

**Definition**: Current period volume / Average of past 20 periods

**Ranges**:
- < 0.8x: Low volume, unreliable signal
- 0.8-1.2x: Normal volume
- 1.2-1.5x: Elevated volume, increased interest
- 1.5-2.0x: High volume, strong conviction
- > 2.0x: Extreme volume, reversal/breakout

## Composite Order Flow Score (0-100)

Combines multiple order flow signals into a single score:

### Scoring Components

1. **Delta Trend (20 points)**
   - Recent delta positive: +15
   - CVD making higher lows: +5

2. **CVD Divergence (15 points)**
   - Price falling but CVD rising: +15 (bullish)
   - Price rising but CVD falling: -15 (bearish)

3. **Order Book Imbalance (20 points)**
   - Bid > Ask by > 20%: +15
   - Major bid wall at support: +5
   - Major ask wall at resistance: -5 per wall

4. **Trade Flow (20 points)**
   - Aggressive buy/sell ratio > 1.5: +15
   - Main order taker accumulating (5m ratio > 1.3): +5
   - Main order taker distributing: -15

5. **Volume Confirmation (15 points)**
   - High volume on breakout: +15
   - Volume declining on move: -10
   - Relative volume > 1.5x: +5

6. **Anomaly Flags (10 points)**
   - Large liquidation cluster nearby: -5
   - Multiple Wall disappearances: +5
   - Hidden order spoofing detected: -10

### Score Zones

- **0-25**: Heavy selling pressure (bearish)
- **25-40**: Mild selling pressure
- **40-60**: Balanced (no clear direction)
- **60-75**: Mild buying pressure
- **75-100**: Heavy buying pressure (bullish)

## Integration with Trading

**Order Flow + Technical Indicators**:
- RSI oversold + positive CVD divergence = strong buy signal
- MACD death cross + falling CVD = strong sell signal
- Divergence with order flow confirmation = high conviction

**Order Flow + Price Action**:
- Support with big bid wall + low volume fall = likely bounce
- Resistance with big ask wall + high-volume push = likely rejection
- Price rejection at level but order book flips bullish = reversal coming

## Data Sources

**For OKX (via MCP or CLI)**:
- `market_get_orderbook(instId, sz=20)` → Real-time bid/ask levels
- `market_get_trades(instId, limit=100)` → Recent 100 trades
- `market_get_open_interest()` → Perpetual positioning
- `market_get_funding_rate()` → Derivatives sentiment

**Limitations**:
- Public orderbook = visible intention (smart money hides with iceberg orders)
- Trade data = taker direction (but not always matched with intention)
- Funding rate = leverage positioning (not order flow, but sentiment proxy)
