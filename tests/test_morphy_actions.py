"""Управляющий контур Morphy (MORPHY-UI-CONTROLS): только временные каталоги и фейки.

Ни одного реального kill/cancel/reset/engine stop/start и ни одного вызова OKX:
биржа, emergency_stop и ops/engine.ps1 подменены, риск-ядро — на временной базе.
"""
import io
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from unittest.mock import patch

from src import morphy_actions as ma
from src import risk

PROJECT = Path(__file__).resolve().parents[1]
SECRET = "private-key=do-not-expose"


class Recorder:
    """Фейковые зависимости run(): фиксируют вызовы, сети нет."""

    def __init__(self, root: Path, *, running=True, report=None, exchange_error=None,
                 script_result=None, env_mode="demo"):
        self.calls = []
        self.running = running
        self.report = report if report is not None else {"cancelled": ["o1", "o2"], "failed": [], "errors": []}
        self.exchange_error = exchange_error
        self.script_result = script_result or {"ok": True, "exit_code": 0, "output": SECRET}
        self.env = env_mode
        data = root / "data"
        self.deps = ma.Deps(risk_db=data / "risk_state.db", kill_flag=data / "KILL",
                            stop_flag=data / "STOP_ENGINE", env_mode=self.env_mode,
                            engine_running=self.engine_running, exchange=self.exchange,
                            emergency_stop=self.emergency_stop, engine_script=self.engine_script)

    def env_mode(self):
        self.calls.append(("env_mode",))
        return self.env

    def engine_running(self):
        self.calls.append(("engine_running",))
        return self.running

    def exchange(self):
        self.calls.append(("exchange",))
        if self.exchange_error:
            raise self.exchange_error
        return "fake-exchange"

    def emergency_stop(self, ex, include_bots):
        self.calls.append(("emergency_stop", ex, include_bots))
        return self.report

    def engine_script(self, operation):
        self.calls.append(("engine_script", operation))
        return self.script_result

    def names(self):
        return [call[0] for call in self.calls]


class Base(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.root = Path(self._tmp.name)
        (self.root / "data").mkdir()
        (self.root / "ops").mkdir()
        (self.root / "ops" / "engine.ps1").write_text("# фейк для проверки наличия\n", encoding="utf-8")
        patcher = patch.object(ma, "_pwsh_available", return_value=True)
        patcher.start()
        self.addCleanup(patcher.stop)

    def tearDown(self):
        risk.init(Path(self._tmp.name) / "unused.db")  # отпустить файл базы до очистки
        self._tmp.cleanup()

    def risk_db(self) -> Path:
        risk.init(self.root / "data" / "risk_state.db")
        # Первый status() делает дневной rollover (как в работающей базе, где day_date
        # уже есть) — иначе он снял бы дневной breaker, выставленный тестом
        risk.status()
        return self.root / "data" / "risk_state.db"

    def audit(self) -> list[dict]:
        path = self.root / "logs" / "control-panel-actions.jsonl"
        if not path.exists():
            return []
        return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]

    def assert_clean(self, result):
        text = json.dumps(result, ensure_ascii=False)
        self.assertNotIn("do-not-expose", text)
        for key in ("output", "stdout", "stderr"):
            self.assertNotIn(key, result)


HEALTHY_RISK = {"kill_active": False, "daily_breaker": False, "global_breaker": False}
BLOCKED_RISK = {"kill_active": True, "daily_breaker": True, "global_breaker": True}


