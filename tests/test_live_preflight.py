"""Предстартовая проверка live-кармана и окно live-политики (LIVE-PREFLIGHT).

Биржа — фейк с ответами в формате OKX v5 (поля account/config сверены с demo
24.09: perm, uid, mainUid, ip, acctLv, posMode). Сеть не используется.
"""
import copy
import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from src import live_policy
from src.live_preflight import run_preflight

NOW = datetime(2026, 9, 24, 12, 0, tzinfo=timezone.utc)
GOOD_CONFIG = {"perm": "read_only,trade", "uid": "111", "mainUid": "999",
               "ip": "203.0.113.7", "acctLv": "1", "posMode": "net_mode"}
GOOD_POCKET = {
    "pocket": "live-main", "mode": "live", "budget_usdt": 1000, "require_ip_whitelist": False,
    "sleeves": {"dca": {"enabled": True, "inst_id": "BTC/USDT", "quote_per_buy_usdt": 10,
                        "interval_hours": 24, "max_total_usdt": 150}},
}
OPEN_POLICY = {"live_enabled": True, "enabled_until": (NOW + timedelta(days=7)).isoformat()}


class FakeExchange:
    def __init__(self, config=None, total_bal="1000", pending=None):
        self.config = dict(GOOD_CONFIG if config is None else config)
        self.total_bal = total_bal
        self.pending = pending or []

    def private_get_account_config(self, params=None):
        return {"data": [self.config]}

    def private_get_asset_asset_valuation(self, params=None):
        return {"data": [{"totalBal": self.total_bal}]}

    def private_get_account_balance(self, params=None):
        return {"data": [{"totalEq": self.total_bal}]}

    def milliseconds(self):
        return 1_790_000_000_000

    def fetch_time(self):
        return 1_790_000_000_000

    def private_get_trade_orders_pending(self, params=None):
        return {"data": list(self.pending)}


class PreflightTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(self._tmp.cleanup)
        self.dir = Path(self._tmp.name)

    def _run(self, mode="live", exchange=None, pocket=GOOD_POCKET, policy=OPEN_POLICY):
        pocket_path, policy_path = self.dir / "pocket.json", self.dir / "policy.json"
        if pocket is not None:
            pocket_path.write_text(json.dumps(pocket), encoding="utf-8")
        policy_path.write_text(json.dumps(policy), encoding="utf-8")
        report = run_preflight(mode, exchange or FakeExchange(), pocket_path=pocket_path,
                               policy_path=policy_path, now=NOW, save=False)
        return report, {c["name"]: c["status"] for c in report["checks"]}

    def test_good_live_setup_passes(self):
        report, st = self._run()
        self.assertTrue(report["ok"], report)
        for name in ("policy_window", "pocket", "key_permissions", "sub_account",
                     "ip_whitelist", "equity_vs_budget", "time_sync", "foreign_orders"):
            self.assertEqual(st[name], "ok", name)

    def test_withdraw_permission_fails_live(self):
        cfg = dict(GOOD_CONFIG, perm="read_only,withdraw,trade")
        report, st = self._run(exchange=FakeExchange(cfg))
        self.assertFalse(report["ok"])
        self.assertEqual(st["key_permissions"], "fail")

    def test_main_account_key_fails_live(self):
        report, st = self._run(exchange=FakeExchange(dict(GOOD_CONFIG, uid="999")))
        self.assertFalse(report["ok"])
        self.assertEqual(st["sub_account"], "fail")

    def test_open_ended_or_expired_window_fails(self):
        for policy in ({"live_enabled": True, "enabled_until": None},
                       {"live_enabled": True, "enabled_until": (NOW - timedelta(minutes=1)).isoformat()},
                       {"live_enabled": False, "enabled_until": OPEN_POLICY["enabled_until"]}):
            report, st = self._run(policy=policy)
            self.assertFalse(report["ok"], policy)
            self.assertEqual(st["policy_window"], "fail", policy)

    def test_money_above_budget_fails(self):
        report, st = self._run(exchange=FakeExchange(total_bal="5000"))
        self.assertFalse(report["ok"])
        self.assertEqual(st["equity_vs_budget"], "fail")

    def test_ip_whitelist_warn_then_required(self):
        no_ip = FakeExchange(dict(GOOD_CONFIG, ip=""))
        report, st = self._run(exchange=no_ip)
        self.assertTrue(report["ok"])
        self.assertEqual(st["ip_whitelist"], "warn")
        strict = dict(GOOD_POCKET, require_ip_whitelist=True)
        report, st = self._run(exchange=no_ip, pocket=strict)
        self.assertFalse(report["ok"])
        self.assertEqual(st["ip_whitelist"], "fail")

    def test_foreign_orders_warn(self):
        ex = FakeExchange(pending=[{"ordId": "1", "clOrdId": ""}, {"ordId": "2", "clOrdId": "botabc"}])
        report, st = self._run(exchange=ex)
        self.assertTrue(report["ok"])
        self.assertEqual(st["foreign_orders"], "warn")

    def test_missing_pocket_fails_live(self):
        report, st = self._run(pocket=None)
        self.assertFalse(report["ok"])
        self.assertEqual(st["pocket"], "fail")

    def test_demo_is_report_only(self):
        demo_cfg = {"perm": "read_only,withdraw,trade", "uid": "5", "mainUid": "5", "ip": "",
                    "acctLv": "1", "posMode": "net_mode"}
        report, st = self._run("demo", FakeExchange(demo_cfg, total_bal="103903.7"), pocket=None,
                               policy={"live_enabled": True, "enabled_until": None})
        self.assertTrue(report["ok"], report)
        self.assertEqual(st["key_permissions"], "warn")
        self.assertEqual(st["sub_account"], "info")

    def test_report_hides_uid_and_ip(self):
        report, _ = self._run()
        text = json.dumps(report)
        for secret in ("111", "999", "203.0.113.7"):
            self.assertNotIn(secret, text)


