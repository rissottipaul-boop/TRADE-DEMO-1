# Advanced Automation Conditions Guide

Practical examples for condition-based DCA automation using technical indicators.

### Indicator Fetching Priority

1. **OKX TradeKit CLI** (primary):
   ```bash
   okx market indicator rsi BTC-USDT --bar 1H
   ```
2. **OKX TradeKit MCP** (if CLI unavailable):
   ```
   market_get_indicator(instId="BTC-USDT", indicator="rsi", bar="1H")
   ```
3. **OKX REST API** (last resort):
   ```
   GET https://www.okx.com/api/v5/market/candles?instId=BTC-USDT&bar=1H&limit=50
   ```
   Then compute the indicator locally using `${CLAUDE_SKILL_DIR}/scripts/indicators.py`.

`${CLAUDE_SKILL_DIR}/scripts/indicators.py` is only needed for ATR and indicators
not available via CLI or MCP.

**Important:** After fetching an indicator value, embed it directly in the script
as a literal (e.g., `rsi_value = 27.4`). Never use `input()` to pass values —
Claude Code runs scripts as non-interactive subprocesses and `input()` will hang.

---

## Basic Conditions

### Oversold Condition (RSI < 30)

**Step 1 — Fetch RSI:**
```bash
# Primary
okx market indicator rsi BTC-USDT --bar 1H
# MCP fallback
# market_get_indicator(instId="BTC-USDT", indicator="rsi", bar="1H")
```

**Step 2 — Embed value and evaluate:**

```python
python3 << 'PYEOF'
import os, sys
sys.path.insert(0, os.path.expanduser('${CLAUDE_SKILL_DIR}/scripts'))
from auth_helper import okx_recurring_api

# Claude replaces this with the actual value from Step 1
rsi_value = 27.4

if rsi_value < 30:
    print(f"RSI {rsi_value:.1f} — Oversold! Starting DCA...")
    body = {
        "stgyName": f"RSI-Oversold DCA (RSI={rsi_value:.0f})",
        "recurringList": [{"ccy": "BTC", "ratio": "1"}],
        "period": "daily",
        "amt": "50",
        "investmentCcy": "USDT",
        "tdMode": "cash",
        "recurringTime": "10",
        "timeZone": "8",
    }
    result = okx_recurring_api("POST", "/api/v5/tradingBot/recurring/order-algo", body=body)
    print(f"DCA Started: {result[0].get('algoId')}")
else:
    print(f"RSI = {rsi_value:.1f}. Not oversold. Wait for better entry.")
PYEOF
```

### Overbought Condition (RSI > 70)

```python
# Claude replaces this with the actual value
rsi_value = 74.2

if rsi_value > 70:
    print("Overbought detected. Pausing new buys...")
    # Stop active strategies (see Stop Conditions below)
```

---

## Multi-Condition Automation

### Condition 1: RSI + Funding Rate

**Fetch both:**
```bash
okx market indicator rsi BTC-USDT --bar 1H
okx market funding-rate BTC-USDT-SWAP
# MCP fallback: market_get_indicator(...) / market_get_funding_rate(...)
```

**Evaluate (Claude embeds actual fetched values):**
```python
# Claude replaces with actual fetched values
rsi = 28.0
funding_rate = -0.0015

if rsi < 30 and funding_rate < -0.001:
    print("Golden signal: Oversold + negative funding (institutional weakness)")
    # Start DCA with larger amount
else:
    print(f"Wait for confirmation: RSI={rsi}, Funding={funding_rate:.4f}")
```

### Condition 2: MACD Crossover

**Fetch:**
```bash
okx market indicator macd BTC-USDT --bar 1H
# MCP fallback: market_get_indicator(instId="BTC-USDT", indicator="macd", bar="1H")
```

**Evaluate:**
```python
# Claude replaces with actual fetched values
histogram = 0.5
macd_line = 120.3
signal_line = 119.8

if histogram > 0 and macd_line > signal_line:
    print("MACD bullish crossover detected — Start DCA")
elif histogram < 0:
    print("MACD bearish. Hold off on buys.")
```

### Condition 3: Bollinger Band Position

**Fetch:**
```bash
okx market indicator bb BTC-USDT --bar 1H
# MCP fallback: market_get_indicator(instId="BTC-USDT", indicator="bb", bar="1H")
```

