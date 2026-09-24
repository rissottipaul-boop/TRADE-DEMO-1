# 示例：永续合约套利机会扫描

## 用户输入
```
扫描套利机会
```

## Skill 处理流程

### 1. 解析指令
- 识别操作：`扫描套利机会`
- 识别类型：全市场套利扫描（未指定具体标的）
- 识别策略：包括资金费率套利、期现套利、跨期套利、跨交易所套利
- 识别优先级：高年化、低风险机会优先

### 2. 套利类型定义

本 Skill 扫描以下 4 类套利机会：

1. **资金费率套利**：
   - 原理：做多资金费率为负的合约，做空资金费率为正的合约
   - 风险：低（市场中性，赚取资金费率差）
   - 持有期：8小时（资金费率结算周期）

2. **期现套利**：
   - 原理：做多现货，做空永续合约（当基差为正时）
   - 风险：中（需对冲价格风险）
   - 持有期：数小时至数天（等待基差收敛）

3. **跨期套利**：
   - 原理：做多远月合约，做空近月合约（期限结构 contango）
   - 风险：中（期限结构变化风险）
   - 持有期：数周至数月

4. **跨交易所套利**：
   - 原理：同一合约在不同交易所价差套利
   - 风险：高（跨所转账风险、执行风险）
   - 持有期：数分钟至数小时

### 3. 数据采集阶段

#### 3.1 资金费率扫描
```bash
# 扫描所有永续合约资金费率
okx market funding-rate --instType SWAP --limit 50
okx market open-interest --instType SWAP --limit 50
okx market mark-price --instType SWAP --limit 50
```
返回摘要（前10个机会）：
- BTC-USDT-SWAP: -0.012% (空头支付)
- ETH-USDT-SWAP: +0.008% (多头支付)  
- SOL-USDT-SWAP: -0.025% (高负费率)
- XRP-USDT-SWAP: +0.005% (低正费率)

#### 3.2 基差（期现价差）扫描
```bash
# 计算主要币种基差
okx market tickers --instId BTC-USDT
okx market mark-price --instId BTC-USDT-SWAP
# 计算：(永续价 - 现货价) / 现货价
```
返回结果：
- BTC 基差: +0.15% (永续溢价)
- ETH 基差: -0.08% (永续折价)
- SOL 基差: +0.25% (较高溢价)

#### 3.3 期限结构扫描
```bash
# 查看不同到期日合约
okx market instruments --instType FUTURES --uly BTC-USD
okx market tickers --instType FUTURES --uly BTC-USD
```
返回结果：
- BTC-USD-240329: 51,200 (近月)
- BTC-USD-240628: 51,450 (远月)
- 期限溢价: +0.49% (contango结构)

#### 3.4 跨交易所价差扫描（外部数据）
```bash
# 查询其他交易所价格（模拟）
okx external binance --symbol BTCUSDT --price
okx external bybit --symbol BTCUSDT --price
okx external bitget --symbol BTCUSDT --price
```
返回结果（模拟）：
- OKX: 51,234.56
- Binance: 51,230.12 (-4.44点)
- Bybit: 51,240.50 (+5.94点)
- Bitget: 51,228.80 (-5.76点)

### 4. 套利机会评分与排序

#### 4.1 机会识别矩阵

| 机会类型 | 标的 | 参数 | 年化收益 | 风险等级 | 综合评分 |
|----------|------|------|----------|----------|----------|
| 资金费率 | SOL-USDT-SWAP | 费率-0.025% | 32.85% | 低 | 85 |
| 期现套利 | BTC 基差+0.15% | 做多现货+做空永续 | 18.25% | 中低 | 72 |
| 资金费率 | ETH-USDT-SWAP | 费率+0.008% | 10.51% | 低 | 68 |
| 跨期套利 | BTC 期限+0.49% | 做多远月+做空近月 | 24.36% | 中 | 65 |
| 跨交易所 | OKX-Binance BTC | 价差-4.44点 | 15.62% | 高 | 58 |

