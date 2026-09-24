# Signal Analysis Framework

Reference file for `trading-plan-generator`. Load this file during Step 3 (signal analysis).

---

## Default Signal Weights

```
信号类别              默认权重    数据来源
─────────────────────────────────────────────────
OI 变化趋势            20%       OKX TradeKit MCP / CLI
多空比 (L/S Ratio)     20%       CoinGlass（不可用时记 0）
资金费率               15%       OKX TradeKit MCP / CLI（或 CoinGlass）
清算数据               15%       CoinGlass（不可用时记 0）
大户持仓变化           15%       CoinGlass（不可用时记 0）
OKX 内置信号           15%       OKX TradeKit MCP market_get_indicator
─────────────────────────────────────────────────
合计                  100%
```

If a data source is unavailable, redistribute its weight proportionally across available sources,
or assign 0 pts for that dimension and note it in the output table.

---

## Scoring Rules

### OI 变化趋势 (max ±20 pts)

Data source: `market_get_open_interest(instId, instType="SWAP")` via OKX TradeKit MCP,
or `okx market open-interest --instType SWAP --instId <instId>` via CLI.

| 条件 | 分值 |
|------|------|
| OI 上升 + 价格上涨（趋势强化） | +20 |
| OI 上升 + 价格下跌（空头主导） | −20 |
| OI 下降 + 价格上涨（轧空/回补，弱势） | +8 |
| OI 下降 + 价格下跌（多头止损，弱势） | −8 |
| OI 横盘（中性） | 0 |

### 多空比 L/S Ratio (max ±20 pts)

Data source: `coinglass_long_short_ratio`. If CoinGlass unavailable, mark `⚠️ 数据不可用` and assign 0 pts.
CoinGecko does not provide L/S ratio data.

> L/S Ratio = 多头持仓量 / 空头持仓量

| 条件 | 分值 |
|------|------|
| L/S > 1.5（多头极度拥挤，反转风险） | −15 |
| L/S 1.2–1.5（多头偏多，轻微看空） | −8 |
| L/S 0.9–1.2（均衡，中性） | 0 |
| L/S 0.7–0.9（空头偏多，轻微看多） | +8 |
| L/S < 0.7（空头极度拥挤，轧空机会） | +20 |

### 资金费率 (max ±15 pts)

Data source (priority order):
1. OKX TradeKit MCP: `market_get_funding_rate(instId=<SWAP instId>)`
2. OKX TradeKit CLI: `okx market funding-rate <instId>`
3. CoinGlass MCP: `coinglass_funding_rate` (if available)

All three sources are equivalent for this dimension.

| 条件 | 分值 |
|------|------|
| 资金费率 > +0.1%（做多成本极高，市场过热） | −15 |
| 资金费率 +0.05% ~ +0.1%（偏多拥挤） | −8 |
| 资金费率 −0.01% ~ +0.05%（正常区间） | 0 |
| 资金费率 −0.05% ~ −0.01%（做多成本低，轻微看多） | +8 |
| 资金费率 < −0.05%（做空过热，强烈看多信号） | +15 |

### 清算数据 (max ±15 pts)

Data source: `coinglass_liquidation`. If CoinGlass unavailable, mark `⚠️ 数据不可用` and assign 0 pts.
CoinGecko does not provide liquidation data.

| 条件 | 分值 |
|------|------|
| 近24h 多头清算量 >> 空头清算量（多头遭受重创） | −15 |
| 近24h 多头清算量 > 空头清算量 | −8 |
| 多空清算量均衡 | 0 |
| 近24h 空头清算量 > 多头清算量 | +8 |
| 近24h 空头清算量 >> 多头清算量（轧空压力大） | +15 |

### 大户持仓变化 (max ±15 pts)

Data source: `coinglass_top_trader_position`. If CoinGlass unavailable, mark `⚠️ 数据不可用` and assign 0 pts.
CoinGecko does not provide whale position data.

