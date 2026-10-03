"""Unit-тесты для модуля src.grid_bot (GRID-IMPL).

Проверяет:
- Range-гейт (§1, dca-bot-parameterizer);
- Сайзинг quoteSz через risk.size_position() (§6.3);
- Изоляцию entries_today (§6.1) при множественных арбитражах;
- Двусторонний hard stop (§3.1) и план остановки;
- Единый агрегированный record_pnl по итогам сессии (§6.2);
- Cooldown 4ч (block_instrument) при выходе за границы диапазона.
"""
import math
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import MagicMock

from src import order_owner, risk
from src.grid_bot import (
    REGIME_MILD_TREND,
    REGIME_RANGE,
    REGIME_STRONG_TREND,
    GridBot,
    calculate_atr,
    calculate_ema,
    check_range_gate,
    classify_market_regime,
    size_grid_session,
)
from src.grid_engine import GridConfig, build_levels, stop_decision, validate_config


class RangeGateTest(unittest.TestCase):
    """Тесты классификатора режима рынка и Range-гейта (§1)."""

    def test_ema_and_atr_calculation(self):
        closes = [100.0 + i for i in range(30)]
        highs = [c + 2.0 for c in closes]
        lows = [c - 2.0 for c in closes]

        ema = calculate_ema(closes, period=20)
        self.assertEqual(len(ema), 30)
        self.assertGreater(ema[-1], ema[0])

        atr = calculate_atr(highs, lows, closes, period=14)
        self.assertEqual(len(atr), 30)
        self.assertAlmostEqual(atr[-1], 4.0, delta=0.5)

    def test_range_regime_oscillating(self):
        # Колебания вокруг базовой цены
        candles = []
        base = 85000.0
        for i in range(40):
            c = base + math.sin(i * 0.5) * 50.0
            candles.append({"h": c + 20.0, "l": c - 20.0, "c": c})

        regime, details = classify_market_regime(candles)
        self.assertEqual(regime, REGIME_RANGE)
        ok, reason, _ = check_range_gate(candles)
        self.assertTrue(ok)
        self.assertIn("Range", reason)

    def test_strong_trend_up(self):
        # Мощный восходящий тренд
        candles = []
        p = 80000.0
        for i in range(45):
            p += 250.0
            candles.append({"h": p + 50.0, "l": p - 20.0, "c": p})

        regime, details = classify_market_regime(candles)
        self.assertEqual(regime, REGIME_STRONG_TREND)
        ok, reason, _ = check_range_gate(candles)
        self.assertFalse(ok)
        self.assertIn("отклонено", reason)

    def test_strong_trend_down(self):
        # Мощный нисходящий тренд
        candles = []
        p = 90000.0
        for i in range(45):
            p -= 250.0
            candles.append({"h": p + 20.0, "l": p - 50.0, "c": p})

        regime, details = classify_market_regime(candles)
        self.assertEqual(regime, REGIME_STRONG_TREND)
        ok, reason, _ = check_range_gate(candles)
        self.assertFalse(ok)
        self.assertIn("отклонено", reason)

    def test_insufficient_candles(self):
        # При недостатке свечей возвращается безопасный дефолт
        candles = [{"h": 80100.0, "l": 79900.0, "c": 80000.0} for _ in range(10)]
        regime, _ = classify_market_regime(candles)
        self.assertEqual(regime, REGIME_RANGE)


class GridSizingTest(unittest.TestCase):
    """Тесты сайзинга quoteSz (§6.3)."""

    def test_sizing_within_limits(self):
        equity = 10000.0
        res = size_grid_session(
            equity=equity,
            current_px=84000.0,
            min_px=80000.0,
            max_px=88000.0,
            ct_val=0.01,
            lot_sz=0.001,
            min_sz=0.001,
            risk_pct=1.0,
        )
        self.assertTrue(res["ok"])
        self.assertGreater(res["quote_sz"], 0.0)
        # notional <= 15% equity
        self.assertLessEqual(res["quote_sz"], equity * 0.15)

    def test_sizing_invalid_inputs(self):
        res = size_grid_session(equity=-100.0, current_px=84000.0, min_px=80000.0, max_px=88000.0)
        self.assertFalse(res["ok"])
        res2 = size_grid_session(equity=1000.0, current_px=0.0, min_px=80000.0, max_px=88000.0)
        self.assertFalse(res2["ok"])