class DescribeTests(Base):
    def by_id(self, controls):
        return {item["id"]: item for item in controls["actions"]}

    def test_contract_shape_and_fields(self):
        controls = ma.describe(self.root, risk=HEALTHY_RISK, engine={"running": True},
                               flags={"KILL": False, "STOP_ENGINE": False})
        self.assertEqual(controls["mode"], "demo")
        self.assertTrue(controls["enabled"])
        self.assertEqual([item["id"] for item in controls["actions"]],
                         ["kill.engage", "orders.cancel_all", "engine.pause", "engine.resume",
                          "breaker.reset.daily", "breaker.reset.global", "kill.reset"])
        keys = {"id", "label", "group", "level", "hold_ms", "confirm_phrase", "available", "reason", "warning"}
        for item in controls["actions"]:
            self.assertEqual(set(item), keys)
            self.assertIn(item["group"], ("safety", "engine", "reset"))
            self.assertIn(item["level"], ("emergency", "caution", "critical-reset"))
            self.assertTrue(item["warning"])
            if item["group"] == "reset":
                self.assertEqual(item["hold_ms"], 2500)
                self.assertTrue(item["confirm_phrase"])
            else:
                self.assertEqual(item["hold_ms"], 2000)
                self.assertIsNone(item["confirm_phrase"])
        items = self.by_id(controls)
        self.assertEqual(items["kill.reset"]["confirm_phrase"], "СНЯТЬ KILL")
        self.assertEqual(items["breaker.reset.daily"]["confirm_phrase"], "СНЯТЬ DAILY")
        self.assertEqual(items["breaker.reset.global"]["confirm_phrase"], "СНЯТЬ GLOBAL")
        self.assertIn("защитные стопы", items["orders.cancel_all"]["warning"])
        self.assertIn("P1-72H", items["engine.pause"]["warning"])
        self.assertEqual(items["kill.engage"]["level"], "emergency")

    def test_healthy_state_availability(self):
        items = self.by_id(ma.describe(self.root, risk=HEALTHY_RISK, engine={"running": True},
                                       flags={"KILL": False, "STOP_ENGINE": False}))
        for action in ("kill.engage", "orders.cancel_all", "engine.pause"):
            self.assertTrue(items[action]["available"], action)
            self.assertIsNone(items[action]["reason"])
        self.assertEqual(items["engine.resume"]["reason"], "движок уже запущен")
        self.assertEqual(items["kill.reset"]["reason"], "kill-switch не активен")
        self.assertEqual(items["breaker.reset.daily"]["reason"], "дневной breaker не активен")
        self.assertEqual(items["breaker.reset.global"]["reason"], "глобальный breaker не активен")
        for action in ("engine.resume", "kill.reset", "breaker.reset.daily", "breaker.reset.global"):
            self.assertFalse(items[action]["available"], action)

    def test_blocked_state_enables_resets_and_resume(self):
        items = self.by_id(ma.describe(self.root, risk=BLOCKED_RISK, engine={"running": False},
                                       flags={"KILL": False, "STOP_ENGINE": False}))
        for action in ("kill.reset", "breaker.reset.daily", "breaker.reset.global", "engine.resume",
                       "kill.engage", "orders.cancel_all"):
            self.assertTrue(items[action]["available"], action)
        self.assertEqual(items["engine.pause"]["reason"], "движок уже остановлен")
        self.assertIn("входы останутся заблокированы", items["engine.resume"]["warning"])
        self.assertIn("уже активен", items["kill.engage"]["warning"])

    def test_live_or_unknown_mode_disables_everything(self):
        for mode in ("live", "weird"):
            controls = ma.describe(self.root, risk=BLOCKED_RISK, engine={"running": True},
                                   flags={}, mode=mode)
            self.assertFalse(controls["enabled"])
            self.assertIn(controls["mode"], ("live", "unknown"))
            for item in controls["actions"]:
                self.assertFalse(item["available"])
                self.assertEqual(item["reason"], ma.ONLY_DEMO)

    def test_missing_sources_keep_safety_actions(self):
        items = self.by_id(ma.describe(self.root, risk=None, engine=None, flags=None))
        self.assertTrue(items["kill.engage"]["available"])
        self.assertTrue(items["orders.cancel_all"]["available"])
        self.assertEqual(items["engine.pause"]["reason"], "состояние движка неизвестно")
        self.assertEqual(items["engine.resume"]["reason"], "состояние движка неизвестно")
        for action in ("kill.reset", "breaker.reset.daily", "breaker.reset.global"):
            self.assertFalse(items[action]["available"])
            self.assertIn("не прочитано", items[action]["reason"])

    def test_flags_block_kill_reset_and_repeated_stop(self):
        items = self.by_id(ma.describe(self.root, risk=BLOCKED_RISK, engine={"running": True},
                                       flags={"KILL": True, "STOP_ENGINE": True}))
        self.assertFalse(items["kill.reset"]["available"])
        self.assertIn("data/KILL", items["kill.reset"]["reason"])
        self.assertFalse(items["engine.pause"]["available"])
        self.assertIn("STOP_ENGINE", items["engine.pause"]["reason"])
        self.assertTrue(items["breaker.reset.daily"]["available"])

    def test_engine_script_or_pwsh_missing(self):
        (self.root / "ops" / "engine.ps1").unlink()
        items = self.by_id(ma.describe(self.root, risk=HEALTHY_RISK, engine={"running": True}, flags={}))
        self.assertEqual(items["engine.pause"]["reason"], "нет ops/engine.ps1")
        (self.root / "ops" / "engine.ps1").write_text("", encoding="utf-8")
        with patch.object(ma, "_pwsh_available", return_value=False):
            items = self.by_id(ma.describe(self.root, risk=HEALTHY_RISK, engine={"running": False}, flags={}))
        self.assertIn("pwsh", items["engine.resume"]["reason"])

    def test_current_state_reads_temp_db_without_creating_missing(self):
        risk_db, engine, flags = ma.current_state(self.root)
        self.assertIsNone(risk_db)
        self.assertFalse((self.root / "data" / "risk_state.db").exists())
        self.assertEqual(engine, {"running": False})
        self.assertEqual(flags, {"KILL": False, "STOP_ENGINE": False})
        self.risk_db()
        risk.trip_breaker("тест", scope="daily")
        state, _, _ = ma.current_state(self.root)
        self.assertTrue(state["daily_breaker"])
        self.assertFalse(state["kill_active"])

    def test_module_import_is_light(self):
        code = ("import sys, src.morphy_actions; "
                "print(json.dumps(sorted(m for m in ('ccxt', 'src.config', 'src.connector', "
                "'src.control_panel', 'src.risk', 'dotenv') if m in sys.modules)))")
        output = subprocess.run([sys.executable, "-c", "import json; " + code], cwd=PROJECT,
                                capture_output=True, text=True, timeout=60, check=True).stdout
        self.assertEqual(json.loads(output), [])


