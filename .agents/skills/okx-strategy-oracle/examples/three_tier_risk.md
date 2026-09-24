# 示例：三档风险方案生成

## 用户输入
```
生成三档风险方案 BTC
```

## Skill 处理流程

### 1. 解析指令
- 识别标的：`BTC`（默认 BTC-USDT）
- 识别需求：`三档风险方案`
- 识别类型：同时提供保守、稳健、激进三套方案
- 识别用途：用户可根据自身风险偏好选择执行

### 2. 用户风险偏好评估

#### 2.1 风险维度评估（如用户未提供，使用默认值）
```bash
# 风险偏好问卷（模拟）
okx strategy risk-assessment --questionnaire brief
```
返回默认风险画像：
- **风险承受能力**：中等（可承受 15-25% 回撤）
- **投资经验**：1-3年（有一定经验）
- **投资目标**：资产增值（年化 15-30%）
- **投资期限**：3-12个月（中期）
- **流动性需求**：中等（可接受部分资金锁定）

#### 2.2 账户状态分析
```bash
okx account balance --ccy USDT
okx account config
okx spot positions --instId BTC-USDT
```
返回结果：
- 账户总资产：50,000 USDT
- 可用余额：40,000 USDT
- 当前持仓：BTC 0.2 (约 10,000 USDT)
- 风险等级：Lv.2（可开 10x 杠杆）

### 3. 市场状态多维度分析

#### 3.1 趋势强度分析
```bash
okx market indicator --instId BTC-USDT --indicator ADX --period 14
okx market indicator --instId BTC-USDT --indicator EMA --periods 20,50,200
okx market candles --instId BTC-USDT --bar 1d --limit 30
```
返回结果：
- ADX(14): 28.5（中等趋势强度）
- EMA排列：20日>50日>200日（多头排列）
- 30日涨幅：+12.5%（温和上涨）

#### 3.2 波动率与风险度量
```bash
okx market indicator --instId BTC-USDT --indicator ATR --period 14
okx market indicator --instId BTC-USDT --indicator Bollinger --period 20
okx market indicator --instId BTC-USDT --indicator HistoricalVolatility --period 30
```
返回结果：
- ATR(14): 850.3（日波动率 1.66%）
- 布林带宽度：1,700点（3.3%，适中）
- 历史波动率(30日)：42.5%（年化）

#### 3.3 衍生品市场状态
```bash
okx market funding-rate --instId BTC-USDT-SWAP
okx market open-interest --instId BTC-USDT-SWAP
okx market mark-price --instId BTC-USDT-SWAP
```
返回结果：
- 资金费率：+0.005%（多头轻微支付）
- 未平仓合约：$15.2B（近期增加）
- 基差：+0.12%（永续轻微溢价）

#### 3.4 链上与情绪指标
```bash
okx external glassnode --metric exchange_flows --symbol BTC --period 7d
okx external alternative --metric fear_greed --symbol BTC
```
返回结果（模拟）：
- 交易所净流出：-2,500 BTC（7天）
- 恐惧贪婪指数：62（贪婪）

### 4. 综合市场评分

| 维度 | 指标值 | 评分(0-100) | 权重 | 贡献分 | 风险提示 |
|------|--------|-------------|------|--------|----------|
| 趋势方向 | 多头排列，ADX28.5 | 72 | 25% | 18.0 | 中等趋势，适合趋势跟踪 |
| 波动率 | ATR1.66%，HV42.5% | 65 | 20% | 13.0 | 波动适中，需合理止损 |
| 衍生品 | 费率+0.005%，基差+0.12% | 68 | 20% | 13.6 | 健康的多头环境 |
| 链上 | 净流出-2,500 BTC | 75 | 15% | 11.3 | 供应减少，看涨 |
| 情绪 | 贪婪指数62 | 60 | 10% | 6.0 | 情绪偏热，短期回调风险 |
| 宏观 | 美元走弱，利率稳定 | 70 | 10% | 7.0 | 宏观环境有利 |
| **总分** | **69.9** | **100%** | **68.9** | **看涨偏震荡** | |

**市场状态判断**：
- 趋势强度：中等偏强（适合趋势策略）
- 波动率：适中（需 2-3×ATR 止损）
- 风险等级：中低（当前无极端风险）
- 策略建议：趋势跟踪为主，震荡策略为辅

### 5. 三档方案生成原理

基于凯利公式、风险平价、现代投资组合理论：

#### 5.1 仓位计算（凯利公式调整版）
```
f* = (p × b - q) / b
其中：
p = 胜率（基于历史回测）
q = 1 - p
b = 盈亏比

保守型：f* × 0.5（半凯利）
稳健型：f* × 0.75  
激进型：f* × 1.0（全凯利）
```

