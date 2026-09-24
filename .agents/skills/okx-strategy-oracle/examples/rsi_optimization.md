# 示例：RSI 参数优化

## 用户输入
```
优化 RSI 参数
```

## Skill 处理流程

### 1. 解析指令
- 识别指标：`RSI`（相对强弱指数）
- 识别操作：`优化参数`
- 识别标的：默认 BTC-USDT（未指定时）
- 识别目标：找到最优 RSI 参数组合（周期、超买超卖阈值）
- 识别策略：基于历史数据的参数回测与优化

### 2. RSI 策略基础

#### 2.1 RSI 指标原理
- **公式**：RSI = 100 - 100 / (1 + RS)，其中 RS = 平均上涨幅度 / 平均下跌幅度
- **标准参数**：周期14，超买线70，超卖线30
- **交易信号**：
  - 超卖区（<30）上穿：买入信号
  - 超买区（>70）下穿：卖出信号
  - 背离信号：价格新高但RSI未新高（顶背离），或价格新低但RSI未新低（底背离）

#### 2.2 优化目标
1. **最大化夏普比率**：风险调整后收益
2. **最大化胜率**：盈利交易比例
3. **最小化最大回撤**：最大累计亏损
4. **最大化收益风险比**：总收益 / 最大回撤
5. **最小化交易频率**：避免过度交易导致手续费磨损

### 3. 数据准备与参数空间

#### 3.1 历史数据获取
```bash
# 获取1年日线数据用于回测
okx market candles --instId BTC-USDT --bar 1d --limit 365
# 获取4小时数据用于参数敏感度分析
okx market candles --instId BTC-USDT --bar 4h --limit 2000
```
数据范围：2023-01-15 至 2024-01-15（365个交易日）

#### 3.2 参数搜索空间
定义待优化的参数范围：
- **RSI周期**：7-21（步长1），共15个值
- **超买线**：65-80（步长1），共16个值  
- **超卖线**：20-35（步长1），共16个值
- **过滤条件**：是否需要确认K线，是否需要成交量配合

**总组合数**：15 × 16 × 16 = 3,840 种参数组合

#### 3.3 回测设置
- **初始资金**：10,000 USDT
- **交易成本**：0.1% 每笔（双边0.2%）
- **仓位管理**：固定比例20%，无杠杆
- **止损止盈**：基于ATR的动态止损（2×ATR）
- **回测方法**：Walk-forward 滚动优化（避免过拟合）

### 4. 参数优化算法

#### 4.1 网格搜索 + 遗传算法
```python
# 优化算法流程
1. 初始网格搜索：均匀采样 10% 参数空间（384个点）
2. 遗传算法优化：基于初始结果进化50代
3. 局部精细搜索：在最优区域进行密集搜索
4. 过拟合检验：对比样本内和样本外表现
5. 稳健性测试：不同市场环境下的表现
```

#### 4.2 多目标优化
同时优化4个目标函数（加权综合得分）：
1. 夏普比率（权重40%）
2. 胜率（权重25%）
3. 收益风险比（权重20%）
4. 交易频率倒数（权重15%）

**综合得分** = 0.4×Sharpe + 0.25×WinRate + 0.2×Return/Risk + 0.15×(1/TradeFreq)

### 5. 优化结果分析

#### 5.1 最优参数组合
经过3,840种组合回测，排名前5的参数组合：

| 排名 | RSI周期 | 超买线 | 超卖线 | 夏普比率 | 胜率 | 年化收益 | 最大回撤 | 综合得分 |
|------|---------|--------|--------|----------|------|----------|----------|----------|
| 1 | 11 | 72 | 28 | 1.85 | 58.3% | 34.2% | -12.5% | 89.2 |
| 2 | 9 | 75 | 25 | 1.72 | 56.8% | 31.5% | -14.2% | 85.7 |
| 3 | 14 | 70 | 30 | 1.68 | 55.2% | 29.8% | -13.8% | 83.4 |
| 4 | 13 | 68 | 32 | 1.59 | 57.1% | 27.4% | -15.1% | 81.9 |
| 5 | 10 | 73 | 27 | 1.76 | 54.9% | 32.1% | -16.3% | 80.5 |

**标准参数对比**：周期14，超买70，超卖30 → 夏普1.42，胜率52.1%，年化22.3%

