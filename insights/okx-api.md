# OKX API v5 — исследование первой целевой биржи

- **Дата:** 2026-09-23
- **Тема:** OKX API v5 как первая целевая биржа для полной автоматизации торговли
- **Статус:** validated (ключевые факты подтверждены ≥2 независимыми источниками; отдельные пункты помечены «требует проверки»)
- **Решение, которое принимается:** выбор первой биржи и клиентского стека (CCXT vs нативный SDK), выбор рынка (SPOT/SWAP), подход к paper trading

> Примечание по источникам: сайт okx.com/docs-v5 — SPA и плохо извлекается автоматически. Основным первоисточником послужило **официальное markdown-зеркало документации в GitHub-организации OKX** (`okx/ai-builder-openapi-md`, `okx/builder-integration-demo`) — это те же тексты, что на сайте. Вторичная верификация — код Hummingbot/Freqtrade/CCXT.

---

## 1. Аутентификация и аккаунт

**Подпись запросов (REST):**
- Обязательные заголовки приватных запросов: `OK-ACCESS-KEY`, `OK-ACCESS-SIGN`, `OK-ACCESS-TIMESTAMP`, `OK-ACCESS-PASSPHRASE` (+ `Content-Type: application/json`).
- Подпись: `sign = Base64(HMAC_SHA256(secret, timestamp + METHOD + requestPath + body))`.
  - `timestamp` — ISO 8601 UTC с миллисекундами, напр. `2020-12-08T09:08:57.715Z`; тот же timestamp идёт в prehash и в заголовок.
  - METHOD — в верхнем регистре. `requestPath` включает query-строку для GET (`/api/v5/account/balance?ccy=BTC`). GET: body = пустая строка. Тело POST подписывается байт-в-байт как отправляется (нельзя давать HTTP-клиенту пересобирать JSON после подписи).
- Расхождение timestamp с серверным временем более 30 секунд → отказ. **Карта кодов подтверждена эмпирически на demo API 2026-09-24** (подписанный `GET /api/v5/account/balance` с испорченным timestamp; только чтение, ордеров не было):
  - **50102** `Timestamp request expired` — timestamp просрочен (проверены оба знака: −10 мин и +10 мин от серверного времени);
  - **50112** `Invalid OK-ACCESS-TIMESTAMP` — невалидный формат timestamp;
  - **50107** `Request header OK-ACCESS-TIMESTAMP can not be empty.` — заголовок отсутствует.
- Серверное время: `GET /api/v5/public/time` — синхронизировать перед торговлей.
- Права ключей: `Read` / `Trade` / `Withdraw` (вывод — отдельным правом, боту НЕ нужен). Passphrase не восстанавливается — хранится salted hash.
- **Безопасность:** к ключу можно привязать до 20 IP (IPv4/IPv6, подсети). Ключи с правами trade/withdraw **без IP-привязки истекают через 14 дней неактивности** (ключи demo-торговли не истекают). «Использованием» считается только вызов аутентифицированных REST или `login` по WS — подписки/ордера по уже залогиненному WS не продлевают жизнь ключа!
- **Аккаунт-режимы:** Spot (Simple), Futures (Single-currency margin), Multi-currency margin, Portfolio margin. Первая установка — только через Web/App. Hummingbot perp-коннектор требует именно **Single-currency margin mode**. Ошибка **51010** — «нельзя выполнить запрос в текущем режиме аккаунта».
- **Суб-аккаунты:** поддерживаются API; rate limit ордеров считается на уровне суб-аккаунта (1000 ордер-запросов/2с, ошибка **50061**); для масштабирования частоты ордеров OKX официально рекомендует несколько суб-аккаунтов.
- **Регионы:** Global = `www.okx.com` / `openapi.okx.com`; EEA = `eea.okx.com` (регистрация my.okx.com); US/AU = `us.okx.com`. Ключи между сущностями не работают — неверный домен даёт **50119 «API key doesn't exist»**. В CCXT это отдельные id: `okx`, `myokx`, `okxus`.

**Источники:**
- https://github.com/okx/ai-builder-openapi-md → skills/okx-v5-api/docs/okex/en/introduction.md (Overview: API key, REST auth, Account mode, regional domains)
- https://github.com/okx/builder-integration-demo/blob/master/docs/OPENAPI_SIGNING.md (правила подписи + known-answer векторы)
- https://www.freqtrade.io/en/stable/exchanges/#okx (50119, passphrase)
- https://hummingbot.org/exchanges/okx/ (Single-currency margin mode)

---

## 2. Demo Trading (тестовый режим)

- **Это НЕ отдельный хост и не мок.** REST — тот же `https://openapi.okx.com` (или `www.okx.com`), переключение — заголовок **`x-simulated-trading: 1`**. В live этот заголовок отправлять нельзя.
- WS для демо — **отдельный хост**: `wss://wspap.okx.com:8443/ws/v5/public|private|business` (в интеграциях Hummingbot добавляется `?brokerId=9999`).
- Ключи демо создаются отдельно: Login → Trade → Demo Trading → Personal Center → Demo Trading API → Create Demo Trading API Key. Демо- и live-ключи разделять.
- **Ограничения демо:** не поддерживаются withdraw, deposit, purchase/redemption и ряд других функций. Демо-ключи не истекают по неактивности (в отличие от live без IP-привязки).
- Практический нюанс из `okx/agent-trade-kit` CHANGELOG: если клиент запущен в demo-режиме глобально, то и рыночные данные по умолчанию приходят из **симулированного** стакана; для получения реальных котировок при демо-торговле надо явно слать публичные market-запросы без заголовка. Важно для бэктестинга/сигналов: решить, какие данные считать «истиной».
- В CCXT: `exchange.set_sandbox_mode(True)` выставляет заголовок; в python-okx — `flag="1"` в конструкторе каждого клиента.

