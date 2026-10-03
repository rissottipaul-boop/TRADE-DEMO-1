"""PANEL-LAUNCHER-SEC: ops/panel.ps1 в Windows PowerShell 5.1 и PowerShell 7.

Каждый тест работает во временной копии проекта: ops/panel.ps1 + поддельный src/control_panel.py
(тот же контракт main(['--port', P, '--token', T]) и /health). Подделка печатает ссылку с токеном в
stdout и заваливает stdout/stderr мегабайтами — с непрочитанными пайпами сервер повис бы до /health.
Вывод PowerShell пишется в файлы, а не в пайпы: фоновая панель не должна держать вывод вызывающего.
PANEL-LAUNCHER-FLAKY: запуск PowerShell повторяется, только если упал сам процесс PowerShell
(_host_crashed); код скрипта без повтора проверяется как есть.
"""
from __future__ import annotations

import contextlib
import functools
import io
import json
import os
import re
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import unittest
import urllib.error
import urllib.request
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SCRIPT = ROOT / "ops" / "panel.ps1"

FAKE_PANEL = r'''
import argparse, os, sys
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler
from urllib.parse import urlsplit, parse_qs


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def do_GET(self):
        parsed = urlsplit(self.path)
        if parsed.path == "/health":
            code, body = 200, b'{"ok": true}'
        elif parsed.path == "/" and parse_qs(parsed.query).get("token", [""])[0] == self.server.token:
            code, body = 200, b"panel"
        else:
            code, body = 403, b"{}"
        self.send_response(code)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int)
    parser.add_argument("--token")
    args = parser.parse_args(argv)
    server = ThreadingHTTPServer(("127.0.0.1", args.port), Handler)
    server.token = args.token
    print("Panel: http://127.0.0.1:%d/?token=%s" % (args.port, args.token), flush=True)
    line = "x" * 1023 + "\n"
    for _ in range(2048):
        sys.stdout.write(line)
    sys.stdout.flush()
    for _ in range(512):
        sys.stderr.write("stderr-noise " + "y" * 100 + "\n")
    sys.stderr.flush()
    os.write(1, b"z" * 300000)
    os.write(2, b"w" * 300000 + b"\n")
    server.serve_forever(poll_interval=0.2)
    return 0
'''

# PANEL-LAUNCHER-FLAKY: лаунчер, который сам порт не слушает, а с паузой порождает сервер с той же
# меткой запуска — как .venv\Scripts\python.exe, запускающий базовый интерпретатор дочерним процессом
SLOW_SPAWN_BOOT = r'''
import subprocess, sys, time

SERVE = "import sys; from src import control_panel; control_panel.main(['--port', sys.argv[3], '--token', 'slow' * 8])"


def main():
    time.sleep(0.8)
    child = subprocess.Popen([sys.executable, "-c", SERVE] + sys.argv[1:], stdin=subprocess.DEVNULL,
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    return child.wait()


if __name__ == "__main__":
    raise SystemExit(main())
'''


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def _alive(pid: int) -> bool:
    out = subprocess.run(["tasklist", "/FI", f"PID eq {pid}", "/NH", "/FO", "CSV"],
                         capture_output=True, text=True, errors="replace").stdout
    return f'"{pid}"' in out


def _kill(pid: int) -> None:
    subprocess.run(["taskkill", "/F", "/T", "/PID", str(pid)], capture_output=True)


def _http(url: str) -> int:
    try:
        with urllib.request.build_opener(urllib.request.ProxyHandler({})).open(url, timeout=3) as resp:
            return resp.status
    except urllib.error.HTTPError as exc:
        exc.close()
        return exc.code


def _windows_powershell() -> str | None:
    path = Path(os.environ.get("SystemRoot", r"C:\Windows")) / "System32" / "WindowsPowerShell" / "v1.0" / "powershell.exe"
    return str(path) if path.exists() else None


