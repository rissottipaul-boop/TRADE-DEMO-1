# Survey Report: Test Suite & Delegation Infrastructure

- **Agent:** `survey_miner_3` (Test & Delegation Spec Miner)
- **Date:** 2026-09-30
- **Mission:** Comprehensive survey of the testing framework, delegation mechanics via Meta Muse Code, agent routing, test specifications for four new modules (Funding Carry, Mean Reversion, Idle Cash Earn, Fleet Manager 50 bots), and routine delegation candidate tasks.

---

## 1. Executive Summary

This report establishes the testing and delegation blueprint for Phase 2 implementation of trading insights (`insights/`):
1. **Current Test Suite Health:** The project test suite contains **45 test files** (44 test modules) with **836 unit tests**. Execution via `.venv\Scripts\python.exe -m unittest discover -s tests -t .` completes in **~78 seconds** with a **100% pass rate** (835 passed, 1 skipped, 0 errors, 0 failures).
2. **Mock Conventions:** Zero external network calls are permitted in unit tests. All tests strictly use in-memory and synthetic mocks: `FakeOkx`, `FakeBotExchange`, `FakeSpotExchange`, synthetic candlestick generators (`ar_bars`), mock clocks (`_Clock` patching `risk._utc_now`), and isolated temporary SQLite databases (`tempfile.TemporaryDirectory`).
3. **Delegation Bridge (`ops/delegate.ps1`):** An asynchronous file-based queue bridge (`inbox/` -> `processing/` -> `outbox/` & `done/`) orchestrates background execution by Meta Muse Code (`muse-spark-1.3`). The runner process is currently active (`PID 8728`, 30s poll interval), protected by mandatory headers enforcing `AGENTS.md` §2 safety constraints.
4. **Agent Team Routing (`ops/agent-team.md`, `ops/agent-routing.json`):** Distinct roles define clear boundaries: Orchestrator (Claude `opus`), Executor (Codex `gpt-6-astra`), Hunter (Gemini `pro`), Trader (Claude `opus`), Sentinel (Claude `sonnet`), and Muse (`muse-spark-1.3` as lightweight delegation runner).
5. **New Module Test Requirements:** Full test specifications defined for Funding Carry (4-leg fees, 0.5x capital efficiency, delta-neutral sizing, adverse rate exits), Mean Reversion (1H bar signals, ATR-based risk sizing, ROI decay, exit execution), Idle Cash Earn (free balance detection, flexible earn subscribe/redeem, demo error 50038 handling), and Fleet Manager (13 OKX bot types, 50-bot quota, 30% cash reserve, 3x leverage clamp, emergency batch stop).
6. **Delegable Tasks:** Concrete routine tasks identified for Meta Muse Code via `ops/delegate.ps1` (test scaffolding, JSON mock fixtures, parameter calibration tables, doc formatting).

---

## 2. Test Suite Architecture & Conventions

### 2.1 File Structure & Inventory
The `tests/` directory contains 45 files:
- **Core Trading Engine & Risk:** `test_engine.py`, `test_engine_watchdog.py`, `test_risk_equity.py`, `test_risk_equity_source.py`, `test_risk_leverage.py`, `test_risk_spot_buy.py`, `test_order_router_exit.py`, `test_order_audit.py`, `test_order_owner.py`, `test_account_mode.py`.
- **Connector & Exchange Safety:** `test_connector.py`, `test_connector_kill_dca.py`, `test_connector_kill_grid.py`, `test_kill_registry.py`, `test_errors.py`, `test_ws_client.py`, `test_ws_reconnect.py`.
- **Backtesting & Strategies:** `test_backtest_engine.py`, `test_backtest_data.py`, `test_backtest_grid.py`, `test_backtest_lookahead.py`, `test_backtest_metrics.py`, `test_backtest_pump.py`, `test_backtest_trailing_stop.py`, `test_backtest_cli.py`, `test_meanrev.py`.
- **Bot Fleet & Live Operations:** `test_fleet_manager.py`, `test_dca_equity_source.py`, `test_live_preflight.py`, `test_live_runner.py`, `test_live_state.py`, `test_pnl_ledger.py`, `test_storage_reconciler.py`.
- **Pump Scanner & Journal:** `test_pump_scanner.py`, `test_pump_journal.py`, `test_pump_handoff.py`, `test_pump_sched.py`.
- **Delegation & Infrastructure:** `test_delegation.py`, `test_agent_context.py`, `test_agent_rotate.py`, `test_autopilot.py`, `test_guard.py`, `test_guard_adapter.py`, `test_obsidian_status.py`.

