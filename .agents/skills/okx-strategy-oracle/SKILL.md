---
name: okx-strategy-oracle
description: 基于实时行情+指标+链上数据生成交易策略，提供三档风险方案、参数优化及 bot 配置，支持一键转执行。依赖 market/portfolio。
license: MIT
metadata:
  author: web3_xiaoyao
  version: 1.0.0
  homepage: https://www.okx.com
  requires:
    install:
      bins: okx
    kind: package
    package: npm
---

# OKX 神谕策略师 (okx-strategy-oracle)

## Description

高级交易策略生成器，整合多维数据源生成交易策略，提供三档风险方案和参数优化建议。

**核心功能**：
- 多维度数据分析（价格、指标、衍生品等）
- 趋势评分和策略类型识别
- 三档风险方案（保守/稳健/激进）
- 参数优化和 bot 配置生成
- 一键转执行（通过 `okx-execution-vortex`）

**依赖 Skill**：`okx-cex-market`, `okx-cex-portfolio`

## Activation Rules (触发词)

当用户输入以下模式时，激活本 Skill：

- **策略生成**：
  - "生成 BTC 策略"
  - "ETH 交易策略分析"
  - "SOL 未来一周走势预测"
  - "评估 BTC 当前市场状态"
  - "综合研判 ETH-USDT-SWAP"

- **参数优化**：
  - "优化马丁格尔参数"
  - "RSI 最佳参数计算"
  - "优化网格策略 BTC 震荡区间"
  - "计算 DCA 最优投资频率"
  - "EMA 交叉策略参数回测"

- **特定策略**：
  - "RSI 抄底策略"
  - "网格策略 BTC 震荡区间 50000-52000"
  - "AHR999 定投计划"
  - "趋势跟踪策略 BTC"
  - "套利机会扫描"

- **风险评估**：
  - "评估当前市场风险等级"
  - "生成三档风险方案 BTC"
  - "保守型 ETH 投资策略"
  - "激进型 SOL 交易计划"

**禁止直接执行**：本 Skill 仅生成策略建议，不执行任何交易操作。所有执行必须通过 `okx-execution-vortex` 完成。

## Available Commands (可用命令表)

本 Skill 封装以下 OKX CLI 命令进行数据采集和分析：

### 市场数据查询
| CLI 命令 | 参数示例 | 说明 |
|---------|---------|------|
| `okx market candles` | `--instId BTC-USDT --bar 4h --limit 100` | K线数据 |
| `okx market tickers` | `--instId BTC-USDT` | 实时行情 |
| `okx market indicator` | `--instId BTC-USDT --indicator RSI --period 14` | 技术指标（RSI/MACD/EMA等70+） |
| `okx market funding-rate` | `--instId BTC-USDT-SWAP` | 资金费率 |
| `okx market open-interest` | `--instId BTC-USDT-SWAP` | 未平仓合约 |
| `okx account balance` | `--ccy USDT` | 账户余额 |
| `okx spot positions` | `--instId BTC-USDT` | 现货持仓 |
| `okx bot grid params` | `--instId BTC-USDT --strategy moderate` | 网格参数生成（依赖okx-cex-bot） |
| `okx bot dca params` | `--instId ETH-USDT --frequency weekly` | DCA参数生成 |
| `okx external coinglass` | `--metric funding_rates --symbol BTC` | CoinGlass数据（可选） |

## Safety Notes (安全说明)

### 仅建议不执行
- 所有输出均为策略建议，不构成投资推荐
- 执行必须通过 `okx-execution-vortex` 进行二次确认
- 历史表现不代表未来结果

### 核心风险警告
- **强趋势警告**：评分 >+80 或 <-80 时避免逆势操作
- **高波动警告**：ATR(14) > 2倍平均值时缩小仓位
- **衍生品风险**：资金费率 > 0.1%时警告空头成本
- **杠杆限制**：根据趋势评分提供杠杆建议（3x-20x）

### 数据免责
- 数据来自OKX和第三方API，不保证100%准确
- 免费数据可能存在1-5分钟延迟
- 外部数据可能需要API Key（本Skill不存储密钥）

## Examples (使用示例)

完整示例见 `examples/` 文件夹：

- `btc_trend_strategy.md` - BTC 趋势跟踪策略
- `eth_range_strategy.md` - ETH 震荡网格策略
- `swap_arbitrage.md` - 永续合约套利策略
- `rsi_optimization.md` - RSI 参数优化
- `three_tier_risk.md` - 三档风险方案

示例包含完整CLI调用序列、6维评分计算、结构化策略卡片和JSON格式输出。

## Preflight (前置检查)

### 必需条件
- OKX CLI 已安装 (`okx` 命令可用)
- 有效的 API 配置（含市场数据权限）
- 网络连接正常
- 依赖 Skill 已安装：`okx-cex-market`, `okx-cex-portfolio`

### 数据质量检查
- K线数据完整性
- 技术指标计算结果合理性
- 数据新鲜度（延迟不超过5分钟）

## 输出格式规范

### JSON 格式（程序化接口）
```json
{
  "strategy_id": "BTC_20240115_TREND",
  "instrument": "BTC-USDT",
  "score": 72.5,
  "trend_direction": "bullish",
  "risk_levels": {
    "conservative": { /* 参数 */ },
    "moderate": { /* 参数 */ },
    "aggressive": { /* 参数 */ }
  },
  "indicators": { /* 技术指标值 */ },
  "entry_zones": [ /* 入场区间 */ ],
  "exit_zones": [ /* 出场区间 */ ],
  "execution_plan": "okx-execution-vortex 命令"
}
```

### Markdown 策略卡片（人类可读）
输出结构化策略卡片，包含：
- 6维评分表格
- 三档风险方案（保守/稳健/激进）
- 技术指标状态
- 执行计划（CLI命令）
- 风险提示

完整示例见 `examples/` 文件夹。

## 版本记录
- v1.0.0 (2026-04-09)：初始版本，支持6维评分和三档风险方案
- v1.1.0 (2026-04-09)：增加70+技术指标支持，优化参数优化算法
- v1.2.0 (2026-04-09)：集成外部数据源，增强链上分析

## 贡献指南
欢迎提交 Issue 和 Pull Request。请确保：
1. 所有新指标都有完整的回测数据支持
2. 添加对应的风险警告逻辑
3. 更新 Examples 部分
4. 通过数据准确性验证

## 支持与反馈
- 官方文档：https://github.com/okx/agent-trade-kit
- 策略讨论：https://github.com/okx/agent-skills/discussions
- 数据问题：data-feedback@okx.com
- 算法建议：strategy-algo@okx.com