## 2026-09-30T07:06:43Z
[Message] timestamp=2026-09-30T07:06:43Z sender=0392c235-01a5-43cd-9010-ac02cbb6d35c priority=MESSAGE_PRIORITY_HIGH content=You are survey_explorer_2, a Codebase Architecture Explorer (teamwork_preview_explorer).
Your working directory is: c:\TG\BOT\TRADE DEMO 1\.agents\teamwork\survey_explorer_2

MANDATORY FIRST STEP: Read the original user request at:
c:\TG\BOT\TRADE DEMO 1\.agents\teamwork\ORIGINAL_REQUEST.md

Your mission in this Survey phase is to investigate the existing architecture, runtime environment, and interfaces:
1. Explore c:\TG\BOT\TRADE DEMO 1\ops\code-map.md, ops/board.md, and existing modules in src/ (src/engine.py, src/risk.py, src/order_router.py, src/order_owner.py, src/fleet_manager.py, and any existing strategy modules).
2. Understand how src.engine runs its main loop, tickers, portfolio tracking, and how strategies and managers integrate into it. Note how engine continuity is maintained (do NOT stop, restart, or disrupt any running engine process).
3. Analyze src/risk.py: what methods exist, how position sizing and risk validation work, what breaker / limit checks are enforced.
4. Analyze src/order_router.py: how orders are dispatched, how simulated / demo mode is handled, how order responses are formatted.
5. Analyze src/order_owner.py: ORDER-OWNER-TAG generation, clOrdId 32-character requirement, prefixes (trd, iex, pmp, sen, bot*).
6. Analyze src/fleet_manager.py: current bot types, methods, data structures, and how it needs to be extended to 50 bots across 13 types.
7. Map exact interfaces and dependency relationships.

Do NOT write or modify any source code.
Write your architecture findings to:
c:\TG\BOT\TRADE DEMO 1\.agents\teamwork\survey_explorer_2\survey_architecture.md
And write your final handoff to:
c:\TG\BOT\TRADE DEMO 1\.agents\teamwork\survey_explorer_2\handoff.md

Report back via send_message when done with your architecture map and interface recommendations.
