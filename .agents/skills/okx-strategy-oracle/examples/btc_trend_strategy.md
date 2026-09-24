# 示例：BTC 趋势跟踪策略生成

## 用户输入
```
生成 BTC 策略
```

## Skill 处理流程

### 1. 解析指令
- 识别标的：`BTC`（默认 BTC-USDT 现货）
- 识别类型：综合策略生成（未指定具体策略类型）
- 识别时间范围：默认未来 3-7 天
- 识别风险偏好：未指定，提供三档方案

### 2. 数据采集阶段

#### 2.1 K线数据获取（价格维度）
```bash
# 获取多时间框架数据
okx market candles --instId BTC-USDT --bar 1d --limit 30
okx market candles --instId BTC-USDT --bar 4h --limit 50
okx market candles --instId BTC-USDT --bar 1h --limit 100
```
返回摘要：
- 日线：30天，涨幅 +18.5%，当前价 51,234.56
- 4小时：50根，上升通道明显
- 1小时：100根，近期突破前高 51,000

#### 2.2 技术指标计算（量价维度）
```bash
# 计算关键指标
okx market indicator --instId BTC-USDT --indicator RSI --period 14
okx market indicator --instId BTC-USDT --indicator MACD
okx market indicator --instId BTC-USDT --indicator EMA --periods 20,50,200
okx market indicator --instId BTC-USDT --indicator ATR --period 14
okx market indicator --instId BTC-USDT --indicator Bollinger --period 20
okx market indicator --instId BTC-USDT --indicator OBV
```
返回结果：
- RSI(14): 65.2（中性偏多，未超买）
- MACD: 柱状图 +120.5，金叉状态
- EMA: 20日=49,800，50日=48,200，200日=45,000（多头排列）
- ATR(14): 850.3（日波动率 1.66%）
- 布林带：价格突破中轨向上
- OBV: 持续上升，资金流入

#### 2.3 衍生品数据（衍生品维度）
```bash
# 永续合约数据
okx market funding-rate --instId BTC-USDT-SWAP
okx market open-interest --instId BTC-USDT-SWAP
okx market mark-price --instId BTC-USDT-SWAP
```
返回结果：
- 资金费率: 0.008%（多头支付，中性）
- 未平仓合约: $12.5B（近期增加 15%）
- 标记价格: 51,230.12（与现货基本持平）

#### 2.4 链上数据（链上维度）
```bash
# 外部数据源（示例）
okx external coinglass --metric exchange_flows --symbol BTC --period 7d
okx external glassnode --metric whale_transactions --symbol BTC --threshold 1000
```
返回结果（模拟）：
- 交易所净流出: -8,200 BTC（7天）
- 巨鲸交易: +24%（近期活跃）
- 矿工持仓: -1.5%（小幅减持）

#### 2.5 宏观数据（宏观维度）
```bash
# 宏观指标查询
okx external macroeconomic --metric dxy  # 美元指数
okx external macroeconomic --metric fed_rate  # 利率预期
```
返回结果（模拟）：
- 美元指数: 102.3（近期下跌）
- 利率预期: 维持不变概率 75%
- BTC-美股相关性: 0.45（中等）

#### 2.6 情绪数据（情绪维度）
```bash
# 市场情绪指标
okx external santiment --metric social_volume --symbol BTC
okx external alternative --metric fear_greed --symbol BTC
```
返回结果（模拟）：
- 社交媒体讨论量: +32%（7天）
- 恐惧贪婪指数: 68（贪婪）
- 多空比: 1.25（多头略占优）

### 3. 6维评分计算

| 维度 | 原始数据 | 评分(0-100) | 权重 | 贡献分 | 说明 |
|------|----------|-------------|------|--------|------|
| **价格** | 突破前高，上升通道 | 85 | 25% | 21.25 | 日线+18.5%，EMA多头排列 |
| **量价** | OBV上升，放量突破 | 78 | 20% | 15.60 | 资金持续流入 |
| **衍生品** | 资金费率中性，OI上升 | 72 | 20% | 14.40 | 健康的多头环境 |
| **链上** | 交易所净流出 | 80 | 15% | 12.00 | 供应减少，看涨 |
| **宏观** | 美元走弱，利率稳定 | 65 | 10% | 6.50 | 宏观环境有利 |
| **情绪** | 贪婪指数68，讨论活跃 | 70 | 10% | 7.00 | 情绪偏热但未极端 |
| **总分** | **72.75** | **100%** | **76.75** | **看涨** | |

