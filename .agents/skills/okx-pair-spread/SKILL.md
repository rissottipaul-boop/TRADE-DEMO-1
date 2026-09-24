---
name: okx-pair-spread
license: MIT
description: |
  在 OKX 支持同时对两个 USDT 永续币种做相反方向的市价开仓（一腿做多、一腿做空），形成比值/汇率对冲组合。
  支持后台盯盘止盈止损：支持部分和全部仓位的统一止盈/止损（客户端只支持单一仓位的止盈止损）。用户声明(币种A/币种B)比值（ratio）或组合未实现 PnL 触发条件后，启动一个独立的 Python 守护进程，1 秒级轮询，命中后由代码立即平仓。守护进程独立于 AI 客户端运行，关闭 AI 客户端不影响盯盘；仅在主机关机或进程被强制结束时失效。
  当用户说"汇率对开仓"、"做多做空配对"、"比值对冲"、"pair spread"、"一腿多一腿空"、"平汇率对"、"盯汇率对"、"汇率对止盈止损"、"比值跌破/涨破 X 就平"时使用此技能。
metadata:
  openclaw:
    requires:
      bins: [python]
    os: [win32, darwin, linux]
    install:
      - kind: pip
        package: ccxt
        label: Install ccxt (pip)
---

# 汇率对 双腿市价开平仓

> 一次同时开两个方向相反的 USDT 永续（做多 A + 做空 B），形成比值对冲。
> 入场用市价确保同时到位；双腿失衡自动回滚。
> 平仓支持全部一起平、按 USDT 分别平每腿、或只平一腿。平仓不依赖开仓记录，直接读交易所实时仓位。

```
开仓请求                           平仓请求
    ↓                                 ↓
doctor 双合约环境检查            status 读双腿实时仓位
    ↓                                 ↓
open-preview 冲突/余额/下单校验   close-preview 方向校验 + 平仓计划
    ↓                                 ↓
用户确认                          用户确认
    ↓                                 ↓
open 市价开两腿 + 失衡回滚        close 市价 reduce-only 平腿
    ↓                                 ↓
报告双腿成交 + 当前比值           报告结果 + 残留仓位提示
```

---

## 使用方式

### 首次设置

对 AI 助手说 **"设置汇率对"** 或 **"setup pair spread"**，将自动检测环境并执行初始化流程。

本 skill 与 `okx-maker-entry` 共用同一份 `~/.okx/config.toml`，如果你已经配过 maker-entry，这里可以直接跳过初始化。

### 日常使用

直接说即可，示例：

**开仓**：

- **"BTC 做多 ETH 做空 各 1000U 汇率对开仓"**
- **"做多 BTC 做空 SOL 各 500U 汇率对 10 倍杠杆"**
- **"BTC ETH 汇率对 各 2000U 实盘"**

**查看状态**：

- **"查看 BTC ETH 汇率对状态"**
- **"BTC ETH 当前比值"**

**平仓**：

- **"平 BTC ETH 汇率对"**（全部平）
- **"BTC ETH 汇率对 BTC 平 500U ETH 平 300U"**
- **"BTC ETH 汇率对只平 BTC 500U"**

**盯盘（止盈止损，后台守护进程，所有 AI 客户端通用）**：

- **"盯 BTC ETH 汇率对，比值跌破 27 就全平"**
- **"DOGE SOL 汇率对 组合 PnL 到 -50 或 +80 就平"**
- **"盯着 BTC ETH，比值涨破 28 或 亏 200U 就全平"**
- **"看下我有哪些盯盘"** / **"停止盯盘 watch-XXXX"**（查看 / 停止已启动的盯盘）

---

## 前置条件

1. **AI 客户端**：Claude Code 或 OpenClaw（小龙虾）
2. **Python**：>= 3.9（推荐 3.11+，否则需额外安装 `tomli`）
3. **ccxt**：Python 交易所 SDK
4. **OKX API Key**：已配置 `~/.okx/config.toml`，权限需 Read + Trade
5. **账户设置**：OKX 账户必须是 **单向持仓模式**（Net Mode）

---

## 运行方式

