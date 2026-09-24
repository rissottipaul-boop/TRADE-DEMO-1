# Report Templates

## Full Research Report

**Price formatting:** Use enough decimal places to show 3-4 significant digits.
For tokens priced below $0.01, show all leading zeros (e.g., $0.000003381 for
PEPE). Never round a sub-cent price to $0.00.

```
## [Token Name] Research Report

### Overview
- Category: [DeFi/Layer 1/Meme/etc.]
- Launched: [Date]
- Rank: #XX by market cap
- Website: [URL]

### Market Snapshot
- Price: $X.XX (24h: +X.X% | vs BTC 24h: [outperforming/underperforming by X.X%])
- Market Cap: $X.XX B
- 24h Volume: $X.XX M (Volume/MCap ratio: X.X%)
- Performance: 7d X.X% | 30d X.X% | 90d X.X%
- Bid-Ask Spread: X.XX% ([tight/normal/wide] liquidity)

### Supply
- Circulating: X.XX M (XX% of max)
- Max Supply: X.XX M [or "Unlimited"]

### Technical Outlook
- Trend: [Bullish/Bearish] — price [above/below] 200d EMA by X.X%
- RSI (14d): XX — [oversold (<30) / neutral (30-70) / overbought (>70)]
- MACD: [Bullish/Bearish] — DIF [above/below] DEA, histogram [expanding/contracting]
- Bollinger Bands: Price at [lower/middle/upper] band region ($XX - $XX range)
- Supertrend: [UP/DOWN] — support at $X.XX
- Key Levels: Support ~$X.XX (BB lower/Supertrend) | Resistance ~$X.XX (BB upper/200d EMA)

### Derivatives Data
- Funding Rate: X.XXXX% ([balanced/mildly bullish/elevated/extreme] — see interpretation)
- Open Interest: $X.XX M ([rising/falling] — [interpretation])
- Order Book Imbalance: X% bids vs X% asks ([balanced/bid-heavy/ask-heavy])

### Holder Analysis (if CMC data available)
- Total Addresses: X.XX M
- Whale Concentration: X.X%
- Long-term Holders: XX%
- Holder Trend: Growing/Stable/Declining

### Recent News
- [Headline 1] — [positive/negative/neutral]
- [Headline 2] — [positive/negative/neutral]
- ...

### Green Flags
- [Flag]: [Specific data point + why it matters]

### Red Flags
- [Flag]: [Specific data point + why it matters]

### Risk Rating

**Overall Risk: [Low / Medium / High / Very High]**

Scoring guide (count red flags vs green flags, weight by severity):
- 0-1 red flags, mostly green → **Low Risk** (established, healthy metrics)
- 2-3 red flags, mixed signals → **Medium Risk** (caution warranted, monitor closely)
- 4-5 red flags or any critical red flag → **High Risk** (significant concerns, position size accordingly)
- 6+ red flags or multiple critical → **Very High Risk** (extreme caution, speculative only)

Critical red flags that auto-elevate to High: price >50% below ATH with
declining holders, extreme funding rate, or very low volume relative to market cap.

### Summary
[2-3 sentence synthesis: What does the data say overall? What's the key risk?
What would change the picture (e.g., "A reclaim of the 200d EMA at $128 would
flip the trend bullish")?]

*This is research data, not financial advice. Always do your own research before
making investment decisions.*
```

---

## Quick Snapshot Template

Use this for Quick Research mode (see SKILL.md for when to apply).

```
## [Token Name] — Quick Snapshot

**Price:** $X.XX (24h: +X.X% | vs BTC: [outperforming/underperforming])
**Volume:** $X.XX M (24h) | Spread: X.XX%
**Trend:** [Bullish/Bearish/Neutral] — [above/below] 200d EMA by X.X%
**RSI:** XX ([oversold/neutral/overbought]) | **MACD:** [bullish/bearish]
**Funding:** X.XXXX% ([interpretation]) | **OI:** $X.XX M
**Key Levels:** Support ~$X.XX | Resistance ~$X.XX

**Quick Take:** [1-2 sentences: what's the story right now?]

*Quick snapshot — run a full research request for complete due diligence.*
```
