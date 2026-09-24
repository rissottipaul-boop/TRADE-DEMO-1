"""Бэктестер: загрузчик свечей, gap-check, хранилище (backtester-design.md §4). Без сети:
«биржа» — фейковый клиент с семантикой history-candles (новые первыми, after = старше ts,
первая строка — формирующаяся свеча confirm=0)."""
import json
import tempfile
import unittest
from pathlib import Path

from src.backtest.data import (Bar, Gap, InstrumentSpec, MarketDataStore, OkxApiError,
                               OkxPublicClient, check_bars, check_gaps, download_candles,
                               load_dataset)

H = 3_600_000
T0 = 1_767_225_600_000


def row(ts, px=100.0, confirm="1", vol="1"):
    p = str(px)
    return [str(ts), p, str(px + 1), str(px - 1), p, vol, vol, vol, confirm]


class FakeExchange:
    """history-candles: страницы по limit, от новых к старым; after — строго старше."""

    def __init__(self, first_ts, n, missing=(), forming=True, limit=300):
        self.all_ts = [first_ts + i * H for i in range(n) if i not in set(missing)]
        self.forming = forming
        self.limit = limit
        self.calls = []

    @property
    def now_ms(self):
        return self.all_ts[-1] + H + 60_000  # внутри формирующейся свечи

    def history_candles(self, inst_id, bar, after=None, limit=300):
        self.calls.append(after)
        rows = [row(ts, 100.0 + (ts - T0) / H) for ts in self.all_ts]
        if self.forming:
            rows.append(row(self.all_ts[-1] + H, confirm="0"))
        rows = [r for r in rows if after is None or int(r[0]) < after]
        return list(reversed(rows))[: self.limit]


class GapCheckTest(unittest.TestCase):
    def test_continuous_series_has_no_gaps(self):
        self.assertEqual(check_gaps([T0 + i * H for i in range(10)], H), [])

    def test_missing_bars_reported(self):
        ts = [T0, T0 + H, T0 + 4 * H, T0 + 5 * H, T0 + 7 * H]
        self.assertEqual(check_gaps(ts, H), [Gap(T0 + 2 * H, T0 + 3 * H, 2),
                                             Gap(T0 + 6 * H, T0 + 6 * H, 1)])

    def test_non_increasing_is_error(self):
        with self.assertRaises(ValueError):
            check_gaps([T0, T0 + H, T0 + H], H)

    def test_zero_volume_bar_is_not_gap(self):
        bars = [Bar(T0, 1, 1, 1, 1, 1.0), Bar(T0 + H, 1, 1, 1, 1, 0.0), Bar(T0 + 2 * H, 1, 1, 1, 1, 2.0)]
        gaps, anomalies = check_bars(bars, H)
        self.assertEqual((gaps, anomalies), ([], []))

    def test_ohlc_anomalies(self):
        bars = [Bar(T0, 10, 9, 8, 10), Bar(T0 + H, 0, 1, 0, 1), Bar(T0 + H + 5, 1, 1, 1, 1)]
        _, anomalies = check_bars(bars, H)
        self.assertEqual(len(anomalies), 3, anomalies)


class StoreTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.store = MarketDataStore(Path(self._tmp.name) / "md.db")

    def tearDown(self):
        self._tmp.cleanup()

    def test_only_confirmed_and_closed_candles_stored(self):
        now = T0 + 3 * H + 10
        rows = [row(T0), row(T0 + H), row(T0 + 2 * H, confirm="0"), row(T0 + 3 * H)]
        # T0+3H закрывается в T0+4H > now: не пишется даже с confirm=1
        self.assertEqual(self.store.upsert_candles("X-USDT", "1H", rows, now_ms=now), 2)
        self.assertEqual([b.ts for b in self.store.load_bars("X-USDT", "1H")], [T0, T0 + H])

    def test_append_only_dedup(self):
        now = T0 + 10 * H
        self.assertEqual(self.store.upsert_candles("X-USDT", "1H", [row(T0, 100.0)], now), 1)
        self.assertEqual(self.store.upsert_candles("X-USDT", "1H", [row(T0, 555.0)], now), 0)
        self.assertEqual(self.store.load_bars("X-USDT", "1H")[0].c, 100.0)

    def test_dataset_id_stable_and_content_sensitive(self):
        now = T0 + 10 * H
        self.store.upsert_candles("X-USDT", "1H", [row(T0 + i * H) for i in range(5)], now)
        a = load_dataset(self.store, "X-USDT", "1H")
        b = load_dataset(self.store, "X-USDT", "1H")
        self.assertEqual(a.dataset_id, b.dataset_id)
        self.store.upsert_candles("X-USDT", "1H", [row(T0 + 5 * H)], now)
        c = load_dataset(self.store, "X-USDT", "1H")
        self.assertNotEqual(a.dataset_id, c.dataset_id)
        d = load_dataset(self.store, "X-USDT", "1H", end_ms=T0 + 5 * H)
        self.assertEqual(a.dataset_id, d.dataset_id)  # тот же диапазон -> тот же снапшот
        with self.store._conn() as conn:
            n = conn.execute("SELECT COUNT(*) FROM datasets").fetchone()[0]
        self.assertEqual(n, 2)

    def test_instrument_and_holdout_log(self):
        spec = InstrumentSpec("X-USDT", 0.1, 1e-8, 1e-5)
        self.store.save_instrument(spec, raw={"tickSz": "0.1"})
        self.assertEqual(self.store.get_instrument("X-USDT"), spec)
        self.assertIsNone(self.store.get_instrument("Y-USDT"))
        self.assertEqual(self.store.log_holdout_touch("ds1", "sma", {"fast": 1}), 1)
        self.assertEqual(self.store.log_holdout_touch("ds1", "sma", {"fast": 2}), 2)
        self.assertEqual(self.store.holdout_touch_counts(), {"sma": 2})
        self.assertEqual(self.store.holdout_touch_counts("X-USDT"), {})  # ds1 не зарегистрирован
        self.store.upsert_candles("X-USDT", "1H", [row(T0 + i * H) for i in range(3)], T0 + 9 * H)
        ds = load_dataset(self.store, "X-USDT", "1H")
        self.store.log_holdout_touch(ds.dataset_id, "buy_hold", {})
        self.assertEqual(self.store.holdout_touch_counts("X-USDT"), {"buy_hold": 1})


class DownloadTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.store = MarketDataStore(Path(self._tmp.name) / "md.db")

    def tearDown(self):
        self._tmp.cleanup()

    def test_full_download_paginates_back_to_since(self):
        ex = FakeExchange(T0, 1000)
        since = T0 + 100 * H
        rep = download_candles(self.store, ex, "X-USDT", "1H", since, now_ms=ex.now_ms)
        ds = load_dataset(self.store, "X-USDT", "1H")
        self.assertEqual(ds.gaps, [])
        self.assertLessEqual(ds.first_ts, since)
        self.assertEqual(ds.last_ts, ex.all_ts[-1])         # формирующаяся свеча не попала
        self.assertEqual(rep["pages"], 4)                    # 1001 строка: 300+300+300 до since
        self.assertEqual(ex.calls[0], None)
        self.assertEqual(ex.calls[1], int(ex.history_candles("X", "1H")[299][0]))

    def test_repeat_download_is_idempotent_and_cheap(self):
        ex = FakeExchange(T0, 800)
        download_candles(self.store, ex, "X-USDT", "1H", T0, now_ms=ex.now_ms)
        n_before = self.store.ts_range("X-USDT", "1H")[2]
        ex.calls.clear()
        rep = download_candles(self.store, ex, "X-USDT", "1H", T0, now_ms=ex.now_ms)
        self.assertEqual(rep["inserted"], 0)
        self.assertEqual(len(ex.calls), 1)                   # одна страница до перекрытия
        self.assertEqual(self.store.ts_range("X-USDT", "1H")[2], n_before)

    def test_incremental_new_and_older_history(self):
        old = FakeExchange(T0, 1000)
        old.all_ts = old.all_ts[500:700]                     # в базе середина истории
        download_candles(self.store, old, "X-USDT", "1H", old.all_ts[0], now_ms=old.now_ms)
        ex = FakeExchange(T0, 1000)
        rep = download_candles(self.store, ex, "X-USDT", "1H", T0, now_ms=ex.now_ms)
        ds = load_dataset(self.store, "X-USDT", "1H")
        self.assertEqual((ds.first_ts, ds.last_ts, len(ds.bars)), (T0, ex.all_ts[-1], 1000))
        self.assertEqual(rep["inserted"], 800)

    def test_interrupted_download_hole_is_refilled(self):
        ex = FakeExchange(T0, 1000)
        now = ex.now_ms
        rows = [row(ts) for ts in ex.all_ts[:300] + ex.all_ts[700:]]
        self.store.upsert_candles("X-USDT", "1H", rows, now_ms=now)
        self.assertEqual(len(load_dataset(self.store, "X-USDT", "1H").gaps), 1)
        rep = download_candles(self.store, ex, "X-USDT", "1H", T0, now_ms=now)
        self.assertEqual(load_dataset(self.store, "X-USDT", "1H").gaps, [])
        self.assertEqual(rep["exchange_gaps"], 0)

    def test_exchange_gap_reported_not_invented(self):
        ex = FakeExchange(T0, 600, missing=range(200, 203))
        rep = download_candles(self.store, ex, "X-USDT", "1H", T0, now_ms=ex.now_ms)
        ds = load_dataset(self.store, "X-USDT", "1H")
        self.assertEqual(ds.gaps, [Gap(T0 + 200 * H, T0 + 202 * H, 3)])
        self.assertEqual(rep["exchange_gaps"], 1)


class PublicClientTest(unittest.TestCase):
    def _client(self, payloads):
        self.sleeps, self.t = [], [0.0]
        queue = list(payloads)

        def opener(url, timeout):
            self.urls.append(url)
            return json.dumps(queue.pop(0)).encode()

        def sleep(s):
            self.sleeps.append(s)
            self.t[0] += s

        self.urls = []
        return OkxPublicClient(min_interval=0.25, sleep=sleep, clock=lambda: self.t[0],
                               opener=opener)

    def test_throttle_spacing(self):
        c = self._client([{"code": "0", "data": []}] * 3)
        for _ in range(3):
            c.history_candles("BTC-USDT", "1H", after=123)
        self.assertEqual(len(self.sleeps), 2)
        self.assertTrue(all(abs(s - 0.25) < 1e-9 for s in self.sleeps))
        self.assertIn("after=123", self.urls[0])
        self.assertIn("limit=300", self.urls[0])

    def test_error_code_raises(self):
        c = self._client([{"code": "51001", "msg": "Instrument ID does not exist", "data": []}])
        with self.assertRaises(OkxApiError):
            c.history_candles("NOPE-USDT", "1H")

    def test_rate_limit_code_retried(self):
        c = self._client([{"code": "50011", "msg": "Too Many Requests"},
                          {"code": "0", "data": [row(T0)]}])
        self.assertEqual(len(c.history_candles("BTC-USDT", "1H")), 1)
        self.assertEqual(c.requests, 2)

    def test_instrument_parsed(self):
        c = self._client([{"code": "0", "data": [{"tickSz": "0.1", "lotSz": "0.00000001",
                                                  "minSz": "0.00001"}]}])
        spec, _ = c.instrument("BTC-USDT")
        self.assertEqual(spec, InstrumentSpec("BTC-USDT", 0.1, 1e-8, 1e-5))


if __name__ == "__main__":
    unittest.main()