本 skill 纯 Python 实现，单文件无额外子模块，通过命令行调用：

```bash
python "<skillDir>/scripts/okx_pair_spread.py" <subcommand> [options]
```

所有子命令都**同步执行**，每次都是"进去做完就出来"。所有输出都是 JSON。详见 `references/cli-contract.md`。

**唯一的例外是 `watch-*` 系列**：`watch-start` 会 fork 一个独立的 Python 守护子进程在后台轮询并触发平仓，状态文件和日志写在 `~/.okx/watches/`。`watch-list` / `watch-stop` / `watch-status` 用来查询和停止守护进程。`watch-start` 自身仍然是同步返回的，几百毫秒内 fork 完毕。

---

## 初始化流程（首次设置时执行）

当用户说 "设置汇率对" / "setup pair spread" 时：

### Step 1：确认 Python 与 ccxt

```bash
python --version
python -c "import ccxt; print(ccxt.__version__)"
```

### Step 2：完成首次配置

按 `references/credential-setup.md` 走一遍首次配置引导（创建模板、在 OKX 建 Key、填 `~/.okx/config.toml`、doctor 验证）。

如果用户已经配过 `okx-maker-entry`，直接跑 doctor 验证即可，不需要重填凭证。

### Step 3：向用户展示就绪摘要

doctor 验证全部通过后，向用户展示设置完成摘要和使用示例：

```
✅ 汇率对环境就绪！

你现在可以直接说以下命令（默认模拟盘）：

开仓：
  "BTC 做多 ETH 做空 各 1000U 汇率对开仓"
  "做多 BTC 做空 SOL 各 500U 汇率对 10 倍杠杆"

查看：
  "查看 BTC ETH 汇率对状态"   — 看双腿仓位和当前比值
  "BTC ETH 当前比值"

平仓：
  "平 BTC ETH 汇率对"          — 全部平
  "BTC ETH 汇率对 BTC 平500U ETH 平300U"  — 分别平
  "BTC ETH 汇率对只平 BTC 500U"  — 只平一腿（会警告失去对冲）

建议先用模拟盘试一次小额开仓，比如：
  "BTC 做多 ETH 做空 各 100U 汇率对开仓"
```

---

## 参数说明

| 参数                    | 类型   | 必填       | 默认值   | 说明                                       |
| ----------------------- | ------ | ---------- | -------- | ------------------------------------------ |
| longInstId              | string | 必填       | -        | 做多腿合约 ID，如 BTC-USDT-SWAP            |
| shortInstId             | string | 必填       | -        | 做空腿合约 ID，如 ETH-USDT-SWAP            |
| entryNotionalUsdtPerLeg | number | 开仓必填   | -        | 每腿开仓名义价值（USDT）。总风险 = 2x 该值 |
| targetLeverage          | number | 选填       | 当前杠杆 | 目标杠杆倍数，对两腿生效一致               |
| profile                 | string | 选填       | demo     | demo（模拟盘）或 live（实盘）              |
| closeAll                | flag   | 平仓选填   | -        | 全部平仓（两腿都清零）                     |
| longCloseUsdt           | number | 平仓选填   | 0        | 做多腿平仓名义价值                         |
| shortCloseUsdt          | number | 平仓选填   | 0        | 做空腿平仓名义价值                         |
| pollIntervalSeconds     | number | watch 选填 | 1        | 守护进程轮询间隔（1–60 秒）                |
| ratioStopLte            | number | watch 选填 | -        | currentRatio ≤ 该值时触发                  |
| ratioStopGte            | number | watch 选填 | -        | currentRatio ≥ 该值时触发                  |
| pnlStopLte              | number | watch 选填 | -        | combinedUnrealizedPnlUsdt ≤ 该值时触发     |
| pnlStopGte              | number | watch 选填 | -        | combinedUnrealizedPnlUsdt ≥ 该值时触发     |
| watchId                 | string | watch 选填 | -        | 已启动盯盘的句柄（list/stop/status 用）    |

---

## 开仓执行流程

### Phase 1：参数确认

