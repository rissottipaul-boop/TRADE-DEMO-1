"""Ротация агентов: изолированный PowerShell, без сети и настоящих CLI.

Копируем launcher и registry во временный проект. Все command в registry
заменены тестовыми функциями, а Get-Command разрешает только их имена.
Ни агенты, ни рабочие журналы, ни движок проекта не запускаются.
"""

import json
import re
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace


ROOT = Path(__file__).resolve().parent.parent
POWERSHELL = shutil.which("powershell") or shutil.which("pwsh")
ROLES = {
    "project-orchestrator",
    "insight-executor",
    "crypto-insight-hunter",
    "okx-trader",
    "pump-risk-taker",
    "ops-sentinel",
}
RUNTIMES = ("claude", "gemini", "muse", "codex")

HARNESS = r"""
$ErrorActionPreference = 'Stop'
[Console]::OutputEncoding = New-Object System.Text.UTF8Encoding($false)
$cfg = Get-Content -LiteralPath (Join-Path $PSScriptRoot 'input.json') -Raw -Encoding UTF8 | ConvertFrom-Json
$parameters = @{}
foreach ($property in $cfg.parameters.PSObject.Properties) {
    $parameters[$property.Name] = $property.Value
}
function Get-Command {
    param([string]$Name, [string]$ErrorAction)
    if ($Name -notmatch '^__agent_rotate_test_(claude|gemini|muse|codex)$') {
        throw "Forbidden command lookup in test: $Name"
    }
    if ($Matches[1] -in $cfg.available) {
        [pscustomobject]@{Name = $Name}
    }
}
function Invoke-FakeCli {
    param([string]$AgentName, [object[]]$ReceivedArgs)
    [ordered]@{agent = $AgentName; arguments = @($ReceivedArgs)} |
        ConvertTo-Json -Compress -Depth 5 |
        Add-Content -LiteralPath (Join-Path $PSScriptRoot 'calls.jsonl') -Encoding UTF8
    Write-Output $cfg.output
    $global:LASTEXITCODE = [int]$cfg.exit_code
}
function __agent_rotate_test_claude { Invoke-FakeCli -AgentName 'claude' -ReceivedArgs $args }
function __agent_rotate_test_gemini { Invoke-FakeCli -AgentName 'gemini' -ReceivedArgs $args }
function __agent_rotate_test_muse { Invoke-FakeCli -AgentName 'muse' -ReceivedArgs $args }
function __agent_rotate_test_codex { Invoke-FakeCli -AgentName 'codex' -ReceivedArgs $args }
& (Join-Path $PSScriptRoot 'ops/agent-rotate.ps1') @parameters
exit $LASTEXITCODE
"""


