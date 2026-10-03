# Morphy: локальная проверка для общего пульта OKX-проекта

**Статус:** локальный smoke пройден; AI настроен человеком; read-only экран проекта установлен. Авторизованный экран подтверждён в Opera после входа человека (запись в `MORPHY-VERIFY-LOGIN`); мобильная ширина не проверена. `MORPHY-PROJECT-VIEW` закрыта 03.10 07:42. Guard проекта подключён к AI-агенту Morphy (`MORPHY-GUARD-WIRE`, 03.10 09:10); harness Codex (provider `openai`) мостом не покрыт.
**Дата:** 03.10.2026, Asia/Qyzylorda (+05:00).
**Исполнитель:** Insight Executor / Codex.

## Результат

### Guard подключён к AI-агенту Morphy (MORPHY-GUARD-WIRE, 03.10 09:10)

До этой задачи действия AI-агента Morphy не проходили через общий guard:
harness Claude запускает Claude Agent SDK с `permissionMode: 'bypassPermissions'`
и `allowDangerouslySkipPermissions: true`, `cwd` — workspace Morphy (не корень
проекта), `settingSources` не задан — проектный `.claude/settings.json` с hook
guard SDK не загружал. Pi-harness исполняет инструменты прямо в Node
(`executeTool` → `tool.run(...)`) без какой-либо точки подтверждения.

Фактические точки подключения найдены и использованы, периметр не менялся:

- Установленный `@anthropic-ai/claude-agent-sdk` 0.3.220 поддерживает
  `Options.hooks.PreToolUse` — in-process callback, возвращающий
  `hookSpecificOutput.permissionDecision` (тот же формат, в котором отвечает
  `ops/hooks/guard_adapter.py` клиенту claude); по типам SDK deny хука
  обходит `canUseTool` и действует и при `bypassPermissions`.
- В pi-harness выбран `executeTool` (`supervisor/harnesses/pi/session.ts`) —
  единственная горловина всех инструментов pi.

Реализация: мост [`ops/morphy/guard-hook.mjs`](../ops/morphy/guard-hook.mjs)
спавнит `.venv` Python с `ops/hooks/guard_adapter.py --client claude`
(новый `--client morphy` не требуется: инструменты обоих harness — Read/Write/
Edit/Bash — зеркалят формат Claude Code). Правил в мосте нет; fail-open по
контракту guard. [`ops/morphy/deploy-guard.ps1`](../ops/morphy/deploy-guard.ps1)
копирует мост в установленный пакет и идемпотентно патчит `claude.ts`
(`hooks: guardHooks()` во все 3 `query()`-сайта: живой разговор, one-shot
pulse/cron/customer, agent-API) и `pi/session.ts` (pre-check `piGuardDeny`).

Проверено фактически, а не по наличию файлов:

- `node ops/morphy/guard-hook.test.mjs` — 6/6: контроль `echo guard-e2e-ok`
  проходит; канарейка 2 `echo withdraw-canary` и канарейка 3 (Write в
  `.github/hooks/e2e-canary.txt`) отклоняются с `[guard]` и для SDK-хука, и для
  `piGuardDeny`; в `data/guard.log` добавлены записи `"decision": "deny",
  "client": "claude"`; все 3+1 точки патча на месте. Тест гоняет задеплоенную
  копию моста — ту, которую импортирует supervisor.
- Рестарт `trial.ps1 stop/start`: health `/api/health` и `/app/api/health` 200;
  в `logs/morphy-trial.out.log` — `Pre-warming Claude subprocess... Subprocess
  pre-warmed`: SDK принял options с `hooks` и поднял подпроцесс; патченные
  `claude.ts` и `pi/session.ts` импортированы без ошибок (`[claude] mirror
  skill` warm-up строки, ошибок `[guard-hook]` нет).
- Полный `.venv\Scripts\python.exe -m unittest discover -s tests -t .` — зелёный
  (см. отчёт задачи); Python-код проекта не менялся.

Ограничения и остаток:

- **Harness Codex не покрыт.** Provider `openai` идёт через Codex app-server
  (`supervisor/harnesses/codex.ts`) — отдельный протокол без `Options.hooks`;
  при переключении человеком провайдера на openai guard на действия Morphy не
  действует. На момент проверки `/api/onboard/status` показывал
  `provider=anthropic` (человек сменил с openai) — активный harness покрыт.
  Покрытие codex-harness — отдельная задача.
- E2E с живой моделью (промпт из guard-compat через чат Morphy) не выполнялся:
  это отправка данных во внешний AI-сервис и расход квоты человека — за рамками
  ограничений задачи. Канарейки прогнаны через задеплоенный мост на реальном
  адаптере и guard; вызов hook самим SDK подтверждён типами/контрактом SDK и
  успешным warm-up, но не живым tool-use. При желании человек может прогнать
  промпт из `insights/guard-compat.md` в чате Morphy.
