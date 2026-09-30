"""Сторож движка (src/engine_watchdog.py), faulthandler движка и защита PID в ops/engine.ps1.

Задачи ENGINE-WATCHDOG и ENGINE-PID-GUARD. Решение «жив / тишина / мёртв» и штатные
состояния — на фактах без процессов; сбор фактов и --act — во временном корне с фейковыми
часами, пробой процесса и запуском engine.ps1. Проверки engine.ps1 (чужой PID, ротация
engine.log.out) — копия скрипта во временном корне через pwsh 7, живой движок не трогается.
"""
import asyncio
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from datetime import datetime
from pathlib import Path
from unittest import mock

from src import engine as engine_mod
from src import engine_watchdog as wd
from tests.test_connector_kill_dca import FakeBotExchange
from tests.test_engine import _EngineCase

ROOT = Path(__file__).resolve().parent.parent
NOW = 1_790_720_000.0


def facts(**kw) -> wd.Facts:
    base = dict(now=NOW, pid_file=True, pid=30584, owner="свой", log_mtime=NOW - 30,
                stop_marker=False, stop_flag=False, off_flag=False, kill_flag=False,
                restarts_last_hour=0)
    base.update(kw)
    return wd.Facts(**base)


class DecideTest(unittest.TestCase):
    """Решение по фактам: жив / тишина / мёртв и когда сторож движок не трогает."""

    def test_alive(self):
        v = wd.decide(facts())
        self.assertEqual((v.state, v.restart), ("жив", False))
        self.assertIn("PID 30584", v.reason)

    def test_silence_when_process_alive_but_log_stale(self):
        v = wd.decide(facts(log_mtime=NOW - wd.SILENCE_S - 1))
        self.assertEqual((v.state, v.restart), ("тишина", True))
        self.assertIn("завис", v.reason)

    def test_threshold_is_inclusive(self):
        self.assertEqual(wd.decide(facts(log_mtime=NOW - wd.SILENCE_S)).state, "жив")
        self.assertEqual(wd.decide(facts(log_mtime=NOW - wd.SILENCE_S - 1)).state, "тишина")

    def test_dead_when_process_gone_and_log_stale(self):
        v = wd.decide(facts(owner="нет", log_mtime=NOW - 600))
        self.assertEqual((v.state, v.restart), ("мёртв", True))

    def test_missing_log_counts_as_silence(self):
        self.assertEqual(wd.decide(facts(log_mtime=None)).state, "тишина")
        self.assertEqual(wd.decide(facts(owner="нет", log_mtime=None)).state, "мёртв")

    def test_foreign_pid_is_not_our_engine(self):
        v = wd.decide(facts(owner="чужой", log_mtime=NOW - 600))
        self.assertEqual((v.state, v.restart), ("мёртв", True))
        self.assertIn("чужой", v.reason)

    def test_dead_pid_but_fresh_log_no_second_engine(self):
        # лаунчер убит, интерпретатор жив и пишет лог — второй движок нельзя
        for owner in ("нет", "чужой"):
            v = wd.decide(facts(owner=owner, log_mtime=NOW - 10))
            self.assertEqual((v.state, v.restart), ("жив", False), owner)
            self.assertIn("второй движок не запускаю", v.reason)

    def test_log_mtime_in_future_is_fresh(self):
        self.assertEqual(wd.decide(facts(log_mtime=NOW + 100)).state, "жив")

    def test_autostart_off_wins(self):
        v = wd.decide(facts(off_flag=True, owner="нет", log_mtime=None))
        self.assertEqual((v.state, v.restart), ("пауза", False))

    def test_stop_flag_means_stopping(self):
        v = wd.decide(facts(stop_flag=True, owner="нет", log_mtime=None))
        self.assertEqual((v.state, v.restart), ("останавливается", False))

    def test_no_pid_file_after_normal_stop(self):
        v = wd.decide(facts(pid_file=False, pid=None, owner="нет", log_mtime=NOW - 9999))
        self.assertEqual((v.state, v.restart), ("остановлен", False))

    def test_stop_marker_in_log_means_normal_exit(self):
        v = wd.decide(facts(stop_marker=True, owner="нет", log_mtime=NOW - 9999))
        self.assertEqual((v.state, v.restart), ("остановлен", False))

    def test_kill_flag_does_not_block_restart(self):
        v = wd.decide(facts(kill_flag=True, owner="нет", log_mtime=NOW - 600))
        self.assertTrue(v.restart)
        self.assertIn("data/KILL", v.reason)

    def test_restart_limit_per_hour(self):
        v = wd.decide(facts(owner="нет", log_mtime=NOW - 600, restarts_last_hour=3))
        self.assertEqual((v.state, v.restart), ("лимит", False))
        self.assertTrue(wd.decide(facts(owner="нет", log_mtime=NOW - 600, restarts_last_hour=2)).restart)

    def test_unreadable_pid_is_dead(self):
        v = wd.decide(facts(pid=None, owner="нет", log_mtime=NOW - 600))
        self.assertEqual(v.state, "мёртв")
        self.assertIn("PID не читается", v.reason)


