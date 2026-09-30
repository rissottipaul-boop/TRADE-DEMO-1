## 2026-09-30T07:19:37Z
You are m1_explorer_3, an Explorer (teamwork_preview_explorer) for Milestone 1: Funding Carry Arbitrage (R1).
Your working directory is: c:\TG\BOT\TRADE DEMO 1\.agents\teamwork\m1_explorer_3

MANDATORY FIRST STEP: Read the original user request at:
c:\TG\BOT\TRADE DEMO 1\.agents\teamwork\ORIGINAL_REQUEST.md
Also read the project architecture at:
c:\TG\BOT\TRADE DEMO 1\.agents\teamwork\orchestrator_1\PROJECT.md
c:\TG\BOT\TRADE DEMO 1\.agents\teamwork\survey_miner_3\survey_testing_delegation.md

Your mission:
Design the unit test architecture and mock fixtures for `tests/test_funding_carry.py`:
1. Design mock patterns (conforming to `FakeOkx` in `tests/test_order_router_exit.py` or standalone test doubles).
2. Detail all required test cases:
   - 4-leg fee calculations for maker, taker, mixed tiers.
   - Net annualized yield calculation and capital efficiency (0.5x).
   - Breakeven holding period calculation.
   - Delta-neutral pairing order generation (spot buy + swap short).
   - Liquidation price calculation and margin check.
   - Adverse funding rate detection and exit signal.
   - Atomic rollback / hedge recovery on partial fill.
3. Ensure all tests run with 100% independence, zero live network requests, and execute via `python -m unittest discover -s tests -t .`.
4. Provide the exact test file blueprint and test method signatures.

Do NOT write code to tests/ directly.
Write your analysis to:
c:\TG\BOT\TRADE DEMO 1\.agents\teamwork\m1_explorer_3\analysis.md
And your handoff to:
c:\TG\BOT\TRADE DEMO 1\.agents\teamwork\m1_explorer_3\handoff.md

Report back via send_message when complete.
