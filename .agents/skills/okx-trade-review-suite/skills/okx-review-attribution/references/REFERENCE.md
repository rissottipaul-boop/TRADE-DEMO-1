# okx-review-attribution · 算法与规则参考

本文件给出 Skill 主体省略的复杂公式、阈值推导与边界说明。

---

## 1. 核心数据字段映射

### `account positions-history` 返回字段

| 字段 | 类型 | 说明 |
|------|------|------|
| `instId` | string | 品种，如 `BTC-USDT-SWAP` |
| `instType` | enum | `SPOT` / `SWAP` / `FUTURES` / `OPTION` |
| `direction` | enum | `long` / `short`（SPOT 永远 `long`） |
| `openAvgPx` | decimal | 开仓均价 |
| `closeAvgPx` | decimal | 平仓均价 |
| `realizedPnl` | decimal | **已实现盈亏（已扣手续费）** |
| `pnlRatio` | decimal | 收益率（相对保证金） |
| `lever` | decimal | 杠杆倍数 |
| `cTime` | timestamp ms | 平仓时间 |
| `oTime` | timestamp ms | 开仓时间 |
| `openMaxPos` | decimal | 历史最大持仓（张） |

### `account bills` 返回字段（辅助做手续费归因）

| 字段 | 类型 | 说明 |
|------|------|------|
| `billId` | string | 流水号 |
| `instId` | string | 品种 |
| `type` | enum | `2` 买卖 / `3` 结算 / `4` 资金费 / `8` 手续费 |
| `ccy` | string | 币种 |
| `balChg` | decimal | 余额变化 |
| `bal` | decimal | 账户余额 |
| `ts` | timestamp ms | 发生时间 |

---

## 2. 分桶算法

### 2.1 时段分桶

```
持仓时长 = cTime - oTime (毫秒)
if 持仓时长 < 6h → 日内 (intraday)
elif 持仓时长 < 24h → 隔夜 (overnight)
else → 超隔夜 (multi-day)

跨周末 = (oTime 或 cTime 落在 周六/周日) AND 持仓时长 ≥ 24h
```

### 2.2 杠杆分桶

```
lever_bucket = floor(lever)
  1x: 无杠杆（现货或 1 倍合约）
  2-3x: 低杠杆
  4-10x: 中杠杆
  11-20x: 高杠杆
  >20x: 极高杠杆
```

### 2.3 噪声折叠

```
threshold_pct = 用户参数（默认 0.5%）
if abs(品种 PnL / 总绝对 PnL) < threshold_pct → 归入"其他"
```

---

## 3. 指标公式

### 3.1 胜率 & 盈亏比

```
win_count   = sum(1 for t in trades if t.realizedPnl > 0)
loss_count  = sum(1 for t in trades if t.realizedPnl < 0)
win_rate    = win_count / (win_count + loss_count)

avg_win     = mean(t.realizedPnl for t in trades if t.realizedPnl > 0)
avg_loss    = mean(|t.realizedPnl| for t in trades if t.realizedPnl < 0)
profit_factor = avg_win / avg_loss     # 盈亏比（>1.5 良好）
```

### 3.2 期望值

```
expectancy = win_rate × avg_win - (1 - win_rate) × avg_loss
```

期望值 >0 才有正向边际；本 Skill 不单独展示，由 `okx-review-streak` 负责。

### 3.3 集中度（HHI）

用赫芬达尔指数衡量品种集中度：

```
share_i = |pnl_i| / sum(|pnl_all|)
HHI = sum(share_i ** 2) × 10000

0-1500: 分散
1500-2500: 适中
>2500: 高度集中 → 触发"集中度偏高"告警
```

---

## 4. 洞察规则表

| 规则 | 触发条件 | 洞察话术 | 建议话术 |
|------|---------|---------|---------|
| 集中度风险 | HHI > 2500 或单品种占比 > 50% | "{instId} 贡献 X%，集中度偏高" | "建议敞口上限设到总保证金 {N}%" |
| 反向操作低效 | 某方向胜率 <45% 且样本 ≥5 | "做{方向}胜率仅 X%" | "下周{方向}比例从 X% 降至 ≤{Y}%" |
| 时段劣势 | 某时段胜率 <45% 且样本 ≥5 | "{时段}胜率 X%，表现疲软" | "{时段}前平仓一半，观察胜率" |
| 黑洞品种 | 单品种亏损 > 总 PnL 20% | "{instId} 亏损 $X，属于黑洞" | "暂停交易 2 周或把仓位减半" |
| 盈亏比偏低 | profit_factor < 1.0 | "盈亏比 X，亏得多赚得少" | "提高止盈或降低止损额度" |
| 过度交易 | 每日均笔数 >20 | "日均 X 笔，交易频率偏高" | "设置每日开仓上限" |

---

## 5. 边界与约束

### 5.1 数据时间窗

- OKX API `positions-history` 默认窗口：**近 7 天**
- 加 `--archive`：**近 3 个月**
- 超 3 个月：**不可检索**，Skill 告知用户

### 5.2 限流

- 账户类接口：**10 次/2 秒**
- 分批拉取时每批间隔 250ms 以避免 429

### 5.3 字段缺失

| 缺失字段 | 处理 |
|---------|------|
| `realizedPnl` | 该笔跳过，计数减 1 |
| `direction` | 按 `pos > 0 ? long : short` 推断 |
| `lever` | 归入"未知杠杆"桶 |
| `oTime` | 持仓时长按 `cTime - 创建时间最早 bill` 近似 |

### 5.4 现货特殊处理

现货的 "方向" 永远是 long，"持仓时长" 按买入与卖出时间差计算：

```
对每个 SPOT instId：
  按 FIFO 匹配买入 bill 与卖出 bill
  持仓时长 = 卖出 ts - 对应买入 ts
  realizedPnl = (卖出价 - 买入价) × 数量 - 双边手续费
```

---

## 6. 条形图渲染规则

```
max_abs_contrib = max(|pnl_i|) over Top N
for each row:
  bar_len = round(|pnl_i| / max_abs_contrib * 15)
  bar = '█' * bar_len + '░' * (15 - bar_len)
```

- 盈利用 `█`
- 亏损用 `░` + `⚠️` 后缀
- 占比显示保留 1 位小数

---

## 7. 版本历史

| 版本 | 日期 | 变更 |
|------|------|------|
| 1.0.0 | 2026-04-12 | 初版发布 |
