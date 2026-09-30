# Спецификация технических требований: Funding Carry, Mean Reversion, Idle Cash Earn и Bot Fleet 50

- **Дата:** 2026-09-30
- **Статус:** validated — глубокое исследование первоисточников (`insights/`, `src/`, `tests/`, OKX API v5, CLI 1.4.8)
- **Исполнитель:** survey_miner_1 (Specification Miner)
- **Целевые файлы источников:**
  - R1: `insights/funding-carry.md`, `insights/futures-bots.md`, `insights/demo-slippage.md`, `insights/pnl-ledger.md`
  - R2: `insights/meanrev-strategy-design.md`, `src/backtest/meanrev.py`, `tests/test_meanrev.py`, `src/risk.py`, `src/order_router.py`
  - R3: `insights/idle-cash-earn.md`, `.agents/skills/okx-cex-earn/SKILL.md`, `references/savings-commands.md`, `references/autoearn-commands.md`
  - R4: `insights/bot-fleet-50.md`, `src/fleet_manager.py`, `tests/test_fleet_manager.py`, `src/risk.py`, `ops/sleeves.json`, `src/order_owner.py`

---

## 1. Реестр обнаруженных возможностей (Features Discovered)

| # | Category | Feature | Description | Inputs | Outputs | Error Behavior | Discovered Via |
|---|----------|---------|-------------|--------|---------|----------------|----------------|
| 1 | R1: Funding Carry | Мониторинг ставки funding rate | Получение текущей и прогнозной ставки финансирования для бессрочного свопа (BTC/ETH-USDT-SWAP) | `instId` (напр. `BTC-USDT-SWAP`) | `fundingRate`, `fundingTime`, `nextFundingTime`, `minFundingRate`, `maxFundingRate`, `interestRate` | Сетевой сбой / невалидный `instId` -> ошибка REST/CCXT | `insights/funding-carry.md` §1, `GET /api/v5/public/funding-rate` |
| 2 | R1: Funding Carry | История ставки funding rate | Пагинация исторической ставки фандинга до 94 дней (282 периода по 8ч) через курсор `after` | `instId`, `after` (ts самого старого периода) | Массив периодов: `fundingRate`, `realizedRate`, `fundingTime` | Запрос с `before` не возвращает старых данных; глубже 94 дней — пустой массив | `insights/funding-carry.md` §1, `GET /api/v5/public/funding-rate-history` |
| 3 | R1: Funding Carry | Расчет чистой доходности (Net Yield & Annualization) | Расчет годовой доходности с учетом 1095 периодов (3×365) и дисконта капиталоэффективности 0.5x | `fundingRate` (среднее/текущее), нотионал, капитал | `annualized_rate_%`, `net_yield_on_capital_%` (с поправкой на x2 капитал и комиссии) | При отрицательной ставке доходность становится отрицательной (шорт платит лонгу) | `insights/funding-carry.md` §2, §6 |
| 4 | R1: Funding Carry | Расчет комиссии 4 ног (4 fee legs) | Расчет round-trip издержек входа и выхода: spot buy, swap short open, swap short close, spot sell | Тарифные ставки maker/taker для spot (0.08%/0.10%) и swap (0.02%/0.05%) | Суммарная комиссия: taker 0.30%, mixed 0.25%, maker 0.20% | Неверный tier тарифа завышает/занижает оценку | `insights/funding-carry.md` §4, `okx.com/fees` |
| 5 | R1: Funding Carry | Расчет порога окупаемости (Breakeven Days) | Определение минимального горизонта удержания позиции для компенсации комиссий 4 ног | `round_trip_%`, `annualized_funding_%` | `breakeven_days = round_trip_% / (annualized_% / 365)` | При funding <= 0 окупаемость недостижима (`inf`) | `insights/funding-carry.md` §5 |
| 6 | R1: Funding Carry | Контроль ликвидации шорта 1x isolated | Оценка дистанции до цены ликвидации при изолированном плече 1x | `entry_px`, `ctVal`, `margin`, `mmr` (0.4%), `fee` (0.05%) | `liq_px / entry_px = 2 / (1 + mmr + fee) ≈ 1.991` (+99.1% роста цены) | Ошибочный ввод leverage > 1 сокращает дистанцию до ликвидации | `insights/funding-carry.md` §6, `src/risk.py:335` |
| 7 | R1: Funding Carry | Дельта-нейтральное парное исполнение | Синхронная покупка спота и открытие шорта на свопе с одинаковым базовым объемом | `instId_spot`, `instId_swap`, целевой объем | Парные ордера: spot buy + swap sell short (1x isolated) | Рассинхрон исполнения ног (execution leg risk) -> дельта-риск | `insights/funding-carry.md` §0, §9, `insights/futures-bots.md` §4 |
| 8 | R1: Funding Carry | Мониторинг маржинального коэффициента шорта | Контроль таяния изолированной маржи шорт-ноги при росте базовой цены | `marginRatio` из `account/positions` | Коэффициент риска шорт-позиции, сигнал на ребалансировку маржи | При резком пампе маржа шорта не пополняется прибылью спота автоматически | `insights/funding-carry.md` §6 |
| 9 | R1: Funding Carry | Изоляция PnL в рукав `funding_carry` | Атрибуция выплат фандинга (bills type 8) и торгового результата в рукав `funding_carry` | Bills OKX `type=8`, `clOrdId` | Направление PnL в рукав `funding_carry` в `pnl_ledger` | Неразмеченный ордер попадает в `manual` | `insights/pnl-ledger.md` §2, `ops/sleeves.json` |
| 10 | R2: Mean Reversion | Расчет индикаторов 1H (RSI, BB, ATR) | Построение RSI(14) Уайлдера, Bollinger Bands(20, 2) на Typical Price, ATR(14) Уайлдера | Бары 1H (Open, High, Low, Close, Volume) | Ряды `rsi`, `bb_mid`, `bb_upper`, `bb_lower`, `atr` | При истории < 40 баров — NaN; безопасный warmup = 200 баров | `insights/meanrev-strategy-design.md` §2, `src/backtest/meanrev.py:98` |
| 11 | R2: Mean Reversion | Детекция сигнала входа (Entry Signal) | Проверка пересечения RSI 30 снизу вверх, фильтра средней линии BB и подтверждения разворота цены | `rsi[t-1, t]`, `mid[t]`, `bars[t-1, t]` | `bool`: вход `long` на подтвержденной свече `t`, исполнение на `open(t+1)` | `volume == 0` или `close > mid` блокируют ложный сигнал | `insights/meanrev-strategy-design.md` §3, `src/backtest/meanrev.py:52` |
| 12 | R2: Mean Reversion | Детекция выхода по индикатору (Exit Signal) | Пересечение RSI 70 снизу вверх при нахождении цены выше средней линии BB | `rsi[t-1, t]`, `mid[t]`, `bars[t-1, t]`, `price_guard` | `bool`: закрытие позиции по сигналу перекупленности | Guard `close(t) < close(t-1)` математически несовместим с ростом RSI на баре -> требует `exit_price_guard=False` | `insights/meanrev-strategy-design.md` §4, `tests/test_meanrev.py:111` |
| 13 | R2: Mean Reversion | Волатильно-адаптивный стоп-лосс (ATR Stop) | Вычисление фиксированного уровня стопа в момент входа `entry - 1.5 * ATR(14)` | `entry_px`, `atr(14)`, `stop_atr_mult=1.5` | `stop_price` | `stop_price <= 0` или `stop_price >= entry` -> отказ сайзинга | `insights/meanrev-strategy-design.md` §5.1, `src/backtest/meanrev.py:130` |
| 14 | R2: Mean Reversion | Таблица динамического Minimal ROI | Каскадный выход из позиции по достижению требуемой нетто-доходности в зависимости от времени | Время в сделке (минуты), нетто-доходность позиции | `close("roi")` при превышении порога таблицы (0m: 2%, 240m: 1.2%, 720m: 0.6%) | При отрицательной доходности ROI-выход не срабатывает | `insights/meanrev-strategy-design.md` §6, `src/backtest/meanrev.py:38` |
| 15 | R2: Mean Reversion | Принудительный тайм-стоп (Time Stop) | Закрытие зависшей позиции через 1440 минут (24 бара / 24ч) при любом знаке PnL | Время в сделке `minutes >= 1440` | `close("time_stop")` | Задержка проверки бара откладывает выход до следующего тика | `insights/meanrev-strategy-design.md` §6.2, `src/backtest/meanrev.py:122` |
| 16 | R2: Mean Reversion | Валидация допуска входа в `src/risk.py` | Проверка kill-switch, глобального (-15%) и дневного (-6%) брейкеров, свежести equity, лимитов входов | `inst_id`, `side="buy"` | `allowed: bool`, `reason: str` | Отказ при просадке, устаревшем equity (>600s), >10 сделок/день, 3 убытках подряд | `src/risk.py:264`, `check_entry_allowed` |
| 17 | R2: Mean Reversion | Fixed-fractional сайзинг позиции | Расчет размера ордера от риска 1% equity, обрезка потолком 15% equity и капом 30% | `equity`, `entry`, `stop`, `ct_val`, `lot_sz`, `min_sz`, `risk_pct=1.0` | `{"size": contracts, "dollar_risk": ..., "notional": ..., "warnings": [...]}` | Notional > 30% equity -> отказ (size=0.0); size < min_sz -> отказ | `src/risk.py:295`, `size_position` |
| 18 | R2: Mean Reversion | Маршрутизация через `src/order_router.py` | Исполнение входа со спотовым `tdMode` (cash/cross), проверкой запрета займа, throttler и clOrdId | Параметры ордера, `owner="botmr"` | Результат OKX REST, запись в `storage.db`, регистрация слота в риске | Ошибка биржи, превышение лимита 20/2с (задержка), попытка займа -> отказ | `src/order_router.py:228`, `place_order` |
| 19 | R2: Mean Reversion | Контролируемый выход через `place_exit_order` | Закрытие позиции через `check_exit_allowed`, `release_position` без блокировки брейкерами аварии | `inst_id`, `side="sell"`, `ord_type`, `sz=None` | Закрытие ордера выхода, уменьшение остатка, освобождение слота риска | Выход превышает остаток позиции -> отказ; сторона совпадает с позицией -> отказ | `src/order_router.py:354`, `src/risk.py:448, 477` |
| 20 | R3: Idle Cash Earn | Мониторинг свободной ликвидности | Вычисление доступного остатка USDT/USDC за вычетом маржи позиций, открытых ордеров и 30% буфера | `account/balance`: `availBal`, `frozenBal`, `eq` | `free_cash_usdt = max(0, availBal - frozenBal - reserve_30pct)` | Неверный баланс при отсутствии обновления equity | `insights/idle-cash-earn.md` §4, `insights/bot-fleet-50.md` §1 |
| 21 | R3: Idle Cash Earn | Пороговый свиппинг (Sweeping Threshold) | Автоматическое размещение свободных средств в Simple Earn Flexible при превышении порога | `free_cash_usdt >= sweep_threshold` (напр. 50 или 100 USDT) | Запуск подписки `earn savings purchase` | Свиппинг не запускается, если свободный кэш ниже порога | `insights/idle-cash-earn.md` §6, `skills/okx-cex-earn` |
| 22 | R3: Idle Cash Earn | Подписка на Simple Earn Flexible | Отправка ордера подписки на гибкие сбережения OKX | `ccy="USDT"`, `amt`, `rate="0.01"` | ID подписки, перемещение средств на счет `earn` | В demo возвращает ошибку `50038` (недоступно в demo); в live требует права `Earn` | `POST /api/v5/finance/savings/purchase-redempt`, `insights/idle-cash-earn.md` §1 |
| 23 | R3: Idle Cash Earn | Мгновенный выкуп (Instant Redemption) | Возврат средств из Simple Earn на торговый счет по запросу стратегий или маржин-колла | `ccy="USDT"`, `amt` | Мгновенное зачисление средств на торговый счет | При 100% утилизации пула OKX возможна задержка до 1 часа | `POST /api/v5/finance/savings/purchase-redempt` (`side="redempt"`), `insights/idle-cash-earn.md` §3 |
| 24 | R3: Idle Cash Earn | Опрос процентных ставок (Lending Rates) | Публичный мониторинг заматченной ставки (`lendingRate`) и индикативной (`rate`) | `ccy="USDT"`, `limit` | Почасовая история ставок, 24ч среднее (`avgRate`), прогноз след. часа (`estRate`) | Эндпоинт публичный, работает без авторизации даже при неактивном ключе | `GET /api/v5/finance/savings/lending-rate-summary`, `insights/idle-cash-earn.md` §2 |
| 25 | R3: Idle Cash Earn | Атрибуция дохода в рукав `cash_earn` | Учет начисленных процентов лендинга в реестре PnL рукава `cash_earn` | `finance/savings/lending-history` | Запись доходов в `pnl_ledger` рукава `cash_earn` | В demo история начислений возвращает ошибку `50038` | `insights/pnl-ledger.md` §2, `ops/sleeves.json` |
| 26 | R4: Bot Fleet 50 | Каталог 13 типов нативных ботов OKX | Структурированный реестр спецификаций всех 13 типов стратегий OKX | Ключ стратегии (`spot_grid`, `contract_grid_usdt`, ...) | `BotTypeSpec` (название RU/EN, категория, семейство, плечо, ссылка) | Запрос неизвестного ключа -> `ValueError` | `src/fleet_manager.py:49`, `insights/bot-fleet-50.md` §2 |
| 27 | R4: Bot Fleet 50 | Валидация квоты флота (до 50 ботов) | Проверка непревышения общего лимита 50 работающих ботов | `current_active_bots`, `adding_bots` | `(allowed: bool, msg: str)` | При `current + adding > 50` -> `allowed = False`, запуск блокируется | `src/fleet_manager.py:228`, `src/risk.py:70` |
| 28 | R4: Bot Fleet 50 | Расчет сайзинга капитала на бота | Расчет аллокации с сохранением >= 30% резерва equity и потолком 2.0% на одного бота | `total_equity`, `target_bots=50` | `{"total_equity", "recommended_single_bot_usdt", "max_single_bot_usdt", "reserve_usdt"}` | Equity <= 0 -> минимальный предохранитель 50 USDT | `src/fleet_manager.py:239`, `insights/bot-fleet-50.md` §3 |
| 29 | R4: Bot Fleet 50 | Жесткий потолок плеча деривативных ботов | Ограничение кредитного плеча контрактных сеток и DCA ботов до 3x | Параметр `lever` | `lever = min(lever, 3)` (инвариант `MAX_LEVERAGE = 3`) | `lever > 3` обрезается до 3x с логированием | `src/fleet_manager.py:299, 339`, `src/risk.py:66, 752` |
| 30 | R4: Bot Fleet 50 | Генерация безопасных CLI-команд | Формирование вызовов `okx bot grid/dca create` с обязательным стоп-лоссом и тегом владельца | `bot_type_key`, `params` | Массив аргументов командной строки `["okx", "--demo", "bot", ...]` | Отсутствие стоп-лосса или неизвестный тип бота -> ошибка валидации | `src/fleet_manager.py:267`, `insights/bot-fleet-50.md` §4 |
| 31 | R4: Bot Fleet 50 | Маркировка ордеров ORDER-OWNER-TAG | Генерация 32-значного `algoClOrdId` с префиксом `trd` (или `flt` для флота) | Код владельца (`trd`/`flt`) | 32-значный буквенно-цифровой идентификатор `algoClOrdId` | Невалидный код владельца -> `ValueError` в `order_owner.require` | `src/order_owner.py:36, 124`, `src/fleet_manager.py:262` |
| 32 | R4: Bot Fleet 50 | Изоляция рисков по рукавам (Sleeves) | Разделение позиций и капитала между рукавами (`demo_fleet`, `native_grid`, `dca`, `signal`) | Таблицы маппинга `ops/sleeves.json` | Изолированная отчетность в `pnl_ledger` | Бот без префикса попадает в `manual` | `ops/sleeves.json`, `insights/pnl-ledger.md` §2 |
| 33 | R4: Bot Fleet 50 | Пакетная экстренная остановка (Kill-Switch) | Массовая остановка всех ботов пачками по 10 штук через `connector.emergency_stop` | Команда `kill_switch(flatten=...)` | Отмена всех ордеров и остановка сеток флота | Сбой сетевого вызова логируется в `risk_events` | `src/risk.py:654`, `src/connector.py:77` |

