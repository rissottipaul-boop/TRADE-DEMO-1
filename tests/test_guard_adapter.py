"""Адаптер hook-вызовов Codex, Gemini и Muse к общему guard (AGENT-GUARD-COMPAT). Всё на фейках."""
import importlib.util
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT).startswith("\\\\?\\"):
    # Артефакт песочницы: префикс \\?\ уродует WSL_ROOT/WIN_ROOT и корень
    # адаптера — в проде hook стартует с обычным путём. Убираем здесь,
    # сам периметр (ops/hooks/) не трогаем.
    ROOT = Path(str(ROOT)[4:])
# После применения патча адаптер живёт в периметре ops/hooks/, до него — в src/
_CANDIDATES = [ROOT / "ops" / "hooks" / "guard_adapter.py", ROOT / "src" / "guard_adapter.py"]
ADAPTER_PATH = next(p for p in _CANDIDATES if p.is_file())
_spec = importlib.util.spec_from_file_location("guard_adapter", ADAPTER_PATH)
adapter = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(adapter)
guard = adapter.load_guard()

OFF = {"live_enabled": False}
PROFILES = ("okx-demo", {"okx-demo", "okx-demo-v2"})
WIN_ROOT = str(ROOT)
WSL_ROOT = "/mnt/" + WIN_ROOT[0].lower() + WIN_ROOT[2:].replace("\\", "/")


def check(tool_name, tool_input, event="PreToolUse"):
    payload = {"hook_event_name": event, "tool_name": tool_name, "tool_input": tool_input}
    reason, _ = adapter.evaluate(payload, guard, policy=OFF, okx_profiles=PROFILES)
    return reason


def patch(*body):
    return "\n".join(["*** Begin Patch", *body, "*** End Patch"])


class ParityTest(unittest.TestCase):
    """Формат Claude через адаптер решается так же, как самим guard: адаптер не ослабляет."""

    COMMANDS = [
        "python -m src.ops status", "git status", "okx --demo spot orders",
        "python -m src.ops reset global", "cat .env", "okx --demo account withdraw --ccy USDT",
        "Remove-Item -Recurse -Force data", "echo x > ops/hooks/guard.py", "okx --live spot place --sz 1",
        "taskkill /F /IM python.exe",
    ]

    def test_commands_same_as_guard(self):
        for cmd in self.COMMANDS:
            with self.subTest(cmd=cmd):
                direct = guard.decide("Bash", {"command": cmd}, policy=OFF, okx_profiles=PROFILES)
                self.assertEqual(check("Bash", {"command": cmd}), direct)

    def test_file_tools_same_as_guard(self):
        cases = [("Write", {"file_path": "ops/hooks/guard.py", "content": "x"}),
                 ("Edit", {"file_path": "src/engine.py", "old_string": "a", "new_string": "b"}),
                 ("Read", {"file_path": ".env"}), ("Read", {"file_path": "README.md"})]
        for tool, tool_input in cases:
            with self.subTest(tool=tool, tool_input=tool_input):
                direct = guard.decide(tool, tool_input, policy=OFF, okx_profiles=PROFILES)
                self.assertEqual(check(tool, tool_input), direct)


