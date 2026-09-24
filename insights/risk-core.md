# Risk-core: требования к риск-ядру проекта (src/risk.py)

- **Дата:** 2026-09-24
- **Тема:** Минимальное риск-ядро: сайзинг фикс-% капитала, дневной лимит убытка, max-drawdown circuit breaker, kill-switch, блокировка инструмента после серии убытков, API-поверхность `src/risk.py`
- **Статус:** implemented (p1-risk-module, 2026-09-24); источник equity исправлен в RISK-PNL-DOUBLE (2026-09-24, §6)
- **Задача:** p0-research-risk
- **Решение, которое принимается:** какие числа, формулы и функции должен реализовать Insight Executor в модуле `src/risk.py` до запуска любой стратегии (Фаза 3 roadmap, раздел 2.5–2.6 phase0-demo-setup).

> Это инженерная спецификация риск-контура, а не финансовый совет. Все пороги — стартовые дефолты для демо, подлежат калибровке на paper-trading.

---

## 1. Суть идеи

Риск-ядро — единственный компонент, который должен существовать **до** первой стратегии (phase0-demo-setup, §2.5: «Risk-модуль до любой стратегии»). Его задача — не максимизировать прибыль, а гарантировать, что ни одна ошибка стратегии/AI/коннектора не убьёт счёт. Три локальных скилла дают согласованный набор правил; расхождения разрешены в пользу более строгого варианта (принцип «strictest constraint wins», position-sizer, Key Principles №4).

---

## 2. Сайзинг позиции фиксированным % капитала

### 2.1 Базовая модель — Fixed Fractional Risk

Источник: `position-sizer` (SKILL.md, «Fallback — inline calculation») и `resilience-trader` (Step 3.3). Формулы совпадают:

```
dollar_risk       = equity * risk_pct / 100
risk_per_contract = abs(entry - stop) * ctVal          # swap, USDT-margined
contracts         = floor(dollar_risk / risk_per_contract)

# spot:
risk_per_unit = abs(entry - stop)
units         = floor(dollar_risk / risk_per_unit)     # округление к lotSz

# ATR-вариант (опционально, position-sizer Mode B):
stop_distance = ATR(14) * multiplier                   # multiplier 1.5–3.0
```

Где `ctVal` — размер контракта из `GET /public/instruments` (BTC-USDT-SWAP = 0.01, ETH-USDT-SWAP = 0.1, SOL-USDT-SWAP = 1 — справочная таблица position-sizer; модуль обязан тянуть актуальные `ctVal`/`lotSz`/`minSz` через API, а не хардкодить).

### 2.2 Границы (дефолты проекта)

| Параметр | Значение | Источник |
|---|---|---|
| risk_pct (дефолт) | **1%** equity на сделку | position-sizer, Key Principle №2 («1% rule») |
| risk_pct (жёсткий максимум) | **2%** | position-sizer №2 («never exceed 2%»); resilience-trader P1 (2% — потолок) |
| Portfolio heat (сумма открытых рисков) | **≤ 6%** equity | position-sizer №7 (6–8%; берём нижнюю границу) |
| Макс. размер одной позиции | **≤ 15%** equity (notional-independent cap по марже/экспозиции) | resilience-trader Step 3.3 |
| Макс. одновременных позиций | **2**, запрет хеджа по одному instId | resilience-trader P3 |
| Округление | всегда `floor` к целым контрактам / lotSz | position-sizer №3 |
| Проверка ликвидации | стоп обязан быть **ближе** цены ликвидации; иначе вход запрещён | position-sizer №9 + блок «CRITICAL: Stop beyond liquidation» |
| Плечо (SWAP и контрактные боты) | **≤ 3x** — жёсткий потолок `MAX_LEVERAGE = 3`, проверка `check_leverage`, guard не даёт поднять (RISK-LEVERAGE-CAP, решение человека 24.09). Первая волна demo-флота — ≤ 2x | business-plan.md §7 (инвариант ≤ 3x, только isolated); futures-bots.md §6; rookie-airbag, «Ловушка 3» (20x ≈ 5% движения до ликвидации) |
| tdMode | **isolated** для первых live-попыток | rookie-airbag, «Ловушка 4» («новичкам — только逐仓») |

Отклонение от дефолта в сторону увеличения риска допустимо только через явный конфиг, не через код стратегии.

**Анти-паттерн (из rookie-airbag, «翻车 2: 单位混淆»):** перед выставлением ордера модуль обязан пересчитать итоговый размер в % equity и отклонить ордер, если notional > заданного потолка (дефолт 30% — порог «тревоги» из rookie-airbag).

---

