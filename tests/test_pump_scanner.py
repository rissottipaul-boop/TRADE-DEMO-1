"""Памп-сканер PUMP-CODIFY + ликвидность кандидата PUMP-LIQ (src/pump_scanner.py): без сети.

«Биржа» — фейковый публичный REST OKX в памяти с семантикой candles/history-candles:
новые свечи первыми, первая строка — формирующаяся (confirm=0), after — строго старше ts;
books отдаёт снимок стакана (по умолчанию DEFAULT_BOOK — глубокий и узкий, тестам без
интереса к ликвидности не мешает). Запросы идут через настоящий OkxPublicClient (троттлинг
и повторы не подменяются). pump-pocket.json в тестах — временный файл (pocket_file);
setUpModule подменяет путь по умолчанию на крошечную позицию — реальный проектный файл
тесты не читают.
"""
import ast
import importlib.util
import io
import json
import math
import statistics
import tempfile
import unittest
import urllib.error
import urllib.parse
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest import mock

from src import pump_scanner as ps
from src.backtest.data import OkxApiError, OkxPublicClient
from src.backtest.indicators import macd, rsi_wilder, sma

H = 3_600_000
T0 = 1_790_000_000_000 // H * H                    # 2026-09-21, выровнено по часу
ROOT = Path(__file__).resolve().parent.parent
SKILL_SCRIPT = ROOT / ".agents/skills/spot-momentum-scan-validate/scripts/calc_indicators.py"


def setUpModule():
    """Страховка: журнал и карман по умолчанию — во временном каталоге, а не в настоящих
    data/pump_journal.jsonl и pump-pocket.json."""
    tmp = tempfile.TemporaryDirectory()
    unittest.addModuleCleanup(tmp.cleanup)
    patcher = mock.patch.object(ps, "JOURNAL_PATH", Path(tmp.name) / "default_journal.jsonl")
    patcher.start()
    unittest.addModuleCleanup(patcher.stop)
    pocket_patcher = mock.patch.object(ps, "POCKET_PATH", pocket_file(tmp.name))
    pocket_patcher.start()
    unittest.addModuleCleanup(pocket_patcher.stop)


# --- Синтетические свечи ---

def candle_row(ts, o, c, vol_quote, confirm="1"):
    """Строка OKX. vol и volCcy (индексы 5, 6) = 1: объём сканер обязан брать из индекса 7."""
    h, l = max(o, c) * 1.001, min(o, c) * 0.999
    return [str(ts), repr(o), repr(h), repr(l), repr(c), "1", "1", repr(vol_quote), confirm]


def saw_step(i):
    """Пила вокруг слабого роста: RSI ≈ 60, MACD-гистограмма около нуля."""
    return 0.0005 + 0.004 * (1 if i % 2 == 0 else -1) * (1 + 0.3 * math.sin(i / 3))


def make_rows(n=99, *, last_pct=2.0, last_vol_mult=3.0, step=saw_step, t0=T0, forming=True):
    """n закрытых свечей по возрастанию; последняя — импульс last_pct % и объём
    last_vol_mult × медианы 20 предыдущих; затем формирующаяся +10% с объёмом 1e9."""
    rows, vols, p = [], [], 100.0
    for i in range(n - 1):
        c = p * (1 + step(i))
        v = 1000.0 * (1 + 0.2 * math.sin(i / 2.0))
        rows.append(candle_row(t0 + i * H, p, c, v))
        vols.append(v)
        p = c
    c = p * (1 + last_pct / 100)
    rows.append(candle_row(t0 + (n - 1) * H, p, c, statistics.median(vols[-20:]) * last_vol_mult))
    if forming:
        rows.append(candle_row(t0 + n * H, c, c * 1.10, 1e9, confirm="0"))
    return rows


def closes_of(rows):
    return [float(r[4]) for r in rows if r[8] == "1"]


def now_after(rows):
    """Момент внутри формирующейся свечи (через 30 мин после её открытия)."""
    return int(rows[-1][0]) + H // 2 if rows[-1][8] == "0" else int(rows[-1][0]) + H + H // 2


# --- Стакан и карман (PUMP-LIQ) ---

def fake_book(asks, bids, ts=T0):
    """[(px, sz), …] по ask/bid -> ответ market/books (снимок; тот же формат, что у OKX)."""
    fmt = lambda levels: [[repr(px), repr(sz)] for px, sz in levels]
    return {"asks": fmt(asks), "bids": fmt(bids), "ts": str(ts)}


# Глубокий узкий стакан: не мешает тестам, которым проверка ликвидности безразлична.
DEFAULT_BOOK = fake_book([(100.0, 10_000.0), (100.05, 10_000.0), (100.1, 10_000.0)],
                        [(99.95, 10_000.0), (99.9, 10_000.0), (99.85, 10_000.0)])
DEFAULT_POCKET_POSITION = 1.0   # тестам без интереса к ликвидности: пороги × позиция — заведомо малы


def pocket_file(dirpath, position=DEFAULT_POCKET_POSITION, **extra):
    """Временный pump-pocket.json: max_position_pct = position. Настоящий файл не трогает."""
    path = Path(dirpath) / "pump-pocket.json"
    path.write_text(json.dumps({"max_position_pct": position, **extra}), encoding="utf-8")
    return path


def vol_quote_of(rows, inst_id="A-USDT"):
    """Оборот (volCcyQuote) последней закрытой свечи серии — для порогов ликвидности в тестах."""
    return ps.compute_metrics(ps.closed_candles(rows, inst_id))["vol_quote"]


