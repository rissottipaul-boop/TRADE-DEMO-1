---
name: dca-bot-parameterizer
description: >
  Use this skill when a user wants to set up, configure, or get
  parameter recommendations for a DCA bot on OKX — whether spot or
  contract (perpetual swap). Trigger on any intent to open a DCA bot,
  choose DCA bot settings, size a DCA position, or decide between spot
  vs. contract DCA mode. Also trigger for Chinese phrases like
  "开单dca", "开dca", "帮我开dca", "现货dca", "合约dca". The core value:
  analyzing market state (trend, volatility, structure) to recommend
  safe DCA bot parameters like price step, safety order count,
  take-profit %, and leverage. Do NOT trigger for recurring/scheduled
  buy management (use recurring-dca), standalone technical analysis
  (use kline-indicator), or full trade plan generation (use
  trading-plan-generator).
---

# DCA Bot Parameterizer

## Core Method

Use only these market inputs unless the user explicitly provides more:

- trend: `EMA20` plus price location relative to that average
- volatility: `ATR% = ATR / current price`
- structure:
  - spot DCA or contract `long`: nearest valid support
  - contract `short`: nearest valid resistance

Execute these six steps in order:

1. Define the trading scene
2. Classify market state
3. Measure volatility
4. Inspect structure
5. Generate parameters
6. Validate capital usage and coverage

The order matters because each step feeds the next — classifying market state before
knowing volatility produces trend labels that ignore how far the move has stretched,
and generating parameters before inspecting structure means safety orders may land in
empty air. Stick to EMA20 and ATR% as the default indicators; they capture trend
direction and spacing need without the noise that additional overlays tend to introduce
in DCA sizing decisions.

## Recommendation Gate

When the user expresses intent to open a DCA bot, do not jump straight into
recommendations.

1. Ask whether they want parameter recommendations.
2. If they decline, stop the recommendation flow and state that they can set
   custom parameters themselves.
3. Only continue if they explicitly want recommendations.

The gate result must appear in the final output as `Recommendation Gate Result`.

## Required Inputs

Collect or infer only what is necessary:

- symbol or `instId`
- mode: spot DCA or contract DCA
- asset type: major coin or high-volatility altcoin
- timeframe: `1h`, `4h`, or `1d`
- total budget in `USDT`
- optional risk input:
  - maximum drawdown or target coverage depth
  - explicit low risk tolerance

If the user provides a full OKX instrument ID, use it directly. Otherwise resolve:

- spot: `<BASE>-USDT`
- contract: `<BASE>-USDT-SWAP`

## OKX Trade Kit Data Source

Use OKX Trade Kit market data, not guesswork.

### Fallback Chain

Fetch market data in this order — stop at the first that works:

1. **CLI (primary):** `okx --json market [command]` (okx-trade-cli)
2. **MCP fallback:** `market_get_*` OKX Trade Kit MCP tools
3. **REST API fallback:** `okx.com/api/v5/market/[endpoint]` (read-only, no auth)
4. **Manual input:** Ask the user to provide price, volatility estimate, or structure manually

**CLI commands:**
```bash
okx --json market ticker BTC-USDT
okx --json market indicator ema BTC-USDT --bar 1H --period 20
okx --json market candles BTC-USDT --bar 1H --limit 60
okx --json market instruments --instType SWAP --instId BTC-USDT-SWAP
```

**MCP equivalents (use if CLI unavailable):**
- `market_get_ticker(instId=...)` — current price
- `market_get_indicator(instId=..., indicator="ema", bar=..., period=20)` — EMA20 directly (no manual candle calculation needed)
- `market_get_candles(instId=..., bar=..., limit=60)` — raw candles for ATR calculation (ATR not available via indicator API)
- `market_get_instruments(instType=..., instId=...)` — contract leverage cap metadata

### Candle Windows

Fetch:

- 60 candles on the working timeframe
- 60 candles on the higher timeframe only when contract DCA needs higher-timeframe bias

Use the fixed higher-timeframe mapping:

- `1h -> 4H`
- `4h -> 1D`
- `1d -> 1W`

## Market Classification

Use `EMA20` as the default moving average.
Fetch via `market_get_indicator(instId, indicator="ema", bar, period=20)`.
Use `market_get_candles` only when computing ATR (which requires raw OHLC data).

### Working-Timeframe State

