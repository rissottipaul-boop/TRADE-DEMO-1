# okx-trade-review-suite · Suite 级路由与配置规则

## 1. 智能路由决策表

当用户输入包含下列关键词时，Suite 按优先级顺序路由到对应子 Skill：

| 优先级 | 关键词簇 | 目标子 Skill |
|------|---------|-------------|
| 1 | 报复交易、连亏后立即、情绪交易 | `okx-review-behavior` + 提前告警 |
| 2 | MDD、最大回撤、水下、新高、曲线 | `okx-review-equity-curve` |
| 3 | 追高、割肉、FOMO、死扛、behavior | `okx-review-behavior` |
| 4 | 连胜、连亏、streak、胜率、凯利、期望值 | `okx-review-streak` |
| 5 | alpha、Sharpe、Sortino、benchmark、躺平、跑赢 | `okx-review-benchmark` |
| 6 | 手续费、fee、maker、taker、滑点 | `okx-review-fees-audit` |
| 7 | 拿不住、持仓时长、hold duration、短线、长持 | `okx-review-hold-duration` |
| 8 | 归因、哪个币赚钱、贡献、拆解、attribution | `okx-review-attribution` |
| 9 | 日报、周报、月报、总结、digest、一键 | `okx-review-daily-digest` |
| 0（兜底） | "复盘"、"review"、无特定维度 | `okx-review-daily-digest`（简要模式）|

冲突处理：
- 同时命中多个 → 优先级数字最小者胜
- 首次对话 + 模糊意图 → `daily-digest` 简要 + 询问是否下钻

## 2. 参数继承

Suite 级参数优先级：**用户当前指令 > Suite 默认 > 子 Skill 默认**。

| Suite 级参数 | 继承到子 Skill |
|-------------|---------------|
| profile | 所有子 Skill |
| 时间窗 | 所有子 Skill |
| 详细度 | 所有子 Skill |
| 语言 | 所有子 Skill |

子 Skill 专有参数（如 behavior 的追高阈值）仅在对应子 Skill 内生效，Suite 透传。

## 3. 订阅调度表

| 订阅类型 | 默认 cron | 调用子 Skill |
|---------|-----------|-------------|
| 日报 | `0 9 * * *` | `daily-digest`（周期=日）|
| 周报 | `0 9 * * 1` | `daily-digest`（周期=周）|
| 月报 | `0 9 1 * *` | `daily-digest`（周期=月）|
| 红线告警 | 实时 | `behavior`（报复检测）+ `equity-curve`（MDD 检测）|

## 4. 红线事件触发告警

| 事件 | 来源 | 动作 |
|------|------|------|
| 报复交易 ≥3 次 | behavior | 立即推送 + 下次聚合报告顶部 |
| MDD 创新低 | equity-curve | 立即推送 |
| 连亏达阈值 | streak | 立即推送 |
| 手续费单日 >$X | fees-audit | 次日报告标注 |

## 5. 数据共享策略

Suite 在一次对话中缓存：
- `positions-history`（时间窗内）
- `bills`（时间窗内，区分 type）
- `balance` 快照
- `market candles`（涉及品种）
- `fees` 档位

共享给所有子 Skill 使用，避免重复拉取。缓存 TTL 默认 5 分钟；用户说 "重新拉数据" 即清缓存。

## 6. 输出格式规则

- Markdown + emoji 分节
- ASCII 图形（条形图/曲线/饼图）
- 所有金额带符号（+/-）
- 所有百分比 1 位小数
- 报告末尾带 `[profile: <live|demo>]` 标签与数据覆盖率

## 7. 路由防歧义规则

| 用户输入 | 预期 | 解决 |
|---------|------|------|
| "复盘 + 多个维度" | 用户要聚合报告 | → `daily-digest` |
| "我上周亏损在哪里" | 归因 | → `attribution` |
| "我上周最大回撤" | 回撤 | → `equity-curve` |
| "我上周追高了吗" + "手续费多少" | 两个问题 | → 分别调 `behavior` + `fees-audit`，分别出报告 |
| 单独"复盘" | 模糊 | → `daily-digest` 简要 + 询问下钻 |

## 8. 版本历史

| 版本 | 日期 | 变更 |
|------|------|------|
| 1.0.0 | 2026-04-12 | 初版：Suite 总入口 + 8 子 skill 路由 |
