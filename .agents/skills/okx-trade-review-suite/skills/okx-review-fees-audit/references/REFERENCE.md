# okx-review-fees-audit · 算法与规则参考

本文件补充手续费口径、maker/taker 结构与节省空间估算公式。

---

## 1. 数据字段映射

### `account bills --type 8`

| 字段 | 说明 |
|------|------|
| `instId` | 品种 |
| `balChg` | 费用变动（通常为负） |
| `ts` | 时间 |
| `subType` | 费用子类型（若有） |
| `ccy` | 计费币种 |

### `account fees`

| 字段 | 说明 |
|------|------|
| `maker` | maker 费率 |
| `taker` | taker 费率 |
| `instType` | 账户品类 |
| `level` | 费率等级 |

### `account positions-history`

| 字段 | 说明 |
|------|------|
| `realizedPnl` | 已实现盈亏 |
| `instId` | 品种 |
| `cTime` | 平仓时间 |

---

## 2. 指标定义

### 2.1 毛利润 / 净利润 / 总费用

```text
gross_pnl = Σ realizedPnl
fee_total = Σ |balChg_fee_type8|
if include_funding = true:
  fee_total += Σ |balChg_type4|
net_pnl = gross_pnl - fee_total
```

### 2.2 费用占毛利润比

```text
fee_ratio = fee_total / max(|gross_pnl|, eps)
```

若 `gross_pnl` 接近 0，输出“比例不稳定”提示。

### 2.3 平均每笔费用

```text
avg_fee_per_trade = fee_total / trade_count
```

### 2.4 maker / taker 笔数比

当账单含成交属性时：

```text
maker_count = count(trade where liquidity == maker)
taker_count = count(trade where liquidity == taker)
maker_ratio = maker_count / (maker_count + taker_count)
```

若无成交属性，则基于成交明细近似映射并标注“估算”。

### 2.5 费率使用比

```text
maker_rate_used = weighted_avg(maker_rate, maker_notional)
taker_rate_used = weighted_avg(taker_rate, taker_notional)
```

---

## 3. 全 maker 节省估算

设：
- `N_taker`：taker 成交名义金额总和
- `r_taker`：taker 费率
- `r_maker`：maker 费率

```text
saving_if_all_maker = N_taker * (r_taker - r_maker)
```

若 `r_taker <= r_maker`，节省值置 0 并提示“当前费档无 maker 优势”。

---

## 4. 高费用品种识别

```text
fee_by_inst = Σ fee where instId == i
share_i = fee_by_inst / fee_total
```

按 `fee_by_inst` 降序取 Top N，并给出：
- 费用金额
- 费用占比
- 对净利润影响

---

## 5. 洞察规则表

| 规则 | 触发条件 | 洞察 | 建议 |
|------|---------|------|------|
| 过度交易 | `fee_ratio > 5%` | “交易成本侵蚀利润明显” | “减少低质量交易频次” |
| maker不足 | `maker_ratio < 50%` | “taker 占比过高” | “提高限价成交比例” |
| 品种漏损 | 单品种 `fee_share > 20%` | “单品种成本集中” | “该品种降频或改执行模板” |
| 单笔过高 | `avg_fee_per_trade > threshold` | “单次执行成本偏高” | “拆单或换时段成交” |
| 费档落后 | taker/maker rate 高于同级均值 | “费率档位不优” | “评估提升费档或转成交方式” |

---

## 6. 边界与注意事项

1. 本 Skill 估算“全 maker 节省”为理论上限，不含未成交机会成本。
2. 多币种费用需统一折算币种后再汇总。
3. 资金费率具有策略属性，建议与手续费分列展示。
4. 不做任何下单或撤单行为。

---

## 7. 版本历史

| 版本 | 日期 | 变更 |
|------|------|------|
| 1.0.0 | 2026-04-12 | 初版：费用瀑布、maker/taker、节省估算 |
