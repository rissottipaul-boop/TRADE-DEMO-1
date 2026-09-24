---
name: recurring-dca
description: >
  Use this skill whenever a user wants to create, manage, or automate
  recurring crypto purchases on OKX — including Chinese requests about
  定投, 每周买BTC, 创建定投策略, or 定期定额. Core capabilities: scheduled DCA
  (hourly/daily/weekly/monthly), multi-coin portfolio allocation
  (e.g., BTC 70% + SOL 30%), condition-triggered automation (buy when
  RSI below 30, MACD crossover, Bollinger Band touch), and full strategy
  lifecycle (list, stop, rename). This is the go-to skill for any
  automatic periodic investment plan in crypto, regardless of
  language. Do NOT invoke for one-time trades, pure chart/indicator
  analysis, or DCA bot grid parameter tuning.
---

# Recurring Buy (DCA) System

Manage server-side dollar-cost averaging (DCA) strategies on OKX via the Trading Bot Recurring Buy API. Create multi-coin portfolios, set flexible schedules, and automate buys based on technical indicators.

---

## Confirmation Rule

All write operations (create, stop, amend) require explicit user confirmation. Before executing any write script, show the user a clear summary of what will happen and wait for a "yes" or equivalent. Only then run the script. Never embed `input()` in scripts — Claude Code runs scripts as non-interactive subprocesses and `input()` will cause them to hang indefinitely.

### Confirmation Template

Use this format before every write operation:

```
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
🔄 定投策略确认
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
操作：[创建 / 停止 / 修改] 策略
名称：[策略名称]
币种：[BTC:70% + ETH:30%]
金额：每期 [X] USDT（最低每币 10 USDT）
频率：[每天 / 每周X / 每月X] [时区] [时间]
模式：[真实资金 / 演示模式]

⚠️ 此操作涉及真实资金。演示测试请先执行：
   export OKX_SIMULATED=true

回复"确认"执行，其他取消。
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
```

---

## Setup & Credentials

### Installation

Ensure `okx-trade-cli` is installed:

```bash
npm install -g @okx_ai/okx-trade-cli
which okx || echo "Install with: npm install -g @okx_ai/okx-trade-cli"
```

### Credential Configuration

OKX API credentials can be provided via (in priority order):

1. **Environment variables:**
   ```bash
   export OKX_API_KEY="your-key"
   export OKX_SECRET_KEY="your-secret"
   export OKX_PASSPHRASE="your-passphrase"
   ```

2. **`~/.oktrade.env` file** (see `references/credential-setup.md`)

3. **`~/.okx/config.toml` file** (see `references/credential-setup.md`)

For demo mode, set `export OKX_SIMULATED=true` — all API calls will hit simulated endpoints automatically.

See `${CLAUDE_SKILL_DIR}/scripts/auth_helper.py` for the shared authentication module used by all operations.

---

## 1. Create Recurring Buy Strategy

### Preset Strategy (Single Coin)

Show this summary to the user and get confirmation before running the script.

**Example: Daily BTC DCA at 10:00 UTC+8**

```bash
python3 << 'PYEOF'
import os, sys
sys.path.insert(0, os.path.expanduser('${CLAUDE_SKILL_DIR}/scripts'))
from auth_helper import okx_recurring_api

body = {
    "stgyName": "BTC Daily DCA",
    "recurringList": [{"ccy": "BTC", "ratio": "1"}],
    "period": "daily",
    "amt": "50",
    "investmentCcy": "USDT",
    "tdMode": "cash",   # "cash" = spot (recommended); "cross" = cross-margin
    "recurringTime": "10",
    "timeZone": "8",
}

result = okx_recurring_api("POST", "/api/v5/tradingBot/recurring/order-algo", body=body)
if result:
    print(f"SUCCESS: algoId = {result[0].get('algoId')}")
else:
    print("ERROR: No response from API")
PYEOF
```

### Multi-Coin Portfolio

