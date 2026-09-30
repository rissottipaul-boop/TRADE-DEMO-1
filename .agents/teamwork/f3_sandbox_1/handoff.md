# f3_sandbox_1 — handoff (follow-up 3, ПРЕРВАНО оркестратором)

Время остановки: ~16:30 UTC+5, 30.09.2026. Периметр guard НЕ тронут
(ops/hooks, autopilot.json, live-policy.json, pump-pocket.json, настройки клиентов —
только чтение). Движок не перезапускался, ордеров нет.

## Итог: 44/44 красных из скоупа закрыты правками только тестов

| Файл | Было | Стало | Фикс (только код тестов) |
|---|---|---|---|
| tests/test_agent_rotate.py | 21 fail | 12/12 OK | strip префикса `\\?\` песочницы с temp-пути: Join-Path в PS 5.1 падает на extended-путях («value of argument "drive" is null») — доказано пробой f3_probe_join |
| tests/test_pump_sched.py | 3 error | 3 skip (честно) | детект pwsh пробным запуском: в PATH только 0-байтный Store-стаб WindowsApps/pwsh.exe, настоящий pwsh 7 НЕ установлен (WinError 1920) |
| tests/test_engine_watchdog.py | 3 error | 3 skip (честно) | тот же фикс детекта pwsh |
| tests/test_guard.py | 13 fail | OK | strip `\\?\` с тест-ROOT: префикс уродовал входы (мусорные `file:////%3A?...`) и корень guard через loader-path; прод-эквивалент восстановлен |
| tests/test_guard_adapter.py | 4 fail | OK | тот же фикс ROOT (WSL_ROOT/WIN_ROOT и корень адаптера) |
| guard-файлы вместе | 17 fail | 61/61 OK | — |

Изменённые файлы (5, все в собственности): tests/test_agent_rotate.py,
tests/test_pump_sched.py, tests/test_engine_watchdog.py, tests/test_guard.py,
tests/test_guard_adapter.py. ops/*.ps1 НЕ менялись — не понадобилось.

## Финальный полный discover (обязательный, выполнен)

- До (baseline оркестратора): `Ran 1033, FAILED (failures=38, errors=6)`
- После: `Ran 1040, FAILED (failures=2, skipped=6)` — лог `f3_final_discover.log`
  (в корне репо, удалить при уборке).
- Все 44 из моего скоупа зелёные (38 pass + 6 skip по отсутствию pwsh 7).

## Остаток: 2 красных ВНЕ скоупа — не мои, не трогал

- tests/test_pnl_ledger.py: test_warnings_and_notes, test_every_registered_owner_has_a_sleeve
  (`missing ['botcar', 'botmr']` в ops/sleeves.json).
- Причина: параллельный агент правил src/order_owner.py в 16:11 (mtime во время
  моей сессии) — добавил коды без строк в sleeves.json. По AGENTS.md §5 (чужой
  claim) не вмешивался. +7 тестов к baseline (1033→1040) — тоже его работа.

## needs-user / blocked

- Нет. Вопросов человеку нет. В скоупе незакрытого нет.
- Env-заметка (не блокер): для реального прогона 6 скипнутых ps1-тестов нужен
  установленный pwsh 7 (`C:\Program Files\PowerShell\7\pwsh.exe` отсутствует).

## На чём застрял / не успел (прерывание)

- Не успел проверить `git status` (git под sandbox-uid падает на dubious ownership)
  и удалить свои scratch-файлы в корне репо — удалить: `f3_repro_rotate.log`,
  `f3_repro_rotate2.log`, `f3_repro_guard.log`, `f3_repro_guard2.log`,
  `f3_final_discover.log`, `f3_orch_excerpt.txt`, `f3_probe_psroot.py`,
  `f3_probe_join.py`, `f3_summ_guard.py`, `f3_psroot.ps1`, `f3_probe_join.ps1`.
- Запись в .agents через shell запрещена политикой — этот handoff записан
  через muse.write_file; если файл не виден, весь текст продублирован в ответе.