1. 从用户请求中提取 longInstId、shortInstId、entryNotionalUsdtPerLeg。
2. 如果用户未指定 profile，**默认使用 demo 模拟盘**。
3. 确保两个 instId 都是 USDT 永续格式（如 `BTC-USDT-SWAP`）。用户说"BTC"时自动补全为 `BTC-USDT-SWAP`。
4. 明确谁是多头腿、谁是空头腿 —— 如果用户表达模糊，必须向用户确认。
5. **自动推算杠杆**：如果用户未指定杠杆，先在 Phase 2 后检查 `2 * entryNotionalUsdtPerLeg > availableUsdt`。若是，自动计算所需最低杠杆 = `ceil(2 * entryNotionalUsdtPerLeg / availableUsdt)`，再上浮 20% 留余量（取整），作为 targetLeverage。在参数确认摘要中告知用户"根据余额自动设置 Nx 杠杆"。
6. 向用户确认完整参数后继续。

### Phase 2：环境检查

**首先检查 OKX 配置是否就绪。** 如果 doctor / open-preview 报错，按下列规则自动引导修复：

1. **报 `ccxt is required` / `ModuleNotFoundError: No module named 'ccxt'`** → 错误消息里会包含脚本的 `sys.executable` 路径，直接用它跑 `"<该路径>" -m pip install ccxt`（**务必用 `-m pip`**）。装完后重跑 doctor。
2. **报 config 文件找不到 / profile 缺失 / 账户凭据字段为空** → 按 `references/credential-setup.md` 完成首次配置，再跑 doctor 验证。
3. **报 `oneWayMode: false`** → 引导用户去 OKX 交易设置 > 持仓模式 > 单向持仓，切换后重跑。
4. **报 `canTrade: false`** → API Key 未勾 Trade 权限，重建 Key 并勾 Read + Trade。
5. **报任一 `symbolTradable: false`** → 用户输的合约不存在或不可交易，纠正后重试。
6. 全部搞定 → 回到 Phase 2 继续原来的开仓请求。

验证命令：

```bash
python "<skillDir>/scripts/okx_pair_spread.py" doctor \
  --profile <profile> \
  --longInstId <longInstId> \
  --shortInstId <shortInstId>
```

### Phase 3：Open Preview 预检

```bash
python "<skillDir>/scripts/okx_pair_spread.py" open-preview \
  --profile <profile> \
  --longInstId <longInstId> \
  --shortInstId <shortInstId> \
  --entryNotionalUsdtPerLeg <amount> \
  --targetLeverage <leverage>
```

输出 JSON 的关键字段：

- `canStart`：是否可以启动
- `blockReason`：阻止原因（如有）
- `feasibility`：双腿杠杆、所需保证金、可用余额、最大名义价值
- `longLeg.conflictOrders` / `shortLeg.conflictOrders`：将被取消的非 reduce-only 挂单
- `longLeg.currentPosition` / `shortLeg.currentPosition`：当前双腿仓位
- `currentRatio`：当前比值 A/B
- `validation.passed`：双腿样本市价单是否通过 OKX precheck
- `warnings`：注意事项

**关键规则：**

- `canStart = false` 或 `validation.passed = false` → 向用户报告原因，**不继续**
- 任一腿有反向持仓 → block，提示用户先平掉反向仓
- 有 conflictOrders → 警告用户 open 会取消这些挂单
- 有 targetLeverage 变更 → 警告用户 open 会修改杠杆

向用户展示以下摘要，**等待明确确认后才执行 Phase 4**：

```
汇率对开仓预览 [模拟盘]
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
多头腿：BTC-USDT-SWAP  1,000 USDT  当前无仓位
空头腿：ETH-USDT-SWAP  1,000 USDT  当前无仓位
杠杆：10x（两腿一致）| 预估保证金：200 USDT
当前比值 A/B = 27.28 | 可用余额：5,230 USDT

执行方式：两腿同时市价开仓，若第二腿失败会自动回滚第一腿。
确认开始吗？
```

### Phase 4：执行开仓

用户确认后：

```bash
python "<skillDir>/scripts/okx_pair_spread.py" open \
  --profile <profile> \
  --longInstId <longInstId> \
  --shortInstId <shortInstId> \
  --entryNotionalUsdtPerLeg <amount> \
  --targetLeverage <leverage>
```

