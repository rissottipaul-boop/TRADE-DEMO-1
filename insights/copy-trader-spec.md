# Спецификация `src/copy_trader.py` — сопровождение лид-трейдинга (COPY)

- **Дата:** 2026-09-30
- **Статус:** spec (проектная спецификация, не инсайт-доходность)
- **Заявка:** `d20260930-140121-2646` (Insight Executor, Muse)
- **Источники фактов:** [copy-trading.md](copy-trading.md) (статус validated, §1–§7),
  [futures-bots.md](futures-bots.md) §2.1/§4 (сегмент Bot, `profitSharingRatio`),
  [business-plan.md](business-plan.md) §2.1–§2.2 (рукава, трек до заявки),
  код: `src/fleet_manager.py`, `src/pnl_ledger.py` (bills `type 8`/`type 12`),
  `src/order_owner.py`, `src/risk.py`
- **Принцип:** модуль готовит и проверяет, **не заявляет**. Подача заявки
  лид-трейдера, смена региона/KYC, публикация профиля — действия человека
  (AGENTS.md §2: правая колонка). Модуль в сеть ходит только чтением
  (публичные copytrading-эндпоинты и read-only CCXT-мониторинг).

> Реализация — зона задачи INSIGHTS-ALL-IMPL (Antigravity, in-progress):
> на 14:08 в дереве лежат пустые плейсхолдеры `src/copy_trader.py` и
> `tests/test_copy_trader.py` (0 байт, чужая активная задача по AGENTS.md §5 —
> не трогать). Настоящий документ — контракт, под который пишется реализация.

## 1. Факты OKX, зашитые в спецификацию (copy-trading.md §1–§2)

| Факт | Значение для модуля |
| --- | --- |
| Доля лида Futures/Spot без уровня | ≤ 10%; уровни 10/13/15/30% (Legend ≥ 500k USDT) |
| Доля сегмента Bot (лид-боты grid/DCA) | плоские **30% на любом уровне**, включая без уровня |
| Расчёт доли | понедельник 00:00 UTC+8, только чистая прибыль подписчика за вычетом комиссий, на funding-счёт |
| Порог счёта | > 500 USDT, иначе копии не исполняются |
| Лимит покупок | ≤ 500 buy-ордеров/сутки (пекинское время) |
| Публичный список | lead assets ≥ 100 000 USDT; ниже — только прямая ссылка |
| Метка «API trader» | ≥ 80% сделок через API за 30 дней; фильтр внутри списка, не канал видимости |
| Продукты | SWAP + спот + Bot; carry из двух ног **не копируется** (у подписчика останется одна нога) |
| Регионы без copy trading | HK, SG, CU, IR, KP, Крым, MY, SY, US, CA, UK, BD, BO, MT |
| CCXT-read | `copytrading/set-instruments`, `current-subpositions`, `close-subposition`, `amend-profit-sharing-ratio` |
| `profitSharingRatio` нативных ботов | `contract_grid` / `contract_dca`: 0 / 0.1 / 0.2 / 0.3; у signal-бота поля нет |

## 2. Предлагаемый интерфейс (контракт для INSIGHTS-ALL-IMPL)

```python
@dataclass(frozen=True)
class LeadReadiness:
    strategy_gate: bool      # гейт бэктеста + 14 дней demo пройдены
    live_track_days: int     # дней live-трека (цель: 90–180 до заявки)
    account_equity_usdt: float  # > 500
    region_ok: bool          # страна KYC не в ban-листе §1 (флаг от человека)
    sub_account_verified_by_human: bool  # §7 п.4 copy-trading: открыт, ответ только из UI
    kyc_done_by_human: bool

def check_lead_readiness(r: LeadReadiness) -> list[str]: ...  # [] = готов, иначе блокеры
def lead_economics(sub_aum, sub_return, ratio) -> float: ...  # AUM × доходность × доля
def validate_lead_bot_ratio(v) -> float: ...  # только {0, 0.1, 0.2, 0.3}
def validate_copy_strategy(kind) -> list[str]: ...  # carry → запрет; DCA → предупреждение; signal → без доли
def monitor_subpositions(exchange) -> dict: ...  # только чтение current-subpositions
```

## 3. Инварианты (нарушение → `ValueError` или `blocked`-вердикт)

1. Модуль **не подаёт** заявку лид-трейдера и **не меняет** `profitSharingRatio`
   на живых ботах — только валидация и отчёт (правая колонка §2).
2. `check_lead_readiness`: все флаги истинны + `equity > 500` + `live_track_days > 0`;
   `sub_account_verified_by_human is False` → жёсткий блок lead-режима (fail-closed,
   открытый вопрос §7 п.4 нельзя предполагать закрытым ни в какую сторону).
3. `region_ok is False` или неизвестен → блок (ban-лист §1; регион проекта в доках
   не зафиксирован — copy-trading.md §7 п.5).
4. `validate_lead_bot_ratio`: только {0, 0.1, 0.2, 0.3}; до лидирования — 0
   (доля не передаётся в `profitSharingRatio`, см. futures-bots.md §1.6).