**Источники:**
- https://github.com/okx/ai-builder-openapi-md → introduction.md (Demo Trading Services)
- https://github.com/okx/builder-integration-demo → demos/oauth-user/PITFALLS.md (demo/live ключи раздельно, заголовок только для демо)
- https://github.com/ccxt/ccxt/wiki/comparisons/ccxt-vs-okx-api.md (set_sandbox_mode)
- https://github.com/okx/agent-trade-kit → CHANGELOG.md, docs/site-compatibility.md

---

## 3. REST API: ключевые эндпоинты и rate limits

**Базовые эндпоинты (все под `/api/v5/`):**
| Задача | Эндпоинт |
|---|---|
| Серверное время | `GET /public/time` |
| Инструменты (tickSz, lotSz, minSz, ctVal, state) | `GET /public/instruments?instType=SPOT|SWAP|FUTURES` |
| Тикеры / стакан | `GET /market/tickers`, `GET /market/books` |
| Свечи | `GET /market/candles` (+ `/market/history-candles`) |
| Баланс | `GET /account/balance` |
| Позиции | `GET /account/positions` |
| Конфиг аккаунта (режим, posMode) | `GET /account/config` |
| Поставить ордер | `POST /trade/order` (batch: `POST /trade/batch-orders`) |
| Отменить | `POST /trade/cancel-order` (batch: `POST /trade/cancel-batch-orders`) |
| Активные / история ордеров | `GET /trade/orders-pending`, `GET /trade/orders-history` |
| Сделки (fills) | `GET /trade/fills` (+ `/trade/fills-history`) |
| Текущий rate limit аккаунта | `GET /trade/account-rate-limit` |
| Algo-ордера | `POST /trade/order-algo` (TP/SL, trigger, trailing, iceberg, TWAP) |

**Rate limits — модель:**
- Превышение → ошибка **50011**; лимит разный у каждого эндпоинта и указан в его доке.
- Публичные REST — по IP; приватные REST — по User ID (у суб-аккаунтов свой UID); WS login/subscribe — по соединению.
- Ордерные лимиты (place/amend/cancel) **независимы друг от друга**, считаются **на уровне instId** (для опционов — instFamily) и **общие для REST и WS**.
- Суб-аккаунт-лимит: 1000 ордер-запросов/2с суммарно по place/amend (batch считается поштучно) → ошибка **50061**. Для VIP5+ есть fill-ratio-тиеры до 10 000/2с.
- Числа из констант Hummingbot (проверены против доков, актуальность на дату исследования):
  - place order: 60/2с (perp-коннектор) / 20/2с (spot-коннектор, на инструмент)
  - cancel: 60/2с; batch cancel: 300/2с
  - balance: 10/2с; positions: 10/2с; account/config: 5/2с
  - fills: 60/2с (spot), 120/60с (perp)
  - instruments: 20/2с; tickers: 20/2с; books: 20–40/2с; server time: 10/2с
- CCXT для OKX: встроенный token-bucket throttler включён по умолчанию (`rateLimit` ≈ 110 мс), веса эндпоинтов закодированы в коннекторе.
- Вместо классического `recvWindow` OKX предлагает заголовок/параметр **`expTime`** (unix ms) — дедлайн, после которого запрос place/amend отбрасывается сервером. Полезно для защиты от «застрявших» ордеров.

**Rate limits — замерено на demo 2026-09-24** (P1-RATELIMIT, `src/ratelimit_test.py`, параллельный шторм 12–24 потока без throttler CCXT; логи `logs/ratelimit_*_2026-09-24.log`):
- `POST /trade/order` (spot BTC-USDT, лимит по UserID+instId): первый отказ **50011** после **~115–117 ордер-запросов за <2 с** (прогон 1: 116 запросов за 1470 мс до отказа; прогон 2: 117 за 1924 мс) — при документированных 60/2с. Поведение похоже на token bucket с бакетом ~60 и пополнением ~30/с: при продолжении обстрела отказы идут рывками (58 отказов за ~1.5 с), пиковая пропускная способность ~189 запросов/2 с; одиночный запрос сразу после отказа часто проходит.
- Отказ 50011 — это HTTP 429 с телом `{"code":"50011","msg":"Too Many Requests"}`; заголовков `Retry-After` / `x-ratelimit-*` биржа **не присылает** (проверено на обоих эндпоинтах) — интервал паузы подбирать самим (экспоненциальный backoff 0.5→8 с подтверждён вживую: ретрай через 0.5 с после 50011 — успех).
- Backoff-стратегия для ордеров: пауза 0.5 с достаточна при одиночном ретрае; под постоянной нагрузкой окно остаётся горячим — нужен экспоненциальный ряд с капом (у нас 0.5→8 с, макс 6 ретраев).
- `POST /trade/batch-orders` на demo считается **по запросам, а не по ордерам** внутри батча: 5 батчей × 20 ордеров за 1636 мс (04:57) — без единого отказа. Т.е. эндпоинт-лимит ~300 запросов/2с последовательным клиентом (~3–4 запроса/с) недостижим физически; batch — эффективный способ обойти per-order лимит place.
- Публичный `GET /market/ticker` (лимит по IP): 50011 после **~20–30 запросов/2с** — соответствует документированным 20/2с.
- **50061** (суб-аккаунт, 1000 ордер-запросов/2с) намеренно не провоцировался: потребовал бы ≥1000 ордеров (~6000 USDT заморозки) параллельно с работающим движком — риск не оправдан, семантика задокументирована по докам.