#### 4.2 评分维度（权重）
1. **年化收益率**（30%）：预期年化收益，调整后值
2. **夏普比率**（25%）：风险调整后收益
3. **资金占用**（20%）：所需保证金比例
4. **流动性**（15%）：合约深度，成交难度
5. **执行复杂度**（10%：对冲难度、跨平台等

### 5. 最优机会：SOL 资金费率套利深度分析

#### 5.1 机会详情
- **合约**：SOL-USDT-SWAP
- **当前资金费率**：-0.025%（每8小时）
- **年化计算**：-0.025% × 3 × 365 = -27.375%（支付方）
- **套利方向**：做多SOL-USDT-SWAP（收取资金费率）
- **对冲需求**：做空等值SOL现货或期权对冲Delta风险
- **持有周期**：8小时（资金费率结算周期）

#### 5.2 收益计算（保守估计）
- **投入资金**：$10,000
- **合约乘数**：1 SOL/张
- **持仓数量**：100张（名义价值 $15,000，1.5倍杠杆）
- **每8小时收益**：100张 × $15 × (-0.00025) = -$0.375（支付）
- **实际收益**：作为多头，收取 $0.375
- **日收益**：$0.375 × 3 = $1.125
- **年化收益**：($1.125 × 365) / $10,000 = 4.11%
- **调整后年化**：考虑复利、价格波动、手续费 = 3.2-3.8%

#### 5.3 风险分析
1. **价格风险**：SOL价格波动导致保证金变化（通过现货对冲消除）
2. **费率反转风险**：资金费率转为正值，从收取变为支付
3. **流动性风险**：大额平仓可能产生滑点
4. **系统风险**：交易所故障、API限制等

#### 5.4 对冲方案
- **完全对冲**：做空等值SOL现货（100% Delta中性）
- **部分对冲**：做空50%现货，保留部分方向性暴露
- **期权对冲**：买入看跌期权保护下行风险
- **动态对冲**：根据资金费率变化调整对冲比例

### 6. 三档套利方案

| 参数 | 保守型 | 稳健型 | 激进型 | 说明 |
|------|--------|--------|--------|------|
| **套利类型** | 资金费率套利 | 期现套利 | 跨期套利 | 风险递增 |
| **标的** | SOL-USDT-SWAP | BTC 基差套利 | BTC 期限套利 | |
| **年化收益** | 3.2-3.8% | 8-12% | 15-20% | 预期范围 |
| **最大回撤** | -1.5% | -5% | -12% | 历史回测 |
| **夏普比率** | 2.5-3.0 | 1.8-2.2 | 1.2-1.5 | 风险调整 |
| **资金占用** | 150%名义 | 200%名义 | 250%名义 | 保证金需求 |
| **持有周期** | 8小时 | 3-7天 | 2-4周 | 平均持仓 |
| **对冲比例** | 100% Delta | 80% Delta | 60% Delta | 风险暴露 |
| **最小规模** | $5,000 | $10,000 | $20,000 | 门槛金额 |
| **适合用户** | 低风险偏好 | 中等风险 | 专业套利者 | |

#### 6.1 保守型方案详情（推荐新手）
- **策略**：SOL-USDT-SWAP 资金费率套利
- **操作**：做多永续合约 + 做空等值现货
- **杠杆**：永续1.5x，现货1x（无杠杆）
- **预期收益**：年化3.5%，月化0.29%
- **风控**：每日检查资金费率，反转超过0.01%即平仓

#### 6.2 执行步骤
1. **步骤1**：做多SOL-USDT-SWAP 100张（$15,000名义）
2. **步骤2**：做空SOL-USDT现货 100 SOL（$15,000）
3. **步骤3**：每8小时收取资金费率
4. **步骤4**：监控费率变化，超出阈值平仓

### 7. JSON 格式输出

