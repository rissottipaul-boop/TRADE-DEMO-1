# Спецификация `src/futures_bot.py` — обёртка контрактных ботов (FUTURES-BOT)

- **Дата:** 2026-09-30
- **Статус:** spec (проектная спецификация, не инсайт-доходность)
- **Заявка:** `d20260930-140121-2646` (Insight Executor, Muse)
- **Источники фактов:** [futures-bots.md](futures-bots.md) (механика validated, demo-факты CFLEET-PROBE §11),
  [grid-strategy-design.md](grid-strategy-design.md) §3/§6, [risk-core.md](risk-core.md),
  [business-plan.md](business-plan.md) §7 (инвариант плеча), код: `src/risk.py`
  (`MAX_LEVERAGE=3`, `MAX_ACTIVE_BOTS=50`, `validate_stop_vs_liquidation`),
  `src/connector.py` (`emergency_stop` → `stopType "2"`, `no_close_position`),
  `src/order_owner.py` (ORDER-OWNER-TAG), `src/fleet_manager.py` (каталог 13 типов),
  `src/grid_engine.py` (образец стиля: `is_grid_service`, hard stop)
- **Вне области:** signal-бот С6 (позже, после гейта сигнальной стратегии),
  Smart Arbitrage С7 (отклонён — нет API), инверсный грид С9 (позже),
  TWAP/chase/recurring С8 (исполнение внутри CARRY-IMPL, не флот)

> Модуль **не ходит в сеть**: он строит и валидирует параметры нативных ботов
> (`contract_grid`, `contract_dca`), а создаёт их человек/OKX Trader через
> `okx --demo bot …` или код NATIVE-CBOT-WRAP. Ошибки маппит через `src/errors.py`
> (задача ERR-BOT-CODES).
>
> Реализация — зона задачи INSIGHTS-ALL-IMPL (Antigravity, in-progress):
> на 14:08 в дереве лежат пустые плейсхолдеры `src/futures_bot.py` и
> `tests/test_futures_bot.py` (0 байт, чужая активная задача по AGENTS.md §5 —
> не трогать). Настоящий документ — контракт, под который пишется реализация.

## 1. Покрываемые стратегии (из futures-bots.md §4)

| ID модуля | Тип OKX | Направление | Режим 4H | Плечо (волна 1) |
| --- | --- | --- | --- | --- |
| F1 | `contract_grid` | neutral | Range, ADX < 25, BBW 2–15% | ≤ 2x |
| F2 | `contract_grid` | long | MildUp | ≤ 2x (A/B со спотом — 1x) |
| F3 | `contract_grid` | short | MildDn, funding ≥ 0 | ≤ 2x (альт в Mild — 1x) |
| F4 | `contract_dca` | long | Range/MildUp у поддержки | ≤ 2x |
| F5 | `contract_dca` | short | MildDn у сопротивления | 1x |

## 2. Предлагаемый интерфейс (контракт для INSIGHTS-ALL-IMPL)

```python
@dataclass(frozen=True)
class FuturesGridConfig:
    inst_id: str          # "<BASE>-USDT-SWAP"
    direction: str        # "neutral" | "long" | "short"
    lever: int            # 1..3 (волна 1: <= 2)
    margin_usdt: float    # sz — маржа в USDT
    min_px: float; max_px: float; grid_num: int
    run_type: int = 2     # геометрическая, как у спот-флота
    base_pos: Optional[bool] = None  # None = не передавать (обязательно для neutral)
    sl_trigger_px: Optional[float] = None  # long/short
    sl_ratio: Optional[float] = None       # neutral
    owner_code: str = "trd"

@dataclass(frozen=True)
class FuturesDCAConfig:
    inst_id: str; direction: str  # "long" | "short"
    lever: int
    init_ord_amt: float; safety_ord_amt: float; max_safety_ords: int
    px_steps: float; px_steps_mult: float; vol_mult: float
    tp_pct: float; sl_pct: float
    allow_reinvest: bool = False  # API-дефолт true — всегда гасить явно
    owner_code: str = "trd"

def validate_grid(cfg, equity_usdt) -> list[str]: ...   # [] = ок, иначе причины
def validate_dca(cfg, equity_usdt) -> list[str]: ...
def liquidation_estimate(...) -> float: ...  # формула futures-bots.md §1.3
def stop_plan(algo_id) -> dict: ...  # план стопа для Sentinel (§5 ниже)
```

