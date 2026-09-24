# Output Templates

Use the template that matches the query type. Always end every response with a
**Quick Take** — 1-2 sentences in plain language summarizing what the data
means for a retail trader. Never omit this.

---

## Price Check

Use for: "BTC 多少?" / "how much is ETH?" / any direct price query.

```
## [COIN] Price Summary

| Metric | Value |
|--------|-------|
| Price (CMC) | $XX,XXX.XX |
| OKX Real-time | $XX,XXX.XX (bid $XX,XXX / ask $XX,XXX) |
| 24h Change | +X.XX% |
| 7d / 30d Change | -X.XX% / +X.XX% |
| Market Cap | $X.XXB (#rank) |
| 24h Volume | $X.XXB |
| BTC Dominance | XX.X% |
| Supply | XX.XXM / max (XX.X%) |

Source: CoinMarketCap + OKX (real-time)
```

CMC data may lag 1–3 minutes behind OKX real-time prices. For active trading
decisions, prefer OKX ticker data. For market cap and dominance context, CMC
is authoritative.

---

## Technical Analysis

Use for: "ETH TA" / "BTC RSI" / "give me indicators" / "日线怎么看".

```
## [COIN] Technical Analysis ([Timeframe])

| Indicator | Value | Signal | What It Means |
|-----------|-------|--------|---------------|
| RSI(14) | XX.X | Neutral / Overbought / Oversold | [plain explanation] |
| MACD | DIF: X / DEA: X | Bullish ↗ / Bearish ↘ | Histogram [positive/negative] = [direction] momentum |
| MA(50) | $X,XXX | Price [above/below] | [trend implication] |
| MA(200) | $X,XXX | Price [above/below] ⚠️ | [long-term trend] |
| Bollinger | $X,XXX–$X,XXX | [position] | [volatility signal] |

Support: $X,XXX ([level name]) → $X,XXX ([level name])
Resistance: $X,XXX ([level name]) → $X,XXX ([level name])

Overall: 🟢 Bullish / 🔴 Bearish / 🔶 Neutral — [1-sentence summary]
```

**Timeframe guide:**
- `1Dutc` — daily, default for swing traders
- `4H` — 4-hour, for active/day traders
- `1Wutc` — weekly, for position traders

Always state which timeframe is shown. If the user didn't specify, use daily
and mention it.

---

## Market Overview

Use for: "大盘怎样?" / "how's the market?" / "what's happening in crypto?".

```
## Crypto Market Overview

| Metric | Value | Context |
|--------|-------|---------|
| Total Market Cap | $X.XXT | 24h: +X.XX% |
| Fear & Greed | XX / 100 | [Greed / Neutral / Fear] |
| BTC Dominance | XX.X% | High = risk-off, altcoins underperforming |
| ETH Dominance | XX.X% | |
| BTC RSI (Daily) | XX.X | [signal] |
| 24h Volume | $XX.XB | |
| Derivatives Vol | $XXXB | [leverage activity level] |
| Stablecoin Cap | $XXXB | Dry powder on sidelines |

### Hot Narratives
1. [narrative name] — +X% (7d), $XB market cap
2. [narrative name] — +X% (7d), $XB market cap

### Upcoming Catalysts
- [date] — [event name]
```

Retail context tip: when BTC dominance is rising, altcoins typically
underperform. When stablecoin market cap grows while prices are flat, it
signals potential buying power waiting on the sidelines. Include this
interpretation when relevant.

---

## Coin Comparison

Use for: "BTC vs ETH" / "比较 BTC ETH SOL" / multi-coin queries.

```
## Crypto Comparison: [COIN] vs [COIN] vs [COIN]

| Metric | [COIN] | [COIN] | [COIN] |
|--------|--------|--------|--------|
| Price | $X | $X | $X |
| 24h | +X.X% | +X.X% ✅ | +X.X% |
| 7d | -X.X% | +X.X% ✅ | -X.X% |
| 30d | +X.X% | +X.X% | +X.X% ✅ |
| Market Cap | $XXXB | $XXXB | $XXB |
| 24h Volume | $XX.XB | $XX.XB | $X.XB |
| Vol/MCap | X.X% | X.X% ✅ | X.X% ✅ |
| Dominance | XX.X% | XX.X% | X.X% |

✅ = best performer in row
```

Mark the best performer per row with ✅. Always include a Quick Take
comparing relative strength across the coins.

---

## Derivatives / Funding Rate

Use for: "funding rate?" / "资金费率" / "open interest" / derivatives queries.

```
## [COIN] Derivatives Snapshot (OKX)

| Metric | Value | What It Means |
|--------|-------|---------------|
| Funding Rate (current) | +X.XXXX% | [bias description] |
| Funding Rate (last settled) | -X.XXXX% | [who paid whom] |
| Annualized Rate | ~X.X% APR | Cost of holding long perpetual |
| Open Interest | $XXX.XB | Total contract value outstanding |
| Next Settlement | [datetime UTC] | Funding paid every 8 hours |

Interpretation:
- Positive funding → longs pay shorts (bullish crowd)
- Negative funding → shorts pay longs (bearish crowd)
- Near zero → balanced market, no crowded trade
- High OI + rising price → trend confirmation
- High OI + falling price → potential liquidation cascade ⚠️
```

**Funding rate conversion:** raw rate is per-period (8h). Annualized ≈
raw × 3 × 365. For example, 0.01% per 8h ≈ 10.95% APR. Always show both
the raw rate and annualized for context.

---

## Top Movers

Use for: "什么币涨最多?" / "top gainers" / "what's pumping?".

```
## Top Movers on OKX (24h, >$1M volume)

### 🟢 Gainers
| # | Coin | Price | 24h Change | Volume |
|---|------|-------|------------|--------|
| 1 | XXX-USDT | $X.XX | +XX.X% | $XX.XM |
| 2 | XXX-USDT | $X.XX | +XX.X% | $XX.XM |
...

### 🔴 Losers
| # | Coin | Price | 24h Change | Volume |
|---|------|-------|------------|--------|
| 1 | XXX-USDT | $X.XX | -XX.X% | $XX.XM |
...

Source: OKX real-time spot data
```

CMC trending gainers/losers require a premium plan — if unavailable, OKX
`market_get_tickers` is the primary source and is real-time and free.
