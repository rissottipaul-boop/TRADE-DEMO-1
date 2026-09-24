"""
okx-review CLI.

配合 OKX 官方 agent-skills 使用。主入口：

  python okx_review.py serve --positions pos.json [--instruments inst.json]

serve 一条命令：先 ingest，再起 Flask，自动开浏览器。
子命令 ingest / ingest-instruments 保留供 stdin 管道等边缘场景。

所有子命令在最后一行输出 JSON，便于 AI 助手解析。
人类可读日志写 stderr。
"""

from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
import webbrowser
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

# Windows cmd 默认不是 UTF-8，强制切换以正确输出中文 stderr 日志
for _s in (sys.stdout, sys.stderr):
    if hasattr(_s, "reconfigure") and (_s.encoding or "").lower() != "utf-8":
        try:
            _s.reconfigure(encoding="utf-8")
        except Exception:
            pass

APP_DIR = Path.home() / ".okx-review"
DB_PATH = APP_DIR / "data.db"
TEMPLATES_DIR = Path(__file__).resolve().parent.parent / "templates"
ASSETS_DIR = Path(__file__).resolve().parent.parent / "assets"

SCHEMA_SQL = """
-- 注意：OKX 的 posId 是 (mgnMode, posSide, instId, ccy) 组合生成，
-- 同一币种反复开平仓会**复用同一个 posId**。所以唯一键必须包含 closed_at (uTime)。
CREATE TABLE IF NOT EXISTS position (
  id                INTEGER PRIMARY KEY AUTOINCREMENT,
  okx_position_id   TEXT NOT NULL,
  symbol            TEXT NOT NULL,
  direction         TEXT NOT NULL,
  leverage          REAL,
  margin_mode       TEXT,
  size              REAL,
  entry_price_avg   REAL,
  exit_price_avg    REAL,
  opened_at         TEXT,
  closed_at         TEXT NOT NULL,
  realized_pnl      REAL NOT NULL DEFAULT 0,
  total_fee         REAL NOT NULL DEFAULT 0,
  raw_payload       TEXT NOT NULL,
  ingested_at       TEXT NOT NULL,
  UNIQUE(okx_position_id, closed_at)
);

CREATE INDEX IF NOT EXISTS idx_position_closed_at ON position(closed_at);
CREATE INDEX IF NOT EXISTS idx_position_symbol    ON position(symbol);

CREATE TABLE IF NOT EXISTS sync_log (
  id                INTEGER PRIMARY KEY AUTOINCREMENT,
  ingested_at       TEXT NOT NULL,
  source            TEXT,
  ingested_count    INTEGER NOT NULL DEFAULT 0,
  skipped_count     INTEGER NOT NULL DEFAULT 0,
  error             TEXT
);

-- v0.2: 标签 & 复盘笔记
CREATE TABLE IF NOT EXISTS tag (
  id         INTEGER PRIMARY KEY AUTOINCREMENT,
  name       TEXT NOT NULL UNIQUE,
  color      TEXT,
  created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS position_tag (
  position_id INTEGER NOT NULL REFERENCES position(id) ON DELETE CASCADE,
  tag_id      INTEGER NOT NULL REFERENCES tag(id) ON DELETE CASCADE,
  created_at  TEXT NOT NULL,
  PRIMARY KEY (position_id, tag_id)
);

CREATE INDEX IF NOT EXISTS idx_position_tag_tag ON position_tag(tag_id);

CREATE TABLE IF NOT EXISTS reflection (
  position_id INTEGER PRIMARY KEY REFERENCES position(id) ON DELETE CASCADE,
  content     TEXT NOT NULL,
  created_at  TEXT NOT NULL,
  updated_at  TEXT NOT NULL
);

-- OKX 合约规格，用来算名义本金。由 ingest-instruments 喂入。
CREATE TABLE IF NOT EXISTS instrument (
  inst_id     TEXT PRIMARY KEY,
  inst_type   TEXT,
  ct_val      REAL,    -- 每张合约代表多少 base/quote token
  ct_val_ccy  TEXT,    -- ct_val 的单位
  ct_type     TEXT,    -- linear 或 inverse
  settle_ccy  TEXT,
  updated_at  TEXT
);
"""


# 预设标签：启动 serve 时 INSERT OR IGNORE 一次。
# 颜色对齐用户原来的 crypto-trade-review2 (apps/web/src/lib/system-tags.ts)。
PRESET_TAGS: list[tuple[str, str]] = [
    ("仓位过轻", "#10B981"),
    ("过度加仓", "#F97316"),
    ("技术面",   "#0EA5E9"),
    ("没有止损", "#EF4444"),
    ("心态失衡", "#EC4899"),
    ("新闻交易", "#3B82F6"),
]