@unittest.skipUnless(POWERSHELL, "Для launcher-тестов нужен powershell или pwsh")
class AgentRotateTests(unittest.TestCase):
    def setUp(self):
        self.registry = json.loads(
            (ROOT / "ops/agent-routing.json").read_text(encoding="utf-8-sig")
        )

    def run_launcher(self, *, available=RUNTIMES, exit_code=0, output="ok", **parameters):
        with tempfile.TemporaryDirectory(prefix="agent-rotate-test-") as temporary:
            # Песочница может вернуть путь с префиксом \\?\ — Join-Path в
            # Windows PowerShell 5.1 такие пути не разбирает («drive» is null).
            if temporary.startswith("\\\\?\\"):
                temporary = temporary[4:]
            project = Path(temporary)
            (project / "ops").mkdir()
            shutil.copy2(ROOT / "ops/agent-rotate.ps1", project / "ops/agent-rotate.ps1")
            registry = json.loads(json.dumps(self.registry))
            for name, runtime in registry["runtimes"].items():
                runtime["command"] = "__agent_rotate_test_" + name
            (project / "ops/agent-routing.json").write_text(
                json.dumps(registry), encoding="utf-8"
            )
            (project / "input.json").write_text(
                json.dumps({"parameters": parameters, "available": list(available),
                            "exit_code": exit_code, "output": output}, ensure_ascii=False),
                encoding="utf-8",
            )
            (project / "harness.ps1").write_text(HARNESS, encoding="utf-8-sig")
            result = subprocess.run(
                [POWERSHELL, "-NoLogo", "-NoProfile", "-NonInteractive",
                 "-ExecutionPolicy", "Bypass", "-File", str(project / "harness.ps1")],
                cwd=project, capture_output=True, encoding="utf-8", errors="replace",
                timeout=30, check=False,
            )
            calls_path = project / "calls.jsonl"
            logs_path = project / "logs/agent-rotate.log"
            calls = [json.loads(line) for line in calls_path.read_text(encoding="utf-8-sig").splitlines()] if calls_path.exists() else []
            logs = [json.loads(line) for line in logs_path.read_text(encoding="utf-8-sig").splitlines()] if logs_path.exists() else []
            return SimpleNamespace(
                code=result.returncode, stdout=result.stdout, stderr=result.stderr,
                calls=calls, logs=logs, log_directory=(project / "logs").exists(),
                project=str(project),
            )

    def plan(self, **parameters):
        result = self.run_launcher(Plan=True, **parameters)
        self.assertEqual(result.code, 0, result.stderr)
        self.assertEqual(result.calls, [], "-Plan не должен вызывать CLI")
        self.assertFalse(result.log_directory, "-Plan не должен создавать logs")
        plan = json.loads(result.stdout)
        self.assertIs(plan["retry_after_start"], False)
        return plan

    def test_script_has_utf8_bom_for_windows_powershell(self):
        self.assertTrue((ROOT / "ops/agent-rotate.ps1").read_bytes().startswith(b"\xef\xbb\xbf"))

    def test_claude_role_wrappers_match_registry_models(self):
        for role in sorted(ROLES - {"project-orchestrator"}):
            with self.subTest(role=role):
                wrapper = (ROOT / ".claude/agents" / (role + ".md")).read_text(encoding="utf-8-sig")
                frontmatter = wrapper.split("---", 2)[1]
                models = re.findall(r"^model:\s*(\S+)\s*$", frontmatter, re.MULTILINE)
                self.assertEqual(models, [self.registry["roles"][role]["models"]["claude"]])

    def test_plan_all_six_roles_use_declared_priorities_and_models(self):
        self.assertEqual(set(self.registry["roles"]), ROLES)
        for role in sorted(ROLES):
            with self.subTest(role=role):
                plan = self.plan(Role=role, TaskId="T42")
                config = self.registry["roles"][role]
                self.assertEqual(plan["role"], role)
                self.assertEqual(plan["task"], "T42")
                self.assertEqual(plan["selected"], config["priority"][0])
                self.assertEqual([c["agent"] for c in plan["candidates"]], config["priority"])
                for candidate in plan["candidates"]:
                    self.assertEqual(candidate["model"], config["models"][candidate["agent"]])
                    arguments = candidate["arguments"]
                    self.assertEqual(arguments[arguments.index("--model") + 1], candidate["model"])
                    self.assertIn(config["file"], arguments[-1])
                    self.assertIn("T42", arguments[-1])
                    if candidate["agent"] in {"codex", "gemini", "muse"}:
                        self.assertIn("разрешены только анализ, чтение и рекомендации", arguments[-1])

    def test_prompt_points_to_token_economy_and_addressed_board_read(self):
        with_task = self.plan(Role="insight-executor", TaskId="T42")
        for candidate in with_task["candidates"]:
            prompt = candidate["arguments"][-1]
            self.assertIn("ops/token-economy.md", prompt)
            self.assertIn("не снижая модель, проверки и риск-контроль", prompt)
            self.assertIn("src.agent_context --task T42", prompt)
        without_task = self.plan(Role="insight-executor")
        for candidate in without_task["candidates"]:
            self.assertIn("ops/token-economy.md", candidate["arguments"][-1])
            self.assertNotIn("src.agent_context --task", candidate["arguments"][-1])

    def test_legacy_order_and_default_models_preserved(self):
        plan = self.plan()
        self.assertEqual([c["agent"] for c in plan["candidates"]], list(RUNTIMES))
        self.assertEqual(plan["selected"], "claude")
        for candidate in plan["candidates"]:
            if candidate["agent"] == "codex":
                self.assertEqual(candidate["model"], self.registry["runtimes"]["codex"]["default_model"])
                self.assertIn("разрешены только анализ, чтение и рекомендации", candidate["arguments"][-1])
            else:
                self.assertIsNone(candidate["model"])
                self.assertNotIn("--model", candidate["arguments"])
                self.assertNotIn("разрешены только анализ, чтение и рекомендации", candidate["arguments"][-1])

    def test_skip_alias_and_explicit_antigravity_alias(self):
        plan = self.plan(SkipAgent=["claude", "antigravity"])
        self.assertEqual(plan["selected"], "muse")
        self.assertEqual([c["agent"] for c in plan["candidates"] if c["skipped"]], ["claude", "gemini"])
        plan = self.plan(Agent="antigravity", Role="crypto-insight-hunter")
        self.assertEqual(plan["selected"], "gemini")
        self.assertEqual(len(plan["candidates"]), 1)
        self.assertIn("--prompt-interactive", plan["candidates"][0]["arguments"])

    def test_explicit_model_requires_explicit_agent(self):
        result = self.run_launcher(Plan=True, Model="test-model", Role="insight-executor")
        self.assertNotEqual(result.code, 0)
        self.assertIn("-Model requires an explicit -Agent", result.stderr)
        self.assertEqual(result.calls, [])
        self.assertFalse(result.log_directory)
        plan = self.plan(Agent="codex", Model="test-model")
        self.assertEqual(plan["candidates"][0]["model"], "test-model")

    def test_headless_arguments_and_prompt_are_passed_intact(self):
        prompt = 'Задача: "двойные", \'одинарные\', $(), $(throw "не выполнять").\nСледующая строка; --model чужая'
        for agent in RUNTIMES:
            with self.subTest(agent=agent):
                result = self.run_launcher(Agent=agent, Model="test-model", Headless=True, Prompt=prompt)
                self.assertEqual(result.code, 0, result.stderr)
                self.assertEqual(len(result.calls), 1)
                self.assertEqual(result.calls[0]["agent"], agent)
                arguments = result.calls[0]["arguments"]
                self.assertTrue(arguments[-1].endswith(prompt))
                self.assertEqual(sum(prompt in argument for argument in arguments), 1)
                expected = {
                    "claude": ["--print", "--output-format", "text", "--model", "test-model", "--"],
                    "gemini": ["--model", "test-model", "--prompt"],
                    "muse": ["exec", "--model", "test-model", "--"],
                    "codex": ["--ask-for-approval", "never", "exec", "--sandbox", "read-only", "--cd", result.project, "--model", "test-model", "--"],
                }[agent]
                self.assertEqual(arguments[:-1], expected)
                self.assertEqual([line["event"] for line in result.logs], ["start", "finish"])
                self.assertNotIn(prompt, json.dumps(result.logs, ensure_ascii=False))

    def test_failure_never_replays_prompt_and_preserves_exit_code(self):
        for code, message in [(23, "authentication failed"), (41, "guard denied"), (75, "rate limit quota exhausted")]:
            with self.subTest(code=code):
                result = self.run_launcher(Headless=True, exit_code=code, output=message)
                self.assertEqual(result.code, code, result.stderr)
                self.assertEqual([call["agent"] for call in result.calls], ["claude"])
                self.assertEqual(result.logs[-1]["exit_code"], code)
                self.assertIn("Stopped without replay", result.stdout)

    def test_successful_output_mentioning_rate_limit_does_not_trigger_fallback(self):
        result = self.run_launcher(Headless=True, output="Fixed rate limit handling; quota test passed")
        self.assertEqual(result.code, 0, result.stderr)
        self.assertEqual([call["agent"] for call in result.calls], ["claude"])
        self.assertEqual(result.logs[-1]["exit_code"], 0)
        self.assertNotIn("Stopped without replay", result.stdout)

    def test_missing_cli_is_skipped_before_first_launch(self):
        plan = self.plan(available=["muse", "codex"])
        self.assertEqual(plan["selected"], "muse")
        self.assertEqual([c["available"] for c in plan["candidates"]], [False, False, True, True])
        result = self.run_launcher(available=["muse", "codex"], Headless=True)
        self.assertEqual(result.code, 0, result.stderr)
        self.assertEqual([call["agent"] for call in result.calls], ["muse"])

    def test_no_eligible_cli_does_not_invoke_any_model_or_create_log(self):
        for available, skipped in [((), []), (RUNTIMES, list(RUNTIMES))]:
            with self.subTest(available=available, skipped=skipped):
                plan = self.plan(available=available, SkipAgent=skipped)
                self.assertIsNone(plan["selected"])
                result = self.run_launcher(available=available, SkipAgent=skipped)
                self.assertNotEqual(result.code, 0)
                self.assertIn("No eligible CLI found", result.stderr)
                self.assertEqual(result.calls, [])
                self.assertFalse(result.log_directory)


if __name__ == "__main__":
    unittest.main()