class CodexTest(unittest.TestCase):
    def test_bash_command_string_and_argv(self):
        self.assertIsNotNone(check("Bash", {"command": "python -m src.ops reset global"}))
        self.assertIsNone(check("Bash", {"command": "python -m src.ops status"}))
        self.assertIsNotNone(check("Bash", {"command": ["bash", "-lc", "cat .env"]}))
        self.assertIsNone(check("Bash", {"command": ["powershell.exe", "-NoProfile", "-Command",
                                                     "python -m src.ops status"]}))

    def test_exec_command_cmd(self):
        self.assertIsNotNone(check("exec_command", {"cmd": "rm -rf data"}))
        self.assertIsNotNone(check("functions.exec_command", {"cmd": ["cmd", "/c", "type .env"]}))
        self.assertIsNone(check("exec_command", {"cmd": ["bash", "-lc", "ls -la"], "workdir": WIN_ROOT}))

    def test_absolute_root_with_spaces_in_redirect(self):
        # в корне проекта пробелы: без замены корня guard не видит «> …\ops\hooks\guard.py»
        for form in (WIN_ROOT, WIN_ROOT.replace("\\", "/"), "/" + WIN_ROOT[0].lower() + WIN_ROOT[2:].replace("\\", "/")):
            with self.subTest(form=form):
                self.assertIsNotNone(check("Bash", {"command": f"echo x > '{form}\\ops\\hooks\\guard.py'"}))
                self.assertIsNotNone(check("Bash", {"command": f'echo x >> "{form}/pump-pocket.json"'}))
        self.assertEqual(adapter.relativize_command(f"type \"{WIN_ROOT}\\README.md\""), 'type "README.md"')
        self.assertIsNone(check("Bash", {"command": f"echo x > '{WIN_ROOT}\\insights\\x.md'"}))

    def test_argv_unwraps_shell(self):
        self.assertEqual(adapter.argv_to_command(["bash", "-lc", "git status"]), "git status")
        self.assertEqual(adapter.argv_to_command(["C:\\Windows\\pwsh.exe", "-NoProfile", "-Command", "a; b"]),
                         "a; b")
        self.assertEqual(adapter.argv_to_command(["git", "status"]), "git status")

    def test_apply_patch_in_command_field(self):
        # Codex кладёт текст патча в tool_input.command (docs: «Bash and apply_patch use tool_input.command»)
        for target in ("ops/hooks/guard.py", ".claude/settings.json", "pump-pocket.json",
                       f"{WIN_ROOT}\\ops\\live-policy.json", ".github/hooks/x.json"):
            with self.subTest(target=target):
                self.assertIsNotNone(check("apply_patch", {"command": patch(f"*** Update File: {target}",
                                                                            "@@", "-a", "+b")}))
        move = patch("*** Update File: insights/a.md", "*** Move to: ops/hooks/a.py", "@@", "-a", "+b")
        self.assertIsNotNone(check("apply_patch", {"command": move}))

    def test_apply_patch_allowed_and_no_false_positive_on_content(self):
        body = patch("*** Add File: insights/note.md", "+Не читать .env и не делать withdraw.")
        self.assertIsNone(check("apply_patch", {"command": body}))

    def test_apply_patch_risk_limits(self):
        # текст патча доходит до проверки лимитов guard (ослабление потолка плеча)
        loosen = patch("*** Update File: src/risk.py", "@@", "-MAX_LEVERAGE = 3", "+MAX_LEVERAGE = 50")
        self.assertIn("MAX_LEVERAGE", check("apply_patch", {"command": loosen}))

    def test_apply_patch_via_shell_heredoc(self):
        cmd = "apply_patch <<'EOF'\n" + patch("*** Update File: .claude/settings.json", "@@", "-a", "+b") + "\nEOF"
        self.assertIsNotNone(check("Bash", {"command": cmd}))
        ok = "apply_patch <<'EOF'\n" + patch("*** Add File: insights/a.md", "+x") + "\nEOF"
        self.assertIsNone(check("Bash", {"command": ok}))

    def test_nested_parallel_calls(self):
        uses = {"tool_uses": [
            {"recipient_name": "functions.exec_command", "parameters": {"cmd": "python -m src.ops status"}},
            {"recipient_name": "functions.exec_command", "parameters": {"cmd": "okx --demo account withdraw"}}]}
        self.assertIn("Вывод средств", check("multi_tool_use.parallel", uses))
        safe = {"tool_uses": [{"recipient_name": "functions.exec_command", "parameters": {"cmd": "git status"}}]}
        self.assertIsNone(check("multi_tool_use.parallel", safe))

    def test_nested_arguments_as_json_string(self):
        calls = {"tool_calls": [{"function": {"name": "exec_command", "arguments": json.dumps({"cmd": "cat .env"})}}]}
        self.assertIsNotNone(check("functions.exec", calls))
        write = {"calls": [{"name": "apply_patch", "arguments": json.dumps(
            {"command": patch("*** Add File: ops/hooks/evil.py", "+x")})}]}
        self.assertIsNotNone(check("wrapper", write))

    def test_code_mode_is_checked_conservatively(self):
        self.assertIsNotNone(check("functions.exec", {"code": "await tools.exec_command({cmd: 'python -m src.ops "
                                                              "reset global'})"}))
        self.assertIsNotNone(check("exec", {"code": "await tools.apply_patch({path: 'ops/hooks/guard.py'})"}))
        self.assertIsNotNone(check("exec", {"code": "fs.readFileSync('.env')"}))
        self.assertIsNone(check("exec", {"code": "await tools.exec_command({cmd: 'git status'})"}))

    def test_unknown_shell_shapes(self):
        self.assertIsNotNone(check("shell", {"script": "cat .env"}))
        self.assertIsNotNone(check("terminal", {"argv": ["sh", "-c", "python -m src.ops reset x"]}))
        self.assertIsNotNone(check("run", "python -m src.ops reset global"))  # tool_input строкой

    def test_too_deep_nesting_is_conservative(self):
        node = {"cmd": "python -m src.ops reset global"}
        for _ in range(adapter.MAX_DEPTH + 4):
            node = {"recipient_name": "functions.wrap", "parameters": node}
        self.assertIsNotNone(check("wrap", node))