---

## 2. Граничные случаи и аномалии (Edge Cases)

| # | Feature | Input | Observed Behavior |
|---|---------|-------|-------------------|
| 1 | R1: Funding Carry | Глубина публичной истории фандинга | Запрос `funding-rate-history` с параметром `before` не возвращает старых записей; пагинация работает строго через `after`. Максимальная глубина — ровно 282 периода (94.0 дня), глубже возвращается пустой список `[]`. |
| 2 | R1: Funding Carry | Расчет ставки в demo vs live | Значения ставки фандинга в demo-среде не совпадают с live для того же момента времени (расхождение до 9 раз: 0.0043% demo vs 0.0005% live), так как demo использует симулированный ордербук. Demo годен для проверки кода, но не экономики. |
| 3 | R1: Funding Carry | Режим аккаунта OKX demo | На аккаунте demo с `acctLv=1` (Simple mode) торговля деривативами (SWAP) заблокирована биржей. Для открытия шорт-ноги требуется переключение на `acctLv >= 2` (Single-currency margin или Multi-currency). |
| 4 | R1: Funding Carry | Асимметрия маржи 1x isolated | При 1x isolated шорт требует 100% обеспечения. Рост цены спота дает нереализованную прибыль, но не пополняет маржу шорт-ноги автоматически. Шорт может приблизиться к margin call при экстремальном росте, если не настроен мониторинг `marginRatio`. |
| 5 | R2: Mean Reversion | Недостижимость price-guard на выходе RSI | В `exit_signal`: условие `crossed_above(rsi, 70)` и guard `close(t) < close(t-1)` одновременно невозможны для RSI Уайлдера от Close, поскольку рост RSI выше 70 требует строго `close(t) > close(t-1)`. Требуется `exit_price_guard=False`. |
| 6 | R2: Mean Reversion | Избыточность price-guard на входе RSI | В `entry_signal`: пересечение `crossed_above(rsi, 30)` автоматически гарантирует `close(t) > close(t-1)`. Проверка цен закрытия валидна, но математически избыточна. |
| 7 | R2: Mean Reversion | Обрезка сайзинга потолком 15% equity | При малом ATR (низкая волатильность) формула fixed-fractional (1% риска) требует размер позиции > 15% equity. `size_position` не отклоняет ордер, а безопасно обрезает размер до 15% с предупреждением в `warnings[]`. |
| 8 | R2: Mean Reversion | Превышение лимита Notional 30% equity | Если даже после обрезки или при ручном вводе `sz * entry > 0.30 * equity`, срабатывает правило `rookie-airbag`: ордер полностью отклоняется (`contracts = 0.0`, `notional = 0.0`). |
| 9 | R2: Mean Reversion | Свежесть equity (Stale Equity) | Если с момента последнего вызова `update_equity` прошло > 600 секунд (или системные часы ушли в прошлое/будущее), `check_entry_allowed` возвращает `False`, запрещая вход в позицию. Выход из позиции (`check_exit_allowed`) при этом разрешен! |
| 10 | R3: Idle Cash Earn | Ошибка `50038` в OKX Demo | Вызовы приватных эндпоинтов `earn savings balance`, `purchase` и `redeem` в demo возвращают `code: "50038", msg: "This feature is unavailable in demo trading"`. Справочные списки `fixed-products` и публичные `lending-rate-history` работают нормально. |
| 11 | R3: Idle Cash Earn | Блокировка отключения Auto Earn на 24 часа | Аппаратное ограничение API OKX для Trading Account Auto Earn (`earn_auto_set`): после включения тумблера функция не может быть отключена в течение 24 часов. |
| 12 | R3: Idle Cash Earn | Временная задержка выкупа при 100% утилизации | Если весь пул ликвидности USDT на бирже заимствован маржинальными трейдерами, моментальный выкуп может быть временно задержан (пересмотр лимитов каждый час). Наличие 30% неприкосновенного кэша полностью защищает от дефицита маржи. |
| 13 | R4: Bot Fleet 50 | Превышение лимита квоты 50 ботов | Попытка добавить 51-го бота при 50 активных отклоняется `validate_fleet_quota` с сообщением `Превышена квота флота: активно 50, максимум 50 ботов`. |
| 14 | R4: Bot Fleet 50 | Потолок плеча для контрактных ботов | Передача параметров с плечом 5x или 10x для `contract_grid_usdt` или `contract_dca` принудительно обрезается функцией `build_cli_command` до `MAX_LEVERAGE = 3`. |
| 15 | R4: Bot Fleet 50 | Коллизия префиксов ORDER-OWNER-TAG | Код владельца проекта обязан начинаться с `bot` и иметь 4-й символ из диапазона `g-z` (не hex!). Префикс `bot` + hex-буква (0-9, a-f) запрещен валидатором `validate_registry`, чтобы избежать коллизий с clOrdId движка (`bot` + uuid hex). |

