"""Тесты архиватора funding OKX (CARRY-FUNDING-ARCHIVE) на фейковых ответах, без сети."""
import io
import json
import tempfile
import unittest
from contextlib import closing, redirect_stdout, redirect_stderr
from pathlib import Path

from src import funding_archive as fa

H8 = 8 * 3600 * 1000
DAY = 86_400_000
T0 = 1_790_000_000_000 - (1_790_000_000_000 % H8)  # кратно 8 ч


class FakeApi:
    """Имитация funding-rate-history: от новых к старым, `after` — строки СТАРШЕ курсора,
    окно ограничено `depth` последними периодами (как ≈ 94 дня у OKX)."""

    def __init__(self, inst_times: dict[str, list[int]], depth: int = 282, page_cap: int = 100):
        self.inst_times = {k: sorted(v, reverse=True)[:depth] for k, v in inst_times.items()}
        self.page_cap = page_cap
        self.calls: list[dict] = []

    def get(self, path, params):
        assert path == fa.ENDPOINT, path
        self.calls.append(dict(params))
        assert "before" not in params, "пагинация назад только через after"
        times = self.inst_times.get(params["instId"], [])
        if "after" in params:
            times = [t for t in times if t < int(params["after"])]
        limit = min(int(params.get("limit", 100)), self.page_cap)
        return [row(params["instId"], t) for t in times[:limit]]


def row(inst, t, rate=None):
    r = rate if rate is not None else f"0.0000{(t // H8) % 97:02d}"
    return {"instId": inst, "instType": "SWAP", "fundingTime": str(t), "fundingRate": r,
            "realizedRate": r, "method": "current_period", "formulaType": "withRate"}


def series(n, end=T0):
    return [end - i * H8 for i in range(n)]


class FundingArchiveTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = Path(self.tmp.name) / "data" / "funding_history.db"

    def tearDown(self):
        self.tmp.cleanup()

    def conn(self):
        return closing(fa.connect(self.db))

    def count(self, inst):
        with self.conn() as c:
            return c.execute("SELECT COUNT(*) FROM funding_rate WHERE inst_id=?", (inst,)).fetchone()[0]

    # --- пагинация ---

    def test_pagination_uses_after_cursor_until_empty_page(self):
        api = FakeApi({"BTC-USDT-SWAP": series(282)})
        with self.conn() as c:
            res = fa.sync_instrument(c, api, "BTC-USDT-SWAP", now_ms=T0)
        self.assertEqual(res.fetched, 282)
        self.assertEqual(res.inserted, 282)
        self.assertEqual(res.pages, 4)  # 100 + 100 + 82 + пустая
        self.assertNotIn("after", api.calls[0])
        times = series(282)
        self.assertEqual(api.calls[1]["after"], str(times[99]))   # самая старая строка 1-й страницы
        self.assertEqual(api.calls[2]["after"], str(times[199]))
        self.assertEqual(api.calls[3]["after"], str(times[281]))
        self.assertEqual(res.window_oldest, times[-1])
        self.assertEqual(res.window_newest, times[0])
        self.assertEqual(res.warnings, [])

    def test_short_pages_still_paginate(self):
        # API может отдать меньше limit — пагинация продолжается до пустой страницы.
        api = FakeApi({"ETH-USDT-SWAP": series(50)}, page_cap=7)
        with self.conn() as c:
            res = fa.sync_instrument(c, api, "ETH-USDT-SWAP", now_ms=T0)
        self.assertEqual(res.fetched, 50)
        self.assertEqual(res.pages, 9)  # 7×7 + 1 + пустая

    def test_cursor_not_moving_back_is_error(self):
        class Stuck:
            def get(self, path, params):  # поведение `before`: та же страница снова
                return [row("BTC-USDT-SWAP", T0), row("BTC-USDT-SWAP", T0 - H8)]
        with self.conn() as c:
            with self.assertRaises(fa.FundingArchiveError):
                fa.sync_instrument(c, Stuck(), "BTC-USDT-SWAP", now_ms=T0)
        self.assertEqual(self.count("BTC-USDT-SWAP"), 0)

    # --- пустая страница ---

    def test_empty_first_page_writes_nothing(self):
        api = FakeApi({})
        with self.conn() as c:
            res = fa.sync_instrument(c, api, "BTC-USDT-SWAP", now_ms=T0)
        self.assertEqual((res.pages, res.fetched, res.inserted), (1, 0, 0))
        self.assertEqual(res.warnings, [])
        with self.conn() as c:
            st = fa.status(c, ["BTC-USDT-SWAP"], now_ms=T0)[0]
        self.assertEqual(st["periods"], 0)
        self.assertIn("архив пуст", st["warnings"][0])

    def test_empty_window_after_previous_data_warns(self):
        with self.conn() as c:
            fa.sync_instrument(c, FakeApi({"BTC-USDT-SWAP": series(10)}), "BTC-USDT-SWAP", now_ms=T0)
            res = fa.sync_instrument(c, FakeApi({}), "BTC-USDT-SWAP", now_ms=T0 + H8)
        self.assertEqual(self.count("BTC-USDT-SWAP"), 10)
        self.assertTrue(any("пустое окно" in w for w in res.warnings))

    # --- дубли и дозаполнение ---

    def test_rerun_gives_no_duplicates(self):
        api = FakeApi({"BTC-USDT-SWAP": series(282), "ETH-USDT-SWAP": series(282)})
        with self.conn() as c:
            fa.sync(c, api, now_ms=T0)
            second = fa.sync(c, api, now_ms=T0 + 1000)
        self.assertEqual([r.inserted for r in second], [0, 0])
        self.assertEqual([r.fetched for r in second], [282, 282])
        self.assertEqual(self.count("BTC-USDT-SWAP"), 282)
        self.assertEqual(self.count("ETH-USDT-SWAP"), 282)

    def test_duplicate_rows_inside_response_are_collapsed(self):
        class Dup:
            def get(self, path, params):
                if "after" in params:
                    return []
                return [row("BTC-USDT-SWAP", T0), row("BTC-USDT-SWAP", T0), row("BTC-USDT-SWAP", T0 - H8)]
        with self.conn() as c:
            res = fa.sync_instrument(c, Dup(), "BTC-USDT-SWAP", now_ms=T0)
        self.assertEqual((res.fetched, res.inserted), (2, 2))

    def test_existing_rows_are_not_rewritten(self):
        inst = "BTC-USDT-SWAP"
        with self.conn() as c:
            fa.sync_instrument(c, FakeApi({inst: series(5)}), inst, now_ms=T0)
            changed = FakeApi({inst: series(5)})
            changed.get_orig = changed.get
            changed.get = lambda p, q: [dict(r, fundingRate="0.0099") for r in changed.get_orig(p, q)]
            res = fa.sync_instrument(c, changed, inst, now_ms=T0 + 1)
            rates = {r for _, r in fa.load_series(c, inst)}
        self.assertEqual(res.inserted, 0)
        self.assertNotIn("0.0099", rates)  # подтверждённый период финален

    def test_gap_shorter_than_window_is_backfilled(self):
        inst = "ETH-USDT-SWAP"
        full = series(282 + 90, end=T0 + 90 * H8)  # «будущее» на 30 дней вперёд
        old_window = [t for t in full if t <= T0][:282]
        new_window = sorted(full, reverse=True)[:282]
        with self.conn() as c:
            fa.sync_instrument(c, FakeApi({inst: old_window}), inst, now_ms=T0)
            res = fa.sync_instrument(c, FakeApi({inst: new_window}), inst, now_ms=T0 + 30 * DAY)
            times = [t for t, _ in fa.load_series(c, inst)]
            st = fa.status(c, [inst], now_ms=T0 + 30 * DAY)[0]
        self.assertEqual(res.inserted, 90)
        self.assertEqual(res.warnings, [])
        self.assertEqual(len(times), 282 + 90)  # архив глубже окна API
        self.assertEqual(fa.find_gaps(times), [])
        self.assertEqual((st["gaps"], st["warnings"]), (0, []))
        self.assertGreater(st["days"], 94)

    def test_hole_inside_window_is_filled_on_next_run(self):
        inst = "BTC-USDT-SWAP"
        times = series(100)
        holey = times[:30] + times[60:]
        with self.conn() as c:
            fa.sync_instrument(c, FakeApi({inst: holey}), inst, now_ms=T0)
            self.assertEqual(len(fa.find_gaps(t for t, _ in fa.load_series(c, inst))), 1)
            res = fa.sync_instrument(c, FakeApi({inst: times}), inst, now_ms=T0 + 1)
            self.assertEqual(fa.find_gaps(t for t, _ in fa.load_series(c, inst)), [])
        self.assertEqual(res.inserted, 30)

    def test_gap_longer_than_window_warns(self):
        inst = "BTC-USDT-SWAP"
        with self.conn() as c:
            fa.sync_instrument(c, FakeApi({inst: series(282)}), inst, now_ms=T0)
            later = series(282, end=T0 + 400 * H8)  # окно API ушло на 133 дня вперёд
            res = fa.sync_instrument(c, FakeApi({inst: later}), inst, now_ms=T0 + 400 * H8)
            st = fa.status(c, [inst], now_ms=T0 + 400 * H8)[0]
        self.assertEqual(res.inserted, 282)
        self.assertTrue(any("невосстановимый разрыв" in w for w in res.warnings))
        self.assertEqual(st["gaps"], 1)

    # --- проверка ответа ---

    def test_foreign_inst_or_bad_rate_aborts_without_writing(self):
        class Bad:
            def __init__(self, raw):
                self.raw = raw

            def get(self, path, params):
                return [] if "after" in params else [row("BTC-USDT-SWAP", T0), self.raw]
        for raw in (row("ETH-USDT-SWAP", T0 - H8), row("BTC-USDT-SWAP", T0 - H8, rate=""),
                    row("BTC-USDT-SWAP", T0 - H8, rate="NaN"), dict(row("BTC-USDT-SWAP", 1), fundingTime="x")):
            with self.subTest(raw=raw), self.conn() as c:
                with self.assertRaises(fa.FundingArchiveError):
                    fa.sync_instrument(c, Bad(raw), "BTC-USDT-SWAP", now_ms=T0)
        self.assertEqual(self.count("BTC-USDT-SWAP"), 0)

    def test_stale_archive_warns_in_status(self):
        inst = "BTC-USDT-SWAP"
        with self.conn() as c:
            fa.sync_instrument(c, FakeApi({inst: series(10)}), inst, now_ms=T0)
            fresh = fa.status(c, [inst], now_ms=T0 + 29 * DAY)[0]
            stale = fa.status(c, [inst], now_ms=T0 + 31 * DAY)[0]
        self.assertEqual(fresh["warnings"], [])
        self.assertIn("старше 30 дней", stale["warnings"][0])

    # --- CLI ---

    def test_cli_sync_and_status_exit_codes(self):
        api = FakeApi({"BTC-USDT-SWAP": series(282), "ETH-USDT-SWAP": series(282)})
        out = io.StringIO()
        with redirect_stdout(out):
            code = fa.main(["sync", "--db", str(self.db), "--json"], client=api, now_ms=T0)
        self.assertEqual(code, 0)
        payload = json.loads(out.getvalue())
        self.assertEqual([r["inserted"] for r in payload["sync"]], [282, 282])
        with redirect_stdout(io.StringIO()):
            self.assertEqual(fa.main(["status", "--db", str(self.db)], now_ms=T0 + 31 * DAY), 1)

    def test_cli_api_error_is_exit_2(self):
        class Boom:
            def get(self, path, params):
                raise RuntimeError("code=50011")
        with redirect_stderr(io.StringIO()) as err:
            self.assertEqual(fa.main(["sync", "--db", str(self.db)], client=Boom(), now_ms=T0), 2)
        self.assertIn("50011", err.getvalue())


if __name__ == "__main__":
    unittest.main()
