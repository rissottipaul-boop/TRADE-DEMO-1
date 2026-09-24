---
name: spot-momentum-scan-validate
description: >
  量价共振两阶段验证策略。第一阶段"只扫描不下单"，输出信号报告供人工确认；
  第二阶段"确认下单"，用户明确传入 confirm_trade=true 后才执行实际交易。
  当用户提到"先扫描再下单"、"验证信号"、"确认后交易"、"两阶段交易"、
  "信号验证"、"先看信号"时，必须使用本 skill。
---

# 量价共振两阶段验证策略

## 策略概述

本 skill 将交易流程拆分为两个明确阶段：

```
阶段一（默认）：扫描 → 计算指标 → 输出信号报告       ← 不触碰账户
阶段二（需确认）：用户审阅报告 → 传入 confirm_trade=true → 执行下单 + 挂止损止盈
```

人工确认作为硬隔离，防止信号逻辑有误时直接造成资金损失。

---

## 参数说明

| 参数 | 默认值 | 说明 |
|------|--------|------|
| `confirm_trade` | `false` | **必须显式设为 true 才会下单**，默认只扫描 |
| `timeframe` | `1H` | K线周期（`1H` / `4H` / `15m`） |
| `price_change_min` | `1.0` | 最小当前K线涨幅（%） |
| `vol_ratio_min` | `1.5` | 最小量比 |
| `rsi_min` / `rsi_max` | `50` / `72` | RSI 区间 |
| `position_pct` | `5` | 每笔仓位占可用 USDT 的比例（%） |
| `stop_loss_pct` | `2.0` | 固定止损幅度（%） |
| `trailing_pct` | `2.0` | 移动止盈回撤幅度（%） |
| `trailing_activate_pct` | 等于 `stop_loss_pct` | 移动止盈激活门槛（%） |
| `scan_pairs` | 前50大成交量 | 可传入指定币对列表 |
| `top_n` | `50` | 按成交额取前 N 个现货 USDT 币对 |

---

## 阶段一：扫描（confirm_trade=false，默认）

### 执行步骤

**Step 1 — 获取行情**

用 `market_get_tickers` 拉取所有现货 USDT 交易对，按 24h 成交额降序，取前 `top_n` 名。
若 `scan_pairs` 不为空，则只扫描指定列表，忽略 `top_n`。

**Step 2 — 拉取 K 线**

对每个币对调用 `market_get_candles`：
```
instType: SPOT
bar: <timeframe>
limit: 60
```
两次调用之间间隔 100ms（`time.sleep(0.1)`），避免触发限速。
若 K 线数量 < 30，跳过该币对并在报告中标注"数据不足"。

**Step 3 — 计算指标**

调用 `scripts/calc_indicators.py` 中的 `analyze()` 函数，计算：

```python
MA20          = 收盘价20周期简单均线
MACD Histogram = 使用完整双EMA历史序列（与TradingView一致，非单点近似）
RSI(14)       = Wilder平滑法
vol_ratio     = 最新K线量 / 近20根均量
price_change  = (收盘 - 开盘) / 开盘 × 100
score         = vol_ratio/vol_ratio_min×3 + price_change/price_change_min×3（上限各5分）
```

**Step 4 — 过滤信号**

五个条件全部满足才触发：

| 条件 | 表达式 |
|------|--------|
| 价格在均线上方 | `close > MA20` |
| MACD 多头 | `macd_hist > 0` |
| 当前K线涨幅 | `price_change >= price_change_min` |
| 放量 | `vol_ratio >= vol_ratio_min` |
| RSI 区间 | `rsi_min <= RSI <= rsi_max` |

有多个信号时，按 `score` 降序排列。

**Step 5 — 输出扫描报告（不操作账户）**

```
=== 量价共振扫描报告（只读，未下单）===
扫描时间: 2024-01-15 14:30 UTC+8
扫描币对: 50 个  |  触发信号: 3 个  |  confirm_trade: false

信号详情:
┌─────────────┬────────┬──────┬──────┬──────────┬───────────────────────────────────┐
│ 币对        │ 涨幅   │ 量比 │ RSI  │ 收盘价   │ 各条件状态                        │
├─────────────┼────────┼──────┼──────┼──────────┼───────────────────────────────────┤
│ SOL-USDT    │ +3.2%  │ 2.8x │ 62.1 │  86.04   │ ✅ MA ✅ MACD ✅ 涨幅 ✅ 量比 ✅ RSI │
│ AVAX-USDT   │ +2.5%  │ 1.9x │ 58.4 │   9.47   │ ✅ MA ✅ MACD ✅ 涨幅 ✅ 量比 ✅ RSI │
│ LINK-USDT   │ +2.1%  │ 1.6x │ 55.2 │   9.22   │ ✅ MA ✅ MACD ✅ 涨幅 ✅ 量比 ✅ RSI │
└─────────────┴────────┴──────┴──────┴──────────┴───────────────────────────────────┘

未触发币对摘要（仅展示差一个条件的，帮助理解信号边界）:
  BNB-USDT  — 差: RSI=74.3（超买）
  DOGE-USDT — 差: 量比=1.3（低于1.5阈值）

⚠️  以上为只读扫描，未执行任何下单操作。
    如需下单，请在下一条消息中明确告知并传入 confirm_trade=true。
```

将扫描结果（含未触发币对的指标）写入 CSV（追加）：

