"""Бэктестер: CLI baseline end-to-end на синтетическом снапшоте (без сети).

Проверяется то, что обещает отчёт: разделы §6.1/§6.2/§1.2 собираются, holdout не тронут,
файловая SQLite — только market_data (data/risk_state.db не открывается), снапшот
--until воспроизводит отчёт бит-в-бит после докачки новых свечей.
"""
import io
import math
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from src.backtest import __main__ as cli
from src.backtest.data import InstrumentSpec, MarketDataStore, ms_to_iso
from src.backtest.walkforward import WalkForwardConfig

H = 3_600_000
T0 = 1_767_225_600_000  # 2026-01-01 00:00 UTC
N = 60 * 24


def candle_rows(n, phase=0.0):
    rows, prev = [], 100.0
    for i in range(n):
        c = (100 + 10 * math.sin(i / 25.0 + phase) + 2 * math.sin(i / 4.0)
             + 0.3 * math.sin(i * 1.7))
        rows.append([str(T0 + i * H), repr(prev), repr(max(prev, c) + 0.2),
                     repr(min(prev, c) - 0.2), repr(c), "1", "1", "1", "1"])
        prev = c
    return rows


class BaselineCliTest(unittest.TestCase):
    INST = ("AAA-USDT", "BBB-USDT")

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(self._tmp.cleanup)
        self.dir = Path(self._tmp.name)
        self.db = self.dir / "md.db"
        self.store = MarketDataStore(self.db)
        for k, inst in enumerate(self.INST):
            self.store.upsert_candles(inst, "1H", candle_rows(N, phase=k), now_ms=T0 + N * H)
            self.store.save_instrument(InstrumentSpec(inst, 0.01, 1e-6, 1e-4))
        small = WalkForwardConfig(train_days=20, valid_days=5, test_days=5, step_days=5,
                                  holdout_days=10, top_k=2)
        for name, value in (("WF_CONFIG", small),
                            ("SMA_GRID", [{"fast": 5, "slow": 20}, {"fast": 10, "slow": 30}]),
                            ("SMA_DEFAULT", {"fast": 10, "slow": 30}),
                            ("LA_PARAMS", [{"fast": 5, "slow": 20}])):
            p = mock.patch.object(cli, name, value)
            p.start()
            self.addCleanup(p.stop)

    def _baseline(self, out, *extra, inst=INST):
        args = cli.build_parser().parse_args(
            ["--db", str(self.db), "baseline", "--inst", *inst, "--since", "2026-01-01",
             "--no-download", "--la-points", "4", "--rc-offsets", "100,300", "--out", str(out),
             *extra])
        return args.func(args)

    def test_report_sections_holdout_untouched_no_state_db(self):
        orig, connects = sqlite3.connect, []

        def spy(database, *a, **k):
            connects.append(str(database))
            return orig(database, *a, **k)

        out = self.dir / "report.md"
        with mock.patch("sqlite3.connect", side_effect=spy):
            summaries = self._baseline(out)
        text = out.read_text(encoding="utf-8")
        for needle in ("## Вывод", "## Данные", "## Сводка", "### In-sample: метрики §6.1",
                       "### Чувствительность к издержкам", "### Walk-forward SMA-cross",
                       "Число испытаний (trials)", "### Гейт приёмки §6.2",
                       "### Anti-lookahead на реальных данных", "**не тронут**",
                       "--until 2026-03-02T00:00 --no-download"):
            self.assertIn(needle, text)
        self.assertNotIn("### Holdout", text)
        for s in summaries:
            self.assertTrue(s["lookahead"] and s["recursive"], s["inst"])
            self.assertGreaterEqual(s["windows"], 3)
            self.assertEqual(len(s["gate"]["checks"]), 8)
            self.assertIn(f"`{s['dataset_id']}`", text)
            self.assertEqual(s["holdout_start"], ms_to_iso(T0 + N * H - 10 * 24 * H))
        with self.store._conn() as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM holdout_touches").fetchone()[0], 0)
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM datasets").fetchone()[0], 2)
        self.assertEqual({c for c in connects if c != ":memory:"}, {str(self.db)})

    def test_until_reproduces_report_after_new_candles(self):
        until = ms_to_iso(T0 + N * H).replace(" ", "T")
        r1, r2, r3 = (self.dir / f"r{i}.md" for i in (1, 2, 3))
        s1 = self._baseline(r1, "--until", until, inst=self.INST[:1])
        more = candle_rows(N + 48)[N:]  # продолжение того же ряда
        self.store.upsert_candles(self.INST[0], "1H", more, now_ms=T0 + (N + 48) * H)
        s2 = self._baseline(r2, "--until", until, inst=self.INST[:1])

        def body(path):  # всё, кроме времени прогона и пути --out в команде воспроизведения
            return [line for line in path.read_text(encoding="utf-8").splitlines()
                    if not line.startswith(("- **Дата:**", "- **Время прогона:**",
                                            "- **Воспроизведение:**"))]

        self.assertIn(f"--until {until} --no-download", r2.read_text(encoding="utf-8"))
        self.assertEqual(s1[0]["dataset_id"], s2[0]["dataset_id"])
        self.assertEqual(body(r1), body(r2))
        s3 = self._baseline(r3, inst=self.INST[:1])  # без --until: снапшот уже другой
        self.assertNotEqual(s3[0]["dataset_id"], s1[0]["dataset_id"])

    def test_bad_offsets_rejected(self):
        for bad in ("0,x", "0,100"):
            with self.assertRaises(SystemExit), mock.patch("sys.stderr", io.StringIO()):
                cli.build_parser().parse_args(["baseline", "--rc-offsets", bad])


if __name__ == "__main__":
    unittest.main()
