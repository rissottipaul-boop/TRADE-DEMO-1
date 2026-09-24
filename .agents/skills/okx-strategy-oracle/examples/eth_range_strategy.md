# 示例：ETH 震荡网格策略生成

## 用户输入
```
网格策略 ETH 震荡区间
```

## Skill 处理流程

### 1. 解析指令
- 识别标的：`ETH`（默认 ETH-USDT 现货）
- 识别策略类型：`网格策略`
- 识别市场状态：`震荡区间`（需自动识别）
- 识别目标：生成适合震荡市情的网格交易参数

### 2. 震荡市识别与验证

#### 2.1 趋势强度分析
```bash
# 计算趋势指标
okx market indicator --instId ETH-USDT --indicator ADX --period 14
okx market indicator --instId ETH-USDT --indicator EMA --periods 20,50
okx market candles --instId ETH-USDT --bar 4h --limit 50
```
返回结果：
- ADX(14): 18.5（<20，趋势弱，适合震荡策略）
- EMA20: 2,750，EMA50: 2,720（均线粘合）
- 价格在 2,650-2,850 区间波动 30天

#### 2.2 波动率分析
```bash
# 计算波动率指标
okx market indicator --instId ETH-USDT --indicator ATR --period 14
okx market indicator --instId ETH-USDT --indicator Bollinger --period 20
okx market indicator --instId ETH-USDT --indicator RSI --period 14
```
返回结果：
- ATR(14): 42.5（日波动率 1.52%）
- 布林带宽度：85点（3.1%，适中）
- RSI(14): 48.5（中性，无超买超卖）

#### 2.3 区间识别
```bash
# 识别支撑阻力位
okx market indicator --instId ETH-USDT --indicator PivotPoints --period daily
okx market indicator --instId ETH-USDT --indicator Fibonacci --period 30
```
返回结果：
- 枢轴点：支撑 2,680，阻力 2,820
- 斐波那契：38.2%回撤 2,720，61.8%回撤 2,780
- 近期高低点：低点 2,650（12月15日），高点 2,850（1月10日）

### 3. 6维评分计算（侧重震荡环境）

| 维度 | 原始数据 | 评分(0-100) | 权重 | 贡献分 | 说明 |
|------|----------|-------------|------|--------|------|
| **价格** | 均线粘合，区间震荡 | 75 | 25% | 18.75 | ADX<20，价格在区间内 |
| **量价** | 成交量平稳，无放量突破 | 70 | 20% | 14.00 | 量价配合一般 |
| **衍生品** | 资金费率接近0，OI稳定 | 68 | 20% | 13.60 | 多空均衡 |
| **链上** | 交易所余额稳定 | 65 | 15% | 9.75 | 无大额流入流出 |
| **宏观** | 宏观消息平静 | 60 | 10% | 6.00 | 无重大事件 |
| **情绪** | 恐惧贪婪指数52，中性 | 55 | 10% | 5.50 | 市场情绪平稳 |
| **总分** | **65.60** | **100%** | **67.60** | **震荡** | |

**市场状态判断**：总分 65.60，处于"震荡市"区间（55-75）。
**网格策略适配度**：82%（高，基于历史回测）。

### 4. 网格参数优化

#### 4.1 区间自动识别
基于技术分析，识别最优震荡区间：
- **保守区间**：2,680-2,820（枢轴点支撑阻力）
- **稳健区间**：2,650-2,850（近期高低点）
- **激进区间**：2,620-2,880（扩展10%缓冲）

当前价格：2,750（位于区间中部）

#### 4.2 网格数量优化
基于波动率(ATR)和手续费计算最优格数：
- ATR=42.5，日波动率1.52%
- 每格最小盈利需覆盖手续费（双边0.1%）
- 计算：区间高度 ÷ (ATR × 2) = 最优格数范围

#### 4.3 三档网格方案

| 参数 | 保守型 | 稳健型 | 激进型 | 计算公式 |
|------|--------|--------|--------|----------|
| **区间范围** | 2,680-2,820 | 2,650-2,850 | 2,620-2,880 | 技术位±缓冲 |
| **区间高度** | 140点 (5.2%) | 200点 (7.5%) | 260点 (9.9%) | 上限-下限 |
| **网格数量** | 7格 | 10格 | 13格 | 高度÷(ATR×2) |
| **格间距** | 20点 (0.73%) | 20点 (0.73%) | 20点 (0.73%) | 固定间距 |
| **投入资金** | 账户10% | 账户15% | 账户20% | 风险预算 |
| **每格资金** | 1.43% | 1.50% | 1.54% | 投入÷格数 |
| **预计年化** | 12-18% | 15-22% | 18-25% | 历史回测 |
| **最大回撤** | 140点 (5.2%) | 200点 (7.5%) | 260点 (9.9%) | 区间高度 |
| **夏普比率** | 1.8-2.2 | 1.5-1.9 | 1.2-1.6 | 风险调整收益 |
| **资金效率** | 45% | 50% | 55% | 持仓比例 |