**Источники:**
- https://github.com/okx/ai-builder-openapi-md → introduction.md (Rate Limits), api/rest/trade/* 
- https://github.com/hummingbot/hummingbot → connector/exchange/okx/okx_constants.py, connector/derivative/okx_perpetual/okx_perpetual_constants.py
- https://github.com/ccxt/ccxt/wiki/comparisons/ccxt-vs-okx-api.md

---

## 4. WebSocket

**Адреса:**
- Public: `wss://ws.okx.com:8443/ws/v5/public`; Private: `wss://ws.okx.com:8443/ws/v5/private`; Business: `wss://ws.okx.com:8443/ws/v5/business` (для ордерных WS-операций; в SDK okx-api маршрутизация private/business автоматическая). AWS-зеркала: `wsaws.okx.com`. Демо: `wspap.okx.com`.

**Правила соединения:**
- Connection limit: **3 новых соединения/сек на IP**.
- `subscribe`/`unsubscribe`/`login`: суммарно **480 запросов/час на соединение**.
- Если нет входящих сообщений >30 сек — соединение рвётся. Heartbeat: таймер N<30с; по срабатыванию шлём строку `ping`, ждём `pong`; нет pong → reconnect (Hummingbot ждёт 25с/24с).
- За 60 секунд до планового разрыва (апгрейд) приходит событие `notice` с кодом **64008** → переподключаться заранее.
- Лимит **30 соединений на канал на суб-аккаунт** для приватных каналов (orders, account, positions, balance_and_position и др.); превышение → событие `channel-conn-count-error`, текущий счёт приходит в `channel-conn-count`.
- Login: `{"op":"login","args":[{apiKey, passphrase, timestamp (unix сек), sign}]}`, где `sign = Base64(HMAC_SHA256(secret, timestamp+'GET'+'/users/self/verify'))`; запрос протухает через 30 сек. Ошибка логина — **60009**. До 100 аккаунтов на одно private-соединение.
- Суммарная длина args при подписке ≤ 64 КБ. Ошибки подписки: 60012 (illegal request), 60018 (channel/instId не существует).

**Каналы для автоматизации:**
- Публичные: `tickers`, `books` (400 уровней, 100мс, snapshot+delta+checksum), `books5`, `bbo-tbt`, `trades`/`trades-all`, `candles*`, `mark-price`, `index-tickers`, `funding-rate`, `instruments`.
- Приватные: `orders` (состояния ордеров), `positions`, `account`, `balance_and_position`.
- **Критично для стакана:** OKX публикует checksum — локальная книга обязана применять дельты по порядку и сверять checksum, иначе «тихий» дрейф книги без ошибок (в CCXT это сделано; при сыром коннекторе — ваша зона ответственности).
- Ордера можно ставить прямо по WS (op `order`, batch, amend, cancel) — rate limit общий с REST; поддержан `expTime`.

**Источники:**
- https://github.com/okx/ai-builder-openapi-md → introduction.md (WebSocket: Connect/Login/Subscribe/Notification)
- https://github.com/tiagosiebler/okx-api (WS API, авто-реконнект/ресабскрайб, 100 аккаунтов)
- https://github.com/ccxt/ccxt/wiki/comparisons/ccxt-vs-okx-api.md (checksum, reconnect, 19 watch*-методов)

---

## 5. Типы ордеров и особенности исполнения

- `ordType`: `market`, `limit`, `post_only`, `fok`, `ioc`, `optimal_limit_ioc` (market-IOC по границе ценового лимита, только SWAP/FUTURES), `rpi` (Retail Price Improvement), `mmp`/`mmp_and_post_only` (опционы, portfolio margin). `elp` deprecated (принимается до 31.10.2026).
- `tdMode`: `cash` (спот), `cross`/`isolated` (маржа/деривативы), `spot_isolated` (только копитрейдинг).
- `posSide`: `net` по умолчанию в net-режиме; в long/short (hedge) режиме **обязателен** (`long`/`short`), иначе ошибка **51000**; для SPOT/MARGIN поле слать нельзя. Position mode аккаунта: `net_mode` или `long_short_mode` (`POST /account/set-position-mode`); **боты не должны менять режим при открытых позициях** (Freqtrade: смена mid-trading = исключения и сбои ордеров).
- `sz`: для деривативов — в контрактах (ctVal/ctMult из instruments); для спота- market buy можно задавать в валюте котировки через `tgtCcy=quote_ccy`. `px` обязателен для limit/post_only/fok/ioc. `clOrdId` — до 32 символов (алфавитно-цифровой).
- `reduceOnly`: только уменьшение позиции (деривативы). Прикреплённые TP/SL — через algo-ордера или параметры attachAlgoOrds.
- **Лимиты отложенных ордеров:** всего 4000; на символ 500; algo: TP/SL 100/инструмент, trigger 500, trailing 50, iceberg 100, TWAP 20.
- **Правило 1000 мейкеров:** если тейкер-ордер матчится больше чем с 1000 мейкер-ордерами, остаток отменяется (FOK отменяется целиком) — важно для оценки проскальзывания крупных market-ордеров.
- Частичное исполнение: статус `partially_filled`; результат ордера в ответе смотреть по **`sCode`/`sMsg` внутри `data[]`**, а не только по верхнеуровневому `code` (типичные sCode: **51008** — недостаточно средств, **51010** — не тот режим аккаунта). Отмена несуществующего ордера: **51603**; уже отменён: **51401**.
- `instId`: SPOT `BTC-USDT`; perpetual SWAP `BTC-USDT-SWAP`; expiry FUTURES `BTC-USDT-250808`; coin-margined `BTC-USD-SWAP`; новые USDⓈ-контракты — instFamily вида `BTC-USD_UM`. Параметры `uly` (индекс) и `instFamily` (семейство, 1:1 с settleCcy) — см. таблицу в Overview доков.

