# Project: Trading Insights Implementation with Meta Muse Delegation

## Architecture
- **Trading Engine Core (`src/engine.py`):** Long-running engine process (PID 15744) running 72H demo Phase 1. Additive integration via external strategy modules and shared SQLite registries. Zero disruption to engine process.
- **Central Risk Management (`src/risk.py`):** Strict invariants: 1% risk per trade, 2% ceiling, 6% total heat, 15% equity notional cap, 2 max concurrent positions, 30% cash reserve, daily loss limit -6%, breaker -15% from HWM.
- **Execution & Order Routing (`src/order_router.py`):** Unified demo order routing, input validation, liquidation buffer check, cash/cross no-borrow enforcement, throttler (20/2s), 10,000 ms expiration, clOrdId tagging.
- **Order Tagging (`src/order_owner.py`):** 32-character `[a-z0-9]` clOrdId format with owner prefixes: `botcar` (Funding Carry), `botmr` (Mean Reversion), `bottrn` (Treasury Earn), `trd` / `flt` (Bot Fleet).
- **Delegation Bridge (`ops/delegate.ps1`, `ops/delegations/`):** Active runner PID 8728 polling inbox every 30s. Automatically wraps tasks in AGENTS.md §2 safety headers and executes via Meta Muse Code (`muse-spark-1.3`).

## Feature Inventory
| # | Feature | Description | Milestone | Source |
|---|---------|-------------|-----------|--------|
| 1 | Funding Rate & Basis Monitor | Monitor 8h funding rates, clamp limits (BTC ±0.375%, ETH ±0.75%), basis spread | M1 | insights/funding-carry.md |
| 2 | 4-Leg Fee Math & Net Yield | Calculate round-trip costs (spot buy maker/taker, swap short open, swap short close, spot sell), 0.5x capital efficiency, annualization, breakeven days | M1 | insights/funding-carry.md |
| 3 | Delta-Neutral Order Pairing | Sizing spot long and swap short 1x with isolated margin, liquidation buffer check (1.991x entry), clOrdId tagging (`botcar`), demo execution | M1 | insights/funding-carry.md |
| 4 | Carry Position & Margin Monitor | Track funding payouts (bills type 8 in `funding_carry` sleeve), marginRatio on isolated short, adverse funding exit signal | M1 | insights/funding-carry.md |
| 5 | Mean Reversion Indicators | 1H bars, Wilder's RSI(14), Bollinger Bands(20, 2) on Typical Price (H+L+C)/3, ATR(14) Wilder | M2 | insights/meanrev-strategy-design.md |
| 6 | Mean Reversion Signals | Entry: crossed_above(RSI14, 30) AND Close <= BB_mid AND Close > Close[-1] AND Volume > 0; Confirmation on close(t), execute open(t+1) | M2 | insights/meanrev-strategy-design.md |
| 7 | Mean Reversion Exit Hierarchy | 1) ATR Stop (Entry - 1.5*ATR14), 2) Decaying Minimal ROI table, 3) Time Stop 24h, 4) Indicator exit crossed_above(RSI, 70) AND Close >= BB_mid | M2 | insights/meanrev-strategy-design.md |
| 8 | MR Risk & Order Routing | Validate entry via `src/risk.py` (check_entry_allowed, size_position), route via `src/order_router.py` with `botmr` tag | M2 | insights/meanrev-strategy-design.md |
| 9 | Free Cash Treasury Calculation | FreeCash = max(0, availBal - frozenBal - 30% equity reserve) | M3 | insights/idle-cash-earn.md |
| 10 | Flexible Earn Sweeping | If FreeCash >= 100 USDT, sweep into Flexible Earn (purchase, rate 0.01), handle demo error 50038 via simulation adapter | M3 | insights/idle-cash-earn.md |
| 11 | On-Demand Instant Redemption | Redeem funds from Flexible Earn when trading strategies require margin, attribute yield to `cash_earn` sleeve | M3 | insights/idle-cash-earn.md |
| 12 | 13 OKX Bot Types Catalog | spot_grid, contract_grid_usdt, contract_grid_coin, smart_portfolio, contract_dca, smart_arbitrage, dcd_pendulum, spot_dca, recurring_buy, signal_bot, iceberg, twap, arbitrage | M4 | insights/bot-fleet-50.md, src/fleet_manager.py |
| 13 | Fleet Sizing & Quota Rules | Max 50 active bots, max 2.0% equity per bot, 30% cash reserve, max 3x leverage on derivative bots, mandatory SL | M4 | insights/bot-fleet-50.md, src/fleet_manager.py |
| 14 | Fleet Auditing & Batch Kill | Audit active bot capital distribution, check sleeve isolation (`demo_fleet`), batch kill-switch in groups of 10 | M4 | insights/bot-fleet-50.md, src/fleet_manager.py |
| 15 | Meta Muse Delegation Pipeline | Submit routine tasks to `ops/delegate.ps1` queue (`ops/delegations/`), verify outbox artifacts | M5 | ops/delegate.ps1, ops/agent-team.md |
| 16 | Unit Test Suites & Regression | Independent unit tests with mocks for all new modules, 100% pass on `python -m unittest discover -s tests -t .` | M6 | tests/, ORIGINAL_REQUEST.md |
| 17 | Board Status Synchronization | Update `ops/board.md` reflecting all implemented insights with done criteria | M6 | ops/board.md |