#### 4.4 保守型方案详情（推荐）
- **区间**：2,680-2,820（技术位支撑阻力）
- **格数**：7格（间距20点/0.73%）
- **投入**：$2,000（账户$20,000的10%）
- **每格**：$285.71
- **初始挂单**：3买单 + 3卖单（当前价在中部）
- **触发价**：2,750（当前价）

#### 4.5 风险控制参数
- **止损条件**：价格突破区间±2%持续2小时
- **止盈条件**：总盈利达到投入的5%
- **再平衡**：每72小时检查区间有效性
- **手续费优化**：使用Maker单（限价单）降低费用

### 5. JSON 格式输出

```json
{
  "strategy_id": "ETH_GRID_20240115_RANGE",
  "timestamp": "2024-01-15T16:00:00Z",
  "instrument": "ETH-USDT",
  "instrument_type": "spot",
  "strategy_type": "grid",
  "market_regime": "ranging",
  "adx_score": 18.5,
  "grid_suitability": 0.82,
  "current_price": 2750.00,
  
  "identified_range": {
    "recent_low": 2650.00,
    "recent_high": 2850.00,
    "pivot_support": 2680.00,
    "pivot_resistance": 2820.00,
    "atr": 42.5,
    "daily_volatility": 0.0152
  },
  
  "grid_parameters": {
    "conservative": {
      "lower_bound": 2680.00,
      "upper_bound": 2820.00,
      "range_height": 140.00,
      "range_percent": 0.0522,
      "grid_count": 7,
      "grid_spacing": 20.00,
      "grid_spacing_percent": 0.0073,
      "investment_percent": 0.10,
      "investment_per_grid": 0.0143,
      "expected_apr": [0.12, 0.18],
      "max_drawdown": -0.0522,
      "sharpe_ratio": [1.8, 2.2],
      "capital_efficiency": 0.45
    },
    "moderate": {
      "lower_bound": 2650.00,
      "upper_bound": 2850.00,
      "range_height": 200.00,
      "range_percent": 0.0755,
      "grid_count": 10,
      "grid_spacing": 20.00,
      "grid_spacing_percent": 0.0073,
      "investment_percent": 0.15,
      "investment_per_grid": 0.0150,
      "expected_apr": [0.15, 0.22],
      "max_drawdown": -0.0755,
      "sharpe_ratio": [1.5, 1.9],
      "capital_efficiency": 0.50
    },
    "aggressive": {
      "lower_bound": 2620.00,
      "upper_bound": 2880.00,
      "range_height": 260.00,
      "range_percent": 0.0992,
      "grid_count": 13,
      "grid_spacing": 20.00,
      "grid_spacing_percent": 0.0073,
      "investment_percent": 0.20,
      "investment_per_grid": 0.0154,
      "expected_apr": [0.18, 0.25],
      "max_drawdown": -0.0992,
      "sharpe_ratio": [1.2, 1.6],
      "capital_efficiency": 0.55
    }
  },
  
  "risk_controls": {
    "stop_loss_condition": "price_outside_range_percent > 0.02 for 2h",
    "take_profit_condition": "total_profit_percent > 0.05",
    "rebalance_frequency_hours": 72,
    "order_type": "limit",
    "fee_optimization": "maker_only",
    "position_sizing_method": "kelly_fraction"
  },
  
  "historical_backtest": {
    "period": "2023-01-01 to 2023-12-31",
    "similar_conditions_count": 42,
    "average_apr": 0.168,
    "win_rate": 0.76,
    "max_consecutive_losses": 3,
    "best_month": "+3.2%",
    "worst_month": "-2.1%"
  },
  
  "execution_plan": {
    "skill": "okx-execution-vortex",
    "bot_skill": "okx-cex-bot",
    "commands": {
      "conservative": "okx bot grid create --instId ETH-USDT --lowerPx 2680 --upperPx 2820 --gridNum 7 --investment 2000 --lever 1 --runType arithmetic",
      "moderate": "okx bot grid create --instId ETH-USDT --lowerPx 2650 --upperPx 2850 --gridNum 10 --investment 3000 --lever 1 --runType arithmetic",
      "aggressive": "okx bot grid create --instId ETH-USDT --lowerPx 2620 --upperPx 2880 --gridNum 13 --investment 4000 --lever 1 --runType arithmetic"
    }
  }
}
```

### 6. 网格策略详细说明

#### 6.1 工作原理
1. **初始建仓**：在区间内等间距挂买单和卖单
2. **成交循环**：买单成交后，在更高价格挂卖单；卖单成交后，在更低价格挂买单
3. **网格盈利**：每完成一次低买高卖循环，盈利 = 格间距 - 手续费
4. **区间突破处理**：价格突破区间时暂停，等待回归或重新评估

#### 6.2 资金分配示例（保守型）
投入 $2,000，7格，每格 $285.71：
- **持仓ETH**：$1,000（50%，约0.364 ETH）
- **预留USDT**：$1,000（50%，用于接货）
- **初始挂单**：
  - 买单：2,680/2,700/2,720（3单）
  - 卖单：2,760/2,780/2,800（3单）
  - 当前价2,750附近不挂单