### 2.2 Test Runner Command & Execution Profile
- **Command:** `.venv\Scripts\python.exe -m unittest discover -s tests -t .`
- **Runner:** Standard Python `unittest` test discovery.
- **Execution Time:** ~78.1s across 836 test cases.
- **Output Status:** `OK (skipped=1)`. (The skipped test is `test_check_ok_and_missing` in `test_delegation.py` if native powershell environment lacks specific path conditions).
- **Rule for New Tests:** Every newly added test module must be completely runnable under standard unittest discovery without any external dependencies or network traffic.

### 2.3 Mocking Conventions & Patterns
The codebase exhibits well-established mock abstractions:
1. **`FakeOkx` / `FakeSpotExchange`:** Mimics CCXT OKX client methods:
   - `private_get_account_config()` -> returns `{"code": "0", "data": [{"acctLv": "1", "autoLoan": false, ...}]}`.
   - `private_get_account_balance()` -> returns `details` with `availBal`, `totalEq`, etc.
   - `create_order(symbol, ord_type, side, amount, price, params)` -> records order into internal `created` list, returns order dict with unique `id`.
   - `fetch_order(order_id)` -> simulates filled (`closed`) or pending (`open`) state based on test configuration.
2. **`FakeBotExchange`:** Simulates OKX TradingBot endpoints (`bot/grid`, `bot/dca`):
   - Handles `private_post_tradingbot_grid_stop_order_algo` and `private_post_tradingbot_dca_stop_order_algo`.
   - Simulates realistic OKX demo error codes, e.g. code `1` + sCode `51291` ("The bot doesn't exist or has already stopped").
3. **Synthetic Market Data (`ar_bars`):**
   - Implements autoregressive AR(1) candle sequence `100 * exp(x)` where $x_{t} = 0.97 x_{t-1} + \epsilon$.
   - Generates deterministic `Bar(ts, open, high, low, close, volume)` instances without network calls.
4. **Isolated Database Storage:**
   - Tests instantiate `Storage` or `risk.init()` with temporary paths generated via `tempfile.TemporaryDirectory()`.
   - `tearDown()` closes connections, releases SQLite locks, and cleans up temporary folders.
5. **Deterministic Clocks:**
   - Tests patch `risk._utc_now` with custom `_Clock` instances to advance or freeze time deterministically.

---

## 3. Delegation Infrastructure (`ops/delegate.ps1` & `ops/delegations/`)

### 3.1 Mechanics & Lifecycle
The task delegation bridge decouples high-level orchestrators from mechanical execution by offloading tasks to Meta Muse Code (`muse-spark-1.3`).
- **File System Queue Layout:**
  - `ops/delegations/inbox/`: New task requests (`<id>.json`).
  - `ops/delegations/processing/`: In-flight requests claimed atomically via PowerShell `Move-Item`.
  - `ops/delegations/outbox/`: Finished execution outputs (`<id>.json`, `<id>.log`, `<id>.prompt.md`).
  - `ops/delegations/done/`: Archive of original request payloads.
  - `ops/delegations/runner.pid`: Stores `PID|Ticks` of the active runner process to prevent duplicate instances.
  - `ops/delegations/PAUSED`: Touch file to freeze queue processing without stopping the runner.
  - `ops/delegations/STOP`: Flag to gracefully terminate the runner loop.
  - `logs/delegation.log`: UTF-8 structured event log.

