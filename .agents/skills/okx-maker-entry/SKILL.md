---
name: okx-maker-entry
version: "1.1.0"
license: MIT
description: |
  通过被动挂单（Post-Only）开仓，替代手动市价单。自动在盘口 1-40 档分散挂 maker 限价单,
  成交即享 maker 费率，比 taker 省手续费。支持所有 USDT 永续合约。
  当用户说"挂单开仓"、"maker开仓"、"被动开仓"、"省手续费开仓"时使用此技能。
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

# Maker 挂单开仓

> 用被动挂单（Post-Only 限价单）开仓，不吃单，自动享受 maker 费率。
> 在盘口 1-40 档分散挂单，价格移动时自动 reclaim 远端订单到近端，直到目标名义价值全部成交。

```
用户下达开仓指令
       ↓
  检查环境 → preview 预检 → 用户确认 → start 启动后台引擎
       ↓                                       ↓
  doctor 查行情/余额/持仓                执行引擎（500ms 循环）
                                       ├─ 40 档 band 挂单
                                       ├─ 实时成交追踪
                                       ├─ 远端订单 reclaim
                                       └─ 完成/暂停/停止
       ↓
   status 查看结果
```

---

## 使用方式

### 首次设置

对 AI 助手说 **"设置 maker 开仓"** 或 **"setup maker entry"**，将自动检测环境并执行初始化流程。

### 日常使用

直接说即可，示例：

- **"BTC 做多 1000U 挂单开仓"**
- **"ETH 做空 500U maker 开仓 10 倍杠杆"**
- **"SOL 做多 200U 被动开仓 实盘"**
- **"BTC 做多 5000U 挂单开仓 最大滑点 0.5%"**
- **"查看挂单开仓状态"**
- **"停止挂单开仓"**

助手会自动执行完整流程：参数确认 → 环境检查 → 预检 → 确认 → 启动 → 监控。

---

## 前置条件

1. **AI 客户端**：Claude Code 或 OpenClaw（小龙虾）
2. **Python**：>= 3.9（执行引擎运行环境，推荐 3.11+，否则需额外安装 `tomli`）
3. **ccxt**：Python 交易所 SDK（需已存在于当前 Python 环境；若缺失，doctor 会在报错中给出对应 Python 解释器路径供用户自行补齐）
4. **OKX API Key**：已配置 `~/.okx/config.toml`，权限需 Read + Trade
5. **账户设置**：OKX 账户必须是 **单向持仓模式**（Net Mode）

---

## 运行方式

本 skill 纯 Python 实现，单文件无额外子模块，通过命令行调用：

```bash
python "<skillDir>/scripts/okx_maker_entry.py" <subcommand> [options]
```

其中 `<skillDir>` 是本 skill 目录的绝对路径。所有子命令都输出 JSON，便于助手解析。

行情/账户查询均由该脚本自身通过 ccxt 完成，不依赖其他 CLI 或 MCP。详见 `references/cli-contract.md`。

---

## 初始化流程（首次设置时执行）

当用户说 "设置 maker 开仓" / "setup maker entry" 时：

### Step 1：确认 Python 与 ccxt

```bash
python --version
python -c "import ccxt; print(ccxt.__version__)"
```

### Step 2：完成首次配置

按 `references/credential-setup.md` 走一遍首次配置引导（创建模板、在 OKX 建 Key、填 `~/.okx/config.toml`、doctor 验证）。

### Step 3：向用户展示就绪摘要

`credential-setup.md` 的 doctor 验证全部通过后，向用户展示设置完成摘要和使用示例：

```
✅ 环境就绪！

你现在可以直接说以下命令来挂单开仓（默认模拟盘）：

  "BTC 做多 1000U 挂单开仓"
  "ETH 做空 500U maker开仓 10倍杠杆"
  "SOL 做多 200U 被动开仓 实盘"

其他操作：
  "查看挂单开仓状态"  — 查看当前任务进度
  "停止挂单开仓"      — 撤销所有挂单并停止

建议先用模拟盘试一次小额开仓，比如：
  "BTC 做多 100U 挂单开仓"
```

---

## 参数说明

| 参数              | 类型   | 必填 | 默认值   | 说明                                 |
| ----------------- | ------ | ---- | -------- | ------------------------------------ |
| instId            | string | 必填 | -        | OKX 合约 ID，如 BTC-USDT-SWAP        |
| side              | string | 必填 | -        | LONG（做多）或 SHORT（做空）         |
| entryNotionalUsdt | number | 必填 | -        | 开仓名义价值（USDT）                 |
| targetLeverage    | number | 选填 | 当前杠杆 | 目标杠杆倍数                         |
| maxAdverseMovePct | number | 选填 | 无限制   | 最大不利价格移动百分比，超过自动暂停 |
| maxRunMinutes     | number | 选填 | 无限制   | 最大运行时间（分钟），超时自动暂停   |
| profile           | string | 选填 | demo     | demo（模拟盘）或 live（实盘）        |

