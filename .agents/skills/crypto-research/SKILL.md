---
name: crypto-research
description: >
  Use this skill for in-depth cryptocurrency research and due
  diligence. Triggers when a user wants to deeply understand a
  specific coin or token — its fundamentals, tokenomics, holder
  distribution, team, risks, or whether it's worth investing in. Also
  triggers for comprehensive analysis requests like "due diligence
  report", "deep dive", "DYOR", "is it legit/safe", or any request
  combining multiple research dimensions (e.g., staking yield
  analysis, tokenomics + holder data). Do NOT trigger for simple price
  checks, trade execution, market overviews, or technical indicator
  queries alone.
---

# Crypto Research Skill

Perform comprehensive due diligence on any cryptocurrency by systematically
gathering and analyzing data from multiple sources. Produces a structured
research report with green flags, red flags, and risk assessment.

## Data Sources

**Either one alone produces a useful report; both together give the most
complete picture.**

### Source A: OKX Trade Kit (Real-time Exchange Data)

Provides real-time price, TA indicators (70+ types), funding rates, open
interest, and order book depth.

**Fetch priority — try in this order:**
1. **CLI**: `okx market ticker BTC-USDT` (okx-trade-cli)
2. **MCP**: `market_get_ticker`, `market_get_indicator`, etc.

Check availability by running `okx market ticker BTC-USDT` or calling
`market_get_ticker`. If either works, OKX is available.

**If OKX Trade Kit is not configured:**
```
Install OKX Trade Kit:
  npm install -g @okx_ai/okx-trade-mcp @okx_ai/okx-trade-cli
  okx config init
  okx-trade-mcp setup --client claude-code

Details: https://github.com/okx/agent-trade-kit
```

### Source B: CoinMarketCap (Fundamentals & Sentiment) — Optional

Provides project info, holder analysis, aggregated market data, and news.
**No API key required — use whichever path is available:**

- **MCP mode** (best): Call CMC tools directly if CMC MCP is configured
- **HTTP fallback**: If the user has a CMC API key, use WebFetch:
  ```
  https://pro-api.coinmarketcap.com/v1/cryptocurrency/quotes/latest?symbol={SYMBOL}&CMC_PRO_API_KEY={key}
  https://pro-api.coinmarketcap.com/v1/cryptocurrency/info?symbol={SYMBOL}&CMC_PRO_API_KEY={key}
  ```
- **Web fallback** (default, no key needed): Use WebSearch for basic project
  info and news

**To enable CMC MCP for richer data** (optional):
```json
{
  "mcpServers": {
    "cmc-mcp": {
      "url": "https://mcp.coinmarketcap.com/mcp",
      "headers": { "X-CMC-MCP-API-KEY": "your-api-key" }
    }
  }
}
```
Free API key: https://pro.coinmarketcap.com/login

## Research Modes

**Full Research (default):** All 6 steps. Use when user says "research",
"DYOR", "deep dive", "is it legit", "evaluate".

**Quick Snapshot:** Step 1 then Steps 2 + 3 + 4 in parallel. Skip
fundamentals and news. Use when user says "quick look", "what's happening
with", "give me a snapshot", or when they want a fast answer.

Minimum calls for Quick Snapshot (all in parallel after Step 1):
- `market_get_ticker` for token + BTC
- `market_get_indicator` for RSI, MACD, EMA 200
- `market_get_funding_rate` + `market_get_open_interest`

Use the **Quick Snapshot template** in `references/report-templates.md`.

## Core Principle

Thorough research requires looking at a token from multiple angles. Fetch all
relevant data before forming conclusions. Surface both green flags and red
flags. Never provide investment advice — present data and let the user decide.

## Research Workflow

**Performance tip:** Steps 2, 3, and 4 are independent — call all their tools
in parallel to minimize wait time. Steps 5 and 6 can also run in parallel.

### Step 1: Identify the Token & Resolve Instrument IDs

**Goal:** Map user input to tradeable identifiers.

**OKX instrument ID format:** `{BASE}-USDT` for spot (e.g., `SOL-USDT`),
`{BASE}-USDT-SWAP` for perpetual swaps. If unsure whether a token is listed
on OKX, call `market_get_instruments` with `instType: "SPOT"` and filter by
base currency.

