---
name: okx-600u-pnl-percent-v1
description: Yield-focused OKX Agent Trade Kit strategy for a 600 USDT account, trading only BTC and ETH USDT perpetuals with single-position risk control, strict stop loss, and skip-first execution logic.
---

# 600U Yield Ranking Strategy V1.0

## Objective

Use a 600 USDT trading account to pursue the highest possible return rate during the competition while prioritizing controlled drawdown, stable equity growth, and disciplined execution.

This strategy allows selective aggression but does not allow undisciplined high-risk bets.

## Allowed Market

Trade only the following USDT perpetual swap instruments:

- BTC-USDT-SWAP
- ETH-USDT-SWAP

Default priority: Prefer BTC-USDT-SWAP by default. Only switch to ETH-USDT-SWAP when ETH is clearly stronger than BTC.

## Position Constraints

- Only 1 open position at any time
- Never hold multiple instruments simultaneously
- No hedging
- No simultaneous long and short

## Execution Cadence

- Run a full decision cycle every 4 hours when there is no open position
- If there is an open position, run a position health check every 1 hour
- If the market is choppy or noisy, explicitly output `本轮跳过`

# Compliance Requirements

All qualifying trades must be executed through OKX official Agent Trade Kit tools.

Do not:

- place orders manually through the OKX app or web UI
- use third-party trading tools
- directly place orders through external API scripts outside the official Agent Trade Kit execution path

Trade only USDT perpetual swaps that qualify for the competition.

# Step 1 - Market Data Collection

For both BTC-USDT-SWAP and ETH-USDT-SWAP, collect:

- 4H candles, latest 200 bars
- 1H candles, latest 120 bars

For each instrument, calculate and record:

- EMA20
- EMA60
- RSI(14)
- ATR(14)
- average volume of the latest 20 candles
- high/low structure of the latest 5 candles
- current price deviation from EMA20

# Step 2 - Sentiment and Crowding Data

For both BTC-USDT-SWAP and ETH-USDT-SWAP, collect:

- current funding rate
- current open interest
- recent open-interest change, preferably 1H and 3H change when available

Focus on:

- whether the trend is supported by open interest
- whether funding is healthy or overcrowded
- whether ATR is too high
- whether the market is in a high-volatility chaotic state or a choppy stop-hunt environment

# Step 3 - AI Reasoning and Final Decision

You must reason first and then decide. Do not execute a purely mechanical rule script.

For every cycle, explicitly determine:

1. Current market regime:
   - uptrend
   - downtrend
   - range
   - high-volatility chaos

2. Which instrument is more tradable, BTC or ETH:  
Compare at least:
   - 4H EMA20 vs EMA60 relationship
   - EMA20 and EMA60 slope
   - whether 1H and 4H align in the same direction
   - whether current price is in a reasonable entry zone instead of an extended chase
   - whether funding rate is healthy
   - whether open-interest change supports continuation

3. Whether a new position is allowed:  
A new position is allowed only if all of the following are true:
   - 4H trend is clear
   - 1H and 4H point in the same direction
   - current price is not obviously extended away from EMA20
   - ATR remains in an acceptable range
   - funding is not obviously overcrowded
   - open-interest change supports continuation

4. If any required condition is missing, you must output `本轮跳过` and explain why.

5. Final output must be exactly one of:
   - 开多
   - 开空
   - 持仓继续
   - 减仓
   - 平仓
   - 本轮跳过

# Step 4 - Direction Rules

Default bias:
- Prefer trend-following longs

Short positions are allowed only when all of the following are true:
- 4H EMA20 is clearly below 4H EMA60
- both EMA20 and EMA60 are sloping down
- 1H structure is also weakening
- open interest supports downside continuation
- funding does not show an extreme opposite-side crowding trap
- price is not already at an oversold exhaustion point

If the short setup is incomplete, do not short.

# Step 5 - Position Sizing and Risk

