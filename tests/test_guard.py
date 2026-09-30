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
if str(ROOT).startswith("\\\\?\\"):
    # Артефакт песочницы: префикс \\?\ уродует входы теста (file://, Git Bash)
    # и корень guard — в проде hook стартует с обычным путём. Убираем здесь,
    # сам guard (ops/hooks/) не трогаем.
    ROOT = Path(str(ROOT)[4:])
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
            "okx --profile main-live spot place --instId BTC-USDT --side buy --sz 1",
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
        runner = "python -m src.live_runner"
        self.assertIsNone(term(runner, {"live_enabled": True, "enabled_until": future}))
        self.assertIsNotNone(term(runner, {"live_enabled": True, "enabled_until": past}))
        self.assertIsNotNone(term(runner, {"live_enabled": "yes"}))  # только буквальное true
        self.assertIsNotNone(term(r"ops\live.ps1 start", {"live_enabled": True, "enabled_until": past}))
        # вход из CLI в live запрещён и в открытом окне: входы — только через src.live_runner
        cmd = "okx --live spot place --instId BTC-USDT --side buy --sz 0.0001 --px 40000"
        self.assertIsNotNone(term(cmd, {"live_enabled": True, "enabled_until": future}))

    def test_withdraw_denied_even_with_live(self):
        self.assertIsNotNone(term("okx --live asset withdraw --amt 1", {"live_enabled": True}))

    def test_default_live_profile_counts_as_live(self):
        self.assertIsNotNone(guard.decide("run_in_terminal", {"command": "okx spot place --sz 1"},
                                          policy=OFF, okx_profiles=("main", {"okx-demo"})))