```
timestamp, instId, price_change_pct, vol_ratio, rsi, macd_hist, ma20, close_price,
score, signal_triggered, failed_checks, confirm_trade, status, notes
```

`status` 固定为 `SCAN_ONLY`，`confirm_trade` 固定为 `false`。

---

## 阶段二：确认下单（confirm_trade=true）

**触发方式**：用户审阅阶段一报告后，明确说"确认下单"或"confirm_trade=true"。

执行前，**再次扫描并输出最新信号**（市场可能已变化），然后展示确认提示：

```
⚠️  即将对以下币对执行实际下单，操作不可撤销：
    SOL-USDT  买入约 50 USDT（可用余额的 5%）
    AVAX-USDT 买入约 50 USDT
    LINK-USDT 买入约 50 USDT

    止损：成交价 -2%（固定）
    止盈：盈利 ≥2% 激活，高点回撤 2% 触发（移动）

    请再次确认：回复"确认执行"后开始下单。
```

收到"确认执行"后，按以下步骤执行（与 spot-momentum-1h 完全一致）：

**Step 1 — 查询余额**

`account_get_balance` 获取可用 USDT。若 < 10 USDT，停止并提示。

**Step 2 — 下市价买单**

对每个信号币对：
1. `market_get_instruments` 获取 `lotSz`、`tickSz`
2. `market_get_ticker` 获取最新 ask 价，计算买入数量（按 `lotSz` floor）
3. `spot_place_order` 下单：
   ```
   instId: <币对>
   tdMode: cash
   side: buy
   ordType: market
   sz: <数量>
   ```
4. `spot_get_order` 轮询直到 `filled`，获取 `fill_price`、`fill_sz`

若已有持仓，跳过该币对。

**Step 3 — 挂固定止损单**

```
slTriggerPx = fill_price × (1 - stop_loss_pct/100)，按 tickSz floor
slOrdPx     = -1（市价止损）
ordType     = conditional
```

`spot_place_algo_order` 提交，记录 `sl_algo_id`。失败则重试 2 次，仍失败则警告并标记 `sl_status=FAILED`。

**Step 4 — 挂移动止盈单**

```
activePx       = fill_price × (1 + trailing_activate_pct/100)，按 tickSz ceil
callbackRatio  = trailing_pct / 100
ordType        = move_order_stop
```

`spot_place_algo_order` 提交，记录 `tp_algo_id`。失败处理同上。

**Step 5 — 写入 CSV**

追加到与阶段一相同的 CSV 文件，字段同阶段一，额外字段：

```
order_id, fill_price, fill_sz,
sl_price, sl_algo_id, sl_status,
tp_activate_price, tp_callback_ratio, tp_algo_id, tp_status
```

`status` 为 `FILLED` 或 `FAILED`，`confirm_trade` 为 `true`。

**Step 6 — 输出交易摘要**

```
=== 量价共振交易执行结果 ===
扫描时间: 2024-01-15 14:32 UTC+8
confirm_trade: true

成交详情:
┌─────────────┬──────────┬──────────┬───────────────────────────────────────────────┐
│ 币对        │ 成交价   │ 成交量   │ 风控挂单                                      │
├─────────────┼──────────┼──────────┼───────────────────────────────────────────────┤
│ SOL-USDT    │  86.04   │  0.5810  │ 止损 @84.32 (-2%) ✅  移动止盈激活 @87.76 (+2%) ✅ │
│ AVAX-USDT   │   9.47   │  5.2800  │ 止损 @ 9.28 (-2%) ✅  移动止盈激活 @ 9.66 (+2%) ✅ │
│ LINK-USDT   │   9.22   │  5.4200  │ 止损 @ 9.04 (-2%) ✅  移动止盈激活 @ 9.40 (+2%) ✅ │
└─────────────┴──────────┴──────────┴───────────────────────────────────────────────┘

账户操作:
- 共使用 USDT: 150.00
- 最大亏损上限: -3.00 USDT（每笔 50 USDT × 2% × 3 笔）
- 记录已写入: ~/momentum_trades.csv
```

---

## 两阶段完整对话示例

```
用户: 帮我扫描一下现货市场有没有量价共振信号，先别下单

Claude: [执行阶段一，输出扫描报告]
        ⚠️ 以上为只读扫描，未执行任何下单操作。
        如需下单，请回复"确认下单"。

用户: SOL 和 AVAX 信号不错，LINK 量比有点低先不买，
      帮我只买 SOL 和 AVAX，confirm_trade=true

Claude: [重新扫描确认信号仍有效，展示确认提示]
        即将对 SOL-USDT、AVAX-USDT 执行实际下单……
        请回复"确认执行"。

用户: 确认执行

Claude: [执行阶段二，仅对 SOL-USDT 和 AVAX-USDT 下单]
        [输出成交摘要]
```

---

## 技术指标计算脚本

读取 `scripts/calc_indicators.py` 获取 `analyze()` 实现。
脚本接受 OKX 原始 K 线数据，返回每个币对的指标值与信号判断。

---

## 常见问题处理

**API 限速**: 每次 K 线请求之间加 100ms 延迟。

**精度**: 下单数量按 `lotSz` floor；`slTriggerPx` 按 `tickSz` floor；`activePx` 按 `tickSz` ceil。

**数据不足**: K 线 < 30 根的币对直接跳过，在扫描报告中标注。

**重试**: 下单或挂算法单失败时最多重试 2 次，仍失败则在 CSV 标记 `FAILED` 并输出 `⚠️` 警告。
