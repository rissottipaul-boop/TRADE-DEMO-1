"""Тесты снимка для пульта Obsidian (OBSIDIAN-STATUS). Без сети; настоящий движок не трогается:
процесс проверяется на фейке kernel32, базы — во временном каталоге."""
import ctypes
import json
import os
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from src import obsidian_status as st

NOW = 1790300000.0      # 2026-09-25 ~06:33 +05:00

ENGINE_KEYS = {"running", "pid", "started_at", "uptime_h", "p1_deadline", "p1_hours_left",
               "p1_progress_pct", "reconciles", "divergences", "errors", "errors_exchange",
               "errors_internal", "pauses", "ws_public_reconnects", "ws_private_reconnects",
               "stats_saved_at", "stats_age_s"}
RISK_KEYS = {"equity", "hwm", "drawdown_pct", "day_pnl", "day_start_equity", "day_pnl_pct",
             "daily_limit_pct", "global_dd_limit_pct", "daily_breaker", "global_breaker",
             "kill_active", "equity_age_s", "entries_today", "max_entries_per_day",
             "portfolio_heat_pct", "max_heat_pct"}
PUMP_KEYS = {"entry_allowed", "budget_total", "budget_free", "in_positions", "day_pnl", "day_limit",
             "drawdown", "drawdown_limit", "open_positions", "blocks"}
OPS_KEYS = {"live", "live_pocket", "autostart_off", "watch", "engine_log_age_s", "guard"}
LIVE_KEYS = {"enabled", "until", "open", "hours_left"}
POCKET_KEYS = {"exists", "example", "budget_usdt", "sleeves"}
WATCH_KEYS = {"log_exists", "last_line", "last_age_s"}
GUARD_KEYS = {"denies_24h", "errors_24h", "last_deny_at"}

RISK_STATUS = {"equity": 104000.123, "hwm": 105000.0, "drawdown_pct": -0.9522, "day_pnl": -52.0,
               "day_start_equity": 104000.0, "equity_age_s": 42.34, "entries_today": 3,
               "portfolio_heat_pct": 1.5, "daily_breaker": False, "global_breaker": False,
               "kill_active": True}
RISK_LIMITS = {"daily_limit_pct": 6.0, "global_dd_limit_pct": 15.0, "max_entries_per_day": 10,
               "max_heat_pct": 6.0}
PUMP_REPORT = {
    "budget": {"budget_usdt": 5000.0, "free_usdt": 3971.41, "in_positions_usdt": 1028.59},
    "day": {"pnl": 11.1, "loss_limit": 250.0},
    "drawdown": {"current_usdt": 0.0, "limit_usdt": 750.0, "max_usdt": 3.0},
    "positions": {"list": [{"pair": "FET-USDT", "size": 2344, "cost_usdt": 514.3, "stop": 0.214,
                            "trade_id": "x", "risk_usdt": 13.7}]},
    "entry": {"allowed": False, "blocks": ["открыто позиций 1/1"]},
}


def make_bot_db(path: Path, stats=None, equity=()):
    with sqlite3.connect(path) as conn:
        conn.execute("CREATE TABLE ws_state (key TEXT PRIMARY KEY, value TEXT, update_time REAL)")
        conn.execute("CREATE TABLE equity_curve (ts REAL PRIMARY KEY, total_eq REAL NOT NULL, "
                     "avail_eq REAL NOT NULL, upl REAL DEFAULT 0)")
        if stats is not None:
            conn.execute("INSERT INTO ws_state VALUES ('engine_stats', ?, ?)", (json.dumps(stats), NOW - 30))
        conn.executemany("INSERT INTO equity_curve VALUES (?, ?, ?, 0)", [(t, e, e) for t, e in equity])
    conn.close()


class FakeKernel32:
    """Фейк kernel32: OpenProcess отдаёт handle или 0, GetExitCodeProcess — заданный код."""

    def __init__(self, handle=77, exit_code=st.STILL_ACTIVE, ok=True):
        self.handle, self.exit_code, self.ok = handle, exit_code, ok
        self.opened, self.closed = [], []

    def OpenProcess(self, access, inherit, pid):
        self.opened.append((access, inherit, pid))
        return self.handle

    def GetExitCodeProcess(self, handle, ref):
        ref._obj.value = self.exit_code
        return self.ok

    def CloseHandle(self, handle):
        self.closed.append(handle)
        return True


class TmpCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self._tmp.name)
        self.data = self.dir / "data"
        self.data.mkdir()

    def tearDown(self):
        self._tmp.cleanup()

    def snapshot(self, **kw):
        kw.setdefault("alive", lambda pid: True)
        kw.setdefault("risk_reader", lambda path: (dict(RISK_STATUS), dict(RISK_LIMITS)))
        kw.setdefault("pump_reader", lambda journal, pocket, now: PUMP_REPORT)
        kw.setdefault("project_root", self.dir)
        return st.build_snapshot(NOW, data_dir=self.data, pocket_path=self.dir / "pocket.json",
                                 journal_path=self.data / "journal.jsonl", **kw)

    def write_ops(self, policy=None, pocket=None, watch_lines=(), guard_lines=(),
                  autostart_off=False, engine_age_s=None):
        (self.dir / "ops").mkdir(exist_ok=True)
        if policy is not None:
            (self.dir / "ops" / "live-policy.json").write_text(json.dumps(policy), encoding="utf-8")
        if pocket is not None:
            (self.dir / "ops" / "live-pocket.json").write_text(json.dumps(pocket), encoding="utf-8")
        logs = self.dir / "logs"
        logs.mkdir(exist_ok=True)
        if watch_lines:
            (logs / "autostart.log").write_text("\n".join(watch_lines) + "\n", encoding="utf-8")
        if engine_age_s is not None:
            eng = logs / "engine.log"
            eng.write_text("x\n", encoding="utf-8")
            os.utime(eng, (NOW - engine_age_s, NOW - engine_age_s))
        if guard_lines:
            (self.data / "guard.log").write_text("\n".join(guard_lines) + "\n", encoding="utf-8")
        if autostart_off:
            (self.data / "AUTOSTART_OFF").write_text("пауза", encoding="utf-8")

    def guard_line(self, ts, decision="deny"):
        from datetime import datetime, timezone
        iso = datetime.fromtimestamp(ts, timezone.utc).isoformat()
        return json.dumps({"ts": iso, "decision": decision, "tool": "t", "reason": "r"})


