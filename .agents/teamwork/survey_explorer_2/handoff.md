# Handoff Report: Codebase Architecture & Runtime Survey (Phase Survey)

**Agent ID:** `survey_explorer_2` (Codebase Architecture Explorer)  
**Parent Caller:** `parent` (`0392c235-01a5-43cd-9010-ac02cbb6d35c`)  
**Date:** 2026-09-30  
**Status:** Hard Handoff (Survey Completed)  
**Artifact Referenced:** `c:\TG\BOT\TRADE DEMO 1\.agents\teamwork\survey_explorer_2\survey_architecture.md`  

---

## 1. Observation

Direct observations and evidence collected from the codebase, runtime environment, and test suite:

### 1.1 Running Process & Engine Continuity (`src/engine.py`)
- **Process State:** PID `15744` is actively running since `2026-09-30 02:59:52 (+05:00)` executing the Phase 1 72-hour qualifying demo run.
- **Engine Loops:**
  - `_reconcile_loop` (interval `60s`): reconciles orders and trades via `reconciler.sync_orders` / `reconciler.sync_trades`. Arms `faulthandler.dump_traceback_later(150.0)` for hang detection.
  - `_equity_loop` (interval `300s`): queries `ex.private_get_account_balance`, updates SQLite `EquityRecord` in `data/bot_state.db`, and calls `risk.update_equity(total)`.
  - `_flag_loop` (interval `2s`): monitors `data/KILL` and `data/STOP_ENGINE` flags using `read_flag_text`.
- **Continuity Protection:** Windows sleep prevention via `SetThreadExecutionState(ES_CONTINUOUS | ES_SYSTEM_REQUIRED)`. Monitored by `src/engine_watchdog.py`.

### 1.2 Risk Core Invariants (`src/risk.py`)
- **Stateful SQLite:** Persisted in `data/risk_state.db`.
- **Parameters:**
  - `DEFAULT_RISK_PCT = 0.01` (1.0%), `MAX_RISK_PCT = 0.02` (2.0%).
  - `MAX_PORTFOLIO_HEAT_PCT = 0.06` (6.0%), `MAX_POSITION_PCT = 0.15` (15.0% notional).
  - `MAX_OPEN_POSITIONS = 2`, `MAX_ENTRIES_PER_DAY = 10`.
  - `DAILY_LOSS_LIMIT_PCT = 0.06` (6.0% daily loss, auto-reset at 00:00 UTC).
  - `GLOBAL_DD_LIMIT_PCT = 0.15` (15.0% drawdown breaker from HWM, manual reset only).
  - `INST_LOSS_STREAK_BLOCK = 3` (3 consecutive losses blocks instrument for 24h).
  - `SYS_LOSS_STREAK_PAUSE = 5` (5 consecutive losses pauses system for 24h).
  - `EQUITY_MAX_AGE_S = 600.0` (freshness ceiling; stale equity blocks entries).
  - `MAX_LEVERAGE = 3` (hard ceiling on derivatives, isolated margin only).
- **Critical Methods:**
  - `check_entry_allowed(inst_id, side)`: 10-step validation chain.
  - `size_position(equity, entry, stop, ct_val, lot_sz, min_sz, risk_pct)`: Fixed-fractional formula with 15% notional ceiling and lot/min size rounding.
  - `check_exit_allowed(inst_id, side, sz)`: Special exit pipeline bypassing entry breakers and equity age check.
  - `record_pnl(inst_id, pnl, closed_at)`: Updates `day_pnl` and streaks; never modifies equity or HWM (`RISK-PNL-DOUBLE` prevention).

### 1.3 Order Router Pipeline (`src/order_router.py`)
- Gatekeeper for all order placement.
- Step-by-step pipeline in `place_order`:
  1. `risk.check_entry_allowed`
  2. `risk.size_position` or size validation
  3. `risk.validate_stop_vs_liquidation`
  4. Account mode check via `account_mode.spot_order_params` (`tdMode="cash"` for `acctLv` 1–2, `"cross"` for `acctLv` 3–4 with `check_no_borrow` verification)
  5. Throttler check (`_PlaceThrottler`: 20 orders per 2 seconds)
  6. Owner prefix tagging via `order_owner.new_cl_ord_id` and request expiration `expTime` (10,000 ms)
  7. Execution via `exchange.create_order` and optional `fetch_order` confirmation
  8. State storage (`Storage.upsert_order`) and risk slot registration (`risk.register_entry`, `risk.register_entry_size`)
