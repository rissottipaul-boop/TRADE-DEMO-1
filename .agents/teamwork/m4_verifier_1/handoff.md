# M4 Verifier handoff — Bot Fleet 50 (R4) audit

- Агент: m4_verifier_1 · Дата: 2026-09-30
- Объект: `src/fleet_manager.py` (аудит, read-only по умолчанию)
- Движок не трогал, ордеров/ботов на бирже не создавал (только `--help` и чтение исходника CLI).

## Вердикты по AC

| # | Проверка | Вердикт | Доказательство |
|---|----------|---------|----------------|
| 1 | Каталог 13 типов | MET | `BOT_CATALOG` — ровно 13 ключей (`src/fleet_manager.py:49-206`), `get_supported_bot_types()` — 13; `test_catalog_size_and_keys`, `test_fleet_ac.TestFleetAcCatalog` зелёные |
| 2 | Квота MAX_ACTIVE_BOTS=50 | MET | `FLEET_MAX_BOTS=50`, `risk.MAX_ACTIVE_BOTS=50`; `validate_fleet_quota(50,1)` → отказ; тесты квот зелёные |
| 3 | ≤2% equity на бота | MET | `risk.MAX_BOT_INVESTMENT_PCT=2.0`; `calculate_bot_allocation(108000,50)`: max_single=2160.0, бюджет ≤70%; тесты зелёные |
| 4 | Резерв 30% | MET | `reserve_usdt = equity*0.30` (32400.0 при 108000); тесты зелёные |
| 5 | Плечо ≤3x isolated | MET | `MAX_LEVERAGE=3`, clamp `min(lever,3)` в contract_grid×2 и contract_dca; тесты `lever 10→3`, `5→3` зелёные. Isolated: у `bot grid/dca create` флага маржи нет (проверен `--help`), изоляция — strategy account по дизайну OKX (CFLEET-PROBE: `okx-api.md` — mgnMode cross внутри счёта бота, убыток ограничен маржой бота) |
| 6 | Обязательный SL | MET ПОСЛЕ ФИКСА | Было: сетки строились без SL вопреки docstring. Стало: `spot_grid`/`contract_grid_*` без `slTriggerPx` → `ValueError`; DCA всегда несут `--slPct` (дефолт 0.15). Регресс: `TestFleetAcMandatoryStop` |
| 7 | Изоляция рукава `demo_fleet` | MET | `ops/sleeves.json`: `bot_algo_cl_ord_id_prefixes` `trd→demo_fleet`, `flt→demo_fleet`; `tests.test_pnl_ledger` зелёные |
| 8 | Batch kill | MET (уровень connector) | `connector.emergency_stop` → `stop_grid_bots` + `stop_dca_bots` (чанки, ретраи, фильтр `algo_ids`); kill-сьюты зелёные (в составе 136). В самом `fleet_manager.py` stop-билдера нет — по дизайну, kill живёт в connector/risk |
| 9 | ORDER-OWNER-TAG `trd` на `build_cli_command` | MET ПОСЛЕ ФИКСА | Было: iceberg/twap молча теряли метку (генерировали id и не вставляли). Стало: все 7 поддерживаемых команд несут `trd*` id (боты — `--algoClOrdId`, algo place — `--clOrdId` по AGENTS.md §6; приём `--clOrdId` подтверждён исходником CLI: `handleSpotAlgoCommand → cmdSpotAlgoPlace(... clOrdId: v.clOrdId ...)`). Регресс: `TestFleetAcOwnerTag` |
| 10 | R4 AC: «запуск и аудит … между 13 типами в лимите 50» | ЧАСТИЧНО (gap) | Каталог 13/13, аудит капитала да, запуск через CLI — 7/13 (grid×3, dca×2, iceberg, twap). Остальные 6 (`smart_portfolio`, `smart_arbitrage`, `dcd_pendulum`, `recurring_buy`, `signal_bot`, `arbitrage`) запустить через CLI нельзя: в `okx bot` только `grid`/`dca` (проверены `--help` и исходник `handleBotCommand`). Было: молча возвращалась нерабочая команда `okx bot <other> create`. Стало: честный `ValueError`. Разблокировка — внешняя (расширение CLI / ручная процедура), см. Блокеры |

## Тесты и результаты

- `.venv\Scripts\python.exe -m unittest tests.test_fleet_manager` → **14/14 OK** (до и после фиксов)
- `.venv\Scripts\python.exe -m unittest tests.test_fleet_manager tests.test_fleet_ac` → **26/26 OK**
- `.venv\Scripts\python.exe -m unittest tests.test_connector_kill_grid tests.test_connector_kill_dca tests.test_pnl_ledger tests.test_order_owner` → **136/136 OK** (`PYEXIT=0`)
- `.venv\Scripts\python.exe -m unittest discover -s tests -t .` → **996 тестов, failures=38, errors=6** — все в `test_agent_rotate`, `test_guard`, `test_guard_adapter`, `test_engine_watchdog`, `test_pump_sched`, `test_mr_trader`; **ни одного fleet-теста**. Совпадает с зафиксированным на доске предсуществующим набором песочных красных (INSIGHTS-ALL-IMPL, 30.09: «38+6 … песочница»). Мои правки их вызвать не могут: прод-код `fleet_manager` не импортирует никто (проверено поиском — только два тестовых файла), `build_cli_command` вызывается только из тестов.

## Фиксы (минимальные, в `src/fleet_manager.py`)

1. Iceberg/TWAP: добавлен `"--clOrdId", cl_ord_id` (метка `trd` больше не теряется).
2. Сетки: `spot_grid`/`contract_grid_usdt`/`contract_grid_coin` без `slTriggerPx` → `ValueError` (спека `bot-fleet-50.md` §1 п. 4, docstring это уже обещал).
3. Fallback для 6 типов без CLI-запуска: вместо нерабочей команды — `ValueError` с объяснением.
4. Сопутствующее: вход `test_build_contract_grid_leverage_limit` дополнен `slTriggerPx` (ассерт плеча не менялся); новый `tests/test_fleet_ac.py` — 12 AC/регресс-тестов, без сети.

## Блокеры / открытые gap

1. **Запуск 6/13 типов через CLI невозможен** (ограничение `okx` CLI: `bot` знает только `grid`/`dca`; DCD частично покрыт `earn dcd`, но это не бот-флоу `build_cli_command`). Требуется решение: ручная процедура запуска + аудит через каталог, либо расширение CLI. Модуль теперь честно отказывает вместо выдачи битой команды.
2. **Полный discover красный (38+6)** — предсуществующие, не fleet, в основном песочные пути (`\\?\`, WSL) и `test_mr_trader`. Не блокирует R4, но блокирует AC «регрессионный сьют 100%».
3. Файлы `src/fleet_manager.py` / `tests/test_fleet_manager.py` менялись сегодня в 10:27 — активных claims на них на доске нет (проверены `in-progress`: AGENT-PANEL-RUNS, P1-72H, ERRORS-TRADFI — чужие файлы), мои правки claim-конфликта не создают.
