# CLI Contract

All commands are invoked as:

```bash
python "<skillDir>/scripts/okx_pair_spread.py" <subcommand> [options]
```

Output is always JSON printed to stdout. Errors are reported with non-zero exit codes and a JSON body containing a `message` field.

The trading commands are **stateless**: `doctor`, `status`, `open-preview`, `open`, `close-preview`, and `close` read live exchange state and return synchronously.

The `watch-*` commands are the exception: `watch-start` spawns a detached local Python daemon, and `watch-list`, `watch-status`, and `watch-stop` manage persisted state under `~/.okx/watches/`. `watch-start` itself still returns synchronously after the daemon is spawned (or fails to spawn).

## Commands

### `init`

Purpose: create a template config file at `~/.okx/config.toml` if it does not already exist. Shares the same file with `okx-maker-entry` skill.

No arguments.

Output fields:
- `ok`: always true
- `configPath`: absolute path to the config file
- `created`: true if a new file was written, false if it already existed
- `message`: next-step instructions

### `doctor`

Purpose: verify Python environment, OKX profile readability, account mode, permissions, and both instruments' tradability.

Required:
- `--profile demo|live`
- `--longInstId <OKX swap instrument>` — the leg you intend to go LONG
- `--shortInstId <OKX swap instrument>` — the leg you intend to go SHORT

Output fields:
- `profile`, `pythonVersion`, `configPath`
- `profileExists`: whether the named profile was found in the config
- `okxSite`: global, eea, or us
- `oneWayMode`: true if the account is in net position mode (required)
- `canRead`, `canTrade`, `canTradeFutures`: API permission checks
- `longSymbolChecked`, `longSymbolTradable`
- `shortSymbolChecked`, `shortSymbolTradable`
- `leverage`, `marginMode`: current account reference settings
- `warnings`, `message`

### `status`

Purpose: read live positions of both legs and report current pair state. **Stateless** — does not depend on any previous `open` call.

Required:
- `--profile demo|live`
- `--longInstId <OKX swap instrument>`
- `--shortInstId <OKX swap instrument>`

Output fields:
- `profile`
- `longLeg`: `{ symbol, side, signedContracts, absNotionalUsdt, markPrice, leverage, marginMode, unrealizedPnlUsdt }`
- `shortLeg`: same structure
- `currentRatio`: `longLeg.markPrice / shortLeg.markPrice` (null if either mark is missing)
- `combinedUnrealizedPnlUsdt`: sum of both legs' unrealized PnL
- `pairClosableNotionalUsdt`: `min(longLeg.absNotional, shortLeg.absNotional)` **only when directions are correct** (long leg is LONG, short leg is SHORT). 0 if directions don't match.
- `directionMatches`: true if long leg is actually LONG and short leg is actually SHORT
- `warnings`: directional mismatches, missing positions, etc.

### `open-preview`

Purpose: compute pair open feasibility and run sample order precheck on both legs.

Required:
- `--profile demo|live`
- `--longInstId`, `--shortInstId`
- `--entryNotionalUsdtPerLeg <positive number>` — per-leg notional; total exposure is 2x this

Optional:
- `--targetLeverage <positive number>` — applied to both legs. If omitted, current per-leg leverage is kept.

Output fields:
- `profile`
- `longLeg.currentPosition`, `shortLeg.currentPosition`: live position snapshots
- `longLeg.conflictOrders`, `shortLeg.conflictOrders`: non-reduce-only open orders that `open` will cancel
- `currentRatio`: informational
- `feasibility`:
  - `currentLongLeverage`, `currentShortLeverage`
  - `targetLeverage`: requested (or null)
  - `effectiveLeverage`: leverage that will actually be used
  - `willSetLeverageOnStart`: boolean (if either leg differs from target)
  - `availableUsdt`: account available margin
  - `estimatedRequiredMarginUsdt`: `2 * entryNotionalUsdtPerLeg / effectiveLeverage`
  - `estimatedMaxNotionalPerLegUsdt`: rough upper bound given balance
- `validation`: market-order precheck results on both legs
  - `passed`: true if both legs' precheck passed
  - `items`: `[{ id, label, ok, message }]`
- `warnings`: list of user-visible cautions
- `canStart`: boolean
- `blockReason`: string or null

**Block conditions:**
- Long leg currently has a non-zero SHORT position, or short leg currently has a non-zero LONG position (reverse exposure — must flatten first)
- `validation.passed` is false
- Insufficient balance for the combined margin requirement

**Warnings (non-blocking):**
- Existing same-direction exposure on either leg (will stack)
- Conflict orders present (will be cancelled on start)
- `targetLeverage` differs from current on either leg (will be changed on start)

### `open`

Purpose: execute the pair open. Synchronous — blocks until both legs are filled, or rollback completes.

Arguments: same as `open-preview`.

Behavior:
1. Re-run preview + validation. Refuse if blocked.
2. Cancel conflict orders on both symbols.
3. Set leverage on both symbols (if target differs from current).
4. Place market order on long leg. If it fails → abort, return failure (no exposure).
5. Place market order on short leg. If it fails → **place reduce-only market order on long leg to rollback**, return failure.
6. Verify both legs filled within entry imbalance threshold (max of 10 USDT or 20% of per-leg notional).
7. Return result with both legs' fills and final position snapshots.

