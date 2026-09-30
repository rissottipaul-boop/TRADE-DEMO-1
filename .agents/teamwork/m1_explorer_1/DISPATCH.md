## 2026-09-30T07:19:37Z

You are m1_explorer_1, an Explorer (teamwork_preview_explorer) for Milestone 1: Funding Carry Arbitrage (R1).
Your working directory is: c:\TG\BOT\TRADE DEMO 1\.agents\teamwork\m1_explorer_1

MANDATORY FIRST STEP: Read the original user request at:
c:\TG\BOT\TRADE DEMO 1\.agents\teamwork\ORIGINAL_REQUEST.md
Also read the project architecture at:
c:\TG\BOT\TRADE DEMO 1\.agents\teamwork\orchestrator_1\PROJECT.md
c:\TG\BOT\TRADE DEMO 1\.agents\teamwork\survey_miner_1\survey_specs.md

Your mission:
Design the mathematical and analytical core for `src/funding_carry.py`:
1. 4-leg fee calculations: Spot Buy, Swap Short Open, Swap Short Close, Spot Sell (Maker 0.20%, Taker 0.30%, Mixed 0.25%).
2. Capital efficiency: 1x isolated short requires 100% margin -> 0.5x capital efficiency on total capital.
3. Annualized net yield calculation, basis spread, and breakeven period (days) formula.
4. Clamping and rate bounds (BTC ±0.375%, ETH ±0.75% per 8h).
5. Adverse funding rate monitoring (exit trigger when rate turns negative or drops below fee amortization threshold).
6. Provide complete specification of classes, data structures (dataclasses), method signatures, and docstrings for `src/funding_carry.py`.

Do NOT write code to src/ directly.
Write your analysis to:
c:\TG\BOT\TRADE DEMO 1\.agents\teamwork\m1_explorer_1\analysis.md
And your handoff to:
c:\TG\BOT\TRADE DEMO 1\.agents\teamwork\m1_explorer_1\handoff.md

Report back via send_message when complete.