class RunGuardTests(Base):
    def test_unknown_action_rejected_and_logged_without_echo(self):
        fake = Recorder(self.root)
        result = ma.run(self.root, "engine.reset; rm -rf data", deps=fake.deps)
        self.assertFalse(result["ok"])
        self.assertEqual(result["outcome"], "rejected")
        self.assertEqual(result["action"], "invalid")
        self.assertEqual(fake.calls, [])
        self.assertEqual(self.audit()[-1]["action"], "invalid")

    def test_live_mode_rejected_before_any_dependency(self):
        fake = Recorder(self.root)
        result = ma.run(self.root, "kill.engage", mode="live", deps=fake.deps)
        self.assertEqual(result["outcome"], "rejected")
        self.assertIn("demo", result["summary"])
        self.assertEqual(fake.calls, [])

    def test_live_environment_rejected(self):
        fake = Recorder(self.root, env_mode="live")
        result = ma.run(self.root, "orders.cancel_all", deps=fake.deps)
        self.assertEqual(result["outcome"], "rejected")
        self.assertEqual(fake.names(), ["env_mode"])

    def test_audit_has_only_contract_fields(self):
        self.risk_db()
        fake = Recorder(self.root)
        ma.run(self.root, "kill.engage", deps=fake.deps)
        ma.run(self.root, "kill.reset", confirm="неверно", deps=fake.deps)
        entries = self.audit()
        self.assertEqual(len(entries), 2)
        for entry in entries:
            self.assertEqual(set(entry), {"ts", "action", "outcome", "via"})
            self.assertEqual(entry["via"], "morphy")
            self.assertIn(entry["outcome"], ma.OUTCOMES)
        self.assertEqual([e["outcome"] for e in entries], ["ok", "rejected"])
        raw = (self.root / "logs" / "control-panel-actions.jsonl").read_text(encoding="utf-8")
        self.assertNotIn("неверно", raw)

    def test_default_deps_are_rooted_and_demo(self):
        deps = ma.default_deps(self.root)
        self.assertEqual(deps.risk_db, self.root / "data" / "risk_state.db")
        self.assertEqual(deps.kill_flag, self.root / "data" / "KILL")
        self.assertFalse(deps.engine_running())          # нет data/engine.pid


