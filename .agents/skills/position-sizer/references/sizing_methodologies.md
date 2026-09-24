# Crypto Position Sizing Methodologies

## Overview

Position sizing determines how many contracts (perpetual swap) or units (spot) to trade. Correct sizing is the single most important factor in long-term account survival. A great trade idea with bad sizing can destroy an account; a mediocre trade with proper sizing preserves capital for the next opportunity.

This reference covers three primary methods: Fixed Fractional, ATR-based, and Kelly Criterion — all adapted for OKX crypto spot and perpetual swap trading.

---

## Fixed Fractional Method (Percentage Risk)

### Concept

Risk a fixed percentage of account equity on every trade. The most widely used method among professional traders. Direction-agnostic — works identically for long and short positions.

### Formula

**Perpetual swap:**
```
risk_per_contract = abs(entry_price - stop_price) * ctVal
dollar_risk       = account_size * risk_pct / 100
contracts         = floor(dollar_risk / risk_per_contract)
```

**Spot:**
```
risk_per_unit = abs(entry_price - stop_price)
dollar_risk   = account_size * risk_pct / 100
units         = dollar_risk / risk_per_unit
```

### Standard Risk Levels

| Risk % | Profile | Notes |
|--------|---------|-------|
| 0.25-0.50% | Conservative / large account | Institutional-grade risk |
| 0.50-1.00% | Experienced trader | Recommended default range |
| 1.00-1.50% | Active trader, proven edge | Standard for tested systems |
| 1.50-2.00% | Aggressive, high win-rate | Maximum for most strategies |
| > 2.00% | Dangerous | Ruin risk increases rapidly |

### Example: BTC-USDT-SWAP Long

- Account: 100,000 USDT
- Entry: $84,200, Stop: $80,000, ctVal: 0.01
- risk_per_contract = |84,200 - 80,000| × 0.01 = **$42.00**
- At 1% risk: $1,000 / $42.00 = **23 contracts**
- Notional: 23 × 0.01 × $84,200 = $19,366
- → `okx swap place --sz 23`

### Example: ETH-USDT Spot

- Account: 50,000 USDT
- Entry: $3,200, Stop: $3,000
- risk_per_unit = |3,200 - 3,000| = $200
- At 1% risk: $500 / $200 = **2.5 ETH**
- → `okx spot place --sz 2.5`

### When to Use

- Default method for most directional trades (long or short)
- When you have a clear technical stop level (support/resistance, MA, prior low/high)
- When trading a system with established risk parameters

---

## ATR-Based Method (Volatility Sizing)

### Concept

Use the Average True Range (ATR) to set stop distance, automatically adjusting position size to an instrument's volatility. Originated with the Turtle Traders (Richard Dennis, 1983).

**Get ATR from OKX CLI:**
```bash
okx market indicator atr BTC-USDT-SWAP --bar 1D --params 14 --list --limit 1
```

### Formula

```
stop_distance = atr * atr_multiplier
# Long: stop = entry - stop_distance
# Short: stop = entry + stop_distance
risk_per_contract = stop_distance * ctVal
dollar_risk       = account_size * risk_pct / 100
contracts         = floor(dollar_risk / risk_per_contract)
```

### ATR Multiplier Guidance

| Multiplier | Stop Width | Style |
|-----------|-----------|-------|
| 1.0x | Tight | Scalping, very short-term |
| 1.5x | Moderate-tight | Swing trading, 2-5 day holds |
| 2.0x | Standard | Default for most swing trades |
| 2.5x | Wide | Position trading, 2-8 week holds |
| 3.0x | Very wide | Trend following, multi-month holds |

### Example: ETH-USDT-SWAP Long

- Account: 100,000 USDT, Risk: 1%
- Entry: $3,200, ATR(14) = $120, Multiplier = 2.0x, ctVal = 0.1
- Stop distance: $240, Stop: $2,960
- risk_per_contract = $240 × 0.1 = **$24.00**
- Dollar risk: $1,000
- Contracts: floor($1,000 / $24.00) = **41 contracts**
- → `okx swap place --sz 41`

### When to Use

- When you want volatility-adjusted sizing across different crypto assets
- When an instrument lacks clear support/resistance for a discrete stop
- For systematic/mechanical trading systems
- When comparing opportunities across assets with different volatility profiles

