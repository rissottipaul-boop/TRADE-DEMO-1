# Мост делегирования Claude / Antigravity / Codex → Muse

Любой рантайм кладёт заявку в `inbox/`, исполнитель Muse забирает её и пишет
результат в `outbox/`. Раннер — `ops/delegate.ps1` (persistent loop + автозапуск).

## Быстрый старт (из любого рантайма)

```powershell
# 1. Отдать подзадачу
$id = ops\delegate.ps1 submit -Prompt "Прочитай src/risk.py и перечисли все пороги" `
    -From claude -Role crypto-insight-hunter

# 2. Забрать результат (pending — ещё исполняется)
ops\delegate.ps1 fetch -Id $id
```

Без PowerShell — прямая запись JSON (экранируй как удобно твоему рантайму):

`inbox/<id>.json`:

```json
{
  "id": "d20260930-064500-1A2B",
  "from": "codex",
  "role": "insight-executor",
  "prompt": "Напиши tests/test_example.py ...",
  "timeout_min": 20
}
```

Поля: `prompt` — обязательно, непустая строка; `timeout_min` — 1..120, по умолчанию
20; `from`/`role` — строки для журнала. Имя файла должно совпадать с `id`.

## Результат

`outbox/<id>.json`:

```json
{
  "id": "d20260930-064500-1A2B",
  "status": "done",
  "exit_code": 0,
  "elapsed_s": 97,
  "killed": true,
  "finished": "2026-09-30T06:47:00+05:00",
  "error": "",
  "output_tail": "последние ~4 КБ вывода..."
}
```

`status`: `done` (exit 0), `error` (ненулевой exit, битая заявка, нет muse),
`timeout` (лимит исчерпан, `killed` — прибит ли процесс). Полный вывод — `outbox/<id>.log`,
промт с шапкой — `outbox/<id>.prompt.md`, исходная заявка уезжает в `done/`.

## Правила исполнителя

Каждая заявка дополняется шапкой: прочитать и буквально соблюдать `AGENTS.md`,
включая §2 — действия правой колонки (live-торговля, сброс kill-switch/breaker,
ослабление лимитов, секреты, удаление данных, правка guard/автопилота, вывод
средств) запрещены. Политика full access (правки кода разрешены) — решение
человека 30.09.2026; guard Muse при этом остаётся непроверенным
(`wsl-and-trust-unverified`, задача AGENT-GUARD-COMPAT).

## Управление раннером

```powershell
ops\delegate.ps1 status        # раннер + глубины очередей + хвост лога
ops\delegate.ps1 start         # persistent loop, опрос раз в 30 с
ops\delegate.ps1 stop          # штатная остановка
ops\delegate.ps1 run-once      # один проход по очереди, без цикла
ops\delegate.ps1 check         # доступен ли muse (до 120 с на холодный старт WSL)
ops\delegate.ps1 register      # автозапуск при входе (выполняет человек)
ops\delegate.ps1 unregister    # убрать автозапуск
```

Пауза без остановки — файл `PAUSED` в этом каталоге. Лог — `logs/delegation.log`.
Исполнитель ищется так: `$env:DELEGATE_MUSE_CMD`, иначе `ops/muse.cmd` (WSL),
иначе `muse` из PATH. Доп. флаги — `$env:DELEGATE_MUSE_ARGS`.

## Smoke (для DELEG-SMOKE)

```powershell
ops\delegate.ps1 check
$smoke = ops\delegate.ps1 submit -Prompt "Выведи в stdout одну строку: SMOKE-OK. Ничего не меняй." -From human -Role ops-sentinel
ops\delegate.ps1 run-once
ops\delegate.ps1 fetch -Id $smoke   # ждём status done и SMOKE-OK в output_tail
```

Если раннер уже крутится (`status` — работает), `run-once` не нужен: заявка
подберётся в течение 30 с.
