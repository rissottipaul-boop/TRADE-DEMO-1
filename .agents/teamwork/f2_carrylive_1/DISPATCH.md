## 2026-09-30T11:15:00Z (follow-up 2)
Sender: orchestrator_1 (resumed session)

You are f2_carrylive_1, Trader-Worker for follow-up 2: live demo carry pair.
Working directory: c:\TG\BOT\TRADE DEMO 1\.agents\teamwork\f2_carrylive_1

MANDATORY FIRST READS:
1. c:\TG\BOT\TRADE DEMO 1\AGENTS.md (§2: demo-orders allowed within risk limits; §6 ORDER-OWNER-TAG)
2. `.agents/teamwork/m1_worker_1/handoff.md` (esp. blockers §38: isolated-margin check!)
3. `.agents/teamwork/e2e_worker_1/handoff.md` (§3: acctLv is 2 — mode blocker refuted)
4. `src/carry_executor.py` (build_pair/place_pair/dry_run) + `insights/funding-carry.md` §0 (§breakeven)

Mission: place and verify ONE small delta-neutral carry pair on DEMO.
1. Fresh `src.ops status`: kill/breakers off, free risk slots (need 2), entries_today headroom.
   Fresh `account_mode status`: confirm acctLv 2. Check current funding rate (read-only):
   proceed only if non-negative and breakeven math sane; else ABORT with evidence.
2. `build_pair` dry_run first: quote gates, sizes, breakeven vs planned hold. Bounds:
   notional ≤ $100/side, BTC preferred, swap 1x isolated. Verify swap leverage/margin
   mode via position/account read BEFORE placing (M1 warning: margin is account-level).
3. Single explicit `place_pair(dry_run=False)`. On partial fill → `decide_partial_fill`
   immediately (close the overweight leg, do not leave naked).
4. Verify by RE-QUERY (spot orders + swap positions + bills): both legs filled,
   clOrdId `botcar*` present. Record order IDs, fill px, sizes, funding rate, breakeven.
5. Board (freshness check first!): append result note to TEAMWORK-M1 row + create
   MON-CARRY-PAIR `scheduled` row for Sentinel (check pair 1x/day: funding sign,
   margin, exit via `funding_exit_signal`). Verify new row via agent_context.
6. Write `handoff.md` in your working dir.

ABORT (no placement, honest report) if: gates fail, negative/adverse funding, no free
risk slots, margin-mode mismatch, or any doubt about pair atomicity.

FORBIDDEN: >1 pair, leverage ≠1x, live (non-demo), engine restart, code changes
(except board rows + your handoff.md), secrets, risk-limit changes.

HANDOFF: reply with — pre-checks (status/mode/funding numbers), pair spec, order IDs +
fill evidence (re-query output), board rows touched, monitoring plan, blockers.
