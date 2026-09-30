# OKX trading bot (demo-first)

Автоматизация торговли на OKX: Python + CCXT + okx CLI, демо-режим по умолчанию.
План и фазы — [insights/roadmap.md](insights/roadmap.md), пульт человека —
[Home.md](Home.md) (Obsidian) и локальная панель [Контур](ops/control-panel/README.md).

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
| PnL-отчёт по рукавам (только чтение) | `python -m src.pnl_ledger --days 7` |
| Бэктест стратегии | `python -m src.backtest run --strategy sma_cross --help` |
| Обзор доски для агентов (экономия контекста) | `.venv\Scripts\python.exe -m src.agent_context [--task ID]` |
| Локальная панель «Контур» (доска, рантаймы, движок, очередь) | `.venv\Scripts\python.exe -m src.control_panel` |
| Мост делегирования: отдать подзадачу Muse | `ops\delegate.ps1 submit -Prompt "..." -From claude`, `fetch -Id`, `status`, `start` |
| Ротация агентов (план без запуска моделей) | `ops\agent-rotate.ps1 -Role insight-executor -Plan` |
| Unit-тесты (без сети) | `python -m unittest discover -s tests -t .` |
| Самотесты модулей | `python -m src.risk_selftest`, `python -m src.order_router_selftest`, `python -m src.dca_selftest`, `python -m src.ws_business_selftest` |

## Агенты и автономность

Шесть ролей (Orchestrator, Insight Hunter, Insight Executor, OKX Trader,
Pump Risk Taker, Ops Sentinel) работают по общему регламенту [AGENTS.md](AGENTS.md)
и доске задач [ops/board.md](ops/board.md) на четырёх рантаймах: Claude Code,
Codex, Gemini CLI / Antigravity, Muse Code (см. [AGENTS.md](AGENTS.md) §10).

- **Мост делегирования:** любой рантайм отдаёт мелкие подзадачи исполнителям
  Muse через файловую очередь `ops/delegations/` (`ops/delegate.ps1`).
- **Вопросы к вам** агенты оставляют только на доске — задачи со статусом `needs-user`.
- **Периметр безопасности** держит хук `ops/hooks/guard.py` перед каждым вызовом
  инструмента у всех агентов. Агенты сами могут всё, что снижает риск (kill-switch,
  отмена, остановка ботов), но не могут: торговать в live вне окна
  `ops/live-policy.json`, снимать kill-switch и breaker'ы, ослаблять лимиты риска,
  читать `.env`, выводить средства, удалять базы состояния, менять guard и политики.
  Отказы пишутся в `data/guard.log`.
- **Файлы, которые меняете только вы:** `ops/live-policy.json` (окно live-торговли),
  `ops/autopilot.json`, `pump-pocket.json`, `.env`.

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
| `fleet_manager.py` | Каталог 13 типов ботов, квота 50, аллокация, сборка CLI-команд |
| `agent_context.py` | Адресное чтение доски для агентов (обзор вместо 230+ КБ) |
| `order_owner.py` | Генератор меток владельца ордеров (ORDER-OWNER-TAG) |
| `control_panel.py` | Локальная HTTP-панель «Контур» (только 127.0.0.1) |
| `pnl_ledger.py` | Учёт PnL по рукавам |
| `backtest/` | Бэктестер стратегий |
| `live_runner.py`, `live_policy.py`, `live_preflight.py` | Live-контур: окно торговли, предполётные проверки, раннер |
| `guard_adapter.py` | Адаптер guard для Codex/Gemini/Muse (совместимость входов) |
| `obsidian_status.py` | Снапшоты состояния для Obsidian-пульта |

## Безопасность

- Ключи только в `.env` (он в `.gitignore`); права ключа — read + trade, без withdraw; для live — IP-whitelist.
- `OKX_MODE=live` включается только явно; demo- и live-ключи не смешиваются.
- Любой вход проходит `risk.check_entry_allowed`; выходы breaker'ами не блокируются.
- Локальная панель слушает только `127.0.0.1`; порт наружу не публикуется.