class ClassifyProcessTest(unittest.TestCase):
    """ENGINE-PID-GUARD: свой / чужой / нет процесса — по пути образа лаунчера venv."""

    expected = Path(r"C:\TG\BOT\TRADE DEMO 1\.venv\Scripts\python.exe")

    def test_own(self):
        self.assertEqual(wd.classify_process(str(self.expected), self.expected), "свой")

    @unittest.skipUnless(sys.platform == "win32", "регистр пути важен только на Windows")
    def test_own_case_insensitive(self):
        self.assertEqual(wd.classify_process(str(self.expected).lower(), self.expected), "свой")

    def test_copy_of_project_is_foreign(self):
        other = r"D:\TRADE DEMO 22\.venv\Scripts\python.exe"
        self.assertEqual(wd.classify_process(other, self.expected), "чужой")

    def test_unknown_image_is_foreign(self):
        self.assertEqual(wd.classify_process("", self.expected), "чужой")

    def test_no_process(self):
        self.assertEqual(wd.classify_process(None, self.expected), "нет")

    def test_real_process_image(self):
        self.assertTrue(Path(wd.process_image(os.getpid())).name.lower().startswith("python"))
        self.assertIsNone(wd.process_image(0))
        dead = subprocess.Popen([sys.executable, "-c", "pass"])
        dead.wait()
        self.assertIsNone(wd.process_image(dead.pid))


class _RootCase(unittest.TestCase):
    """Временный корень проекта со структурой data/, logs/."""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        (self.root / "data").mkdir()
        (self.root / "logs").mkdir()
        self.paths = wd.Paths.under(self.root)
        self.own_image = str(self.paths.python)

    def write_pid(self, pid, raw=None):
        self.paths.pid.write_bytes(raw if raw is not None else f"{pid}\r\n".encode())

    def write_log(self, age, text="2026-09-30 02:40:58,361 INFO okx.engine: Reconcile BTC-USDT\n"):
        self.paths.log.write_text(text, encoding="utf-8")
        os.utime(self.paths.log, (NOW - age, NOW - age))

    def autostart_lines(self):
        if not self.paths.autostart_log.exists():
            return []
        return self.paths.autostart_log.read_text(encoding="utf-8").splitlines()


class ReadPidTest(_RootCase):
    def test_variants(self):
        self.assertEqual(wd.read_pid(self.paths.pid), (False, None))
        self.write_pid(0, b"\xef\xbb\xbf30584\r\n")  # Set-Content с BOM
        self.assertEqual(wd.read_pid(self.paths.pid), (True, 30584))
        self.write_pid(0, b"garbage")
        self.assertEqual(wd.read_pid(self.paths.pid), (True, None))