def _host_crashed(returncode: int | None) -> bool:
    """Процесс PowerShell упал сам, а не вернул код скрипта: NTSTATUS 0xC0000005 и т. п.

    PANEL-LAUNCHER-FLAKY: powershell.exe 5.1 изредка (≈1 запуск из тысяч) гибнет при первом обращении
    к CIM с «internal error in the .NET Runtime» (.NET Runtime 1023, clr.dll, 0xC0000005). Скрипты тестов
    возвращают 0/1/2, коды ≥ 0x80000000 бывают только у аварийно завершённого процесса.
    """
    return returncode is not None and (returncode & 0xFFFFFFFF) >= 0x80000000


HOST_ATTEMPTS = 3


def _retry_after_crash(args: list[str], returncode: int, attempt: int) -> bool:
    """Повторять ли запуск PowerShell. Повтор виден в выводе тестов, чтобы сбои хоста не терялись."""
    if not _host_crashed(returncode) or attempt >= HOST_ATTEMPTS:
        return False
    sys.stderr.write("\n[PANEL-LAUNCHER-FLAKY] %s аварийно завершился (код 0x%08X), повтор %d\n"
                     % (Path(args[0]).name, returncode & 0xFFFFFFFF, attempt))
    return True


def _run_shell(args: list[str], **kwargs) -> subprocess.CompletedProcess:
    """subprocess.run для PowerShell: повтор, только если упал сам процесс PowerShell (не скрипт).

    Действия panel.ps1 и вспомогательные запросы повторяемы; любой другой код выхода — без повтора.
    """
    attempt = 1
    while True:
        proc = subprocess.run(args, **kwargs)
        if not _retry_after_crash(args, proc.returncode, attempt):
            return proc
        attempt += 1


def _without_module_path() -> dict[str, str]:
    """Окружение без PSModulePath — так powershell.exe стартует из Проводника, cmd или планировщика.

    Ключи os.environ в Windows приведены к верхнему регистру, поэтому сравнение без учёта регистра.
    """
    return {k: v for k, v in os.environ.items() if k.upper() != "PSMODULEPATH"}


@functools.lru_cache(maxsize=1)
def _pwsh7_module_path() -> str | None:
    """PSModulePath, который наследуют потомки pwsh 7: каталоги модулей 7.x стоят впереди.

    PANEL-LAUNCHER-PS51-ENV: Python, запущенный из pwsh 7, передаёт это значение дальше, и
    powershell.exe 5.1 автозагружал Microsoft.PowerShell.Security от 7.x («module could not be loaded»).
    """
    pwsh = shutil.which("pwsh")
    if not pwsh:
        return None
    out = _run_shell([pwsh, "-NoProfile", "-NonInteractive", "-Command", "$env:PSModulePath"],
                     capture_output=True, text=True, stdin=subprocess.DEVNULL,
                     env=_without_module_path(), timeout=60).stdout.strip()
    return out or None


def _helper_env(shell: str) -> dict[str, str] | None:
    """Окружение для вспомогательных вызовов PowerShell, которые сами не проверяются.

    Windows PowerShell 5.1 — без унаследованного PSModulePath (иначе исход зависит от родителя тестов),
    PowerShell 7 — как есть.
    """
    return _without_module_path() if shell == _windows_powershell() else None