class FakeOkx:
    """Opener для OkxPublicClient: tickers, candles, history-candles, books в памяти.
    books без явного значения для пары -> DEFAULT_BOOK."""

    def __init__(self, series=None, tickers=None, errors=None, books=None):
        self.series = series or {}
        self.tickers = tickers or []
        self.errors = errors or {}           # instId или "tickers" -> исключение или payload
        self.books = books or {}             # instId -> снимок market/books (fake_book(...))
        self.urls = []

    def __call__(self, url, timeout):
        self.urls.append(url)
        parts = urllib.parse.urlsplit(url)
        q = dict(urllib.parse.parse_qsl(parts.query))
        key = "tickers" if parts.path == "/api/v5/market/tickers" else q.get("instId")
        err = self.errors.get(key)
        if isinstance(err, Exception):
            raise err
        if err is not None:
            return json.dumps(err).encode()
        if parts.path == "/api/v5/market/tickers":
            return self._ok(self.tickers)
        if parts.path in ("/api/v5/market/candles", "/api/v5/market/history-candles"):
            if key not in self.series:
                return json.dumps({"code": "51001", "msg": "Instrument ID does not exist",
                                   "data": []}).encode()
            after = int(q["after"]) if "after" in q else None
            rows = [r for r in self.series[key] if after is None or int(r[0]) < after]
            return self._ok(list(reversed(rows))[: int(q.get("limit", "100"))])
        if parts.path == "/api/v5/market/books":
            return self._ok([self.books.get(key, DEFAULT_BOOK)])
        raise AssertionError(f"неожиданный эндпоинт: {parts.path}")

    @staticmethod
    def _ok(data):
        return json.dumps({"code": "0", "msg": "", "data": data}).encode()

    def paths(self):
        return [urllib.parse.urlsplit(u).path for u in self.urls]


def client_for(fake):
    return OkxPublicClient(min_interval=0.0, sleep=lambda s: None, opener=fake)


def ticker(inst, vol):
    return {"instType": "SPOT", "instId": inst, "last": "1", "volCcy24h": str(vol)}


def run_main(argv, fake, now_ms):
    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        code = ps.main(argv, client=client_for(fake), now_ms=now_ms)
    return code, out.getvalue(), err.getvalue()


def ref_macd_hist(closes, fast=12, slow=26, signal=9):
    """Независимый расчёт MACD: списки и циклы, без функций проекта."""
    def ema_list(xs, n):
        out = [None] * len(xs)
        if len(xs) < n:
            return out
        k = 2.0 / (n + 1)
        e = sum(xs[:n]) / n
        out[n - 1] = e
        for i in range(n, len(xs)):
            e = xs[i] * k + e * (1 - k)
            out[i] = e
        return out
    f, s = ema_list(closes, fast), ema_list(closes, slow)
    line = [a - b for a, b in zip(f, s) if a is not None and b is not None]
    sig = ema_list(line, signal)
    return [None] * (len(closes) - len(line)) + [
        None if g is None else m - g for m, g in zip(line, sig)]


# --- Индикатор MACD (src/backtest/indicators.py) ---

class MacdIndicatorTest(unittest.TestCase):
    def setUp(self):
        self.closes = closes_of(make_rows(150))

    def test_matches_independent_calc(self):
        hist = macd(self.closes)[2]
        ref = ref_macd_hist(self.closes)
        for i, (a, b) in enumerate(zip(hist, ref)):
            if b is None:
                self.assertTrue(math.isnan(a), i)
            else:
                self.assertTrue(math.isclose(a, b, rel_tol=1e-9, abs_tol=1e-12), (i, a, b))

    def test_warmup(self):
        line, sig, hist = macd(self.closes)
        self.assertTrue(math.isnan(line[24]) and not math.isnan(line[25]))
        self.assertTrue(math.isnan(sig[32]) and not math.isnan(sig[33]))
        self.assertTrue(math.isnan(hist[32]) and not math.isnan(hist[33]))
        self.assertTrue(all(math.isnan(x) for x in macd(self.closes[:25])[0]))
        self.assertTrue(all(math.isnan(x) for x in macd(self.closes[:33])[2]))

    def test_constant_series_zero(self):
        _, _, hist = macd([5.0] * 60)
        self.assertTrue(all(abs(x) < 1e-12 for x in hist[33:]))

    def test_prefix_invariance(self):
        full = macd(self.closes)[2]
        for k in (20, 34, 77, 149):
            part = macd(self.closes[:k])[2]
            for a, b in zip(full[:k], part):
                self.assertTrue((math.isnan(a) and math.isnan(b)) or a == b, (k, a, b))

    def test_invalid_periods(self):
        with self.assertRaises(ValueError):
            macd(self.closes, fast=26, slow=12)
        with self.assertRaises(ValueError):
            macd(self.closes, signal=0)


@unittest.skipUnless(SKILL_SCRIPT.exists(), "нет calc_indicators.py скилла")
class SkillParityTest(unittest.TestCase):
    """Сверка с исходной методикой: calc_indicators.py скилла spot-momentum-scan-validate."""

    @classmethod
    def setUpClass(cls):
        spec = importlib.util.spec_from_file_location("skill_calc_indicators", SKILL_SCRIPT)
        cls.skill = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(cls.skill)

    def test_indicators_match_skill(self):
        rows = make_rows(99, forming=False)
        closes = closes_of(rows)
        vq = [float(r[7]) for r in rows]
        _, _, skill_hist = self.skill.calc_macd(closes)
        self.assertAlmostEqual(macd(closes)[2][-1], skill_hist, places=10)
        self.assertAlmostEqual(rsi_wilder(closes)[-1], self.skill.calc_rsi(closes), places=9)
        self.assertAlmostEqual(sma(closes, 20)[-1], self.skill.calc_ma(closes), places=9)
        m = ps.compute_metrics(ps.closed_candles(rows, "X-USDT"))
        # среднее по 20 предыдущим — это vol_ratio скилла; фильтр проекта — по медиане
        self.assertAlmostEqual(m["vol_ratio_mean"], self.skill.calc_vol_ratio(vq), places=9)


# --- Метрики по свечам ---

