---
name: dcd-auto-trader
description: |
  OKX DCD（双币赢）全自动交易系统。PUT 低买赚权利金、CALL 高卖降成本，每天 17:05 自动执行。
  内置 v3 波动率引擎和 FOMC/CPI 事件日历风控。支持 Claude Code（MCP）和 OpenClaw（CLI）。
  涉及 DCD/双币赢时必须使用本 skill 而非 okx-cex-earn。
version: 3.1.0
metadata:
  openclaw:
    requires:
      bins: [okx, python3]
      config: [~/.okx/config.toml]
    os: [macos, linux]
emoji: 🎯
---

# DCD 双币赢全自动交易系统 v3

> **策略 A（PUT 低买）**：用 USDG 赚 USDG，尽量不被行权
> **策略 B（CALL 高卖）**：被行权拿到的 BTC，通过 CALL 赚权利金降低成本，等待保本卖出
> 两个策略可同时运行，资金互不冲突（A 用 USDG/USDT，B 用 BTC）。

```
  USDG ──PUT低买──┬── ✅ 未行权 → USDG+权利金 → 继续PUT
                  └── ❌ 被行权 → 收到BTC ──┐
                                             ▼
  BTC ──CALL高卖──┬── ✅ 被行权 → USDG回收 → 回到PUT
                  └── ❌ 未行权 → BTC+权利金 → 降低有效成本 → 继续CALL
```

---

## 使用方式

### 首次设置

对 AI 助手说 **"设置双币赢"** 或 **"setup dcd"**，将自动检测环境并执行初始化。

### 日常运行

设置完成后，定时任务每天 **17:05 UTC+8** 自动触发，无需人工干预。

---

## 前置条件

1. **AI 客户端**：Claude Code 或 OpenClaw（小龙虾）
2. **OKX API Key（实盘）**：已配置 `~/.okx/config.toml`
3. **okx-trade-cli**：已安装（`npm install -g okx-trade-cli`）— CLI 模式必需，MCP 模式可选
4. **资金**：OKX 资金账户有 USDG 或 USDT（建议 >= 1,000）

---

## 环境适配

本 skill 支持两种运行模式，首次执行时自动检测：

### MCP 模式（Claude Code）
- 直接调用 `okx-trade-mcp-live` MCP 工具
- 需配置权限白名单和 scheduled-tasks MCP
- 详见 `references/claude-code-setup.md`

### CLI 模式（OpenClaw / 通用）
- 通过 `okx` CLI 命令执行，所有命令加 `--json --live` 参数
- 需已安装 okx-trade-cli 且 `~/.okx/config.toml` 已配置实盘 profile
- 详见 `references/openclaw-setup.md`

### 检测方法

```
1. 尝试 MCP 调用 market_get_index_ticker（instId: BTC-USD）
   → 成功 = MCP 模式
2. 若失败，执行 `okx market index-ticker --instId BTC-USD --json --live`
   → 成功 = CLI 模式
3. 均失败 → 提示用户安装配置（见 references/ 下对应文档）
```

确定模式后，后续所有工具调用统一遵循该模式。

---

## 初始化流程（首次设置时执行）

当用户说"设置双币赢"/"setup dcd"时：

### Step 1：检测环境
按上述"检测方法"确定运行模式（MCP 或 CLI）。

### Step 2：创建项目文件
1. 用 `templates/trade_log.md` 创建 `交易记录.md`
2. **MCP 模式**额外创建 `CLAUDE.md`（内容见 `references/claude-code-setup.md`）
3. **CLI 模式**将 `templates/soul_dcd.md` 合并到用户的 `~/.openclaw/SOUL.md`。soul_dcd.md 包含授权声明、工具使用规范，以及策略文件路径指引——OpenClaw agent 每次执行时会据此找到并阅读完整 SKILL.md

### Step 3：配置权限与定时任务
- **MCP 模式**：按 `references/claude-code-setup.md` 配置权限白名单 + 注册 scheduled-task
- **CLI 模式**：按 `references/openclaw-setup.md` 安装 skill 到 OpenClaw skills 目录 + 注册 cron（prompt 中包含完整的文件路径和执行指引，确保 agent 能找到策略和交易记录）