### Advantages Over Fixed Stop

1. Low-volatility assets get larger positions (tighter stop relative to price)
2. High-volatility assets get smaller positions (wider stop protects against noise)
3. Normalizes risk across the portfolio regardless of asset price or volatility

---

## Kelly Criterion

### Concept

The Kelly Criterion calculates the mathematically optimal fraction of capital to risk, given known win rate and payoff ratio. Developed by John L. Kelly Jr. (1956) at Bell Labs.

### Formula

```
R              = avg_win / avg_loss       (payoff ratio)
kelly_pct      = W - (1 - W) / R         (full Kelly percentage)
half_kelly_pct = kelly_pct / 2           (practical recommendation)
```

Where W = historical win rate (0 to 1).

### Full Kelly vs. Half Kelly

**Full Kelly** maximizes long-term geometric growth but produces extreme drawdowns (50%+). No professional fund uses full Kelly.

**Half Kelly** achieves approximately 75% of the theoretical growth rate with dramatically lower drawdowns.

| Metric | Full Kelly | Half Kelly | Quarter Kelly |
|--------|-----------|-----------|---------------|
| Growth rate | 100% | ~75% | ~50% |
| Max drawdown | Severe (50%+) | Moderate (25-35%) | Mild (15-20%) |
| Practical use | Never | Aggressive | Conservative |

### Kelly and Leverage

When using leverage, effective Kelly = kelly_pct / leverage. Leverage amplifies both gains and losses, so the Kelly fraction must be scaled down proportionally.

**Example:**
- Win rate: 55%, Avg win: $2.50, Avg loss: $1.00
- R = 2.5, Kelly = 0.55 - 0.45/2.5 = 0.37 = **37%**
- Half Kelly = **18.5%**
- With 10x leverage: effective = 18.5% / 10 = **1.85%**
- On $100,000 account: risk budget = $1,850

This ensures the leverage-amplified risk stays within sustainable bounds.

### Negative Expectancy

When the Kelly formula produces a negative value, the system has negative expected value. Kelly floors at 0% — meaning "do not trade this system."

### Two Modes of Use

1. **Budget Mode** (no entry price): Returns a recommended risk budget as a percentage. Useful for capital allocation planning.
2. **Contracts Mode** (with entry and stop): Converts the half-Kelly budget into a specific contract count using the entry/stop distance and contract value.

### When to Use

- When you have reliable historical win rate and payoff statistics (100+ trades minimum)
- For portfolio-level capital allocation across multiple strategies
- As a ceiling check: "Am I risking more than Kelly suggests?"
- Not suitable for discretionary traders without track records

---

## Leverage and Liquidation

### OKX Isolated Margin Liquidation Formula

For USDT-margined perpetual swaps:

| Direction | Formula |
|-----------|---------|
| Long | `liq = (margin - ctVal × \|N\| × entry) / (ctVal × \|N\| × (mmr + fee - 1))` |
| Short | `liq = (margin + ctVal × \|N\| × entry) / (ctVal × \|N\| × (mmr + fee + 1))` |

**Variables:**
- `margin` = margin balance (initial margin = ctVal × |N| × entry / leverage)
- `ctVal` = contract face value (e.g., 0.01 for BTC-USDT-SWAP)
- `|N|` = absolute number of contracts
- `entry` = average open price
- `mmr` = maintenance margin rate (tiered by position size, Tier 1 ≈ 0.4%)
- `fee` = taker fee rate (VIP0 = 0.05%)

### Leverage vs. Liquidation Distance

Example: entry = $84,200, mmr = 0.4%, fee = 0.05%

| Leverage | Liq (Long) | Distance | Liq (Short) | Distance |
|----------|-----------|----------|-------------|----------|
| 5x | ~$67,698 | -19.6% | ~$100,702 | +19.6% |
| 10x | ~$76,123 | -9.6% | ~$92,451 | +9.8% |
| 20x | ~$80,075 | -4.9% | ~$88,325 | +4.9% |
| 50x | ~$82,512 | -2.0% | ~$85,888 | +2.0% |
| 125x | ~$83,528 | -0.8% | ~$84,872 | +0.8% |

### Risk Warnings

1. **Isolated vs. Cross margin**: The script calculates isolated margin estimates only. In cross margin mode, liquidation price fluctuates with all positions' PnL — it is not a fixed value. After opening a position in cross mode, verify via `okx account positions`.

