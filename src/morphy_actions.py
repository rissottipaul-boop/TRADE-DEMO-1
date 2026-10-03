"""Управляющий контур экрана проекта Morphy (задача MORPHY-UI-CONTROLS).

    python -m src.morphy_actions describe        # controls по текущему локальному состоянию (JSON)
    python -m src.morphy_actions run <action>    # выполнить действие: {"ok", "action", "summary", ...}

Действия запускает человек кнопками экрана проекта Morphy: бэкенд
(ops/morphy/project-backend.ts) проверяет сессию, Origin, удержание кнопки по
часам сервера и одноразовый arm_id, затем вызывает `run` через execFile без
shell. Агенты эти действия не запускают (AGENTS.md §2, контракт пакета
MORPHY-UI-*, правило 3).

Действия (только demo; live отклоняется, даже если передан):
  kill.engage           — как `python -m src.ops kill`: risk.kill_switch с отменой всех
                          ордеров (включая algo) и остановкой grid/DCA-ботов
                          (connector.emergency_stop), входы блокируются.
  orders.cancel_all     — только connector.emergency_stop(ex, include_bots=False);
                          kill-флаг не ставится, защитные стопы тоже отменяются.
  engine.pause/resume   — ops/engine.ps1 stop/start через src.control_panel._script.
  kill.reset, breaker.reset.daily, breaker.reset.global
                        — сброс блокировки риск-ядра с by="morphy-ui". Решение человека:
                          нужна фраза подтверждения, блокировка должна быть активна.

Фраза для сброса приходит только из переменной окружения CONFIRM_ENV, которую
ставит бэкенд Morphy. Имя содержит «reset_breaker», поэтому guard проекта
отклоняет любую команду агента, где оно встречается.

Предусловия перепроверяются в момент выполнения, а не берутся из describe.
Каждый вызов run пишет строку в logs/control-panel-actions.jsonl:
{ts, action, outcome, via: "morphy"}; outcome — ok | failed | rejected. Токен,
фраза, вывод команд и тексты исключений в журнал и summary не попадают.

Модуль импортируется быстро: на уровне модуля только stdlib. Биржа, конфиг
(.env), риск-ядро и control_panel подключаются лениво внутри run и CLI.
"""
from __future__ import annotations

import argparse
from dataclasses import dataclass
from datetime import datetime, timezone
import json
import logging
import os
from pathlib import Path
import shutil
import subprocess
import sys
from typing import Any, Callable, Optional

ROOT = Path(__file__).resolve().parents[1]
log = logging.getLogger("okx.morphy_actions")

# Переменная окружения с фразой подтверждения сброса. Имя содержит reset_breaker
# намеренно: правило guard `reset_breaker` отклоняет команды агентов с ним.
CONFIRM_ENV = "MORPHY_UI_RESET_BREAKER_CONFIRM"
RESET_BY = "morphy-ui"
# ops/engine.ps1 stop ждёт до 30 с, start — мьютекс до 120 с. Таймаут Python
# короче таймаута бэкенда (120 с), чтобы отказ пришёл ответом, а не обрывом.
ENGINE_TIMEOUT_S = 100
AUDIT_RELATIVE = Path("logs") / "control-panel-actions.jsonl"
OUTCOMES = ("ok", "failed", "rejected")
ONLY_DEMO = "Управление доступно только в demo"

_KILL_WARNING = ("Отменит все ордера, включая защитные стопы и algo, остановит grid- и DCA-ботов "
                 "и заблокирует новые входы. Позиции не закрываются. Снять kill-switch — только "
                 "человеку, текстовым подтверждением.")
_CANCEL_WARNING = ("Отменит ВСЕ ордера, включая защитные стопы: открытые позиции останутся без стопов. "
                   "Kill-switch не включается, боты не останавливаются.")
_PAUSE_WARNING = ("Остановка движка сбросит 72-часовой прогон P1-72H. Ордера и позиции на бирже "
                  "остаются; аварийная остановка торговли — kill-switch.")
_RESUME_WARNING = "Движок снова начнёт торговать по стратегиям в пределах риск-лимитов (demo)."
_HUMAN_ONLY = " Решение человека (AGENTS.md §2)."