引擎会：

1. 取消冲突挂单（如有）
2. 设置杠杆（如需变更）
3. **市价开第一腿**（做多腿）→ 成功后继续
4. **市价开第二腿**（做空腿）→ 失败则 reduce-only 市价回滚第一腿
5. 校验双腿名义价值差在阈值内（10 USDT 或 20% 以内）
6. 返回双腿成交结果 + 当前仓位

**endReason 含义：**

- `completed`：两腿都成功开仓
- `entry_failed`：开仓失败，无残留仓位（第一腿失败，或第二腿失败且回滚成功）
- `entry_rollback_failed`：第二腿失败 + 回滚也失败 → **紧急警告用户手动处理残留仓位**
- `imbalance_warning`：两腿都成交但差额超阈值 → 提醒用户手动对齐

### Phase 5：报告结果

向用户展示开仓结果：

```
汇率对开仓完成 - BTC/ETH [模拟盘]
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
多头腿：BTC-USDT-SWAP
  请求：1,000 USDT | 成交：1,002.34 USDT
  均价：$104,230 | 合约数：0.00961
空头腿：ETH-USDT-SWAP
  请求：1,000 USDT | 成交：998.76 USDT
  均价：$3,820 | 合约数：0.2615
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
双腿差额：3.58 USDT ✓（阈值内）
开仓后比值 A/B：27.28
组合未实现 PnL：-0.45 USDT

下一步：
  "查看 BTC ETH 汇率对状态"  — 监控比值
  "平 BTC ETH 汇率对"         — 全部平仓
```

朗读 `followupHint`（如有）。

---

## 平仓执行流程

### 关键原则：平仓完全无状态

**平仓不依赖开仓记录**。只要用户在 OKX 上有 `longInstId` 的多头 + `shortInstId` 的空头，skill 就能帮他平。可以是本 skill 开的，也可以是手工开的、toolbox 开的、甚至昨天开的。只信任交易所实时仓位。

### Phase 1：参数确认

1. 从用户请求中提取 longInstId、shortInstId。
2. 判断平仓模式：
   - "平全部" / "全部平仓" / "平汇率对" → `--closeAll`
   - "BTC 平 500 ETH 平 300" → `--longCloseUsdt 500 --shortCloseUsdt 300`（前提：已识别 BTC 是多头腿）
   - "只平 BTC 500" → `--longCloseUsdt 500`（短头腿金额默认 0）
3. 如果用户表达模糊（不知道谁是多头谁是空头），**先跑 status 帮他识别**再确认。
4. 默认 profile 与开仓一致，未指定时默认 demo。

### Phase 2：状态确认（可选，但强烈建议）

如果用户说"平 BTC ETH 汇率对"而没有开仓上下文，先读实时仓位：

```bash
python "<skillDir>/scripts/okx_pair_spread.py" status \
  --profile <profile> \
  --longInstId <longInstId> \
  --shortInstId <shortInstId>
```

- 如果 `directionMatches = false`：多空腿标错了，向用户确认哪个是多头、哪个是空头，然后交换参数
- 如果任一腿 `side = FLAT`：用户没有该腿的仓位，向用户说明并请求确认

### Phase 3：Close Preview 预检

```bash
python "<skillDir>/scripts/okx_pair_spread.py" close-preview \
  --profile <profile> \
  --longInstId <longInstId> \
  --shortInstId <shortInstId> \
  [--closeAll | --longCloseUsdt N --shortCloseUsdt M]
```

输出字段：

- `canClose`：是否可以平
- `blockReason`：阻止原因（方向不匹配 / 金额超仓 / 都是 0）
- `plan.longClose` / `plan.shortClose`：每腿的平仓计划（null 表示不平该腿）
- `warnings`：注意事项（只平一腿会失去对冲等）

**关键规则：**

- `canClose = false` → 向用户报告原因，**不继续**
- 只平一腿 → 必须**醒目警告**"剩余的那腿会变成裸单，失去对冲保护"
- 方向不匹配 → block，让用户确认

向用户展示以下摘要，**等待明确确认后才执行 Phase 4**：

