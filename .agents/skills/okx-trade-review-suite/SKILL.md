---
name: okx-trade-review-suite
description: "交易复盘全家桶 · Pro。一个 Skill 集成 8 大复盘维度：盈亏归因拆解、权益曲线回放、最大回撤体检、连胜连亏期望值、vs Buy&Hold/SMA/DCA 的 alpha 计量、追高/割肉/报复交易行为指纹、手续费与 maker-taker 审计、持仓时长 × PnL 相关性，以及日/周/月自动聚合报告。只读，不下单，不转账。适用场景：复盘我的交易、我哪里做错了、我比躺平多赚多少、一键周报、我是不是总追高、手续费吃了多少、最大回撤多少、哪个币最赚钱、review my trades、trading review、performance check、是不是该转定投、我的策略还能不能继续、下月怎么改进、trade audit、psychology check、alpha analysis、drawdown review、fee audit、win rate analysis。关键词：复盘、review、归因、attribution、权益曲线、equity curve、MDD、最大回撤、连胜连亏、streak、胜率、期望值、凯利、Kelly、alpha、Sharpe、Sortino、IR、benchmark、追高、割肉、报复交易、FOMO、死扛、behavior、手续费、fees、maker、taker、持仓时长、hold duration、日报、周报、月报、digest、digest summary。"
license: MIT
metadata:
  author: okx-skill
  version: "1.0.0"
  homepage: "https://www.okx.com"
  categories: ["复盘"]
  agent:
    requires:
      bins: ["okx"]
    install:
      - id: npm
        kind: node
        package: "@okx_ai/okx-trade-cli"
        bins: ["okx"]
        label: "Install okx CLI (npm)"
  bundled_skills:
    - okx-review-attribution
    - okx-review-equity-curve
    - okx-review-streak
    - okx-review-benchmark
    - okx-review-behavior
    - okx-review-fees-audit
    - okx-review-hold-duration
    - okx-review-daily-digest
---

# 交易复盘全家桶 · Pro — 8 个专业维度一次打包

**给你的交易生涯装一面全景镜。**

不用装 8 个 Skill，不用记一堆触发词。说一句"复盘一下"、"我哪里做错了"、"给我一份周报"——**Suite 自动路由到最合适的专项 Skill 并出报告**。

- 🧩 **8 个专项复盘 Skill，一次安装**
- 📊 **只读零风险** — 从不下单、撤单、转账
- 🗣️ **自然语言路由** — 不用记 skill 名
- ⏰ **定时订阅** — 日报 / 周报 / 月报全自动
- 📝 **结构化 Markdown 报告** — 可存档、可分享、可对比

---

## 💎 8 大复盘维度一览

| # | 子 Skill | 解决什么问题 | 典型触发语 |
|---|---------|-------------|-----------|
| 1 | [`okx-review-attribution`](skills/okx-review-attribution/) | 钱从哪赚、从哪亏？币种/方向/时段拆解 | "我这个月靠哪个币赚钱"、"亏损集中在哪里" |
| 2 | [`okx-review-equity-curve`](skills/okx-review-equity-curve/) | 权益曲线回放 + 最大回撤体检 | "最大回撤多少"、"水下多久了"、"画一下我的曲线" |
| 3 | [`okx-review-streak`](skills/okx-review-streak/) | 连胜连亏 & 期望值 & 凯利仓位 | "最近是不是连亏了"、"我的胜率怎么样" |
| 4 | [`okx-review-benchmark`](skills/okx-review-benchmark/) | vs Buy&Hold / SMA / DCA 算 alpha | "我比 BTC 躺平多赚多少"、"Sharpe 怎么样" |
| 5 | [`okx-review-behavior`](skills/okx-review-behavior/) | 追高 / 割肉 / 报复交易行为指纹 | "我是不是总追高"、"亏了之后是不是报复交易" |
| 6 | [`okx-review-fees-audit`](skills/okx-review-fees-audit/) | 手续费 + maker/taker 审计 | "手续费吃了多少"、"能省多少" |
| 7 | [`okx-review-hold-duration`](skills/okx-review-hold-duration/) | 持仓时长 × PnL 相关性 | "我是拿不住还是死扛"、"最佳持仓多久" |
| 8 | [`okx-review-daily-digest`](skills/okx-review-daily-digest/) | 日/周/月综合复盘聚合器 | "一份周报"、"本月总结"、"所有维度都扫一遍" |

---

## 🎯 为什么需要这个 Suite？