2. **MMR tiers**: Default mmr=0.004 applies to Tier 1 (small positions). Larger positions fall into higher tiers with higher MMR, meaning liquidation is closer than estimated. Check OKX's tiered margin rules for your position size.

3. **Stop vs. Liquidation**: If your stop-loss price is beyond the liquidation price (long: stop < liq, short: stop > liq), the position will be liquidated before the stop triggers. This means a total loss of margin rather than a controlled exit. Always verify this relationship before trading.

---

## Spot vs. Perpetual Sizing

| Dimension | Spot | Perpetual Swap |
|-----------|------|----------------|
| Output unit | Base currency (e.g., 0.238 BTC) | Contracts (integer) |
| `--sz` param | Base amount | Number of contracts |
| Leverage | None (1x) | 1x–Nx (varies by instrument) |
| Liquidation risk | None | Yes |
| Funding cost | None | Settled every 8 hours |
| Best for | Long-term holds, DCA | Directional trades, hedging |

**When to use spot**: Accumulating positions over time, no leverage needed, holding through volatility.

**When to use swap**: Taking directional views with leverage, hedging spot holdings, short selling.

---

## Portfolio Constraints

### Maximum Position Size

Limit any single position to a percentage of account value:

**Swap:**
```
max_contracts = floor(account_size * max_position_pct / 100 / (entry_price * ctVal))
```

**Guidelines:**
- 5-10%: Conservative (diversified portfolio, 10-20 positions)
- 10-15%: Moderate (concentrated portfolio, 7-10 positions)
- 15-25%: Aggressive (high-conviction, 4-7 positions)
- > 25%: Speculative (not recommended)

### Position Count and Diversification

| Positions | Diversification | Notes |
|-----------|----------------|-------|
| 1-4 | Very concentrated | High volatility, requires high conviction |
| 5-10 | Focused | Sweet spot for active traders |
| 10-20 | Diversified | Diminishing returns above 15 |
| 20+ | Over-diversified | Dilutes edge, approaches index performance |

### Binding Constraint Logic

When multiple constraints apply, the strictest (minimum quantity) wins:

1. Risk-based quantity (from Fixed Fractional, ATR, or Kelly)
2. Max position % limit
3. Exchange max-size limit (from `okx account max-size`)
4. Final = minimum of all candidates

---

## Method Comparison

| Feature | Fixed Fractional | ATR-Based | Kelly Criterion |
|---------|-----------------|-----------|-----------------|
| Input needed | Entry, stop, risk % | Entry, ATR, multiplier, risk % | Win rate, avg win/loss |
| Adjusts for volatility | No | Yes | No (uses historical stats) |
| Requires track record | No | No | Yes (100+ trades) |
| Best for | Discretionary trades | Systematic/mechanical | Capital allocation |
| Stop determined by | Chart analysis | ATR calculation | External (chart or ATR) |

### Recommended Workflow

1. **Start with Fixed Fractional** at 1% risk for new strategies or market conditions
2. **Switch to ATR-based** when comparing opportunities across different volatility profiles
3. **Use Kelly as a ceiling** after accumulating 100+ trade records
4. **Always apply constraints** (position limit, exchange max-size) as a final filter
5. **Reduce risk** after consecutive losses (progressive exposure in reverse)

---

## Risk Management Principles

### The 1% Rule

Never risk more than 1% of account equity on a single trade:

- 10 consecutive losses at 1% = 9.6% drawdown (recoverable)
- 10 consecutive losses at 5% = 40.1% drawdown (devastating)
- 10 consecutive losses at 10% = 65.1% drawdown (account-threatening)

### Portfolio Heat

Total open risk across all positions should not exceed 6-8% of account:

```
portfolio_heat = sum(quantity_i * risk_per_unit_i * ctVal_i) / account_size * 100
```

If portfolio heat exceeds 8%, do not add new positions until existing trades are moved to breakeven or closed.

### Asymmetry of Losses

Losses require disproportionately larger gains to recover:

| Loss | Gain to Recover |
|------|----------------|
| 10% | 11.1% |
| 20% | 25.0% |
| 30% | 42.9% |
| 50% | 100.0% |
| 75% | 300.0% |

This asymmetry is why position sizing and loss cutting are more important than entry selection.