#### 5.2 止损计算（基于波动率）
```
止损距离 = N × ATR
保守型：N = 3.0（宽止损，低触发率）
稳健型：N = 2.5（平衡）
激进型：N = 2.0（紧止损，高触发率）
```

#### 5.3 杠杆计算（基于风险预算）
```
最大杠杆 = 风险预算 / (仓位×止损%)
保守型：风险预算 1%/交易 → 杠杆较低
稳健型：风险预算 2%/交易 → 杠杆适中
激进型：风险预算 3%/交易 → 杠杆较高
```

### 6. 完整三档风险方案

#### 6.1 方案概览表

| 参数 | 保守型 | 稳健型（推荐） | 激进型 | 说明 |
|------|--------|----------------|--------|------|
| **策略类型** | 趋势跟踪+严格风控 | 趋势跟踪+适度风控 | 趋势跟踪+动量增强 | |
| **目标用户** | 新手/低风险偏好 | 多数投资者 | 经验丰富/高风险偏好 | |
| **心理特征** | 厌恶亏损，求稳为主 | 平衡收益与风险 | 追求高收益，接受大波动 | |

#### 6.2 入场与出场规则

| 规则 | 保守型 | 稳健型 | 激进型 |
|------|--------|--------|--------|
| **入场信号** | EMA20上穿EMA50 + RSI>50 | EMA20上穿EMA50 | 价格突破前高 |
| **确认条件** | 需成交量放大+阳线确认 | 需阳线确认 | 无需确认 |
| **入场价格** | 回调至EMA20支撑 | 现价或小幅回调 | 突破追价 |
| **分批入场** | 3批（33%,33%,34%） | 2批（50%,50%） | 1批（100%） |
| **止损位置** | -3.0×ATR（约-5.0%） | -2.5×ATR（约-4.2%） | -2.0×ATR（约-3.3%） |
| **移动止损** | 盈利2%后上移至成本价 | 盈利3%后上移至成本价 | 盈利4%后上移至成本价 |
| **止盈目标** | 风险回报比 3:1 | 风险回报比 2.5:1 | 风险回报比 2:1 |
| **部分止盈** | 盈利5%止盈50% | 盈利8%止盈50% | 盈利10%止盈50% |
| **持仓时间** | 5-20天 | 3-15天 | 2-10天 |

#### 6.3 仓位与杠杆管理

| 管理项 | 保守型 | 稳健型 | 激进型 |
|--------|--------|--------|--------|
| **单笔风险** | 账户0.5% | 账户1.0% | 账户1.5% |
| **仓位大小** | 账户5% | 账户8% | 账户12% |
| **杠杆倍数** | 3x | 5x | 8x |
| **保证金率** | ≥300% | ≥200% | ≥150% |
| **最大持仓** | 2个品种 | 3个品种 | 4个品种 |
| **总风险暴露** | ≤账户10% | ≤账户20% | ≤账户30% |
| **加仓条件** | 盈利5%后加仓 | 盈利3%后加仓 | 盈利2%后加仓 |
| **减仓条件** | 亏损2%减半仓 | 亏损3%减半仓 | 亏损4%减半仓 |

#### 6.4 预期表现（基于历史回测）

| 表现指标 | 保守型 | 稳健型 | 激进型 | 备注 |
|----------|--------|--------|--------|------|
| **年化收益率** | 18-25% | 25-35% | 35-50% | 预期区间 |
| **胜率** | 60-65% | 55-60% | 50-55% | 盈利交易比例 |
| **盈亏比** | 2.0-2.5 | 1.8-2.2 | 1.5-1.8 | 平均盈利/平均亏损 |
| **夏普比率** | 1.8-2.2 | 1.5-1.8 | 1.2-1.5 | 风险调整收益 |
| **最大回撤** | -8%至-12% | -12%至-18% | -18%至-25% | 历史最大亏损 |
| **Calmar比率** | 2.2-3.0 | 1.8-2.5 | 1.5-2.0 | 收益/回撤比 |
| **月胜率** | 70-75% | 65-70% | 60-65% | 盈利月份比例 |
| **最大连胜** | 5-7次 | 4-6次 | 3-5次 | 连续盈利次数 |
| **最大连败** | 2-3次 | 3-4次 | 4-5次 | 连续亏损次数 |
| **收益稳定性** | 高 | 中高 | 中 | 月度收益波动 |

#### 6.5 适用市场环境