**Источники:**
- https://github.com/okx/ai-builder-openapi-md → api/rest/trade/placeOrder.md, batchOrders.md, introduction.md (General Info)
- https://www.freqtrade.io/en/stable/exchanges/#okx (position mode)
- https://github.com/freqtrade/freqtrade → freqtrade/exchange/okx.py (`_get_posSide`, tdMode cash/isolated)

---

## 6. SPOT vs SWAP vs FUTURES — что брать для старта

| Критерий | SPOT | SWAP (perpetual) | FUTURES (expiry) |
|---|---|---|---|
| Плечо/ликвидации | нет | есть, нужен риск-модуль | есть + даты экспирации |
| Шорт | нет (только через маржу) | да | да |
| Данные для бэктеста | свечи без ограничений по типу | MARK-свечи только ~3 мес. истории (funding-поправки!) | как у SWAP + роллирование контрактов |
| Сложность API | минимальная (`tdMode=cash`) | +posSide, leverage, funding | +экспирации |

**Рекомендация (для v1 автоматизации):** начать со **SPOT** (проще модель состояния, нет ликвидаций, полная история свечей), затем добавить **USDT-margined SWAP** как второй рынок (шорт + плечо, самый ликвидный сегмент). FUTURES — позднее.

---

## 7. Поддержка в CCXT и ботах

- **CCXT:** коннектор зрелый и «глубокий»: 446 сырых эндпоинтов как implicit-методы, 125 унифицированных возможностей, 19 `watch*` методов + ордерные операции по WS, `set_sandbox_mode(True)` для демо, маппинг ошибок в типизированные исключения, throttler по весам эндпоинтов (110 мс), авто-checksum/ресеed стакана, региональные id (`okx`/`myokx`/`okxus`). 4.8M установок PyPI/мес. **Подводный камень:** унифицированные методы могут отставать от новых фич OKX (закрывается implicit API).
- **python-okx** (okxapi): «неофициальный», но именно на него ссылаются официальные доки; 15 доменных модулей, `flag="1"` для демо, ~858 звёзд, 94k PyPI/мес.
- **okx-api** (tiagosiebler/sieblyio, Node/TS): зрелый, 100+ e2e-тестов, WS API клиент, все регионы.
- **Freqtrade:** OKX официально поддержан (spot market/limit; futures isolated; stoploss-on-exchange limit+market). Нюансы: passphrase передаётся как `password` в конфиге; OHLCV-лимит **100 свечей/запрос** (в коде — 300 для spot/futures recent, 100 для mark/funding/истории); MARK-свечи ~3 мес. истории → funding в бэктесте раньше этого срока неточен; position mode не менять на ходу; `ws_enabled: true`; `trades_has_history: false` (эндпоинт fills без `since`); баланс включает нереализованный PnL.
- **Hummingbot:** `okx` (spot: LIMIT, LIMIT_MAKER; paper-trade `okx_paper_trade`) и `okx_perpetual` (LIMIT, MARKET). Требования: Single-currency margin mode; при рестарте с открытыми позициями — сначала закрыть (position mode выставляется при старте). OKX — партнёр Hummingbot Foundation, коннектор активно поддерживается.

**Источники:**
- https://github.com/ccxt/ccxt/wiki/comparisons/ccxt-vs-okx-api.md
- https://github.com/okxapi/python-okx (README)
- https://github.com/tiagosiebler/okx-api (README)
- https://www.freqtrade.io/en/stable/exchanges/#okx
- https://github.com/freqtrade/freqtrade → freqtrade/exchange/okx.py
- https://hummingbot.org/exchanges/okx/

---

## 8. Подводные камни (чек-лист для архитектуры бота)

