# Фаза 0 — результаты smoke-теста OKX Demo

- **Дата:** 2026-09-24
- **Статус:** validated (проверено на живом демо-API через okx CLI 1.4.8)

## Что проверено

| Тест | Результат | Детали |
|---|---|---|
| Баланс (demo) | ✅ | `okx --demo account balance` — демо-средства видны (BTC и др.) |
| Публичный тикер | ✅ | `okx market ticker BTC-USDT` — last 84373.1 |
| Параметры инструмента | ✅ | `okx market instruments --instType SPOT --instId BTC-USDT` |
| Place limit order (demo) | ✅ | `ordId` выдан, `sCode: 0` |
| Orders-pending (demo) | ✅ | ордер виден в `okx --demo spot orders` |
| Cancel order (demo) | ✅ | `sCode: 0` |
| Grid bot API | ✅ | `okx bot grid create/amend/stop/positions/liquidate-price` доступны, `--demo` поддержан |

## Важные находки

1. **Минимальный ордер BTC-USDT ≠ minSz.** `minSz = 0.00001 BTC`, но ордер 0.0001 BTC по цене 50000 (~$5) прошёл, а 0.00001 (~$0.85) отклонён с `sCode 51020` ("should meet or exceed the minimum order amount"). Минимум считается по **стоимости**, а не только по размеру — для BTC-USDT практический минимум ~$1–5. В боте: проверять `sz * px >= minNotional` (уточнить эмпирически).
2. **Синтаксис CLI:** опции camelCase (`--instId`, `--ordType`, `--maxPx`), не kebab-case; субкоманды через пробел.
3. **Ответ place/cancel:** смотреть `sCode`/`sMsg` в `data[]` — верхнеуровневый код не показывает отказ (51020 пришёл при успешном HTTP).
4. **Grid-бот в демо работает** — закрыт открытый вопрос №2 из spot-strategies.md: tradingBot-эндпоинты доступны в Demo Trading. Можно валидировать grid-идеи через биржевого бота до написания своей реализации.

## Критерий выхода Фазы 0 — выполнен

- [x] Ордер ставится/отменяется в демо
- [x] Ошибки API распознаются (51020 пойман и интерпретирован)
- [x] Скаффолд проекта (Python + CCXT + .env) — реализован (`src/`: config, connector, storage, engine, reconciler, ws_client, smoke_test; `.env.example` полон: OKX_MODE, OKX_DEMO_*, OKX_*, OKX_DOMAIN; `.env` покрыт `.gitignore`)
- [x] Прогон smoke-теста 2026-09-24: `python -m src.smoke_test` (venv, ccxt 4.5.83) — «SMOKE-ТЕСТ ПРОЙДЕН»; дрейф часов +149 мс (< 5 с); баланс демо USDT free 5000.0; лимитный ордер BTC/USDT поставлен и отменён (id 3949218045114658817)
- [x] NTP-синхронизация — проверена при smoke-прогоне, дрейф +149 мс в норме

**Примечание (2026-09-24):** реализованы `src/errors.py` (карта кодов OKX + `explain_error`, обёртка `okx_call` в `src/connector.py`) и `src/state.py` (SQLite state.db: orders/positions/trades/equity_snapshots). Smoke-тест пишет размещённый/отменённый ордер в БД: повторный прогон «SMOKE-ТЕСТ ПРОЙДЕН», SELECT показывает ордер `BTC-USDT` (exchange_order_id 3949235454731038721) со статусом `canceled`.

**Примечание (2026-09-24, ARCH-DEDUP):** `src/state.py` консолидирован в канонический `src/storage.py` и удалён; smoke-тест пишет ордер в `data/bot_state.db` (таблица `orders`, сырые состояния OKX). Файл `state.db` в корне — исторические данные, не используется кодом.