**趋势判断**：总分 76.75/100，属于"强看涨"区间（70-85）。
**置信度**：85%（基于多指标共振和数据质量）。

### 4. 策略生成

#### 策略类型：趋势跟踪
- **核心逻辑**：顺势而为，突破入场，移动止损
- **时间框架**：4小时图为主，日线确认方向
- **持有周期**：5-15天（中线趋势）
- **适合人群**：有一定风险承受能力的趋势交易者

#### 三档风险方案

| 参数 | 保守型 | 稳健型 | 激进型 | 说明 |
|------|--------|--------|--------|------|
| **入场区间** | 51,000-51,500 | 50,500-52,000 | 50,000-52,500 | 分批入场 |
| **止损位置** | 49,800 (-2.8%) | 49,000 (-4.4%) | 48,000 (-6.3%) | ATR×2-3 |
| **止盈目标** | 53,500 (+4.4%) | 55,000 (+7.4%) | 57,500 (+12.2%) | 风险回报比1.5-2 |
| **仓位大小** | 账户3% | 账户5% | 账户8% | 基于凯利公式 |
| **杠杆倍数** | 3x | 5x | 8x | 永续合约 |
| **预期收益** | +13.2% | +22.0% | +35.2% | 年化估算 |
| **最大回撤** | -8.4% | -14.0% | -22.4% | 基于历史波动 |
| **胜率估算** | 65% | 60% | 55% | 回测数据 |

#### 保守型方案详情（推荐新手）
- **入场策略**：51,200 现价入 50%，50,800 加仓 50%
- **止损策略**：统一止损 49,800，跌破立即离场
- **止盈策略**：53,500 止盈 50%，剩余移动止损
- **风险管理**：单笔最大亏损 ≤ 账户 2%

#### 技术指标关键位
- **支撑位**：50,800（前高转支撑），50,000（心理关口）
- **阻力位**：52,000（前高），53,500（斐波那契扩展）
- **多空分界**：49,500（EMA20支撑）
- **趋势确认**：站稳 51,500 看 53,000+

### 5. JSON 格式输出（程序化接口）

```json
{
  "strategy_id": "BTC_TREND_20240115_4H",
  "timestamp": "2024-01-15T15:30:00Z",
  "instrument": "BTC-USDT",
  "instrument_type": "spot",
  "timeframe": "4h",
  "score": 76.75,
  "trend_direction": "bullish",
  "trend_strength": "strong",
  "confidence": 0.85,
  "valid_until": "2024-01-22T15:30:00Z",
  
  "dimension_scores": {
    "price": {"score": 85, "weight": 0.25, "contribution": 21.25},
    "volume_price": {"score": 78, "weight": 0.20, "contribution": 15.60},
    "derivatives": {"score": 72, "weight": 0.20, "contribution": 14.40},
    "on_chain": {"score": 80, "weight": 0.15, "contribution": 12.00},
    "macro": {"score": 65, "weight": 0.10, "contribution": 6.50},
    "sentiment": {"score": 70, "weight": 0.10, "contribution": 7.00}
  },
  
  "risk_levels": {
    "conservative": {
      "entry_range": [51000, 51500],
      "stop_loss": 49800,
      "take_profit": 53500,
      "position_size": 0.03,
      "leverage": 3,
      "expected_return": 0.132,
      "max_drawdown": -0.084,
      "win_rate": 0.65
    },
    "moderate": {
      "entry_range": [50500, 52000],
      "stop_loss": 49000,
      "take_profit": 55000,
      "position_size": 0.05,
      "leverage": 5,
      "expected_return": 0.220,
      "max_drawdown": -0.140,
      "win_rate": 0.60
    },
    "aggressive": {
      "entry_range": [50000, 52500],
      "stop_loss": 48000,
      "take_profit": 57500,
      "position_size": 0.08,
      "leverage": 8,
      "expected_return": 0.352,
      "max_drawdown": -0.224,
      "win_rate": 0.55
    }
  },
  
  "technical_levels": {
    "support": [50800, 50000, 49500],
    "resistance": [52000, 53500, 55000],
    "pivot": 51234.56,
    "atr": 850.3,
    "volatility": 0.0166
  },
  
  "indicators": {
    "rsi_14": 65.2,
    "macd": {"value": 120.5, "signal": 80.2, "histogram": 40.3},
    "ema": {"20": 49800, "50": 48200, "200": 45000},
    "bollinger": {"upper": 52500, "middle": 50800, "lower": 49100},
    "atr_14": 850.3
  },
  
  "execution_plan": {
    "skill": "okx-execution-vortex",
    "commands": {
      "conservative": "执行买入 BTC 3%仓位，价格区间 51000-51500，止损 49800，止盈 53500",
      "moderate": "执行买入 BTC 5%仓位，价格区间 50500-52000，止损 49000，止盈 55000",
      "aggressive": "执行买入 BTC 8%仓位，价格区间 50000-52500，止损 48000，止盈 57500"
    }
  },
  
  "warnings": [
    "RSI接近超买区(70)，短期可能回调",
    "关键阻力52,000尚未有效突破",
    "建议分批入场降低风险"
  ]
}
```

