# Максимальная автономия клиентов: целевые настройки периметра

**Статус:** применено человеком 03.10 09:02; проверено оркестратором 09:04 (файлы совпадают с целевыми, tests.test_guard + test_guard_adapter 61/61 OK, канарейка в свежей сессии Claude отклонена, guard активен поверх bypassPermissions).
**Дата:** 03.10.2026, Asia/Qyzylorda (+05:00).
**Исполнитель:** Insight Executor (Copilot CLI).

## Применение одной командой

Готовые целевые файлы лежат в [autonomy-settings/](autonomy-settings/). Выполни из корня проекта:

```powershell
Copy-Item insights\autonomy-settings\claude.settings.json .claude\settings.json
Copy-Item insights\autonomy-settings\gemini.settings.json .gemini\settings.json
Copy-Item insights\autonomy-settings\muse.settings.json .muse\settings.json
.venv\Scripts\python.exe -m unittest tests.test_guard tests.test_guard_adapter
```

Hook-секции в файлах идентичны текущим: guard продолжает работать и в полном
авторежиме блокирует только правую колонку AGENTS.md §2 (live, kill-switch,
ослабление лимитов, секреты, вывод средств). Обычную работу агентов guard
не блокирует и сейчас.

## Контекст

Запрос человека 03.10 в чате: максимальная автоматизация Claude Code, Antigravity/Gemini,
Codex, Muse и Morphy; торговый guard сохраняется. E2E guard подтверждён для всех
четырёх клиентов ([launch-e2e.json](../ops/hooks/launch-e2e.json), решение человека
по GUARD-COMPAT-APPLY). Лаунчер [agent-rotate.ps1](../ops/agent-rotate.ps1) уже
переведён на data-driven автономию по `guard_status`.

Три файла ниже — периметр безопасности (`GUARDRAIL_FILES` в guard.py): guard
отклонил их правку агентом, менять их может только человек. Это не ослабление
guard: hook-секции не меняются, добавляются только разрешения автономии поверх
работающего guard (PreToolUse/BeforeTool продолжает запрещать правую колонку
AGENTS.md §2) и запреты на чтение секретов (ужесточение).

## 1. `.claude/settings.json` — целевое содержимое

`bypassPermissions` убирает все запросы разрешений; hooks при этом исполняются,
guard решает. `deny` закрывает чтение секретов инструментом Read.

```json
{
  "permissions": {
    "defaultMode": "bypassPermissions",
    "deny": [
      "Read(./.env)",
      "Read(./.env.*)",
      "Read(./data/control_panel_token.dpapi)"
    ]
  },
  "hooks": {
    "PreToolUse": [
      {
        "matcher": "*",
        "hooks": [
          {
            "type": "command",
            "command": ".venv/Scripts/python.exe ops/hooks/guard.py",
            "windows": ".venv\\Scripts\\python.exe ops\\hooks\\guard.py",
            "timeout": 20
          }
        ]
      }
    ],
    "Stop": [
      {
        "hooks": [
          {
            "type": "command",
            "command": ".venv/Scripts/python.exe ops/hooks/autopilot.py",
            "windows": ".venv\\Scripts\\python.exe ops\\hooks\\autopilot.py",
            "timeout": 20
          }
        ]
      }
    ]
  }
}
```

## 2. `.gemini/settings.json` — целевое содержимое

`tools.autoAccept` авто-одобряет безопасные (read-only) инструменты; полный
авторежим даёт лаунчер флагом `--approval-mode yolo`. Hook `BeforeTool` не меняется.

```json
{
  "tools": {
    "autoAccept": true
  },
  "hooks": {
    "BeforeTool": [
      {
        "matcher": "*",
        "hooks": [
          {
            "type": "command",
            "name": "project-guard",
            "command": ".venv\\Scripts\\python.exe ops\\hooks\\guard_adapter.py --client gemini",
            "timeout": 20000
          }
        ]
      }
    ]
  }
}
```

## 3. `.muse/settings.json` — целевое содержимое

Добавлен `approval_mode: auto` в обоих форматах (плоском и вложенном), остальное
без изменений. `.muse/hooks.json` не меняется.

```json
{
  "schema_version": 1,
  "settings": {
    "model": "muse-spark-1.3",
    "theme": "dark",
    "reasoning": "visible",
    "run.approval_mode": "auto",
    "run.subagent_delegation_mode": "auto",
    "run.workflow_trigger_mode": "auto"
  },
  "run": {
    "approval_mode": "auto",
    "subagent_delegation_mode": "auto",
    "workflow_trigger_mode": "auto"
  },
  "runtime_capabilities": {
    "observer_agents": true,
    "parallel_workers": 10
  }
}
```

## Проверка после применения

1. `.venv\Scripts\python.exe -m unittest tests.test_guard tests.test_guard_adapter` — guard цел.
2. Новая сессия Claude Code: безвредная канарейка из [guard-compat.md](guard-compat.md)
   (например, запись в `ops/hooks/`) должна быть отклонена с `[guard]`, обычная
   правка `src/` — пройти без запроса разрешения.
3. `ops\agent-rotate.ps1 -Role insight-executor -Plan` — план показывает
   yolo/workspace-write/trust-workspace.

## Morphy

Morphy ([оценка](morphy-local-evaluation.md)) — локальный портал со своим AI
(provider openai); его действия не проходят через общий guard. Автономия записи
для Morphy-агента до интеграции с `ops/hooks/guard_adapter.py` не включается —
задача MORPHY-GUARD-WIRE на доске.