### Step 4：首次验证
手动触发一次完整交易流程（Phase 1 到 Phase 5），确认数据获取、波动率计算、选品、下单、记录更新均正常。

---

## 策略 A：PUT 低买

### 硬性规则

1. **行权价 < 安全下界**（安全下界含 √到期天数 缩放，见公式）
2. **安全距离**：距现价 >= 3.5%（日常），>= 5%（事件日及前后 1 天）
3. **每天都交易**：事件日不跳过，用事件乘数加大安全距离（回测验证：89天全交易，安全率 98.9%）
4. **投入规则**：先选品再投入（不因投入而放松安全标准）。资金账户有 USDG/USDT 时全仓投入；无则从交易账户划转 1000 USDG 或 USDT 后投入

### 软性规则
- FOMC 周只选 1 天产品，平时优先 1 天（√N 缩放下 1 天产品安全下界最宽松）
- BTC 跌至距行权价 < 1% 时提醒用户关注提前赎回
- 连续被行权 2 次后暂停 1 天

### PUT 选品逻辑

1. 只选风险评级 ✅（行权价 < 安全下界）的产品
2. 同一行权价+到期日，比较 BTC-USDT 和 BTC-USDG 两个产品，选 APY 更高的
3. ✅ 中选 APY 最高（但 <= 150%）；全部 APY < 5% 则跳过
4. 优先 1 天产品
5. 同 APY 选距安全下界更远的

---

## 策略 B：CALL 高卖

> 目标：行权价 >= 合并有效成本时被行权，保本卖出全部 BTC，回收 USDG。
> 绝不主动亏损卖出。每轮权利金持续降低有效成本，时间站在我们这边。

### 合并有效成本

多次 PUT 被行权时，所有 BTC 合并管理：

```
合并有效成本 = 所有批次总投入USDG ÷ 所有批次总持有BTC（含累计权利金）

每轮 CALL 未行权后更新：
新合并有效成本 = 总投入USDG ÷ (总持有BTC + 本轮权利金BTC)

新 PUT 被行权时：新增 BTC 并入合并池，重新计算。
```

全部 BTC 投入同一个 CALL，不分批。

### 硬性规则

1. **行权价 >= 合并有效成本**：绝不低于有效成本，绝不主动亏损卖出
2. **不做市价止损**：BTC 下跌时持续通过 CALL 积累权利金，耐心等待
3. **每天交易**：与 PUT 一致，事件日不跳过

### CALL 选品逻辑

1. 行权价 = 最接近且 >= 合并有效成本的可用产品
2. 优先 1 天产品（加快迭代）
3. 同一行权价+到期日，比较 BTC-USDT 和 BTC-USDG 两个产品，选 APY 更高的
4. 多个符合条件时选 APY 最高
5. BTC 可用量 <= 0.0001 则跳过
6. 无 >= 合并有效成本的行权价可选则跳过（不降低标准）

### 执行规则

- 全仓投入全部 BTC，向下取整到 0.0001
- 低 APY 完全可接受（低 APY 也是正权利金）
- 每轮复盘后更新合并有效成本

### 退出条件

1. **被行权** → BTC 卖出换回 USDG → 回到策略 A
2. **有效成本降至当前价以下** → 可市价卖出或继续 CALL 赚权利金

---

## v3 波动率预测引擎

### 核心思路

v3 = 多源波动率基础 + 三项危机感知：

| 层级 | 成分 | 作用 |
|------|------|------|
| 基础 | IV×w + ATR×w + BB×0.15 + 资金费率×0.10 | 综合多源波动率 |
| 置信缓冲 | vol_accel 或临近事件 → +0.3~1.0% | 防低估 |
| 回撤安全垫 | 距30天高点回撤 >10%→+1.5%, >20%→+2.5%, >30%→+4.0% | 识别脆弱状态 |
| 暴跌动量 | 7天跌幅 >5%→+1.5%, >10%→+3.0% | 捕捉下跌趋势 |
| 动态权重 | ATR > 2×IV 时，ATR 权重升至 0.55（IV 降至 0.20） | 急跌时 IV 滞后补偿 |