# Спецификация действий — зеркало контракта (§2) и таблицы ACTIONS в project-backend.ts.
SPECS: dict[str, dict[str, Any]] = {
    "kill.engage": {
        "label": "Остановить торговлю (kill-switch)", "group": "safety", "level": "emergency",
        "hold_ms": 2000, "confirm_phrase": None, "warning": _KILL_WARNING},
    "orders.cancel_all": {
        "label": "Отменить все ордера", "group": "safety", "level": "caution",
        "hold_ms": 2000, "confirm_phrase": None, "warning": _CANCEL_WARNING},
    "engine.pause": {
        "label": "Остановить движок", "group": "engine", "level": "caution",
        "hold_ms": 2000, "confirm_phrase": None, "warning": _PAUSE_WARNING},
    "engine.resume": {
        "label": "Запустить движок", "group": "engine", "level": "caution",
        "hold_ms": 2000, "confirm_phrase": None, "warning": _RESUME_WARNING},
    "breaker.reset.daily": {
        "label": "Снять дневной breaker", "group": "reset", "level": "critical-reset",
        "hold_ms": 2500, "confirm_phrase": "СНЯТЬ DAILY",
        "warning": ("Возобновит входы до автосброса в 00:00 UTC, хотя дневной лимит убытка уже "
                    "исчерпан." + _HUMAN_ONLY)},
    "breaker.reset.global": {
        "label": "Снять глобальный breaker", "group": "reset", "level": "critical-reset",
        "hold_ms": 2500, "confirm_phrase": "СНЯТЬ GLOBAL",
        "warning": ("Возобновит входы после просадки от high-water mark. Без устранения причины "
                    "просадка может продолжиться." + _HUMAN_ONLY)},
    "kill.reset": {
        "label": "Снять kill-switch", "group": "reset", "level": "critical-reset",
        "hold_ms": 2500, "confirm_phrase": "СНЯТЬ KILL",
        "warning": ("Возобновит входы после аварийной остановки. Отменённые ордера и остановленные "
                    "боты не восстанавливаются." + _HUMAN_ONLY)},
}
ACTION_IDS = tuple(SPECS)
# Сброс: действие → (scope для risk.reset_breaker, флаг в risk.status())
RESETS = {"kill.reset": ("kill", "kill_active"),
          "breaker.reset.daily": ("daily", "daily_breaker"),
          "breaker.reset.global": ("global", "global_breaker")}
_RESET_INACTIVE = {"kill.reset": "kill-switch не активен",
                   "breaker.reset.daily": "дневной breaker не активен",
                   "breaker.reset.global": "глобальный breaker не активен"}
_RESET_DONE = {"kill.reset": "Kill-switch снят: входы снова разрешены риск-ядром",
               "breaker.reset.daily": "Дневной breaker снят: входы снова разрешены риск-ядром",
               "breaker.reset.global": "Глобальный breaker снят: входы снова разрешены риск-ядром"}
_KILL_FLAG_REASON = "есть флаг data/KILL: движок снова включит kill-switch — флаг снимает человек"
_STOP_FLAG_REASON = "остановка уже запрошена (data/STOP_ENGINE)"


# --- Состояние (только чтение) ---

def _data_dir(root: Path) -> Path:
    # Каталог состояния demo — как config.data_dir("demo"); config не импортируем:
    # он загружает .env, а describe должен оставаться быстрым и без секретов.
    return Path(root) / "data"


def _pwsh_available() -> bool:
    return shutil.which("pwsh") is not None


def engine_script_reason(root: Path) -> Optional[str]:
    """Почему ops/engine.ps1 нельзя вызвать, или None."""
    if not (Path(root) / "ops" / "engine.ps1").is_file():
        return "нет ops/engine.ps1"
    if not _pwsh_available():
        return "нужен PowerShell 7 (pwsh)"
    return None


def engine_running(root: Path) -> Optional[bool]:
    """Жив ли процесс из data/engine.pid (как src.obsidian_status). None — не удалось проверить."""
    from .obsidian_status import process_alive, read_pid
    pid_path = _data_dir(root) / "engine.pid"
    try:
        if not pid_path.exists():
            return False             # ops/engine.ps1 stop удаляет файл PID
        return bool(process_alive(read_pid(pid_path)))
    except Exception:
        return None


def _flag(source: Optional[dict], key: str) -> Optional[bool]:
    if not isinstance(source, dict) or source.get(key) is None:
        return None
    return bool(source.get(key))