Pass `recurringList` as an array with ratios summing to 1:
```python
"recurringList": [
    {"ccy": "BTC", "ratio": "0.6"},
    {"ccy": "ETH", "ratio": "0.4"},
]
```

### Parameters

| Parameter | Description | Default | Example |
|-----------|-------------|---------|---------|
| `coins` | "BTC" or "BTC:0.6,ETH:0.4" | Required | "BTC:0.7,SOL:0.3" |
| `amount` | Per-period investment (USDT). Min 10 USDT per coin. | Required | "100" |
| `period` | hourly / daily / weekly / monthly | daily | "weekly" |
| `name` | Strategy name | "DCA" | "BTC Daily" |
| `recurring_day` | 1-7 (Mon-Sun) for weekly, 1-28 for monthly | — | "1" |
| `recurring_hour` | 1/4/8/12 for hourly interval | — | "4" |
| `recurring_time` | Hour of day (0-23). Required. | — | "10" |
| `time_zone` | UTC offset (e.g., "8" for UTC+8) | — | "8" |

---

## 2. Manage Existing Strategies

### List All Pending Strategies

```bash
python3 << 'PYEOF'
import os, sys
sys.path.insert(0, os.path.expanduser('${CLAUDE_SKILL_DIR}/scripts'))
from auth_helper import okx_recurring_api

data = okx_recurring_api("GET", "/api/v5/tradingBot/recurring/orders-algo-pending")
if not data:
    print("No pending DCA strategies")
else:
    print(f"{'#':<4} {'AlgoID':<18} {'Name':<20} {'Coins':<15} {'Period':<8}")
    print("-" * 70)
    for i, o in enumerate(data):
        coins = "+".join(c.get("ccy", "-") for c in o.get("recurringList", []))
        print(f"{i+1:<4} {o.get('algoId',''):<18} {o.get('stgyName',''):<20} {coins:<15} {o.get('period',''):<8}")
    print(f"\nTotal: {len(data)} strategies")
PYEOF
```

### View Strategy Details

```python
data = okx_recurring_api("GET", "/api/v5/tradingBot/recurring/orders-algo-details",
                          query={"algoId": algo_id})
```

### Stop a Strategy

Show the user which strategy will be stopped (name + algoId) and get confirmation before running.

```bash
python3 << 'PYEOF'
import os, sys
sys.path.insert(0, os.path.expanduser('${CLAUDE_SKILL_DIR}/scripts'))
from auth_helper import okx_recurring_api

algo_id = "REPLACE_WITH_ACTUAL_ALGO_ID"

# NOTE: Stop API expects body as ARRAY
result = okx_recurring_api("POST", "/api/v5/tradingBot/recurring/stop-order-algo", body=[{"algoId": algo_id}])
print(f"Strategy stopped: {algo_id}")
PYEOF
```

### Amend Strategy Name

```bash
python3 << 'PYEOF'
import os, sys
sys.path.insert(0, os.path.expanduser('${CLAUDE_SKILL_DIR}/scripts'))
from auth_helper import okx_recurring_api

algo_id = "REPLACE_WITH_ACTUAL_ALGO_ID"
new_name = "REPLACE_WITH_NEW_NAME"

body = {"algoId": algo_id, "stgyName": new_name}
okx_recurring_api("POST", "/api/v5/tradingBot/recurring/amend-order-algo", body=body)
print(f"Strategy renamed: {new_name}")
PYEOF
```

---

## 3. Auto Start/Stop Based on Conditions

Automatically trigger DCA based on technical indicators.

### Indicator Fetching Priority

**Primary — OKX TradeKit CLI:**
```bash
okx market indicator rsi BTC-USDT --bar 1H
okx market indicator macd BTC-USDT --bar 1H
okx market indicator bb BTC-USDT --bar 1H
okx market funding-rate BTC-USDT-SWAP
```

**Fallback — OKX TradeKit MCP** (if CLI unavailable):
```
market_get_indicator(instId="BTC-USDT", indicator="rsi", bar="1H")
market_get_indicator(instId="BTC-USDT", indicator="macd", bar="1H")
market_get_indicator(instId="BTC-USDT", indicator="bb", bar="1H")
market_get_funding_rate(instId="BTC-USDT-SWAP")
```

