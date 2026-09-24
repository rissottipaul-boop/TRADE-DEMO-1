"""Риск-ядро: equity и HWM ведёт только update_equity (задача RISK-PNL-DOUBLE).

Баланс биржи по рынку уже содержит PnL открытой позиции. Раньше record_pnl
прибавлял PnL сделки к этой equity ещё раз: прибыль у пика завышала HWM, убыток
вычитался дважды, и глобальный breaker −15% срабатывал уже при 9–11% реальной
просадки (insights/backtest-baseline.md, «Вывод»). Теперь record_pnl ведёт только
day_pnl, серии убытков и блокировки, а вход без свежего equity запрещён
(risk.EQUITY_MAX_AGE_S): иначе без фида equity глобальный breaker не сработал бы.

Без сети. Часы риск-ядра — фейковые (risk._utc_now), в бэктесте — модельные.
"""
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock

from src import risk
from src.backtest.data import Bar, InstrumentSpec
from src.backtest.engine import Backtest, CostModel, Strategy
from src.backtest.report import breaker_trips
from src.backtest.risk_sim import SimRisk
from src.dca_bot import DCABot
from src.order_router import OrderRouter
from tests.test_order_owner import FakeExchange

T0 = datetime(2026, 9, 24, 6, 0, tzinfo=timezone.utc).timestamp()  # середина суток UTC
INST = "ETH-USDT"
OTHER = "BTC-USDT"


class _Clock:
    """Часы риск-ядра: время двигает тест."""

    def __init__(self, t: float) -> None:
        self.t = t

    def __call__(self) -> float:
        return self.t


