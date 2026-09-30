"""Сторож движка (ENGINE-WATCHDOG): перезапуск при смерти или зависании процесса.

    python -m src.engine_watchdog            # только проверка: вердикт, ничего не делает
    python -m src.engine_watchdog --json     # то же, машинный вердикт
    python -m src.engine_watchdog --act      # как Планировщик: повторная проверка, перезапуск, лог

Код выхода: 0 — делать нечего (движок жив, остановлен штатно, пауза); 1 — нужен перезапуск
(с --act: перезапуск выполнен или упёрся в лимит); 2 — ошибка сторожа.

Задача Планировщика «OKX-Bot Watchdog» (регистрирует человек: `ops\\autostart.ps1 register`)
раз в 5 мин запускает `.venv\\Scripts\\pythonw.exe -m src.engine_watchdog --act`. pythonw — без
консоли, поэтому окно не мелькает раз в 5 мин; `ops\\engine.ps1` вызывается скрытым pwsh 7.
Вручную то же самое — `ops\\autostart.ps1 watch`.

Две проверки
------------
1. Процесс из data/engine.pid жив и это движок ЭТОГО каталога (ENGINE-PID-GUARD): в PID
   записан лаунчер .venv\\Scripts\\python.exe этого корня (он ждёт настоящий интерпретатор и
   завершается вместе с ним), поэтому «свой» — живой процесс с этим путём образа
   (OpenProcess + GetExitCodeProcess + QueryFullProcessImageNameW). Другой путь — «чужой»:
   копия проекта с тем же data/engine.pid (25.09 09:09 так убили наш движок) или повторно
   выданный PID. Чужой процесс сторож считает мёртвым движком и сам не трогает никогда;
   `ops\\engine.ps1 stop` тоже проверяет владельца и чужой PID не останавливает.
2. logs/engine.log обновлялся не больше SILENCE_S = 210 с назад. Частота проверена по логам
   прогонов 24.09–30.09: сверка пишет строку раз в минуту в любом режиме — успех, ошибка биржи
   или обрыв DNS (реальный максимум промежутка между строками — 72 с). Heartbeat не нужен:
   210 с — это почти три таких паузы. Дамп стеков faulthandler (src/engine.py,
   HANG_DUMP_S = 150 с) при зависании пишется один раз, поэтому тишину он откладывает не
   дольше чем на 150 с и не маскирует её.

Решение (decide) — по порядку
-----------------------------
- data/AUTOSTART_OFF → «пауза»: человек выключил автоматику (тот же флаг, что у автозапуска).
- data/STOP_ENGINE → «останавливается»: идёт штатная остановка. Движок удаляет флаг сам,
  как только его прочитал; флаг остался — движок его не прочитал, а намерение остановить
  было. Перезапуск спорил бы с человеком или агентом.
- Нет data/engine.pid → «остановлен»: `ops\\engine.ps1 stop` удаляет PID после остановки
  (или движок ещё не запускали). Штатная остановка — не авария.
- В текущем engine.log есть строка «STOP_ENGINE: штатная остановка» или «Остановлено
  пользователем» → «остановлен»: движок вышел по флагу или Ctrl+C, хотя PID остался. Лог
  ротируется при каждом старте, поэтому строка относится к текущему запуску.
- Лог свежий → «жив». Если при этом процесса из PID нет — всё равно «жив» и без перезапуска:
  лог пишет кто-то ещё (осиротевший интерпретатор после убийства лаунчера), и второй движок
  нельзя. Свежая трассировка падения тоже держит лог свежим — перезапуск на следующем круге.
- Лог молчит дольше SILENCE_S: процесс жив → «тишина» (завис), процесса нет → «мёртв».
  Оба — перезапуск `ops\\engine.ps1 stop` + `start`, если за час их было меньше
  MAX_RESTARTS_PER_HOUR, иначе «лимит»: падающий на старте движок не крутится по кругу.

data/KILL перезапуск не блокирует. Kill-switch хранится в риск-ядре (risk-БД) и после
рестарта остаётся активным до ручного `python -m src.ops reset kill`, а сам флаг исполняет
цикл флагов движка: мёртвый движок флаг не прочитает, новый — прочитает и отменит ордера.
Перезапуск здесь — действие в сторону безопасности.

Перед перезапуском (--act) проверка повторяется через CONFIRM_S = 90 с: после сна машины
цикл сверки догоняет не сразу, а PID меняется при `engine.ps1 start`. Одновременный старт с
задачей автозапуска исключает мьютекс в `ops\\engine.ps1 start`.

Лог logs/autostart.log (общий с автозапуском): строка «watch <состояние>: …» пишется при
смене состояния и при каждом перезапуске — «жив» раз в 5 мин лог не засоряет.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import time
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path
from typing import Callable, Optional

ROOT = Path(__file__).resolve().parent.parent
SILENCE_S = 210.0  # лог молчит дольше — движок завис или мёртв
CONFIRM_S = 90.0  # пауза перед повторной проверкой, до перезапуска
MAX_RESTARTS_PER_HOUR = 3
LOG_TAIL_BYTES = 16384  # хвост engine.log для поиска строки штатной остановки
AUTOSTART_LOG_TAIL_BYTES = 65536
STOP_MARKERS = ("STOP_ENGINE: штатная остановка", "Остановлено пользователем")
RESTART_STATES = ("тишина", "мёртв")
_WATCH_LINE_RE = re.compile(r"^(\S+) watch (\S+):")


@dataclass(frozen=True)
class Paths:
    pid: Path
    log: Path
    stop_flag: Path
    off_flag: Path
    kill_flag: Path
    autostart_log: Path
    engine_ps1: Path
    python: Path  # лаунчер venv этого корня — образ процесса из data/engine.pid

    @classmethod
    def under(cls, root: Path) -> "Paths":
        return cls(
            pid=root / "data" / "engine.pid",
            log=root / "logs" / "engine.log",
            stop_flag=root / "data" / "STOP_ENGINE",
            off_flag=root / "data" / "AUTOSTART_OFF",
            kill_flag=root / "data" / "KILL",
            autostart_log=root / "logs" / "autostart.log",
            engine_ps1=root / "ops" / "engine.ps1",
            python=root / ".venv" / "Scripts" / "python.exe",
        )


@dataclass(frozen=True)
class Facts:
    """Что сторож увидел; now и log_mtime — секунды Unix."""
    now: float
    pid_file: bool
    pid: Optional[int]  # None — файла нет или он не читается
    owner: str  # свой | чужой | нет — процесс из PID (classify_process)
    log_mtime: Optional[float]  # None — engine.log нет
    stop_marker: bool  # в engine.log строка штатной остановки
    stop_flag: bool
    off_flag: bool
    kill_flag: bool
    restarts_last_hour: int

    @property
    def alive(self) -> bool:
        return self.owner == "свой"

    @property
    def log_age(self) -> Optional[float]:
        return None if self.log_mtime is None else max(0.0, self.now - self.log_mtime)


@dataclass(frozen=True)
class Verdict:
    state: str  # жив | тишина | мёртв | остановлен | останавливается | пауза | лимит
    restart: bool
    reason: str


def _age_text(facts: Facts) -> str:
    age = facts.log_age
    return "engine.log нет" if age is None else f"engine.log обновлён {age:.0f} с назад"


def _owner_text(facts: Facts) -> str:
    return ("— чужой процесс (не .venv этого каталога), его не трогаю"
            if facts.owner == "чужой" else "не найден")


def decide(facts: Facts, silence_s: float = SILENCE_S,
           max_restarts: int = MAX_RESTARTS_PER_HOUR) -> Verdict:
    """Жив / тишина / мёртв (и штатные состояния) — правила в docstring модуля."""
    pid_text = f"PID {facts.pid}" if facts.pid is not None else "PID не читается"
    if facts.off_flag:
        return Verdict("пауза", False, "есть data/AUTOSTART_OFF — сторож выключен человеком")
    if facts.stop_flag:
        return Verdict("останавливается", False, "есть data/STOP_ENGINE — идёт штатная остановка")
    if not facts.pid_file:
        return Verdict("остановлен", False, "нет data/engine.pid — движок остановлен штатно")
    if facts.stop_marker:
        return Verdict("остановлен", False,
                       f"{pid_text}: в engine.log строка штатной остановки текущего запуска")
    age = facts.log_age
    if age is not None and age <= silence_s:
        if facts.alive:
            return Verdict("жив", False, f"{pid_text} жив, {_age_text(facts)}")
        return Verdict("жив", False, f"{pid_text} {_owner_text(facts)}, но {_age_text(facts)} — "
                                     f"лог пишет другой процесс; второй движок не запускаю")
    state = "тишина" if facts.alive else "мёртв"
    what = (f"{pid_text} жив, но {_age_text(facts)} (порог {silence_s:.0f} с) — завис"
            if facts.alive else f"{pid_text} {_owner_text(facts)}, {_age_text(facts)}")
    if facts.kill_flag:
        what += "; есть data/KILL — новый движок исполнит kill-switch"
    if facts.restarts_last_hour >= max_restarts:
        return Verdict("лимит", False, f"{state}: {what}; перезапусков за час уже "
                                       f"{facts.restarts_last_hour} — жду человека")
    return Verdict(state, True, what)


# --- Сбор фактов ---

def read_pid(path: Path) -> tuple[bool, Optional[int]]:
    """(файл есть, PID). PID пишет Set-Content — возможен BOM и перевод строки."""
    try:
        raw = path.read_bytes()
    except FileNotFoundError:
        return False, None
    except OSError:
        return True, None
    text = raw.decode("utf-8-sig", errors="replace").strip()
    return True, int(text) if text.isdigit() else None


def process_image(pid: int) -> Optional[str]:
    """Путь образа живого процесса; "" — жив, но путь не прочитан; None — процесса нет."""
    if pid <= 0:
        return None
    if sys.platform != "win32":
        try:
            os.kill(pid, 0)
        except PermissionError:
            return ""
        except OSError:
            return None
        try:
            return os.readlink(f"/proc/{pid}/exe")
        except OSError:
            return ""
    import ctypes
    from ctypes import wintypes

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.OpenProcess.restype = wintypes.HANDLE
    kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel32.GetExitCodeProcess.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
    kernel32.QueryFullProcessImageNameW.argtypes = [
        wintypes.HANDLE, wintypes.DWORD, wintypes.LPWSTR, ctypes.POINTER(wintypes.DWORD)]
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    process_query_limited_information, still_active, error_access_denied = 0x1000, 259, 5
    handle = kernel32.OpenProcess(process_query_limited_information, False, pid)
    if not handle:
        # нет процесса (ERROR_INVALID_PARAMETER) или нет доступа: тогда он есть, но не наш
        return "" if ctypes.get_last_error() == error_access_denied else None
    try:
        code = wintypes.DWORD()
        if not kernel32.GetExitCodeProcess(handle, ctypes.byref(code)) or code.value != still_active:
            return None
        buf = ctypes.create_unicode_buffer(1024)
        size = wintypes.DWORD(len(buf))
        if not kernel32.QueryFullProcessImageNameW(handle, 0, buf, ctypes.byref(size)):
            return ""
        return buf.value
    finally:
        kernel32.CloseHandle(handle)


def _same_path(a: str, b: Path) -> bool:
    return os.path.normcase(os.path.abspath(a)) == os.path.normcase(os.path.abspath(str(b)))


def classify_process(image: Optional[str], expected: Path) -> str:
    """ENGINE-PID-GUARD: «свой» — образ = лаунчер venv этого корня, «нет» — процесса нет,
    иначе «чужой» (в том числе путь не прочитан: не доказано, что наш, — не трогаем)."""
    if image is None:
        return "нет"
    return "свой" if image and _same_path(image, expected) else "чужой"


def _tail_text(path: Path, limit: int) -> str:
    try:
        with path.open("rb") as fh:
            fh.seek(0, os.SEEK_END)
            size = fh.tell()
            fh.seek(max(0, size - limit))
            return fh.read().decode("utf-8", errors="replace")
    except OSError:
        return ""


def watch_lines(autostart_log: Path) -> list[tuple[Optional[float], str]]:
    """Строки сторожа из хвоста logs/autostart.log: (время Unix или None, состояние)."""
    out = []
    for line in _tail_text(autostart_log, AUTOSTART_LOG_TAIL_BYTES).splitlines():
        m = _WATCH_LINE_RE.match(line)
        if not m:
            continue
        try:
            ts: Optional[float] = datetime.fromisoformat(m.group(1)).timestamp()
        except ValueError:
            ts = None
        out.append((ts, m.group(2)))
    return out


def restarts_since(autostart_log: Path, since: float) -> int:
    return sum(1 for ts, state in watch_lines(autostart_log)
               if state == "перезапуск" and ts is not None and ts >= since)


def gather(paths: Paths, *, clock: Callable[[], float] = time.time,
           probe: Callable[[int], Optional[str]] = process_image) -> Facts:
    now = clock()
    pid_file, pid = read_pid(paths.pid)
    try:
        log_mtime: Optional[float] = paths.log.stat().st_mtime
    except OSError:
        log_mtime = None
    tail = _tail_text(paths.log, LOG_TAIL_BYTES) if pid_file else ""
    return Facts(
        now=now,
        pid_file=pid_file,
        pid=pid,
        owner=classify_process(probe(pid), paths.python) if pid else "нет",
        log_mtime=log_mtime,
        stop_marker=any(marker in tail for marker in STOP_MARKERS),
        stop_flag=paths.stop_flag.exists(),
        off_flag=paths.off_flag.exists(),
        kill_flag=paths.kill_flag.exists(),
        restarts_last_hour=restarts_since(paths.autostart_log, now - 3600),
    )


# --- Действие ---

def append_log(autostart_log: Path, state: str, text: str) -> None:
    autostart_log.parent.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().astimezone().isoformat(timespec="seconds")
    line = " ".join(f"{stamp} watch {state}: {text}".split())
    with autostart_log.open("a", encoding="utf-8") as fh:
        fh.write(line + "\n")


def last_watch_state(autostart_log: Path) -> Optional[str]:
    lines = watch_lines(autostart_log)
    return lines[-1][1] if lines else None


def pwsh_path() -> str:
    """pwsh 7: alias WindowsApps стабилен между версиями; 5.1 ломает кириллицу в .ps1 без BOM."""
    alias = Path(os.environ.get("LOCALAPPDATA", "")) / "Microsoft" / "WindowsApps" / "pwsh.exe"
    if alias.exists():
        return str(alias)
    found = shutil.which("pwsh")
    if not found:
        raise FileNotFoundError("pwsh 7 не найден")
    return found


def run_engine_ps1(engine_ps1: Path, action: str, timeout: float = 180.0) -> str:
    """`ops\\engine.ps1 <action>` скрытым pwsh; вывод одной строкой (UTF-8 задаём явно:
    у процесса без окна кодовая страница консоли OEM)."""
    command = ("[Console]::OutputEncoding = [Text.UTF8Encoding]::new(); "
               f"& '{engine_ps1}' {action}; exit $LASTEXITCODE")
    flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    proc = subprocess.run(
        [pwsh_path(), "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", command],
        cwd=engine_ps1.parent.parent, capture_output=True, timeout=timeout, creationflags=flags)
    text = (proc.stdout + proc.stderr).decode("utf-8", errors="replace")
    text = " | ".join(line.strip() for line in text.splitlines() if line.strip())
    return f"{text or 'нет вывода'} (код {proc.returncode})"


def check(paths: Paths, *, act: bool = False, confirm_s: float = CONFIRM_S,
          silence_s: float = SILENCE_S, clock: Callable[[], float] = time.time,
          sleep: Callable[[float], None] = time.sleep,
          probe: Callable[[int], Optional[str]] = process_image,
          runner: Callable[[Path, str], str] = run_engine_ps1) -> Verdict:
    """Проверка; с act — повторная через confirm_s, перезапуск и запись в autostart.log."""
    verdict = decide(gather(paths, clock=clock, probe=probe), silence_s)
    if not act:
        return verdict
    if verdict.restart and confirm_s > 0:
        sleep(confirm_s)
        verdict = decide(gather(paths, clock=clock, probe=probe), silence_s)
    if verdict.restart:
        append_log(paths.autostart_log, verdict.state, verdict.reason)
        parts = []
        for action in ("stop", "start"):
            try:
                parts.append(f"engine.ps1 {action}: {runner(paths.engine_ps1, action)}")
            except Exception as exc:  # старт всё равно пробуем: stop мог упасть на мёртвом PID
                parts.append(f"engine.ps1 {action}: ошибка {type(exc).__name__}: {exc}")
        append_log(paths.autostart_log, "перезапуск", " | ".join(parts))
    elif verdict.state != last_watch_state(paths.autostart_log):
        append_log(paths.autostart_log, verdict.state, verdict.reason)
    return verdict


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="python -m src.engine_watchdog",
                                description="Сторож движка: жив / тишина / мёртв (ENGINE-WATCHDOG)")
    p.add_argument("--act", action="store_true",
                   help="перезапустить при тишине или смерти и записать в logs/autostart.log")
    p.add_argument("--json", action="store_true", help="вердикт в JSON")
    p.add_argument("--confirm-s", type=float, default=CONFIRM_S,
                   help=f"пауза перед повторной проверкой с --act, с (по умолчанию {CONFIRM_S:.0f})")
    p.add_argument("--silence-s", type=float, default=SILENCE_S,
                   help=f"порог тишины engine.log, с (по умолчанию {SILENCE_S:.0f})")
    return p


def main(argv: Optional[list[str]] = None, root: Path = ROOT) -> int:
    args = build_parser().parse_args(argv)
    paths = Paths.under(root)
    try:
        verdict = check(paths, act=args.act, confirm_s=args.confirm_s, silence_s=args.silence_s)
    except Exception as exc:  # у pythonw нет stderr — ошибку сохраняем в лог
        if args.act:
            try:
                append_log(paths.autostart_log, "ошибка", f"{type(exc).__name__}: {exc}")
            except OSError:
                pass
        print(f"ошибка сторожа: {type(exc).__name__}: {exc}")
        return 2
    if args.json:
        print(json.dumps(asdict(verdict), ensure_ascii=False))
    else:
        print(f"{verdict.state}: {verdict.reason}"
              + (" → перезапуск" if verdict.restart and not args.act else ""))
    return 1 if verdict.restart or verdict.state == "лимит" else 0


if __name__ == "__main__":
    if sys.stdout is not None and hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    sys.exit(main())
