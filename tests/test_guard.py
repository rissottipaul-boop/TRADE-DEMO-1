"""Guard-хук агентов: опасное блокируется, обычная работа — нет."""
import importlib.util
import json
import os
import subprocess
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
GUARD_PATH = ROOT / "ops" / "hooks" / "guard.py"
spec = importlib.util.spec_from_file_location("guard", GUARD_PATH)
guard = importlib.util.module_from_spec(spec)
spec.loader.exec_module(guard)

OFF = {"live_enabled": False}
PROFILES = ("okx-demo", {"okx-demo", "okx-demo-v2"})


def term(cmd, policy=OFF):
    return guard.decide("run_in_terminal", {"command": cmd}, policy=policy, okx_profiles=PROFILES)


def edit(tool, **tool_input):
    return guard.decide(tool, tool_input, policy=OFF, okx_profiles=PROFILES)


class AllowedTest(unittest.TestCase):
    """Обычная работа агентов проходит без помех."""

    def test_commands(self):
        for cmd in [
            "python -m src.ops status",
            'python -m src.ops kill "flash crash"',
            "python -m unittest discover -s tests -t .",
            "okx --demo spot place --instId BTC-USDT --side buy --ordType limit --sz 0.0001 --px 40000",
            "okx spot orders",  # профиль по умолчанию — demo
            "okx market ticker BTC-USDT",
            "okx --demo bot grid withdraw-income --algoId 1",
            "okx --demo bot grid stop --algoOrdType grid --algoId 1 --stopType 2",
            "cat pump-pocket.json 2>&1",
            "Stop-Process -Id 1234",
            "python -m src.engine > logs/engine.log 2>&1",
            "copy .env.example backup.txt",
            "New-Item -ItemType File data/KILL",
        ]:
            with self.subTest(cmd=cmd):
                self.assertIsNone(term(cmd))

    def test_file_tools(self):
        self.assertIsNone(edit("read_file", filePath=".env.example"))
        self.assertIsNone(edit("create_file", filePath="src/strategy.py", content="x = 1\n"))
        self.assertIsNone(edit("read_file", filePath="ops/live-policy.json"))  # читать можно
        # ужесточение лимита разрешено
        self.assertIsNone(edit("replace_string_in_file", filePath="src/risk.py",
                               oldString="MAX_POSITION_PCT = 15.0", newString="MAX_POSITION_PCT = 10.0"))
        self.assertIsNone(edit("Edit", file_path=str(ROOT / "src" / "risk.py"),
                               old_string="x", new_string="MAX_ENTRIES_PER_DAY = 10  # без изменений"))


class DeniedTest(unittest.TestCase):
    def test_commands(self):
        for cmd in [
            "python -m src.ops reset kill",
            "python -c \"from src import risk; risk.reset_breaker('global')\"",
            "cat .env",
            r"Get-Content .\.env",
            "python -c \"from dotenv import dotenv_values; print(dotenv_values())\"",
            "echo $OKX_DEMO_SECRET",
            "type %USERPROFILE%\\.okx\\config.toml",
            "okx --live spot place --instId BTC-USDT --side buy --sz 1",
            "okx --profile main-live account balance",
            "$env:OKX_MODE='live'; python -m src.engine",
            "okx asset withdraw --ccy USDT --amt 100",
            "okx config init",
            "okx --demo auth login",
            "taskkill /IM python.exe /F",
            "Get-Process python | Stop-Process",
            "rm data/bot_state.db",
            "Remove-Item state.db",
            "Remove-Item -Recurse -Force data",
            "rm -rf src",
            "rm data/KILL",
            "echo {} > ops/live-policy.json",
            "Set-Content pump-pocket.json '{}'",
            "sed -i s/15.0/30.0/ src/risk.py",
            "sqlite3 data/risk_state.db \"delete from risk_kv\"",
        ]:
            with self.subTest(cmd=cmd):
                self.assertIsNotNone(term(cmd))

    def test_env_via_file_tools(self):
        for tool, key in [("read_file", "filePath"), ("Read", "file_path"), ("Grep", "path")]:
            with self.subTest(tool=tool):
                self.assertIsNotNone(edit(tool, **{key: str(ROOT / ".env")}))
        self.assertIsNotNone(edit("grep_search", query="SECRET", includePattern=".env"))

    def test_guardrail_files(self):
        self.assertIsNotNone(edit("create_file", filePath="ops/live-policy.json", content="{}"))
        self.assertIsNotNone(edit("replace_string_in_file", filePath=".github/hooks/guard.json",
                                  oldString="a", newString="b"))
        self.assertIsNotNone(edit("Write", file_path=str(ROOT / "ops" / "hooks" / "guard.py"), content=""))
        self.assertIsNotNone(edit("Edit", file_path=str(ROOT / ".claude" / "settings.json"),
                                  old_string="a", new_string="b"))
        self.assertIsNotNone(edit("apply_patch", input="*** Begin Patch\n*** Delete File: pump-pocket.json\n"))

    def test_risk_limits_cannot_be_loosened(self):
        self.assertIsNotNone(edit("replace_string_in_file", filePath="src/risk.py",
                                  oldString="MAX_POSITION_PCT = 15.0", newString="MAX_POSITION_PCT = 30.0"))
        self.assertIsNotNone(edit("replace_string_in_file", filePath="src/risk.py",
                                  oldString="INST_BLOCK_HOURS = 24", newString="INST_BLOCK_HOURS = 1"))
        self.assertIsNotNone(edit("create_file", filePath="src/risk.py",
                                  content='MAX_RISK_PCT = float(os.getenv("RISK", "5"))\n'))
        self.assertIsNotNone(edit("apply_patch", input=(
            "*** Begin Patch\n*** Update File: src/risk.py\n@@\n-DAILY_LOSS_LIMIT_PCT = 6.0\n"
            "+DAILY_LOSS_LIMIT_PCT = 20.0\n*** End Patch")))

    def test_equity_max_age_cannot_be_loosened(self):
        # GUARD-EQUITY-AGE: порог «equity не старше 10 минут» ослабляется ростом
        self.assertIsNotNone(edit("replace_string_in_file", filePath="src/risk.py",
                                  oldString="EQUITY_MAX_AGE_S = 600", newString="EQUITY_MAX_AGE_S = 3600"))
        self.assertIsNotNone(edit("Edit", file_path=str(ROOT / "src" / "risk.py"), old_string="x",
                                  new_string='EQUITY_MAX_AGE_S = float("inf")'))
        self.assertIsNotNone(edit("create_file", filePath="src/engine.py",
                                  content="from . import risk\nrisk.EQUITY_MAX_AGE_S = 10**9\n"))
        # ужесточение и прежнее значение проходят
        self.assertIsNone(edit("replace_string_in_file", filePath="src/risk.py",
                               oldString="EQUITY_MAX_AGE_S = 600", newString="EQUITY_MAX_AGE_S = 300"))
        self.assertIsNone(edit("Edit", file_path=str(ROOT / "src" / "risk.py"), old_string="x",
                               new_string="EQUITY_MAX_AGE_S = 600  # без изменений"))

    def test_max_leverage_cannot_be_loosened(self):
        # RISK-LEVERAGE-CAP: потолок плеча 3x
        self.assertIsNotNone(edit("replace_string_in_file", filePath="src/risk.py",
                                  oldString="MAX_LEVERAGE = 3", newString="MAX_LEVERAGE = 10"))
        self.assertIsNotNone(edit("create_file", filePath="src/strategy.py",
                                  content="from . import risk\nrisk.MAX_LEVERAGE = 20\n"))
        self.assertIsNone(edit("replace_string_in_file", filePath="src/risk.py",
                               oldString="MAX_LEVERAGE = 3", newString="MAX_LEVERAGE = 2"))

    def test_limit_monkeypatch_from_other_module(self):
        self.assertIsNotNone(edit("create_file", filePath="src/dca_bot.py",
                                  content="from . import risk\nrisk.MAX_POSITION_PCT = 50\n"))
        # сравнение (==) — не присваивание
        self.assertIsNone(edit("create_file", filePath="src/x.py",
                               content="assert risk.MAX_POSITION_PCT == 15.0\n"))


