---
name: 极速猎手v1
description: "激进高收益策略 — 当用户说「激进策略」「高收益」「短期暴利」「极速猎手」「合并策略」「双信号」「复利交易」「高频交易」时使用此技能。RSI超卖+EMA金叉双信号驱动，10x~20x高杠杆，1H高频周期，复利加仓模式，专为2周内最大化收益率设计。⚠️高风险高回报，仅适合激进投资者。"
license: MIT
version: "1.1.0"
user-invocable: true
metadata:
  author: 极速猎手v1
  tags: [激进, 高收益, 复利, 高频, RSI, EMA, 双信号, 极速猎手, 高杠杆]
  agent:
    requires:
      bins: ["okx"]
    install:
      - id: npm
        kind: node
        package: "@okx_ai/okx-trade-cli"
        bins: ["okx"]
        label: "Install okx CLI (npm)"
---

# 极速猎手v1 — 激进高收益双信号策略

> **⚠️ 高风险警告**：本策略采用 **10x~20x 高杠杆** + **高频交易** + **复利加仓**，专为 **2周内最大化收益率** 设计。历史回测显示可能产生 **-100% 本金损失**。仅适合能承受全部本金亏损的激进投资者。不构成投资建议，盈亏自负。

---

## 策略定位

**「合约小师傅」+「趋势猎手」合并升级版**

| 维度 | 合约小师傅 | 趋势猎手 | **极速猎手（本策略）** |
|---|---|---|---|
| **核心思想** | RSI超卖抄底 | EMA金叉追涨 | **双信号驱动，择强入场** |
| **信号类型** | 单一RSI | 单一EMA | **RSI+EMA双确认** |
| **时间周期** | 4H | 4H | **1H（更高频）** |
| **杠杆倍数** | 3x~5x | 3x~5x | **10x~20x（激进）** |
| **止损方式** | 固定2% | ATR动态 | **固定1.5%（更紧）** |
| **止盈方式** | 固定3% | 1:2盈亏比 | **5%~10%（让利润奔跑）** |
| **仓位管理** | 固定仓位 | 固定仓位 | **复利加仓（盈利后加码）** |
| **持仓时间** | 短线 | 中线 | **超短线~短线** |
| **目标收益** | 稳健 | 稳健 | **2周内最大化** |

---

## 核心创新：双信号驱动系统

### 信号A：RSI超卖反弹（均值回归）
```
条件：RSI(14) < 25（比原版30更激进）
      + 价格接近近期低点
      + 成交量放大（>20日均量1.5倍）
方向：做多（抄底）
优势：抓住恐慌性抛售后反弹
```

### 信号B：EMA趋势加速（动量延续）
```
条件：EMA(9) 上穿 EMA(21)（金叉）
      + ADX > 30（强趋势确认，比原版25更严格）
      + 价格突破前1H高点
方向：做多（追涨）
优势：抓住趋势启动加速段
```

### 信号优先级（智能选择）
```
IF 信号A和信号B同时出现 → 满仓入场（复利加仓模式）
IF 仅信号A出现 → 50%仓位（RSI抄底）
IF 仅信号B出现 → 75%仓位（趋势追涨）
IF 无信号 → 空仓等待
```

---

## 激进参数配置（2周高收益目标）

| 参数 | 默认值 | 激进优化 | 说明 |
|---|---|---|---|
| `TIMEFRAME` | `1H` | ✅ | 1小时K线，更高频交易机会 |
| `LEVER` | `15x` | ✅ | 10x~20x可调，默认15x激进 |
| `RSI_THRESHOLD` | `30` | ✅ | RSI超卖阈值 |
| `RSI_EXIT` | `55` | ✅ | RSI>55即止盈，不贪多 |
| `EMA_FAST` | `9` | ✅ | 快线周期 |
| `EMA_SLOW` | `21` | ✅ | 慢线周期 |
| `ADX_THRESHOLD` | `25` | ✅ | ADX趋势强度阈值 |
| `STOP_LOSS_PCT` | `ATR×2.0 (1.5%~3%)` | ✅ | ATR动态止损 |
| `TAKE_PROFIT_PCT` | `ATR×3.0 (最少6%)` | ✅ | ATR动态止盈 |
| `TRAILING_STOP` | `true` | ✅ | 启用移动止盈 |
| `TRAILING_PCT` | `3%` | ✅ | 回撤3%触发移动止盈 |
| `COMPOUND_MODE` | `true` | ✅ | 盈利后复利加仓 |
| `COMPOUND_STEP` | `5%` | ✅ | 每盈利5%加仓一次 |
| `MAX_POSITIONS` | `3` | ✅ | 最多同时3个币种持仓 |
| `POSITION_SIZE` | `30%` | ✅ | 单币种初始仓位30% |

