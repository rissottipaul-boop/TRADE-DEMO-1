---
name: btc-altcoin-market-pulse
description: "Activated when users say 'BTC analysis', 'Bitcoin market update', 
  'altcoin pulse', 'altcoin season check', 'what's pumping today', 'top altcoins', 
  'crypto market briefing', 'BTC dominance', 'which alts are moving', 
  'market overview', 'daily crypto update', 'altcoin movers', 
  'is it altseason', 'Bitcoin funding rate', 'crypto news today', 
  'what coins are up', 'market sentiment check', or 'BTC vs alts'. 
  Fetches live OKX market data across Bitcoin and major altcoins, analyzes 
  price momentum, funding rates, open interest, RSI, MACD, and BTC dominance 
  signals to produce a structured BTC + Altcoin Market Pulse report."
license: Apache-2.0
metadata:
  author: muendocolo
  version: "1.0.0"
  moltbot:
    requires:
      bins: ["okx"]
---

# BTC & Altcoin Market Pulse

A structured market intelligence skill that delivers a real-time snapshot of
Bitcoin and the broader altcoin market — covering price momentum, RSI/MACD
technical signals, funding sentiment, open interest shifts, and BTC dominance
to help traders quickly assess where the market stands.

---

## Trigger Phrases

This skill activates on phrases like:
- "BTC analysis", "Bitcoin update", "Bitcoin market check"
- "altcoin pulse", "altcoin season", "is it altseason"
- "what's pumping today", "top altcoins", "which alts are moving"
- "market overview", "daily crypto update", "market sentiment"
- "BTC dominance", "BTC vs alts", "crypto news today"

---

## Tracked Assets

**Bitcoin:**
`BTC-USDT` (spot) · `BTC-USDT-SWAP` (perpetual)

**Major Altcoins:**
`ETH-USDT` · `SOL-USDT` · `BNB-USDT` · `XRP-USDT` · `DOGE-USDT` ·
`ADA-USDT` · `AVAX-USDT` · `LINK-USDT` · `DOT-USDT` · `TON-USDT`

---

## Workflow

### Step 1 — BTC Price & 24h Momentum
```
okx market ticker BTC-USDT
okx market candles BTC-USDT --bar 1H --limit 24
```
Extract: current price, 24h change %, 24h high/low range, volume.

---

### Step 2 — BTC Technical Indicators (RSI + MACD)
```
okx market indicators BTC-USDT --bar 1H --indicator RSI
okx market indicators BTC-USDT --bar 1H --indicator MACD
okx market indicators BTC-USDT --bar 1D --indicator RSI
okx market indicators BTC-USDT --bar 1D --indicator MACD
```
Interpret RSI:
- `RSI > 70` → Overbought — potential pullback risk 🔴
- `RSI 50–70` → Bullish momentum, healthy trend 🟢
- `RSI 30–50` → Bearish/neutral, watch for reversal 🟡
- `RSI < 30` → Oversold — potential bounce setup 🟢

Interpret MACD:
- MACD line **above** signal line → Bullish momentum
- MACD line **below** signal line → Bearish momentum
- Histogram expanding → Trend strengthening
- Histogram shrinking → Trend weakening, possible reversal

---

### Step 3 — BTC Derivatives Sentiment
```
okx market funding-rate BTC-USDT-SWAP
okx market open-interest --instType SWAP --instId BTC-USDT-SWAP
```
Interpret funding rate:
- `> +0.05%` → Strong bullish leverage, longs paying — overheated long risk
- `0% to +0.05%` → Mild bullish bias, healthy
- `< 0%` → Bearish bias, shorts paying — possible short squeeze setup

---

### Step 4 — Altcoin Sweep (Top Movers)
```
okx market tickers SPOT
```
From the full ticker list, filter for USDT pairs and rank by:
1. **Top 5 Gainers** — highest 24h change %
2. **Top 5 Losers** — lowest 24h change %
3. **Top 5 by Volume** — highest 24h USDT volume