## 3. Инварианты валидации (нарушение → `ValueError`, как `grid_engine.py:50-53`)

1. `lever`: 1 ≤ L ≤ `risk.MAX_LEVERAGE` (3); волна 1 — ≤ 2 (константа `WAVE1_MAX_LEVER=2`).
2. Маржа бота ≤ 2% equity (`MAX_BOT_INVESTMENT_PCT`); Σ маржи контрактных ботов ≤ 4% equity.
3. Σ планового худшего по SL ≤ 0.8% equity (формула `L_SL` из futures-bots.md §3.2).
4. SL обязателен: `sl_trigger_px` (long/short) или `sl_ratio` (neutral). DCA: `sl_pct > MPD`.
5. Правило SL↔ликвидация (§3.3): SL строго между краем позиции и `liqPx`;
   дистанция SL→liq ≥ 3 × дистанция край→SL и ≥ 15% цены (альты ≥ 20%).
   Реализация — через существующий `risk.validate_stop_vs_liquidation`.
6. Шаг сетки: ≥ 0.25% мажоры / ≥ 0.35% альты (пол OKX 0.1%/уровень — код 51381);
   геометрическая (`runType=2`); 2 ≤ `gridNum` ≤ min(100, `grid-quantity`).
7. `base_pos`: для `neutral` — всегда None (не передавать); long/short — явное bool.
8. `allow_reinvest` у DCA — всегда False (API-дефолт true раздувает объём).
9. `inst_id` — только `*-USDT-SWAP` (coin-M запрещён до С9).
10. Гейты создания (§2 futures-bots.md): ATR-шок (ATR(1H) > 3× среднего 30 баров) —
    запрет; перегрев funding (|funding| > 0.05%/8ч) — запрет направленного бота
    на платящей стороне; окно funding ±5 мин — запрет создания и добавления SO.
11. Режим аккаунта `acctLv == 2`, иначе боты вернут 51057 (модуль валидирует заранее).
12. Один бот на инструмент+направление; `algoClOrdId` — `order_owner.new_client_order_id("trd")`;
    бот регистрируется в `ops/sleeves.json → bots[algoId] = "demo_fleet"`.
13. Создание нативного бота — **не более 1 инкремента `entries_today`**
    (аналог grid-strategy-design §6.1 для обслуживающих ордеров сетки).

## 4. Учёт в риск-ядре и ledger (факты CFLEET-PROBE, futures-bots.md §11)

- Маржа бота лежит в USDT `stgyEq` и **входит** в `totalEq` (п.1 ответов);
  создание/остановка пишут пары bills `type 12` (202/200) — ledger их уже знает.
- `mgnMode` позиций бота — `cross` **внутри** счёта бота; убыток ≤ маржи бота (п.2).
- Позиции ботов **не видны** в `account/positions` (п.3) — реконсилятор их не трогает;
  надзор только через `bot grid positions` / `dca position-details`.
- Funding бота — в bills основного счёта (`type 8`, subType 173/174) **и** в `fundingFee`
  бота (п.8): модуль возвращает оба поля для `pnl_ledger` (задача PNL-LEDGER-CBOTS).
- Neutral с `basePos=false` позицию на старте не открывает (п.4);
  long с `basePos=true` покупает базу рынком (п.4) — подтверждать в `details`.

## 5. Kill-switch (решение KILL-CONTRACT-STOPTYPE 24.09 — буквально)

- `connector.emergency_stop` шлёт контрактным ботам `stopType "2"`:
  сетка снимается, **позиция с плечом остаётся без SL** (`no_close_position`).
- Модуль **не закрывает** позиции при kill (решение человека: при kill не закрывать).
- `stop_plan(algo_id)` возвращает `{former_sl_px, close_cmd}` — по нему Ops Sentinel
  закрывает остаток `stopType 1` (или `grid/close-position --mktClose`), когда цена
  дошла до бывшего SL (futures-bots.md §6.5). Повторный `stop` по `no_close_position`
  со `stopType 2` — не провал (`BOT_ALREADY_STOPPED`, ср. `connector.py:317`).
- Signal-ботов kill-switch не видит — модуль их не покрывает (задача HUNT-SIGNAL-BOT).

## 6. Коды ошибок → `src/errors.py` (задача ERR-BOT-CODES, таблица futures-bots.md §1.7)

