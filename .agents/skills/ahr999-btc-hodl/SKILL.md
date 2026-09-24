---
name: ahr999-btc-hodl
description: >
  Use this skill when the user mentions AHR999 or 九神指数 (in any form),
  OR when they want to: check BTC valuation signals, set up
  automatic/recurring BTC purchases (定投/DCA), review their BTC
  accumulation history or performance, or decide whether to hodl/accumulate
  bitcoin right now. This skill owns all AHR999 queries and all BTC DCA
  workflows — checking the index value, interpreting thresholds, scheduling
  timed buys, and generating performance reports from local records. Skip
  for: grid bots, BTC selling, pure price questions, altcoin DCA without
  BTC context.
---

# AHR999 BTC Hodl（九神BTC囤币）

> **核心理念**：AHR999 是比特币链上估值指标，反映加密市场整体周期位置。低估时定投，高估时暂停——用客观指标替代主观情绪。
> ⚠️ **多资产说明**：AHR999 基于 BTC 链上数据，对其他资产（ETH、SOL 等）仅作市场周期参考信号，相关性较低，请注意。
> ⚠️ **风险声明**：AHR999 仅为参考指标，不构成投资建议。所有定投决策由用户自行负责。

---

## 使用模式

| 用户说 | 执行模式 |
|--------|---------|
| "该不该定投" / "现在适合 DCA 吗" | **建议模式**：AHR999 + 推荐行动 |
| "帮我每天定投 100U BTC" / "设置定投计划" | **定时定投模式**：理解策略意图 → 确认 → CronCreate 建立定时任务 |
| "帮我现在买 100U BTC" | **单次买入模式**：确认 → 立即下单 → 记录 |
| "给我本周报告" / "定投复盘" | **报告模式**：读本地文件 → 汇总 → OpenClaw message |
| "查看定投任务" / "停止定投" / "修改金额" | **任务管理模式**：CronList 查看 → CronDelete 停止 → 可重建 |

---

## Step 1：获取 AHR999（直接 CLI）

```bash
okx market indicator ahr999 BTC-USDT
```

返回字段：`ahr999`（当前值）、`dca_cost_200`（200日均价）、`fitted_price`（拟合估值价）、`zone`（区间）。

对照以下估值区间：

| AHR999 值 | 市场状态 | 定投建议 | 信号 |
|-----------|---------|---------|------|
| < 0.45 | 严重低估，历史底部区 | 🔥 强烈建议加大定投 | 🟢 深绿 |
| 0.45–1.0 | 低估，价值机会 | ✅ 建议正常定投 | 🟢 绿 |
| 1.0–1.2 | 公允估值，临界区 | ⚖️ 观望，不加仓 | 🟡 黄 |
| 1.2–2.0 | 略微高估 | ⚠️ 暂停定投 | 🟠 橙 |
| > 2.0 | 明显高估 | 🛑 暂停定投，等待回调 | 🔴 红 |

> ⚠️ **反弹提醒**（每次建议创建或暂停时必须输出）：
> 不要根据单日 AHR999 值做决策。更稳健的方式：等待 AHR999 连续 3 天处于该区间后再执行，避免因短期波动反复开关。

---

## Step 2：决策引擎

```
if AHR999 ≤ 1.0:
    ACTION_BUY   → 建议执行定投买入
elif 1.0 < AHR999 ≤ 1.2:
    ACTION_HOLD  → 观望，维持现状
else:
    ACTION_PAUSE → 暂停下单（持仓不动，不卖出）
```

**关键原则**：`ACTION_PAUSE` 只意味着"停止新的买入指令"，绝不触发任何卖出操作。

---

## Step 3A：定时定投模式

### 理解策略意图并收集参数

**第一步：收集缺失参数**（一次性询问）

1. **策略**：每次买多少 USDT？是否根据 AHR999 分级？（必填）
2. **频率**：每天 / 每周几？（必填）
3. **执行时间**：可选，默认 10:00

记录文件固定为 `~/btc-dca-log.jsonl`，无需询问。

**第二步：将用户意图翻译为 prompt 决策逻辑**

用户的策略可以多样，不要套固定模板。根据用户描述自由设计：

| 策略类型 | 用户说法示例 | prompt 内决策逻辑 |
|----------|------------|-----------------|
| 固定定投 | "每天买 100U" | `invest = 100`（直接传 USDT，无需算 BTC 数量） |
| 变额定投 | "AHR999 越低买越多" | `if ahr999 < 0.45: invest=300; elif ahr999 <= 1.0: invest=100; else: skip` |
| 指标分级 | "AHR999 < 0.45 买 500U，其余 100U" | `if ahr999 < 0.45: invest=500; elif ahr999 <= 1.0: invest=100; else: skip` |
| 频率可变 | "AHR999 < 0.5 时每天买，否则每周买一次" | 创建两个任务：① 每天 cron，prompt 内 `if ahr999 >= 0.5: skip`；② 每周 cron 无条件买入 |

