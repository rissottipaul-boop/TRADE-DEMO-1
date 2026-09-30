"""Тесты для ops/pump_scan.ps1 (задача PUMP-SCHED).

Проверяет:
- Синтаксис PowerShell-скрипта через парсер AST.
- Поведение при флаге AUTOSTART_OFF (пропуск скана).
- Запись строки error v=1 в data/pump_journal.jsonl при сбое.
- Реакцию на отсутствие python.
"""
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
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
class PumpScanPs1Test(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        (self.root / "ops").mkdir()
        (self.root / "data").mkdir()
        (self.root / "logs").mkdir()
        shutil.copy(ROOT / "ops" / "pump_scan.ps1", self.root / "ops" / "pump_scan.ps1")

    def run_ps1(self, action: str):
        env = {k: v for k, v in os.environ.items() if k != "PYTHONPATH"}
        proc = subprocess.run(
            [PWSH, "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command",
             "[Console]::OutputEncoding = [Text.UTF8Encoding]::new(); "
             f"& '{self.root / 'ops' / 'pump_scan.ps1'}' {action}; exit $LASTEXITCODE"],
            cwd=self.root, capture_output=True, timeout=60, env=env)
        out = proc.stdout.decode("utf-8", "replace") + proc.stderr.decode("utf-8", "replace")
        return proc.returncode, out

    def test_syntax_ast(self):
        cmd = [
            PWSH, "-NoProfile", "-Command",
            f"$errors = $null; $tokens = $null; "
            f"[System.Management.Automation.Language.Parser]::ParseFile('{self.root / 'ops' / 'pump_scan.ps1'}', [ref]$tokens, [ref]$errors); "
            f"if ($errors.Count -gt 0) {{ $errors | ForEach-Object {{ Write-Error $_ }}; exit 1 }}"
        ]
        proc = subprocess.run(cmd, capture_output=True)
        self.assertEqual(proc.returncode, 0, proc.stderr.decode("utf-8", "replace"))

    def test_run_skips_when_autostart_off(self):
        (self.root / "data" / "AUTOSTART_OFF").write_text("pause\n", encoding="utf-8")
        code, out = self.run_ps1("run")
        self.assertEqual(code, 0)
        log = (self.root / "logs" / "pump_scan.log").read_text(encoding="utf-8")
        self.assertIn("пропуск: есть data\\AUTOSTART_OFF", log)

    def test_run_logs_error_when_no_python(self):
        # Временный корень без .venv -> ошибка отсутствия python
        code, out = self.run_ps1("run")
        self.assertEqual(code, 2)
        log = (self.root / "logs" / "pump_scan.log").read_text(encoding="utf-8")
        self.assertIn("нет", log)
        journal = self.root / "data" / "pump_journal.jsonl"
        self.assertTrue(journal.exists())
        lines = [json.loads(line) for line in journal.read_text(encoding="utf-8").strip().splitlines() if line.strip()]
        self.assertEqual(len(lines), 1)
        err = lines[0]
        self.assertEqual(err["event"], "error")
        self.assertEqual(err["v"], 1)
        self.assertEqual(err["kind"], "scan")
        self.assertEqual(err["code"], 2)


if __name__ == "__main__":
    unittest.main()

