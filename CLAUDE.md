@AGENTS.md

Claude Code в этом проекте работает по тому же регламенту, что и Copilot-агенты: доска `ops/board.md`, guard `ops/hooks/guard.py` (подключён в `.claude/settings.json`).

Основная сессия Claude Code — оркестратор (`.github/agents/project-orchestrator.agent.md`). Задачи доски она отдаёт профильным субагентам из `.claude/agents/`: `insight-executor`, `crypto-insight-hunter`, `okx-trader`, `ops-sentinel`, `pump-risk-taker`. Это обёртки ролей из `.github/agents/`: субагент читает свой файл роли и `AGENTS.md`. Результат оркестратор проверяет сам — тестами и критерием из доски.
