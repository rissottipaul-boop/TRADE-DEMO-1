# Задачи, запуски и наблюдаемость AI — пункты 21–30 и 39–48

Дата: 03.10.2026 (+05:00). Статус: локальная реализация и серверная приёмка; внешний Agents API ожидает решения `AI-OPENAI-ADAPTER-ACCESS`, визуальная проверка — `AI-UI-BROWSER-VERIFY`.

Реализация дополняет существующий Agent Control Plane. Источник задач — [ops/board.md](../ops/board.md), роли — `.github/agents/`, права — [AGENTS.md](../AGENTS.md). Новая доска или параллельный торговый исполнитель не создаются.

## Что доступно

| Пункт | Реализация | Граница результата |
| --- | --- | --- |
| 21 | `agent_assistant.task_draft`: описание → цель, критерий, предлагаемая роль и статус, Markdown | Локальные правила, без вызова модели; черновик не записывает доску |
| 22 | `recommend_role`: Executor / Hunter / Sentinel с объяснением выбора | Это рекомендация; действующие claims и реестр моделей сохраняются |
| 23 | `build_handoff`: цель, полный критерий, runtime/model, файлы, проверки, внешние IDs, следующий шаг, источники | Отсутствующие сведения перечислены в `missing_evidence`; неизвестное действие сначала сверяется |
| 24 | Курсор `/api/run/events`; выбранный запуск обновляется каждые 3 с в Контуре и Morphy | Нативные метаданные либо журнал лаунчера с provenance; стадия файла Muse не означает живую сессию |
| 25 | `/api/run/context`: восстановленный handoff и внешний session ID, если есть | `session_resumed=false`; подтверждённого протокола resume у текущих адаптеров нет |
| 26 | Возможности адаптера определяют видимые команды; resume/cancel/steer без протокола дают `unsupported` | Завершение по чужому PID не применяется, новый run не маскируется под продолжение |
| 27 | `/api/run/result` и структурированный `agent_run_report` | Отчёт агента имеет качество `reported`; код выхода и отчёт не доказывают критерий доски |
| 28 | `review_diff`: staged, unstaged, untracked, committed относительно lease base commit | Ограничение размера/времени, проверка worktree и защищённых файлов; итог `requires-human-review` |
| 29 | Специалисты: закрытые `task.read`, `sources.search`, `diff.review`; ответ `facts` + `sources` | Локальные ограниченные инструменты; внешняя достоверность источников не объявляется проверенной |
| 30 | HTTP-запуск Insight Executor по умолчанию получает отдельный worktree | Требуется чистый checkout; lease атомарен, worktree/ветка не удаляются и не сливаются автоматически |
| 39 | Журнал фактических вызовов локальных инструментов в `data/ai/observability.sqlite3` | Схема сохраняет метаданные, длину описания и итог; сырые prompts/description/outputs исключены |
| 40 | `usage_summary`: запуски, модели, reported USD, unknown расходы, задержки и ошибки функций | Частичная сумма явно partial; нет тарифной оценки, локальная задержка не называется задержкой модели |
| 41 | 11 offline-сценариев: TTL, отсутствие/будущее время, секреты, схемы, запрещённые действия, неизвестный исход | Фикстуры проверяют код и известные нарушения, не реальную модель |
| 42 | CLI `compare`: сохранённые ответы до/после по шести контрольным примерам | В отчёт попадают оценки и регрессии, исходные ответы не публикуются; лексические правила не доказывают весь ответ |
| 43 | `grade_trace`: соответствие call/result, разрешённые схемы, ошибки, неизвестный исход, повторы | Это локальная проверка сохранённых шагов; provider traces/trace grading отдельно требуют доступа |
| 44 | Русские статусы сбоя и штатный следующий шаг в сводке | Автоматический повтор ошибки выключен; отказ guard не является квотой |
| 45 | Атомарная SQLite-резервация запросов и межпроцессный lock старта; intent пишется до spawn | Потеря ответа/аварийный `starting` → `unknown`; новая попытка запрещена до сверки |
| 46 | Общая key-sensitive фильтрация, env assignments, токены, JWT, PEM; allowlist журнала | Секретные файлы diff не читает; AI-инструменты не отправляют данные провайдеру |
| 47 | Типы, неизвестные поля, диапазоны, IDs, вложенные аргументы проверяются перед dispatch | Shell/write/trade/start/cancel/reset не входят в read-only assistant allowlist |
| 48 | Один read-only dispatcher используется Контуром и stdin-мостом Morphy | Операционные кнопки панели отделены от инструментов AI; права модели не расширены |

Пункт 49 в запросе не содержит описания и не превращён в выдуманную задачу.

## Интерфейс и команды

В Morphy: **Агенты → Задачи, запуски и качество AI**. Можно подготовить черновик, выбрать задачу/запуск, получить handoff, проверить diff, открыть результат, смотреть события и контрольные сценарии. Существующая сессия Morphy обязательна. POST передаёт JSON в фиксированный модуль Python по stdin, без shell и описаний в командной строке.

В Контуре: **Помощник**, **Работа AI**, карточка задачи и **Запуски → События**. Список операций следует capabilities. Read-only GET/POST защищены localhost, Host, Origin и токеном панели.

```powershell
.venv\Scripts\python.exe -m src.ai_observability usage
.venv\Scripts\python.exe -m src.ai_evals builtin
.venv\Scripts\python.exe -m src.ai_evals cases --output logs\ai-eval-cases.json
.venv\Scripts\python.exe -m src.ai_evals compare --baseline before.json --candidate after.json --output logs\ai-eval-comparison.json
.venv\Scripts\python.exe -m src.ai_evals trace --input trace.json --output logs\ai-trace-grade.json
```