class KillAndCancelTests(Base):
    def test_kill_engages_flag_and_cancels_with_bots(self):
        self.risk_db()
        fake = Recorder(self.root, report={"cancelled": ["o1", "bot1"], "failed": [], "bots_stopped": ["bot1"]})
        result = ma.run(self.root, "kill.engage", deps=fake.deps)
        self.assertTrue(result["ok"], result)
        self.assertEqual(result["outcome"], "ok")
        self.assertIn(("emergency_stop", "fake-exchange", True), fake.calls)
        self.assertEqual(result["details"], {"cancelled": 2, "failed": 0, "bots_stopped": 1})
        risk.init(self.root / "data" / "risk_state.db")
        self.assertTrue(risk.status()["kill_active"])
        self.assert_clean(result)

    def test_kill_without_exchange_still_blocks_entries(self):
        self.risk_db()
        fake = Recorder(self.root, exchange_error=RuntimeError(SECRET))
        result = ma.run(self.root, "kill.engage", deps=fake.deps)
        self.assertFalse(result["ok"])
        self.assertEqual(result["outcome"], "failed")
        self.assertIn("биржа demo недоступна", result["summary"])
        self.assertNotIn("emergency_stop", fake.names())
        risk.init(self.root / "data" / "risk_state.db")
        self.assertTrue(risk.status()["kill_active"])
        self.assert_clean(result)

    def test_kill_partial_failure_is_reported(self):
        self.risk_db()
        fake = Recorder(self.root, report={"cancelled": ["o1"], "failed": ["o2", SECRET]})
        result = ma.run(self.root, "kill.engage", deps=fake.deps)
        self.assertFalse(result["ok"])
        self.assertIn("не отменено 2", result["summary"])
        self.assert_clean(result)

    def test_cancel_all_does_not_touch_kill_or_bots(self):
        self.risk_db()
        fake = Recorder(self.root)
        result = ma.run(self.root, "orders.cancel_all", deps=fake.deps)
        self.assertTrue(result["ok"], result)
        self.assertIn(("emergency_stop", "fake-exchange", False), fake.calls)
        self.assertIn("без стопов", result["summary"])
        risk.init(self.root / "data" / "risk_state.db")
        self.assertFalse(risk.status()["kill_active"])

    def test_cancel_all_exchange_error_hidden(self):
        fake = Recorder(self.root, exchange_error=OSError(SECRET))
        result = ma.run(self.root, "orders.cancel_all", deps=fake.deps)
        self.assertEqual(result["outcome"], "failed")
        self.assert_clean(result)

    def test_unexpected_exception_hidden(self):
        fake = Recorder(self.root)
        fake.deps.emergency_stop = lambda ex, include_bots: (_ for _ in ()).throw(ValueError(SECRET))
        result = ma.run(self.root, "orders.cancel_all", deps=fake.deps)
        self.assertEqual(result["outcome"], "failed")
        self.assertIn("ValueError", result["summary"])
        self.assert_clean(result)


