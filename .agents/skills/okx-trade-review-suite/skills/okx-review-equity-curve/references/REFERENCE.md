# okx-review-equity-curve · 算法与规则参考

本文件补充权益曲线重建、回撤识别与可视化规则的公式细节，供主 Skill 调整阈值与排障时使用。

---

## 1. 核心数据字段映射

### `account bills` 字段

| 字段 | 类型 | 说明 |
|------|------|------|
| `billId` | string | 流水唯一标识 |
| `type` | enum | 流水类型（交易、手续费、资金费、转账等） |
| `instId` | string | 品种 |
| `balChg` | decimal | 余额变化 |
| `bal` | decimal | 变更后余额 |
| `ccy` | string | 币种 |
| `ts` | timestamp ms | 发生时间 |

### `account balance` 字段

| 字段 | 类型 | 说明 |
|------|------|------|
| `totalEq` | decimal | 总权益 |
| `uTime` | timestamp ms | 更新时间 |
| `details[].ccy` | string | 各币种 |
| `details[].eq` | decimal | 分币种权益 |

### `account config` 字段

| 字段 | 类型 | 说明 |
|------|------|------|
| `acctLv` | enum | 账户模式 |
| `posMode` | enum | 持仓模式 |
| `autoLoan` | boolean | 自动借币标记 |

---

## 2. 权益重建算法

### 2.1 预处理

```text
输入：bills[]（时间窗内），balance（当前）
步骤：
1) 过滤空值 balChg/ts
2) 统一按 ts 升序
3) 根据参数决定是否剔除入金/出金/内部转账类 type
4) 若多币种，默认使用账户折算后的总权益口径；必要时转单币种模式
```

### 2.2 曲线计算

```text
equity_0 = 参数起点资金（默认窗口起始余额）
equity_t = equity_0 + Σ balChg_i  (i <= t)
```

若使用“交易级”采样，`equity_t` 每笔流水更新；日级/小时级按 bucket 聚合：

```text
bucket_balChg = Σ balChg within bucket
equity_bucket_k = equity_bucket_(k-1) + bucket_balChg
```

### 2.3 尾点校正

```text
error = |equity_last - balance.totalEq| / max(balance.totalEq, 1)
if error <= 0.5%: 直接通过
if 0.5% < error <= 2%: 输出黄线提示
if error > 2%: 标记估值偏差，降级为相对变化曲线
```

---

## 3. 回撤与恢复指标

### 3.1 峰值序列

```text
running_peak_t = max(running_peak_(t-1), equity_t)
```

### 3.2 最大回撤 MDD

```text
drawdown_t = (equity_t - running_peak_t) / running_peak_t
MDD = min(drawdown_t)
```

`MDD` 对应区间：
- `peak_time`: 触发该段回撤前的最近峰值时间
- `trough_time`: 最低点时间

### 3.3 MDD Duration / Underwater Days / Recovery

```text
MDD Duration = trough_time - peak_time
Underwater Days = Σ( equity_t < running_peak_t ) 的时间总长
Recovery Time = first_time(equity_t >= peak_equity_after_trough) - trough_time
```

若窗口结束仍未恢复：
- `Recovery Time = 未恢复`
- `Underwater Days` 持续累计到窗口终点

### 3.4 Calmar Ratio

```text
period_return = equity_end / equity_start - 1
annualized_return = (1 + period_return)^(annual_base / window_days) - 1
Calmar = annualized_return / |MDD|
```

默认 `annual_base = 365`，可切换 `252`。

### 3.5 Recovery Factor

```text
net_profit = equity_end - equity_start
Recovery Factor = net_profit / |peak_to_trough_loss|
```

其中：

```text
peak_to_trough_loss = peak_equity - trough_equity
```

---

## 4. ASCII 曲线绘制规则

### 4.1 8 档高度映射

字符集：`▁▂▃▄▅▆▇█`

```text
normalized = (x - minEq) / (maxEq - minEq + eps)
level = floor(normalized * 7)
char = charset[level]
```

### 4.2 回撤遮罩

- 在 `equity_t < running_peak_t` 的位置绘制 `░`
- 峰值点标记 `▲`，谷值点标记 `▼`
- 已恢复点再标一个 `▲`

### 4.3 宽度压缩

目标宽度 `W`（默认 20）：

```text
segment_size = ceil(N / W)
每段取最后一个采样点做代表值
```

---

## 5. 洞察规则表

| 规则 | 触发条件 | 洞察 | 建议 |
|------|---------|------|------|
| 高回撤 | `|MDD| > 20%` | “最大回撤超 20%，风险偏高” | “把单日风险预算降到 1.0%-1.5%” |
| 长水下 | `Underwater Days > 14` | “水下时间过长，恢复效率低” | “降仓到基准仓位 50%-70%” |
| 效率偏低 | `Calmar < 1` | “风险调整收益偏弱” | “先优化回撤再加杠杆” |
| 恢复慢 | `Recovery Time > MDD Duration` | “回撤修复慢于下跌” | “提高止损一致性，减少补亏交易” |
| 噪声过高 | 交易级曲线震荡率 > 阈值 | “曲线噪声大，频率过高” | “改小时/日级执行并设开仓上限” |

---

## 6. 边界与约束

1. 时间窗 >7 天通常需要 `--archive`。
2. API 结果可能分页，需按 `ts` 拼接后再排序。
3. 多币种权益折算受标记价格影响，短时误差属于正常现象。
4. 仅使用只读命令，不涉及下单、撤单、资金划转。

---

## 7. 版本历史

| 版本 | 日期 | 变更 |
|------|------|------|
| 1.0.0 | 2026-04-12 | 初版：权益重建、MDD、水下与恢复指标、ASCII 曲线 |