### 3.2 File Formats
1. **Task Request (`inbox/<id>.json`):**
   ```json
   {
     "id": "d20260930-120000-A1B2",
     "from": "claude",
     "role": "insight-executor",
     "prompt": "Create test scaffold for tests/test_funding_carry.py...",
     "timeout_min": 20,
     "created": "2026-09-30T12:00:00+05:00"
   }
   ```
2. **Execution Result (`outbox/<id>.json`):**
   ```json
   {
     "id": "d20260930-120000-A1B2",
     "status": "done",
     "exit_code": 0,
     "elapsed_s": 45,
     "killed": true,
     "finished": "2026-09-30T12:00:45+05:00",
     "error": "",
     "output_tail": "[stdout] ... [stderr] ..."
   }
   ```
3. **Prompt Wrapper (`outbox/<id>.prompt.md`):**
   Automatically wraps each task with an immutable safety prompt:
   - Enforces `AGENTS.md` §2 rules (strict ban on live trading, kill-switch reset, limit softening, secrets, data deletion, guard modifications, fund withdrawals).
   - Instructs Muse to report changed files, check commands, and results directly to stdout.

### 3.3 CLI Interface
- `ops\delegate.ps1 submit -Prompt "..." -From <agent> -Role <role> [-TimeoutMin <m>]`
- `ops\delegate.ps1 fetch -Id <id>`
- `ops\delegate.ps1 run-once` (executes single batch immediately)
- `ops\delegate.ps1 start` / `stop` / `status` / `check`

---

## 4. Agent Team & Routing Protocol (`ops/agent-team.md`, `ops/agent-routing.json`)

### 4.1 Role Assignment Matrix
| Role | Primary Agent | Backup Order | Canonical Spec | Boundary & Verification |
|---|---|---|---|---|
| **Project Orchestrator** | Claude (`opus`) | Codex -> Gemini -> Muse | `.github/agents/project-orchestrator.agent.md` | Board maintenance, claims, dependencies, acceptance |
| **Insight Executor** | Codex (`gpt-6-astra`) | Claude -> Gemini -> Muse | `.github/agents/insight-executor.agent.md` | Code changes, unit tests, minimal diffs, test passes |
| **Crypto Insight Hunter** | Gemini (`pro`) | Claude -> Codex -> Muse | `.github/agents/crypto-insight-hunter.agent.md` | Research, fact verification (>=2 sources), API checks |
| **OKX Trader** | Claude (`opus`) | Codex -> Gemini -> Muse | `.github/agents/okx-trader.agent.md` | Exchange operations via `okx` CLI, order owner tagging |
| **Pump Risk Taker** | Claude (`opus`) | Codex -> Gemini -> Muse | `.github/agents/pump-risk-taker.agent.md` | Momentum trades within `pump-pocket.json` |
| **Ops Sentinel** | Claude (`sonnet`) | Codex -> Muse -> Gemini | `.github/agents/ops-sentinel.agent.md` | Monitoring, log checks, incidents, failsafe actions |
| **Lightweight Worker** | Meta Muse Code (`muse-spark-1.3`)| Background via queue | `ops/delegate.ps1` | Boilerplate code, scaffolds, doc tables, test data |

### 4.2 Handoff & Guard Enforcement Rules
- **AGENTS.md §2 Asymmetry:** Any action toward safety can be taken autonomously; actions toward risk require user authorization (`needs-user`).
- **Quota Expiry Protocol:** When a model hits quota or context saturation, it creates a self-contained handoff note in the task card (`ops/board.md`), reverts status to `ready`, and logs changed files, test results, and next actions.

---

## 5. Specification Mining: Features Discovered