---

## 3. Детальные спецификации по направлениям (Technical Deep Dive)

### 3.1. R1: Funding Carry Арбитраж (Spot Long + Swap Short 1x)

#### 1. Официальная формула Funding Rate OKX
Расчет ставки финансирования производится по формуле:
$$\text{Funding Rate} = \text{Clamp}\Big(\text{AvgPremiumIndex} + \text{Clamp}(\text{InterestRate} - \text{AvgPremiumIndex}, 0.05\%, -0.05\%), \text{Cap}, \text{Floor}\Big)$$
Где:
- $\text{Interest Rate} = \frac{0.03\%}{24 / N}$, при стандартном интервале $N = 8$ часов составляет $0.01\%$ за период (фиксированная базовая часть).
- $\text{Premium Index} = \frac{\max(0, \text{ImpactBid} - \text{Index}) - \max(0, \text{Index} - \text{ImpactAsk})}{\text{Index}}$.
- Лимиты Cap/Floor:
  - BTC-USDT-SWAP: $\text{Cap} = +0.375\%$, $\text{Floor} = -0.375\%$ за 8 часов.
  - ETH-USDT-SWAP: $\text{Cap} = +0.750\%$, $\text{Floor} = -0.750\%$ за 8 часов.
- График начислений: каждые 8 часов (00:00, 08:00, 16:00 UTC). При экстремальной волатильности интервал может динамически сокращаться (8ч $\to$ 4ч $\to$ 2ч $\to$ 1ч).