## Milestones
| # | Name | Scope | Dependencies | Status |
|---|------|-------|-------------|--------|
| M1 | Funding Carry Arbitrage (R1) | `src/funding_carry.py`, `tests/test_funding_carry.py` | none | PLANNED |
| M2 | Mean Reversion Strategy (R2) | `src/mean_reversion.py`, `tests/test_mean_reversion.py` | none | PLANNED |
| M3 | Idle Cash Earn Treasury (R3) | `src/idle_earn.py`, `tests/test_idle_earn.py` | none | PLANNED |
| M4 | Bot Fleet Expansion to 50 (R4) | `src/fleet_manager.py`, `tests/test_fleet_manager_50.py` | none | PLANNED |
| M5 | Muse Code Delegation Pipeline (R5) | `ops/delegations/`, `ops/delegate.ps1` tasks & verification | none | PLANNED |
| M6 | System Regression & Board Sync (R6, ACs) | Full test suite verification, engine continuity check, `ops/board.md` update | M1, M2, M3, M4, M5 | PLANNED |

## Interface Contracts
### FundingCarry ↔ Risk & OrderRouter
- Sizing: respects `risk.size_position()` and `risk.check_entry_allowed()`
- Orders: `order_router.place_order(inst_id, side="buy", td_mode="cash", ...)` and `order_router.place_order(inst_id=swap, side="sell", td_mode="isolated", pos_side="short", leverage=1, ...)`
- Tag: `botcar` clOrdId prefix

### MeanReversion ↔ Risk & OrderRouter
- Signal: 1H candles `DataFrame` or list of dicts with `open, high, low, close, volume`
- Validation: `risk.check_entry_allowed("BTC-USDT", "buy")` and `risk.size_position("BTC-USDT", entry_px, stop_px)`
- Orders: `order_router.place_order(inst_id="BTC-USDT", side="buy", td_mode="cash", ...)`
- Exit: `order_router.place_exit_order(...)`
- Tag: `botmr` clOrdId prefix

### IdleCashEarn ↔ Portfolio & Exchange
- Balance query: `exchange.fetch_balance()` or private balance API
- Calculation: `free_cash = max(0, availBal - frozenBal - 0.30 * total_equity)`
- Purchase: `POST /api/v5/finance/savings/purchase-redempt` (`side="purchase"`, `rate="0.01"`, `ccy="USDT"`)
- Redempt: `POST /api/v5/finance/savings/purchase-redempt` (`side="redempt"`, `amt=..., ccy="USDT"`)
- Demo error handling: catch sCode `50038` and activate simulated ledger in demo mode.

### FleetManager ↔ OKX Bot API & Engine
- Types: 13 OKX bot types
- Constraints: `MAX_ACTIVE_BOTS = 50`, `MAX_LEVERAGE = 3`, `MAX_PCT_PER_BOT = 0.02`, `RESERVE_CASH_PCT = 0.30`
- Kill-switch: batch stop in chunks of 10 bots via `bot/grid/stop` or `bot/dca/stop`
- Owner tag: `trd` / `flt`

## Code Layout
- `src/funding_carry.py`: Funding Carry arbitrage engine and yield calculator
- `src/mean_reversion.py`: 1H Mean Reversion live strategy runner and signal evaluator
- `src/idle_earn.py`: Idle Cash Earn treasury controller and demo simulation adapter
- `src/fleet_manager.py`: Bot fleet manager (expanded to 50 bots and 13 types)
- `tests/test_funding_carry.py`: Unit tests for Funding Carry module
- `tests/test_mean_reversion.py`: Unit tests for Mean Reversion strategy
- `tests/test_idle_earn.py`: Unit tests for Idle Cash Earn module
- `tests/test_fleet_manager_50.py`: Unit tests for Fleet Manager 50 bots
- `ops/delegations/`: Task files and artifacts for Meta Muse Code
- `ops/board.md`: Project tasks board
