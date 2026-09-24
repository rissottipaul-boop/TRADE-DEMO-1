# Trading Signals & Decision Framework

## Signal Quality Grades

### Technical Signal Strength

| Grade | Criteria | Confidence | Action |
|-------|----------|------------|--------|
| **A+ (Strongest)** | MACD gold cross + RSI < 45 + price > MA25 + volume > 1.5x + positive CVD + macro score 30-50 | 95%+ | **Immediate entry** |
| **A (Very Strong)** | 3+ converging signals (MACD, RSI, BB, volume) + macro confirmation | 85-95% | **Enter position** |
| **B+ (Strong)** | 2-3 signals aligning + macro neutral | 75-85% | **Enter with sizing** |
| **B (Good)** | Single strong indicator + divergence confirmation | 60-75% | **Small entry** |
| **C (Weak)** | Single indicator in isolation | 40-60% | **Alert only** |
| **D (Insufficient)** | No confirmation, isolated signal | < 40% | **Ignore** |

### Pattern Recognition Grades

| Pattern Type | Strength | Success Rate | Minimum Trade | Risk/Reward |
|--------------|----------|--------------|---|---|
| **Strong reversal** (Morning Star, Engulfing at support with CVD) | A+ | 75-85% | 1.5:1 | Tight stops |
| **Breakout patterns** (3 White Soldiers, Kicking with volume) | A | 70-80% | 1.8:1 | Moderate |
| **Continuation** (Harami, Spinning Top consolidation) | B+ | 60-70% | 2:1 | Wider stops |
| **Neutral/Uncertain** (Doji in isolation) | C | 50-55% | 2.5:1 | Manual review |

## Entry Conditions (Mode: trade)

Entry is generated only when ALL three conditions are met:

### 1. Signal Quality (Weighted Score ≥ 65/100)

Composite from:
- **Price-Volume (40%)**: MACD, RSI, BB, volume confirmation scoring
- **Pattern (30%)**: Candlestick pattern grade + divergence strength
- **Order Flow (20%)**: Delta trend, CVD divergence, bid/ask imbalance
- **Macro Context (10%)**: 3-pillar score (0-100 normalized)

**Minimum**: 65 points = confidence threshold

### 2. Risk/Reward Ratio ≥ 1.5:1

**Calculation**:
```
Entry Price = current close
Target Price = first resistance (swing high / MACD resistance)
Stop Loss = nearest support (swing low / MA or pivot)

R/R = (Target - Entry) / (Entry - Stop)
```

**Minimum**: R/R ≥ 1.5 (e.g., 100 pips risk for 150+ pips gain)

**Special Cases**:
- Market maker strategies: R/R ≥ 1.2:1 (thin spreads)
- Long-term holds: R/R ≥ 2:1 (lower win rate)

### 3. Macro Score in Acceptable Zone (30-70/100)

**Why**:
- Score 0-30: Capitulation phase (sideways/choppy, low win rate)
- Score 30-70: Trending phase (higher win rate)
- Score 70-100: Mania phase (high volatility, low accuracy)

**Recommendation**: Best range is **40-65** (trending but not overheated)

## Position Sizing

### Kelly Criterion Adaptation

```
Position Size = (Win% × RR - Loss%) / RR

Where:
  Win% = historical win rate for this signal (60-75% typical)
  RR = risk/reward ratio
  Loss% = 1 - Win%
```

### Risk Management by Score

| Macro Score | Leverage | Position Size | Stop Loss |
|---|---|---|---|
| **0-20** | Up to 10x | 5-10% per trade | 1.5x normal (loose) |
| **20-40** | Up to 5x | 2-5% per trade | Normal |
| **40-60** | Up to 3x | 2-4% per trade | Normal |
| **60-80** | Up to 2x | 1-2% per trade | 0.8x normal (tight) |
| **80-100** | Up to 1x (spot) | 0.5-1% per trade | 0.5x normal (very tight) |

### Single-Trade Risk Cap

Default: **2% of account per trade** (adjustable via `--risk 2`)

```
Dollar Risk = Account Size × Risk% × (1 + Leverage)

Example:
  Account: $10,000
  Risk: 2%
  Leverage: 5x
  Dollar Risk = $10,000 × 0.02 × 5 = $1,000

  Entry: $50,000, Stop: $49,000 (=$1,000 loss)
  Position: 20 contracts (on $50,000)
```

