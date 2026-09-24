---
name: alpha-vantage
description: >-
  Fetch crypto and financial market data from Alpha Vantage API including daily OHLCV, intraday candles, exchange rates, and 50+ technical indicators (RSI, MACD, SMA, EMA, BBANDS). Trigger when user asks for Alpha Vantage data, crypto historical prices, BTC or ETH daily chart, technical indicator calculation, or cross-market analysis combining crypto with equities, forex, or economic indicators.
---

# Alpha Vantage — Crypto and Financial Market Data

Access 20+ years of financial data from Alpha Vantage: crypto prices, technical indicators, equities, forex, commodities, and economic indicators. Focused on crypto market analysis with cross-asset context.

## Prerequisites

### Step 1: API Key Setup (Required)

```bash
export ALPHAVANTAGE_API_KEY="your_key_here"
```

Get a free key at https://www.alphavantage.co/support/#api-key (premium plans available for higher rate limits).

Check if key is configured:
```bash
echo $ALPHAVANTAGE_API_KEY
```

If not set, guide the user to obtain and set the key before proceeding.

### Step 2: Install Dependencies

```bash
pip install requests
```

## Execution Flow

### Step 1: Identify the Request

Parse the user's query to determine:
- **Asset**: Which crypto or financial instrument (BTC, ETH, AAPL, EUR/USD, etc.)
- **Data type**: Price data, technical indicator, fundamentals, news, or economic indicator
- **Timeframe**: Intraday (1min-60min), daily, weekly, or monthly
- **Period**: Compact (last 100 points) or full (20+ years)

### Step 2: Fetch Data via Alpha Vantage API

All requests use this base pattern:

```python
import requests, os

API_KEY = os.environ.get("ALPHAVANTAGE_API_KEY")
BASE_URL = "https://www.alphavantage.co/query"

def av_get(function, **params):
    if not API_KEY:
        raise ValueError("ALPHAVANTAGE_API_KEY not set. Get a free key at https://www.alphavantage.co/support/#api-key")
    response = requests.get(BASE_URL, params={"function": function, "apikey": API_KEY, **params})
    data = response.json()
    if "Error Message" in data:
        raise ValueError(f"API Error: {data['Error Message']}")
    if "Note" in data:
        print(f"Rate limit warning: {data['Note']}")
    return data
```

**Crypto-specific functions:**

| Function | Use Case | Example |
|----------|----------|---------|
| `DIGITAL_CURRENCY_DAILY` | BTC/ETH daily OHLCV | `av_get("DIGITAL_CURRENCY_DAILY", symbol="BTC", market="USD")` |
| `CRYPTO_INTRADAY` | Intraday crypto candles | `av_get("CRYPTO_INTRADAY", symbol="ETH", market="USD", interval="60min")` |
| `CURRENCY_EXCHANGE_RATE` | Real-time exchange rate | `av_get("CURRENCY_EXCHANGE_RATE", from_currency="BTC", to_currency="USD")` |

**Technical indicators (all work with crypto symbols):**

| Function | Use Case | Example |
|----------|----------|---------|
| `RSI` | Relative Strength Index | `av_get("RSI", symbol="BTC", interval="daily", time_period=14, series_type="close")` |
| `MACD` | MACD | `av_get("MACD", symbol="BTC", interval="daily", series_type="close")` |
| `SMA` | Simple Moving Average | `av_get("SMA", symbol="BTC", interval="daily", time_period=200, series_type="close")` |
| `EMA` | Exponential Moving Average | `av_get("EMA", symbol="BTC", interval="daily", time_period=50, series_type="close")` |
| `BBANDS` | Bollinger Bands | `av_get("BBANDS", symbol="BTC", interval="daily", time_period=20, series_type="close")` |
| `STOCH` | Stochastic Oscillator | `av_get("STOCH", symbol="BTC", interval="daily")` |
| `ADX` | Average Directional Index | `av_get("ADX", symbol="BTC", interval="daily", time_period=14)` |
| `ATR` | Average True Range | `av_get("ATR", symbol="BTC", interval="daily", time_period=14)` |
| `OBV` | On-Balance Volume | `av_get("OBV", symbol="BTC", interval="daily")` |
| `VWAP` | Volume-Weighted Avg Price | `av_get("VWAP", symbol="BTC", interval="60min")` |

**Cross-asset context (equities, forex, macro):**