def _seed_preset_tags(conn: sqlite3.Connection) -> None:
    now = datetime.now(timezone.utc).isoformat()
    with conn:
        for name, color in PRESET_TAGS:
            conn.execute(
                "INSERT OR IGNORE INTO tag (name, color, created_at) VALUES (?,?,?)",
                (name, color, now),
            )
            # 名字存在但颜色为空时补颜色
            conn.execute(
                "UPDATE tag SET color=? WHERE name=? AND (color IS NULL OR color='')",
                (color, name),
            )


# ---------- Output helpers ----------

def log(msg: str) -> None:
    print(msg, file=sys.stderr)


def emit(payload: dict) -> None:
    print(json.dumps(payload, ensure_ascii=False, default=str))


def emit_error(kind: str, message: str, **extra) -> int:
    emit({"ok": False, "error_kind": kind, "message": message, **extra})
    return 1


# ---------- DB ----------

def open_db() -> sqlite3.Connection:
    APP_DIR.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.executescript(SCHEMA_SQL)
    return conn


# ---------- JSON normalization ----------

# OKX v5 /api/v5/account/positions-history 真实字段
# 参考：https://www.okx.com/docs-v5/en/#trading-account-rest-api-get-positions-history
_ID_KEYS       = ("okx_position_id", "posId")
_SYMBOL_KEYS   = ("symbol", "instId")
_SIDE_KEYS     = ("direction", "posSide")            # net mode 下 direction 有值，long/short mode 用 posSide
_LEVER_KEYS    = ("leverage", "lever")
_MARGIN_KEYS   = ("margin_mode", "mgnMode")
_SIZE_KEYS     = ("size", "closeTotalPos", "openMaxPos")
_ENTRY_KEYS    = ("entry_price_avg", "openAvgPx")
_EXIT_KEYS     = ("exit_price_avg", "closeAvgPx")
_OPENED_KEYS   = ("opened_at", "cTime")
_CLOSED_KEYS   = ("closed_at", "uTime")
_PNL_KEYS      = ("realized_pnl", "realizedPnl", "pnl")  # realizedPnl 是 OKX 给出的最终净盈亏
_FEE_KEYS      = ("fee",)                            # 交易手续费（负值）
_FUNDING_FEE_KEYS = ("fundingFee",)                  # SWAP 资金费
_LIQ_PENALTY_KEYS = ("liqPenalty",)                  # 强平罚金


def _pick(obj: dict, keys: Iterable[str]) -> Any:
    for k in keys:
        if k in obj and obj[k] not in (None, ""):
            return obj[k]
    return None


def _to_float(v: Any) -> float | None:
    if v is None or v == "":
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _to_iso(v: Any) -> str | None:
    """Accept ms-timestamp (int or str) or ISO8601 string → ISO8601 UTC."""
    if v is None or v == "":
        return None
    if isinstance(v, (int, float)):
        return datetime.fromtimestamp(v / 1000, tz=timezone.utc).isoformat()
    s = str(v).strip()
    if s.isdigit() or (s.startswith("-") and s[1:].isdigit()):
        return datetime.fromtimestamp(int(s) / 1000, tz=timezone.utc).isoformat()
    try:
        dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.isoformat()
    except ValueError:
        return None


def _direction(raw: dict) -> str:
    side = _pick(raw, _SIDE_KEYS)
    if isinstance(side, str):
        s = side.lower()
        if s in ("long", "short"):
            return s
    size = _to_float(_pick(raw, ("pos", "size")))
    if size is not None:
        return "long" if size >= 0 else "short"
    return "long"


@dataclass
class NormalizedPosition:
    okx_position_id: str
    symbol: str
    direction: str
    leverage: float | None
    margin_mode: str | None
    size: float | None
    entry_price_avg: float | None
    exit_price_avg: float | None
    opened_at: str | None
    closed_at: str
    realized_pnl: float
    total_fee: float
    raw_payload: str


_USD_LIKE = {"USDT", "USDC", "USD", ""}


def _to_usdt(value: float | None, raw: dict) -> float | None:
    """OKX 的 pnl/fee 以 ccy 计价：
    - USDT/USDC/USD 本位合约：ccy=USDT 等，数值已是美元
    - MARGIN 现货 / 币本位合约：ccy=base token，数值是 base 数量，需乘 closeAvgPx 换算

    换算失败（ccy 不是 USD-like 且 closeAvgPx 缺失/为 0）时返回 None。
    由 caller 决定如何处理：fee 三项会被 sum 跳过；pnl 会降到 0（避免 base 数量混入 USD 总和）。
    """
    if value is None:
        return None
    ccy = str(raw.get("ccy", "")).upper()
    if ccy in _USD_LIKE:
        return value
    close_px = _to_float(_pick(raw, _EXIT_KEYS))
    if close_px is None or close_px <= 0:
        return None
    return value * close_px


