# Handoff Report — survey_miner_1

- **Дата:** 2026-09-30T07:17:00Z
- **Агент:** survey_miner_1 (teamwork_preview_spec_miner)
- **Получатель:** parent (0392c235-01a5-43cd-9010-ac02cbb6d35c)
- **Тип передачи:** Hard Handoff (задача исследования спецификаций завершена в полном объеме)
- **Основной артефакт:** `c:\TG\BOT\TRADE DEMO 1\.agents\teamwork\survey_miner_1\survey_specs.md`

---

## 1. Observation (Прямые наблюдения)

1. **R1: Funding Carry Арбитраж**
   - Файл: `insights/funding-carry.md:87–96`
     > `Funding rate = Clamp[ Average premium index + Clamp(Interest rate − Average premium index, 0.05%, −0.05%), Cap, Floor ]`
     > `Interest rate = 0.03% / (24 / N)` где N — интервал в часах (8ч $\to$ 0.01%/период).
     > Cap/Floor: BTC $\pm 0.375\%/8\text{ч}$, ETH $\pm 0.75\%/8\text{ч}$.
   - Файл: `insights/funding-carry.md:110–116`
     > 4 ноги round-trip carry (открытие: buy spot + sell short swap; закрытие: sell spot + buy-to-cover swap):
     > Весь taker: $(0.10\% + 0.05\%) \times 2 = \mathbf{0.30\%}$; Смешанный: $\mathbf{0.25\%}$; Весь maker: $(0.08\% + 0.02\%) \times 2 = \mathbf{0.20\%}$.
   - Файл: `insights/funding-carry.md:124–134`
     > `breakeven_дней = round_trip_% / (funding_annualized_% / 365)`.
     > BTC: 12.7–19.0 дней; ETH: 17.6–26.4 дней при бычьем funding.
   - Файл: `insights/funding-carry.md:143–153`
     > `liq / entry = 2 / (1 + mmr + fee) ≈ 1.991` $\implies$ цена базового актива должна вырасти на $\mathbf{+99.1\%}$ для ликвидации 1x isolated шорта.
   - Файл: `insights/funding-carry.md:164–168`
     > Капиталоэффективность: при 1x маржа шорта равна 100% нотионала $\implies$ на $\$1$ капитала рукава доступно только $\$0.5$ экспозиции. Заголовочная доходность вдвое ниже на баланс: $5.75\% \to \mathbf{2.88\%}$.

2. **R2: Сигнальный Mean Reversion**
   - Файл: `insights/meanrev-strategy-design.md:17–24`
     > BTC-USDT спот, 1H бары; RSI(14) Уайлдера; Bollinger Bands(20, 2) на Typical Price $(H+L+C)/3$; ATR(14) с множителем 1.5; Minimal ROI таблица $((0, 2.0), (240, 1.2), (720, 0.6), (1440, 0.0))$; warmup = 200 баров.
   - Файл: `src/backtest/meanrev.py:52–73` и `tests/test_meanrev.py:111–125`
     > `test_guard_unreachable_for_wilder_rsi`: условие `crossed_above(rsi, 70)` и guard `close(t) < close(t-1)` несовместимы для RSI Уайлдера на Close (RSI растет только при росте close). Для выхода по индикатору требуется `exit_price_guard=False`.
   - Файл: `src/risk.py:295–334`
     > Fixed-fractional сайзинг: 1% риска (`DEFAULT_RISK_PCT = 1.0`), обрезка размером до `MAX_POSITION_PCT = 15.0%` equity, и жесткий отказ при нотионале $> 30\%$ equity (rookie-airbag).
   - Файл: `src/order_router.py:228–350`
     > `place_order` проверяет `risk.check_entry_allowed`, выполняет сайзинг, проверяет `account_mode` (запрет займа), прогоняет через throttler (20 запросов / 2с), выставляет ордер с expTime и clOrdId (`botmr`), регистрирует в storage и риск-ядре (`register_entry`, `register_entry_size`).