- Exit pipeline in `place_exit_order`: handles `reduceOnly=True` for SWAP contracts and releases risk slots via `risk.release_position`.
- Emergency dispatch: registered callback calls `connector.emergency_stop` on kill-switch trigger.

### 1.4 Order Ownership & Prefix Catalog (`src/order_owner.py`)
- Enforces OKX constraint: `clOrdId` must be 1 to 32 alphanumeric lowercase characters (`^[a-z0-9]{1,32}$`).
- Current registry:
  - `bot`: default engine prefix (`own=True`)
  - `botr`: order router default (`own=True`)
  - `botsdca`: demo DCA bot (`own=True`)
  - `botldca`: live DCA bot (`own=True`)
  - `trd`: OKX trader agent and native fleet bots (`own=False`, tracked as external)
  - `pmp`, `sen`, `iex`, `hnt`, `usr`: other role and human prefixes.
- Reconciler classification: only prefixes starting with `bot*` are matched as internal system orders (`own=True`).

### 1.5 Fleet Manager Catalog & Capacities (`src/fleet_manager.py`)
- Supported bot types (13 native OKX types): `spot_grid`, `contract_grid_usdt`, `contract_grid_coin`, `smart_portfolio`, `contract_dca`, `smart_arbitrage`, `dcd_pendulum`, `spot_dca`, `recurring_buy`, `signal_bot`, `iceberg`, `twap`, `arbitrage`.
- Invariants:
  - `FLEET_MAX_BOTS = 50`.
  - `reserve_usdt = total_equity * 0.30` (30% liquidity buffer).
  - Single bot cap: `max_single_bot_cap = total_equity * 0.02` (2.0% equity maximum).
  - Allocation: `recommended_single_bot_usdt = min(max_single_bot_cap, total_equity * 0.70 / target_bots)`, with minimum 50 USDT.
  - Derivative leverage ceiling: `min(lever, 3)` (isolated).
  - Order prefix: `trd` (external agent category, routed to sleeve `demo_fleet`).

### 1.6 Existing Test Suite Verification
- Command executed: `.venv\Scripts\python.exe -m unittest discover -s tests -t .`
- Result: **836 tests passed**, 0 failures, 0 errors, 1 skipped in 105.150s.

---

## 2. Logic Chain

1. **Premise 1 (Non-disruption of Engine):** Since PID 15744 is actively in the middle of its 72-hour qualifying run and monitors `data/KILL` and `data/STOP_ENGINE` flags every 2s, all new development for R1–R5 must consist of additive, decoupled modules that communicate via shared databases (`data/bot_state.db`, `data/risk_state.db`), standard Python library imports, or CLI pipelines, requiring **zero engine restarts**.
2. **Premise 2 (Order Attribution & Reconciler Compatibility):** From Observation 1.4, `src/order_owner.py` classifies any prefix starting with `bot*` as internal system orders (`own=True`). Therefore, to enable automatic trade sync and reconciliation for R1 (Funding Carry), R2 (Mean Reversion), and R3 (Idle Cash Earn), new unique prefixes must be registered in `src/order_owner.py`:
   - `botcar` -> Funding Carry Arbitrage
   - `botmr` -> Mean Reversion Strategy
   - `bottrn` -> Treasury Idle Cash Earn
3. **Premise 3 (Risk Gatekeeping):** From Observation 1.2 and 1.3, `src/risk.py` enforces a strict 2-position limit (`MAX_OPEN_POSITIONS = 2`) and 6% total risk heat. Direct calls to CCXT bypass these checks and corrupt risk accounting. Therefore, R1 and R2 must execute orders exclusively through `OrderRouter.place_order` and `OrderRouter.place_exit_order`.
4. **Premise 4 (Fleet Scalability vs Discrete Risk Slots):** From Observation 1.5, native OKX grid/DCA/arbitrage bots run on exchange-side algorithms. They are managed by `src/fleet_manager.py` up to the 50-bot quota with a 30% equity reserve and 2% per-bot allocation. They use the `trd` prefix and are attributed to `demo_fleet`, operating harmoniously alongside the discrete 2-position risk slots.
5. **Premise 5 (Demo API Isolation for Simple Earn):** OKX Demo environment returns error code `50038` ("This feature is unavailable in demo trading") for Simple Earn Flexible endpoints. Therefore, `src/idle_earn.py` must encapsulate an exchange adapter that transparently switches to a local mock/simulator in demo mode while providing genuine REST calls in live mode, allowing testing without API errors.
6. **Premise 6 (Offloading to Muse Code):** From `ops/delegations/` and `ops/delegate.ps1`, subtasks like test mock generation and parameter tuning tables can be dispatched to Meta Muse Code via PowerShell without blocking or impacting runtime safety.