def normalize_position(raw: dict) -> NormalizedPosition:
    pid = _pick(raw, _ID_KEYS)
    if pid is None:
        raise ValueError(f"position 缺少 id 字段 (tried {_ID_KEYS})")
    symbol = _pick(raw, _SYMBOL_KEYS)
    if symbol is None:
        raise ValueError(f"position {pid} 缺少 symbol")
    closed_at = _to_iso(_pick(raw, _CLOSED_KEYS))
    if closed_at is None:
        raise ValueError(f"position {pid} 缺少 closed_at/uTime")

    size = _to_float(_pick(raw, _SIZE_KEYS))

    # 总成本 = 手续费 + 资金费 + 强平罚金 (OKX 这几项都是负值)
    fee_components = [
        _to_usdt(_to_float(_pick(raw, _FEE_KEYS)), raw),
        _to_usdt(_to_float(_pick(raw, _FUNDING_FEE_KEYS)), raw),
        _to_usdt(_to_float(_pick(raw, _LIQ_PENALTY_KEYS)), raw),
    ]
    total_fee = sum(c for c in fee_components if c is not None)

    raw_pnl = _to_float(_pick(raw, _PNL_KEYS))

    return NormalizedPosition(
        okx_position_id=str(pid),
        symbol=str(symbol),
        direction=_direction(raw),
        leverage=_to_float(_pick(raw, _LEVER_KEYS)),
        margin_mode=_pick(raw, _MARGIN_KEYS),
        size=abs(size) if size is not None else None,
        entry_price_avg=_to_float(_pick(raw, _ENTRY_KEYS)),
        exit_price_avg=_to_float(_pick(raw, _EXIT_KEYS)),
        opened_at=_to_iso(_pick(raw, _OPENED_KEYS)),
        closed_at=closed_at,
        realized_pnl=_to_usdt(raw_pnl, raw) or 0.0,
        total_fee=total_fee,
        raw_payload=json.dumps(raw, ensure_ascii=False, default=str),
    )


def unwrap_payload(data: Any) -> list[dict]:
    """Accept list, or OKX-style {code, data: [...]} wrapper."""
    if isinstance(data, list):
        return data
    if isinstance(data, dict):
        if isinstance(data.get("data"), list):
            return data["data"]
        if isinstance(data.get("positions"), list):
            return data["positions"]
    raise ValueError("输入 JSON 不是数组，也不是含 data/positions 数组的包裹对象")


# ---------- Commands ----------

def _ingest_raw_list(raw_list: list[dict], source: str) -> dict:
    normalized: list[NormalizedPosition] = []
    errors: list[str] = []
    for i, item in enumerate(raw_list):
        if not isinstance(item, dict):
            errors.append(f"[{i}] 不是对象")
            continue
        try:
            normalized.append(normalize_position(item))
        except ValueError as e:
            errors.append(f"[{i}] {e}")

    conn = open_db()
    now = datetime.now(timezone.utc).isoformat()
    ingested = 0
    skipped = 0
    try:
        with conn:
            for p in normalized:
                cur = conn.execute(
                    """
                    INSERT OR IGNORE INTO position (
                      okx_position_id, symbol, direction, leverage, margin_mode,
                      size, entry_price_avg, exit_price_avg,
                      opened_at, closed_at, realized_pnl, total_fee,
                      raw_payload, ingested_at
                    ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                    """,
                    (
                        p.okx_position_id, p.symbol, p.direction, p.leverage, p.margin_mode,
                        p.size, p.entry_price_avg, p.exit_price_avg,
                        p.opened_at, p.closed_at, p.realized_pnl, p.total_fee,
                        p.raw_payload, now,
                    ),
                )
                if cur.rowcount == 1:
                    ingested += 1
                else:
                    skipped += 1
            conn.execute(
                "INSERT INTO sync_log (ingested_at, source, ingested_count, skipped_count, error) VALUES (?,?,?,?,?)",
                (now, source, ingested, skipped, "\n".join(errors) if errors else None),
            )
        total = conn.execute("SELECT COUNT(*) FROM position").fetchone()[0]
        rng = conn.execute("SELECT MIN(closed_at), MAX(closed_at) FROM position").fetchone()
    finally:
        conn.close()

    return {
        "ingested": ingested,
        "skipped_duplicates": skipped,
        "skipped_malformed": len(errors),
        "total_positions_in_db": total,
        "date_range": {"from": rng[0], "to": rng[1]},
        "errors_sample": errors[:5],
    }


def _upsert_instruments(raw_list: list[dict]) -> int:
    """把 OKX public-instruments 格式的 dict 列表 upsert 到 instrument 表。返回写入条数。"""
    now = datetime.now(timezone.utc).isoformat()
    rows: list[tuple] = []
    for inst in raw_list:
        if not isinstance(inst, dict):
            continue
        inst_id = inst.get("instId")
        if not inst_id:
            continue
        rows.append((
            inst_id,
            inst.get("instType"),
            _to_float(inst.get("ctVal")),
            inst.get("ctValCcy") or None,
            inst.get("ctType") or None,
            inst.get("settleCcy") or None,
            now,
        ))
    if not rows:
        return 0
    conn = open_db()
    try:
        with conn:
            conn.executemany(
                """
                INSERT INTO instrument (inst_id, inst_type, ct_val, ct_val_ccy, ct_type, settle_ccy, updated_at)
                VALUES (?,?,?,?,?,?,?)
                ON CONFLICT(inst_id) DO UPDATE SET
                  inst_type=excluded.inst_type, ct_val=excluded.ct_val,
                  ct_val_ccy=excluded.ct_val_ccy, ct_type=excluded.ct_type,
                  settle_ccy=excluded.settle_ccy, updated_at=excluded.updated_at
                """,
                rows,
            )
    finally:
        conn.close()
    return len(rows)


