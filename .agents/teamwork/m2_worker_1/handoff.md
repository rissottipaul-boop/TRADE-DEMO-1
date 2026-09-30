# Handoff m2_worker_1 — Mean Reversion runner (M2/R2)

Дата: 2026-09-30. Ордеров не выставлялось (только dry-run трассировка + мок-роутер).

## Созданные файлы (единственные затронутые)

- `src/mr_trader.py` — раннер 1H: бар-буфер → `src/mean_reversion` (entry/exit/stop) →
  `risk.check_entry_allowed` + `risk.size_position` → `OrderRouter`
  (`place_order` / `place_exit_order` / `settle_exits`). Сигналы переиспользуются,
  не дублируются: дефолтный путь делегирует `mean_reversion.check_entry/check_exit`,
  калиброванный — тем же `backtest.meanrev.entry_signal/exit_signal/roi_required`
  с инжектированными параметрами.
- `tests/test_mr_trader.py` — 29 unit-тестов: фейковые бары + FakeRouter, ноль сети.

Ключевые решения:

- `dry_run=True` по умолчанию; dry-run не зовёт `register_entry` (не занимает слоты/heat).
- Live-вход на дефолтных параметрах ЗАПРЕЩЁН кодом (`blocked`/`default_params_live`):
  бэктест WF OOS net −17.96%, PF 0.54 — сначала MEANREV-CALIB.
- Владелец `botr` (`order_owner.ROUTER`): кода `botmr` в реестре репозитория нет,
  реестр побеждает PROJECT.md. Чужой/не-bot код — `ValueError` в конструкторе.
- Калибровочный хук `MRParams`: `rsi_entry`, `rsi_exit`, `stop_mult`, `roi_table`,
  `time_stop_min`, `fee_pct`, `risk_pct`, `exit_price_guard`.

## Проверка

Команда (из корня):

`.venv\Scripts\python.exe -m unittest tests.test_mr_trader tests.test_mean_reversion_strategy tests.test_meanrev`

Результат: `Ran 58 tests … OK`, exit 0. Из них 29 новых `test_mr_trader`
(настоящий сигнал seed 4 на баре 201, форсированные входы/выходы stop/roi/time_stop,
live-гейт дефолтов, делегирование mean_reversion, инъекция параметров, settle).

## Покрытие AC

- R2 «сигналы по барам 1H → валидация размера через `src/risk.py` → `src/order_router.py» —
  закрыт кодом раннера; live-путь упирается в честный гейт калибровки.
- «Unit-тесты с моками» для модуля — закрыт (29 тестов, FakeRouter, мок индикаторов).
- «Полный регрессионный сьют 100%» — НЕ запускался: вне владения (параллельные воркеры
  мутят дерево), оркестратору прогнать `discover` после слияния.
- Demo-проверка на бирже — не выполнялась: запрещена диспетчем (дефолты убыточны).

## Блокеры

Нет. Следующий шаг (не мой): MEANREV-CALIB подбирает параметры → оркестратор решает
о demo-прогоне калиброванного `MRTrader(dry_run=False)` отдельным допуском.