### 数据源

> 技术指标统一取现货 BTC-USDT 数据，避免永续合约基差干扰。资金费率取永续合约 BTC-USD-SWAP（仅合约有此数据）。

| 数据 | 用途 |
|------|------|
| 期权 IV（BTC-USD ATM 近月） | 隐含波动率基础 |
| ATR(7) + ATR(14)（BTC-USDT 1H） | 双速实际波动率 |
| Bollinger(20)（BTC-USDT 1H） | 带宽波动率 |
| 资金费率（BTC-USD-SWAP） | 市场情绪指标 |
| 30天日K（BTC-USD 指数） | 回撤安全垫 + 暴跌动量 |

具体 MCP/CLI 调用命令见下方 Phase 3 表格。

> **IV 降级**：若 `option_get_greeks` 不可用，降级为 30 天 HV（权重从 0.45 降至 0.35，差额补入 confidence_buffer）。

### 完整计算公式

```python
# ── A. IV 成分 ──
IV_daily = IV_annual / sqrt(365)

# ── B. 双速 ATR（1H → 日化）──
ATR_fast = ATR_1H_7 * sqrt(24) / price
ATR_slow = ATR_1H_14 * sqrt(24) / price
ATR_comp = max(ATR_fast, ATR_slow * 0.9)

# ── C. BB 成分（1H → 日化）──
BB_std = (BB_upper - BB_lower) / 4
BB_comp = BB_std * sqrt(24) / price

# ── D. 资金费率调整 ──
funding_adj:
    |rate| < 0.01%   → 0.0
    rate < -0.01%    → 0.003  (负费率 = 空头恐慌)
    |rate| 0.01-0.03% → 0.002
    |rate| 0.03-0.05% → 0.005
    |rate| > 0.05%   → 0.008

# ── E. 动态权重 ──
若 ATR_comp > 2 * IV_daily:
    w_iv=0.20, w_atr=0.55    # ATR主导模式（急跌时 IV 滞后）
否则:
    w_iv=0.45, w_atr=0.30    # 标准模式

vol_base = w_iv*IV_daily + w_atr*ATR_comp + 0.15*BB_comp + 0.10*funding_adj

# ── F. 置信缓冲 ──
vol_accel = |ATR_fast - ATR_slow| / ATR_slow
conf_buffer:
    vol_accel > 0.3 或临近事件 → +1.0%
    vol_accel > 0.1            → +0.5%
    否则                       → +0.3%

# ── G. 回撤安全垫（v3）──
drawdown = (price - high_30d) / high_30d
dd_adj:
    |drawdown| > 30% → +4.0%
    |drawdown| > 20% → +2.5%
    |drawdown| > 10% → +1.5%
    否则             → 0

# ── H. 暴跌动量（v3）──
drop_7d = (price - price_7d_ago) / price_7d_ago
mom_adj:
    drop_7d < -10% → +3.0%
    drop_7d < -5%  → +1.5%
    否则           → 0

# ── 最终波动率 ──
vol_24h = vol_base + conf_buffer + dd_adj + mom_adj
```

### 安全下界计算

```
buy_dist = max(vol_24h * sqrt(到期天数) * 事件乘数, 最小安全距离)
行权价 = floor(BTC价格 * (1 - buy_dist) / 500) * 500
```

### 自适应事件乘数表

| 事件类型 | 当天 | 前1天 | 后1天 | 最低安全距离 |
|---------|------|-------|-------|-------------|
| FOMC 决议 | 1.8× | 1.8× | 1.3× | >= 5.0% |
| CPI 数据 | 1.5× | 1.5× | 1.2× | >= 5.0% |
| CME 交割 | 1.5× | 1.5× | 1.3× | >= 5.0% |
| 就业数据(NFP) | 1.4× | 1.4× | 1.2× | >= 5.0% |
| 多重到期日 | 1.8× | 1.8× | 1.5× | >= 5.0% |
| 无事件 | 1.0× | — | — | >= 3.5% |

