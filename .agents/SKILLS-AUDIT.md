# Аудит `.agents/skills` — 30.09.2026

Покрытие: 63 папки скиллов. У всех есть `SKILL.md`, у всех есть `description`.

## Исправлено

7 скиллов отклонялись загрузчиком (`invalid skill name` — `name` не в формате `^[a-z0-9-]+$`).
`name` приведён к имени папки, проверено повторным поиском:

| Папка | Было | Стало |
| --- | --- | --- |
| `crypto-swing-signal-analyst` | 币圈波段信号分析师 | `crypto-swing-signal-analyst` |
| `hindsight-reviewer` | 事后诸葛亮 | `hindsight-reviewer` |
| `less-is-more` | 少即是多 | `less-is-more` |
| `speed-hunter` | 极速猎手v1 | `speed-hunter` |
| `stochrsi-mdi-trend-v1` | StochRSI-MDI 趋势追踪 V1.1 | `stochrsi-mdi-trend-v1` |
| `trendline-symmetry-breakout` | 趋势线对称形态突破交易系统 | `trendline-symmetry-breakout` |
| `whale-tracker` | 鲸鱼追踪者 | `whale-tracker` |

Остальные 56 имён уже совпадают с папками (`NeuroGrid_V4_Milestone` в кавычках — загрузчик принимает, не трогал).
Вступят в силу при следующей загрузке каталога скиллов (перезапуск сессии).

## Лок `skills-lock.json` — дрейф, не чинил

Все 10 `computedHash` НЕ совпадают с текущими SHA256 файлов (файлы правились после установки
или хеш считался по другой схеме). Лок нигде кодом не проверяется — только упоминание
в `insights/futures-bots.md` (какие скиллы «официальные» из `okx/agent-skills`).
Рекомендация: перегенерить лок через `okx-cex-skill-mp` (там есть verify) или зафиксировать,
что он advisory. Остальные 53 скилла — локальные, в лок не входят (так задумано).

## Коллизии триггеров (маршрутизация неоднозначна)

1. **«复盘» заявляют 4 скилла:** `hindsight-reviewer`, `pnl-loss-reviewer`, `trade-mirror`,
   `okx-trade-review-suite` (+ рядом `okx-review-prism`, `okx-review`). Нужен один дефолт,
   остальным — нишевые триггеры или `Do NOT` оговорки.
2. **DCD:** `dcd-auto-trader` («используй меня, а не okx-cex-earn») против `okx-cex-earn`
   (покрывает Dual Investment). Прямое противоречие в подсказках.
3. **Торговые планы:** `kline-indicator` и `trading-plan-generator` оба строят план
   входа/целей/стопа.
4. **BTC-перпы (5):** `btc-trader` (только сигналы), `okx-btc-contract` (полная система),
   `btc-usdt-swap-defensive-ai`, `okx-cex-volatility-strategy`, `btc-naked-k-breakout` —
   триггер «BTC合约» общий, философии разные. Наблюдать, не дефект.
5. **`market-intel`** ссылается на несуществующий `cmc-mcp` — вероятно, имелся в виду `cmc-okx`.

Описания (UX-копи) не правил — это решение человека.

## Мусор (не удалял — чужие файлы)

- `.agents/teamwork/`: 8 подпапок (`m1_*`, `orchestrator_1`, `sentinel_1`, `survey_*`) —
  рабочие заметки утреннего teamwork-прогона 30.09 (12:02–12:19), задачи уже `done`.
  Можно архивировать/удалить после подтверждения человека.
- Блуждающие копии с `name:`-фронтматтером внутри папок скиллов (лоадер их не видит,
  безвредны): `btc-naked-k-breakout/btc-naked-k-breakout/`, `okx-btc-contract/target_skill_optimized_V2.2/`,
  `dcd-auto-trader/templates/soul_dcd.md`.
- 8 вложенных `okx-trade-review-suite/skills/okx-review-*/` лоадером не подхватываются —
  нормально, если suite ссылается на них явно.
- User-scope скилл `quadruple-filter-trend-hunter` (вне репозитория) тоже с битым именем —
  вне досягаемости из этой сессии.

## Не трогал сознательно

- `.muse/hooks.json` — guard-периметр (AGENTS.md §2, только человек; уже есть задача GUARD-COMPAT-APPLY).
- `ops/board.md` — в активном claim Codex (AGENT-PANEL-HANDOFF), правка доски не требовалась.