**第三步：展示确认框**，让用户确认翻译后的逻辑正确再创建任务。

### 展示确认框

```
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
📅 AHR999 BTC 定投计划确认
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
当前 AHR999: [X]（[区间状态]）
资产: BTC-USDT 现货
策略: [翻译后的买入逻辑，如"AHR999<0.45买300U，否则100U"]
频率: [每天/每周X] [H]:00
记录文件: ~/btc-dca-log.jsonl

⚠️ 暂停 = 不下单，持仓永不被自动卖出

回复"确认"执行，其他取消。
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
```

### 用户确认后：创建定时任务

> ⚠️ 创建任务前，将 prompt 中所有 `[占位符]` 替换为用户实际配置值，不要将字面量写入。

**两步操作**：

**① 注册定时调度**（创建两个 `CronCreate`，均 `durable: true`）：

- **定投执行任务**：按用户设定频率执行 AHR999 检查 + 下单
- **复盘报告任务**：每周一 09:00 自动汇总上周数据，推送 OpenClaw message 给用户（用户也可指定改为月报）
```
# 任务一：定投执行
taskName:       "ahr999-btc-dca-[asset]-[period]"   示例: ahr999-btc-dca-daily
cronExpression: "0 10 * * *"   # 每天 10:00（本地时区）
                "0 10 * * 3"   # 每周三 10:00

# 任务二：复盘报告（同时创建，默认每周一 09:00）
taskName:       "ahr999-btc-report-weekly"
cronExpression: "0 9 * * 1"   # 每周一 09:00

prompt 内容（完全自包含，无上下文依赖，占位符已替换）：
─────────────────────────────────────
执行 AHR999 BTC 定投检查任务。

配置：目标资产 BTC-USDT，记录文件 ~/btc-dca-log.jsonl

Step 1 - 获取 AHR999：
  okx market indicator ahr999 BTC-USDT
  读取 ahr999 字段值

Step 2 - 决策：
  [此处填入翻译后的策略逻辑，例如：]
  if AHR999 < 0.45: invest = 300
  elif AHR999 <= 1.0: invest = 100
  else: skip（跳到 Step 4，记录 action=skip）

Step 3 - 现货买入：
  a) okx spot place --instId BTC-USDT --side buy --ordType market --sz [invest] --tgtCcy quote_ccy
     注：--sz 传 USDT 金额，--tgtCcy quote_ccy 表示以计价货币（USDT）计量
  b) 从返回结果取 ordId（返回结构：{"ordId":"...","sCode":"0",...}）
  c) okx spot get --instId BTC-USDT --ordId [ordId]
     → 读取 fillSz（成交 BTC 数量）、avgPx（成交均价）、fee（手续费）、feeCcy（手续费币种）
     注：买单 feeCcy 为 BTC（fee 为负数，取绝对值 × avgPx 换算为 USDT）
  如下单失败：记录 action=fail，note=错误信息，不重试，直接进 Step 4

Step 4 - 追加记录到 ~/btc-dca-log.jsonl（JSON Lines，一行一条）：
  {"date":"YYYY-MM-DD","ahr999":[X],"action":"buy|skip|fail",
   "invest_usdt":[X],"fill_price":[avgPx],"fill_sz":[fillSz],
   "order_id":"[ordId]","fee_btc":[abs(fee)],"fee_usdt":[abs(fee)*avgPx],"note":"[原因]"}

Step 5 - 输出执行摘要：
  买入成功 → "✅ [日期] AHR999=[X] 买入 [fillSz] BTC @ [avgPx] USDT（投入 [invest] U，手续费 [fee] U）"
  跳过     → "⏸ [日期] AHR999=[X]（> 1.0），本次跳过定投"
  失败     → "❌ [日期] 下单失败：[错误信息]"

约束：
- 只做现货买入（side=buy），不做任何卖出
- 下单失败不重试
- 时间基准统一 UTC+8
─────────────────────────────────────

# 复盘报告任务 prompt（完全自包含）：
─────────────────────────────────────
执行 AHR999 BTC 定投每周复盘报告。

Step 1 - 读取记录：
  读取 ~/btc-dca-log.jsonl，筛选最近 7 天的记录

Step 2 - 聚合统计：
  买入次数、跳过次数、失败次数
  累计投入 USDT（Σ invest_usdt）
  总买入 BTC（Σ fill_sz）
  加权平均成本（Σ(fill_sz × fill_price) / Σ(fill_sz)）

Step 3 - 获取市场数据：
  okx market ticker BTC-USDT → 取 last 为当前价
  okx market indicator ahr999 BTC-USDT → 今日 AHR999

Step 4 - 计算盈亏：
  浮动盈亏% = (当前价 - 平均成本) / 平均成本 × 100%
  浮动盈亏U = (当前价 - 平均成本) × 总持有BTC

Step 5 - 输出报告（OpenClaw message 格式，参照 Step 4 报告模板）
─────────────────────────────────────
```