## Exit Signals

### Profit-Taking Targets

| Profit Level | Target | Action |
|---|---|---|
| **First Target (Partial)** | First resistance / MACD resistance | Take 30-40% profit |
| **Second Target (Reduce)** | Previous swing high / BB upper band | Take another 30-40% |
| **Trailing** | Remaining position: use 2-3% trailing stop | Let winner run |

### Stop-Loss Triggers

**Hard Stops** (exit immediately):
1. Macro score flips to > 80 or < 20 (regime change)
2. Critical support breaks with volume
3. Anti-correlated asset signals reversal
4. Funding rate spikes (liquidation risk)

**Soft Stops** (partial exit or tighten):
1. Signal confirmation weakens (MACD flattens, RSI loses momentum)
2. Volume dries up on move
3. Pattern becomes less favorable

## Conditional Trading Workflow

### Pre-Trade Checklist

```
1. [ ] Is Macro Score in 30-70 range? (YES/NO)
2. [ ] Is Weighted Signal Score ≥ 65? (YES/NO)
3. [ ] Is R/R ≥ 1.5:1? (YES/NO)
4. [ ] Is position size ≤ 2% risk? (YES/NO)
5. [ ] Is order book healthy (no huge walls)? (YES/NO)
6. [ ] CVD positive for buy, negative for sell? (YES/NO)
```

If all YES: Generate trade plan and ask for confirmation
If any NO: Display reason and skip trade

### Trade Execution Flow

1. **Analysis Phase**: Run full scan (Pillar 2 + macro score + orderflow)
2. **Signal Scoring**: Compute weighted score from indicators
3. **Entry Validation**: Check R/R and macro filters
4. **Size Calculation**: Apply Kelly/risk percentage
5. **Order Generation**: Entry, target 1-2, trailing stop
6. **Confirmation Gate**: Show plan, wait for user `/confirm`
7. **Execution**: Place orders via OKX MCP or CLI
8. **Monitoring**: Track position, exit on signals

### Parameter Defaults

```
--risk 2              # 2% account per trade
--leverage 5          # 5x for spot/perpetuals
--mode isolated       # Cross/isolated margin
--type swap           # spot or swap (perpetual)
--confirm false       # Require confirmation (--confirm = skip)
--dry-run true        # Show plan only (--execute = place orders)
```

## Backtesting & Win Rate

### Typical Win Rates by Signal

| Signal Type | Backtest Win% | Live Win% | Notes |
|---|---|---|---|
| MACD + RSI + MA triple confluence | 72% | 68% | Fewer signals, higher quality |
| Pattern + volume confirmation | 65% | 60% | More signals, balanced |
| Single indicator (MACD only) | 55% | 50% | Too many false signals |
| Random price levels | 50% | 50% | Baseline (no edge) |

### Profit Factor Target

**Profit Factor = Total Winning $ / Total Losing $**

- **Minimum viable**: 1.2:1 (winning trades are 20% more $ than losers)
- **Target**: 1.5:1 (50% more $ from winners)
- **Excellent**: 2:1+ (twice as much from winners)

With 60% win rate and 1.5:1 R/R:
```
Profit Factor = (0.6 × 1.5) / (0.4 × 1) = 0.9 / 0.4 = 2.25
Expected Return per 100 trades: 60 × 1.5 units - 40 × 1 unit = 90 - 40 = 50 units
```

## Risk Warnings

### When NOT to Trade

1. **Macro Score extreme** (< 20 or > 80): Avoid automated trading, use manual signals only
2. **Funding rate spike** (> 0.1% or < -0.1% hourly): Exit long positions, liquidation risk
3. **Liquidation cascade nearby**: Don't use max leverage near cluster
4. **Low volume period** (e.g., weekends): Wider spreads, order book thin
5. **Breaking news event**: Wait for volatility to settle (30+ mins)
6. **Unfilled orders**: If orders don't fill within 2 minutes, cancel and reassess

### Slippage & Fees

Account for:
- **Maker fee**: -0.02% OKX (place limit orders)
- **Taker fee**: -0.05% OKX (market orders)
- **Slippage**: -0.1-0.2% on larger orders
- **Funding rate carry**: ±0.01% per 8 hours if holding perpetuals

Adjust R/R calculation:
```
Actual Net R/R = (Target - Entry - Fees - Slippage) / (Entry - Stop + Fees + Slippage)
```
