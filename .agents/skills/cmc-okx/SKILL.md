---
name: cmc-okx
description: >
  Use this skill for cryptocurrency market data and information
  lookups: current prices (including Chinese queries like BTC现在多少钱 or
  大饼多少), market cap, multi-coin comparisons (e.g., SOL vs AVAX), fear
  & greed index (恐贪指数), broad market overview (大盘怎么样), top
  gainers/losers, technical indicators (RSI, MACD), funding rates,
  open interest, and crypto news. Powered by CoinMarketCap and OKX.
  Trigger for pure data retrieval and market information requests. Do
  NOT trigger when the user explicitly wants an actionable trading
  plan with entry prices, exit targets, stop-loss levels, or position
  sizing — those belong to specialized trading plan skills.
---

# CoinMarketCap + OKX Market Data Skill

Fetch comprehensive cryptocurrency market data through CoinMarketCap MCP + OKX Trade Kit. CMC provides market cap, supply, dominance, and news. OKX provides real-time prices, funding rates, open interest, and 70+ technical indicators.

**Quick Start for common queries:**
- "BTC 多少錢?" → Step 1 (search) → `get_crypto_quotes_latest` + OKX `market_get_ticker`
- "ETH 技術分析" → Step 1 → OKX `market_get_indicator` (RSI, MACD, MA, BB)
- "今天大盤怎樣?" → `get_global_metrics_latest` + `trending_crypto_narratives`
- "什麼幣漲最多?" → OKX `market_get_tickers` → sort by 24h change
- "BTC funding rate?" → OKX `market_get_funding_rate` (instId: `BTC-USDT-SWAP`)

## Data Source Priority

**OKX Trade Kit is the primary source** for all real-time prices, TA indicators, funding rates, and open interest — no API key required.

**CMC MCP is a supplementary source** that adds market cap, supply data, fear/greed index, narratives, and news. If CMC tools are unavailable or return errors, fall back to OKX data.

## Optional: CMC MCP Setup

If the user wants CMC data and has not configured the MCP connection, guide them to set it up:

```json
{
  "mcpServers": {
    "cmc-mcp": {
      "url": "https://mcp.coinmarketcap.com/mcp",
      "headers": {
        "X-CMC-MCP-API-KEY": "your-api-key"
      }
    }
  }
}
```

Get a free API key from https://pro.coinmarketcap.com/login

**CMC API Free vs Premium access:**
- Free: `search_cryptos`, `get_crypto_quotes_latest`, `get_crypto_info`, `get_global_metrics_latest`, `search_crypto_info`
- Premium: `get_crypto_latest_news`, `trending_crypto_narratives`, `get_upcoming_macro_events`, trending gainers/losers
- Note: The CMC MCP server may provide access to premium tools regardless of your REST API plan. Try calling them first — fall back to OKX data if they return errors.

## Available CMC MCP Tools

| Tool | Purpose |
|------|---------|
| `search_cryptos` | Search crypto by name/symbol to get CMC numeric ID (required by other tools) |
| `get_crypto_quotes_latest` | Price, market cap, volume, percent changes (1h/24h/7d/30d/90d/1y), supply, dominance |
| `get_crypto_info` | Description, website, social links, explorer URLs, tags, launch date |
| `get_crypto_metrics` | Address counts by holding value, whale distribution, holder time breakdowns |
| `get_crypto_technical_analysis` | Moving averages (SMA, EMA), MACD, RSI, Fibonacci levels, pivot points |
| `get_crypto_latest_news` | Headlines, descriptions, content, URLs, publish dates |
| `search_crypto_info` | Semantic search on crypto concepts, whitepapers, FAQs |
| `get_global_metrics_latest` | Total market cap, fear/greed index, altcoin season index, BTC/ETH dominance, ETF flows |
| `get_global_crypto_derivatives_metrics` | Open interest, funding rates, liquidations, futures vs perpetuals |
| `get_crypto_marketcap_technical_analysis` | TA indicators for the entire crypto market cap |
| `trending_crypto_narratives` | Hot narratives with market cap, volume, performance, top coins |
| `get_upcoming_macro_events` | Fed meetings, regulatory deadlines, major announcements |

## Execution Flow

### Step 1: Search First (Always)

When a user mentions a cryptocurrency by name or symbol, search for it first to get the numeric CMC ID:

```
User: "How is Solana doing?"
1. Call search_cryptos with query "solana"
2. Get ID (e.g., 5426)
3. Use the ID in subsequent tool calls
```