`51057` (нужен acctLv 2/3) · `51055` (не Portfolio margin) · `51070` (первый переход
режима — в Web/App, действие человека) · `51340` (маржа < минимума → сначала
`min-investment`) · `51399` (уменьшить sz/плечо) · `51370` · `51381` (шаг ≥ 0.1%)
· `51348`/`51344` (SL-шаблон minPx×0.97 / maxPx×1.03) · `51349`/`51343`
· `51398`/`51380` (пересчитать SL/TP) · `51313` (не переводить маржу руками)
· `51065` (новый ID из `order_owner`) · `51291` (`BOT_ALREADY_STOPPED`) · `50016`.

## 7. Сценарии тестов (будущий `tests/test_futures_bot.py`; стиль — `unittest`, фейки, без сети)

Валидация конфигов (каждый пункт §3 — минимум по одному тесту, пример — таблица):

| # | Кейс | Ожидание |
| --- | --- | --- |
| V1 | `lever=4` / `lever=0` | `ValueError` (потолок `risk.MAX_LEVERAGE`) |
| V2 | `lever=3` при `WAVE1_MAX_LEVER=2` | `ValueError` волны 1 |
| V3 | маржа 3% equity / Σ маржи 5% | `ValueError` (2% / 4%) |
| V4 | Σ планового худшего 1.0% equity | `ValueError` (лимит 0.8%) |
| V5 | грид long без `sl_trigger_px`; neutral без `sl_ratio`; DCA `sl_pct <= MPD` | `ValueError` |
| V6 | SL за ликвидацией; SL→liq < 3× край→SL; альт < 20% | `ValueError` |
| V7 | шаг 0.08% (мажор) | `ValueError` (пол 0.25%; OKX-пол 0.1%) |
| V8 | `neutral` + `base_pos=True/False` | `ValueError` (только None) |
| V9 | DCA без явного `allow_reinvest=False` | `ValueError` |
| V10 | `BTC-USD-SWAP` (coin-M) | `ValueError` (только USDT-SWAP) |
| V11 | ATR-шок / funding > 0.05% / окно funding ±5 мин | запрет создания (не `ValueError`, а `blocked`-вердикт) |
| V12 | `grid_num=1` / `> maxGridQty`; диапазон min ≥ max | `ValueError` |
| V13 | дубль направления на инструмент; clOrdId без префикса `trd` | `ValueError` |

Математика и поведение:

- M1. Формула ликвидации §1.3: сверка с таблицей инсайта
  (long 2x/mmr 0.4% → −49.8%; short 2x → +49.3%; 1x long → нет ликвидации).
- M2. `L_SL` §3.2: грид long 2x (10%, 25 ур., SL min×0.97) → ≈15.1% маржи;
  neutral 2x → ≈4.9% одной стороны; DCA-пример (794 USDT, SL 12%) → ≈134 USDT.
- M3. `sl_ratio = ceil(1.2 × L_edge / sz; 0.01)` в коридоре [0.03; 0.15].
- M4. `stop_plan`: `no_close_position` + бывший SL → команда `stopType 1`;
  повторный stop 2 по `no_close_position` — не провал.
- M5. Маппинг §6: каждый код → осмысленное действие (образец — `test_errors.py`).
- M6. Факты PROBE как регрессия: `stgyEq` входит в `totalEq`; позиции ботов
  отсутствуют в `account/positions`; funding дублируется (`type 8` + `fundingFee`).

Ручной demo-чеклист (не автотесты; исполнитель — OKX Trader по futures-bots.md §6.4):
`ops status` чист → `acctLv=2` → сверка demo/live цены < 0.5% → режим по §2 →
`liquidate-price`/`min-investment`/`grid-quantity` → `order_owner new trd` →
создание вне окна funding → повторный запрос (`details`+`positions`: `algoId`,
`liqPx`, `actualLever`, `eq`, `fundingFee`) → `totalEq` до/после в журнал §11 →
`sleeves.json`.

## 8. Критерий готовности модуля (для INSIGHTS-ALL-IMPL)

Датаклассы §2 + все проверки §3 + `liquidation_estimate` + `stop_plan` +
коды §6 в `errors.py`; `tests/test_futures_bot.py` зелёный (≥ 20 тестов);
`unittest discover` без регрессий; доки/доска обновлены оркестратором.
Первым делом после реализации — CFLEET-PROBE-повтор §9 п.5 (база `slRatio`).
