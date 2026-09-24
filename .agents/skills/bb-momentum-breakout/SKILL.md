---
name: bb-momentum-breakout
description: "布林带收缩突破 + MACD 确认的动量交易 Skill。当用户说'布林突破'、'动量突破'、'BB突破'、'启动突破策略'时使用此技能。自动检测布林带收缩后的方向性突破，结合 MACD 信号和 ATR 动态止损，快进快出捕捉动量行情。"
version: "1.0.0"
user-invocable: true
---

# 布林带动量突破策略（BB Momentum Breakout）

基于**布林带收缩（Squeeze）→ 方向突破**的动量交易策略，结合 MACD 信号过滤虚假突破，使用 ATR 动态设置止损止盈，适合捕捉短周期的方向性爆发行情。

## 策略逻辑

```
核心假设：价格在布林带收窄期积蓄能量，突破时产生方向性动量

信号触发条件（做多）：
  1. BB Width < 阈值（布林带收缩，波动率低）
  2. 当前收盘价 > 布林带上轨（向上突破）
  3. MACD 柱状 > 0 且 MACD 线 > 信号线（动量确认多头）
  4. 价格 > EMA20（在均线上方，过滤弱势突破）

信号触发条件（做空）：
  1. BB Width < 阈值（布林带收缩）
  2. 当前收盘价 < 布林带下轨（向下突破）
  3. MACD 柱状 < 0 且 MACD 线 < 信号线（动量确认空头）
  4. 价格 < EMA20（在均线下方）

止损：入场价 ± 1.5 × ATR(14)
止盈：入场价 ± 3.0 × ATR(14)（风险收益比 2:1）
```

## 参数说明

| 参数 | 类型 | 必填 | 默认值 | 说明 |
|------|------|------|--------|------|
| instId | string | ✅ | - | 交易对，如 BTC-USDT-SWAP |
| amount | number | ✅ | - | 每次开仓金额（USDT） |
| bar | string | ❌ | 15m | K 线周期：5m / 15m / 1H |
| bb_period | integer | ❌ | 20 | 布林带周期 |
| bb_std | number | ❌ | 2.0 | 布林带标准差倍数 |
| squeeze_threshold | number | ❌ | 0.03 | BB Width 收缩阈值（<此值视为收缩，建议 0.02-0.05） |
| atr_period | integer | ❌ | 14 | ATR 周期 |
| sl_atr_mult | number | ❌ | 1.5 | 止损 ATR 倍数 |
| tp_atr_mult | number | ❌ | 3.0 | 止盈 ATR 倍数 |
| lever | integer | ❌ | 3 | 合约杠杆倍数（建议 2-5，不超过 5） |
| mode | string | ❌ | swap | 交易模式：swap（永续合约）/ spot（现货，只能做多） |
| profile | string | ❌ | demo | 实盘 live / 模拟盘 demo |

## 执行流程

### Phase 1：参数确认

1. 用户触发后，确认 instId 和 amount。
2. 未指定 profile 时**默认 demo 模拟盘**，保护资金安全。
3. mode=swap 时，instId 格式须为 `XXX-USDT-SWAP`；mode=spot 时为 `XXX-USDT`。
4. 向用户展示完整参数并确认后开始执行。

### Phase 2：多指标信号采集

**Step 1：获取布林带数据**

```bash
# BB Width（布林带宽度，用于判断收缩）
okx market indicator bbwidth <instId> --bar <bar> --params <bb_period> <bb_std>

# BB %B（价格在布林带中的位置，>1 表示突破上轨，<0 表示突破下轨）
okx market indicator bbpct <instId> --bar <bar> --params <bb_period> <bb_std>

# 布林带上下轨（获取具体数值）
okx market indicator bb <instId> --bar <bar> --params <bb_period> <bb_std>
```

**Step 2：获取 MACD 信号**

```bash
# MACD（返回 macd 线、signal 线、histogram 柱状值）
okx market indicator macd <instId> --bar <bar>
# 默认参数：快线12, 慢线26, 信号线9
```

**Step 3：获取 EMA20 均线**

```bash
okx market indicator ema <instId> --bar <bar> --params 20
```

**Step 4：获取 ATR（用于动态止损止盈）**

```bash
okx market indicator atr <instId> --bar <bar> --params <atr_period>
```

**Step 5：获取当前价格**

```bash
okx market ticker <instId>
```

### Phase 3：信号判断

从各指标提取最新值后，按以下逻辑判断：

**收缩检测：**
```
is_squeeze = bbwidth < squeeze_threshold
```

**突破方向判断：**
```
# 多头突破
long_signal = is_squeeze AND bbpct > 1.0 AND macd_hist > 0 AND macd_line > signal_line AND price > ema20

# 空头突破（仅 mode=swap 时可用）
short_signal = is_squeeze AND bbpct < 0.0 AND macd_hist < 0 AND macd_line < signal_line AND price < ema20
```