#### 5.2 最优参数深度分析（RSI周期11，超买72，超卖28）

**回测表现（2023年全年）**：
- **总收益率**：+34.2%（BTC现货同期+152.3%，策略跑输大盘但风险更低）
- **交易次数**：47次（平均7.8天一次）
- **盈利交易**：28次（胜率59.6%）
- **平均盈利**：+3.8%（盈利交易）
- **平均亏损**：-2.1%（亏损交易）
- **盈亏比**：1.81:1（平均盈利/平均亏损）
- **最大连胜**：6次
- **最大连败**：3次
- **最长持仓**：18天
- **最短持仓**：2天

**月度表现分布**：
- 最佳月份：2023年1月（+8.2%）
- 最差月份：2023年8月（-4.1%）
- 盈利月份：9个月（75%）
- 亏损月份：3个月（25%）

#### 5.3 参数敏感性分析

**RSI周期敏感性**：
- 周期7-9：交易频繁，胜率低（45-50%），夏普<1.2
- 周期10-12：最佳区间，平衡频率与胜率
- 周期13-16：交易稀少，可能错过机会
- 周期17-21：信号滞后，表现下降

**超买线敏感性**：
- 65-68：频繁触发，胜率低但盈亏比高
- 69-73：最优区间，平衡敏感度与胜率
- 74-78：信号稀少，可能错过趋势
- 79-80：极少触发，基本无效

**超卖线敏感性**：
- 20-25：激进，频繁抄底，胜率低
- 26-30：平衡区间
- 31-35：保守，错过底部机会

### 6. 三档优化方案

基于不同风险偏好和交易风格，提供三档优化方案：

| 参数 | 保守型 | 稳健型（推荐） | 激进型 | 说明 |
|------|--------|----------------|--------|------|
| **RSI周期** | 14 | 11 | 9 | 周期越短越敏感 |
| **超买线** | 70 | 72 | 75 | 线越高越难触发 |
| **超卖线** | 30 | 28 | 25 | 线越低越激进 |
| **确认K线** | 需要2根 | 需要1根 | 无需确认 | 减少假信号 |
| **成交量过滤** | 需要 | 推荐 | 不需要 | 量价配合 |
| **夏普比率** | 1.68 | 1.85 | 1.72 | 风险调整收益 |
| **胜率** | 55.2% | 58.3% | 56.8% | 盈利交易比例 |
| **年化收益** | 29.8% | 34.2% | 31.5% | 预期收益 |
| **最大回撤** | -13.8% | -12.5% | -14.2% | 历史最大亏损 |
| **交易频率** | 年均35次 | 年均47次 | 年均62次 | 交易次数 |
| **适合市场** | 趋势市 | 震荡+趋势 | 震荡市 | 环境适应性 |
| **适合用户** | 低频交易者 | 多数投资者 | 高频交易者 | |

#### 6.1 稳健型方案详情（综合最优）
- **参数**：RSI(11)，超买72，超卖28
- **买入条件**：RSI上穿28，且当日收盘价 > 开盘价（阳线确认）
- **卖出条件**：RSI下穿72，或达到止损位（2×ATR）
- **仓位管理**：每次20%仓位，盈利5%后止损上移至成本价
- **过滤规则**：避免在ADX>30的强趋势市中使用（改用趋势策略）

#### 6.2 策略逻辑代码（伪代码）
```
# RSI策略逻辑
def rsi_strategy(price_data, period=11, overbought=72, oversold=28):
    rsi = calculate_rsi(price_data, period)
    signals = []
    
    for i in range(1, len(rsi)):
        # 买入信号：RSI上穿超卖线，且阳线确认
        if rsi[i-1] < oversold and rsi[i] >= oversold:
            if price_data.close[i] > price_data.open[i]:  # 阳线确认
                signals.append(('BUY', price_data.close[i]))
        
        # 卖出信号：RSI下穿超买线
        elif rsi[i-1] > overbought and rsi[i] <= overbought:
            signals.append(('SELL', price_data.close[i]))
    
    return signals
```

### 7. JSON 格式输出

