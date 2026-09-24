---
name: kline-indicator
description: >
  K-line Indicator Engine for crypto technical analysis using a 3-pillar
  framework (macro cycle + price-volume factors + derivatives). Use this
  skill whenever the user wants to analyze a coin or market, asks about
  trading signals or entry conditions, wants a K-line or candlestick chart,
  asks about order flow or order book analysis, wants a trade plan with
  entry/target/stop, or asks about market sentiment. Also trigger for casual
  phrases like "分析BTC", "看看ETH行情", "BTC现在怎么样", "帮我看看SOL",
  "画个K线图", "扫描热门币", "有没有信号", "市场情绪", or "BTC能不能做多",
  even if the user does not explicitly say "technical analysis" or
  "indicator". Do NOT trigger when the user explicitly asks for a full
  structured trading plan with position sizing and risk tiers (use
  trading-plan-generator), DCA bot parameter recommendations (use
  dca-bot-parameterizer), or social narrative analysis (use market-intel).
---

# K-Line Indicator Engine

Complete technical analysis framework combining macro cycle assessment,
price-volume indicator scoring, and derivative market positioning into
actionable trading signals.

## Operating Modes

Detect the user's intent and select the mode. Each mode specifies which
reference files to read and which scripts to run — load only what is needed.

| Mode | Trigger | Reference files to load | Scripts to run |
|------|---------|------------------------|----------------|
| **quick** | Fast scan, 一句话看行情 | — | `kline_ext_indicators.py` (core only) |
| **full** | Full analysis, 完整分析 | `indicators.md`, `three-pillars.md`, `orderflow.md`, `trading.md` | `kline_ext_indicators.py`, `kline_orderflow.py` |
| **scan** | Coin list or top N, 扫描 | `indicators.md` | `kline_ext_indicators.py` |
| **alert** | Condition monitoring, 提醒 | `trading.md` | `kline_ext_indicators.py` |
| **orderflow** | Delta, CVD, order book | `orderflow.md` | `kline_orderflow.py` |
| **macro** | Market cycle stage | `three-pillars.md` | — |
| **custom** | Specific indicators | `indicators.md` | `kline_ext_indicators.py` |
| **chart** | Chart or K-line image | — | `kline_chart.py` |
| **conditional** | Trade plan with entry/target/stop | `trading.md` | `kline_ext_indicators.py`, `kline_orderflow.py` |

When no mode is explicit, use **quick** for a single symbol and **full** when
the user asks for a complete analysis or trade decision.

## 3-Pillar Framework

The composite score combines three pillars — each answers a different question
about market conditions. For full pillar definitions, scoring formulas, and
market stage classifications, read `references/three-pillars.md`.

| Pillar | Weight | Question |
|--------|--------|----------|
| Macro Cycle | 30% | Where are we in the market cycle? |
| Price-Volume Factors | 40% | What do price and volume signals say? |
| Derivatives | 30% | What is smart money pricing in? |

## Data Sources

### Primary: OKX TradeKit CLI

Try CLI first — no MCP overhead, same data:

```bash
okx market ticker <instId>
okx market indicator rsi <instId> --bar 1H
okx market indicator macd <instId> --bar 4H
okx market indicator ema <instId> --bar 1D
okx market indicator bb <instId> --bar 1D
okx market indicator supertrend <instId> --bar 1D
okx market indicator kdj <instId> --bar 1D
okx market funding-rate <instId>
okx market open-interest --instType SWAP --instId <instId>
okx market candles <instId> --bar 1H --limit 60
```

### Fallback: OKX TradeKit MCP

If CLI is unavailable, call MCP tools directly — no scripts needed:

| Data | MCP call |
|------|----------|
| Price / 24h stats | `market_get_ticker(instId)` |
| Candles | `market_get_candles(instId, bar, limit)` |
| RSI | `market_get_indicator(instId, indicator="rsi", bar)` |
| MACD | `market_get_indicator(instId, indicator="macd", bar)` |
| EMA | `market_get_indicator(instId, indicator="ema", bar)` |
| Bollinger Bands | `market_get_indicator(instId, indicator="bb", bar)` |
| Supertrend | `market_get_indicator(instId, indicator="supertrend", bar)` |
| KDJ | `market_get_indicator(instId, indicator="kdj", bar)` |
| Funding rate | `market_get_funding_rate(instId)` |
| Open interest | `market_get_open_interest(instId, instType)` |
| Order book | `market_get_orderbook(instId)` |

### Macro Data

Rainbow Chart, AHR999, MVRV, Fear & Greed Index, BTC dominance
(fetched via public APIs or user-provided values).

## Python Scripts

Use scripts **only** for indicators not available via OKX TradeKit MCP. Feed
them candles from `market_get_candles`.

- **`${CLAUDE_SKILL_DIR}/scripts/kline_ext_indicators.py`**: ATR, Stochastic,
  Ichimoku, VWAP, divergence detection, support/resistance, Alpha 101/191
  factors, 30+ candlestick patterns — everything not covered by MCP

- **`${CLAUDE_SKILL_DIR}/scripts/kline_orderflow.py`**: Order flow delta, CVD,
  bid/ask wall detection, composite score (0–100)

- **`${CLAUDE_SKILL_DIR}/scripts/kline_chart.py`**: Terminal (ASCII) and PNG
  chart rendering with MACD/RSI/volume panels and pattern annotations.
  After generating a PNG, print the output file path so the user can access it.

## Reference Documentation

Read these files on demand — do not load all at once:

- **`references/indicators.md`**: Complete indicator catalog, pattern
  definitions, divergence methodology, signal scoring (−5 to +5 → 0–100)
- **`references/three-pillars.md`**: Pillar definitions, macro cycle stages,
  composite scoring formula, market stage classification
- **`references/orderflow.md`**: Delta/CVD/order book metrics, 6-component
  scoring, smart money interpretation
- **`references/trading.md`**: Signal grades, entry conditions (all 3
  required), position sizing, exit signals, pre-trade checklist, when not to
  trade, fee and slippage adjustment
