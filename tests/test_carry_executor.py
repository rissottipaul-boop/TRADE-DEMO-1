"""Тесты carry execution layer (M1). Моки, ноль сети, ноль реальной БД риска."""
import math
import unittest
from unittest.mock import MagicMock

from src import carry_executor, order_owner
from src.carry_executor import (CarryPlan, build_pair, decide_partial_fill,
                                funding_exit_signal, pair_sizes, place_pair,
                                synthetic_stop)
from src.funding_carry import CarryLegs


class FakeRisk:
    """Стенд-ин src.risk: чистая математика сайзинга + программируемые гейты."""

    DEFAULT_RISK_PCT = 1.0
    MAX_POSITION_PCT = 15.0

    def __init__(self, deny: tuple[str, ...] = ()):
        self.deny = set(deny)
        self.calls: list[tuple[str, str]] = []

    def check_entry_allowed(self, inst_id, side):
        self.calls.append((inst_id, side))
        if inst_id in self.deny:
            return False, f"стенд: {inst_id} запрещён"
        return True, "ok"

    def size_position(self, equity, entry, stop, ct_val, lot_sz, min_sz, risk_pct=1.0):
        # Та же формула, что src.risk.size_position (риск + потолок 15% + floor
        # к lotSz + min_sz + анти-паттерн 30%), без БД.
        dollar_risk = equity * risk_pct / 100.0
        contracts = math.floor(dollar_risk / (abs(entry - stop) * ct_val))
        cap = math.floor((equity * self.MAX_POSITION_PCT / 100.0) / (entry * ct_val))
        warnings = []
        if contracts > cap:
            warnings.append(f"обрезан потолком {cap}")
            contracts = cap
        if lot_sz > 0:
            contracts = math.floor(contracts / lot_sz) * lot_sz
        if contracts < min_sz:
            warnings.append(f"{contracts} < min_sz {min_sz}")
            contracts = 0.0
        notional = contracts * ct_val * entry
        if notional > equity * 0.30:
            warnings.append("notional > 30% equity")
            contracts, notional = 0.0, 0.0
        return {"size": float(contracts), "dollar_risk": dollar_risk,
                "notional": notional, "warnings": warnings}


def _legs(**kw):
    args = {"spot_inst_id": "BTC-USDT", "swap_inst_id": "BTC-USDT-SWAP"}
    args.update(kw)
    return CarryLegs(**args)


def _build_args(**kw):
    args = {"headline_apy": 0.0575, "planned_hold_days": 30.0, "spot_px": 80000.0,
            "equity": 10000.0, "acct_lv": 2, "spot_lot_sz": 0.00001,
            "swap_ct_val": 0.01, "swap_lot_sz": 1.0}
    args.update(kw)
    return args


class OwnerTagTest(unittest.TestCase):
    def test_owner_registered_and_own(self):
        owner = order_owner.require(carry_executor.OWNER, own=True)
        self.assertTrue(owner.own)

    def test_botcar_registered_strategy(self):
        # Follow-up 1: `botcar` зарегистрирован (kind=strategy), OWNER переключён
        # с `botr` на код стратегии (AGENTS.md §6, реестр — источник правды).
        owner = order_owner.get("botcar")
        self.assertEqual(owner.kind, "strategy")
        self.assertTrue(owner.own)
        self.assertEqual(carry_executor.OWNER, "botcar")
        self.assertEqual(carry_executor.OWNER, order_owner.CARRY)


class SyntheticStopTest(unittest.TestCase):
    def test_cap_binds_deterministically(self):
        # Дистанция риск/потолок: риск-подразумеваемый размер = потолку 15%.
        # Канон «контракт = 1 лот»: ct_val=лот, иначе floor даст 0 целых монет.
        entry, risk_pct, cap, lot = 80000.0, 1.0, 15.0, 0.00001
        stop = synthetic_stop(entry, risk_pct, cap)
        self.assertAlmostEqual(stop, entry * (1 - risk_pct / cap))
        fake = FakeRisk()
        sized = fake.size_position(10000.0, entry, stop, lot, 1.0, 0.0, risk_pct)
        self.assertGreater(sized["size"], 0)
        self.assertAlmostEqual(sized["notional"], 10000.0 * cap / 100.0, delta=1.0)

    def test_bad_inputs_raise(self):
        with self.assertRaises(ValueError):
            synthetic_stop(0.0, 1.0, 15.0)
        with self.assertRaises(ValueError):
            synthetic_stop(100.0, 0.0, 15.0)


