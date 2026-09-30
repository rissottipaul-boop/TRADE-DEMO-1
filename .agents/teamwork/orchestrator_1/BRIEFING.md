# BRIEFING — 2026-09-30T07:05:00Z

## Mission
Orchestrate the full implementation of Trading Insights (Funding Carry, Mean Reversion, Idle Cash Earn, Bot Fleet 50, Muse Code delegation) with strict verification and engine continuity in OKX Demo.

## 🔒 My Identity
- Archetype: teamwork_preview_orchestrator
- Roles: orchestrator, user_liaison, human_reporter, successor
- Working directory: c:\TG\BOT\TRADE DEMO 1\.agents\teamwork\orchestrator_1
- Original parent: parent (Sentinel)
- Original parent conversation ID: 9e4a6825-6f34-43c1-8a0d-3799719960ff

## 🔒 My Workflow
- **Pattern**: Project
- **Scope document**: c:\TG\BOT\TRADE DEMO 1\PROJECT.md
1. **Decompose**: Survey full scope with 3 Explorers/Spec Miners, record Feature Inventory, define milestones:
   - M1: Funding Carry Arbitrage (insights/funding-carry.md)
   - M2: Mean Reversion Strategy (insights/meanrev-strategy-design.md)
   - M3: Idle Cash Earn Treasury (insights/idle-cash-earn.md)
   - M4: Bot Fleet 50 Expansion (insights/bot-fleet-50.md, src/fleet_manager.py)
   - M5: Meta Muse Code Delegation Pipeline (ops/delegate.ps1, ops/delegations/)
   - M6: E2E Testing, 100% Unit Regression & Board Status
2. **Dispatch & Execute**:
   - Direct iteration loop per milestone: Explorer(s) -> Worker -> Reviewer(s) -> Challenger(s) -> Auditor gate.
   - E2E Testing Track in parallel deriving opaque-box tests from requirements.
3. **On failure**: Retry -> Replace -> Skip (non-critical) -> Redistribute -> Redesign. Auditor failure is a hard binary veto (never skip).
4. **Succession**: At 16 spawns, write handoff.md, kill timers, spawn successor, record ID.
- **Work items**:
  1. Survey & Architecture Specification [in-progress]
  2. M1: Funding Carry Arbitrage [pending]
  3. M2: Mean Reversion Strategy [pending]
  4. M3: Idle Cash Earn Module [pending]
  5. M4: Bot Fleet 50 Expansion [pending]
  6. M5: Muse Code Delegation Pipeline [pending]
  7. M6: E2E Regression & System Verification [pending]
- **Current phase**: 0 (Survey)
- **Current focus**: Survey & Architecture Specification

## 🔒 Key Constraints
- NEVER write, modify, or create source code files directly.
- NEVER run build/test commands yourself — require workers to do so.
- NEVER investigate or explore the problem at the code level — dispatch Explorers for technical investigation.
- Use file-editing tools ONLY for metadata/state files (.md) in .agents/teamwork/ and project-level PROJECT.md.
- Never reuse a subagent after it has delivered its handoff — always spawn fresh.
- Hard audit veto: Forensic Auditor failure is non-negotiable.
- OKX Demo only (`--demo`, `simulated-trading`), maintain `clOrdId` with ORDER-OWNER-TAG.
- Protect active `src.engine` process without restarting or disrupting it.
- Language of answers and docs: Russian as per AGENTS.md.

## Current Parent
- Conversation ID: 9e4a6825-6f34-43c1-8a0d-3799719960ff
- Updated: 2026-09-30T07:04:01Z

## Key Decisions Made
- Project Orchestrator initialized; Project Pattern selected with Dual Track (Implementation + E2E Testing).
- Initiating Survey phase with 3 Explorers / Spec Miners to map codebase and specifications.

