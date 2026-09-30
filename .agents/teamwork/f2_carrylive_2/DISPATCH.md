## 2026-09-30T11:35:00Z (follow-up 2, successor)
Sender: orchestrator_1 (resumed session)

You are f2_carrylive_2, Successor trader-worker for follow-up 2.
Working directory: c:\TG\BOT\TRADE DEMO 1\.agents\teamwork\f2_carrylive_2

MANDATORY FIRST READS:
1. `.agents/teamwork/f2_carrylive_1/handoff.md` (predecessor state — BTC rejected, ETH passes, leverage 3x must go 1x, posSide unchecked)
2. `.agents/teamwork/f2_carrylive_1/DISPATCH.md` (bounds + ABORT rules — still in force)
3. c:\TG\BOT\TRADE DEMO 1\AGENTS.md (§2, §6)

Mission: finish the ONE small ETH carry pair on DEMO (predecessor stopped before dry_run).
1. REDO all pre-checks fresh (status/mode/funding — old numbers expired). Re-verify ETH
   demo funding non-negative + breakeven sane; else ABORT.
2. Resolve predecessor open items: posSide/tdMode hedge check (read-only); set swap
   leverage 1x isolated demo (`lever 1`, tightening — allowed). If margin-mode mismatch
   or atomicity doubt → ABORT.
3. dry_run `build_pair` (quote gates/sizes/breakeven) → single explicit
   `place_pair(dry_run=False)` (≤$100/side) → partial-fill policy if needed → RE-QUERY
   verify (orders + positions + bills, `botcar*` tags).
4. Board (freshness check first!): append result to TEAMWORK-M1 row + fix its stale
   `OWNER=botr` mention (now `botcar` after F1) + create MON-CARRY-PAIR scheduled row
   for Sentinel. Verify via agent_context.
5. Write `handoff.md` in your working dir.

Same FORBIDDEN as predecessor: >1 pair, leverage ≠1x at placement, live, code changes
(board rows + handoff only), engine, secrets, risk changes.

HANDOFF: reply with — fresh pre-check numbers, pair spec, order IDs + fill evidence,
board rows touched, monitoring plan, blockers.
