## 2026-09-30T10:05:00Z (launch)
Sender: orchestrator_1 (resumed session)

You are m4_verifier_1, Verifier for Milestone 4: Bot Fleet 50 (R4).
Working directory: c:\TG\BOT\TRADE DEMO 1\.agents\teamwork\m4_verifier_1

MANDATORY FIRST READS (in order):
1. c:\TG\BOT\TRADE DEMO 1\.agents\teamwork\ORIGINAL_REQUEST.md (R4 + ACs)
2. c:\TG\BOT\TRADE DEMO 1\AGENTS.md (§2 safety columns, §5 file claims, §6 ORDER-OWNER-TAG)
3. c:\TG\BOT\TRADE DEMO 1\insights\bot-fleet-50.md
4. c:\TG\BOT\TRADE DEMO 1\src\fleet_manager.py (existing — audit, do not refactor)

Mission: verify R4 ACs against the existing implementation (audit, not rewrite):
1. Check: 13-type catalog, MAX_ACTIVE_BOTS=50 quota, ≤2% equity/bot, 30% reserve,
   leverage ≤3x isolated, mandatory SL, sleeve isolation (`demo_fleet`), batch kill,
   ORDER-OWNER-TAG `trd` on build_cli_command.
2. Run `python -m unittest tests.test_fleet_manager` (and fleet-related suites) — record result.
3. If a REAL bug blocks an AC: fix it minimally in place + regression test, OR report as
   blocker with evidence if the fix is risky. No refactoring, no new features.
4. Optional NEW `tests/test_fleet_ac.py` ONLY if an AC has zero coverage — mocks, no network.

OWNERSHIP: default read-only. Allowed writes: minimal bug fix in `src/fleet_manager.py`
(with test) or NEW `tests/test_fleet_ac.py`. Nothing else.

FORBIDDEN: bot creation/cancellation on the exchange, engine disruption, secrets,
risk-limit weakening, guard/autopilot edits, refactoring.

VERIFY: fleet test suites green; quote commands + results.

HANDOFF: reply with — per-AC verdict table (met/gap + evidence), tests run + results,
fixes (if any), blockers. Also write `handoff.md` in your working dir.
