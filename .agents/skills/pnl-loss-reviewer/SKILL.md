---
name: pnl-loss-reviewer
description: "盈亏复盘图表版 Skill。当用户说'复盘'、'亏损分析'、'盈亏报告'、'为什么亏了'、'交易总结'、'回顾交易'、'盈亏图表'、'复盘报告'时使用此技能。自动拉取历史交易记录，分析亏损原因，生成可视化图表和复盘报告。依赖 okx-cex-market、okx-cex-trade、okx-cex-portfolio 三个 Skill。"
---

# 盈亏复盘大师 Pro（图表版）

自动拉取 OKX 历史交易记录，统计盈亏数据，深度分析亏损原因，生成 6 张可视化图表 + 结构化复盘报告。帮你看清亏在哪、为什么亏、怎么改。支持现货与合约。

## 依赖 Skills（必须先安装）

- okx-cex-market — 获取历史价格和技术指标，用于回溯行情环境
- okx-cex-trade — 获取历史订单和成交明细
- okx-cex-portfolio — 获取账户账单、已平仓位、余额变化

## 参数说明

| 参数 | 类型 | 必填 | 默认值 | 说明 |
|------|------|------|--------|------|
| instId | string | 否 | 全部 | 指定交易对复盘，如 BTC-USDT 或 BTC-USDT-SWAP，留空则分析全部 |
| days | integer | 否 | 7 | 复盘天数范围：7 / 14 / 30 / 90 |
| instType | string | 否 | 全部 | 筛选类型：SPOT（现货）/ SWAP（永续）/ FUTURES（交割）/ 全部 |
| profile | string | 否 | demo | 实盘 live / 模拟盘 demo |
| focus | string | 否 | loss | 分析重点：loss（只看亏损）/ all（盈亏都分析）/ win（只看盈利） |
| chart | string | 否 | all | 图表输出：all（全部 6 张）/ summary（仅总览）/ none（纯文字） |

## 执行流程

### Phase 1：参数确认

1. 用户触发技能后，确认复盘范围（days）和账户类型（profile）。
2. 如果用户未指定 profile，默认使用 demo 模拟盘。
3. 确认分析重点：默认聚焦亏损交易。
4. 向用户确认参数后开始拉取数据。

### Phase 2：数据采集

Step 1：拉取已平仓记录（合约）

```
okx account positions-history --instType SWAP --profile {profile}
okx account positions-history --instId BTC-USDT-SWAP --limit 50 --profile {profile}
okx account positions-history --instType FUTURES --profile {profile}
```

从返回值中提取每笔仓位的关键字段：instId、direction、openAvgPx、closeAvgPx、realizedPnl、uTime。

Step 2：拉取历史订单（现货 + 合约）

```
okx spot orders --history --profile {profile}
okx spot orders --instId BTC-USDT --history --profile {profile}
okx swap orders --history --profile {profile}
okx spot fills --instId {instId} --profile {profile}
okx swap fills --instId {instId} --profile {profile}
```

Step 3：拉取账单流水

```
okx account bills --profile {profile}
okx account bills --instType SWAP --profile {profile}
okx account bills --archive --profile {profile}
```

Step 4：拉取手续费率

```
okx account fees --instType SPOT --profile {profile}
okx account fees --instType SWAP --profile {profile}
```

Step 5：获取当前账户状态

```
okx account balance USDT --profile {profile}
okx account positions --instType SWAP --profile {profile}
```

### Phase 3：数据统计

3.1 总体统计

- 总交易笔数、盈利笔数、亏损笔数
- 胜率 = 盈利笔数 / 总笔数 x 100%
- 总盈利金额、总亏损金额、净盈亏
- 盈亏比 = 平均盈利金额 / 平均亏损金额
- 最大单笔盈利、最大单笔亏损
- 累计手续费支出、累计资金费率支出（合约）

3.2 按交易对统计

