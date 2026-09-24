---
name: sol-rebound-short
description: SOL-USDT-SWAP 阶梯做空策略 / SOL-USDT-SWAP laddered short strategy.
---

# SOL Rebound Short

## What This Skill Does / 这个 Skill 是做什么的

`SOL Rebound Short` is a rules-based OKX perpetual futures skill for `SOL-USDT-SWAP` that turns a discretionary short-recovery idea into an agent-readable strategy.

`SOL Rebound Short` 是一个面向 `SOL-USDT-SWAP` 的 OKX 永续合约规则化做空 Skill，用来把原本偏主观的空头回补思路，整理成 Agent 可以理解、执行和复盘的策略框架。

It is designed for traders who want to:
- start with a small short position,
- add only when adverse movement hits predefined thresholds,
- reduce rescue layers on rebound,
- take profit in fixed increments,
- and keep account-level risk bounded with explicit capital and drawdown limits.

它适合这样的交易者：
- 先用小仓位试空，
- 只有在不利波动达到阈值时才分层加仓，
- 在价格回弹时优先减掉补仓层，
- 用固定利润目标逐步止盈，
- 同时用明确的资金和回撤限制，把风险控制在账户可承受范围内。

This is **not** a hype skill for showing only winning screenshots. It is a structured strategy skill focused on **repeatability, bounded risk, and reviewability**.

它**不是**那种只会展示盈利截图的“包装型 Skill”，而是一个强调**可重复、风险有边界、并且方便复盘**的结构化交易策略 Skill。

## Why It Is Different / 它和普通补仓策略有什么不同

Compared with uncontrolled averaging or martingale-style shorting, this skill adds visible risk boundaries:

和常见的无上限补仓、马丁式做空相比，这个 Skill 明确加入了可见的风险边界：

- use only **50%** of current account equity as strategy capital
- cap total position size at **50%** of the account's current maximum openable quantity
- stop trading after **20% daily drawdown**
- avoid new exposure in the funding blackout window

- 只使用当前账户权益的 **50%** 作为策略资金
- 最大总仓位限制为当前账户可开最大仓位的 **50%**
- 日内回撤达到 **20%** 后停止交易
- 在资金费率结算前后黑窗期内不新增风险敞口

In one line:

**small starter short -> controlled adverse adds -> rebound de-risk -> fixed profit harvesting -> dynamic size cap -> daily circuit breaker**

一句话概括：

**小仓试空 -> 逆向受控加仓 -> 反弹去风险 -> 固定利润收割 -> 动态仓位上限 -> 日内熔断保护**

## Best-fit Market Conditions / 适用行情

Use this skill when SOL is in:
- high-volatility range conditions
- failed breakout / fade structures
- weak rallies with repeated rejection
- noisy short-term mean-reversion environments

这个 Skill 更适合以下 SOL 行情：
- 高波动震荡
- 假突破后的回落结构
- 反弹无力、反复受压的弱势行情
- 短周期有噪音但存在均值回归特征的环境

Avoid presenting it as ideal for strong one-way squeeze markets.

不要把它包装成适合单边强逼空行情的策略，那不是它擅长的环境。

## Risk Warning / 风险提示

This is still a recovery-style averaging model. In strong one-way rebound or squeeze conditions, repeated adds can amplify drawdown. The purpose of this skill is not to eliminate losses, but to keep them **bounded, observable, and reviewable**.

本质上它仍然属于一种“回补型、受控补仓”策略。在单边强反弹或逼空环境中，连续加仓仍可能放大回撤。这个 Skill 的目标不是消灭亏损，而是让亏损保持在**有边界、可观察、可复盘**的范围内。

## Live-account Example Snapshot / 真实账户案例摘要

A recent authorized account example showed:

近期一个已授权账户样例结果如下：

- Account equity: `102.3302 USDT`
- Strategy capital: `51.1651 USDT`
- Max position in cycle: `4 SOL`
- Realized PnL: `-1.5145 USDT`
- Strategy ROI: `-2.9601%`
- Account impact: `-1.4800%`