#### 2. Издержки 4 ног и порог окупаемости (Breakeven)
Для открытия и закрытия позиции carry необходимо совершить 4 сделки:
1. Открытие Long на споте (Spot Buy).
2. Открытие Short на свопе (Swap Short Open).
3. Закрытие Short на свопе (Swap Short Close / Buy-to-cover).
4. Закрытие Long на споте (Spot Sell).

Базовые комиссии OKX:
- SPOT: Maker $0.08\%$, Taker $0.10\%$.
- SWAP: Maker $0.02\%$, Taker $0.05\%$.

Итоговые сценарии комиссий на круг (Round-trip fee):
- **Сценарий All Taker:** $(0.10\% + 0.05\%) \times 2 = \mathbf{0.30\%}$ от нотионала.
- **Сценарий Mixed (2 maker + 2 taker):** $0.10\% + 0.05\% + 0.08\% + 0.02\% = \mathbf{0.25\%}$.
- **Сценарий All Maker (лимитные заявки с Chase):** $(0.08\% + 0.02\%) \times 2 = \mathbf{0.20\%}$.

Формула безубыточности по времени:
$$\text{Breakeven Days} = \frac{\text{Round-trip Fee } \%}{\text{Annualized Funding } \% / 365}$$
При среднем бычьем funding на BTC $5.75\%$ годовых:
- При Maker ($0.20\%$): $\mathbf{12.7}$ дней.
- При Taker ($0.30\%$): $\mathbf{19.0}$ дней.
На ETH (при funding $4.14\%$): от $\mathbf{17.6}$ до $\mathbf{26.4}$ дней (в слабом боковике при $2.3\%$ — до $47.6$ дней).