# 没有 SWAP/FUTURES/OPTION 头寸时，instrument 表是否为空都不影响 notional 计算
_INSTRUMENT_REQUIRED_TYPES = {"SWAP", "FUTURES", "OPTION"}
_INSTRUMENT_STALE_DAYS = 30


def _instruments_hint() -> dict | None:
    """检查 instrument 表对当前库里的头寸是否够用。不够用时返回一个 hint dict 给 AI 助手。"""
    if not DB_PATH.exists():
        return None
    conn = open_db()
    try:
        count = conn.execute("SELECT COUNT(*) FROM instrument").fetchone()[0]
        latest = conn.execute("SELECT MAX(updated_at) FROM instrument").fetchone()[0]
        # 只统计走合约规格路径的头寸：SWAP / FUTURES / OPTION
        needs_inst = 0
        for r in conn.execute("SELECT raw_payload FROM position").fetchall():
            try:
                it = json.loads(r["raw_payload"]).get("instType")
            except Exception:
                continue
            if it in _INSTRUMENT_REQUIRED_TYPES:
                needs_inst += 1
    finally:
        conn.close()

    if needs_inst == 0:
        return None
    if count == 0:
        return {
            "status": "empty",
            "positions_needing_instruments": needs_inst,
            "message": (
                "instrument 表为空，dashboard 的「本金」列将显示 '-'。"
                "请让 OKX 官方 skill 拉 /api/v5/public/instruments（SWAP + FUTURES），"
                "再把 JSON 喂给 `okx_review ingest-instruments --file <path>` 或 `... --file -` (stdin)。"
            ),
        }
    if latest:
        try:
            dt = datetime.fromisoformat(latest)
            age_days = (datetime.now(timezone.utc) - dt).days
            if age_days > _INSTRUMENT_STALE_DAYS:
                return {
                    "status": "stale",
                    "age_days": age_days,
                    "message": f"instrument 表已 {age_days} 天未更新，建议重新 ingest-instruments",
                }
        except (ValueError, TypeError):
            pass
    return None


def _notional_usd(inst_type: str | None, inst_id: str, size: float | None,
                  entry: float | None, instruments: dict[str, dict]) -> float | None:
    """按 OKX 合约规格估算名义本金 (USD)。计算失败返回 None。"""
    if not size or not entry or size <= 0 or entry <= 0:
        return None
    if inst_type == "MARGIN" or inst_type == "SPOT":
        return size * entry  # MARGIN 里 size 直接是 base token 数量
    if inst_type in ("SWAP", "FUTURES", "OPTION"):
        inst = instruments.get(inst_id)
        if not inst:
            return None
        ct_val = inst.get("ct_val")
        if not ct_val or ct_val <= 0:
            return None
        if (inst.get("ct_type") or "").lower() == "inverse":
            return size * ct_val  # 币本位合约 ctVal 本身就是 USD
        return size * ct_val * entry  # 线性合约: contracts × base/contract × USD/base
    return None


def cmd_ingest(args: argparse.Namespace) -> int:
    src_path = args.positions
    if src_path == "-":
        try:
            raw_text = sys.stdin.read()
        except Exception as e:
            return emit_error("input_missing", f"stdin 读取失败: {e}")
    else:
        p = Path(src_path).expanduser()
        if not p.exists():
            return emit_error("file_not_found", f"找不到文件: {p}")
        raw_text = p.read_text(encoding="utf-8")

    try:
        data = json.loads(raw_text)
    except json.JSONDecodeError as e:
        return emit_error("parse_error", f"JSON 解析失败: {e}")

    try:
        raw_list = unwrap_payload(data)
    except ValueError as e:
        return emit_error("parse_error", str(e))

    if args.dry_run:
        normalized: list[NormalizedPosition] = []
        errors: list[str] = []
        for i, item in enumerate(raw_list):
            if not isinstance(item, dict):
                errors.append(f"[{i}] 不是对象")
                continue
            try:
                normalized.append(normalize_position(item))
            except ValueError as e:
                errors.append(f"[{i}] {e}")
        emit({
            "ok": True,
            "dry_run": True,
            "parsed": len(normalized),
            "skipped_malformed": len(errors),
            "errors_sample": errors[:5],
        })
        return 0

    result = _ingest_raw_list(raw_list, source=str(src_path))
    payload: dict[str, Any] = {"ok": True, **result, "db_path": str(DB_PATH)}
    hint = _instruments_hint()
    if hint is not None:
        payload["instruments_hint"] = hint
    emit(payload)
    return 0