```
汇率对平仓预览 [模拟盘]
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
多头腿：BTC-USDT-SWAP  当前 1,002 USDT (入均价 104,230)
空头腿：ETH-USDT-SWAP  当前   999 USDT (入均价 3,820)
当前比值 A/B = 27.45 | 组合 PnL = +8.20 USDT

平仓计划：
  BTC 市价卖出 1,002 USDT (全平)
  ETH 市价买入   999 USDT (全平)

确认执行吗？
```

### Phase 4：执行平仓

```bash
python "<skillDir>/scripts/okx_pair_spread.py" close \
  --profile <profile> \
  --longInstId <longInstId> \
  --shortInstId <shortInstId> \
  [--closeAll | --longCloseUsdt N --shortCloseUsdt M]
```

引擎会：

1. 再次 close-preview 校验
2. 对每个非零腿下 reduce-only 市价单
3. 若某一腿失败另一腿已成，独立报告，不自动回滚（平仓失败的语义和开仓不同）
4. 返回双腿成交结果 + 最终仓位

### Phase 5：报告结果

向用户展示平仓结果：

```
汇率对平仓完成 - BTC/ETH [模拟盘]
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
多头腿：BTC-USDT-SWAP
  平仓：1,002 USDT | 均价：$104,450
  剩余仓位：0 USDT
空头腿：ETH-USDT-SWAP
  平仓：999 USDT | 均价：$3,815
  剩余仓位：0 USDT
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
最终组合 PnL：+8.15 USDT
```

**如果平仓后有残留仓位**（用户选择只平部分），必须醒目提示：

```
⚠️ 注意：平仓后仍有残留仓位
  BTC-USDT-SWAP: 500 USDT 多头（剩余）
  ETH-USDT-SWAP: 已清零

你现在持有 500 USDT 的 BTC 裸多头，已失去对冲。
如需平掉，说"平 BTC ETH 汇率对"或"BTC 平 500U"。
```

---

## 查看状态流程

当用户说"查看汇率对状态"、"BTC ETH 当前比值"等：

```bash
python "<skillDir>/scripts/okx_pair_spread.py" status \
  --profile <profile> \
  --longInstId <longInstId> \
  --shortInstId <shortInstId>
```

向用户展示：

```
汇率对状态 - BTC/ETH [模拟盘]
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
多头腿：BTC-USDT-SWAP  1,002.45 USDT (入均价 104,230, mark 104,560)
空头腿：ETH-USDT-SWAP    998.30 USDT (入均价 3,820,  mark 3,815)
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
当前比值 A/B：27.41 (开仓时 27.28, +0.48%)
组合未实现 PnL：+6.80 USDT
对冲可平金额：998.30 USDT (较小腿)
```

如果 `directionMatches = false`，警告用户方向标反了。

---

## 盯盘工作流（止盈止损自动触发）

当用户说**"盯 X Y 汇率对 比值跌破 Z 就全平"**、**"X Y 汇率对 亏 50U 就平"**、**"watch X Y stop loss at ratio Y"** 等，进入盯盘模式。

### 盯盘模式的本质

本 skill 提供 **真正的后台止损守护进程**（Python daemon），独立于 AI 客户端运行：

- **架构**：`watch-start` 会 fork 一个分离的 Python 子进程，1 秒级轮询双腿 mark price 与 PnL，触发条件由代码硬编码判断，命中立即在守护进程内调用 `close` 平仓。无 ccxt 调用经过 AI 客户端，无 LLM 参与触发判定。
- **不依赖 AI 客户端在线**：关闭 Claude Code / OpenClaw / 终端，盯盘照常运行
- **不挂 OKX 服务端条件单**：触发判定全部在本机进程内做

**生效边界 —— 必须在启动盯盘前向用户朗读：**

> 守护进程跑在你这台机器上。**机器关机、休眠、重启、断电、Python 进程被任务管理器杀掉** 会让盯盘失效，且不会自动恢复。实盘强烈建议同时在 OKX 原生界面给每条腿挂 reduce-only 条件单作为兜底。

### 支持的触发条件

