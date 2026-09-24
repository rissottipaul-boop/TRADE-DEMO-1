---
name: market-intel
description: >
  Crypto market social intelligence for OKX traders. Aggregates Twitter/X
  trending narratives, detects social momentum shifts, and maps insights to
  OKX trading pairs with live price verification via OKX Trade Kit. Use
  when: "market briefing", "watch market", "research [topic]", "any
  signals", "market sentiment", "social trends", "narrative-driven trading
  signals", or Chinese equivalents: "市场简报", "晨报", "有什么异动",
  "社交面怎么样", "搜索 [keyword]", "监控市场", "今天市场怎么样". Do NOT
  trigger for: technical indicator analysis (use kline-indicator), simple
  price checks (use cmc-mcp), or trade execution requests.
---

# Market Intel — Crypto Social Intelligence for OKX

Social intelligence -> OKX pair mapping -> price verification -> trade execution.
Follow the user's input language.

## Data Source: Chainbase Tops API (free, no key)

**CLI**: prefer `chainbase` (global install), fallback `npx --registry https://registry.npmjs.org chainbase-cli`

First run: check `which chainbase`. If missing, prompt user: `npm install -g chainbase-cli --registry https://registry.npmjs.org`

**Error**: non-zero exit or non-JSON -> "Data source temporarily unavailable."
**Rate limit**: max 1 call per 5 min per endpoint.

### Data Extraction (always use these, never raw API)

**CMD_TRENDING** -- compact trending (100KB -> 5KB):
```bash
chainbase tops trending --json 2>/dev/null | python3 -c "
import json,sys;data=json.load(sys.stdin)
for t in data.get('items',[])[:20]:
    print(json.dumps({'id':t.get('id',''),'kw':t.get('keyword',''),'summary':t.get('summary','')[:150],'authors':len(t.get('authors',[]))},ensure_ascii=False))"
```

**CMD_SEARCH(keyword)** -- top 5 topics (450KB -> 2KB):
```bash
chainbase tops search "KEYWORD_HERE" --json 2>/dev/null | python3 -c "
import json,sys;data=json.load(sys.stdin)
for t in data.get('items',[])[:5]:
    print(json.dumps({'id':t.get('id',''),'kw':t.get('keyword',''),'summary':t.get('summary','')[:150],'authors':len(t.get('authors',[]))},ensure_ascii=False))"
```

**CMD_MENTIONS(keyword)** -- 15 samples + themes (140KB -> 3KB):
```bash
chainbase tops mentions "KEYWORD_HERE" --json 2>/dev/null | python3 -c "
import json,sys;data=json.load(sys.stdin);items=data.get('items',[])
texts=[m.get('text','') if isinstance(m,dict) else str(m) for m in items]
from collections import Counter;words=Counter()
for t in texts:
    for w in t.lower().split():
        if len(w)>3: words[w]+=1
print(json.dumps({'total':len(texts),'samples':[t[:150] for t in texts[:15]],'themes':[w for w,c in words.most_common(10) if c>2]},ensure_ascii=False))"
```

**Keyword sanitization**: Before substituting any user keyword into the above commands, strip all shell metacharacters: `"; | & $ ( ) \ ` { } < > ! #`. Only allow alphanumeric characters, spaces, hyphens, and underscores. If a keyword contains disallowed characters, reject it with a clear error message.

If `chainbase` not found, replace with `npx --registry https://registry.npmjs.org chainbase-cli` in above commands.

---

## OKX Price Verification

After mapping social signals to OKX pairs, always verify with live market data. Use CLI first, MCP as fallback:

**CLI (primary):**
```bash
okx market ticker --instId BTC-USDT
okx market orderbook --instId BTC-USDT
okx market indicator --instId BTC-USDT --ind RSI
```

**MCP fallback (if CLI unavailable):**
- `market_get_ticker(instId="BTC-USDT")` — last price, 24h high/low, volume, change
- `market_get_orderbook(instId="BTC-USDT")` — liquidity depth for HIGH impact signals
- `market_get_indicator(instId="BTC-USDT", indicator="rsi", bar="1Dutc")` — RSI/MACD context

If OKX Trade Kit is not available at all, skip price verification and note: "Price data unavailable — install OKX Trade Kit for live market context."

---

## Module 1: Market Briefing

**Triggers**: market briefing / daily briefing / how is the market / what is happening in crypto / 市场简报 / 晨报 / 今天市场怎么样

1. Run **CMD_TRENDING**
2. For each topic: classify sentiment (BULLISH / BEARISH / NEUTRAL), impact (>30 authors = HIGH, 10-30 = MED, <10 = LOW)
3. Read `references/okx-pairs.md` -> map keywords to OKX pairs
4. **Price verification**: For each mapped OKX pair, fetch current price and 24h change via CLI or MCP (see OKX Price Verification above)
5. **Group by OKX pair**, show bull/bear signals together per pair
6. Add per-pair synthesis + actionable section

