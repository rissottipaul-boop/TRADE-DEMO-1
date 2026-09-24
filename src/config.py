"""Конфигурация проекта. Контракт зафиксирован в insights/roadmap.md (Фаза 0).

Правила:
- demo по умолчанию; live только при явном OKX_MODE=live или load_settings("live")
- position mode и account mode фиксируются до старта бота и не меняются
  при открытых позициях (см. insights/okx-api.md, п.12 подводных камней)
- состояние demo и live раздельное (state_paths): demo — прежние пути в data/,
  live — data/live/ (задача LIVE-STATE-SPLIT, insights/business-plan.md §3)
"""
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from dotenv import load_dotenv

load_dotenv()

MODES = ("demo", "live")
# Корень состояния. Относительный путь — как у risk._DB_PATH и storage.DB_PATH:
# процессы проекта запускаются из корня (ops/*.ps1 ставят WorkingDirectory).
DATA_ROOT = Path("data")


def normalize_mode(mode: str) -> str:
    value = (mode or "").strip().lower()
    if value not in MODES:
        raise ValueError(f"OKX_MODE должен быть demo|live, получено: {mode!r}")
    return value


def default_mode() -> str:
    """Режим из окружения (OKX_MODE), по умолчанию demo."""
    return normalize_mode(os.getenv("OKX_MODE", "demo"))


@dataclass(frozen=True)
class StatePaths:
    """Файлы состояния одного режима."""
    root: Path
    risk_db: Path   # риск-ядро (risk.init)
    bot_db: Path    # ордера, сделки, equity, ws_state (Storage)
    kill_flag: Path  # файл-флаг аварийной остановки процесса этого режима


def data_dir(mode: str) -> Path:
    """Каталог состояния режима: demo — data/ (эти пути использует работающий
    движок P1-72H, их не меняем), live — data/live/.

    Общее состояние недопустимо: live-процесс с demo-базой риска увидел бы
    demo-HWM ~104k USDT и принял бы equity кармана в $1k за просадку −99%.
    """
    return DATA_ROOT if normalize_mode(mode) == "demo" else DATA_ROOT / "live"


def state_paths(mode: str) -> StatePaths:
    root = data_dir(mode)
    return StatePaths(root=root, risk_db=root / "risk_state.db",
                      bot_db=root / "bot_state.db", kill_flag=root / "KILL")


@dataclass(frozen=True)
class Settings:
    mode: str  # "demo" | "live"
    # repr=False: секреты не попадают в логи и трейсбеки при печати Settings
    api_key: str = field(repr=False)
    secret: str = field(repr=False)
    passphrase: str = field(repr=False)
    domain: str = "www.okx.com"

    @property
    def is_demo(self) -> bool:
        return self.mode == "demo"


def load_settings(mode: Optional[str] = None) -> Settings:
    """Настройки режима. mode=None — из OKX_MODE (по умолчанию demo).

    Live-процессы (src.live_*) передают mode="live" явно: OKX_MODE в .env
    остаётся demo, иначе в live ушли бы и demo-процессы (движок, тесты).
    """
    mode = default_mode() if mode is None else normalize_mode(mode)

    prefix = "OKX_DEMO_" if mode == "demo" else "OKX_"
    api_key = os.getenv(prefix + "API_KEY", "")
    secret = os.getenv(prefix + "SECRET", "")
    passphrase = os.getenv(prefix + "PASSPHRASE", "")
    if not all((api_key, secret, passphrase)):
        raise RuntimeError(
            f"Не заданы {prefix}API_KEY/SECRET/PASSPHRASE в .env "
            f"(шаблон — .env.example)"
        )

    return Settings(
        mode=mode,
        api_key=api_key,
        secret=secret,
        passphrase=passphrase,
        domain=os.getenv("OKX_DOMAIN", "www.okx.com").strip() or "www.okx.com",
    )
