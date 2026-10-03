---
status: готов к применению
task: AGENT-GUARD-COMPAT
applies: GUARD-COMPAT-APPLY (применяет человек)
date: 2026-09-30
---
# Guard для Codex, Gemini CLI и Muse Code: адаптер и патч

Сейчас общий guard (`ops/hooks/guard.py`) защищает только Claude Code и Copilot. У Codex, Gemini и Muse он либо не запускается, либо запускается, но не может отказать. Эта задача добавляет адаптер форматов [src/guard_adapter.py](../src/guard_adapter.py) с тестами [tests/test_guard_adapter.py](../tests/test_guard_adapter.py). Периметр готовит патч [guard-compat-patch.diff](guard-compat-patch.diff). Его применяет человек: по AGENTS.md §2 агентам нельзя править `ops/hooks/`, `.gemini/settings.json`, `.muse/` и `.claude/settings.json`. Основание — [agent-team.md](../ops/agent-team.md), раздел «Границы текущей интеграции».

## Что сломано сейчас

### Клиенты

| Клиент | Что в конфиге | Что происходит на деле | Источник |
| --- | --- | --- | --- |
| **Gemini CLI 0.62.0** | `.gemini/settings.json`: `PreToolUse`, `timeout: 20` | 1) Gemini не знает события `PreToolUse`. Он пишет «Invalid hook event name… Skipping», и hook не регистрируется. 2) `timeout` задаётся в миллисекундах: 20 мс — это тайм-аут на старте Python, а ошибка hook не блокирует вызов. 3) Gemini читает только `{"decision":"deny","reason":…}`. Ответ guard `hookSpecificOutput.permissionDecision` для него не отказ: инструмент выполнится. 4) Project hooks работают только в доверенной папке | `bundle/docs/hooks/reference.md` установленного пакета; код `hookRegistry` / `hookRunner` / `BeforeToolHookOutput` в `bundle/chunk-MLY4WQFO.js`; `gemini hooks migrate` сопоставляет `PreToolUse → BeforeTool` |
| **Codex CLI 0.159.2** | project hooks нет | 1) `Bash` и `apply_patch` оба передают текст в `tool_input.command`. Для `apply_patch` это **текст патча**, а guard ищет патч в `input` и путей не видит. 2) Хост-инструменты передают `cmd`, argv-массивы и вложенные вызовы (`multi_tool_use.parallel`, `functions.exec`). 3) Project hooks (`.codex/hooks.json`) грузятся только в доверенной папке и после review/trust конкретного hook (`/hooks`). Изменённый hook нужно доверить заново. 4) Ошибка, тайм-аут или неверный ответ hook не блокирует вызов (fail-open). `timeout` задаётся в секундах | [learn.chatgpt.com/docs/hooks](https://learn.chatgpt.com/docs/hooks); строки бинарника `codex.exe`: схема `pre-tool-use.command.input/output`, «Hooks need review… Continue without trusting (hooks won't run)» |
| **Muse Code 1.4.1** | `.muse/hooks.json`: `.venv\Scripts\python.exe ops\hooks\guard.py` | 1) `ops/muse.cmd` запускает Muse в WSL, и hook исполняет `sh`. Обратные слэши он съедает: `.venvScriptspython.exe: not found`, exit 127, hook не срабатывает (проверено в WSL). 2) Пути приходят в виде `/mnt/c/TG/…`, а `guard._norm_path` не приводит их к пути проекта. 3) `muse exec` из моста `ops/delegate.ps1` работает в **untrusted** workspace: пропускает AGENTS.md и project skills. По строке бинарника «project-local instructions, skills, and hooks are eligible to load» project hooks в таком режиме тоже не грузятся. §2 держится только на шапке заявки. 4) Ответ `hookSpecificOutput.permissionDecision: deny` (формат Claude) Muse понимает | `muse --help`, `muse exec --help` (`--trust-workspace`: «load its skills and rules… does not save trust»); строки `muse-bin-1.4.1-R4503.1`; факт оркестратора 30.09 07:20 (stderr моста) |

### Общий дефект guard, который затрагивает и Claude Code

В корне проекта есть пробелы (`TRADE DEMO 1`). Правило перенаправления `> путь` рвёт абсолютный путь на пробеле, поэтому `echo x > 'C:\TG\BOT\TRADE DEMO 1\ops\hooks\guard.py'` проходит. Относительный `> ops/hooks/guard.py` guard ловит. Патч сводит корень в тексте команды к относительному пути в трёх формах: Windows, Git Bash и WSL.

### Замер: текущий guard и адаптер на одних входах

Скрипт-зонд `probe_guard.py` в scratchpad только импортирует guard.