**Evaluate:**
```python
# Claude replaces with actual fetched values
price = 41200.0
lower_band = 41150.0
upper_band = 43800.0

if price <= lower_band:
    print("Price at Bollinger lower band — Good DCA entry")
elif price >= upper_band:
    print("Price at upper band — Consider pausing DCA")
```

---

## Sentiment-Based Conditions

### Condition 4: Multiple Timeframe RSI Confirmation

**Fetch RSI on two timeframes:**
```bash
okx market indicator rsi BTC-USDT --bar 1H
okx market indicator rsi BTC-USDT --bar 4H
# MCP fallback: market_get_indicator(instId="BTC-USDT", indicator="rsi", bar="1H/4H")
```

**Evaluate and create strategy (Claude embeds actual values):**

```python
python3 << 'PYEOF'
import os, sys
sys.path.insert(0, os.path.expanduser('${CLAUDE_SKILL_DIR}/scripts'))
from auth_helper import okx_recurring_api

# Claude replaces with actual fetched values
rsi_1h = 28.5
rsi_4h = 33.2

print(f"RSI(1H): {rsi_1h:.2f} | RSI(4H): {rsi_4h:.2f}")

if rsi_1h < 30 and rsi_4h < 35:
    print("CONFIRMED: Oversold on 1H and 4H. Strong buy signal!")
    body = {
        "stgyName": "Multi-TF Oversold DCA",
        "recurringList": [{"ccy": "BTC", "ratio": "0.7"}, {"ccy": "ETH", "ratio": "0.3"}],
        "period": "daily",
        "amt": "100",
        "investmentCcy": "USDT",
        "tdMode": "cash",
        "recurringTime": "10",
        "timeZone": "8",
    }
    result = okx_recurring_api("POST", "/api/v5/tradingBot/recurring/order-algo", body=body)
    print(f"Strong DCA started: {result[0].get('algoId')}")
else:
    print("Weak signal. Wait for multi-timeframe confirmation.")
PYEOF
```

---

## Advanced Patterns

### Pattern 1: Contrarian Bottom (Put/Call Ratio)

```python
# put_call_ratio from external data source (e.g., Deribit)
# Claude replaces with actual fetched values
put_call_ratio = 1.8
price_24h_change = -8  # percent

if put_call_ratio > 1.5 and price_24h_change < -5:
    print("Capitulation signal: High put/call + sharp drop — good DCA entry")
```

### Pattern 2: Smart Money Following

```python
# top_trader_long_ratio from CoinGlass MCP: coinglass_top_trader_position
# Claude replaces with actual fetched values
top_trader_long_ratio = 1.6
price_drop_24h = 6  # percent drop

if top_trader_long_ratio > 1.5 and price_drop_24h > 5:
    print("Smart money signal: Institutions going long after drop")
```

### Pattern 3: ATR Volatility Spike

ATR is not available via CLI/MCP — use `scripts/indicators.py` with candles:

```bash
# Step 1: fetch candles via CLI
okx market candles BTC-USDT --bar 1H --limit 50
# MCP fallback: market_get_candles(instId="BTC-USDT", bar="1H", limit=50)
```

```python
python3 << 'PYEOF'
import os, sys
sys.path.insert(0, os.path.expanduser('${CLAUDE_SKILL_DIR}/scripts'))
from indicators import calc_atr

# Claude passes in actual candle data from Step 1
# candles format: [[ts, open, high, low, close, vol], ...]
highs  = [float(c[2]) for c in candles]
lows   = [float(c[3]) for c in candles]
closes = [float(c[4]) for c in candles]

atr = calc_atr(highs, lows, closes, period=14)
atr_pct = (atr / closes[-1]) * 100

if atr_pct > 5:
    print(f"High volatility: {atr_pct:.2f}% ATR — good accumulation window")
else:
    print(f"Low volatility ({atr_pct:.2f}%). Wait for opportunity.")
PYEOF
```

---

## Stop Conditions

### Stop Condition 1: Take Profits (RSI Overbought)

