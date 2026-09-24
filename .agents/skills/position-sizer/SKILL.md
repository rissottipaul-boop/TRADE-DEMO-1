---
name: position-sizer
description: >-
  Crypto position sizing calculator for OKX spot and perpetual swap.
  Use when the user asks how many contracts to open, how much to buy,
  or evaluates trade risk given entry/stop/leverage/account size.
  Covers fixed-fractional, Kelly criterion, ATR stops, liquidation
  price, and leverage comparison. Trigger: position size, how many
  contracts, risk per trade, 开多少张, 仓位计算, 风险大不大, 该买多少.
---

# OKX Crypto Position Sizer

## Triggers

- "How many contracts should I long/short?"
- "Position size for BTC-USDT-SWAP with 10x leverage"
- "How much risk am I taking on this trade?"
- "Kelly criterion for my win rate"
- "ATR-based stop and size for ETH"
- "帮我算一下 BTC 仓位"
- "做空 ETH 开多少张合约"
- "10x 杠杆开仓数量"

## Prerequisites

- Python 3.9+ (for script mode — see inline fallback if unavailable)
- OKX Trade Kit optional (auto-fills account size, contract params, leverage)

## Workflow

> All `okx` CLI commands below use `--profile demo` by default. Replace with `--profile live` for real trades. Always confirm profile before placing orders.

### Step 1: Gather Account Size

**Option A — Auto-fill from OKX CLI** (recommended):
```bash
okx --profile demo account balance USDT
```
Extract `equity` field as `--account-size`.

**Option B — User provides manually**: Ask for total account equity in USDT.

### Step 2: Gather Trade Parameters

Collect from user:
- **Side**: long or short
- **Inst type**: spot or swap (default: swap)
- **Entry price** and **stop price** (or ATR for Mode B)
- **Risk %** (default 1%) or **Kelly stats** (win rate, avg win/loss)
- **Leverage** (swap only, default 1)

**Fetch current price:**
```bash
okx --profile demo market ticker BTC-USDT-SWAP
```

**Fetch ATR (for ATR-based sizing):**
```bash
okx market indicator atr BTC-USDT-SWAP --bar 1D --params 14 --list --limit 1
```

**Fetch contract parameters:**
```bash
okx market instruments --instType SWAP --instId BTC-USDT-SWAP
```
Extract `ctVal` (contract value → `--contract-value`), `lotSz` (order precision → `--lot-size` for spot), `minSz`.

**Common ctVal reference (avoid API call for popular pairs):**

| Instrument | ctVal | Meaning |
|-----------|-------|---------|
| BTC-USDT-SWAP | 0.01 | 1 contract = 0.01 BTC |
| ETH-USDT-SWAP | 0.1 | 1 contract = 0.1 ETH |
| SOL-USDT-SWAP | 1 | 1 contract = 1 SOL |
| DOGE-USDT-SWAP | 100 | 1 contract = 100 DOGE |

**Check/set leverage (varies by instrument):**
```bash
okx --profile demo swap get-leverage --instId BTC-USDT-SWAP --mgnMode cross
# Set if needed:
okx --profile demo swap leverage --instId BTC-USDT-SWAP --lever 10 --mgnMode cross
```

### Step 3: Run Position Sizer Script

```bash
# Swap long + 10x leverage
python3 scripts/position_sizer.py \
  --account-size 100000 --entry 84200 --stop 80000 --risk-pct 1.0 \
  --side long --contract-value 0.01 --leverage 10 --inst-type swap \
  --output-dir /tmp/position-sizer-reports/

# Swap short + 5x leverage
python3 scripts/position_sizer.py \
  --account-size 100000 --entry 84200 --stop 88000 --risk-pct 1.0 \
  --side short --contract-value 0.01 --leverage 5 --inst-type swap \
  --output-dir /tmp/position-sizer-reports/

# Spot (--lot-size from instruments API lotSz field)
python3 scripts/position_sizer.py \
  --account-size 100000 --entry 84200 --stop 80000 --risk-pct 1.0 \
  --inst-type spot --lot-size 0.00001 \
  --output-dir /tmp/position-sizer-reports/

# ATR-based (get ATR from indicator first)
python3 scripts/position_sizer.py \
  --account-size 100000 --entry 84200 --atr 1850 --atr-multiplier 2.0 \
  --risk-pct 1.0 --side long --contract-value 0.01 --leverage 10 \
  --inst-type swap --output-dir /tmp/position-sizer-reports/

# Kelly budget mode
python3 scripts/position_sizer.py \
  --account-size 100000 --win-rate 0.55 --avg-win 2.5 --avg-loss 1.0 \
  --output-dir /tmp/position-sizer-reports/

# Kelly + leverage + contracts
python3 scripts/position_sizer.py \
  --account-size 100000 --win-rate 0.55 --avg-win 2.5 --avg-loss 1.0 \
  --entry 84200 --stop 80000 --contract-value 0.01 --leverage 10 \
  --inst-type swap --output-dir /tmp/position-sizer-reports/

# With portfolio constraints
python3 scripts/position_sizer.py \
  --account-size 100000 --entry 84200 --stop 80000 --risk-pct 1.0 \
  --side long --contract-value 0.01 --leverage 10 --inst-type swap \
  --inst-id BTC-USDT-SWAP \
  --max-position-pct 10 --max-contracts 50 \
  --output-dir /tmp/position-sizer-reports/

# Large position with custom MMR tier and fee rate
python3 scripts/position_sizer.py \
  --account-size 1000000 --entry 84200 --stop 80000 --risk-pct 1.0 \
  --side long --contract-value 0.01 --leverage 10 --inst-type swap \
  --mmr 0.01 --fee-rate 0.0003 \
  --output-dir /tmp/position-sizer-reports/
```