---

## 3. Caveats

1. **Earn API in Demo Environment:** OKX Demo API does not support Simple Earn endpoints (error `50038`). Live testing of R3 (Idle Cash Earn) cannot be performed on OKX demo infrastructure; tests must rely on unit mocks and local ledger simulations until live credentials and explicit human permission are granted.
2. **Capital Efficiency Factor in Funding Carry:** Funding Carry requires equal capital for Spot Long ($1,000) and 1x Isolated Swap Short ($1,000 margin), yielding a capital efficiency factor of 0.5x. Effective APY on total capital is half the nominal funding rate (e.g., nominal 5.84% APR yields 2.92% on allocated equity).
3. **Bar Completion in Mean Reversion:** The RSI(14) and Bollinger Bands logic in `src/backtest/meanrev.py` assumes completed 1H candles. Real-time execution in `src/meanrev_strategy.py` must synchronize with candle close times (`:00:05` UTC) to prevent false signals caused by mid-bar intra-candle fluctuations.
4. **Database Lock Contention:** The running engine process (PID 15744) continually accesses `data/bot_state.db` and `data/risk_state.db`. External tools and modules must utilize WAL mode and short, read-only transactions to avoid `sqlite3.OperationalError: database is locked`.

---

## 4. Conclusion

The architecture of the OKX trading bot is cleanly layered, modular, and fully capable of hosting the planned trading strategies and fleet expansion:
- **OrderRouter & RiskManager** provide robust gatekeeping, position sizing, liquidation buffers, and account mode enforcement.
- **FleetManager** is ready to orchestrate 50 native bots across 13 types while protecting account solvency through the 30% reserve and 2% single-bot limit.
- **OrderOwner** provides a clear, 32-character prefix convention that ensures full traceability of all orders and sleeve PnL attribution.
- **Engine Continuity (PID 15744)** can and must be preserved during all subsequent implementation phases by adopting a strictly modular, additive design pattern.

Detailed blueprints, interface matrices, and phased implementation roadmaps have been compiled into `c:\TG\BOT\TRADE DEMO 1\.agents\teamwork\survey_explorer_2\survey_architecture.md`.

---

## 5. Verification Method

To independently verify the observations, architecture readiness, and system stability:

1. **Verify Unit Test Suite Health:**
   ```powershell
   .venv\Scripts\python.exe -m unittest discover -s tests -t .
   ```
   *Expected outcome:* Exactly 836+ tests passing, 0 failures, 0 errors.

2. **Verify Engine Continuity (PID 15744):**
   ```powershell
   Get-Process -Id 15744 | Select-Object Id, ProcessName, StartTime, CPU
   ```
   *Expected outcome:* Process exists, started at `2026-09-30 02:59:52 (+05:00)` or earlier, zero unintended restarts.

3. **Verify Engine Operational Heartbeat:**
   ```powershell
   Get-Content logs/engine.log -Tail 30
   ```
   *Expected outcome:* Heartbeat records showing active `_reconcile_loop` (interval 60s) and `_equity_loop` (interval 300s) without errors or divergences.

4. **Verify Risk & Fleet Status Command:**
   ```powershell
   .venv\Scripts\python.exe -m src.ops status
   ```
   *Expected outcome:* Healthy engine status, risk limits active, zero trip breakers, and fleet configuration visible.

5. **Inspect Architecture Document:**
   Review `c:\TG\BOT\TRADE DEMO 1\.agents\teamwork\survey_explorer_2\survey_architecture.md` for full implementation blueprints and interface specifications.
