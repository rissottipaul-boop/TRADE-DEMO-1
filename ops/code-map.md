# Карта кода и команды

Справочник к [AGENTS.md §6](../AGENTS.md#6-карта-кода-и-команды). Пути в таблицах и команды указаны относительно корня проекта; команды выполняются из него. Обязательные правила, права и проверки остаются в [AGENTS.md](../AGENTS.md).

| Модуль | Назначение |
| --- | --- |
| `src/config.py` | Настройки из `.env` (demo по умолчанию) |
| `src/connector.py` | CCXT-клиент OKX (`expTime` в заголовке), `emergency_stop` — отмена всех ордеров и остановка grid- и DCA-ботов |
| `src/errors.py` | Карта кодов ошибок OKX с действиями |
| `src/risk.py` | Риск-ядро: сайзинг, дневной лимит, max drawdown, блокировки, kill-switch |
| `src/engine.py` | Движок: WS public + private, реконсиляция, equity раз в 300 с, флаги `data/KILL` и `data/STOP_ENGINE` (текст флага — в лог), kill-switch через реестр `connector.register_kill_callback`, tdMode спота по режиму аккаунта (`account_mode.py`), faulthandler: дамп стеков в лог при зависании цикла > 150 с |
| `src/engine_watchdog.py` | Сторож движка (ENGINE-WATCHDOG): жив / тишина / мёртв по `data/engine.pid` (только процесс этого каталога, ENGINE-PID-GUARD) и свежести `logs/engine.log` (≤ 180 с); перезапуск через `ops\engine.ps1`, лог `logs/autostart.log` |
| `src/ws_client.py`, `src/storage.py`, `src/reconciler.py` | WS-клиент, SQLite-состояние движка, сверка с биржей |
| `src/order_router.py` | Выставление ордеров с риск-проверкой (используют стратегии); выход из позиции — `place_exit_order` или `is_exit=True` (ROUTER-EXIT), дочитывание неисполненных выходов — `settle_exits` |
| `src/account_mode.py` | Режим аккаунта (`acctLv`, autoLoan): tdMode спота и запрет скрытого займа — для роутера, движка, скриптов и live-preflight |
| `src/dca_bot.py` | Спот-DCA (demo) |
| `src/agent_context.py` | Адресное чтение доски для агентов (AGENT-TOKEN-ECONOMY): карточка задачи дословно, транзитивные зависимости, активные claims, дубликаты ID; разбор — `ops/hooks/autopilot.py`, без записи и сети |
| `src/control_panel.py` + `ops/control-panel/` | Локальный веб-пульт «Контур»: доска, рантаймы, движок, риск, очередь Muse, единая активность и курсор локальных событий; allowlist ops-действий, localhost + токен, без Flowise — [инструкция](control-panel/README.md) |
| `src/worktree_lease.py` | Резервирование задачи и изолированного Git worktree от чистого HEAD, heartbeat и инспекция регистрации; не запускает агента и не освобождает lease автоматически |
| `src/guard_adapter.py` | Адаптер hook-вызовов Codex / Gemini CLI / Muse Code к `ops/hooks/guard.py` (AGENT-GUARD-COMPAT): `apply_patch` в `command`, `cmd`/argv, вложенные вызовы, `BeforeTool`, пути WSL, корень с пробелами; ответ в формате клиента, правил не содержит. Hooks пока не вызывают: патч `insights/guard-compat-patch.diff` переносит его в `ops/hooks/` (решение человека, [guard-compat.md](../insights/guard-compat.md)) |
| `src/order_owner.py`, `src/order_audit.py` | Реестр владельцев ордеров (префикс clOrdId → владелец) и аудит «чей ордер» (только чтение) |
| `src/live_policy.py`, `src/live_preflight.py`, `src/live_runner.py` | Live-карман: окно и карман, предстартовая проверка (только чтение), runner рукава DCA (фоном — `ops\live.ps1`) |
| `src/pnl_ledger.py` + `ops/sleeves.json` | PnL по рукавам из bills (только чтение), отчёт за день или неделю; правила атрибуции; методика — `insights/pnl-ledger.md` |
| `src/backtest/` | Бэктестер: загрузка свечей, событийный движок с риск-ядром, walk-forward, anti-lookahead, отчёт (`insights/backtester-design.md`) |
| `src/pump_scanner.py` | Памп-сканер без LLM (PUMP-CODIFY): только публичные GET, закрытые 1H-свечи, фильтр скана №5; лента live по умолчанию (PUMP-FEED-LIVE): универсум — live топ-N по `volCcy24h` среди пар demo `public/instruments` (`state=live`), пара без данных (51001) — статус `no_data`, `--feed demo` — диагностика и повтор сканов №1–6; ликвидность кандидата (PUMP-LIQ): глубина `market/books`, спред и оборот против лимита позиции `pump-pocket.json` (только чтение), иначе статус `illiquid` с причиной, `--no-liquidity` — без проверки; строка `scan` в `data/pump_journal.jsonl`; повтор прошлого скана `--at` (без стакана — только оборот) |
| `src/pump_handoff.py` | Передача кандидата автоматического скана (PUMP-SCHED-HANDOFF). Берёт последнюю строку `scan` сканера. Если в ней есть свежий live-кандидат (не старше окна PUMP-STALE на момент передачи), PUMP-SCAN на доске переходит `scheduled` → `ready` с пометкой пары. Меняет только ячейки «Статус» и «Заметки» этой строки, идемпотентно, со сравнением перед записью. Занятые и человеческие статусы не трогает. Ордеров нет. Вызывается из `ops\pump_scan.ps1 run` после скана |
| `src/pump_journal.py` | Журнал памп-кармана (PUMP-JOURNAL): схема строк `entry`, `stop`, `exit`, `error` (v=1) рядом со строкой `scan` сканера; остаток бюджета, дневной PnL (сутки UTC), просадка, открытые позиции и риск до стопов против `pump-pocket.json` — только чтение |
| `src/obsidian_status.py` | Снимок состояния для пульта Obsidian (OBSIDIAN-STATUS, схема v2): движок и прогресс P1-72H, риск, флаги, памп-карман, дежурство ops (live-окно, карман, сторож, guard), история equity → `data/obsidian/status.json`; без сети, только чтение, запись атомарная |
| src/funding_carry.py | Дельта-нейтральный Funding Carry арбитраж (спот long + своп short 1x isolated, окупаемость 4 ног) — insights/funding-carry.md |
| src/mean_reversion.py | Сигнальная стратегия Mean Reversion (RSI(14) + BB(20, 2), ATR-стоп, minimal ROI, спот) — insights/meanrev-strategy-design.md |
| src/treasury.py | Казначейский модуль ликвидности (Idle Cash Earn: Simple Earn, буфер резерва, мгновенный возврат под маржу) — insights/idle-cash-earn.md |
| src/grid_engine.py | Собственная grid-стратегия с жестким Stop-Out при выходе из диапазона и перестановкой уровней — insights/grid-strategy-design.md |
| src/futures_bot.py | Контрактный бот-раннер: плечо <= 2x, tdMode isolated, аварийное закрытие stopType 1 — insights/futures-bots.md |
| src/copy_trader.py | Менеджер OKX Copy Trading / Lead Bots: профит-шеринг 30%, аудит условий лида — insights/copy-trading.md |
| ops/deploy-vps.ps1, ops/deploy-vps.sh | Скрипты развертывания 24/7 VPS, проверка NTP/латентности, бэкап баз SQLite — insights/deploy-vps.md |
| `src/netdata_monitor.py` | Модуль проверки здоровья и метрик Netdata (NETDATA-MONITOR): опрос `/api/v1/info`, сбор версии, ядер CPU, алертов (критичные/предупреждения); CLI `python -m src.netdata_monitor` — [insights/netdata-monitoring.md](../insights/netdata-monitoring.md) |
| `ops/netdata/docker-compose.yml`, `ops/netdata.ps1` | Инфраструктура мониторинга Netdata: локальный docker-compose сервиса `netdata`, PowerShell-скрипт управления (`start`, `stop`, `restart`, `status`, `url`, `logs`) на порту 19999 |
| Дубли консолидированы (ARCH-DEDUP): `state.py` и `ws_public.py` удалены, канон — `storage.py` + `ws_client.py` | |

| Что | Команда |
| --- | --- |
| Все unit-тесты (без сети) | `.venv\Scripts\python.exe -m unittest discover -s tests -t .` |
| Самотесты модулей | `python -m src.risk_selftest`, `src.order_router_selftest`, `src.dca_selftest` |
| Smoke-тест demo | `python -m src.smoke_test` |
| Движок фоном | `ops\engine.ps1 start` / `status` / `stop` — трогают только движок этого каталога (чужой PID: предупреждение, pid-файл удаляется); `start` сохраняет старые `engine.log` и `engine.log.out` в `logs\engine_<время>.*` |
| Автозапуск и сторож движка | `ops\autostart.ps1 status` (1 — нет хотя бы одной из задач «OKX-Bot Engine», «OKX-Bot Watchdog»); `register` / `unregister` — только человек; пауза обеих без удаления — файл `data\AUTOSTART_OFF`, лог `logs\autostart.log` |
| Проверка сторожа | `python -m src.engine_watchdog [--json]` — только вердикт: 0 — делать нечего, 1 — нужен перезапуск, 2 — ошибка; `ops\autostart.ps1 watch` — как по расписанию: повтор через 90 с, `engine.ps1 stop` + `start`, не больше 3 перезапусков в час |
| Риск и метрики | `python -m src.ops status` |
| Аварийная остановка торговли | `python -m src.ops kill "причина"` |
| Биржа (только demo) | `okx --demo <module> <action> …`, скиллы в `.agents/skills/` |
| Чьи ордера на счёте | `python -m src.order_audit [--hours 24]` — 0: норма, 1: есть ордер без метки агента или кода, 2: ошибка чтения |
| Реестр и новый clOrdId | `python -m src.order_owner`, `python -m src.order_owner new trd` |
| PnL по рукавам | `python -m src.pnl_ledger [--date YYYY-MM-DD] [--days 7] [--mode live]` — 0: норма, 1: есть предупреждения, 2: отчёт не построен |
| Бэктест | `python -m src.backtest baseline …` / `run …` (публичные свечи OKX, без ключей); mean-reversion — `python -m src.backtest.meanrev --out insights/meanrev-backtest.md` |
| Режим аккаунта | `python -m src.account_mode status` (только чтение), `precheck --acct-lv N` (только чтение), `switch --acct-lv N` (только demo; при блокерах precheck не переключает) |
| Памп-скан (без ключей) | `python -m src.pump_scanner --exclude <базы флота> [--json] [--no-journal]`; повтор сканов 24.09: `--feed demo --at 2026-09-24T09:47+05:00 --pairs OKB-USDT …` — 0: скан выполнен, 2: ошибка данных или сети |
| Передача памп-кандидата | `python -m src.pump_handoff [--dry-run] [--json]`; сухой прогон по расписанию — `ops\pump_scan.ps1 check`. 0 — решение принято (передано или передавать нечего); 2 — журнал или доска не читаются, строка PUMP-SCAN не разбирается. В Планировщике ошибка передачи — код 3 |
| Остатки памп-кармана | `python -m src.pump_journal [--json]` — 0: вход разрешён, 1: входов нет (лимит, позиции, позиция без стопа), 2: карман или журнал не читаются |
| Ротация AI-агента | `ops\agent-rotate.ps1` — авто (Claude → Antigravity → Muse → Codex), `-Role <роль>` (приоритеты и модели из `ops/agent-routing.json`), `-TaskId`, `-SkipAgent`, `-Plan`, `-Headless`, `-Prompt "..."` — [agent-team.md](agent-team.md) |
| Адресное чтение доски | `python -m src.agent_context` — обзор без критериев; `--task <ID>` — полная карточка, зависимости, все активные claims; `--stats` — байты до/после. 0 — ок, 2 — доску читать напрямую. Правила — [token-economy.md](token-economy.md) |
| Локальная панель | `.venv\Scripts\python.exe -m src.control_panel [--port 8765]` — ссылка с токеном, только 127.0.0.1; [действия и API](control-panel/README.md) |
| Делегирование Muse | `ops\delegate.ps1 status / start / stop / submit -Prompt "…" -From <рантайм> -Role <роль> / fetch -Id <id>` — [delegations/README.md](delegations/README.md) |
| Управление Netdata | `ops\netdata.ps1 status` / `start` / `stop` / `restart` / `url` / `logs` — управление локальным контейнером Netdata (порт 19999) |
| Метрики Netdata | `python -m src.netdata_monitor [--json]` — опрос API Netdata (/api/v1/info), статус, ядра CPU, алерты |

| Файл | Назначение |
| --- | --- |