class EngineTests(Base):
    def test_pause_only_when_running(self):
        fake = Recorder(self.root, running=False)
        result = ma.run(self.root, "engine.pause", deps=fake.deps)
        self.assertEqual(result["outcome"], "rejected")
        self.assertNotIn("engine_script", fake.names())

        fake = Recorder(self.root, running=True)
        result = ma.run(self.root, "engine.pause", deps=fake.deps)
        self.assertTrue(result["ok"], result)
        self.assertIn(("engine_script", "stop"), fake.calls)
        self.assert_clean(result)

    def test_pause_rejected_when_stop_already_requested(self):
        (self.root / "data" / "STOP_ENGINE").write_text("x", encoding="utf-8")
        fake = Recorder(self.root, running=True)
        result = ma.run(self.root, "engine.pause", deps=fake.deps)
        self.assertEqual(result["outcome"], "rejected")
        self.assertNotIn("engine_script", fake.names())

    def test_resume_only_when_stopped(self):
        fake = Recorder(self.root, running=True)
        self.assertEqual(ma.run(self.root, "engine.resume", deps=fake.deps)["outcome"], "rejected")
        self.assertNotIn("engine_script", fake.names())
        fake = Recorder(self.root, running=False)
        result = ma.run(self.root, "engine.resume", deps=fake.deps)
        self.assertTrue(result["ok"])
        self.assertIn(("engine_script", "start"), fake.calls)

    def test_unknown_engine_state_rejected(self):
        fake = Recorder(self.root, running=None)
        for action in ("engine.pause", "engine.resume"):
            self.assertEqual(ma.run(self.root, action, deps=fake.deps)["outcome"], "rejected")
        self.assertNotIn("engine_script", fake.names())

    def test_script_failure_returns_code_without_output(self):
        fake = Recorder(self.root, running=True,
                        script_result={"ok": False, "exit_code": 3, "output": SECRET})
        result = ma.run(self.root, "engine.pause", deps=fake.deps)
        self.assertEqual(result["outcome"], "failed")
        self.assertIn("кодом 3", result["summary"])
        self.assertEqual(result["details"], {"exit_code": 3})
        self.assert_clean(result)

    def test_script_timeout(self):
        fake = Recorder(self.root, running=True)
        fake.deps.engine_script = lambda op: (_ for _ in ()).throw(subprocess.TimeoutExpired("pwsh", 100, output=SECRET.encode()))
        result = ma.run(self.root, "engine.pause", deps=fake.deps)
        self.assertEqual(result["outcome"], "failed")
        self.assertIn("не завершился", result["summary"])
        self.assert_clean(result)

    def test_missing_script_rejected_before_call(self):
        (self.root / "ops" / "engine.ps1").unlink()
        fake = Recorder(self.root, running=True)
        result = ma.run(self.root, "engine.pause", deps=fake.deps)
        self.assertEqual(result["outcome"], "rejected")
        self.assertNotIn("engine_script", fake.names())


class ResetTests(Base):
    def events(self) -> list[tuple[str, str]]:
        from contextlib import closing
        import sqlite3
        with closing(sqlite3.connect(self.root / "data" / "risk_state.db")) as conn:
            return conn.execute("SELECT event, detail FROM risk_events ORDER BY id").fetchall()

    def test_phrase_required_and_checked(self):
        self.risk_db()
        risk.kill_switch(by="тест")
        fake = Recorder(self.root)
        for confirm in (None, "", "снять kill", "СНЯТЬ DAILY", "СНЯТЬ KILL "):
            result = ma.run(self.root, "kill.reset", confirm=confirm, deps=fake.deps)
            self.assertEqual(result["outcome"], "rejected", confirm)
        risk.init(self.root / "data" / "risk_state.db")
        self.assertTrue(risk.status()["kill_active"])

    def test_reset_only_when_block_active(self):
        self.risk_db()
        fake = Recorder(self.root)
        for action, (scope, _) in ma.RESETS.items():
            result = ma.run(self.root, action, confirm=ma.SPECS[action]["confirm_phrase"], deps=fake.deps)
            self.assertEqual(result["outcome"], "rejected", action)
            self.assertIn("не активен", result["summary"])
        self.assertFalse(any(event.startswith("breaker_reset") for event, _ in self.events()))

    def test_resets_clear_active_blocks_with_morphy_marker(self):
        self.risk_db()
        risk.kill_switch(by="тест")
        risk.trip_breaker("тест", scope="daily")
        risk.trip_breaker("тест", scope="global")
        fake = Recorder(self.root)
        for action in ("breaker.reset.daily", "breaker.reset.global", "kill.reset"):
            result = ma.run(self.root, action, confirm=ma.SPECS[action]["confirm_phrase"], deps=fake.deps)
            self.assertTrue(result["ok"], result)
            self.assertEqual(result["details"], {"scope": ma.RESETS[action][0]})
        risk.init(self.root / "data" / "risk_state.db")
        status = risk.status()
        self.assertFalse(status["kill_active"] or status["daily_breaker"] or status["global_breaker"])
        resets = [(event, detail) for event, detail in self.events() if event.startswith("breaker_reset")]
        self.assertEqual([event for event, _ in resets],
                         ["breaker_reset_daily", "breaker_reset_global", "breaker_reset_kill"])
        self.assertTrue(all(detail == "by=morphy-ui" for _, detail in resets))
        self.assertNotIn("exchange", fake.names())

    def test_kill_reset_refused_while_kill_flag_pending(self):
        self.risk_db()
        risk.kill_switch(by="тест")
        (self.root / "data" / "KILL").write_text("причина", encoding="utf-8")
        result = ma.run(self.root, "kill.reset", confirm="СНЯТЬ KILL", deps=Recorder(self.root).deps)
        self.assertEqual(result["outcome"], "rejected")
        self.assertIn("data/KILL", result["summary"])
        risk.init(self.root / "data" / "risk_state.db")
        self.assertTrue(risk.status()["kill_active"])

    def test_missing_risk_db_not_created(self):
        result = ma.run(self.root, "breaker.reset.global", confirm="СНЯТЬ GLOBAL",
                        deps=Recorder(self.root).deps)
        self.assertEqual(result["outcome"], "rejected")
        self.assertFalse((self.root / "data" / "risk_state.db").exists())


