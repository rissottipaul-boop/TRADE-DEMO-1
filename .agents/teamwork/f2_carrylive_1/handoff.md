# Handoff: f2_carrylive_1 — follow-up 2 (ПРЕРВАН оркестратором)

- Дата: 2026-09-30 (~16:20–16:30 +05). Агент: f2_carrylive_1 (teamwork).
- Диспетч: `.agents/teamwork/f2_carrylive_1/DISPATCH.md` (follow-up 2).
- Первые чтения выполнены: AGENTS.md §2/§6 · m1 handoff · e2e handoff · src/carry_executor.py · insights/funding-carry.md.
- Статус: **ОСТАНОВЛЕНО по прерыванию до dry_run и выставления. Ордеров НЕ ставилось, доска НЕ тронута, код НЕ менялся, плечо НЕ менялось.**

## 1. Пре-чеки (цифры, свежие)

- `src.ops status`: mode demo; kill/breakers off; entries_today 0/10; open_risk [] (оба слота свободны); engine жив (P1-72H, reconciles 770, divergences 0, errors 24 = все errors_exchange, internal 0); open_orders_local [].
- `account_mode status`: **acctLv 2** (Spot and futures), posMode long_short_mode, spot tdMode cash, autoLoan false.
- Позиции (`okx --demo account positions`): **[]** — пусто.
- Реестр: `botcar` (CARRY, kind=strategy) **зарегистрирован** в src/order_owner.py — ожидание диспетча `botcar*` выполнимо.

## 2. Фандинг (read-only, `--json`)

- BTC live текущий: **+0.0000360348/период** (~+3.95% APY).
- BTC demo последние 3: **-0.0021085575, -0.0002678197, -0.0011443210** (все отрицательные; свежий ≈ −231% годовых) → **BTC ОТВЕРГНУТ** гейтом «non-negative».
- ETH live текущий: **+0.0000526446/период** (~+5.77% APY).
- ETH demo последние 9: **все положительные** (0.0000709071, 0.0000826624, 0.0000894806, 0.0000716580, 0.0000739836, spike 0.0034421191, 0.0001, 0.0000474545, 0.0000599037). Среднее последних 5 (без спайка): 0.0000777383/период ≈ **8.51% APY** → breakeven taker ≈ 12.9 дн < плановых 30 дн; funding_exit_signal (amort 0.0000333/период) = держать. **ETH проходит гейт.**

## 3. Цены и спеки (демо, `--json`)

- BTC: spot last 83989.2 (bid 83849.9 / ask 83989.2), swap last 83814.7.
- ETH: spot last 2693.92 (bid/ask 2693.92/93), swap last 2692.68.
- Спот: BTC-USDT lotSz 1e-8, minSz 0.00002; ETH-USDT lotSz 1e-6, minSz 0.00073.
- Своп: BTC-USDT-SWAP ctVal 0.01, lotSz 0.01, **minSz 0.01**; ETH-USDT-SWAP ctVal 0.1, lotSz 0.01, **minSz 0.01** → дробные контракты разрешены, баунд ≤$100/сторона **выполним** (мин. BTC-своп ≈ $8.40, мин. ETH-своп ≈ $2.69).
- Плечо isolated (`swap get-leverage --mgnMode isolated`): **3x на long и short, оба инструмента** → перед выставлением обязательно `swap leverage --lever 1 --mgnMode isolated --posSide short` (+long симметр., решение преемника). **НЕ выполнялось.**

## 4. Что НЕ сделано (оборвано)

1. Проверка posSide/tdMode в CCXT okx.py для hedge-режима (команда отменена песочницей) — роутер для свопов шлёт extra_params={}; критично для атомарности своп-ноги.
2. `build_pair` dry_run (план был: ETH, headline_apy ≈ 0.0851, hold 30d, taker, sleeve equity 600 → ~$89/сторона: спот 0.033 ETH, своп 0.33 ct).
3. Установка плеча 1x, выставление пары, re-query верификация.
4. Доска: ни TEAMWORK-M1, ни MON-CARRY-PAIR не тронуты (freshness-чек не проводился — записи не было).

## 5. План для преемника

1. Дочитать CCXT `.venv/Lib/site-packages/ccxt/okx.py` (posSide в hedge-режиме); при проблеме — ABORT (код менять запрещено).
2. Свежие пре-чеки заново (статус/режим/фандинг — цифры выше устареют).
3. `swap leverage --lever 1 --mgnMode isolated` (демо, ужесточение — разрешено).
4. dry_run → одно `place_pair(dry_run=False)` → re-query → доска → handoff.

## 6. Блокеры

- Внешний STOP от оркестратора (это прерывание).
- Потенциальные для преемника: posSide hedge (не проверен); плечо 3x (не снижено); BTC-фандинг отрицателен (брать ETH).