class LiveWindowTest(unittest.TestCase):
    def test_window_rules(self):
        self.assertFalse(live_policy.live_window({"live_enabled": True, "enabled_until": None}, NOW)[0])
        self.assertFalse(live_policy.live_window({"live_enabled": True, "enabled_until": "завтра"}, NOW)[0])
        self.assertTrue(live_policy.live_window(OPEN_POLICY, NOW)[0])
        naive = {"live_enabled": True, "enabled_until": "2026-09-24T12:30:00"}  # без зоны = UTC
        self.assertTrue(live_policy.live_window(naive, NOW)[0])
        self.assertFalse(live_policy.live_window(naive, NOW + timedelta(hours=1))[0])


class PocketValidationTest(unittest.TestCase):
    def test_good_pocket(self):
        self.assertEqual(live_policy.validate_pocket(GOOD_POCKET), [])

    def test_template_is_rejected(self):
        template = json.loads(Path("ops/live-pocket.example.json").read_text(encoding="utf-8"))
        errors = live_policy.validate_pocket(template)
        self.assertEqual(len(errors), 1)
        self.assertIn("шаблон", errors[0])

    def test_caps_above_budget(self):
        pocket = copy.deepcopy(GOOD_POCKET)
        pocket["sleeves"]["dca"]["max_total_usdt"] = 5000
        self.assertTrue(any("budget_usdt" in e for e in live_policy.validate_pocket(pocket)))

    def test_unsupported_sleeve_enabled(self):
        pocket = copy.deepcopy(GOOD_POCKET)
        pocket["sleeves"]["grid"] = {"enabled": True, "max_total_usdt": 100}
        self.assertTrue(any("grid" in e for e in live_policy.validate_pocket(pocket)))
        pocket["sleeves"]["grid"]["enabled"] = False
        self.assertEqual(live_policy.validate_pocket(pocket), [])

    def test_dca_limits(self):
        pocket = copy.deepcopy(GOOD_POCKET)
        pocket["sleeves"]["dca"].update(quote_per_buy_usdt=0.5, interval_hours=0.1, inst_id="BTC-USDT")
        errors = live_policy.validate_pocket(pocket)
        self.assertEqual(len(errors), 3, errors)


if __name__ == "__main__":
    unittest.main()
