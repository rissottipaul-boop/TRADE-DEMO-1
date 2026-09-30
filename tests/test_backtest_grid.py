"""Тесты сеточного симулятора бэктестера (GRID-BT-SIM).

Спецификация: insights/grid-strategy-design.md §8.1–8.2:
1. Fee-floor pre-check: отклонение конфига с fee_share > 0.5 до прогона;
2. Anti-lookahead для уровней: усечение ряда баров не меняет решения и филлы на префиксе;
3. Entries/day изоляция (§6.1): >= 20 исполнений за день -> entries_today <= 1;
4. Hard stop на трендовом отрезке (§3.1): «low раньше high», отмена ордеров, ликвидация, record_pnl, cooldown 4ч;
5. Non-triggering на боковике: отсутствие ложных срабатываний hard stop внутри диапазона.
"""
import math
import unittest

from src.backtest.data import Bar, InstrumentSpec
from src.backtest.engine import Backtest, CostModel
from src.backtest.grid import GridConfig, GridStrategy

H = 3_600_000
T0 = 1_767_225_600_000  # 2026-01-01 00:00 UTC
SPEC = InstrumentSpec("BTC-USDT", tick_sz=0.1, lot_sz=1e-5, min_sz=1e-4)


def bars_from(ohlc, t0=T0):
    return [Bar(t0 + i * H, o, h, l, c, 1.0) for i, (o, h, l, c) in enumerate(ohlc)]