Output fields:
- `ok`: true if both legs filled successfully, false otherwise
- `endReason`: `completed` | `entry_failed` | `entry_rollback_failed` | `imbalance_warning`
- `longLeg.fill`: `{ orderId, side, requestedNotionalUsdt, filledAmount, filledNotionalUsdt, avgFillPrice }`
- `shortLeg.fill`: same structure
- `longLeg.finalPosition`, `shortLeg.finalPosition`: post-execution position snapshots
- `entryImbalanceNotionalUsdt`: `abs(long.filled - short.filled)`
- `currentRatio`: post-open ratio
- `combinedUnrealizedPnlUsdt`
- `rollbackPerformed`: boolean
- `followupHint`: next-step guidance ("you now hold a paired spread, monitor via `status` ...")

### `close-preview`

Purpose: compute a close plan from the user's request + current live positions.

Required:
- `--profile demo|live`
- `--longInstId`, `--shortInstId`

Exactly one of:
- `--closeAll` — close full positions on both legs
- `--longCloseUsdt N` and/or `--shortCloseUsdt N` — explicit per-leg amounts in USDT. Unset value defaults to 0 (don't touch that leg). At least one must be > 0.

Output fields:
- `profile`
- `longLeg.currentPosition`, `shortLeg.currentPosition`: live snapshots
- `currentRatio`, `combinedUnrealizedPnlUsdt`
- `plan.longClose`: `{ requestedNotionalUsdt, orderSide, estimatedAmount }` (or null if 0)
- `plan.shortClose`: same structure (or null if 0)
- `warnings`: directional mismatch, single-leg close warning, amount > position, etc.
- `canClose`: boolean
- `blockReason`: string or null

**Block conditions:**
- Long leg is not actually LONG, or short leg is not actually SHORT (directional mismatch — ask user to reconfirm which symbol is which)
- A requested close amount exceeds the actual position notional on that leg
- Both requested amounts are 0 and `--closeAll` is not set

**Warnings (non-blocking):**
- Only closing one leg — the other becomes naked exposure
- Close amount is approximate because we convert USDT → contracts at mark price (small residuals may remain)

### `close`

Purpose: execute the close plan. Synchronous.

Arguments: same as `close-preview`.

Behavior:
1. Re-run close-preview. Refuse if blocked.
2. For each leg with a non-zero plan: place a reduce-only market order.
3. Orders are placed in parallel (best-effort); if one fails, the other still executes. Report per-leg outcome.
4. Fetch final positions and return.

Output fields:
- `ok`: true if all requested legs closed successfully
- `longLeg.fill`: `{ orderId, requestedNotionalUsdt, filledAmount, filledNotionalUsdt, avgFillPrice, error }` or null (not requested)
- `shortLeg.fill`: same structure or null
- `longLeg.finalPosition`, `shortLeg.finalPosition`: post-execution snapshots
- `combinedUnrealizedPnlUsdt`: post-close
- `followupHint`: human-readable next-step guidance, especially when a single leg was closed ("你还持有 X USDT 的 ETH 空头，已失去对冲，考虑手动平掉")

### `watch-start`

Purpose: start a detached local watch daemon that polls the live pair state and executes `close` when any configured trigger fires.

Required:
- `--profile demo|live`
- `--longInstId`, `--shortInstId`
- At least one trigger:
  - `--ratioStopLte N`
  - `--ratioStopGte N`
  - `--pnlStopLte N`
  - `--pnlStopGte N`

Optional:
- `--pollIntervalSeconds N` — polling interval in seconds, clamped to 1..60
- Action override:
  - `--closeAll` — default action if no per-leg amount is provided
  - `--longCloseUsdt N` and/or `--shortCloseUsdt N` — partial close action to execute on trigger

Behavior:
1. Read live pair status and refuse to start if one or both legs are flat.
2. Refuse to start if `directionMatches` is false.
3. Refuse to start if any configured trigger is already satisfied at start time.
4. Write an initial watch state file and spawn a detached child process that runs the daemon body.

Output fields:
- `ok`: true if the daemon was spawned successfully
- `watchId`: persistent identifier for this watch
- `pid`: daemon process ID on the local machine
- `statePath`, `logPath`: absolute paths under `~/.okx/watches/`
- `profile`, `longInstId`, `shortInstId`
- `pollIntervalSeconds`
- `baselineRatio`, `baselinePnl`
- `triggers`: normalized trigger list used by the daemon
- `action`: normalized close action used by the daemon
- `message`

### `watch-list`

Purpose: list all known watch entries from `~/.okx/watches/`, with running watches first.

No arguments.

Output fields:
- `ok`
- `entries`: list of watches with fields such as `watchId`, `status`, `profile`, `longInstId`, `shortInstId`, `pid`, `pidAlive`, `tickCount`, `lastTickAt`, `lastRatio`, `lastPnl`, `startedAt`, `stoppedAt`, `endReason`
- `runningCount`, `totalCount`

Notes:
- If a watch state says `running` but its PID is no longer alive, `watch-list` reconciles it to `crashed` and persists that status.

### `watch-status`

Purpose: show full persisted state for one watch, plus a recent tail of its log file.

Required:
- `--watchId <watch id>`

Output fields:
- `ok`
- `watch`: full watch state JSON
- `logTail`: recent daemon log lines, if available

### `watch-stop`

Purpose: stop a running watch daemon by ID.

Required:
- `--watchId <watch id>`

Behavior:
1. Load the persisted watch state.
2. If the watch is still running, create a sentinel file to request graceful shutdown.
3. Wait briefly for the daemon to update its own state; if it does not exit in time, force-kill the PID.
4. Return the final reconciled state.

Output fields:
- `ok`
- `watchId`
- `status`
- `message`
- `watch`: final reconciled watch state
- `pidKilled`: true if a force-kill was required

## Error conventions

- Non-zero exit code on failure
- JSON body always includes `message` field (human-readable)
- `ccxt is required` errors include the exact `sys.executable` path for installation
- Network / auth / position-mode errors are normalized into friendly messages by the adapter
