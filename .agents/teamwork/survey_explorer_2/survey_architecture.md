# Архитектурный обзор кодовой базы OKX Trading Bot (Survey Phase)

**Автор:** survey_explorer_2 (Codebase Architecture Explorer)  
**Дата:** 2026-09-30  
**Контекст задачи:** Подготовка к реализации торговых направлений из `insights/` (R1 Funding Carry, R2 Mean Reversion, R3 Idle Cash Earn, R4 Bot Fleet 50, R5 Делегирование Muse Code, R6 Безопасность и непрерывность движка).

---

## 1. Общий архитектурный ландшафт системы

Архитектура OKX-бота построена по модульному принципу с четким разделением ответственности между приемом рыночных данных, хранением состояния, управлением рисками, маршрутизацией ордеров и автономными стратегиями.

```
                    ┌───────────────────────────────────────────────────┐
                    │               OKX Exchange (v5 API)               │
                    │   REST (Trading/Account/Market) + WebSocket (Pub/Priv)
                    └───────────▲───────────────────────────▲───────────┘
                                │                           │
                 REST (ccxt)    │                           │ WS streams
                                │                           │ (books, tickers, orders)
                    ┌───────────┴──────────┐    ┌───────────┴──────────┐
                    │   src/connector.py   │    │   src/ws_client.py   │
                    │  (OKXExchange, expTime,│    │ (OKXWebSocket public,│
                    │ emergency_stop, lock)│    │  private, business)  │
                    └───────────▲──────────┘    └───────────▲──────────┘
                                │                           │
                    ┌───────────┴───────────────────────────┴──────────┐
                    │                 src/engine.py                    │
                    │  TradingEngine: main loops (reconcile 60s,       │
                    │  equity 300s, flags 2s), keep-awake, faulthandler │
                    │  [PID 15744 — Phase 1 72H Active Run — НЕ ТРОГАТЬ]│
                    └───────▲───────────────▲─────────────────▲────────┘
                            │               │                 │
           ┌────────────────┴────┐   ┌──────┴──────────┐   ┌──┴───────────────┐
           │   src/storage.py    │   │  src/risk.py    │   │src/order_router.py
           │ SQLite bot_state.db │   │ SQLite risk.db  │   │ Gatekeeper ордеров,
           │ orders, trades, ws  │   │ Breakers, limits│   │ checks, throttler│
           └─────────────────────┘   └────────▲────────┘   └──▲───────────────┘
                                              │               │
                                              ├───────────────┤
                                              │               │
                         ┌────────────────────┴───────────────┴────────────────────┐
                         │              Торговые модули и флоты                     │
                         │ ├─ src/dca_bot.py (Demo / Live DCA)                     │
                         │ ├─ src/fleet_manager.py (50 ботов / 13 типов)           │
                         │ ├─ [R1 Plan] src/funding_carry.py (Spot Long + Swap Sh) │
                         │ ├─ [R2 Plan] src/meanrev_strategy.py (RSI/BB 1H)        │
                         │ └─ [R3 Plan] src/idle_earn.py (Flexible Earn Treasury)  │
                         └─────────────────────────────────────────────────────────┘
```

### Принципы межмодульного взаимодействия:
1. **Единая точка входа для ордеров:** Любое выставление торговых ордеров выполняется **исключительно** через `src/order_router.py`. Прямой вызов `exchange.create_order` в стратегиях запрещен.
2. **Раздельная персистентность:**
   - `data/bot_state.db` (`src/storage.py`) хранит ордера, сделки, снэпшоты equity и произвольное KV-состояние (`ws_state`).
   - `data/risk_state.db` (`src/risk.py`) изолированно хранит лимиты, breakers, streaks убытков, heat портфеля и открытые слоты риска.
3. **Безопасность и непрерывность (R6):**
   - Движок `src/engine.py` (PID 15744) выполняет квалификационный 72-часовой прогон Phase 1. Любой перезапуск или аварийная остановка обнуляет таймер.
   - Новые торговые модули интегрируются через разделяемые базы данных, общие модульные интерфейсы и `OrderRouter`, не требуя перезапуска движка.

---

## 2. Анализ `src/engine.py` (Торговый движок)