---

## 每日执行流程（定时任务 Prompt）

你是 DCD 自动交易器。每天 17:05 UTC+8 执行。根据已检测的环境模式使用 MCP 工具或 CLI 命令（加 `--json --live`）。

**核心原则：每天都交易，永不跳过。** 事件日通过乘数加大安全距离。

> **路径约定**：CLI 模式下，交易记录位于 `~/dcd-project/交易记录.md`，脚本位于 `~/.openclaw/workspace/skills/dcd-auto-trader/scripts/`。MCP 模式下均为当前项目目录的相对路径。

### Phase 1：读取策略与状态

1. 完整阅读本 skill 的策略规则部分
2. 读取 `交易记录.md`，获取：
   - 当前合并有效成本（如有 BTC 持仓）
   - 当前 BTC 持仓量
   - 最近订单状态

### Phase 2：复盘已到期订单

1. 查询最近订单：
   - MCP：`dcd_get_orders`
   - CLI：`okx earn dcd orders --json --live`
2. 如有到期订单：记录结算结果到 `交易记录.md`
3. PUT 被行权 → 更新合并有效成本（新用户首次被行权：有效成本 = 行权价）
4. CALL 被行权 → 记录 BTC 已卖出，回到纯 PUT 模式
5. 如有资金在交易账户，划转到资金账户：
   - MCP：`account_transfer`（from: 18, to: 6）
   - CLI：`okx account transfer --ccy <ccy> --amt <n> --from 18 --to 6 --json --live`

### Phase 3：并行获取数据

以下操作按当前模式执行。CLI 模式下可后台并行（`&` + `wait`）：

| 数据 | MCP 工具 | CLI 命令 |
|------|---------|---------|
| BTC 现价 | `market_get_index_ticker`(instId: BTC-USD) | `okx market index-ticker --instId BTC-USD --json --live` |
| 30天K线 | `market_get_index_candles`(instId: BTC-USD, bar: 1D, limit: 30) | `okx market index-candles BTC-USD --bar 1D --limit 30 --json --live` |
| 快速ATR | `market_get_indicator`(instId: BTC-USDT, indicator: ATR, period: 7, bar: 1H) | `okx market indicator atr BTC-USDT --period 7 --bar 1H --json --live` |
| 慢速ATR | `market_get_indicator`(instId: BTC-USDT, indicator: ATR, period: 14, bar: 1H) | `okx market indicator atr BTC-USDT --period 14 --bar 1H --json --live` |
| BB指标 | `market_get_indicator`(instId: BTC-USDT, indicator: BOLLINGER, period: 20, bar: 1H) | `okx market indicator bollinger BTC-USDT --period 20 --bar 1H --json --live` |
| 资金费率 | `market_get_funding_rate`(instId: BTC-USD-SWAP) | `okx market funding-rate BTC-USD-SWAP --json --live` |
| IV | `option_get_greeks`(uly: BTC-USD) | `okx option greeks --uly BTC-USD --json --live` |
| PUT USDG产品 | `dcd_get_products`(baseCcy: BTC, quoteCcy: USDG, optType: P) | `okx earn dcd products --baseCcy BTC --quoteCcy USDG --optType P --json --live` |
| PUT USDT产品 | `dcd_get_products`(baseCcy: BTC, quoteCcy: USDT, optType: P) | `okx earn dcd products --baseCcy BTC --quoteCcy USDT --optType P --json --live` |
| CALL USDG产品 | `dcd_get_products`(baseCcy: BTC, quoteCcy: USDG, optType: C) | `okx earn dcd products --baseCcy BTC --quoteCcy USDG --optType C --json --live` |
| CALL USDT产品 | `dcd_get_products`(baseCcy: BTC, quoteCcy: USDT, optType: C) | `okx earn dcd products --baseCcy BTC --quoteCcy USDT --optType C --json --live` |
| 余额 | `account_get_asset_balance`(ccy: USDG,USDT,BTC) | `okx account asset-balance --ccy USDG,USDT,BTC --json --live` |
| 事件日历 | WebSearch | WebSearch（两端相同） |

