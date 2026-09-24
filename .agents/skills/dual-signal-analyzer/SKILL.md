---
name: dual-signal-analyzer
title: 双信号分析器
description: 基于RSI和MACD双指标交叉验证的交易信号分析Skill，提供智能买卖信号和风险管理建议
version: 1.0.0
author: 菊中菊
category: trading-analysis
tags:
  - trading
  - strategy
  - analysis
  - crypto
  - signals
  - rsi
  - macd
  - risk-management
requires:
  - okx-cex-market
  - okx-cex-trade
---

# 双信号分析器

## 🎯 技能概述

双信号分析器是一个基于RSI和MACD双指标交叉验证的交易信号分析Skill。通过两个独立技术指标的协同分析，提供更可靠的交易信号和风险管理建议。

### 触发词
- '双信号分析'
- 'RSI MACD验证'
- '交叉验证信号'
- '智能交易分析'
- '风险评分'
- '机会扫描'

### 核心优势
- **双指标验证**: RSI超卖/超买 + MACD金叉/死叉双重确认
- **风险智能评分**: 基于市场波动性和信号一致性的动态风险评估
- **批量高效扫描**: 同时分析多个交易对，快速发现最佳机会
- **差异化功能**: 相比单一指标分析，提供更高的信号可靠性

## 🚀 安装方式

### 一键安装
```bash
npx @okx_ai/okx-trade-cli@latest skill add dual-signal-analyzer
```

### 验证安装
```bash
okx skill list | grep dual-signal
```

## 📖 使用方法

### 基本命令
```bash
# 分析单个交易对（双指标验证）
okx skill dual-signal-analyzer analyze --symbol BTC/USDT

# 批量扫描多个交易对
okx skill dual-signal-analyzer scan --symbols BTC/USDT,ETH/USDT,SOL/USDT,BNB/USDT

# 获取详细风险报告
okx skill dual-signal-analyzer risk --symbol BTC/USDT --detail full
```

### 参数说明
- `--symbol`: 交易对符号 (默认: BTC/USDT)
- `--symbols`: 交易对列表，逗号分隔（最多10个）
- `--timeframe`: 时间周期 (默认: 1h, 可选: 15m, 30m, 1h, 4h, 1d)
- `--confidence`: 最小置信度阈值 (默认: 70, 范围: 50-95)
- `--risk-profile`: 风险偏好 (默认: balanced, 可选: conservative, balanced, aggressive)

## 📊 输出示例

### 单个分析输出
```json
{
  "skill": "dual-signal-analyzer",
  "version": "1.0.0",
  "timestamp": "2024-01-15T10:30:00Z",
  "symbol": "BTC/USDT",
  "analysis": {
    "signal": "BUY",
    "confidence": 85,
    "riskLevel": "LOW",
    "verification": "DOUBLE_CONFIRMED",
    "indicators": {
      "RSI": {"value": 28.5, "signal": "OVERSOLD_BUY", "strength": 88},
      "MACD": {"signal": "GOLDEN_CROSS", "strength": 82, "histogram": "POSITIVE"}
    },
    "recommendation": "双指标强烈确认买入信号，建议中等仓位"
  }
}
```

### 批量扫描输出
```json
{
  "skill": "dual-signal-analyzer",
  "version": "1.0.0",
  "timestamp": "2024-01-15T10:30:00Z",
  "scanSummary": {
    "totalSymbols": 8,
    "analyzed": 8,
    "buySignals": 3,
    "sellSignals": 1,
    "holdSignals": 4,
    "doubleConfirmed": 2,
    "topOpportunities": [
      {"symbol": "SOL/USDT", "signal": "STRONG_BUY", "confidence": 92, "riskScore": 25},
      {"symbol": "ETH/USDT", "signal": "BUY", "confidence": 78, "riskScore": 35}
    ]
  }
}
```

## 🔧 技术架构

### 依赖技能
- **okx-cex-market**: 获取实时市场数据和K线
- **okx-cex-trade**: 执行交易操作（可选，用于自动化交易）

### 分析流程
1. **数据获取** → 通过okx-cex-market获取实时数据
2. **指标计算** → 并行计算RSI和MACD指标
3. **信号生成** → 独立生成两个指标信号
4. **交叉验证** → 验证信号一致性，计算综合置信度
5. **风险评估** → 分析市场条件和信号强度
6. **建议输出** → 生成交易建议和风险管理方案

### 算法特点
- **RSI策略**: 14日周期，动态超卖/超买阈值
- **MACD策略**: 12/26/9标准参数，金叉/死叉检测
- **验证逻辑**: 双指标一致时增强信号，冲突时提示风险
- **风险模型**: 波动率分析 + 趋势一致性评估

## ⚠️ 风险提示

### 交易风险说明
- 本技能提供的信号基于历史数据和技术分析
- 加密货币市场波动性极高，存在重大风险
- 所有交易决策应由用户自行负责

### 使用限制
- 需要有效的OKX账户和API权限
- 依赖okx-cex-market技能的可用性
- 网络延迟可能影响实时性

## 📞 支持与反馈

### 问题报告
如遇到技术问题或需要支持：
- 提交Issue: https://github.com/Lobster-Tempo/trading-strategy-skill/issues
- 联系开发者: yu230650@github.com

### 功能建议
欢迎提出改进建议：
- 新指标添加请求
- 分析参数调整
- 输出格式优化
- 性能改进建议

## 📝 版本记录

### v1.0.0 (2024-01-15)
- 🎉 初始版本发布
- ✅ RSI + MACD双指标分析
- ✅ 智能风险评分系统
- ✅ 批量机会扫描功能
- ✅ OKX技能市场集成
- ✅ 完整的文档和示例

## 🔒 安全与合规

### 数据安全
- 不存储用户API密钥或交易数据
- 所有分析在请求时实时进行
- 符合OKX API使用规范

### 合规声明
- 本技能仅提供技术分析工具
- 不提供财务建议或投资指导
- 用户应遵守当地法律法规

## 📄 许可证
MIT License

## ⚠️ 免责声明
本技能提供的分析结果仅供参考，不构成任何投资建议。加密货币交易存在极高风险，可能造成资金损失。用户应独立判断并承担所有交易风险。

---

**技能ID**: `dual-signal-analyzer`  
**版本**: 1.0.0  
**最后更新**: 2024-01-15  
**状态**: ✅ 生产就绪  
**开发者**: 菊中菊  
**依赖**: okx-cex-market, okx-cex-trade  
**唯一标识**: 双指标交叉验证分析器