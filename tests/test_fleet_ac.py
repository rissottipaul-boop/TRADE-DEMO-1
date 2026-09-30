"""AC-тесты R4 (Bot Fleet 50): вердикты приёмки против `src/fleet_manager.py`.

Только чистые функции модуля, без сети и без обращений к бирже.
Регрессии фиксов M4: метка владельца на iceberg/twap, обязательный SL сеток,
честный отказ для типов без поддержки запуска через okx CLI.
"""

import unittest

from src.fleet_manager import (
    BOT_CATALOG,
    FLEET_MAX_BOTS,
    MAX_LEVERAGE,
    build_cli_command,
    calculate_bot_allocation,
    generate_fleet_bot_id,
    get_supported_bot_types,
    validate_fleet_quota,
)
from src.risk import MAX_ACTIVE_BOTS, MAX_BOT_INVESTMENT_PCT


def _grid_params(**over):
    params = {
        "instId": "BTC-USDT",
        "maxPx": 100000,
        "minPx": 80000,
        "gridNum": 20,
        "quoteSz": 500,
        "slTriggerPx": 78000,
    }
    params.update(over)
    return params


def _contract_grid_params(**over):
    params = {
        "instId": "BTC-USDT-SWAP",
        "maxPx": 100000,
        "minPx": 80000,
        "gridNum": 20,
        "sz": 2,
        "lever": 3,
        "slTriggerPx": 76000,
    }
    params.update(over)
    return params


def _dca_params(**over):
    params = {
        "instId": "SOL-USDT",
        "initOrdAmt": 50,
        "safetyOrdAmt": 100,
    }
    params.update(over)
    return params


class TestFleetAcCatalog(unittest.TestCase):
    """AC: каталог поддерживает 13 типов ботов."""

    def test_catalog_has_13_types(self):
        self.assertEqual(len(BOT_CATALOG), 13)
        self.assertEqual(len(get_supported_bot_types()), 13)


class TestFleetAcQuota(unittest.TestCase):
    """AC: лимит 50 активных ботов, превышение блокируется."""

    def test_quota_is_50_everywhere(self):
        self.assertEqual(FLEET_MAX_BOTS, 50)
        self.assertEqual(MAX_ACTIVE_BOTS, 50)

    def test_quota_allows_50th_rejects_51st(self):
        ok, _ = validate_fleet_quota(49, 1)
        self.assertTrue(ok)
        ok, msg = validate_fleet_quota(50, 1)
        self.assertFalse(ok)
        self.assertIn("50", msg)


class TestFleetAcAllocation(unittest.TestCase):
    """AC: аудит распределения капитала — ≤2% на бота, резерв 30%."""

    def test_per_bot_cap_and_reserve(self):
        self.assertEqual(MAX_BOT_INVESTMENT_PCT, 2.0)
        alloc = calculate_bot_allocation(108_000.0, 50)
        self.assertEqual(alloc["reserve_usdt"], 32_400.0)
        self.assertEqual(alloc["max_single_bot_usdt"], 2_160.0)
        self.assertLessEqual(alloc["recommended_single_bot_usdt"], alloc["max_single_bot_usdt"])
        self.assertLessEqual(alloc["total_fleet_budget_usdt"], 108_000.0 * 0.70)


