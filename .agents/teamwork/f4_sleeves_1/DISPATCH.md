## 2026-09-30T11:35:00Z (follow-up 4)
Sender: orchestrator_1 (resumed session)

You are f4_sleeves_1, Worker for follow-up 4: sleeve mapping + scratch cleanup.
Working directory: c:\TG\BOT\TRADE DEMO 1\.agents\teamwork\f4_sleeves_1

MANDATORY FIRST READS:
1. c:\TG\BOT\TRADE DEMO 1\AGENTS.md (§2, §5)
2. `.agents/teamwork/f3_sandbox_1/handoff.md` (§"Остаток": 2 reds in test_pnl_ledger)
3. `.agents/teamwork/f1_tags_1/handoff.md` (§"Наблюдения" п.1: sleeves.json lacks botcar/botmr)
4. `ops/sleeves.json` + `src/pnl_ledger.py` attribution rules (read how prefixes map to sleeves)

Mission:
1. Add `botcar`/`botmr` prefix mappings to `ops/sleeves.json` following EXISTING sleeve
   names and longest-prefix semantics (check which sleeves exist — do NOT invent new
   sleeve names unless the ledger requires it; document the choice).
2. Run `tests.test_pnl_ledger` green, then FULL `unittest discover`: expect Ran ~1040
   with failures=0 (6 skips for missing pwsh 7 are honest). Save log to
   `logs/teamwork_f4_discover.log`. If anything else is red — fix if trivially safe
   within test-only scope, else report as blocker with evidence (do NOT touch guard
   perimeter, risk limits, engine, or others' files).
3. Delete F3's scratch files in repo root (all verified scratch, listed in F3 handoff):
   f3_repro_rotate.log, f3_repro_rotate2.log, f3_repro_guard.log, f3_repro_guard2.log,
   f3_final_discover.log, f3_orch_excerpt.txt, f3_probe_psroot.py, f3_probe_join.py,
   f3_summ_guard.py, f3_psroot.ps1, f3_probe_join.ps1. Verify each name matches before delete.

OWNERSHIP: `ops/sleeves.json`, the 11 scratch files (delete), `logs/teamwork_f4_discover.log`
(new), your `handoff.md`. Nothing else.

FORBIDDEN: new sleeve semantics, live/demo orders, engine, secrets, risk/guard edits.

HANDOFF: reply with — mappings added (+ why these sleeves), test numbers
(test_pnl_ledger + full discover), scratch deleted (count), blockers.