### 2.1 Жизненный цикл и фоновые циклы
Класс `TradingEngine` управляет тремя независимыми асинхронными циклами:
1. **`_reconcile_loop` (интервал 60 с):**
   - Вызывает `reconciler.sync_orders` и `reconciler.sync_trades` по каждому активному инструменту (`BTC-USDT`).
   - Синхронизирует расхождения между биржей и локальным `Storage`. Ордера с префиксом `bot*` считаются своими, внешние ордера классифицируются отдельно (`external_orders`).
   - Каждым кругом перевзводит таймер `faulthandler.dump_traceback_later(HANG_DUMP_S=150.0)` для защиты от зависаний.
   - Фиксирует паузы процесса через `_check_pause` (если интервал > 120 с, инкрементирует `pauses` — признак сна машины).
2. **`_equity_loop` (интервал 300 с):**
   - Запрашивает баланс через `ex.private_get_account_balance`.
   - Извлекает `totalEq` (общий баланс аккаунта в USD), `availBal` (USDT) и `upl` (нереализованный PnL).
   - Сохраняет `EquityRecord` в SQLite (`data/bot_state.db`).
   - Передает `totalEq` в `risk.update_equity(total)`. При срабатывании breakers логирует критическое событие.
3. **`_flag_loop` (интервал 2 с):**
   - Проверяет существование файлов `data/KILL` и `data/STOP_ENGINE`.
   - Читает причину через `read_flag_text` (поддержка UTF-8, UTF-16 BOM, CP1251) до удаления файла.
   - При `KILL`: вызывает `risk.kill_switch(False, f"flag: {reason}")`.
   - При `STOP_ENGINE`: инициирует штатную остановку цикла.

### 2.2 WebSocket-соединения
- **Public WS (`OKXWebSocket`):** подключается к `wss://wspap.okx.com:8443/ws/v5/public`, подписывается на каналы `books` и `tickers`. Ведет локальный кэш `self._tickers[inst_id]`.
- **Private WS (`OKXWebSocket`):** подключается с подписью API-ключа к `.../ws/v5/private`, подписывается на канал `orders`. Полученные события ордеров и трейдов сразу сохраняются в базу данных (`upsert_order`, `insert_trade`).
- **Business WS (`ws_business`):** создается лениво при вызове `subscribe_market_data("candle*")`, так как публичный WS OKX отклоняет подписку на свечи ошибкой 60018.

### 2.3 Обеспечение непрерывности (Engine Continuity Guard)
- Защита от сна Windows: `SetThreadExecutionState(ES_CONTINUOUS | ES_SYSTEM_REQUIRED)`.
- Контроль PID: `data/engine.pid` отслеживается сторожем `src/engine_watchdog.py`.
- Текущий статус: **PID 15744 работает непрерывно с 2026-09-30 02:59:52 (+05:00)**, `errors_internal == 0`, `divergences == 0`.
- **Правило для разработчиков:** Запрещено вызывать `ops/engine.ps1 stop/restart`, создавать флаги `data/STOP_ENGINE` или `data/KILL`, а также запускать скрипты, выполняющие `sys.exit` или изменяющие системные библиотеки на лету.

---

## 3. Анализ `src/risk.py` (Ядро управления рисками)

Модуль `src/risk.py` является центральным арбитром допустимости любых торговых операций. Состояние хранится в `data/risk_state.db`.

### 3.1 Ключевые параметры и инварианты
| Параметр | Значение | Описание |
|---|---|---|
| `DEFAULT_RISK_PCT` | 1.0% | Базовый риск на сделку от equity |
| `MAX_RISK_PCT` | 2.0% | Жесткий потолок риска на сделку |
| `MAX_PORTFOLIO_HEAT_PCT` | 6.0% | Суммарный открытый риск портфеля |
| `MAX_POSITION_PCT` | 15.0% | Максимальный notional одной позиции от equity |
| `MAX_OPEN_POSITIONS` | 2 | Лимит одновременно открытых позиций |
| `DAILY_LOSS_LIMIT_PCT` | 6.0% | Дневной лимит потерь (автосброс в 00:00 UTC) |
| `GLOBAL_DD_LIMIT_PCT` | 15.0% | Глобальный breaker просадки от HWM (только ручной сброс) |
| `MAX_ENTRIES_PER_DAY` | 10 | Максимум новых входов в сутки |
| `INST_LOSS_STREAK_BLOCK`| 3 | Серия убытков по инструменту -> блокировка на 24 часа |
| `SYS_LOSS_STREAK_PAUSE` | 5 | Серия убытков по системе -> общая пауза на 24 часа |
| `EQUITY_MAX_AGE_S` | 600 с | Максимальный возраст фида equity (иначе входы блокируются) |
| `MAX_LEVERAGE` | 3x | Жесткий потолок плеча для деривативов (только isolated) |
| `MAX_ACTIVE_BOTS` | 50 | Максимум активных нативных ботов (квота флота) |
| `MAX_BOT_INVESTMENT_PCT`| 2.0% | Максимальная аллокация на одного бота от equity |