| Category | Key Functions |
|----------|--------------|
| Equities | GLOBAL_QUOTE, TIME_SERIES_DAILY, TIME_SERIES_INTRADAY |
| Fundamentals | OVERVIEW, INCOME_STATEMENT, BALANCE_SHEET, EARNINGS |
| Forex | CURRENCY_EXCHANGE_RATE, FX_DAILY |
| Commodities | GOLD, BRENT, NATURAL_GAS, ALL_COMMODITIES |
| Economic | REAL_GDP, TREASURY_YIELD, FEDERAL_FUNDS_RATE, CPI, INFLATION, UNEMPLOYMENT |
| News/Sentiment | NEWS_SENTIMENT, TOP_GAINERS_LOSERS |

### Step 3: Supplement with OKX Trade Kit (When Available)

For real-time exchange data, supplement Alpha Vantage with OKX Trade Kit:

**OKX MCP tools (primary for real-time OKX data):**
- Call OKX Trade MCP `market.ticker` for real-time bid/ask on OKX
- Call OKX Trade MCP `market.candles` for OKX candlestick data
- Call OKX Trade MCP `market.funding-rate` for perpetual swap funding rates
- Call OKX Trade MCP `market.indicator` for OKX-calculated indicators (70+ types)

**OKX CLI fallback:**
```bash
okx market ticker BTC-USDT
okx market candles BTC-USDT --bar 1D --limit 100
okx market indicator BTC-USDT --ind RSI
okx market funding-rate BTC-USDT-SWAP
```

**When to use OKX vs Alpha Vantage:**
- **OKX**: Real-time prices, funding rates, order book, OKX-specific volume
- **Alpha Vantage**: Historical data (20+ years), cross-asset analysis, economic indicators, news sentiment
- **Both together**: Comprehensive analysis combining real-time OKX data with AV historical context

**If OKX Trade Kit is unavailable:**
```
For real-time OKX exchange data, install OKX Trade Kit:

1. Install the packages:
   npm install -g @okx_ai/okx-trade-mcp @okx_ai/okx-trade-cli

2. Configure your API credentials:
   okx config init

3. (For AI agent use) Register the MCP server:
   okx-trade-mcp setup --client claude-code

For details, see: https://github.com/okx/agent-trade-kit
```

### Step 4: Present Results

Format results based on query type:

**Crypto price output:**
```
## BTC/USD Daily Summary (Alpha Vantage)

| Date | Open | High | Low | Close | Volume |
|------|------|------|-----|-------|--------|
| 2024-01-15 | $42,500 | $43,200 | $42,100 | $42,800 | $1.2B |
| 2024-01-14 | $41,800 | $42,600 | $41,500 | $42,500 | $1.1B |

Source: Alpha Vantage DIGITAL_CURRENCY_DAILY
```

**Technical indicator output:**
```
## BTC RSI(14) — Daily

| Date | RSI | Signal |
|------|-----|--------|
| 2024-01-15 | 62.3 | Neutral |
| 2024-01-14 | 58.7 | Neutral |

Current: 62.3 (Neutral — between 30 and 70)
Overbought threshold: 70 | Oversold threshold: 30
```

**Cross-asset context output:**
```
## Market Context

| Asset | Value | Change |
|-------|-------|--------|
| BTC/USD | $42,800 | +2.3% |
| S&P 500 | 4,783 | +0.5% |
| Gold | $2,035 | -0.2% |
| DXY | 102.3 | -0.1% |
| Fed Funds Rate | 5.50% | unchanged |

Correlation note: BTC moving with risk assets today.
```

## Error Handling

| Error | Cause | Solution |
|-------|-------|----------|
| `ALPHAVANTAGE_API_KEY not set` | Missing API key | Guide user to obtain key at alphavantage.co |
| `Error Message` in response | Invalid symbol or function | Check symbol format, verify function name |
| `Note` in response | Rate limit warning | Add 0.5s delay between requests, consider premium |
| HTTP 429 | Rate limit exceeded | Free tier: 25 req/day. Suggest waiting or upgrading |
| Empty time series | Symbol not found or no data | Verify crypto symbol format (use "BTC" not "BTC-USDT") |

## Rate Limits

- Free tier: 25 requests/day
- Premium plans: higher limits, real-time data, intraday access
- Add `time.sleep(0.5)` between batch requests
- For heavy usage, combine Alpha Vantage (historical) with OKX Trade Kit (real-time) to reduce AV API calls

## Common Parameters

| Parameter | Values | Notes |
|-----------|--------|-------|
| `outputsize` | `compact` / `full` | compact = last 100; full = 20+ years |
| `datatype` | `json` / `csv` | Default: json |
| `interval` | `1min`, `5min`, `15min`, `30min`, `60min`, `daily`, `weekly`, `monthly` | Depends on endpoint |
| `series_type` | `close`, `open`, `high`, `low` | For TA indicators |
| `time_period` | Integer (e.g., 14, 50, 200) | Lookback period for TA indicators |
