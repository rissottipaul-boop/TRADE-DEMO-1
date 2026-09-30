# BRIEFING — 2026-09-30T07:17:40Z

## Mission
Probe and document comprehensive technical specifications for R1 (Funding Carry), R2 (Mean Reversion), R3 (Idle Cash Earn), and R4 (Bot Fleet 50 expansion).

## 🔒 My Identity
- Archetype: teamwork_preview_spec_miner
- Roles: Specification Miner
- Working directory: c:\TG\BOT\TRADE DEMO 1\.agents\teamwork\survey_miner_1
- Original parent: 0392c235-01a5-43cd-9010-ac02cbb6d35c
- Milestone: Survey Phase (Completed)

## 🔒 Key Constraints
- Read-only on source code: do NOT modify any source code.
- Write only to own directory: c:\TG\BOT\TRADE DEMO 1\.agents\teamwork\survey_miner_1.
- All actions in OKX Demo environment; respect live-policy.
- Follow Russian language convention for documentation and communications per AGENTS.md.
- Ensure thorough probing of all 4 key requirement areas and related features.

## Current Parent
- Conversation ID: 0392c235-01a5-43cd-9010-ac02cbb6d35c
- Updated: 2026-09-30T07:17:40Z

## Task Summary
- **What to build**: Specification discovery document (survey_specs.md) and handoff report (handoff.md).
- **Success criteria**: Full mathematical formulas, fee legs, thresholds, states, API endpoints, risk rules, and edge cases documented for R1, R2, R3, R4.
- **Interface contracts**: insights/funding-carry.md, insights/meanrev-strategy-design.md, insights/idle-cash-earn.md, insights/bot-fleet-50.md, src/risk.py, src/order_router.py, src/fleet_manager.py, ops/sleeves.json, src/order_owner.py.
- **Code layout**: src/, tests/, insights/, ops/.

## Key Decisions Made
- All mathematical formulas, fee structures, and capital efficiency discounts verified against primary sources.
- Highlighted critical edge cases: exit price guard incompatibility with Wilder RSI on close; error 50038 in Demo Earn; 1x isolated margin asymmetry in Carry; ORDER-OWNER-TAG prefix non-hex 4th character rule.
- Full regression suite verified: 836 tests passing cleanly.

## Artifact Index
- DISPATCH.md — incoming dispatch instructions
- BRIEFING.md — persistent situational awareness
- progress.md — liveness heartbeat and step tracking
- survey_specs.md — comprehensive specification mining findings (33 features, 15 edge cases, in-depth formulas)
- handoff.md — self-contained 5-component handoff report for implementation team