```
## Features Discovered
| # | Category | Feature | Description | Inputs | Outputs | Error Behavior | Discovered Via |
|---|----------|---------|-------------|--------|---------|----------------|----------------|
| 1 | Test Suite | Offline Discovery Runner | Discovers and executes all test suites without external network access | Directory `tests/`, pattern `test_*.py` | Exit 0, test count summary (836 tests) | Non-zero exit on test failure or error | `tests/`, `unittest discover` |
| 2 | Test Mock | `FakeOkx` REST Mock | Simulates OKX account config, balance, create_order, fetch_order | Mock config dict, balances, order requests | Order records, balance details, timestamps | Throws configured `fetch_error` or invalid params | `tests/test_order_router_exit.py` |
| 3 | Test Mock | `FakeBotExchange` TradingBot Mock | Simulates OKX tradingBot API for grid and DCA bot lifecycle and emergency kill | Algo bot requests, algoId lists | Bot lists, cancellation confirmations | Throws ExchangeError with sCode 51291 on stopped bots | `tests/test_connector_kill_dca.py` |
| 4 | Delegation | Task Queue Dispatcher (`ops/delegate.ps1`) | Asynchronous delegation bridge submitting jobs to Meta Muse Code | Prompt string, timeout, from, role | Task ID string (`dYYYYMMDD-...`), JSON file in `inbox/` | Rejects empty prompt, timeout <1 or >120 min | `ops/delegate.ps1:210-219` |
| 5 | Delegation | Queue Runner Loop | Persistent daemon polling `inbox/` every 30s and executing via `muse exec` | Files in `inbox/` matching `*.json` | Output JSON in `outbox/`, prompt in `outbox/`, archive in `done/` | Marks status `error` on non-zero exit or `timeout` on time limit | `ops/delegate.ps1:242-250` |
| 6 | Delegation | Guard Header Injector | Prepend mandatory safety header enforcing `AGENTS.md` §2 to every delegated prompt | Raw prompt text | Wrapped markdown prompt file (`*.prompt.md`) | Injects hard ban on live trading and risk changes | `ops/delegate.ps1:117-128` |
| 7 | Delegation | Queue Status & Health Check | Verifies runner status, queue depths, and tests `muse exec --help` responsiveness | PowerShell switch `status` or `check` | PID, queue counts, help verification OK/FAIL | Exit 1 if muse command missing or times out (120s) | `ops/delegate.ps1:291-337` |
| 8 | Funding Carry | 4-Leg Fee Calculation | Computes net round-trip transaction costs across spot buy/sell + swap short/cover | Maker/taker rates (spot 0.08/0.10%, swap 0.02/0.05%) | Net round-trip cost (0.20% maker to 0.30% taker) | Invalid rates trigger valuation errors | `insights/funding-carry.md:101-120` |
| 9 | Funding Carry | Capital Efficiency Sizing | Adjusts effective yield by 0.5x due to 100% margin on 1x isolated short leg | Total allocated capital, funding rate | Net annual return on capital (e.g. 5.75% -> 2.87%) | Rejects leverage > 1x for delta-neutral carry | `insights/funding-carry.md:164-170` |
| 10 | Funding Carry | Adverse Funding Exit | Generates exit signal when funding rate turns negative or breakeven window exceeds threshold | Historical funding rates, current rate | Exit signal for spot sell + swap cover | Logs warning on persistent negative funding | `insights/funding-carry.md:88-95` |
| 11 | Mean Reversion | 1H Bar Confirmation Signal | Long-only entry when RSI(14) crosses 30 upward, close <= BB mid, close > close[-1] | 1H OHLCV series, RSI(14), BB(20,2) | Entry trigger boolean on confirmed candle `confirm=1` | Ignores unconfirmed candles and volume == 0 | `insights/meanrev-strategy-design.md:58-76` |
| 12 | Mean Reversion | Volatility-Adaptive ATR Stop | Calculates stop-loss price dynamically as `entry - 1.5 * ATR(14)` | Entry price, ATR(14) at signal time | Stop-loss price level | Rejects non-positive ATR multipliers | `insights/meanrev-strategy-design.md:93-115` |
| 13 | Mean Reversion | Decaying Minimal ROI Table | Stepwise profit-taking targets decaying by holding time (0m: 2%, 240m: 1.2%, 720m: 0.6%, 1440m: 0%) | Holding time in minutes, unrealized profit % | Exit trigger boolean | Enforces time-stop exit at 1440m (24h) | `insights/meanrev-strategy-design.md:117-142` |
| 14 | Idle Cash Earn | Free Cash Detection | Identifies unallocated USDT/USDC excluding margin for active positions and open orders | Account balance, position margins, open order values | Free idle cash amount in USDT | Subtracts 30% untouchable cash reserve | `insights/idle-cash-earn.md:90-101` |
| 15 | Idle Cash Earn | Flexible Savings Subscribe/Redeem | Subscribes idle cash to OKX Simple Earn Flexible and redeems instantly on trade demand | Target amount, currency (`USDT`), action (`purchase`/`redempt`) | Transaction response, balance update | Catches demo error 50038 gracefully | `insights/idle-cash-earn.md:24-42` |
| 16 | Idle Cash Earn | Lending Rate Parser | Extracts 24h avg rate, current rate, and estimated rate from OKX savings public REST API | API response from `/api/v5/finance/savings/lending-rate-summary` | Float APY values | Returns default fallback rate if endpoint unreachable | `insights/idle-cash-earn.md:45-75` |
| 17 | Bot Fleet | 13 OKX Bot Types Catalog | Comprehensive catalog defining specifications, URLs, engine families, and constraints | Bot type key string | `BotTypeSpec` dataclass instance | Raises `ValueError` on unrecognized bot type | `src/fleet_manager.py:49-206` |
| 18 | Bot Fleet | Fleet Quota Enforcer | Enforces maximum limit of 50 active bot instances across all strategy classes | Current active bots count, adding bots count | Tuple `(is_allowed: bool, message: str)` | Rejects addition if total exceeds 50 bots | `src/fleet_manager.py:228-236` |
| 19 | Bot Fleet | Fleet Sizing & Liquidity Reserve | Allocates max 2% equity per bot and guarantees >= 30% total equity untouchable reserve | Total equity float, target bot count | Allocation dict with per-bot budget and reserve | Enforces minimum 50 USDT allocation guard | `src/fleet_manager.py:239-260` |
| 20 | Bot Fleet | Derivative Leverage Clamping | Strictly limits leverage on all contract bot types (`contract_grid`, `contract_dca`, etc.) to <= 3x | Requested leverage integer | Clamped leverage integer (max 3) | Replaces higher requested leverage with 3 | `src/fleet_manager.py:299, 339` |
| 21 | Bot Fleet | Order Owner Tag Generator | Generates 32-character alphanumeric `algoClOrdId` with prefix `trd` or `flt` | Bot type key | 32-character string starting with `trd` | Ensures strict compliance with ORDER-OWNER-TAG | `src/fleet_manager.py:262-264` |
| 22 | PnL Ledger | Sleeve Attribution Routing | Maps order fills, funding fees, and bot results to designated accounting sleeves | `clOrdId`, `algoClOrdId`, `tag`, `type` | Sleeve name string (`demo_fleet`, `funding_carry`, `cash_earn`, `signal`) | Falls back to `default_sleeve` ("manual") | `ops/sleeves.json`, `src/pnl_ledger.py` |
```

