---
name: rolling-position
description: "智能滚仓助手 — 融合比特皇趋势滚仓与予与裸K波段两种模式。分析行情后给出结构化操作建议，通过 OKX Agent Trade Kit CLI 安全执行交易（密钥不离开本地）。触发词：/rolling-position, 滚仓, 加仓策略, 仓位管理"
license: MIT
metadata:
  author: awan
  version: "2.0.0"
---

# 智能滚仓助手

## 概述

本 Skill 融合两位顶级交易员的核心方法论：
- **比特皇模式**：趋势突破后浮盈加仓，随资金增长动态降杠杆，三次止损强制休息
- **予与模式**：裸K识别震荡区间做波段，趋势行情浮盈1:1滚仓，只做龙头标的

**核心铁律（不可违反）**：
1. 浮亏时绝对禁止加仓
2. 单日止损3次 → 强制停止当日所有交易
3. 加仓仓位必须同时设置止损
4. 止损优先于止盈考虑

---

## 执行流程

用户调用 `/rolling-position [交易对]` 时，按以下步骤执行：

### Step 1：读取状态文件

检查 `~/.claude/rolling-position-state.json` 是否存在：

```bash
cat ~/.claude/rolling-position-state.json 2>/dev/null || echo '{"daily_stops": 0, "last_stop_date": "", "total_equity_usd": 0}'
```

如果 `daily_stops >= 3` 且 `last_stop_date == 今日日期` → **立即停止，输出强制休息提示，不执行任何操作**。

### Step 2：获取账户信息

通过 OKX Trade CLI 获取（需要用户已通过 `okx config init` 配置密钥，密钥存储在本地 `~/.okx/config.toml`，不经过 AI）：

```bash
okx account balance
```

提取：
- `totalEq`：账户总净值（USD）
- `availBal`：可用余额

根据净值确定**最大杠杆上限**（比特皇复利降杠杆原则）：

| 净值范围 | 最大杠杆 | 单笔最大仓位 |
|---------|---------|------------|
| < $10,000 | 20x | 净值 30% |
| $10k–$100k | 10x | 净值 25% |
| $100k–$1M | 5x | 净值 20% |
| > $1M | 3x | 净值 15% |

### Step 2.5：操作前检查（每次必做）

在执行任何交易操作前，必须先检查现有挂单和策略单：

```bash
# 查看现有策略单（止损/止盈/trailing stop）
okx swap algo orders --instId {交易对}

# 查看现有限价挂单
okx swap orders --instId {交易对}
```

**注意事项**：
- 修改杠杆前必须先取消所有挂单和策略单，否则会报错
- 取消策略单：`okx swap algo cancel --instId {交易对} --algoId {策略单ID}`
- 取消限价单：`okx swap cancel {交易对} --ordId {订单ID}`

### Step 3：获取当前持仓

```bash
okx swap positions {交易对}
```

提取：
- `pos`：持仓数量（正=多，负=空）
- `avgPx`：开仓均价
- `upl`：未实现盈亏
- `uplRatio`：浮盈率
- `liqPx`：强平价格
- `lever`：当前杠杆

### Step 4：获取 K 线数据

```bash
okx market candles {交易对} --bar 4H --limit 100
okx market candles {交易对} --bar 1H --limit 50
```

同时获取4小时和1小时两个级别，用于判断趋势/震荡。

### Step 5：行情模式判断（裸K分析，不依赖指标）

#### 趋势模式判断条件（满足2条以上）
- 4H级别：连续3根以上同向K线
- 价格突破近20根K线的最高/最低点
- 回调不超过前一波涨幅的50%（更高的低点/更低的高点）
- 成交量在突破时明显放大

#### 震荡模式判断条件（满足2条以上）
- 价格在明确的高低点区间内来回运动
- 多次触及同一支撑/阻力位后反弹
- 4H级别无明显方向，1H级别有小波段
- 近期高点和低点的差距 < 15%

#### 关键价位识别
- **支撑位**：近期多次触及的低点，取最近3个低点的均值
- **阻力位**：近期多次触及的高点，取最近3个高点的均值
- **突破确认**：收盘价超过阻力位/支撑位 2% 以上