class TestFleetAcOwnerTag(unittest.TestCase):
    """AC (R6): каждая строящаяся команда несёт ORDER-OWNER-TAG trd."""

    def test_generated_id_is_trd(self):
        cl_id = generate_fleet_bot_id("spot_grid")
        self.assertEqual(len(cl_id), 32)
        self.assertTrue(cl_id.startswith("trd"))
        self.assertTrue(cl_id.isalnum())

    def test_all_supported_commands_carry_trd_tag(self):
        cases = [
            ("spot_grid", _grid_params()),
            ("contract_grid_usdt", _contract_grid_params()),
            ("contract_grid_coin", _contract_grid_params(instId="BTC-USD-SWAP")),
            ("spot_dca", _dca_params()),
            ("contract_dca", _dca_params(instId="SOL-USDT-SWAP", lever=3)),
            ("iceberg", {"instId": "BTC-USDT", "side": "buy", "sz": "1.5"}),
            ("twap", {"instId": "ETH-USDT", "side": "sell", "sz": "10"}),
        ]
        for key, params in cases:
            with self.subTest(bot_type=key):
                cmd = build_cli_command(key, params)
                self.assertEqual(cmd[:2], ["okx", "--demo"])
                tag_flags = {"--algoClOrdId", "--clOrdId"}
                hits = [cmd[i + 1] for i, tok in enumerate(cmd[:-1]) if tok in tag_flags]
                self.assertEqual(len(hits), 1, f"{key}: ровно одна метка владельца, cmd={cmd}")
                self.assertTrue(hits[0].startswith("trd"), f"{key}: метка {hits[0]!r} без trd")
                self.assertEqual(len(hits[0]), 32)

    def test_iceberg_twap_use_cl_ord_id_flag(self):
        """Регрессия M4: algo place принимает метку как --clOrdId (AGENTS.md §6)."""
        for key in ("iceberg", "twap"):
            cmd = build_cli_command(key, {"instId": "BTC-USDT", "side": "buy", "sz": "1.5"})
            self.assertIn("--clOrdId", cmd)
            self.assertNotIn("--algoClOrdId", cmd)


class TestFleetAcMandatoryStop(unittest.TestCase):
    """AC: ни один бот не создаётся без стоп-лосса (bot-fleet-50.md §1 п. 4)."""

    def test_grid_without_sl_rejected(self):
        """Регрессия M4: сетка без slTriggerPx не строится."""
        for key, params in (
            ("spot_grid", _grid_params()),
            ("contract_grid_usdt", _contract_grid_params()),
            ("contract_grid_coin", _contract_grid_params()),
        ):
            params.pop("slTriggerPx")
            with self.subTest(bot_type=key):
                with self.assertRaises(ValueError):
                    build_cli_command(key, params)

    def test_dca_always_emits_sl_pct(self):
        for key in ("spot_dca", "contract_dca"):
            cmd = build_cli_command(key, _dca_params(instId="BTC-USDT"))
            self.assertIn("--slPct", cmd)

    def test_grid_with_sl_emits_sl_flag(self):
        cmd = build_cli_command("spot_grid", _grid_params())
        self.assertIn("--slTriggerPx", cmd)
        cmd = build_cli_command("contract_grid_usdt", _contract_grid_params())
        self.assertIn("--slTriggerPx", cmd)


class TestFleetAcLeverage(unittest.TestCase):
    """AC: плечо контрактных ботов жёстко ≤ 3x."""

    def test_leverage_clamped_to_3x(self):
        self.assertEqual(MAX_LEVERAGE, 3)
        for key in ("contract_grid_usdt", "contract_grid_coin", "contract_dca"):
            base = _contract_grid_params(lever=10) if "grid" in key else _dca_params(lever=10)
            cmd = build_cli_command(key, base)
            self.assertEqual(cmd[cmd.index("--lever") + 1], "3")


class TestFleetAcUnsupportedLaunch(unittest.TestCase):
    """AC-граница: 6 типов каталога okx CLI не запускает — честный отказ."""

    def test_unsupported_types_raise_instead_of_broken_command(self):
        """Регрессия M4: раньше возвращался нерабочий `okx bot <other> create`."""
        for key in ("smart_portfolio", "smart_arbitrage", "dcd_pendulum",
                    "recurring_buy", "signal_bot", "arbitrage"):
            with self.subTest(bot_type=key):
                with self.assertRaises(ValueError):
                    build_cli_command(key, {"instId": "BTC-USDT"})


if __name__ == "__main__":
    unittest.main()
