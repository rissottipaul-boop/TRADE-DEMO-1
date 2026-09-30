# Handoff f1_tags_1 — follow-up 1: owner-tag registry

- Дата: 2026-09-30. Агент: f1_tags_1 (teamwork), роль Insight Executor.
- Диспетч: `.agents/teamwork/f1_tags_1/DISPATCH.md` (follow-up 1).
- Первые чтения выполнены: AGENTS.md §2/§5/§6 · `src/order_owner.py` · reviewer_1 handoff («Owner-теги») · `src/carry_executor.py`, `src/mr_trader.py` + тесты реестра.

## Добавленные записи реестра

`src/order_owner.py`, формат существующих `Owner` соблюдён точь-в-точь:

- `CARRY = "botcar"` — `Owner(CARRY, "Funding Carry src/carry_executor.py", "Insight Executor", "strategy")`
- `MR = "botmr"` — `Owner(MR, "Mean Reversion src/mr_trader.py", "Insight Executor", "strategy")`

Оба `own=True` (корень `bot*`), `live=False`. Существующие записи не менялись.

## Важная находка: валидатор был строже реальности (исправлено)

`botcar` после `bot` продолжается hex-буквой `c`, и старый `validate_registry`
(проверял только первый символ хвоста) отклонил бы его как коллизию с clOrdId
движка (`bot` + uuid hex). Но коллизия невозможна: хвост `car` содержит не-hex
`r`, а движок ставит после `bot` только hex — `botcar…` он сгенерировать не
может. Валидатор уточнён до точного условия: коллизия, только если ВЕСЬ хвост
после родителя — hex (`bota` по-прежнему запрещён, `botcar` разрешён).
Докстринг-буллит про подвладельцев обновлён соответственно. Поведение для всех
старых кодов не изменилось.

## Файлы изменены (только разрешённые)

- `src/order_owner.py` — константы `CARRY`/`MR`, 2 записи `Owner`, точный hex-гейт валидатора + комментарий, буллит докстринга.
- `src/carry_executor.py` — `OWNER = order_owner.CARRY` (`"botcar"`), докстринг обновлён.
- `src/mr_trader.py` — `DEFAULT_OWNER = order_owner.MR` (`"botmr"`), докстринг обновлён.
- `tests/test_carry_executor.py` — `test_botcar_not_in_registry_repo_wins` → `test_botcar_registered_strategy` (presence: kind/own/OWNER).
- `tests/test_mr_trader.py` — `DEFAULT_OWNER`/`owner` ассерты `botr`→`botmr` (4 места), `test_owner_validation`: `botmr` больше не ValueError, а позитивная проверка + kind=strategy. Правки внесены скриптом `.agents/teamwork/f1_tags_1/fix_mr_tests.py` (файл в UTF-8 с BOM — только ASCII-замены с проверкой вхождения ×1, BOM и переводы строк сохранены, кириллица в комментариях цела).
- `tests/test_order_owner.py` — новый `test_validator_allows_tail_with_non_hex` (пинает уточнённую семантику: `botcar` ок, `bota` нет).

Зависимостей от `OWNER == "botr"` для этих двух модулей больше нет (поиск по
репо): в `test_carry_executor.py` остались только исторический комментарий и
строки-моки `botr1/botr2` в ответах MagicMock (не ассертятся, реестр их не
видит). Дефолт `OrderRouter` (`botr`) не тронут — соседние сьюты это подтверждают.

## Команды и точные результаты (из корня, `.venv\Scripts\python.exe`)

- `python -m unittest tests.test_order_owner tests.test_carry_executor tests.test_mr_trader` → **Ran 85 tests, OK** (31 реестр вкл. +1 новый, 25 carry, 29 mr).
- `python -m unittest tests.test_funding_carry tests.test_meanrev tests.test_mean_reversion_strategy` → **Ran 40 tests, OK**.
- `python -m unittest tests.test_order_router_exit tests.test_account_mode` → **Ran 70 tests, OK** (дефолт роутера `botr` цел).
- Санити реестра: `validate_registry() == []`; `OWNER=botcar`, `DEFAULT_OWNER=botmr`, оба kind=strategy; 5000 ID движка маппятся только в `bot` (коллизий нет); `new/owner_of` roundtrip `botcar`/`botmr` ок.

(Примечание: PowerShell оборачивает stderr-точки unittest в NativeCommandError —
статус «failed» в обёртке при фактическом `OK`; цифры выше — из вывода unittest.)

## Блокеры

Нет. Ордера/боты не ставились, секреты/лимиты/guard не тронуты.

## Наблюдения оркестратору (не блокеры, вне моего владения)

1. `ops/sleeves.json` не знает `botcar`/`botmr` — в PnL-ледежере они упадут в `bot`→engine (longest-prefix). Маппинг на рукава `funding_carry`/`signal` — решение оркестратора.
2. Строки TEAMWORK-M1/M2 в `ops/board.md` упоминают `OWNER=botr` — текст устарел после этого follow-up.
3. `bottrn` (treasury) из PROJECT.md не регистрировал — в диспетче только `botcar`/`botmr`.