class _LauncherCase:
    shell: str | None = None
    # Окружение panel.ps1: "inherit" — как у тестов; "clean" — без PSModulePath;
    # "pwsh7" — PSModulePath потомка pwsh 7 (худший случай для 5.1, не зависит от родителя тестов)
    env_mode = "inherit"

    def setUp(self):
        if os.name != "nt":
            self.skipTest("только Windows")
        if not self.shell:
            self.skipTest("нет нужной версии PowerShell")
        self.tmp = Path(tempfile.mkdtemp(prefix="panel-launcher-"))
        # Чистим и при падении setUp ниже: tearDown тогда не вызывается (TEST-TEMP-CLEANUP).
        self.addCleanup(shutil.rmtree, self.tmp, True)
        (self.tmp / "ops").mkdir()
        (self.tmp / "src").mkdir()
        shutil.copy2(SCRIPT, self.tmp / "ops" / "panel.ps1")
        shutil.copy2(ROOT / "src" / "control_panel_boot.py", self.tmp / "src" / "control_panel_boot.py")
        (self.tmp / "src" / "__init__.py").write_text("", encoding="utf-8")
        (self.tmp / "src" / "control_panel.py").write_text(FAKE_PANEL, encoding="utf-8")
        self.port = _free_port()
        self.helpers: list[subprocess.Popen] = []
        self.pid_file = self.tmp / "data" / "control_panel.pid"
        self.token_file = self.tmp / "data" / "control_panel_token.dpapi"
        self.log_file = self.tmp / "logs" / "control-panel.log"

    def tearDown(self):
        try:
            record = self._record()
            if record:
                self.run_ps("stop")
                nonce = record.get("nonce")
                for key in ("server_pid", "launcher_pid"):
                    pid = int(record.get(key) or 0)
                    # Добиваем только процесс этого запуска: освободившийся PID мог достаться чужому процессу
                    if pid and nonce and _alive(pid) and f"kontur-panel {nonce}" in self._command_line(pid):
                        _kill(pid)
            for helper in self.helpers:
                if helper.poll() is None:
                    helper.kill()
                    helper.wait(10)
        finally:
            # rmtree — всегда, даже если остановка/убийство процессов выше упали:
            # иначе временный каталог остаётся в корне (TEST-TEMP-CLEANUP).
            shutil.rmtree(self.tmp, ignore_errors=True)

    # ---------- помощники ----------

    def _ps_env(self) -> dict[str, str] | None:
        if self.env_mode == "inherit":
            return None
        env = _without_module_path()
        if self.env_mode == "pwsh7":
            polluted = _pwsh7_module_path()
            if polluted:  # без pwsh 7 на машине такого PSModulePath не бывает — остаётся чистое
                env["PSModulePath"] = polluted
        return env

    def run_ps(self, action: str, *extra: str, port: bool = True, python: str | None = None) -> tuple[int, str]:
        args = [self.shell, "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass",
                "-File", str(self.tmp / "ops" / "panel.ps1"), "-Action", action,
                "-PythonExe", python or sys.executable]
        if port:
            args += ["-Port", str(self.port)]
        args += list(extra)
        attempt = 1
        while True:
            out_path = self.tmp / f"out-{uuid.uuid4().hex}.txt"
            with open(out_path, "wb") as out:
                proc = subprocess.run(args, stdout=out, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL,
                                      cwd=str(self.tmp), env=self._ps_env(), timeout=120)
            text = out_path.read_bytes().decode("utf-8", errors="replace")
            # Файл вывода свободен после выхода PowerShell: фоновая панель не унаследовала дескриптор
            # вызывающего (иначе WinError 32 здесь, а читатель через пайп ждал бы её остановки)
            out_path.unlink()
            if not _retry_after_crash(args, proc.returncode, attempt):
                return proc.returncode, text
            attempt += 1

    def _record(self) -> dict | None:
        if not self.pid_file.exists():
            return None
        try:
            data = json.loads(self.pid_file.read_text(encoding="utf-8-sig"))
        except ValueError:
            return None
        return data if isinstance(data, dict) else None

    def _created_ticks(self, pid: int) -> str:
        cmd = f"(Get-CimInstance Win32_Process -Filter 'ProcessId={pid}').CreationDate.ToUniversalTime().Ticks"
        return _run_shell([self.shell, "-NoProfile", "-Command", cmd], capture_output=True, text=True,
                          stdin=subprocess.DEVNULL, env=_helper_env(self.shell), timeout=60).stdout.strip()

    def _command_line(self, pid: int) -> str:
        cmd = f"(Get-CimInstance Win32_Process -Filter 'ProcessId={pid}').CommandLine"
        return _run_shell([self.shell, "-NoProfile", "-Command", cmd], capture_output=True, text=True,
                          errors="replace", stdin=subprocess.DEVNULL, env=_helper_env(self.shell),
                          timeout=60).stdout

    def _helper(self, code: str, *argv: str) -> subprocess.Popen:
        proc = subprocess.Popen([sys.executable, "-c", code, *argv], stdin=subprocess.DEVNULL,
                                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        self.helpers.append(proc)
        time.sleep(1.0)
        self.assertIsNone(proc.poll(), "вспомогательный процесс не запустился")
        return proc

    def _write_record(self, launcher_pid: int, created: str, nonce: str) -> None:
        self.pid_file.parent.mkdir(exist_ok=True)
        self.pid_file.write_text(json.dumps({
            "schema": 1, "nonce": nonce, "port": self.port, "python": sys.executable,
            "launcher_pid": launcher_pid, "launcher_created": created,
            "server_pid": launcher_pid, "server_created": created}), encoding="utf-8")

    # ---------- тесты ----------

    def test_start_status_url_stop_lifecycle(self):
        rc, start_out = self.run_ps("start")
        self.assertEqual(rc, 0, start_out)
        self.assertIn("/health ok", start_out)
        record = self._record()
        self.assertIsNotNone(record, start_out)
        launcher, server = int(record["launcher_pid"]), int(record["server_pid"])
        self.assertTrue(_alive(launcher) and _alive(server))

        rc, full = self.run_ps("url", "-ShowToken")
        self.assertEqual(rc, 0, full)
        match = re.search(r"http://127\.0\.0\.1:(\d+)/\?token=([A-Za-z0-9_\-]{22,})", full)
        self.assertIsNotNone(match, full)
        token = match.group(2)
        self.assertEqual(int(match.group(1)), self.port)
        # Токен дошёл до сервера через stdin: страница открывается с ним и не открывается без него
        self.assertEqual(_http(f"http://127.0.0.1:{self.port}/?token={token}"), 200)
        self.assertEqual(_http(f"http://127.0.0.1:{self.port}/?token=wrong"), 403)

        rc, status_out = self.run_ps("status")
        self.assertEqual(rc, 0, status_out)
        self.assertRegex(status_out, r"Running\s*:\s*True")
        self.assertRegex(status_out, r"ApiHealthy\s*:\s*True")
        rc, masked = self.run_ps("url")
        self.assertEqual(rc, 0, masked)
        rc, logs_out = self.run_ps("logs")
        self.assertEqual(rc, 0, logs_out)

        # Токен не печатается, не лежит открытым текстом и не виден в командных строках
        for text in (start_out, status_out, masked, logs_out,
                     self.pid_file.read_text(encoding="utf-8-sig"),
                     self.token_file.read_text(encoding="utf-8-sig"),
                     self.log_file.read_text(encoding="utf-8", errors="replace"),
                     self._command_line(launcher), self._command_line(server)):
            self.assertNotIn(token, text)
        self.assertIn("kontur-panel " + record["nonce"], self._command_line(launcher))
        # Шум stderr сервера (больше буфера пайпа) дошёл до лога — значит запись не блокировалась
        self.assertGreater(self.log_file.stat().st_size, 300000)

        rc, stop_out = self.run_ps("stop")
        self.assertEqual(rc, 0, stop_out)
        time.sleep(0.5)
        self.assertFalse(_alive(server))
        self.assertFalse(_alive(launcher))
        self.assertFalse(self.pid_file.exists())
        self.assertFalse(self.token_file.exists())
        rc, status_out = self.run_ps("status")
        self.assertEqual(rc, 1, status_out)
        self.assertRegex(status_out, r"Running\s*:\s*False")

    def test_stop_spares_reused_pid_other_copy_and_legacy_record(self):
        nonce = uuid.uuid4().hex
        sleeper = "import time; time.sleep(120)"
        # 1) Повторно выданный PID: время создания не совпадает
        reused = self._helper(sleeper, "kontur-panel", nonce)
        self._write_record(reused.pid, "638000000000000000", nonce)
        rc, out = self.run_ps("stop")
        self.assertEqual(rc, 0, out)
        self.assertIsNone(reused.poll(), "stop завершил процесс с чужим временем создания")
        self.assertFalse(self.pid_file.exists())
        # 2) Другая копия: время создания совпадает, метка запуска другая
        other = self._helper(sleeper, "kontur-panel", uuid.uuid4().hex)
        self._write_record(other.pid, self._created_ticks(other.pid), nonce)
        rc, out = self.run_ps("stop")
        self.assertEqual(rc, 0, out)
        self.assertIsNone(other.poll(), "stop завершил процесс с чужой меткой запуска")
        # 3) pid-файл прежней версии с голым числом
        self.pid_file.parent.mkdir(exist_ok=True)
        self.pid_file.write_text(str(other.pid), encoding="utf-8")
        rc, out = self.run_ps("stop")
        self.assertEqual(rc, 0, out)
        self.assertIsNone(other.poll(), "stop завершил процесс по pid-файлу старого формата")
        # Контроль: те же время и метка — процесс признан нашим (проверка не вырождена в «никогда»)
        own = self._helper(sleeper, "kontur-panel", nonce)
        self._write_record(own.pid, self._created_ticks(own.pid), nonce)
        rc, out = self.run_ps("stop")
        self.assertEqual(rc, 0, out)
        own.wait(10)

    def test_foreign_port_listener_is_never_adopted(self):
        listener = self._helper(
            "import socket, sys, time\n"
            "s = socket.socket(); s.bind(('127.0.0.1', int(sys.argv[1]))); s.listen(5); time.sleep(120)",
            str(self.port))
        rc, status_out = self.run_ps("status")
        self.assertEqual(rc, 1, status_out)
        self.assertRegex(status_out, r"Running\s*:\s*False")
        self.assertRegex(status_out, r"LauncherPid\s*:\s*0")
        rc, stop_out = self.run_ps("stop")
        self.assertEqual(rc, 0, stop_out)
        self.assertIsNone(listener.poll(), "stop завершил чужой слушатель порта")
        rc, start_out = self.run_ps("start")
        self.assertEqual(rc, 1, start_out)
        self.assertIsNone(listener.poll(), "start завершил чужой слушатель порта")
        self.assertFalse(self.pid_file.exists())

    def test_start_failure_is_reported_and_cleaned_up(self):
        (self.tmp / "src" / "control_panel.py").write_text(
            "import sys\n"
            "def main(argv=None):\n"
            "    sys.stderr.write('boom: panel failed\\n')\n"
            "    return 3\n", encoding="utf-8")
        rc, out = self.run_ps("start", "-StartTimeoutSec", "5")
        self.assertEqual(rc, 1, out)
        self.assertIn("boom: panel failed", out)
        self.assertFalse(self.pid_file.exists())
        self.assertFalse(self.token_file.exists())

    def test_explicit_token_is_validated_and_never_printed(self):
        rc, out = self.run_ps("start", "-Token", "short")
        self.assertEqual(rc, 1, out)
        self.assertFalse(self.pid_file.exists())
        token = "T" * 10 + uuid.uuid4().hex
        rc, out = self.run_ps("start", "-Token", token)
        self.assertEqual(rc, 0, out)
        self.assertNotIn(token, out)
        self.assertEqual(_http(f"http://127.0.0.1:{self.port}/?token={token}"), 200)
        self.assertNotIn(token, self.token_file.read_text(encoding="utf-8-sig"))
        rc, out = self.run_ps("stop")
        self.assertEqual(rc, 0, out)

    def test_server_child_that_starts_listening_mid_check_is_own(self):
        # PANEL-LAUNCHER-FLAKY: .venv\Scripts\python.exe сам порт не слушает — порождает базовый
        # интерпретатор. Если тот начинал слушать между запросом своих процессов и запросом слушателей,
        # start принимал собственный сервер за посторонний («порт слушает посторонний PID») и падал.
        # Окно гонки здесь расширено детерминированно: сервер порождается через 0.8 с, а опрос
        # слушателей порта (CIM) медленный — 1.5 с, как под нагрузкой от параллельных агентов.
        script = self.tmp / "ops" / "panel.ps1"
        text = script.read_text(encoding="utf-8-sig")
        head = "function Get-ListenerPids([int]$P) {"
        self.assertIn(head, text)
        script.write_text(text.replace(head, head + "\n    Start-Sleep -Milliseconds 1500", 1), encoding="utf-8-sig")
        (self.tmp / "src" / "control_panel_boot.py").write_text(SLOW_SPAWN_BOOT, encoding="utf-8")
        base = getattr(sys, "_base_executable", None) or sys.executable
        rc, out = self.run_ps("start", python=base)
        self.assertEqual(rc, 0, out)
        record = self._record()
        self.assertIsNotNone(record, out)
        launcher, server = int(record["launcher_pid"]), int(record["server_pid"])
        self.assertNotEqual(launcher, server, "сервер должен быть дочерним процессом лаунчера")
        self.assertIn("kontur-panel " + record["nonce"], self._command_line(server))
        rc, out = self.run_ps("stop", python=base)
        self.assertEqual(rc, 0, out)
        self.assertFalse(self.pid_file.exists())


@unittest.skipUnless(os.name == "nt", "коды аварийного завершения Windows")
class HostCrashRetryTests(unittest.TestCase):
    # Процесс, который при первом запуске падает с кодом 0xC0000005 (как powershell.exe 5.1 при ошибке CLR),
    # а при следующем возвращает обычный код; счётчик запусков — в файле
    FLAKY = ("import ctypes, sys\n"
             "from pathlib import Path\n"
             "counter = Path(sys.argv[1]); n = int(counter.read_text() or 0) + 1; counter.write_text(str(n))\n"
             "if n == 1: ctypes.windll.kernel32.ExitProcess(0xC0000005)\n"
             "print('run', n); sys.exit(int(sys.argv[2]))\n")

    def _run(self, script_rc: int) -> tuple[subprocess.CompletedProcess, int, str]:
        tmp = Path(tempfile.mkdtemp(prefix="panel-crash-"))
        self.addCleanup(shutil.rmtree, tmp, True)
        counter = tmp / "count.txt"
        counter.write_text("", encoding="utf-8")
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            proc = _run_shell([sys.executable, "-c", self.FLAKY, str(counter), str(script_rc)],
                              capture_output=True, text=True, stdin=subprocess.DEVNULL, timeout=60)
        return proc, int(counter.read_text()), err.getvalue()

    def test_host_crash_is_retried_and_reported(self):
        proc, runs, err = self._run(0)
        self.assertEqual((proc.returncode, proc.stdout.strip(), runs), (0, "run 2", 2))
        self.assertIn("0xC0000005", err)

    def test_script_failure_is_not_retried(self):
        rc = _run_shell([sys.executable, "-c", "import sys; sys.exit(1)"], capture_output=True,
                        stdin=subprocess.DEVNULL, timeout=60).returncode
        self.assertEqual(rc, 1)
        self.assertFalse(_host_crashed(1))
        self.assertTrue(_host_crashed(0xC0000005))
        self.assertTrue(_host_crashed(0x80131506))  # COR_E_EXECUTIONENGINE из события .NET Runtime 1023


@unittest.skipUnless(os.name == "nt", "DPAPI — только Windows")
class ControlPanelBootTests(unittest.TestCase):
    def _protect(self, value: str, shell: str) -> Path:
        blob = _run_shell(
            [shell, "-NoProfile", "-Command",
             f"ConvertFrom-SecureString (ConvertTo-SecureString '{value}' -AsPlainText -Force)"],
            capture_output=True, text=True, stdin=subprocess.DEVNULL, env=_helper_env(shell),
            timeout=60).stdout.strip()
        path = Path(tempfile.mkdtemp(prefix="panel-boot-")) / "token.dpapi"
        path.write_text(blob, encoding="utf-8")
        self.addCleanup(shutil.rmtree, path.parent, True)
        return path

    def test_reads_token_protected_by_both_powershell_versions(self):
        from src.control_panel_boot import read_dpapi_token
        token = "Abc_def-" + uuid.uuid4().hex
        for shell in filter(None, (_windows_powershell(), shutil.which("pwsh"))):
            with self.subTest(shell=shell):
                path = self._protect(token, shell)
                self.assertNotIn(token, path.read_text(encoding="utf-8"))
                self.assertEqual(read_dpapi_token(str(path)), token)

    def test_rejects_garbage_and_bad_arguments(self):
        from src.control_panel_boot import main, read_dpapi_token
        path = Path(tempfile.mkdtemp(prefix="panel-boot-")) / "token.dpapi"
        self.addCleanup(shutil.rmtree, path.parent, True)
        path.write_text("00ff", encoding="utf-8")
        with self.assertRaises(OSError):
            read_dpapi_token(str(path))
        with contextlib.redirect_stderr(io.StringIO()) as err:
            self.assertEqual(main(["other", "n", "1", "log", "tok"]), 2)
            self.assertEqual(main(["kontur-panel", "n", "notaport", "log", "tok"]), 2)
        self.assertIn("kontur-panel", err.getvalue())


class WindowsPowerShell51Tests(_LauncherCase, unittest.TestCase):
    shell = _windows_powershell()
    env_mode = "pwsh7"

    def test_pwsh7_module_path_is_really_polluted(self):
        # Контроль худшего случая: без правки panel.ps1 такой 5.1 не загрузит модуль безопасности
        polluted = _pwsh7_module_path()
        if not polluted:
            self.skipTest("нет pwsh 7 — PSModulePath от 7.x взяться неоткуда")
        env = dict(_without_module_path(), PSModulePath=polluted)
        out = _run_shell(
            [self.shell, "-NoProfile", "-NonInteractive", "-Command",
             "try { ConvertTo-SecureString 'x' -AsPlainText -Force | Out-Null; 'loaded' } catch { 'broken' }"],
            capture_output=True, text=True, stdin=subprocess.DEVNULL, env=env, timeout=60).stdout.strip()
        self.assertEqual(out, "broken", "сценарий PANEL-LAUNCHER-PS51-ENV больше не воспроизводится")

    def test_lifecycle_with_clean_module_path(self):
        # Обычный запуск 5.1 (Проводник, cmd, планировщик): правка PSModulePath ничего не ломает
        self.env_mode = "clean"
        rc, out = self.run_ps("start")
        self.assertEqual(rc, 0, out)
        rc, full = self.run_ps("url", "-ShowToken")
        self.assertEqual(rc, 0, full)
        match = re.search(r"/\?token=([A-Za-z0-9_\-]{22,})", full)
        self.assertIsNotNone(match, full)
        self.assertEqual(_http(f"http://127.0.0.1:{self.port}/?token={match.group(1)}"), 200)
        rc, out = self.run_ps("stop")
        self.assertEqual(rc, 0, out)


class PowerShell7Tests(_LauncherCase, unittest.TestCase):
    shell = shutil.which("pwsh")


if __name__ == "__main__":
    unittest.main()