---

## Step 3C：任务管理

用户说"查看定投任务"、"停止定投"、"修改定投金额"时进入此模式。

### 查看当前任务

调用 `CronList`，列出所有活跃定时任务，找到 `ahr999-btc-dca-*` 相关条目，展示给用户：

```
📋 当前 AHR999 定投任务
─────────────────────────
任务名: ahr999-btc-dca-daily
调度:   每天 10:00（0 10 * * *）
状态:   活跃
⚠️ 注意：定时任务 7 天后自动过期，到期前请重新创建。
```

> 注：若 `CronList` 返回为空，说明任务已过期或本次会话未注册。提醒用户重新触发"帮我设置定投计划"即可重建。

### 停止任务

1. `CronList` 找到对应任务的 Job ID
2. `CronDelete(id)` 停止调度
3. 输出确认："✅ 定投任务已停止，持仓不受影响。如需恢复，重新设置即可。"

### 修改任务（金额/频率/策略）

1. `CronList` 找到旧任务 Job ID → `CronDelete` 停止
2. 重新走 Step 3A 流程（重新收集参数 → 翻译策略 → 确认框 → `CronCreate` 注册新调度）

---

## Step 3B：单次买入模式

用户说"帮我现在买 X U BTC"时使用此流程。

1. 获取 AHR999 → 输出当前区间
2. 展示确认框：

```
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
🛒 单次买入确认
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
当前 AHR999: [X]（[区间状态]）
资产: BTC-USDT 现货
金额: [X] USDT（约 [sz] BTC）

回复"确认"执行，其他取消。
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
```

3. 用户确认后执行买入：
   - `okx spot place --instId BTC-USDT --side buy --ordType market --sz [invest_usdt] --tgtCcy quote_ccy`
   - 从返回取 `ordId`
   - `okx spot get --instId BTC-USDT --ordId [ordId]` → 读取 `fillSz`、`avgPx`、`fee`（BTC）、`feeCcy`
4. 追加记录到 `~/btc-dca-log.jsonl`（格式同上）
5. 输出摘要

---

## Step 4：复盘报告模式

**触发方式**：
- 用户主动触发：`"给我本周报告"` / `"定投复盘"` / `"DCA 回顾"`
- 定时自动触发：开启定投时同步创建的复盘报告任务（默认每周一 09:00）自动执行并推送

记录文件固定读取 `~/btc-dca-log.jsonl`。

### 执行步骤

1. 读取 `~/btc-dca-log.jsonl`（JSON Lines）
2. 按时间范围筛选（本周 / 本月 / 全部，未指定则默认本周）
3. 聚合统计：
   - 买入次数、跳过次数、失败次数
   - 累计投入 USDT
   - 总买入 BTC 数量（Σ fill_sz）
   - 加权平均成本（Σ(fill_sz × fill_price) / Σ(fill_sz)）
4. `okx market ticker BTC-USDT` → 取 `last` 为当前价
5. 计算浮动盈亏：`(currentPrice - avgCost) / avgCost × 100%`
6. `okx market indicator ahr999 BTC-USDT` → 今日 AHR999
7. 输出报告（OpenClaw message 格式）

### 报告格式

```
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
📊 AHR999 BTC 定投报告 [起止日期]
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
执行统计: [X] 次买入 / [Y] 次跳过
累计投入: [XXXX] USDT
平均成本: [XX,XXX] USDT/BTC
累计持有: [X.XXXXX] BTC
当前价格: [XX,XXX] USDT
浮动盈亏: [+/-XX.XX%]（[+/-XXX] USDT）

── 明细记录 ──────────────────
日期   AHR999  成交价   金额    状态
[DD]   [X.XX]  [XX,XXX] [X]U   ✅买入
[DD]   [X.XX]  —        —      ⏸跳过
[DD]   [X.XX]  —        —      ❌失败

── 当前状态 ──────────────────
今日 AHR999: [X]（[区间信号]）
下期建议: [继续每天 [X] USDT 定投 / 当前 AHR999 > 1.0，建议暂停]
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
```

---

## 本地记录文件规范

**固定路径**：`~/btc-dca-log.jsonl`（不询问用户，文件不存在时自动创建）
**格式**：JSON Lines（每行一个 JSON 对象，追加写入）

