# OKX trading bot (demo-first)

Автоматизация торговли на OKX: Python + CCXT, демо-режим по умолчанию. План и статус фаз — [insights/roadmap.md](insights/roadmap.md), текущая фаза — [insights/phase1-progress.md](insights/phase1-progress.md).

## Установка

```powershell
python -m venv .venv
.venv\Scripts\pip install -r requirements.txt
copy .env.example .env   # заполнить OKX_DEMO_* (ключ demo-торговли, без права withdraw)
```

## Команды

| Что | Команда |
| --- | --- |
| Smoke-тест (time, баланс, place/cancel в демо) | `python -m src.smoke_test` |
| Движок: WS public + private, реконсиляция, equity, риск | `python -m src.engine` или фоном: `ops\engine.ps1 start` / `status` / `stop` |
| Статус риска и метрики движка | `python -m src.ops status` |
| Kill-switch: отменить все ордера (включая algo) и остановить grid-ботов | `python -m src.ops kill "причина"` или создать файл `data/KILL` |
| Снять kill-switch / breaker вручную | `python -m src.ops reset kill` (`global`, `daily`) |
| Unit-тесты (без сети) | `python -m unittest discover -s tests -t .` |
| Самотесты модулей | `python -m src.risk_selftest`, `python -m src.order_router_selftest`, `python -m src.dca_selftest`, `python -m src.ws_business_selftest` |

## Агенты и автономность

Шесть Copilot-агентов в `.github/agents/` работают по общему регламенту [AGENTS.md](AGENTS.md) и доске задач [ops/board.md](ops/board.md). Claude Code работает по тем же правилам через [CLAUDE.md](CLAUDE.md).

- **Запуск автопилота:** в чате Copilot выбрать агента **Project Orchestrator** и написать «работай по доске». Он сам берёт готовые задачи, раздаёт их агентам-субагентам, проверяет результат и не останавливается, пока есть работа (хук `ops/hooks/autopilot.py`, лимит продолжений — `ops/autopilot.json`).
- **Вопросы к вам** агенты оставляют только на доске — задачи со статусом `needs-user`.
- **Периметр безопасности** держит хук `ops/hooks/guard.py` перед каждым вызовом инструмента у всех агентов (подключён в `.github/hooks/guard.json` и `.claude/settings.json`). Агенты сами могут всё, что снижает риск (kill-switch, отмена, остановка ботов), но физически не могут: торговать в live вне окна `ops/live-policy.json`, снимать kill-switch и breaker'ы, ослаблять лимиты риска, читать `.env`, выводить средства, удалять базы состояния, менять guard и политики. Отказы пишутся в `data/guard.log`.
- **Файлы, которые меняете только вы:** `ops/live-policy.json` (окно live-торговли), `ops/autopilot.json`, `pump-pocket.json`, `.env`.

## Модули `src/`

| Модуль | Назначение |
| --- | --- |
| `config.py` | Настройки из `.env`: режим demo/live, ключи, домен региона |
| `connector.py` | CCXT-клиент OKX (`expTime` в заголовке), синхронизация времени, отмена всех ордеров и ботов для kill-switch |
| `errors.py` | Карта кодов ошибок OKX с рекомендуемыми действиями |
| `ws_client.py` | Асинхронный WS: стакан по цепочке seqId, private login, reconnect; свечи candle* — через business-эндпоинт (`ws_urls(...).business`) |
| `storage.py` | SQLite-состояние движка (`data/bot_state.db`): ордера, сделки, позиции, equity |
| `reconciler.py` | Сверка локального состояния с биржей, метрика рассинхрона |
| `risk.py` | Риск-ядро: сайзинг, дневной лимит, max drawdown, блокировки, kill-switch (`data/risk_state.db`) |
| `engine.py` | Движок Фазы 1 |
| `order_router.py` | Единая точка выставления ордеров с риск-проверкой (пишет в `storage.py`; kill-switch — через `connector.emergency_stop`) |
| `ops.py` | Операторский CLI |

## Безопасность

- Ключи только в `.env` (он в `.gitignore`); права ключа — read + trade, без withdraw; для live — IP-whitelist.
- `OKX_MODE=live` включается только явно; demo- и live-ключи не смешиваются.
- Любой вход проходит `risk.check_entry_allowed`; выходы breaker'ами не блокируются.