class _RiskCase(unittest.TestCase):
    """Риск-ядро на временной БД и фейковых часах."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.clock = _Clock(T0)
        patcher = mock.patch.object(risk, "_utc_now", self.clock)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.db = Path(self._tmp.name) / "risk.db"
        risk.init(self.db)

    def tearDown(self):
        risk.init(Path(self._tmp.name) / "unused.db")  # отпустить файл до очистки
        self._tmp.cleanup()

    def now(self) -> datetime:
        return datetime.fromtimestamp(self.clock.t, tz=timezone.utc)

    def tick(self, seconds: float) -> None:
        self.clock.t += seconds


class EquityOnlyFromBalanceTest(_RiskCase):
    """record_pnl не меняет equity и HWM: их источник — только update_equity."""

    def test_profit_at_peak_does_not_inflate_hwm(self):
        risk.update_equity(10_000)
        risk.register_entry(INST, "buy")
        risk.update_equity(11_000)  # позиция в плюсе на 1 000 — прибыль уже в балансе
        risk.record_pnl(INST, 1_000, self.now())  # закрыли у пика
        st = risk.status()
        self.assertEqual(st["hwm"], 11_000, "HWM не растёт на PnL сделки второй раз (было 12 000)")
        self.assertEqual(st["equity"], 11_000, "equity — из баланса, а не баланс + PnL")
        self.assertEqual(st["day_pnl"], 1_000)
        risk.update_equity(11_000)  # баланс после закрытия: PnL реализован, сумма та же
        self.assertEqual(risk.status()["hwm"], 11_000)

    def test_loss_is_not_subtracted_twice(self):
        risk.update_equity(10_000)
        risk.register_entry(INST, "buy")
        risk.update_equity(9_700)  # нереализованный убыток −300 уже в балансе
        events = risk.record_pnl(INST, -300, self.now())
        st = risk.status()
        self.assertEqual(events, [])
        self.assertEqual(st["equity"], 9_700, "убыток не вычтен из equity повторно (было 9 400)")
        self.assertEqual(st["hwm"], 10_000)
        self.assertEqual(st["day_pnl"], -300)
        self.assertEqual(st["open_risk"], [], "слот позиции освобождён, как и раньше")

    def test_record_pnl_global_check_uses_balance_equity(self):
        """Глобальный breaker в record_pnl — по текущей equity из update_equity."""
        risk.update_equity(10_000)
        self.assertIn("global_breaker", risk.update_equity(8_400))  # −16% по балансу
        risk.reset_breaker("global", by="test")  # сброс человеком, просадка осталась
        events = risk.record_pnl(INST, -10, self.now())
        self.assertEqual(events, ["global_breaker"])
        self.assertEqual(risk.status()["equity"], 8_400)


class GlobalBreakerThresholdTest(_RiskCase):
    """Breaker ровно на −15% реальной просадки от HWM по update_equity, не раньше.

    До фикса record_pnl(+2 000) у пика поднимал HWM до 14 000: просадка 12% от
    настоящего пика (10 560) была −24.6% от ложного HWM, и breaker срабатывал.
    """

    def _profit_at_peak(self) -> float:
        risk.update_equity(10_000)
        risk.register_entry(INST, "buy")
        risk.update_equity(12_000)
        risk.record_pnl(INST, 2_000, self.now())
        risk.update_equity(12_000)
        hwm = risk.status()["hwm"]
        self.assertEqual(hwm, 12_000)
        return hwm

    def test_drawdown_12pct_after_profit_at_peak_no_breaker(self):
        hwm = self._profit_at_peak()
        self.assertEqual(risk.update_equity(hwm * 0.88), [])  # −12%
        self.assertFalse(risk.status()["global_breaker"])
        allowed, reason = risk.check_entry_allowed(OTHER, "buy")
        self.assertTrue(allowed, reason)

    def test_breaker_exactly_at_15pct_real_drawdown(self):
        hwm = self._profit_at_peak()
        threshold = hwm * (1 - risk.GLOBAL_DD_LIMIT_PCT / 100.0)  # 10 200, формула ядра
        self.assertEqual(risk.update_equity(threshold + 1), [])  # −14.99%: ещё нет
        self.assertFalse(risk.status()["global_breaker"])
        self.assertEqual(risk.update_equity(threshold), ["global_breaker"])  # ровно −15%
        allowed, reason = risk.check_entry_allowed(OTHER, "buy")
        self.assertFalse(allowed)
        self.assertIn("глобальный", reason)


class DayPnlAndStreaksTest(_RiskCase):
    """day_pnl, серии и блокировки работают как раньше; equity при этом не меняется."""

    def test_daily_limit_by_day_pnl(self):
        risk.update_equity(10_000)
        self.assertEqual(risk.record_pnl(INST, -300, self.now()), [])
        self.assertEqual(risk.record_pnl("SOL-USDT", -300, self.now()), ["daily_limit"])  # −6%
        allowed, reason = risk.check_entry_allowed(OTHER, "buy")
        self.assertFalse(allowed)
        self.assertIn("дневной", reason)
        st = risk.status()
        self.assertEqual((st["equity"], st["day_pnl"]), (10_000, -600))

    def test_three_losses_block_instrument(self):
        risk.update_equity(10_000)
        events = [risk.record_pnl(INST, -10, self.now()) for _ in range(3)]
        self.assertEqual(events, [[], [], ["instrument_blocked"]])
        allowed, reason = risk.check_entry_allowed(INST, "buy")
        self.assertFalse(allowed)
        self.assertIn("заблокирован", reason)
        self.assertTrue(risk.check_entry_allowed(OTHER, "buy")[0])

    def test_five_losses_system_pause(self):
        risk.update_equity(10_000)
        events = [risk.record_pnl(f"X{i}-USDT", -10, self.now()) for i in range(5)]
        self.assertEqual(events[-1], ["system_pause"])
        self.assertTrue(all(e == [] for e in events[:-1]), events)
        allowed, reason = risk.check_entry_allowed(OTHER, "buy")
        self.assertFalse(allowed)
        self.assertIn("пауза", reason)
        self.assertEqual(risk.status()["equity"], 10_000)


class EquityFreshnessTest(_RiskCase):
    """Вход запрещён, если equity не выставлялась или старше EQUITY_MAX_AGE_S."""

    def assert_denied(self, fragment: str) -> None:
        allowed, reason = risk.check_entry_allowed(OTHER, "buy")
        self.assertFalse(allowed)
        self.assertIn(fragment, reason)
        self.assertIn("update_equity", reason, "причина ведёт к фиду equity")

    def test_never_set_equity_denies_entry(self):
        self.assert_denied("ни разу")
        st = risk.status()
        self.assertIsNone(st["equity_updated_at"])
        self.assertIsNone(st["equity_age_s"])

    def test_stale_equity_denies_until_fresh_update(self):
        risk.update_equity(10_000)
        self.tick(risk.EQUITY_MAX_AGE_S)  # ровно 10 мин — ещё свежая
        self.assertTrue(risk.check_entry_allowed(OTHER, "buy")[0])
        self.tick(1)
        self.assert_denied("устарела")
        self.assertEqual(risk.status()["equity_age_s"], risk.EQUITY_MAX_AGE_S + 1)
        risk.update_equity(10_000)  # фид ожил
        allowed, reason = risk.check_entry_allowed(OTHER, "buy")
        self.assertTrue(allowed, reason)
        self.assertEqual(risk.status()["equity_age_s"], 0)

    def test_record_pnl_does_not_refresh_equity(self):
        risk.update_equity(10_000)
        self.tick(risk.EQUITY_MAX_AGE_S + 1)
        risk.record_pnl(INST, 50, self.now())
        self.assert_denied("устарела")

    def test_freshness_mark_survives_restart(self):
        risk.update_equity(10_000)
        self.tick(60)
        risk.init(self.db)  # рестарт процесса
        self.assertEqual(risk.status()["equity_updated_at"], T0)
        self.assertTrue(risk.check_entry_allowed(OTHER, "buy")[0])
        self.tick(risk.EQUITY_MAX_AGE_S)  # после рестарта фид не пришёл
        self.assert_denied("устарела")

    def test_clock_moved_back_beyond_max_age_is_stale(self):
        risk.update_equity(10_000)
        self.tick(-5)  # мелкая коррекция часов не мешает
        self.assertTrue(risk.check_entry_allowed(OTHER, "buy")[0])
        self.tick(-risk.EQUITY_MAX_AGE_S)  # отметка «из будущего» на 10 мин 5 с
        self.assert_denied("устарела")

    def test_non_finite_equity_is_ignored(self):
        risk.update_equity(10_000)
        self.tick(risk.EQUITY_MAX_AGE_S + 1)
        self.assertEqual(risk.update_equity(float("nan")), [])
        st = risk.status()
        self.assertEqual((st["equity"], st["equity_updated_at"]), (10_000, T0))
        self.assert_denied("устарела")

    def test_breakers_reported_before_staleness(self):
        risk.trip_breaker("test", scope="global")  # equity не выставлялась
        allowed, reason = risk.check_entry_allowed(OTHER, "buy")
        self.assertFalse(allowed)
        self.assertIn("глобальный", reason)


class EquityFeedConsumersTest(_RiskCase):
    """OrderRouter и DCABot при устаревшем equity ордер не ставят.

    DCABot при equity = None (баланс не прочитан) update_equity не зовёт: вход
    решает свежесть equity в общем риск-состоянии, кто бы её ни выставил.
    """

    def _router(self, ex: FakeExchange) -> OrderRouter:
        router = OrderRouter(ex, db_path=Path(self._tmp.name) / "bot.db")
        self.addCleanup(router.close)
        return router

    def _run_bot(self) -> tuple[str, FakeExchange]:
        ex = FakeExchange()
        router = self._router(ex)
        bot = DCABot(ex, router, max_buys=1, interval_sec=0, demo=True, storage=router.storage,
                     equity_fn=lambda: None, sleep=lambda s: None)
        return bot.run(), ex

    def test_router_rejects_order_on_stale_equity(self):
        ex = FakeExchange()
        risk.update_equity(10_000)
        self.tick(risk.EQUITY_MAX_AGE_S + 1)
        result = self._router(ex).place_order("BTC/USDT", "buy", "limit", px=30_000.0, sz=0.001)
        self.assertEqual((result["ok"], result["stage"]), (False, "check_entry_allowed"))
        self.assertIn("устарела", result["reason"])
        self.assertEqual(ex.created, [])

    def test_no_equity_ever_pauses_without_orders(self):
        final, ex = self._run_bot()
        self.assertEqual(final, DCABot.STATE_PAUSED)
        self.assertEqual(ex.created, [])

    def test_stale_equity_pauses_without_orders(self):
        risk.update_equity(10_000)  # последний фид — 10 мин 1 с назад
        self.tick(risk.EQUITY_MAX_AGE_S + 1)
        final, ex = self._run_bot()
        self.assertEqual(final, DCABot.STATE_PAUSED)
        self.assertEqual(ex.created, [])

    def test_fresh_equity_from_other_feed_allows_buy(self):
        risk.update_equity(10_000)  # например, движок минуту назад
        self.tick(60)
        final, ex = self._run_bot()
        self.assertEqual(final, DCABot.STATE_DONE)
        self.assertEqual(len(ex.created), 1)


# --- Бэктест: тот же код ядра, модельное время ---

H_MS = 3_600_000
BT_T0 = 1_767_225_600_000  # 2026-01-01 00:00 UTC
SPEC = InstrumentSpec("TEST-USDT", tick_sz=0.01, lot_sz=1e-8, min_sz=1e-5)
ZERO_COSTS = CostModel().scaled(fee_mult=0.0, slip_mult=0.0)


def _bars(closes):
    """Бары без гэпов: open = прошлый close."""
    out, prev = [], closes[0]
    for i, c in enumerate(closes):
        out.append(Bar(BT_T0 + i * H_MS, prev, max(prev, c), min(prev, c), c, 1.0))
        prev = c
    return out


# Рост 100 -> 200 (бары 2–11), фиксация прибыли у пика (сигнал на баре 11),
# повторный вход у пика (сигнал на баре 12), падение 200 -> 66 (бары 13–32).
# Позиция — 15% equity: пик кривой 11 500, дно 10 344.25 — просадка 10.05%.
PEAK_THEN_DRAWDOWN = ([100.0, 100.0] + [100.0 + 10 * k for k in range(1, 11)] + [200.0]
                      + [round(200.0 - 6.7 * k, 2) for k in range(1, 21)] + [66.0] * 3)


class _ProfitAtPeakThenHold(Strategy):
    name = "profit_at_peak"

    def on_bar(self, ctx):
        if ctx.i == 11:
            ctx.close("peak")
        if ctx.i in (0, 12):
            ctx.buy(equity_pct=15.0)


def _max_drawdown_pct(curve) -> float:
    peak, worst = 0.0, 0.0
    for _, eq in curve:
        peak = max(peak, eq)
        worst = max(worst, 1.0 - eq / peak)
    return worst * 100.0


class BacktestEquitySourceTest(unittest.TestCase):
    """Сценарий из backtest-baseline: прибыльная сделка у пика, затем просадка ~10%."""

    def _run(self):
        return Backtest(_bars(PEAK_THEN_DRAWDOWN), _ProfitAtPeakThenHold(), SPEC,
                        costs=ZERO_COSTS).run()

    def test_profit_at_peak_then_10pct_drawdown_no_breaker(self):
        r = self._run()
        self.assertEqual(len(r.trades), 2)
        self.assertGreater(r.trades[0].pnl, 1_000)  # прибыль зафиксирована у пика
        self.assertAlmostEqual(_max_drawdown_pct(r.curve), 10.05, places=2)
        self.assertNotIn("global_breaker", [e for _, e in r.risk_events])
        self.assertFalse([k for k in r.rejections if "breaker" in k], r.rejections)

    def test_breaker_not_before_real_drawdown(self):
        """Порог ужесточён до 5% (только тест): breaker ровно на 5% просадки кривой.
        До фикса ложный HWM 13 000 давал срабатывание сразу после фиксации прибыли."""
        with mock.patch.object(risk, "GLOBAL_DD_LIMIT_PCT", 5.0):
            r = self._run()
        trips = breaker_trips(r)
        self.assertEqual(len(trips), 1, r.risk_events)
        self.assertGreaterEqual(trips[0][1], 5.0)
        self.assertLess(trips[0][1], 5.1)

    def test_simrisk_hwm_follows_balance(self):
        sim = SimRisk(INST)
        try:
            with sim.activate():
                sim.now = BT_T0 / 1000.0
                sim.update_equity(10_000)
                sim.register_entry(1.0)
                sim.update_equity(11_500)
                sim.record_pnl(1_500)  # закрыли у пика: equity ядра не меняется
                self.assertEqual((sim.status()["equity"], sim.status()["hwm"]), (11_500, 11_500))
                sim.now += 3600
                self.assertEqual(sim.update_equity(10_350), [])  # −10% от пика
                self.assertTrue(sim.check_entry()[0])
        finally:
            sim.close()

    def test_flat_equity_keeps_feed_fresh(self):
        """Equity без движения много баров: вход не отклоняется как «устаревший»."""
        class LateBuy(Strategy):
            name = "late_buy"

            def on_bar(self, ctx):
                if ctx.i == 30:
                    ctx.buy(equity_pct=10.0)

        r = Backtest(_bars([100.0] * 40), LateBuy(), SPEC).run()
        self.assertEqual(len(r.trades), 1, r.rejections)
        self.assertEqual(r.rejections, {})


if __name__ == "__main__":
    unittest.main()