```jsonl
{"date":"2026-04-06","ahr999":0.47,"action":"buy","invest_usdt":100,"fill_price":68845.3,"fill_sz":0.00145253,"order_id":"3455277159819657216","fee_btc":0.00000145253,"fee_usdt":0.1001,"note":""}
{"date":"2026-04-07","ahr999":1.05,"action":"skip","invest_usdt":0,"fill_price":null,"fill_sz":0,"order_id":null,"fee_btc":0,"fee_usdt":0,"note":"AHR999 > 1.0"}
{"date":"2026-04-08","ahr999":0.68,"action":"fail","invest_usdt":100,"fill_price":null,"fill_sz":0,"order_id":null,"fee_btc":0,"fee_usdt":0,"note":"insufficient balance"}
```

| 字段 | 类型 | 说明 |
|------|------|------|
| date | string | 执行日期 YYYY-MM-DD |
| ahr999 | number | 当日 AHR999 值 |
| action | string | `buy` / `skip` / `fail` |
| invest_usdt | number | 计划投入金额（跳过时为 0） |
| fill_price | number\|null | 成交均价 `avgPx`（未成交时为 null） |
| fill_sz | number | 成交 BTC 数量 `fillSz`（未成交时为 0） |
| order_id | string\|null | OKX 订单 ID |
| fee_btc | number | 手续费 BTC 数量（买单费用扣 BTC，取 `fee` 绝对值） |
| fee_usdt | number | 手续费折算 USDT（fee_btc × fill_price） |
| note | string | 备注（跳过/失败原因） |

---

## CLI 工具参考

| 命令 | 用途 |
|------|------|
| `okx market indicator ahr999 BTC-USDT` | 获取当前 AHR999 值 |
| `okx market ticker BTC-USDT` | 获取实时价格（`last` 字段） |
| `okx spot place --instId BTC-USDT --side buy --ordType market --sz [usdt] --tgtCcy quote_ccy` | 现货市价买入（sz 传 USDT 金额），返回 ordId |
| `okx spot get --instId BTC-USDT --ordId [ordId]` | 查询成交详情（fillSz、avgPx、fee） |
| `okx account balance` | 查询账户余额（下单前可选检查 USDT 余额） |

> **多资产支持**：将 `BTC-USDT` 替换为其他交易对即可，但须显示跨资产免责说明。

**跨资产使用说明（非 BTC 资产时必须输出）**：
> ℹ️ AHR999 是 BTC 专属链上指标。用于 [资产] 时仅作市场周期参考：BTC 低估 → 整体市场接近底部 → 有利于主流资产配置。请结合该资产自身基本面综合判断。

---

## 安全规则（不可跳过）

1. **确认门不可绕过**：定时任务或单次买入前，必须展示确认框，等待用户明确回复"确认"
2. **不做任何卖出**：本 skill 只执行现货买入，任何模式下均不触发 sell
3. **每次执行必须记录**：买入、跳过、失败三种结果均须追加写入 `~/btc-dca-log.jsonl`
4. **失败不重试**：下单失败记录原因后退出，等待下次定时触发
5. **反弹提醒必须输出**：每次建议买入（ACTION_BUY）时，必须附上反弹提醒
6. **跨资产声明必须输出**：用户指定非 BTC 资产时，必须显示跨资产使用说明

---

## 边缘案例处理

| 场景 | 处理方式 |
|------|---------|
| `okx market indicator ahr999` 调用失败 | 自动降级为 K 线自算（见下方公式），无需用户干预 |
| USDT 余额不足 | 记录 action=fail，note="insufficient balance" |
| 记录文件不存在 | 自动创建后追加写入 |
| AHR999 在临界区 1.0–1.2 | 额外输出："当前处于临界区，建议观察趋势方向后再决策" |
| 用户请求跳过确认直接执行 | 拒绝；定投涉及真实资金，确认门为必须步骤 |
| 非 BTC 资产 | 正常执行，但必须显示跨资产免责说明 |
| 记录文件已有当日记录 | 追加写入（允许同一天多条记录） |

### AHR999 降级自算公式（CLI 失败时启用）

```bash
okx market candles BTC-USDT --bar 1D --limit 210   # 取 close 字段
okx market ticker BTC-USDT                          # 取 last 字段为 P
```

```
MA200_geo = exp( mean( ln(close₁), ..., ln(close₂₀₀) ) )
coinAgeDays = (今日日期 - 2009-01-03) 的天数
FitPrice    = 10 ^ (5.84 × log₁₀(coinAgeDays) - 17.01)
AHR999 = P² / (MA200_geo × FitPrice)
```

> 输出时注明："AHR999 CLI 不可用，已通过 K 线数据自算（数据源：OKX market candles）"
