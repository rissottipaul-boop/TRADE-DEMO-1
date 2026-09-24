---
name: okx-review-prism
description: 自动拉取历史交易记录，计算绩效指标，结合当时行情进行结构化复盘并输出改进建议。依赖 portfolio/market/trade。
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

# OKX 棱镜复盘师 (okx-review-prism)

## Description

交易复盘工具，自动拉取历史交易记录，计算绩效指标，结合当时行情进行结构化复盘并输出改进建议。

**核心功能**：
- 自动拉取历史交易记录（订单、成交、持仓）
- 计算胜率、盈亏比、最大回撤等关键指标
- 结合交易时的市场环境进行分析
- 输出结构化复盘报告和改进建议

**依赖 Skill**：`okx-cex-portfolio`, `okx-cex-market`, `okx-cex-trade`

## Activation Rules (触发词)

当用户输入以下模式时，激活本 Skill：

- **时间范围复盘**：
  - "复盘我最近7天交易"
  - "分析我上个月的交易表现"
  - "查看本周的交易记录"
  - "统计我今年的交易数据"

- **特定交易复盘**：
  - "分析BTC这笔亏损单"
  - "复盘订单 #123456789"
  - "查看ETH那笔盈利交易的详情"
  - "分析我最大的亏损交易"

- **品种与策略复盘**：
  - "复盘我的BTC所有交易"
  - "分析我的网格策略表现"
  - "查看永续合约的交易记录"
  - "复盘我的DCA投资计划"

- **绩效报告**：
  - "输出本月绩效报告"
  - "生成季度交易总结"
  - "查看我的交易胜率"
  - "分析我的风险收益比"

- **对比分析**：
  - "对比我现货和合约的表现"
  - "分析不同时间段的交易差异"
  - "比较我不同策略的盈利能力"

**纯只读操作**：本 Skill 仅读取历史数据进行分析，不执行任何交易操作，无需二次确认。

## Available Commands (可用命令表)

| CLI 命令 | 参数示例 | 说明 |
|---------|---------|------|
| `okx spot orders` | `--state filled --after 2024-01-01` | 现货历史订单 |
| `okx spot fills` | `--instId BTC-USDT --after 2024-01-01` | 现货成交记录 |
| `okx swap orders` | `--state filled --after 2024-01-01` | 永续历史订单 |
| `okx swap fills` | `--instId BTC-USDT-SWAP --after 2024-01-01` | 永续成交记录 |
| `okx account bills` | `--type trade --after 2024-01-01` | 账户账单 |
| `okx market candles` | `--instId BTC-USDT --bar 1h --after 2024-01-01` | 历史K线数据 |
| `okx market indicator` | `--instId BTC-USDT --indicator RSI --period 14` | 历史技术指标 |
| `okx market funding-rate` | `--instId BTC-USDT-SWAP --after 2024-01-01` | 历史资金费率 |

## Safety Notes (安全说明)

### 纯只读操作
- 仅读取历史数据，不执行任何交易操作
- 无需二次确认
- 敏感信息脱敏显示

### 数据准确性
- 历史数据可能存在1-5分钟延迟
- 数据完整性取决于OKX API
- 计算结果可能与交易所统计有小幅差异

### 使用建议
- 建议定期复盘（每周/每月）
- 客观分析亏损交易
- 将改进建议落实到后续交易中

## Examples (使用示例)

完整示例见 `examples/` 文件夹：

- `weekly_review.md` - 最近7天交易复盘
- `loss_analysis.md` - 亏损交易分析
- `btc_deep_review.md` - BTC深度复盘
- `monthly_report.md` - 月度绩效报告

示例包含完整的数据查询命令序列、绩效指标计算和结构化报告输出。

## Preflight (前置检查)

### 必需条件
- OKX CLI 已安装 (`okx` 命令可用)
- 有效的 API 配置（含历史数据读取权限）
- 网络连接正常
- 依赖 Skill 已安装：`okx-cex-portfolio`, `okx-cex-market`, `okx-cex-trade`

### 数据质量检查
- 查询时间范围合理性
- 数据完整性检查
- 异常值检测

## 绩效指标体系

计算关键绩效指标，包括胜率、盈亏比、最大回撤、夏普比率等。基于情绪、执行、风控三个维度进行综合评分（0-100分），输出改进建议。

## 输出格式规范

输出结构化复盘报告，包含绩效摘要、关键指标、三维度分析和改进建议。支持 Markdown 格式（人类可读）和 JSON 格式（程序化接口）。

## 版本记录
- v1.0.0 (2026-04-09)：初始版本，支持基础复盘和三维度分析
- v1.1.0 (2026-04-09)：增加高级绩效指标，优化分析算法
- v1.2.0 (2026-04-09)：增加可视化图表描述，支持策略对比

## 贡献指南
欢迎提交 Issue 和 Pull Request。请确保：
1. 所有新指标都有明确的定义和计算公式
2. 添加对应的数据分析算法
3. 更新 Examples 部分
4. 通过数据隐私和安全审查

## 支持与反馈
- 官方文档：https://github.com/okx/agent-trade-kit
- 复盘方法论：https://github.com/okx/agent-skills/discussions
- 数据问题：data-feedback@okx.com
- 分析建议：analysis-feedback@okx.com