class GridBacktestTest(unittest.TestCase):
    def test_fee_floor_precheck(self):
        """Fee-floor pre-check отклоняет нерентабельные сетки (fee_share > 0.5) до прогона."""
        # 1. 35 уровней на диапазоне 80225-88670 (шаг ~0.286% < 0.32%, fee_share ~0.559 > 0.5)
        with self.assertRaises(ValueError) as ctx:
            GridConfig(min_px=80225, max_px=88670, grid_num=35, maker_fee=0.0008)
        self.assertIn("fee_share", str(ctx.exception))
        self.assertIn("слишком мал", str(ctx.exception))

        # 2. Неверные границы и число уровней
        with self.assertRaises(ValueError):
            GridConfig(min_px=90000, max_px=80000, grid_num=20)
        with self.assertRaises(ValueError):
            GridConfig(min_px=-100, max_px=100, grid_num=20)
        with self.assertRaises(ValueError):
            GridConfig(min_px=80000, max_px=88000, grid_num=1)

        # 3. Валидный конфиг: шаг ~0.476%, fee_share ~0.336 <= 0.5
        cfg = GridConfig(min_px=80000, max_px=88000, grid_num=20, maker_fee=0.0008)
        self.assertLessEqual(cfg.fee_share, 0.5)
        self.assertEqual(len(cfg.levels), 21)
        self.assertAlmostEqual(cfg.levels[0], 80000.0)
        self.assertAlmostEqual(cfg.levels[-1], 88000.0)

    def test_anti_lookahead_slicing(self):
        """Anti-lookahead: усечение ряда не меняет решения и филлы на префиксе."""
        cfg = GridConfig(min_px=80000, max_px=90000, grid_num=10, quote_sz=1000.0)
        # Колебания 83000-87000 вокруг центра 85000
        ohlc = []
        for i in range(30):
            p = 85000 + 3000 * math.sin(i * 0.5)
            ohlc.append((p, p + 500, p - 500, p))
        bars = bars_from(ohlc)

        # Полный прогон
        strat_full = GridStrategy(cfg)
        res_full = Backtest(bars, strat_full, SPEC, initial_cash=10000.0).run()

        # Прогоны на префиксах k = 10, 15, 20, 25
        for k in (10, 15, 20, 25):
            strat_sliced = GridStrategy(cfg)
            res_sliced = Backtest(bars[:k], strat_sliced, SPEC, initial_cash=10000.0).run()
            cut_ts = bars[k - 1].ts

            # Сравниваем решения до конца префикса, исключая события завершения данных
            dec_full = [d for d in res_full.decisions
                        if d.ts < cut_ts and "end_of_data" not in d.outcome and d.action != "cancel_resting_limits"]
            dec_sliced = [d for d in res_sliced.decisions
                          if d.ts < cut_ts and "end_of_data" not in d.outcome and d.action != "cancel_resting_limits"]
            self.assertEqual(dec_full, dec_sliced, f"Несовпадение решений на срезе k={k}")

            # Сравниваем филлы до конца префикса
            fills_full = [(f.ts, f.side, round(f.qty, 5), round(f.exec_px, 1))
                          for f in res_full.fills if f.ts < cut_ts]
            fills_sliced = [(f.ts, f.side, round(f.qty, 5), round(f.exec_px, 1))
                            for f in res_sliced.fills if f.ts < cut_ts]
            self.assertEqual(fills_full, fills_sliced, f"Несовпадение филлов на срезе k={k}")

    def test_entries_today_isolation(self):
        """Сетка с >=20 исполнениями за модельный день инкрементирует entries_today не более чем на 1."""
        cfg = GridConfig(min_px=80000, max_px=90000, grid_num=10, quote_sz=1000.0)
        # 24 бара в рамках одних суток (с 00:00 до 23:00 UTC) с частыми осцилляциями
        ohlc = []
        for i in range(24):
            # Размах колебаний внутри каждого бара пересекает несколько уровней
            ohlc.append((85000, 89500, 80500, 85000))
        bars = bars_from(ohlc, t0=T0)

        strat = GridStrategy(cfg)
        bt = Backtest(bars, strat, SPEC, initial_cash=10000.0, record_state=True)
        res = bt.run()

        # Проверяем, что исполнений было много (>= 20)
        self.assertGreaterEqual(len(res.fills), 20)

        # Проверяем, что entries_today в риск-ядре остался <= 1
        # (register_entry вызвался 1 раз на старте, а все перестановки шли с count_as_entry=False)
        last_state = res.states[-1]
        sim_state = last_state[2]
        entries_today = sim_state[0]
        self.assertLessEqual(entries_today, 1,
                             f"entries_today = {entries_today} > 1 при {len(res.fills)} исполнениях")

    def test_hard_stop_on_trend(self):
        """Пробой min_px на трендовом отрезке вызывает hard stop: отмена уровней, ликвидация, блок 4ч."""
        cfg = GridConfig(min_px=80000, max_px=90000, grid_num=10, quote_sz=1000.0)
        # Старт в диапазоне (85000), затем резкий пробой вниз до 75000
        ohlc = [
            (85000, 85500, 84500, 85000),
            (85000, 85200, 82000, 82500),  # buys filled
            (82500, 82600, 78000, 78500),  # low 78000 < min_px 80000 -> hard stop!
            (78500, 79000, 75000, 76000),
        ]
        bars = bars_from(ohlc, t0=T0)

        strat = GridStrategy(cfg)
        bt = Backtest(bars, strat, SPEC, initial_cash=10000.0)
        res = bt.run()

        # Hard stop сработал
        self.assertTrue(strat.stopped)
        self.assertEqual(strat.stop_reason, "min_px_breached")

        # Все активные лимиты отменены
        self.assertEqual(len(strat.active_levels), 0)
        self.assertEqual(len(bt.resting_orders), 0)

        # Инвентарь ликвидирован на момент срабатывания стопа
        self.assertEqual(bt.inventory_qty, 0.0)

        # Зафиксирован Fill ликвидации
        liq_fills = [f for f in res.fills if "hard_stop" in f.reason]
        self.assertTrue(len(liq_fills) > 0)

        # Инструмент заблокирован на 4 часа в риск-ядре: событие instrument_blocked в risk_events
        stop_time_s = (T0 + 2 * H) / 1000.0
        self.assertIn((stop_time_s, "instrument_blocked"), res.risk_events)

    def test_non_triggering_on_sideways(self):
        """На спокойном боковике внутри диапазона hard stop не срабатывает."""
        cfg = GridConfig(min_px=80000, max_px=90000, grid_num=10, quote_sz=1000.0)
        # 50 баров осцилляций внутри [82000, 88000], строго внутри [80000, 90000]
        ohlc = []
        for i in range(50):
            mid = 85000 + 2000 * math.sin(i * 0.4)
            ohlc.append((mid, mid + 1000, mid - 1000, mid))
        bars = bars_from(ohlc, t0=T0)

        strat = GridStrategy(cfg)
        res = Backtest(bars, strat, SPEC, initial_cash=10000.0).run()

        # Стоп не сработал
        self.assertFalse(strat.stopped)
        self.assertEqual(strat.stop_reason, "")

        # Завершены арбитражи
        self.assertGreater(strat.arbitrage_count, 0)
        self.assertGreater(len(res.fills), 0)

    def test_hard_stop_on_trend_upward(self):
        """Пробой max_px вверх вызывает hard stop с причиной max_px_breached."""
        cfg = GridConfig(min_px=80000, max_px=90000, grid_num=10, quote_sz=1000.0)
        ohlc = [
            (85000, 85500, 84500, 85000),
            (85000, 88000, 84800, 87500),
            (87500, 91500, 87000, 91000),  # high 91500 > max_px 90000 -> hard stop!
            (91000, 93000, 90500, 92000),
        ]
        bars = bars_from(ohlc, t0=T0)
        strat = GridStrategy(cfg)
        bt = Backtest(bars, strat, SPEC, initial_cash=10000.0)
        res = bt.run()

        self.assertTrue(strat.stopped)
        self.assertEqual(strat.stop_reason, "max_px_breached")
        self.assertEqual(len(bt.resting_orders), 0)

    def test_hard_stop_double_breach_low_before_high(self):
        """При одновременном пробое low < min_px и high > max_px побеждает 'low раньше high' (min_px_breached)."""
        cfg = GridConfig(min_px=80000, max_px=90000, grid_num=10, quote_sz=1000.0)
        ohlc = [
            (85000, 85500, 84500, 85000),
            # Экстремальный бар: low 78000 < min_px 80000 И high 92000 > max_px 90000
            (85000, 92000, 78000, 86000),
        ]
        bars = bars_from(ohlc, t0=T0)
        strat = GridStrategy(cfg)
        res = Backtest(bars, strat, SPEC, initial_cash=10000.0).run()

        self.assertTrue(strat.stopped)
        self.assertEqual(strat.stop_reason, "min_px_breached")

    def test_record_pnl_single_call_on_hard_stop(self):
        """При срабатывании hard stop record_pnl вызывается ровно один раз за всю сессию (on_finish не дублирует)."""
        from unittest import mock
        from src.backtest.engine import Context
        cfg = GridConfig(min_px=80000, max_px=90000, grid_num=10, quote_sz=1000.0)
        ohlc = [
            (85000, 85500, 84500, 85000),
            (85000, 85200, 82000, 82500),
            (82500, 82600, 78000, 78500),  # hard stop
            (78500, 79000, 75000, 76000),
            (76000, 77000, 75500, 76500),
        ]
        bars = bars_from(ohlc, t0=T0)
        strat = GridStrategy(cfg)
        orig_record_pnl = Context.record_pnl
        with mock.patch.object(Context, "record_pnl", autospec=True, side_effect=orig_record_pnl) as spy:
            res = Backtest(bars, strat, SPEC, initial_cash=10000.0).run()
            self.assertEqual(spy.call_count, 1, f"record_pnl вызван {spy.call_count} раз(а), ожидался ровно 1")


if __name__ == "__main__":
    unittest.main()
