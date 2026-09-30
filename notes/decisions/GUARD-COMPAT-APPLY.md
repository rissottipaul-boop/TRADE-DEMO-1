---
task: GUARD-COMPAT-APPLY
date: 2026-09-30 10:31
status: перенесено
---
# Решение по GUARD-COMPAT-APPLY

**Задача:** Применить патч совместимости guard для Codex, Gemini и Muse и пройти E2E

**Вопрос с доски:**

> Вопрос: применить патч `insights/guard-compat-patch.diff` (адаптер переезжает в `ops/hooks/`, `.gemini/settings.json` → `BeforeTool` + 20000 мс, `.muse/hooks.json` — прямые слэши, новый `.codex/hooks.json`, в guard.py — `.codex/` в периметре и фикс абсолютного пути с пробелами)? Варианты: (а) целиком + E2E по сценарию; (б) только `ops/hooks/guard.py` + `tests/test_guard.py` (фикс для Claude и `.codex/` в периметре), клиенты пока без hooks; (в) не применять — Codex остаётся read-only, Gemini plan, Muse только анализ. Рекомендация: (а); если нет времени на E2E — хотя бы (б), он закрывает дыру и у Claude. Перед применением сверить SHA256 `src/guard_adapter.py` из guard-compat.md. После E2E Muse — отдельно решить, добавлять ли `--trust-workspace` в мост `ops/delegate.ps1` (без него hooks и AGENTS.md в Muse не грузятся).

## Решение

вариант а

## Почему

