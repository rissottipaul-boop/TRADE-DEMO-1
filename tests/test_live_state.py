"""Раздельное состояние demo и live (задача LIVE-STATE-SPLIT, insights/business-plan.md §3).

Инварианты:
- demo-пути не меняются: их использует работающий движок P1-72H;
- live-состояние лежит в data/live/ и не видит demo-HWM (иначе $1k equity
  кармана выглядела бы просадкой −99% от demo-HWM ~104k и сработал бы breaker);
- `src.ops kill --mode live` блокирует live-входы даже без live-ключей;
- у `src.ops` нет опций перед командой — правило guard для `src.ops reset`.
"""
import contextlib
import io
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from src import config, ops, risk, storage

LIVE_KEYS = ("OKX_API_KEY", "OKX_SECRET", "OKX_PASSPHRASE")


class StatePathsTest(unittest.TestCase):
    def test_demo_paths_unchanged(self):
        paths = config.state_paths("demo")
        self.assertEqual(paths.risk_db, risk._DB_PATH)
        self.assertEqual(paths.bot_db, storage.DB_PATH)
        self.assertEqual(paths.kill_flag, Path("data/KILL"))  # engine.py: KILL_FLAG

    def test_live_paths_are_separate(self):
        demo, live = config.state_paths("demo"), config.state_paths("live")
        self.assertEqual(live.root, Path("data/live"))
        for name in ("risk_db", "bot_db", "kill_flag"):
            self.assertNotEqual(getattr(demo, name), getattr(live, name), name)

    def test_mode_validation(self):
        self.assertEqual(config.normalize_mode(" LIVE "), "live")
        with self.assertRaises(ValueError):
            config.data_dir("prod")


class _TempDataRoot(unittest.TestCase):
    """DATA_ROOT во временном каталоге; риск-ядро отпускается до очистки."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.root = Path(self._tmp.name)
        patcher = mock.patch.object(config, "DATA_ROOT", self.root)
        patcher.start()
        self.addCleanup(patcher.stop)

    def tearDown(self):
        risk.init(self.root / "unused.db")
        self._tmp.cleanup()


class RiskSeparationTest(_TempDataRoot):
    def test_live_does_not_see_demo_hwm(self):
        risk.init(config.state_paths("demo").risk_db)
        risk.update_equity(104_000)

        risk.init(config.state_paths("live").risk_db)
        events = risk.update_equity(1_000)
        live = risk.status()
        self.assertEqual(events, [])
        self.assertFalse(live["global_breaker"])
        self.assertEqual(live["hwm"], 1_000)

        risk.init(config.state_paths("demo").risk_db)
        self.assertEqual(risk.status()["hwm"], 104_000)


class OpsModeTest(_TempDataRoot):
    def _run(self, argv):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = ops.main(argv)
        return code, out.getvalue()

    def test_status_uses_live_paths(self):
        code, out = self._run(["status", "--mode", "live"])
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(out)["mode"], "live")
        self.assertTrue((self.root / "live" / "risk_state.db").exists())
        self.assertFalse((self.root / "risk_state.db").exists(), "demo-состояние тронуто")

    def test_mode_before_command_is_rejected(self):
        with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
            ops.main(["--mode", "live", "status"])

    def test_live_kill_blocks_entries_without_keys(self):
        env = {k: v for k, v in os.environ.items() if k not in LIVE_KEYS}
        with mock.patch.dict(os.environ, env, clear=True):
            code, out = self._run(["kill", "--mode", "live", "test"])
        self.assertEqual(code, 1, "без ключей отмена ордеров невозможна — это failed")
        self.assertTrue(json.loads(out)["failed"])
        risk.init(config.state_paths("live").risk_db)
        self.assertTrue(risk.status()["kill_active"])
        allowed, _ = risk.check_entry_allowed("BTC/USDT", "buy")
        self.assertFalse(allowed)
        self.assertFalse((self.root / "risk_state.db").exists(), "demo-состояние тронуто")


class LoadSettingsModeTest(unittest.TestCase):
    def test_explicit_live_mode(self):
        fake = {"OKX_MODE": "demo", "OKX_API_KEY": "k-live", "OKX_SECRET": "s-live",
                "OKX_PASSPHRASE": "p-live"}
        with mock.patch.dict(os.environ, fake):
            settings = config.load_settings("live")
        self.assertEqual(settings.mode, "live")
        self.assertFalse(settings.is_demo)
        self.assertNotIn("s-live", repr(settings))

    def test_missing_live_keys(self):
        env = {k: v for k, v in os.environ.items() if k not in LIVE_KEYS}
        with mock.patch.dict(os.environ, env, clear=True), self.assertRaises(RuntimeError):
            config.load_settings("live")


if __name__ == "__main__":
    unittest.main()