class SchemaTest(TmpCase):
    def test_all_sections_and_keys(self):
        started = NOW - 36 * 3600
        (self.data / "engine.pid").write_text("36888\r\n", encoding="utf-8")
        (self.data / "KILL").write_text("тест", encoding="utf-8")
        stats = {"started_at": started, "reconciles": 39, "divergences": 0, "errors": 1,
                 "errors_exchange": 1, "errors_internal": 0, "pauses": 0, "ws_public_reconnects": 2,
                 "ws_business_reconnects": 0, "ws_private_reconnects": 1, "saved_at": NOW - 10}
        make_bot_db(self.data / "bot_state.db", stats, [(NOW - 3600, 104000.5), (NOW - 60, 104010.0)])
        self.write_ops(policy={"live_enabled": True, "enabled_until": st._iso(NOW + 48 * 3600)},
                       pocket={"pocket": "live-main", "budget_usdt": 1000,
                               "sleeves": {"dca": {"enabled": True}}},
                       watch_lines=[f"{st._iso(NOW - 120)} watch жив: PID 1"],
                       guard_lines=[self.guard_line(NOW - 3600), self.guard_line(NOW - 3600, "error")],
                       engine_age_s=45)
        snap = self.snapshot()

        self.assertEqual(set(snap), {"v", "generated_at", "generated_ts", "engine", "risk", "flags",
                                     "pump", "ops", "equity_history", "errors"})
        self.assertEqual(snap["v"], 2)
        self.assertEqual(snap["generated_ts"], int(NOW))
        self.assertTrue(snap["generated_at"].endswith("+05:00"))
        self.assertEqual(snap["errors"], [])
        self.assertEqual(set(snap["engine"]), ENGINE_KEYS)
        self.assertEqual(set(snap["risk"]), RISK_KEYS)
        self.assertEqual(set(snap["pump"]), PUMP_KEYS)
        self.assertEqual(set(snap["ops"]), OPS_KEYS)

        ops = snap["ops"]
        self.assertEqual(set(ops["live"]), LIVE_KEYS)
        self.assertTrue(ops["live"]["open"])
        self.assertEqual(ops["live"]["hours_left"], 48.0)
        self.assertEqual(set(ops["live_pocket"]), POCKET_KEYS)
        self.assertTrue(ops["live_pocket"]["exists"])
        self.assertFalse(ops["live_pocket"]["example"])
        self.assertEqual(ops["live_pocket"]["budget_usdt"], 1000.0)
        self.assertEqual(ops["live_pocket"]["sleeves"], ["dca"])
        self.assertFalse(ops["autostart_off"])
        self.assertEqual(set(ops["watch"]), WATCH_KEYS)
        self.assertTrue(ops["watch"]["log_exists"])
        self.assertEqual(ops["watch"]["last_age_s"], 120.0)
        self.assertEqual(ops["engine_log_age_s"], 45.0)
        self.assertEqual(set(ops["guard"]), GUARD_KEYS)
        self.assertEqual(ops["guard"]["denies_24h"], 1)
        self.assertEqual(ops["guard"]["errors_24h"], 1)
        self.assertEqual(snap["flags"], {"KILL": True, "STOP_ENGINE": False})

        eng = snap["engine"]
        self.assertTrue(eng["running"])
        self.assertEqual(eng["pid"], 36888)
        self.assertEqual(eng["uptime_h"], 36.0)
        self.assertEqual(eng["reconciles"], 39)
        self.assertEqual(eng["ws_private_reconnects"], 1)
        self.assertEqual(eng["stats_age_s"], 10.0)

        risk = snap["risk"]
        self.assertEqual(risk["equity"], 104000.12)
        self.assertEqual(risk["day_pnl_pct"], -0.05)
        self.assertEqual(risk["daily_limit_pct"], 6.0)
        self.assertTrue(risk["kill_active"])
        self.assertEqual(risk["entries_today"], 3)

        pump = snap["pump"]
        self.assertFalse(pump["entry_allowed"])
        self.assertEqual(pump["budget_free"], 3971.41)
        self.assertEqual(pump["open_positions"],
                         [{"pair": "FET-USDT", "size": 2344, "cost_usdt": 514.3, "stop": 0.214}])
        self.assertEqual(pump["blocks"], ["открыто позиций 1/1"])
        self.assertEqual(snap["equity_history"], [[int(NOW - 3600), 104000.5], [int(NOW - 60), 104010.0]])
        json.dumps(snap, allow_nan=False)      # сериализуется без NaN

    def test_engine_old_code_counters_null(self):
        make_bot_db(self.data / "bot_state.db", {"started_at": NOW - 3600, "reconciles": 1})
        eng = self.snapshot()["engine"]
        self.assertFalse(eng["running"])          # нет engine.pid — движок остановлен
        self.assertIsNone(eng["pid"])
        self.assertIsNone(eng["uptime_h"])        # аптайм только у живого процесса
        self.assertIsNone(eng["errors_exchange"])
        self.assertEqual(eng["stats_saved_at"], NOW - 30)   # нет saved_at — время строки ws_state