def cmd_ingest_instruments(args: argparse.Namespace) -> int:
    src = args.file
    if src == "-":
        try:
            raw_text = sys.stdin.read()
        except Exception as e:
            return emit_error("input_missing", f"stdin 读取失败: {e}")
    else:
        p = Path(src).expanduser()
        if not p.exists():
            return emit_error("file_not_found", f"找不到文件: {p}")
        raw_text = p.read_text(encoding="utf-8")

    try:
        data = json.loads(raw_text)
    except json.JSONDecodeError as e:
        return emit_error("parse_error", f"JSON 解析失败: {e}")

    try:
        raw_list = unwrap_payload(data)
    except ValueError as e:
        return emit_error("parse_error", str(e))

    n = _upsert_instruments(raw_list)
    emit({"ok": True, "instruments_ingested": n, "db_path": str(DB_PATH)})
    return 0


def cmd_status(_: argparse.Namespace) -> int:
    if not DB_PATH.exists():
        emit({"ok": True, "db_path": str(DB_PATH), "exists": False, "positions_total": 0})
        return 0
    conn = open_db()
    try:
        total = conn.execute("SELECT COUNT(*) FROM position").fetchone()[0]
        rng = conn.execute("SELECT MIN(closed_at), MAX(closed_at) FROM position").fetchone()
        last = conn.execute("SELECT MAX(ingested_at) FROM sync_log").fetchone()[0]
    finally:
        conn.close()
    emit({
        "ok": True,
        "db_path": str(DB_PATH),
        "db_size_bytes": DB_PATH.stat().st_size,
        "positions_total": total,
        "last_ingest_at": last,
        "date_range": {"from": rng[0], "to": rng[1]},
    })
    return 0


def cmd_clean(args: argparse.Namespace) -> int:
    if not args.yes:
        return emit_error("input_missing", "需要 --yes 确认删除")
    conn = open_db()
    try:
        with conn:
            cur = conn.execute("DELETE FROM position WHERE closed_at < ?", (args.before,))
            deleted = cur.rowcount
        remaining = conn.execute("SELECT COUNT(*) FROM position").fetchone()[0]
    finally:
        conn.close()
    emit({"ok": True, "deleted": deleted, "remaining": remaining})
    return 0


def _ingest_file(path: str, *, kind: str) -> tuple[int, dict | None]:
    """读取 JSON 文件（或 stdin），按 kind 导入到 DB。
    返回 (rc, payload_for_emit)。rc 非 0 时 payload 含错误；rc=0 时 payload 是成功摘要，由 caller 合并进最终输出。
    """
    if path == "-":
        try:
            raw_text = sys.stdin.read()
        except Exception as e:
            return 1, {"ok": False, "error_kind": "input_missing", "message": f"stdin 读取失败: {e}"}
    else:
        p = Path(path).expanduser()
        if not p.exists():
            return 1, {"ok": False, "error_kind": "file_not_found", "message": f"找不到文件: {p}"}
        raw_text = p.read_text(encoding="utf-8")

    try:
        data = json.loads(raw_text)
    except json.JSONDecodeError as e:
        return 1, {"ok": False, "error_kind": "parse_error", "message": f"JSON 解析失败: {e}"}
    try:
        raw_list = unwrap_payload(data)
    except ValueError as e:
        return 1, {"ok": False, "error_kind": "parse_error", "message": str(e)}

    if kind == "positions":
        return 0, _ingest_raw_list(raw_list, source=str(path))
    if kind == "instruments":
        return 0, {"instruments_ingested": _upsert_instruments(raw_list)}
    return 1, {"ok": False, "error_kind": "internal_error", "message": f"unknown kind: {kind}"}