class LivePolicyTest(unittest.TestCase):
    def test_live_window(self):
        future = (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat()
        past = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()
        cmd = "okx --live spot place --instId BTC-USDT --side buy --sz 0.0001 --px 40000"
        self.assertIsNone(term(cmd, {"live_enabled": True, "enabled_until": future}))
        self.assertIsNotNone(term(cmd, {"live_enabled": True, "enabled_until": past}))
        self.assertIsNotNone(term(cmd, {"live_enabled": "yes"}))  # только буквальное true

    def test_withdraw_denied_even_with_live(self):
        self.assertIsNotNone(term("okx --live asset withdraw --amt 1", {"live_enabled": True}))

    def test_default_live_profile_counts_as_live(self):
        self.assertIsNotNone(guard.decide("run_in_terminal", {"command": "okx spot place --sz 1"},
                                          policy=OFF, okx_profiles=("main", {"okx-demo"})))


class HookContractTest(unittest.TestCase):
    """Контракт stdin/stdout, который читают VS Code и Claude Code."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.env = {**os.environ, "AGENT_GUARD_LOG": str(Path(self._tmp.name) / "guard.log")}

    def tearDown(self):
        self._tmp.cleanup()

    def run_hook(self, payload):
        proc = subprocess.run([sys.executable, str(GUARD_PATH)], input=json.dumps(payload).encode(),
                              capture_output=True, timeout=30, env=self.env)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        return proc.stdout.decode("utf-8").strip()

    def test_deny_output(self):
        out = json.loads(self.run_hook({"hook_event_name": "PreToolUse", "tool_name": "run_in_terminal",
                                        "tool_input": {"command": "python -m src.ops reset global"}}))
        self.assertEqual(out["hookSpecificOutput"]["permissionDecision"], "deny")
        self.assertIn("[guard]", out["hookSpecificOutput"]["permissionDecisionReason"])
        logged = json.loads(Path(self.env["AGENT_GUARD_LOG"]).read_text(encoding="utf-8"))
        self.assertEqual(logged["decision"], "deny")

    def test_allow_is_silent(self):
        self.assertEqual(self.run_hook({"tool_name": "Bash", "tool_input": {"command": "python -m src.ops status"}}), "")

    def test_garbage_input_fails_open(self):
        proc = subprocess.run([sys.executable, str(GUARD_PATH)], input=b"not json", capture_output=True,
                              timeout=30, env=self.env)
        self.assertEqual((proc.returncode, proc.stdout), (0, b""))

    def test_bom_input_is_still_checked(self):
        # PowerShell 5.1 шлёт вход хука с BOM; guard не должен уходить в fail-open
        payload = {"tool_name": "run_in_terminal", "tool_input": {"command": "python -m src.ops reset global"}}
        proc = subprocess.run([sys.executable, str(GUARD_PATH)],
                              input=b"\xef\xbb\xbf" + json.dumps(payload).encode(),
                              capture_output=True, timeout=30, env=self.env)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        out = json.loads(proc.stdout.decode("utf-8"))
        self.assertEqual(out["hookSpecificOutput"]["permissionDecision"], "deny")


if __name__ == "__main__":
    unittest.main()