def _reason(action: str, risk: Optional[dict], engine: Optional[dict], flags: dict,
            script_reason: Optional[str]) -> Optional[str]:
    if action in ("kill.engage", "orders.cancel_all"):
        return None              # сторона безопасности: в demo доступно всегда
    if action in ("engine.pause", "engine.resume"):
        if script_reason:
            return script_reason
        running = _flag(engine, "running")
        if running is None:
            return "состояние движка неизвестно"
        if action == "engine.pause":
            if not running:
                return "движок уже остановлен"
            return _STOP_FLAG_REASON if flags.get("STOP_ENGINE") else None
        return "движок уже запущен" if running else None
    if not isinstance(risk, dict):
        return "риск-ядро не прочитано — блокировка не проверена"
    active = _flag(risk, RESETS[action][1])
    if active is None:
        return "состояние блокировки неизвестно"
    if not active:
        return _RESET_INACTIVE[action]
    if action == "kill.reset" and flags.get("KILL"):
        return _KILL_FLAG_REASON
    return None


def describe(root: Path, *, risk: Optional[dict], engine: Optional[dict],
             flags: Optional[dict], mode: str = "demo") -> dict:
    """Объект controls контракта (§2): действия и их доступность по текущим флагам.

    Без сети и без записи: только переданные словари и наличие ops/engine.ps1/pwsh.
    risk — срез risk_section (kill_active, daily_breaker, global_breaker), engine —
    с ключом running, flags — {KILL, STOP_ENGINE}. Нет данных — None.
    """
    demo = mode == "demo"
    flags = flags if isinstance(flags, dict) else {}
    script_reason = engine_script_reason(root)
    kill_active = _flag(risk, "kill_active")
    actions = []
    for action in ACTION_IDS:
        spec = SPECS[action]
        reason = _reason(action, risk, engine, flags, script_reason) if demo else ONLY_DEMO
        warning = spec["warning"]
        if action == "kill.engage" and kill_active:
            warning += " Kill-switch уже активен: повтор ещё раз отменит ордера и остановит ботов."
        if action == "engine.resume" and kill_active:
            warning += " Kill-switch активен: движок запустится, но входы останутся заблокированы."
        actions.append({"id": action, "label": spec["label"], "group": spec["group"],
                        "level": spec["level"], "hold_ms": spec["hold_ms"],
                        "confirm_phrase": spec["confirm_phrase"],
                        "available": reason is None, "reason": reason, "warning": warning})
    return {"mode": mode if mode in ("demo", "live") else "unknown", "enabled": demo,
            "actions": actions}


def current_state(root: Path) -> tuple[Optional[dict], dict, dict]:
    """(risk, engine, flags) из локального состояния — как в src.obsidian_status, только чтение."""
    from .obsidian_status import flags_section, read_risk, risk_section
    data = _data_dir(root)
    try:
        status, limits = read_risk(data / "risk_state.db")
        risk = risk_section(status, limits)
    except Exception:
        risk = None
    try:
        flags = flags_section(data)
    except OSError:
        flags = {}
    return risk, {"running": engine_running(root)}, flags


def _env_mode() -> str:
    from .config import default_mode
    return default_mode()


def describe_current(root: Path = ROOT) -> dict:
    try:
        mode = _env_mode()
    except Exception:
        mode = "unknown"
    risk, engine, flags = current_state(root)
    return _redact(describe(root, risk=risk, engine=engine, flags=flags, mode=mode))


# --- Выполнение ---

@dataclass
class Deps:
    """Внешние зависимости run(): тесты подменяют их фейками и временными каталогами."""
    risk_db: Path
    kill_flag: Path
    stop_flag: Path
    env_mode: Callable[[], str]
    engine_running: Callable[[], Optional[bool]]
    exchange: Callable[[], Any]
    emergency_stop: Callable[..., dict]
    engine_script: Callable[[str], dict]


def default_deps(root: Path = ROOT) -> Deps:
    """Настоящие зависимости: demo-ключи, demo-база риска, ops/engine.ps1 этого корня."""
    root = Path(root)
    data = _data_dir(root)

    def exchange():
        from .config import load_settings
        from .connector import create_exchange
        return create_exchange(load_settings("demo"))   # режим задан явно: только demo

    def stop(ex, include_bots: bool) -> dict:
        from .connector import emergency_stop
        return emergency_stop(ex, include_bots=include_bots)

    def engine_script(operation: str) -> dict:
        from .control_panel import _script
        return _script(root, "engine.ps1", operation, ENGINE_TIMEOUT_S)

    return Deps(risk_db=data / "risk_state.db", kill_flag=data / "KILL",
                stop_flag=data / "STOP_ENGINE", env_mode=_env_mode,
                engine_running=lambda: engine_running(root), exchange=exchange,
                emergency_stop=stop, engine_script=engine_script)