**CMC (if available):** Call `search_cryptos` with the token name/symbol to
get the CMC ID. If multiple results, pick the lowest rank number (highest
market cap); if rank >500, confirm with the user.

### Step 2: Real-time Market Data

**From OKX (primary for real-time — CLI first, MCP fallback):**
- CLI: `okx market ticker {TOKEN}-USDT` / `okx market ticker BTC-USDT`
- MCP: `market_get_ticker(instId="{TOKEN}-USDT")` / `market_get_ticker(instId="BTC-USDT")`
- → price, 24h high/low, volume, bid/ask spread; BTC for relative performance

**From CMC (primary for aggregated/historical):**
- `get_crypto_quotes_latest` (MCP) or WebFetch CMC API → market cap, rank,
  circulating supply, 7d/30d/90d/1y price changes

**Interpretation:**
- 24h change: `(last - open24h) / open24h * 100`
- Bid-ask spread: `(askPx - bidPx) / last * 100` — tight (<0.05%) = good
  liquidity
- Compare token 24h vs BTC 24h → outperforming or underperforming?
- Volume/MCap ratio: >10% = healthy; <1% = concern

### Step 3: Technical Analysis

**From OKX (primary — CLI first, MCP fallback; use `bar: "1Dutc"` for daily):**

| Indicator | Call | Interpretation |
|-----------|------|----------------|
| RSI | `indicator: "rsi"` | <30 oversold, 30-70 neutral, >70 overbought |
| MACD | `indicator: "macd"` | DIF > DEA = bullish; histogram expanding = momentum |
| 200d EMA | `indicator: "ema", params: [200]` | Above = bullish; >20% below = broken trend |
| Bollinger Bands | `indicator: "bb"` | Near lower = support zone; near upper = resistance |
| Supertrend | `indicator: "supertrend"` | UP = bullish; DOWN = bearish; flip = trend change |

**Optional deeper TA:**
- `indicator: "kdj"` — J <0 = extreme oversold; J >100 = extreme overbought
- `indicator: "ema", params: [7, 30]` — 7d crossing above 30d = bullish
- `indicator: "ma", params: [50, 200]` — golden/death cross

**Multi-timeframe:** Default `1Dutc` for swing traders; also check `4H` for
active traders or `1Wutc` for position traders. State the timeframe used.

**From CMC (if available):**
- `get_crypto_technical_analysis` → Fibonacci levels, pivot points
- If CMC TA is unavailable, OKX indicators (RSI/MACD/EMA/BB/Supertrend) are
  sufficient for a complete technical assessment.

### Step 4: Derivatives & Sentiment Data

**From OKX (primary — CLI first, MCP fallback):**

| Data | CLI | MCP | Interpretation |
|------|-----|-----|----------------|
| Funding Rate | `okx market funding-rate {TOKEN}-USDT-SWAP` | `market_get_funding_rate(instId="{TOKEN}-USDT-SWAP")` | See thresholds below |
| Open Interest | `okx market open-interest --instType SWAP --instId {TOKEN}-USDT-SWAP` | `market_get_open_interest(instType="SWAP")` | OI/MCap >50% = heavy derivatives. Rising OI + rising price = trend confirmation; rising OI + falling price = liquidation risk |
| Order Book | `okx market orderbook {TOKEN}-USDT` | `market_get_orderbook(instId="{TOKEN}-USDT", sz=20)` | >60/40 bid/ask imbalance = directional bias |

**Not all tokens have perpetual swaps.** If the funding rate call returns
an error, skip derivatives. Note in the report: "No perpetual swap available
— this itself signals lower institutional interest."

**Funding rate thresholds** (8h settlement equivalent):

| Range | Meaning | Signal |
|-------|---------|--------|
| -0.001% to +0.001% | Near zero | Balanced, no extreme bias |
| +0.001% to +0.01% | Mildly positive | Slight bullish bias, normal |
| +0.01% to +0.05% | Elevated positive | Longs paying premium, caution |
| > +0.05% | Extreme positive | Overleveraged longs, high reversal risk |
| -0.01% to -0.05% | Elevated negative | Shorts dominant, squeeze risk |
| < -0.05% | Extreme negative | Panic/heavy shorting, potential bounce |

