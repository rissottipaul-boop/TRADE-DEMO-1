"""Интеграция с CockroachDB Cloud (PostgreSQL-совместимая распределённая БД).

Используется для облачного хранения событий Agent Control Plane, телеметрии эквити
и состояния запусков без нарушения локального режима (zero-breakage fallback).
Если CockroachDB не настроен или недоступен, вся система продолжает работать
штатно на локальных SQLite и JSON-файлах.
"""

from __future__ import annotations

import json
import logging
import os
import time
from typing import Any, Optional

try:
    import certifi
except ImportError:
    certifi = None

try:
    import psycopg2
    from psycopg2 import sql
    from psycopg2.extras import RealDictCursor
    PSYCOPG2_AVAILABLE = True
except ImportError:
    psycopg2 = None
    sql = None
    RealDictCursor = None
    PSYCOPG2_AVAILABLE = False

log = logging.getLogger("okx.cloud_db")

SCHEMA_DDL = """
-- Agent Control Plane: реестр запусков
CREATE TABLE IF NOT EXISTS cp_runs (
    id VARCHAR(120) PRIMARY KEY,
    task_id VARCHAR(80),
    role VARCHAR(80),
    runtime VARCHAR(80),
    model VARCHAR(80),
    status VARCHAR(50),
    pid INT,
    started_at TIMESTAMPTZ,
    finished_at TIMESTAMPTZ,
    exit_code INT,
    cost JSONB,
    provenance JSONB,
    updated_at TIMESTAMPTZ DEFAULT clock_timestamp()
);

-- Agent Control Plane: append-only события запусков
CREATE TABLE IF NOT EXISTS cp_run_events (
    id BIGSERIAL PRIMARY KEY,
    run_id VARCHAR(120) REFERENCES cp_runs(id) ON DELETE CASCADE,
    cursor INT NOT NULL,
    event_type VARCHAR(80) NOT NULL,
    payload JSONB,
    created_at TIMESTAMPTZ DEFAULT clock_timestamp(),
    CONSTRAINT uq_run_cursor UNIQUE (run_id, cursor)
);
CREATE INDEX IF NOT EXISTS idx_cp_run_events_run_id ON cp_run_events(run_id);

-- Agent Control Plane: резервирование рабочих копий
CREATE TABLE IF NOT EXISTS cp_worktree_leases (
    task_id VARCHAR(80) PRIMARY KEY,
    run_id VARCHAR(120) NOT NULL,
    worktree TEXT NOT NULL,
    branch VARCHAR(120) NOT NULL,
    base_commit VARCHAR(40) NOT NULL,
    state VARCHAR(50) NOT NULL,
    heartbeat_at TIMESTAMPTZ NOT NULL,
    created_at TIMESTAMPTZ DEFAULT clock_timestamp()
);

-- Телеметрия торговли: кривая эквити и риск
CREATE TABLE IF NOT EXISTS telemetry_equity (
    id BIGSERIAL PRIMARY KEY,
    ts TIMESTAMPTZ NOT NULL,
    equity NUMERIC(16, 4) NOT NULL,
    hwm NUMERIC(16, 4),
    drawdown_pct NUMERIC(8, 4),
    day_pnl NUMERIC(16, 4),
    raw_json JSONB,
    created_at TIMESTAMPTZ DEFAULT clock_timestamp()
);
CREATE INDEX IF NOT EXISTS idx_telemetry_equity_ts ON telemetry_equity(ts);

-- Телеметрия торговли: история ордеров
CREATE TABLE IF NOT EXISTS telemetry_orders (
    ord_id VARCHAR(64) PRIMARY KEY,
    inst_id VARCHAR(32) NOT NULL,
    side VARCHAR(16) NOT NULL,
    ord_type VARCHAR(32) NOT NULL,
    px NUMERIC(16, 8),
    sz NUMERIC(24, 8) NOT NULL,
    state VARCHAR(32) NOT NULL,
    cl_ord_id VARCHAR(64),
    create_time TIMESTAMPTZ,
    update_time TIMESTAMPTZ NOT NULL,
    raw_json JSONB
);
CREATE INDEX IF NOT EXISTS idx_telemetry_orders_inst ON telemetry_orders(inst_id);
"""