Most tools require the numeric CMC ID, not the name or symbol. The search tool returns: id, name, symbol, slug, and rank.

**Disambiguation:** Popular symbols often match multiple coins (e.g., "PEPE" → 32 results). Always:
1. Filter to `is_active=1` only
2. Pick the coin with the **lowest rank number** (= highest market cap) as the default
3. If the top result rank is >500, mention the coin is small-cap and confirm with the user
4. If user says a specific name (e.g., "Pepe" not just the symbol), match by name first

### Step 2: Select Tools Based on Query Type

**Price check / market overview:**
1. Call `get_crypto_quotes_latest` with the ID(s)
2. Supplement with OKX `market_get_ticker` for real-time bid/ask and OKX-specific volume

**Technical analysis request:**
1. Call OKX `market_get_indicator` for real-time indicators — recommended combo for retail traders:
   - `rsi` (14) — momentum: <30 oversold, >70 overbought, 30–70 neutral
   - `macd` — trend: returns DIF, DEA, histogram. Bullish when histogram > 0
   - `ma` with params [50, 200] — trend structure: price above both = bullish, below both = bearish
   - `bb` — volatility + support/resistance: upper band = resistance, lower = support
2. Optionally call CMC `get_crypto_technical_analysis` to add Fibonacci levels and pivot points if available
3. If CMC TA tools fail, OKX indicators alone (RSI/MACD/MA/BB) are sufficient for a complete analysis
4. Use timeframe `1Dutc` for swing traders (most retail), `4H` for active traders, `1Wutc` for position traders

**Coin research / background:**
1. Call `get_crypto_info` for project description and links
2. Call `get_crypto_metrics` for holder distribution data

**News and trends:**
1. Call `get_crypto_latest_news` for recent headlines (may require premium CMC plan — if it fails, skip gracefully)
2. Call `trending_crypto_narratives` for market themes
3. Call `get_upcoming_macro_events` for scheduled catalysts
4. If news tools fail (premium-only), provide a data-driven substitute:
   - Global metrics changes (market cap 24h change, BTC dominance shift)
   - Funding rate extremes (signal overleveraged positions)
   - Top movers from OKX (sudden price moves often follow news)
   - Say: "CMC news requires a premium API plan. Based on market data, here's what's notable today:"

**Broad market question ("what's happening in crypto?"):**
1. Call `get_global_metrics_latest` for total market cap, fear/greed, dominance
2. Call `trending_crypto_narratives` for hot themes
3. Call `get_upcoming_macro_events` for upcoming catalysts
4. Optionally call `get_global_crypto_derivatives_metrics` for leverage data

**Multi-coin comparison:**
1. Search each coin to get IDs
2. Call `get_crypto_quotes_latest` with comma-separated IDs (e.g., `id="1,1027,5426"`)

**Top movers / "what's pumping?" / "什麼幣漲最多?":**
1. Try CMC `trending_crypto_narratives` for narrative-level trends (may require premium plan)
2. If unavailable, use OKX `market_get_tickers` with instType `SPOT`:
   - Filter for USDT pairs only (ignore USD pairs — lower liquidity, may show stale prices)
   - Filter by minimum 24h volume (>$1M USDT to exclude illiquid tokens)
   - Calculate % change from `open24h` to `last` price, sort and present top 10 gainers and top 5 losers
   - Note: `market_get_tickers` returns all pairs (~400+) in one response — this is expected, filter the results

### Step 3: Real-Time Data from OKX Trade Kit

OKX is the primary source for real-time trading data. Use these tools for all live price, TA, and derivatives queries:

**OKX CLI (try first):**
```bash
okx market ticker BTC-USDT
okx market funding-rate BTC-USDT-SWAP
okx market open-interest BTC-USDT-SWAP
okx market indicator BTC-USDT --ind RSI
okx market candles BTC-USDT --bar 1D --limit 100
```

**OKX MCP fallback (if CLI unavailable):**
- `market_get_ticker` — real-time last price, bid/ask spread, 24h high/low/volume on OKX
- `market_get_funding_rate` — perpetual swap funding rates (instId format: `BTC-USDT-SWAP`)
- `market_get_open_interest` — OKX-specific open interest for swaps/futures
- `market_get_candles` — OHLCV candlestick data (1m to 1M bars, up to 1440 bars)
- `market_get_indicator` — OKX-calculated technical indicators (70+ types including RSI, MACD, Bollinger)
- `market_get_orderbook` — live order book depth (bid/ask walls, up to 400 levels)