class GridSessionLifecycleTest(unittest.TestCase):
    """Тесты сессии, изоляции entries_today и агрегированного record_pnl."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.db_path = Path(self._tmp.name) / "risk_test.db"
        risk.init(self.db_path)
        risk.update_equity(10000.0)

        self.placed_orders = []

        class FakeRouter:
            owner = order_owner.GRID

            def place_order(_self, inst_id, side, ord_type, px=None, sz=None,
                            owner=None, count_as_entry=True, is_exit=False):
                self.placed_orders.append({
                    "inst_id": inst_id, "side": side, "px": px, "sz": sz,
                    "count_as_entry": count_as_entry, "is_exit": is_exit,
                    "owner": owner
                })
                if count_as_entry:
                    risk.register_entry(inst_id, side)
                return {
                    "ok": True,
                    "order_id": f"ord_{len(self.placed_orders)}",
                    "cl_ord_id": f"botg_{len(self.placed_orders)}"
                }

            def cancel_order(_self, inst_id, cl_id):
                return {"ok": True}

        self.router = FakeRouter()
        self.cfg = GridConfig(
            inst_id="BTC-USDT",
            min_px=80225.0,
            max_px=88670.0,
            grid_num=30,  # шаг > 0.32%
            investment_quote=1000.0,
        )

    def tearDown(self):
        risk.init(Path(self._tmp.name) / "unused.db")
        self._tmp.cleanup()

    def test_start_and_isolation(self):
        bot = GridBot(exchange=None, router=self.router, config=self.cfg)
        status_init = risk.status()
        self.assertEqual(status_init["entries_today"], 0)

        # 1. Запуск сессии
        start_res = bot.start_session(current_px=84000.0, equity=10000.0)
        self.assertTrue(start_res["ok"])
        self.assertEqual(bot.state, GridBot.STATE_RUNNING)

        # entries_today увеличился РОВНО НА 1
        self.assertEqual(risk.status()["entries_today"], 1)

        # Все начальные уровни выставлены с count_as_entry=False
        for order in self.placed_orders:
            self.assertFalse(order["count_as_entry"])
            self.assertEqual(order["owner"], order_owner.GRID)

        # 2. Множество арбитражных филлов
        for i in range(30):
            # buy fill на уровне 12
            bot.on_level_fill(level_idx=12, side="buy", fill_px=83000.0, fill_qty=0.0004)
            # sell fill на уровне 13
            bot.on_level_fill(level_idx=13, side="sell", fill_px=83300.0, fill_qty=0.0004)

        # entries_today ПО-ПРЕЖНЕМУ 1! (§6.1 доказан)
        self.assertEqual(risk.status()["entries_today"], 1)
        self.assertEqual(bot.arbitrages_count, 30)
        self.assertGreater(bot.realized_arbitrage_pnl, 0.0)

        # 3. Штатная остановка сессии
        stop_res = bot.stop_session(reason="plan_completed", current_px=84000.0)
        self.assertTrue(stop_res["ok"])
        self.assertEqual(bot.state, GridBot.STATE_STOPPED)

        # record_pnl вызван ровно один раз: PnL записан в day_pnl, слот позиции освобожден
        self.assertAlmostEqual(risk.status()["day_pnl"], stop_res["total_session_pnl"], delta=0.01)
        self.assertIsNone(risk.open_position("BTC-USDT"))

    def test_hard_stop_breakout(self):
        bot = GridBot(exchange=None, router=self.router, config=self.cfg)
        bot.start_session(current_px=84000.0, equity=10000.0)

        # Внутри диапазона — monitoring
        m1 = bot.check_market(mid_px=84000.0)
        self.assertEqual(m1["action"], "monitoring")
        self.assertEqual(m1["decision"], "none")

        # Гэп за пределы min_px больше чем на 1 шаг сетки — немедленный hard stop!
        m2 = bot.check_market(mid_px=self.cfg.min_px - 400.0)
        self.assertEqual(m2["action"], "hard_stop")
        self.assertEqual(bot.state, GridBot.STATE_STOPPED)

        # Инструмент заблокирован на 4ч
        self.assertTrue(risk.is_instrument_blocked("BTC-USDT"))


if __name__ == "__main__":
    unittest.main()