**两类，可组合多个，任一命中即执行**（守护进程每个 tick 按顺序检查，第一个命中的胜出）：

| 类型  | 字段                        | 含义                            | 操作符        | CLI 参数                            |
| ----- | --------------------------- | ------------------------------- | ------------- | ----------------------------------- |
| ratio | `currentRatio`              | 多头 markPrice / 空头 markPrice | `lte` / `gte` | `--ratioStopLte` / `--ratioStopGte` |
| pnl   | `combinedUnrealizedPnlUsdt` | 双腿未实现盈亏之和（USDT）      | `lte` / `gte` | `--pnlStopLte` / `--pnlStopGte`     |

两个字段都来自 `status` 命令同款的原始数值，**严格按字段原值比较**，不做单位换算、不做方向反转。

### 支持的触发动作

1. **全平** (`closeAll`)：默认动作。命中后两腿都 reduce-only 市价平至 0
2. **按 USDT 分别平**：传 `--longCloseUsdt N --shortCloseUsdt M`，命中后调用与普通 `close` 等价的部分平仓

**明确不支持**（遇到时拒绝并向用户说明）：

- "平一半"、"平 50%" → 请用户自己算好 USDT 数额传入
- 多阶段触发（"先平一半再平剩下"）→ 等第一次触发后重新启动一轮新的盯盘
- 追踪止损 / trailing stop / 时间窗口限制

### Phase W1：解析与结构化

从用户请求中解析出以下结构：

```
watchPlan:
  longInstId:   <如 DOGE-USDT-SWAP>
  shortInstId:  <如 SOL-USDT-SWAP>
  profile:      <沿用当前会话，未指定时 demo>
  pollIntervalSeconds: <默认 1，最短 1，最长 60>
  triggers:
    - ratioStopLte: <数值>     # 至少给一个；可同时设多个
    - ratioStopGte: <数值>
    - pnlStopLte:   <数值>
    - pnlStopGte:   <数值>
  action:
    closeAll       # 默认
    # 或 longCloseUsdt: N, shortCloseUsdt: M
```

**模糊表达必问清**：没说轮询间隔可以默认 1 秒、不必问；但触发动作（全平还是部分平）、方向（多头腿是谁）必须问清。

### Phase W2：安全栏（一次性确认）

向用户展示完整盯盘计划摘要，等待明确确认：

```
盯盘计划 [模拟盘]
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
汇率对：DOGE-USDT-SWAP (多) / SOL-USDT-SWAP (空)
轮询间隔：每 1 秒
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
T1：currentRatio ≤ 0.00108           → 全平
T2：combinedUnrealizedPnlUsdt ≤ -200 → 全平
T3：combinedUnrealizedPnlUsdt ≥ 80   → 全平
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
⚠️ 盯盘期间**任一触发命中会立即自动平仓，不再请求确认**。
⚠️ 守护进程跑在本机。机器关机/休眠/进程被杀 → 盯盘失效。实盘请在 OKX 挂条件单兜底。
⚠️ 随时可说"停止盯盘"或"看下我有哪些盯盘"。

确认启动吗？
```

**必须**等用户明确回复"确认"/"启动"/"开始"之类才能进入 Phase W3。

不需要单独跑 `status` 做前置检查 —— `watch-start` 命令本身会做以下硬编码 preflight，失败会以 `ok=false` 拒绝启动：仓位存在性、方向匹配、所有触发器在启动时都未命中。

### Phase W3：启动守护进程

用户确认后，调用 `watch-start`。**不要**用任何 /loop / 循环 / TodoWrite 调度 —— 这是同步命令，几百毫秒返回。

```bash
python "<skillDir>/scripts/okx_pair_spread.py" watch-start \
  --profile <profile> \
  --longInstId <longInstId> \
  --shortInstId <shortInstId> \
  --pollIntervalSeconds 1 \
  --ratioStopLte <数值或省略> \
  --ratioStopGte <数值或省略> \
  --pnlStopLte <数值或省略> \
  --pnlStopGte <数值或省略>
  # closeAll 是默认动作；要部分平就再加：
  # --longCloseUsdt N --shortCloseUsdt M
```