def cmd_serve(args: argparse.Namespace) -> int:
    try:
        from flask import Flask, render_template, request, abort, jsonify, Response
    except ImportError:
        return emit_error("missing_dependency", "需要 flask: pip install flask")

    _TAG = "s" + "cript"
    SCRIPT_TAG = f'<{_TAG} src="/assets/bundle.js"></{_TAG}>'

    # 把 chart.js + htmx.min.js + app.js 拼成单一 bundle 在内存里
    try:
        BUNDLE_JS = (
            (ASSETS_DIR / "chart.umd.min.js").read_text(encoding="utf-8")
            + "\n;\n"
            + (ASSETS_DIR / "htmx.min.js").read_text(encoding="utf-8")
            + "\n;\n"
            + (ASSETS_DIR / "app.js").read_text(encoding="utf-8")
        )
    except FileNotFoundError as e:
        return emit_error("file_not_found", f"assets 缺失: {e}")

    ingest_summary: dict[str, Any] = {}
    if args.positions:
        rc, payload = _ingest_file(args.positions, kind="positions")
        if rc != 0:
            emit(payload)
            return rc
        ingest_summary["positions"] = payload
    if args.instruments:
        rc, payload = _ingest_file(args.instruments, kind="instruments")
        if rc != 0:
            emit(payload)
            return rc
        ingest_summary["instruments"] = payload

    if not DB_PATH.exists():
        return emit_error(
            "db_error",
            f"DB 不存在: {DB_PATH}。请用 --positions <file> 指定 OKX 官方 skill 产出的 positions-history JSON。",
        )

    # 触发 schema 升级（兼容 v0.1 建的旧库） + seed 预设标签
    _db = open_db()
    try:
        _seed_preset_tags(_db)
    finally:
        _db.close()

    app = Flask(
        "okx_review",
        template_folder=str(TEMPLATES_DIR),
        static_folder=None,
    )

    @app.get("/assets/bundle.js")
    def _bundle():
        return Response(BUNDLE_JS, mimetype="application/javascript")

    def _conn() -> sqlite3.Connection:
        c = sqlite3.connect(DB_PATH)
        c.row_factory = sqlite3.Row
        c.execute("PRAGMA foreign_keys=ON")
        return c

    def _load_instruments(conn: sqlite3.Connection) -> dict[str, dict]:
        return {r["inst_id"]: dict(r) for r in conn.execute("SELECT * FROM instrument").fetchall()}

    def _inst_type_of(p: dict) -> str | None:
        try:
            return json.loads(p["raw_payload"]).get("instType")
        except Exception:
            return None

    def _load_positions(since: str | None, until: str | None) -> list[dict]:
        conn = _conn()
        try:
            where, params = [], []
            if since:
                where.append("closed_at >= ?")
                params.append(since)
            if until:
                where.append("closed_at <= ?")
                params.append(until + "T23:59:59+00:00")
            sql = "SELECT * FROM position"
            if where:
                sql += " WHERE " + " AND ".join(where)
            sql += " ORDER BY closed_at DESC"
            positions = [dict(r) for r in conn.execute(sql, params).fetchall()]

            tag_map: dict[int, list[dict]] = {}
            for row in conn.execute(
                "SELECT pt.position_id, t.id, t.name, t.color "
                "FROM position_tag pt JOIN tag t ON t.id = pt.tag_id"
            ).fetchall():
                tag_map.setdefault(row["position_id"], []).append(
                    {"id": row["id"], "name": row["name"], "color": row["color"]}
                )

            has_refl = {r["position_id"] for r in conn.execute("SELECT position_id FROM reflection").fetchall()}
            instruments = _load_instruments(conn)

            for p in positions:
                p["tags"] = tag_map.get(p["id"], [])
                p["has_reflection"] = p["id"] in has_refl
                p["inst_type"] = _inst_type_of(p)
                p["notional_usd"] = _notional_usd(
                    p["inst_type"], p["symbol"], p["size"], p["entry_price_avg"], instruments
                )
            return positions
        finally:
            conn.close()

    def _load_detail(pid: int) -> dict | None:
        conn = _conn()
        try:
            row = conn.execute("SELECT * FROM position WHERE id=?", (pid,)).fetchone()
            if not row:
                return None
            p = dict(row)
            p["inst_type"] = _inst_type_of(p)
            p["notional_usd"] = _notional_usd(
                p["inst_type"], p["symbol"], p["size"], p["entry_price_avg"],
                _load_instruments(conn),
            )
            p["tags"] = [
                dict(t) for t in conn.execute(
                    "SELECT t.id, t.name, t.color FROM position_tag pt "
                    "JOIN tag t ON t.id = pt.tag_id WHERE pt.position_id = ? ORDER BY t.name",
                    (pid,),
                ).fetchall()
            ]
            refl = conn.execute(
                "SELECT content, created_at, updated_at FROM reflection WHERE position_id=?", (pid,)
            ).fetchone()
            p["reflection"] = dict(refl) if refl else None
            p["all_tags"] = [
                dict(t) for t in conn.execute("SELECT id, name, color FROM tag ORDER BY name").fetchall()
            ]
            try:
                p["raw_pretty"] = json.dumps(json.loads(p["raw_payload"]), indent=2, ensure_ascii=False)
            except Exception:
                p["raw_pretty"] = p["raw_payload"]
            return p
        finally:
            conn.close()

    @app.get("/")
    def dashboard():
        since = request.args.get("since") or None
        until = request.args.get("until") or None
        positions = _load_positions(since, until)
        summary = compute_summary(positions)
        conn = _conn()
        try:
            all_tags = [
                dict(t) for t in conn.execute(
                    "SELECT id, name, color FROM tag ORDER BY id"
                ).fetchall()
            ]
        finally:
            conn.close()
        return render_template(
            "dashboard.html",
            positions=positions,
            summary=summary,
            leaderboard=compute_leaderboard(positions),
            all_tags=all_tags,
            filter_since=since or "",
            filter_until=until or "",
            generated_at=datetime.now(timezone.utc).isoformat(),
            script_tag=SCRIPT_TAG,
        )

    @app.get("/positions/<int:pid>")
    def position_detail(pid: int):
        p = _load_detail(pid)
        if not p:
            abort(404)
        return render_template("detail.html", p=p)

    @app.post("/positions/<int:pid>/tags")
    def add_tag(pid: int):
        name = (request.form.get("name") or "").strip()
        if not name:
            abort(400, "tag name required")
        color = (request.form.get("color") or "").strip() or None
        now = datetime.now(timezone.utc).isoformat()
        conn = _conn()
        try:
            with conn:
                conn.execute(
                    "INSERT OR IGNORE INTO tag (name, color, created_at) VALUES (?,?,?)",
                    (name, color, now),
                )
                if color:
                    conn.execute("UPDATE tag SET color=? WHERE name=? AND (color IS NULL OR color='')", (color, name))
                tag_id = conn.execute("SELECT id FROM tag WHERE name=?", (name,)).fetchone()["id"]
                conn.execute(
                    "INSERT OR IGNORE INTO position_tag (position_id, tag_id, created_at) VALUES (?,?,?)",
                    (pid, tag_id, now),
                )
        finally:
            conn.close()
        return position_detail(pid)

    @app.delete("/positions/<int:pid>/tags/<int:tag_id>")
    def remove_tag(pid: int, tag_id: int):
        conn = _conn()
        try:
            with conn:
                conn.execute(
                    "DELETE FROM position_tag WHERE position_id=? AND tag_id=?", (pid, tag_id)
                )
        finally:
            conn.close()
        return position_detail(pid)

    @app.post("/positions/<int:pid>/reflection")
    def save_reflection(pid: int):
        content = (request.form.get("content") or "").strip()
        now = datetime.now(timezone.utc).isoformat()
        conn = _conn()
        try:
            with conn:
                if content:
                    conn.execute(
                        """
                        INSERT INTO reflection (position_id, content, created_at, updated_at)
                        VALUES (?,?,?,?)
                        ON CONFLICT(position_id) DO UPDATE SET
                          content=excluded.content,
                          updated_at=excluded.updated_at
                        """,
                        (pid, content, now, now),
                    )
                else:
                    conn.execute("DELETE FROM reflection WHERE position_id=?", (pid,))
        finally:
            conn.close()
        return position_detail(pid)

    # ---- 全局标签管理：创建 / 改名 / 改色 / 删除 ----

    @app.post("/tags")
    def create_tag_global():
        name = (request.form.get("name") or "").strip()
        color = (request.form.get("color") or "").strip() or None
        if not name:
            abort(400, "tag name required")
        now = datetime.now(timezone.utc).isoformat()
        conn = _conn()
        try:
            with conn:
                conn.execute(
                    "INSERT OR IGNORE INTO tag (name, color, created_at) VALUES (?,?,?)",
                    (name, color, now),
                )
                row = conn.execute("SELECT id, name, color FROM tag WHERE name=?", (name,)).fetchone()
        finally:
            conn.close()
        return jsonify({"ok": True, "tag": dict(row)})

    @app.patch("/tags/<int:tag_id>")
    def update_tag_global(tag_id: int):
        name = request.form.get("name")
        color = request.form.get("color")
        if name is not None:
            name = name.strip()
            if not name:
                abort(400, "name cannot be empty")
        if color is not None:
            color = color.strip() or None
        conn = _conn()
        try:
            with conn:
                row = conn.execute("SELECT id, name, color FROM tag WHERE id=?", (tag_id,)).fetchone()
                if not row:
                    abort(404, "tag not found")
                new_name = name if name is not None else row["name"]
                new_color = color if color is not None else row["color"]
                if name is not None and name != row["name"]:
                    dup = conn.execute(
                        "SELECT id FROM tag WHERE name=? AND id<>?", (new_name, tag_id)
                    ).fetchone()
                    if dup:
                        abort(409, f"tag '{new_name}' already exists")
                conn.execute(
                    "UPDATE tag SET name=?, color=? WHERE id=?",
                    (new_name, new_color, tag_id),
                )
        finally:
            conn.close()
        return jsonify({"ok": True, "tag": {"id": tag_id, "name": new_name, "color": new_color},
                        "old_name": row["name"]})

    @app.delete("/tags/<int:tag_id>")
    def delete_tag_global(tag_id: int):
        conn = _conn()
        try:
            with conn:
                row = conn.execute("SELECT name FROM tag WHERE id=?", (tag_id,)).fetchone()
                if not row:
                    abort(404, "tag not found")
                # position_tag 的 ON DELETE CASCADE 会自动清理关联
                conn.execute("DELETE FROM tag WHERE id=?", (tag_id,))
        finally:
            conn.close()
        return jsonify({"ok": True, "deleted": {"id": tag_id, "name": row["name"]}})

    @app.get("/health")
    def health():
        return jsonify({"ok": True, "db_path": str(DB_PATH)})

    url = f"http://{args.host}:{args.port}"
    log(f"启动 Flask 服务: {url}  (Ctrl-C 停止)")
    payload: dict[str, Any] = {
        "ok": True,
        "url": url,
        "host": args.host,
        "port": args.port,
        "db_path": str(DB_PATH),
    }
    if ingest_summary:
        payload["ingest"] = ingest_summary
        hint = _instruments_hint()
        if hint is not None:
            payload["instruments_hint"] = hint
    emit(payload)
    sys.stdout.flush()

    if not args.no_browser:
        import threading
        threading.Timer(0.8, lambda: webbrowser.open(url)).start()

    try:
        app.run(host=args.host, port=args.port, debug=False, use_reloader=False)
    except KeyboardInterrupt:
        log("已退出")
    return 0