## 3. Дневной лимит убытка и max-drawdown circuit breaker

Источник порогов: `resilience-trader` (风控总则 P2, Step 2 вопрос 3) + `rookie-airbag` (翻车 3: «жёсткие熔断 обязательны») + roadmap Фаза 3 п.1.

| Параметр | Дефолт | Поведение |
|---|---|---|
| **Дневной лимит убытка** | **−6% от equity на 00:00 UTC** | Стоп всех новых входов до конца суток; открытые позиции управляются по их стопам (не паник-закрытие) |
| **Сброс дневного лимита** | **00:00 UTC** ежедневно | Автоматический; снапшот equity на момент сброса пишется в state |
| **Max drawdown (глобальный breaker)** | **−15% от пикового equity** (high-water mark) | Полная остановка: блокировка входов + требование **ручного** подтверждения для возврата. Дефолт консервативный: между дневным 6% и «болью» восстановления (asymmetry: −15% требует +17.6%; −50% требует +100% — position-sizer №8) |
| **Сброс глобального breaker** | только вручную | Не автосброс; фиксируется событие `breaker_trip` с причиной и equity |

Поведение после срабатывания (любого уровня):
1. Немедленная блокировка новых ордеров (`check_entry_allowed() → False` глобально).
2. Событие в лог/алерт (Telegram — roadmap Фаза 3 п.3) с причиной, equity, drawdown.
3. Запись в state (SQLite) — переживает рестарт процесса; breaker **не должен** сниматься рестартом бота.
4. Дневной лимит сбрасывается сам в 00:00 UTC; глобальный — только ручным `reset_breaker()`.

---

## 4. Kill-switch

Источник: roadmap Фаза 3 п.2 + phase0-demo-setup §2.6 + rookie-airbag (翻车 5: «残留算法单»).

Требования:
1. **Ручной триггер** (файл-флаг / CLI-команда / Telegram-команда) — `kill_switch()`.
2. **Авто-отмена ВСЕХ активных ордеров**, включая algo-ордера (стопы/тейки): rookie-airbag явно требует чистить残留算法单 через `GET /trade/orders-algo-pending`, иначе остаточный стоп «выстрелит» в следующей сессии.
3. Опциональный режим `flatten=True`: рыночное закрытие позиций (по умолчанию — НЕТ, позиции закрываются по стопам; flatten — отдельное явное решение оператора).
4. Идемпотентность: повторный вызов безопасен (нет ордеров → no-op).
5. Полное логирование: кто/что/когда вызвал, сколько ордеров отменено, какие не отменились (retry с экспоненциальным backoff + эскалация в алерт, если отмена не удалась N раз).
6. Kill-switch — **терминальный** для текущего запуска: выход из него только ручной, после фиксации причины.

---

## 5. Блокировка инструмента после серии убытков

Источник: rookie-airbag (翻车 3: «连续 5 笔亏损自动暂停» + «每日开仓次数上限») + roadmap Фаза 3 п.1 («блокировка пары после серии убытков»).

Дефолты:

| Правило | Дефолт | Действие |
|---|---|---|
| Серия убытков по инструменту | **3 подряд** (мягче скилла — 5 — т.к. блокируем per-instrument, а не всю систему) | `block_instrument(instId)` на cooldown **24ч** |
| Серия убытков по системе | **5 подряд суммарно** | Глобальная пауза новых входов на 24ч (аналог дневного лимита, но по счётчику) |
| Лимит сделок в день | **≤ 10 входов/день** | Блокировка новых входов до 00:00 UTC (защита от overtrading-петли AI) |
| Разблокировка инструмента | автоматически после cooldown **или** вручную | Событие `instrument_unblocked` в лог |

Причина раздвоения порогов: per-instrument блокировка — дешёвая и частая (3), глобальная пауза — дорогая (5). Cooldown в 24ч — компромисс между «не торговать горячую голову» и не потерять инструмент навсегда.

---

## 6. Минимальная API-поверхность `src/risk.py`

Контракт для Insight Executor. Модуль stateful (состояние в SQLite — phase0-demo-setup §2.3), все функции чисты от сетевых вызовов, кроме kill_switch (он дергает коннектор через колбэк).