#### 3. Дисконт капиталоэффективности (Capital Efficiency Drag)
Для поддержания дельта-нейтральности на $\$1000$ спотового актива требуется открыть шорт нотионалом $\$1000$. При изолированном плече 1x обеспечение шорта требует $\$1000$ USDT маржи.
- Суммарный капитал рукава: $\$1000 \text{ (спот)} + \$1000 \text{ (маржа шорта)} = \mathbf{\$2000}$.
- Коэффициент капиталоемкости: $\mathbf{0.5x}$.
- Реальная доходность на весь вложенный капитал:
$$\text{Yield}_{\text{capital}} = \frac{\text{Funding Yield}_{\text{notional}}}{2}$$
При заголовочной ставке BTC $5.75\%$ реальная доходность на баланс составляет $\approx \mathbf{2.88\%}$ годовых.

#### 4. Дистанция до ликвидации шорта 1x Isolated
Официальная формула цены ликвидации OKX для USDT-margined контрактов:
$$\text{Liq Price} = \frac{\text{Margin} + \text{ctVal} \cdot |N| \cdot \text{Entry}}{\text{ctVal} \cdot |N| \cdot (1 + \text{MMR} + \text{Fee})}$$
При плече 1x $\text{Margin} = \text{ctVal} \cdot |N| \cdot \text{Entry}$, что упрощает отношение:
$$\frac{\text{Liq Price}}{\text{Entry}} = \frac{2}{1 + \text{MMR} + \text{Fee}} = \frac{2}{1 + 0.004 + 0.0005} \approx \mathbf{1.991}$$
Цена базового актива должна вырасти на $\mathbf{+99.1\%}$ от точки входа для наступления ликвидации.

#### 5. Правила дельта-нейтральности и ребалансировки
- **Квантование контрактов:** На BTC-USDT-SWAP 1 контракт = $0.01$ BTC ($\text{ctVal} = 0.01$). Спотовый объем обязан быть кратен шагу лота контракта: $\text{Qty}_{\text{spot}} = N_{\text{contracts}} \times 0.01$.
- **Базисный спред (Basis):** $\text{Basis} = P_{\text{swap}} - P_{\text{spot}}$. Вход предпочтителен при положительном базисе (контанго, $P_{\text{swap}} \ge P_{\text{spot}}$).
- **Слиппедж-контроль:** По замерам `insights/demo-slippage.md`, спред BTC/ETH составляет $0.01$ бп, среднее проскальзывание market-ордеров — $0.02$ бп (максимум $0.11$ бп). Для минимизации комиссий рекомендуется использовать maker-ордера.
- **Мониторинг маржи шорта:** При росте базового актива прибыль на споте накапливается, но изолированная маржа шорта уменьшается. Необходимо отслеживать `marginRatio` позиции шорта и выполнять довнос маржи при падении ниже $300\%$.

---

### 3.2. R2: Сигнальная стратегия Mean Reversion (RSI + Bollinger Bands)

#### 1. Параметры индикаторов и прогрев
- **Таймфрейм:** 1H (`bar = "1H"`), инструмент `BTC-USDT` спот, режим `long-only` без плеча.
- **RSI(14):** Модифицированное экспоненциальное сглаживание Уайлдера (Wilder smoothing), период 14, пороги 30/70.
- **Bollinger Bands(20, 2):** Простая скользящая средняя (SMA) за 20 периодов, полосы на $\pm 2.0$ стандартных отклонения (смещение `ddof=0`).
  - База расчета: **Typical Price** $TP = \frac{\text{High} + \text{Low} + \text{Close}}{3}$ (для полной совместимости с эталонной реализацией Freqtrade).
- **ATR(14):** Сглаживание Уайлдера за 14 периодов от истинного диапазона (True Range).
- **Warmup:** Требуется минимум 200 подтвержденных баров 1H (`startup_candle_count = 200`) для устойчивой сходимости рекуррентных индикаторов.

#### 2. Алгоритм входа (Entry Signal)
Сигнал генерируется на закрытии подтвержденного бара $t$ (флаг биржи `confirm=1`), исполнение происходит по цене открытия следующего бара $\text{open}(t+1)$:
$$\begin{aligned}
\text{Entry}(t) = &\;\text{crossed\_above}(\text{RSI}_{14}, 30, t) \\
&\land \text{Close}(t) \le \text{BB\_Middle}(t) \\
&\land \text{Close}(t) > \text{Close}(t-1) \\
&\land \text{Volume}(t) > 0
\end{aligned}$$
Условие `crossed_above(rsi, 30)` означает $\text{RSI}(t-1) \le 30 \land \text{RSI}(t) > 30$. Это предотвращает покупку «падающего ножа».

