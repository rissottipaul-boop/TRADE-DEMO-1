---
name: hyperliquid-analyzer
description: "Hyperliquid DEX on-chain intelligence for OKX trading. Trigger on: 'hyperliquid', 'HL', 'HYPE', '巨鲸', 'whale', 'DEX whale', 'HL signal', 'HL funding', 'HL portfolio', or any Hyperliquid question. Capabilities: whale hunt (scan recent trades → on-chain portfolios, find accounts >$500K), order wall analysis (large bid/ask clusters as support/resistance), autonomous whale monitor (push alerts on new positions/size changes via Telegram/webhook), wallet portfolio lookup (entry price, liquidation price, unrealized PnL), HL vs OKX funding rate comparison (crowd positioning signal), HL vs OKX price spread. Do NOT use for OKX-only queries (use okx-cex-market), placing orders (use okx-cex-trade), or other DEXes (GMX, dYdX)."
---

# Hyperliquid 信号情报工具

Hyperliquid 是透明的链上 DEX：巨鲸真实持仓、清算价、大单挂墙全部公开可读。这个 skill 把这些链上情报转化为可在 OKX 执行的交易信号——谁在开大仓、多空在哪里集中、资金费率是否出现极端偏离。

## 依赖工具

- **jq**（必须）：所有 shell 脚本依赖 JSON 解析。未安装时提示：`brew install jq`
- **python3**（必须）：巨鲸扫描脚本依赖，系统通常已内置
- **okx CLI**（可选）：跨平台对比功能需要。未安装时，HL 独立功能正常工作

## 分析类型

**链上情报（核心价值）**

| 类型 | 用途 | 脚本 |
|------|------|------|
| **巨鲸成交扫描** | 从成交流溯源大户地址，批量查持仓和清算价 | `hl_whale_hunt.py [min_usd] [top_n_coins]` |
| **巨鲸挂单墙** | 扫描订单簿找大单聚集区，作为支撑/阻力参考 | `hl_whales.sh [min_usd] [COINS]` |
| **巨鲸持续监控** | 定时检测持仓变化，新开仓/加减仓自动推送 | `hl_whale_monitor.py [--dry-run]` |
| **链上持仓查询** | 任意地址的仓位、入场价、清算价、浮盈亏 | `hl_portfolio.sh` |

**OKX 对比参考**

| 类型 | 用途 | 脚本 |
|------|------|------|
| **资金费率对比** | HL vs OKX 费率偏离，判断多空拥挤程度 | `compare_funding.sh` |
| **价格价差** | HL vs OKX 基差，DEX 相对 CEX 的情绪偏差 | `compare_price.sh` |

**辅助查询**

| 类型 | 脚本 |
|------|------|
| HL 价格 | `hl_price.sh` |
| HL 市场概览 | `hl_market.sh` |
| HL 资金费率排名 | `hl_funding.sh` |

## 执行流程

### Step 0: 环境检查

```bash
command -v jq >/dev/null 2>&1 || {
  echo "需要安装 jq: brew install jq (macOS) / apt install jq (Linux)"; exit 1
}
```

如 jq 不可用，立即告知用户安装方式并停止执行。

### Step 1: 识别分析意图

**链上情报（优先匹配）**：
- **谁在开大仓 / 大户持仓 / 扫巨鲸** → `hl_whale_hunt.py`
- **支撑阻力 / 大买墙 / 大卖墙** → `hl_whales.sh`
- **开启监控 / 定时推送 / 开仓信号** → `hl_whale_monitor.py`
  - 参数：`--min-account`（巨鲸门槛，默认 $500K）、`--min-position`（报警仓位，默认 $200K）、`--change-threshold`（加减仓比例，默认 30%）、`--top-coins`（扫描币种数，默认 20）
- **钱包地址持仓 / 清算价** → `hl_portfolio.sh`

**OKX 对比参考**：
- **费率偏离 / 多空拥挤 / HL vs OKX 费率** → `compare_funding.sh`
- **DEX/CEX 价差 / 基差** → `compare_price.sh`

**辅助查询**：
- HL 价格 → `hl_price.sh`；市场行情 → `hl_market.sh`；HL 费率排名 → `hl_funding.sh`

意图不明确时询问，例如：
- "您是想一次性扫描巨鲸，还是开启持续监控定期推送信号？"

### Step 2: 执行脚本

所有脚本位于 skill 目录的 `scripts/` 子目录。执行前先用 `ls` 或工具定位 skill 绝对路径（典型路径：`~/.openclaw/workspace/skills/hyperliquid-analyzer/scripts/`）。

