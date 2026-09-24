---
name: okx-execution-vortex
description: 接收交易信号后安全执行现货/永续/交割/期权订单，支持 OCO、网格、DCA、仓位管理及二次确认风控。依赖 market/trade/portfolio/bot 基础 Skill。
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

# OKX 漩涡执行器 (okx-execution-vortex)

## Description

安全、智能的交易执行层，专为 AI Agent 设计。接收交易请求后，经过严格风险检查和二次确认，自动执行现货、永续、交割、期权的下单、改单、撤单、止盈止损等操作。

**核心流程**：
1. **行情查询**：获取最新价格、深度等市场数据
2. **仓位计算**：根据账户余额和风险参数计算最优仓位
3. **二次确认**：输出完整交易计划并等待用户确认
4. **安全执行**：检查保证金率、余额、系统状态后执行
5. **结果返回**：返回订单详情和执行状态

**高级功能**：
- **OCO订单**：止盈止损同时设置
- **网格交易**：震荡区间自动交易（依赖 `okx-cex-bot`）
- **DCA定投**：定期分批买入
- **仓位管理**：自动调整杠杆和保证金

**依赖 Skill**：`okx-cex-market`, `okx-cex-trade`, `okx-cex-portfolio`, `okx-cex-bot`

## Activation Rules (触发词)

当用户输入以下模式时，激活本 Skill：

- **直接执行**：
  - “执行买入 BTC 1000U”
  - “对 BTC-USDT-SWAP 开多 5 张”
  - “执行 RSI 信号：买入 ETH”
  - “市价卖出 SOL 全部仓位”
  - “执行止损单，价格跌破 50000”
  - “执行止盈单，价格涨到 52000”

- **高级执行**：
  - “网格下单 BTC-USDT，区间 50000-52000，10 格”
  - “DCA 买入 ETH 每周 500U”
  - “OCO 订单：BTC 现价 51000，止盈 52000，止损 50000”
  - “批量执行这三个币种各 500U”
  - “切换 BTC-USDT-SWAP 杠杆到 10x”

- **管理操作**：
  - “撤销所有挂单”
  - “修改订单 #12345 价格为 51000”
  - “平掉 BTC 全部仓位”
  - “检查执行状态”

**禁止直接调用基础 trade Skill**：所有写操作必须通过本 Skill 执行，以确保统一的风险控制。如果用户尝试直接调用 `okx spot place` 等命令，应引导其使用本 Skill。

## Available Commands (可用命令表)

本 Skill 封装 OKX CLI 命令，所有命令均经过二次确认和风险检查：

| 类别 | 关键命令 | 示例 | 说明 |
|------|----------|------|------|
| **现货** | `okx spot place` | `--instId BTC-USDT --side buy --sz 0.01` | 现货下单 |
| | `okx spot cancel/amend` | `--ordId 123456789` | 撤单/改单 |
| **永续** | `okx swap place` | `--instId BTC-USDT-SWAP --side buy --posSide long --sz 1` | 永续合约下单 |
| | `okx swap close` | `--posSide long` | 平仓 |
| | `okx swap leverage` | `--lever 10` | 调整杠杆 |
| **交割** | `okx futures place` | `--instId BTC-USD-240329 --side buy --posSide long` | 交割合约下单 |
| **期权** | `okx option place` | `--instId BTC-USD-240329-50000-C --side buy` | 期权下单 |
| **算法单** | `okx algo place` | `--tpTriggerPx 52000 --slTriggerPx 50000` | OCO/止盈止损 |
| **网格/DCA** | `okx bot grid create` | `--lowerPx 50000 --upperPx 52000 --gridNum 10` | 网格交易 |
| | `okx bot dca create` | `--investment 500 --frequency weekly` | 定期投资 |

**常用参数**：
- `--tgtCcy`: `quote_ccy`(报价货币), `base_ccy`(标的货币)
- `--tdMode`: `isolated`(逐仓), `cross`(全仓)
- `--posSide`: `long`(多), `short`(空)

## Safety Notes (安全说明)

### 强制二次确认
任何写操作执行前必须输出交易计划并等待用户确认，用户必须输入 **Y** 才能继续。

### 风险控制规则
1. **保证金率**：必须 > 150%（可配置）
2. **单笔风险**：不超过账户总资产 2%（可配置）
3. **余额检查**：确保可用余额充足
4. **杠杆限制**：合约杠杆 ≤ 20x
5. **市场状态**：检查交易状态、资金费率、系统维护

### 测试模式
支持 `--demo` 参数进行模拟测试，不实际执行。

## Examples (使用示例)

### 示例 1：现货市价买入
**用户输入**：“执行买入 BTC 1000U”

**Skill 响应**：
```
📈 正在分析交易请求...

1. 查询行情：BTC-USDT 当前价格 50,123.45 USDT
2. 计算数量：1000 USDT ÷ 50,123.45 ≈ 0.01995 BTC
3. 检查风险：账户余额 15,000 USDT，单笔风险 6.67%（超过 2% 阈值）
4. 自动调整：建议买入 300 USDT（0.00598 BTC），风险 2%

⚠️ **交易确认请求**

市场：BTC-USDT
操作：买入
价格：市价
数量：0.00598 BTC（约 300 USDT）
预估手续费：0.15 USDT
账户余额：15,000.00 USDT
单笔风险：2.0%

📊 **风险检查结果**：
✅ 保证金率：不适用（现货）
✅ 余额充足
✅ 系统状态：正常

请确认是否执行？（Y/N）
```

用户输入 `Y` 后执行：
```bash
okx spot place --instId BTC-USDT --side buy --sz 0.00598 --tgtCcy quote_ccy
```

**返回结果**：
```
✅ 订单执行成功！
订单ID：123456789
成交均价：50,125.12 USDT
实际数量：0.00598 BTC
手续费：0.15 USDT
成交时间：2026-04-09 15:30:25
```

**更多示例**：查看 `examples/` 文件夹获取完整示例（永续合约、网格交易、DCA定投等）。


## Preflight (前置检查)

执行前自动检查以下条件：

### 必需条件
- OKX CLI 已安装 (`okx` 命令可用)
- 有效的 API 配置（含交易权限）
- 网络连接正常
- 依赖 Skill 已安装：`okx-cex-market`, `okx-cex-trade`, `okx-cex-portfolio`, `okx-cex-bot`

### 环境检查
- 市场交易状态正常
- 资金费率无异常
- 无重大系统故障

## 版本记录
- v1.0.0 (2026-04-09)：初始版本

## 链接
- 官方文档：https://github.com/okx/agent-trade-kit
- 技能广场：https://github.com/okx/agent-skills