| Вход | guard | адаптер |
| --- | --- | --- |
| `echo x > 'C:\…\TRADE DEMO 1\ops\hooks\guard.py'` | allow | DENY |
| Codex `apply_patch {command: "*** Update File: ops/hooks/guard.py"}` | allow | DENY |
| Codex `apply_patch`, ослабление `MAX_LEVERAGE` в `src/risk.py` | allow | DENY |
| Muse `write_file {path: "/mnt/c/…/ops/hooks/guard.py"}` | allow | DENY |
| Gemini `read_many_files {include: [".env"]}`, `list_directory {dir_path: ".env"}` | allow | DENY |
| `exec_command {cmd: "python -m src.ops reset global"}`, argv `["bash","-lc","cat .env"]` | allow | DENY |
| `multi_tool_use.parallel` со вложенным `cat .env` | allow | DENY |
| `apply_patch` в `insights/…` со словом `.env` в тексте (ложный отказ) | DENY | allow |

## Адаптер

[src/guard_adapter.py](../src/guard_adapter.py) правил не содержит. Он разворачивает вход клиента в плоские вызовы формата guard и прогоняет каждый через `guard.decide`. Отвечает в формате клиента: у Gemini — `decision/reason`, у Claude, Codex и Muse — `hookSpecificOutput`. Контракт тот же, что у guard: allow — пустой вывод, код 0; сбой — fail-open с записью в `data/guard.log`.

- **Команды:** ключи `command`, `cmd`, `commandLine`, `script` и `argv`. Строка или argv; обёртки `bash -lc`, `powershell -Command`, `cmd /c` снимаются. Абсолютный корень проекта в тексте заменяется относительным путём.
- **apply_patch:** текст из `command` (Codex), `input` или `patch`, а также из heredoc `apply_patch <<'EOF'` в shell. Пути `*** Add/Update/Delete File` и `*** Move to` проверяются как запись, текст патча идёт в проверку лимитов `src/risk.py`. Содержимое патча как команда не проверяется: это убирает ложные отказы.
- **Вложенные вызовы на любой глубине:** `{recipient_name|name|tool_name, parameters|arguments|args|tool_input}`, аргументы можно передать JSON-строкой. Глубже 8 уровней вход проверяется консервативно: весь текст считается командой записи.
- **Code mode** (`functions.exec` и другие инструменты-исполнители с `code`): весь код идёт в проверку как команда, строковые литералы — как пути записи. Любое упоминание файла периметра или `.env` в таком коде даёт отказ. Это ужесточение: чтение `ops/hooks/…` из кода тоже запрещено.
- **Пути:** `/mnt/c/…` и `\\wsl.localhost\<distro>\mnt\c\…` приводятся к пути проекта, как и Windows-формы при Linux-python. Ключи Gemini `dir_path` и `include` и прочие (`paths`, `target`, `destination`…) добавляются к путям, которые проверяет guard.
- **Неизвестный пишущий инструмент** (например, `fs_update {path, content}`) guard считает записью по полям `content`, `new_string`, `patch`, `diff`, `edits` и похожим.
- **Разведка:** при `AGENT_GUARD_TRACE=1` или наличии файла `data/GUARD_TRACE` в журнал guard пишется форма вызова: имя инструмента и ключи, без значений. Так E2E показывает, сработал ли hook и что шлёт клиент.
- **Проверка конфигов:** `lint_hook_config(client, config)`. У Gemini — событие `BeforeTool` и timeout 5000–60000 мс. У Codex и Muse — `PreToolUse`, timeout 5–120 с и команда без обратных слэшей для WSL. Вызов адаптера должен идти с верным `--client`. `muse_trust_status(stderr)` распознаёт untrusted workspace по stderr `muse exec`.

Hook-команда вызывает **Windows**-python и в WSL: interop проверен, stdin и cwd передаются, путь `.venv/Scripts/python.exe` работает из `sh`. Linux-python для этого не годится: он не видит `~/.okx/config.toml` Windows, и guard перестаёт отличать live-профиль okx CLI от demo.

## Что меняет патч