1. **Timestamp-дрейф** → 50102/50112. Решение: NTP-синхронизация + периодический `GET /public/time`; единый timestamp для prehash и заголовка.
2. **Подпись:** GET query-строка подписывается в составе requestPath; POST body — ровно та строка, что уходит по сети. Тестировать на known-answer векторах из `okx/builder-integration-demo/docs/OPENAPI_SIGNING.md`.
3. **`recvWindow` как у Binance нет** — вместо него `expTime` (дедлайн запроса) для place/amend.
4. **Двухуровневые ошибки:** верхний `code` И внутренний `sCode`/`sMsg` на каждый ордер в `data[]` — обрабатывать оба (особенно batch).
5. **WS обрывы:** >30 сек тишины = разрыв; heartbeat `ping`/`pong` <30с; событие 64008 — плановый разрыв через 60с; при reconnect — login заново, ресеnd подписок, ресинк состояния ордеров через REST.
6. **Дрейф стакана:** snapshot → дельты по порядку → checksum; на несовпадении — полный ресеed.
7. **Rate limit:** 50011 (эндпоинт), 50061 (суб-аккаунт); лимиты ордерные раздельные по instId, но общие REST+WS. Backoff + учёт весов; для HFT-паттернов — несколько суб-аккаунтов.
8. **Региональный домен:** ключ от my.okx.com на www.okx.com даёт 50119. Домен — часть конфигурации аккаунта.
9. **Демо ≠ бой:** в демо нет withdraw/deposit; убедиться, что нужные эндпоинты поддержаны; market-data в демо-режиме может быть симулированной — для сигналов использовать боевые публичные данные без заголовка.
10. **IP-ограничения:** live-ключи с trade без IP-привязки протухают за 14 дней неактивности; WS-активность (кроме login) «использованием» не считается → регулярный приватный REST-вызов или привязка IP обязательны.
11. **Правило 1000 мейкеров:** крупные тейкер-ордера частично отменяются — закладывать в расчёт проскальзывания.
12. **Позиционный режим:** фиксировать `net_mode` ИЛИ `long_short_mode` до старта бота; смена при открытых позициях ломает торговлю (Freqtrade/Hummingbot единогласно).

---

## 9. Применимость к проекту

| Идея | Ценность | Сложность | Риск |
|---|---|---|---|
| Взять **CCXT** как первый коннектор (okx + sandbox-флаг + типизированные ошибки + throttler) | высокая | низкая | низкий |
| Первый рынок — **SPOT**, второй — USDT-SWAP | высокая | низкая | низкий |
| Demo Trading как этап перед боем (заголовок, не отдельная инфраструктура) | высокая | низкая | средний (не все фичи поддержаны, симулированная ликвидность) |
| WS для рыночных данных + приватных каналов (orders/positions/account), REST для reconciliaция | высокая | средняя | средний (reconnect/checksum-логика) |
| `expTime` на все ордерные запросы | средняя | низкая | низкий |
| IP-whitelist ключей + права только read+trade (без withdraw) | высокая | низкая | низкий |
| Отдельный суб-аккаунт под бота | средняя | низкая | низкий |
| Нативный python-okx/okx-api вместо CCXT | средняя (точное совпадение с доками) | средняя | средний (своя обработка rate limit/ошибок) |

## 10. Подтверждено экспериментально на demo (2026-09-24)

Факты ниже получены прямыми вызовами demo API (заголовок `x-simulated-trading: 1`, только чтение либо уже задокументированные прогоны движка) и весомее статей:

1. **Карта ошибок timestamp**: 50102 (просрочен, оба знака смещения) / 50112 (невалидный формат) / 50107 (заголовок отсутствует) — см. §1.
2. **`GET /api/v5/trade/account-rate-limit` в демо работает, но поля пустые** (`accRateLimit`, `fillRatio`, `mainFillRatio`, `nextAccRateLimit` = `""`). Причина: эндпоинт отражает fill-ratio-тиеры, применимые только к VIP5+; для остальных действует дефолтный Tier 1 = 1000 ордер-запросов/2с на суб-аккаунт (introduction.md, «Fill ratio based sub-account rate limit»).
3. **TradingBot API доступен в демо:** `GET /tradingBot/grid/orders-algo-pending` (200, виден работающий grid-бот), `orders-algo-history`, `recurring/orders-algo-pending` — все 200/code 0. Создание grid-бота в демо подтверждено запуском [grid-bot-demo-run.md](grid-bot-demo-run.md).
4. **`expTime` — HTTP-заголовок**, в теле запроса биржа его игнорирует; просроченный дедлайн → `sCode 50036` ([phase1-progress.md](phase1-progress.md)).
5. **checksum=0 на канале `books` и в демо, и в live** — единственная рабочая проверка целостности стакана: цепочка `seqId`/`prevSeqId` ([phase1-progress.md](phase1-progress.md)).
6. **Каналы `candle*` живут на business-эндпоинте** (`/ws/v5/business`, логин не нужен); на public-эндпоинте отклоняются с ошибкой 60018 ([phase1-progress.md](phase1-progress.md)).
7. **`POST /tradingBot/grid/min-investment` — только POST**; GET-вариант возвращает `code 3 "Operation not supported"` ([grid-bot-demo-run.md](grid-bot-demo-run.md) §4).
8. **В демо-ленте бывают выбросы объёма ×1000** (напр. HBAR: бар 508 млн при норме ~0.5 млн) — при расчёте vol_ratio использовать медиану N баров, а не среднее ([pump-scan-2026-09-24.md](pump-scan-2026-09-24.md), скан №2).
9. **`GET /trade/order` (CCXT `spot get`) при нескольких fills возвращает `fillSz` только части объёма** (пример: 0.00391886 вместо 0.00591887), при этом `avgPx` корректен — сверку исполненного объёма делать через `fills` ([demo-slippage.md](demo-slippage.md) §7).
10. **Средства нативного spot grid-бота остаются в торговом счёте как `frozenBal` и входят в `eq`/`totalEq`** (EQUITY-TOTAL, 08:12). Бот `3949248990629228544` (investment 400):
    - `curBaseSz` 0.002325518574 BTC = `frozenBal` BTC торгового счёта;
    - `curQuoteSz` 203.6517 USDT = `frozenBal` USDT;
    - у обеих валют `eq = cashBal + frozenBal`, `ordFrozen` = 0 — ордера бота не считаются ордерами аккаунта.

    Следствие: запуск и остановка spot grid не меняют `totalEq`, ложного breaker'а нет, отдельный учёт ботов в equity не нужен. Контрактный grid не проверялся.