**Key parameters for advanced users:**
- `--mmr`: Maintenance margin rate (default 0.004 = 0.4%, Tier 1). Large positions have higher MMR — check OKX tiered margin rules and adjust accordingly.
- `--fee-rate`: Taker fee rate (default 0.0005 = 0.05%, VIP0). VIP users with lower fees should adjust for more accurate liquidation estimates.
- `--inst-id`: Instrument ID (e.g., BTC-USDT-SWAP) — included in reports for clarity.
- `--lot-size`: Spot minimum order precision (lotSz from `okx market instruments`). Ensures output units are precision-aligned for OKX spot orders. Not needed for swap (swap always outputs integer contracts).

Note: Use `--output-dir /tmp/position-sizer-reports/` (absolute path) to avoid working directory issues.

**Fallback — inline calculation** (if Python unavailable):

Fixed Fractional (swap):
```
risk_per_contract = abs(entry - stop) * ctVal
dollar_risk       = account_size * risk_pct / 100
contracts         = floor(dollar_risk / risk_per_contract)
```

Fixed Fractional (spot):
```
risk_per_unit = abs(entry - stop)
dollar_risk   = account_size * risk_pct / 100
units         = dollar_risk / risk_per_unit
```

ATR-Based:
```
stop_distance = atr * multiplier
stop (long)   = entry - stop_distance
stop (short)  = entry + stop_distance
risk_per_contract = stop_distance * ctVal
contracts     = floor(dollar_risk / risk_per_contract)
```

Kelly:
```
R              = avg_win / avg_loss
kelly_pct      = win_rate - (1 - win_rate) / R   [floor at 0]
half_kelly_pct = kelly_pct / 2
# With leverage: effective = half_kelly_pct / leverage
```

### Step 4: Multi-Scenario Comparison

If no single method is specified, run all applicable methods and present a comparison:

| Method | Contracts | Notional | Margin | Dollar Risk | Risk % |
|--------|-----------|----------|--------|-------------|--------|
| Fixed 1% @ 5x | — | — | — | — | — |
| Fixed 1% @ 10x | — | — | — | — | — |
| Fixed 1% @ 20x | — | — | — | — | — |
| ATR 1.5x @ 10x | — | — | — | — | — |
| ATR 2.0x @ 10x | — | — | — | — | — |
| ATR 3.0x @ 10x | — | — | — | — | — |

### Step 5: Apply Portfolio Constraints

Check (tightest constraint determines final size):

1. Max single position % of account (`--max-position-pct`)
2. Total portfolio heat (sum of all open risks <= 6-8%)
3. Exchange max orderable size:
   ```bash
   okx --profile demo account max-size --instId BTC-USDT-SWAP --tdMode cross
   ```
   Extract `maxBuy` (long) or `maxSell` (short), pass as `--max-contracts`.
4. Available margin check:
   ```bash
   okx --profile demo account max-avail-size --instId BTC-USDT-SWAP --tdMode cross
   ```

### Step 5.5: Funding Rate Check (Perpetual Swap, Optional)

For positions expected to be held multiple days:
```bash
okx --profile demo market funding-rate BTC-USDT-SWAP
```
```
daily_cost  = notional * funding_rate * 3  (OKX settles 3x/day)
weekly_cost = daily_cost * 7
```
Present as a cost consideration alongside the position sizing result.