**散户最常见的 3 个死法**：
1. 不复盘 → 一年下来不知道为啥亏 → 下一年继续亏
2. 复盘太累 → 翻账单 → Excel → 心态崩了 → 放弃
3. 只看总收益 → 看不到"拿不住盈利、死扛亏损"的结构问题

**Suite 一次解决**：
- 🤖 **自然语言问** — "我最近是不是追高了？" → 自动跑 behavior
- 📊 **结构化报告** — 每份都有洞察 + 改进建议（带数字）
- 🗓️ **自动订阅** — 每周一 09:00 一份周报，躺着复盘
- 🎯 **综合建议 Top 3** — 按"风控 > 行为 > 策略"排优先级

---

## 📊 一眼看懂：总报告输出示例

```
📰 交易复盘周报 · 2026-04-05 ~ 2026-04-11
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
📌 TL;DR（本周 3 件最重要的事）
1. ⚠️ 报复交易 4 次，4 次全亏，立即加暂停规则
2. ✅ alpha +6.7% vs BH-BTC，主动交易本周有效
3. 🟡 MDD 触黄线 (-22%)，水下 20 天接近恢复

💰 总览卡
权益：$10,000 → $12,485 (+24.9%, 年化 89%)
胜率：55.3%   盈亏比：1.87   期望值：+$102/笔

📊 归因 | 📈 权益曲线 | 🎯 Streak | ⚖️ 基准
🧠 行为 | 💸 费用 | ⏱️ 持仓时长
（每块 2-3 行关键指标）

🎯 本周 Top 3 行动
[风控] 亏损后强制暂停 30 分钟 ← behavior+streak
[行为] 止盈分批（+2%/+4%/追踪） ← hold-duration
[策略] Maker 比例提到 60%，月省 $286 ← fees-audit
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
[profile: live]
```

---

## ⚙️ 可配置参数（Suite 级）

### 入口参数

| 参数 | 默认值 | 可选范围 | 说明 |
|------|--------|----------|------|
| 默认周期 | 周报 | 日/周/月 | 聚合报告粒度 |
| 默认 profile | 询问 | live/demo | 实盘 or 模拟 |
| 路由模式 | 智能 | 智能/显式 | 智能=自动路由子 skill；显式=必须指定 |
| 详细度 | 标准 | 简要/标准/完整 | 输出详尽程度 |
| 语言 | 中文 | 中文/英文 | 报告语言 |

### 订阅参数

| 参数 | 默认值 | 可选范围 | 说明 |
|------|--------|----------|------|
| 日报 | 关 | 开/关 | 每日 09:00 |
| 周报 | 开（默认推荐）| 开/关 | 周一 09:00 |
| 月报 | 关 | 开/关 | 每月 1 号 09:00 |
| 红线告警 | 开 | 开/关 | 报复交易/MDD 触线时立即推送 |
| 落盘 | 关 | 开/关 | 报告写到 `./reviews/` |

### 子 Skill 级参数

8 个子 skill 各自的参数（追高阈值、桶边界、基准列表等）保留完整可配能力。通过 Suite 修改：

```
"把追高阈值改成 1.5%"   → 修改 behavior 子 skill
"基准加上 ETH"           → 修改 benchmark 子 skill
"桶边界改为 30m/2h/12h"  → 修改 hold-duration 子 skill
```

---

## 🎬 首次使用：3 步极速启动

```
Step 1  安装 + 凭证
   npm install -g @okx_ai/okx-trade-cli
   okx config init

Step 2  确认 profile
   "用 live（实盘）复盘"  或  "用 demo（模拟）试试"

Step 3  问一句话
   "帮我看看这个月的交易"     → 路由到归因
   "给我一份周报"            → 路由到 daily-digest
   "我是不是总追高"          → 路由到 behavior
   "我跑赢 BTC 了吗"         → 路由到 benchmark
```

就这样。不用学命令行，不用配参数，首次就给你一份可读的报告。

---

## 🗺️ 智能路由表

自然语言输入 → 自动选择子 skill：

| 用户问法（关键词） | 路由目标 |
|-------------------|---------|
| "赚钱"、"亏钱"、"哪个币"、"归因"、"拆解" | `okx-review-attribution` |
| "曲线"、"回撤"、"MDD"、"水下"、"新高" | `okx-review-equity-curve` |
| "连胜"、"连亏"、"胜率"、"期望值"、"凯利" | `okx-review-streak` |
| "alpha"、"Sharpe"、"跑赢"、"躺平"、"定投对比" | `okx-review-benchmark` |
| "追高"、"割肉"、"报复"、"FOMO"、"死扛" | `okx-review-behavior` |
| "手续费"、"maker"、"taker"、"费率" | `okx-review-fees-audit` |
| "拿不住"、"持仓时长"、"短线"、"长持" | `okx-review-hold-duration` |
| "日报"、"周报"、"月报"、"总结"、"全部" | `okx-review-daily-digest` |