#### 3. Алгоритм выхода (Exit Conditions) и приоритеты
Внутри каждого бара условия выхода проверяются в детерминированном консервативном порядке:
1. **Приоритет 1: Стоп-лосс (Stop-Loss)**
   $$\text{Stop Price} = \text{Entry Price} - 1.5 \times \text{ATR}(14, t_{\text{entry}})$$
   Стоп фиксируется на баре входа и не сдвигается. Исполнение проверяется по внутрибаровому минимуму $\text{Low}(t) \le \text{Stop Price}$ с учетом проскальзывания 5 бп.
2. **Приоритет 2: Каскадная таблица Minimal ROI**
   Проверяется нетто-доходность позиции на закрытии бара:
   $$\text{Net ROI} = \frac{\text{Qty} \times \text{Close}(t) \times (1 - \text{TakerFee})}{\text{Entry Cost}} - 1.0$$
   Таблица порогов:
   - $0$ минут в сделке $\implies$ требуемая доходность $\ge \mathbf{2.0\%}$
   - $240$ минут (4 часа) $\implies$ требуемая доходность $\ge \mathbf{1.2\%}$
   - $720$ минут (12 часов) $\implies$ требуемая доходность $\ge \mathbf{0.6\%}$
   - $1440$ минут (24 часа) $\implies$ требуемая доходность $\ge \mathbf{0.0\%}$
3. **Приоритет 3: Тайм-стоп (Time Stop)**
   При нахождении в сделке $\ge 1440$ минут (24 часа / 24 свечи 1H) позиция принудительно закрывается при любом знаке PnL.
4. **Приоритет 4: Индикаторный сигнал выхода (Exit Signal)**
   $$\text{Exit}(t) = \text{crossed\_above}(\text{RSI}_{14}, 70, t) \land \text{Close}(t) \ge \text{BB\_Middle}(t)$$
   *(С обязательным отключением `exit_price_guard=False` ввиду доказанной математической несовместимости с RSI Уайлдера).*

#### 4. Интеграция с Risk Core (`src/risk.py`) и Order Router (`src/order_router.py`)
- Перед отправкой сигнала входа вызывается `risk.check_entry_allowed(inst_id, "buy")`.
- Расчет сайзинга выполняется через `risk.size_position`:
  - $\text{Dollar Risk} = \text{Equity} \times 1.0\%$.
  - $\text{Contracts} = \lfloor \frac{\text{Dollar Risk}}{|\text{Entry} - \text{Stop}| \times \text{ctVal}} \rfloor$.
  - Обрезка по лимиту $\text{MAX\_POSITION\_PCT} = 15\%$ equity.
  - Проверка анти-паттерна rookie-airbag ($\text{Notional} \le 30\%$ equity).
- Ордер передается в `OrderRouter.place_order(inst_id="BTC-USDT", side="buy", ord_type="limit"/"market", px=..., sz=..., stop_px=..., equity=..., owner="botmr")`.
- Закрытие позиции оформляется через `OrderRouter.place_exit_order(inst_id="BTC-USDT", side="sell", ord_type="market", sz=None, owner="botmr")`.

---

### 3.3. R3: Казначейский модуль размещения свободной ликвидности (Idle Cash Earn)

#### 1. Модель расчета свободного остатка ликвидности (Free Cash)
Казначейский контроллер опрашивает баланс торгового аккаунта `account/balance`:
$$\text{Free Cash}_{\text{USDT}} = \max\Big(0, \;\text{availBal}_{\text{USDT}} - \text{frozenBal}_{\text{USDT}} - \text{Reserve}_{\text{uncommitted}}\Big)$$
Где:
- $\text{availBal}_{\text{USDT}}$ — доступный остаток собственных средств (без учета кредитного плеча и заемных средств autoLoan).
- $\text{frozenBal}_{\text{USDT}}$ — средства, заблокированные под обеспечение лимитных ордеров и маржу открытых позиций.
- $\text{Reserve}_{\text{uncommitted}} = \text{Equity}_{\text{total}} \times 30\%$ — неприкосновенный резерв свободной ликвидности (инвариант бизнес-плана и регламента флота ботов).

#### 2. Пороговый свиппинг (Sweeping Protocol)
- **Условие триггера:** Если $\text{Free Cash}_{\text{USDT}} \ge \text{Sweep Threshold}$ (дефолт: $100$ USDT):
- **Параметры подписки:**
  - Эндпоинт: `POST /api/v5/finance/savings/purchase-redempt`
  - Тело запроса: `{"ccy": "USDT", "amt": "<FreeCash>", "side": "purchase", "rate": "0.01"}`
  - CLI эквивалент: `okx earn savings purchase --ccy USDT --amt <FreeCash> --rate 0.01`
  - Поле `rate = 0.01` (1% годовых) задает минимальный фильтр соответствия, гарантирующий немедленный матчинг с рыночным пулом без снижения фактической доходности `lendingRate`.

#### 3. Начисление процентов и экономика лендинга
- **Текущая ставка:** Почасовая ставка `lendingRate` составляет в среднем $3.0\% - 3.6\%$ годовых (медиана 30 дней — $3.03\%$, пики до $5.66\%$).
- **Комиссия OKX:** Биржа удерживает $15\%$ от процентного дохода заемщиков, инвестор получает $85\%$ (соотношение `lendingRate / rate ≈ 0.85–0.88`).
- **Тайминг начислений:** Проценты начисляются каждый час. Первое начисление происходит через 2 часа после подписки (интервал T+2).
- **Атрибуция PnL:** Начисления отражаются в эндпоинте `GET /api/v5/finance/savings/lending-history` и направляются в рукав `cash_earn` (`ops/sleeves.json`).

#### 4. Протокол мгновенного отзыва средств (On-Demand Instant Redemption)
- **Триггер маржин-колла / новой сделки:** Когда торговая стратегия (Mean Reversion, Grid, Carry) запрашивает открытие позиции и $\text{availBal}_{\text{USDT}} < \text{Required Margin}$:
- **Расчет суммы отзыва:**
  $$\text{Redemption Amt} = \text{Required Margin} - \text{availBal}_{\text{USDT}} + \text{Safety Buffer}$$