class GeminiTest(unittest.TestCase):
    def test_tools(self):
        self.assertIsNotNone(check("run_shell_command", {"command": "python -m src.ops reset global",
                                                         "dir_path": "."}, event="BeforeTool"))
        self.assertIsNotNone(check("write_file", {"file_path": "ops/hooks/guard.py", "content": "x"}, "BeforeTool"))
        self.assertIsNotNone(check("replace", {"file_path": f"{WIN_ROOT}\\.claude\\settings.json",
                                               "old_string": "a", "new_string": "b"}, "BeforeTool"))
        self.assertIsNotNone(check("read_many_files", {"include": ["README.md", ".env"]}, "BeforeTool"))
        self.assertIsNotNone(check("list_directory", {"dir_path": ".env"}, "BeforeTool"))
        self.assertIsNone(check("read_file", {"file_path": "README.md"}, "BeforeTool"))
        self.assertIsNone(check("write_file", {"file_path": "insights/x.md", "content": "x"}, "BeforeTool"))

    def test_detect_client_by_event(self):
        self.assertEqual(adapter.detect_client({"hook_event_name": "BeforeTool"}), "gemini")
        self.assertEqual(adapter.detect_client({"hook_event_name": "PreToolUse"}), "claude")
        self.assertEqual(adapter.detect_client({"hook_event_name": "BeforeTool"}, "muse"), "muse")

    def test_deny_format(self):
        out = json.loads(adapter.render_deny("gemini", "нельзя"))
        self.assertEqual(out, {"decision": "deny", "reason": "[guard] нельзя"})
        claude = json.loads(adapter.render_deny("codex", "нельзя"))["hookSpecificOutput"]
        self.assertEqual((claude["permissionDecision"], claude["permissionDecisionReason"]), ("deny", "[guard] нельзя"))

    def test_lint_current_config_found_both_problems(self):
        current = {"hooks": {"PreToolUse": [{"matcher": "*", "hooks": [
            {"type": "command", "command": ".venv\\Scripts\\python.exe ops\\hooks\\guard.py", "timeout": 20}]}]}}
        problems = "\n".join(adapter.lint_hook_config("gemini", current))
        self.assertIn("PreToolUse Gemini не знает", problems)
        self.assertIn("нет события BeforeTool", problems)

    def test_lint_timeout_is_milliseconds(self):
        cfg = {"hooks": {"BeforeTool": [{"matcher": "*", "hooks": [
            {"type": "command", "command": ".venv\\Scripts\\python.exe src\\guard_adapter.py --client gemini",
             "timeout": 20}]}]}}
        self.assertIn("миллисекунды", "\n".join(adapter.lint_hook_config("gemini", cfg)))
        cfg["hooks"]["BeforeTool"][0]["hooks"][0]["timeout"] = 20000
        self.assertEqual(adapter.lint_hook_config("gemini", cfg), [])


