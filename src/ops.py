"""Операторские команды: статус, kill-switch, сброс блокировок.

Работает и без запущенного движка — состояние риска общее для всех процессов
одного режима. Режим выбирает и ключи, и файлы состояния (config.state_paths):
demo — data/risk_state.db и data/bot_state.db, live — data/live/.

    python -m src.ops status                     # риск-снапшот + метрики движка + открытые ордера
    python -m src.ops status --mode live         # то же для live-кармана
    python -m src.ops kill "причина"             # отменить ВСЕ ордера (включая algo), остановить
                                                 # grid-ботов OKX, заблокировать входы
    python -m src.ops kill --keep-bots           # то же, но нативные grid-боты не трогать
    python -m src.ops kill --mode live "причина" # аварийная остановка live-кармана
    python -m src.ops reset kill|global|daily    # ручной сброс (только человек)

--mode — только ПОСЛЕ команды (по умолчанию OKX_MODE из окружения, иначе demo).
Опций перед командой нет намеренно: правило guard для `src.ops reset` (сброс —
только человек) ищет команду сразу за именем модуля.
"""
import argparse
import json
import logging
import sys

from . import risk
from .config import MODES, StatePaths, default_mode, load_settings, state_paths
from .connector import create_exchange, emergency_stop
from .storage import Storage

log = logging.getLogger("okx.ops")


def _exchange(mode: str):
    return create_exchange(load_settings(mode))


def _init_risk(mode: str) -> StatePaths:
    """Риск-ядро на файле состояния режима (demo и live не смешиваются)."""
    paths = state_paths(mode)
    risk.init(paths.risk_db)
    return paths


def cmd_status(args: argparse.Namespace) -> int:
    paths = _init_risk(args.mode)
    storage = Storage(paths.bot_db)
    print(json.dumps({
        "mode": args.mode,
        "risk": risk.status(),
        "engine": storage.get_ws_state("engine_stats"),
        "open_orders_local": [dict(r) for r in storage.get_open_orders()],
    }, ensure_ascii=False, indent=2, default=str))
    return 0


def cmd_kill(args: argparse.Namespace) -> int:
    _init_risk(args.mode)
    try:
        ex = _exchange(args.mode)
    except Exception as exc:
        # Входы блокируются и без связи с биржей: kill_switch ставит kill_active
        # до вызова отмены, а отсутствие канцеллера попадает в failed отчёта
        log.error("биржа %s недоступна (%s) — входы блокирую, ордера не отменены", args.mode, exc)
    else:
        risk.set_order_canceller(lambda flatten: emergency_stop(ex, include_bots=not args.keep_bots))
    report = risk.kill_switch(flatten=False, by=f"cli[{args.mode}]: {args.reason}")
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 1 if report["failed"] else 0


def cmd_reset(args: argparse.Namespace) -> int:
    _init_risk(args.mode)
    risk.reset_breaker(args.scope, by="cli")
    print(f"Сброшено [{args.mode}]: {args.scope}")
    return 0


def main(argv=None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    parser = argparse.ArgumentParser(prog="python -m src.ops", description=__doc__.splitlines()[0])
    # --mode только у подкоманд (см. docstring: правило guard для reset)
    mode_opt = argparse.ArgumentParser(add_help=False)
    mode_opt.add_argument("--mode", choices=MODES, default=None,
                          help="demo|live — ключи и файлы состояния (по умолчанию OKX_MODE, иначе demo)")

    sub = parser.add_subparsers(dest="cmd", required=True)
    sub.add_parser("status", parents=[mode_opt], help="состояние риска и движка").set_defaults(func=cmd_status)
    kill = sub.add_parser("kill", parents=[mode_opt], help="аварийная остановка")
    kill.add_argument("reason", nargs="?", default="manual")
    kill.add_argument("--keep-bots", action="store_true", help="не останавливать нативные grid-боты OKX")
    kill.set_defaults(func=cmd_kill)
    reset = sub.add_parser("reset", parents=[mode_opt], help="ручной сброс kill-switch / breaker")
    reset.add_argument("scope", choices=("kill", "global", "daily"))
    reset.set_defaults(func=cmd_reset)
    args = parser.parse_args(argv)
    if args.mode is None:
        args.mode = default_mode()
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
