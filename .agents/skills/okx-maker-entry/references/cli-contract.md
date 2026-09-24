# CLI Contract

All commands are invoked as:

```bash
python "<skillDir>/scripts/okx_maker_entry.py" <subcommand> [options]
```

Output is always JSON printed to stdout. Errors are reported with non-zero exit codes and a JSON body containing an `error` field.

## Commands

### `init`

Purpose: create a template config file at `~/.okx/config.toml` if it does not already exist.

No arguments.

Output fields:
- `ok`: always true
- `configPath`: absolute path to the config file
- `created`: true if a new file was written, false if it already existed
- `message`: next-step instructions

### `doctor`

Purpose: verify Python environment, OKX profile readability, account mode, permissions, and optional instrument support.

Required:
- `--profile demo|live`

Optional:
- `--instId BTC-USDT-SWAP`

Output fields:
- `profile`, `pythonVersion`, `configPath`
- `profileExists`: whether the named profile was found in the config
- `okxSite`: global, eea, or us
- `oneWayMode`: true if the account is in net position mode (required)
- `canRead`, `canTrade`, `canTradeFutures`: API permission checks
- `symbolChecked`, `symbolTradable`: instrument validation (when `--instId` provided)
- `leverage`, `marginMode`: current account settings
- `warnings`, `message`

### `preview`

Purpose: compute Maker Entry feasibility and sample order validation.

Required:
- `--profile demo|live`
- `--instId <OKX swap instrument>`
- `--side LONG|SHORT`
- `--entryNotionalUsdt <positive number>`

Optional:
- `--targetLeverage <positive number>`
- `--maxAdverseMovePct <positive number>`
- `--maxRunMinutes <positive number>`

Output fields:
- `preview.canStart`: whether the task can proceed
- `preview.blockReason`: why it cannot start (if blocked)
- `preview.feasibility`: leverage, margin, max notional estimates
- `preview.conflictOrders`: non-reduce-only orders that will be canceled on start
- `preview.warnings`: things to be aware of. For large notional entries (default threshold: 10,000 USDT) preview pre-emptively warns about (a) OKX's per-instrument concurrent same-side exposure cap and the resulting rolling "place → fill → place" execution pattern, and (b) missing `maxAdverseMovePct` slippage guard. Assistants MUST surface these warnings to the user instead of hiding them.
- `validation.passed`: whether sample orders passed OKX precheck
- `validation.items`: individual order check results

### `start`

Purpose: persist a task, spawn the detached runner, and return the initial status.

Arguments: same as `preview`.

Behavior:
- Reject if another Maker Entry task is active.
- Re-run preview and validation before spawning the runner.
- Refuse to start when `preview.canStart` is false.
- Refuse to start when `validation.passed` is false.
- The detached runner may cancel conflicting non-reduce-only open orders.
- The detached runner may set leverage before placing any maker orders.

### `status`

Purpose: read the latest persisted task status.

No arguments.

Primary fields:
- `taskId`
- `profile`
- `instId`
- `side`
- `phase`
- `currentAction`
- `bestBid`
- `bestAsk`
- `filledEntryNotionalUsdt` — already filled notional
- `openWorkingNotionalUsdt` — notional currently resting in live maker orders
- `unplacedNotionalUsdt` — target minus filled minus open-working. Large when the runner is throttled by OKX's concurrent same-side exposure cap (rolling fill → place behaviour)
- `unfilledNotionalUsdt` — target minus filled. Equals `openWorkingNotionalUsdt + unplacedNotionalUsdt`
- `remainingNotionalUsdt` — backward-compatibility alias of `unplacedNotionalUsdt`. Prefer the two fields above; this may be removed in a future major version
- `completionRatio`
- `activeWorkingOrders`
- `warnings`
- `updatedAt`
- `pid`

When the task is in a terminal phase (completed, stopped, failed, paused, stopped_unexpectedly), a `result` block is included:
- `requestedEntryNotionalUsdt`: target notional from the user
- `filledEntryNotionalUsdt`: actual filled notional
- `completionRatio`: filled / requested
- `avgFillPrice`: volume-weighted average fill price (null if nothing filled)
- `placedOrderCount`: total maker orders placed
- `cancelCount`: total orders canceled
- `reclaimCount`: orders reclaimed (canceled and re-placed at better levels)
- `endReason`: `completed` | `dust_remainder` | `paused` | `stopped` | `error`
- `pauseReason`: `slippage_guard` | `runtime_limit` | `order_sync` | `market_data_unavailable` | `manual` | null
- `recommendationText`: human-readable outcome summary
- `finalPosition`: snapshot of the actual account position after the task ended. Fields: `symbol`, `side`, `signedContracts`, `absNotionalUsdt`, `markPrice`, `leverage`, `marginMode`, `unrealizedPnlUsdt`, `taskAvgFillPrice`, `taskFilledNotionalUsdt`. Used by the assistant to tell the user what they are still holding.
- `followupHint`: human-readable next-step suggestion. Assistants MUST read this to the user for non-completed terminal states so residual positions aren't forgotten.

### `stop`

Purpose: request a graceful stop through the stop-flag file.

No arguments.

Behavior:
- Set `stop.flag`
- Keep the runner responsible for cancel cleanup and final state

## Phase Meaning

- `idle`: no active task
- `starting`: detached runner is launching
- `preflight`: execution is about to begin
- `working`: live maker orders are active
- `reclaiming`: far orders are being canceled and pulled forward
- `paused`: task auto-paused on a guard
- `stopped`: task stopped by user
- `failed`: task ended on an error
- `completed`: requested notional finished
- `stopped_unexpectedly`: persisted task existed but the runner PID was no longer alive