11. **`GET /asset/asset-valuation?ccy=USDT`** — `details`: `classic`, `earn`, `funding`, `trading`. Отдельного поля для ботов нет: их средства внутри `trading` (п. 10). Earn — отдельный счёт `earn`, его `account/balance totalEq` не видит.
    - Live-runner берёт equity из `asset-valuation` (`src/live_preflight.py::fetch_pocket_equity`), поэтому перевод в Earn для него не просадка.
    - Движку в live нужно то же самое (ENGINE-LIVE-MODE).
    - Purchase/redemption Earn в demo недоступны (п. 3 открытых вопросов) — поведение Earn проверяется только в live.
12. **`GET /account/config` у demo-ключа проекта:**
    - `perm = read_only,withdraw,trade` — право withdraw есть, вопреки отметке «без withdraw» в roadmap Фаза 0;
    - `uid == mainUid` — главный аккаунт;
    - `ip` пуст;
    - `acctLv = 1` (spot), `posMode = net_mode`, `roleType = 0`.

    Поля `perm`, `uid`, `mainUid`, `ip` проверяет `src/live_preflight.py`.
13. **Создание ботов на части пар в demo → `55301`** «Trading is currently unavailable… complete the disclaimer on the TradFi Spot trading page» (FLEET-DEMO, 24.09 08:46–08:47):
    - отказ: XRP-USDT (`bot grid create`), AVAX-USDT (`bot dca create`);
    - успех на тех же командах: ETH, SOL, SUI, ADA, TRX, ETC, APT, BNB, XLM, DOT;
    - снимается только дисклеймером в UI (действие человека). Доступность пары под ботов проверять до расчёта параметров.
14. **`contract_grid` при `acctLv=1` → `51057`** «This bot isn't available in current account mode». Контрактные боты требуют режима с деривативами. При этом `bot grid liquidate-price` в том же режиме работает.
15. **`spot_dca` сразу резервирует средства под все страховочные ордера.** Резерв лежит в `frozenBal` USDT торгового счёта (входит в `eq`), `ordFrozen=0`. Сверка: `frozenBal` − Σ`curQuoteSz` гридов = Σ SO трёх DCA (2 035.34) до цента.
    - `slPx = initPx × (1 − slPct)`, `tpPx = avgPx × (1 + tpPct)` — до исполнения SO. Пересчитывается ли `slPx` после SO — открыто.
    - `bot dca orders` возвращает `allowReinvest=false`, если флаг не передан (скилл пишет «default true»).
    - `slMode` в ответе пустой, хотя передан `market`.
16. **Spot grid `details`:**
    - `tpslTriggerParam.stopType=1` — при срабатывании `slTriggerPx` база продаётся;
    - `activeOrdNum = gridNum`;
    - `perMinProfitRate`/`perMaxProfitRate` — чистая прибыль на сетку после maker-комиссий (≈ шаг − 0.16%);
    - CLI сам ставит `tag=CLI`;
    - худший убыток при SL считается из ответа: `investment − gridNum × singleAmt × slTriggerPx` ([bot-fleet-demo.md](bot-fleet-demo.md) §4.1).
17. **Кросс-пары X-BTC/X-ETH в demo почти все мёртвые** (FLEET-EXPAND, 24.09 08:57):
    - из 104 пар двусторонний стакан со спредом < 1% только у трёх: ETH-BTC (0.03%, но 80 из 99 часовых свечей без сделок), OKB-BTC (0.30%, живая) и SOL-ETH (цена +289% к кроссу, бидов на $14);
    - у остальных пустой или односторонний стакан, цены оторваны от кросса X-USDT/BTC-USDT на ±40…10⁷%;
    - SOL-BTC в demo нет.

    Следствие: в demo сверять цену кросс-пары с кроссом через USDT и смотреть долю пустых свечей до расчёта параметров. Кросс-пары demo не годятся для масштабирования флота.
18. **`spot_dca` на паре с котировкой в BTC работает** (OKB-BTC, `3950026152344997888`):
    - `initOrdAmt`/`safetyOrdAmt` задаются в BTC, `investmentCcy=BTC`, `tradeQuoteCcy=BTC`;
    - резерв под SO лежит в BTC `frozenBal`, как и в USDT (п. 15).
