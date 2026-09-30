## 2026-09-30T07:06:43Z
You are survey_miner_1, a Specification Miner (teamwork_preview_spec_miner).
Your working directory is: c:\TG\BOT\TRADE DEMO 1\.agents\teamwork\survey_miner_1

MANDATORY FIRST STEP: Read the original user request at:
c:\TG\BOT\TRADE DEMO 1\.agents\teamwork\ORIGINAL_REQUEST.md

Your mission in this Survey phase is to thoroughly probe and document all technical specifications for the required insights:
1. R1: Funding Carry arbitrage (insights/funding-carry.md) - formulas, 4 fee legs (spot buy, swap open short, swap close short, spot sell), net yield calculation, annualization, basis spread, delta-neutrality rules, rebalance thresholds, slippage protection, and OKX demo execution mechanics.
2. R2: Mean Reversion strategy (insights/meanrev-strategy-design.md) - 1H timeframe bars, RSI(14), Bollinger Bands(20, 2), entry conditions (oversold/lower band touch), exit conditions (mean touch/upper band), trailing stop-loss logic, minimal ROI table, risk validation constraints via src/risk.py, and order routing via src/order_router.py.
3. R3: Idle Cash Earn treasury module (insights/idle-cash-earn.md) - free cash calculation (USDT/USDC free of margin and open orders), sweeping threshold, OKX Flexible Earn / Simple Earn API endpoints / commands, interest accrual, on-demand instant redemption protocol for trading margin calls.
4. R4: Bot Fleet 50 expansion (insights/bot-fleet-50.md, src/fleet_manager.py) - full enumeration of the 13 OKX bot types, capital allocation and sizing rules, fleet-level auditing, risk isolation across sleeves, ORDER-OWNER-TAG prefix conventions.

Explore any additional relevant markdown files in `insights/`.
Do NOT write or modify any source code.
Write your comprehensive specification findings to:
c:\TG\BOT\TRADE DEMO 1\.agents\teamwork\survey_miner_1\survey_specs.md
And write your final handoff to:
c:\TG\BOT\TRADE DEMO 1\.agents\teamwork\survey_miner_1\handoff.md

Report back via send_message when done with a summary of key specification rules and formulas.