class SectionFailureTest(TmpCase):
    def test_failed_sections_are_null_with_errors(self):
        def boom(*a):
            raise RuntimeError("риск недоступен")

        def bad_pump(*a):
            raise ValueError("карман не читается")

        snap = self.snapshot(risk_reader=boom, pump_reader=bad_pump)   # и базы движка нет
        self.assertIsNone(snap["risk"])
        self.assertIsNone(snap["pump"])
        self.assertIsNone(snap["equity_history"])
        self.assertEqual(snap["flags"], {"KILL": False, "STOP_ENGINE": False})
        self.assertIsNotNone(snap["engine"])      # процесс виден и без статистики
        self.assertIsNotNone(snap["ops"])         # ops не падает: части — null, ошибка — в errors
        self.assertFalse(snap["ops"]["live_pocket"]["exists"])
        self.assertFalse(snap["ops"]["watch"]["log_exists"])
        self.assertIsNone(snap["ops"]["guard"]["denies_24h"])
        text = "\n".join(snap["errors"])
        for part in ("risk: RuntimeError: риск недоступен", "pump: ValueError: карман не читается",
                     "equity_history: FileNotFoundError", "engine.stats: FileNotFoundError",
                     "ops.live: FileNotFoundError"):
            self.assertIn(part, text)

    def test_bad_pid_file_gives_null_running(self):
        (self.data / "engine.pid").write_text("abc", encoding="utf-8")
        snap = self.snapshot()
        self.assertIsNone(snap["engine"]["running"])
        self.assertTrue(any(e.startswith("engine.pid: ValueError") for e in snap["errors"]))

    def test_engine_section_crash_is_null(self):
        with mock.patch.object(st, "engine_section", side_effect=RuntimeError("сбой")):
            snap = self.snapshot()
        self.assertIsNone(snap["engine"])
        self.assertIn("engine: RuntimeError: сбой", snap["errors"])

    def test_missing_risk_db_not_created(self):
        with self.assertRaises(FileNotFoundError):
            st.read_risk(self.data / "risk_state.db")
        self.assertFalse((self.data / "risk_state.db").exists())

    def test_ro_connection_does_not_create_db(self):
        with self.assertRaises(FileNotFoundError):
            st.read_equity_history(self.data / "bot_state.db", NOW)
        self.assertFalse((self.data / "bot_state.db").exists())


class HistoryTest(TmpCase):
    def test_thin_keeps_ends_and_limit(self):
        pts = [[i, float(i)] for i in range(1000)]
        out = st.thin(pts, 300)
        self.assertEqual(len(out), 300)
        self.assertEqual(out[0], pts[0])
        self.assertEqual(out[-1], pts[-1])
        self.assertEqual([p[0] for p in out], sorted({p[0] for p in out}))   # по возрастанию, без повторов
        self.assertEqual(st.thin(pts[:5], 300), pts[:5])

    def test_history_window_48h_and_thinned(self):
        rows = [(NOW - 72 * 3600 + i * 60, 100000.0 + i) for i in range(72 * 60)]   # раз в минуту, 72 ч
        make_bot_db(self.data / "bot_state.db", None, rows)
        hist = st.read_equity_history(self.data / "bot_state.db", NOW)
        self.assertLessEqual(len(hist), 300)
        self.assertGreaterEqual(hist[0][0], NOW - 48 * 3600)
        self.assertEqual(hist[-1][0], int(rows[-1][0]))


class P1DeadlineTest(TmpCase):
    def engine(self, started, running=True):
        (self.data / "engine.pid").write_text("1234", encoding="utf-8")
        make_bot_db(self.data / "bot_state.db", {"started_at": started, "saved_at": NOW})
        return st.engine_section(self.data, NOW, [], alive=lambda pid: running)

    def test_mid_run(self):
        eng = self.engine(NOW - 18 * 3600)
        self.assertEqual(eng["p1_hours_left"], 54.0)
        self.assertEqual(eng["p1_progress_pct"], 25.0)
        self.assertEqual(eng["p1_deadline"], st._iso(NOW + 54 * 3600))
        self.assertTrue(eng["p1_deadline"].endswith("+05:00"))

    def test_after_deadline_clamped(self):
        eng = self.engine(NOW - 80 * 3600)
        self.assertEqual(eng["p1_hours_left"], 0.0)
        self.assertEqual(eng["p1_progress_pct"], 100.0)

    def test_dead_process_no_uptime(self):
        eng = self.engine(NOW - 3600, running=False)
        self.assertFalse(eng["running"])
        self.assertEqual(eng["pid"], 1234)
        self.assertIsNone(eng["uptime_h"])
        self.assertEqual(eng["p1_hours_left"], 71.0)


