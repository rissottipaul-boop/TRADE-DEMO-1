---
name: okx-cex-volatility-strategy
description: "Use this skill when the user wants to start, run, monitor, evaluate, or stop an automated BTC/ETH perpetual swap strategy on OKX based on 1H technical signals and volatility-adaptive risk rules, including requests like 'start volatility strategy', 'run BTC ETH auto strategy', 'auto trade BTC ETH based on indicators', 'volatility adaptive trading', 'multi-indicator confluence strategy', 'strategy status', or 'stop the strategy'. This skill covers 1H data collection, sentiment scoring, AI decision logic, Agent Trade Kit execution, and strategy-level risk control for BTC-USDT-SWAP and ETH-USDT-SWAP. Do NOT use for manual orders (use okx-cex-trade), read-only market data queries (use okx-cex-market), or account-only queries (use okx-cex-portfolio)."
license: MIT
metadata:
  author: okx
  version: "1.3.1"
  homepage: "https://www.okx.com"
  agent:
    emoji: "📊"
    requires:
      bins: ["okx"]
    install:
      - id: npm
        kind: node
        package: "@okx_ai/okx-trade-cli"
        bins: ["okx"]
        label: "Install okx CLI (npm)"
---

# OKX CEX Volatility Strategy

This skill follows the competition template:

1. Step 1 — Data Collection / 数据采集
2. Step 2 — Sentiment Assessment / 情绪评估
3. Step 3 — AI Decision Logic / AI 判断逻辑
4. Step 4 — Order Execution / 下单执行
5. Step 5 — Risk Rules / 风控规则

Automated 1H strategy for BTC-USDT-SWAP and ETH-USDT-SWAP only. The strategy runs on the agent side through the OKX CLI / Agent Trade Kit. It should stay conservative and low-ambiguity by default.

## Scope & Routing

| User intent | Route |
|---|---|
| Market prices, candles, technical indicators | `okx-cex-market` |
| Manual spot/swap/futures/options orders | `okx-cex-trade` |
| Account balance, positions, transfers, account config | `okx-cex-portfolio` |
| Grid / DCA bots | `okx-cex-bot` |
| **Automated 1H BTC/ETH swap strategy with sentiment + risk rules** | **This skill** |

## Prerequisites

1. Install and preflight the OKX CLI:

   ```bash
   npm install -g @okx_ai/okx-trade-cli
   okx upgrade
   okx --version
   okx config show
   ```

2. Never accept API credentials directly in chat. If the user pastes credentials, stop and ask them to edit `~/.okx/config.toml` locally or run `okx config init`.

3. Never assume profile names. `okx config init` usually creates `okx-demo` and `okx-prod`, but users may rename profiles.
   - If the user explicitly names a configured profile, use it.
   - If exactly one configured profile exists, use it and tell the user.
   - If multiple configured profiles exist and the user did not choose one, ask before the first write.

4. Before the first strategy run, confirm account state:

   ```bash
   okx --profile <profile> account config --json
   okx --profile <profile> account balance USDT --json
   ```

5. This strategy assumes:
   - `posMode = net_mode`
   - `tdMode = cross`
   - one direction per instrument

   If `okx account config` shows `long_short_mode`, stop and ask the user whether to switch with:

   ```bash
   okx --profile <profile> account set-position-mode --posMode net_mode
   ```

6. A readable balance proves read access only. Do not claim trade permission is active until the first confirmed write succeeds.

## Strategy Snapshot

- Instruments: `BTC-USDT-SWAP`, `ETH-USDT-SWAP`
- Timeframe: `1H` only
- Margin mode: `cross`
- Position mode: `net`
- Indicators: `EMA(20,60)`, `RSI(14)`, `MACD(12,26,9)`, `BB(20,2)`, `ATR(14)`
- Closed-bar convention:
  - `index 0` = live / forming 1H bar, never use it for signal generation
  - `c1 = index 1` = last closed 1H bar
  - `c2 = index 2` = previous closed 1H bar
  - closed 30-bar window = indexes `1..30`
  - 30 closed bars now represent about 30 hours of lookback

Detailed formulas and workflow payloads live in:

- `{baseDir}/references/strategy-params.md`
- `{baseDir}/references/workflows.md`

## Step 1 — Data Collection / 数据采集

For each instrument, collect the following before any decision:

