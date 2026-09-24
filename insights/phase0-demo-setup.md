# Phase 0: минимальный чек-лист для безопасного запуска OKX demo-бота

- **Дата:** 2026-09-24
- **Тема:** Что именно нужно закрыть до первого live-перехода: настройка demo API, state, risk, smoke-test и kill-switch
- **Статус:** validated
- **Решение, которое принимается:** строить Phase 0 как управляемый и проверяемый контур без стратегии в «боевом» смысле; сначала достигаем стабильности исполнения и контроля, потом добавляем торговую логику.

---

## 1. Суть идеи

Из локальной базы и проектных заметок понятно: проект должен стартовать не с «идеальной стратегии», а с надёжного контура исполнения. Успех на раннем этапе определяется не тем, насколько стратегия сложная, а тем, насколько стабильно бот:

- создаёт и отменяет ордеры;
- понимает account state и market state;
- корректно логирует ошибки API;
- умеет останавливать всё при выходе за риск-лимиты.

Фундаментальный принцип: Phase 0 — это не торговля, а доказательство, что система безопасно живёт в демо-режиме и не теряет состояние.

---

## 2. Что обязательно закрыть до первой реальной стратегии

### 2.1 OKX demo environment

1. Создать отдельный demo account и demo API key.
2. Зафиксировать регион/домен: global / EEA / US и соответствие домена ключам.
3. Проверить, что ключи разделены между demo и live.
4. Убедиться, что account mode и posMode зафиксированы и не меняются без необходимости.
5. Проверить, что в demo-режиме не включён live-заголовок `x-simulated-trading` в неправильный контекст.

Риски:
- неверный домен API → 50119;
- неверный timestamp → 50102/50112;
- несоответствие demo/live ключей → непредсказуемое поведение клиента;
- изменение account mode в процессе позиции → состояние в проекте становится невалидным.

### 2.2 Базовый коннектор и time sync

1. `GET /public/time` — обязательный первый запрос.
2. Синхронизация с NTP/серверным временем до уровня, где drift не превышает 30s.
3. Обработка подписи и заголовков REST в строгом порядке.
4. Проверка `sCode` / `sMsg` внутри `data[]` для каждого модели ответа.
5. Отдельный слой логирования для ошибок HTTP, JSON, rate limit, errors by code.

Это критично: без корректного времени и без стандартизированного разборa ошибок система не может считаться безопасной.

### 2.3 Состояние и журналирование

Нужно хранить минимум:

- all orders (status, type, instId, side, price, qty, createdAt);
- positions (entry, side, leverage, liquidation price, PnL);
- trades / fills;
- equity curve and account snapshot;
- risk events and kill-switch events;
- timestamp and exchange response payload for each issue.

Практичность:
- SQLite достаточно на старте;
- не нужно строить сложную схему сразу, но без единого источника truth проект быстро разваливается.

### 2.4 Smoke-test ордеров

Минимальный успешный smoke-test:

- `GET /public/time`
- `GET /account/balance`
- `POST` лимитный ордер на demo паре
- `DELETE` отмена ордера
- повторная проверка статусов (open, filled, cancelled)
- чтение `sCode`/`sMsg` и связанного идентификатора ордера

Критерий выхода:
- система может открыть/отменить ордер в demo без ошибок и без рассинхронизации состояний.

### 2.5 Risk-модуль до любой стратегии

Даже если торговая логика отсутствует, должен быть встроен минимум:

- fixed % risk per trade;
- daily loss limit;
- max drawdown trigger;
- max open positions;
- emergency closure by manual trigger;
- protection from oversized position (position-sizer logic);
- stop for volatility spike / params check.

Из local skills: `position-sizer` и `resilience-trader` показывают, что без жесткого фильтра риска даже хороший сигнал становится аварийным входом.

### 2.6 Kill-switch и аварийное управление

Обязательные элементы:

- ручной kill-switch;
- автоматическая отмена всех активных ордеров;
- блокировка новых ордеров при срабатывании risk breaker;
- логирование триггера и причины срабатывания;
- механизм возврата в safe mode после разблокировки.

Без этого невозможно безопасно переходить к хотя бы demo-торговле на реальных деньгах (или в режимах close to live).

---

## 3. Что должно быть сделано до запуска первой стратегии

### 3.1 Демо-бот на простых сценариях

Нужно проверить не «супер-стратегию», а минимальный сценарий:

- buy order on signal;
- sell order on signal;
- stop-loss placement;
- order cancellation;
- retry on API timeout;
- resume after reconnect.

### 3.2 Проверка связи данных

Нужно доказать, что:

- рыночные данные и состояние account совпадают;
- локальный state не уходит в рассинхрон;
- reconnect после WS drop не ломает логическую модель.

### 3.3 Проверка биржевого bot API (если планируется grid)

Для стратегии grid критически важно проверить, доступен ли `grid/order-algo` в demo-режиме и есть ли корректная реакция на `min-investment`, `ai-param`, `state` и ограничения по API. Это касается также `tradingBot` в OKX.

---

## 4. Критерии выхода из Phase 0

Проект можно считать готовым к следующей фазе только если выполняются все пункты:

1. Demo account и API key работают стабильно.
2. `time`, `balance`, `place`, `cancel` проходят smoke-test без аномалий.
3. Состояние заказа/позиции полностью отслеживается и можно восстановить после reconnect.
4. Risk-модуль корректно блокирует вход в небезопасных условиях.
5. Есть kill-switch и база логирования.
6. 72 часа непрерывной работы в demo без рассинхронизации состояния.

Это согласуется с планом в [roadmap.md](../insights/roadmap.md) и локальными skill-ограничениями по безопасности и защитным фильтрам.

---

## 5. Приоритетный набор решений для старта

### Стартовый набор

- Demo API + sync + logs
- Risk layer + position sizing
- Simple state model
- One simple execution loop
- One basic signal strategy only after Phase 0 exits

### Не запускать в Phase 0

- сложный ML-подслой;
- LLM-сентимент как primary signal;
- ограниченные фьючерсные стратегии с плечом без подтверждённой risk-политики;
- масштабный multi-strategy loop до стабильности базовой инфраструктуры.

---

## 6. Источники

1. [roadmap.md](../insights/roadmap.md)
2. [okx-api.md](../insights/okx-api.md)
3. [spot-strategies.md](../insights/spot-strategies.md)
4. [startup-priority-brief.md](../insights/startup-priority-brief.md)
5. [position-sizer](../.agents/skills/position-sizer/SKILL.md)
6. [resilience-trader](../.agents/skills/resilience-trader/SKILL.md)
7. https://www.okx.com/docs-v5/
8. https://github.com/okx/ai-builder-openapi-md
9. https://github.com/okx/builder-integration-demo
10. https://hummingbot.org/exchanges/okx/
11. https://www.freqtrade.io/en/stable/exchanges/#okx

---

## 7. Открытые вопросы

- Есть ли реальный demo-поддержанный `grid/order-algo` в регионе проекта?
- Какой набор прав ключа и какой домен будут использоваться в окружении проекта?
- Нужно ли делать фокус на spot-подходе в Phase 0, или в проекте сразу готовится swap-ориентированная архитектура?
- Какой минимальный капитал/потолок риска задаётся в проекте для первых demo и live проб?

---

## 8. Следующие шаги

1. Сформировать exact Phase 0 checklist в виде task-list для проекта.
2. Подтвердить demo-методы для `time`, `balance`, `place`, `cancel` на реальном окружении.
3. Выбрать одну простую стратегию (DCA или grid) как первый real-dry run после Phase 0.
4. Ввести risk-breaker и kill-switch в систему до любого live-перехода.

Итог: Phase 0 должен закрываться как инженерный выход «система жива и безопасна», а не как «стратегия заработала». Это принцип, который удерживает проект от преждевременной смерти.