**向用户输出当前指标快照：**
```
📊 指标快照 - <instId> [<bar>]
━━━━━━━━━━━━━━━━━━━━
当前价格：$xxx
BB Width：0.025（收缩中 ✅ / 未收缩 ❌）
BB %B：1.05（突破上轨 ✅）
MACD 柱：+12.5（多头 ✅）
EMA20：$xxx（价格在均线上方 ✅）
ATR(14)：$xxx
━━━━━━━━━━━━━━━━━━━━
信号：🚀 多头突破 / 🔻 空头突破 / ⏳ 等待收缩突破
```

### Phase 4：开仓执行

**条件满足时执行开仓：**

**Step 1：查询账户余额与现有仓位**

```bash
okx account balance USDT --profile <profile>
okx account positions --instType SWAP --profile <profile>
```

检查：
- USDT 余额 >= amount
- 同一标的无已有持仓（避免重复开仓）

**Step 2：设置杠杆**

```bash
okx swap leverage <instId> --lever <lever> --mgnMode cross --profile <profile>
```

**Step 3：计算止损止盈价**

```
# 多头
sl_price = entry_price - sl_atr_mult × atr
tp_price = entry_price + tp_atr_mult × atr

# 空头
sl_price = entry_price + sl_atr_mult × atr
tp_price = entry_price - tp_atr_mult × atr
```

**Step 4：下单（附带 TP/SL）**

多头开仓：
```bash
okx swap place --instId <instId> \
  --side buy --ordType market \
  --sz <amount> --tgtCcy quote_ccy \
  --tdMode cross --posSide long \
  --tpTriggerPx <tp_price> --tpOrdPx -1 \
  --slTriggerPx <sl_price> --slOrdPx -1 \
  --profile <profile>
```

空头开仓：
```bash
okx swap place --instId <instId> \
  --side sell --ordType market \
  --sz <amount> --tgtCcy quote_ccy \
  --tdMode cross --posSide short \
  --tpTriggerPx <tp_price> --tpOrdPx -1 \
  --slTriggerPx <sl_price> --slOrdPx -1 \
  --profile <profile>
```

**Step 5：开仓报告**

```
⚡ 开仓成功！
━━━━━━━━━━━━━━━━━━━━
方向：做多 / 做空
开仓价：$xxx
开仓金额：xxx USDT（杠杆 3x，名义价值 xxx USDT）
止盈价：$xxx（+x.x% / +ATR×3.0）
止损价：$xxx（-x.x% / -ATR×1.5）
风险收益比：1:2
订单ID：xxxxxxxxx
━━━━━━━━━━━━━━━━━━━━
```

### Phase 5：持仓监控

当检测到有未平仓位时，自动进入监控模式：

```bash
# 查询持仓和未平仓盈亏
okx account positions --instType SWAP --instId <instId> --profile <profile>
```

每个 bar 周期输出持仓状态：
```
📍 持仓监控 - <instId>
方向：做多 | 开仓价：$xxx
当前价：$xxx | 浮盈：+x.xx%
止盈：$xxx | 止损：$xxx
状态：持仓中... (TP/SL 已挂单，自动平仓)
```

> TP/SL 已在开仓时通过参数挂单，无需手动干预，触发后自动平仓。

### Phase 6：复盘输出

平仓后（无论止盈还是止损），输出本次交易复盘：

```
📋 交易复盘
━━━━━━━━━━━━━━━━━━━━
交易对：<instId>
方向：做多
开仓价：$xxx | 平仓价：$xxx
盈亏：+xxx USDT（+x.xx%）
持仓时长：约 x 小时 x 分
触发原因：止盈 / 止损
━━━━━━━━━━━━━━━━━━━━
下一步：继续监控新的突破信号...
```

## 使用示例

- "启动布林突破，BTC-USDT-SWAP，每次 100U"
- "BB突破策略，ETH-USDT-SWAP，200U，15分钟线"
- "动量突破 SOL-USDT-SWAP 50U，杠杆2倍，模拟盘"
- "查看 BTC-USDT-SWAP 当前布林带信号"
- "切换到实盘，布林突破 BTC-USDT-SWAP 100U"

## 风险提示

1. **默认模拟盘（demo）**，切换实盘需用户明确指定 `profile=live`。
2. 动量突破策略在震荡行情中假突破率较高，建议配合更高周期趋势判断。
3. 建议单笔开仓金额不超过账户总资金的 5-10%，杠杆不超过 5 倍。
4. BB Width 收缩阈值（squeeze_threshold）需根据标的波动特性调整，低波动标的建议调低至 0.02。
5. 本 Skill 仅供学习研究，不构成投资建议，盈亏自负。