```python
# --- Входы / сайзинг ---
def check_entry_allowed(inst_id: str, side: str) -> tuple[bool, str]:
    """Единая точка допуска. Проверяет по порядку: kill-switch активен →
    глобальный breaker → дневной лимит → свежесть equity (update_equity был
    и не старше EQUITY_MAX_AGE_S = 600 с) → системная пауза → лимит сделок/день
    → инструмент заблокирован → лимит одновременных позиций → portfolio heat ≤ 6%.
    Возвращает (False, причина) при первом отказе. Все отказы логируются."""

def size_position(equity: float, entry: float, stop: float,
                  ct_val: float, lot_sz: float, min_sz: float,
                  risk_pct: float = 1.0) -> dict:
    """Fixed-fractional сайзинг: floor-округление, caps (2% риск, 15% позиция),
    min_sz/lot_sz из instruments API. Возвращает {size, dollar_risk, notional,
    warnings[]} — пустой size при нарушении min_sz."""

def validate_stop_vs_liquidation(entry: float, stop: float, liq_price: float,
                                 side: str) -> bool:
    """Стоп обязан срабатывать раньше ликвидации (position-sizer №9)."""

# --- Equity ---
def update_equity(equity: float) -> list[str]:
    """Equity по балансу биржи (движок — totalEq; live-runner — стоимость
    кармана) — ЕДИНСТВЕННЫЙ источник equity и HWM. Проверяет дневной лимит
    от equity на 00:00 UTC и глобальный breaker −15% от HWM. Пишет момент
    вызова в risk_kv (equity_updated_at, переживает рестарт) — по нему
    check_entry_allowed судит о свежести. NaN/inf отбрасываются и свежесть
    не продлевают. Возвращает ['daily_limit', 'global_breaker'] или []."""

# --- Учёт результата ---
def record_pnl(inst_id: str, pnl: float, closed_at: datetime) -> list[str]:
    """Закрытая сделка: дневной PnL (day_pnl), серии убытков (per-instrument
    и глобальная), блокировка инструмента, системная пауза, дневной лимит по
    day_pnl. Equity и HWM НЕ меняет; глобальный breaker проверяет по текущей
    equity из update_equity. Возвращает список сработавших событий:
    ['daily_limit', 'global_breaker', 'instrument_blocked', 'system_pause']."""

def register_spot_buy(inst_id: str) -> None:
    """Доливка спот-позиции без плеча и стопа (DCA и т.п.). Освобождает слот
    risk_open_risk (иначе следующая покупка — «хедж»), пишет событие spot_buy.
    Это НЕ закрытие сделки: намеренно НЕ трогает серии убытков (per-instrument
    и global_loss_streak), day_pnl, equity; entries_today уже учтён через
    register_entry в OrderRouter. Реализованный PnL — record_pnl при продаже
    (day_pnl и серии), equity — только update_equity. (RISK-DCA-SLOT, 2026-09-24: заменил хак
    record_pnl(inst, 0), который обнулял глобальную серию убытков.)"""

# --- Breakers ---
def trip_breaker(reason: str, scope: str = "global") -> None:
    """Немедленная остановка входов. scope: 'daily' (автосброс 00:00 UTC) |
    'global' (только ручной сброс). Идемпотентно."""

def reset_breaker(scope: str, by: str = "manual") -> None:
    """Ручной сброс; 'daily' вызывается планировщиком в 00:00 UTC."""

def block_instrument(inst_id: str, hours: int = 24) -> None: ...
def is_instrument_blocked(inst_id: str) -> bool: ...

# --- Аварийная остановка ---
def kill_switch(flatten: bool = False, by: str = "manual") -> dict:
    """Отменяет ВСЕ ордера включая algo (orders-algo-pending!), опционально
    закрывает позиции, блокирует всё до ручного сброса. Возвращает отчёт:
    {cancelled: [...], failed: [...], flattened: bool}. Retry + алерт на failed."""

# --- Диагностика ---
def status() -> dict:
    """Снапшот: equity, свежесть фида equity (equity_updated_at, equity_age_s),
    day_pnl, drawdown от HWM, активные блокировки, счётчики серий, состояние
    breakers — для Telegram-алертов и дашборда."""
```