3. **R3: Idle Cash Earn**
   - Файл: `insights/idle-cash-earn.md:14–20`
     > Simple Earn Flexible живет на счете `earn` (вне `totalEq` торгового счета); Trading Account Auto Earn (Lending) доступен только VIP1–VIP9. Для капитала $\$500–1000$ доступен только Simple Earn Flexible.
   - Файл: `insights/idle-cash-earn.md:28–35`
     > В OKX demo эндпоинты `earn savings balance` и `rate-history` возвращают ошибку `50038: "This feature is unavailable in demo trading"`. Справочные `fixed-products` и публичные `lending-rate-summary` / `lending-rate-history` работают без авторизации.
   - Файл: `skills/okx-cex-earn/references/savings-commands.md:15–53`
     > Команды подписки и выкупа: `earn savings purchase --ccy USDT --amt <AMT> --rate 0.01`, `earn savings redeem --ccy USDT --amt <AMT>`. Выкуп мгновенный (Instant), 24/7.
   - Файл: `skills/okx-cex-earn/references/autoearn-commands.md:35, 59`
     > Аппаратное ограничение API OKX: `earn auto-earn` нельзя отключить в течение 24 часов после включения.

4. **R4: Bot Fleet 50 и 13 типов ботов**
   - Файл: `src/fleet_manager.py:31–32, 49–206`
     > Лимит флота `FLEET_MAX_BOTS = 50`, `MAX_LEVERAGE = 3`. Полный каталог из 13 спецификаций: `spot_grid`, `contract_grid_usdt`, `contract_grid_coin`, `smart_portfolio`, `contract_dca`, `smart_arbitrage`, `dcd_pendulum`, `spot_dca`, `recurring_buy`, `signal_bot`, `iceberg`, `twap`, `arbitrage`.
   - Файл: `src/fleet_manager.py:239–259`
     > Неприкосновенный резерв кэша $\ge 30\%$ equity, бюджет флота $\le 70\%$, максимальная инвестиция на бота $\le 2.0\%$ equity (`MAX_BOT_INVESTMENT_PCT = 2.0`). Базовая аллокация при $\$108\,000$ — $\approx \$1\,400$ USDT на бота.
   - Файл: `src/order_owner.py:124–147`
     > Префиксы владельцев: боты флота через CLI маркируются `trd`, программный флот — `flt` (рукав `demo_fleet` в `ops/sleeves.json`). Внутренние коды обязаны начинаться с `bot` и иметь 4-й символ в диапазоне `g-z` (не hex!).
   - Файл: запуск тестов командой `.venv\Scripts\python.exe -m unittest discover -s tests -t .`
     > Результат: `Ran 836 tests in 76.850s, OK (skipped=1)`. Все существующие 836 тестов проекта полностью зеленые.

---

## 2. Logic Chain (Логическая цепочка выводов)

1. **По R1 (Funding Carry):**
   - Из формулы ликвидации шорта при 1x isolated (`liq/entry = 2 / 1.0045 ≈ 1.991`) следует, что риск ликвидации практически нулевой (рост цены на 99.1%).
   - Однако из механики isolated-маржи следует, что рост цены спота не пополняет маржу шорта автоматически. Следовательно, модуль обязан иметь контур мониторинга `marginRatio` шорта и сигнализировать о необходимости ребалансировки/довноса маржи при сильных ралли.
   - Из затрат на 4 ноги ($0.20\% - 0.30\%$) и ставки фандинга ($4.1\% - 5.75\%$) следует, что стратегия требует удержания не менее 13–26 дней для окупаемости, а 7-дневный демо-пробег проверяет исключительно бессбойность механики парных ордеров, но не финансовую прибыль.
   - Из требования 100% маржи на шорт при 1x следует, что общая капиталоемкость удваивается ($0.5x$ yield), поэтому в бизнес-плане и сайзинге нужно закладывать чистую доходность $\approx 2.5–3.0\%$ годовых на весь депозит рукава.

2. **По R2 (Mean Reversion):**
   - На барах 1H спота BTC-USDT комбинация RSI(14) и BB(20, 2 Typical Price) генерирует качественные сигналы разворота на перепроданности.
   - Реализация в `tests/test_meanrev.py:111` доказала, что guard выхода `close(t) < close(t-1)` при росте RSI выше 70 невыполним математически. Следовательно, спецификация исполнения обязана устанавливать `exit_price_guard=False`.
   - Приоритет выходов строго детерминирован: стоп-лосс ($1.5 \times \text{ATR}$) $\to$ Minimal ROI $\to$ Time-stop (1440 мин / 24 бара) $\to$ индикаторный выход.
   - Интеграция со спотовым `OrderRouter` полностью гарантирует соблюдение ограничений: режим счета без скрытого займа (`check_no_borrow`), проверка лимитов через `risk.check_entry_allowed`, сайзинг с капом 15% через `risk.size_position` и тегирование `clOrdId` с префиксом `botmr`.