| 条件 | 分值 |
|------|------|
| 大户净多头持仓明显增加（>5%） | +15 |
| 大户净多头持仓小幅增加 | +8 |
| 大户持仓变化不明显 | 0 |
| 大户净空头持仓小幅增加 | −8 |
| 大户净空头持仓明显增加（>5%） | −15 |

### OKX 内置信号 / 技术指标 (max ±15 pts)

使用 RSI（日线）+ MACD（日线）综合评分。

Data source (priority order):
1. OKX TradeKit MCP:
   ```
   market_get_indicator(instId=<instId>, indicator="rsi",  bar="1Dutc")
   market_get_indicator(instId=<instId>, indicator="macd", bar="1Dutc")
   market_get_indicator(instId=<instId>, indicator="ema",  bar="1Dutc")  # optional context
   market_get_indicator(instId=<instId>, indicator="bb",   bar="1Dutc")  # optional context
   ```
2. OKX TradeKit CLI (fallback):
   ```bash
   okx market indicator rsi <instId> --bar 1Dutc
   okx market indicator macd <instId> --bar 1Dutc
   ```

| 条件 | 分值 |
|------|------|
| RSI < 30（超卖）+ MACD 金叉 | +15 |
| RSI < 40 或 MACD 金叉 | +8 |
| RSI 40–60，MACD 震荡 | 0 |
| RSI > 60 或 MACD 死叉 | −8 |
| RSI > 70（超买）+ MACD 死叉 | −15 |

---

## Composite Score Interpretation

| 得分区间 | 市场环境标签 | 建议偏向 |
|----------|-------------|---------|
| +60 ~ +100 | 强势看多 📈📈 | 积极多单，宽止盈 |
| +30 ~ +60 | 看多 📈 | 标准多单 |
| −30 ~ +30 | 震荡中性 ↔️ | 区间操作或观望 |
| −60 ~ −30 | 看空 📉 | 标准空单 |
| −100 ~ −60 | 强势看空 📉📉 | 积极空单，宽止盈 |

---

## Direction Conflict Handling

If the user's stated direction conflicts with the composite score by more than 40 pts
(e.g., user says 看多 but score = −50), do not override the user's intent silently.
Instead, output a conflict notice like:

> ⚠️ 方向冲突提示：您倾向看多，但当前综合信号得分为 −50（偏空）。
> 以下提供两套方案：A) 尊重用户方向（看多，降低仓位/提高止损精度）；B) 跟随信号（看空）。

Then present both plans labeled 方案A and 方案B.

---

## Risk Rating Formula

Risk Rating (1–10) is computed from:
- Market signal strength: abs(composite score) / 10 → 0–10 baseline
- User leverage: capped at 10x; higher leverage adds +1~+3 pts
- Time uncertainty: 短线 +1, 中线 0, 长线 −1

Final rating = clamp(baseline + leverage_adj + time_adj, 1, 10).

Higher score = more caution warranted. Surface this clearly in the Risk Summary section.

---

## CoinGlass Unavailable — Summary

When CoinGlass MCP is not available, the following dimensions fall back:

| 维度 | 回退策略 |
|------|---------|
| 资金费率 | OKX TradeKit `market_get_funding_rate` — 完整替代 |
| OI 变化趋势 | OKX TradeKit `market_get_open_interest` — 完整替代 |
| 多空比 L/S | CoinGecko 无法提供 → `⚠️ 数据不可用`，记 0 分 |
| 清算数据 | CoinGecko 无法提供 → `⚠️ 数据不可用`，记 0 分 |
| 大户持仓变化 | CoinGecko 无法提供 → `⚠️ 数据不可用`，记 0 分 |
| OKX 内置信号 | OKX TradeKit `market_get_indicator` — 不受影响 |

CoinGecko (market cap, sentiment votes, price change %) can be used as supplementary context
in the 【计划推理说明】section, but does not score into any dimension directly.