5. `validate_copy_strategy`: `"carry"` → запрет (две ноги не копируются, §5 вывод 2);
   `"spot_dca"` → предупреждение (технически копируется, подписчикам неинтересен);
   `"signal"` → предупреждение (нет `profitSharingRatio`, лид-ботом не сделать —
   не подтверждено); `"grid"`/`"contract_grid"`/`"contract_dca"` → ок (сегмент Bot, 30%).
6. Лид-бот создаётся только из стратегий, прошедших гейт бэктеста и 14 дней demo
   (copy-trading.md §5 вывод 5: раньше Q1 2027 лидирование не имеет смысла).
7. Лимит ≤ 500 buy/сутки контролируется счётчиком заявок лид-аккаунта
   (пекинские сутки); превышение → блок новых входов до 00:00 CST.
8. Любой создаваемый лид-бот: `algoClOrdId` через `order_owner` с кодом создателя,
   запись в `ops/sleeves.json`, риск-лимиты `src/futures_bot.py` §3 — без исключений.
9. Доход лида считается только с прибыли (`lead_economics` ≥ 0 при убытке подписчиков
   даёт 0, не отрицательное число).

## 4. Мониторинг (только чтение)

- `monitor_subpositions`: AUM подписчиков, число подписчиков, открытые субпозиции,
  недельный расчётный срез (понедельник 00:00 UTC+8). Источник — CCXT
  `current-subpositions` + `public-lead-traders` для бенчмарка (медиана топ-20:
  AUM ~80 300, подписчиков 67, win rate 60% — copy-trading.md §3, с поправкой
  на ошибку выжившего).
- Запись в журнал инсайта (не в торговый журнал): дата, AUM, доходность подписчиков,
  начисленная доля (сверка с funding-счётом через bills).
- Аномалии → задача на доску (не автофикс): падение AUM > 20%/нед, субпозиции
  без стопа, приближение к лимиту 500 buy/сутки (≥ 450 — предупреждение).

## 5. Сценарии тестов (будущий `tests/test_copy_trader.py`; `unittest`, фейки, без сети)

| # | Кейс | Ожидание |
| --- | --- | --- |
| C1 | readiness: все флаги + equity 1000 + track 100 дней | `[]` (готов) |
| C2 | equity 500 / 499 | блок (порог — строго > 500) |
| C3 | `sub_account_verified_by_human=False` | блок lead-режима (fail-closed) |
| C4 | `region_ok=False` / неизвестен | блок |
| C5 | `kyc_done_by_human=False` | блок |
| C6 | ratio 0.3 / 0 / 0.15 / −0.1 | ок / ок / `ValueError` / `ValueError` |
| C7 | kind `carry` | запрет; `spot_dca` — предупреждение; `signal` — предупреждение (нет доли); `contract_grid` — ок |
| C8 | `lead_economics(10000, 0.15, 0.10)` | 150.0 (сверка с таблицей §4 инсайта); убыток подписчиков → 0.0 |
| C9 | `lead_economics(80000, 0.20, 0.10)` / `(500000, 0.20, 0.13)` | 1600.0 / 13000.0 |
| C10 | счётчик buy 499/500/501 в пекинских сутках | ок / ок / блок до 00:00 CST (граница суток — тест на фейковых часах) |
| C11 | `monitor_subpositions` на фейке CCXT | парсинг AUM/позиций; ошибка сети → вердикт `unknown`, не исключение наружу |
| C12 | ban-лист §1: HK/US/GB/RU | True/True/True/False (RU явно нет в списке — тест фиксирует факт инсайта) |
| C13 | недельный срез: сделки до/после понедельника 00:00 UTC+8 | в долю входит только прошлая неделя |
| C14 | убыточные и прибыльные сделки взаимозачёт + комиссии до доли | доля считается с чистой прибыли |

Ручные проверки (не автотесты, исполнитель — человек/OKX Trader, copy-trading.md §7):
UI `Lead trader recruitment` с суб-аккаунта (п.4); видимость раздела Copy trading
под KYC-аккаунтом проекта (п.5); сверка rules-долей с приложением OKX
(правила меняются — дисклеймер §1 инсайта).

## 6. Связь с `src/futures_bot.py`

Лид-боты сегмента Bot (плоские 30%) — это F1–F5 из [futures-bot-spec.md](futures-bot-spec.md):
`copy_trader.validate_lead_bot_ratio` проверяет значение, `futures_bot.validate_grid/dca`
— риск-рамку. Порядок внедрения: сначала `futures_bot` (риск), потом `copy_trader`
(доля). До live-трека (Q1 2027 ориентир) оба — demo/валидация.

## 7. Критерий готовности модуля (для INSIGHTS-ALL-IMPL)

Датаклассы §2 + проверки §3 + мониторинг §4 (read-only);
`tests/test_copy_trader.py` зелёный (≥ 14 тестов); `unittest discover` без регрессий;
заявка лид-трейдера и смена ratio — только задачами `needs-user` (правая колонка §2).
