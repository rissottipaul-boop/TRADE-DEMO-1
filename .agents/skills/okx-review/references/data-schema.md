# Position History JSON 字段映射

本 skill 接受 OKX 官方 agent-skills（`github.com/okx/agent-skills`）产出的 **position history** JSON，
对应 OKX v5 REST API `GET /api/v5/account/positions-history`。

字段名和语义严格对齐 [OKX 官方文档](https://www.okx.com/docs-v5/en/#trading-account-rest-api-get-positions-history)。

---

## 接受的输入结构

两种形式都支持：

1. **裸数组**：`[{position_1}, {position_2}, ...]`
2. **OKX 标准响应包裹**：`{"code": "0", "msg": "", "data": [...]}` — 自动解包 `data` 字段

---

## 字段映射

| 本地字段 | OKX 原字段 | 说明 |
|---|---|---|
| `okx_position_id` | `posId` | 去重键。OKX 由 mgnMode + posSide + instId + ccy 组合生成 |
| `symbol` | `instId` | e.g. `BTC-USDT-SWAP`、`ETH-USDT-SWAP` |
| `direction` | `direction` / `posSide` | net mode 用 `direction`（long/short）；long/short mode 用 `posSide` |
| `leverage` | `lever` | 杠杆倍数 |
| `margin_mode` | `mgnMode` | `cross` / `isolated` |
| `size` | `closeTotalPos`（主）/ `openMaxPos`（退化） | 平仓总量；若缺则用开仓最大量 |
| `entry_price_avg` | `openAvgPx` | 加权开仓均价 |
| `exit_price_avg` | `closeAvgPx` | 加权平仓均价 |
| `opened_at` | `cTime` | 头寸创建时间（毫秒 unix 时间戳） |
| `closed_at` | `uTime` | 头寸更新（平仓）时间（毫秒 unix 时间戳） |
| `realized_pnl` | `realizedPnl`（主）/ `pnl`（退化） | OKX 定义的最终净盈亏；缺失时退化到 `pnl` |
| `total_fee` | `fee + fundingFee + liqPenalty` | **本地合并三项成本**（OKX 三项都是负值） |
| `raw_payload` | *全量对象* | 原始 JSON 字符串，便于调试和未来补字段 |

---

## 重要语义细节

### `realizedPnl` vs `pnl`

- `pnl` = 纯价格盈亏（不含成本）
- `realizedPnl` = `pnl + fee + fundingFee + liqPenalty` = **最终净盈亏**

本地 `realized_pnl` 字段优先取 `realizedPnl`（更精确），退化到 `pnl`。
如果用户的数据源是 `pnl`，dashboard 的"总盈亏"就不含手续费，**"总手续费"字段仍会从 `fee + fundingFee + liqPenalty` 汇总得到**，两者语义独立。

### 方向推断

- `posSide = "net"`（单向持仓模式）→ 看 `direction` 字段（`"long"` / `"short"`）
- `posSide = "long"` / `"short"`（双向持仓模式）→ 直接用

### 时间格式

OKX 原生返回**毫秒 unix 时间戳字符串**（e.g. `"1708934400000"`）。ingest 统一转成 ISO8601 UTC 存 SQLite（e.g. `2024-02-26T14:00:00+00:00`）。

### ⚠️ PnL 计价单位不统一（跨 instType 的大坑）

OKX 返回的 `pnl` / `realizedPnl` / `fee` 等数值的**单位由 `ccy` 字段决定**，不是都以 USDT 计价：

| instType | 典型 ccy | pnl 单位 |
|---|---|---|
| `SWAP`（USDT/USDC 本位） | `USDT` / `USDC` | 美元 ✓ |
| `SWAP`（币本位如 BTC-USD-SWAP） | `BTC` 等 base token | **base token 数量** |
| `MARGIN`（现货杠杆） | base token（如 `NEIRO`） | **base token 数量** |
| `FUTURES` / `OPTION` | 视合约而定 | 同上规则 |

**本 skill 在 ingest 时统一换算成 USDT**：若 `ccy` 不在 `{USDT, USDC, USD, ""}` 内，则乘以 `closeAvgPx`（= 以 USDT 计的 base 价格）得到 USDT 等值。

这条规则同时应用于 `realizedPnl` / `pnl` / `fee` / `fundingFee` / `liqPenalty` 五个字段。

实测案例：某 NEIRO-USDT MARGIN 头寸返回 `pnl = -19451 (NEIRO)`，换算后 `-19451 × 0.00192 ≈ -37.34 USDT`。不换算会导致总盈亏严重失真。

### 成本构成（SWAP）

SWAP 头寸的总成本由三部分构成：
- `fee`：交易手续费（开仓 + 平仓的吃/挂单费）
- `fundingFee`：资金费率累计结算
- `liqPenalty`：若被强平，这里是强平罚金

本 skill 合并为单一 `total_fee`。若未来需要分项展示，读 `raw_payload` 里的原始对象即可。

---

## 去重策略

**⚠️ 重要**：OKX 的 `posId` 是 `(mgnMode, posSide, instId, ccy)` 的组合 hash，**同一币种反复开平仓会复用同一个 posId**，只是 `cTime` / `uTime` 不同。因此不能仅用 `posId` 去重。

本 skill 的唯一键是 **`(okx_position_id, closed_at)`**：同一 posId 的多次生命周期按 `uTime` 区分保留。

重复 `ingest` 走 `INSERT OR IGNORE`，不覆盖已有行。如需强制刷新某条数据，先 `clean --before ...` 再重 `ingest`。

实测例子：demo 账户同一 DOGE-USDT-SWAP cross 方向 3 次开平仓，返回 3 条记录，posId 全相同，uTime 不同，pnl 各异。若按 posId 单独去重会丢失 2 条真实数据。

---

## 最小示例（真实 OKX 字段）

```json
{
  "code": "0",
  "msg": "",
  "data": [
    {
      "posId": "733328045967261697",
      "instId": "BTC-USDT-SWAP",
      "instType": "SWAP",
      "mgnMode": "cross",
      "posSide": "net",
      "direction": "long",
      "lever": "10",
      "openAvgPx": "62345.5",
      "closeAvgPx": "63120.0",
      "openMaxPos": "0.1",
      "closeTotalPos": "0.1",
      "cTime": "1708934400000",
      "uTime": "1708956000000",
      "pnl": "77.45",
      "realizedPnl": "77.13",
      "fee": "-0.32",
      "fundingFee": "0",
      "liqPenalty": "0",
      "pnlRatio": "0.0124",
      "ccy": "USDT",
      "uly": "BTC-USDT",
      "type": "2"
    }
  ]
}
```

经 `ingest` 规范化后存入 SQLite `position` 表。

---

## 参考

- OKX API v5 docs: <https://www.okx.com/docs-v5/en/#trading-account-rest-api-get-positions-history>
- 时间范围由 OKX 官方 agent-skill 决定（实测可拿到一年以上历史）
- Portfolio margin 模式自 2024-11-11 支持