class GatherCheckTest(_RootCase):
    """Сбор фактов, повторная проверка, перезапуск и лог logs/autostart.log."""

    def setUp(self):
        super().setUp()
        self.calls = []
        self.sleeps = []

    def runner(self, engine_ps1, action):
        self.calls.append(action)
        return f"{action} ok (код 0)"

    def check(self, probe, **kw):
        return wd.check(self.paths, act=True, clock=lambda: NOW, sleep=self.sleeps.append,
                        probe=probe, runner=self.runner, **kw)

    def test_alive_does_nothing_and_logs_once(self):
        self.write_pid(30584)
        self.write_log(age=20)
        probe = lambda pid: self.own_image  # noqa: E731
        for _ in range(3):
            v = self.check(probe)
            self.assertEqual(v.state, "жив")
        self.assertEqual(self.calls, [])
        self.assertEqual(self.sleeps, [])
        self.assertEqual(len(self.autostart_lines()), 1)  # «жив» — один раз, не каждые 5 мин
        self.assertIn(" watch жив: PID 30584", self.autostart_lines()[0])

    def test_dry_run_writes_nothing(self):
        self.write_pid(30584)
        self.write_log(age=600)
        v = wd.check(self.paths, clock=lambda: NOW, probe=lambda pid: None,
                     runner=self.runner, sleep=self.sleeps.append)
        self.assertEqual((v.state, v.restart), ("мёртв", True))
        self.assertEqual((self.calls, self.sleeps, self.autostart_lines()), ([], [], []))

    def test_dead_confirmed_then_restarted(self):
        self.write_pid(30584)
        self.write_log(age=600)
        v = self.check(lambda pid: None)
        self.assertEqual((v.state, v.restart), ("мёртв", True))
        self.assertEqual(self.sleeps, [wd.CONFIRM_S])
        self.assertEqual(self.calls, ["stop", "start"])
        lines = self.autostart_lines()
        self.assertEqual(len(lines), 2)
        self.assertIn(" watch мёртв: PID 30584 не найден", lines[0])
        self.assertIn(" watch перезапуск: engine.ps1 stop: stop ok (код 0) | engine.ps1 start: start ok", lines[1])

    def test_hung_process_restarted(self):
        self.write_pid(30584)
        self.write_log(age=400)
        v = self.check(lambda pid: self.own_image)
        self.assertEqual(v.state, "тишина")
        self.assertEqual(self.calls, ["stop", "start"])

    def test_recovered_on_confirm_no_restart(self):
        self.write_pid(30584)
        self.write_log(age=400)

        def sleep(seconds):  # за время паузы движок написал строку (например, после сна машины)
            self.sleeps.append(seconds)
            self.write_log(age=1)

        v = wd.check(self.paths, act=True, clock=lambda: NOW, sleep=sleep,
                     probe=lambda pid: self.own_image, runner=self.runner)
        self.assertEqual((v.state, v.restart), ("жив", False))
        self.assertEqual(self.calls, [])

    def test_stop_failure_still_tries_start(self):
        self.write_pid(30584)
        self.write_log(age=600)

        def runner(engine_ps1, action):
            self.calls.append(action)
            if action == "stop":
                raise subprocess.TimeoutExpired("pwsh", 180)
            return "start ok"

        wd.check(self.paths, act=True, clock=lambda: NOW, sleep=self.sleeps.append,
                 probe=lambda pid: None, runner=runner)
        self.assertEqual(self.calls, ["stop", "start"])
        self.assertIn("engine.ps1 stop: ошибка TimeoutExpired", self.autostart_lines()[-1])

    def test_foreign_pid_restarts_via_engine_ps1_only(self):
        self.write_pid(36888)
        self.write_log(age=600)
        seen = []
        probe = lambda pid: seen.append(pid) or r"D:\TRADE DEMO 22\.venv\Scripts\python.exe"  # noqa: E731
        v = self.check(probe)
        self.assertEqual(v.state, "мёртв")
        self.assertIn("чужой процесс", v.reason)
        self.assertEqual(seen, [36888, 36888])
        self.assertEqual(self.calls, ["stop", "start"])  # чужой PID разбирает engine.ps1 сам

    def test_normal_stop_states(self):
        self.write_log(age=9999)
        self.assertEqual(self.check(lambda pid: None).state, "остановлен")  # нет PID
        self.write_pid(30584)
        self.write_log(age=9999, text="... INFO okx.engine: STOP_ENGINE: штатная остановка по флагу "
                                      "data\\STOP_ENGINE: ops/engine.ps1 stop\n... Engine stopped.\n")
        self.assertEqual(self.check(lambda pid: None).state, "остановлен")
        self.write_log(age=9999)
        self.paths.stop_flag.write_text("x")
        self.assertEqual(self.check(lambda pid: None).state, "останавливается")
        self.paths.off_flag.write_text("")
        self.assertEqual(self.check(lambda pid: None).state, "пауза")
        self.assertEqual(self.calls, [])

    def test_restart_limit_from_log(self):
        self.write_pid(30584)
        self.write_log(age=600)
        stamp = datetime.fromtimestamp(NOW - 600).astimezone().isoformat(timespec="seconds")
        old = datetime.fromtimestamp(NOW - 7200).astimezone().isoformat(timespec="seconds")
        self.paths.autostart_log.write_text(
            f"{old} watch перезапуск: давно\n" + f"{stamp} watch перезапуск: x\n" * 3, encoding="utf-8")
        v = self.check(lambda pid: None)
        self.assertEqual((v.state, v.restart), ("лимит", False))
        self.assertEqual(self.calls, [])
        self.assertIn(" watch лимит: мёртв:", self.autostart_lines()[-1])

    def test_main_json_and_codes(self):
        self.write_pid(30584)
        self.write_log(age=1)
        os.utime(self.paths.log, (time.time() - 5, time.time() - 5))  # свежий по настоящим часам
        with mock.patch.object(wd, "process_image", return_value=self.own_image), \
                mock.patch("builtins.print") as printed:
            code = wd.main(["--json"], root=self.root)
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(printed.call_args[0][0])["state"], "жив")
        self.paths.log.unlink()
        with mock.patch.object(wd, "process_image", return_value=None), mock.patch("builtins.print"):
            self.assertEqual(wd.main([], root=self.root), 1)