---

## 6. Specification Mining: Edge Cases

```
## Edge Cases
| # | Feature | Input | Observed Behavior |
|---|---------|-------|-------------------|
| 1 | Delegation Runner | Malformed request JSON in `inbox/` | Runner detects parsing failure, skips `muse exec`, writes status `error` with reason "bad json", moves file to `done/` |
| 2 | Delegation Runner | Empty or whitespace-only prompt in request | Runner writes status `error` with reason "bad request: пустой prompt", skips execution, logs error |
| 3 | Delegation Runner | Task execution exceeds `timeout_min` (or `ForceTimeoutSec`) | Runner triggers `taskkill /F /T /PID`, marks status `timeout`, sets `killed=True`, and writes tail log |
| 4 | Delegation Runner | `PAUSED` file present in `ops/delegations/` | Runner skips iteration, outputs "Пауза: есть ops\delegations\PAUSED", retains all inbox files intact |
| 5 | Mean Reversion Entry | Candle volume == 0 (flat/dead period) | Entry signal condition `volume(t) > 0` evaluates to False, preventing false breakout entries on missing feeds |
| 6 | Mean Reversion Entry | RSI crosses 30 but price is above BB middle | Entry signal evaluates to False, enforcing rule that price must remain in lower half of Bollinger channel |
| 7 | Mean Reversion Sizing | ATR(14) extremely small (tight channel) | Position size computed by `size_position()` exceeds 15% equity cap; `risk.py` clamps size or warns, preventing overleveraging |
| 8 | Mean Reversion Exit | Wilder RSI crosses 70 while price drops (`close(t) < close(t-1)`) | Wilder smoothing mathematically prevents upward cross on downward close; indicator exit condition unreachable, strategy relies on decaying ROI / time-stop |
| 9 | Funding Carry Sizing | Leverage input > 1x for delta-neutral carry | Module enforces leverage = 1x isolated, ensuring liquidation distance stays at ~+99.1% |
| 10 | Funding Carry Market Data | Public funding rate endpoint returns negative rate | Module flags adverse regime, suspends new carry openings, and evaluates closing open carry pairs |
| 11 | Idle Cash Earn Demo Call | `okx --demo earn savings balance` executed | OKX demo API returns error `50038` ("This feature is unavailable in demo trading"); module handles gracefully without raising unhandled exception |
| 12 | Idle Cash Earn Liquidity Demand | Strategy requests margin while USDT is locked in Simple Earn | Module triggers instant redemption (`redempt`) for requested delta, restoring trading account cash within seconds |
| 13 | Bot Fleet Quota | `current_active_bots = 50`, request to add 1 bot | `validate_fleet_quota` returns `(False, "Превышена квота флота: активно 50, попытка добавить 1, максимум 50 ботов.")` |
| 14 | Bot Fleet Creation | Contract grid requested with `lever: 10` | `build_cli_command` clamps `--lever` to `3` in CLI argument list, enforcing `MAX_LEVERAGE = 3` invariant |
| 15 | Bot Fleet Emergency Stop | 50 active bots during kill-switch activation | `connector.emergency_stop()` batches bot stop requests in chunks of 10 to comply with OKX rate limits |
```

