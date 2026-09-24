# OpenClaw 环境初始化

本文档仅适用于 OpenClaw（CLI 模式）环境的首次设置。

## 前置安装

```bash
# 安装 okx-trade-cli
npm install -g okx-trade-cli

# 验证安装
okx --version
```

## 配置 OKX API

编辑 `~/.okx/config.toml`，确保有实盘 profile：

```toml
default_profile = "live"

[profiles.live]
api_key = "your-live-api-key"
secret_key = "your-live-secret-key"
passphrase = "your-live-passphrase"
```

验证连通性：

```bash
okx market index-ticker --instId BTC-USD --json --live
```

## 安装 Skill

将 `dcd-auto-trader` 整个目录复制到 OpenClaw 的 skills 目录：

```bash
cp -r ./dcd-auto-trader ~/.openclaw/workspace/skills/dcd-auto-trader
```

## 配置 SOUL.md

将 `templates/soul_dcd.md` 的内容合并到你的 `~/.openclaw/SOUL.md` 中。

关键点：`soul_dcd.md` 告诉 agent 策略文档的位置（`~/.openclaw/workspace/skills/dcd-auto-trader/SKILL.md`），agent 每次执行时会先阅读完整策略。

## 创建项目目录与交易记录

```bash
mkdir -p ~/dcd-project
cp ~/.openclaw/workspace/skills/dcd-auto-trader/templates/trade_log.md ~/dcd-project/交易记录.md
```

## 注册定时任务

方式一（OpenClaw cron，推荐）：

```bash
openclaw cron add \
  --name dcd-auto-trader \
  --schedule "5 17 * * *" \
  --skill dcd-auto-trader \
  --prompt "$(cat <<'EOF'
你是 DCD 自动交易器。现在执行每日交易流程。

1. 先阅读完整策略：读取 ~/.openclaw/workspace/skills/dcd-auto-trader/SKILL.md
2. 读取 ~/dcd-project/交易记录.md 获取当前持仓状态
3. 按 SKILL.md 中的"每日执行流程"（Phase 1 到 Phase 5）逐步执行
4. 所有 okx 命令加 --json --live 参数
5. 波动率计算用 python3 ~/.openclaw/workspace/skills/dcd-auto-trader/scripts/calc_volatility.py
6. 完成后更新 ~/dcd-project/交易记录.md

核心原则：每天都交易，永不跳过。事件日通过乘数加大安全距离。
EOF
)"
```

> **为什么 prompt 要写这么详细？** OpenClaw 的 cron 触发不像 Claude Code 的 scheduled-tasks 会自动加载项目上下文。prompt 必须明确告诉 agent：策略文件在哪、交易记录在哪、用什么工具、按什么流程执行。SKILL.md 中有完整的 Phase 1-5 步骤，agent 读取后就能自主执行。不建议简化 prompt 内容——缩短后 agent 可能无法正确定位文件和执行流程。