- 账户权益：`102.3302 USDT`
- 策略资金：`51.1651 USDT`
- 本轮最大持仓：`4 SOL`
- 已实现收益：`-1.5145 USDT`
- 策略资金收益率：`-2.9601%`
- 对账户总权益影响：`-1.4800%`

Why include a losing example up front? Because it proves the point of the skill: this strategy can lose in adverse conditions, but the combination of sizing caps, capital allocation rules, and circuit-breaker logic can keep damage relatively contained at the account level.

为什么要把一个亏损案例放在前面？因为这恰恰能说明这个 Skill 的核心价值：它在不利行情下仍然可能亏损，但通过仓位上限、资金占比和熔断机制，能够把账户层面的损伤控制在相对有限的范围内。

## Strategy Profile

- Exchange: `okx`
- Instrument: `SOL-USDT-SWAP`
- Account mode: `cross`
- Position mode: `long_short_mode`
- Trade direction: `short`
- Signal filter enabled: `true`
- Night trading: `true`
- Dry run: `false` by default

## Core Logic

1. Open an initial short with a small base position.
2. If floating loss reaches the configured loss threshold and price has moved far enough from the previous add level, add one unit to improve average entry.
3. Cap total position size at no more than 50% of the maximum quantity the current account can open.
4. When price reverts toward average entry, reduce the newest added layer first, then the next layer, to de-risk exposure.
5. Take profit in fixed per-SOL increments instead of waiting for one large move.
6. Stop trading for the day when daily drawdown reaches 20% or failure conditions are triggered.

## Trading Parameters

### Leverage and Risk

- Exchange max leverage allowed by setup: `50x`
- Strategy-side risk leverage cap: `8x`

Always obey the lower effective leverage implied by risk controls and capital allocation, even if the exchange allows higher leverage.

### Capital

- Strategy capital budget: `50%` of current account equity
- Reserve capital: remaining `50%` of current account equity

Interpretation:

- Use only half of current account equity for this strategy.
- Keep the other half as reserve and untouched by default.
- Recompute the strategy budget dynamically from current account state instead of assuming a fixed USDT balance.

### Position Sizing

- Base position: small starter short
- Add unit: fixed incremental layer per add event
- Max position: `50%` of the maximum quantity the current account can open under current margin and instrument constraints

Interpretation:

- Do not use a hard-coded max position size such as `4 SOL`.
- Query current account buying power, leverage limits, and instrument constraints.
- Derive an account-level maximum openable quantity.
- Limit this strategy to at most `50%` of that maximum openable quantity.

## Entry and Add Rules

## Initial Entry

Open the first short only if the upstream signal filter permits it.

Because `signal_filter_enabled: true`, do not blindly enter on every price tick. Require an external or precomputed bearish filter before opening the base position.

## Add Conditions

Add exactly `1 SOL` only when **all** of the following are true:

- Current floating loss reaches at least `13 USDT`
- Price has dropped at least `2.5%` from the last add reference level
- Current total position is below the strategy cap of `50%` of current account max openable quantity
- No circuit breaker is active
- Current time is outside the funding blackout window

If any condition fails, do not add.

## Profit-Taking and De-Risking

### Fixed Profit Objective

- Target profit: `1.0 USDT` per `1 SOL`

Interpretation:

- Harvest gains in small, repeatable increments.
- Do not wait for outsized trend extension if the fixed target is reached.

### Rebound De-Risk Rules

When price rebounds toward the average entry price of the current short stack:

1. If mark price reaches `avg_price + 0.2`, reduce the most recent add layer.
2. If mark price reaches `avg_price + 1.0`, reduce the next add layer.

Execution intent:

- Remove newest risk first.
- Shrink exposure as the trade recovers toward breakeven.
- Preserve the core position longer than the latest rescue adds.

Map the provided actions as:

- `reduce_last_add_6` -> reduce the newest add tranche
- `reduce_next_add_6` -> reduce the second-newest add tranche

If implementation needs exact quantity mapping, default to reducing one add unit per rule unless the exchange lot size requires rounding.

## Circuit Breakers

Stop new trading activity when any of the following happens:

- Daily strategy drawdown reaches `20%`
- Position is already at the strategy max size and market breaks the defined support level
- Two recovery attempts fail

### Implementation Notes

Because `break support` and `failed recovery` need market-structure interpretation, apply these conservative defaults if no richer signal model is supplied:

- **Break support**: price closes below the most recent local support zone identified before the latest add
- **Failed recovery**: after an add, price fails to rebound enough to trigger a de-risk rule and then continues against the position to a new adverse threshold

When either structural rule is ambiguous, choose the safer path and stop adding.

## Funding Window Controls

Suspend opening and adding around funding time:

- Block new risk `5` minutes before funding
- Block new risk `5` minutes after funding

Inside the blackout window:

- Do not open a new base short
- Do not add to an existing short
- Allow risk reduction and take-profit exits

## Operational Checklist

Before execution:

1. Confirm instrument is `SOL-USDT-SWAP`
2. Confirm account mode is `cross`
3. Confirm position mode supports independent short exposure
4. Confirm available strategy capital is `50%` of current account equity
5. Confirm effective leverage does not exceed the risk cap intent
6. Confirm no funding blackout is active
7. Confirm no daily circuit breaker is active
8. Confirm bearish signal filter is valid

During execution:

1. Track every layer separately
2. Recompute average entry after each add or reduction
3. Evaluate add rules only on adverse movement
4. Evaluate de-risk rules continuously on rebound
5. Stop immediately when circuit-breaker conditions are met

After execution:

1. Record entries, adds, reductions, exits, and reasons
2. Record daily PnL and circuit-breaker triggers
3. Review whether adds improved exit quality or merely increased drawdown

## Default Structured Config

```yaml
exchange: okx
instrument: SOL-USDT-SWAP
account_mode: cross
position_mode: long_short_mode
trade_direction: short
signal_filter_enabled: true
max_leverage: 50
risk_leverage_cap: 8
strategy_capital_ratio: 0.5
reserve_capital_ratio: 0.5
max_position_ratio_of_account_openable_qty: 0.5
base_position_mode: small_starter
add_unit_mode: fixed_increment
take_profit_per_sol_usdt: 1.0
add_trigger_floating_loss_usdt: 13
add_trigger_drop_from_last_add_pct: 2.5
derisk_rules:
  - trigger: "avg_price + 0.2"
    action: "reduce_last_add_6"
  - trigger: "avg_price + 1.0"
    action: "reduce_next_add_6"
daily_circuit_breaker_drawdown_pct: 20
stop_when_max_pos_and_break_support: true
stop_when_two_failed_recoveries: true
night_trading: true
funding_blackout_before_min: 5
funding_blackout_after_min: 5
dry_run: false
```

## What to Tell the User

When presenting this strategy, summarize it plainly:

- Bias: short SOL perpetuals
- Style: layered averaging into adverse movement
- Exit: small fixed take-profit plus rebound de-risk
- Main risk: averaging into a continued squeeze
- Main protection: dynamic 50% size cap, funding blackout, and daily 20% drawdown stop logic

## Executable Parameter Schema

Use `references/executable-schema.json` as the runtime config contract.

Execution engines should resolve the following live values before placing or adjusting any order:

- current account equity
- current instrument max openable quantity
- current leverage setting and exchange constraints
- current average entry and active layer state
- realized and unrealized daily PnL
- upcoming funding timestamp and blackout window

Recommended execution mapping:

1. Query equity via `okx account balance`
2. Query max available/openable size via `okx account max-size` or `okx account max-avail-size`
3. Query positions via `okx swap positions` or `okx account positions`
4. Place or reduce short exposure via `okx swap place`, `okx swap amend`, and `okx swap close`
5. Compute ROI outputs after fills using the case-study formulas in `references/case-study-template.md`

## Marketplace Publishing Copy

Use `references/publish-copy.md` for bilingual marketplace copy.

## References

- `references/strategy-spec.md` — compact machine-readable spec and publishing notes
- `references/executable-schema.json` — executable runtime config schema
- `references/publish-copy.md` — Chinese and English marketplace copy
- `references/case-study-template.md` — ROI logic and case-study template