class MetricsTest(unittest.TestCase):
    def test_indicators_on_closed_candles_only(self):
        rows = make_rows(99)
        row = ps.analyze_pair("X-USDT", ps.closed_candles(rows, "X-USDT"), ps.Thresholds())
        closes = closes_of(rows)
        self.assertEqual(len(closes), 99)
        m = row["metrics"]
        self.assertEqual(m["rsi"], rsi_wilder(closes, 14)[-1])
        self.assertEqual(m["ma20"], sma(closes, 20)[-1])
        self.assertEqual(m["macd_hist"], macd(closes)[2][-1])
        self.assertAlmostEqual(m["impulse_pct"], 2.0, places=9)
        self.assertAlmostEqual(m["vol_ratio_median"], 3.0, places=9)

    def test_forming_candle_not_used(self):
        rows = make_rows(99)                      # формирующаяся: +10%, объём 1e9
        client = client_for(FakeOkx({"X-USDT": rows}))
        candles = ps.fetch_closed_candles(client, "X-USDT", 99)
        self.assertEqual(candles[-1].ts, int(rows[-2][0]))
        self.assertEqual(len(candles), 99)
        row = ps.analyze_pair("X-USDT", candles, ps.Thresholds())
        self.assertAlmostEqual(row["metrics"]["impulse_pct"], 2.0, places=9)
        # та же свеча с confirm=1 попала бы в расчёт — фильтр по confirm решает исход
        confirmed = rows[:-1] + [rows[-1][:8] + ["1"]]
        other = ps.closed_candles(confirmed, "X-USDT")
        self.assertAlmostEqual(ps.compute_metrics(other)["impulse_pct"], 10.0, places=9)

    def test_median_vs_mean_with_outlier(self):
        vols = [100.0] * 19 + [100_000.0]          # выброс ×1000 в окне (демо-лента)
        rows = [candle_row(T0 + i * H, 100.0, 100.0, 100.0) for i in range(10)]
        rows += [candle_row(T0 + (10 + i) * H, 100.0, 100.0, v) for i, v in enumerate(vols)]
        rows.append(candle_row(T0 + 30 * H, 100.0, 102.0, 300.0))
        m = ps.compute_metrics(ps.closed_candles(rows, "X-USDT"))
        self.assertEqual(m["vol_median"], 100.0)
        self.assertAlmostEqual(m["vol_ratio_median"], 3.0)
        self.assertAlmostEqual(m["vol_ratio_mean"], 300.0 / ((19 * 100.0 + 100_000.0) / 20))
        self.assertLess(m["vol_ratio_mean"], 0.1)  # среднее «не видит» всплеск, медиана — видит

    def test_zero_median_volume_fails_volume(self):
        rows = [candle_row(T0 + i * H, 100.0, 100.0, 0.0) for i in range(34)]
        rows.append(candle_row(T0 + 34 * H, 100.0, 102.0, 500.0))
        row = ps.analyze_pair("X-USDT", ps.closed_candles(rows, "X-USDT"), ps.Thresholds())
        self.assertIsNone(row["metrics"]["vol_ratio_median"])
        self.assertIn("volume", row["failed"])

    def test_insufficient_data(self):
        few = ps.analyze_pair("NEW-USDT", ps.closed_candles(make_rows(29), "NEW-USDT"),
                              ps.Thresholds())
        self.assertEqual(few["status"], "insufficient")
        self.assertIn("данных мало", few["reasons"][0])
        ok = ps.analyze_pair("NEW-USDT", ps.closed_candles(make_rows(30), "NEW-USDT"),
                             ps.Thresholds())
        self.assertNotEqual(ok["status"], "insufficient")
        self.assertIn("macd", ok["failed"])        # 30 < 34 свечей прогрева MACD

    def test_bad_candle_row_is_data_error(self):
        with self.assertRaises(ps.ScanError):
            ps.parse_candle(["1", "2", "3", "4", "5", "6", "7"], "X-USDT")   # старый формат
        with self.assertRaises(ps.ScanError):
            ps.parse_candle([str(T0), "x", "1", "1", "1", "1", "1", "1", "1"], "X-USDT")


# --- Условия фильтра по отдельности ---

BASE = {"impulse_pct": 2.0, "vol_ratio_median": 3.0, "rsi": 60.0, "ma20": 100.0,
        "close": 105.0, "macd_hist": 0.1}