Treat 600 USDT as the account base capital for risk decisions.

There are 3 execution modes:

## Default Mode

Use for normal high-quality setups:
- max loss per trade: 9 USDT
- target notional: 700 to 900 USDT
- leverage: 2x to 3x

## Strong Mode

Use only when trend, structure, funding, and open interest all strongly align:
- max loss per trade: 12 USDT
- target notional: 900 to 1200 USDT
- leverage: 3x to 4x
- never exceed 4x leverage

## Defensive Mode

Use after losses or when protecting existing gains:
- max loss per trade: 6 USDT
- target notional: 400 to 600 USDT
- leverage: 2x

Additional limits:
- only 1 position at a time
- never add to a losing position
- never flip aggressively after a loss
- if the previous trade was a loss, reduce the next position size by 30%
- for the same instrument, attempt at most 2 new entries within 24 hours

# Step 6 - Stop Loss Rules

Immediately after opening a position, place a stop-loss order through the official trading workflow.

Stop-loss logic:
- prioritize technical structure first
- also reference ATR
- final stop must keep the trade risk within the mode's maximum loss limit

For long positions:
- stop loss should normally be around 0.9 to 1.1 ATR below entry on 4H context
- or slightly below the most recent valid pullback low
- choose the more reasonable level that also respects the risk cap

For short positions:
- stop loss should normally be around 0.9 to 1.1 ATR above entry on 4H context
- or slightly above the most recent valid rebound high
- choose the more reasonable level that also respects the risk cap

If stop distance is too large:
- reduce position size first
- if risk still cannot be controlled properly, skip the trade

# Step 7 - Take Profit and Position Management

This strategy protects return rate first, then seeks continuation profit.

Management rules:
1. When unrealized profit reaches 1R, reduce 30% of the position and move stop loss close to breakeven
2. When unrealized profit reaches 2R, reduce another 30%
3. Let the remaining position follow the trend
4. If 1H structure breaks, prioritize exiting the remaining position
5. If the 4H trend clearly weakens, exit proactively
6. If price moves too fast and funding becomes crowded, lock profit proactively
7. If the market enters a high-volatility chaotic state, protect profits and avoid overstaying

# Step 8 - Drawdown Control and Cooldown Rules

Strictly enforce:
- if daily cumulative loss reaches 18 USDT, stop all new entries for the rest of the day
- if account equity falls 36 USDT from the competition-period peak, enter defensive mode for the next 24 hours
- after 2 consecutive losing trades, pause new trading for 12 hours
- after 3 consecutive losing trades, pause new trading for 24 hours
- if the market is clearly a choppy stop-hunt environment, remain flat

# Step 9 - Return-Rate Priority Principles

- do not force trades just to increase trade count
- do not overuse leverage just to chase nominal profit
- do not low-quality churn trade for volume
- avoid additional account funding during the competition unless absolutely necessary
- only trade high-quality opportunities
- trade less, but make each trade have a clear reason and acceptable risk-reward quality

# Step 10 - Execution Requirements

A new order may be placed only when Step 3 concludes that execution is valid.

Every opening order must include:
- instrument ID
- side
- market order type
- size
- official Agent Trade Kit trade attribution when supported, including `agentTradeKit` tag or equivalent official attribution field

Immediately after entry:
- place a stop-loss order
- do not leave a new position without protection

If there is already an open position:
- do not open a second position
- only decide among `持仓继续`, `减仓`, or `平仓`

# Hard Prohibitions

Never do any of the following:
- manual override trades outside the strategy
- spot trading
- options trading
- coin-margined contracts
- multiple simultaneous positions
- hedging
- revenge trading
- adding to losers
- trading when conditions are unclear
- placing a trade when the reasoning does not support it

# Final Principle

Your job is not to trade every cycle.

When the market is clear, act decisively.  
When the market is messy, stay flat.  
When profit already exists, protect return rate first.  
Drawdown control has higher priority than offense.