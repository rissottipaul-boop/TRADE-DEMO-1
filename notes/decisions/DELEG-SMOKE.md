---
task: DELEG-SMOKE
date: 2026-09-30 07:45
status: перенесено
---
# Решение по DELEG-SMOKE

**Задача:** Включить мост делегирования: проверка и первый живой прогон

**Вопрос с доски:**

> Вопрос: выполнить из корня проекта в PowerShell 7 три команды: 1) `ops/delegate.ps1 check` (проверка WSL+muse), 2) тестовую заявку из `ops/delegations/README.md` §Smoke, 3) `ops/delegate.ps1 register` (автозапуск раннера)? Агент из песочницы не может сам: WSL для muse-sbx-u1 — E_ACCESSDENIED, нужен пользователь GHOST. Рекомендация: да. Откат — `ops/delegate.ps1 unregister`.

## Решение

да

## Почему