---

## 复利加仓系统（核心收益放大器）

### 加仓触发条件
```
当前持仓盈利 ≥ 5% 且 趋势未反转
→ 自动加仓至原仓位的 50%
→ 新止损上移至成本价（保本）
→ 新止盈目标上调至 +12%
```

### 加仓层级示例
```
初始：BTC 做多，仓位 $3,000（本金$10,000的30%）

第1次加仓：盈利5% → 加仓$1,500 → 总仓位$4,500
           止损上移至成本价，止盈目标+12%

第2次加仓：再盈利5%（累计10%）→ 加仓$2,250 → 总仓位$6,750
           止损上移至第1次加仓价，止盈目标+15%

最大仓位限制：单币种不超过本金的 80%
```

---

## 策略执行流程

```
Phase 0  →  前置检查（API + 余额 + 当前持仓数 < MAX_POSITIONS）
Phase 1  →  扫描所有目标币种（BTC/ETH/SOL/BNB/XRP/DOGE）
Phase 2  →  对每个币种计算：RSI + EMA + ADX + 成交量
Phase 3  →  信号评分：信号A强度 + 信号B强度
Phase 4  →  选择评分最高的1-3个币种入场
Phase 5  →  设置15x杠杆，市价开仓 + 止盈止损条件单
Phase 6  →  高频轮询（每30秒）：检查止盈/止损/加仓条件
Phase 7  →  平仓结算，更新复利基数，扫描下一机会
```

---

## ⚠️ 重要配置：策略状态文件

**文件路径**：`~/.qclaw/workspace-agent-ca7a859e/.agents/skills/极速猎手v1/strategy_state.json`

```json
{
  "active_positions": [],
  "available_margin": 10000,
  "total_equity": 10000,
  "compound_base": 10000,
  "daily_pnl": 0,
  "daily_trades": 0,
  "consecutive_losses": 0,
  "circuit_breaker": false,
  "last_update": null,
  "signal_scores": {
    "BTC": {"rsi_signal": 0, "ema_signal": 0, "total": 0},
    "ETH": {"rsi_signal": 0, "ema_signal": 0, "total": 0}
  }
}
```

---

## Phase 0 — 前置检查

### Step 0.1 验证API凭据
```bash
okx config show
# 确认 apiKey / secretKey / passphrase 已配置
```

### Step 0.2 检查账户权益
```bash
okx account balance
# 确保 USDT 可用余额 > 0
# 记录当前权益作为复利基数
```

### Step 0.3 检查当前持仓
```bash
okx swap positions
# 确保当前持仓数 < MAX_POSITIONS (默认3)
# 如有持仓，进入Phase 6监控模式
```

---

## Phase 1 — 多币种信号扫描

### Step 1.1 批量获取指标
```bash
# 对 BTC/ETH/SOL/BNB/XRP/DOGE 分别执行：

okx market indicator rsi BTC-USDT-SWAP --bar 1H --params 14 --limit 3
okx market indicator ema BTC-USDT-SWAP --bar 1H --params 9 --limit 3
okx market indicator ema BTC-USDT-SWAP --bar 1H --params 21 --limit 3
okx market indicator adx BTC-USDT-SWAP --bar 1H --params 14 --limit 3

# 记录：当前RSI、EMA9、EMA21、ADX、成交量
```

