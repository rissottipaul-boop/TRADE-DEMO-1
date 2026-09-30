## 2026-09-30T10:05:00Z (launch)
Sender: orchestrator_1 (resumed session)

You are m2_worker_1, Worker for Milestone 2: Mean Reversion Strategy (R2).
Working directory: c:\TG\BOT\TRADE DEMO 1\.agents\teamwork\m2_worker_1

MANDATORY FIRST READS (in order):
1. c:\TG\BOT\TRADE DEMO 1\.agents\teamwork\ORIGINAL_REQUEST.md (R2 + ACs)
2. c:\TG\BOT\TRADE DEMO 1\AGENTS.md (§2 safety columns, §5 file claims, §6 ORDER-OWNER-TAG)
3. c:\TG\BOT\TRADE DEMO 1\insights\meanrev-strategy-design.md + insights\meanrev-backtest.md
   (defaults FAILED the gate: WF OOS net −17.96%, PF 0.54 — edge negative pre-cost)
4. c:\TG\BOT\TRADE DEMO 1\src\mean_reversion.py + src\backtest\meanrev.py (existing signals — REUSE)
5. c:\TG\BOT\TRADE DEMO 1\src\risk.py, src\order_router.py, src\order_owner.py (read)

Mission: close the AC gap — 1H signal → risk → router runner over existing signals:
1. NEW `src/mr_trader.py`: bar-buffer evaluation via `src/mean_reversion` (entry/exit/stop),
   entry validation via `risk.check_entry_allowed` + sizing via `risk.size_position`,
   placement/exit via `order_router` (entry + `place_exit_order`/`settle_exits`) with owner
   tag per `src/order_owner.py` registry (repo registry wins over PROJECT.md `botmr`).
2. `dry_run=True` by default. NO live demo entries on default params (backtest says they lose):
   live demo placement is FORBIDDEN for this worker — dry-run signal trace only.
3. Honest calibration hook: params (rsi_entry, stop_mult, roi table) injectable for MEANREV-CALIB.
4. NEW `tests/test_mr_trader.py`: mocks/fake bars only, zero network, green via unittest.

OWNERSHIP: you may create ONLY `src/mr_trader.py` + `tests/test_mr_trader.py`.
Read everything else; modify nothing else.

FORBIDDEN: live/demo order placement of any kind, engine disruption, secrets,
risk-limit weakening, guard/autopilot edits, claiming profitability.

VERIFY: your tests green + `python -m unittest tests.test_mr_trader
tests.test_mean_reversion_strategy tests.test_meanrev` green.

HANDOFF: reply with — files created, test command + result, AC coverage, blockers.
Also write `handoff.md` in your working dir.
