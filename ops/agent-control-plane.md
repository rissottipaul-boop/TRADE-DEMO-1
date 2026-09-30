# Agent Control Plane: проект интерфейса рантаймов

Статус: архитектура и локальная панель v1, 2026-09-30. Владелец: Project Orchestrator. Предмет: координация инженерных задач проекта; торговые разрешения остаются в [AGENTS.md](../AGENTS.md) §2. Flowise удалён по решению человека.

## Граница ответственности

```text
ops/board.md → Scheduler → Router → Execution Manager → RuntimeAdapter
                                      │                   ├─ Codex
                                      │                   ├─ Claude Code
                                      │                   ├─ Gemini CLI
                                      │                   └─ Muse Code (CodingTeam)
                                      ├─ журнал / проекция / API / панель
                                      └─ проверка результата / handoff
```

Верхний уровень создаёт **один run на задачу доски** и рассматривает Muse Code как один runtime. Его внутренние subagents, worktrees и event log принадлежат Muse; control plane хранит ссылки и нормализованные события, но не планирует каждого ребёнка Muse. Для Codex, Claude и Gemini действует тот же контракт. Cursor и CustomAgent — будущие адаптеры, только после проверки их автоматизируемых интерфейсов.

`ops/board.md` остаётся источником задач и человеческих решений. `ops/agent-routing.json` — начальные приоритеты ролей и моделей. `ops/agent-rotate.ps1` и [очередь Muse](delegations/README.md) — существующие пути запуска; первая версия control plane читает их состояние, не создавая вторую независимую доску. Локальная [панель](control-panel/README.md) заменяет прежний Flowise UI. LangGraph можно подключить как workflow-клиент для сложных графов; он не владеет жизненным циклом run.

## Контракт

```typescript
type Capability = "start" | "status" | "events" | "steer" | "cancel" | "resume";
type RunState = "queued" | "starting" | "running" | "waiting_input" |
  "verifying" | "completed" | "failed" | "cancelled" | "unknown";

interface RuntimeAdapter {
  id: string;
  provider: "openai" | "anthropic" | "google" | "meta";
  capabilities(): Promise<ReadonlySet<Capability>>;
  start(spec: RunSpec): Promise<{ externalRunId: string; cursor?: string }>;
  status(externalRunId: string): Promise<RunState>;
  events(externalRunId: string, after?: string): AsyncIterable<RuntimeEvent>;
  steer?(externalRunId: string, text: string): Promise<void>;
  cancel?(externalRunId: string): Promise<void>;
  resume?(externalRunId: string, context: Handoff): Promise<void>;
  result(externalRunId: string): Promise<RunResult>;
}
```

Операции `steer`, `cancel` и `resume` вызываются только при заявленной и проверенной возможности адаптера. Недоступная операция возвращает `unsupported`, не имитирует успех через новый run. `cancel` означает запрос отмены, затем требуется подтверждение конечного состояния. `start` идемпотентен по `task_id + attempt + idempotency_key`; неизвестный исход запуска сначала сверяется с рантаймом и журналом. Exit code процесса сам по себе не означает выполненный критерий доски.

`RunSpec`: `task_id`, `role`, `runtime_id`, `model`, `prompt_ref`, `base_commit`, `workspace_ref`, `permission_profile`, `timeout_s`, `max_cost`, `idempotency_key`. `RunResult`: `summary`, `changed_files`, `commits`, `test_commands`, `test_results`, `external_actions`, `event_cursor`, `artifact_refs`. Секреты в этих структурах запрещены.

## Состояние и события

Основное хранилище для промышленной версии — PostgreSQL: `tasks` (ссылка на строку доски и ревизия), `runs` (одна попытка исполнения), `run_events` (append-only), `workspace_leases`, `artifacts`, `handoffs`, `approvals`, `adapter_capabilities`. В `run_events` уникальность `(runtime_id, external_run_id, external_event_id)`; если клиент не даёт ID события — `(run_id, ingest_seq)` плюс hash исходного фрагмента. Сохраняются `schema_version`, `observed_at`, `occurred_at`, `raw_ref` и нормализованный payload. События не переписываются; статус, токены и стоимость — вычисляемые проекции с отметкой качества данных (`reported`, `estimated`, `unknown`).

Нормализованные типы: `run.started`, `run.progress`, `run.waiting`, `agent.spawned`, `tool.started`, `tool.finished`, `file.changed`, `test.finished`, `approval.requested`, `approval.resolved`, `run.completed`, `run.failed`, `run.cancelled`. Дочерние Muse-события содержат `parent_external_id` и `worktree_ref`, но остаются внутри родительского run. Сырые логи хранятся как ограниченные по доступу артефакты; перед показом в UI удаляются секреты. Event cursor позволяет повторную загрузку после рестарта без дублирования.

Для доставки в первую очередь хватает PostgreSQL transactional outbox и фонового worker. Redis Streams подключается при подтверждённой нагрузке на live-обновления; потеря брокера не должна терять событие, потому что источником остаётся БД и cursor рантайма. UI получает `GET /v1/runs/{id}/events?after=...` или SSE с тем же cursor.

## API

