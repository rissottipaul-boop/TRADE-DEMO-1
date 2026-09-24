---
name: btc-naked-k-breakout
description: "BTC裸K结构AI突破策略。每5分钟触发，基于300根K线识别结构点、构建箱体、评估结构质量，通过AI综合判断执行突破回踩交易，支持动态仓位和高级持仓管理。"
---

# BTC 裸K结构 AI 交易策略 V4

基于 300K 结构分析的 BTC 裸K突破策略。每 5 分钟触发一次，通过识别关键结构点、构建箱体、评估结构质量，进行 AI 综合判断，仅交易干净结构且有空间的突破，执行突破回踩策略并动态管理仓位。

## 执行节奏

每 5 分钟触发一次

但仅在以下条件满足时进入决策流程：
- 新 15M K线刚收盘
- 或 新 1H K线刚收盘
- 或 当前价格接近箱体边界 ±0.2%

否则：本轮跳过（避免噪音交易）

## 依赖 Skills（必须先安装）

- `okx-cex-market` — 获取 K线数据、实时价格、ATR 等技术指标
- `okx-cex-trade` — 执行买入和卖出订单、设置止盈止损、修改止损
- `okx-cex-portfolio` — 查询账户持仓、余额、净值

## 参数说明

| 参数 | 类型 | 必填 | 默认值 | 说明 |
|------|------|------|--------|------|
| instId | string | ✅ | BTC-USDT-SWAP | 交易对，仅支持 BTC-USDT-SWAP |
| risk_per_trade | number | ❌ | 1 | 单笔风险占账户净值百分比（建议 0.5%-2%） |
| max_daily_trades | integer | ❌ | 5 | 每日最大交易笔数（建议 3-8） |
| max_hourly_trades | integer | ❌ | 1 | 每小时最大交易笔数（建议 1-2） |
| profile | string | ❌ | demo | 实盘 live / 模拟盘 demo（强烈建议先用 demo 测试） |

## 执行流程

### Step 1 · 行情数据采集（300K结构）

调用 `okx-cex-market` 获取行情数据：

```bash
# 获取 300 根 1H K线
okx market candles BTC-USDT-SWAP --bar 1H --limit 300

# 获取 300 根 15M K线
okx market candles BTC-USDT-SWAP --bar 15m --limit 300

# 获取当前价格
okx market ticker BTC-USDT-SWAP

# 获取 ATR(14)
okx market indicator atr BTC-USDT-SWAP --bar 1H --params 14
```

AI 需要完成：

**① 提取关键结构点**

从 300 根 K线中识别：
- Swing High（局部高点）
- Swing Low（局部低点）

过滤小波动，仅保留有效结构点

**② 构建 1H 箱体**

判断是否满足：
- 上沿至少被触及 ≥ 2 次
- 下沿至少被触及 ≥ 2 次
- 中间存在明显震荡

输出：
- 箱体上沿
- 箱体下沿

**③ 箱体质量评分（新增核心）**

AI 对结构打分：
- 清晰度（边界是否干净）
- 测试次数（越多越强）
- 假突破次数（越少越好）

输出：
```id="score1"
结构评分：
- 高（可交易）
- 中（降低仓位）
- 低（跳过）
```

### Step 2 · 市场状态识别

基于 300 根 1H K线判断：

```id="market_state"
市场状态：
- 趋势
- 震荡
- 高风险
```

判断规则：
- 高点持续抬高 → 上升趋势
- 低点持续降低 → 下降趋势
- 高低点重叠 → 震荡

若出现：
- 连续 ≥ 3 根强趋势K
- 或 ATR > 20周期均值 1.5倍

判定为：**高风险 → 本轮跳过**

### Step 3 · AI 综合决策（核心）

**① 是否允许交易**

```id="check_trade"
条件：
- 结构评分 ≥ 中
- 市场状态 = 震荡
```

否则：本轮跳过

**② 判断突破性质**

必须满足：
- 收盘价突破箱体（不看影线）
- K线实体明显
- 未立即回到箱体内部
- 回踩仅触边界