class PairSizesTest(unittest.TestCase):
    def test_delta_neutral_exact(self):
        fake = FakeRisk()
        out = pair_sizes(equity=10000.0, spot_px=80000.0, spot_lot_sz=0.00001,
                         swap_ct_val=0.01, swap_lot_sz=1.0, risk_mod=fake)
        self.assertGreater(out["spot_sz"], 0)
        self.assertGreater(out["swap_contracts"], 0)
        # Базовые количества совпадают точно: спот = контракты × ctVal.
        self.assertAlmostEqual(out["spot_sz"], out["swap_contracts"] * 0.01)
        # 15% cap = 0.01875 BTC → 1 своп-контракт (0.01) → спот подогнан к 0.01.
        self.assertAlmostEqual(out["spot_sz"], 0.01)
        self.assertLessEqual(out["spot_notional"], 10000.0 * 0.15)

    def test_min_sz_blocks_whole_pair(self):
        fake = FakeRisk()
        out = pair_sizes(equity=10000.0, spot_px=80000.0, swap_ct_val=0.01,
                         swap_lot_sz=1.0, swap_min_sz=10 ** 9, risk_mod=fake)
        self.assertEqual((out["spot_sz"], out["swap_contracts"]), (0.0, 0.0))
        self.assertTrue(out["warnings"])

    def test_bad_ct_val_raises(self):
        with self.assertRaises(ValueError):
            pair_sizes(equity=10000.0, spot_px=80000.0, swap_ct_val=0.0,
                       risk_mod=FakeRisk())


class BuildPairTest(unittest.TestCase):
    def test_happy_path_placeable(self):
        plan = build_pair(_legs(), risk_mod=FakeRisk(), **_build_args())
        self.assertIsInstance(plan, CarryPlan)
        self.assertEqual(plan.gates, ())
        self.assertTrue(plan.placeable)
        self.assertEqual((plan.spot.side, plan.swap.side), ("buy", "sell"))
        self.assertEqual(plan.spot.ord_type, "limit")
        self.assertEqual(plan.owner, carry_executor.OWNER)

    def test_legs_gate_acct_lv(self):
        plan = build_pair(_legs(), risk_mod=FakeRisk(), **_build_args(acct_lv=1))
        self.assertFalse(plan.placeable)
        self.assertTrue(any("acctLv" in g for g in plan.gates))

    def test_legs_gate_unpaired(self):
        plan = build_pair(_legs(spot_inst_id="ETH-USDT"), risk_mod=FakeRisk(),
                          **_build_args())
        self.assertFalse(plan.placeable)
        self.assertTrue(any("парные" in g for g in plan.gates))

    def test_economics_gate_short_hold(self):
        plan = build_pair(_legs(), risk_mod=FakeRisk(),
                          **_build_args(planned_hold_days=5.0))
        self.assertFalse(plan.placeable)
        self.assertTrue(any("экономика" in g for g in plan.gates))

    def test_risk_gate_both_legs_checked(self):
        fake = FakeRisk(deny=("BTC-USDT-SWAP",))
        plan = build_pair(_legs(), risk_mod=fake, **_build_args())
        self.assertFalse(plan.placeable)
        self.assertTrue(any("BTC-USDT-SWAP" in g for g in plan.gates))
        self.assertIn(("BTC-USDT", "buy"), fake.calls)
        self.assertIn(("BTC-USDT-SWAP", "sell"), fake.calls)

    def test_sizing_gate_zero_size(self):
        plan = build_pair(_legs(), risk_mod=FakeRisk(),
                          **_build_args(equity=1.0, swap_lot_sz=10 ** 9))
        self.assertFalse(plan.placeable)
        self.assertTrue(any("сайзинг" in g for g in plan.gates))

    def test_bad_ord_type_and_px_raise(self):
        with self.assertRaises(ValueError):
            build_pair(_legs(), risk_mod=FakeRisk(), **_build_args(ord_type="stop"))
        with self.assertRaises(ValueError):
            build_pair(_legs(), risk_mod=FakeRisk(), **_build_args(spot_px=0.0))


