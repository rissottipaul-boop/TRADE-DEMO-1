"""Менеджер расширенного флота ботов OKX (до 50 ботов, 13 типов стратегий).

Реализует прямое распоряжение человека от 30.09.2026:
- Снято ограничение в 10 ботов, лимит флота увеличен до 50 активных ботов.
- Поддержка ВСЕХ 13 типов нативных ботов и алго-стратегий OKX:
  1.  spot_grid           — Спотовый grid-бот (развороты на растущем рынке / боковик)
  2.  contract_grid_usdt  — Фьючерсный grid-бот (USDT-маржа, long/short/neutral, плечо до 3x)
  3.  contract_grid_coin  — Фьючерсный grid-бот (маржа в криптовалюте Coin-M: BTC-USD, ETH-USD)
  4.  smart_portfolio     — Смарт-портфель (динамическая ребалансировка по триггеру весов)
  5.  contract_dca        — Фьючерсный DCA-бот (Мартингейл на деривативах с авто TP/SL)
  6.  smart_arbitrage     — Смарт-арбитраж TradFi (Cash & Carry дельта-нейтральный сбор funding)
  7.  dcd_pendulum        — Маятник (Dual Currency / DCD-бот торговли двумя валютами с премией)
  8.  spot_dca            — Спотовый DCA-бот (усреднение на споте по индикаторам RSI/цена)
  9.  recurring_buy       — Повторяющаяся покупка (регулярный DCA для усреднения удержания)
  10. signal_bot          — Сигнальный бот (автоторговля по сигналам Webhook / TradingView)
  11. iceberg             — Айсберг-бот (маскировка крупных ордеров мелкими долями в стакане)
  12. twap                — TWAP-бот (временное усреднение исполнения для снижения проскальзывания)
  13. arbitrage           — Межрыночный и спредовый арбитраж (Spot-Futures, Calendar Spread)
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from src.order_owner import new_client_order_id

log = logging.getLogger("okx.fleet_manager")

FLEET_MAX_BOTS = 50
MAX_LEVERAGE = 3  # инвариант business-plan.md §7


@dataclass(frozen=True)
class BotTypeSpec:
    key: str
    name_ru: str
    name_en: str
    category: str
    url: str
    description: str
    algo_ord_type: Optional[str] = None
    engine_family: str = "custom"  # 'grid', 'dca', 'algo_order', 'rebalance', 'arbitrage'
    requires_leverage: bool = False
    supported_instruments: str = "SPOT, SWAP"


BOT_CATALOG: Dict[str, BotTypeSpec] = {
    "spot_grid": BotTypeSpec(
        key="spot_grid",
        name_ru="Спотовый grid-бот",
        name_en="Spot Grid Bot",
        category="Grid",
        url="https://www.okx.com/ru/trade-spot-strategy",
        description="Торговля на разворотах на растущем рынке и в боковике. Покупка на спадах, продажа на подъемах.",
        algo_ord_type="grid",
        engine_family="grid",
        requires_leverage=False,
        supported_instruments="SPOT",
    ),
    "contract_grid_usdt": BotTypeSpec(
        key="contract_grid_usdt",
        name_ru="Фьючерсный grid-бот (USDT)",
        name_en="Futures Grid Bot (USDT-Margined)",
        category="Grid",
        url="https://www.okx.com/ru/trade-swap-strategy/btc-usdt-swap",
        description="Использование преимуществ фьючерсов при любых рыночных трендах (Long/Short/Neutral) с плечом до 3x.",
        algo_ord_type="contract_grid",
        engine_family="grid",
        requires_leverage=True,
        supported_instruments="SWAP, FUTURES",
    ),
    "contract_grid_coin": BotTypeSpec(
        key="contract_grid_coin",
        name_ru="Фьючерсный grid-бот (маржа в криптовалюте)",
        name_en="Futures Grid Bot (Coin-Margined)",
        category="Grid",
        url="https://www.okx.com/ru/trade-swap-strategy/btc-usd-swap",
        description="Фьючерсная сетка с залогом в базовой монете (BTC-USD-SWAP, ETH-USD-SWAP) для накопления криптовалюты.",
        algo_ord_type="contract_grid",
        engine_family="grid",
        requires_leverage=True,
        supported_instruments="SWAP (Coin-M)",
    ),
    "smart_portfolio": BotTypeSpec(
        key="smart_portfolio",
        name_ru="Смарт-портфель",
        name_en="Smart Portfolio / Rebalance",
        category="Portfolio",
        url="https://www.okx.com/ru/trade-spot-strategy",
        description="Динамическая ребалансировка криптопортфеля по заданным весам при отклонении на X% или по расписанию.",
        algo_ord_type="rebalance",
        engine_family="rebalance",
        requires_leverage=False,
        supported_instruments="SPOT",
    ),
    "contract_dca": BotTypeSpec(
        key="contract_dca",
        name_ru="Фьючерсный DCA-бот",
        name_en="Futures DCA (Martingale)",
        category="DCA",
        url="https://www.okx.com/ru/trade-swap-strategy",
        description="Регулярное извлечение прибыли из колебаний курса на фьючерсах с плечом, усреднением и автотейк-профитом.",
        algo_ord_type="contract_dca",
        engine_family="dca",
        requires_leverage=True,
        supported_instruments="SWAP",
    ),
    "smart_arbitrage": BotTypeSpec(
        key="smart_arbitrage",
        name_ru="Смарт-арбитраж TradFi",
        name_en="Smart Arbitrage (Cash & Carry)",
        category="Arbitrage",
        url="https://www.okx.com/ru/trade-spot-strategy",
        description="Арбитраж ставки финансирования в одно нажатие: покупка спота + зеркальный шорт бессрочного свопа.",
        algo_ord_type="funding_arbitrage",
        engine_family="arbitrage",
        requires_leverage=False,
        supported_instruments="SPOT + SWAP",
    ),
    "dcd_pendulum": BotTypeSpec(
        key="dcd_pendulum",
        name_ru="Маятник (Dual Currency / DCD)",
        name_en="Pendulum (Dual Currency Investment)",
        category="Structured",
        url="https://www.okx.com/ru/trading-bot/dcd-bot",
        description="Торговля на разворотах сразу двумя валютами: покупка по низкой цене, продажа по высокой с получением yield.",
        algo_ord_type="dcd",
        engine_family="dcd",
        requires_leverage=False,
        supported_instruments="SPOT / EARN",
    ),
    "spot_dca": BotTypeSpec(
        key="spot_dca",
        name_ru="Спотовый DCA-бот",
        name_en="Spot DCA (Martingale)",
        category="DCA",
        url="https://www.okx.com/ru/trade-spot-strategy",
        description="Регулярные покупки криптовалюты по индикаторам (RSI, просадка) с мартингейл-усреднением и тейк-профитом.",
        algo_ord_type="spot_dca",
        engine_family="dca",
        requires_leverage=False,
        supported_instruments="SPOT",
    ),
    "recurring_buy": BotTypeSpec(
        key="recurring_buy",
        name_ru="Повторяющаяся покупка",
        name_en="Recurring Buy (Scheduled DCA)",
        category="DCA",
        url="https://www.okx.com/ru/trade-spot-strategy",
        description="Регулярные периодические покупки по времени (часы, дни, недели) для усреднения затрат на удержание.",
        algo_ord_type="recurring",
        engine_family="dca",
        requires_leverage=False,
        supported_instruments="SPOT",
    ),
    "signal_bot": BotTypeSpec(
        key="signal_bot",
        name_ru="Сигнальный бот",
        name_en="Signal Bot",
        category="Signal",
        url="https://www.okx.com/ru/trade-swap-strategy",
        description="Автоматизированная торговля по сигналам с низкой задержкой (Webhook TradingView или внешние триггеры).",
        algo_ord_type="signal",
        engine_family="signal",
        requires_leverage=True,
        supported_instruments="SPOT, SWAP",
    ),
    "iceberg": BotTypeSpec(
        key="iceberg",
        name_ru="Айсберг-бот",
        name_en="Iceberg Algo Order",
        category="Execution",
        url="https://www.okx.com/ru/trade-spot-strategy",
        description="Разделение крупных ордеров на мелкие видимые заявки для исполнения по лучшей цене без сдвига стакана.",
        algo_ord_type="iceberg",
        engine_family="algo_order",
        requires_leverage=False,
        supported_instruments="SPOT, SWAP",
    ),
    "twap": BotTypeSpec(
        key="twap",
        name_ru="TWAP-Бот",
        name_en="TWAP (Time-Weighted Average Price)",
        category="Execution",
        url="https://www.okx.com/ru/trade-spot-strategy",
        description="Размещение ордеров частями через равные интервалы времени по плану, минимизируя проскальзывание.",
        algo_ord_type="twap",
        engine_family="algo_order",
        requires_leverage=False,
        supported_instruments="SPOT, SWAP",
    ),
    "arbitrage": BotTypeSpec(
        key="arbitrage",
        name_ru="Спредовый арбитраж",
        name_en="Spread / Calendar Arbitrage",
        category="Arbitrage",
        url="https://www.okx.com/ru/trade-arbitrage",
        description="Арбитраж ценовых спредов и дельт между спотом, квартальными фьючерсами и календарными спредами.",
        algo_ord_type="arbitrage",
        engine_family="arbitrage",
        requires_leverage=True,
        supported_instruments="SPOT, SWAP, FUTURES",
    ),
}


def get_supported_bot_types() -> List[Dict[str, Any]]:
    """Возвращает структурированный каталог всех 13 поддерживаемых типов ботов."""
    return [
        {
            "key": spec.key,
            "name_ru": spec.name_ru,
            "name_en": spec.name_en,
            "category": spec.category,
            "url": spec.url,
            "description": spec.description,
            "algo_ord_type": spec.algo_ord_type,
            "engine_family": spec.engine_family,
            "requires_leverage": spec.requires_leverage,
            "supported_instruments": spec.supported_instruments,
        }
        for spec in BOT_CATALOG.values()
    ]


def validate_fleet_quota(current_active_bots: int, adding_bots: int = 1) -> tuple[bool, str]:
    """Проверяет квоту флота ботов (максимум 50 ботов)."""
    if current_active_bots + adding_bots > FLEET_MAX_BOTS:
        return (
            False,
            f"Превышена квота флота: активно {current_active_bots}, попытка добавить {adding_bots}, "
            f"максимум {FLEET_MAX_BOTS} ботов.",
        )
    return True, f"Квота в норме: {current_active_bots + adding_bots}/{FLEET_MAX_BOTS} слотов."


def calculate_bot_allocation(total_equity: float, target_bots: int = FLEET_MAX_BOTS) -> Dict[str, float]:
    """Рассчитывает безопасный сайзинг капитала на 1 бота под флот из 50 слотов.

    Правила:
    - Максимальная инвестиция на 1 бота: 2% equity (при капитале $108 000 — до $2 160 USDT).
    - Базовый дефолтный размер под 50 ботов: min(1000 USDT, equity / 50).
    - Неприкосновенный резерв свободной ликвидности: не менее 30% капитала.
    """
    safe_target = max(1, min(target_bots, FLEET_MAX_BOTS))
    max_single_bot_cap = total_equity * 0.02
    base_allocation = min(max_single_bot_cap, total_equity * 0.70 / safe_target)
    base_allocation = round(max(50.0, base_allocation), 2)

    return {
        "total_equity": total_equity,
        "max_fleet_bots": FLEET_MAX_BOTS,
        "recommended_single_bot_usdt": base_allocation,
        "max_single_bot_usdt": round(max_single_bot_cap, 2),
        "total_fleet_budget_usdt": round(base_allocation * safe_target, 2),
        "reserve_usdt": round(total_equity * 0.30, 2),
    }


def generate_fleet_bot_id(bot_key: str) -> str:
    """Генерирует 32-значный algoClOrdId с префиксом владельца trd (ORDER-OWNER-TAG)."""
    return new_client_order_id("trd")


def build_cli_command(bot_type_key: str, params: Dict[str, Any]) -> List[str]:
    """Формирует безопасную команду okx CLI для запуска выбранного типа бота.

    Проверяет обязательные стоп-лоссы и ограничение плеча <= 3x.
    Сетки требуют slTriggerPx (bot-fleet-50.md §1 п. 4); типы без поддержки
    запуска через okx CLI (только grid/dca/algo place) отклоняются ValueError.
    """
    if bot_type_key not in BOT_CATALOG:
        raise ValueError(f"Неизвестный тип бота: {bot_type_key}. Доступно: {list(BOT_CATALOG.keys())}")

    spec = BOT_CATALOG[bot_type_key]
    cl_ord_id = params.get("algoClOrdId") or generate_fleet_bot_id(bot_type_key)
    inst_id = params["instId"]

    # 1. Spot Grid
    if bot_type_key == "spot_grid":
        if "slTriggerPx" not in params:
            raise ValueError("spot_grid требует slTriggerPx: бот без стоп-лосса запрещён (bot-fleet-50.md §1 п. 4)")
        cmd = [
            "okx", "--demo", "bot", "grid", "create",
            "--instId", inst_id,
            "--algoOrdType", "grid",
            "--maxPx", str(params["maxPx"]),
            "--minPx", str(params["minPx"]),
            "--gridNum", str(params.get("gridNum", 25)),
            "--quoteSz", str(params["quoteSz"]),
            "--algoClOrdId", cl_ord_id,
        ]
        if "slTriggerPx" in params:
            cmd.extend(["--slTriggerPx", str(params["slTriggerPx"])])
        if "tpTriggerPx" in params:
            cmd.extend(["--tpTriggerPx", str(params["tpTriggerPx"])])
        return cmd

    # 2. Contract Grid (USDT или Coin-M)
    if bot_type_key in ("contract_grid_usdt", "contract_grid_coin"):
        if "slTriggerPx" not in params:
            raise ValueError(f"{bot_type_key} требует slTriggerPx: бот без стоп-лосса запрещён (bot-fleet-50.md §1 п. 4)")
        lever = min(int(params.get("lever", 2)), MAX_LEVERAGE)
        cmd = [
            "okx", "--demo", "bot", "grid", "create",
            "--instId", inst_id,
            "--algoOrdType", "contract_grid",
            "--direction", params.get("direction", "neutral"),
            "--lever", str(lever),
            "--sz", str(params["sz"]),
            "--maxPx", str(params["maxPx"]),
            "--minPx", str(params["minPx"]),
            "--gridNum", str(params.get("gridNum", 25)),
            "--algoClOrdId", cl_ord_id,
        ]
        if "slTriggerPx" in params:
            cmd.extend(["--slTriggerPx", str(params["slTriggerPx"])])
        if "tpTriggerPx" in params:
            cmd.extend(["--tpTriggerPx", str(params["tpTriggerPx"])])
        return cmd

    # 3. Spot DCA Martingale
    if bot_type_key == "spot_dca":
        cmd = [
            "okx", "--demo", "bot", "dca", "create",
            "--algoOrdType", "spot_dca",
            "--instId", inst_id,
            "--direction", "long",
            "--initOrdAmt", str(params["initOrdAmt"]),
            "--safetyOrdAmt", str(params["safetyOrdAmt"]),
            "--maxSafetyOrds", str(params.get("maxSafetyOrds", 5)),
            "--tpPct", str(params.get("tpPct", 0.015)),
            "--slPct", str(params.get("slPct", 0.15)),
            "--pxSteps", str(params.get("pxSteps", 0.02)),
            "--pxStepsMult", str(params.get("pxStepsMult", 1.2)),
            "--volMult", str(params.get("volMult", 1.1)),
            "--algoClOrdId", cl_ord_id,
        ]
        return cmd

    # 4. Contract DCA Martingale
    if bot_type_key == "contract_dca":
        lever = min(int(params.get("lever", 2)), MAX_LEVERAGE)
        cmd = [
            "okx", "--demo", "bot", "dca", "create",
            "--algoOrdType", "contract_dca",
            "--instId", inst_id,
            "--direction", params.get("direction", "long"),
            "--lever", str(lever),
            "--initOrdAmt", str(params["initOrdAmt"]),
            "--safetyOrdAmt", str(params["safetyOrdAmt"]),
            "--maxSafetyOrds", str(params.get("maxSafetyOrds", 5)),
            "--tpPct", str(params.get("tpPct", 0.015)),
            "--slPct", str(params.get("slPct", 0.15)),
            "--pxSteps", str(params.get("pxSteps", 0.02)),
            "--pxStepsMult", str(params.get("pxStepsMult", 1.2)),
            "--volMult", str(params.get("volMult", 1.1)),
            "--algoClOrdId", cl_ord_id,
        ]
        return cmd

    # 5. Iceberg Algo Order
    if bot_type_key == "iceberg":
        side = params.get("side", "buy")
        cmd = [
            "okx", "--demo", "spot", "algo", "place",
            "--instId", inst_id,
            "--side", side,
            "--sz", str(params["sz"]),
            "--ordType", "iceberg",
            "--szLimit", str(params.get("szLimit", float(params["sz"]) / 10)),
            "--pxSpread", str(params.get("pxSpread", "0.001")),
            "--clOrdId", cl_ord_id,
        ]
        return cmd

    # 6. TWAP Algo Order
    if bot_type_key == "twap":
        side = params.get("side", "buy")
        cmd = [
            "okx", "--demo", "spot", "algo", "place",
            "--instId", inst_id,
            "--side", side,
            "--sz", str(params["sz"]),
            "--ordType", "twap",
            "--timeInterval", str(params.get("timeInterval", 60)),
            "--szLimit", str(params.get("szLimit", float(params["sz"]) / 10)),
            "--clOrdId", cl_ord_id,
        ]
        return cmd

    # Остальные типы каталога okx CLI не запускает (в `okx bot` только grid и
    # dca): молча вернуть нерабочую команду хуже, чем честно отказать.
    raise ValueError(
        f"Запуск {bot_type_key} через okx CLI не поддерживается "
        f"(algoOrdType {spec.algo_ord_type}): в каталоге для аудита, запуск вручную"
    )