**ATR** (not available via CLI/MCP): use `${CLAUDE_SKILL_DIR}/scripts/indicators.py`
with candles from `okx market candles BTC-USDT --bar 1H --limit 50`.

### Condition-Based Workflow

**Step 1 — Fetch indicator** (CLI or MCP as above)

**Step 2 — Embed the fetched value in the script and run**

Claude fills in the actual indicator value before executing. Never use `input()` to pass values — the script must be self-contained.

```bash
python3 << 'PYEOF'
import os, sys
sys.path.insert(0, os.path.expanduser('${CLAUDE_SKILL_DIR}/scripts'))
from auth_helper import okx_recurring_api

# Claude replaces 27.4 with the actual RSI value fetched in Step 1
rsi_value = 27.4

if rsi_value < 30:
    print(f"RSI {rsi_value:.1f} < 30 (超卖) — 启动定投...")
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
    print(f"定投已启动，AlgoID: {result[0].get('algoId')}")
elif rsi_value > 70:
    print(f"RSI {rsi_value:.1f} > 70 (超买) — 请查询并停止现有定投策略")
else:
    print(f"RSI {rsi_value:.1f}，条件未触发，维持现状")
PYEOF
```

### Common Condition Examples

| 策略 | 启动条件 | 暂停条件 |
|------|---------|---------|
| RSI 均值回归 | RSI(14, 1H) < 30 | RSI > 70 |
| MACD 金叉 | MACD histogram 由负转正 | histogram 由正转负 |
| 布林下轨 | 价格触及或跌破下轨 | 价格触及上轨 |
| 资金费率套利 | 资金费率 < −0.01% | 资金费率 > +0.05% |
| 复合条件 | RSI < 40 **且** 资金费率 < 0 | 任一条件反转 |

See `references/conditions-guide.md` for full multi-condition automation examples.

---

## API Reference

Full endpoint documentation in `references/api-reference.md`.

Key endpoints at a glance:

| Operation | Method | Endpoint |
|-----------|--------|----------|
| Create | POST | `/api/v5/tradingBot/recurring/order-algo` |
| List pending | GET | `/api/v5/tradingBot/recurring/orders-algo-pending` |
| Get details | GET | `/api/v5/tradingBot/recurring/orders-algo-details?algoId=...` |
| Stop | POST | `/api/v5/tradingBot/recurring/stop-order-algo` (body is **array**) |
| Amend | POST | `/api/v5/tradingBot/recurring/amend-order-algo` |

---

## Fallback Chain

| 优先级 | 用途 | 方式 |
|--------|------|------|
| **指标获取 1** | 行情 / 指标 | OKX TradeKit CLI：`okx market indicator rsi BTC-USDT --bar 1H` |
| **指标获取 2** | 行情 / 指标 | OKX TradeKit MCP：`market_get_indicator(...)` |
| **策略管理** | 创建/停止/查询 | `scripts/auth_helper.py` 直接调 OKX Recurring Buy REST API |
| **定时触发** | 条件检查自动化 | `mcp__scheduled-tasks__create_scheduled_task` |
| **兜底** | 安装引导 | 提示用户安装 `npm install -g @okx_ai/okx-trade-cli` |

---

## Troubleshooting

| Issue | Solution |
|-------|----------|
| "Credentials not found" | Set `OKX_API_KEY`, `OKX_SECRET_KEY`, `OKX_PASSPHRASE` env vars or create `~/.oktrade.env` |
| "API Error: insufficient balance" | Check account balance; min 10 USDT per coin |
| "HTTP 429: Rate limited" | Implement backoff; OKX allows ~10 req/sec per endpoint |
| Strategy not executing | Verify timezone is correct; check account has sufficient balance |
| Lost algoId | Query `/orders-algo-history` to find stopped strategies |

See `references/troubleshooting.md` for detailed debugging steps.