| Data | CLI command | Purpose |
|---|---|---|
| Instrument meta | `okx market instruments --instType SWAP --instId <instId> --json` | Get `ctVal`, `tickSz`, `lotSz`, `minSz`, `state` |
| 1H candles | `okx market candles <instId> --bar 1H --limit 31 --json` | Get closed-bar `close[c1]` and recent close series |
| EMA | `okx market indicator ema <instId> --bar 1H --params 20,60 --list --limit 3 --json` | Trend filter |
| RSI | `okx market indicator rsi <instId> --bar 1H --params 14 --list --limit 3 --json` | Momentum filter |
| MACD | `okx market indicator macd <instId> --bar 1H --params 12,26,9 --list --limit 3 --json` | Crossover trigger |
| Bollinger Bands | `okx market indicator bb <instId> --bar 1H --params 20,2 --list --limit 31 --json` | Volatility percentile + mid-band filter |
| ATR | `okx market indicator atr <instId> --bar 1H --params 14 --list --limit 31 --json` | TP/SL distance + ATR spike guard |
| Available balance | `okx --profile <profile> account balance USDT --json` | Capital sizing |
| Max order size | `okx --profile <profile> account max-size --instId <instId> --tdMode cross --json` | Prevent oversizing |
| Open positions | `okx --profile <profile> swap positions --instId <instId> --json` | Current strategy state |
| Active TP/SL algos | `okx --profile <profile> swap algo orders --instId <instId> --json` | Existing stop structure |

Data collection rules:

- Use `market candles` for `close[c1]`. Do not assume `BB` output contains a close price.
- Use `c1 = 1`, `c2 = 2`, and indexes `1..30` for all rolling calculations.
- If any required market field is missing, stale, or inconsistent, output `action = "skip"` for that instrument.
- Do not fire all public requests without throttling. Small batched parallelism is fine; unbounded fan-out is not needed for a 1H strategy.

## Step 2 — Sentiment Assessment / 情绪评估

Sentiment is an intermediate output. It is not an order by itself.

Use the last closed bar `c1` and previous closed bar `c2`:

```text
bullish_points =
  35 if ema20[c1] > ema60[c1]
  + 25 if dif[c1] > dea[c1] and dif[c2] <= dea[c2]
    else 10 if dif[c1] > dea[c1]
  + 15 if 50 <= rsi[c1] <= 70
  + 15 if close[c1] > bb_middle[c1]

bearish_points =
  35 if ema20[c1] < ema60[c1]
  + 25 if dif[c1] < dea[c1] and dif[c2] >= dea[c2]
    else 10 if dif[c1] < dea[c1]
  + 15 if 30 <= rsi[c1] < 50
  + 15 if close[c1] < bb_middle[c1]

sentiment_score = bullish_points - bearish_points
```

Classification:

- `sentiment_score >= 50` -> `bullish`
- `sentiment_score <= -50` -> `bearish`
- otherwise -> `neutral` / `range`

Volatility regime:

- `BB bandwidth percentile 0-25%` -> `low_vol`
- `25-75%` -> `normal_vol`
- `75-100%` -> `high_vol`
- if `ATR[c1] > 3 * mean(ATR[1..30])` -> `shock`, `trade_blocked = true`

Expected sentiment output:

```json
{
  "instId": "BTC-USDT-SWAP",
  "sentimentDirection": "bullish",
  "sentimentScore": 65,
  "volatilityRegime": "normal_vol",
  "tradeBlocked": false,
  "reason": [
    "EMA20 above EMA60 on last closed 1H bar",
    "Fresh MACD golden cross on c1 vs c2",
    "Close above BB middle"
  ]
}
```

## Step 3 — AI Decision Logic / AI 判断逻辑

Use only the strict signal conditions below for entries.

### Entry Conditions

**Long**

1. `ema20[c1] > ema60[c1]`
2. `40 <= rsi[c1] <= 70`
3. `dif[c1] > dea[c1] && dif[c2] <= dea[c2]`
4. `close[c1] > bb_middle[c1]`
5. `trade_blocked = false`

**Short**

1. `ema20[c1] < ema60[c1]`
2. `30 <= rsi[c1] <= 60`
3. `dif[c1] < dea[c1] && dif[c2] >= dea[c2]`
4. `close[c1] < bb_middle[c1]`
5. `trade_blocked = false`

### Position Actions

| Current position | Signal | Action |
|---|---|---|
| No position | Long | `open_long` |
| No position | Short | `open_short` |
| No position | None | `skip` |
| Long | Long | `hold` |
| Short | Short | `hold` |
| Long | Short | `reverse_to_short` |
| Short | Long | `reverse_to_long` |
| Any | Hard risk triggered | `close_only` |

### Add / Reduce Rules

Default competition template behavior:

- `allow_add = false`
- `allow_reduce = false`

This v1 strategy is intentionally single-entry / single-exit. It does not pyramid and does not scale out unless the user explicitly asks for a different policy.