| 市场环境 | 保守型 | 稳健型 | 激进型 |
|----------|--------|--------|--------|
| **强趋势市** | 表现良好 | 表现优秀 | 表现卓越 |
| **震荡市** | 表现一般 | 表现中等 | 表现较差 |
| **高波动市** | 暂停交易 | 降低仓位 | 可尝试但高风险 |
| **低波动市** | 耐心等待 | 降低预期 | 寻找其他机会 |
| **转折市** | 观望为主 | 轻仓试探 | 高风险高回报 |

### 7. 具体执行方案（以稳健型为例）

#### 7.1 当前参数计算
- 当前价格：51,234.56 USDT
- ATR(14)：850.3 USDT
- 账户规模：50,000 USDT

**计算过程**：
1. 止损距离 = 2.5 × ATR = 2,125.75 USDT (-4.15%)
2. 止损价 = 51,234.56 - 2,125.75 = 49,108.81 USDT
3. 止盈距离 = 止损距离 × 2.5 = 5,314.38 USDT (+10.38%)
4. 止盈价 = 51,234.56 + 5,314.38 = 56,548.94 USDT
5. 单笔风险 = 账户1% = 500 USDT
6. 仓位大小 = 单笔风险 ÷ 止损% = 500 ÷ 0.0415 ≈ 12,048 USDT
7. 仓位比例 = 12,048 ÷ 50,000 = 24.1%
8. 杠杆调整：24.1%仓位 ÷ 5x杠杆 = 名义价值60,240 USDT
9. 实际张数：60,240 ÷ 51,234.56 ≈ 1.176张（永续合约）

#### 7.2 执行计划详情
```
📋 稳健型执行计划（BTC-USDT-SWAP）

入场计划：
- 价格：51,234.56 USDT（当前价）
- 方向：做多
- 数量：1.2张（名义价值61,481 USDT）
- 杠杆：5x（逐仓）
- 保证金：12,296 USDT
- 分批：2批（首批0.6张 @ 51,234.56，第二批0.6张 @ 50,800）

止损设置：
- 价格：49,108.81 USDT（-4.15%）
- 类型：市价止损
- 触发条件：标记价格 ≤ 49,108.81

止盈设置：
- 第一目标：53,500 USDT（+4.42%），止盈50%
- 第二目标：56,548.94 USDT（+10.38%），止盈剩余50%
- 移动止损：盈利3%后上移至成本价

风险管理：
- 单笔风险：500 USDT（账户1%）
- 最大亏损：1,250 USDT（仓位×止损%）
- 保证金率监控：<200%时预警
- 对冲建议：可买入平价看跌期权保护
```

#### 7.3 替代入场方案
如当前价格偏高，可选择等待回调：
```
🎯 回调入场方案（更优风险回报）

等待回调至：
1. 50,800 USDT（EMA20支撑，-0.85%）
2. 50,200 USDT（前高转支撑，-2.02%）
3. 49,500 USDT（心理关口，-3.39%）

分批入场比例：
- 50,800：入场40%
- 50,200：入场30%
- 49,500：入场30%

平均入场价：50,230 USDT
改进后风险回报比：3.2:1（原2.5:1）
```

### 8. JSON 格式输出

