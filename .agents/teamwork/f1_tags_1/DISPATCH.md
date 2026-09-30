## 2026-09-30T11:00:00Z (follow-up 1)
Sender: orchestrator_1 (resumed session)

You are f1_tags_1, Worker for follow-up 1: owner-tag registry.
Working directory: c:\TG\BOT\TRADE DEMO 1\.agents\teamwork\f1_tags_1

MANDATORY FIRST READS:
1. c:\TG\BOT\TRADE DEMO 1\AGENTS.md (§2, §5, §6 ORDER-OWNER-TAG)
2. c:\TG\BOT\TRADE DEMO 1\src\order_owner.py (registry format, kinds)
3. `.agents/teamwork/reviewer_1/handoff.md` ("Owner-теги" section)
4. `src/carry_executor.py`, `src/mr_trader.py` (OWNER usage + registry tests)

Mission:
1. Register `botcar` (Funding Carry) and `botmr` (Mean Reversion) with kind=strategy
   in `src/order_owner.py` (follow existing Owner entry format exactly).
2. Switch OWNER to the new codes: `botcar` in `src/carry_executor.py`, `botmr` in
   `src/mr_trader.py`. Update the registry-absence tests (e.g.
   `test_botcar_not_in_registry_repo_wins`) into registry-presence tests.
3. Run: `tests.test_order_owner`, `tests.test_carry_executor`, `tests.test_mr_trader`
   (+ neighbours test_funding_carry, test_meanrev, test_mean_reversion_strategy) — all green.
4. Verify no other code depends on OWNER == "botr" for these two modules (search).

OWNERSHIP: `src/order_owner.py`, `src/carry_executor.py`, `src/mr_trader.py`,
`tests/test_carry_executor.py`, `tests/test_mr_trader.py`, `tests/test_order_owner.py`
(if a new test is needed). Nothing else.

FORBIDDEN: changing existing registry entries' meaning, live/demo orders, engine,
secrets, risk limits, guard/autopilot files.

HANDOFF: reply with — entries added, files changed, test commands + exact results,
blockers. Also write `handoff.md` in your working dir.
