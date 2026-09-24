# 使用示例

本 skill 自身不联网。所有 OKX 数据由 **OKX 官方 agent-skill** 提供 JSON 喂入。

---

## 一、前置

```bash
pip install flask
```

并装好 OKX 官方 agent-skill（<https://github.com/okx/agent-skills>），它负责管理 API Key（只需 Read 权限）。

---

## 二、典型对话

**用户**：「复盘我 OKX 所有交易」

**助手的执行顺序**（一条命令走全程）：

```
1. 调 OKX 官方 skill → positions-history JSON   →  /tmp/pos.json
2. 调 OKX 官方 skill → public-instruments JSON  →  /tmp/inst.json   (首次 / 陈旧时)
3. python <skillDir>/scripts/okx_review.py serve \
       --positions /tmp/pos.json \
       --instruments /tmp/inst.json
   ↓ ingest → 起 Flask → 自动开浏览器
```

已有 DB 的日常刷新，也只是重复步骤 1 + 3，`--instruments` 可省。

走 stdin 管道省掉中间文件（需要用单独的 ingest 子命令）：

```bash
okx_official_skill positions-history | python okx_review.py ingest --positions -
okx_official_skill public-instruments | python okx_review.py ingest-instruments --file -
python okx_review.py serve
```

---

## 三、增量更新

`ingest` 走 `INSERT OR IGNORE`、按 `(okx_position_id, closed_at)` 复合键去重。隔几天让官方 skill 重新拉一份完整 JSON 喂进 `serve --positions` 即可，重复部分自动跳过。

`instrument` 表也是 upsert，按 `instId` 覆盖。启动时若 instrument 数据超过 30 天未更新，输出 JSON 里会出现 `instruments_hint` 字段提示 AI 助手刷一次。

---

## 四、查看当前状态

```bash
python okx_review.py status
```

输出本地 DB 情况：总头寸数、上次入库时间、日期范围。

---

## 五、清理旧数据

```bash
python okx_review.py clean --before 2025-01-01 --yes
```

---

## 六、数据文件位置

| 路径 | 说明 |
|---|---|
| `~/.okx-review/data.db` | SQLite 主库（positions + instruments + tags + reflections） |

可以随时把 `~/.okx-review/` 整个删掉重新开始。

---

## 为什么拆成两个 skill

- **OKX 官方 skill 的职责**：API 认证、调 REST、下单、跨产品线（spot/swap/futures/option）
- **本 skill 的职责**：本地持久化、离线分析、可视化、复盘笔记 + 标签

分离的好处：
1. 本 skill 不碰 API Key，攻击面小
2. OKX API 变更只影响官方 skill
3. 本 skill 接受任何产出同样 JSON 结构的数据源
