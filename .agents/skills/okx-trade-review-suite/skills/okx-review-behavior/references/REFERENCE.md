# okx-review-behavior · 算法与规则参考

本文件定义五类行为标签的判定公式、字段需求、误差边界与阈值建议。

---

## 1. 数据字段映射

### `account positions-history`

| 字段 | 说明 |
|------|------|
| `instId` | 品种 |
| `openAvgPx` | 开仓均价 |
| `closeAvgPx` | 平仓均价 |
| `oTime` | 开仓时间 |
| `cTime` | 平仓时间 |
| `realizedPnl` | 已实现盈亏 |
| `direction` | long/short |
| `size` | 仓位规模 |

### `market candles`

| 字段 | 说明 |
|------|------|
| `ts` | K 线时间 |
| `h` | 最高价 |
| `l` | 最低价 |
| `c` | 收盘价 |

---

## 2. 行为判定规则

### 2.1 追高（Chase Entry）

```text
dist_to_24h_high = (high_24h - open_price) / high_24h
if dist_to_24h_high < chase_threshold (默认 2%) => 追高
```

### 2.2 割肉（Panic Exit）

```text
rebound_1h_before_close = (close_price - low_1h_window) / low_1h_window
if rebound_1h_before_close > rebound_threshold (默认 1%)
and post_close_price continues up within confirm_window
=> 割肉
```

### 2.3 报复交易（Revenge Trade）

```text
if previous_trade_pnl < 0
and next_open_time - previous_close_time <= revenge_window (默认 30m)
=> 报复交易
```

可选附加条件：同品种或仓位放大倍数 >1.2。

### 2.4 FOMO 加仓

```text
if existing_position_unrealized_pnl > 0
and add_position_price > previous_avg_entry
=> FOMO add-on
```

### 2.5 死扛（Bag Holding）

```text
if unrealized_drawdown <= hold_loss_threshold (默认 -10%)
and holding_time >= hold_time_threshold (默认 48h)
=> 死扛
```

---

## 3. 行为率指标

```text
behavior_rate = behavior_count / eligible_sample_count
```

`eligible_sample_count` 应按行为类型分别定义：
- 追高：有入场价格且可取 24h 高点的样本
- 割肉：有平仓上下文 K 线的样本
- 报复：有前后相邻交易记录的样本
- FOMO：存在分批加仓事件的样本
- 死扛：持仓时间与浮亏可计算样本

---

## 4. 热力等级建议

| 行为 | 低 | 中 | 高 |
|------|----|----|----|
| 追高率 | <15% | 15%-30% | >30% |
| 割肉率 | <20% | 20%-40% | >40% |
| 报复次数 | 0-1 | 2 | >=3 |
| FOMO率 | <10% | 10%-20% | >20% |
| 死扛率 | <8% | 8%-15% | >15% |

---

## 5. 洞察规则表

| 规则 | 触发条件 | 洞察 | 建议 |
|------|---------|------|------|
| 追高过多 | 追高率 >30% | “入场纪律不足” | “新增入场过滤：距 24h 高点需 >3%” |
| 报复交易 | 次数 >=3 | “亏后快速反手明显” | “亏损后禁开仓 30-60 分钟” |
| 高割肉 | 割肉率 >40% | “平仓过早” | “引入分批止损与确认窗口” |
| FOMO偏高 | FOMO率 >20% | “追涨加仓倾向明显” | “限制加仓条件与加仓比例” |
| 死扛偏高 | 死扛率 >15% | “亏损持仓拖延处理” | “设置硬止损与时间止损” |

---

## 6. 边界与误差说明

1. 行为识别是统计推断，不等同主观意图。
2. K 线粒度会影响“割肉”判定，建议至少 5m 级别。
3. 极端单边行情中，追高与趋势跟随可能重叠，需结合策略上下文解释。
4. 分批成交细节缺失时，FOMO 判定准确率下降。

---

## 7. 版本历史

| 版本 | 日期 | 变更 |
|------|------|------|
| 1.0.0 | 2026-04-12 | 初版：五类行为判定、热力评分、案例提取 |