- **Команда исполнения:**
  - REST: `POST /api/v5/finance/savings/purchase-redempt` с параметрами `{"ccy": "USDT", "amt": "<RedemptionAmt>", "side": "redempt"}`
  - CLI: `okx earn savings redeem --ccy USDT --amt <RedemptionAmt>`
- **Скорость возврата:** Мгновенно (Instant, 24/7). Средства поступают на баланс торгового счете без задержки. Сумма, отозванная в текущем часе, не получает процент за неполный час, ранее начисленные проценты полностью сохраняются.
- **Защита от риска блокировки:** В редких случаях 100% утилизации пула OKX выкуп может задерживаться до 1 часа. Постоянное удержание 30% кэша на торговом счете гарантирует, что стратегии никогда не столкнутся с отказом исполнения из-за временной задержки выкупа.

---

### 3.4. R4: Расширение каталога флота ботов (Bot Fleet 50 Expansion)

#### 1. Полный перечень 13 типов нативных ботов OKX

| № | Ключ стратегии | Название (RU) | Название (EN) | Категория | Поддерживаемые рынки | Плечо (макс) | Семейство движка | OKX algoOrdType / CLI |
|---|---|---|---|---|---|---|---|---|
| 1 | `spot_grid` | Спотовый grid-бот | Spot Grid Bot | Grid | SPOT | 1x (нет) | `grid` | `grid` / `okx bot grid create --algoOrdType grid` |
| 2 | `contract_grid_usdt` | Фьючерсный grid (USDT) | Futures Grid (USDT) | Grid | SWAP, FUTURES | до 3x | `grid` | `contract_grid` / `okx bot grid create --algoOrdType contract_grid` |
| 3 | `contract_grid_coin` | Фьючерсный grid (Coin-M) | Futures Grid (Coin-M) | Grid | SWAP (Coin-M) | до 3x | `grid` | `contract_grid` / `okx bot grid create --algoOrdType contract_grid` |
| 4 | `smart_portfolio` | Смарт-портфель | Smart Portfolio / Rebalance | Portfolio | SPOT | 1x (нет) | `rebalance` | `rebalance` / `okx bot rebalance create` |
| 5 | `contract_dca` | Фьючерсный DCA-бот | Futures DCA (Martingale) | DCA | SWAP | до 3x | `dca` | `contract_dca` / `okx bot dca create --algoOrdType contract_dca` |
| 6 | `smart_arbitrage` | Смарт-арбитраж TradFi | Smart Arbitrage (Cash & Carry)| Arbitrage | SPOT + SWAP | 1x / дельта-нейтр. | `arbitrage` | `funding_arbitrage` / UI TradFi |
| 7 | `dcd_pendulum` | Маятник (Dual Currency) | Pendulum (Dual Currency) | Structured | SPOT / EARN | 1x (нет) | `dcd` | `dcd` / `okx trading-bot/dcd-bot` |
| 8 | `spot_dca` | Спотовый DCA-бот | Spot DCA (Martingale) | DCA | SPOT | 1x (нет) | `dca` | `spot_dca` / `okx bot dca create --algoOrdType spot_dca` |
| 9 | `recurring_buy` | Повторяющаяся покупка | Recurring Buy (Scheduled DCA) | DCA | SPOT | 1x (нет) | `dca` | `recurring` / `okx bot recurring create` |
| 10 | `signal_bot` | Сигнальный бот | Signal Bot | Signal | SPOT, SWAP | до 3x | `signal` | `signal` / `okx bot signal create` |
| 11 | `iceberg` | Айсберг-бот | Iceberg Algo Order | Execution | SPOT, SWAP | 1x (нет) | `algo_order` | `iceberg` / `okx spot algo place --ordType iceberg` |
| 12 | `twap` | TWAP-Бот | TWAP (Time-Weighted) | Execution | SPOT, SWAP | 1x (нет) | `algo_order` | `twap` / `okx spot algo place --ordType twap` |
| 13 | `arbitrage` | Спредовый арбитраж | Spread / Calendar Arbitrage | Arbitrage | SPOT, SWAP, FUTURES | до 3x | `arbitrage` | `arbitrage` / `okx trade-arbitrage` |

#### 2. Правила аллокации капитала и сайзинга под 50 ботов
- **Квота слотов:** `MAX_ACTIVE_BOTS = 50` (закреплено в `src/risk.py:70` и `src/fleet_manager.py:31`).
- **Резерв ликвидности:** $\text{Reserve} \ge 30\%$ от общего equity. При балансе $\$108\,000$ USDT резерв составляет $\mathbf{\$32\,400}$ USDT.
- **Общий бюджет флота:** $\le 70\%$ от equity ($\mathbf{\$75\,600}$ USDT).
- **Потолок инвестиций на одного бота:** $\text{MAX\_BOT\_INVESTMENT\_PCT} = \mathbf{2.0\%}$ equity ($\le \mathbf{\$2\,160}$ USDT на одного бота).
- **Рекомендуемый базовый размер на бота:**
  $$\text{Alloc}_{\text{bot}} = \min\Big(\text{Equity} \times 0.02, \;\frac{\text{Equity} \times 0.70}{50}\Big) \approx \mathbf{\$1\,400}\text{ USDT}.$$

#### 3. Правила риск-изоляции по рукавам и ORDER-OWNER-TAG
- **Изоляция плеча:** Для всех 5 деривативных типов ботов плечо жестко ограничено:
  $$\text{Leverage} \le \text{MAX\_LEVERAGE} = 3\text{ (только isolated margin)}$$
