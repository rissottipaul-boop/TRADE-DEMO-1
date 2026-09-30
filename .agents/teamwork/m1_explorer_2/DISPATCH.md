## 2026-09-30T07:19:37Z
You are m1_explorer_2, an Explorer (teamwork_preview_explorer) for Milestone 1: Funding Carry Arbitrage (R1).
Your working directory is: c:\TG\BOT\TRADE DEMO 1\.agents\teamwork\m1_explorer_2

MANDATORY FIRST STEP: Read the original user request at:
c:\TG\BOT\TRADE DEMO 1\.agents\teamwork\ORIGINAL_REQUEST.md
Also read the project architecture at:
c:\TG\BOT\TRADE DEMO 1\.agents\teamwork\orchestrator_1\PROJECT.md
c:\TG\BOT\TRADE DEMO 1\.agents\teamwork\survey_explorer_2\survey_architecture.md

Your mission:
Design the execution, routing, and risk management integration for `src/funding_carry.py`:
1. Order pairing execution: Spot Long (tdMode cash/cross no-borrow) + Swap Short (tdMode isolated, 1x leverage) for BTC-USDT and ETH-USDT in OKX Demo.
2. Integration with `src/order_router.py`: Order placement, clOrdId formatting with `botcar` prefix via `src/order_owner.py`, throttling (20/2s), expiration 10,000ms.
3. Integration with `src/risk.py`: Checking entry permission and size limits.
4. Liquidation buffer verification (1.991x entry price for 1x isolated short) and isolated margin monitoring.
5. Error handling and atomicity: what happens if spot fills but swap fails (immediate hedge or spot exit).
6. Provide exact class design for `FundingCarryExecutor` or `FundingCarryStrategy`.

Do NOT write code to src/ directly.
Write your analysis to:
c:\TG\BOT\TRADE DEMO 1\.agents\teamwork\m1_explorer_2\analysis.md
And your handoff to:
c:\TG\BOT\TRADE DEMO 1\.agents\teamwork\m1_explorer_2\handoff.md

Report back via send_message when complete.