---

## 操作决策树

### 情况A：无持仓 + 趋势模式

**开仓条件**：价格刚突破阻力位（多）或跌破支撑位（空），突破幅度 > 2%

**操作**：
1. 建底仓：账户净值 × 20%，杠杆 = 当前级别最大杠杆的 50%
2. 同时设置止损：止损位 = 突破点下方 3%（多）/ 上方 3%（空）
3. 止盈目标：盈亏比至少 3:1，理想 10:1

**下单命令模板**：
```bash
# 1. 查看当前杠杆
okx swap get-leverage --instId {交易对} --mgnMode cross

# 2. 市价开多，同时附带止损（会自动创建策略止损单）
okx swap place --instId {交易对} --side buy --posSide long --ordType market --sz {合约张数} \
  --tdMode cross --slTriggerPx {止损价} --slOrdPx=-1

# 2. 市价开空，同时附带止损
okx swap place --instId {交易对} --side sell --posSide short --ordType market --sz {合约张数} \
  --tdMode cross --slTriggerPx {止损价} --slOrdPx=-1

# 3. 【必做】验证止损单已生效
okx swap algo orders --instId {交易对}
# 必须看到 slTrigger 列有正确的止损价格，state 为 live
# 如果没有止损单，立即补挂（见下方"补挂止损"命令）
```

**补挂止损**（如果开仓时止损未生效，或需要单独添加止损）：
```bash
# 用策略单命令单独挂止损（注意：这里用 swap algo place，不是 swap place）
# swap place --side sell 是直接市价平仓，不是挂止损！
okx swap algo place --instId {交易对} --side sell --sz {合约张数} \
  --slTriggerPx {止损价} --slOrdPx=-1 --posSide long --tdMode cross

# 空单补挂止损
okx swap algo place --instId {交易对} --side buy --sz {合约张数} \
  --slTriggerPx {止损价} --slOrdPx=-1 --posSide short --tdMode cross

# 验证
okx swap algo orders --instId {交易对}
```

---

### 情况B：无持仓 + 震荡模式（予与裸K波段）

**开仓条件**：
- 做多：价格接近支撑位（距离 < 2%），且1H出现止跌信号（下影线、锤子线）
- 做空：价格接近阻力位（距离 < 2%），且1H出现止涨信号（上影线、射击之星）

**操作**：
1. 建仓：账户净值 × 15%，杠杆 = 当前级别最大杠杆的 60%
2. 止损：区间外 3%（支撑位下方 3% / 阻力位上方 3%）
3. 止盈：区间另一端的 80% 位置

---

### 情况C：有持仓 + 浮盈 > 10% + 趋势延续（滚仓核心）

**加仓条件**（必须同时满足）：
- `uplRatio > 0.10`（浮盈超过10%）
- 价格继续突破新高/新低（趋势延续确认）
- 当前杠杆 < 最大杠杆上限
- 今日止损次数 < 3

**加仓规则（比特皇/予与共同原则）**：
- 加仓量 ≤ 底仓量（1:1 原则，不超过底仓）
- 加仓后整体杠杆不超过上限
- 加仓部分必须单独设置止损（止损位 = 加仓价下方 3%）
- 底仓止损上移至成本价（保本止损）

**加仓命令模板**：
```bash
# 1. 加仓（市价开多，附带止损）
okx swap place --instId {交易对} --side buy --posSide long --ordType market --sz {加仓张数} \
  --tdMode cross --slTriggerPx {加仓止损价} --slOrdPx=-1

# 2. 验证加仓止损单已生效
okx swap algo orders --instId {交易对}

# 3. 底仓止损上移至成本价（修改已有的止损策略单）
okx swap algo amend --instId {交易对} --algoId {底仓止损单ID} \
  --newSlTriggerPx {成本价} --newSlOrdPx=-1

# 4. 验证所有止损单
okx swap algo orders --instId {交易对}
```

**趋势行情中保护浮盈（移动止损/Trailing Stop）**：
```bash
# 用 trailing stop 替代固定止损，自动跟踪最高价回撤3%平仓
okx swap algo trail --instId {交易对} --side sell --sz {合约张数} \
  --callbackRatio 0.03 --posSide long --tdMode cross

# 空单 trailing stop
okx swap algo trail --instId {交易对} --side buy --sz {合约张数} \
  --callbackRatio 0.03 --posSide short --tdMode cross
```