```python
python3 << 'PYEOF'
import os, sys
sys.path.insert(0, os.path.expanduser('${CLAUDE_SKILL_DIR}/scripts'))
from auth_helper import okx_recurring_api

# Claude replaces with actual fetched RSI and algoId from prior list call
rsi = 74.2
active_algo_id = "REPLACE_WITH_ACTUAL_ALGO_ID"

if rsi > 70:
    print(f"RSI {rsi:.1f} — Overbought. Stopping DCA {active_algo_id}...")
    okx_recurring_api("POST", "/api/v5/tradingBot/recurring/stop-order-algo",
                      body=[{"algoId": active_algo_id}])
    print("Strategy stopped.")
else:
    print(f"RSI {rsi:.1f} — Keep accumulating")
PYEOF
```

### Stop Condition 2: Position Size Limit

```python
# Claude replaces with actual values from strategy details call
total_invested = 9800.0
active_algo_id = "REPLACE_WITH_ACTUAL_ALGO_ID"
target = 10000

if total_invested >= target:
    print(f"Reached target investment: ${total_invested:.2f} — Stopping DCA")
    okx_recurring_api("POST", "/api/v5/tradingBot/recurring/stop-order-algo",
                      body=[{"algoId": active_algo_id}])
```

### Stop Condition 3: MACD Bearish Crossover

```python
# Claude replaces with actual fetched MACD values and algoId
histogram = -0.3
macd_line = 118.5
signal_line = 118.8
active_algo_id = "REPLACE_WITH_ACTUAL_ALGO_ID"

if histogram < 0 and macd_line < signal_line:
    print("MACD bearish crossover. Stopping DCA...")
    okx_recurring_api("POST", "/api/v5/tradingBot/recurring/stop-order-algo",
                      body=[{"algoId": active_algo_id}])
```

---

## Automation: Scheduled Condition Checks + Buy Execution

Use `mcp__scheduled-tasks__create_scheduled_task` for both condition checks and
triggering buys on a schedule (no server-side cron needed):

```
Create scheduled task — condition check:
  name:     "BTC DCA Condition Check"
  schedule: "0 */4 * * *"   (every 4 hours)
  prompt:   "1. Fetch BTC RSI(1H) via: okx market indicator rsi BTC-USDT --bar 1H
                MCP fallback: market_get_indicator(instId='BTC-USDT', indicator='rsi', bar='1H')
             2. If RSI < 30 and no active recurring strategy → create daily 50 USDT DCA
                via auth_helper.py POST /api/v5/tradingBot/recurring/order-algo
             3. If RSI > 70 and active strategy exists → stop it
                via auth_helper.py POST /api/v5/tradingBot/recurring/stop-order-algo"
```

```
Create scheduled task — fixed recurring buy (no conditions):
  name:     "BTC Daily DCA Buy"
  schedule: "0 2 * * *"   (daily 10:00 UTC+8)
  prompt:   "Place a recurring buy order for 50 USDT of BTC-USDT via
             auth_helper.py POST /api/v5/tradingBot/recurring/order-algo
             with tdMode=cash, period=daily, recurringTime=10, timeZone=8"
```

---

## Practical Examples

### Example 1: RSI Dip Buyer

```
Fetch:   okx market indicator rsi BTC-USDT --bar 1H
Trigger: RSI(1H) < 35 → start DCA (50 USDT/day)
Stop:    RSI(1H) > 65 → pause
```

### Example 2: Multi-Signal Aggressive Accumulation

```bash
# Fetch all indicators via CLI (MCP fallback)
okx market indicator rsi BTC-USDT --bar 1H
okx market indicator macd BTC-USDT --bar 1H
okx market indicator bb BTC-USDT --bar 1H
```

```python
# Claude replaces with actual fetched values
rsi = 28.0
macd_histogram = 0.4
price = 41200.0
bb_lower = 41150.0

signals = 0
if rsi < 30:              signals += 1
if macd_histogram > 0:    signals += 1
if price <= bb_lower:     signals += 1

if signals >= 2:
    print(f"Strong signal ({signals}/3). Starting DCA (200 USDT/day)...")
```

---

## Tips & Best Practices

1. **Avoid over-trading**: Don't start/stop DCA too frequently (weekly minimum)
2. **Use multiple confirmations**: Don't rely on a single indicator
3. **Paper trade first**: Test with `OKX_SIMULATED=true` before using real funds
4. **Log everything**: Record why each DCA was started/stopped
5. **Prefer 4H over 1H**: RSI(4H) < 35 is a stronger signal than RSI(1H) < 35
6. **Account for fees**: Higher DCA frequency = more fees; daily is usually optimal