**兜底规则**：意图不清时优先调 `daily-digest` 给出全维度简报，再让用户选择下钻。

---

## 🔐 凭证与 Profile 校验（所有子 Skill 共用）

### Step A — 凭证检查

```bash
okx config show
```

无配置 → 引导 `okx config init`，**停止一切操作**。

### Step B — Profile 确认（必填）

| 值 | 模式 | 资金 |
|---|---|---|
| `live` | 实盘 | 真实资金 |
| `demo` | 模拟盘 | 模拟资金 |

当前消息有明确意图 → 使用并告知；无声明 → 查上下文；无上下文 → 问 "live 还是 demo？" 等回答。

### 401 认证失败

立即停止 → 告知 → 引导用户本地改 `~/.okx/config.toml` → 跑 `okx config show` 验证 → 再重试。**绝不要求用户把密钥粘到对话里**。

---

## 🔁 核心工作流（Suite 级）

```
用户输入
  ↓
① 凭证 / Profile 校验
  ↓
② 意图识别 → 路由到某个子 Skill（或 daily-digest 聚合器）
  ↓
③ 首次调用时 → 引导配置子 Skill 参数（默认即可跳过）
  ↓
④ 执行子 Skill 分析流水线：
     ├─ 拉数据（positions-history / bills / candles / fees）
     ├─ 数据健康校验（红黄线）
     ├─ 分析（分桶/行为/序列/曲线/归因/对比）
     └─ 生成中文 Markdown 报告
  ↓
⑤ 输出报告
  ↓
⑥ 若用户订阅周报/月报 → 注册定时任务
```

---

## 🛡️ 数据健康红黄线（跨所有子 Skill）

### 🔴 红线 — 拒绝出报告

1. 凭证未配置 / 401
2. 所有子 skill 均失败
3. Profile 未确认

### 🟡 黄线 — 出报告带警告

1. 某子 skill 数据覆盖率 <80% → 报告顶部标注
2. 时间窗 <7 天 → 样本不足提醒
3. 检测到报复交易 ≥3 次、MDD >20% 等关键红线 → 顶部置顶告警
4. Demo 模式 → 标 "⚠️ Demo 数据仅供演示"

---

## ⏸️ 暂停 / 恢复 / 重置

| 用户说 | 行为 |
|--------|------|
| "暂停周报" | 停订阅保配置 |
| "恢复周报" | 重启 |
| "暂停所有订阅" | 全停 |
| "去掉 behavior 模块" | 聚合报告不再含该块 |
| "重置" | 全部回默认 |
| "改追高阈值为 3%" | 修改 behavior 参数 |

---

## 🚧 异常处理

| 场景 | 处理方式 |
|------|---------|
| API 失败 | 30s 重试 3 次 |
| 单子 skill 失败 | 跳过 + 报告标 "未生成模块 X" |
| 数据稀疏（新账户/Demo）| 标"样本不足" + 建议放宽时间窗 |
| 参数冲突 | 以 Suite 级参数为准 |
| 落盘失败 | 降级为对话输出 |
| 路由歧义 | 优先 daily-digest，让用户二选下钻 |

---

## 🧭 总工作流程

```
          ┌────────── "复盘" / "周报" / "我是不是追高了" ──────────┐
          │                                                      │
          ▼                                                      │
   ┌──────────────┐                                              │
   │ Suite 路由层  │  ← 智能识别意图 + Profile 校验              │
   └──────┬───────┘                                              │
          │                                                      │
  ┌───────┼────────┬────────┬─────────┬────────┬────────┬───────┤
  ▼       ▼        ▼        ▼         ▼        ▼        ▼       ▼
归因  权益曲线   streak  benchmark  behavior  fees  hold-dur  digest
  └──┬────┴────────┴────────┴─────────┴────────┴────────┴───────┘
     ▼
共享一次数据拉取（positions-history / bills / candles / fees）
     ▼
子 skill 各自分析 → 生成子报告
     ▼
Suite 汇总 → 输出中文 Markdown
```

---

## 🔗 子 Skill 详细入口

点进每个子 skill 查看完整 12 章节文档（算法、参数、案例）：