class PlacePairTest(unittest.TestCase):
    def test_dry_run_default_no_placement(self):
        plan = build_pair(_legs(), risk_mod=FakeRisk(), **_build_args())
        router = MagicMock()
        res = place_pair(plan, router)  # dry_run по умолчанию
        self.assertTrue(res["ok"] and res["dry_run"])
        router.place_order.assert_not_called()

    def test_live_call_places_spot_then_swap_with_owner(self):
        plan = build_pair(_legs(), risk_mod=FakeRisk(), **_build_args())
        router = MagicMock()
        router.place_order.side_effect = [
            {"ok": True, "order_id": "spot1", "cl_ord_id": "botr1"},
            {"ok": True, "order_id": "swap1", "cl_ord_id": "botr2"},
        ]
        res = place_pair(plan, router, dry_run=False)
        self.assertTrue(res["ok"])
        self.assertEqual(router.place_order.call_count, 2)
        first, second = router.place_order.call_args_list
        self.assertEqual(first.args[:3], ("BTC-USDT", "buy", "limit"))
        self.assertEqual(second.args[:3], ("BTC-USDT-SWAP", "sell", "limit"))
        self.assertEqual(first.kwargs["owner"], carry_executor.OWNER)
        self.assertEqual(second.kwargs["owner"], carry_executor.OWNER)
        # Дельта-нейтральность выставленного: базовые количества равны.
        self.assertAlmostEqual(first.kwargs["sz"], second.kwargs["sz"] * 0.01)

    def test_blocked_plan_never_places(self):
        plan = build_pair(_legs(), risk_mod=FakeRisk(), **_build_args(acct_lv=1))
        router = MagicMock()
        res = place_pair(plan, router, dry_run=False)
        self.assertFalse(res["ok"])
        router.place_order.assert_not_called()

    def test_swap_reject_yields_exit_spot_decision(self):
        plan = build_pair(_legs(), risk_mod=FakeRisk(), **_build_args())
        router = MagicMock()
        router.place_order.side_effect = [
            {"ok": True, "order_id": "spot1", "cl_ord_id": "botr1"},
            {"ok": False, "stage": "check_entry_allowed", "reason": "стенд"},
        ]
        res = place_pair(plan, router, dry_run=False)
        self.assertFalse(res["ok"])
        self.assertEqual(res["decision"].action, "exit_spot")

    def test_spot_reject_skips_swap(self):
        plan = build_pair(_legs(), risk_mod=FakeRisk(), **_build_args())
        router = MagicMock()
        router.place_order.return_value = {"ok": False, "reason": "стенд"}
        res = place_pair(plan, router, dry_run=False)
        self.assertFalse(res["ok"])
        self.assertEqual(router.place_order.call_count, 1)
        self.assertEqual(res["decision"].action, "abort_pair")


class PartialFillTest(unittest.TestCase):
    def test_matrix(self):
        self.assertEqual(decide_partial_fill(spot_fill=1.0, swap_fill=1.0).action,
                         "hold_pair")
        self.assertEqual(decide_partial_fill(spot_fill=0.0, swap_fill=0.0).action,
                         "abort_pair")
        self.assertEqual(decide_partial_fill(spot_fill=1.0, swap_fill=0.0).action,
                         "exit_spot")
        self.assertEqual(decide_partial_fill(spot_fill=0.0, swap_fill=1.0).action,
                         "exit_swap")
        self.assertEqual(decide_partial_fill(spot_fill=0.5, swap_fill=0.5).action,
                         "wait")
        self.assertEqual(decide_partial_fill(spot_fill=0.7, swap_fill=0.4).action,
                         "exit_spot")
        self.assertEqual(decide_partial_fill(spot_fill=0.3, swap_fill=0.9).action,
                         "exit_swap")

    def test_bad_inputs_raise(self):
        for bad in (-0.1, 1.5, float("nan"), "1"):
            with self.assertRaises(ValueError, msg=repr(bad)):
                decide_partial_fill(spot_fill=bad, swap_fill=0.5)


class FundingExitTest(unittest.TestCase):
    def test_negative_exits(self):
        sig = funding_exit_signal(recent_mean_per_period=-0.0001, planned_hold_days=30)
        self.assertTrue(sig.exit)

    def test_below_amortization_exits(self):
        # Порог taker/30дн = 0.0030/90 ≈ 0.0000333/период.
        sig = funding_exit_signal(recent_mean_per_period=0.00001, planned_hold_days=30)
        self.assertTrue(sig.exit)

    def test_healthy_holds(self):
        # BTC §2.1: 0.00525%/период = 0.0000525 — выше порога taker/30дн.
        sig = funding_exit_signal(recent_mean_per_period=0.0000525, planned_hold_days=30)
        self.assertFalse(sig.exit)

    def test_bad_inputs_raise(self):
        with self.assertRaises(ValueError):
            funding_exit_signal(recent_mean_per_period=0.001, planned_hold_days=0)
        with self.assertRaises(ValueError):
            funding_exit_signal(recent_mean_per_period=float("nan"),
                                planned_hold_days=30)


if __name__ == "__main__":
    unittest.main()