- Обновление upstream Morphy может затереть патч — повторить
  `ops/morphy/deploy-guard.ps1` (сверяет версию 0.5.0, идемпотентен).
- Мост fail-open (контракт guard): сбой Python/адаптера не блокирует инструмент,
  пишется в лог supervisor.

### Продолжение 03.10 после настройки человеком

Человек подтвердил «сделал» и уточнил охват: всё, что относится к проекту.
GET `/api/onboard/status` подтвердил `portalConfigured=true`, `provider=openai`,
`tunnelMode=off`. Пароль, токены и профили агент не менял.

Реализованы `src/morphy_project.py` и собственные компоненты workspace в
`ops/morphy/`. На корневой странице Morphy установлен обзор проекта с пятью
разделами: обзор, задачи, агенты, интеграции, знания/события. Источники — прежняя
доска и read-only агрегаторы проекта; другая доска не создаётся. Проекция включает
12 интеграций, четыре рантайма и семь групп модулей, Git, Netdata, Obsidian,
статус Telegram. Новые подключения к внешним сервисам и отправка сообщений
не выполнялись. Наличие файлов и CLI явно отделено от проверки работающей сессии.

В API использована существующая сессия Morphy: Bearer из native frontend
проверяется через локальный `/api/portal/validate-token`, без записи токена
и логирования его значения. Новые endpoints принимают только GET, требуют
локальный Host/Origin, отклоняют cross-site. Python вызывается с фиксированной
командой без shell; ID задачи проходит строгую проверку. Сырые журналы,
credentials, prompt и ответы модели из очереди не проецируются.

Проверено: 8 тестов Python-проекции, 7 Node-тестов API, Vite build, реальные
HTTP health 200 и неавторизованный state 401. Проверочная вкладка Edge отрисовала
новый экран, но показывала форму входа; авторизованная проверка ожидает
`MORPHY-VERIFY-LOGIN`.

Полная `.venv/Scripts/python.exe -m unittest discover -s tests -t .`: **1109 OK**,
186.734 s, код 0; лог `logs/morphy-project-full-tests.log`. После старта suite
добавлена отдельная проверка, что очередь не раскрывает prompt/model output:
все **8** тестов `tests.test_morphy_project` также прошли. Node boundary: **7 OK**,
лог `logs/morphy-api-tests.log`. Финальный Vite build и парсеры обоих PowerShell
скриптов — код 0. `git diff --check` — без ошибок.

Реальные HTTP-проверки: GET без токена 401, POST 405, чужой Origin 403,
чужой Host 403, невалидная сессия 401 — **5/5**, артефакт
`data/morphy-eval/project-http-boundary.json`. Браузер Edge подключился,
новые русские меню и форма необходимости входа отрисованы. Подсказка первого
запуска закрыта; вкладка `/bloby` сохранена с формой входа для человека.
Пароль не вводился агентом. Проверка фактических данных после входа и мобильного
размера ещё не пройдена, поэтому `MORPHY-PROJECT-VIEW` остаётся `needs-user`.

Настроенный launcher поддерживает выбранный человеком AI, по-прежнему не
допускает relay и кошелёк. Stop/start Morphy пройден; данные аккаунта сохранились.
`src.ops status`: started_at движка не изменился, сверки 2277 → 2301,
errors_internal=0. Чужой claim панели «Контур» не затронут.

Далее ниже — первоначальная проверка до настройки аккаунта.

На Windows установлена и запущена отдельная копия `morphyagent@0.5.0`.
Адрес: http://127.0.0.1:7480. Повторный запрос подтвердил HTTP 200 для интерфейса,
health, мастера настройки и backend. Проверены остановка с освобождением всех
трёх портов и повторный запуск. Это ещё не готовый пульт нашего проекта.

Инструкции: [ops/morphy/README.md](../ops/morphy/README.md).

## Бесплатность и лицензия

