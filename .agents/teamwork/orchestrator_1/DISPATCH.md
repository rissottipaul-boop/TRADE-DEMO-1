## 2026-09-30T07:04:01Z
Sender: 9e4a6825-6f34-43c1-8a0d-3799719960ff (parent)

You are the Project Orchestrator (teamwork_preview_orchestrator).
Your working directory is: c:\TG\BOT\TRADE DEMO 1\.agents\teamwork\orchestrator_1
The verbatim user request is recorded in: c:\TG\BOT\TRADE DEMO 1\.agents\teamwork\ORIGINAL_REQUEST.md

Your mission:
Orchestrate the full implementation of the Trading Insights with Meta Muse Code delegation according to the specifications in ORIGINAL_REQUEST.md:
- R1: Funding Carry arbitrage (Spot Long + Swap Short 1x) as per insights/funding-carry.md
- R2: Mean Reversion signal strategy (RSI + Bollinger Bands) as per insights/meanrev-strategy-design.md
- R3: Idle Cash Earn treasury module as per insights/idle-cash-earn.md
- R4: Bot fleet integration & expansion to 50 bots across 13 types as per insights/bot-fleet-50.md and src/fleet_manager.py
- R5: Routine/lightweight task delegation to Meta Muse Code via ops/delegate.ps1
- R6: Strict security and controlled demo environment, order tagging (ORDER-OWNER-TAG), preserving src.engine continuity.

Ensure all Acceptance Criteria are fully met:
- Funding Carry calculates net yield with 4 fee legs and pairs orders in demo.
- Mean Reversion generates 1H signals, validates via src/risk.py, and routes to src/order_router.py.
- Idle Cash Earn queries free balance, subscribes to Flexible Earn, and redeems on demand.
- Bot fleet manager supports running and auditing capital distribution for up to 50 bots across 13 types.
- All new modules have independent unit tests with mocks; full regression suite (python -m unittest discover -s tests -t .) passes 100%.
- ops/delegations/ queue is utilized for Muse Code tasks.
- src.engine process remains running without errors.
- ops/board.md reflects all implemented insights.

Follow team protocol: create your BRIEFING.md, plan.md, and progress.md in your working directory. Dispatch specialized subagents as appropriate. When complete, provide your handoff report and notify Sentinel.
