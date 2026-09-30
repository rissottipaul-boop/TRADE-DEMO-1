# Execution Plan: Trading Insights & Muse Delegation

## Overview
This plan orchestrates the implementation and verification of all requirements (R1–R6) from ORIGINAL_REQUEST.md.

## Execution Tracks & Milestones

### Milestone 1: Funding Carry Arbitrage (R1)
- **Files:** `src/funding_carry.py`, `tests/test_funding_carry.py`
- **Specification:** `insights/funding-carry.md`
- **Key Deliverables:**
  - Funding rate & basis spread monitoring
  - 4-leg fee calculations (maker/taker, spot buy, swap short open, swap short close, spot sell)
  - 0.5x capital efficiency and net annualized yield formula
  - Breakeven period calculation (12-19 days BTC, 18-26 days ETH)
  - Delta-neutral pairing and demo order placement with `botcar` prefix
  - Margin ratio monitoring on isolated short and adverse funding exit
- **Verification:** Unit tests with mocks + Reviewer + Challenger + Forensic Auditor gate.

### Milestone 2: Mean Reversion Strategy (R2)
- **Files:** `src/mean_reversion.py`, `tests/test_mean_reversion.py`
- **Specification:** `insights/meanrev-strategy-design.md`, `src/backtest/meanrev.py`
- **Key Deliverables:**
  - 1H bars with Wilder's RSI(14) and Bollinger Bands(20, 2) on Typical Price
  - Entry signal: `crossed_above(RSI, 30)` AND `Close <= BB_mid` AND `Close > Close[-1]` AND `Volume > 0`
  - Exit priority: 1) ATR Stop (Entry - 1.5*ATR), 2) Minimal ROI table (0m: 2%, 240m: 1.2%, 720m: 0.6%, 1440m: 0%), 3) Time Stop 24h, 4) Indicator exit `crossed_above(RSI, 70)` AND `Close >= BB_mid`
  - Central risk integration (`risk.check_entry_allowed`, `risk.size_position`)
  - OrderRouter routing with `botmr` prefix
- **Verification:** Unit tests with mocks + Reviewer + Challenger + Forensic Auditor gate.

### Milestone 3: Idle Cash Earn Treasury Module (R3)
- **Files:** `src/idle_earn.py`, `tests/test_idle_earn.py`
- **Specification:** `insights/idle-cash-earn.md`, `okx-cex-earn`
- **Key Deliverables:**
  - Free cash calculation: `FreeCash = max(0, availBal - frozenBal - 30% equity reserve)`
  - Sweeping: if FreeCash >= 100 USDT, call Simple Earn purchase
  - Demo mode adapter: handle OKX Demo code 50038 gracefully via simulated ledger
  - On-demand instant redemption when trading margin is required
  - Yield attribution to `cash_earn` sleeve
- **Verification:** Unit tests with mocks + Reviewer + Challenger + Forensic Auditor gate.

### Milestone 4: Bot Fleet Integration & Expansion to 50 Bots (R4)
- **Files:** `src/fleet_manager.py`, `tests/test_fleet_manager_50.py`
- **Specification:** `insights/bot-fleet-50.md`, `src/fleet_manager.py`
- **Key Deliverables:**
  - 13 OKX bot types catalog
  - Maximum 50 active bots quota enforcement
  - Cash reserve >= 30%, max 2% equity per bot, max 3x leverage on derivative bots
  - Mandatory stop-loss on all bots
  - Capital distribution auditing across sleeves (`demo_fleet`)
  - Emergency kill-switch batch stop in groups of 10 bots
  - ORDER-OWNER-TAG tagging with `trd`/`flt` prefixes
- **Verification:** Unit tests with mocks + Reviewer + Challenger + Forensic Auditor gate.

### Milestone 5: Meta Muse Code Routine Delegation (R5)
- **Scripts/Directories:** `ops/delegate.ps1`, `ops/delegations/`
- **Key Deliverables:**
  - Submit routine/scaffolding tasks to runner PID 8728
  - Monitor processing and verify completed JSON artifacts in `ops/delegations/outbox/`
- **Verification:** Task completion with exit code 0 and validated artifacts.

### Milestone 6: System Regression, Engine Continuity & Board Sync (R6, All ACs)
- **Key Deliverables:**
  - Full regression run: `python -m unittest discover -s tests -t .` passes 100% (>= 836 baseline + new tests)
  - Engine process check: verify `src.engine` PID 15744 running uninterrupted
  - Update `ops/board.md` with statuses of all implemented insights
  - Deliver final handoff report to Sentinel