### 3.2 Ключевые методы
- `check_entry_allowed(inst_id, side) -> (bool, reason)`:
  Проверяет: активность kill-switch -> global breaker (-15%) -> daily breaker (-6%) -> свежесть equity (<= 600 с) -> system pause (5 убытков) -> entries_today (< 10) -> блокировку инструмента (3 убытка) -> отсутствие противоположной позиции (хедж запрещен) -> лимит позиций (< 2) -> portfolio heat + 1% <= 6%.
- `size_position(equity, entry, stop, ct_val, lot_sz, min_sz, risk_pct) -> dict`:
  Сайзинг по формуле Fixed Fractional: `contracts = floor(dollar_risk / (abs(entry - stop) * ct_val))`. Размер обрезается потолком 15% equity, округляется до `lot_sz` и валидируется против `min_sz`. Если notional > 30% equity — отклоняется.
- `check_exit_allowed(inst_id, side, sz) -> (bool, reason, pos_dict)`:
  Специализированная валидация для выходов (ROUTER-EXIT). Не проверяет breakers, лимиты дня и свежесть equity (в аварии выход всегда разрешен). Проверяет: позиция открыта, сторона противоположна, `sz <= remaining`.
- `record_pnl(inst_id, pnl, closed_at) -> list[str]`:
  Учитывает закрытую сделку в `day_pnl` и обновляет серии убытков. **Не изменяет equity и HWM** (защита `RISK-PNL-DOUBLE`), так как equity ведется только через `update_equity`.
- `register_spot_buy(inst_id)`:
  Специнтерфейс для спотовых DCA/накоплений: освобождает слот риска без начисления PnL и без сброса серий убытков.

---

## 4. Анализ `src/order_router.py` (Маршрутизатор ордеров)

`OrderRouter` — единственный разрешенный исполнитель торговых операций.

### 4.1 Пайплайн `place_order`
1. **Риск-валидация входа:** `risk.check_entry_allowed(inst_id, side)`.
2. **Сайзинг:** `risk.size_position(...)` (если `sz` не задан) или проверка переданного размера.
3. **Проверка ликвидации:** `risk.validate_stop_vs_liquidation(px, stop_px, liq_price, side)`.
4. **Режим аккаунта (SPOT-TDMODE):** `_spot_params()` через `account_mode.py`.
   - `acctLv` 1–2 -> `tdMode=cash`.
   - `acctLv` 3–4 -> `tdMode=cross`.
   - Запрет скрытого займа: `check_no_borrow()` сверяет стоимость покупки с `availBal`.
5. **Throttler:** Ограничение 20 ордеров за 2 секунды на инструмент (`_PlaceThrottler`).
6. **Маркировка и expTime:**
   - Генерация `clOrdId` с префиксом владельца через `order_owner.new_cl_ord_id(owner_code)` (например, `botr...`, `botsdca...`).
   - Добавление `expTime` (дедлайн запроса, дефолт 10 000 мс) в тело и HTTP-заголовок (перехватывается `OKXExchange.sign`).
7. **Исполнение и подтверждение:** Вызов `create_order`, при необходимости `fetch_order` для дочитывания статуса.
8. **Фиксация состояния:**
   - Сохранение в `Storage` (`upsert_order`).
   - Регистрация в риске: `risk.register_entry` и `risk.register_entry_size`.

