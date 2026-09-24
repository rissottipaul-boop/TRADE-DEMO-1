# Claude Code 环境初始化

本文档仅适用于 Claude Code（MCP 模式）环境的首次设置。

## 创建 CLAUDE.md

在项目目录创建 `CLAUDE.md`：

```markdown
# DCD 双币赢项目

- 始终使用实盘工具：`okx-trade-mcp-live`
- 时间统一 UTC+8，使用中文回复

## 自动交易授权

本项目已授权全自动交易，定时任务可直接执行以下操作，无需用户确认：

1. **自动下单**：`dcd_subscribe` — 按策略规则自动选择产品并下单
2. **自动复盘**：`dcd_get_orders` — 查询结算结果并更新交易记录
3. **自动创建下一期任务**：`create_scheduled_task` — 下单后自动设置到期复盘
4. **自动划转**：`account_transfer` — 如需将 USDG/BTC 从交易账户转到资金账户

## 自动交易硬性约束（不可违反）

即使全自动，以下规则必须严格遵守，违反任一条则**跳过本轮，不下单**：

- PUT 行权价必须低于安全下界（基于预测波动率计算）
- PUT 安全距离 >= 3.5%（事件前后 >= 5%）
- PUT 风险评级必须为 ✅（行权价 < 安全下界）
- PUT 投入 = 全部可用 USDG/USDT
- CALL 投入 = 全部可用 BTC（向下取整 0.0001）
- CALL 行权价 >= 合并有效成本（绝不亏损卖出）
- 每笔下单后必须更新 `交易记录.md`
```

## 配置权限

在 `.claude/settings.local.json` 中添加以下权限（或自动写入）：

```json
{
  "permissions": {
    "allow": [
      "mcp__okx-trade-mcp-live__market_get_index_ticker",
      "mcp__okx-trade-mcp-live__market_get_index_candles",
      "mcp__okx-trade-mcp-live__market_get_candles",
      "mcp__okx-trade-mcp-live__market_get_indicator",
      "mcp__okx-trade-mcp-live__market_get_ticker",
      "mcp__okx-trade-mcp-live__market_get_tickers",
      "mcp__okx-trade-mcp-live__market_get_funding_rate",
      "mcp__okx-trade-mcp-live__option_get_greeks",
      "mcp__okx-trade-mcp-live__option_get_instruments",
      "mcp__okx-trade-mcp-live__account_get_asset_balance",
      "mcp__okx-trade-mcp-live__account_get_balance",
      "mcp__okx-trade-mcp-live__account_transfer",
      "mcp__okx-trade-mcp-live__dcd_get_products",
      "mcp__okx-trade-mcp-live__dcd_subscribe",
      "mcp__okx-trade-mcp-live__dcd_get_orders",
      "mcp__scheduled-tasks__list_scheduled_tasks",
      "mcp__scheduled-tasks__create_scheduled_task"
    ]
  }
}
```

## 注册定时任务

调用 `create_scheduled_task` 注册每日自动交易：

- **taskId**: `dcd-auto-trader`
- **cronExpression**: `5 17 * * *`（每天 17:05 本地时间）
- **description**: `DCD 双币赢全自动交易 v3：每天 17:05 检查到期、复盘、下单`
- **prompt**: 使用 SKILL.md 中"每日执行流程"全文作为 prompt