`before.json` и `after.json`: объект `case_id → строка ответа`; список case IDs и условия выдаёт `cases`. `trace.json`: список нормализованных шагов, правила и примеры — в `src/ai_evals.py`. Ключи и токены в этих артефактах запрещены.

## Приёмка и ограничения

Серверная приёмка: `tests.test_agent_assistant`, `tests.test_ai_observability`, `tests.test_ai_evals`, `tests.test_ai_panel_api`, `tests.test_ai_tools_cli`, `tests.test_agent_run_report`, существующий `tests.test_control_panel`; Node boundary — `ops/morphy/project-backend.test.mjs`. Полные логи — `logs/ai-*.log`, `logs/test-agent-assistant.log`; общий итог фиксируется на доске после завершения проверок.

На настоящем локальном HTTP-сервере проверены health, read-only observability, evals и draft. Фронтенд Morphy собирается esbuild; существующая проверка контраста зелёная. Скриншоты и клики в браузере не подтверждены: Opera provider дважды вернул `Unable to load browser request-header policy`, iab недоступен. Это отдельный блокер; HTTP smoke не выдаётся за визуальную проверку.

Итог целевой проверки: **158 тестов OK, 2 skipped** (Windows symlink privilege), лог `logs/ai-final-targeted-tests.log`. Node API **32/32 OK** (`logs/ai-morphy-api-tests-final.log`); `node --check` и `py_compile` зелёные, сборка `logs/ai-morphy-build.log`, контраст `logs/ai-morphy-contrast.log`. CLI producer → артефакт → HTTP result/context проверен тестами; данные помечены `reported`, неизвестный ID остаётся `unknown`, `acceptance_verified=false`.

`ops/morphy/deploy-project.ps1` применён, Контур перезапущен штатным проверенным лаунчером. Оба сервиса ответили HTTP 200; секреты в вывод не попадали. Проверка `python -m src.ops status`: internal errors 0, divergences 0, local open orders пуст; существующие 40 exchange errors из состояния движка не связаны с этой реализацией. Торговых запросов новая функциональность не выполняет.

## Отчёт исполнителя запуска

Лаунчер даёт агенту инструкцию подготовить JSON и выполнить `python -m src.agent_run_report --run-id <ID> --task-id <TASK> --input report.json` (либо stdin). Артефакт всегда `data/run-results/<ID>.json` в checkout запуска; произвольного пути вывода нет. Для worktree панель сначала сверяет независимый lease, ожидаемый каталог и регистрацию Git. Привязка task/run, версия, списки, пути, exit code/status и секреты валидируются до записи и при чтении. Отчёт не меняет статус задачи и не разрешает повтор unknown-действия.

```json
{
  "summary": "Локальный инструмент реализован, требуется ревью diff",
  "changed_files": ["src/example.py", "tests/test_example.py"],
  "checks": [{"command": "python -m unittest tests.test_example", "exit_code": 0, "status": "passed"}],
  "external_actions": [],
  "next_step": "Сверить критерий задачи и принять результат"
}
```

Каждая запись `external_actions`: `id`, `kind`, `status`; при неизвестном исходе `status: unknown`. Это инструкция и транспорт для фактического отчёта будущего запуска. В этой приёмке внешняя модель не запускалась; seeded fixtures не объявляются её доказательствами.

Первый полный suite обнаружил пустой `src/perimeter.py`, появившийся в параллельном claim `PERIMETER-DRIFT-CHECK`. Чужой файл не исправляется этой задачей; после завершения claim общий suite необходимо повторить. Общий diff check также видит ранее изменённый `src/backtest/grid_gate.py` (пустая строка EOF); проверка файлов этой реализации выполняется отдельно.

Повторный полный suite после интеграции: **1357 tests, 1 failure, 2 skipped**, `logs/ai-full-unit-tests-final.log`. Единственное падение — тот же `tests.test_empty_files` на пустом `src/perimeter.py`. Обе попытки записаны в `AI-ACCEPTANCE-UNIT`; AI-задачи не помечены `done`, пока общий критерий не выполнен. Код и установка локального слоя завершены; общая unit-приёмка и визуальная проверка ждут устранения внешних блокеров.

## Внешний OpenAI адаптер

Официальная документация описывает управляемые сессии и продолжение работы в [Agents API](https://developers.openai.com/api/docs/guides/agents-api/overview), а ограниченных специалистов для главного агента — в [Orchestration and handoffs](https://developers.openai.com/api/docs/guides/agents/orchestration). Это подтверждает возможность отдельного адаптера, но не наличие доступа у аккаунта проекта.

OpenAI различает живые события сессии и трассы завершённого turn; экспорт трасс требует прав и настройки организации. См. [Tracing](https://developers.openai.com/api/docs/guides/agents-api/tracing). [Trace grading](https://developers.openai.com/api/docs/guides/trace-grading) оценивает шаги всей трассы; локальные проверки выше не называются подключением этого сервиса.

Правка или чтение `.env` запрещены guard. Проверка наличия ключа была отклонена; обход не выполнялся. До решения `AI-OPENAI-ADAPTER-ACCESS` не добавляются API-клиент, сетевые вызовы, hosted sandbox или SDK-специалисты. Модель текущих рантаймов и их guard-настройки сохранены.