class EvaluateTest(unittest.TestCase):
    th = ps.Thresholds()

    def failed(self, **changes):
        checks, reasons = ps.evaluate(dict(BASE, **changes), self.th)
        failed = [k for k in ps.CONDITIONS if not checks[k]]
        self.assertEqual(len(failed), len(reasons))
        return failed

    def test_all_pass(self):
        self.assertEqual(self.failed(), [])

    def test_impulse(self):
        self.assertEqual(self.failed(impulse_pct=1.49), ["impulse"])
        self.assertEqual(self.failed(impulse_pct=-3.0), ["impulse"])
        self.assertEqual(self.failed(impulse_pct=1.5), [])          # порог включительно

    def test_volume(self):
        self.assertEqual(self.failed(vol_ratio_median=1.49), ["volume"])
        self.assertEqual(self.failed(vol_ratio_median=None), ["volume"])
        self.assertEqual(self.failed(vol_ratio_median=1.5), [])

    def test_rsi_window(self):
        self.assertEqual(self.failed(rsi=49.9), ["rsi"])
        self.assertEqual(self.failed(rsi=72.8), ["rsi"])            # OKB-USDT, скан №5
        self.assertEqual(self.failed(rsi=None), ["rsi"])
        self.assertEqual(self.failed(rsi=50.0), [])
        self.assertEqual(self.failed(rsi=72.0), [])

    def test_close_above_ma20_strict(self):
        self.assertEqual(self.failed(close=99.0), ["ma20"])
        self.assertEqual(self.failed(close=100.0), ["ma20"])
        self.assertEqual(self.failed(ma20=None), ["ma20"])

    def test_macd_hist_positive(self):
        self.assertEqual(self.failed(macd_hist=0.0), ["macd"])
        self.assertEqual(self.failed(macd_hist=-1e-9), ["macd"])
        self.assertEqual(self.failed(macd_hist=None), ["macd"])

    def test_scan5_rows(self):
        """Строки таблицы скана №5: OKB — только RSI, LTC — импульс и RSI."""
        okb = dict(BASE, impulse_pct=5.47, vol_ratio_median=39.2, rsi=72.8)
        ltc = dict(BASE, impulse_pct=1.02, vol_ratio_median=11.5, rsi=73.7)
        checks, _ = ps.evaluate(okb, self.th)
        self.assertEqual([k for k in ps.CONDITIONS if not checks[k]], ["rsi"])
        checks, _ = ps.evaluate(ltc, self.th)
        self.assertEqual([k for k in ps.CONDITIONS if not checks[k]], ["impulse", "rsi"])

    def test_thresholds_are_parameters(self):
        tight = ps.Thresholds(impulse_min=3.0, vol_ratio_min=4.0, rsi_min=55, rsi_max=58)
        checks, _ = ps.evaluate(BASE, tight)
        self.assertEqual([k for k in ps.CONDITIONS if not checks[k]], ["impulse", "volume", "rsi"])
        loose = ps.Thresholds(rsi_max=75)
        self.assertTrue(ps.evaluate(dict(BASE, rsi=72.8), loose)[0]["rsi"])

    def test_score_formula(self):
        self.assertAlmostEqual(ps.signal_score(BASE, self.th), 5 + 4)       # 6 -> кэп 5, 4
        self.assertAlmostEqual(ps.signal_score(dict(BASE, vol_ratio_median=None), self.th), 4)
        self.assertAlmostEqual(ps.signal_score(dict(BASE, impulse_pct=-1.5), self.th), 5 - 3)

    def test_each_condition_on_candles(self):
        th = ps.Thresholds()

        def status(rows):
            row = ps.analyze_pair("X-USDT", ps.closed_candles(rows, "X-USDT"), th)
            return row["status"], row["failed"]

        self.assertEqual(status(make_rows(99)), ("candidate", []))
        self.assertEqual(status(make_rows(99, last_pct=1.0)), ("near", ["impulse"]))
        self.assertEqual(status(make_rows(99, last_vol_mult=1.2)), ("near", ["volume"]))
        uptrend = make_rows(99, step=lambda i: 0.004 + 0.002 * (1 if i % 2 == 0 else -1))
        self.assertEqual(status(uptrend), ("near", ["rsi"]))              # RSI > 72


# --- Ранжирование, «почти кандидаты», устаревшая свеча ---

class RankingTest(unittest.TestCase):
    def scan(self, series, **kw):
        fake = FakeOkx(series)
        now = max(now_after(r) for r in series.values())
        return ps.run_scan(client_for(fake), pairs=list(series), now_ms=now, **kw), fake

    def test_candidates_by_score_and_near(self):
        series = {
            "A-USDT": make_rows(99, last_pct=2.0, last_vol_mult=3.0),     # 5 + 4 = 9
            "B-USDT": make_rows(99, last_pct=2.6, last_vol_mult=4.0),     # 5 + 5 = 10
            "C-USDT": make_rows(99, last_pct=1.0, last_vol_mult=3.0),     # почти: импульс, 7
            "D-USDT": make_rows(99, last_pct=2.0, last_vol_mult=1.2),     # почти: объём, 6.4
            "E-USDT": make_rows(99, last_pct=0.5, last_vol_mult=1.0),     # два условия
            "F-USDT": make_rows(20),                                      # данных мало
        }
        report, _ = self.scan(series)
        self.assertEqual(report["candidates"], ["B-USDT", "A-USDT"])
        self.assertEqual(report["near"], ["C-USDT", "D-USDT"])
        self.assertEqual(report["insufficient"], ["F-USDT"])
        self.assertEqual([r["inst_id"] for r in report["results"]],
                         ["B-USDT", "A-USDT", "C-USDT", "D-USDT", "E-USDT", "F-USDT"])
        by_id = {r["inst_id"]: r for r in report["results"]}
        self.assertEqual(by_id["E-USDT"]["status"], "rejected")
        self.assertEqual(by_id["E-USDT"]["failed"][:2], ["impulse", "volume"])
        self.assertAlmostEqual(by_id["A-USDT"]["score"], 9.0, places=3)
        self.assertEqual(report["counts"], {"scanned": 6, "candidates": 2, "near": 2,
                                            "insufficient": 1, "stale": 0})
        self.assertTrue(report["note"].startswith("Кандидатов 2 из 6: B-USDT"))
        text = ps.render_text(report)
        self.assertIn("Почти кандидаты (не хватило одного условия): C-USDT, D-USDT", text)
        self.assertIn("КАНДИДАТ", text)

    def test_stale_pair_is_not_candidate(self):
        series = {"A-USDT": make_rows(99), "OLD-USDT": make_rows(99, t0=T0 - H)}
        report, _ = self.scan(series)
        self.assertEqual(report["candidates"], ["A-USDT"])
        self.assertEqual(report["stale"], ["OLD-USDT"])
        self.assertEqual(report["candle_ts"], ps.iso_utc(T0 + 98 * H))

    def test_lagging_feed_warning(self):
        rows = make_rows(99)
        fake = FakeOkx({"A-USDT": rows})
        report = ps.run_scan(client_for(fake), pairs=["A-USDT"], now_ms=now_after(rows) + 2 * H)
        self.assertEqual(len(report["warnings"]), 1)
        self.assertIn("старее ожидаемой", report["warnings"][0])
        fresh = ps.run_scan(client_for(fake), pairs=["A-USDT"], now_ms=now_after(rows))
        self.assertEqual(fresh["warnings"], [])