Ключевые инварианты для исполнителя:
- **Источник equity — только баланс биржи** (RISK-PNL-DOUBLE, 2026-09-24, решение человека — вариант (а)). Equity и HWM пишет только `update_equity`. Баланс по рынку уже содержит PnL открытой позиции, поэтому `record_pnl` equity не трогает. Раньше он прибавлял PnL ещё раз: прибыль у пика завышала HWM, убыток вычитался дважды, и глобальный breaker срабатывал раньше номинала — в бэктесте при 9.11% и 10.69% реальной просадки ([backtest-baseline.md](backtest-baseline.md), «Вывод»). Лимиты не менялись: breaker снова срабатывает на заданных −15% реальной просадки от HWM.
- **Сначала equity, потом вход.** `check_entry_allowed` отказывает, если `update_equity` не вызывался ни разу или последний вызов старше `EQUITY_MAX_AGE_S = 600` с. Это двойной запас к интервалу движка (`engine._equity_interval = 300` с): без фида equity глобальный breaker не сработал бы никогда. Отметка времени «из будущего» дальше 600 с (часы переведены назад) тоже считается устаревшей. `record_pnl` свежесть не продлевает. Кто кормит equity: движок — раз в 300 с и при старте; `DCABot` — на каждой итерации перед входом (если баланс не прочитан, решает свежесть от другого фида, например движка); бэктест (`SimRisk`) — на close каждого бара. Увеличивать `EQUITY_MAX_AGE_S` — значит ослаблять защиту, это решает только человек. С 24.09 порог в списке guard (`RISK_LIMITS_UP_IS_RISKIER` в `ops/hooks/guard.py`, GUARD-EQUITY-AGE, решение человека). Guard отклоняет рост значения в `src/risk.py`, нечисловое значение (`float("inf")`) и подмену `risk.EQUITY_MAX_AGE_S = …` из другого модуля; уменьшать можно.
- **Переход движка.** Движок P1-72H (старт 03:22 24.09) работает на старом коде: `equity_updated_at` не пишет, а `record_pnl` не вызывает. Процессы на новом коде, которые полагаются только на его фид (не зовут `update_equity` сами), до ENGINE-RESTART получат отказ «equity ни разу не выставлялась» — это отказ в безопасную сторону. После рестарта движок пишет отметку при старте и каждые 300 с.
- **Состояние переживает рестарт** (SQLite), breaker не снимается перезапуском.
- Каждый отказ/триггер — запись в журнал риск-событий (phase0-demo-setup §2.3: «risk events and kill-switch events»).
- `check_entry_allowed` вызывается **перед каждым** ордером, включая ордера DCA/grid-лестниц — без исключений.
- **Владение kill-switch колбэком** (KILL-CALLBACK-OWNER и ENGINE-KILL-REG, 2026-09-24). `set_order_canceller` хранит один слот, поэтому в него регистрируется единый диспетчер `connector.kill_switch_dispatch`. Владельцы подписываются через `connector.register_kill_callback`: OrderRouter — в `__init__`, отписка в `close()`; движок — в `TradingEngine.__init__`, отписка в `stop()`. Регистрации из нескольких мест не перезаписывают друг друга: при kill вызываются все подписчики, повторная регистрация идемпотентна, порядок создания движка и роутера не важен. Напрямую в слот пишет только `python -m src.ops kill`: это отдельный процесс без других подписчиков.
- **Что останавливает колбэк kill** (FLEET-KILL-DCA, 2026-09-24). `connector.emergency_stop` отменяет обычные и algo-ордера, затем останавливает нативных ботов OKX: `grid`/`contract_grid` и `spot_dca`/`contract_dca`, со `stopType 2` (без flatten, KILL-FLATTEN-DEFAULT). `failed` содержит algoId неостановленных ботов и описание шага, если список ботов не получен. `risk.kill_switch` читает только `cancelled`/`failed`, поэтому сбой списка не выглядит «успехом». Транзиентные ошибки DCA (`ccxt.NetworkError`: сеть, 50011, 50001, 50013) повторяются 2 раза с backoff 1 и 2 с. Код `51291` (бот уже остановлен) отказом не считается. Процесс берёт код kill при старте: движок P1-72H (старт 03:22 24.09) до перезапуска DCA-ботов не останавливает (см. [bot-fleet-demo.md](bot-fleet-demo.md) §7).

---

## 7. Источники

1. [.agents/skills/position-sizer/SKILL.md](../.agents/skills/position-sizer/SKILL.md) — формулы fixed-fractional/ATR/Kelly, 1% rule, portfolio heat 6–8%, floor-округление, проверка ликвидации, ctVal-таблица
2. [.agents/skills/resilience-trader/SKILL.md](../.agents/skills/resilience-trader/SKILL.md) — P1 (2% max risk), P2 (дневной −6%, сброс 00:00 UTC), P3 (макс. 2 позиции, без хеджа), 15% cap позиции, стоп 3%
3. [.agents/skills/rookie-airbag/SKILL.md](../.agents/skills/rookie-airbag/SKILL.md) — серия убытков → пауза, лимит сделок/день, чистка残留算法单, isolated-по-умолчанию, ловушки плеча, проверка %-от-аккаунта перед ордером
4. [insights/roadmap.md](roadmap.md) — Фаза 3: риск-модуль, kill-switch, circuit breaker, блокировка пары
5. [insights/phase0-demo-setup.md](phase0-demo-setup.md) — §2.5 risk до стратегии, §2.6 kill-switch, §2.3 журналирование риск-событий

