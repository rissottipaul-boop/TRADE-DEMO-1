"""Live-runner кармана (LIVE-RUNNER): периметр проверяется самим процессом.

Биржа — фейк (без сети), DATA_ROOT — временный каталог, время — фейковые часы:
ожидание 24 ч проходит мгновенно, флаги подкладываются «во время сна».
"""
import copy
import json
import tempfile
import time
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

from src import config, risk
from src.dca_bot import DCABot
from src.live_runner import dca_spent_usdt, run
from src.storage import Storage

NOW = datetime(2026, 9, 24, 12, 0, tzinfo=timezone.utc)
PRICE = 50_000.0
POCKET = {
    "pocket": "live-main", "mode": "live", "budget_usdt": 1000, "require_ip_whitelist": False,
    "sleeves": {"dca": {"enabled": True, "inst_id": "BTC/USDT", "quote_per_buy_usdt": 10,
                        "interval_hours": 24, "max_total_usdt": 150}},
}
OPEN_POLICY = {"live_enabled": True, "enabled_until": (NOW + timedelta(days=7)).isoformat()}


class FakeExchange:
    """Ответы OKX v5 для preflight, DCA и kill-switch."""

    def __init__(self):
        self.created: list[dict] = []
        self.headers: dict = {}

    # preflight
    def private_get_account_config(self, params=None):
        return {"data": [{"perm": "read_only,trade", "uid": "1", "mainUid": "2", "ip": "203.0.113.7",
                          "acctLv": "1", "posMode": "net_mode"}]}

    def private_get_asset_asset_valuation(self, params=None):
        return {"data": [{"totalBal": "1000"}]}

    def private_get_account_balance(self, params=None):
        return {"data": [{"totalEq": "1000"}]}

    def milliseconds(self):
        return 1_790_000_000_000

    def fetch_time(self):
        return 1_790_000_000_000

    # kill-switch (emergency_stop)
    def private_get_trade_orders_pending(self, params=None):
        return {"data": []}

    def private_get_trade_orders_algo_pending(self, params=None):
        return {"data": []}

    def private_get_tradingbot_grid_orders_algo_pending(self, params=None):
        return {"data": []}

    # DCA
    def fetch_balance(self):  # USDT-баланс: в live для риск-ядра не используется
        return {"total": {"USDT": 500.0}}

    def fetch_ticker(self, symbol):
        return {"symbol": symbol, "last": PRICE}

    def create_order(self, symbol, ord_type, side, amount, price=None, params=None):
        self.created.append({"symbol": symbol, "type": ord_type, "side": side,
                             "amount": amount, "params": params or {}})
        return {"id": f"live-{len(self.created)}", "status": "closed", "symbol": symbol}

    def fetch_order(self, order_id, symbol=None):
        return {"id": order_id, "status": "closed", "symbol": symbol}


class FakeClock:
    """Часы + сон: сон двигает время; on_sleep — действие «во время сна»."""

    def __init__(self):
        self.t = time.time()
        self.slept = 0.0
        self.on_sleep = None

    def __call__(self):
        return self.t

    def sleep(self, seconds):
        self.t += seconds
        self.slept += seconds
        if self.on_sleep:
            self.on_sleep()


class RunnerTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.root = Path(self._tmp.name)
        patcher = mock.patch.object(config, "DATA_ROOT", self.root / "data")
        patcher.start()
        self.addCleanup(patcher.stop)
        self.pocket_path = self.root / "live-pocket.json"
        self.policy_path = self.root / "live-policy.json"
        self._write(self.pocket_path, POCKET)
        self._write(self.policy_path, OPEN_POLICY)
        self.ex = FakeExchange()
        self.clock = FakeClock()
        self.live = config.state_paths("live")

    def tearDown(self):
        risk.init(self.root / "unused.db")
        self._tmp.cleanup()

    @staticmethod
    def _write(path, data):
        path.write_text(json.dumps(data), encoding="utf-8")

    def _run(self, once=True):
        return run(once=once, exchange=self.ex, pocket_path=self.pocket_path,
                   policy_path=self.policy_path, sleep=self.clock.sleep, clock=self.clock,
                   now=lambda: NOW)

    def _storage(self):
        return Storage(self.live.bot_db)

    def test_once_buys_one_via_router_with_pocket_equity(self):
        self.assertEqual(self._run(), 0)
        self.assertEqual(len(self.ex.created), 1)
        order = self.ex.created[0]
        self.assertEqual((order["symbol"], order["type"], order["side"]), ("BTC/USDT", "market", "buy"))
        self.assertAlmostEqual(order["amount"], 10 / PRICE)
        self.assertTrue(order["params"]["clOrdId"].startswith("bot"))
        self.assertAlmostEqual(dca_spent_usdt(self._storage(), "BTC/USDT"), 10.0)
        risk.init(self.live.risk_db)
        self.assertEqual(risk.status()["equity"], 1000.0, "equity = стоимость кармана, не USDT-баланс")
        self.assertFalse((self.root / "data" / "risk_state.db").exists(), "demo-состояние тронуто")

    def test_preflight_failure_blocks_start(self):
        self._write(self.policy_path, {"live_enabled": True, "enabled_until": None})
        self.assertEqual(self._run(), 2)
        self.assertEqual(self.ex.created, [])

    def test_window_closing_mid_run_pauses(self):
        self.clock.on_sleep = lambda: self._write(self.policy_path, {"live_enabled": False})
        self.assertEqual(self._run(once=False), 1)
        self.assertEqual(len(self.ex.created), 1, "после закрытия окна покупок быть не должно")

    def test_sleeve_cap_is_never_exceeded(self):
        pocket = copy.deepcopy(POCKET)
        pocket["sleeves"]["dca"]["max_total_usdt"] = 25
        self._write(self.pocket_path, pocket)
        self.assertEqual(self._run(once=False), 0)
        self.assertEqual(len(self.ex.created), 2)
        self._run(once=False)
        self.assertEqual(len(self.ex.created), 2, "повторный запуск не тратит сверх лимита")

    def test_restart_waits_for_interval(self):
        self._run()
        self._run()
        self.assertEqual(len(self.ex.created), 2)
        self.assertGreater(self.clock.slept, 24 * 3600 - 60, "вторая покупка — только через интервал")

    def test_kill_flag_during_wait(self):
        self.clock.on_sleep = lambda: self.live.kill_flag.write_text("test kill", encoding="utf-8")
        self.assertEqual(self._run(once=False), 3)
        self.assertEqual(len(self.ex.created), 1)
        self.assertFalse(self.live.kill_flag.exists(), "флаг снимается при обработке")
        risk.init(self.live.risk_db)
        self.assertTrue(risk.status()["kill_active"])

    def test_stop_flag_is_graceful(self):
        self.clock.on_sleep = lambda: (self.live.root / "STOP_RUNNER").write_text("test", encoding="utf-8")
        self.assertEqual(self._run(once=False), 0)
        risk.init(self.live.risk_db)
        self.assertFalse(risk.status()["kill_active"])


class DcaLiveGuardTest(unittest.TestCase):
    def test_live_requires_cap_and_gate(self):
        with self.assertRaises(RuntimeError):
            DCABot(object(), mock.Mock(), demo=False, before_buy=lambda: (True, "ok"))
        with self.assertRaises(RuntimeError):
            DCABot(object(), mock.Mock(), demo=False, max_total_usdt=100)


if __name__ == "__main__":
    unittest.main()