class ProcessCheckTest(unittest.TestCase):
    """Проверка процесса только на фейках: настоящий PID не используется."""

    def test_windows_alive_and_never_os_kill(self):
        k32 = FakeKernel32(exit_code=st.STILL_ACTIVE)
        with mock.patch.object(st.os, "kill", side_effect=AssertionError("os.kill на Windows")) as kill:
            self.assertTrue(st.process_alive(4242, os_name="nt", kernel32=k32, get_last_error=lambda: 0))
        kill.assert_not_called()
        self.assertEqual(k32.opened, [(st.PROCESS_QUERY_LIMITED_INFORMATION, False, 4242)])
        self.assertEqual(k32.closed, [77])          # handle закрыт

    def test_windows_exited_process(self):
        k32 = FakeKernel32(exit_code=0)
        self.assertFalse(st.process_alive(4242, os_name="nt", kernel32=k32, get_last_error=lambda: 0))
        self.assertEqual(k32.closed, [77])

    def test_windows_no_such_pid_and_access_denied(self):
        self.assertFalse(st.process_alive(4242, os_name="nt", kernel32=FakeKernel32(handle=0),
                                          get_last_error=lambda: 87))
        self.assertTrue(st.process_alive(4242, os_name="nt", kernel32=FakeKernel32(handle=0),
                                         get_last_error=lambda: st.ERROR_ACCESS_DENIED))

    def test_windows_exit_code_failure_raises(self):
        k32 = FakeKernel32(ok=False)
        with self.assertRaises(OSError):
            st.process_alive(4242, os_name="nt", kernel32=k32, get_last_error=lambda: 6)
        self.assertEqual(k32.closed, [77])

    def test_posix_uses_signal_zero(self):
        with mock.patch.object(st.os, "kill") as kill:
            self.assertTrue(st.process_alive(4242, os_name="posix"))
        kill.assert_called_once_with(4242, 0)
        with mock.patch.object(st.os, "kill", side_effect=ProcessLookupError):
            self.assertFalse(st.process_alive(4242, os_name="posix"))

    def test_nonpositive_pid(self):
        with mock.patch.object(st.os, "kill") as kill:
            self.assertFalse(st.process_alive(0, os_name="posix"))
        kill.assert_not_called()

    def test_fake_matches_ctypes_byref(self):
        # Фейк пишет в ctypes.byref так же, как настоящий GetExitCodeProcess
        code = ctypes.c_ulong()
        FakeKernel32(exit_code=259).GetExitCodeProcess(1, ctypes.byref(code))
        self.assertEqual(code.value, 259)