Derive the state from EMA20 (from MCP) and the last 60 candles for ATR and structure.

- `Strong Trend`
  - `EMA20` is clearly rising or falling
  - current price is on the same side of `EMA20`
  - at least 4 of the last 5 closes are on the same side of `EMA20`
  - current price is at least `1 x current ATR` away from `EMA20`

- `Mild Trend`
  - `EMA20` is rising or falling
  - current price is on the same side of `EMA20`
  - at least 3 of the last 5 closes are on the same side of `EMA20`
  - current price is less than `1 x current ATR` away from `EMA20`

- `Range`
  - any setup that does not meet the trend conditions above

State the market as one of: `Range`, `Mild Trend`, `Strong Trend`.

## Contract Direction Logic

Spot DCA stays `long` only.

Contract DCA must output a direction.

### If the Working Timeframe is Trending

Follow the working-timeframe trend:

- price mostly above a rising `EMA20` => `long`
- price mostly below a falling `EMA20` => `short`

### If the Working Timeframe is `Range`

Inspect the next higher timeframe:

- higher timeframe trending up => `long`
- higher timeframe trending down => `short`
- higher timeframe also `Range` => default `long`

When higher-timeframe bias is used, include it in the final output as `Higher Timeframe Bias`.

## Structure Logic

Use structure only after trend and volatility are known.

- spot DCA and contract `long`
  - use the nearest valid support
  - prefer the most recent visible swing low below current price from the last 20 closed candles
  - if no clean swing low exists, fall back to the lowest low of the last 20 closed candles

- contract `short`
  - use the nearest valid resistance
  - prefer the most recent visible swing high above current price from the last 20 closed candles
  - if no clean swing high exists, fall back to the highest high of the last 20 closed candles

Check:

- whether current price is reasonably close to the relevant structure
- whether planned safety orders land near meaningful structure instead of far beyond it

## Volatility Logic

Use only `ATR%`.

- current `ATR%` drives `pxSteps`
- current `ATR%` plus trading cost drives `tpPct`

For contract leverage, also compute a volatility regime on the working timeframe by
comparing current `ATR%` with the 20-bar average `ATR%`:

- `Low`: current `ATR% < 0.9x` average `ATR%`
- `Normal`: current `ATR%` is `0.9x` to `1.2x` average `ATR%`
- `High`: current `ATR% > 1.2x` average `ATR%`

## Unsafe Setup Gate

Apply this gate before giving parameters for a poor setup.

- spot DCA in `Strong Trend`: warn first and stop unless the user explicitly insists
- contract DCA in `Strong Trend`: warn first and stop unless the user explicitly insists, even if direction follows trend

Accept clear confirmations such as: `continue`, `continue anyway`, `继续`, `继续执行`,
or equivalent explicit insistence.

If the user insists on continuing in `Strong Trend`, switch to defensive mode:

- reduce `initOrdAmt`
- widen `pxSteps`
- increase `pxStepsMult`
- lower `volMult`
- tighten capital-usage and coverage checks
- for contract DCA, set recommended leverage to `1x`

## Parameter Rules

These rules reflect how DCA bots fail in practice. Safety orders that are too
tight fill rapidly into a continuing trend; orders that are too loose waste
capital and miss the rebound. The goal is spacing that absorbs normal volatility
without committing the full budget to a runaway move.

### `pxSteps`

- set from current `ATR%`
- in `Range`, keep it near current `ATR%`
- in `Mild Trend`, set it slightly above current `ATR%`
- in `Strong Trend` after explicit confirmation, widen it clearly above `Mild Trend`
- widen it further for high-volatility altcoins

### `pxStepsMult`

- low in `Range`
- medium in `Mild Trend`
- high in `Strong Trend` only after explicit confirmation

### `initOrdAmt`

Base it on entry quality, not a formula.

- larger when the setup is ranging, structure is near, and volatility is not elevated
- smaller when the setup is trending, structure is far, or volatility is elevated

### `safetyOrdAmt` and `volMult`

- keep `safetyOrdAmt` close to `initOrdAmt` in most cases
- use a higher `volMult` in `Range`
- use a lower `volMult` in trend conditions
- keep `volMult` restrained in defensive mode

### `maxSafetyOrds`

Derive this last, after spacing and amounts are drafted. The reason: choosing a
count first tempts over-allocation — picking a number of orders and then
reverse-engineering amounts to fit the budget. Instead:

1. Draft `pxSteps`, `pxStepsMult`, `initOrdAmt`, `safetyOrdAmt`, and `volMult`
2. Simulate cumulative coverage: at each safety order level, compute the price
   distance from entry and the running capital consumed
3. Stop adding orders when either condition is met:
   - cumulative capital reaches the budget
   - coverage depth reaches the target drawdown or passes the relevant structure
4. The count at that point is `maxSafetyOrds`

Keep only as many safety orders as the budget and target coverage allow.

### `tpPct`

- derive it from current `ATR%` and trading cost
- the minimum floor is `ATR% × 0.3` to clear OKX fees and slippage
  (spot taker ~0.1%, futures taker ~0.05%; see [references/parameter-bands.md](references/parameter-bands.md) for the exact floor formula)
- keep it realistic for the timeframe's usual rebound size

### Contract Leverage

Recommend leverage only. Do not read or modify account leverage settings.

Use the exact leverage matrix from [references/parameter-bands.md](references/parameter-bands.md),
then cap it by:

- the OKX instrument maximum leverage
- an internal hard cap of `4x`

If the user explicitly states low risk tolerance or a tight maximum drawdown, reduce
the recommendation by one leverage step, with a floor of `1x`.

## Validation

Always validate:

- total capital usage if every safety order fills
- total downside coverage from entry to the final safety order
- whether the final safety order lands too far beyond the relevant structure
- whether the setup still fits the user's stated risk tolerance

If validation fails:

- increase `maxSafetyOrds` or `pxStepsMult` when coverage is too shallow
- increase `pxSteps` when the bot would fill too easily
- reduce `volMult` or `maxSafetyOrds` when capital usage is too heavy

## Edge Case Handling

### Missing or Invalid Symbol

If the user does not provide a trading pair:

1. Ask explicitly: "Which trading pair? (e.g., BTC-USDT for spot, BTC-USDT-SWAP for contract)"
2. Do not proceed without a valid symbol.

### Invalid Trading Pair (Not Found on OKX)

1. Attempt `Call OKX Trade MCP market_get_ticker(instId="<symbol>")` with the provided symbol.
2. If the MCP call fails, retry with `okx --json market ticker <symbol>` (CLI fallback).
3. If both fail, inform the user: "Trading pair not found on OKX. Please verify the symbol."
4. Do not guess or offer alternative symbols.

### Budget Too Small

Recommended minimum budgets:

- Spot DCA: $50 USDT (allows at least 5 orders at ~$10 each)
- Contract DCA: $100 USDT (allows leverage and position sizing)

If the provided budget falls below the minimum, ask: "Your budget of $X is very small.
Minimum recommended for this setup is $Y. Continue with smaller positions?"

### Strong Trend Forced Continuation with High Volatility

When the user insists on continuing in a Strong Trend AND current volatility (ATR%) is elevated:

- Cap leverage to `1x` (no negotiation).
- Widen `pxSteps` by at least 1.5x the current `ATR%`.
- Set `volMult` to `1.0` or lower (no escalation).
- Reduce `initOrdAmt` to 5% of budget (vs. normal 8-12%).
- Output warning: "This is a highly defensive configuration. Safety orders may not fill if the trend continues."

### Contract Leverage Cap Exceeded

When the user requests leverage or the matrix suggests leverage higher than the OKX instrument maximum:

1. Query `Call OKX Trade MCP market_get_instruments(instType="SWAP", instId="<symbol>")`.
2. Extract the `maxLever` field from the response.
3. Cap the recommended leverage to `min(matrix_recommendation, maxLever, 4x)`.
4. Output note: "Recommended leverage capped to $X (instrument maximum: $X, internal cap: 4x)."

## Output Format

Produce the final answer in this order:

1. `Scene`
2. `Recommendation Gate Result`
3. `Market State`
4. `Higher Timeframe Bias` (when used)
5. `Volatility`
6. `Structure`
7. `Direction` (contract DCA only)
8. `Decision`
9. `Warning` (when needed)
10. `Recommended Leverage` (contract DCA only)
11. `Parameters`
12. `Validation`

## Reference

For numeric parameter bands, the contract leverage matrix, the tpPct fee floor,
and the validation checklist, read [references/parameter-bands.md](references/parameter-bands.md).
