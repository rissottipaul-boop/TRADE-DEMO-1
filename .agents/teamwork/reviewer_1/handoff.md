# Handoff reviewer_1 — review gate M1–M4

- Дата: 2026-09-30. Агент: reviewer_1 (teamwork), роль Reviewer + Challenger.
- Диспетч: `.agents/teamwork/reviewer_1/DISPATCH.md`.
- Первые чтения выполнены: ORIGINAL_REQUEST.md (R1–R4 + AC) · AGENTS.md §2/§5/§6/§7 · handoffs m1/m2/m3/m4 · `src/carry_executor.py`, `src/mr_trader.py`, `src/treasury_exec.py`, `src/fleet_manager.py` · тесты `test_carry_executor`, `test_mr_trader`, `test_treasury_exec`, `test_fleet_ac` · точечно `src/order_owner.py`, `src/order_router.py`, `src/funding_carry.py`, `src/treasury.py`, `ops/sleeves.json`, `insights/meanrev-backtest.md`, PROJECT.md оркестратора.
- Код НЕ правился: только чтение + прогон тестов. Изменений в дереве от reviewer_1 нет (этот файл — единственный записанный).

## Прогон тестов (все цифры перепроверены лично)

| Сьют (команда из корня) | Заявлено воркером | Факт |
|---|---|---|
| `unittest tests.test_carry_executor tests.test_funding_carry` | OK, 36/36 | **Ran 36, OK** ✅ |
| `unittest tests.test_carry_executor` (один) | 25 новых | **Ran 25, OK** ✅ |
| `unittest tests.test_mr_trader tests.test_mean_reversion_strategy tests.test_meanrev` | Ran 58, OK | **Ran 58, OK** ✅ |
| `unittest tests.test_mr_trader` (один) | 29 новых | **Ran 29, OK** ✅ |
| `unittest tests.test_treasury_exec tests.test_treasury` | Ran 29, OK | **Ran 29, OK** ✅ |
| `unittest tests.test_treasury_exec` (один) | 21 новый | **Ran 21, OK** ✅ |
| `unittest tests.test_fleet_manager tests.test_fleet_ac` | 26/26 OK | **Ran 26, OK** ✅ |
| `unittest tests.test_fleet_ac` / `tests.test_fleet_manager` (по одному) | 12 / 14 | **Ran 12, OK / Ran 14, OK** ✅ |
| `unittest tests.test_connector_kill_grid tests.test_connector_kill_dca tests.test_pnl_ledger tests.test_order_owner` | 136/136 OK | **Ran 136, OK** ✅ |
| `unittest discover -s tests -t .` (полный) | 996, failures=38, errors=6 (M4); 996/44 (M1) | **Ran 1025, failures=38, errors=6** — все 44 в `test_agent_rotate`, `test_guard`, `test_guard_adapter`, `test_engine_watchdog`, `test_pump_sched`; **ноль FAIL/ERROR по carry/mr_trader/treasury/fleet** (проверено вторым прогоном с фильтром) ✅ |

