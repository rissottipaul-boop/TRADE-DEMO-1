# Handoff f1_verify_1 — перепроверка follow-up 1 (owner-tag registry)

- Дата: 2026-09-30. Агент: f1_verify_1 (teamwork), read-only + тесты.
- Проверялся handoff: `.agents/teamwork/f1_tags_1/handoff.md`. Код не правился.

## Вердикт: CONFIRMED

Все заявления F1 сошлись с прогоном, расхождений нет.

## Прогон (из корня, `.venv\Scripts\python.exe`)

1. `python -m unittest tests.test_order_owner tests.test_carry_executor tests.test_mr_trader` → **Ran 85 tests, OK** (совпало с 85).
2. `python -m unittest tests.test_funding_carry tests.test_meanrev tests.test_mean_reversion_strategy` → **Ran 40 tests, OK** (совпало с 40).
3. `python -m unittest tests.test_order_router_exit tests.test_account_mode` → **Ran 70 tests, OK** (совпало с 70).

(Примечание: обёртка PowerShell возвращает exit 1 + NativeCommandError на stderr-точках unittest при фактическом `OK` — тот же артефакт, что описан в handoff F1.)

## Санити реестра

- `validate_registry() == []` — подтверждено.
- `CARRY == "botcar"`, `MR == "botmr"`; `carry_executor.OWNER == "botcar"`, `mr_trader.DEFAULT_OWNER == "botmr"` — подтверждено.
- `botcar`/`botmr`: kind=strategy, own=True (корень `bot*`), live=False — подтверждено.
- roundtrip: `new_cl_ord_id('botcar'/'botmr')` → 32 символа, `owner_of` возвращает `botcar`/`botmr`, `is_owned_by` True — подтверждено.
- Записей в `OWNERS`: 17 (15 старых + 2 новые); старые записи не менялись (по чтению `src/order_owner.py`).

## Блокеры

Нет.