---

### 情况D：有持仓 + 浮亏（铁律）

**禁止加仓**，分析浮亏程度：
- 浮亏 < 5%：观察，不操作
- 浮亏 5%–止损位：等待，准备止损
- 触及止损位：**立即执行止损**

止损执行：
```bash
# 平多仓
okx swap close --instId {交易对} --mgnMode cross --posSide long

# 平空仓
okx swap close --instId {交易对} --mgnMode cross --posSide short
```

止损后更新状态文件（daily_stops + 1）。

---

### 情况E：有持仓 + 趋势反转信号

**平仓条件**（满足1条即可）：
- 4H级别出现明显反转形态（头肩顶/底、双顶/底）
- 价格跌破/突破关键支撑/阻力位
- 行情从趋势转为震荡（连续3根K线无方向）

**操作**：
- 信号强（形态完整）：一次性全平
- 信号弱（仅价格异常）：平仓50%，剩余移动止损

---

## 输出格式

每次分析后，输出以下结构化报告：

```
═══════════════════════════════════════
📊 滚仓策略分析报告
═══════════════════════════════════════
交易对：{instId}
当前价格：{price}
分析时间：{timestamp}

【行情模式】{趋势/震荡} - 置信度 {高/中/低}
  4H趋势：{上涨/下跌/横盘}
  关键支撑：{price}
  关键阻力：{price}

【当前持仓】
  方向：{多/空/无仓}
  均价：{avgPx}
  浮盈率：{uplRatio%}
  止损位：{slPx}

【账户状态】
  净值：${totalEq}
  今日止损次数：{n}/3
  最大杠杆上限：{lever}x

【操作建议】
  动作：{开仓/加仓/持仓观察/止损/平仓/强制休息}
  方向：{多/空}
  仓位：净值的 {n}%（约 ${amount}）
  杠杆：{lever}x
  入场价：{price}（{市价/限价}）
  止损位：{slPx}（亏损 {n}%）
  止盈位：{tpPx}（盈利 {n}%）
  盈亏比：{ratio}:1

【风险提示】
  {具体风险说明}

【执行确认】
  输入 'yes' 执行上述操作，输入 'no' 取消
═══════════════════════════════════════
```

---

## OKX Agent Trade Kit CLI 前置要求

本 Skill 通过 OKX Agent Trade Kit 的 CLI 工具执行所有交易操作，API 密钥始终存储在用户本地，不会传递给 AI。

**安装**：
```bash
npm install -g @okx_ai/okx-trade-cli
```

**配置密钥**（交互式，密钥保存在本地 `~/.okx/config.toml`）：
```bash
okx config init
```

**验证安装**：
```bash
okx account balance
```

如果 CLI 未安装或未配置，提示用户先完成上述步骤。

**安全说明**：
- API 密钥由 `okx-trade-cli` 在本地管理，AI 不接触密钥
- 所有交易通过 CLI 子进程执行，签名在本地完成
- 建议使用模拟盘 profile 进行测试：`okx config init` 时选择 demo 模式

---

## 状态文件

`~/.claude/rolling-position-state.json`：

```json
{
  "daily_stops": 0,
  "last_stop_date": "2026-04-07",
  "total_equity_usd": 10000,
  "positions": {
    "BTC-USDT-SWAP": {
      "entry_price": 80000,
      "base_size": 0.1,
      "add_count": 0,
      "stop_loss": 77600,
      "mode": "trend"
    }
  }
}
```

---

## 使用示例

```
/rolling-position BTC-USDT-SWAP
/rolling-position ETH-USDT-SWAP
/rolling-position SOL-USDT-SWAP
```

不带参数时，默认分析 BTC-USDT-SWAP。

---

## 免责声明

本 Skill 提供基于规则的策略建议，不构成投资建议。加密货币合约交易存在极高风险，可能导致本金全部损失。执行任何交易前请确认止损已正确设置，仓位大小在你的风险承受范围内。
