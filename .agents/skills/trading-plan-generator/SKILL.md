---
name: trading-plan-generator
description: >-
  Use this skill whenever a user wants a crypto trading plan, trading strategy, position sizing,
  signal analysis, or risk-managed trade recommendation for any instrument on OKX (spot, perpetual
  swap, delivery futures, options). Trigger on: "交易计划", "交易策略", "做多方案", "做空方案",
  "仓位管理", "信号分析", "风险管理框架", "BTC/ETH/SOL trading plan", "help me trade",
  "how should I trade", "entry and exit levels", "stop loss and take profit", "market signal",
  "看多看空分析", "合约策略", "现货策略". Integrates real-time data from OKX TradeKit MCP
  (price, OI, candles, technical indicators, funding rate) and CoinGlass MCP (L/S ratio,
  liquidation heatmap, whale positions) to produce a structured trading plan with three risk tiers,
  concrete entry/exit prices, and a signal-weight breakdown. Always use this skill — not a generic
  response — when any trading planning is involved.
---

# Trading Plan Generator

> **Compliance notice**: This skill outputs objective market data and structured trade frameworks only.
> All prices and signals are sourced from public APIs. Interpretation and execution decisions remain solely with the user.
> Always append a disclaimer that outputs are not investment advice.

Generates professional crypto trading plans by combining OKX TradeKit market data with CoinGlass
on-chain signals. Supports spot, perpetual swap, delivery futures, and options. Outputs a structured
text plan with three risk tiers and a 6-dimension signal weight breakdown.

**Skill routing**
- This skill (trading plan, strategy, risk management) → `trading-plan-generator`
- Market data only (price, candles, indicators) → `okx-cex-market`
- Place / cancel / amend orders → `okx-cex-trade`
- Account balance / positions / P&L → `okx-cex-portfolio`
- Grid / DCA bots → `okx-cex-bot`

---

## Data Sources

**Primary — OKX TradeKit MCP** (no setup required):
MCP tools: `market_get_ticker`, `market_get_candles`, `market_get_indicator`, `market_get_funding_rate`, `market_get_open_interest`, `market_get_orderbook`

**Fallback — OKX TradeKit CLI** (if MCP unavailable):
```bash
npm install -g @okx_ai/okx-trade-cli
okx market ticker BTC-USDT   # verify install
```

**Optional — CoinGlass MCP** (provides L/S ratio, liquidation, whale data):
See https://github.com/gpsxtreme/mcp-coinglass for setup.
If unavailable, use CoinGecko + OKX TradeKit as fallback (see Step 2).

---

## Step 1 — Collect User Intent

If the user's message already contains all required fields, skip to Step 2.
Otherwise, ask for the missing items in one message (do not ask one-by-one).

**Required:**
| Field | Examples |
|-------|---------|
| 交易标的 | BTC、ETH、SOL |
| 交易类型 | 现货 / 永续合约 / 交割合约 / 期权（可多选） |
| 方向预期 | 看多 📈 / 看空 📉 / 震荡 ↔️ |
| 时间框架 | 短线 <1天 / 中线 1-7天 / 长线 >7天 |
| 最大亏损容忍 | 账户的 2% / 5% / 10% |
| 最大杠杆 | 3x / 5x / 10x / 无杠杆 |
| 单笔仓位上限 | 总资金的 5% / 10% / 20% |

**Optional** (use defaults if not provided):
- 信号偏好（是否优先链上数据、情绪数据、大户持仓）
- 交易所偏好（默认 OKX）

---

## Step 2 — Gather Market Data

For any command or tool that fails, mark the field `⚠️ 数据不可用` and continue.

> instId format: spot `BTC-USDT` · swap `BTC-USDT-SWAP` · futures `BTC-USDT-250328`

### OKX TradeKit MCP (primary)

```
market_get_ticker(instId=<instId>)                          # price and 24h stats
market_get_open_interest(instId=<instId>, instType="SWAP")  # OI (SWAP/FUTURES only)
market_get_candles(instId=<instId>, bar="1H", limit=48)     # 1H candles
market_get_candles(instId=<instId>, bar="4H", limit=30)     # 4H candles
market_get_indicator(instId=<instId>, indicator="rsi",  bar="1Dutc")   # RSI daily
market_get_indicator(instId=<instId>, indicator="macd", bar="1Dutc")   # MACD daily
market_get_indicator(instId=<instId>, indicator="ema",  bar="1Dutc")   # EMA daily
market_get_funding_rate(instId=<instId>)                    # funding rate (SWAP only)
```

### OKX TradeKit CLI (fallback if MCP unavailable)

```bash
okx market ticker <instId>
okx market open-interest --instType SWAP --instId <instId>
okx market candles <instId> --bar 1H --limit 48
okx market candles <instId> --bar 4H --limit 30
okx market indicator rsi <instId> --bar 1Dutc
okx market indicator macd <instId> --bar 1Dutc
okx market funding-rate <instId>
```

### CoinGlass MCP tools (if available)

| Data | Tool |
|------|------|
| 资金费率 | `coinglass_funding_rate` |
| 多空比 (L/S Ratio) | `coinglass_long_short_ratio` |
| 清算数据 | `coinglass_liquidation` |
| 大户持仓 | `coinglass_top_trader_position` |

### CoinGecko fallback (when CoinGlass is unavailable)

Call the CoinGecko free API to supplement missing context:

