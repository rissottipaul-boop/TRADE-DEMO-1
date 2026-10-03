"""Общие помощники пакета аналитики: чтение баз только на чтение, числа, JSON, таблицы."""
from __future__ import annotations

import json
import math
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable, Optional, Sequence

ROOT = Path(__file__).resolve().parents[2]
PROJECT_TZ = timezone(timedelta(hours=5))
RUNS_DIR = Path("data/analytics/runs")
RISK_DB = Path("data/risk_state.db")
BOT_DB = Path("data/bot_state.db")


def connect_ro(path: Path | str) -> sqlite3.Connection:
    """SQLite только на чтение: нет файла — FileNotFoundError (базу не создаём)."""
    path = Path(path)
    if not path.is_file():
        raise FileNotFoundError(str(path))
    conn = sqlite3.connect(f"file:{path.resolve().as_posix()}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def num(value: Any) -> Optional[float]:
    """Конечное float или None (строки OKX, bool и NaN — None)."""
    if isinstance(value, bool) or value is None:
        return None
    try:
        x = float(value)
    except (TypeError, ValueError):
        return None
    return x if math.isfinite(x) else None


def clean(obj: Any) -> Any:
    """Объект для JSON: inf/NaN -> строка, кортежи -> списки, ключи -> str."""
    if isinstance(obj, float):
        if math.isnan(obj):
            return "nan"
        if math.isinf(obj):
            return "inf" if obj > 0 else "-inf"
        return obj
    if isinstance(obj, dict):
        return {str(k): clean(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [clean(v) for v in obj]
    return obj


def unclean(value: Any) -> Any:
    """Обратное к clean для чисел: "inf" -> math.inf."""
    if value == "inf":
        return math.inf
    if value == "-inf":
        return -math.inf
    if value == "nan":
        return math.nan
    return value


def dump_json(obj: Any) -> str:
    return json.dumps(clean(obj), ensure_ascii=False, indent=2)


def read_json(path: Path | str) -> Any:
    return json.loads(Path(path).read_text(encoding="utf-8-sig"))


def write_text(path: Path | str, text: str) -> Path:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    tmp.replace(path)
    return path


def fmt(value: Any, nd: int = 2) -> str:
    value = unclean(value)
    if value is None:
        return "—"
    if isinstance(value, bool):
        return "да" if value else "нет"
    if isinstance(value, (int, float)):
        if isinstance(value, float) and math.isinf(value):
            return "∞" if value > 0 else "−∞"
        if isinstance(value, float) and math.isnan(value):
            return "—"
        if isinstance(value, int):
            return str(value)
        return f"{value:,.{nd}f}".replace(",", " ")
    return str(value)


def iso_ms(ts_ms: Optional[int]) -> str:
    if ts_ms is None:
        return "—"
    return datetime.fromtimestamp(ts_ms / 1000, tz=timezone.utc).strftime("%Y-%m-%d %H:%M UTC")


def iso_s(ts: Optional[float]) -> str:
    if ts is None:
        return "—"
    return datetime.fromtimestamp(ts, tz=PROJECT_TZ).isoformat(timespec="seconds")


def md_table(header: Sequence[str], rows: Iterable[Sequence[Any]]) -> str:
    def cell(v: Any) -> str:
        return str(v).replace("|", "\\|").replace("\n", " ")
    lines = ["| " + " | ".join(cell(h) for h in header) + " |",
             "| " + " | ".join("---" for _ in header) + " |"]
    lines += ["| " + " | ".join(cell(v) for v in row) + " |" for row in rows]
    return "\n".join(lines)