Check `fundingTime` and `prevFundingTime` to determine the period. For 3h
settlement tokens, multiply rate × (8/3) before comparing to 8h thresholds.
Annualized rate ≈ raw rate × 3 × 365.

### Step 5: Fundamentals, Project Info & News

**Project fundamentals (CMC primary, WebSearch fallback):**
- `get_crypto_info` → description, category, launch date, website, tags
- `get_crypto_metrics` → holder distribution, whale concentration, behavior
- For deeper technology/use-case questions: `search_crypto_info` (CMC MCP)
  or WebSearch for "{token name} cryptocurrency project overview"

**News & sentiment (CMC primary, WebSearch fallback):**
- `get_crypto_latest_news` (limit 5-10) → recent headlines and sentiment
- WebSearch fallback: "{token name} crypto news this week"

Read `references/analysis-framework.md` before writing the Fundamentals,
Tokenomics, and Red/Green Flags sections of the report.

### Step 6: Format & Present the Report

Read `references/report-templates.md` and use the **Full Research Report**
template. Fill every section with actual data — never leave template
placeholders in the output.

## Quick Interpretation Cheat Sheet

Translate raw numbers into plain language. Always include the "so what."

| Metric | Raw Data | What to Say |
|--------|----------|-------------|
| RSI 25 | Oversold | "RSI at 25 — deeply oversold, potential bounce zone but don't catch falling knives" |
| RSI 55 | Neutral | "RSI at 55 — neutral territory, no extreme signal" |
| RSI 78 | Overbought | "RSI at 78 — overbought, momentum could exhaust soon" |
| Price 5% above 200d EMA | Bullish | "Trading 5% above the 200d EMA — long-term uptrend intact" |
| Price 35% below 200d EMA | Broken | "Trading 35% below the 200d EMA ($X) — deeply discounted or structural downtrend" |
| MACD DIF > DEA, histogram growing | Bullish momentum | "MACD bullish with expanding histogram — momentum accelerating" |
| MACD DIF < DEA, histogram shrinking | Bearish slowing | "MACD bearish but histogram contracting — selling pressure fading" |
| Funding +0.003% | Normal | "Funding near zero (+0.003%) — balanced market" |
| Funding +0.08% | Extreme | "Funding at +0.08% — extreme bullish leverage, reversal risk elevated" |
| Spread 0.02% | Tight | "Tight 0.02% spread — excellent liquidity" |
| Spread 0.5% | Wide | "Wide 0.5% spread — thin liquidity, watch for slippage" |
| Vol/MCap 15% | Healthy | "Daily volume is 15% of market cap — healthy turnover" |
| Vol/MCap 0.5% | Low | "Volume only 0.5% of market cap — low interest, hard to exit large positions" |
| 24h vs BTC +3% | Outperforming | "Outperforming BTC by 3% today — capital rotating in" |
| 24h vs BTC -5% | Underperforming | "Underperforming BTC by 5% today — relative weakness" |

## Handling Tool Failures

Always complete the report with available data. Never abandon research because
one source is down. Mark unavailable sections with a note explaining what's
missing and how to get it.

| Data Need | Source 1 | Source 2 | Source 3 |
|-----------|----------|----------|----------|
| Price & volume | OKX `market_get_ticker` | CMC `get_crypto_quotes_latest` | WebFetch CMC API |
| Technical analysis | OKX `market_get_indicator` | CMC `get_crypto_technical_analysis` | Note "TA unavailable" |
| Funding rate & OI | OKX `market_get_funding_rate` / `market_get_open_interest` | — | Note "Derivatives unavailable" |
| Order book | OKX `market_get_orderbook` | — | Note "Order book unavailable" |
| Project fundamentals | CMC `get_crypto_info` | WebFetch CMC API | WebSearch |
| Holder analysis | CMC `get_crypto_metrics` | — | Note "Holder data unavailable" |
| News & sentiment | CMC `get_crypto_latest_news` | WebSearch | Note "News unavailable" |
| Token identification | CMC `search_cryptos` | OKX `market_get_instruments` | Ask user for symbol |
