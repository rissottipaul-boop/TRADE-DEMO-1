"""РўРµСЃС‚С‹ mean-reversion runner (src/mr_trader.py). Р¤РµР№РєРѕРІС‹Рµ Р±Р°СЂС‹ Рё РјРѕРє-СЂРѕСѓС‚РµСЂ,
Р±РµР· СЃРµС‚Рё/Р±РёСЂР¶Рё. РћСЂРґРµСЂР° РЅРёРіРґРµ РЅРµ РІС‹СЃС‚Р°РІР»СЏСЋС‚СЃСЏ: live-РїСѓС‚СЊ РёРґС‘С‚ РІ FakeRouter."""
import math
import random
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from src import order_owner, risk
from src import mean_reversion as mr
from src.backtest.data import Bar
from src.mr_trader import DEFAULT_OWNER, MRParams, MRTrader

H = 3_600_000
T0 = 1_767_225_600_000  # 2026-01-01 00:00 UTC
EQUITY = 10_000.0


def flat_bars(n, px=100.0, vol=10.0, t0=T0):
    return [Bar(t0 + i * H, px, px, px, px, vol) for i in range(n)]


def ar_bars(n, seed=1, vol=0.01, t0=T0):
    """Р¦РµРЅР° 100В·exp(x), x вЂ” AR(1) (С‚Р° Р¶Рµ СЃРёРЅС‚РµС‚РёРєР°, С‡С‚Рѕ РІ tests/test_meanrev.py)."""
    rng = random.Random(seed)
    out, x, prev = [], 0.0, 100.0
    for i in range(n):
        x = 0.97 * x + rng.gauss(0.0, vol)
        c = round(100.0 * math.exp(x), 2)
        h = round(max(prev, c) * (1 + abs(rng.gauss(0.0, vol)) / 2), 2)
        lo = round(min(prev, c) * (1 - abs(rng.gauss(0.0, vol)) / 2), 2)
        out.append(Bar(t0 + i * H, prev, h, lo, c, 1.0))
        prev = c
    return out


def _mr_entry_last_only(bars, ind, i=-1):
    """Р¤РѕСЂСЃРёСЂРѕРІР°РЅРЅС‹Р№ РІС…РѕРґ С‚РѕР»СЊРєРѕ РЅР° 201-Рј Р±Р°СЂРµ Р±СѓС„РµСЂР° (side_effect РґР»СЏ РјРѕРєР°)."""
    return len(bars) == 201


def _bt_entry_last_only(rsi, mid, bars, i, level=30.0):
    """РўРѕ Р¶Рµ РґР»СЏ РєР°Р»РёР±СЂРѕРІР°РЅРЅРѕРіРѕ РїСѓС‚Рё С‡РµСЂРµР· backtest.meanrev.entry_signal."""
    return len(bars) == 201


class FakeRouter:
    """РњРѕРє OrderRouter: РїРёС€РµС‚ РІС‹Р·РѕРІС‹, РІ СЃРµС‚СЊ РЅРµ С…РѕРґРёС‚."""

    def __init__(self, entry=None, exit=None):
        self.entries: list[dict] = []
        self.exits: list[dict] = []
        self.settled: list = []
        self._entry = entry
        self._exit = exit

    def place_order(self, inst_id, side, ord_type, px=None, sz=None, **kw):
        rec = {"inst_id": inst_id, "side": side, "ord_type": ord_type,
               "px": px, "sz": sz, **kw}
        self.entries.append(rec)
        if self._entry is not None:
            return dict(self._entry)
        return {"ok": True, "exit": False, "order_id": f"e{len(self.entries)}",
                "status": "closed", "sz": sz}

    def place_exit_order(self, inst_id, side, ord_type, px=None, sz=None, **kw):
        rec = {"inst_id": inst_id, "side": side, "ord_type": ord_type,
               "px": px, "sz": sz, **kw}
        self.exits.append(rec)
        if self._exit is not None:
            return dict(self._exit)
        return {"ok": True, "exit": True, "order_id": f"x{len(self.exits)}",
                "status": "closed", "filled": sz, "pending": False,
                "remaining": 0.0, "closed": True}

    def settle_exits(self, inst_id=None):
        self.settled.append(inst_id)
        return [{"ord_id": "x1", "inst_id": inst_id, "status": "closed"}]