---

## 执行流程

### Phase 1：参数确认

1. 从用户请求中提取 instId、side、entryNotionalUsdt。
2. 如果用户未指定 profile，**默认使用 demo 模拟盘**。
3. 未指定的可选参数使用默认值。
4. 确保 instId 是 USDT 永续格式（如 `BTC-USDT-SWAP`）。用户说"BTC"时自动补全为 `BTC-USDT-SWAP`。
5. **自动推算杠杆**：如果用户未指定杠杆，先在 Phase 2 获取可用余额后，检查 `entryNotionalUsdt > availableUsdt`。若是，自动计算所需最低杠杆 = `ceil(entryNotionalUsdt / availableUsdt)`，再上浮 20% 留余量（取整），作为 targetLeverage。在参数确认摘要中告知用户"根据余额自动设置 Nx 杠杆"即可，无需用户额外操作。
6. 向用户确认完整参数后继续。

### Phase 2：环境检查

**首先检查 OKX 配置是否就绪。** 如果 doctor / preview / 环境检查报错，按下列规则自动引导修复，并向用户说明修复步骤：

1. **报 `ccxt is required` / `ModuleNotFoundError: No module named 'ccxt'`** → 说明 Python 环境没装 ccxt。错误消息里会包含脚本自己的 `sys.executable` 路径，直接用它跑 `"<该路径>" -m pip install ccxt`（**务必用 `-m pip`**，裸 `pip install` 在多 Python 环境下会装到别的解释器）。装完后重跑 doctor。
2. **报 config 文件找不到 / profile 缺失 / 账户凭据字段为空** → 按 `references/credential-setup.md` 完成首次配置，再跑 doctor 验证。
3. **报 `oneWayMode: false`** → 引导用户去 OKX 交易设置 > 持仓模式 > 单向持仓，切换后重跑。
4. **报 `canTrade: false`** → API Key 未勾 Trade 权限，重建 Key 并勾 Read + Trade。
5. 全部搞定 → 回到 Phase 2 继续原来的开仓请求。

验证 API 连通性和账户状态用 doctor 命令即可：

```bash
python "<skillDir>/scripts/okx_maker_entry.py" doctor --profile <profile> --instId <instId>
```

检查项：

- API 凭证有效（canRead / canTrade 均为 true）
- 账户是单向持仓模式（oneWayMode = true）
- 合约可交易（symbolTradable = true）

余额和反向持仓检查由 `preview` 子命令在 Phase 3 完成。

### Phase 3：Preview 预检

调用执行引擎的 preview 命令做精确预检：

```bash
python "<skillDir>/scripts/okx_maker_entry.py" preview \
  --profile <profile> \
  --instId <instId> \
  --side <LONG|SHORT> \
  --entryNotionalUsdt <amount> \
  --targetLeverage <leverage> \
  --maxAdverseMovePct <pct> \
  --maxRunMinutes <minutes>
```

输出 JSON 包含：

- `preview.canStart`：是否可以启动
- `preview.blockReason`：阻止原因（如有）
- `preview.feasibility`：杠杆、保证金、最大名义价值
- `preview.conflictOrders`：将被取消的非 reduce-only 挂单
- `preview.warnings`：注意事项（含大额/无保护/交易所限额提示）
- `validation.passed`：样本订单是否通过 OKX precheck

**关键规则：**

- `canStart = false` 或 `validation.passed = false` → 向用户报告原因，**不继续**
- 有 conflictOrders → 警告用户 start 会取消这些挂单
- 有 targetLeverage 变更 → 警告用户 start 会修改杠杆
- warnings 里出现 "Large notional entry" / "No slippage guard" → 在预览摘要中醒目呈现这条警告，并建议用户补一个 `maxAdverseMovePct`

向用户展示摘要，**等待明确确认后才执行 Phase 4**。

### Phase 4：启动执行引擎

用户确认后启动后台执行引擎：

```bash
python "<skillDir>/scripts/okx_maker_entry.py" start \
  --profile <profile> \
  --instId <instId> \
  --side <LONG|SHORT> \
  --entryNotionalUsdt <amount> \
  --targetLeverage <leverage> \
  --maxAdverseMovePct <pct> \
  --maxRunMinutes <minutes>
```

引擎会：

1. 取消冲突挂单（如有）
2. 设置杠杆（如需变更）
3. 作为独立的后台任务运行，在盘口 1-40 档持续挂 Post-Only 限价单
4. 实时追踪成交，远端订单自动 reclaim 到近端
5. 达到目标名义价值后自动完成