### 4.2 Пайплайн `place_exit_order` (ROUTER-EXIT)
- Вызывается напрямую или через `place_order(..., is_exit=True)`.
- Сверяет незавершенные выходы (`_settle_exits`), резервирует объем позиции.
- Выставляет ордер с `reduceOnly=True` для деривативов.
- При исполнении освобождает риск-слот через `risk.release_position`.

### 4.3 Kill-Switch интеграция
- В конструкторе подписывается: `register_kill_callback(self._cancel_all_orders)`.
- Устанавливает диспетчер: `risk.set_order_canceller(kill_switch_dispatch)`.
- При вызове kill-switch выполняет `connector.emergency_stop(self.exchange)`: отменяет обычные и algo-ордера, останавливает grid/DCA ботов, помечает открытые ордера в storage как `canceled`.

---

## 5. Анализ `src/order_owner.py` (Реестр владельцев и тегов)

OKX накладывает строгое ограничение: `clOrdId` (и `algoClOrdId`) — до 32 символов, только `[a-z0-9]`.

### 5.1 Карта префиксов
| Код | Класс | Владелец | Описание |
|---|---|---|---|
| `bot` | engine | `src/engine.py` | Дефолтный префикс движка |
| `botr` | strategy | `src/order_router.py` | Дефолтный префикс роутера |
| `botsdca` | strategy | `src/dca_bot.py` | Спотовый DCA-бот (demo) |
| `botldca` | live | `src/live_runner.py` | DCA-рукав live-кармана |
| `bottdca` | test | `src/dca_demo_run.py` | Тестовый прогон DCA |
| `lt` | test | `src/load_test.py` | Нагрузочные тесты |
| `rt` | test | `src/ratelimit_test.py`| Тесты rate-limit |
| `smk` | test | `src/smoke_test.py` | Smoke-тесты |
| `trd` | agent | OKX Trader | Сделки агента через CLI и нативные боты флота |
| `pmp` | agent | Pump Risk Taker | Памп-карман |
| `sen` | agent | Ops Sentinel | Аварийные ордера |
| `iex` | agent | Insight Executor | Ручные проверки demo |
| `hnt` | agent | Crypto Insight Hunter| Исследовательские тесты |
| `usr` | human | Human | Ручные сделки человека |

### 5.2 Инварианты владения
- Любой внутренний компонент проекта, ордера которого должны сверяться реконсилятором и писаться в `storage.py`, **обязан** иметь префикс, начинающийся с `bot*` (`own=True`).
- Внешние агенты и ручные скрипты используют независимые префиксы (`trd`, `pmp`, `iex`).

---

## 6. Анализ `src/fleet_manager.py` (Флот ботов: 50 ботов / 13 типов)

Модуль реализует управление расширенным флотом до 50 активных ботов по 13 нативным типам OKX (требование R4, инсайт `insights/bot-fleet-50.md`).

### 6.1 Каталог 13 типов ботов
1. `spot_grid` (Grid, SPOT, 1x)
2. `contract_grid_usdt` (Grid, SWAP/FUTURES USDT, плечо <= 3x)
3. `contract_grid_coin` (Grid, SWAP Coin-M, плечо <= 3x)
4. `smart_portfolio` (Portfolio, SPOT, 1x)
5. `contract_dca` (DCA, SWAP, плечо <= 3x)
6. `smart_arbitrage` (Arbitrage, SPOT+SWAP, дельта-нейтрально 1x)
7. `dcd_pendulum` (Structured, SPOT/EARN, 1x)
8. `spot_dca` (DCA, SPOT, 1x)
9. `recurring_buy` (DCA, SPOT, 1x)
10. `signal_bot` (Signal, SPOT/SWAP, плечо <= 3x)
11. `iceberg` (Execution Algo, SPOT/SWAP, 1x)
12. `twap` (Execution Algo, SPOT/SWAP, 1x)
13. `arbitrage` (Arbitrage, SPOT/SWAP/FUTURES, плечо <= 3x)

### 6.2 Квоты и формулы сайзинга
- `FLEET_MAX_BOTS = 50`. Функция `validate_fleet_quota(active, adding)` жестко отвергает превышение 50 слотов.
- `calculate_bot_allocation(total_equity, target_bots)`:
  - `reserve_usdt = total_equity * 0.30` (минимум 30% капитала всегда свободно).
  - `max_single_bot_cap = total_equity * 0.02` (не более 2% equity на 1 бота).
  - `recommended_single_bot_usdt = min(max_single_bot_cap, total_equity * 0.70 / target_bots)`, минимум 50 USDT.
