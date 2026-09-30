---
cssclasses:
  - wide-page
---
# Пульт OKX-бота

Только чтение: всё ниже собирается из файлов проекта при открытии и при изменении заметок. Работу агентам даёт доска, решения человека — кнопка «Ответить» у задачи `needs-user`. Как устроен vault — [Obsidian в проекте](notes/Obsidian%20в%20проекте.md).

```dataviewjs
await dv.view("notes/views/safety")
```

## Состояние сейчас

```dataviewjs
await dv.view("notes/views/live", { auto: true })
```

## Доска

```dataviewjs
await dv.view("notes/views/board")
```

## Мои ответы, ещё не на доске

```dataviewjs
await dv.view("notes/views/decisions")
```

## Инциденты

```dataviewjs
await dv.view("notes/views/incidents", { limit: 3 })
```

## Инсайты — последние правки

```dataviewjs
await dv.view("notes/views/insights", { limit: 10 })
```

## Документы

| Работа | Правила | Деньги и риск |
| --- | --- | --- |
| [Доска задач](ops/board.md) | [Регламент агентов](AGENTS.md) | [План заработка](insights/business-plan.md) |
| [Журнал инцидентов](ops/incidents.md) | [Roadmap](insights/roadmap.md) | [Риск-ядро](insights/risk-core.md) |
| [Прогресс Фазы 1](insights/phase1-progress.md) | [README](README.md) | [Учёт PnL](insights/pnl-ledger.md) |
| [Демо-флот ботов](insights/bot-fleet-demo.md) | [OKX API](insights/okx-api.md) | [Памп-карман](insights/pump-feed.md) |
| [Карта проекта](notes/Карта%20проекта.canvas) | [Все инсайты (база)](notes/Инсайты.base) | [Мои решения (база)](notes/Решения.base) |
| [Отчёты PnL](data/reports/) | [Снимок пульта](data/obsidian/status.json) | [Сторож](logs/autostart.log) |

## Команды

Запускаются в терминале из корня проекта. В Obsidian их можно только скопировать.

| Что | Команда |
| --- | --- |
| Риск и метрики движка | `.venv\Scripts\python.exe -m src.ops status` |
| Движок: статус / старт / стоп | `ops\engine.ps1 status` |
| **Аварийная остановка торговли** | `.venv\Scripts\python.exe -m src.ops kill "причина"` |
| Чьи ордера на счёте | `.venv\Scripts\python.exe -m src.order_audit --hours 24` |
| PnL по рукавам за неделю | `.venv\Scripts\python.exe -m src.pnl_ledger --days 7` |
| Остатки памп-кармана | `.venv\Scripts\python.exe -m src.pump_journal` |
| Обновить снимок пульта | `.venv\Scripts\python.exe -m src.obsidian_status` |
| Сторож и автозапуск | `ops\autostart.ps1 status` |