- [📊 okx-review-attribution](skills/okx-review-attribution/SKILL.md) — 盈亏归因
- [📈 okx-review-equity-curve](skills/okx-review-equity-curve/SKILL.md) — 权益曲线
- [🎯 okx-review-streak](skills/okx-review-streak/SKILL.md) — 连胜连亏
- [⚖️ okx-review-benchmark](skills/okx-review-benchmark/SKILL.md) — 基准对比
- [🧠 okx-review-behavior](skills/okx-review-behavior/SKILL.md) — 行为指纹
- [💸 okx-review-fees-audit](skills/okx-review-fees-audit/SKILL.md) — 费用审计
- [⏱️ okx-review-hold-duration](skills/okx-review-hold-duration/SKILL.md) — 持仓时长
- [📰 okx-review-daily-digest](skills/okx-review-daily-digest/SKILL.md) — 日/周/月聚合

---

## ❓ 常见问题

**Q: 和单独装 8 个 skill 有什么区别？**
A: 一次性装 Suite 即可获得全部能力，不用记 8 个 skill 名。智能路由会根据你的自然语言自动选对应子 skill。想单独调用某个时，直接说 "用 attribution" 或直接点开子 skill 入口。

**Q: Suite 比单装 daily-digest 多什么？**
A: `daily-digest` 只做**定时聚合报告**。Suite 还包括**按意图路由到单一维度的专项分析**。比如你只问"手续费多少"，Suite 直接调 fees-audit 给你专项报告，不必跑全部 7 个模块。

**Q: 全部 8 个 skill 真的只读吗？**
A: 是。Suite 和所有子 skill 只调 `account positions-history` / `account bills` / `account balance` / `account config` / `account fees` / `market candles`，**绝不下单、撤单、转账、改仓位模式**。哪怕你的 API Key 意外开了交易权限也不会被使用。

**Q: 我想关掉某些子 skill，能行吗？**
A: 能。说 "去掉 benchmark" 或 "只保留 attribution + behavior + fees-audit"，Suite 会记住配置。聚合报告也会自动跳过关闭的模块。

**Q: 订阅周报会打扰我吗？**
A: 默认周一 09:00 一次，可随时 "暂停" 或改时间。想要更轻量：只开"红线告警"，只有检测到报复交易/大回撤时才推送。

**Q: 能保存历史报告做同比环比吗？**
A: 开启"落盘"选项后，报告按日期写入 `./reviews/`。后续版本（v1.1）将内置"同比/环比"章节自动对比上期。

**Q: 我有多个 API Key / 子账户，怎么分别复盘？**
A: 通过 `--profile` 区分。`okx config` 里为每个账户配独立 profile，Suite 会在每次调用前询问。

**Q: 报告里的数字准确度能保证吗？**
A: 基于 OKX 官方 API 原始数据，`realizedPnl` 等字段直接取自 positions-history 返回。**唯一估算的部分**是："若全用 maker 可节省 $X"，这是理论上限，实盘扣除机会成本后会略低。

---

## 📜 MCP 工具映射（Suite 汇总）

Suite 及其子 Skill 仅调用**只读** MCP 工具：

| 分类 | MCP 工具 |
|------|---------|
| 账户账单/持仓 | `account_get_bills` / `account_get_bills_archive` / `account_get_balance` / `account_get_config` |
| 交易历史 | `account_get_positions_history` / `account_get_trade_fee` |
| 行情 | `market_get_candles` / `market_get_ticker` |

**绝不调用**任何下单、撤单、转账、改仓位模式的写入类工具。

---

## 📂 Skill 包结构

```
okx-trade-review-suite/
├── SKILL.md                       ← 本文件（Suite 总入口）
├── references/                    ← Suite 级规则
└── skills/                        ← 内嵌 8 个子 skill
    ├── okx-review-attribution/
    │   ├── SKILL.md
    │   └── references/REFERENCE.md
    ├── okx-review-equity-curve/
    ├── okx-review-streak/
    ├── okx-review-benchmark/
    ├── okx-review-behavior/
    ├── okx-review-fees-audit/
    ├── okx-review-hold-duration/
    └── okx-review-daily-digest/
```

每个子 skill 独立可用（配合 `skill add okx-review-xxx`），打包进 Suite 是为了**一次安装、统一路由、减少心智负担**。

---

## ⚠️ 免责声明

本 Skill 合集仅供交易复盘参考与学习，**不构成任何投资建议**。所有分析基于历史数据，历史表现不代表未来收益。加密资产交易存在高波动与本金损失风险，使用者需自担所有决策后果。作者与本 Skill 合集不对因参考本报告产生的任何损失承担责任。

---

## 📜 License

MIT · 开源共享，欢迎 fork 与二次开发。