# --- Повтор прошлого скана (--at) ---

class ReplayTest(unittest.TestCase):
    def setUp(self):
        self.rows = make_rows(130)                          # T0 .. T0+129H, формирующаяся T0+130H
        pump = [candle_row(T0 + 100 * H, 100.0, 108.0, 1e9)]   # памп в свече, содержащей at
        self.rows[100] = pump[0]
        self.at = T0 + 100 * H + 47 * 60_000                 # «скан в HH:47»
        self.fake = FakeOkx({"X-USDT": self.rows})

    def test_only_candles_closed_before_at(self):
        client = client_for(self.fake)
        candles = ps.fetch_closed_candles(client, "X-USDT", 99, at_ms=self.at, endpoint="history")
        self.assertEqual([c.ts for c in candles], [T0 + i * H for i in range(1, 100)])
        self.assertTrue(all(c.ts + H <= self.at for c in candles))
        url = urllib.parse.urlsplit(self.fake.urls[0])
        q = dict(urllib.parse.parse_qsl(url.query))
        self.assertEqual(url.path, "/api/v5/market/history-candles")
        self.assertEqual(int(q["after"]), self.at - H + 1)

    def test_closed_exactly_at_moment_included(self):
        rows = [candle_row(T0, 1.0, 1.0, 1.0), candle_row(T0 + H, 1.0, 1.0, 1.0)]
        self.assertEqual([c.ts for c in ps.closed_candles(rows, "X", at_ms=T0 + H)], [T0])
        self.assertEqual([c.ts for c in ps.closed_candles(rows, "X", at_ms=T0 + 2 * H - 1)], [T0])
        self.assertEqual(len(ps.closed_candles(rows, "X", at_ms=T0 + 2 * H)), 2)

    def test_replay_cli_no_journal(self):
        with tempfile.TemporaryDirectory() as tmp:
            journal = Path(tmp) / "pump_journal.jsonl"
            at = ps.iso_local(self.at)
            code, out, _ = run_main(["--at", at, "--pairs", "x-usdt", "--journal", str(journal),
                                     "--json"], self.fake, now_ms=T0 + 131 * H)
            self.assertEqual(code, 0)
            self.assertFalse(journal.exists())
        report = json.loads(out)
        self.assertEqual(report["mode"], "replay")
        self.assertEqual(report["candle_ts"], ps.iso_utc(T0 + 99 * H))
        self.assertEqual(report["endpoint"], "/api/v5/market/history-candles")
        self.assertIsNone(report["journal"])
        self.assertNotIn("/api/v5/market/tickers", self.fake.paths())

    def test_endpoint_override_for_diagnostics(self):
        code, out, _ = run_main(["--at", ps.iso_local(self.at), "--pairs", "X-USDT", "--json",
                                 "--endpoint", "candles"], self.fake, now_ms=T0 + 131 * H)
        self.assertEqual(code, 0)
        self.assertEqual(set(self.fake.paths()), {"/api/v5/market/candles"})
        self.assertEqual(json.loads(out)["candle_ts"], ps.iso_utc(T0 + 99 * H))

    def test_bad_at_arguments(self):
        cases = (["--at", "2026-09-24T09:47+05:00"],                       # без --pairs
                 ["--at", "2026-09-24T09:47", "--pairs", "X-USDT"],        # без пояса
                 ["--at", "вчера", "--pairs", "X-USDT"],
                 ["--at", ps.iso_local(T0 + 200 * H), "--pairs", "X-USDT"])  # в будущем
        for argv in cases:
            with self.subTest(argv=argv), self.assertRaises(SystemExit) as cm:
                run_main(argv, self.fake, now_ms=T0 + 131 * H)
            self.assertEqual(cm.exception.code, 2)
        self.assertEqual(self.fake.urls, [])

    def test_parse_at(self):
        self.assertEqual(ps.parse_at("2026-09-24T09:47+05:00"), ps.parse_at("2026-09-24T04:47Z"))
        self.assertEqual(ps.parse_at("2026-09-24T04:47:00+00:00"), 1_790_225_220_000)


# --- Универсум ---

class UniverseTest(unittest.TestCase):
    TICKERS = [ticker("USDC-USDT", 9e9), ticker("DAI-USDT", 8e9), ticker("FDUSD-USDT", 7e9),
               ticker("BTC-USDT", 6e9), ticker("ETH-USDT", 5e9), ticker("SOL-USDT", 4e9),
               ticker("OKB-USDT", 3e9), ticker("LTC-USDT", 2e9), ticker("PEPE-USDT", 1e9),
               ticker("ETH-BTC", 9e9), ticker("OKB-USDC", 9e9), ticker("BBB-USDT", 5e8),
               ticker("AAA-USDT", 5e8), ticker("ZERO-USDT", "")]

    def test_filters_and_top_n(self):
        pairs, info = ps.select_universe(self.TICKERS, top_n=5, exclude=["eth", "SOL-USDT"])
        self.assertEqual(pairs, ["OKB-USDT", "LTC-USDT", "PEPE-USDT", "AAA-USDT", "BBB-USDT"])
        self.assertEqual(info, {"source": "tickers", "tickers": 14, "usdt_pairs": 12,
                                "non_stable": 9, "eligible": 6, "top_n": 5,
                                "exclude": ["BTC", "ETH", "SOL"]})

    def test_default_top_45_and_exclude_normalization(self):
        many = [ticker(f"C{i:03d}-USDT", 1000 - i) for i in range(60)]
        pairs, info = ps.select_universe(many + self.TICKERS[:4])
        self.assertEqual(len(pairs), ps.DEFAULT_TOP_N)
        self.assertEqual(pairs[0], "C000-USDT")
        self.assertEqual(ps.normalize_bases(["eth,sol", " okb-usdt "]), {"ETH", "SOL", "OKB"})

    def test_scan_fetches_candles_only_for_top_n(self):
        series = {i: make_rows(40) for i in ("OKB-USDT", "LTC-USDT")}
        fake = FakeOkx(series, tickers=self.TICKERS)
        report = ps.run_scan(client_for(fake), top_n=2, exclude=["ETH", "SOL"],
                             now_ms=now_after(series["OKB-USDT"]))
        self.assertEqual(report["pairs"], ["OKB-USDT", "LTC-USDT"])
        candle_ids = [dict(urllib.parse.parse_qsl(urllib.parse.urlsplit(u).query))["instId"]
                      for u in fake.urls if "candles" in u]
        self.assertEqual(candle_ids, ["OKB-USDT", "LTC-USDT"])
        self.assertEqual(fake.paths()[0], "/api/v5/market/tickers")

    def test_empty_tickers_is_error(self):
        with self.assertRaises(ps.ScanError):
            ps.run_scan(client_for(FakeOkx(tickers=[])), now_ms=T0)