class OpsTest(TmpCase):
    def test_live_open_and_closed(self):
        self.write_ops(policy={"live_enabled": True, "enabled_until": st._iso(NOW + 10 * 3600)})
        live = self.snapshot()["ops"]["live"]
        self.assertTrue(live["enabled"])
        self.assertTrue(live["open"])
        self.assertEqual(live["hours_left"], 10.0)

        self.write_ops(policy={"live_enabled": True, "enabled_until": st._iso(NOW - 3600)})
        live = self.snapshot()["ops"]["live"]
        self.assertFalse(live["open"])
        self.assertEqual(live["hours_left"], 0.0)

        self.write_ops(policy={"live_enabled": False, "enabled_until": st._iso(NOW + 10 * 3600)})
        live = self.snapshot()["ops"]["live"]
        self.assertFalse(live["enabled"])
        self.assertFalse(live["open"])

    def test_live_no_until_means_no_open(self):
        self.write_ops(policy={"live_enabled": True})
        live = self.snapshot()["ops"]["live"]
        self.assertIsNone(live["until"])
        self.assertIsNone(live["open"])
        self.assertIsNone(live["hours_left"])

    def test_live_broken_file_gives_nulls_with_error(self):
        (self.dir / "ops").mkdir(exist_ok=True)
        (self.dir / "ops" / "live-policy.json").write_text("не json", encoding="utf-8")
        snap = self.snapshot()
        self.assertEqual(snap["ops"]["live"],
                         {"enabled": None, "until": None, "open": None, "hours_left": None})
        self.assertTrue(any(e.startswith("ops.live:") for e in snap["errors"]))

    def test_pocket_example_and_sleeves(self):
        self.write_ops(pocket={"example": True, "budget_usdt": 1000, "sleeves": {"dca": {}}})
        pocket = self.snapshot()["ops"]["live_pocket"]
        self.assertTrue(pocket["exists"])
        self.assertTrue(pocket["example"])

        self.write_ops(pocket={"budget_usdt": 500, "sleeves": ["не словарь"]})
        pocket = self.snapshot()["ops"]["live_pocket"]
        self.assertEqual(pocket["sleeves"], [])
        self.assertEqual(pocket["budget_usdt"], 500.0)

    def test_watch_off_and_stale(self):
        self.write_ops(watch_lines=[f"{st._iso(NOW - 3600)} watch жив: PID 9"],
                       autostart_off=True, engine_age_s=400)
        ops = self.snapshot()["ops"]
        self.assertTrue(ops["autostart_off"])
        self.assertEqual(ops["watch"]["last_age_s"], 3600.0)
        self.assertEqual(ops["engine_log_age_s"], 400.0)

    def test_watch_no_logs(self):
        ops = self.snapshot()["ops"]
        self.assertFalse(ops["autostart_off"])
        self.assertFalse(ops["watch"]["log_exists"])
        self.assertIsNone(ops["watch"]["last_line"])
        self.assertIsNone(ops["engine_log_age_s"])

    def test_guard_window_and_garbage(self):
        self.write_ops(guard_lines=[
            self.guard_line(NOW - 3600),
            self.guard_line(NOW - 3600, "allow"),
            self.guard_line(NOW - 25 * 3600),          # старше суток — не считается
            self.guard_line(NOW - 7200, "error"),
            "не json вообще",
            self.guard_line(NOW - 60),
        ])
        guard = self.snapshot()["ops"]["guard"]
        self.assertEqual(guard["denies_24h"], 2)
        self.assertEqual(guard["errors_24h"], 1)
        self.assertEqual(guard["last_deny_at"], st._iso(NOW - 60))

    def test_parse_ts_tolerant(self):
        self.assertIsNone(st._parse_ts(None))
        self.assertIsNone(st._parse_ts(""))
        self.assertIsNone(st._parse_ts("вчера"))
        self.assertIsNone(st._parse_ts("2026-09-30 05:00"))   # без зоны
        self.assertEqual(st._parse_ts("2026-09-30T00:00:00Z"),
                         st._parse_ts("2026-09-30T00:00:00+00:00"))

    def test_tail_lines_missing_file(self):
        self.assertEqual(st._tail_lines(self.dir / "нет-такого.log"), [])


class WriteTest(TmpCase):
    def test_atomic_write_creates_dir_and_replaces(self):
        out = self.dir / "obsidian" / "status.json"
        st.write_atomic(out, {"v": 1, "текст": "да"})
        st.write_atomic(out, {"v": 1, "n": 2})
        self.assertEqual(json.loads(out.read_text(encoding="utf-8")), {"v": 1, "n": 2})
        self.assertEqual(os.listdir(out.parent), ["status.json"])   # временных файлов не осталось

    def test_failed_replace_keeps_old_file_and_cleans_tmp(self):
        out = self.dir / "status.json"
        st.write_atomic(out, {"v": 1, "old": True})
        with mock.patch.object(st.os, "replace", side_effect=PermissionError("занят")), \
                mock.patch.object(st.time, "sleep"):
            with self.assertRaises(PermissionError):
                st.write_atomic(out, {"v": 1, "old": False})
        self.assertEqual(json.loads(out.read_text(encoding="utf-8")), {"v": 1, "old": True})
        self.assertEqual(sorted(os.listdir(self.dir)), ["data", "status.json"])

    def test_main_exit_codes(self):
        out = self.dir / "o" / "status.json"
        snap = {"v": 1, "errors": ["risk: x"]}
        with mock.patch.object(st, "build_snapshot", return_value=snap), \
                mock.patch("sys.stdout"):
            self.assertEqual(st.main(["--out", str(out)]), 0)
        self.assertEqual(json.loads(out.read_text(encoding="utf-8")), snap)
        with mock.patch.object(st, "build_snapshot", return_value=snap), \
                mock.patch.object(st, "write_atomic", side_effect=OSError("диск")), \
                mock.patch("sys.stderr"):
            self.assertEqual(st.main(["--out", str(out)]), 2)


if __name__ == "__main__":
    unittest.main()
