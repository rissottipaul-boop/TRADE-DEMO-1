"""Unit-тесты для менеджера расширенного флота OKX (до 50 ботов, 13 типов стратегий).
"""

import unittest
from unittest.mock import patch

from src.fleet_manager import (
    BOT_CATALOG,
    FLEET_MAX_BOTS,
    MAX_LEVERAGE,
    BotTypeSpec,
    build_cli_command,
    calculate_bot_allocation,
    generate_fleet_bot_id,
    get_supported_bot_types,
    validate_fleet_quota,
)
from src.risk import MAX_ACTIVE_BOTS, MAX_BOT_INVESTMENT_PCT


class TestFleetCatalog(unittest.TestCase):
    """Проверка каталога 13 типов ботов OKX."""

    def test_catalog_size_and_keys(self):
        """В каталоге ровно 13 нативных ботов и стратегий OKX."""
        expected_keys = {
            "spot_grid",
            "contract_grid_usdt",
            "contract_grid_coin",
            "smart_portfolio",
            "contract_dca",
            "smart_arbitrage",
            "dcd_pendulum",
            "spot_dca",
            "recurring_buy",
            "signal_bot",
            "iceberg",
            "twap",
            "arbitrage",
        }
        self.assertEqual(set(BOT_CATALOG.keys()), expected_keys)
        self.assertEqual(len(BOT_CATALOG), 13)

    def test_bot_spec_structure(self):
        """Каждая спецификация содержит корректные метаданные и валидные ссылки OKX."""
        for key, spec in BOT_CATALOG.items():
            self.assertIsInstance(spec, BotTypeSpec)
            self.assertEqual(spec.key, key)
            self.assertTrue(len(spec.name_ru) > 0)
            self.assertTrue(len(spec.name_en) > 0)
            self.assertTrue(spec.url.startswith("https://www.okx.com/ru/"))
            self.assertTrue(len(spec.description) > 0)
            self.assertIn(spec.engine_family, {"grid", "dca", "algo_order", "rebalance", "arbitrage", "dcd", "signal"})

    def test_get_supported_bot_types(self):
        """Функция get_supported_bot_types возвращает список из 13 структурированных словарей."""
        types_list = get_supported_bot_types()
        self.assertEqual(len(types_list), 13)
        keys = [item["key"] for item in types_list]
        self.assertIn("spot_grid", keys)
        self.assertIn("contract_grid_coin", keys)
        self.assertIn("smart_arbitrage", keys)
        self.assertIn("dcd_pendulum", keys)


class TestFleetQuota(unittest.TestCase):
    """Проверка лимита флота (FLEET_MAX_BOTS = 50)."""

    def test_fleet_quota_constants(self):
        self.assertEqual(FLEET_MAX_BOTS, 50)
        self.assertEqual(MAX_ACTIVE_BOTS, 50)
        self.assertEqual(MAX_BOT_INVESTMENT_PCT, 2.0)

    def test_quota_validation_within_limit(self):
        ok, msg = validate_fleet_quota(0, 10)
        self.assertTrue(ok)
        self.assertIn("10/50", msg)

        ok, msg = validate_fleet_quota(49, 1)
        self.assertTrue(ok)
        self.assertIn("50/50", msg)

    def test_quota_validation_exceeded(self):
        ok, msg = validate_fleet_quota(50, 1)
        self.assertFalse(ok)
        self.assertIn("Превышена квота", msg)

        ok, msg = validate_fleet_quota(45, 10)
        self.assertFalse(ok)
        self.assertIn("Превышена квота", msg)


class TestFleetAllocation(unittest.TestCase):
    """Проверка сайзинга капитала и соблюдения резерва свободной ликвидности."""

    def test_allocation_for_large_capital(self):
        equity = 100_000.0
        alloc = calculate_bot_allocation(equity, 50)

        # Резерв должен быть >= 30%
        self.assertEqual(alloc["reserve_usdt"], 30_000.0)

        # Максимум на одного бота <= 2% equity
        self.assertEqual(alloc["max_single_bot_usdt"], 2_000.0)

        # Рекомендованный размер на 1 бота не превышает 2% equity
        self.assertLessEqual(alloc["recommended_single_bot_usdt"], alloc["max_single_bot_usdt"])

        # Общий бюджет флота не превышает 70% equity
        self.assertLessEqual(alloc["total_fleet_budget_usdt"], equity * 0.70)

    def test_allocation_minimum_guard(self):
        equity = 1_000.0
        alloc = calculate_bot_allocation(equity, 50)
        self.assertGreaterEqual(alloc["recommended_single_bot_usdt"], 50.0)


