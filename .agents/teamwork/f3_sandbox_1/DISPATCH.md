## 2026-09-30T11:00:00Z (follow-up 3)
Sender: orchestrator_1 (resumed session)

You are f3_sandbox_1, Worker for follow-up 3: sandbox-red triage and fix.
Working directory: c:\TG\BOT\TRADE DEMO 1\.agents\teamwork\f3_sandbox_1

MANDATORY FIRST READS:
1. c:\TG\BOT\TRADE DEMO 1\AGENTS.md (§2 — guard perimeter is FORBIDDEN: ops/hooks/,
   .github/hooks/, ops/autopilot.json, ops/live-policy.json, pump-pocket.json; §7)
2. `logs/teamwork_e2e_tests.log` (latest full discover: 38 failures + 6 errors)

Mission: make `unittest discover` green WITHOUT touching the guard perimeter.
Scope (only these red files): `tests/test_agent_rotate.py` (21), `tests/test_guard.py` (13),
`tests/test_guard_adapter.py` (4), `tests/test_engine_watchdog.py` (3),
`tests/test_pump_sched.py` (3).
1. Reproduce and classify EVERY red: (a) fixable in test code/helpers/ps1 (sandbox paths
   `\\?\`, $PSScriptRoot, pwsh-vs-powershell, encodings) → FIX with minimal change;
   (b) requires guard/perimeter/settings change → DO NOT touch, record as NEEDS-USER
   with exact evidence (test name, expected vs actual, which perimeter file blocks);
   (c) environment-only (cannot be fixed from repo) → record as BLOCKED with evidence.
2. Allowed writes: the 5 test files above + test helpers they import + `ops/*.ps1`
   ONLY if a fix is provably safe for the RUNNING engine (no behaviour change for
   start/stop/status paths; engine must never be restarted by you).
3. Re-run full discover at the end; quote Ran/failures/errors + per-file delta.
   If everything fixable is fixed and only (b)/(c) remain, that is a DONE with honest list.

OWNERSHIP: the 5 test files + their helpers + (conditionally) `ops/*.ps1`.
ABSOLUTE FORBIDDEN: `ops/hooks/*`, `.github/hooks/*`, `ops/autopilot.json`,
`ops/live-policy.json`, `pump-pocket.json`, `.claude/*`, `.muse/*`, `.gemini/*`, `.codex/*`,
`src/risk.py` limits, engine start/stop/restart, live/demo orders, secrets.

HANDOFF: reply with — per-test verdict table (fixed / needs-user / blocked + evidence),
full-discover numbers before/after, files changed, exact needs-user questions (if any).
Also write `handoff.md` in your working dir.