class LiveEmergencyTest(unittest.TestCase):
    """GUARD-LIVE-PATCH: аварийные live-команды — всегда, OKX_MODE=live — только src.live_*."""

    def test_emergency_allowed_outside_window(self):
        for cmd in [
            "$env:OKX_MODE='live'; python -m src.ops kill \"flash crash\"",
            "$env:OKX_MODE = \"live\"; .venv\\Scripts\\python.exe -m src.ops status",
            "OKX_MODE=live python -m src.ops kill manual",
            "python -m src.ops kill --mode live \"x\"",
            "$env:OKX_MODE='live'; python -m src.live_preflight",
            r"$env:OKX_MODE='live'; ops\live.ps1 stop",
            "okx --live spot cancel --instId BTC-USDT --ordId 1",
            "okx --live spot algo cancel --instId BTC-USDT --algoId 1",
            "okx --live bot grid stop --algoOrdType grid --algoId 1 --stopType 1",
            "okx --live bot dca stop --algoId 1",
            "okx --live swap close-position --instId BTC-USDT-SWAP --mgnMode cross",
            "okx --live account balance",
            "okx --profile main-live spot orders",
        ]:
            with self.subTest(cmd=cmd):
                self.assertIsNone(term(cmd))

    def test_okx_mode_live_only_for_live_entry_points(self):
        future = (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat()
        window = {"live_enabled": True, "enabled_until": future}
        for cmd in [
            "$env:OKX_MODE='live'; python -m src.engine",
            "$env:OKX_MODE='live'; python -m src.dca_bot",
            "OKX_MODE=live python -m src.smoke_test",
            "$env:OKX_MODE='live'; python -c \"import src.connector\"",
            "cd src; $env:OKX_MODE='live'; python -m src.ops kill x",  # префикс не в начале
        ]:
            with self.subTest(cmd=cmd):
                self.assertIsNotNone(term(cmd, window))
        self.assertIsNone(term("$env:OKX_MODE='live'; python -m src.live_runner", window))
        self.assertIsNotNone(term("$env:OKX_MODE='live'; python -m src.live_runner"))  # окно закрыто

    def test_emergency_prefix_cannot_carry_payload(self):
        for cmd in [
            "$env:OKX_MODE='live'; python -m src.ops kill x; python -m src.engine",
            "$env:OKX_MODE='live'; python -m src.ops status && python -m src.dca_bot",
            "okx --live spot cancel --ordId 1; okx --live spot place --side buy --sz 1",
            "okx --live spot orders | okx --live spot place --sz 1",
            "okx --live spot amend --ordId 1 --newSz 5",
            "okx --live bot grid create --algoOrdType grid",
            "okx --live account set-leverage --lever 10",
            "okx --live asset transfer --amt 1",
            "okx --live spot place --instId BTC-USDT --side buy --sz 1  # cancel",
            "echo $env:OKX_API_KEY",  # исключение для OKX_MODE не открывает секреты
            "Get-ChildItem env:OKX_DEMO_SECRET",
            "$env:OKX_MODE='live'; echo $env:OKX_SECRET",
        ]:
            with self.subTest(cmd=cmd):
                self.assertIsNotNone(term(cmd))

    def test_live_pocket_is_guardrail(self):
        self.assertIsNotNone(edit("create_file", filePath="ops/live-pocket.json", content="{}"))
        self.assertIsNotNone(term("Set-Content ops/live-pocket.json '{}'"))
        self.assertIsNone(edit("read_file", filePath="ops/live-pocket.json"))
        self.assertIsNone(edit("create_file", filePath="ops/live-pocket.example.json", content="{}"))


class NormPathTest(unittest.TestCase):
    """_norm_path: все формы пути от инструментов VS Code и Claude Code."""

    def test_forms(self):
        root_bs = str(ROOT)
        root_fs = root_bs.replace("\\", "/")
        drive, rest = root_fs[0].lower(), root_fs[2:]
        rest_url = rest.replace(" ", "%20")
        target = "ops/hooks/guard.py"
        for raw in [
            target,
            "./" + target,
            ".\\ops\\hooks\\guard.py",
            "OPS/Hooks/Guard.py",
            '"ops/hooks/guard.py"',
            "'ops/hooks/guard.py'",
            "  ops/hooks/guard.py  ",
            root_bs + "\\ops\\hooks\\guard.py",
            root_fs + "/ops/hooks/guard.py",
            root_fs.upper() + "/OPS/HOOKS/GUARD.PY",
            drive + ":" + rest + "/ops/hooks/guard.py",
            "/" + drive + rest + "/ops/hooks/guard.py",  # Git Bash
            "file:///" + drive + "%3A" + rest_url + "/ops/hooks/guard.py",  # VS Code URI
            "file:///" + drive.upper() + ":" + rest_url + "/ops/hooks/guard.py",
            "\\\\?\\" + root_bs + "\\ops\\hooks\\guard.py",
            "ops/./hooks/guard.py",
            "src/../ops/hooks/guard.py",
            root_fs + "/src/../ops/hooks/guard.py",
        ]:
            with self.subTest(raw=raw):
                self.assertEqual(guard._norm_path(raw), target)

    def test_guardrail_via_any_form(self):
        root_fs = str(ROOT).replace("\\", "/")
        for raw in ["/" + root_fs[0].lower() + root_fs[2:] + "/pump-pocket.json",
                    "file:///" + root_fs[0].lower() + "%3A" + root_fs[2:].replace(" ", "%20") + "/ops/live-pocket.json",
                    "src/../ops/hooks", "ops/hooks/", ".github/hooks"]:
            with self.subTest(raw=raw):
                self.assertIsNotNone(edit("create_file", filePath=raw, content="x"))

    def test_env_via_any_form(self):
        root_fs = str(ROOT).replace("\\", "/")
        for raw in ["/" + root_fs[0].lower() + root_fs[2:] + "/.env", "src/../.env",
                    "file:///" + root_fs[0].lower() + "%3A" + root_fs[2:].replace(" ", "%20") + "/.env"]:
            with self.subTest(raw=raw):
                self.assertIsNotNone(edit("Read", file_path=raw))

    def test_outside_paths_stay_outside(self):
        self.assertEqual(guard._norm_path("C:/Other/ops/hooks/guard.py"), "c:/other/ops/hooks/guard.py")
        self.assertIsNone(edit("create_file", filePath="C:/Other/ops/hooks/guard.py", content="x"))
        self.assertEqual(guard._norm_path(str(ROOT)), ".")


class ClientPerimeterTest(unittest.TestCase):
    """AGENT-GUARD-COMPAT: конфиги hooks клиентов и адаптер — периметр; абсолютный корень с пробелами."""

    def test_client_hook_configs_are_guardrail(self):
        for path in (".codex/hooks.json", ".codex/config.toml", ".gemini/settings.json", ".muse/hooks.json",
                     "ops/hooks/guard_adapter.py"):
            with self.subTest(path=path):
                self.assertIsNotNone(edit("create_file", filePath=path, content="{}"))
                self.assertIsNotNone(term(f"echo x > {path}"))

    def test_absolute_root_with_spaces(self):
        git_bash = "/" + str(ROOT)[0].lower() + str(ROOT)[2:].replace("\\", "/")
        for form in (str(ROOT), str(ROOT).replace("\\", "/"), git_bash, "/mnt" + git_bash):
            with self.subTest(form=form):
                self.assertIsNotNone(term(f"echo x > '{form}\\ops\\hooks\\guard.py'"))
                self.assertIsNotNone(term(f'echo x >> "{form}/pump-pocket.json"'))
        self.assertIsNone(term(f"echo x > '{ROOT}\\insights\\x.md'"))


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