def compute_summary(rows: list[dict]) -> dict:
    if not rows:
        return {
            "count": 0, "total_pnl": 0, "win_rate": 0, "total_fee": 0,
            "profit_factor": 0, "trades_per_week": 0,
            "tagged_count": 0, "best": 0, "worst": 0,
        }
    wins = sum(1 for r in rows if (r["realized_pnl"] or 0) > 0)
    gross_win = sum(r["realized_pnl"] for r in rows if (r["realized_pnl"] or 0) > 0)
    gross_loss = sum(r["realized_pnl"] for r in rows if (r["realized_pnl"] or 0) < 0)
    if gross_loss == 0:
        profit_factor = None if gross_win > 0 else 0  # None = 无亏损（无穷大）
    else:
        profit_factor = round(gross_win / abs(gross_loss), 2)

    try:
        dates = [datetime.fromisoformat(r["closed_at"]) for r in rows if r.get("closed_at")]
        span_days = max(1, (max(dates) - min(dates)).days) if len(dates) >= 2 else 1
        trades_per_week = round(len(rows) * 7 / span_days, 1)
    except Exception:
        trades_per_week = 0

    tagged_count = sum(1 for r in rows if r.get("tags"))

    return {
        "count": len(rows),
        "total_pnl": round(sum(r["realized_pnl"] or 0 for r in rows), 6),
        "win_rate": round(wins / len(rows), 4),
        "total_fee": round(sum(r["total_fee"] or 0 for r in rows), 6),
        "profit_factor": profit_factor,
        "trades_per_week": trades_per_week,
        "tagged_count": tagged_count,
        "best": max((r["realized_pnl"] or 0 for r in rows), default=0),
        "worst": min((r["realized_pnl"] or 0 for r in rows), default=0),
    }