```json
{
  "scan_id": "ARBITRAGE_SCAN_20240115",
  "timestamp": "2024-01-15T16:30:00Z",
  "opportunities_found": 5,
  "top_opportunity": {
    "rank": 1,
    "type": "funding_rate",
    "instrument": "SOL-USDT-SWAP",
    "annualized_return": 0.038,
    "risk_level": "low",
    "score": 85,
    
    "parameters": {
      "current_rate": -0.00025,
      "rate_history_7d": [-0.00028, -0.00025, -0.00022, -0.00026, -0.00024, -0.00025, -0.00023],
      "average_rate": -0.00025,
      "rate_volatility": 0.00002,
      "next_funding_time": "2024-01-15T16:00:00Z",
      "funding_interval_hours": 8
    },
    
    "execution_plan": {
      "long_leg": {
        "instrument": "SOL-USDT-SWAP",
        "side": "buy",
        "size": 100,
        "leverage": 1.5,
        "margin_required": 10000,
        "order_type": "limit",
        "suggested_price": "market - 0.2%"
      },
      "short_leg": {
        "instrument": "SOL-USDT",
        "side": "sell",
        "size": 100,
        "leverage": 1,
        "margin_required": 15000,
        "order_type": "limit",
        "suggested_price": "market + 0.1%"
      },
      "hedge_ratio": 1.0,
      "delta_neutral": true,
      "rebalance_frequency": "8h"
    },
    
    "profit_calculation": {
      "per_funding": 0.375,
      "daily": 1.125,
      "weekly": 7.875,
      "monthly": 33.75,
      "annual": 410.625,
      "return_on_capital": 0.0411,
      "after_fees": 0.038,
      "compounded_annual": 0.039
    },
    
    "risk_metrics": {
      "var_95_1d": -75.50,
      "expected_shortfall_95": -112.25,
      "max_historical_drawdown": -1.42,
      "sharpe_ratio": 2.85,
      "sortino_ratio": 3.62,
      "calmar_ratio": 2.69
    }
  },
  
  "alternative_opportunities": [
    {
      "rank": 2,
      "type": "basis",
      "instrument": "BTC",
      "annualized_return": 0.1225,
      "risk_level": "medium_low",
      "score": 72
    },
    {
      "rank": 3,
      "type": "funding_rate", 
      "instrument": "ETH-USDT-SWAP",
      "annualized_return": 0.0851,
      "risk_level": "low",
      "score": 68
    }
  ],
  
  "market_conditions": {
    "total_swap_open_interest": "45.2B",
    "average_funding_rate": "0.006%",
    "positive_funding_count": 28,
    "negative_funding_count": 22,
    "basis_opportunities": 12,
    "calendar_spread_opportunities": 8,
    "cross_exchange_opportunities": 3
  },
  
  "risk_warnings": [
    "资金费率可能快速反转，需密切监控",
    "期现套利需考虑现货借贷成本",
    "跨交易所套利存在转账延迟风险",
    "所有套利策略在极端行情下可能失效"
  ]
}
```

### 8. 一键转执行（资金费率套利）

用户选择保守型方案后，自动生成对冲套利执行指令：

```
🚀 即将调用【漩涡执行器】执行套利策略：

策略：SOL 资金费率套利（保守型）
方向：做多永续 + 做空现货（Delta中性）
持仓：永续100张 + 现货100 SOL
杠杆：永续1.5x，现货1x
预期年化：3.8%
最大回撤：-1.5%

📋 执行命令序列：

步骤1：做多 SOL-USDT-SWAP
okx swap place --instId SOL-USDT-SWAP --side buy --posSide long --sz 100 --tdMode isolated --lever 1.5

步骤2：做空 SOL-USDT 现货
okx spot place --instId SOL-USDT --side sell --sz 100

步骤3：设置资金费率监控（每8小时）
okx algo place --instId SOL-USDT-SWAP --algoType funding_monitor --threshold 0.01%

步骤4：设置自动再平衡（Delta对冲）
okx algo place --algoType delta_rebalance --target 0 --threshold 0.05

是否确认执行此套利组合？（Y/N）
```