19. **DCA-боты: список и остановка** (FLEET-KILL-DCA, 24.09 09:41). Тестовый spot DCA `3950110382492143616` на LTC-USDT: init 10 + 1 SO 10 USDT. Источники — demo API и исходник okx CLI 1.4.8.
    - `GET /tradingBot/dca/ongoing-list` — один `algoOrdType` за запрос, `limit` ≤ 100 (101 → `51000` «Parameter limit error»). Боты идут от новых к старым, курсор `after` = `algoId` работает. Фильтр `algoId` по остановленному боту даёт не пустой список, а ошибку `51291`. `contract_dca` при `acctLv=1` — code 0 и пустой список.
    - `POST /tradingBot/dca/stop` — один бот за запрос, тело — объект `{algoId, algoOrdType, stopType}`; grid `stop-order-algo`, для сравнения, принимает массив до 10. `stopType` обязателен для `spot_dca` (1 — продать монеты, 2 — оставить). У `contract_dca` по умолчанию 1 — закрыть позицию. Ответ: `code 0`, в `data[0]` — `algoId`, `algoClOrdId`, `tag`, `sCode 0`, `sMsg`.
    - После stop бот сразу пропадает из `ongoing-list`. Через ~1 с он в `history-list` со `state=stopped`, `stopTime` = `uTime`, `cancelType=1`. Промежуточный `stopping` в опросе не пойман.
    - Повторный stop → `code 1`, `sCode 51291` «The bot doesn't exist or has already stopped». В карте исключений CCXT этого кода нет — летит общий `ExchangeError`, код достаёт `errors.extract_error_code`.
    - Ордера DCA-бота не видны в `trade/orders-pending`. У тестового бота были живые TP sell 71.3 и SO buy 64.51, а `orders-pending` по LTC-USDT пуст. Отсюда: kill останавливает DCA только через bot API. В `bot dca sub-orders --cycleId` поле `ordType` = `init_order` / `safety_order` / `tp_order`.
    - `stopType 2` у `spot_dca`: TP и SO сняты, в цикле остаётся только исполненный `init_order`. Купленные монеты свободны: `availBal` 0.147105747 LTC, `frozenBal` 0.
    - Эндпоинта `min-investment` у DCA нет ни в CCXT 4.5.83, ни в CLI. 10 USDT init + 10 USDT SO на LTC-USDT приняты.
    - `slPx` = 61.11 (67.91 × 0.9) и `tpPx` = 71.3 (67.91 × 1.05) — округление вниз до `tickSz`, правило п. 15. Переданные `pxStepsMult = volMult = 1` `bot dca orders` возвращает пустыми строками.
    - CCXT: cost 1 при `rateLimit` 110 мс ≈ 18 запросов за 2 с — под лимитом tradingBot 20/2 с на UID. `50011`, `50001`, `50013` CCXT мапит в подклассы `NetworkError`, по ним `connector.stop_dca_bots` повторяет запрос.
20. **Метка владельца ордера: надёжен только префикс `clOrdId`** (ORDER-OWNER-TAG, 24.09 09:40–09:58). Источники — исходники CCXT 4.5.83 и okx CLI 1.4.8, история ордеров demo за 7 дней (940 SPOT) и живой ордер.
    - **CCXT** (`ccxt/okx.py`: `create_order_request`, `sign`): если `clOrdId` не передан, CCXT ставит `clOrdId` = brokerId + uuid16 и `tag` = brokerId `6b9ad766b55dBCDE` (`options.brokerId`). Так же он поступает с неявными вызовами `trade/order`, `batch-orders`, `order-algo`. Переданный `clOrdId` уходит как есть, `tag` тогда не подставляется, а свой `tag` из params доходит без изменений.
    - Все 22 ордера demo с `6b9ad…` поставил Python-код проекта через CCXT без `clOrdId`: smoke-тест в 01:52–02:41, рыночные пары в 02:49 и 03:59. MCP к ним отношения не имеет.
    - **okx CLI:** `tag` = `sourceTag` — `CLI` у CLI, у MCP-ядра по умолчанию `MCP`. Флага `--tag` нет, а `--aiBuilderCode` подменяет tag кодом атрибуции OKX. `--clOrdId` есть у `spot/swap/futures/option place`; у `algo place` он уходит как `algoClOrdId`. У ботов — `--algoClOrdId`.
    - **Ручной ордер человека** из веб-интерфейса или приложения: `clOrdId` и `tag` пустые (OKB-USDT `3949445926919024641`).
    - **Живой ордер:** post-only buy LTC-USDT 0.121478 @41.16, `3950144895231930369`, `clOrdId` `iexa786915852e44a9396c3490ebb96b`, `tag` `iex`. Оба поля вернулись как есть в `trade/order` и `orders-history`, поиск `trade/order?clOrdId=` работает. Ордер отменён через 2.6 с, висящих ордеров 0.
    - Суб-ордера grid/DCA-ботов в `trade/orders-history` не попадают: за 7 дней ни одного от 11 ботов флота (см. и п. 19).

    Следствие: владелец ордера определяется по префиксу `clOrdId` через реестр `src/order_owner.py`, аудит — `python -m src.order_audit`, правило для агентов — AGENTS.md §6.
21. **`GET /trade/orders-history`: порядок страниц зависит от `begin`** (24.09 09:47).
    - Без `begin`: от новых к старым, курсор `after` = `ordId`, перекрытий нет.
    - С `begin`: от старых к новым. Работает курсор `before`, а `after` возвращает уже полученные записи (4 из 4 повторились).
    - `begin` и `end` фильтруют по `cTime`.
    - MARGIN/SWAP/FUTURES/OPTION без ордеров отвечают `code 0` и пустым списком, без ошибки.

    `src/order_audit.py` поэтому листает без `begin` и останавливается на первом ордере старше периода.
22. **Demo-аккаунт сейчас в `acctLv=3` (multi-currency margin), а не 1, как в п. 12** — `account/config` в 09:56.
    - Спот-ордер с `tdMode=cash` → `sCode 51000` «Parameter tdMode error», ордер не создаётся. Тот же ордер с `tdMode=cross` принят: `instType SPOT`, `ccy USDT`.
    - `enableSpotBorrow=false`, `autoLoan=true`.
    - Кто сменил режим, не найдено: в журнале okx CLI (`~/.okx/logs/trade-2026-09-24.log`) вызова смены режима нет, в коде проекта тоже. Задача ACCT-MODE-DERIV на доске — ещё `ready`.

    Следствие: спот через CCXT с дефолтным `tdMode=cash` (OrderRouter, `engine.place_order`, `load_test`, `ratelimit_test`, `smoke_test`) и `okx spot place` без `--tdMode cross` (дефолт CLI — `cash`) сейчас получают `51000`.

    **SPOT-TDMODE (24.09, 12:01):** `OrderRouter`, `smoke_test`, `load_test` и `ratelimit_test` берут tdMode из `account/config` через `src/account_mode.py`. Если заём возможен, ордер больше `availBal` отклоняется до биржи, а live-preflight даёт fail. Без правки остался только `engine.place_order` — задача ENGINE-TDMODE.