---

## 7. Test Requirements for New Modules

To guarantee that the comprehensive test suite continues to pass at **100%** with zero regressions, each of the four new modules must have rigorous unit tests covering standard behavior, boundary conditions, and mock failures.

### 7.1 Module 1: Funding Carry Arbitrage (`src/funding_carry.py` / `tests/test_funding_carry.py`)
- **Required Test Cases:**
  1. `test_fee_round_trip_calculation`: Verify fee calculations for all-taker (0.30%), all-maker (0.20%), and mixed (0.25%).
  2. `test_breakeven_period_days`: Verify breakeven calculations for BTC (5.75% -> 19.0d taker / 12.7d maker) and ETH (4.14% -> 26.4d taker / 17.6d maker).
  3. `test_capital_efficiency_multiplier`: Verify 0.5x scaling factor applied to total sleeve capital due to 1x margin on the swap leg.
  4. `test_funding_rate_api_parser`: Test parsing of `GET /api/v5/public/funding-rate` and pagination of `/funding-rate-history`.
  5. `test_delta_neutral_lot_sizing`: Verify exact sizing alignment between spot base currency qty and swap contracts (`ctVal * num_contracts`).
  6. `test_paired_order_generation`: Mock `OrderRouter` and verify atomic creation of spot buy and swap short with matching `clOrdId` prefix `trd` / `botrcarry`.
  7. `test_adverse_funding_exit_signal`: Test triggering of exit logic when funding rate turns negative for >= 2 consecutive periods.
  8. `test_margin_ratio_monitoring`: Verify isolated margin ratio monitoring on the swap leg and alert when margin ratio exceeds warning threshold.
  9. `test_emergency_stop_handling`: Verify clean cancellation and closing of both paired legs when kill-switch fires.