启动后 `start` 命令输出 `taskId` 和后台 `pid`。

### Phase 5：监控与结果

启动后定期查询状态：

```bash
python "<skillDir>/scripts/okx_maker_entry.py" status
```

输出字段（核心）：

- `phase`：working / reclaiming / completed / paused / stopped / failed
- `completionRatio`：完成百分比
- `filledEntryNotionalUsdt`：已成交名义价值
- `openWorkingNotionalUsdt`：当前挂单中的名义价值
- `unfilledNotionalUsdt`：尚未成交 = `target - filled`
- `unplacedNotionalUsdt`：尚未铺出的 = `target - filled - openWorking`（被交易所并发限额卡住时会很大）
- `remainingNotionalUsdt`：等价于 `unplacedNotionalUsdt`，保留是为了向下兼容（未来版本可能移除）
- `activeWorkingOrders`：活跃挂单数
- `bestBid` / `bestAsk`：当前盘口

**理解 `unplaced` vs `unfilled` 很重要**：大额任务在 OKX 上会被"并发同向暴露上限"卡住，此时 `unplaced > 0` 但 `unfilled = unplaced + openWorking`。表现为"铺满上限 → 等成交 → 再铺"的滚动模式，不是 bug。

**终态时额外输出 `result` 块：**

- `avgFillPrice`：加权平均成交价
- `placedOrderCount`：总下单数
- `reclaimCount`：reclaim 次数
- `endReason`：结束原因（completed / dust_remainder / paused / stopped / error）
- `recommendationText`：结果摘要
- `finalPosition`：终态时账户的实际持仓快照（side / absNotionalUsdt / leverage / unrealizedPnlUsdt 等）
- `followupHint`：下一步建议文案 — 比如用户停止但还留着仓位时会提醒"你还持有 XX USDT 的多头，考虑手动平仓或反向 Maker Entry"

终态场景下应当向用户朗读 `followupHint`，尤其是 stop / paused / dust_remainder 场景 — 这是防止用户遗忘残留持仓的最后一道保护。

### 停止任务

用户说"停止挂单开仓"时：

```bash
python "<skillDir>/scripts/okx_maker_entry.py" stop
```

引擎会撤销所有活跃挂单后优雅退出。之后查一次 `status` 确认最终状态，并向用户朗读 `result.followupHint`。

---

## 硬性规则

1. **preview 必做**：start 前需先跑 preview 预检
2. **确认必须**：preview 后需等用户明确确认才能 start
3. **配置引导**：遇到 config 相关错误时，按 `references/credential-setup.md` 引导用户完成首次配置
4. **默认模拟盘**：未指定 profile 时默认 demo
5. **反向持仓阻断**：有反向持仓时拒绝启动，提示用户先平仓
6. **大额无保护需二次确认**：当 `entryNotionalUsdt >= 10000` 且 `maxAdverseMovePct` 未设置时，preview 会发出 "No slippage guard" 警告。此时应向用户醒目呈现这条警告，并建议补一个 `maxAdverseMovePct`（常用 0.3 ~ 1.0）。
7. **终态向用户通报 `followupHint`**：任务进入终态（completed / stopped / paused / dust_remainder / failed）后，应向用户朗读 `result.followupHint`，避免用户遗忘残留持仓。

## 输出格式

每次执行后向用户报告：

```
Maker 挂单开仓 - BTC-USDT-SWAP [模拟盘]
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
方向：LONG | 目标：1,000 USDT | 杠杆：5x
状态：working | 完成度：65.3%
已成交：653.00 USDT | 均价：$104,230
挂单中：200.00 USDT | 未铺：147.00 USDT
活跃挂单：12 | 盘口：104,228 / 104,232
未成交合计：347.00 USDT
```

- "挂单中" = `openWorkingNotionalUsdt`
- "未铺" = `unplacedNotionalUsdt`（被交易所限额卡住时会较大，是正常的滚动铺单行为）
- "未成交合计" = `unfilledNotionalUsdt` = 挂单中 + 未铺

## 风险提示

1. **默认使用模拟盘（demo）**，切换实盘需用户明确指定 profile=live。
2. Maker 挂单不保证成交速度 — 单边行情中 Post-Only 可能被持续拒绝。
3. 设置 `maxAdverseMovePct` 可在价格剧烈不利移动时自动暂停，保护资金。大额任务强烈建议设置。
4. 设置 `maxRunMinutes` 可限制最长运行时间。
5. 执行期间如需紧急停止，说"停止挂单开仓"，引擎会撤销所有挂单。停止后可能仍有已成交持仓，注意查看 `followupHint`。
6. 本 Skill 仅供学习研究，不构成投资建议，盈亏自负。
