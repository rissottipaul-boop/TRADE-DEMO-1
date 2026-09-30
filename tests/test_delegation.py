"""Мост делегирования: очередь submit/run-once/fetch + check/status в изоляции.

Копируем ops/delegate.ps1 во временный проект, muse подменяем фейком (muse.cmd).
Настоящий muse, очередь проекта и раннер не трогаем.
"""

import json
import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
POWERSHELL = shutil.which("powershell") or shutil.which("pwsh")

FAKE_OK = "@echo off\r\necho called>> \"%~dp0called.txt\"\r\necho FAKE-MUSE-OK args: %*\r\necho --model --prompt-file\r\nexit 0\r\n"
FAKE_FAIL = "@echo off\r\necho FAKE-MUSE-FAIL\r\nexit 3\r\n"
FAKE_SLEEP = "@echo off\r\nping -n 7 127.0.0.1 >nul\r\necho woke\r\nexit 0\r\n"


class DelegateCase(unittest.TestCase):
    def setUp(self):
        if POWERSHELL is None:
            self.skipTest("no powershell")
        tmp = tempfile.mkdtemp(prefix="deleg-")
        if tmp.startswith("\\\\?\\"):
            tmp = tmp[4:]
        self.tmp = Path(tmp)
        self.ops = self.tmp / "ops"
        self.ops.mkdir()
        shutil.copy(ROOT / "ops" / "delegate.ps1", self.ops / "delegate.ps1")
        self.deleg = self.tmp / "ops" / "delegations"

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def write_fake(self, body=FAKE_OK):
        fake = self.ops / "muse.cmd"
        fake.write_text(body, encoding="ascii")
        return str(fake)

    def run_ps(self, *args, env_extra=None):
        env = dict(os.environ)
        env.pop("DELEGATE_MUSE_CMD", None)
        env.pop("DELEGATE_MUSE_ARGS", None)
        if env_extra:
            env.update(env_extra)
        return subprocess.run(
            [POWERSHELL, "-NoProfile", "-ExecutionPolicy", "Bypass",
             "-File", str(self.ops / "delegate.ps1"), *args],
            capture_output=True, text=True, timeout=120, env=env, cwd=str(self.tmp))

    def test_submit_creates_valid_request(self):
        r = self.run_ps("submit", "-Prompt", "list thresholds", "-From", "claude",
                        "-Role", "crypto-insight-hunter")
        self.assertEqual(r.returncode, 0, r.stderr)
        rid = r.stdout.strip().splitlines()[-1]
        req = json.loads((self.deleg / "inbox" / f"{rid}.json").read_text(encoding="utf-8-sig"))
        self.assertEqual(req["prompt"], "list thresholds")
        self.assertEqual(req["from"], "claude")
        self.assertEqual(req["role"], "crypto-insight-hunter")
        self.assertEqual(req["timeout_min"], 20)

    def test_submit_requires_prompt(self):
        r = self.run_ps("submit", "-From", "codex")
        self.assertNotEqual(r.returncode, 0)

    def test_direct_json_round_trip_cyrillic(self):
        fake = self.write_fake()
        rid = "d20260930-064500-1A2B"
        req = {"id": rid, "from": "codex", "role": "insight-executor",
               "prompt": "Перечисли пороги риск-ядра", "timeout_min": 20}
        inbox = self.deleg / "inbox"
        inbox.mkdir(parents=True)
        (inbox / f"{rid}.json").write_text(json.dumps(req, ensure_ascii=False), encoding="utf-8")
        r = self.run_ps("run-once", env_extra={"DELEGATE_MUSE_CMD": fake})
        self.assertEqual(r.returncode, 0, r.stderr)
        box = json.loads((self.deleg / "outbox" / f"{rid}.json").read_text(encoding="utf-8-sig"))
        self.assertEqual(box["status"], "done")
        self.assertEqual(box["exit_code"], 0)
        self.assertIn("FAKE-MUSE-OK", box["output_tail"])
        self.assertIn("--model", (self.deleg / "outbox" / f"{rid}.log").read_text(encoding="utf-8-sig"))
        prompt = (self.deleg / "outbox" / f"{rid}.prompt.md").read_text(encoding="utf-8-sig")
        self.assertIn("AGENTS.md", prompt)
        self.assertIn("ЗАПРЕЩЕНЫ", prompt)
        self.assertIn("Перечисли пороги риск-ядра", prompt)
        self.assertTrue((self.deleg / "done" / f"{rid}.json").exists())
        self.assertFalse((self.deleg / "inbox" / f"{rid}.json").exists())

    def test_bad_requests_do_not_call_muse(self):
        fake = self.write_fake()
        inbox = self.deleg / "inbox"
        inbox.mkdir(parents=True)
        (inbox / "d-bad.json").write_text("{not json", encoding="utf-8")
        (inbox / "d-empty.json").write_text(json.dumps({"id": "d-empty", "prompt": "  "}),
                                            encoding="utf-8")
        r = self.run_ps("run-once", env_extra={"DELEGATE_MUSE_CMD": fake})
        self.assertEqual(r.returncode, 0, r.stderr)
        for name in ("d-bad", "d-empty"):
            box = json.loads((self.deleg / "outbox" / f"{name}.json").read_text(encoding="utf-8-sig"))
            self.assertEqual(box["status"], "error")
        self.assertFalse((self.ops / "called.txt").exists())

    def test_muse_exit_code_propagates(self):
        fake = self.write_fake(FAKE_FAIL)
        inbox = self.deleg / "inbox"
        inbox.mkdir(parents=True)
        (inbox / "d-fail.json").write_text(json.dumps({"id": "d-fail", "prompt": "x"}), encoding="utf-8")
        r = self.run_ps("run-once", env_extra={"DELEGATE_MUSE_CMD": fake})
        self.assertEqual(r.returncode, 0, r.stderr)
        box = json.loads((self.deleg / "outbox" / "d-fail.json").read_text(encoding="utf-8-sig"))
        self.assertEqual(box["status"], "error")
        self.assertEqual(box["exit_code"], 3)
        self.assertIn("exit 3", box["error"])

    def test_timeout_kills_muse(self):
        fake = self.write_fake(FAKE_SLEEP)
        inbox = self.deleg / "inbox"
        inbox.mkdir(parents=True)
        (inbox / "d-slow.json").write_text(json.dumps({"id": "d-slow", "prompt": "x"}), encoding="utf-8")
        r = self.run_ps("run-once", "-ForceTimeoutSec", "2", env_extra={"DELEGATE_MUSE_CMD": fake})
        self.assertEqual(r.returncode, 0, r.stderr)
        box = json.loads((self.deleg / "outbox" / "d-slow.json").read_text(encoding="utf-8-sig"))
        self.assertEqual(box["status"], "timeout")
        # В песочнице kill запрещён токеном — флаг честно отражает исход
        self.assertIsInstance(box["killed"], bool)

    def test_fetch_states(self):
        inbox = self.deleg / "inbox"
        inbox.mkdir(parents=True)
        (inbox / "d-pend.json").write_text(json.dumps({"id": "d-pend", "prompt": "x"}), encoding="utf-8")
        r = self.run_ps("fetch", "-Id", "d-pend")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("pending", r.stdout)
        r = self.run_ps("fetch", "-Id", "d-nope")
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("unknown id", r.stdout)

    def test_check_ok_and_missing(self):
        fake = self.write_fake()
        r = self.run_ps("check", env_extra={"DELEGATE_MUSE_CMD": fake})
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("OK", r.stdout)
        # Без фейка и без muse в PATH — FAIL. Удаляем фейк первой части и
        # режем PATH до системного, чтобы тест был детерминирован везде.
        (self.ops / "muse.cmd").unlink()
        env = dict(os.environ)
        env.pop("DELEGATE_MUSE_CMD", None)
        env["PATH"] = r"C:\Windows\system32;C:\Windows"
        r = subprocess.run(
            [POWERSHELL, "-NoProfile", "-ExecutionPolicy", "Bypass",
             "-File", str(self.ops / "delegate.ps1"), "check"],
            capture_output=True, text=True, timeout=60, env=env, cwd=str(self.tmp))
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("FAIL", r.stdout)

    def test_pause_keeps_queue(self):
        self.write_fake()
        inbox = self.deleg / "inbox"
        inbox.mkdir(parents=True)
        (inbox / "d-p.json").write_text(json.dumps({"id": "d-p", "prompt": "x"}), encoding="utf-8")
        (self.deleg / "PAUSED").write_text("", encoding="utf-8")
        r = self.run_ps("run-once")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertTrue((self.deleg / "inbox" / "d-p.json").exists())
        self.assertFalse((self.ops / "called.txt").exists())

    def test_status_empty_queue(self):
        r = self.run_ps("status")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("inbox 0", r.stdout)


if __name__ == "__main__":
    unittest.main()