3. **По R3 (Idle Cash Earn):**
   - Свободный остаток вычисляется за вычетом 30% неприкосновенного резерва и занятой маржи.
   - Свиппинг запускается при превышении порога (напр. 100 USDT) через `earn savings purchase`.
   - Ввиду ошибки `50038` на demo OKX для приватных эндпоинтов Simple Earn, модуль в демо-режиме должен работать через мок/виртуальный леджер свободных средств, а для чтения рыночных ставок использовать реальные публичные эндпоинты `lending-rate-history` / `summary`.
   - Функция мгновенного выкупа (Instant Redemption) через `earn savings redeem` обеспечивает возврат ликвидности при маржин-коллах или запросах ордеров торговыми стратегиями.

4. **По R4 (Bot Fleet 50):**
   - Каталог `src/fleet_manager.py` содержит полные спецификации 13 типов нативных ботов OKX.
   - Квота 50 ботов контролируется на уровне `validate_fleet_quota` и `src/risk.py:70`.
   - Плечо для деривативных ботов жестко зажато на уровне $\le 3x$ (`MAX_LEVERAGE = 3`), стоп-лоссы строго обязательны.
   - Атрибуция ордеров ботов в рукав `demo_fleet` по префиксам `trd` и `flt` в `ops/sleeves.json` изолирует PnL флота от дискретных спотовых стратегий.

---

## 3. Caveats (Ограничения и допущения)

1. **OKX Demo Earn (Ошибка 50038):**
   Приватный баланс Simple Earn и история начислений недоступны в demo. Для демонстрации казначейского модуля в demo необходим эмулятор/логгер распределения свободных средств, опрашивающий реальные публичные ставки.
2. **OKX Demo Funding Rates:**
   Ставки финансирования в demo рассчитываются по симулированному стакану и отличаются от live. Демо-тестирование проверяет корректность выставления парных ордеров, отслеживания позиций и записи в `pnl_ledger`, но не экономический результат.
3. **Режим аккаунта для SWAP:**
   На demo-аккаунте для открытия деривативов требуется `acctLv >= 2`. На `acctLv = 1` биржа отклоняет ордера на свопы.
4. **Неизменность исходного кода:**
   В роли Specification Miner исходный код не модифицировался. Все тесты и файлы оставлены в исходном стабильном состоянии.

---

## 4. Conclusion (Итоговое заключение)

Технические спецификации для всех четырех ключевых направлений (R1: Funding Carry, R2: Mean Reversion, R3: Idle Cash Earn, R4: Bot Fleet 50) полностью исследованы, математически выверены и задокументированы в `survey_specs.md`:
- Все формулы (фандинг, ликвидация 1x, комиссии 4 ног, окупаемость, RSI, BB typical price, волатильный стоп, minimal ROI, свободный кэш, сайзинг флота ботов) детально расписаны.
- Граничные случаи (недостижимость price-guard в Mean Reversion, специфика ошибки 50038 в Demo Earn, асимметрия изолированной маржи шорта в Carry, кап 15% equity и 30% rookie-airbag) зафиксированы с практическими рекомендациями.
- Каталог 13 типов ботов, квота 50 активных ботов и маппинг рукавов в `ops/sleeves.json` синхронизированы.

Команда реализации (`Insight Executor`, `Meta Muse Code`) может приступать к имплементации модулей без необходимости повторного исследования контрактов и API.

---

## 5. Verification Method (Метод независимой верификации)

1. **Проверка файла спецификаций:**
   Убедиться в наличии и полноте таблиц Features Discovered (33 фичи) и Edge Cases (15 кейсов) в `c:\TG\BOT\TRADE DEMO 1\.agents\teamwork\survey_miner_1\survey_specs.md`.
2. **Проверка тестового сьюта:**
   Выполнить команду полного прогона тестов:
   ```powershell
   .venv\Scripts\python.exe -m unittest discover -s tests -t .
   ```
   Ожидаемый результат: 836 тестов успешно пройдены (OK, skipped=1), 0 ошибок, 0 провалов.
3. **Проверка ключевых модулей рисков и флота:**
   - `python -c "import src.risk as r; print(r.MAX_ACTIVE_BOTS, r.MAX_LEVERAGE)"` $\implies$ `50 3`
   - `python -c "import src.fleet_manager as f; print(len(f.BOT_CATALOG))"` $\implies$ `13`
   - `python -c "import src.order_owner as o; print(o.validate_registry())"` $\implies$ `[]`
