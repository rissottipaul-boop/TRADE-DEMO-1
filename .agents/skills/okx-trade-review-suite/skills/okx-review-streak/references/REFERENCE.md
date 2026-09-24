# okx-review-streak · 算法与规则参考

本文件定义 streak 识别、期望值与凯利建议的计算口径，以及样本边界与解释约束。

---

## 1. 核心字段映射

### `account positions-history`

| 字段 | 类型 | 说明 |
|------|------|------|
| `instId` | string | 品种 |
| `realizedPnl` | decimal | 已实现盈亏 |
| `oTime` | timestamp ms | 开仓时间 |
| `cTime` | timestamp ms | 平仓时间 |
| `direction` | enum | long/short |
| `lever` | decimal | 杠杆 |
| `tradeId` | string | 成交标识（若有） |

---

## 2. W/L 序列定义

```text
if realizedPnl > 0 => W
if realizedPnl < 0 => L
if realizedPnl = 0 => N（默认忽略，不计 streak）
```

排序规则：按 `cTime` 升序；时间相同按 `tradeId` 次序。

去重键：`tradeId + cTime + instId + realizedPnl`。

---

## 3. 指标公式

### 3.1 胜率、盈亏比、期望值

```text
win_rate = win_count / (win_count + loss_count)
avg_win  = mean(pnl_i | pnl_i > 0)
avg_loss = mean(|pnl_i| | pnl_i < 0)
R        = avg_win / avg_loss
expectancy = win_rate * avg_win - (1 - win_rate) * avg_loss
```

### 3.2 当前 streak 与最长 streak

```text
scan sequence from left to right
if current_symbol == previous_symbol:
   run_len += 1
else:
   run_len = 1
track max_win_run / max_loss_run
current_streak = suffix run at end of sequence
```

### 3.3 连亏后胜率衰减

定义“连亏段后观察窗口” `k`（默认 5 笔）：

```text
post_loss_win_rate = mean( wins in next k trades after each L-run of len>=threshold )
decay = overall_win_rate - post_loss_win_rate
```

`decay > 0` 表示连亏后状态劣化。

### 3.4 凯利建议

```text
p = win_rate
q = 1 - p
b = R
kelly_raw = p - q / b
kelly_suggest = max(0, kelly_raw) * discount
```

默认 `discount = 0.5`。

---

## 4. 滚动胜率曲线

窗口长度 `w`（默认 20）：

```text
rolling_win_rate_t = sum(W in [t-w+1, t]) / w
```

ASCII 映射：`▁▂▃▄▅▆▇█`。

若样本不足 `w`：
- 不绘曲线，改为输出“样本不足滚动统计”。

---

## 5. 洞察规则表

| 规则 | 触发条件 | 洞察 | 建议 |
|------|---------|------|------|
| 连亏预警 | 最长连亏 `>= 5` | “可能进入情绪交易阶段” | “出现 L4 后自动降仓 50%” |
| 负期望 | `expectancy < 0` | “策略无正向边际” | “暂停新策略，先做小样本回测” |
| 胜率盈亏比失衡 | `50%<=win_rate<=55%` 且 `R<1.5` | “结构不达标” | “提高止盈或收窄止损，目标 R>=1.5” |
| 连亏后衰减 | `decay >= 8 pct` | “连亏后执行质量下降” | “设置 6-24h 冷静期” |
| 连胜过载 | 最长连胜 `>= 8` 且杠杆提升 | “过度自信风险” | “限制连续盈利后的加仓幅度” |

---

## 6. 边界与约束

1. 仅分析“已平仓”记录，未平仓不纳入 W/L。
2. `realizedPnl=0` 默认不计入 streak，可配置单独统计。
3. 样本数 < 最小样本量时，只输出描述性统计，不给仓位建议。
4. 不包含主观交易日志时，行为解释只基于数据推断。

---

## 7. 版本历史

| 版本 | 日期 | 变更 |
|------|------|------|
| 1.0.0 | 2026-04-12 | 初版：streak、期望值、凯利、衰减分析 |