def _redact(payload):
    """Общая чистка строк (_redact_data). Summary строится из констант и чисел, поэтому
    сбой импорта control_panel не должен блокировать аварийное действие."""
    try:
        from .control_panel import _redact_data
    except Exception:
        return payload
    return _redact_data(payload)


def _audit(root: Path, action: str, outcome: str) -> None:
    """Строка журнала без токена, фразы, prompt и вывода команд."""
    try:
        path = Path(root) / AUDIT_RELATIVE
        path.parent.mkdir(parents=True, exist_ok=True)
        entry = {"ts": datetime.now(timezone.utc).isoformat(), "action": action,
                 "outcome": outcome, "via": "morphy"}
        with path.open("a", encoding="utf-8") as stream:
            stream.write(json.dumps(entry, ensure_ascii=False) + "\n")
    except OSError:
        pass


def _count(report: dict, key: str) -> int:
    value = report.get(key) if isinstance(report, dict) else None
    return len(value) if isinstance(value, (list, tuple)) else 0


def _risk_flags(deps: Deps) -> Optional[dict]:
    """risk.status() demo-базы или None, если базы нет (пустую базу не создаём)."""
    if not Path(deps.risk_db).exists():
        return None
    from . import risk
    risk.init(deps.risk_db)
    return risk.status()


def _kill(deps: Deps) -> tuple[str, str, dict]:
    """Как src.ops cmd_kill: флаг ставится и без биржи, отмена — через emergency_stop."""
    from . import risk
    risk.init(deps.risk_db)
    connected = True
    raw: dict = {}               # полный отчёт emergency_stop: kill_switch отдаёт не все поля

    def canceller(_flatten: bool) -> dict:
        report = deps.emergency_stop(ex, include_bots=True)
        if isinstance(report, dict):
            raw.update(report)
        return report

    try:
        ex = deps.exchange()
    except Exception as exc:
        connected = False
        # Текст исключения не печатаем: только тип, и только в stderr
        log.error("биржа demo недоступна (%s) — входы блокирую, ордера не отменены", type(exc).__name__)
    else:
        risk.set_order_canceller(canceller)
    report = risk.kill_switch(flatten=False, by=RESET_BY)
    details = {"cancelled": _count(report, "cancelled"), "failed": _count(report, "failed"),
               "bots_stopped": _count(raw, "bots_stopped")}
    if not connected:
        return "failed", "Kill-switch включён, входы заблокированы, но биржа demo недоступна: ордера не отменены — проверьте вручную", details
    if details["failed"]:
        return "failed", (f"Kill-switch включён, входы заблокированы; отменено {details['cancelled']}, "
                          f"не отменено {details['failed']} — проверьте ордера и ботов вручную"), details
    return "ok", (f"Kill-switch включён: входы заблокированы, отменено ордеров и ботов: "
                  f"{details['cancelled']}"), details


def _cancel_all(deps: Deps) -> tuple[str, str, dict]:
    try:
        ex = deps.exchange()
    except Exception as exc:
        log.error("биржа demo недоступна (%s) — ордера не отменены", type(exc).__name__)
        return "failed", "Биржа demo недоступна: ордера не отменены", {"cancelled": 0, "failed": 0}
    report = deps.emergency_stop(ex, include_bots=False)
    details = {"cancelled": _count(report, "cancelled"), "failed": _count(report, "failed")}
    if details["failed"]:
        return "failed", (f"Отменено {details['cancelled']}, не отменено {details['failed']} — "
                          f"проверьте ордера вручную"), details
    return "ok", (f"Отменено ордеров: {details['cancelled']}. Kill-switch не включён; позиции "
                  f"могут остаться без стопов"), details