- Генерация ID: `generate_fleet_bot_id()` генерирует 32-значный `algoClOrdId` с префиксом `trd`.
- Формирование CLI-команд: `build_cli_command(bot_type, params)` с обязательной проверкой стоп-лосса и принудительным ограничением плеча `min(lever, 3)`.

---

## 7. Архитектурный план реализации новых модулей (R1 – R5)

### 7.1 R1: Модуль Funding Carry арбитража (`src/funding_carry.py`)
- **Задача:** Дельта-нейтральный сбор ставки финансирования между спотом и бессрочным свопом BTC/ETH (`insights/funding-carry.md`).
- **Архитектурные требования:**
  - Парные ордера: Spot Long (`BTC-USDT`) + Swap Short 1x isolated (`BTC-USDT-SWAP`).
  - Оценка издержек: Учет 4 ног комиссий (спот × 2 + своп × 2) = 0.20% (maker) … 0.30% (taker).
  - Капиталоэффективность: Учет фактора 0.5x (для $1 000 нотионала требуется $1 000 спота + $1 000 маржи шорта = $2 000 капитала).
  - Маржинальный контроль: Мониторинг `marginRatio` шорт-ноги. При 1x isolated ликвидация наступает при росте цены на +99.1%.
  - Роутинг: Выставление через `OrderRouter` с префиксом `botcar` (зарегистрировать в `order_owner.py`).
  - Атрибуция: PnL автоматически мапится в рукав `funding_carry` (`ops/sleeves.json`).

### 7.2 R2: Модуль сигнальной стратегии Mean Reversion (`src/meanrev_strategy.py`)
- **Задача:** Исполняемый сигнальный бот на споте BTC-USDT по барам 1H на базе `insights/meanrev-strategy-design.md` и бэктестера `src/backtest/meanrev.py`.
- **Архитектурные требования:**
  - Индикаторы: RSI(14) Уайлдера, Bollinger Bands(20, 2) на `typical_price`, ATR(14).
  - Вход: `crossed_above(RSI, 30)` AND `close <= BB_mid` AND `vol > 0`.
  - Выход: Приоритет 1: ATR-стоп (`entry - 1.5 * ATR(14)`) -> Приоритет 2: Minimal ROI (2.0% -> 1.2% -> 0.6% -> 0% на 24ч) -> Приоритет 3: Индикаторный выход (`crossed_above(RSI, 70)` AND `close >= BB_mid`).
  - Роутинг: Вызовы `OrderRouter.place_order` (вход) и `OrderRouter.place_exit_order` (выход).
  - Префикс владельца: `botmr` (зарегистрировать в `order_owner.py`).
  - Атрибуция: Рукав `signal` в `ops/sleeves.json`.

### 7.3 R3: Казначейский модуль размещения свободной ликвидности (`src/idle_earn.py`)
- **Задача:** Автоматический опрос свободного остатка USDT/USDC и размещение в Simple Earn Flexible (`insights/idle-cash-earn.md`).
- **Архитектурные требования:**
  - Свободный остаток: `availBal` за вычетом резерва (30% equity) и обязательств активных ботов.
  - Поведение в Demo: В OKX Demo эндпоинты `earn/savings/balance` и `purchase` возвращают ошибку `50038` ("This feature is unavailable in demo trading"). Модуль обязан иметь адаптер с поддержкой симулированного/мок-режима в Demo, чтобы не падать с `50038`, и реального API при наличии live-прав.
  - Мгновенный отзыв: Предоставление метода `ensure_liquidity(required_usdt)`, который выполняет `redeem` при запросе маржи от торговых стратегий.
  - Атрибуция: Рукав `cash_earn` в `ops/sleeves.json`.

### 7.4 R4: Интеграция флота ботов (50 ботов)
- **Архитектурные требования:**
  - Подключение `src/fleet_manager.py` к `src/ops.py` (статус флота, квота, распределение капитала).
  - Интеграция с диспетчером kill-switch: при аварийной остановке нативные боты останавливаются пачками по 10 штук через `connector.stop_grid_bots` и `connector.stop_dca_bots`.