### Decision Prompt Template

Use the following instruction style when asking the AI layer to produce a decision:

```text
You are the execution brain of okx-cex-volatility-strategy.
Use only the last closed 1H bar (c1=index 1) and the previous closed 1H bar (c2=index 2).
Never use live bar index 0 for signal generation.
Default allow_add=false and allow_reduce=false.
If any required field is missing, action must be "skip".
If any hard risk rule is triggered, action must be "close_only" or "skip".
Return JSON only.
```

Expected decision payload:

```json
{
  "instId": "BTC-USDT-SWAP",
  "sentimentDirection": "bullish",
  "sentimentScore": 65,
  "volatilityRegime": "normal_vol",
  "signal": "long",
  "action": "open_long",
  "leverage": 4,
  "sizePct": 24,
  "sizeMode": "quote_ccy",
  "tp": "102000",
  "sl": "96000",
  "riskFlags": []
}
```

## Step 4 — Order Execution / 下单执行

Execution rules:

1. Always fetch `ctVal`, `tickSz`, `minSz`, and `state` before entry.
2. Entry sizing may be expressed in USDT by using `tgtCcy=quote_ccy`.
3. After the entry fills, read the actual filled contracts from the position / fill result.
4. Place TP/SL using the actual filled contracts, not the requested USDT amount.
5. Round `tpTriggerPx` and `slTriggerPx` to `tickSz`.
6. Verify after every write. If TP/SL attach fails after one retry, immediately `close_only` the position.

### Entry Example

```text
tag="CLI"
okx market instruments --instType SWAP --instId BTC-USDT-SWAP --json
okx --profile <profile> account max-size --instId BTC-USDT-SWAP --tdMode cross --json
okx --profile <profile> swap leverage --instId BTC-USDT-SWAP --lever 4 --mgnMode cross
okx --profile <profile> swap place --instId BTC-USDT-SWAP --side buy --ordType market --sz 1000 --tgtCcy quote_ccy --tdMode cross
okx --profile <profile> swap positions --instId BTC-USDT-SWAP --json
okx --profile <profile> swap algo place --instId BTC-USDT-SWAP --side sell --ordType oco --sz <filled_contracts> --tdMode cross --tpTriggerPx <tp> --tpOrdPx=-1 --slTriggerPx <sl> --slOrdPx=-1
okx --profile <profile> swap algo orders --instId BTC-USDT-SWAP --json
```

```text
tag="MCP"
market_get_instruments({instType:"SWAP", instId:"BTC-USDT-SWAP"})
account_get_max_size({instId:"BTC-USDT-SWAP", tdMode:"cross"})
swap_set_leverage({instId:"BTC-USDT-SWAP", lever:"4", mgnMode:"cross"})
swap_place_order({instId:"BTC-USDT-SWAP", tdMode:"cross", side:"buy", ordType:"market", sz:"1000", tgtCcy:"quote_ccy"})
swap_get_positions({instId:"BTC-USDT-SWAP"})
swap_place_algo_order({instId:"BTC-USDT-SWAP", tdMode:"cross", side:"sell", ordType:"oco", sz:"<filled_contracts>", tpTriggerPx:"<tp>", tpOrdPx:"-1", slTriggerPx:"<sl>", slOrdPx:"-1"})
swap_get_algo_orders({instId:"BTC-USDT-SWAP", status:"pending"})
```

### Reverse Example

```text
tag="CLI"
okx --profile <profile> swap algo orders --instId BTC-USDT-SWAP --json
okx --profile <profile> swap algo cancel --instId BTC-USDT-SWAP --algoId <algoId>
okx --profile <profile> swap close --instId BTC-USDT-SWAP --mgnMode cross
okx --profile <profile> swap positions --instId BTC-USDT-SWAP --json
# only after the previous position is confirmed flat:
okx --profile <profile> swap leverage --instId BTC-USDT-SWAP --lever 4 --mgnMode cross
okx --profile <profile> swap place --instId BTC-USDT-SWAP --side sell --ordType market --sz 1000 --tgtCcy quote_ccy --tdMode cross
okx --profile <profile> swap positions --instId BTC-USDT-SWAP --json
okx --profile <profile> swap algo place --instId BTC-USDT-SWAP --side buy --ordType oco --sz <filled_contracts> --tdMode cross --tpTriggerPx <tp> --tpOrdPx=-1 --slTriggerPx <sl> --slOrdPx=-1
```