Веб-источники не потребовались: все ключевые числа подтверждены минимум двум локальными источниками (например, дневной лимит и breaker — resilience-trader + phase0-demo-setup + roadmap).

---

## 8. Открытые вопросы

- ~~Точный порог глобального max-drawdown (−15% — стартовая гипотеза)~~ → **вынесено в задачу `RISK-CALIB`** (калибровка на paper/demo-данных Фазы 2–3, 2026-09-24).
- ~~Нужен ли per-instrument daily limit в дополнение к глобальному (например −3% на пару)?~~ → **отвечено 2026-09-24: на старте не нужен.** Арифметика: макс. 2 позиции × риск ≤1% + portfolio heat ≤6% + глобальный дневной −6% уже ограничивают дневной убыток по одной паре тремя полными стопами (~3%); дополнительный per-pair лимит дублирует существующую связку «серия 3 убытков → блок инструмента 24ч». Пересмотреть в рамках `RISK-CALIB`, если статистика paper покажет концентрацию убытков в одной паре. Источники: position-sizer (heat 6–8%), resilience-trader (P2/P3).
- ~~Режим flatten при kill-switch: согласовать дефолт с оператором перед live~~ → **отвечено 2026-09-24 (задача `KILL-FLATTEN-DEFAULT`): дефолт `flatten=False` подтверждён человеком** — позиции при kill-switch закрываются по своим стопам, рыночное закрытие (`flatten=True`) остаётся отдельным явным решением оператора. Совпадает с текущей реализацией (`kill_switch(flatten: bool = False, ...)`, §4 п.3) — код менять не требуется.
- ~~Автосброс блокировки инструмента vs ручной — 24ч cooldown может быть избыточен для mean-reversion~~ → **отвечено 2026-09-24: оставить авто-cooldown 24ч + ручную разблокировку, cooldown сделать конфигурируемым.** Прецедент Freqtrade: `lock_pair(pair, until, reason)` — блокировка пары на время с автоснятием, плюс ручной `unlock_pair`; те же два механизма у нас уже есть (`block_instrument(hours)` + ручной сброс). Для mean-reversion-стратегии cooldown можно будет опустить (например до 4ч) через конфиг стратегии после калибровки в `RISK-CALIB`. Источники: Freqtrade strategy-customization («Locking pairs»), rookie-airbag (серия убытков → пауза).
- ~~Учитывать ли funding rate в record_pnl для swap-позиций~~ → **отвечено 2026-09-24: да, обязательно для SWAP, реализация отложена до перехода на SWAP — задача `RISK-FUNDING-PNL`.** Funding — реальный денежный поток позиции (события каждые 1ч/4ч/8ч по расписанию инструмента): без него дневной PnL и серии убытков по swap-позициям систематически искажены. Источники: position-sizer (Step 5.5), Freqtrade DataProvider (`funding_rate(pair)`, исторический funding как отдельные события, а не свечи). На текущей спот-фазе funding отсутствует — блокирующим не является.
- ~~Закрывать ли позицию контрактного бота при kill-switch (`stopType 1`)~~ → **отвечено 2026-09-24 (задача `KILL-CONTRACT-STOPTYPE`): нет, при kill-switch позиции не закрывать.** Kill останавливает всех ботов, спотовых и контрактных, со `stopType 2`; `connector.emergency_stop` не меняется. Вместе с остановкой у контрактного бота снимается его SL. Остаток позиции закрывает надзор, только если цена дошла до бывшего SL бота ([futures-bots.md](futures-bots.md) §6.5), — это «закрытие по своему стопу» из `KILL-FLATTEN-DEFAULT`. Худший случай между проверками — маржа бота: плечо ≤ `MAX_LEVERAGE`, маржа бота ≤ 2% equity.

## 9. Следующие шаги

1. ~~Insight Executor реализует `src/risk.py` по §6 + таблицу состояний в SQLite~~ ✅ Выполнено (p1-risk-module): `src/risk.py`, состояние в `data/risk_state.db` (risk_kv, risk_events, risk_instruments, risk_open_risk).
2. ~~Unit-тесты~~ ✅ Выполнено: `src/risk_selftest.py` (21 проверка, зелёный). Идемпотентность trip_breaker покрыта реализацией; kill_switch с колбэком коннектора — проверить на интеграции.
3. Интеграционный тест в демо: kill_switch отменяет и обычные, и algo-ордера (критерий выхода Фазы 3 roadmap).
4. Калибровка порогов после ≥7–14 дней paper-trading (критерий выхода Фазы 2).
