# Risk-core: требования к риск-ядру проекта (src/risk.py)

- **Дата:** 2026-09-24
- **Тема:** Минимальное риск-ядро: сайзинг фикс-% капитала, дневной лимит убытка, max-drawdown circuit breaker, kill-switch, блокировка инструмента после серии убытков, API-поверхность `src/risk.py`
- **Статус:** implemented (p1-risk-module, 2026-09-24)
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
| Плечо (когда дойдём до SWAP) | старт 3x, потолок 10x | rookie-airbag, «Ловушка 3» (таблица: 20x ≈ 5% движения до ликвидации) |
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
    глобальный breaker → дневной лимит → лимит сделок/день → инструмент
    заблокирован → лимит одновременных позиций → portfolio heat ≤ 6%.
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

# --- Учёт результата ---
def record_pnl(inst_id: str, pnl: float, closed_at: datetime) -> list[str]:
    """Обновляет дневной PnL, high-water mark, серии убытков (per-instrument
    и глобальную). Возвращает список сработавших событий:
    ['daily_limit', 'global_breaker', 'instrument_blocked', ...]."""

def register_spot_buy(inst_id: str) -> None:
    """Доливка спот-позиции без плеча и стопа (DCA и т.п.). Освобождает слот
    risk_open_risk (иначе следующая покупка — «хедж»), пишет событие spot_buy.
    Это НЕ закрытие сделки: намеренно НЕ трогает серии убытков (per-instrument
    и global_loss_streak), day_pnl, equity; entries_today уже учтён через
    register_entry в OrderRouter. Реализованный PnL — record_pnl при продаже,
    нереализованный — update_equity. (RISK-DCA-SLOT, 2026-09-24: заменил хак
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
    """Снапшот: equity, day_pnl, drawdown от HWM, активные блокировки,
    счётчики серий, состояние breakers — для Telegram-алертов и дашборда."""
```

Ключевые инварианты для исполнителя:
- **Состояние переживает рестарт** (SQLite), breaker не снимается перезапуском.
- Каждый отказ/триггер — запись в журнал риск-событий (phase0-demo-setup §2.3: «risk events and kill-switch events»).
- `check_entry_allowed` вызывается **перед каждым** ордером, включая ордера DCA/grid-лестниц — без исключений.
- **Владение kill-switch колбэком** (KILL-CALLBACK-OWNER, 2026-09-24): `set_order_canceller` хранит один слот, поэтому в него регистрируется единый диспетчер `connector.kill_switch_dispatch`, а владельцы (OrderRouter и др.) подписываются через `connector.register_kill_callback` — регистрация из нескольких мест не перезаписывается, при kill вызываются все подписчики, повторная регистрация идемпотентна. Исключение: `engine.py` регистрирует свой колбэк напрямую (файл заморожен, работает движок) — если engine создан ПОСЛЕ роутера, его колбэк выигрывает слот; это безопасно (тот же `connector.emergency_stop`), пометку canceled в storage восстановит реконсиляция. Порядок в общем процессе: сначала engine, затем OrderRouter.
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

## 9. Следующие шаги

1. ~~Insight Executor реализует `src/risk.py` по §6 + таблицу состояний в SQLite~~ ✅ Выполнено (p1-risk-module): `src/risk.py`, состояние в `data/risk_state.db` (risk_kv, risk_events, risk_instruments, risk_open_risk).
2. ~~Unit-тесты~~ ✅ Выполнено: `src/risk_selftest.py` (21 проверка, зелёный). Идемпотентность trip_breaker покрыта реализацией; kill_switch с колбэком коннектора — проверить на интеграции.
3. Интеграционный тест в демо: kill_switch отменяет и обычные, и algo-ордера (критерий выхода Фазы 3 roadmap).
4. Калибровка порогов после ≥7–14 дней paper-trading (критерий выхода Фазы 2).