### Phase 4：v3 波动率计算 + 选品

#### 4a. 事件判断（不跳过，只决定乘数）
- 今天/明天有事件 → 用对应事件乘数 + min_dist = 5%
- 无事件 → mult = 1.0, min_dist = 3.5%

#### 4b. v3 波动率计算

优先调用 `scripts/calc_volatility.py` 计算：

```bash
python3 scripts/calc_volatility.py \
  --price <BTC现价> \
  --iv-annual <年化IV> \
  --atr-fast <ATR_7_1H> --atr-slow <ATR_14_1H> \
  --bb-upper <BB上轨> --bb-lower <BB下轨> \
  --funding-rate <资金费率> \
  --high-30d <30天高点> --price-7d-ago <7天前价格> \
  --event-mult <事件乘数> --min-dist <最小安全距离> \
  --expiry-days <到期天数>
```

脚本不可用时，按上方"完整计算公式"手动计算。

#### 4c. PUT 选品
```
buy_dist = max(vol_24h * sqrt(到期天数) * 事件乘数, min_dist)
PUT 行权价 = price * (1 - buy_dist)，向下取整到 500
```

1. 行权价 < 安全下界的产品中筛选
2. 同一行权价+到期日，比较 USDG 和 USDT 产品，选 APY 更高的
3. ✅ 中选 APY 最高（≤150%），全部 APY < 5% 则跳过
4. 优先 1 天产品
5. 同 APY 选距安全下界更远的

#### 4d. CALL 选品（仅当有 BTC 持仓时）

从 `交易记录.md` 读取合并有效成本。无 BTC 持仓则跳过。

1. 行权价 >= 合并有效成本，选最接近的
2. 同一行权价+到期日，比较 USDG 和 USDT 产品，选 APY 更高的
3. 多个符合条件时选 APY 最高
4. 优先 1 天产品

### Phase 5：下单 + 记录

1. **PUT 下单**（USDG >= 10 或 USDT >= 10）：
   - MCP：`dcd_subscribe`（productId, investAmt, ...）
   - CLI：`okx earn dcd quote-and-buy --productId <id> --sz <amt> --notionalCcy <ccy> --json --live`

2. **CALL 下单**（BTC >= 0.0001 且有合并有效成本）：
   - MCP：`dcd_subscribe`（productId, investAmt, ...）
   - CLI：`okx earn dcd quote-and-buy --productId <id> --sz <btc_amt> --notionalCcy BTC --json --live`

3. 更新 `交易记录.md`（按 `templates/trade_log.md` 中的格式追加条目，并同步更新文件头部的合并有效成本、累计权利金汇总）


### 硬性约束检查清单（下单前逐条验证）

- [ ] PUT 行权价 < 安全下界
- [ ] PUT 安全距离 >= 3.5%（事件日 >= 5%）
- [ ] CALL 行权价 >= 合并有效成本（如有 BTC）
违反任一条 → 跳过该笔，不下单，记录原因。 

### 错误处理

- **CLI 命令失败**：重试 1 次。仍失败则记录错误到 `交易记录.md`，跳过本轮，不下单
- **`quote-and-buy` 下单失败**：不重试（避免重复下单），记录错误原因，等待下一轮
- **`option_get_greeks` 不可用**：降级为 30 天 HV（见 IV 降级规则），不影响交易
- **WebSearch 获取事件日历失败**：按"无事件"处理（mult=1.0, min_dist=3.5%），这是保守安全的默认值
- **`交易记录.md` 读取失败**：跳过本轮，通知用户检查文件

---

## 风险提示

- 本策略涉及真实资金交易，使用者需自行承担所有风险
- PUT 被行权意味着以行权价买入 BTC，可能面临 BTC 下跌风险
- 建议先小额测试，熟悉流程后再加大投入