## Team Roster
| Agent | Type | Work Item | Status | Conv ID |
|-------|------|-----------|--------|---------|
| survey_miner_1 | teamwork_preview_spec_miner | Survey Specs (R1-R4) | completed | d1be15b2-99ed-4628-b806-e8b36126c4d9 |
| survey_explorer_2 | teamwork_preview_explorer | Survey Architecture & Runtime | completed | 7aff69e2-30d4-476b-912c-c36e4ae1f101 |
| survey_miner_3 | teamwork_preview_spec_miner | Survey Testing & Muse Delegation | completed | 8040c48d-de0d-4379-84ad-f20a7e4f92f3 |
| m1_explorer_1 | teamwork_preview_explorer | M1 Funding Carry Math & Models | orphaned (no handoff, superseded) | 5a11a6d9-a613-4c77-a338-d629f5d3eb45 |
| m1_explorer_2 | teamwork_preview_explorer | M1 Execution & OrderRouter Integration | orphaned (no handoff, superseded) | 3e947dc3-cd83-48ea-9a1b-d846e66c6d28 |
| m1_explorer_3 | teamwork_preview_explorer | M1 Unit Tests & Mock Fixtures | orphaned (no handoff, superseded) | 90463b57-f10d-43e9-adc7-e4705e6182bf |
| m1_worker_1 | worker | M1 Carry executor (src/carry_executor.py) | completed (36/36 tests OK; no live pair — acctLv blocker) | subagent-1 |
| m2_worker_1 | worker | M2 MR runner (src/mr_trader.py, dry-run only) | completed (58/58 slice OK; 0 orders) | subagent-2 |
| reviewer_1 | reviewer+challenger | M1–M4 review gate | completed (M3 PASS; M1/M2/M4 PASS-WITH-NOTES; no VETO) | subagent-5 |
| e2e_worker_1 | e2e+regression+boardsync | M5–M6 final | completed (1033: 38+6 pre-existing; scope 149/149; engine +34; R5 E2E ok; 6 board rows) | subagent-6 |
| f1_tags_1 | worker | Follow-up 1: botcar/botmr registry | completed (85+40+70 green; hex-gate fix) | subagent-7 |
| f2_carrylive_1 | trader-worker | Follow-up 2: live demo carry pair | in-progress | subagent-9 |
| f3_sandbox_1 | worker | Follow-up 3: 38+6 sandbox reds | in-progress | subagent-8 |
| m3_worker_1 | worker | M3 Treasury controller (src/treasury_exec.py) | completed (29/29 tests OK) | subagent-3 |
| m4_verifier_1 | verifier | M4 Fleet 50 audit | completed (26/26; 3 fixes; gap: 6/13 types unlaunchable via CLI) | subagent-4 |

## Succession Status
- Succession required: no
- Spawn count: 12 (BUDGET LIFTED by human 30.09 ~15:55 +05: "ограничение в 5 агентов не для тебя, у тебя нет ограничений" — 16-spawn succession cap no longer applies to this orchestra)
- Pending subagents: subagent-6
- Predecessor: none
- Successor: not yet spawned

## Active Timers
- Heartbeat cron: 0392c235-01a5-43cd-9010-ac02cbb6d35c/task-14
- Safety timer: none (handled by heartbeat cron)
- On succession: kill all timers before spawning successor
- On context truncation: run `manage_task(Action="list")` — re-create if missing

## Artifact Index
- c:\TG\BOT\TRADE DEMO 1\.agents\teamwork\ORIGINAL_REQUEST.md — Original User Request
- c:\TG\BOT\TRADE DEMO 1\.agents\teamwork\orchestrator_1\DISPATCH.md — Parent dispatch log
- c:\TG\BOT\TRADE DEMO 1\.agents\teamwork\orchestrator_1\BRIEFING.md — Persistent working memory
- c:\TG\BOT\TRADE DEMO 1\.agents\teamwork\orchestrator_1\progress.md — Liveness heartbeat and milestone tracking
- c:\TG\BOT\TRADE DEMO 1\.agents\teamwork\orchestrator_1\plan.md — Execution plan