```text
tag="MCP"
swap_get_algo_orders({instId:"BTC-USDT-SWAP", status:"pending"})
swap_cancel_algo_orders({orders:[{"algoId":"<algoId>","instId":"BTC-USDT-SWAP"}]})
swap_close_position({instId:"BTC-USDT-SWAP", mgnMode:"cross"})
swap_get_positions({instId:"BTC-USDT-SWAP"})
# only after the previous position is confirmed flat:
swap_set_leverage({instId:"BTC-USDT-SWAP", lever:"4", mgnMode:"cross"})
swap_place_order({instId:"BTC-USDT-SWAP", tdMode:"cross", side:"sell", ordType:"market", sz:"1000", tgtCcy:"quote_ccy"})
swap_get_positions({instId:"BTC-USDT-SWAP"})
swap_place_algo_order({instId:"BTC-USDT-SWAP", tdMode:"cross", side:"buy", ordType:"oco", sz:"<filled_contracts>", tpTriggerPx:"<tp>", tpOrdPx:"-1", slTriggerPx:"<sl>", slOrdPx:"-1"})
```

## Step 5 — Risk Rules / 风控规则

### Volatility-Adaptive Leverage and Position Size

Use Bollinger Bandwidth percentile on the closed 30-bar window (`indexes 1..30`):

| BB bandwidth percentile | Leverage | Margin (sizePct) | Notional (margin × leverage) |
|---|---|---|---|
| `0-25%` | `5x` | `30%` of available USDT | `150%` of available USDT |
| `25-50%` | `4x` | `24%` of available USDT | `96%` of available USDT |
| `50-75%` | `3x` | `16%` of available USDT | `48%` of available USDT |
| `75-100%` | `2x` | `10%` of available USDT | `20%` of available USDT |

**Sizing semantics:** `sizePct` is the **margin ratio** (actual collateral committed), not the notional.
The entry order size is calculated as:

```
notional (sz, tgtCcy=quote_ccy) = availableUSDT × sizePct% × leverage
margin used                      = notional / leverage = availableUSDT × sizePct%
```

### Trade-Level Risk

- Long:
  - `SL = entry - 2 * ATR[c1]`
  - `TP = entry + 3 * ATR[c1]`
- Short:
  - `SL = entry + 2 * ATR[c1]`
  - `TP = entry - 3 * ATR[c1]`
- Reward-to-risk target: `1.5 : 1`

### Strategy-Level Risk

- Per-instrument margin <= `30%` of available USDT (notional up to `150%` at 5x)
- Combined BTC + ETH margin <= `60%` of available USDT
- Block new entries if:
  - `ATR[c1] > 3 * mean(ATR[1..30])`
  - requested size exceeds `account max-size`
  - requested size is below `minSz` / `lotSz`
  - instrument `state` is not tradable

### Drawdown Guards

- Soft guard: if strategy drawdown from start equity reaches `4%`
  - stop opening new positions
  - keep existing TP/SL working
- Hard guard: if strategy drawdown from start equity reaches `8%`
  - cancel all strategy algo orders
  - close all strategy positions
  - disable the recurring automation

### Forced Flat Conditions

Immediately flatten and stop automation if any of the following happens:

1. Hard drawdown guard triggered
2. A position is open but no TP/SL algo order can be attached after one retry
3. Position mode is not `net_mode` and the strategy cannot guarantee one-way semantics
4. The exchange rejects leverage / margin / size twice for the same action

### Cooldown Rule

If the same instrument hits stop-loss twice in a rolling 24-hour window, skip that instrument for the next full 1H cycle.

## Communication Requirements

Before the first write, print a confirmation summary and wait for explicit approval:

```text
Strategy: okx-cex-volatility-strategy
Profile: <profile>
Instruments: BTC-USDT-SWAP, ETH-USDT-SWAP
Timeframe: 1H closed bars only
Position mode: net_mode
Margin mode: cross
Available USDT: <balance>
Drawdown guards: soft 4% | hard 8%
Automation: one-shot or recurring 1H host scheduler

Proceed? (yes/no)
```

Per cycle, report:

- instrument
- sentiment direction / score
- signal
- action
- leverage / size %
- TP / SL
- risk flags

If host automation exists, include next run time. If not, explicitly say this was a one-shot cycle.

## Global Notes

- 1H only. Do not substitute 4H or 1D bars for signal generation.
- Closed bars only. Never use `index 0` for signal generation.
- Use `tgtCcy=quote_ccy` only for entry sizing. TP/SL algo size must use actual filled contracts.
- Host scheduling is outside Agent Trade Kit. Prefer the host automation layer; do not hardcode `/schedule` into a portable skill.
- Respond in the user's language. Logs may remain structured / bilingual if that improves clarity.