返回的 JSON 关键字段：

- `ok`：true 表示守护进程已 fork 并 PID 存活
- `watchId`：盯盘 ID（形如 `watch-20260410-091356-0bf56e`），是后续 list/stop/status 的句柄
- `pid`：守护进程 PID（用户机器上的真进程）
- `baselineRatio` / `baselinePnl`：启动瞬间的基线值
- `triggers`：守护进程将检查的触发器列表（T1/T2/...）

向用户朗读：

```
✅ 盯盘已启动 [模拟盘]
ID: watch-20260410-091356-0bf56e (pid 21052)
基线：ratio=0.001108  pnl=-170.39 USDT
触发器：T1/T2/T3（如上）
守护进程已在后台运行。我现在可以做别的事，触发命中会由代码自动平仓。
```

启动失败的常见原因（命令的 `message` 字段会说明，按需引导用户）：

- `one or both legs are FLAT` → 用户没仓位，让他先开仓
- `direction mismatch` → 方向标反了，交换 long/short
- `one or more triggers are already satisfied at start time` → 阈值过松或市场已经在触发区，让用户复核或先手动平

### Phase W4：日常查询与停止

启动后，AI 客户端**不需要也不应该**主动循环查询。只在用户明确询问或要求时再调命令：

**用户问"现在盯盘怎么样了" / "看下我有哪些盯盘"**：

```bash
python "<skillDir>/scripts/okx_pair_spread.py" watch-list
```

返回所有盯盘条目（运行中优先），含 `status`、`tickCount`、`lastRatio`、`lastPnl`、`pidAlive`。运行中的标 `running`，正常停的标 `stopped/triggered/positions_flat`，僵尸进程会被 `watch-list` 自动 reconcile 成 `crashed`。

**用户问"那个 watch-XXX 详情"**：

```bash
python "<skillDir>/scripts/okx_pair_spread.py" watch-status --watchId watch-XXX
```

返回完整 state JSON + 最后 20 行 log。

**用户说"停止盯盘 watch-XXX" / "停掉那个盯盘"**（多个盯盘时必须问清是哪一个）：

```bash
python "<skillDir>/scripts/okx_pair_spread.py" watch-stop --watchId watch-XXX
```

该命令通过 sentinel 文件通知守护进程优雅退出，最多等 10 秒，超时后强杀 PID。返回的 `status` 应为 `stopped`，`endReason` 为 `stop_requested`。

**触发命中是异步的 —— AI 客户端通常感知不到事件**：守护进程命中后自己平仓并退出，状态写到 state JSON。下次用户问起时跑 `watch-list` 才能看到。如果用户希望"等到平了告诉我"，告诉他 AI 客户端做不到主动通知，建议自行查询或等下一轮对话。

### Phase W5：修改盯盘

**不支持**原地热更新触发器。要改阈值就：先 `watch-stop` 旧的，再用新参数 `watch-start`。预授权范围只限本次启动的触发器集合。

### 跨 AI 客户端兼容性

watch-start / watch-list / watch-stop / watch-status 都是同步 CLI 命令，**与 AI 客户端能力无关**。Claude Code、OpenClaw 或任何能 spawn 子进程的客户端都能用同一套流程。**禁止**用 /loop、TodoWrite 等 Claude Code 独有能力替代守护进程，也禁止用 `while True: sleep()` 这种 busy-wait 假装后台。

---

## 硬性规则

### 开仓规则

1. **open-preview 必做**：open 前需先跑 open-preview 预检
2. **确认必须**：preview 后需等用户明确确认才能 open
3. **反向持仓阻断**：任一腿有反向持仓时拒绝启动，提示用户先平仓
4. **失衡回滚**：第二腿失败时必须尝试 reduce-only 市价回滚第一腿
5. **回滚失败警报**：若 `endReason = entry_rollback_failed`，**醒目警告**用户手动处理残留仓位
6. **默认模拟盘**：未指定 profile 时默认 demo

### 平仓规则