| Метод | Путь | Результат |
| --- | --- | --- |
| `POST` | `/v1/tasks/{id}/runs` | claim задачи, проверка lease, создание попытки; обязательный `Idempotency-Key` |
| `GET` | `/v1/runs/{id}` | статус, рантайм, модель, worktree, cursor, качество метрик |
| `GET` | `/v1/runs/{id}/events` | журнал с cursor или SSE |
| `POST` | `/v1/runs/{id}/steer` | подтверждённая передача инструкции либо `unsupported` |
| `POST` | `/v1/runs/{id}/cancel` | запрос отмены и отдельное подтверждение остановки |
| `POST` | `/v1/runs/{id}/retry` | новая попытка после сверки прежней; внешние действия не повторяются вслепую |
| `GET` | `/v1/tasks/{id}/handoff` | снимок передачи между рантаймами |

Запись API доступна только локальному доверенному оператору или служебному аккаунту с нужной ролью. Для trade/ops задач API отдаёт работу только в пределах [AGENTS.md](../AGENTS.md) §2 и состояния guard; управление run не превращается в разрешение ордера. Человеческие решения проходят через существующий `needs-user` и `notes/decisions/`; автоматический API не снимает эти границы.

## Планировщик и рабочие каталоги

Scheduler читает `ready` и время `scheduled`, проверяет зависимости, одну активную заявку на task ID и claims файлов. Router применяет `ops/agent-routing.json`, доступность рантайма, подтверждённые capabilities, guard и лимит параллелизма, установленный человеком для проекта. Неуспех guard или отсутствие trust исключают рантайм из задач с записью; это не повод направить то же действие в другой клиент.

Для пишущих запусков Execution Manager выдаёт отдельный git worktree и lease с `base_commit`, `branch`, `owner_run_id`, TTL и heartbeat. Для Muse есть два режима: верхнеуровневый worktree для всего run или внутренние worktrees Muse для subagents. Нельзя предполагать, что флаг `--subagent-worktree-isolation` изолирует каждого ребёнка: каждый пишущий subagent должен запросить изоляцию, а отклонение запроса останавливает параллельную запись. Перед принятием результата проверяются diff, тесты и конфликт с более свежими изменениями; автоматический merge не входит в первую очередь работ.

Handoff содержит исходную задачу, критерий, роль, рантайм и модель, commit/diff, занятые файлы, команды и результаты проверок, журнальные cursor, внешние ID, незавершённые действия, следующий шаг. Он дополняет заметку доски, а не заменяет её. Если прежний процесс оборвался, статус `unknown` требует реконструкции до retry.

## Адаптер Muse Code

Базовый путь: `muse exec --json --prompt-file ...` с сохранением JSONL, `externalRunId`/session ID, exit code и `muse export --session ...` для аудита. Для интерактивных `status`, `steer`, `cancel` и потоковых событий предпочтителен versioned session protocol через `muse serve` / официальный SDK, если установленная версия и среда это поддерживают. Нужен capability probe при запуске: версия, `muse schema`, WSL/Windows, trust, hook, event resume, cancellation. `muse exec` через текущий [delegate.ps1](delegate.ps1) пока подтверждает только submit/fetch/result, поэтому остальные методы первой реализации честно `unsupported`.

Meta документирует event log, восстановление сессий, `muse exec --json`, `muse serve` и управляемые subagents: [Muse Code](https://dev.meta.ai/docs/muse-code), [Extending and automating](https://dev.meta.ai/docs/muse-code/extending), [Research](https://research.meta.ai/blog/introducing-muse-code-and-muse-spark-1-2). При этом текущий мост работал в untrusted workspace, где project hooks не загружались; перед пишущими запуском требуется закрыть `GUARD-COMPAT-APPLY` и проверить E2E. Параметры модели и стоимости читать из фактической сессии, не выводить из названия провайдера.

## Внедрение

1. **Готово в v1 (MVP + Observability):** инвентаризирован текущий checkout после удаления Flowise. Локальный HTTP-пульт показывает доску, рантаймы, очередь Muse (стадии `inbox`, `processing`, `outbox`, provenance без утечки prompt), журнал ротации, состояние движка и риска.
2. **Применён патч Guard Compatibility:** решение человека `GUARD-COMPAT-APPLY` позволило обновить hook-конфиги и адаптер. Локальные тесты адаптера прошли, но E2E-отказы клиента с записью `deny` в `data/guard.log` ещё не подтверждены. Поэтому запуск через панель остаётся закрытым.
3. **Наблюдение за запусками в v1:** `GET /api/runs` читает локальные записи `data/runs/`; `GET /api/capabilities` не объявляет `start`, пока для клиента нет E2E-подтверждения. У записей есть локальный ID, курсор локальных событий и редактирование типичных секретов; поток реальных событий и точная стоимость клиента пока не подключены. Исчезновение процесса переводит запись в `unknown` и блокирует повтор до сверки. `POST /api/run/cancel` возвращает `unsupported` и не завершает процесс по PID.
4. **Следующие шаги:** E2E guard каждого клиента; подтверждённая сессия/результат CLI; worktree lease и атомарный claim перед пишущим стартом; безопасная отмена с подтверждением конечного состояния; event ingest и стоимость с provenance. Затем SSE / live stream и при необходимости LangGraph как клиент API.

Критерий готовности control plane: одна задача проходит claim → run → events → проверка → запись `done` или handoff без двойного запуска; после рестарта состояние восстанавливается; ни один неподтверждённый запуск, отказ guard или неизвестное внешнее действие не повторяется автоматически.