### 9. 套利策略监控与管理

#### 9.1 实时监控面板
```
🔄 SOL-USDT-SWAP 套利监控
时间：2024-01-15 16:45:00
运行时长：45分钟

📊 持仓状态：
- 永续多头：100张 @ $15.00
- 现货空头：100 SOL @ $15.00
- Delta暴露：+0.2 SOL（0.2%）
- 保证金率：245%

💰 收益统计：
- 资金费率收取：$0.375（已到账）
- 价格盈亏：-$12.50（现货-永续价差）
- 净收益：-$12.125
- 年化收益：-2.1%（临时负值）

⚠️ 风险指标：
- 资金费率：-0.025%（稳定）
- 基差：-0.08%（永续折价）
- 建议操作：无
```

#### 9.2 每日检查清单
1. **资金费率趋势**：是否持续为负
2. **对冲有效性**：Delta是否接近零
3. **保证金安全**：保证金率是否 > 150%
4. **流动性**：买卖价差是否正常
5. **机会成本**：是否有更好的套利机会

#### 9.3 退出条件
1. **止盈条件**：
   - 累计收益达到投入资金的1%
   - 资金费率转正超过0.01%
   - 发现年化5%以上的更好机会

2. **止损条件**：
   - 单日亏损超过0.5%
   - Delta暴露超过名义价值的5%
   - 市场流动性枯竭（价差>0.5%）

3. **强制平仓**：
   - 保证金率 < 120%
   - 交易所系统风险警告
   - 极端行情（24h波动>20%）

### 10. 高级套利策略

#### 10.1 多腿套利组合
```bash
# 三重套利：资金费率 + 基差 + 跨期
okx strategy arbitrage create \
  --type multi_leg \
  --legs "SOL-USDT-SWAP long, SOL-USDT short, SOL-USD-240329 short" \
  --target_apr 0.15 \
  --max_drawdown 0.08
```

#### 10.2 统计套利
```bash
# 基于历史价差统计的套利
okx strategy arbitrage create \
  --type statistical \
  --pair "BTC-USDT-SWAP,ETH-USDT-SWAP" \
  --lookback_days 90 \
  --zscore_threshold 2.0 \
  --mean_reversion_hours 24
```

#### 10.3 跨交易所三角套利
```bash
# BTC/USDT -> ETH/BTC -> ETH/USDT 三角套利
okx strategy arbitrage create \
  --type triangular \
  --exchanges "OKX,Binance,Bybit" \
  --paths "BTC/USDT,ETH/BTC,ETH/USDT" \
  --min_spread 0.003
```

## 测试命令（模拟模式）
```bash
# 模拟套利机会扫描
okx strategy arbitrage scan --demo

# 模拟套利回测
okx strategy arbitrage backtest --type funding_rate --instrument SOL-USDT-SWAP --period 30d --demo
```

## 错误处理场景

### 场景1：套利空间过小
```
⚠️ 套利空间不足！
最优机会：ETH 资金费率套利
年化收益：2.1%（<3% 阈值）
手续费影响：0.4%（双边）
净收益：1.7%（过低）

建议：
1. 提高机会阈值至年化4%
2. 等待更好机会出现
3. 考虑其他策略类型
```

### 场景2：对冲执行失败
```
❌ 对冲执行失败！
计划：做多永续 + 做空现货
已执行：永续多头 100张
失败：现货空头（余额不足）

应急方案：
1. 立即平仓永续多头
2. 改用期权对冲（买入看跌）
3. 降低仓位至50张
请选择应急方案 (1/2/3)：
```

### 场景3：费率快速反转
```
🚨 资金费率快速反转！
原费率：-0.025%（收取）
当前费率：+0.015%（支付）
变化：+0.040%（8小时内）

建议操作：
1. 立即平仓所有套利仓位
2. 反转方向（做空永续+做多现货）
3. 观望等待费率稳定
请选择操作 (1/2/3)：
```