```bash
SCRIPTS=~/.openclaw/workspace/skills/hyperliquid-analyzer/scripts

# Shell 脚本
bash $SCRIPTS/hl_price.sh BTC ETH
bash $SCRIPTS/compare_funding.sh BTC ETH SOL
bash $SCRIPTS/hl_portfolio.sh 0xWALLET_ADDRESS
bash $SCRIPTS/hl_whales.sh 500000 BTC ETH   # 挂单墙，可选参数：最小USD + 币种列表

# Python 脚本
python3 $SCRIPTS/hl_whale_hunt.py 500000 20         # 一次性扫描：最小USD 币种数
python3 $SCRIPTS/hl_whale_monitor.py --dry-run       # 首次测试（不发消息）
python3 $SCRIPTS/hl_whale_monitor.py \               # 生产运行（带自定义参数）
  --min-account 500000 \    # 巨鲸净值门槛 USD
  --min-position 200000 \   # 值得报警的仓位 USD
  --change-threshold 0.30 \ # 加减仓触发比例
  --top-coins 20            # 扫描高OI币种数
```

**巨鲸监控的 OpenClaw Cron 配置**：

```bash
openclaw cron add \
  --name hl-whale-monitor \
  --schedule "*/30 * * * *" \
  --skill hyperliquid-analyzer \
  --prompt "执行 python3 ~/.openclaw/workspace/skills/hyperliquid-analyzer/scripts/hl_whale_monitor.py，脚本会自动扫描巨鲸、对比仓位变化、发送信号。无需其他操作。"
```

消息渠道通过环境变量配置（在 `~/.zshrc` 或系统环境中设置）：

```bash
export NOTIFY_CHANNEL=telegram   # telegram | webhook | none
export TG_BOT_TOKEN=xxx
export TG_CHAT_ID=yyy
# 或 webhook:
export NOTIFY_CHANNEL=webhook
export NOTIFY_WEBHOOK_URL=https://...
```

**Portfolio 类脚本的钱包地址处理**：
- 用户已提供 → 直接使用
- 环境变量 `HYPERLIQUID_WALLET_ADDRESS` 已设置 → 自动使用
- 两者均无 → 告知用户提供，并说明仅读取公开链上数据，无需私钥

### Step 3: 结果分析与建议

**巨鲸信号解读**：
- 净值 >$1M 巨鲸新开仓 → 强方向参考，可跟进在 OKX 同向建仓
- 多个巨鲸同向操作同一币 → 叠加信号，可信度更高
- 巨鲸清算价密集靠近当前价 → 踩踏风险（空头密集尤其关注）
- 挂单墙被大量吃掉 → 突破信号；挂单墙持续存在 → 支撑/阻力有效
- 注意：成交流每币种约 10 笔快照，不代表全量巨鲸

**资金费率对比解读**（核心是"拥挤程度"信号，不是套利）：
- OKX 费率显著高于 HL → OKX 多头更拥挤，在 OKX 追多需警惕反转
- HL 费率显著高于 OKX → HL 链上多头承压，可参考 OKX 方向反向做
- 两所费率方向相反 → 市场分歧最大，等待方向确认，避免追势
- 任一平台费率极端（>0.05% / 8h，年化 >55%）→ 拥挤一侧过热，反转概率上升
- 切勿将费率差直接解读为"套利空间"——费率随时会变，且 HL 结算时间与 OKX 不同步

**价格价差解读**：
- HL mark 高于预言机（正溢价）→ DEX 情绪偏乐观，注意回调
- 跨所价差 > 0.1% → 两平台定价分歧，方向交易注意用哪个价格参考

### Step 4: 后续引导

- 查完价格/行情 → "需要扫描当前有哪些巨鲸在这个币上开仓吗？"
- 查完巨鲸扫描 → "需要开启持续监控，有开仓变化时自动推送信号吗？"
- 查完挂单墙 → "需要查这些巨鲸地址的完整持仓和清算价吗？"
- 查完资金费率 → "费率出现极端偏离，需要看看链上巨鲸的方向吗？"
- 查完持仓 → "需要监控这个地址的仓位变化吗？"

## 错误处理

### API 不可达
```
无法连接 Hyperliquid API (https://api.hyperliquid.xyz/info)
可能原因: 网络问题 / API 维护 / 地区限制
请检查网络连接后重试
```

### 速率限制 (HTTP 429)
```
Hyperliquid API 速率限制: 120 次/分钟
请稍等片刻后重试
```

### 币种未上市
```
"[COIN]" 未在 Hyperliquid DEX 上市
运行 hl_price.sh 查看全部可用资产
```

## 重要说明

- **只读**：所有操作仅获取公开市场数据，不提交交易或访问私钥
- **无需 API Key**：Hyperliquid 公开数据接口无需认证
- **OKX 必须用实盘**：所有 okx 命令使用 `--live` 标志，避免误读模拟盘数据
- **价格类型**：`allMids` 返回订单簿中间价；`metaAndAssetCtxs` 含标记价和预言机价
- **资金费率周期**：8 小时结算；年化 = 8h 费率 × 3 × 365
- **巨鲸监控状态文件**：默认存于 `~/.hl-whale-monitor/whale_positions.json`（可用 `--state-file` 覆盖），首次运行建立基准，第二次起检测变化