import urllib.parse

def _normalize_connection_url(url: str) -> str:
    """Добавляет root CA сертификат certifi, если в Windows задан sslmode=verify-full."""
    if "sslmode=verify-full" in url and "sslrootcert=" not in url and certifi:
        ca_path = urllib.parse.quote(certifi.where())
        sep = "&" if "?" in url else "?"
        url = f"{url}{sep}sslrootcert={ca_path}"
    return url


def get_connection_string() -> Optional[str]:
    """Возвращает строку подключения из окружения или локального файла config/cloud_db.json."""
    url = os.getenv("COCKROACH_DATABASE_URL") or os.getenv("DATABASE_URL")
    if not url:
        # Проверяем локальный конфиг-файл, если .env недоступен
        conf_file = os.path.join("data", "cloud_db.json")
        if os.path.exists(conf_file):
            try:
                with open(conf_file, "r", encoding="utf-8") as f:
                    cfg = json.load(f)
                    url = cfg.get("url")
            except Exception:
                pass

    if url:
        return _normalize_connection_url(url)

    host = os.getenv("COCKROACH_HOST")
    if not host:
        return None

    user = os.getenv("COCKROACH_USER", "paul")
    password = os.getenv("COCKROACH_PASSWORD", "")
    port = os.getenv("COCKROACH_PORT", "26257")
    database = os.getenv("COCKROACH_DATABASE", "defaultdb")
    sslmode = os.getenv("COCKROACH_SSLMODE", "require")

    auth = f"{user}:{password}@" if password else f"{user}@"
    base = f"postgresql://{auth}{host}:{port}/{database}?sslmode={sslmode}"
    return _normalize_connection_url(base)