### 7.5 R5: Конвейер делегирования легковесных задач в Meta Muse Code
- **Архитектурные требования:**
  - Использование скрипта `ops/delegate.ps1`:
    - `submit -Prompt "..." -Role <роль>`: постановка в `ops/delegations/inbox/`.
    - `fetch -Id <id>`: опрос результатов из `ops/delegations/outbox/`.
  - Делегирование поддерживающих задач: генерация моков unit-тестов, форматирование документации, калибровочные расчеты.

---

## 8. Матрица интерфейсов и зависимостей

| Модуль | Вызывает (Зависимости) | Предоставляет интерфейс для | Ключевые методы / контракты |
|---|---|---|---|
| `src.engine` | `connector`, `storage`, `reconciler`, `risk`, `ws_client`, `account_mode` | Ops-скрипты (`ops/engine.ps1`), Watchdog | `start()`, `stop()`, `place_order()`, `stats` |
| `src.risk` | `sqlite3`, `connector.kill_switch_dispatch` (через колбэк) | `engine`, `order_router`, торговые модули | `check_entry_allowed()`, `size_position()`, `check_exit_allowed()`, `release_position()`, `register_spot_buy()`, `update_equity()`, `record_pnl()`, `kill_switch()`, `status()` |
| `src.order_router`| `risk`, `connector`, `storage`, `order_owner`, `account_mode` | `engine`, `dca_bot`, будущие R1/R2 стратегии | `place_order(inst_id, side, ord_type, ...)`, `place_exit_order(inst_id, side, ...)`, `settle_exits()`, `close()` |
| `src.order_owner` | `connector.new_client_order_id` | `order_router`, `fleet_manager`, `dca_bot`, CLI | `new_cl_ord_id(code)`, `owner_of(cl_ord_id)`, `require(code, own=True)`, `classify(order)` |
| `src.fleet_manager`| `order_owner.new_client_order_id`, `risk` | CLI, Gateway, будущие флоу управления | `get_supported_bot_types()`, `validate_fleet_quota()`, `calculate_bot_allocation()`, `build_cli_command()` |
| `src.account_mode`| `connector`, CCXT | `engine`, `order_router`, CLI | `fetch_account_mode()`, `check_no_borrow()`, `spot_order_params()`, `switch_account_level()` |
| `src.connector` | `ccxt`, `config`, `errors` | `engine`, `order_router`, `risk` (через реестр) | `create_exchange()`, `emergency_stop()`, `register_kill_callback()`, `kill_switch_dispatch()` |
| `src.storage` | `sqlite3` | `engine`, `order_router`, `reconciler`, стратегии | `upsert_order()`, `insert_trade()`, `record_equity()`, `set_ws_state()`, `get_ws_state()` |

---

## 9. Рекомендации по последовательности реализации

1. **Фаза 1: Реестр префиксов владения (`src/order_owner.py`):**
   - Добавить новые префиксы для стратегий: `botcar` (Funding Carry, `own=True`), `botmr` (Mean Reversion, `own=True`), `bottrn` (Treasury Earn, `own=True`).
   - Синхронизировать `ops/sleeves.json`.
2. **Фаза 2: Реализация торговых модулей R1, R2, R3:**
   - **R1 (`src/funding_carry.py`):** модуль дельта-нейтрального арбитража с синхронным открытием spot/swap и контролем маржи.
   - **R2 (`src/meanrev_bot.py`):** модуль сигнальной стратегии Mean Reversion на базе `src/backtest/meanrev.py`, подключенный к `OrderRouter`.
   - **R3 (`src/idle_earn.py`):** казначейский контроллер Simple Earn Flexible с поддержкой Demo-мока (обход ошибки 50038).
3. **Фаза 3: Интеграция флота ботов (R4):**
   - Подключение `src/fleet_manager.py` к единой отчетности и мониторингу `src/ops.py`.
4. **Фаза 4: Делегирование через Meta Muse Code (R5):**
   - Формирование задач генерации тестов и проверок через `ops/delegate.ps1 submit`.
5. **Фаза 5: Полный регресс и верификация:**
   - Запуск независимых unit-тестов с моками API.
   - Подтверждение непрерывности работы `src.engine` (PID 15744).