class HangDumpTest(_EngineCase):
    """ENGINE-WATCHDOG: faulthandler — таймер дампа стеков перевзводится кругом сверки."""

    def test_off_by_default(self):
        eng = self.make_engine(FakeBotExchange())
        self.assertIsNone(eng.hang_dump_s)
        with mock.patch.object(engine_mod.faulthandler, "dump_traceback_later") as arm:
            eng._arm_hang_dump()
        arm.assert_not_called()

    def test_rearmed_each_reconcile_and_cancelled_on_stop(self):
        eng = self.make_engine(FakeBotExchange())
        eng.hang_dump_s = engine_mod.HANG_DUMP_S
        eng._reconcile_interval = 0.001
        done = []

        async def fake_reconcile():
            done.append(1)
            if len(done) >= 3:
                eng._running = False

        eng._reconcile = fake_reconcile
        eng._check_pause = lambda gap: None  # паузы — не предмет теста
        eng._running = True
        with mock.patch.object(engine_mod.faulthandler, "dump_traceback_later") as arm, \
                mock.patch.object(engine_mod.faulthandler, "cancel_dump_traceback_later") as cancel:
            asyncio.run(asyncio.wait_for(eng._reconcile_loop(), timeout=10))
            asyncio.run(eng.stop())
        self.assertEqual(arm.call_count, 3)
        arm.assert_called_with(engine_mod.HANG_DUMP_S, repeat=False)  # без repeat: дампы не освежают лог
        cancel.assert_called_once()

    def test_dump_before_watchdog_threshold(self):
        # дамп стеков должен попасть в лог раньше, чем сторож сочтёт лог молчащим
        self.assertLess(engine_mod.HANG_DUMP_S, wd.SILENCE_S)
        self.assertGreater(engine_mod.HANG_DUMP_S, 2 * 72)  # 72 с — максимум круга в логах


def _pwsh():
    """Путь к рабочему pwsh 7 либо None.

    shutil.which может найти 0-байтный Store-стаб WindowsApps/pwsh.exe
    без установленного pwsh 7 — запуск такого стаба падает WinError 1920.
    """
    exe = shutil.which("pwsh")
    if not exe:
        return None
    try:
        probe = subprocess.run([exe, "-NoProfile", "-Command", "1"],
                               capture_output=True, timeout=30)
    except OSError:
        return None
    return exe if probe.returncode == 0 else None


