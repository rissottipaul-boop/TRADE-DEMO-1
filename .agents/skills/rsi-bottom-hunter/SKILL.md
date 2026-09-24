---
name: rsi-bottom-hunter
description: "RSI抄底策略交易 Skill。当用户说'RSI抄底'、'超卖买入'、'RSI策略'、'抄底宝'、'启动RSI交易'时使用此技能。自动监控RSI指标，在超卖区域买入，并执行止盈止损。依赖 okx-cex-market、okx-cex-trade、okx-cex-portfolio 三个 Skill。"
version: "1.0.0"
user-invocable: true
---

# RSI抄底宝

基于 RSI(14) 超卖信号的自动抄底策略。当 RSI 低于设定阈值时自动买入，持仓后根据涨跌幅自动止盈止损。支持现货与合约模式。

## 依赖 Skills（必须先安装）

- `okx-cex-market` — 获取实时价格和 RSI 等技术指标
- `okx-cex-trade` — 执行买入和卖出订单
- `okx-cex-portfolio` — 查询账户持仓和余额

## 参数说明

| 参数 | 类型 | 必填 | 默认值 | 说明 |
|------|------|------|--------|------|
| instId | string | ✅ | - | 交易对，现货用 BTC-USDT，合约用 BTC-USDT-SWAP |
| amount | number | ✅ | - | 每次买入金额（USDT） |
| bar | string | ❌ | 1H | K线周期：1m/5m/15m/1H/4H/1Dutc |
| rsi_period | integer | ❌ | 14 | RSI 计算周期 |
| rsi_buy_threshold | number | ❌ | 30 | RSI 低于此值时触发买入（建议 25-35） |
| take_profit_pct | number | ❌ | 8 | 止盈百分比（建议 5-12） |
| stop_loss_pct | number | ❌ | 6 | 止损百分比（建议 5-10） |
| max_positions | integer | ❌ | 1 | 最大同时持仓数 |
| mode | string | ❌ | spot | 交易模式：spot（现货）/ swap（永续合约） |
| profile | string | ❌ | demo | 实盘 live / 模拟盘 demo（强烈建议先用 demo 测试） |

## 执行流程

### Phase 1：参数确认

1. 用户触发技能后，确认交易对（instId）和买入金额（amount）。
2. 如果用户未指定 profile，**默认使用 demo 模拟盘**，保护资金安全。
3. 如果用户未指定其他参数，使用默认值：RSI阈值 30、止盈 8%、止损 6%、周期 1H、现货模式。
4. 根据 mode 自动处理 instId 格式：
   - mode=spot → instId 格式为 `BTC-USDT`
   - mode=swap → instId 格式为 `BTC-USDT-SWAP`
5. 向用户确认完整参数后开始执行。

### Phase 2：获取 RSI 信号

调用 `okx-cex-market` 获取当前 RSI 值和实时价格：

```bash
# 获取 RSI 值
okx market indicator rsi <instId> --bar <bar> --params <rsi_period>
# 示例：okx market indicator rsi BTC-USDT --bar 1H --params 14

# 获取当前价格
okx market ticker <instId>
# 示例：okx market ticker BTC-USDT
```

从 ticker 返回值中提取 `last`（最新价格）。
从 indicator 返回值中提取最新 RSI 值。

输出当前状态：
- 当前 RSI 值
- 当前价格
- 是否触发买入信号（RSI < rsi_buy_threshold）

### Phase 3：买入判断与执行

**Step 1：检查前置条件**

```bash
# 查询账户 USDT 余额
okx account balance USDT --profile <profile>

# 查询当前持仓（如果是合约模式）
okx account positions --instType SWAP --profile <profile>
```

**Step 2：判断是否满足买入条件**

全部满足才执行买入：
1. RSI < rsi_buy_threshold（默认 30）
2. 当前持仓数 < max_positions
3. 账户 USDT 可用余额（available）>= amount

**Step 3：执行买入**