### Step 6: Present Result and OKX Handoff

Present the recommendation:

```
Position Sizing Result
──────────────────────
Instrument:      BTC-USDT-SWAP
Side:            Long
Method:          Fixed Fractional 1%
Entry:           $84,200
Stop:            $80,000
Contracts:       23        ← directly usable as --sz
Leverage:        10x
Notional:        $19,366
Margin Required: $1,936.60
Est. Liquidation: $76,123 (isolated, mmr=0.4%, fee=0.05%)
Dollar Risk:     $966.00 (0.97%)
Binding:         none

Risk Warnings:
  - Liquidation price is an isolated margin estimate (Tier 1 small position)
  - Cross margin mode: liquidation price fluctuates with all positions' PnL
  - Large positions have higher tiered MMR — actual liquidation is closer
```

**If stop is beyond liquidation:**
```
CRITICAL: Stop $70,000 is beyond liquidation $76,123!
Position will be liquidated before stop triggers.
Action: Reduce leverage / tighten stop-loss / use cross margin mode
```

**If user wants to place the trade**, present confirmation then execute:

```
Confirm trade:
  Instrument: BTC-USDT-SWAP
  Side: buy (long)
  Size: 23 contracts
  Order type: limit @ $84,200
  Stop-loss: $80,000
  Margin mode: cross
  Profile: demo

Reply YES to proceed, or adjust parameters.
```

On confirmation:

**Step A — Verify/set leverage (swap only):**
```bash
# Check current leverage
okx --profile demo swap get-leverage --instId BTC-USDT-SWAP --mgnMode cross
# Set to match position sizer calculation (if different)
okx --profile demo swap leverage --instId BTC-USDT-SWAP --lever 10 --mgnMode cross
```

**Step B — Place order:**
```bash
# Long swap (hedge mode — uses --posSide)
okx --profile demo swap place --instId BTC-USDT-SWAP \
  --side buy --ordType limit --sz 23 --px 84200 \
  --tdMode cross --posSide long

# Short swap (hedge mode)
okx --profile demo swap place --instId BTC-USDT-SWAP \
  --side sell --ordType limit --sz 23 --px 84200 \
  --tdMode cross --posSide short

# Spot (long only)
okx --profile demo spot place --instId BTC-USDT \
  --side buy --ordType limit --sz 0.238 --px 84200
```

> **Position mode note:** `--posSide long/short` is required in **hedge mode** (long_short_mode). In **net mode**, omit `--posSide` entirely. Check current mode: `okx --profile demo account config` (look for `posMode` field).

If OKX Trade Kit is not installed:
```
OKX Trade Kit is not detected. To place this trade:

  npm install -g @okx_ai/okx-trade-cli
  okx config init

Details: https://github.com/okx/agent-trade-kit
```

## Key Principles

1. **Survival first**: Sizing is about surviving losing streaks, not maximizing winners
2. **1% rule**: Default to 1% risk per trade; never exceed 2% without strong reason
3. **Round down**: Always floor to whole contracts (swap) or precision-aligned units (spot)
4. **Strictest constraint wins**: Tightest limit determines final size
5. **Half Kelly only**: Never use full Kelly; half Kelly captures ~75% of growth
6. **Kelly + leverage**: Divide effective Kelly by leverage multiplier to avoid over-exposure
7. **Portfolio heat**: Total open risk must not exceed 6-8% of account equity
8. **Asymmetry of losses**: A 50% loss requires 100% gain to recover — size accordingly
9. **Verify liquidation**: Always confirm liquidation price is beyond stop-loss before trading
10. **Cross margin caveat**: Liquidation price fluctuates with all positions' PnL — recheck after opening
11. **MMR tiers**: Large positions have higher maintenance margin — liquidation is closer than Tier 1 estimates

## Limitations

- **USDT-margined only**: Liquidation formula and sizing logic are designed for USDT-margined contracts (e.g., BTC-USDT-SWAP). Coin-margined contracts (e.g., BTC-USD-SWAP) use different formulas — do not use this tool for coin-margined positions.
- **Spot is long-only**: OKX spot trading does not support short selling. Use perpetual swap for short positions.
- **Isolated margin liquidation only**: The script estimates isolated margin liquidation. Cross margin liquidation depends on total account state and cannot be computed locally.

## References

- `references/sizing_methodologies.md`: Detailed guide to all three methods with crypto examples, leverage/liquidation formulas
- `scripts/position_sizer.py`: CLI position sizer for OKX (Python 3.9+, stdlib only)
