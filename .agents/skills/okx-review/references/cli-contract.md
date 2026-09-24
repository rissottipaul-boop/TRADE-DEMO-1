# CLI 契约

本 skill 的所有子命令都输出一行 JSON（最后一行），便于 AI 助手解析。`stderr` 输出人类可读日志。

调用形式：

```bash
python <skillDir>/scripts/okx_review.py <subcommand> [options]
```

> 本 skill 自身不联网，所有 OKX API 数据由 OKX 官方 agent-skill 提供 JSON 喂入。

---

## `ingest`

从 OKX 官方 agent-skill 产出的 positions-history JSON 导入，去重后写入 `~/.okx-review/data.db`。

**参数**：

| 参数 | 必填 | 说明 |
|---|---|---|
| `--positions <path>` | 是 | position history JSON 文件路径，或 `-` 表示从 stdin 读 |
| `--dry-run` | 否 | 只解析不写库 |

**成功输出**：

```json
{
  "ok": true,
  "ingested": 142,
  "skipped_duplicates": 3,
  "total_positions_in_db": 389,
  "date_range": {"from": "2025-10-01T00:00:00+00:00", "to": "2026-04-14T13:22:00+00:00"},
  "db_path": "/Users/x/.okx-review/data.db",
  "instruments_hint": {
    "status": "empty",
    "positions_needing_instruments": 128,
    "message": "instrument 表为空，dashboard 的「本金」列将显示 '-'..."
  }
}
```

> `instruments_hint` 仅当 DB 里存在 SWAP/FUTURES/OPTION 头寸、且 `instrument` 表为空或超过 30 天未更新时出现。AI 助手可据此决定是否自动跑一次 `ingest-instruments`。

**失败输出**：

```json
{"ok": false, "error_kind": "parse_error", "message": "字段 closedAvgPx 缺失"}
```

---

## `ingest-instruments`

导入 OKX `/api/v5/public/instruments` 的 JSON，写入 `instrument` 表。用于 dashboard 计算 SWAP/FUTURES/OPTION 头寸的**名义本金**（`size × ctVal × entry_price`）。

数据来源：**OKX 官方 agent-skill**（`/api/v5/public/instruments` 是公开接口，不需 API Key）。

**参数**：

| 参数 | 必填 | 说明 |
|---|---|---|
| `--file <path>` | 是 | public-instruments JSON 文件路径，或 `-` 表示从 stdin 读 |

**接受的输入格式**：

1. OKX 标准响应：`{"code": "0", "msg": "", "data": [{...}]}`
2. 裸数组：`[{"instId": "...", "ctVal": "...", ...}, ...]`

SWAP、FUTURES、OPTION、MARGIN、SPOT 等所有 `instType` 都吃；按 `instId` upsert（重复导入会覆盖旧规格）。

**成功输出**：

```json
{"ok": true, "instruments_ingested": 342, "db_path": "/Users/x/.okx-review/data.db"}
```

典型工作流（对 AI 助手）：

```
1. 调 OKX 官方 skill → /api/v5/public/instruments?instType=SWAP   → ok-swap.json
2. 调 OKX 官方 skill → /api/v5/public/instruments?instType=FUTURES → ok-fut.json
3. cat ok-swap.json  | okx_review ingest-instruments --file -
4. cat ok-fut.json   | okx_review ingest-instruments --file -
```

---

## `serve`（主入口）

一条命令走全程：可选先 ingest，再起本地 Flask 服务，自动开浏览器。前台运行，Ctrl-C 停止。依赖 `flask`。

**参数**：

| 参数 | 必填 | 说明 |
|---|---|---|
| `--positions <path>` | 否 | 给了就先 ingest 这份 positions-history JSON（文件路径或 `-` 表示 stdin）再起服务 |
| `--instruments <path>` | 否 | 给了就先 ingest-instruments 这份 public-instruments JSON 再起服务 |
| `--host <ip>` | 否 | 绑定 host，默认 `127.0.0.1` |
| `--port <n>` | 否 | 端口，默认 `4738` |
| `--no-browser` | 否 | 不自动打开浏览器 |

当既没给 `--positions` 也没有现成 DB（`~/.okx-review/data.db`）时，直接报 `db_error`。

**启动时立即输出**（随后 app.run 阻塞）：

```json
{
  "ok": true,
  "url": "http://127.0.0.1:4738",
  "host": "127.0.0.1",
  "port": 4738,
  "db_path": "/Users/x/.okx-review/data.db",
  "ingest": {
    "positions": {
      "ingested": 42,
      "skipped_duplicates": 96,
      "total_positions_in_db": 138,
      "date_range": {"from": "2024-09-24T01:33:18+00:00", "to": "2025-12-12T08:16:23+00:00"}
    },
    "instruments": {"instruments_ingested": 342}
  },
  "instruments_hint": { "status": "empty", "...": "..." }
}
```

`ingest` / `instruments_hint` 仅在传了对应参数 / 满足触发条件时出现。

**路由**：

| Method | Path | 说明 |
|---|---|---|
| GET | `/` | 主 dashboard（汇总卡片 + 图表 + 头寸表 + 详情侧栏） |
| GET | `/positions/<id>` | 头寸详情 HTML 片段（HTMX 目标） |
| POST | `/positions/<id>/tags` | 给头寸加标签（form: `name`, 可选 `color`），tag 不存在时自动创建 |
| DELETE | `/positions/<id>/tags/<tag_id>` | 从头寸移除标签 |
| POST | `/positions/<id>/reflection` | upsert 复盘笔记（form: `content`），`content` 为空则删除 |
| POST | `/tags` / PATCH `/tags/<id>` / DELETE `/tags/<id>` | 全局标签管理 |
| GET | `/health` | JSON 探活 |

---

## `status`

打印本地 DB 状态，不做任何修改。

**输出**：

```json
{
  "ok": true,
  "db_path": "/Users/x/.okx-review/data.db",
  "db_size_bytes": 81920,
  "positions_total": 389,
  "last_ingest_at": "2026-04-14T15:18:22+00:00",
  "date_range": {"from": "2025-10-01T00:00:00+00:00", "to": "2026-04-14T13:22:00+00:00"}
}
```

---

## `clean`

删除旧数据。

**参数**：

| 参数 | 必填 | 说明 |
|---|---|---|
| `--before <date>` | 是 | 删除 `closed_at < date` 的所有 positions |
| `--yes` | 否 | 跳过确认提示（在 AI 调用场景必加） |

**输出**：

```json
{"ok": true, "deleted": 23, "remaining": 366}
```

---

## 通用错误码

| `error_kind` | 含义 |
|---|---|
| `input_missing` | 必填参数缺失 |
| `file_not_found` | 输入 JSON 文件或配置文件不存在 |
| `parse_error` | JSON 格式或字段不符 |
| `db_error` | SQLite 读写失败 |
| `missing_dependency` | pip 依赖未安装（flask） |
| `internal_error` | 其他未分类，包含 OKX API 调用失败 |