PWSH = _pwsh()


@unittest.skipUnless(sys.platform == "win32" and PWSH, "нужны Windows и pwsh 7")
class EnginePs1Test(unittest.TestCase):
    """ops/engine.ps1 в копии корня: чужой PID не трогается (ENGINE-PID-GUARD), старт хранит .out."""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        (self.root / "ops").mkdir()
        (self.root / "data").mkdir()
        (self.root / "logs").mkdir()
        shutil.copy(ROOT / "ops" / "engine.ps1", self.root / "ops" / "engine.ps1")

    def run_ps1(self, action):
        env = {k: v for k, v in os.environ.items() if k != "PYTHONPATH"}
        proc = subprocess.run(
            [PWSH, "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command",
             "[Console]::OutputEncoding = [Text.UTF8Encoding]::new(); "
             f"& '{self.root / 'ops' / 'engine.ps1'}' {action}; exit $LASTEXITCODE"],
            cwd=self.root, capture_output=True, timeout=120, env=env)
        return proc.returncode, proc.stdout.decode("utf-8", "replace") + proc.stderr.decode("utf-8", "replace")

    def test_stop_refuses_foreign_engine_pid(self):
        # «движок» другой копии: python из настоящего .venv с src.engine в командной строке
        foreign = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(120)", "src.engine"])
        self.addCleanup(foreign.wait, 10)
        self.addCleanup(foreign.kill)
        (self.root / "data" / "engine.pid").write_text(str(foreign.pid))
        started = time.monotonic()
        code, out = self.run_ps1("stop")
        self.assertEqual(code, 0, out)
        self.assertIn("ВНИМАНИЕ: PID", out)
        self.assertIn("не движок этого каталога", out)
        self.assertIn("Движок не запущен", out)
        self.assertIsNone(foreign.poll(), "чужой процесс не должен быть остановлен")
        self.assertFalse((self.root / "data" / "engine.pid").exists(), "устаревший pid-файл удаляется")
        self.assertFalse((self.root / "data" / "STOP_ENGINE").exists(), "флаг остановки не пишется")
        self.assertLess(time.monotonic() - started, 25, "не ждёт 30 с остановки чужого процесса")

    def test_garbage_pid_file_removed(self):
        (self.root / "data" / "engine.pid").write_text("not-a-pid")
        code, out = self.run_ps1("stop")
        self.assertEqual(code, 0, out)
        self.assertIn("не PID", out)
        self.assertFalse((self.root / "data" / "engine.pid").exists())

    def test_start_keeps_previous_log_and_out(self):
        # лаунчер venv в копии корня: src.engine там нет, процесс сразу выходит — нам нужна ротация
        scripts = self.root / ".venv" / "Scripts"
        scripts.mkdir(parents=True)
        shutil.copy(ROOT / ".venv" / "Scripts" / "python.exe", scripts / "python.exe")
        shutil.copy(ROOT / ".venv" / "pyvenv.cfg", self.root / ".venv" / "pyvenv.cfg")
        logs = self.root / "logs"
        (logs / "engine.log").write_text("old log\n", encoding="utf-8")
        (logs / "engine.log.out").write_text("old out\n", encoding="utf-8")
        code, out = self.run_ps1("start")
        self.assertEqual(code, 1, out)  # «завершился сразу после старта»
        archived_out = [p for p in logs.glob("engine_*.log.out")]
        archived_log = [p for p in logs.glob("engine_*.log")]
        self.assertEqual(len(archived_out), 1, out)
        self.assertEqual(len(archived_log), 1, out)
        self.assertEqual(archived_out[0].read_text(encoding="utf-8"), "old out\n")
        self.assertEqual(archived_log[0].read_text(encoding="utf-8"), "old log\n")
        self.assertEqual(archived_out[0].name, archived_log[0].name + ".out")  # одна метка времени


if __name__ == "__main__":
    unittest.main()