**Output**:
```markdown
## Market Intel Briefing -- {date/time}

### BTC-USDT (${price} | 24h: {change}%)
| Narrative | Sentiment | Impact |
|-----------|-----------|--------|
| **{keyword}** ({N}) -- {summary} | BULLISH | HIGH |
| **{keyword}** ({N}) -- {summary} | BEARISH | MED |
> Synthesis: {multi-signal synthesis for this pair}

### Other (no direct OKX pair mapping)
| ... |

### Actionable
- **{pair}**: {recommendation}

### Quick Trade
To act on these signals via OKX Trade Kit:
- Check orderbook depth: `okx market orderbook --instId {pair}`
- View recent candles: `okx market candles --instId {pair} --bar 1H --limit 24`
- Place order when ready
```

---

## Module 2: Signal Alert

**Triggers**: any signals / watch market / what changed / new narratives / 有什么异动 / 监控市场

1. Run **CMD_TRENDING**, also save raw to `/tmp/mi-raw.json`:
   `chainbase tops trending --json 2>/dev/null > /tmp/mi-raw.json`
   Then run the compact extraction on `/tmp/mi-raw.json`
2. Load previous snapshot `/tmp/market-intel-snapshot.json` (if exists)
3. Diff:
   - topic ID only in current -> **NEW: New narrative**
   - topic ID in both, authors >2x previous -> **SURGE (+X%)**
   - First run (no snapshot) -> all flagged as new
4. Classify sentiment, map OKX pairs via `references/okx-pairs.md`
5. **Price verification**: For each signal's OKX pair, fetch current price via CLI or MCP
6. Output signals
7. Save compact snapshot (AFTER comparison):
   ```bash
   python3 -c "
   import json;data=json.load(open('/tmp/mi-raw.json'))
   json.dump([{'id':t['id'],'kw':t['keyword'],'n':len(t.get('authors',[]))} for t in data.get('items',[])],open('/tmp/market-intel-snapshot.json','w'))"
   ```

**Output** per signal:
```
SIGNAL: {New narrative | Surge (+120%)}
"{keyword}" -- {N} authors | {Category} | {Sentiment}
-> OKX: {pairs} @ ${price} (24h: {change}%)
```
No signals: "No new signals since last check."

For periodic monitoring, the user can re-run this module on a schedule (e.g., every 10 minutes via cron or a looping terminal command).

---

## Module 3: Research Query

**Triggers**: research {topic} / tell me about {token} / social sentiment for {keyword} / {token} 社交面怎么样 / 搜索 {keyword}

1. Extract keyword from user query
2. **Sanitize keyword** per the sanitization rule above
3. Run **CMD_SEARCH(keyword)** -> top 5 related topics
4. Run **CMD_MENTIONS(keyword)** -> total count + 15 samples + themes
5. Classify sentiment per topic + aggregate mention sentiment
6. Map to OKX pairs via `references/okx-pairs.md`
7. **Price verification**: Fetch price via CLI or MCP; optionally fetch RSI/MACD for HIGH impact signals

**Output**:
```
RESEARCH: {keyword}

-- Related Topics ({count}) --
1. **{topic}** -- {N} authors, {sentiment}
   {summary}

-- Recent Mentions ({total} posts) --
- Sentiment: ~{X}% Bullish / ~{Y}% Neutral / ~{Z}% Bearish
- Themes: {theme1}, {theme2}, {theme3}

-- OKX Market Context --
| Pair | Price | 24h Change | RSI(14) |
|------|-------|------------|---------|
| {pair} | ${price} | {change}% | {rsi} |

-> Verify pair availability on OKX before trading
```

---

## OKX Pair Mapping

Read `references/okx-pairs.md` for the full keyword → OKX pair mapping table.

Fallback (if file unavailable): btc -> BTC-USDT, eth -> ETH-USDT, sol -> SOL-USDT, etf -> BTC-USDT + ETH-USDT

**Rules**: short words (sui, ai, sei) = exact match only. Unknown tokens = infer pair + flag "(unverified)".

---

## Sentiment Classification

Classify each summary as BULLISH / BEARISH / NEUTRAL. Use language understanding across all languages. Reference keywords (guidance only):
- Bullish: launch, listing, approved, inflow, surge, record, institutional, partnership, upgrade
- Bearish: crash, hack, exploit, sell-off, ban, lawsuit, investigation, liquidation
- Neutral: discussion, analysis, comparison, overview, update

---

## OKX Trade Kit Integration

This skill outputs pair suggestions with live prices. No auto-execution. Workflow:
1. Social intelligence identifies narratives and maps to OKX pairs
2. OKX Trade Kit CLI/MCP confirms current price and momentum
3. User reviews signals and decides to trade
4. User confirms in natural language -> Claude invokes OKX Trade Kit to execute
