## 2026-09-30T10:05:00Z (launch)
Sender: orchestrator_1 (resumed session)

You are m1_worker_1, Worker for Milestone 1: Funding Carry Arbitrage (R1).
Working directory: c:\TG\BOT\TRADE DEMO 1\.agents\teamwork\m1_worker_1

MANDATORY FIRST READS (in order):
1. c:\TG\BOT\TRADE DEMO 1\.agents\teamwork\ORIGINAL_REQUEST.md (R1 + ACs)
2. c:\TG\BOT\TRADE DEMO 1\AGENTS.md (§2 safety columns, §5 file claims, §6 ORDER-OWNER-TAG)
3. c:\TG\BOT\TRADE DEMO 1\insights\funding-carry.md (validated numbers)
4. c:\TG\BOT\TRADE DEMO 1\src\funding_carry.py (existing pure engine — REUSE, do not rewrite)
5. c:\TG\BOT\TRADE DEMO 1\src\risk.py, src\order_router.py, src\order_owner.py (integration APIs — read)

Mission: close the AC gap — execution layer over the existing math engine:
1. NEW `src/carry_executor.py`: delta-neutral pairing (spot long cash + swap short 1x isolated),
   pre-trade gates via `src/funding_carry.validate_legs` + `risk.check_entry_allowed`,
   sizing via `risk.size_position`, placement via `order_router` with owner tag per
   `src/order_owner.py` registry (reconcile `botcar` from PROJECT.md with the real registry —
   repo registry wins; document the choice).
2. `dry_run=True` by default (build + validate orders, no placement). Live demo placement only
   as an explicit separate call, one small pair, within risk limits; record algoId/clOrdId.
3. Partial-fill policy: spot filled + swap failed → immediate hedge-or-exit decision function (pure).
4. Adverse funding exit signal (pure): rate turns negative or below fee-amortization threshold.
5. NEW `tests/test_carry_executor.py`: mocks only, zero network, green via unittest.

OWNERSHIP: you may create ONLY `src/carry_executor.py` + `tests/test_carry_executor.py`.
Read everything else; modify nothing else. Parallel workers own other files.

FORBIDDEN: live trading, engine restart/disruption, secrets (.env/keys), risk-limit weakening,
guard/autopilot edits, gold-plating beyond R1 AC.

VERIFY: your tests green + `python -m unittest tests.test_carry_executor tests.test_funding_carry`
green; engine untouched (`src.ops status` before/after if you place demo orders).

HANDOFF: reply with — files created, test command + result, AC coverage (which R1 AC lines
are now met), demo order IDs if any, blockers. Also write `handoff.md` in your working dir.