- **Обязательная защита:** Сетки обязаны содержать `slTriggerPx`; DCA-боты обязаны содержать `slPct`.
- **Маркировка ORDER-OWNER-TAG:**
  - Все нативные боты флота, создаваемые агентами, получают `algoClOrdId` с префиксом `trd` (`order_owner.new_client_order_id("trd")`).
  - Боты, запускаемые автоматическим флотом, используют префикс `flt`.
  - Внутренние программные модули используют корень `bot` + символ вне диапазона `0-9, a-f` (символы `g-z`), например: `botmr` (Mean Reversion), `botsdca` (DCA demo), `botr` (Router).
  - Маппинг в `ops/sleeves.json`:
    - `bot_algo_cl_ord_id_prefixes`: `flt` $\to$ `demo_fleet`, `trd` $\to$ `demo_fleet`.
    - PnL и комиссии всех дочерних ордеров ботов автоматически изолируются в рукав `demo_fleet` и не смешиваются со спотовыми стратегиями.
- **Аудит и реконсиляция флота:**
  - Периодический опрос `tradingBot/grid/orders-algo-pending` и `tradingBot/dca/ongoing-list`.
  - Сверка количества работающих ботов с лимитом 50.
  - Массовая аварийная остановка: `connector.emergency_stop()` останавливает ботов пачками по 10 штук при срабатывании kill-switch.

---

## 4. Сводная таблица параметров и конфигурации компонентов

| Компонент / Модуль | Ключевой параметр / Константа | Значение | Файл конфигурации / Исходник | Назначение |
|---|---|---|---|---|
| R1: Funding Carry | `LEVERAGE` | `1` (1x isolated) | `insights/funding-carry.md` §6 | Нулевой риск ликвидации шорт-ноги |
| R1: Funding Carry | `CAPITAL_EFFICIENCY` | `0.5x` | `insights/funding-carry.md` §6 | Учет удвоенной капиталоемкости рукава |
| R1: Funding Carry | `BREAKEVEN_DAYS_BTC` | `12.7 – 19.0` дней | `insights/funding-carry.md` §5 | Минимальный горизонт удержания позиции |
| R1: Funding Carry | `SLEEVE_NAME` | `funding_carry` | `ops/sleeves.json`, `pnl_ledger.py` | Атрибуция PnL выплат фандинга (bills type 8) |
| R2: Mean Reversion | `TIMEFRAME` | `1H` | `meanrev-strategy-design.md` §1 | Рабочий таймфрейм баров BTC-USDT спот |
| R2: Mean Reversion | `RSI_PERIOD` / `THRESHOLDS` | `14` / `30` (entry) / `70` (exit) | `meanrev-strategy-design.md` §2.1 | Осциллятор перепроданности/перекупленности |
| R2: Mean Reversion | `BB_WINDOW` / `STDS` / `PRICE` | `20` / `2.0` / Typical Price | `meanrev-strategy-design.md` §2.2 | Полосы Боллинджера от $(H+L+C)/3$ |
| R2: Mean Reversion | `STOP_ATR_MULT` | `1.5` | `meanrev-strategy-design.md` §5.1 | Волатильный стоп от ATR(14) момента входа |
| R2: Mean Reversion | `MINIMAL_ROI` | `((0, 2.0), (240, 1.2), (720, 0.6), (1440, 0.0))` | `meanrev.py:38`, `design.md` §6.2 | Каскадный тейк-профит и тайм-стоп на 24ч |
| R2: Mean Reversion | `RISK_PCT` / `MAX_POS_PCT` | `1.0%` / `15.0%` equity | `src/risk.py:43, 46` | Риск на сделку и потолок нотионала позиции |
| R2: Mean Reversion | `ROOKIE_AIRBAG_CAP` | `30.0%` equity | `src/risk.py:327` | Жесткий отказ при превышении нотионала 30% |
| R2: Mean Reversion | `OWNER_PREFIX` | `botmr` | `src/order_owner.py`, `order_router.py` | ORDER-OWNER-TAG метка стратегии |
| R3: Idle Cash Earn | `RESERVE_PCT` | `30.0%` equity | `bot-fleet-50.md` §1, `business-plan.md` | Неприкосновенный резерв свободной ликвидности |
| R3: Idle Cash Earn | `SWEEP_THRESHOLD` | `100.0` USDT | `insights/idle-cash-earn.md` §6 | Минимальная сумма для свиппинга в Simple Earn |
| R3: Idle Cash Earn | `PURCHASE_RATE` | `0.01` (1%) | `references/savings-commands.md` | Минимальная пороговая ставка для 100% матчинга |
| R3: Idle Cash Earn | `REDEMPTION_SPEED` | `instant` (24/7) | `insights/idle-cash-earn.md` §3 | Мгновенный отзыв средств при маржин-коллах |
| R3: Idle Cash Earn | `SLEEVE_NAME` | `cash_earn` | `ops/sleeves.json` | Атрибуция процентного дохода лендинга |
| R4: Bot Fleet 50 | `MAX_ACTIVE_BOTS` | `50` | `src/risk.py:70`, `fleet_manager.py:31` | Максимальный размер флота активных ботов |
| R4: Bot Fleet 50 | `MAX_LEVERAGE` | `3` (3x isolated) | `src/risk.py:66`, `fleet_manager.py:32` | Потолок кредитного плеча контрактных ботов |
| R4: Bot Fleet 50 | `MAX_BOT_INVEST_PCT`| `2.0%` equity | `src/risk.py:71`, `bot-fleet-50.md` §1 | Максимальная аллокация на одного бота |
| R4: Bot Fleet 50 | `BOT_TYPES_COUNT` | `13` | `src/fleet_manager.py:49` | Полный спектр нативных ботов OKX |
| R4: Bot Fleet 50 | `FLEET_OWNER_CODE` | `trd` / `flt` | `src/order_owner.py:56`, `sleeves.json` | Префиксы владельцев algoClOrdId флота |
| R4: Bot Fleet 50 | `SLEEVE_NAME` | `demo_fleet` | `ops/sleeves.json` | Изолированный рукав отчетности флота |