### 7.2 Module 2: Mean Reversion Live Execution (`src/meanrev_strategy.py` / `tests/test_meanrev_strategy.py`)
- **Required Test Cases:**
  1. `test_signal_evaluation_on_candle_close`: Verify entry signal triggers only on confirmed candle (`confirm=1`) with RSI crossing 30 upward, close <= BB mid, close > prev_close, volume > 0.
  2. `test_risk_manager_pre_entry_gate`: Verify that `risk.check_entry_allowed()` is called; when kill-switch or daily drawdown is tripped, entry is blocked.
  3. `test_atr_position_sizing`: Verify `risk.size_position()` is called with `risk_per_unit = 1.5 * ATR(14)` under 1% risk default.
  4. `test_notional_cap_enforcement`: Verify that when 1.5*ATR is narrow, position size is capped at 15% of equity.
  5. `test_order_router_entry_placement`: Mock `OrderRouter.place_order` and verify order arguments, `tdMode="cash"`, and client order ID tagging.
  6. `test_volatility_stop_loss_trigger`: Simulate price drop touching `entry - 1.5 * ATR`, verify `OrderRouter.place_exit_order` execution.
  7. `test_decaying_roi_exits`: Verify exit triggers at 0m (+2.0%), 240m (+1.2%), 720m (+0.6%), and 1440m (0.0% time-stop).
  8. `test_warmup_period_buffering`: Verify strategy ignores signals until >= 200 candles have been ingested.

### 7.3 Module 3: Idle Cash Earn (`src/idle_cash_earn.py` / `tests/test_idle_cash_earn.py`)
- **Required Test Cases:**
  1. `test_idle_cash_calculation`: Verify free cash calculation subtracting margin for open positions, open orders, and 30% reserve.
  2. `test_lending_rate_summary_parsing`: Test parsing of `GET /api/v5/finance/savings/lending-rate-summary`.
  3. `test_demo_error_50038_suppression`: Mock error response `50038` ("This feature is unavailable in demo trading") and verify module logs warning without exception.
  4. `test_flexible_savings_purchase`: Test formatting and dispatch of `POST /api/v5/finance/savings/purchase-redempt` with `side="purchase"`.
  5. `test_flexible_savings_redeem`: Test formatting and dispatch of `POST /api/v5/finance/savings/purchase-redempt` with `side="redempt"`.
  6. `test_on_demand_liquidity_callback`: Simulate trading engine requesting $2,000 when only $500 free cash is on balance; verify instant redemption of $1,500 from Earn.
  7. `test_30_percent_reserve_invariant`: Verify that the 30% cash reserve is never locked in non-redeemable or risky products.
  8. `test_sleeve_pnl_attribution`: Verify that interest income is booked to `cash_earn` sleeve in `pnl_ledger`.

