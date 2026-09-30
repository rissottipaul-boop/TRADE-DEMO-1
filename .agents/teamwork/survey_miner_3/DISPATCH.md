## 2026-09-30T07:06:43Z

You are survey_miner_3, a Test & Delegation Spec Miner (teamwork_preview_spec_miner).
Your working directory is: c:\TG\BOT\TRADE DEMO 1\.agents\teamwork\survey_miner_3

MANDATORY FIRST STEP: Read the original user request at:
c:\TG\BOT\TRADE DEMO 1\.agents\teamwork\ORIGINAL_REQUEST.md

Your mission in this Survey phase is to inspect the test suite and the delegation infrastructure:
1. Inspect the test suite under tests/: what test files exist, how mocks are constructed, test runner conventions, coverage, and how python -m unittest discover -s tests -t . executes.
2. Inspect ops/delegate.ps1 and the directory ops/delegations/: how task delegation to Meta Muse Code works, file formats (.task.json, result artifacts), queuing, execution lifecycle, and validation.
3. Inspect ops/agent-team.md and ops/agent-routing.json: understanding agent roles, routing, and task boundaries.
4. Define test requirements for each of the new modules (Funding Carry, Mean Reversion, Idle Cash Earn, Fleet Manager 50 bots): what unit tests and mock tests will be required to ensure 100% pass rate.
5. Determine what routine tasks can be delegated to Meta Muse Code via ops/delegate.ps1 (e.g. test scaffolds, doc formatting, parameter tables).

Do NOT write or modify any source code.
Write your findings to:
c:\TG\BOT\TRADE DEMO 1\.agents\teamwork\survey_miner_3\survey_testing_delegation.md
And write your final handoff to:
c:\TG\BOT\TRADE DEMO 1\.agents\teamwork\survey_miner_3\handoff.md

Report back via send_message when done with your test & delegation recommendations.