输出：
- 真突破 / 假突破

假突破 → 跳过

**③ 方向确定**

- 上破 → 做多
- 下破 → 做空

**④ 15M 入场确认**

做多：
- 回踩后低点抬高
- 出现转强结构

做空：
- 反抽后高点降低
- 出现转弱结构

**⑤ 盈亏比评估**

计算：
- 止损 = 箱体内部失效位
- 止盈 = 下一个 1H 结构位

要求：盈亏比 ≥ 1:2

否则：放弃交易

**⑥ 动态仓位计算**

```id="position"
单笔风险 = 账户权益 × 1%

仓位 = 单笔风险 ÷ (入场价 - 止损价)
```

调整：
- 结构评分 = 中 → 仓位 × 0.5
- ATR 偏高 → 仓位 × 0.5
- ATR 偏低 → 不交易

**⑦ 频率限制（防过度交易）**

```id="limit"
- 每小时最多 1 笔
- 每日最多 5 笔
- 1小时内同方向只允许1次
```

**⑧ 最终决策（必须输出）**

```id="decision"
1. 开多（理由 + 仓位）
2. 开空（理由 + 仓位）
3. 本轮跳过（理由）
```

### Step 4 · 执行下单

当 AI 决策为开多或开空时，执行以下步骤：

**Step 4.1：检查前置条件**

```bash
# 查询账户 USDT 余额和净值
okx account balance USDT --profile <profile>

# 查询当前持仓
okx account positions --instType SWAP --profile <profile>

# 查询今日交易次数（需记录）
```

检查条件：
1. 账户可用余额充足
2. 当前未持有 BTC-USDT-SWAP 仓位（避免对冲）
2. 今日交易次数 < max_daily_trades
3. 本小时交易次数 < max_hourly_trades
4. 本小时同方向未开仓
5. 连续亏损次数 < 3
6. 当日回撤 < 5%

**Step 4.2：计算仓位**

根据 Step 3.⑥ 的动态仓位计算公式确定实际下单金额。

**Step 4.3：执行下单**

调用 `swap_place_order`：

```bash
# 做多
okx swap place --instId BTC-USDT-SWAP --side buy --ordType market --sz <仓位金额> --tdMode cross --posSide long --tgtCcy quote_ccy --profile <profile> --tag agentTradeKit

# 做空
okx swap place --instId BTC-USDT-SWAP --side sell --ordType market --sz <仓位金额> --tdMode cross --posSide short --tgtCcy quote_ccy --profile <profile> --tag agentTradeKit
```

**重要**：`tag` 参数必须为 `agentTradeKit`，否则不计入排行榜

记录开仓价格（从 ticker 返回的 last 值）

### Step 5 · 止损设置

开仓成功后立即设置止损：

**Step 5.1：计算止损价格**

根据 Step 3.⑤ 确定的箱体内部失效位：
- 多单止损 = 箱体内部失效位
- 空单止损 = 箱体内部失效位

**Step 5.2：设置止损**

调用 `swap_place_algo_order`：

```bash
# 多单止损
okx swap place-algo --instId BTC-USDT-SWAP --side sell --ordType stop_loss --sz <持仓数量> --slTriggerPx <止损价格> --slOrdType market --tdMode cross --posSide long --profile <profile>

# 空单止损
okx swap place-algo --instId BTC-USDT-SWAP --side buy --ordType stop_loss --sz <持仓数量> --slTriggerPx <止损价格> --slOrdType market --tdMode cross --posSide short --profile <profile>
```

### Step 6 · 持仓管理（高级）

**盈利管理：**

当存在持仓时，持续监控价格变化：

```bash
# 获取当前价格
okx market ticker BTC-USDT-SWAP

# 获取 ATR
okx market indicator atr BTC-USDT-SWAP --bar 1H --params 14
```

盈利 ≥ 1 ATR：
- 将止损移动至开仓价（保本）

盈利 ≥ 2 ATR：
- 平仓 50% 锁定利润
- 剩余仓位止损移动至开仓价

**提前退出：**

