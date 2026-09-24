---
name: apex-crypto-intelligence
description: >
  Use this skill to compare live crypto prices across exchanges,
  detect price-based arbitrage, or run a multi-exchange market scan.
  The primary trigger: two or more exchange names appear together
  (Binance, OKX, Bybit, KuCoin, Gate.io, MEXC), or the user asks about
  price differences, spreads, or arbitrage between venues. Also invoke
  for: multi-exchange market reports, institutional-grade
  cross-exchange analysis, APEX analysis, Hyper-Council verdicts, or
  trading blueprints. If the query is about how a coin trades
  differently across venues, this is the right skill. Skip for:
  single-exchange data, one-venue price lookups, funding rate queries
  on a single instrument, or tokenomics research.
---

# APEX Crypto Intelligence — Multi-Exchange Trading Analysis

Multi-exchange cryptocurrency analysis that fetches live market data from OKX, CoinGecko, Binance, Bybit, KuCoin, MEXC, and Gate.io, performs cross-exchange arbitrage detection, and delivers AI-powered institutional trading analysis through a Hyper-Council of 5 specialized agents.

> **Note:** The Hyper-Council analysis is powered by `api.neurodoc.app` (GPT-4o), a separate AI service — not Claude. Only aggregated market prices and your query text are sent; exchange API keys never leave your machine.

---

## Prerequisites

### OKX Trade Kit

```bash
npm install -g @okx_ai/okx-trade-mcp @okx_ai/okx-trade-cli
okx config init
okx-trade-mcp setup --client claude-code
```

### Exchange API Keys (all optional)

The skill works without any keys using OKX Trade Kit and CoinGecko free tier. Keys enable authenticated endpoints (higher rate limits) for other exchanges.

| Variable | Exchange |
|----------|----------|
| `BINANCE_API_KEY` / `BINANCE_API_SECRET` | Binance |
| `BYBIT_API_KEY` / `BYBIT_API_SECRET` | Bybit |
| `KUCOIN_API_KEY` / `KUCOIN_API_SECRET` | KuCoin |
| `MEXC_API_KEY` / `MEXC_API_SECRET` | MEXC |
| `GATEIO_API_KEY` / `GATEIO_API_SECRET` | Gate.io |

Configure with **read-only permissions** only. Keys are used locally to fetch data and are never sent to `api.neurodoc.app`.

---

## Data Source Priority

For OKX data, fetch in this order — stop at the first that works:

1. **CLI** (primary): `okx market ticker BTC-USDT`
2. **MCP** (fallback): `market_get_ticker(instId="BTC-USDT")`
3. **REST API** (last resort): direct exchange API calls via `scripts/client.py`

---

## Workflow

### Step 1: Fetch OKX Data

**CLI (try first):**
```bash
okx market ticker {TOKEN}-USDT
okx market candles {TOKEN}-USDT --bar 1D --limit 100
okx market indicator {TOKEN}-USDT --ind RSI
okx market indicator {TOKEN}-USDT --ind MACD
okx market funding-rate {TOKEN}-USDT-SWAP
okx market open-interest --instType SWAP --instId {TOKEN}-USDT-SWAP
okx market orderbook {TOKEN}-USDT
```

**MCP fallback (if CLI unavailable):**
- `market_get_ticker(instId="{TOKEN}-USDT")` — price, bid/ask, 24h stats
- `market_get_candles(instId="{TOKEN}-USDT", bar="1Dutc", limit=100)` — OHLCV
- `market_get_indicator(instId="{TOKEN}-USDT", indicator="rsi", bar="1Dutc")` — RSI
- `market_get_indicator(instId="{TOKEN}-USDT", indicator="macd", bar="1Dutc")` — MACD
- `market_get_funding_rate(instId="{TOKEN}-USDT-SWAP")` — funding rate
- `market_get_open_interest(instId="{TOKEN}-USDT-SWAP", instType="SWAP")` — open interest
- `market_get_orderbook(instId="{TOKEN}-USDT")` — order book depth

### Step 2: Fetch Cross-Exchange Data

Run `scripts/client.py` for multi-exchange price comparison and arbitrage detection. The script fetches from CoinGecko, Binance, Bybit, KuCoin, MEXC, and Gate.io locally, then sends only aggregated prices (no keys) to `api.neurodoc.app`.

```bash
python ${CLAUDE_SKILL_DIR}/scripts/client.py
```

> **Symbol coverage note:** The client currently has hardcoded symbol mappings for BTC, ETH, and SOL. For other coins, cross-exchange comparison falls back to CoinGecko data only.

**To verify the exact payload before sending:**
```bash
python ${CLAUDE_SKILL_DIR}/scripts/client.py  # runs in verification mode by default
```

### Step 3: Send to Hyper-Council API

Choose the appropriate analysis mode:

| Mode | API payload | Use when |
|------|-------------|----------|
| Scanner | `mode="scanner"` | Quick cross-exchange scan |
| Analysis | `mode="analysis"` | Full APEX council report |
| Blueprint | `mode="blueprint"` | Hedge fund-grade trading plan |

API endpoint: `POST https://api.neurodoc.app/aetherlang/execute`

**Example payloads:**

Cross-exchange scan:
```json
{
  "code": "flow CryptoScan {\n  using target \"neuroaether\" version \">=0.3\";\n  input text query;\n  node Scanner: crypto exchanges=\"all\", language=\"en\";\n  output text result from Scanner;\n}",
  "query": "BTC ETH SOL"
}
```

APEX council analysis:
```json
{
  "code": "flow ApexAnalysis {\n  using target \"neuroaether\" version \">=0.3\";\n  input text query;\n  node Apex: crypto mode=\"analysis\", language=\"en\";\n  output text result from Apex;\n}",
  "query": "Full APEX analysis for BTC"
}
```

Trading blueprint:
```json
{
  "code": "flow Blueprint {\n  using target \"neuroaether\" version \">=0.3\";\n  input text query;\n  node Report: crypto mode=\"blueprint\", language=\"en\";\n  output text result from Report;\n}",
  "query": "Generate trading blueprint for BTC"
}
```

Rate limit: 100 req/hour (free tier). If exceeded, wait before retrying.

### Step 4: Present Results

Combine OKX real-time data with the Hyper-Council verdict into a single report covering:
- Cross-exchange price comparison and arbitrage spread
- OKX technical indicators (RSI, MACD, funding rate, OI)
- Council verdicts from MACRO, QUANT, STATS, RISK (Damocles), EXECUTION agents
- Risk assessment and trading blueprint (if blueprint mode)

---

## Hyper-Council Agents

| Agent | Role | Can Veto |
|-------|------|----------|
| MACRO | Global Macro CIO | No |
| QUANT | Head of Quant Research | No |
| STATS | Chief Statistician | No |
| RISK (Damocles) | Chief Risk Officer | **Yes** |
| EXECUTION | Execution Architect | No |

---

## Error Handling

| Error | Cause | Action |
|-------|-------|--------|
| OKX CLI/MCP unavailable | Not installed | Fall back to `scripts/client.py` for OKX REST data |
| Exchange API returns empty | Rate limit or auth error | Use CoinGecko free tier as price baseline |
| `api.neurodoc.app` timeout | Network / server load | Retry once; if still failing, present OKX data only without council analysis |
| Rate limit hit (100 req/hr) | Free tier exceeded | Inform user, wait before retrying |

---

*Analysis is for informational purposes only. Not financial advice.*
