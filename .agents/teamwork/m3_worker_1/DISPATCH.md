## 2026-09-30T10:05:00Z (launch)
Sender: orchestrator_1 (resumed session)

You are m3_worker_1, Worker for Milestone 3: Idle Cash Earn Treasury (R3).
Working directory: c:\TG\BOT\TRADE DEMO 1\.agents\teamwork\m3_worker_1

MANDATORY FIRST READS (in order):
1. c:\TG\BOT\TRADE DEMO 1\.agents\teamwork\ORIGINAL_REQUEST.md (R3 + ACs)
2. c:\TG\BOT\TRADE DEMO 1\AGENTS.md (§2 safety columns, §5 file claims, §6 ORDER-OWNER-TAG)
3. c:\TG\BOT\TRADE DEMO 1\insights\idle-cash-earn.md (validated: demo blocks Earn with 50038;
   Simple Earn funds leave totalEq; instant redeem; ~2h first-accrual lag)
4. c:\TG\BOT\TRADE DEMO 1\src\treasury.py (existing math — REUSE, do not rewrite)

Mission: close the AC gap — treasury controller with demo simulation adapter:
1. NEW `src/treasury_exec.py`: free-cash calc from a passed balance snapshot
   (pure function of availBal/frozenBal/equity + reserve pct — NO network inside),
   sweep decision (FreeCash >= 100 USDT → purchase plan), redeem-on-margin-demand plan,
   yield attribution record for the `cash_earn` sleeve (data-only, no ledger writes).
2. Demo adapter: sCode 50038 → simulated in-memory ledger (purchase/redeem/accrue),
   clearly marked `simulated=True` in every record. No real Earn calls from this module.
3. NEW `tests/test_treasury_exec.py`: mocks only, zero network, green via unittest.

OWNERSHIP: you may create ONLY `src/treasury_exec.py` + `tests/test_treasury_exec.py`.
Read everything else; modify nothing else.

FORBIDDEN: any network/exchange calls, engine disruption, secrets, risk-limit changes,
guard/autopilot edits, writing to data/*.db or sleeves.json.

VERIFY: your tests green + `python -m unittest tests.test_treasury_exec tests.test_treasury`
green.

HANDOFF: reply with — files created, test command + result, AC coverage, blockers.
Also write `handoff.md` in your working dir.