class CloudDB:
    """Управление облачным подключением CockroachDB с безопасным fallback."""

    def __init__(self, connection_string: Optional[str] = None, timeout: int = 5):
        raw = connection_string or get_connection_string()
        self.conn_str = _normalize_connection_url(raw) if raw else None
        self.timeout = timeout

    @property
    def is_configured(self) -> bool:
        return bool(self.conn_str and PSYCOPG2_AVAILABLE)

    def get_connection(self):
        if not self.is_configured:
            raise RuntimeError("CockroachDB не настроен или psycopg2 не установлен")
        return psycopg2.connect(self.conn_str, connect_timeout=self.timeout)

    def init_schema(self) -> dict:
        """Создание таблиц схемы CockroachDB."""
        if not self.is_configured:
            return {"ok": False, "error": "CockroachDB не настроен"}
        try:
            with self.get_connection() as conn:
                with conn.cursor() as cur:
                    cur.execute(SCHEMA_DDL)
                conn.commit()
            log.info("Схема CockroachDB успешно инициализирована")
            return {"ok": True, "message": "Схема инициализирована"}
        except Exception as exc:
            log.warning("Ошибка инициализации схемы CockroachDB: %s", exc)
            return {"ok": False, "error": str(exc)}

    def check_health(self) -> dict:
        """Проверка соединения с кластером."""
        if not PSYCOPG2_AVAILABLE:
            return {"ok": False, "configured": False, "error": "psycopg2 не установлен"}
        if not self.conn_str:
            return {"ok": False, "configured": False, "error": "COCKROACH_DATABASE_URL не задан"}

        start = time.perf_counter()
        try:
            with self.get_connection() as conn:
                with conn.cursor() as cur:
                    cur.execute("SELECT version(), current_database(), current_user;")
                    row = cur.fetchone()
            latency = (time.perf_counter() - start) * 1000
            return {
                "ok": True,
                "configured": True,
                "latency_ms": round(latency, 2),
                "version": row[0] if row else "unknown",
                "database": row[1] if row else "unknown",
                "user": row[2] if row else "unknown",
            }
        except Exception as exc:
            return {
                "ok": False,
                "configured": True,
                "latency_ms": round((time.perf_counter() - start) * 1000, 2),
                "error": str(exc),
            }

    def sync_run(self, run: dict) -> bool:
        """Идемпотентная запись/обновление agent run в CockroachDB."""
        if not self.is_configured:
            return False
        try:
            with self.get_connection() as conn:
                with conn.cursor() as cur:
                    cur.execute(
                        """
                        INSERT INTO cp_runs (
                            id, task_id, role, runtime, model, status, pid,
                            started_at, finished_at, exit_code, cost, provenance
                        ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                        ON CONFLICT (id) DO UPDATE SET
                            status = EXCLUDED.status,
                            finished_at = EXCLUDED.finished_at,
                            exit_code = EXCLUDED.exit_code,
                            cost = EXCLUDED.cost,
                            updated_at = clock_timestamp();
                        """,
                        (
                            run.get("id"),
                            run.get("task_id"),
                            run.get("role"),
                            run.get("runtime"),
                            run.get("model"),
                            run.get("status"),
                            run.get("pid"),
                            run.get("started_at"),
                            run.get("finished_at"),
                            run.get("exit_code"),
                            json.dumps(run.get("cost")) if run.get("cost") else None,
                            json.dumps(run.get("provenance")) if run.get("provenance") else None,
                        ),
                    )
                conn.commit()
            return True
        except Exception as exc:
            log.warning("Не удалось синхронизировать run %s в CockroachDB: %s", run.get("id"), exc)
            return False

    def sync_equity_snapshot(self, equity_data: dict) -> bool:
        """Зеркалирование снимка эквити в CockroachDB."""
        if not self.is_configured:
            return False
        try:
            with self.get_connection() as conn:
                with conn.cursor() as cur:
                    cur.execute(
                        """
                        INSERT INTO telemetry_equity (
                            ts, equity, hwm, drawdown_pct, day_pnl, raw_json
                        ) VALUES (clock_timestamp(), %s, %s, %s, %s, %s);
                        """,
                        (
                            equity_data.get("equity", 0.0),
                            equity_data.get("hwm", 0.0),
                            equity_data.get("drawdown_pct", 0.0),
                            equity_data.get("day_pnl", 0.0),
                            json.dumps(equity_data),
                        ),
                    )
                conn.commit()
            return True
        except Exception as exc:
            log.warning("Не удалось записать снимок эквити в CockroachDB: %s", exc)
            return False


# Глобальный экземпляр для переиспользования
_global_cloud_db: Optional[CloudDB] = None


def get_cloud_db() -> CloudDB:
    global _global_cloud_db
    if _global_cloud_db is None:
        _global_cloud_db = CloudDB()
    return _global_cloud_db


def cloud_db_status() -> dict:
    """Безопасный опрос статуса облачной БД для Control Panel API."""
    try:
        db = get_cloud_db()
        if not db.is_configured:
            return {
                "enabled": False,
                "configured": False,
                "status": "not_configured",
                "message": "CockroachDB не настроен (локальный режим)",
            }
        health = db.check_health()
        return {
            "enabled": True,
            "configured": health.get("configured", True),
            "status": "connected" if health.get("ok") else "error",
            "latency_ms": health.get("latency_ms"),
            "database": health.get("database"),
            "error": health.get("error"),
        }
    except Exception as exc:
        return {
            "enabled": False,
            "configured": False,
            "status": "error",
            "error": str(exc),
        }


if __name__ == "__main__":
    import sys
    cmd = sys.argv[1] if len(sys.argv) > 1 else "status"
    db = get_cloud_db()
    if cmd == "status":
        st = cloud_db_status()
        print(json.dumps(st, ensure_ascii=False, indent=2))
    elif cmd == "init":
        res = db.init_schema()
        print(json.dumps(res, ensure_ascii=False, indent=2))
    else:
        print(f"Неизвестная команда: {cmd}. Доступны: status, init")