---

### Step 5 — Core Altcoin Deep Check + RSI
Run for each tracked altcoin: ETH, SOL, BNB, XRP, DOGE, ADA, AVAX, LINK
```
okx market ticker ETH-USDT
okx market indicators ETH-USDT --bar 1H --indicator RSI
okx market ticker SOL-USDT
okx market indicators SOL-USDT --bar 1H --indicator RSI
okx market ticker BNB-USDT
okx market indicators BNB-USDT --bar 1H --indicator RSI
okx market ticker XRP-USDT
okx market indicators XRP-USDT --bar 1H --indicator RSI
okx market ticker DOGE-USDT
okx market indicators DOGE-USDT --bar 1H --indicator RSI
okx market ticker ADA-USDT
okx market indicators ADA-USDT --bar 1H --indicator RSI
okx market ticker AVAX-USDT
okx market indicators AVAX-USDT --bar 1H --indicator RSI
okx market ticker LINK-USDT
okx market indicators LINK-USDT --bar 1H --indicator RSI
```
For each: capture price, 24h change %, volume, and RSI signal.

---

### Step 6 — Altcoin Funding Rates (Sentiment Layer)
```
okx market funding-rate ETH-USDT-SWAP
okx market funding-rate SOL-USDT-SWAP
okx market funding-rate XRP-USDT-SWAP
```
Flag any altcoin with funding rate > +0.08% (overheated) or < -0.03% (bearish squeeze risk).

---

### Step 7 — BTC Dominance Signal (Alt Season Check)
Calculate a simple proxy:
- Sum the 24h volume of all tracked altcoins vs BTC volume
- If altcoin aggregate volume > 2× BTC volume AND majority of alts outperforming BTC → flag as **Alt Season Signal 🟢**
- If BTC volume dominates and alts underperforming → flag as **BTC Season Signal 🟡**
- Mixed signals → **Neutral / Rotating 🔵**

---

## Output Format

Present the final report in this structure:

---

### 🟠 BTC Snapshot
| Metric | Value |
|---|---|
| Price | $XX,XXX |
| 24h Change | +X.X% |
| 24h Range | $XX,XXX – $XX,XXX |
| RSI (1H) | XX — Bullish / Overbought / Oversold |
| RSI (1D) | XX — Bullish / Overbought / Oversold |
| MACD (1H) | Bullish / Bearish / Weakening |
| Funding Rate | +0.0X% (Mild Bullish) |
| Open Interest | $X.XB |

---

### 🌊 Alt Season Meter
**Signal:** [Alt Season 🟢 / BTC Season 🟡 / Neutral 🔵]
Brief 1-sentence reasoning based on dominance proxy.

---

### 📈 Top Altcoin Movers (24h)
| Coin | Price | 24h % | RSI (1H) | Volume |
|---|---|---|---|---|
| SOL | $XXX | +X.X% | 62 — Bullish | $XXX M |
| ... | | | | |

---

### 📉 Top Losers (24h)
| Coin | Price | 24h % | RSI (1H) |
|---|---|---|---|
| ... | | | |

---

### ⚠️ Alerts
- **Funding Rate Alerts:** List any coin with abnormal funding rates and what it signals.
- **RSI Extremes:** Flag any coin with RSI > 70 (overbought) or RSI < 30 (oversold).
- **MACD Signal:** Flag any BTC MACD crossover signals detected.

---

### 💡 Market Summary
2–3 sentence plain-English summary of overall market conditions —
is BTC leading or lagging, are alts rotating, any overbought/oversold coins
worth watching based on RSI and MACD signals?

---

## Notes & Limitations

- All data is pulled live from OKX public market endpoints (no API key required).
- Funding rates update every 8 hours; check timing when interpreting sentiment.
- RSI and MACD are lagging indicators — always combine with price action context.
- This skill provides market data and analysis only — it does not place trades.
- For trade execution, combine with `okx-cex-trade` skill.
- Always do your own research. This is not financial advice.