1. **close-preview 必做**：close 前需先跑 close-preview
2. **确认必须**：preview 后需等用户明确确认才能 close（**盯盘模式命中触发时例外 —— 见盯盘规则**）
3. **方向校验**：多头腿实际必须是 LONG，空头腿实际必须是 SHORT，否则 block
4. **单腿平仓警告**：只平一腿时必须**醒目提醒**"失去对冲，变成裸单"
5. **残留仓位提醒**：平仓完成后如仍有仓位，必须朗读残留仓位和下一步建议

### 盯盘规则

1. **一律走 watch-start 守护进程**：禁止用 /loop、TodoWrite、`while True: sleep()` 或任何 AI 客户端循环假装盯盘 —— 触发判定必须由 Python 守护进程做，AI 客户端只负责"启动 / 查询 / 停止"
2. **一次性确认**：Phase W2 的盯盘计划必须向用户完整朗读并等待明确确认才能调 watch-start
3. **预授权作用域**：一次确认只对本次启动的触发器集合生效，**任何修改都必须 watch-stop + 重新 watch-start + 重新确认**
4. **触发命中跳过二次确认**：守护进程命中触发时**直接调用 close**，不再请求用户确认 —— 这是盯盘的核心价值
5. **不要主动 polling 守护进程**：watch-start 返回后立刻把控制权交回用户。**只有在用户问起时**才跑 watch-list / watch-status；不要在没有用户请求的情况下定时查
6. **多盯盘必须问清 watchId**：用户机器上可能同时跑多个盯盘。"停止盯盘"模糊时先 watch-list 列出，让用户指定 watchId 再 stop
7. **启动失败按 message 引导**：watch-start 的 preflight 失败原因（FLAT、direction mismatch、already-hit）写在响应 `message` 字段，照着引导用户即可，不要瞎猜
8. **盯盘期间 open 阻断**：若 watch-list 显示同一对有 running 状态的盯盘，拒绝同一对的 open / close 请求，提示用户先 watch-stop
9. **守护进程边界要朗读**：启动前必须朗读"机器关机/休眠/进程被杀会失效"，让用户知道盯盘范围
10. **轮询间隔范围**：1–60 秒，默认 1 秒。低于 1 秒被命令拒绝，高于 60 秒被拒绝

### 通用规则

1. **配置引导**：遇到 config 相关错误时，按 `references/credential-setup.md` 引导用户完成首次配置
2. **一步步确认**：涉及金额/方向/杠杆的关键步骤必须向用户展示摘要并等待确认
3. **模糊表达必问清**：用户表达不清时（如没说谁做多谁做空、没说金额、没说是开仓还是加仓），必须先问清再动手

---

## 风险提示

1. **默认使用模拟盘（demo）**，切换实盘需用户明确指定 profile=live。
2. 市价开仓在流动性差的合约上会吃滑点，总成本 = 做多腿买价 + 做空腿卖价。
3. 失衡阈值设为 max(10 USDT, 20% per-leg)，超过会在开仓结果中警告但不会自动调仓 —— 大金额时建议手动看一下是否需要补齐。
4. **单腿平仓 = 失去对冲**，剩下的那腿会完全暴露在市场波动中。除非明确知道自己在做什么，否则优先整对平仓。
5. 本 skill 平仓不依赖开仓记录，但方向校验是关键 —— 如果你把 long/short 两个参数填反了，preview 会 block，按提示交换参数即可。
6. 监控比值走势请用 `status` 命令手动查询，或启动 `watch-start` 让守护进程在后台盯。
7. **盯盘 = 本机 Python 守护进程，不是交易所侧条件单**。守护进程跑在你这台机器上：关机、休眠、重启、断电、进程被任务管理器/Activity Monitor 杀掉都会让盯盘立即失效，且不会自动恢复。实盘中推荐同时在 OKX 原生界面给每条腿挂 reduce-only 条件单作为兜底（但注意两腿触发时机可能不同步，其中一腿先被触发后另一腿会变成裸单）。
8. **盯盘触发命中时跳过人工确认**。这是预授权的必然代价：若你在盯盘期间改变主意，必须主动用 `watch-stop` 停掉对应的 watchId，否则阈值一到守护进程就立即平仓。
9. 本 Skill 仅供学习研究，不构成投资建议，盈亏自负。