23. **CCXT 4.5.83: рыночная покупка спота в режиме cross** (исходник `ccxt/okx.py`, `create_order_request`; тест `tests/test_account_mode.py::CcxtRequestTest`, без сети).
    - Для маржинального спота (`tdMode` не `cash`) CCXT не ставит `tgtCcy` и добавляет `ccy` — котируемую валюту у покупки и базу у продажи.
    - У okx `options['createMarketBuyOrderRequiresPrice'] = False`. При `tgtCcy=quote_ccy` без `cost` CCXT отправляет `amount` как сумму в котируемой валюте без умножения на цену и обрезает её до шага цены: 0.0002 BTC превращается в `sz="0"`.
    - Поэтому `account_mode.spot_order_params` для рыночной покупки в режиме cross передаёт `tgtCcy=quote_ccy` и `cost = sz × px`: в запрос уходит `sz="10"` USDT. Лимитные ордера и рыночная продажа — в базе.
    - По документации OKX (`POST /trade/order`, поле `sz`) рыночная покупка с маржой задаётся в котируемой валюте. На demo в `acctLv=3` это ещё не проверено — остаток SPOT-TDMODE.

## Открытые вопросы

1. ~~Точная карта ошибок timestamp~~ → **закрыто 2026-09-24**, эмпирически на demo: 50102 = просрочен (оба знака), 50112 = невалидный формат, 50107 = заголовок отсутствует. Источники: demo API (прямой вызов) + introduction.md (50102, окно 30 сек).
2. ~~Актуальные числовые rate limits~~ → **закрыто 2026-09-24**: `account-rate-limit` в демо возвращает пустые поля — fill-ratio-тиеры только для VIP5+; для проекта действует Tier 1 (1000 ордер-запросов/2с на суб-аккаунт) + per-endpoint лимиты из доков каждого эндпоинта (зеркало `okx/ai-builder-openapi-md`, числа в теле каждого эндпоинта, напр. grid order-algo = 20/2с). Снимать числа с `account-rate-limit` смысла нет до VIP5.
3. ~~Полный список функций, недоступных в Demo Trading~~ → **закрыто 2026-09-24, исчерпывающего списка не существует**: официальная документация называет лишь «`withdraw`, `deposit`, `purchase/redemption`, etc.» (introduction.md, Demo Trading Services). Эмпирически подтверждено, что в демо **работают**: tradingBot grid (создание, чтение, sub-orders), tradingBot recurring (чтение), trade/account/public эндпоинты, WS public/private/business. Правило проекта: поддержку конкретного эндпоинта в демо проверять точечно перед использованием.
4. Поведение демо-стакана: насколько симулированная ликвидность искажает исполнение market-ордеров → **вынесено в задачу `DEMO-SLIPPAGE`** (требует market-ордеров в демо — роль OKX Trader).
5. ~~Лимит подписок WS (480/час) vs потребность стратегии~~ → **закрыто 2026-09-24 расчётом**: лимит считается по *запросам* subscribe/unsubscribe/login, а не по числу каналов; в одном запросе можно передать массив args (до 64 КБ — сотни подписок за 1 запрос). Бюджет: 10 инструментов × 3 канала = 30 args = 1 запрос; даже при reconnect каждые 10 минут (login + 1 батч-подписка = 2 запроса) расход ~12 запросов/час из 480 — запас ×40. Узкое место не 480/час, а 30 соединений на приватный канал и 3 новых соединения/сек на IP. Источник: introduction.md (WebSocket Connect/Subscribe) + проверка формата subscribe с массивом args.
6. ~~Grid/CopyTrading/SpreadTrading API OKX — переиспользовать ли встроенные grid-механики~~ → **закрыто 2026-09-24 (validated)**: tradingBot API покрывает grid (spot/contract), DCA, recurring, signal-ботов — полный CRUD + публичные ai-param/min-investment/rsi-back-testing; всё это доступно на сайте okex (Global) и в демо (подтверждено запуском, [grid-bot-demo-run.md](grid-bot-demo-run.md)). Решение проекта уже в работе: биржевая сетка для валидации (Фаза 1), своя реализация — параллельно ([spot-strategies.md](spot-strategies.md) §1.1). CopyTrading/SpreadTrading — вне скоупа v1. Источники: зеркало доков `index/tradingBot.md` + эмпирика на демо.

## Следующие шаги

1. Завести OKX demo-аккаунт, создать demo API-ключ, проверить: подпись (known-answer вектор), `x-simulated-trading`, place/cancel ордера, WS login + orders-канал, намеренно словить 50011/51008.
2. Прогнать smoke-тест CCXT (`set_sandbox_mode(True)`) и python-okx (`flag="1"`) на идентичном сценарии — выбрать стек.
3. Зафиксировать конфиг-контракт проекта: домен региона, account mode, position mode, набор прав ключа.
4. Отдельное исследование: методология paper trading → demo → live с критериями перехода (риск-менеджмент).