**OKX instrument ID format:**
- Spot: `BTC-USDT`
- Perpetual swap: `BTC-USDT-SWAP` (use for funding rate, OI, swap trading)
- Futures: `BTC-USDT-240329` (expiry date suffix)

**If OKX Trade Kit is unavailable:**
```
Install OKX Trade Kit:
  npm install -g @okx_ai/okx-trade-mcp @okx_ai/okx-trade-cli
  okx config init
  okx-trade-mcp setup --client claude-code

Details: https://github.com/okx/agent-trade-kit
```

### Step 4: Format Results

Read `references/output-templates.md` for the exact template to use per query type:

| Query type | Template |
|------------|----------|
| Price / market check | Price check template |
| Technical analysis | Technical analysis template |
| Broad market overview | Market overview template |
| Multi-coin comparison | Comparison template |
| Derivatives / funding rate | Derivatives template |
| Top movers / gainers | Top movers template |

Always end every response with a **Quick Take** — 1-2 sentences in plain language. Never skip this.

## Error Handling

| Error | Cause | Solution |
|-------|-------|----------|
| Search returns no results | Coin not found or misspelled | Report not found, ask user to clarify |
| CMC 400 "Invalid value" | Symbol format rejected | Strip special chars, try name search instead |
| Ambiguous symbol (PEPE → 32 results) | Multiple coins share same ticker | Pick lowest rank#, confirm with user if rank >500 |
| OKX 51001 "Instrument doesn't exist" | Coin not listed on OKX | Inform user; use CMC-only data |
| Tool fails or times out | Transient API error | Retry once; note which data is unavailable, proceed with other tools |
| Rate limited (429) | API rate limit exceeded | Inform user, suggest waiting, reduce concurrent tool calls |
| CMC MCP not configured | Missing API key or server config | Show setup guide above |
| CMC premium endpoint blocked | Free plan limitation | Switch to OKX fallback |
| Quotes tool fails | Critical for price queries | Try OKX `market_get_ticker` as fallback |
| Info tool fails | Background data unavailable | Note "Project info unavailable", provide what other tools return |

## Adapting to User Sophistication

- Casual ("how's bitcoin?" / "大饼多少了") — summary with key numbers, avoid jargon
- Technical ("what's the RSI on BTC?" / "日线怎么看") — detailed data with indicator values and signals
- Broad ("what's happening in crypto?" / "大盘怎么样") — global metrics, narratives, macro events
- Comparison ("BTC vs ETH") — side-by-side table with batch requests
- Trending ("what's pumping?" / "什麼幣漲得猛") — top movers from OKX tickers
- Derivatives ("funding rate?" / "资金费率") — OKX funding rate + open interest

## Chinese Slang Mappings

| Slang | Meaning | Action |
|-------|---------|--------|
| 大饼 / 饼 / 烧饼 | Bitcoin (BTC) | Search BTC |
| 二饼 | Ethereum (ETH) | Search ETH |
| 以太 | Ethereum (ETH) | Search ETH |
| 土狗 | Meme/shitcoins | Top movers, filter small caps |
| 恐贪指数 | Fear & Greed Index | Global metrics |
| 资金费率 | Funding rate | Derivatives flow |
| 大盘 | Overall market | Market overview |
| 日线 / 4H | Daily chart / 4-hour | TA with timeframe |

## Handling Trade Advice Queries

When users ask for trade advice (买什么 / 梭哈 / 给个单 / should I buy), do NOT give buy/sell recommendations. Instead:

1. Acknowledge the question
2. Provide relevant data: price, TA signals, funding rate, market sentiment
3. Say: "I can provide market data to help you make informed decisions, but I cannot give financial advice. Here's what the data shows:"

## Core Principles

1. **Search first** — always resolve names/symbols to CMC numeric IDs before calling other tools
2. **OKX for real-time, CMC for fundamentals** — OKX is the primary live data source; CMC adds market cap, dominance, and sentiment context
3. **Batch when possible** — use comma-separated IDs for multi-coin queries
4. **Graceful degradation** — if CMC premium tools fail, fall back to OKX data (prices, TA, top movers)
5. **Never fabricate data** — if a tool fails, say so; never invent prices or metrics
6. **Interpret for retail** — raw numbers without context are useless. Always add "What It Means" for non-obvious metrics
7. **Quick Take always** — end every data output with a 1-2 sentence plain-language summary