### 6. 一键转执行（调用漩涡执行器）

用户选择稳健型方案后，自动生成执行指令：

```
🚀 即将调用【漩涡执行器】执行以下交易：

市场：BTC-USDT-SWAP
方案：稳健型趋势跟踪
入场：50,500-52,000 区间分批买入
仓位：账户5%（约1,000 USDT名义价值）
杠杆：5x（逐仓）
止损：49,000（-4.4%）
止盈：55,000（+7.4%）
预期：胜率60%，预期收益+22.0%

📋 执行命令预览：
okx swap place --instId BTC-USDT-SWAP --side buy --posSide long --sz 0.2 --px 50500 --tdMode isolated --lever 5
okx algo place --instId BTC-USDT-SWAP --side sell --posSide long --sz 0.2 --tpTriggerPx 55000 --slTriggerPx 49000

是否确认执行？（Y/N）
```

### 7. 更新与监控建议

1. **每日检查**：
   - 价格是否在预期区间运行
   - RSI是否突破70（超买）
   - 成交量是否持续放大

2. **调整触发条件**：
   - 突破52,000：上调目标至55,000-57,000
   - 跌破49,500：减仓50%，重新评估趋势
   - 收盘价低于49,000：止损离场

3. **后续策略**：
   - 盈利5%后：止损上移至成本价
   - 盈利10%后：止盈50%，剩余移动止损
   - 持仓7天后：无论盈亏，重新评估

## 关键逻辑说明

1. **6维评分体系**：综合多方面数据，避免单一指标偏见
2. **三档风险方案**：适应不同风险偏好用户
3. **数据驱动决策**：所有参数基于历史回测和统计计算
4. **一键执行集成**：与执行Skill无缝衔接，形成闭环
5. **实时数据更新**：策略基于最新市场数据生成

## 测试命令（模拟模式）
```bash
# 模拟数据采集（不实际调用API）
okx market candles --instId BTC-USDT --bar 4h --limit 10 --demo

# 模拟指标计算
okx market indicator --instId BTC-USDT --indicator RSI --period 14 --demo
```

## 错误处理场景

### 场景1：数据不完整
```
⚠️ 数据质量警告！
K线数据缺失率：15%（最近24小时）
影响：评分置信度降低至70%
建议：等待数据恢复或使用更长周期分析
```

### 场景2：指标矛盾
```
⚠️ 指标信号矛盾！
看涨信号：MACD金叉，EMA多头排列（权重60%）
看跌信号：RSI超买，成交量萎缩（权重40%）
综合判断：中性偏多，但短期回调风险高
建议：等待回调入场或降低仓位
```

### 场景3：极端行情
```
🚨 极端行情检测！
24小时波动率：8.5%（远超平均3%）
价格变化：+12%（单边急涨）
建议：暂停趋势策略，等待波动率回归
替代方案：考虑网格交易或观望
```