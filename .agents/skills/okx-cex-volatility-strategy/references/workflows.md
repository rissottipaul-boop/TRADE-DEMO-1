# Strategy Workflows

Detailed operating workflows for `okx-cex-volatility-strategy`.

---

## Workflow 1: One-Cycle Evaluate & Execute

This is the core unit of work. Run it once per instrument group on each 1H cycle.

### Step 0: Preflight and Profile Resolution

```text
1. okx upgrade
2. okx --version
3. okx config show
4. Resolve the actual configured profile name
5. okx --profile <profile> account config --json
6. okx --profile <profile> account balance USDT --json
```

Rules:

- Never assume profile names such as `demo` / `live`
- Do not proceed if `posMode != net_mode`
- Balance readability does not guarantee trade permission

---

### Step 1: Collect Market Data

Repeat for each instrument: `BTC-USDT-SWAP`, `ETH-USDT-SWAP`

```text
tag="CLI"
okx market instruments --instType SWAP --instId <instId> --json
okx market candles <instId> --bar 1H --limit 31 --json
okx market indicator ema <instId> --bar 1H --params 20,60 --list --limit 3 --json
okx market indicator rsi <instId> --bar 1H --params 14 --list --limit 3 --json
okx market indicator macd <instId> --bar 1H --params 12,26,9 --list --limit 3 --json
okx market indicator bb <instId> --bar 1H --params 20,2 --list --limit 31 --json
okx market indicator atr <instId> --bar 1H --params 14 --list --limit 31 --json
```

```text
tag="MCP"
market_get_instruments({instType:"SWAP", instId:"<instId>"})
market_get_candles({instId:"<instId>", bar:"1H", limit:31})
market_get_indicator({instId:"<instId>", indicator:"ema", bar:"1H", params:[20,60], returnList:true, limit:3})
market_get_indicator({instId:"<instId>", indicator:"rsi", bar:"1H", params:[14], returnList:true, limit:3})
market_get_indicator({instId:"<instId>", indicator:"macd", bar:"1H", params:[12,26,9], returnList:true, limit:3})
market_get_indicator({instId:"<instId>", indicator:"bb", bar:"1H", params:[20,2], returnList:true, limit:31})
market_get_indicator({instId:"<instId>", indicator:"atr", bar:"1H", params:[14], returnList:true, limit:31})
```

Closed-bar rules:

- use `c1 = index 1`
- use `c2 = index 2`
- use closed window `1..30`
- never use `index 0` for signal generation

---

### Step 2: Collect Account / Position State

Repeat for each instrument:

```text
tag="CLI"
okx --profile <profile> account balance USDT --json
okx --profile <profile> account max-size --instId <instId> --tdMode cross --json
okx --profile <profile> swap positions --instId <instId> --json
okx --profile <profile> swap algo orders --instId <instId> --json
```

```text
tag="MCP"
account_get_balance({ccy:"USDT"})
account_get_max_size({instId:"<instId>", tdMode:"cross"})
swap_get_positions({instId:"<instId>"})
swap_get_algo_orders({instId:"<instId>", status:"pending"})
```

---

### Step 3: Build Sentiment and Decision Payload

Use the formulas from `strategy-params.md`.

Expected output per instrument:

```json
{
  "instId": "BTC-USDT-SWAP",
  "sentimentDirection": "bullish",
  "sentimentScore": 65,
  "volatilityRegime": "normal_vol",
  "tradeBlocked": false,
  "signal": "long",
  "action": "open_long",
  "leverage": 4,
  "sizePct": 12,
  "sizeMode": "quote_ccy",
  "tp": "102000",
  "sl": "96000",
  "riskFlags": []
}
```

Decision matrix:

| Current position | Signal | Action |
|---|---|---|
| None | Long | `open_long` |
| None | Short | `open_short` |
| None | None | `skip` |
| Long | Long | `hold` |
| Short | Short | `hold` |
| Long | Short | `reverse_to_short` |
| Short | Long | `reverse_to_long` |
| Any | Hard risk | `close_only` |

---

### Step 4: Execute

#### Open Long / Short

```text
tag="CLI"
okx --profile <profile> swap leverage --instId <instId> --lever <leverage> --mgnMode cross
okx --profile <profile> swap place --instId <instId> --side <buy|sell> --ordType market --sz <entry_usdt> --tgtCcy quote_ccy --tdMode cross
okx --profile <profile> swap positions --instId <instId> --json
okx --profile <profile> swap algo place --instId <instId> --side <sell|buy> --ordType oco --sz <filled_contracts> --tdMode cross --tpTriggerPx <tp> --tpOrdPx=-1 --slTriggerPx <sl> --slOrdPx=-1
okx --profile <profile> swap algo orders --instId <instId> --json
```

```text
tag="MCP"
swap_set_leverage({instId:"<instId>", lever:"<leverage>", mgnMode:"cross"})
swap_place_order({instId:"<instId>", tdMode:"cross", side:"<buy|sell>", ordType:"market", sz:"<entry_usdt>", tgtCcy:"quote_ccy"})
swap_get_positions({instId:"<instId>"})
swap_place_algo_order({instId:"<instId>", tdMode:"cross", side:"<sell|buy>", ordType:"oco", sz:"<filled_contracts>", tpTriggerPx:"<tp>", tpOrdPx:"-1", slTriggerPx:"<sl>", slOrdPx:"-1"})
swap_get_algo_orders({instId:"<instId>", status:"pending"})
```