# --- Журнал, вывод, коды выхода ---

class JournalAndExitTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.journal = Path(self._tmp.name) / "data" / "pump_journal.jsonl"
        self.rows = {"A-USDT": make_rows(99), "C-USDT": make_rows(99, last_pct=1.0)}
        self.now = now_after(self.rows["A-USDT"])

    def tearDown(self):
        self._tmp.cleanup()

    def test_journal_appends_one_utf8_line_per_scan(self):
        for _ in range(2):
            code, out, _ = run_main(["--pairs", "A-USDT", "C-USDT", "--journal",
                                     str(self.journal)], FakeOkx(self.rows), self.now)
            self.assertEqual(code, 0)
        raw = self.journal.read_bytes().decode("utf-8")
        lines = raw.split("\n")
        self.assertEqual(len(lines), 3)
        self.assertEqual(lines[2], "")                            # строка = одна запись + \n
        rec = json.loads(lines[0])
        self.assertEqual((rec["event"], rec["source"], rec["v"], rec["feed"]),
                         ("scan", "src.pump_scanner", 1, "demo"))
        self.assertEqual(rec["candle_ts"], ps.iso_utc(T0 + 98 * H))
        self.assertEqual(rec["pairs"], ["A-USDT", "C-USDT"])
        self.assertEqual([c["inst_id"] for c in rec["candidates"]], ["A-USDT"])
        self.assertEqual(rec["near"][0]["inst_id"], "C-USDT")
        self.assertEqual(rec["near"][0]["failed"], ["impulse"])
        self.assertTrue(rec["ts"].endswith("+05:00"))
        self.assertIn("Кандидатов 1 из 2", raw)                  # кириллица без \u-экранов
        self.assertIn(f"+1 строка scan → {self.journal.as_posix()}", out)

    def test_no_journal_flag(self):
        code, _, _ = run_main(["--pairs", "A-USDT", "--no-journal", "--journal",
                               str(self.journal)], FakeOkx(self.rows), self.now)
        self.assertEqual(code, 0)
        self.assertFalse(self.journal.exists())

    def test_journal_on_by_default(self):
        default = ps.JOURNAL_PATH                               # подменён в setUpModule
        self.assertNotEqual(default.resolve(), (ROOT / "data" / "pump_journal.jsonl").resolve())
        before = default.read_text(encoding="utf-8").count("\n") if default.exists() else 0
        code, _, _ = run_main(["--pairs", "A-USDT"], FakeOkx(self.rows), self.now)
        self.assertEqual(code, 0)
        self.assertEqual(default.read_text(encoding="utf-8").count("\n"), before + 1)

    def test_json_output(self):
        code, out, _ = run_main(["--pairs", "A-USDT", "C-USDT", "--json", "--no-journal"],
                                FakeOkx(self.rows), self.now)
        self.assertEqual(code, 0)
        report = json.loads(out)
        self.assertEqual(report["candidates"], ["A-USDT"])
        self.assertEqual(report["results"][1]["reasons"], ["импульс +1.00% < 1.5%"])

    def test_network_error_exit_2_and_no_journal(self):
        fake = FakeOkx(self.rows, errors={"A-USDT": urllib.error.URLError("connection refused")})
        with self.assertLogs("okx.backtest.data", level="WARNING"):
            code, out, err = run_main(["--pairs", "A-USDT", "--journal", str(self.journal)],
                                      fake, self.now)
        self.assertEqual(code, 2)
        self.assertEqual(out, "")
        self.assertIn("ошибка данных или сети", err)
        self.assertFalse(self.journal.exists())

    def test_http_403_and_api_error_exit_2(self):
        http403 = urllib.error.HTTPError("https://www.okx.com", 403, "Forbidden", {}, None)
        for errors in ({"tickers": http403},
                       {"A-USDT": {"code": "51001", "msg": "Instrument ID does not exist"}}):
            with self.subTest(errors=list(errors)):
                argv = ["--no-journal"] + ([] if "tickers" in errors else ["--pairs", "A-USDT"])
                code, _, err = run_main(argv, FakeOkx(self.rows, errors=errors), self.now)
                self.assertEqual(code, 2)
                self.assertTrue("HTTPError" in err or "OkxApiError" in err, err)

    def test_malformed_candles_exit_2(self):
        bad = {"code": "0", "data": [["1", "2", "3", "4", "5", "6", "7"]]}
        code, _, err = run_main(["--pairs", "A-USDT", "--no-journal"],
                                FakeOkx(self.rows, errors={"A-USDT": bad}), self.now)
        self.assertEqual(code, 2)
        self.assertIn("ScanError", err)

    def test_journal_write_failure_exit_2(self):
        self.journal.mkdir(parents=True)                         # каталог вместо файла
        code, out, err = run_main(["--pairs", "A-USDT", "--journal", str(self.journal)],
                                  FakeOkx(self.rows), self.now)
        self.assertEqual(code, 2)
        self.assertIn("Журнал не записан", err)
        self.assertIn("Кандидатов: 1 из 1", out)


