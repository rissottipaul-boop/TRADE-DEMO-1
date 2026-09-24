# BTC Trader Output Examples

## 示例 1：明确开多信号

```json
{
  "symbol": "BTC-USDT-SWAP",
  "timeframe": "4h",
  "marketRegime": "bull_trend",
  "direction": "buy",
  "signalScore": 8,
  "confidence": "high",
  "positionScale": 0.7,
  "fundingRate": 0.0002,
  "openInterestBias": "rising_with_price",
  "atrPct": 0.021,
  "action": "open",
  "stopLossMethod": "atr",
  "suggestedStopLoss": 81234.5,
  "riskFlags": [],
  "reasoning": [
    "4h 与 1h 均线同向偏多",
    "RSI 位于趋势健康区间",
    "OI 随价格上升，趋势质量较好",
    "资金费率未见极端拥挤"
  ]
}
```

简述：
- 当前偏多趋势较清晰
- 但并非满仓信号，仍按缩放仓位处理
- 推荐 ATR 止损而非裸奔持仓

---

## 示例 2：有方向但先观望

```json
{
  "symbol": "BTC-USDT-SWAP",
  "timeframe": "4h",
  "marketRegime": "bull_trend",
  "direction": "buy",
  "signalScore": 6,
  "confidence": "medium",
  "positionScale": 0.35,
  "fundingRate": 0.0007,
  "openInterestBias": "mixed",
  "atrPct": 0.038,
  "action": "observe",
  "stopLossMethod": "atr",
  "suggestedStopLoss": 80120.0,
  "riskFlags": [
    "moderately_crowded_funding",
    "elevated_volatility",
    "weak_oi_confirmation"
  ],
  "reasoning": [
    "4h 方向偏多，但 1h 确认一般",
    "资金费率偏热，需要缩仓",
    "波动率偏高，不适合激进追价"
  ]
}
```

简述：
- 不是反向做空信号
- 但也不是适合直接追多的位置
- 最合理动作是先等，不硬做

---

## 示例 3：直接跳过

```json
{
  "symbol": "BTC-USDT-SWAP",
  "timeframe": "4h",
  "marketRegime": "range",
  "direction": "flat",
  "signalScore": 3,
  "confidence": "low",
  "positionScale": 0.0,
  "fundingRate": 0.0014,
  "openInterestBias": "unclear",
  "atrPct": 0.056,
  "action": "skip",
  "stopLossMethod": "none",
  "suggestedStopLoss": null,
  "riskFlags": [
    "extreme_funding",
    "extreme_volatility",
    "range_market"
  ],
  "reasoning": [
    "均线结构不清晰，属于震荡环境",
    "资金费率过热，拥挤度高",
    "ATR 过高，风险与噪音都偏大"
  ]
}
```

简述：
- 跳过并不代表看不懂市场，而是代表风控有效
- 低质量机会不参与，本身就是策略的一部分