```json
{
  "strategy_id": "BTC_3TIER_20240115",
  "timestamp": "2024-01-15T17:30:00Z",
  "instrument": "BTC-USDT",
  "account_size": 50000.00,
  "market_score": 69.9,
  "market_regime": "bullish_ranging",
  
  "risk_tiers": {
    "conservative": {
      "tier_name": "保守型",
      "risk_appetite": "low",
      "target_annual_return": [0.18, 0.25],
      "max_acceptable_drawdown": -0.12,
      
      "position_sizing": {
        "single_trade_risk": 0.005,
        "position_size": 0.05,
        "leverage": 3,
        "max_concurrent_trades": 2,
        "total_exposure_limit": 0.10
      },
      
      "entry_rules": {
        "primary_signal": "EMA20 > EMA50 and RSI > 50",
        "confirmation": "volume_expansion and bullish_candle",
        "entry_price": "pullback to EMA20",
        "scaling_in": [0.33, 0.33, 0.34],
        "patience_level": "high"
      },
      
      "exit_rules": {
        "stop_loss": -0.05,
        "stop_loss_type": "atr_multiple",
        "stop_loss_multiple": 3.0,
        "take_profit": [0.10, 0.15],
        "trailing_stop": 0.02,
        "partial_take_profit": [0.05, 0.5]
      },
      
      "risk_management": {
        "daily_loss_limit": -0.02,
        "weekly_loss_limit": -0.05,
        "max_consecutive_losses": 3,
        "cooldown_period_hours": 24,
        "hedge_recommendation": "buy put options"
      },
      
      "performance_expectation": {
        "sharpe_range": [1.8, 2.2],
        "win_rate_range": [0.60, 0.65],
        "profit_factor_range": [2.0, 2.5],
        "calmar_range": [2.2, 3.0],
        "monthly_consistency": 0.70
      }
    },
    
    "moderate": {
      "tier_name": "稳健型",
      "risk_appetite": "medium",
      "target_annual_return": [0.25, 0.35],
      "max_acceptable_drawdown": -0.18,
      
      "position_sizing": {
        "single_trade_risk": 0.01,
        "position_size": 0.08,
        "leverage": 5,
        "max_concurrent_trades": 3,
        "total_exposure_limit": 0.20
      },
      
      "entry_rules": {
        "primary_signal": "EMA20 > EMA50",
        "confirmation": "bullish_candle",
        "entry_price": "current_price or slight pullback",
        "scaling_in": [0.5, 0.5],
        "patience_level": "medium"
      },
      
      "exit_rules": {
        "stop_loss": -0.042,
        "stop_loss_type": "atr_multiple",
        "stop_loss_multiple": 2.5,
        "take_profit": [0.105, 0.21],
        "trailing_stop": 0.03,
        "partial_take_profit": [0.08, 0.5]
      },
      
      "risk_management": {
        "daily_loss_limit": -0.03,
        "weekly_loss_limit": -0.08,
        "max_consecutive_losses": 4,
        "cooldown_period_hours": 12,
        "hedge_recommendation": "optional"
      },
      
      "performance_expectation": {
        "sharpe_range": [1.5, 1.8],
        "win_rate_range": [0.55, 0.60],
        "profit_factor_range": [1.8, 2.2],
        "calmar_range": [1.8, 2.5],
        "monthly_consistency": 0.65
      }
    },
    
    "aggressive": {
      "tier_name": "激进型",
      "risk_appetite": "high",
      "target_annual_return": [0.35, 0.50],
      "max_acceptable_drawdown": -0.25,
      
      "position_sizing": {
        "single_trade_risk": 0.015,
        "position_size": 0.12,
        "leverage": 8,
        "max_concurrent_trades": 4,
        "total_exposure_limit": 0.30
      },
      
      "entry_rules": {
        "primary_signal": "price_breakout_high",
        "confirmation": "none",
        "entry_price": "breakout_price",
        "scaling_in": [1.0],
        "patience_level": "low"
      },
      
      "exit_rules": {
        "stop_loss": -0.033,
        "stop_loss_type": "atr_multiple",
        "stop_loss_multiple": 2.0,
        "take_profit": [0.066, 0.132],
        "trailing_stop": 0.04,
        "partial_take_profit": [0.10, 0.5]
      },
      
      "risk_management": {
        "daily_loss_limit": -0.05,
        "weekly_loss_limit": -0.12,
        "max_consecutive_losses": 5,
        "cooldown_period_hours": 6,
        "hedge_recommendation": "not recommended"
      },
      
      "performance_expectation": {
        "sharpe_range": [1.2, 1.5],
        "win_rate_range": [0.50, 0.55],
        "profit_factor_range": [1.5, 1.8],
        "calmar_range": [1.5, 2.0],
        "monthly_consistency": 0.60
      }
    }
  },
  
  "current_market_parameters": {
    "price": 51234.56,
    "atr_14": 850.3,
    "atr_percent": 0.0166,
    "ema20": 49800.00,
    "ema50": 48200.00,
    "rsi_14": 64.2,
    "adx_14": 28.5,
    "volatility_30d": 0.425
  },
  
  "execution_plans": {
    "conservative": {
      "skill": "okx-execution-vortex",
      "command": "执行买入 BTC 5%仓位，价格回调至49800附近，止损48500，目标54900，杠杆3x",
      "detailed_steps": ["等待回调", "分批入场", "严格止损"]
    },
    "moderate": {
      "skill": "okx-execution-vortex", 
      "command": "执行买入 BTC 8%仓位，现价51234或回调至50800，止损49109，目标56549，杠杆5x",
      "detailed_steps": ["现价入场50%", "回调加仓50%", "移动止盈"]
    },
    "aggressive": {
      "skill": "okx-execution-vortex",
      "command": "执行买入 BTC 12%仓位，现价51234，止损49109，目标54300，杠杆8x",
      "detailed_steps": ["全仓入场", "紧止损", "快速止盈"]
    }
  },
  
  "selection_recommendation": {
    "recommended_tier": "moderate",
    "reason": "平衡收益风险，适合当前市场环境",
    "alternative": "如担心短期回调，可选择保守型等待更好入场价",
    "warning": "激进型仅适合经验丰富、能承受大幅波动的交易者"
  }
}
```