- 每个 instId 的：盈亏总额、交易次数、胜率
- 找出亏损最多的交易对 Top 3
- 找出盈利最多的交易对 Top 3

3.3 按时间维度统计

- 按日统计盈亏曲线
- 找出亏损集中的日期
- 分析是否存在某个时间段连续亏损

### Phase 4：亏损原因分析

对每笔亏损交易，回溯开仓时的行情环境，判断亏损原因归类。

Step 1：获取开仓时的行情数据

```
okx market indicator rsi {instId} --bar 1H --params 14
okx market ticker {instId}
```

Step 2：亏损原因归类（可多选）

| 原因编号 | 亏损类型 | 判断依据 |
|---------|---------|---------|
| L1 | 追涨杀跌 | 开仓价在近期高点/低点附近，RSI 已超买/超卖 |
| L2 | 逆势交易 | 做多时价格处于下降趋势，做空时价格处于上升趋势 |
| L3 | 止损过宽 | 亏损幅度大于10%，未及时止损 |
| L4 | 频繁交易 | 同一交易对在短时间内多次开平仓，手续费占比高 |
| L5 | 仓位过重 | 单笔亏损占总资金比例大于10% |
| L6 | 手续费消耗 | 总手续费占总亏损的 20% 以上 |
| L7 | 资金费率亏损 | 长时间持仓，资金费率累计亏损显著 |
| L8 | 强制平仓 | 账单中出现强平记录 |
| L9 | 情绪化交易 | 亏损后立即加仓或反向开仓（复仇交易） |
| L10 | 时机不佳 | 在重大行情波动前开仓，被插针止损 |

Step 3：统计每种亏损原因出现的次数和占比，找出排名前 3 的亏损原因。

### Phase 5：生成可视化图表

基于 Phase 3 和 Phase 4 的统计数据，生成一个单文件 HTML 图表报告。使用 ECharts 渲染，深色主题（背景 #0D1117，文字 #C9D1D9），支持交互。

6 张图表：

1. 累计盈亏曲线（折线图）：X轴日期，Y轴累计盈亏USDT，标注最高点和最低点，零轴虚线
2. 交易对盈亏排名（横向柱状图）：盈利绿色，亏损红色，按金额排列
3. 亏损原因分布（环形饼图）：中心显示总亏损笔数，不同颜色区分原因
4. 每日盈亏热力图（日历热力图）：深绿到深红色阶，悬停显示日期和金额
5. 胜率仪表盘 + 核心指标卡片：Gauge 显示胜率，卡片显示净盈亏、盈亏比等
6. 持仓时间 vs 盈亏散点图：X轴持仓时间，Y轴盈亏，点大小表示仓位

输出为单个 HTML 文件，用户可直接在浏览器中打开。

### Phase 6：生成文字复盘报告

在图表下方嵌入结构化文字报告，包含：

- 总体表现（笔数、胜率、净盈亏、盈亏比、最大盈亏、手续费、资金费率）
- 亏损交易对 Top 3
- 亏损原因分析（前 3 名原因 + 典型案例）
- 典型亏损交易复盘（开平仓价、盈亏、原因、建议）
- 改进建议（针对主要亏损原因的具体操作建议）

## 使用示例

- "帮我复盘一下最近 7 天的交易"
- "分析一下我为什么亏了"
- "看看我 BTC-USDT-SWAP 的合约盈亏"
- "最近 30 天交易总结，生成图表"
- "模拟盘复盘，只看亏损交易"
- "我的交易胜率是多少"
- "哪个币亏最多，给我看图"
- "生成盈亏复盘报告"

## 风险提示

1. 默认使用模拟盘（demo），查看实盘需用户明确指定 profile=live。
2. 复盘分析基于历史数据回溯，亏损原因判断为 AI 推测，仅供参考。
3. 部分早期交易记录可能因 API 限制无法获取（通常最多 3 个月）。
4. 本 Skill 仅供学习研究，不构成投资建议。