现货买入：
```bash
okx spot place --instId <instId> --side buy --ordType market --sz <amount> --tgtCcy quote_ccy --profile <profile>
# 示例：okx spot place --instId BTC-USDT --side buy --ordType market --sz 100 --tgtCcy quote_ccy --profile demo
# --tgtCcy quote_ccy 表示 sz 的单位是 USDT
```

合约买入（做多）：
```bash
okx swap place --instId <instId> --side buy --ordType market --sz <amount> --tdMode cross --posSide long --tgtCcy quote_ccy --profile <profile>
# 示例：okx swap place --instId BTC-USDT-SWAP --side buy --ordType market --sz 100 --tdMode cross --posSide long --tgtCcy quote_ccy --profile demo
```

买入成功后：
- 从 ticker 记录买入时价格作为 entry_price
- 向用户报告：买入价格、RSI值、买入数量、订单ID

**不满足条件时：**
- 向用户报告当前 RSI 值和余额，说明未达到买入条件
- 如果用户要求持续监控，每隔一个 bar 周期重新从 Phase 2 开始检查

### Phase 4：持仓监控与止盈止损

当存在持仓时，持续监控价格变化：

**Step 1：获取当前价格**
```bash
okx market ticker <instId>
```

**Step 2：计算盈亏**
```
profit_pct = (current_price - entry_price) / entry_price × 100
```

**Step 3：止盈判断**

如果 profit_pct >= take_profit_pct（默认 +8%）：

现货止盈卖出：
```bash
# 先查持仓数量
okx account balance <baseCcy> --profile <profile>
# 全部卖出
okx spot place --instId <instId> --side sell --ordType market --sz <持仓数量> --tgtCcy base_ccy --profile <profile>
```

合约止盈平仓：
```bash
okx swap close --instId <instId> --mgnMode cross --posSide long --profile <profile>
```

向用户报告：卖出价格、盈利百分比、盈利金额。

**Step 4：止损判断**

如果 profit_pct <= -stop_loss_pct（默认 -6%）：

执行方式与止盈相同（现货卖出或合约平仓），向用户报告：卖出价格、亏损百分比、亏损金额。

**Step 5：持仓中**

未触发止盈止损时：
- 向用户报告当前价格、浮盈/浮亏百分比
- 继续监控，每隔一个 bar 周期重新检查

## 使用示例

用户可以这样触发：

- "启动 RSI抄底宝，BTC-USDT，RSI低于30买入，每次100 USDT，涨8%止盈，跌6%止损"
- "查看当前 BTC-USDT 的 RSI 信号"
- "用 RSI 策略抄底 ETH-USDT，每次 50U，1小时线"
- "RSI抄底 SOL-USDT 200U"
- "用模拟盘测试 RSI抄底 BTC-USDT 100U"
- "切换到实盘，RSI抄底 BTC-USDT 100U"

## 输出格式

每次执行后向用户报告：

```
📊 RSI抄底宝 - BTC-USDT [模拟盘]
━━━━━━━━━━━━━━━━━━━━
当前 RSI(14)：28.5
当前价格：$84,230
买入阈值：< 30
━━━━━━━━━━━━━━━━━━━━
⚡ 信号：RSI 进入超卖区，触发买入！
💰 买入金额：100 USDT
📍 买入价格：$84,230
🎯 止盈目标：$90,968（+8%）
🛑 止损价格：$79,176（-6%）
📋 订单ID：xxxxxxxxx
```

## 风险提示

1. **默认使用模拟盘（demo）**，切换实盘需用户明确指定 profile=live。
2. 单笔金额建议不超过总资金的 5%，避免连续止损导致大幅回撤。
3. RSI 抄底策略在单边下跌行情中可能连续触发止损，需结合趋势判断使用。
4. 实际交易存在滑点、手续费、API 延迟等风险，实际盈亏与理论值会有偏差。
5. 本 Skill 仅供学习研究，不构成投资建议，盈亏自负。