```
GET https://api.coingecko.com/api/v3/coins/{coin_id}?localization=false&tickers=false&market_data=true&community_data=true
```
- `coin_id`: e.g., `bitcoin`, `ethereum`, `solana`

CoinGecko supplements: market cap, price change %, sentiment votes, ATH distance, 24h volume context.

**CoinGecko limitations** — it cannot replace CoinGlass for:
- 多空比 (L/S Ratio) → mark `⚠️ 数据不可用`
- 清算数据 → mark `⚠️ 数据不可用`
- 大户持仓变化 → mark `⚠️ 数据不可用`

资金费率: use OKX `market_get_funding_rate` as the primary source (full replacement for CoinGlass on this dimension).
OI 变化趋势: use OKX `market_get_open_interest` (full replacement).

---

## Step 3 — Analyze Signals

Load `references/signal-framework.md` for the full weight table, scoring rules, and interpretation guide.

**Quick reference — default weights:**
```
OI 变化趋势        20%   (OKX TradeKit MCP/CLI)
多空比 L/S Ratio   20%   (CoinGlass → ⚠️ 不可用时记 0)
资金费率           15%   (OKX TradeKit MCP/CLI, or CoinGlass)
清算数据           15%   (CoinGlass → ⚠️ 不可用时记 0)
大户持仓变化       15%   (CoinGlass → ⚠️ 不可用时记 0)
OKX 内置信号       15%   (OKX TradeKit MCP market_get_indicator)
```

Compute a composite signal score (−100 to +100). Score > +30 = bullish bias; < −30 = bearish bias; −30 to +30 = neutral/range.

---

## Step 4 — Output: Structured Trading Plan

Output the full plan in this exact structure (in Chinese, with English labels for prices/numbers):

```
════════════════════════════════════════
  交易计划 — {标的} {交易类型}
  生成时间：{timestamp}  |  市场环境：{看多📈 / 看空📉 / 震荡↔️}
════════════════════════════════════════

【市场信号概览】
综合信号得分：{score}/100  →  {结论}

| 信号维度       | 读数                  | 权重 | 分项得分 |
|----------------|-----------------------|------|----------|
| OI 变化趋势    | {value}               | 20%  | {±pts}   |
| 多空比 L/S     | {value}               | 20%  | {±pts}   |
| 资金费率       | {value}               | 15%  | {±pts}   |
| 清算数据       | {value}               | 15%  | {±pts}   |
| 大户持仓       | {value}               | 15%  | {±pts}   |
| OKX 信号       | {value}               | 15%  | {±pts}   |

注：⚠️ 标注表示数据不可用，该项得分记为 0。

────────────────────────────────────────
【标的组合建议】
推荐交易对：{instId}
建议配置：{现货X% / 合约Y% / 期权Z%}（基于用户风险框架）

────────────────────────────────────────
【三档交易计划】

▌低风险方案（适合：保守型 / 初次建仓 / 不确定性高时）
  入场区间：${entry_low} – ${entry_high}
  止损位：  ${stop_loss}（跌破则离场，无例外）
  止盈目标1：${tp1}
  止盈目标2：${tp2}
  建议仓位：总资金的 {x}%（≤ 用户设定上限）
  杠杆：    {n}x（≤ 用户设定上限）
  预期 R:R：1:{rr}

▌中风险方案（适合：有经验 / 信号较明确时）
  入场区间：...（同上格式）

▌高风险方案（适合：激进型 / 信号强烈时 / 需严格止损纪律）
  入场区间：...（同上格式）

────────────────────────────────────────
【风险总结】
市场风险评级：{1-10} / 10
主要风险：
  1. {risk_1}
  2. {risk_2}
  3. {risk_3}（可选）
建议止损策略：{hard-stop / trailing-stop / time-stop}

────────────────────────────────────────
【计划推理说明】
- {每个信号如何影响结论的 2-3 句话}
- 关键不确定性：{what could invalidate this plan}
- 计划失效条件：{specific price level or event that invalidates the thesis}

────────────────────────────────────────
⚠️ 免责声明：本交易计划由 AI 生成，仅供参考，不构成投资建议。
加密货币交易存在高风险，请独立判断并自行承担风险。
════════════════════════════════════════
```

---

## Edge Cases

- **杠杆/仓位超限**：三档方案的杠杆和仓位必须严格在用户设定上限内，不得超出，即使信号极强
- **方向与信号不一致**：若用户说看多但信号显示看空（得分 < −30），明确提示矛盾并列出两种情景，不要强行生成看多计划
- **数据不可用 > 3 项**：超过 3 个信号无数据时，询问用户是否继续（用不完整数据生成示例计划）
- **期权标的**：需先调用 `market_get_orderbook` 或 `okx option instruments --uly BTC-USD` 获取有效 instId
- **震荡方案**：方向为震荡时，入场改为"区间上沿做空 / 区间下沿做多"的双向说明
- **instId 格式错误**：若 ticker 命令失败，自动尝试 SWAP 格式（如 `BTC-USDT-SWAP`）并告知用户

---

## Global Notes

- 所有价格使用从 MCP 或 CLI 获取的真实数据；若不可用，明确标注 `⚠️ 示例数据`
- 止损位必须是具体价格，不能写"跌破支撑"等模糊描述
- 每次输出后在末尾附上 3 条关键风险提示（精炼版）
- 不需要用户确认即可运行 OKX 市场数据命令（只读）
- CoinGlass MCP 工具调用失败时静默降级，不中断流程