Примечания: 1025 vs 996 — дерево выросло между прогонами (параллельные воркеры добавляют тесты). Наблюдение M4 «test_mr_trader красный в discover» у меня НЕ воспроизвелось ни в одном из двух полных прогонов — вероятно, гонка с параллельной записью файла на момент прогона M4. AC «регрессия 100%» остаётся красным по предсуществующим песочным зонам (ps1-обёртки, `\\?\`/WSL-пути) — вне скоупа M1–M4.

## Проверка запрещённых действий (все 4 модуля + 4 тест-файла)

- Поиск по `src/carry_executor.py`, `src/mr_trader.py`, `src/treasury_exec.py`, `src/fleet_manager.py`: ноль совпадений `requests|urllib|subprocess|os.system|Popen|SECRET|API_KEY|src.engine|import engine|risk.init|MAX_OPEN_POSITIONS =|DEFAULT_RISK_PCT =`. Сети нет, секретов нет, движок не импортируется, лимиты риска не переназначаются, guard-периметр не тронут.
- Записи на диск: только упоминания в комментариях/docstring («НЕ пишет», «register_entry НЕ зовём»); реальных `open/write/data//sleeves/json.dump/sqlite` нет.
- Ордера/боты на бирже: M1 — только `dry_run=True` по умолчанию + моки (`place_pair` требует явного `dry_run=False`); M2 — только dry-run трассировка + FakeRouter; M3 — ордеров нет по дизайну, in-memory леджер; M4 — только `--help` и чтение исходников, `build_cli_command` лишь строит строки команд. Живых выставлений не было — честно заявлено всеми.
- Тесты: `place_order` встречается только в FakeRouter (мок); `okx --demo` — только строковые ассерты строящихся команд, исполнения нет (subprocess отсутствует).
- Файлы вне владения: прод-код `fleet_manager` импортируют только два тестовых файла, `build_cli_command` зовётся только из тестов (проверено поиском по `src/`, `tests/`, `ops/` упоминание в `ops/board.md` — только текст доски). Остальные модули аддитивны. Нарушений владения не найдено.

## Owner-теги: реестр побеждает PROJECT.md — воркеры правы

- PROJECT.md оркестратора предлагает `botcar`/`botmr`/`bottrn`/`flt` — ни `botcar`, ни `botmr` в `src/order_owner.py` НЕ зарегистрированы (реестр: `bot`, `botr`, `botsdca`, `bottdca`, `botldca`, `lt`, `rt`, `smk`, `wsd`, `trd`, `pmp`, `sen`, `iex`, `hnt`, `usr`).
- `src/order_router.py:200,255,375` вызывает `order_owner.require(owner, own=True)` → незарегистрированный код = ValueError до обращения к бирже. Выбор M1/M2 `OWNER = "botr"` (ROUTER, «стратегия без своего владельца») — единственно корректный; зафиксирован тестами (`test_botcar_not_in_registry_repo_wins`, `test_owner_validation` — оба зелёные в моих прогонах).
- M4 `trd` для `build_cli_command`: корректно для CLI-пути (AGENTS.md §6), консистентно с `ops/sleeves.json:25-28` (`trd→demo_fleet`, `flt→demo_fleet`). Флаг `--clOrdId` для iceberg/twap vs `--algoClOrdId` для ботов — по AGENTS.md §6 (проверка `--help` CLI мне была недоступна: песочница блокирует запуск `okx.ps1` политикой исполнения; вместо этого сверил с AGENTS.md §6, где задокументированы ровно те же три семейства команд — grid/dca/algo place — что косвенно подтверждает и gap 6/13).
- Follow-up оркестратору (не блокер): зарегистрировать `botcar`/`botmr`/`bottrn` (kind=strategy) в реестре, затем переключить OWNER в M1/M2. До этого `botr` остаётся правильным.

## Вердикты по milestone

### M1 (Funding Carry, R1) — PASS-WITH-NOTES
- AC «4 ноги комиссий»: честно — математика переиспользована из `funding_carry.roundtrip_cost` (`(TAKER_SPOT + TAKER_SWAP) * 2`, docstring «round-trip 4 ног»), execution layer добавляет гейт breakeven vs планового удержания + `funding_exit_signal`. ✅
- AC «парные ордера в demo»: код (`build_pair` + `place_pair` через OrderRouter с owner-тегом) готов и покрыт моками; живая demo-пара НЕ выставлялась. Воркер честно маркирует ⏳ с двумя внешними причинами: `validate_legs` требует acctLv 2 (подтверждено `src/funding_carry.py:95-96`, известный ACCT-MODE-DERIV) + пара заняла бы оба слота риск-ядра у P1-72H. Это disclosed-ограничение, а не overstated AC.
- Дельта-нейтральность: спот подгоняется как `контракты × ctVal` (точное равенство базовых количеств) — разумно; канон «контракт = 1 лот» из `risk_sim.py` применён осознанно.
- Notes: (1) живая demo-пара — follow-up после ACCT-MODE-DERIV отдельным допуском; (2) isolated-margin 1x своп-ноги — средствами счёта, не параметром ордера (воркер предупредил — сверить leverage/margin перед живым прогоном). Блокеров для E2E в скоупе M1 нет.

### M2 (Mean Reversion, R2) — PASS-WITH-NOTES
- AC «сигналы 1H → `src/risk.py` → `src/order_router.py»`: закрыт кодом — `risk.check_entry_allowed` (`mr_trader.py:159`), `risk.size_position` (:171), `router.place_order` (:198) / `place_exit_order` (:255) / `settle_exits` (:137). Сигналы делегированы `mean_reversion`/`backtest.meanrev`, не скопированы (тесты `DelegationTest` зелёные).
- Live-гейт дефолтов РЕАЛЬНО enforced в коде: `mr_trader.py:192-197` — `if self.params.is_default(): blocked/default_params_live` стоит ПОСЛЕ сайзинга и ДО `place_order`; тест `test_live_default_blocked_no_placement` доказывает `router.entries == []` и отсутствие позиции. Цифры-причина (`net −17.96%`, PF 0.54) дословно подтверждены `insights/meanrev-backtest.md:14`. Направление безопасности верное: `is_default()` смотрит 5 полей — смена только `fee_pct`/`risk_pct`/`exit_price_guard` гейт НЕ снимает.
- Dry-run не мутит риск-ядро: `register_entry` не зовётся (тест `entries_today == 0` зелёный).
- Notes: (1) demo/live-доказательство невозможно по дизайну до MEANREV-CALIB — честный гейт, а не пробел; калиброванный live-путь покрыт FakeRouter-тестами (`test_live_calibrated_places`, `test_live_exit_places`); (2) наблюдение M4 о красном `test_mr_trader` в discover не воспроизвелось (см. таблицу). Блокеров для E2E в скоупе M2 нет.

### M3 (Idle Cash Earn, R3) — PASS
- Все три R3-AC закрыты в пределах, которые demo позволяет: `free_cash` (чистая функция снапшота — воркер честно фиксирует, что опроса сети нет, снимок поставляет вызыватель), `plan_sweep` + `SimulatedEarnLedger.purchase`, `plan_redeem` + `redeem` (мгновенно).
- Замена реальных Earn-вызовов симуляцией обоснована платформой: `DEMO_BLOCKED_CODE = "50038"` + `is_demo_blocked` в `src/treasury.py:20,62-64` (инсайт §1). Каждая запись леджера `simulated=True`; totaleq-предупреждение (§4) и лаг ~2ч переиспользованы, не переписаны.
- Ноль сети, ноль записей в `data/`/sleeves, ордеров нет — подтверждено поиском. Тесты 21/21 + соседи 8/8, под discover чисто.
- Notes (не блокеры): атрибуция `cash_earn` — только данные, в леджер не пишется (заявлено явно); исполнение на live — человек/OKX Trader через CLI.

### M4 (Bot Fleet 50, R4) — PASS-WITH-NOTES
- Каталог 13/13, квота 50, ≤2%/резерв 30%, плечо clamp ≤3x, `trd`-метка на всех 7 строящихся командах, обязательный SL — всё подтверждено чтением кода (`fleet_manager.py:49-206, 228-264, 305/345, 375/390, 283-284/303-304`) и зелёными тестами (26/26 + 136/136 соседей, все перепроверены).
- Фиксы минимальны и безопасны: (1) `--clOrdId` в iceberg/twap, (2) ValueError сеток без SL, (3) ValueError вместо нерабочей команды для 6 типов, (4) вход существующего leverage-теста дополнен `slTriggerPx` (ассерт не менялся). Все правки внутри `build_cli_command`; каталог/квота/аллокация не тронуты; blast radius = только тесты (импортёров прод-кода нет — проверено поиском). Каждый фикс меняет молча-неверное поведение на громкий отказ — направление в сторону безопасности.
- Gap 6/13 честно очерчен: `smart_portfolio`, `smart_arbitrage`, `dcd_pendulum`, `recurring_buy`, `signal_bot`, `arbitrage` честно бросают ValueError вместо битой команды; регресс-тест `TestFleetAcUnsupportedLaunch` зелёный. Прямую проверку `okx bot --help` выполнить не смог (песочница), но AGENTS.md §6 документирует ровно grid/dca/algo-place семейства команд — независимое подтверждение границы CLI.
- Notes (не блокеры, внешние): (1) R4-AC «запуск … между 13 типами» частично: запуск через CLI — 7/13, остальные 6 — каталог+аудит, запуск вручную или расширение CLI (решение за оркестратором/человеком); это ограничение платформы, модуль ведёт себя правильно; (2) batch kill — на уровне connector (kill-сьюты зелёные в составе 136), в `fleet_manager.py` stop-билдера нет по дизайну — принято; (3) рукав `demo_fleet` для `trd`/`flt` подтверждён `ops/sleeves.json:25-28`; (4) isolated-margin для контрактных сеток — через strategy account по дизайну OKX (флага маржи у CLI нет) — зафиксировать процедурой перед живым запуском флота.

## Блокирующие замечания

Нет. VETO не выдано ни по одному milestone — E2E может идти по всем четырём скоупам с учётом notes выше (M1: без живой пары до ACCT-MODE-DERIV; M2: live только калиброванный; M4: CLI-запуск 7/13 типов).

## Follow-up оркестратору (сводно, не блокеры)

1. Зарегистрировать `botcar`/`botmr` (и `bottrn` при нужде) kind=strategy в `src/order_owner.py`, затем переключить OWNER в M1/M2.
2. CARRY-DEMO-ACCRUAL после ACCT-MODE-DERIV (живая demo-пара carry отдельным допуском, не занимая слоты P1-72H).
3. MEANREV-CALIB → решение о demo-прогоне калиброванного `MRTrader(dry_run=False)` отдельным допуском.
4. Решение по 6/13 типам флота: ручная процедура запуска + аудит через каталог, либо расширение `okx` CLI.
5. AC «регрессия 100%»: 38+6 красных — предсуществующие песочные зоны, вне M1–M4; чинить отдельно.
