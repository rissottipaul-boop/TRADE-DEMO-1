# Расширенный флот OKX: 50 ботов и 13 типов стратегий (FLEET-50-EXPAND)

- **Дата утверждения:** 2026-09-30 (+05:00)
- **Основание:** Прямое распоряжение человека («убери ограничение в 10 ботов пусть будет 50 но всех ботов именно а это + 13 типов ботов...», AGENTS.md §2)
- **Статус:** active — лимит флота увеличен с 10 до 50 активных ботов, ядро риска обновлено, модуль флота `src/fleet_manager.py`. 30.09: REST-шлюз и Flowise удалены из проекта — управление только через okx CLI.
- **Среда:** OKX demo (`okx --demo`), профиль `okx-demo`, okx CLI 1.4.8.

---

## 1. Архитектура флота из 50 ботов

Проект перешел от пилотного набора (10 ботов: спотовые grid и DCA) к масштабируемому флоту из **50 одновременно работающих ботов** с диверсификацией по 13 классам стратегий.

### Ключевые принципы безопасности:
1. **Жесткий лимит квоты:** `MAX_ACTIVE_BOTS = 50` в `src/risk.py` и `src/fleet_manager.py`. Превышение квоты блокируется на уровне ядра (`validate_fleet_quota`).
2. **Безопасный сайзинг капитала:**
   - Неприкосновенный резерв свободной ликвидности: **не менее 30% капитала** (при капитале $108 000 USDT — минимум $32 400 USDT всегда в кэше).
   - Лимит инвестиций на одного бота: **не более 2.0% equity** (`MAX_BOT_INVESTMENT_PCT = 2.0`, при $108 000 — до $2 160 USDT).
   - Рекомендуемый рабочий размер на бота: от $100 до $1 400 USDT в зависимости от волатильности и ликвидности пары.
3. **Ограничение плеча для деривативов:** Кредитное плечо для всех контрактных ботов (фьючерсный grid, фьючерсный DCA, арбитраж) **жестко ограничено до 3x** (`MAX_LEVERAGE = 3`, isolated margin, business-plan.md §7).
4. **Обязательный стоп-лосс:** Ни один бот не создается без параметров защиты депозита (`slTriggerPx` для сеток или `slPct` для DCA).
5. **Метка владельца (ORDER-OWNER-TAG):** Все создаваемые боты и их дочерние ордера получают `algoClOrdId` с префиксом `trd` (OKX Trader, AGENTS.md §6).
6. **Атрибуция PnL:** Все боты флота с префиксами `flt` и `trd` автоматически маршрутизируются в рукав `demo_fleet` (`ops/sleeves.json`).

---

## 2. Каталог всех 13 типов нативных ботов OKX