class MuseTest(unittest.TestCase):
    def test_wsl_paths_to_project(self):
        self.assertEqual(adapter.normalize_path(f"{WSL_ROOT}/ops/hooks/guard.py"), "ops/hooks/guard.py")
        self.assertEqual(adapter.normalize_path(WSL_ROOT), ".")
        unc = "\\\\wsl.localhost\\Ubuntu-22.04" + WSL_ROOT.replace("/", "\\") + "\\pump-pocket.json"
        self.assertEqual(adapter.normalize_path(unc), "pump-pocket.json")
        self.assertEqual(adapter.normalize_path("/mnt/d/other/x.txt"), "d:/other/x.txt")
        self.assertEqual(adapter.normalize_path("/home/ttt/x"), "/home/ttt/x")

    def test_linux_python_root(self):
        # Адаптер под Linux-python в WSL: корень /mnt/c/..., путь от Windows-клиента
        linux_root = Path(WSL_ROOT)
        self.assertEqual(adapter.normalize_path(f"{WIN_ROOT}\\pump-pocket.json", linux_root), "pump-pocket.json")

    def test_wsl_write_to_perimeter_denied(self):
        self.assertIsNotNone(check("write_file", {"path": f"{WSL_ROOT}/ops/hooks/guard.py", "content": "x"}))
        self.assertIsNotNone(check("read", {"path": f"{WSL_ROOT}/.env"}))
        self.assertIsNotNone(check("Bash", {"command": f"echo x > '{WSL_ROOT}/ops/hooks/guard.py'"}))
        self.assertIsNone(check("write_file", {"path": f"{WSL_ROOT}/insights/x.md", "content": "x"}))

    def test_unknown_write_tool_detected_by_payload(self):
        self.assertIsNotNone(check("fs_update", {"path": f"{WSL_ROOT}/pump-pocket.json", "content": "{}"}))
        self.assertIsNone(check("fs_stat", {"path": f"{WSL_ROOT}/pump-pocket.json"}))

    def test_lint_wsl_backslash_and_adapter(self):
        current = {"hooks": {"PreToolUse": [{"matcher": "*", "hooks": [
            {"type": "command", "command": ".venv\\Scripts\\python.exe ops\\hooks\\guard.py", "timeout": 20}]}]}}
        self.assertIn("не вызывает адаптер", "\n".join(adapter.lint_hook_config("muse", current)))
        slashes = {"hooks": {"PreToolUse": [{"matcher": "*", "hooks": [
            {"type": "command", "command": ".venv\\Scripts\\python.exe src\\guard_adapter.py --client muse",
             "timeout": 20}]}]}}
        self.assertIn("WSL", "\n".join(adapter.lint_hook_config("muse", slashes)))
        slashes["hooks"]["PreToolUse"][0]["hooks"][0]["command"] = (
            ".venv/Scripts/python.exe src/guard_adapter.py --client muse")
        self.assertEqual(adapter.lint_hook_config("muse", slashes), [])

    def test_trust_from_stderr(self):
        stderr = ("rules file AGENTS.md exists, but the workspace is untrusted, so it is skipped for this session; "
                  "restart with --trust-workspace to load project rules\n"
                  "project skills skipped because workspace is untrusted")
        self.assertEqual(adapter.muse_trust_status(stderr), "untrusted")
        self.assertEqual(adapter.muse_trust_status("ok"), "unknown")


class ProposedConfigTest(unittest.TestCase):
    """Конфиги из insights/guard-compat-patch.diff проходят проверку; после применения — сами файлы."""

    PROPOSED = {
        "gemini": {"hooks": {"BeforeTool": [{"matcher": "*", "hooks": [{
            "type": "command", "name": "project-guard",
            "command": ".venv\\Scripts\\python.exe ops\\hooks\\guard_adapter.py --client gemini",
            "timeout": 20000}]}]}},
        "codex": {"hooks": {"PreToolUse": [{"matcher": "*", "hooks": [{
            "type": "command",
            "command": ".venv/Scripts/python.exe ops/hooks/guard_adapter.py --client codex",
            "commandWindows": ".venv\\Scripts\\python.exe ops\\hooks\\guard_adapter.py --client codex",
            "timeout": 20}]}]}},
        "muse": {"hooks": {"PreToolUse": [{"matcher": "*", "hooks": [{
            "type": "command",
            "command": ".venv/Scripts/python.exe ops/hooks/guard_adapter.py --client muse",
            "timeout": 20}]}]}},
    }
    FILES = {"gemini": ".gemini/settings.json", "codex": ".codex/hooks.json", "muse": ".muse/hooks.json"}

    def test_proposed(self):
        for client, cfg in self.PROPOSED.items():
            with self.subTest(client=client):
                self.assertEqual(adapter.lint_hook_config(client, cfg), [])

    def test_patch_file_matches_proposed(self):
        diff = ROOT / "insights" / "guard-compat-patch.diff"
        if not diff.is_file():
            self.skipTest("патч ещё не подготовлен")
        text = diff.read_text(encoding="utf-8")
        for client, cfg in self.PROPOSED.items():
            command = cfg["hooks"]["BeforeTool" if client == "gemini" else "PreToolUse"][0]["hooks"][0]["command"]
            with self.subTest(client=client):
                self.assertIn(json.dumps(command), text)

    def test_applied_files_when_adapter_in_perimeter(self):
        if ADAPTER_PATH.parent.name != "hooks":
            self.skipTest("патч не применён: адаптер ещё в src/")
        for client, rel in self.FILES.items():
            with self.subTest(client=client):
                cfg = json.loads((ROOT / rel).read_text(encoding="utf-8-sig"))
                self.assertEqual(adapter.lint_hook_config(client, cfg), [])