### 9. 一键转执行（三档方案选择）

用户选择方案后，自动生成对应执行指令：

```
🎯 三档风险方案选择

请选择适合您的风险偏好方案：

1. 保守型（年化18-25%，最大回撤-12%）
   - 适合：新手、低风险偏好、求稳投资者
   - 执行：等待回调至49,800入场，3x杠杆，严格止损

2. 稳健型（年化25-35%，最大回撤-18%）✅ 推荐
   - 适合：多数投资者、平衡收益风险
   - 执行：现价或小幅回调入场，5x杠杆，适度止损

3. 激进型（年化35-50%，最大回撤-25%）
   - 适合：经验丰富者、追求高收益、能承受大波动
   - 执行：现价入场，8x杠杆，快速止盈

请选择 (1/2/3)：
```

用户选择稳健型（2）后：
```
🚀 即将执行【稳健型】方案：

市场：BTC-USDT-SWAP
仓位：账户8%（约4,000 USDT保证金）
名义价值：20,000 USDT（5x杠杆）
入场：分批（现价2,000 USDT + 回调2,000 USDT）
止损：49,109 USDT（-4.15%）
止盈：56,549 USDT（+10.38%）
预期持有：3-15天

📋 执行命令：
okx swap place --instId BTC-USDT-SWAP --side buy --posSide long --sz 0.4 --px 51234 --tdMode isolated --lever 5
okx algo place --instId BTC-USDT-SWAP --side sell --posSide long --sz 0.4 --tpTriggerPx 56549 --slTriggerPx 49109

是否确认执行？（Y/N）
```

### 10. 动态调整与监控

#### 10.1 方案间切换条件
```
🔄 方案切换建议：

从保守型 → 稳健型：
- 连续盈利3笔以上
- 市场趋势明确（ADX>30）
- 个人风险承受能力提升

从稳健型 → 保守型：
- 连续亏损2笔
- 市场波动加剧（波动率>50%）
- 个人风险承受能力下降

从任意型 → 暂停交易：
- 最大回撤达到方案上限的80%
- 市场出现系统性风险
- 个人情绪不稳定
```

#### 10.2 绩效跟踪模板
```
📊 三档方案绩效跟踪（月度）

保守型（实际 vs 预期）：
- 收益率：+2.1% vs +1.5-2.1% ✅
- 胜率：63% vs 60-65% ✅
- 最大回撤：-4.2% vs -8-12% ✅
- 评估：表现符合预期，可继续执行

稳健型（实际 vs 预期）：
- 收益率：+3.8% vs +2.1-2.9% ✅
- 胜率：58% vs 55-60% ✅
- 最大回撤：-6.5% vs -12-18% ✅
- 评估：表现优异，考虑小幅增加仓位

激进型（实际 vs 预期）：
- 收益率：+5.2% vs +2.9-4.2% ✅
- 胜率：52% vs 50-55% ✅
- 最大回撤：-9.8% vs -18-25% ✅
- 评估：表现优秀，但回撤接近心理极限
```

#### 10.3 长期优化建议
1. **每季度回顾**：评估三档方案的实际表现，调整参数
2. **市场环境适应**：不同市况下调整三档方案的权重
3. **个性化定制**：基于个人交易记录进一步个性化参数
4. **组合使用**：可同时运行两档方案（如70%稳健型+30%保守型）

## 测试命令（模拟模式）
```bash
# 模拟三档方案生成
okx strategy three-tier --instrument BTC-USDT --account-size 50000 --demo

# 模拟方案回测
okx strategy backtest --tier moderate --instrument BTC-USDT --period 90d --demo
```

## 错误处理场景

### 场景1：账户规模过小
```
❌ 账户规模不足！
当前账户：2,000 USDT
最小要求：保守型5,000，稳健型10,000，激进型20,000

建议：
1. 充值至最低要求
2. 选择模拟交易积累经验
3. 使用更小仓位比例（调整风险参数）
```

### 场景2：风险偏好与方案不匹配
```
⚠️ 风险偏好冲突！
问卷结果：风险厌恶型（得分15/100）
选择方案：激进型（推荐得分>70）

建议调整：
1. 改选保守型（匹配度92%）
2. 重新评估风险承受能力
3. 从小额试单开始
```

### 场景3：市场环境剧变
```
🚨 市场环境剧变警告！
方案生成时：ADX28.5，波动率42.5%
当前市场：ADX45.2，波动率68.3%

建议：
1. 暂停执行原方案
2. 重新生成适应高波动环境的三档方案
3. 降低所有方案仓位至50%
```