#### Reverse

```text
tag="CLI"
okx --profile <profile> swap algo orders --instId <instId> --json
okx --profile <profile> swap algo cancel --instId <instId> --algoId <algoId>
okx --profile <profile> swap close --instId <instId> --mgnMode cross
okx --profile <profile> swap positions --instId <instId> --json
# only after flat:
okx --profile <profile> swap leverage --instId <instId> --lever <leverage> --mgnMode cross
okx --profile <profile> swap place --instId <instId> --side <buy|sell> --ordType market --sz <entry_usdt> --tgtCcy quote_ccy --tdMode cross
okx --profile <profile> swap positions --instId <instId> --json
okx --profile <profile> swap algo place --instId <instId> --side <sell|buy> --ordType oco --sz <filled_contracts> --tdMode cross --tpTriggerPx <tp> --tpOrdPx=-1 --slTriggerPx <sl> --slOrdPx=-1
```

```text
tag="MCP"
swap_get_algo_orders({instId:"<instId>", status:"pending"})
swap_cancel_algo_orders({orders:[{"algoId":"<algoId>","instId":"<instId>"}]})
swap_close_position({instId:"<instId>", mgnMode:"cross"})
swap_get_positions({instId:"<instId>"})
# only after flat:
swap_set_leverage({instId:"<instId>", lever:"<leverage>", mgnMode:"cross"})
swap_place_order({instId:"<instId>", tdMode:"cross", side:"<buy|sell>", ordType:"market", sz:"<entry_usdt>", tgtCcy:"quote_ccy"})
swap_get_positions({instId:"<instId>"})
swap_place_algo_order({instId:"<instId>", tdMode:"cross", side:"<sell|buy>", ordType:"oco", sz:"<filled_contracts>", tpTriggerPx:"<tp>", tpOrdPx:"-1", slTriggerPx:"<sl>", slOrdPx:"-1"})
```

Safety rules:

- verify after every write
- use actual filled contracts for TP/SL algo size
- if TP/SL attach fails after one retry, flatten immediately

---

### Step 5: Log the Cycle

Recommended log shape:

```text
[Cycle 2026-04-08 12:05 UTC] BTC-USDT-SWAP
  Sentiment: bullish (score=65)
  Signal: LONG
  Action: open_long
  Volatility: normal_vol -> leverage=4x size=12%
  Entry: 97,500
  TP: 102,000
  SL: 94,200
  Risk flags: none
  Status: EXECUTED
```

If skipped:

```text
[Cycle 2026-04-08 12:05 UTC] ETH-USDT-SWAP
  Sentiment: neutral (score=10)
  Action: skip
  Reason: no strict entry signal on last closed 1H bar
```

---

## Workflow 2: Start Strategy

1. Resolve the actual configured profile name
2. Confirm `net_mode` and fetch available USDT
3. Show the strategy summary and wait for explicit approval
4. Record start equity for drawdown guards
5. Run one immediate cycle using the last closed 1H bar
6. If the host supports recurring automation, schedule the cycle to run shortly after each 1H close

Recommended recurring timing:

- every hour at `HH:05`

Use a small post-close buffer so the new 1H candle is definitely closed before evaluation.

Important:

- Host scheduling is not part of Agent Trade Kit itself
- Prefer the host automation layer
- If no automation layer exists, clearly say the strategy was started in one-shot mode only

---

## Workflow 3: Stop Strategy

1. Disable the host automation
2. Ask whether open positions should be closed now
3. If yes:

```text
tag="CLI"
okx --profile <profile> swap algo orders --instId BTC-USDT-SWAP --json
okx --profile <profile> swap algo cancel --instId BTC-USDT-SWAP --algoId <algoId>
okx --profile <profile> swap close --instId BTC-USDT-SWAP --mgnMode cross
```

```text
tag="MCP"
swap_get_algo_orders({instId:"BTC-USDT-SWAP", status:"pending"})
swap_cancel_algo_orders({orders:[{"algoId":"<algoId>","instId":"BTC-USDT-SWAP"}]})
swap_close_position({instId:"BTC-USDT-SWAP", mgnMode:"cross"})
```

4. If no:
   - leave positions and existing TP/SL algos intact
   - report that automation has stopped but exchange-native TP/SL is still active

---

## Workflow 4: Strategy Status

Run the following:

```text
tag="CLI"
okx --profile <profile> account balance USDT --json
okx --profile <profile> swap positions --json
okx --profile <profile> swap algo orders --instId BTC-USDT-SWAP --json
okx --profile <profile> swap algo orders --instId ETH-USDT-SWAP --json
okx market mark-price --instType SWAP --instId BTC-USDT-SWAP --json
okx market mark-price --instType SWAP --instId ETH-USDT-SWAP --json
okx market indicator rsi BTC-USDT-SWAP --bar 1H --params 14 --list --limit 3 --json
okx market indicator rsi ETH-USDT-SWAP --bar 1H --params 14 --list --limit 3 --json
```

Recommended status table:

```text
Instrument      | Side  | Size  | Entry    | Mark     | uPnL     | TP       | SL       | Sentiment
BTC-USDT-SWAP   | LONG  | 0.03  | 97,500   | 98,100   | +18 USDT | 102,000  | 94,200   | bullish 65
ETH-USDT-SWAP   | --    | --    | --       | 3,240    | --       | --       | --       | neutral 10
```

Additional lines:

- drawdown vs start equity
- next run time if automation exists
- last cycle action summary