```json
{
  "optimization_id": "RSI_OPT_20240115_BTC",
  "timestamp": "2024-01-15T17:00:00Z",
  "instrument": "BTC-USDT",
  "timeframe": "1d",
  "backtest_period": "2023-01-15 to 2024-01-15",
  "parameter_space": {
    "rsi_period": {"min": 7, "max": 21, "step": 1},
    "overbought": {"min": 65, "max": 80, "step": 1},
    "oversold": {"min": 20, "max": 35, "step": 1},
    "total_combinations": 3840,
    "tested_combinations": 3840
  },
  
  "top_parameters": [
    {
      "rank": 1,
      "rsi_period": 11,
      "overbought": 72,
      "oversold": 28,
      "performance": {
        "sharpe_ratio": 1.85,
        "win_rate": 0.583,
        "annual_return": 0.342,
        "max_drawdown": -0.125,
        "total_trades": 47,
        "profit_factor": 2.31,
        "calmar_ratio": 2.74,
        "sortino_ratio": 2.56,
        "ulcer_index": 5.2
      },
      "trade_metrics": {
        "avg_winning_trade": 0.038,
        "avg_losing_trade": -0.021,
        "largest_win": 0.112,
        "largest_loss": -0.045,
        "avg_holding_days": 7.2,
        "best_month": "2023-01 (+8.2%)",
        "worst_month": "2023-08 (-4.1%)"
      }
    },
    {
      "rank": 2,
      "rsi_period": 9,
      "overbought": 75,
      "oversold": 25,
      "performance": {
        "sharpe_ratio": 1.72,
        "win_rate": 0.568,
        "annual_return": 0.315,
        "max_drawdown": -0.142
      }
    }
  ],
  
  "benchmark_comparison": {
    "default_parameters": {"period": 14, "overbought": 70, "oversold": 30},
    "improvement": {
      "sharpe_ratio": "+30.3%",
      "win_rate": "+6.2%",
      "annual_return": "+11.9%",
      "max_drawdown": "-1.3%"
    }
  },
  
  "market_regime_analysis": {
    "trending_market": {
      "occurrence": "35%",
      "strategy_performance": "+18.2%",
      "recommendation": "降低仓位或切换趋势策略"
    },
    "ranging_market": {
      "occurrence": "45%",
      "strategy_performance": "+42.5%",
      "recommendation": "最佳环境，可增加仓位"
    },
    "volatile_market": {
      "occurrence": "20%",
      "strategy_performance": "-8.3%",
      "recommendation": "暂停使用，等待稳定"
    }
  },
  
  "robustness_tests": {
    "walk_forward": {
      "in_sample": 0.342,
      "out_of_sample": 0.298,
      "degradation": "12.9%",
      "conclusion": "稳健性良好"
    },
    "monte_carlo": {
      "probability_profit": "92.3%",
      "expected_max_drawdown": "-15.8%",
      "value_at_risk_95": "-8.2%"
    },
    "parameter_stability": {
      "period_tolerance": "±2",
      "overbought_tolerance": "±3",
      "oversold_tolerance": "±2",
      "conclusion": "参数稳定性高"
    }
  },
  
  "execution_plan": {
    "skill": "okx-execution-vortex",
    "strategy_type": "rsi_mean_reversion",
    "parameters": {
      "conservative": {"period": 14, "overbought": 70, "oversold": 30},
      "moderate": {"period": 11, "overbought": 72, "oversold": 28},
      "aggressive": {"period": 9, "overbought": 75, "oversold": 25}
    },
    "triggers": {
      "buy": "RSI crosses above oversold with bullish candle confirmation",
      "sell": "RSI crosses below overbought or stop loss triggered"
    }
  }
}
```

### 8. 一键转执行（RSI策略）

用户选择稳健型方案后，自动生成RSI策略执行指令：

```
🚀 即将生成【RSI均值回归策略】执行计划：

参数：RSI(11)，超买72，超卖28
标的：BTC-USDT（现货）
仓位：每次账户20%
止损：2×ATR（动态）
持有期：平均7天

📊 预期表现（基于1年回测）：
- 年化收益：34.2%
- 胜率：58.3%
- 最大回撤：-12.5%
- 夏普比率：1.85
- 年均交易：47次

📋 执行方案：

方案A：手动执行（推荐新手）
1. 每日计算RSI(11)值
2. 当RSI<28且当日收阳线时买入20%
3. 当RSI>72时卖出，或跌破止损位
4. 记录每笔交易，每周复盘

方案B：条件单自动执行
okx algo place \
  --instId BTC-USDT \
  --algoType rsi_mean_reversion \
  --period 11 \
  --overbought 72 \
  --oversold 28 \
  --positionSize 0.2 \
  --stopLossType atr \
  --stopLossMultiple 2

方案C：网格化RSI策略
okx bot grid create \
  --instId BTC-USDT \
  --gridType rsi_based \
  --rsiPeriod 11 \
  --buyZone 20-28 \
  --sellZone 72-80 \
  --investment 5000

请选择执行方案 (A/B/C)：
```

