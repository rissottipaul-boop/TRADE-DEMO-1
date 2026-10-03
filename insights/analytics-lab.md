# Аналитика стратегий и результатов (ANALYTICS-LAB)

- **Дата:** 2026-10-03
- **Тема:** Инструменты аналитика для работы с бэктестами, журналом сделок, базой инсайтов и метриками риска (п. 31–38 пользовательского регламента)
- **Статус:** implemented (ANALYTICS-LAB, 2026-10-03)
- **Задача:** ANALYTICS-LAB
- **Решение, которое принимается:** Реализован единый пакет `src/analytics` со строгим разделением ответственности: только чтение (read-only), все базы SQLite открываются через `file:...?mode=ro`, никаких ордеров на биржу и приватных API-запросов, никаких модификаций параметров риска или лимитов `src/risk.py` и `pump-pocket.json`.

---

## 1. Назначение и соответствие пунктам требований

Пакет `src/analytics` предоставляет агенту-исследователю (Crypto Insight Hunter) и разработчику (Insight Executor) инструментарий для аналитической работы:

| № | Пункт регламента | Модуль / CLI | Источник данных / Движок | Режим безопасности |
|---|---|---|---|---|
| **31** | Перевод вопроса в параметры бэктеста | `src.analytics.bt_config`<br>`bt-draft`, `bt-run` | `draft_from_text` → `validate` → `src.backtest.engine.Backtest` | Holdout (182 дн.) исключён по умолчанию; in-memory SimRisk; только локальные свечи `market_data.db` |
| **32** | Сравнение прогонов | `src.analytics.bt_compare`<br>`bt-compare` | Файлы прогонов `data/analytics/runs/<id>.json` | Атрибуция различий: данные vs издержки vs версия стратегии vs параметры; арифметика Δnet = Δgross − Δcosts |
| **33** | Сводка по результатам тестирования | `src.analytics.bt_summary`<br>`bt-summary` | `BacktestResult.metrics()` | Полный отчёт: доходность, MDD, комиссии, slippage, сделки, информационный гейт GateConfig, обязательный блок ограничений |
| **34** | Автоматический разбор закрытых сделок | `src.analytics.trade_review`<br>`trade-review` | `data/pump_journal.jsonl`, `pump-pocket.json` | Проверка 8 правил роли Pump Risk Taker (обоснование, algo-стоп, дистанция, лимит риска, размер позиции, метка `pmp`, свежесть, полнота выходов); вопросы для разбора |
| **35** | Поиск противоречий в гипотезе | `src.analytics.hypothesis`<br>`hypothesis` | Event study по `market_data.db` (с учётом издержек CostModel) | Разделение на «Поддерживает», «Противоречит», «Контекст внутри горизонта» (MAE/MFE) и «Чего не хватает» |
| **36** | Ответы по базе инсайтов | `src.analytics.insights_search`<br>`insights` | Файлы `insights/*.md` | Поиск по разделам, расчёт релевантности, выявление даты, статуса, источников и предупреждений об устаревании (>30 дн.) |
| **37** | Сценарный анализ риска | `src.analytics.risk_scenarios`<br>`risk-scenarios` | `data/risk_state.db` (read-only snapshot) + `src.backtest.risk_sim.SimRisk` | Моделирование последствий шоков (equity, обвал цены, серия убытков, гэп) в памяти без изменения реальных лимитов |
| **38** | Объяснение метрик панели | `src.analytics.panel_metrics`<br>`panel-metrics` | `data/risk_state.db`, `data/bot_state.db` (read-only) | Формулы и источники: Equity (totalEq), HWM (пик update_equity), Baseline (внесённые деньги без внешних скачков >20%), Просадка (карточка vs спарклайн 7д) |

---

## 2. Команды CLI

Все подкоманды поддерживают флаг `--json` (для программного чтения субагентами) и `--md <path>` (для сохранения Markdown-отчёта):

```powershell
# 31. Черновик конфигурации по вопросу на естественном языке
python -m src.analytics bt-draft "Как mean reversion на BTC 1H с 2022 при RSI ниже 25 и комиссии x1.5?" --out cfg.json

# 31 + 33. Валидация и запуск бэктеста существующим движком
python -m src.analytics bt-run cfg.json --md report.md

# 33. Сводка уже выполненного прогона
python -m src.analytics bt-summary data/analytics/runs/<run_id>.json

# 32. Сравнение двух прогонов
python -m src.analytics bt-compare data/analytics/runs/run1.json data/analytics/runs/run2.json

# 34. Разбор закрытых сделок памп-кармана
python -m src.analytics trade-review
python -m src.analytics trade-review --last 3
python -m src.analytics trade-review --trade pmp95156ede93b142439bcfb69b022ad

# 35. Проверка гипотезы (event study)
python -m src.analytics hypothesis --claim "RSI ниже 30 даёт отскок за 24 ч" --signal rsi_below --param level=30 --horizon 24 --stop-pct 2.0 --factor funding

# 36. Поиск по базе инсайтов
python -m src.analytics insights "глобальный breaker просадка HWM" --top 5

# 37. Сценарный стресс-тест риска
python -m src.analytics risk-scenarios

# 38. Пояснение метрик пульта и дашборда
python -m src.analytics panel-metrics
```

---

## 3. Архитектура и гарантии безопасности

1. **Режим доступа к данным:**
   - Базы `data/market_data.db`, `data/risk_state.db`, `data/bot_state.db` открываются исключительно через URI `file:...mode=ro`.
   - Сетевых вызовов к закрытым эндпоинтам биржи нет; ключи API не требуются и не используются.
   - Запись разрешена только для артефактов аналитики в каталоге `data/analytics/runs/` и путей, явно переданных пользователем через аргументы `--out` / `--md`.
2. **Нерушимость риск-лимитов (AGENTS.md §2):**
   - Ни одна команда аналитики не изменяет лимиты `src/risk.py` или `pump-pocket.json`.
   - В сценарном анализе (`risk-scenarios`) состояние копируется в отдельный `_MemoryRiskCore` (`:memory:`), где отрабатывает штатная логика ядра без затрагивания файла на диске.
3. **Защита от оверфиттинга и Anti-Lookahead:**
   - При подготовке бэктеста и гипотезы последние 182 дня (holdout) по умолчанию исключаются. Касание holdout допускается только при явном флаге `--touch-holdout` с обязательной фиксацией в аудит-журнале `holdout_touches`.
   - Исполнение сигналов бэктестера происходит строго по цене `open(i+1)` с учётом консервативного проскальзывания и комиссий.