class HookContractTest(unittest.TestCase):
    """stdin/stdout адаптера как у guard: deny в формате клиента, allow — тишина, сбой — fail-open."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.log = Path(self._tmp.name) / "guard.log"
        self.env = {**os.environ, "AGENT_GUARD_LOG": str(self.log)}
        self.env.pop("AGENT_GUARD_TRACE", None)

    def tearDown(self):
        self._tmp.cleanup()

    def run_hook(self, payload, client, raw=None, env=None):
        data = raw if raw is not None else json.dumps(payload).encode()
        proc = subprocess.run([sys.executable, str(ADAPTER_PATH), "--client", client], input=data,
                              capture_output=True, timeout=30, env=env or self.env, cwd=ROOT)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        return proc.stdout.decode("utf-8").strip()

    def test_codex_deny(self):
        out = json.loads(self.run_hook({"hook_event_name": "PreToolUse", "tool_name": "apply_patch",
                                        "tool_input": {"command": patch("*** Add File: ops/hooks/x.py", "+x")}},
                                       "codex"))
        self.assertEqual(out["hookSpecificOutput"]["permissionDecision"], "deny")
        logged = json.loads(self.log.read_text(encoding="utf-8").splitlines()[-1])
        self.assertEqual((logged["decision"], logged["client"]), ("deny", "codex"))

    def test_gemini_deny(self):
        out = json.loads(self.run_hook({"hook_event_name": "BeforeTool", "tool_name": "run_shell_command",
                                        "tool_input": {"command": "python -m src.ops reset global"}}, "gemini"))
        self.assertEqual(out["decision"], "deny")
        self.assertNotIn("hookSpecificOutput", out)

    def test_allow_is_silent(self):
        for client in ("codex", "gemini", "muse"):
            with self.subTest(client=client):
                self.assertEqual(self.run_hook({"tool_name": "Bash", "tool_input": {"command": "git status"}},
                                               client), "")

    def test_garbage_fails_open_and_logs(self):
        self.assertEqual(self.run_hook(None, "muse", raw=b"not json"), "")
        self.assertIn('"error"', self.log.read_text(encoding="utf-8"))

    def test_bom_input_is_checked(self):
        payload = json.dumps({"tool_name": "Bash", "tool_input": {"command": "cat .env"}}).encode()
        out = json.loads(self.run_hook(None, "muse", raw=b"\xef\xbb\xbf" + payload))
        self.assertEqual(out["hookSpecificOutput"]["permissionDecision"], "deny")

    def test_trace_logs_shape_without_values(self):
        env = {**self.env, "AGENT_GUARD_TRACE": "1"}
        self.run_hook({"hook_event_name": "PreToolUse", "tool_name": "fs_write",
                       "tool_input": {"path": "insights/x.md", "content": "секрет-не-логировать"}}, "muse", env=env)
        text = self.log.read_text(encoding="utf-8")
        self.assertIn('"trace"', text)
        self.assertIn("fs_write", text)
        self.assertNotIn("секрет-не-логировать", text)


if __name__ == "__main__":
    unittest.main()