class TestFleetCliCommand(unittest.TestCase):
    """Проверка генерации команд okx CLI для расширенного флота."""

    def test_generate_bot_id_tag(self):
        cl_id = generate_fleet_bot_id("spot_grid")
        self.assertEqual(len(cl_id), 32)
        self.assertTrue(cl_id.startswith("trd"))
        self.assertTrue(cl_id.isalnum())

    def test_build_spot_grid_cmd(self):
        params = {
            "instId": "BTC-USDT",
            "maxPx": 100000,
            "minPx": 80000,
            "gridNum": 20,
            "quoteSz": 500,
            "slTriggerPx": 78000,
            "tpTriggerPx": 105000,
        }
        cmd = build_cli_command("spot_grid", params)
        self.assertEqual(cmd[:5], ["okx", "--demo", "bot", "grid", "create"])
        self.assertIn("--instId", cmd)
        self.assertIn("BTC-USDT", cmd)
        self.assertIn("--quoteSz", cmd)
        self.assertIn("500", cmd)
        self.assertIn("--slTriggerPx", cmd)
        self.assertIn("78000", cmd)

    def test_build_contract_grid_leverage_limit(self):
        """Кредитное плечо должно быть жестко ограничено MAX_LEVERAGE (3x)."""
        params = {
            "instId": "ETH-USDT-SWAP",
            "maxPx": 3500,
            "minPx": 2500,
            "gridNum": 20,
            "sz": 10,
            "lever": 10,  # Запрос на 10x
            "slTriggerPx": 2400,  # SL обязателен (bot-fleet-50.md §1 п. 4)
        }
        cmd = build_cli_command("contract_grid_usdt", params)
        self.assertIn("--lever", cmd)
        lever_idx = cmd.index("--lever") + 1
        self.assertEqual(cmd[lever_idx], str(MAX_LEVERAGE))  # Ограничено до 3x

    def test_build_contract_dca_leverage_limit(self):
        """Кредитное плечо DCA-бота также должно быть ограничено до 3x."""
        params = {
            "instId": "SOL-USDT-SWAP",
            "initOrdAmt": 50,
            "safetyOrdAmt": 100,
            "lever": 5,  # Запрос на 5x
        }
        cmd = build_cli_command("contract_dca", params)
        self.assertIn("--lever", cmd)
        lever_idx = cmd.index("--lever") + 1
        self.assertEqual(cmd[lever_idx], str(MAX_LEVERAGE))  # Ограничено до 3x

    def test_build_iceberg_and_twap(self):
        params_iceberg = {
            "instId": "BTC-USDT",
            "side": "buy",
            "sz": "1.5",
        }
        cmd_iceberg = build_cli_command("iceberg", params_iceberg)
        self.assertEqual(cmd_iceberg[:5], ["okx", "--demo", "spot", "algo", "place"])
        self.assertIn("--ordType", cmd_iceberg)
        self.assertIn("iceberg", cmd_iceberg)

        params_twap = {
            "instId": "ETH-USDT",
            "side": "sell",
            "sz": "10",
            "timeInterval": 120,
        }
        cmd_twap = build_cli_command("twap", params_twap)
        self.assertEqual(cmd_twap[:5], ["okx", "--demo", "spot", "algo", "place"])
        self.assertIn("--ordType", cmd_twap)
        self.assertIn("twap", cmd_twap)
        self.assertIn("--timeInterval", cmd_twap)
        self.assertIn("120", cmd_twap)

    def test_unknown_bot_type_raises(self):
        with self.assertRaises(ValueError):
            build_cli_command("super_unknown_strategy", {"instId": "BTC-USDT"})


if __name__ == "__main__":
    unittest.main()