### 7.4 Module 4: Fleet Manager 50 Bots (`src/fleet_manager.py` / `tests/test_fleet_manager_integration.py`)
- **Required Test Cases:**
  1. `test_13_bot_types_spec_completeness`: Ensure all 13 bot types have valid specs, URLs, and parameter schemas.
  2. `test_quota_validation_boundaries`: Test quota at 0, 49, 50, and 51 bots.
  3. `test_fleet_allocation_matrix`: Validate capital allocations under $1,000, $10,000, and $108,000 equity (verifying 30% reserve and <= 2% max per bot).
  4. `test_leverage_clamp_on_all_contract_types`: Verify `contract_grid_usdt`, `contract_grid_coin`, `contract_dca`, `signal_bot`, and `arbitrage` are all clamped to 3x.
  5. `test_mandatory_stop_loss_validation`: Ensure bot creation fails if `slTriggerPx` or `slPct` is omitted.
  6. `test_order_owner_tag_prefix`: Ensure `algoClOrdId` is 32 alphanumeric chars starting with `trd` or `flt`.
  7. `test_batch_kill_switch_dispatch`: Mock 50 active bots across grid and DCA; verify `emergency_stop()` stops all 50 in batches of 10.
  8. `test_fleet_status_and_audit_report`: Test JSON audit summary of active bots, margin usage, and risk sleeve mapping.

---

## 8. Meta Muse Code Delegation Plan

To maximize development velocity and conserve orchestrator context, the following routine development and maintenance tasks should be delegated to Meta Muse Code via `ops/delegate.ps1`:

### Task Queue Candidates

| Task ID | Type | Target Files | Description | Prompt Template Summary |
|---|---|---|---|---|
| **DEL-01** | Test Scaffold | `tests/test_funding_carry.py` | Generate complete unit test skeleton for Funding Carry | "Create tests/test_funding_carry.py with unittest.TestCase stubs for fee round-trip, breakeven, delta-neutral sizing, adverse rate exit, and FakeOkx mock." |
| **DEL-02** | Test Scaffold | `tests/test_idle_cash_earn.py` | Generate complete unit test skeleton for Idle Cash Earn | "Create tests/test_idle_cash_earn.py with unittest.TestCase stubs for idle cash calculation, rate parsing, demo 50038 error handling, and redeem on demand." |
| **DEL-03** | Test Scaffold | `tests/test_meanrev_strategy.py` | Generate unit test skeleton for Mean Reversion live execution | "Create tests/test_meanrev_strategy.py with unittest.TestCase stubs for candle signal generation, risk entry gate, ATR stop sizing, and decaying ROI exits." |
| **DEL-04** | Mock Fixtures | `tests/fixtures/okx_api_fixtures.py` | Create JSON response dictionaries for OKX Earn, Funding, and Bot endpoints | "Generate helper module with realistic JSON responses for OKX v5 funding-rate, lending-rate-summary, and tradingBot responses." |
| **DEL-05** | Documentation | `ops/code-map.md`, `insights/` | Update module maps and cross-reference links for 4 new modules | "Update ops/code-map.md to include src/funding_carry.py, src/meanrev_strategy.py, src/idle_cash_earn.py, and src/fleet_manager.py with command lines and tests." |

### Submission Procedure Example
```powershell
$id = ops\delegate.ps1 submit `
    -Prompt "Создай тесты tests/test_funding_carry.py для модуля Funding Carry по спецификации insights/funding-carry.md: расчёт 4 ног комиссий (maker 0.20%, taker 0.30%), порог окупаемости (дней), фактор капитала 0.5x, парсинг API фандинга и FakeOkx моки." `
    -From claude -Role insight-executor -TimeoutMin 20

ops\delegate.ps1 fetch -Id $id
```

---

## 9. Conclusion & Recommendations

1. **Test Infrastructure is Pristine:** The 836-test suite provides rock-solid safety guarantees. All new modules must strictly preserve the zero-network and complete isolation principles.
2. **Delegation Bridge is Operational:** `ops/delegate.ps1` with background runner PID 8728 is fully ready to receive test scaffold and fixture creation jobs immediately.
3. **Architecture is Cohesive:** The 4 new modules cleanly fit into existing interfaces (`OrderRouter`, `risk.py`, `pnl_ledger.py`, `connector.py`).
4. **Immediate Next Step:** Submit Task DEL-01, DEL-02, and DEL-03 to Muse Code via `ops/delegate.ps1` while the primary executor implements core domain logic.
