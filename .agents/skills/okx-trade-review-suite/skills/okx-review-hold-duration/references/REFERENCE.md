# okx-review-hold-duration · 算法与规则参考

本文件定义持仓时长分桶、相关指标与推荐区间评分规则。

---

## 1. 数据字段映射

### `account positions-history`

| 字段 | 说明 |
|------|------|
| `oTime` | 开仓时间 |
| `cTime` | 平仓时间 |
| `realizedPnl` | 已实现盈亏 |
| `direction` | long/short |
| `instId` | 品种 |
| `openAvgPx` | 开仓价 |
| `closeAvgPx` | 平仓价 |

---

## 2. 持仓时长计算

```text
hold_ms = cTime - oTime
hold_hours = hold_ms / 3_600_000
```

异常处理：
- `hold_ms <= 0`：剔除
- `oTime/cTime` 缺失：剔除并计入缺失率

---

## 3. 分桶规则

默认边界：
- `<1h`
- `1-6h`
- `6-24h`
- `1-3d`
- `3-7d`
- `>7d`

自定义边界需满足：
1. 单调递增
2. 桶间不重叠
3. 全覆盖正时长

---

## 4. 每桶指标公式

设桶 `b` 的交易集合为 `T_b`：

```text
count_b = |T_b|
win_rate_b = count(pnl>0 in T_b) / count_b
avg_pnl_b = mean(pnl in T_b)
avg_win_b = mean(pnl>0)
avg_loss_b = mean(|pnl| where pnl<0)
R_b = avg_win_b / avg_loss_b
```

---

## 5. 拿不住盈利指数

```text
avg_win_duration = mean(hold_hours where pnl>0)
avg_loss_duration = mean(hold_hours where pnl<0)
hold_bias_index = avg_loss_duration / max(avg_win_duration, eps)
```

解释：
- `<1.0`：盈利仓位持有更久（通常更优）
- `1.0-1.5`：中性偏弱
- `>1.5`：明显“亏损拖延”

---

## 6. 最佳时长区间评分

默认混合评分：

```text
score_b = 0.45 * norm(win_rate_b)
        + 0.45 * norm(avg_pnl_b)
        + 0.10 * norm(log(count_b+1))
```

`Top score` 对应“最佳时长区间”，并输出置信度标签：
- 样本 >=20：高
- 10-19：中
- <10：低

---

## 7. 洞察规则表

| 规则 | 条件 | 洞察 | 建议 |
|------|------|------|------|
| 拿不住盈利 | 指数 >1.5 | “亏损仓位持有过久” | “设置时间止损 + 固定复盘点” |
| 高频拖后腿 | `<1h` 桶胜率 <40% | “短线质量偏低” | “减少日内频次，转中短持有” |
| 长持有效 | `>7d` 桶胜率高且样本达标 | “长持有潜在优势” | “逐步提高长持策略权重” |
| 多空差异 | long/short 最优桶差异显著 | “方向执行节奏不一致” | “分方向制定持仓时长纪律” |
| 样本不足 | 任一关键桶样本<5 | “统计不稳定” | “延长窗口后再决策” |

---

## 8. 边界与注意事项

1. 仅基于已平仓数据，未平仓不参与统计。
2. 极端行情中，时长效果可能被单次大波动掩盖。
3. 桶内均值易受异常值影响，建议开启分位截断。
4. 本 Skill 不提供交易指令，只做复盘建议。

---

## 9. 版本历史

| 版本 | 日期 | 变更 |
|------|------|------|
| 1.0.0 | 2026-04-12 | 初版：默认六桶、时长指数、最优区间评分 |