class RiskCase(unittest.TestCase):
    """РЎРІРµР¶РµРµ СЂРёСЃРє-СЏРґСЂРѕ РІРѕ РІСЂРµРјРµРЅРЅРѕРј SQLite + СЃРІРµР¶РёР№ С„РёРґ equity."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        risk.init(Path(self._tmp.name) / "risk.db")
        risk.update_equity(EQUITY)

    def tearDown(self):
        risk.init(Path(self._tmp.name) / "unused.db")
        self._tmp.cleanup()

    def feed(self, trader, bars, **kw):
        kw.setdefault("equity", EQUITY)
        kw.setdefault("ct_val", 1.0)
        kw.setdefault("lot_sz", 0.001)
        kw.setdefault("min_sz", 0.001)
        out = None
        for b in bars:
            out = trader.on_bar(b, **kw)
        return out


class DefaultsTest(RiskCase):
    def test_dry_run_default_true(self):
        trader = MRTrader()
        self.assertTrue(trader.dry_run)
        self.assertIsNone(trader.router)

    def test_default_owner_is_registered_bot(self):
        self.assertEqual(DEFAULT_OWNER, order_owner.MR)
        self.assertEqual(MRTrader().owner, "botmr")
        order_owner.require(DEFAULT_OWNER, own=True)  # РЅРµ ValueError

    def test_owner_validation(self):
        with self.assertRaises(ValueError):
            MRTrader(owner="trd")  # Р°РіРµРЅС‚СЃРєРёР№ РїСЂРµС„РёРєСЃ вЂ” РЅРµ bot*
        self.assertEqual(MRTrader(owner="botmr").owner, "botmr")
        self.assertEqual(order_owner.get("botmr").kind, "strategy")
        with self.assertRaises(ValueError):
            MRTrader(owner="nope")

    def test_live_requires_router(self):
        with self.assertRaises(ValueError):
            MRTrader(dry_run=False)


class WarmupHoldTest(RiskCase):
    def test_warmup_decisions(self):
        trader = MRTrader()
        last = self.feed(trader, flat_bars(199))
        self.assertEqual(last["decision"], "warmup")
        self.assertEqual(last["bars"], 199)
        self.assertEqual(last["need"], mr.WARMUP)

    def test_flat_hold(self):
        trader = MRTrader()
        last = self.feed(trader, flat_bars(250))
        self.assertEqual(last["decision"], "hold")
        self.assertFalse(last["signal"])
        self.assertIsNone(trader.position)


class DryRunEntryTest(RiskCase):
    def test_genuine_signal_dry_trace(self):
        # РќР°СЃС‚РѕСЏС‰РёР№ СЃРёРіРЅР°Р» С‡РµСЂРµР· РїРµСЂРµРёСЃРїРѕР»СЊР·СѓРµРјС‹Рµ РёРЅРґРёРєР°С‚РѕСЂС‹: seed 4 РґР°С‘С‚
        # РїРµСЂРІС‹Р№ РІС…РѕРґ РЅР° РёРЅРґРµРєСЃРµ 201 (РїСЂРѕРІРµСЂРµРЅРѕ РїСЂРѕР±РѕР№ РїРѕРІРµСЂС… mean_reversion).
        trader = MRTrader()  # router=None вЂ” РІС‹СЃС‚Р°РІРёС‚СЊ РѕСЂРґРµСЂ С‚РµС…РЅРёС‡РµСЃРєРё РЅРµРєСѓРґР°
        bars = ar_bars(600, seed=4)
        decisions = [trader.on_bar(b, equity=EQUITY, ct_val=1.0,
                                   lot_sz=0.001, min_sz=0.001) for b in bars]
        self.assertEqual(len(trader.trace), 600)
        for d in decisions[:201]:
            self.assertIn(d["decision"], ("warmup", "hold"))
        sig = decisions[201]
        self.assertEqual(sig["decision"], "signal")
        self.assertTrue(sig["dry_run"])
        self.assertIsNone(sig["order"])
        self.assertLess(sig["stop_px"], sig["entry_px"])
        self.assertGreater(sig["sz"], 0.0)

    def test_forced_entry_trace_fields(self):
        router = FakeRouter()
        trader = MRTrader(router=router)  # dry-run: СЂРѕСѓС‚РµСЂ РЅРµ С‚СЂРѕРіР°РµРј
        with mock.patch("src.mean_reversion.check_entry", side_effect=_mr_entry_last_only):
            sig = self.feed(trader, ar_bars(201, seed=7))
        self.assertEqual(sig["decision"], "signal")
        self.assertEqual(sig["entry_px"], ar_bars(201, seed=7)[-1].c)
        self.assertLess(sig["stop_px"], sig["entry_px"])
        self.assertGreater(sig["sz"], 0.0)
        self.assertIn("notional", sig)
        self.assertEqual(sig["owner"], "botmr")
        self.assertIsNone(sig["order"])
        self.assertEqual(router.entries, [])
        self.assertIsNotNone(trader.position)

    def test_dry_run_does_not_register_entry(self):
        trader = MRTrader()
        with mock.patch("src.mean_reversion.check_entry", side_effect=_mr_entry_last_only):
            self.feed(trader, ar_bars(201, seed=7))
        self.assertEqual(trader.trace[-1]["decision"], "signal")
        self.assertEqual(risk.status()["entries_today"], 0)
        self.assertEqual(risk.status()["open_risk"], [])

    def test_entry_blocked_without_equity_feed(self):
        risk.init(Path(self._tmp.name) / "stale.db")  # Р±РµР· update_equity
        trader = MRTrader()
        with mock.patch("src.mean_reversion.check_entry", side_effect=_mr_entry_last_only):
            out = self.feed(trader, ar_bars(201, seed=7))
        self.assertEqual(out["decision"], "blocked")
        self.assertEqual(out["stage"], "check_entry_allowed")
        self.assertIsNone(trader.position)

    def test_entry_blocked_zero_size(self):
        trader = MRTrader()
        with mock.patch("src.mean_reversion.check_entry", side_effect=_mr_entry_last_only):
            out = self.feed(trader, ar_bars(201, seed=7), min_sz=1e9)
        self.assertEqual(out["decision"], "blocked")
        self.assertEqual(out["stage"], "size_position")
        self.assertIsNone(trader.position)


class LiveGateTest(RiskCase):
    def test_live_default_blocked_no_placement(self):
        router = FakeRouter()
        trader = MRTrader(router=router, dry_run=False)  # РґРµС„РѕР»С‚С‹ вЂ” СѓР±С‹С‚РѕС‡РЅС‹
        with mock.patch("src.mean_reversion.check_entry", side_effect=_mr_entry_last_only):
            out = self.feed(trader, ar_bars(201, seed=7))
        self.assertEqual(out["decision"], "blocked")
        self.assertEqual(out["stage"], "default_params_live")
        self.assertIn("MEANREV-CALIB", out["reason"])
        self.assertEqual(router.entries, [])
        self.assertIsNone(trader.position)

    def test_live_calibrated_places(self):
        router = FakeRouter()
        trader = MRTrader(router=router, dry_run=False,
                          params=MRParams(rsi_entry=25.0))
        with mock.patch("src.backtest.meanrev.entry_signal", side_effect=_bt_entry_last_only):
            out = self.feed(trader, ar_bars(201, seed=7))
        self.assertEqual(out["decision"], "entry")
        self.assertTrue(out["order"]["ok"])
        self.assertEqual(len(router.entries), 1)
        call = router.entries[0]
        self.assertEqual(call["side"], "buy")
        self.assertEqual(call["ord_type"], "market")
        self.assertEqual(call["owner"], "botmr")
        self.assertEqual(call["sz"], out["sz"])
        self.assertIsNotNone(trader.position)

    def test_live_entry_router_failure(self):
        router = FakeRouter(entry={"ok": False, "stage": "check_entry_allowed",
                                   "reason": "denied"})
        trader = MRTrader(router=router, dry_run=False,
                          params=MRParams(rsi_entry=25.0))
        with mock.patch("src.backtest.meanrev.entry_signal", side_effect=_bt_entry_last_only):
            out = self.feed(trader, ar_bars(201, seed=7))
        self.assertEqual(out["decision"], "blocked")
        self.assertEqual(out["stage"], "router:check_entry_allowed")
        self.assertIsNone(trader.position)


class ExitTest(RiskCase):
    def _dry_entry(self, trader, seed=7):
        with mock.patch("src.mean_reversion.check_entry", side_effect=_mr_entry_last_only):
            sig = self.feed(trader, ar_bars(201, seed=seed))
        self.assertEqual(sig["decision"], "signal")
        return sig

    def test_dry_stop_exit(self):
        trader = MRTrader()
        sig = self._dry_entry(trader)
        stop_bar = Bar(ar_bars(201, seed=7)[-1].ts + H, o=sig["entry_px"],
                       h=sig["entry_px"], l=sig["stop_px"] - 0.5,
                       c=sig["entry_px"], vol=5.0)
        out = trader.on_bar(stop_bar, equity=EQUITY)
        self.assertEqual(out["decision"], "exit")
        self.assertEqual(out["reason"], "stop")
        self.assertIsNone(out["order"])
        self.assertIsNone(trader.position)

    def test_dry_roi_exit(self):
        trader = MRTrader()
        sig = self._dry_entry(trader)
        entry = sig["entry_px"]
        roi_bar = Bar(ar_bars(201, seed=7)[-1].ts + H, o=entry, h=entry * 1.035,
                      l=entry * 1.01, c=entry * 1.03, vol=5.0)
        out = trader.on_bar(roi_bar, equity=EQUITY)
        self.assertEqual(out["decision"], "exit")
        self.assertEqual(out["reason"], "roi")
        self.assertIsNone(trader.position)

    def test_dry_time_stop_exit(self):
        trader = MRTrader()
        sig = self._dry_entry(trader)
        entry = sig["entry_px"]
        last_ts = ar_bars(201, seed=7)[-1].ts
        # Р’РµС‚РєСѓ РёРЅРґРёРєР°С‚РѕСЂРЅРѕРіРѕ РІС‹С…РѕРґР° РЅРµР№С‚СЂР°Р»РёР·СѓРµРј: СЃС‚РѕРї/ROI/С‚Р°Р№Рј-СЃС‚РѕРї РЅР°СЃС‚РѕСЏС‰РёРµ.
        with mock.patch("src.backtest.meanrev.exit_signal", return_value=False):
            out = None
            for k in range(1, 25):
                bar = Bar(last_ts + k * H, o=entry, h=entry, l=entry,
                          c=entry, vol=5.0)
                out = trader.on_bar(bar, equity=EQUITY)
                if out["decision"] == "exit":
                    break
        self.assertEqual(out["decision"], "exit")
        self.assertEqual(out["reason"], "time_stop")
        self.assertIsNone(trader.position)

    def test_live_exit_places(self):
        router = FakeRouter()
        trader = MRTrader(router=router, dry_run=False,
                          params=MRParams(rsi_entry=25.0))
        with mock.patch("src.backtest.meanrev.entry_signal", side_effect=_bt_entry_last_only):
            sig = self.feed(trader, ar_bars(201, seed=7))
        self.assertEqual(sig["decision"], "entry")
        stop_bar = Bar(ar_bars(201, seed=7)[-1].ts + H, o=sig["entry_px"],
                       h=sig["entry_px"], l=sig["stop_px"] - 0.5,
                       c=sig["entry_px"], vol=5.0)
        out = trader.on_bar(stop_bar, equity=EQUITY)
        self.assertEqual(out["decision"], "exit")
        self.assertEqual(out["reason"], "stop")
        self.assertEqual(len(router.exits), 1)
        self.assertEqual(router.exits[0]["side"], "sell")
        self.assertEqual(router.exits[0]["owner"], "botmr")
        self.assertEqual(router.exits[0]["sz"], sig["sz"])
        self.assertIsNone(trader.position)

    def test_live_exit_failure_keeps_position(self):
        router = FakeRouter(exit={"ok": False, "stage": "check_exit_allowed",
                                  "reason": "no slot"})
        trader = MRTrader(router=router, dry_run=False,
                          params=MRParams(rsi_entry=25.0))
        with mock.patch("src.backtest.meanrev.entry_signal", side_effect=_bt_entry_last_only):
            self.feed(trader, ar_bars(201, seed=7))
        pos = trader.position
        stop_bar = Bar(ar_bars(201, seed=7)[-1].ts + H, o=pos.entry_px,
                       h=pos.entry_px, l=pos.stop_px - 0.5,
                       c=pos.entry_px, vol=5.0)
        out = trader.on_bar(stop_bar, equity=EQUITY)
        self.assertEqual(out["decision"], "exit_failed")
        self.assertIs(trader.position, pos)

    def test_live_exit_pending_keeps_position(self):
        router = FakeRouter(exit={"ok": True, "exit": True, "order_id": "x1",
                                  "status": "open", "filled": 0.0,
                                  "pending": True, "remaining": 1.0,
                                  "closed": False})
        trader = MRTrader(router=router, dry_run=False,
                          params=MRParams(rsi_entry=25.0))
        with mock.patch("src.backtest.meanrev.entry_signal", side_effect=_bt_entry_last_only):
            self.feed(trader, ar_bars(201, seed=7))
        pos = trader.position
        stop_bar = Bar(ar_bars(201, seed=7)[-1].ts + H, o=pos.entry_px,
                       h=pos.entry_px, l=pos.stop_px - 0.5,
                       c=pos.entry_px, vol=5.0)
        out = trader.on_bar(stop_bar, equity=EQUITY)
        self.assertEqual(out["decision"], "exit")
        self.assertIs(trader.position, pos)  # Р»РёРјРёС‚РЅС‹Р№ РІС‹С…РѕРґ РЅРµ РёСЃРїРѕР»РЅРµРЅ


class SettleTest(RiskCase):
    def test_dry_settle_empty(self):
        trader = MRTrader(router=FakeRouter())
        self.assertEqual(trader.settle_exits(), [])

    def test_live_settle_delegates(self):
        router = FakeRouter()
        trader = MRTrader(router=router, dry_run=False,
                          params=MRParams(rsi_entry=25.0))
        reports = trader.settle_exits()
        self.assertEqual(router.settled, ["BTC-USDT"])
        self.assertEqual(reports[0]["status"], "closed")


class DelegationTest(RiskCase):
    """Р”РµС„РѕР»С‚РЅС‹Р№ РїСѓС‚СЊ РґРµР»РµРіРёСЂСѓРµС‚ mean_reversion/backtest, Р° РЅРµ РєРѕРїРёСЂСѓРµС‚ Р»РѕРіРёРєСѓ."""

    def test_default_entry_calls_mean_reversion(self):
        trader = MRTrader()
        with mock.patch("src.mean_reversion.check_entry",
                        return_value=False) as spy:
            self.feed(trader, ar_bars(201, seed=7))
        spy.assert_called()

    def test_default_exit_calls_mean_reversion(self):
        trader = MRTrader()
        with mock.patch("src.mean_reversion.check_entry", side_effect=_mr_entry_last_only):
            self.feed(trader, ar_bars(201, seed=7))
        bar = Bar(ar_bars(201, seed=7)[-1].ts + H, o=100.0, h=100.0,
                  l=100.0, c=100.0, vol=5.0)
        with mock.patch("src.mean_reversion.check_exit",
                        return_value=None) as spy:
            out = trader.on_bar(bar, equity=EQUITY)
        spy.assert_called_once()
        self.assertEqual(out["decision"], "hold")

    def test_custom_entry_uses_injected_level(self):
        trader = MRTrader(params=MRParams(rsi_entry=25.0))
        with mock.patch("src.backtest.meanrev.entry_signal",
                        return_value=False) as spy:
            self.feed(trader, ar_bars(201, seed=7))
        spy.assert_called()
        self.assertEqual(spy.call_args[0][4], 25.0)


class ParamsTest(RiskCase):
    def test_is_default(self):
        self.assertTrue(MRParams().is_default())
        self.assertFalse(MRParams(rsi_entry=25.0).is_default())
        self.assertFalse(MRParams(stop_mult=2.0).is_default())
        self.assertFalse(MRParams(roi_table=((0, 1.0),)).is_default())

    def test_stop_mult_injected(self):
        trader = MRTrader(params=MRParams(stop_mult=2.0))
        bars = ar_bars(201, seed=7)
        atr = mr.compute_indicators(bars).atr[-1]
        with mock.patch("src.mean_reversion.check_entry", side_effect=_mr_entry_last_only):
            sig = self.feed(trader, bars)
        self.assertAlmostEqual(sig["stop_px"], sig["entry_px"] - 2.0 * atr)

    def test_custom_time_stop(self):
        trader = MRTrader(params=MRParams(time_stop_min=60))
        bars = ar_bars(201, seed=7)
        with mock.patch("src.mean_reversion.check_entry", side_effect=_mr_entry_last_only):
            sig = self.feed(trader, bars)
        entry = sig["entry_px"]
        bar = Bar(bars[-1].ts + H, o=entry, h=entry, l=entry, c=entry, vol=5.0)
        out = trader.on_bar(bar, equity=EQUITY)
        self.assertEqual(out["decision"], "exit")
        self.assertEqual(out["reason"], "time_stop")

    def test_atr_math_sane(self):
        bars = ar_bars(201, seed=7)
        atr = mr.compute_indicators(bars).atr[-1]
        self.assertTrue(math.isfinite(atr) and atr > 0)


if __name__ == "__main__":
    unittest.main()