### Step 1.2 信号评分计算
```python
# 信号A评分（RSI超卖）
if RSI < 20:  rsi_score = 100      # 极度超卖
elif RSI < 25: rsi_score = 80      # 严重超卖
elif RSI < 30: rsi_score = 60      # 超卖
else: rsi_score = 0

# 信号B评分（EMA金叉+ADX）
if EMA9 > EMA21 and ADX > 35: ema_score = 100    # 强趋势金叉
elif EMA9 > EMA21 and ADX > 30: ema_score = 80   # 趋势金叉
elif EMA9 > EMA21: ema_score = 50                # 弱金叉
else: ema_score = 0

# 总评分
total_score = rsi_score * 0.4 + ema_score * 0.6
```

---

## Phase 2 — 智能选币与仓位分配

### Step 2.1 选择目标币种
```
按 total_score 排序，选择评分最高的 1-3 个币种

IF 最高分 > 80: 满仓模式（单币80%或双币各40%）
IF 最高分 60-80: 标准模式（单币50%或双币各25%）
IF 最高分 < 60: 观望，不入场
```

### Step 2.2 计算开仓数量
```python
# 以 BTC 为例，当前价 $72,000，本金 $10,000，杠杆 15x

position_size_usd = total_equity * position_pct * lever  # $10,000 * 0.3 * 15 = $45,000
btc_amount = position_size_usd / current_price           # $45,000 / $72,000 = 0.625 BTC
contract_size = round(btc_amount, 3)                     # 0.625 → 0.63 BTC
```

---

## Phase 3 — 开仓执行

### Step 3.1 设置杠杆
```bash
okx swap leverage --instId BTC-USDT-SWAP --lever 15 --mgnMode cross
```

### Step 3.2 计算止盈止损价（ATR动态）
```python
# ATR止损：基于波动幅度，更科学
# 公式：止损价 = 入场价 - (ATR × 2.0)
# 动态范围：1.5%~3%
atr = get_atr(instId)  # 获取14周期ATR
atr_pct = (atr / entry_price) * 100
stop_pct = 2.0 * atr_pct
stop_pct = max(1.5, min(3.0, stop_pct))  # 限制范围
stop_loss_price = entry_price * (1 - stop_pct / 100)

# ATR止盈：基于波动幅度
# 公式：止盈价 = 入场价 + (ATR × 3.0)
# 最小止盈6%，确保风险收益比至少1:1.5
tp_pct = 3.0 * atr_pct
tp_pct = max(6.0, tp_pct)  # 最小6%
take_profit_price = entry_price * (1 + tp_pct / 100)

# 移动止盈触发：盈利5%后，回撤3%触发
# 移动止盈价 = 最高价 * 0.97
```

### Step 3.3 市价开仓
```bash
okx swap place \
  --instId BTC-USDT-SWAP \
  --side buy \
  --ordType market \
  --sz 0.63 \
  --tdMode cross \
  --posSide long \
  --slTriggerPx 70920 \
  --slOrdPx -1 \
  --tpTriggerPx 77760 \
  --tpOrdPx -1
```

---

## Phase 4 — 高频监控与复利加仓

### Step 4.1 高频轮询（每30秒）
```bash
okx swap positions --instId BTC-USDT-SWAP
okx market ticker BTC-USDT-SWAP
```

### Step 4.2 复利加仓判断
```python
# 当前盈利百分比
pnl_pct = (current_price - entry_price) / entry_price * 100

# 加仓条件
if pnl_pct >= 5 and not added_position:
    # 加仓50%原仓位
    add_size = original_size * 0.5
    # 新止损上移至成本价
    new_sl = entry_price
    # 新止盈上调
    new_tp = entry_price * 1.12
    # 执行加仓
    execute_add_position(add_size, new_sl, new_tp)
```

### Step 4.3 移动止盈判断
```python
# 记录持仓期间最高价
highest_price = max(highest_price, current_price)

# 移动止盈触发
if pnl_pct > 5 and current_price < highest_price * 0.97:
    # 盈利回撤3%，触发移动止盈
    close_position("trailing_stop")
```

