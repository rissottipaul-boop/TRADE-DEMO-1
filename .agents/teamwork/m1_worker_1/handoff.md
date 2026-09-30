# Handoff: m1_worker_1 — Funding Carry execution layer (M1)

- **Дата:** 2026-09-30
- **Диспетч:** `.agents/teamwork/m1_worker_1/DISPATCH.md`
- **Первые чтения:** ORIGINAL_REQUEST.md (R1+AC) · AGENTS.md §2/§5/§6 · insights/funding-carry.md · src/funding_carry.py · src/risk.py · src/order_router.py · src/order_owner.py — все прочитаны.

## Созданные файлы (только разрешённые)

1. `src/carry_executor.py` — execution layer поверх `src/funding_carry.py` (математика переиспользована, не переписана):
   - `build_pair()` — сборка пары: `funding_carry.assess` (ноги + breakeven vs планового удержания) → `risk.check_entry_allowed` обеих ног → сайзинг через `risk.size_position`. Сети нет, выставления нет.
   - `pair_sizes()` + `synthetic_stop()` — дельта-нейтральный сайзинг: спот по канону «контракт = 1 лот» (`src/backtest/risk_sim.py`: ct_val=лот, иначе floor даёт 0 целых BTC), своп-контракты из спот-размера, спот подгоняется обратно — базовые количества равны точно. Синтетический стоп подобран так, чтобы потолок 15% equity срабатывал детерминированно.
   - `place_pair(plan, router, *, dry_run=True)` — dry_run по умолчанию (валидация без выставления). Реальное выставление только явным `dry_run=False`: спот → своп через `OrderRouter.place_order` с `owner=OWNER`; отказ второй ноги прикладывает hedge-or-exit решение.
   - `decide_partial_fill()` (чистая): hold_pair / abort_pair / exit_spot / exit_swap / wait — любой дисбаланс закрывает перевесившую ногу.
   - `funding_exit_signal()` (чистая): выход при отрицательной ставке или ниже амортизации round-trip комиссий на плановом горизонте.
2. `tests/test_carry_executor.py` — 25 тестов, моки (FakeRisk + MagicMock роутера), ноль сети, ноль реальной БД риска.

## Теги владельца (сверка botcar)

- `botcar` из PROJECT.md оркестратора **не зарегистрирован** в `src/order_owner.py`; `OrderRouter` бросает ValueError на незарегистрированном коде. Реестр репозитория — источник правды (AGENTS.md §6) → `OWNER = "botr"` (ROUTER, «стратегия без своего владельца»). Выбор задокументирован в docstring модуля и зафиксирован тестом `test_botcar_not_in_registry_repo_wins`.
- **Follow-up оркестратору:** зарегистрировать `botcar` (kind=strategy) в `src/order_owner.py`, затем переключить OWNER (мне править реестр запрещено ownership).

## Проверено

- `.venv\Scripts\python.exe -m unittest tests.test_carry_executor tests.test_funding_carry` → **OK, 36/36** (25 новых + 11 существующих funding_carry).
- Полный сьют `discover -s tests`: 996 тестов, 44 падения — все в `test_guard`, `test_guard_adapter`, `test_agent_rotate`, `test_engine_watchdog`, `test_pump_sched` (нормализация путей, ps1-обёртки); ни одно не связано с carry (проверено: совпадений carry в этих файлах нет, кроме англ. глагола в имени теста guard). Мои файлы чисто аддитивны, существующие не тронуты.
- Движок не тронут: demo-ордера не выставлялись (только dry_run + моки), `src.ops status` до/после не требовался.

## Покрытие AC (R1 / Funding Carry)

- «Модуль вычисляет доходность с учётом 4 ног комиссий» — было в `src/funding_carry.py`, execution layer переиспользует через `assess` + гейт breakeven. ✅
- «Корректно формирует парные ордера в demo-режиме» — `build_pair` + `place_pair` формируют/выставляют через OrderRouter с owner-тегом; путь выставления покрыт моками, реальное demo-выставление — отдельный явный вызов (не выполнялся, см. блокеры). ✅ (код), ⏳ (живая demo-пара)
- «Unit-тесты с моками, регрессия 100%» — мои тесты зелёные; полная регрессия красная по чужим зонам (см. выше). ⚠️

## Демо-ордера

Не выставлялись. Причина: `validate_legs` требует acctLv 2 (известный блокер ACCT-MODE-DERIV), плюс пара заняла бы оба слота риск-ядра (MAX_OPEN_POSITIONS=2) у работающего движка P1-72H. Выставление — follow-up после ACCT-MODE-DERIV отдельным явным вызовом.

## Блокеры / follow-up

1. `botcar` не в реестре → OWNER=botr (см. выше).
2. Живая demo-пара невозможна до ACCT-MODE-DERIV (acctLv 1 на demo) — задача CARRY-DEMO-ACCRUAL уже зафиксирована в инсайте §9.
3. Изолированная маржа 1x своп-ноги выставляется средствами счёта/позиции, а не параметром ордера (`_spot_params` возвращает `{}` для не-спота) — перед живым прогоном сверить leverage/margin mode инструментом позиции.