class CliTests(Base):
    def call(self, argv, env=None):
        stdout = io.StringIO()
        with patch.dict(os.environ, env or {}, clear=False), redirect_stdout(stdout):
            code = ma.main(argv)
        return code, json.loads(stdout.getvalue())

    def test_run_reads_phrase_only_from_env_and_sets_exit_code(self):
        seen = {}

        def fake_run(root, action, *, confirm=None, mode="demo", deps=None):
            seen.update(root=root, action=action, confirm=confirm, mode=mode)
            return {"ok": action == "kill.reset", "action": action, "summary": "ok", "outcome": "ok"}

        os.environ.pop(ma.CONFIRM_ENV, None)
        with patch.object(ma, "run", side_effect=fake_run):
            code, payload = self.call(["run", "kill.reset"], {ma.CONFIRM_ENV: "СНЯТЬ KILL"})
            self.assertEqual((code, payload["ok"]), (0, True))
            self.assertEqual(seen["confirm"], "СНЯТЬ KILL")
            self.assertEqual(seen["root"], ma.ROOT)
            os.environ.pop(ma.CONFIRM_ENV, None)
            code, payload = self.call(["run", "engine.pause", "--mode", "live"])
            self.assertEqual(code, 1)
            self.assertIsNone(seen["confirm"])
            self.assertEqual(seen["mode"], "live")

    def test_describe_cli_prints_controls(self):
        with patch.object(ma, "describe_current", return_value={"mode": "demo", "enabled": True, "actions": []}):
            code, payload = self.call(["describe"])
        self.assertEqual(code, 0)
        self.assertTrue(payload["enabled"])
        with patch.object(ma, "describe_current", side_effect=RuntimeError(SECRET)):
            code, payload = self.call(["describe"])
        self.assertFalse(payload["enabled"])
        self.assertNotIn("do-not-expose", json.dumps(payload))


class ContractSyncTests(unittest.TestCase):
    def test_backend_action_table_matches_python_specs(self):
        source = (PROJECT / "ops" / "morphy" / "project-backend.ts").read_text(encoding="utf-8")
        table = dict((m.group(1), (int(m.group(2)), None if m.group(3) == "null" else m.group(3).strip("'")))
                     for m in re.finditer(r"'([a-z_.]+)':\s*\{\s*holdMs:\s*(\d+),\s*phrase:\s*(null|'[^']*')", source))
        expected = {action: (spec["hold_ms"], spec["confirm_phrase"]) for action, spec in ma.SPECS.items()}
        self.assertEqual(table, expected)
        self.assertIn(ma.CONFIRM_ENV, source)


if __name__ == "__main__":
    unittest.main()