#### 6.3 预期收益计算
- 每格间距：20点
- 每格盈利：20 × 持仓量 = 20 × 0.052 = 1.04 USDT
- 手续费：双边0.1% = 0.57 USDT
- 净盈利：0.47 USDT/格
- 日均成交：2-3格（震荡市）
- 日盈利：0.94-1.41 USDT
- 年化收益：($1.18 × 365) / $2000 = 21.5%

#### 6.4 风险因素
1. **单边行情风险**：持续上涨会卖飞，持续下跌会套牢
2. **区间选择风险**：价格长期不在区间内则无收益
3. **资金效率**：约50%资金闲置
4. **手续费磨损**：高频交易累积手续费

### 7. 一键转执行（调用网格bot）

用户选择稳健型方案后，自动生成执行指令：

```
🚀 即将调用【网格交易bot】创建以下策略：

标的：ETH-USDT
区间：2,650 - 2,850（200点，7.5%）
格数：10格（间距20点/0.73%）
投入：$3,000（账户15%）
类型：等差网格，现货，无杠杆

📊 预期表现：
- 年化收益：15-22%
- 最大回撤：7.5%
- 夏普比率：1.5-1.9
- 资金效率：50%

📋 执行命令预览：
okx bot grid create \
  --instId ETH-USDT \
  --lowerPx 2650 \
  --upperPx 2850 \
  --gridNum 10 \
  --investment 3000 \
  --lever 1 \
  --runType arithmetic \
  --triggerPx 2750 \
  --stopType both \
  --stopPxRatio 0.02 \
  --tpRatio 0.005 \
  --slRatio 0.01

是否确认创建？（Y/N）
```

### 8. 监控与调整建议

#### 8.1 每日监控要点
1. **价格位置**：是否在区间内运行
2. **成交频率**：日均成交是否正常（2-5格）
3. **资金利用率**：持仓比例是否合理（40-60%）
4. **区间有效性**：价格是否测试区间边界

#### 8.2 每周调整建议
1. **区间重评估**：基于最新高低点调整区间
2. **参数优化**：根据实际波动率调整格间距
3. **资金再平衡**：提取盈利或补充资金
4. **策略对比**：对比网格收益与简单持有收益

#### 8.3 特殊行情处理
1. **突破上涨**：
   - 价格突破上轨+2%持续4小时：暂停网格
   - 等待回调或重新评估更高区间
   - 考虑部分止盈，保留底仓

2. **跌破下行**：
   - 价格跌破下轨-2%持续4小时：暂停网格
   - 评估是否进入下降趋势
   - 考虑止损或转为DCA补仓

3. **极端波动**：
   - 波动率超过平均2倍：减少格数，扩大间距
   - 流动性不足：减少每格金额
   - 系统风险：暂停所有自动化策略

### 9. 高级功能：动态网格

#### 9.1 自适应区间
```bash
# 动态网格参数
okx bot grid create \
  --instId ETH-USDT \
  --gridType adaptive \
  --basePx 2750 \
  --volatilityFactor 1.5 \
  --rebalanceHours 24 \
  --investment 3000
```

**自适应逻辑**：
- 每24小时重新计算布林带作为新区间
- 根据波动率(ATR)调整格间距
- 自动调整网格数量保持风险恒定

#### 9.2 天地网格（无限网格）
```bash
# 天地网格（无上下限）
okx bot grid create \
  --instId ETH-USDT \
  --gridType heaven_earth \
  --basePx 2750 \
  --gridRange 0.10 \
  --gridNum 20 \
  --investment 5000
```

**特点**：
- 无固定上下限，随价格移动
- 适合趋势+震荡混合行情
- 永不踏空，但资金效率更低

## 测试命令（模拟模式）
```bash
# 模拟网格参数计算
okx bot grid params --instId ETH-USDT --strategy moderate --demo

# 模拟回测结果
okx bot grid backtest --instId ETH-USDT --lowerPx 2650 --upperPx 2850 --gridNum 10 --investment 3000 --period 90d --demo
```

## 错误处理场景

### 场景1：趋势过强不适合网格
```
⚠️ 趋势市场警告！
ADX(14): 35.2（>25，强趋势）
网格策略适配度：42%（低）

建议：
1. 改用趋势跟踪策略
2. 如果坚持网格，使用极宽区间（±15%）
3. 减少投入资金至账户5%以下
```

### 场景2：波动率过低
```
⚠️ 低波动率警告！
ATR(14): 18.5（日波动率0.67%）
网格最小盈利无法覆盖手续费

建议：
1. 扩大网格间距至≥0.8%
2. 减少网格数量，增加每格金额
3. 考虑其他策略（如DCA）
```

### 场景3：流动性不足
```
⚠️ 流动性警告！
订单簿价差：0.4%（>0.1%正常）
成交量：24h $120M（<$500M建议）

建议：
1. 减少每格金额，避免大单影响
2. 使用限价单，避免市价单滑点
3. 考虑更主流的交易对
```