Облачная установка на сайте стоит $29/месяц (Starter) или $49/месяц (Pro).
Сайт заявляет подключение существующих подписок Claude/ChatGPT, других API и
локальных моделей. Это заявления поставщика; вход и расход квот не проверялись.
Источник: [официальный сайт и тарифы](https://www.morphyagent.com/).

Self-host подтверждён [официальной документацией](https://www.morphyagent.com/docs)
и фактическим запуском npm-пакета. Лицензия опубликованной версии — BUSL-1.1,
не MIT и не Apache-2.0 на текущую дату. `LICENSE` разрешает внутреннее и
single-tenant использование в production, включая коммерческое, и ограничивает
конкурирующий hosted/managed/multi-tenant сервис с relay/marketplace.
В лицензии указана смена на Apache-2.0 30.04.2028.

Практический вывод для нашего случая: за локальное внутреннее использование
пакета отдельная лицензионная плата не требуется по тексту Additional Use Grant.
Стоимость AI, собственного оборудования и внешних сервисов учитывается отдельно;
бесплатность relay и прочих hosted-услуг не установлена и для запуска не нужна.
Первоисточники: [npm-пакет](https://www.npmjs.com/package/morphyagent),
[архив версии с LICENSE](https://registry.npmjs.org/morphyagent/-/morphyagent-0.5.0.tgz).

## Windows и изоляция пробной копии

- Системный Node 26.5.0 не входит в engines установленного `better-sqlite3@12.8.0`.
  Подготовлен отдельный Node 22.23.3, системный Node не менялся.
- SHA256 официального Node zip:
  `2b0ff57b049cda1bbcea2240eec20467018713c1efe1f7360c2681859b90ed71`.
  Сверен с [официальными контрольными суммами](https://nodejs.org/dist/v22.23.3/SHASUMS256.txt).
- Архив npm проверен относительно `dist.integrity`; зависимости устанавливались
  с отключёнными lifecycle scripts. Отдельно выполнен rebuild SQLite для Node 22.
- Upstream Windows backend URL исправлен локально через `pathToFileURL`.
- В upstream убраны вызовы startup `killPort`: чужие процессы по портам не убиваются.
- В upstream `shared/paths.ts` не используется `MORPHY_REAL_HOME`, хотя CLI его
  понимает. Поэтому bootstrap меняет `os.homedir()` до импорта модулей; проверка
  paths подтвердила отдельный каталог состояния и профилей.
- Supervisor и backend исходно используют `listen(port)` без явного host.
  Bootstrap сужает Node TCP listeners; фактические listeners 7480/7482/7484
  подтверждены на `127.0.0.1`, не на `0.0.0.0` или `::`.
- AI отсутствует, relay отключён, PULSE отключён, CRONS пуст.
  Автономные задания проекта, биржевые операции и запуск других моделей не выполнялись.

Разделение каталогов не ограничивает права ОС: подключённый AI может получить
доступ к файлам и shell с правами пользователя. Пробная копия пока используется
только для оценки веб-платформы; запуск агентов проекта через неё не подключён.

## Проверено

- `npm ci`, установка workspace и rebuild SQLite — код 0.
- `node --check ops/morphy/bootstrap.mjs` — код 0; парсер PowerShell launcher — без ошибок.
- `trial.ps1 start/status/stop` — процесс найден по пути/командной строке;
  остановка освободила 7480/7482/7484; повторный запуск health 200.
- HTTP `/api/health`, `/api/onboard/status`, `/`, `/bloby/onboard.html`,
  `/app/api/health` — 200. В onboarding status: `provider` пуст,
  `portalConfigured=false`, `tunnelMode=off`, `handle=null`.
- Runtime paths и целостность архива — отдельная проверка в `data/morphy-eval/verification.json`.
- `.venv/Scripts/python.exe -m unittest discover -s tests -t .` — 1102 теста, OK, код 0 (162.885 s); полный лог `logs/morphy-project-tests.log`.
- Повторный `python -m src.ops status` — код 0: demo, kill/breakers false, reconciles выросли с 2267 до 2275, internal errors 0. Движок не останавливался.

Визуальная проверка не завершена: in-app browser отсутствует, Edge через
computer-use дважды вернул `Unable to load browser request-header policy`.
HTTP 200 не подтверждает правильную отрисовку браузером. Скриншоты не получены.

При запуске Vite предупреждает о будущей несовместимости `__dirname` с native
config loader и об устаревающих HMR параметрах. На текущей версии загрузка прошла.

## Пригодность для общего пульта

Morphy предоставляет shell приложения, workspace, backend, SQLite и AI-чат.
Изученные исходники содержат Codex/Claude auth routes и провайдерные harnesses:
это подтверждает наличие механизма, но не авторизацию и не совместимость со всеми
существующими сессиями проекта. Готового коннектора к нашей доске и движку не найдено.

Рекомендация после оценки: продолжать как кандидат на визуальную оболочку.
Прежде чем переносить управление, человек оценивает мастер настройки и выбирает
AI-провайдера. Затем отдельным инкрементом подключаем чтение доски, агрегированных
статусов и журналов. Существующий `src/control_panel.py` имеет токен и allowlist
действий; его нельзя публиковать через relay без отдельного проекта доступа.

Параллельная задача `AGENT-PANEL-RUNS` удерживает существующую панель. Её файлы
не менялись. Доска остаётся источником задач; новая независимая доска внутри
Morphy не создавалась. Guard нового рантайма и запуск моделей через Morphy требуют
отдельной проверки и соблюдения [AGENTS.md](../AGENTS.md) §2/§10.

Продолжение: `MORPHY-ACCOUNT` (решение человека) и `MORPHY-PROJECT-VIEW`
(подключение данных после оценки интерфейса и освобождения claim панели).
