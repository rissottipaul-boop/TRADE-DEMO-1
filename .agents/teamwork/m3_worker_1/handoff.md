# Handoff — m3_worker_1: Idle Cash Earn controller (M3/R3)

Дата: 2026-09-30. Агент: m3_worker_1 (teamwork), роль Insight Executor.
Первые чтения по диспетчу выполнены: ORIGINAL_REQUEST.md (R3+AC),
AGENTS.md, insights/idle-cash-earn.md, src/treasury.py.

## Созданные файлы (только разрешённые)

- `src/treasury_exec.py` — контроллер поверх `src/treasury.py` (REUSE, без переписывания):
  - `BalanceSnapshot` + `free_cash(snapshot, reserve_pct)` — чистая функция
    `min(availBal, equity − frozenBal) − reserve_pct × equity`, сети нет;
  - `plan_sweep` — FreeCash ≥ 100 USDT → purchase-план (иначе hold), с totaleq-предупреждением (§4);
  - `plan_redeem` — план возврата под маржинальную нужду через `recall_for_margin`;
  - `attribute_yield` — атрибуция рукава `cash_earn` (данные, без записи в леджер);
  - `SimulatedEarnLedger` — demo-адаптер: purchase/redeem/accrue в памяти,
    каждая запись `simulated=True`; `should_simulate(50038)` → True.
  - `sweep_cycle` — связка «снимок → план → симулированное исполнение».
  - Ноль сетевых вызовов, ноль записей в `data/`/sleeves, ордеров нет (AGENTS.md §2 соблюдён).
- `tests/test_treasury_exec.py` — 21 тест, только моки/данные, ноль сети.

Прочее не тронуто.

## Команда и результат тестов

`.venv\Scripts\python.exe -m unittest tests.test_treasury_exec tests.test_treasury -v`

Результат: **Ran 29 tests — OK** (21 новый + 8 существующих `test_treasury`, регрессий нет).

## Покрытие AC

- R3 «опрос свободного баланса» → `free_cash` по переданному снапшоту (чистая функция, без сети). ✅
- R3 «подписка свободных средств в Flexible Earn» → `plan_sweep` (план-данные) + `SimulatedEarnLedger.purchase`
  (симуляция вместо заблокированного в demo 50038 вызова). ✅
- R3 «вывод по запросу» → `plan_redeem` + `SimulatedEarnLedger.redeem` (мгновенно, §3). ✅
- AC «unit-тесты с моками, сьют зелёный» → 21/21 зелёных; полный discover-сьют — за оркестратором. ✅ (частично)
- AC «движок не тронут» → движок не запускался/не останавливался, его файлов нет в изменениях. ✅

## Блокеры

Нет. Замечание: песочница запретила запись лога тестов в `.agents/teamwork/m3_worker_1/`
(UnauthorizedAccess) — вывод тестов зафиксирован в консоли выше; handoff.md записан тем же путём,
если запись разрешена, иначе handoff — это сообщение.