---

## Phase 5 — 平仓与结算

### Step 5.1 平仓执行
```bash
# 市价平仓
okx swap close --instId BTC-USDT-SWAP --posSide long --ordType market
```

### Step 5.2 更新状态文件
```json
{
  "last_trade": {
    "instId": "BTC-USDT-SWAP",
    "entry_price": 72000,
    "exit_price": 77760,
    "pnl_pct": 8.0,
    "pnl_usdt": 360,
    "exit_reason": "take_profit"
  },
  "compound_base": 10360,  // 原10000 + 盈利360
  "total_equity": 10360,
  "consecutive_losses": 0
}
```

### Step 5.3 熔断检查
```python
if consecutive_losses >= 3:
    circuit_breaker = true
    print("⚠️ 连续3次亏损，触发熔断，停止交易24小时")
```

---

## 风险控制（必读）

### 硬止损规则
1. **单笔止损**：-1.5%（15x杠杆下实际亏损22.5%本金）
2. **单日止损**：单日亏损达本金的30%即停止交易
3. **连续亏损**：连续3笔亏损触发24小时熔断
4. **最大回撤**：总权益回撤50%立即停止所有策略

### 杠杆风险提示
```
15x杠杆意味着：
- 价格反向波动6.67% → 本金归零（强平）
- 本策略设置1.5%止损 → 实际亏损22.5%本金
- 必须严格止损，不能扛单！
```

---

## 预期收益与风险（2周目标）

### 乐观情景（强趋势市场）
```
胜率：60%
盈亏比：3:1（平均盈利8% vs 平均亏损1.5%）
交易频率：每天2-3笔
2周交易次数：约30笔

预期收益率：+150%~+300%
（复利效应下可能更高）
```

### 悲观情景（震荡市）
```
胜率：40%
频繁止损，连续触发熔断
2周收益率：-50%~-100%（本金亏损）
```

### 最可能情景
```
胜率：50%
有盈有亏，复利缓慢增长
2周收益率：+30%~+80%
```

---

## 使用示例

### 启动极速猎手（默认参数）
```
用户：启动极速猎手，本金1万USDT
AI：扫描信号中... BTC信号评分85，EMA金叉+RSI超卖双确认
    建议：BTC满仓做多，15x杠杆，仓位80%
    止损：-1.5%，止盈：+8%，启用移动止盈
用户：确认执行
AI：开仓成功，进入高频监控模式...
```

### 自定义激进参数
```
用户：极速猎手，20x杠杆，RSI阈值20，止盈10%
AI：⚠️ 警告：20x杠杆风险极高，价格反向5%即爆仓
    确认使用以下参数？
    - 杠杆：20x
    - RSI阈值：20（极度超卖才入场）
    - 止盈：10%
    - 止损：1.5%
用户：确认
```

---

## 与原版策略对比总结

| 特性 | 合约小师傅 | 趋势猎手 | 极速猎手 |
|---|---|---|---|
| **适合市场** | 震荡市 | 强趋势 | 任何有波动市场 |
| **交易频率** | 低 | 中 | **高（1H周期）** |
| **杠杆** | 3x~5x | 3x~5x | **10x~20x** |
| **单笔风险** | 2% | ATR动态 | **1.5%（更紧）** |
| **收益潜力** | 稳健 | 稳健 | **激进高收益** |
| **最大回撤** | 可控 | 可控 | **可能-100%** |
| **操作难度** | 简单 | 中等 | **需要盯盘** |
| **复利** | 无 | 无 | **有（核心）** |

---

> **最后警告**：本策略为激进投机设计，2周高收益目标意味着承担高风险。请确保：
> 1. 只投入能承受全部损失的资金
> 2. 严格遵循止损规则，不扛单
> 3. 市场极端行情时手动干预
> 4. 盈利后及时提取本金，用利润继续博弈

**祝交易顺利，但请做好最坏的打算。**