# --- Только публичные GET ---

class PublicOnlyTest(unittest.TestCase):
    def test_full_scan_uses_only_public_market_get(self):
        series = {"OKB-USDT": make_rows(99), "LTC-USDT": make_rows(99)}
        fake = FakeOkx(series, tickers=UniverseTest.TICKERS)
        code, _, _ = run_main(["--no-journal", "--top", "2", "--exclude", "ETH", "SOL"], fake,
                              now_after(series["OKB-USDT"]))
        self.assertEqual(code, 0)
        self.assertTrue(fake.urls)
        for url in fake.urls:
            parts = urllib.parse.urlsplit(url)
            self.assertEqual(f"{parts.scheme}://{parts.netloc}", "https://www.okx.com")
            self.assertIn(parts.path, ps.PUBLIC_PATHS)
            keys = {k for k, _ in urllib.parse.parse_qsl(parts.query)}
            self.assertLessEqual(keys, {"instType", "instId", "bar", "limit", "after", "sz"})

    def test_opener_refuses_everything_but_public_market(self):
        opener = ps.make_opener("demo")
        with mock.patch.object(ps.urllib.request, "urlopen",
                               side_effect=AssertionError("сеть не должна вызываться")):
            for url in ("https://www.okx.com/api/v5/account/balance",
                        "https://www.okx.com/api/v5/trade/order?instId=BTC-USDT",
                        "https://www.okx.com/api/v5/asset/withdrawal",
                        "https://evil.example/api/v5/market/tickers?instType=SPOT"):
                with self.subTest(url=url), self.assertRaises(ps.ScanError):
                    opener(url, 5)

    def _captured_request(self, feed):
        resp = mock.MagicMock()
        resp.__enter__.return_value.read.return_value = b'{"code":"0","data":[]}'
        with mock.patch.object(ps.urllib.request, "urlopen", return_value=resp) as urlopen:
            ps.make_opener(feed)("https://www.okx.com/api/v5/market/tickers?instType=SPOT", 5)
        return urlopen.call_args[0][0]

    def test_demo_feed_header_and_get_without_keys(self):
        req = self._captured_request("demo")
        self.assertEqual(req.get_method(), "GET")
        self.assertIsNone(req.data)
        self.assertEqual(req.get_header("X-simulated-trading"), "1")
        self.assertFalse([h for h in req.header_items() if h[0].lower().startswith("ok-access")])
        live = self._captured_request("live")
        self.assertIsNone(live.get_header("X-simulated-trading"))

    def test_module_imports_no_private_api(self):
        tree = ast.parse((ROOT / "src" / "pump_scanner.py").read_text(encoding="utf-8"))
        modules = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                modules.update(a.name for a in node.names)
            elif isinstance(node, ast.ImportFrom):
                modules.add("." * node.level + (node.module or ""))
        self.assertLessEqual({m for m in modules if m.startswith(".")},
                             {".backtest.data", ".backtest.indicators"})
        for banned in ("ccxt", "src.connector", ".connector", ".config", ".risk",
                       ".order_router", "requests"):
            self.assertNotIn(banned, modules)


# --- Ликвидность кандидата (PUMP-LIQ) ---