| № | Ключ стратегии | Название (RU) | Название (EN) | Ссылка на OKX | Категория | Инструменты | Плечо |
|---|---|---|---|---|---|---|---|
| 1 | `spot_grid` | Спотовый grid-бот | Spot Grid Bot | [trade-spot-strategy](https://www.okx.com/ru/trade-spot-strategy) | Grid | SPOT | 1x (нет) |
| 2 | `contract_grid_usdt` | Фьючерсный grid-бот (USDT) | Futures Grid (USDT) | [trade-swap-strategy/btc-usdt-swap](https://www.okx.com/ru/trade-swap-strategy/btc-usdt-swap) | Grid | SWAP, FUTURES | до 3x |
| 3 | `contract_grid_coin` | Фьючерсный grid-бот (Coin-M) | Futures Grid (Coin-M) | [trade-swap-strategy/btc-usd-swap](https://www.okx.com/ru/trade-swap-strategy/btc-usd-swap) | Grid | SWAP (Coin-M) | до 3x |
| 4 | `smart_portfolio` | Смарт-портфель | Smart Portfolio / Rebalance | [trade-spot-strategy](https://www.okx.com/ru/trade-spot-strategy) | Portfolio | SPOT | 1x (нет) |
| 5 | `contract_dca` | Фьючерсный DCA-бот | Futures DCA (Martingale) | [trade-swap-strategy](https://www.okx.com/ru/trade-swap-strategy) | DCA | SWAP | до 3x |
| 6 | `smart_arbitrage` | Смарт-арбитраж TradFi | Smart Arbitrage (Cash & Carry)| [trade-spot-strategy](https://www.okx.com/ru/trade-spot-strategy) | Arbitrage | SPOT + SWAP | 1x / дельта-нейтрально |
| 7 | `dcd_pendulum` | Маятник (Dual Currency DCD) | Pendulum (Dual Currency) | [trading-bot/dcd-bot](https://www.okx.com/ru/trading-bot/dcd-bot) | Structured | SPOT / EARN | 1x (нет) |
| 8 | `spot_dca` | Спотовый DCA-бот | Spot DCA (Martingale) | [trade-spot-strategy](https://www.okx.com/ru/trade-spot-strategy) | DCA | SPOT | 1x (нет) |
| 9 | `recurring_buy` | Повторяющаяся покупка | Recurring Buy (Scheduled DCA) | [trade-spot-strategy](https://www.okx.com/ru/trade-spot-strategy) | DCA | SPOT | 1x (нет) |
| 10 | `signal_bot` | Сигнальный бот | Signal Bot | [trade-swap-strategy](https://www.okx.com/ru/trade-swap-strategy) | Signal | SPOT, SWAP | до 3x |
| 11 | `iceberg` | Айсберг-бот | Iceberg Algo Order | [trade-spot-strategy](https://www.okx.com/ru/trade-spot-strategy) | Execution | SPOT, SWAP | 1x (нет) |
| 12 | `twap` | TWAP-Бот | TWAP (Time-Weighted) | [trade-spot-strategy](https://www.okx.com/ru/trade-spot-strategy) | Execution | SPOT, SWAP | 1x (нет) |
| 13 | `arbitrage` | Спредовый арбитраж | Spread / Calendar Arbitrage | [trade-arbitrage](https://www.okx.com/ru/trade-arbitrage) | Arbitrage | SPOT, FUTURES | до 3x |

---

## 3. Матрица управления рисками при 50 ботах

При текущем балансе счета **$107 895 USDT**:

```
Общий капитал:               $107 895 USDT (100%)
Неприкосновенный резерв:      $32 368 USDT (30% equity)
Бюджет на флот из 50 ботов:   $75 527 USDT (70% equity)
Базовая аллокация на 1 бота:   $1 400 USDT (1.30% equity)
Максимальный лимит на бота:    $2 157 USDT (2.00% equity)
```

### Диверсификация по корзинам:
- **Корзина 1: Сетки высокой надежности (15 слотов)** — BTC, ETH, SOL spot grid и futures grid с широким коридором.
- **Корзина 2: DCA усреднение альткоинов (15 слотов)** — топ-30 альткоинов по капитализации (AVAX, LINK, NEAR, SUI, ADA, TON, XRP).
- **Корзина 3: Дельта-нейтральный арбитраж и Funding (10 слотов)** — Smart Arbitrage и Spread Arbitrage со сбором ставки финансирования без направленного риска.
- **Корзина 4: Структурные продукты и смарт-портфель (5 слотов)** — Smart Portfolio rebalance и DCD Pendulum.
- **Корзина 5: Алго-исполнение и сигналы (5 слотов)** — Signal Bot по TradingView сигналам, Iceberg и TWAP для набора/сброса позиций.

---

## 4. Команды запуска и управления

### Запуск через OKX CLI с тегом trd (REST-шлюз удалён 30.09):
```bash
# Спотовый Grid
okx --demo bot grid create --instId ETH-USDT --algoOrdType grid --maxPx 3500 --minPx 2300 --gridNum 30 --quoteSz 500 --slTriggerPx 2150 --algoClOrdId $(python -m src.order_owner new trd)

# Фьючерсный Grid (плечо <= 3x)
okx --demo bot grid create --instId BTC-USDT-SWAP --algoOrdType contract_grid --direction neutral --lever 3 --sz 2 --maxPx 100000 --minPx 80000 --gridNum 40 --slTriggerPx 76000 --algoClOrdId $(python -m src.order_owner new trd)

# Спотовый DCA
okx --demo bot dca create --algoOrdType spot_dca --instId SOL-USDT --direction long --initOrdAmt 50 --safetyOrdAmt 100 --maxSafetyOrds 5 --tpPct 0.02 --slPct 0.12 --algoClOrdId $(python -m src.order_owner new trd)

# Айсберг
okx --demo spot algo place --instId BTC-USDT --side buy --sz 1.5 --ordType iceberg --szLimit 0.15 --pxSpread 0.001
```

---

## 5. Управление из Flowise — удалено 30.09

Раздел оставлен для истории: чат-флоу `OKX Bot Fleet Commander` и инструмент
`okx_fleet_manager` удалены вместе со стеком Flowise. Управление флотом —
только через okx CLI (раздел 4) и модуль `src/fleet_manager.py`.