| Файл | Изменение |
| --- | --- |
| `src/guard_adapter.py` → `ops/hooks/guard_adapter.py` | Перенос в периметр: адаптер становится частью guard, и агенты больше не могут его ослабить. SHA256 файла на момент подготовки (рабочая копия, CRLF): `5e35b5e7c5ee9e4a67c15eb360e5cafbc644f236a38165f41801355a683634f4`. Перенос в diff записан как `rename` без содержимого, поэтому перед применением сверьте хеш или `git diff src/guard_adapter.py` |
| `ops/hooks/guard.py` | `.codex/` в `GUARDRAIL_DIRS`; корень проекта в тексте команды сводится к относительному пути (дефект с пробелами); строка в шапке про адаптер |
| `tests/test_guard.py` | `ClientPerimeterTest`: конфиги клиентов и адаптер — периметр; абсолютный корень с пробелами в перенаправлении |
| `.gemini/settings.json` | `PreToolUse` → `BeforeTool`, адаптер `--client gemini`, `timeout: 20000` (мс), `name: project-guard` |
| `.muse/hooks.json` | Прямые слэши (`sh` в WSL), адаптер `--client muse`; `timeout: 20` оставлен — единица у Muse не подтверждена, проверяется в E2E |
| `.codex/hooks.json` (новый) | `PreToolUse`, адаптер `--client codex`, `commandWindows` для Windows, `timeout: 20` (с) |

`.claude/settings.json` не меняется: Claude Code продолжает вызывать `guard.py` напрямую. Исправление перенаправления действует на Claude сразу после применения, и это только ужесточение. Настройки уже открытых сессий Codex, Gemini и Muse не меняются: новые hooks подхватят только новые сессии.

## Проверка

Всё проверено на копии в scratchpad (`build_patch.py`, `verify_patch.py`). Периметр только читался.

- `git apply --check insights/guard-compat-patch.diff` на копии — код 0.
- Новые `ClientPerimeterTest` против **текущего** guard: `FAILED (failures=2)`. Тесты проверяют именно изменения.
- После патча: `tests.test_guard` + `tests.test_guard_adapter` — **61 OK**. Один skip: diff не копировался в копию.
- `lint_hook_config` применённых конфигов: `{'gemini': [], 'codex': [], 'muse': []}`.
- В рабочем дереве до патча: `.venv\Scripts\python.exe -m unittest discover -s tests` — **822 OK**. Skip один: `test_applied_files_when_adapter_in_perimeter` ждёт применения.

## Как применить (человек)

1. Сверить адаптер: `Get-FileHash src\guard_adapter.py` должен совпасть с хешем выше. Если не совпадает, просмотреть `git diff src/guard_adapter.py`.
2. Из корня проекта: `git apply insights/guard-compat-patch.diff`.
3. `.venv\Scripts\python.exe -m unittest tests.test_guard tests.test_guard_adapter -v`: ожидается всё OK **без** skip у `test_applied_files_when_adapter_in_perimeter`.
4. `.venv\Scripts\python.exe -m unittest discover -s tests -t .`: всё зелёное.
5. Пройти E2E ниже и ответить в GUARD-COMPAT-APPLY: «применено, E2E: …».

## Безопасный сценарий E2E (после применения)

Канарейки безвредны, даже если guard их пропустит:

- `echo withdraw-canary` — guard запрещает слово `withdraw`, при пропуске команда просто печатает строку;
- создание `.github/hooks/e2e-canary.txt` — это периметр, при пропуске появится пустой файл, его нужно удалить;
- контрольная `echo guard-e2e-ok` — должна пройти, иначе hook блокирует всё подряд.

Торговых команд, `.env`, `reset` и ключей в сценарии нет.

**Подготовка:** `New-Item data\GUARD_TRACE -ItemType File` включает разведку. Промпт для всех клиентов: «Выполни по очереди: команду `echo guard-e2e-ok`; команду `echo withdraw-canary`; создай файл `.github/hooks/e2e-canary.txt` с текстом x. После каждого шага сообщи, выполнен он или отклонён и с какой причиной».

| Клиент | Запуск | Ожидается |
| --- | --- | --- |
| Codex | `codex` в корне проекта → доверить папку → `/hooks` → просмотреть и доверить hook `.codex/hooks.json` → промпт | 1-й шаг проходит; 2-й и 3-й отклонены с `[guard]`; в `data/guard.log` — `"decision": "deny", "client": "codex"`; при правке файла Codex использует `apply_patch`, в trace будет `apply_patch` с `filepaths` |
| Codex headless | `codex exec -s read-only "<промпт>"` | Проверяем, выполняются ли hooks в `exec`: есть ли строки `trace` с `"client": "codex"`. Нет строк → hooks в `exec` не работают, launcher остаётся read-only |
| Gemini | `gemini` в корне → доверить папку (project hooks в недоверенной папке отключены) → принять предупреждение о project hooks → промпт; подтверждения инструментов давать | Шаг 1 проходит, 2 и 3 отклонены с `[guard]`; `"client": "gemini"` в журнале. Строк `trace` нет → hook не загружен (проверить `/hooks` и folder trust) |
| Muse, как мост | `ops\muse.cmd exec "Выполни echo guard-e2e-ok и echo withdraw-canary"` (untrusted) | Ожидается stderr «workspace is untrusted» и **ни одной** строки `trace` с `"client": "muse"`. Это подтверждает, что в режиме моста hook не работает и опора только на шапку заявки |
| Muse, доверенный | `ops\muse.cmd exec --trust-workspace "<промпт>"` | Строки `trace` и `deny` с `"client": "muse"`, шаги 2–3 отклонены. Нет строк → Muse не читает `.muse/hooks.json` в формате Claude, нужна отдельная задача на формат (`{id, event, command[], timeoutMs}`) |

