# okx-review-benchmark · 算法与规则参考

本文件定义基准组合构造、风险调整收益指标、alpha 统计与解释边界。

---

## 1. 数据字段映射

### `account positions-history`

| 字段 | 说明 |
|------|------|
| `realizedPnl` | 实际已实现盈亏 |
| `cTime` | 平仓时间 |
| `instId` | 品种 |
| `direction` | 方向 |

### `account bills`

| 字段 | 说明 |
|------|------|
| `type` | 费用/资金费/交易类流水 |
| `balChg` | 余额变化 |
| `ts` | 时间 |

### `market candles`

| 字段 | 说明 |
|------|------|
| `ts` | K 线时间 |
| `o/h/l/c` | 开高低收 |
| `vol` | 成交量（可选） |

---

## 2. 基准构造规则

### 2.1 Buy&Hold

```text
BH return = close_end / close_start - 1
```

支持 `BTC` 与 `ETH`。

### 2.2 SMA(20/60)

```text
if close_t >= SMA_n_t => 持有标的
else => 空仓（现金）
strategy_return_t = signal_(t-1) * asset_return_t
```

默认不做融券、不加杠杆。

### 2.3 DCA-BTC

```text
固定周期（每日/每周）投入固定金额 A
持仓份额 += A / close_t
期末价值 = 持仓份额 * close_end
DCA return = 期末价值 / 累计投入 - 1
```

---

## 3. 实际收益口径

```text
active_pnl = Σ realizedPnl + Σ related_balChg(type in fee/funding etc)
active_return = active_pnl / initial_equity
```

若 `initial_equity` 缺失，可退化为“净变化占比近似值”，并标注口径。

---

## 4. 风险调整指标

日收益序列：`r_t`

### 4.1 年化收益

```text
annualized_return = (1 + cumulative_return)^(annual_base / n_days) - 1
```

### 4.2 Sharpe

```text
sharpe = (mean(r_t) - rf_daily) / std(r_t) * sqrt(annual_base)
```

### 4.3 Sortino

```text
downside_std = std(min(r_t - rf_daily, 0))
sortino = (mean(r_t) - rf_daily) / downside_std * sqrt(annual_base)
```

### 4.4 Alpha / Beta

以选定基准收益 `rb_t`：

```text
beta = cov(r_t, rb_t) / var(rb_t)
alpha_daily = mean(r_t - rf_daily) - beta * mean(rb_t - rf_daily)
alpha_annual = alpha_daily * annual_base
```

### 4.5 Information Ratio

```text
active_diff_t = r_t - rb_t
IR = mean(active_diff_t) / std(active_diff_t) * sqrt(annual_base)
```

---

## 5. 胜过基准概率

滚动窗口 `w`（默认 14 天）：

```text
p_win = count( cum_return_active_w > cum_return_benchmark_w ) / total_windows
```

用百分比输出（0%-100%）。

---

## 6. 洞察规则表

| 规则 | 条件 | 洞察 | 建议 |
|------|------|------|------|
| 主动失效 | `alpha<0` 且 `Sharpe<0.5` | “主动交易未体现边际” | “提高被动仓位到 60%-80%” |
| 过度暴露 | `Beta>1.5` | “风险暴露过高” | “压缩高波动品种仓位” |
| 有效超额 | `IR>0.5` | “超额收益具备稳定性” | “保留该信号并扩大样本观察” |
| 回撤不对称 | 主动收益略高但 Sortino 更低 | “下行风险控制不足” | “先降波动再追收益” |
| 被动更优 | BH/DCA 双双高于主动 | “简单策略胜过主动” | “暂时转被动，降低交易频率” |

---

## 7. 边界与约束

1. 基准仅反映历史同窗，不保证未来继续有效。
2. SMA 基准未包含冲击成本，真实可执行收益通常更低。
3. 若市场极端跳空，Beta/IR 稳定性下降，应延长窗口验证。
4. 不进行任何交易操作，仅输出复盘建议。

---

## 8. 版本历史

| 版本 | 日期 | 变更 |
|------|------|------|
| 1.0.0 | 2026-04-12 | 初版：BH/SMA/DCA 对比、Alpha/Beta/IR、劝退逻辑 |