### 9. 策略监控与优化循环

#### 9.1 实时监控面板
```
📈 RSI(11) 策略监控 | BTC-USDT
时间：2024-01-15 17:30:00
当前价格：51,234.56

📊 指标状态：
- RSI(11)：64.2（中性）
- 距离超买(72)：+7.8点
- 距离超卖(28)：-36.2点
- ATR(14)：850.3（波动率1.66%）
- 趋势强度(ADX)：22.1（弱趋势）

💰 持仓状态：
- 当前持仓：无
- 上次交易：2024-01-12 卖出 @ 50,800
- 累计盈利：+3.2%（本月）
- 最大浮亏：-1.8%（历史）

🔔 信号提醒：
- 暂无交易信号
- 下次检查：4小时后
- 市场状态：震荡偏多
```

#### 9.2 月度优化流程
1. **每周检查**：
   - RSI参数是否仍然有效
   - 市场环境是否变化（趋势/震荡）
   - 策略表现是否达标

2. **每月再优化**：
   - 用最新30天数据重新优化参数
   - 对比新旧参数表现
   - 决定是否切换参数

3. **季度大优化**：
   - 全参数空间重新搜索
   - 加入新特征（成交量、波动率过滤）
   - 策略组合优化（RSI+MACD+均线）

#### 9.3 动态参数调整
- **波动率自适应**：高波动时调高超买超卖线
- **趋势强度自适应**：强趋势时减少RSI交易频率
- **市场状态识别**：震荡市增加仓位，趋势市减少仓位

### 10. 高级优化功能

#### 10.1 多时间框架优化
```bash
# 同时优化日线、4小时、1小时参数
okx strategy optimize \
  --indicator RSI \
  --timeframes 1d,4h,1h \
  --objective multi_timeframe \
  --weight "0.5,0.3,0.2"
```

#### 10.2 机器学习优化
```bash
# 使用机器学习算法优化
okx strategy optimize \
  --indicator RSI \
  --algorithm xgboost \
  --features "price,volume,volatility,sentiment" \
  --cross_validation 5
```

#### 10.3 组合策略优化
```bash
# RSI + MACD + 均线组合优化
okx strategy optimize \
  --indicators "RSI,MACD,EMA" \
  --combination genetic \
  --generations 100 \
  --population 500
```

## 测试命令（模拟模式）
```bash
# 模拟参数优化
okx strategy optimize --indicator RSI --instId BTC-USDT --period 365 --demo

# 模拟回测
okx strategy backtest --strategy rsi --parameters "period=11,overbought=72,oversold=28" --demo
```

## 错误处理场景

### 场景1：过拟合警告
```
⚠️ 过拟合检测警告！
样本内表现：夏普2.15，年化41.2%
样本外表现：夏普1.12，年化18.5%
退化率：56.9%（>30% 阈值）

建议：
1. 简化参数，减少自由度
2. 增加样本外测试比例
3. 使用更保守的参数组合
```

### 场景2：市场环境变化
```
⚠️ 市场环境变化！
优化期：2023年（震荡市占比65%）
当前市场：2024年1月（趋势市占比80%）
策略适配度：42%（低）

建议：
1. 暂停RSI均值回归策略
2. 切换到趋势跟踪策略
3. 重新优化RSI参数（使用趋势市数据）
```

### 场景3：参数失效
```
🚨 参数失效警告！
最优参数：RSI(11,72,28)
最近30天表现：夏普0.45，胜率38.2%
失效可能原因：市场结构变化，流动性变化

应急方案：
1. 立即切换到保守参数(14,70,30)
2. 暂停自动交易，转为手动
3. 启动紧急重新优化
请选择方案 (1/2/3)：
```