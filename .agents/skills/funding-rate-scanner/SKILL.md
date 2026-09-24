---
name: funding-rate-scanner
description: >
  Use this skill to discover, scan, or rank perpetual futures funding
  rates on OKX, Binance, or both. Covers: finding coins with negative
  rates where longs earn funding payments, ranking perps by most
  extreme (positive or negative) rates, spotting cross-exchange rate
  discrepancies for arbitrage, and monitoring a watchlist of funding
  rates.

  Trigger when users want to scan for funding opportunities across
  perps, find negative-funding coins on any exchange, identify the
  most extreme funding rates, earn yield from funding payments, or
  compare rates between exchanges.

  Do NOT use for chart analysis, technical indicators (RSI/MACD), spot
  prices, or single-coin market overviews — those belong to
  kline-indicator or cmc-mcp.
---

# Funding Rate Scanner

Scan perpetual futures funding rates to find high-yield arbitrage opportunities. Uses OKX as the primary data source via OKX Trade Kit (CLI first, MCP fallback), with Binance data for cross-exchange comparison via bundled scripts.

## Data Source Roles

- **OKX Trade Kit (CLI/MCP)** — primary source for real-time OKX funding rates, tickers, open interest, and candles
- **scripts/scan.js** — bulk-scans all Binance USDT-margined perps; use for cross-exchange comparison
- **scripts/analyze.js** — single-coin historical funding rate analysis on Binance
- **scripts/monitor.js** — watchlist monitor for specific coins on Binance

## Workflow

### Step 1 — Fetch OKX Funding Rate (CLI First)

**CLI (primary):**
```bash
okx market funding-rate BTC-USDT-SWAP
okx market ticker BTC-USDT-SWAP
```

**MCP fallback (if CLI unavailable):**
- `market_get_funding_rate(instId="BTC-USDT-SWAP")` — current rate and next settlement time
- `market_get_ticker(instId="BTC-USDT-SWAP")` — price, volume, bid/ask

### Step 2 — Scan All OKX Instruments

To scan all USDT-margined perpetual swaps on OKX, fetch each instrument's funding rate via CLI or MCP. Start by listing all SWAP instruments:

```bash
okx market instruments --instType SWAP
```

Then loop through and fetch funding rates for each. Filter to instruments where `|rate| > 0.01%` to surface meaningful opportunities.

### Step 3 — Binance Cross-Exchange Data

For cross-exchange comparison, use the bundled scripts (Node.js required):

```bash
# Scan all Binance perps — returns top negative and positive rates
node ${CLAUDE_SKILL_DIR}/scripts/scan.js

# Detailed history for a specific coin
node ${CLAUDE_SKILL_DIR}/scripts/analyze.js DUSK

# Monitor a specific watchlist
node ${CLAUDE_SKILL_DIR}/scripts/monitor.js DUSK DASH AXS
```

These scripts query Binance public APIs directly — no API key needed.

### Step 4 — Rank and Analyze

For each instrument, compute:

| Field | Formula |
|---|---|
| Funding Rate | Raw rate from exchange |
| Daily Return | rate × 3 (assumes 8h settlement — see note below) |
| Annualized Return | rate × 3 × 365 |
| Leveraged Annual | annualized × leverage |

> **Settlement period note:** The `rate × 3` formula assumes 8-hour settlement (standard for most OKX and Binance perps). Some OKX instruments settle every 4h or 1h — check `fundingTime` and `nextFundingTime` in the API response to confirm, and adjust the multiplier accordingly.

Sort by most negative rate (best long opportunity) or highest positive rate (best short opportunity).

### Step 5 — Cross-Exchange Comparison

When both OKX and Binance rates are available, identify discrepancies:

```
Coin     OKX Rate    Binance Rate   Delta    Opportunity
DUSK     -0.25%      -0.38%         0.13%    Long Binance / Short OKX
AXS      -0.10%      -0.12%         0.02%    Marginal
```

A delta >0.05% between exchanges signals a potential cross-exchange funding arbitrage opportunity. Before acting on it, factor in:
- Transfer and withdrawal fees between exchanges
- Margin requirements on both sides
- Execution risk if rates shift before positions are opened

### Step 6 — Historical Analysis

For deeper analysis on a specific instrument:

**CLI (primary):**
```bash
okx market candles BTC-USDT-SWAP --bar 1H --limit 24
okx market open-interest BTC-USDT-SWAP
```

**MCP fallback:**
- `market_get_candles(instId="BTC-USDT-SWAP", bar="1H", limit=24)`
- `market_get_open_interest(instId="BTC-USDT-SWAP", instType="SWAP")`

For Binance historical funding rate data, use:
```bash
node ${CLAUDE_SKILL_DIR}/scripts/analyze.js COIN
```

### Step 7 — Monitor Watchlist

To track specific instruments at regular intervals:

```bash
# Binance watchlist
node ${CLAUDE_SKILL_DIR}/scripts/monitor.js DUSK DASH AXS

# OKX watchlist via CLI
for inst in DUSK-USDT-SWAP DASH-USDT-SWAP AXS-USDT-SWAP; do
  echo "--- $inst ---"
  okx market funding-rate "$inst"
done
```

## Output Format

```
=== Funding Rate Opportunities (OKX) ===

Rank  Coin    Rate      Annual(5x)   Volume     Next Settlement
1     DUSK    -0.38%    2071%        $30M       03:00 UTC (2h)
2     AXS     -0.12%    655%         $46M       03:00 UTC (2h)
3     DASH    -0.12%    649%         $50M       03:00 UTC (2h)

=== Cross-Exchange Comparison (OKX vs Binance) ===

Coin    OKX       Binance   Delta
DUSK    -0.25%    -0.38%    0.13%  ← arbitrage candidate
AXS     -0.10%    -0.12%    0.02%  marginal
```

## Strategy Notes

**Negative funding rate** means shorts pay longs every settlement period.

1. Open a long position on a coin with a deeply negative funding rate
2. Collect funding payments every 8 hours (or per settlement period)
3. Set a stop-loss to limit directional downside
4. For delta-neutral exposure: hedge with a spot short or inverse position on another exchange

**Risk Warning**: Funding rates change frequently and can flip within hours. High leverage amplifies both gains and losses. Cross-exchange arbitrage carries execution and transfer risk. Always use stop-loss orders. Only risk capital you can afford to lose.

## Error Handling

- If OKX CLI is unavailable, fall back to MCP tools (`market_get_funding_rate`, etc.)
- If MCP tools are unavailable, fall back to Binance scripts for all data
- If a symbol is invalid, report the error and suggest valid OKX SWAP format (e.g., `BTC-USDT-SWAP`)
- If the API times out, retry once, then report the failure with the last known data if available
- If no negative rates are found, report that all rates are positive and suggest setting a monitoring interval