def _engine(root: Path, action: str, deps: Deps) -> tuple[str, str, dict]:
    script_reason = engine_script_reason(root)
    if script_reason:
        return "rejected", f"Движок недоступен: {script_reason}", {}
    running = deps.engine_running()
    if running is None:
        return "rejected", "Состояние движка неизвестно — действие не выполнено", {}
    operation = "stop" if action == "engine.pause" else "start"
    if operation == "stop":
        if not running:
            return "rejected", "Движок уже остановлен", {}
        if Path(deps.stop_flag).exists():
            return "rejected", "Остановка уже запрошена (data/STOP_ENGINE)", {}
    elif running:
        return "rejected", "Движок уже запущен", {}
    try:
        result = deps.engine_script(operation)
    except subprocess.TimeoutExpired:
        return "failed", f"ops/engine.ps1 {operation} не завершился за {ENGINE_TIMEOUT_S} с — проверьте движок", {}
    code = result.get("exit_code") if isinstance(result, dict) else None
    details = {"exit_code": code if isinstance(code, int) else None}
    # Вывод скрипта (result["output"]) не передаётся: только код завершения
    if isinstance(result, dict) and result.get("ok"):
        done = "Движок остановлен" if operation == "stop" else "Движок запущен"
        return "ok", f"{done} (ops/engine.ps1 {operation})", details
    return "failed", f"ops/engine.ps1 {operation} завершился с кодом {details['exit_code']}", details


def _reset(action: str, deps: Deps) -> tuple[str, str, dict]:
    scope, key = RESETS[action]
    status = _risk_flags(deps)
    if status is None:
        return "rejected", "База риска не найдена — блокировки нет", {}
    if not status.get(key):
        return "rejected", f"Нечего снимать: {_RESET_INACTIVE[action]}", {}
    if action == "kill.reset" and Path(deps.kill_flag).exists():
        return "rejected", f"Не снято: {_KILL_FLAG_REASON}", {}
    from . import risk
    risk.reset_breaker(scope, by=RESET_BY)
    return "ok", _RESET_DONE[action], {"scope": scope}


def run(root: Path, action: str, *, confirm: Optional[str] = None, mode: str = "demo",
        deps: Optional[Deps] = None) -> dict:
    """Выполнить действие экрана Morphy. Возвращает {ok, action, summary, outcome, details?}.

    Только demo: mode и режим окружения процесса проверяются до любого вызова.
    Предусловия перепроверяются здесь же. Сброс — только с верной фразой
    подтверждения. Каждый вызов попадает в журнал действий.
    """
    root = Path(root)
    known = action if isinstance(action, str) and action in SPECS else None
    label = known or "invalid"

    def finish(outcome: str, summary: str, details: Optional[dict] = None) -> dict:
        _audit(root, label, outcome)
        result = {"ok": outcome == "ok", "action": label, "summary": summary, "outcome": outcome}
        if details:
            result["details"] = details
        return _redact(result)

    if known is None:
        return finish("rejected", "Неизвестное действие")
    if mode != "demo":
        return finish("rejected", "Только demo: действие вне demo отклонено")
    deps = deps or default_deps(root)
    try:
        env_mode = deps.env_mode()
    except Exception:
        env_mode = None
    if env_mode != "demo":
        return finish("rejected", "Только demo: окружение процесса не в режиме demo (OKX_MODE)")
    phrase = SPECS[known]["confirm_phrase"]
    if phrase is not None and confirm != phrase:
        return finish("rejected", "Фраза подтверждения не совпала — блокировка не снята")
    try:
        if known == "kill.engage":
            outcome, summary, details = _kill(deps)
        elif known == "orders.cancel_all":
            outcome, summary, details = _cancel_all(deps)
        elif known in ("engine.pause", "engine.resume"):
            outcome, summary, details = _engine(root, known, deps)
        else:
            outcome, summary, details = _reset(known, deps)
    except Exception as exc:
        log.error("действие %s не выполнено: %s", known, type(exc).__name__)
        return finish("failed", f"Действие не выполнено: внутренняя ошибка ({type(exc).__name__})")
    return finish(outcome, summary, details)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="python -m src.morphy_actions",
                                     description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest="cmd", required=True)
    sub.add_parser("describe", help="controls по текущему локальному состоянию")
    run_cmd = sub.add_parser("run", help="выполнить действие экрана Morphy")
    run_cmd.add_argument("action")
    run_cmd.add_argument("--mode", default="demo", help="только demo; иное значение отклоняется")
    args = parser.parse_args(argv)
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    if args.cmd == "describe":
        try:
            payload = describe_current(ROOT)
        except Exception as exc:
            payload = {"mode": "unknown", "enabled": False, "actions": [], "error": type(exc).__name__}
        print(json.dumps(payload, ensure_ascii=False))
        return 0
    logging.basicConfig(level=logging.INFO, stream=sys.stderr,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    result = run(ROOT, args.action, confirm=os.getenv(CONFIRM_ENV), mode=args.mode)
    print(json.dumps(result, ensure_ascii=False))
    return 0 if result.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())