若出现：
- 重新回到箱体内部
- 出现反向结构（15M）

立即平仓：

```bash
# 多单平仓
okx swap close --instId BTC-USDT-SWAP --mgnMode cross --posSide long --profile <profile>

# 空单平仓
okx swap close --instId BTC-USDT-SWAP --mgnMode cross --posSide short --profile <profile>
```

## 风控规则（职业级）

```id="risk"
// 单笔亏损 ≤ 1%
// 盈亏比 ≥ 1:2
// 连续 3 笔亏损 → 停止交易 12 小时
// 当日回撤 ≥ 5% → 停止开仓
// 同时仅允许 1 个仓位
// 禁止加仓 / 补仓 / 对冲
```

## 使用示例

用户可以这样触发：

- "启动 BTC裸K突破策略"
- "BTC结构突破交易，模拟盘"
- "运行裸K策略，每笔风险1%"

## 输出格式

每次执行后向用户报告：

**开仓成功时**：

```
📊 BTC裸K结构AI策略 - 执行报告
━━━━━━━━━━━━━━━━━━━━
⏰ 执行时间：2024-04-10 14:00
💰 账户净值：10,000 USDT
━━━━━━━━━━━━━━━━━━━━
📦 箱体结构：
   上沿：$87,500
   下沿：$85,000
   高度：$2,500
   测试次数：上沿 3 次 / 下沿 4 次
📊 结构评分：高
🎯 市场状态：震荡
━━━━━━━━━━━━━━━━━━━━
🤖 AI 综合判断：
✅ 结构评分 ≥ 中（评分：高）
✅ 市场状态 = 震荡
✅ 真突破确认（收盘价突破）
✅ 回踩后低点抬高
✅ 盈亏比 ≥ 1:2（风险:$500 / 目标:$1,200）
━━━━━━━━━━━━━━━━━━━━
📋 决策：开多
💰 开仓价格：$87,800
📊 动态仓位：300 USDT（风险 1%）
🎯 止盈目标：$89,000
🛑 止损位置：$85,300（箱体内部失效位）
📋 订单ID：xxxxxxxxx
✅ 止损已设置
```

**跳过时**：

```
📊 BTC裸K结构AI策略 - 执行报告
━━━━━━━━━━━━━━━━━━━━
⏰ 执行时间：2024-04-10 14:00
💰 账户净值：10,000 USDT
━━━━━━━━━━━━━━━━━━━━
📦 箱体结构：
   上沿：$87,500
   下沿：$85,000
   高度：$2,500
   测试次数：上沿 2 次 / 下沿 2 次
📊 结构评分：低
🎯 市场状态：趋势
━━━━━━━━━━━━━━━━━━━━
🤖 AI 综合判断：
❌ 结构评分 < 中（评分：低）
❌ 市场状态 ≠ 震荡（趋势状态）
❌ 不满足交易条件
━━━━━━━━━━━━━━━━━━━━
📋 决策：本轮跳过
💡 理由：结构评分低且市场处于趋势状态
⏰ 下次检查：5 分钟后
```

## 注意事项

1. **Agent Trade Kit 标识**：下单时必须包含 `tag=agentTradeKit`，否则不计入排行榜
2. **默认使用模拟盘**：首次运行建议使用 `profile=demo`，充分测试后再切换实盘
3. **高频触发但不乱交易**：每 5 分钟检查，但仅在满足条件时交易，避免噪音
4. **只做干净结构**：结构评分低的交易不参与，保证胜率优势
5. **有空间的突破**：盈亏比必须 ≥ 1:2，否则放弃交易
6. **止损优先**：每笔交易必须设置止损，严格执行风控纪律
7. **禁止加仓补仓**：严格按照策略执行，禁止手动干预
8. **连续亏损保护**：连续 3 笔亏损后停止 12 小时，避免情绪化交易
9. **滑点风险**：实际交易存在滑点、手续费等风险，实际盈亏与理论值会有偏差
10. **学习研究**：本 Skill 仅供学习研究，不构成投资建议，盈亏自负