class LiquidityTest(unittest.TestCase):
    """check_liquidity/apply_liquidity: стакан market/books против позиции кармана P."""

    def scan(self, rows, inst_id="A-USDT", position=None, books=None):
        """run_scan с временным карманом; position по умолчанию — заведомо ликвидная позиция."""
        vq = vol_quote_of(rows, inst_id)
        position = vq / 100 if position is None else position
        fake = FakeOkx({inst_id: rows}, books=books or {})
        with tempfile.TemporaryDirectory() as tmp:
            pocket = pocket_file(tmp, position)
            report = ps.run_scan(client_for(fake), pairs=[inst_id], now_ms=now_after(rows),
                                 pocket=pocket)
        return report, fake

    def test_liquid_candidate_passes(self):
        rows = make_rows(99)
        report, fake = self.scan(rows, books={"A-USDT": DEFAULT_BOOK})
        self.assertEqual(report["candidates"], ["A-USDT"])
        self.assertEqual(report["illiquid"], [])
        liq = report["results"][0]["liquidity"]
        self.assertTrue(liq["ok"])
        self.assertEqual(liq["failed"], [])
        self.assertTrue(all(liq["checks"].values()))
        self.assertTrue(report["liquidity"]["enabled"])
        self.assertTrue(report["liquidity"]["book"])
        self.assertEqual(fake.paths().count("/api/v5/market/books"), 1)

    def test_illiquid_by_depth(self):
        rows = make_rows(99)
        position = vol_quote_of(rows) / 10
        book = fake_book([(100.0, position / 100.0)], [(99.9, 10.0)])   # глубина 1×P < 3×P
        report, _ = self.scan(rows, position=position, books={"A-USDT": book})
        self.assertEqual(report["candidates"], [])
        self.assertEqual(report["illiquid"], ["A-USDT"])
        row = report["results"][0]
        self.assertEqual(row["status"], "illiquid")
        liq = row["liquidity"]
        self.assertEqual(liq["failed"], ["depth"])
        self.assertIn("глубина", liq["reasons"][0])
        self.assertEqual(row["reasons"], liq["reasons"])   # reasons строки переписаны из liquidity

    def test_illiquid_by_spread(self):
        rows = make_rows(99)
        position = vol_quote_of(rows) / 10
        book = fake_book([(100.0, 50 * position / 100.0)], [(50.0, 10.0)])   # спред ≈ 66 %
        report, _ = self.scan(rows, position=position, books={"A-USDT": book})
        self.assertEqual(report["illiquid"], ["A-USDT"])
        liq = report["results"][0]["liquidity"]
        self.assertEqual(liq["failed"], ["spread"])
        self.assertIn("спред", liq["reasons"][0])

    def test_illiquid_by_turnover(self):
        rows = make_rows(99)
        position = vol_quote_of(rows)                       # оборот = 1×P < 5×P
        book = fake_book([(100.0, 10 * position / 100.0)], [(99.9, 10.0)])
        report, _ = self.scan(rows, position=position, books={"A-USDT": book})
        self.assertEqual(report["illiquid"], ["A-USDT"])
        liq = report["results"][0]["liquidity"]
        self.assertEqual(liq["failed"], ["turnover"])
        self.assertIn("оборот", liq["reasons"][0])

    def test_non_usdt_quote_is_illiquid(self):
        rows = make_rows(99)
        report, fake = self.scan(rows, inst_id="A-USDC", position=1.0)
        self.assertEqual(report["illiquid"], ["A-USDC"])
        liq = report["results"][0]["liquidity"]
        self.assertFalse(liq["ok"])
        self.assertIn("USDC", liq["reasons"][0])
        self.assertIn("USDT", liq["reasons"][0])
        self.assertNotIn("/api/v5/market/books", fake.paths())   # книгу не запрашивать незачем

    def test_replay_skips_book_but_checks_turnover(self):
        rows = make_rows(99)
        at = now_after(rows)
        fake = FakeOkx({"A-USDT": rows})
        with tempfile.TemporaryDirectory() as tmp:
            pocket = pocket_file(tmp, vol_quote_of(rows) / 10)
            report = ps.run_scan(client_for(fake), pairs=["A-USDT"], at_ms=at, now_ms=at,
                                 pocket=pocket)
        self.assertNotIn("/api/v5/market/books", fake.paths())
        self.assertFalse(report["liquidity"]["book"])
        self.assertEqual(report["candidates"], ["A-USDT"])
        liq = report["results"][0]["liquidity"]
        self.assertIsNone(liq["checks"]["depth"])
        self.assertIsNone(liq["checks"]["spread"])
        self.assertTrue(liq["checks"]["turnover"])
        self.assertTrue(liq["notes"] and "повтор" in liq["notes"][0])

    def test_pocket_missing_or_invalid_warns_and_continues(self):
        rows = make_rows(99)
        cases = {"нет файла": None, "битый JSON": "not json",
                 "поле не число": json.dumps({"max_position_pct": "много"}),
                 "поле <= 0": json.dumps({"max_position_pct": 0})}
        for name, content in cases.items():
            with self.subTest(case=name):
                with tempfile.TemporaryDirectory() as tmp:
                    pocket = Path(tmp) / "pump-pocket.json"
                    if content is not None:
                        pocket.write_text(content, encoding="utf-8")
                    fake = FakeOkx({"A-USDT": rows})
                    report = ps.run_scan(client_for(fake), pairs=["A-USDT"],
                                         now_ms=now_after(rows), pocket=pocket)
                self.assertEqual(report["candidates"], ["A-USDT"])       # скан не упал
                self.assertFalse(report["liquidity"]["enabled"])
                self.assertIsNone(report["results"][0]["liquidity"])
                self.assertTrue(any("ликвидность не проверена" in w for w in report["warnings"]))
                self.assertNotIn("/api/v5/market/books", fake.paths())

    def test_no_liquidity_flag_disables_check(self):
        rows = make_rows(99)
        fake = FakeOkx({"A-USDT": rows})
        code, out, _ = run_main(["--pairs", "A-USDT", "--no-liquidity", "--no-journal", "--json"],
                                fake, now_after(rows))
        self.assertEqual(code, 0)
        report = json.loads(out)
        self.assertEqual(report["candidates"], ["A-USDT"])
        self.assertFalse(report["liquidity"]["enabled"])
        self.assertIn("no-liquidity", report["liquidity"]["note"])
        self.assertIsNone(report["results"][0]["liquidity"])
        self.assertNotIn("/api/v5/market/books", fake.paths())

    def test_book_requested_only_for_candidates_and_near(self):
        series = {
            "CAND-USDT": make_rows(99),                                     # candidate
            "NEAR-USDT": make_rows(99, last_pct=1.0),                       # near (импульс)
            "REJ-USDT": make_rows(99, last_pct=0.5, last_vol_mult=1.0),     # rejected (2 условия)
            "FEW-USDT": make_rows(20),                                      # insufficient
        }
        fake = FakeOkx(series)
        with tempfile.TemporaryDirectory() as tmp:
            pocket = pocket_file(tmp)
            report = ps.run_scan(client_for(fake), pairs=list(series),
                                 now_ms=max(now_after(r) for r in series.values()), pocket=pocket)
        self.assertEqual(set(report["near"]), {"NEAR-USDT"})
        requested = {dict(urllib.parse.parse_qsl(urllib.parse.urlsplit(u).query))["instId"]
                    for u in fake.urls if urllib.parse.urlsplit(u).path == "/api/v5/market/books"}
        self.assertEqual(requested, {"CAND-USDT", "NEAR-USDT"})

    def test_scan4_imx_style_illiquid(self):
        """Кейс скана №4 (докстринг модуля): фильтр из пяти условий пройден, illiquid — depth
        и turnover, в тех же кратностях позиции P, что в реальном скане (3.98×, 1.51×, 2.12×)."""
        rows = make_rows(99, last_pct=2.51, last_vol_mult=3.98)
        turnover = vol_quote_of(rows, "IMX-USDT")
        position = turnover / 1.51
        book = fake_book([(1.0, 2.12 * position)], [(0.999, 10.0)])
        report, _ = self.scan(rows, inst_id="IMX-USDT", position=position,
                              books={"IMX-USDT": book})
        row = report["results"][0]
        self.assertEqual(row["failed"], [])                     # прошёл фильтр пяти условий
        self.assertEqual(row["status"], "illiquid")
        self.assertEqual(set(row["liquidity"]["failed"]), {"depth", "turnover"})


if __name__ == "__main__":
    unittest.main()