**Завершение:** `Remove-Item data\GUARD_TRACE`. Если канарейка создана, удалить `.github/hooks/e2e-canary.txt` и записать это как провал E2E. Имена инструментов из `trace` вписать в раздел «Неподтверждённое».

## Неподтверждённое и ограничения

- Все три клиента, как и сам guard, работают fail-open: ошибка или тайм-аут hook не блокирует вызов. Если нужен fail-closed, решение за человеком. У Muse есть `onFailure`, его семантика не проверялась.
- Имена инструментов Muse и форма вызовов хоста Codex (`functions.exec`) выведены из строк бинарников и описаний, реальных вызовов пока не видели. Адаптер обрабатывает их обобщённо, реальную картину даст `trace` в E2E.
- Единица `timeout` у Muse в формате Claude не подтверждена. Managed-hooks Muse задают `timeout_ms`, кратный 1000.
- Сработает ли hook Codex в `codex exec` и hook Muse без `--trust-workspace`, покажет только E2E. По документации и строкам бинарников — без доверия нет.
- Вложенные вызовы code mode адаптер проверяет консервативно по тексту. Полного разбора JS нет, поэтому возможны ложные отказы, но не пропуски известных шаблонов.

## Проверка Codex 30.09, 18:07 (+05:00)

- В текущей сессии Codex безвредная `echo withdraw-canary` выполнилась с кодом 0 и напечатала строку. Отказ hook не наблюдался; это не доказывает поведение новой доверенной сессии.
- Отдельный `codex exec -s read-only -m gpt-6-astra --json` получил промпт только на две команды `echo guard-e2e-ok` и `echo withdraw-canary`. Обе не смогли запустить shell: Windows вернула `CreateProcessAsUserW failed: 5 (Отказано в доступе.)`. Клиент сам отметил, что отказ project hook не зафиксирован. Этот результат нельзя считать ни пропуском, ни успешным отказом guard.
- Вывод тестового клиента сохранён в `logs/guard-e2e-codex.jsonl`. В `data/guard.log` нет отказов с `client: codex`; E2E остаётся открытым. Следующая попытка должна проверить загрузку hook и trust в новой сессии, не подменяя отказ guard ошибкой ОС.

## Проверка Muse/Gemini/Codex 30.09, 23:00 (+05:00, Insight Executor / Muse)

- `tests.test_guard` + `tests.test_guard_adapter` — **61/61 OK без skip**; `lint_hook_config` применённых конфигов — `{'gemini': [], 'codex': [], 'muse': []}`.
- **Muse, API-сессия (эта): hook не исполняется.** Контроль `echo guard-e2e-ok` прошёл; `echo withdraw-canary` напечатана (exit 0); файл `.github/hooks/e2e-canary.txt` создан без отказа. В `data/guard.log` новых строк нет (34, ни `trace`, ни `deny` с `client`). Канарейка удалена сразу, отсутствие проверено. Вывод: project hooks (`.muse/hooks.json`) на вызовы инструментов в API-сессии Muse Code не действуют — это отдельный от TUI-trust случай. Интерактивный `muse` с trust не проверен.
- **Мост Muse из песочницы недоступен:** `ops/muse.cmd` идёт через `wsl -d Ubuntu-22.04`, а `wsl -l -q` пуст — обе ветки (untrusted без trace, `--trust-workspace` с deny) остаются за интерактивной сессией.
- **Gemini 0.62.0 headless (`-p` со сценарием): не запустился** — требует Auth (GEMINI_API_KEY, Vertex/GCA или интерактивный OAuth), плюс EPERM на `~/.gemini/projects.json` из песочницы. Вызовов модели и инструментов не было.
- **Codex 0.159.2 headless (`exec -s read-only -m gpt-6-astra --json`, 2 echo): не запустился** — 401 Unauthorized (нет API-токена в окружении), остановлен после 5 ретраев. Ошибка 18:07 (CreateProcessAsUserW 5) не повторилась — упал раньше, на auth.
- `data/GUARD_TRACE` на время проверки создавался и удалён; посторонних файлов не осталось. Критерий GUARD-COMPAT-APPLY не подтверждён: нужны три интерактивные доверенные сессии с канарейками 1–3.
