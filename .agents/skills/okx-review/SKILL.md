---
name: okx-review
version: "0.3.0"
license: MIT
description: |
  OKX 交易本地复盘。配合 OKX 官方 agent-skills 使用：官方 skill 拉取你的 OKX 头寸历史 JSON，
  本 skill 把数据落到本地 SQLite，起一个本地 Flask dashboard（多维图表 + 头寸列表 + 自定义标签系统 + 复盘笔记记录），
  浏览器打开即看盈亏曲线、胜率、手续费、按月表现。数据全留本地，不上云。
  当用户说"OKX 复盘"、"看我 OKX 交易表现"、"生成 OKX 盈亏报告"、"OKX 胜率统计"时使用此技能。
metadata:
  openclaw:
    requires:
      bins: [python]
    os: [win32, darwin, linux]
    install:
      - kind: pip
        package: flask
        label: Install Flask (pip)
---

# OKX 本地复盘

> 把 OKX 官方 skill 拉下来的头寸历史存到本地 SQLite，起一个本地 Flask dashboard，浏览器直开即看。

```
一条命令走全程（数据全部由 OKX 官方 agent-skill 提供 JSON）：

OKX 官方 skill → positions-history JSON   → pos.json
OKX 官方 skill → public-instruments JSON  → inst.json   (首次 / 陈旧时需要)

python okx_review.py serve --positions pos.json [--instruments inst.json]
    ↓ 规范化 + 去重 → 写入 ~/.okx-review/data.db
    ↓ 起本地 Flask 服务（http://127.0.0.1:4738）
    ↓ 自动打开浏览器：dashboard + 图表 + 头寸表 + 打标签 + 复盘笔记
```

---

## 前置条件

1. **AI 客户端**：Claude Code 或 OpenClaw
2. **Python**：>= 3.10
3. **flask**：`pip install flask`
4. **OKX 官方 agent-skills**：已安装并配好（见 <https://github.com/okx/agent-skills>）—— 本 skill 所有数据都从它来
5. **OKX API Key**：由官方 skill 管理，只需 Read 权限

---

## 典型使用

对 AI 助手说：

- **"复盘我最近一个月的 OKX 交易"**
- **"看我 OKX 永续合约的表现"**
- **"查看我 完整历史 的 OKX 交易"**

助手会自动执行：

1. 调 OKX 官方 skill 获取 positions-history JSON（以及首次 / 陈旧时的 public-instruments JSON）
2. 调本 skill 的 `serve --positions ... [--instruments ...]`：ingest → 起 Flask → 开浏览器，一条命令搞定

如果返回的 JSON 里出现 `instruments_hint`（本金列缺数据），助手会再拉一次 public-instruments 并重新 serve。

---

## 运行方式

```bash
python "<skillDir>/scripts/okx_review.py" <subcommand> [options]
```

子命令详情见 `references/cli-contract.md`。所有子命令输出 JSON，便于 AI 助手解析。

### 常用子命令

| 命令                                                           | 说明                                                                                          |
| -------------------------------------------------------------- | --------------------------------------------------------------------------------------------- |
| `serve [--positions <file>] [--instruments <file>] [--port N]` | **主入口**。给了 `--positions` 就先 ingest 再起服务；浏览器里看 dashboard、打标签、写复盘笔记 |
| `ingest --positions <file>`                                    | 只 ingest 不起服务（stdin 管道场景）                                                          |
| `ingest-instruments --file <file>`                             | 只 ingest 合约规格（stdin 管道场景）                                                          |
| `status`                                                       | 打印数据库状态（上次 ingest 时间、头寸总数、日期范围）                                        |
| `clean --before DATE --yes`                                    | 清理旧数据                                                                                    |

---

## 数据流

- **输入**：全部由 OKX 官方 agent-skill 提供 JSON —— positions-history + public-instruments（字段映射见 `references/data-schema.md`）
- **存储**：`~/.okx-review/data.db`（SQLite）
- **出口**：本地 Flask dashboard（`http://127.0.0.1:4738`）

---

## 隐私

所有数据只在用户本地：`~/.okx-review/data.db`（SQLite 数据库）。

本 skill 自身**不直接联网**；OKX API 的调用完全由官方 agent-skill 负责（Read 权限）。数据不上传任何第三方。

---

## 边界与限制

- 只吃 position history，不处理 fills/bills。总手续费依赖 OKX position 数据里是否带 `fee/fundingFee/liqPenalty` 字段
- `serve` 前台运行（Ctrl-C 停止），无 daemon；多实例需手动指定不同 `--port`
- 重复 ingest 根据 `(okx_position_id, closed_at)` 复合键去重

---

## 版本与变更

- **v0.3（当前）**：`serve` 变成唯一主入口，直接接受 `--positions` / `--instruments` 做一键 ingest + 启动；删除 `report` 子命令（静态 HTML 导出不再维护，功能被交互式 dashboard 覆盖）
- **v0.2**：新增 `ingest-instruments`（本金列所需的合约规格）、`serve`（Flask + HTMX 交互，标签 + 复盘笔记）
- **v0.1**：`ingest` + `report`（生成单文件 HTML），纯只读