def compute_leaderboard(rows: list[dict], top_n: int = 3) -> dict:
    winners = sorted(
        (p for p in rows if (p["realized_pnl"] or 0) > 0),
        key=lambda p: p["realized_pnl"], reverse=True,
    )[:top_n]
    losers = sorted(
        (p for p in rows if (p["realized_pnl"] or 0) < 0),
        key=lambda p: p["realized_pnl"],
    )[:top_n]
    return {"winners": winners, "losers": losers}


# ---------- CLI ----------

def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="okx_review", description="OKX 本地复盘")
    sub = p.add_subparsers(dest="command", required=True)

    ing = sub.add_parser(
        "ingest",
        help="从 OKX 官方 skill 产出的 positions-history JSON 导入",
    )
    ing.add_argument("--positions", required=True, help="JSON 路径，或 - 表示 stdin")
    ing.add_argument("--dry-run", action="store_true")
    ing.set_defaults(func=cmd_ingest)

    st = sub.add_parser("status", help="打印 DB 状态")
    st.set_defaults(func=cmd_status)

    cl = sub.add_parser("clean", help="清旧数据")
    cl.add_argument("--before", required=True)
    cl.add_argument("--yes", action="store_true")
    cl.set_defaults(func=cmd_clean)

    ii = sub.add_parser(
        "ingest-instruments",
        help="从 OKX 官方 skill 产出的 public-instruments JSON 导入合约规格（算名义本金必备）",
    )
    ii.add_argument("--file", required=True, help="JSON 路径，或 - 表示 stdin")
    ii.set_defaults(func=cmd_ingest_instruments)

    sv = sub.add_parser(
        "serve",
        help="主入口：可先 ingest 再起 Flask 服务；打标签 / 写复盘笔记",
    )
    sv.add_argument(
        "--positions",
        help="（可选）OKX 官方 skill 产出的 positions-history JSON 路径，或 - 表示 stdin；给了就先 ingest 再启动",
    )
    sv.add_argument(
        "--instruments",
        help="（可选）OKX 官方 skill 产出的 public-instruments JSON 路径，或 - 表示 stdin；给了就先 ingest-instruments 再启动",
    )
    sv.add_argument("--host", default="127.0.0.1")
    sv.add_argument("--port", type=int, default=4738)
    sv.add_argument("--no-browser", action="store_true", help="不自动打开浏览器")
    sv.set_defaults(func=cmd_serve)

    return p


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return args.func(args)
    except Exception as e:
        return emit_error("internal_error", f"{type(e).__name__}: {e}")


if __name__ == "__main__":
    sys.exit(main())
