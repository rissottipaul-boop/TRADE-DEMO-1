# Strategy Parameter Reference

Quick reference for all formulas, data windows, sentiment scoring, execution sizing, and hard risk rules used by `okx-cex-volatility-strategy`.

---

## 1. Bar Convention

Use 1H data only.

| Symbol | Meaning |
|---|---|
| `index 0` | Live / forming 1H bar. Never use for signal generation |
| `c1 = index 1` | Last closed 1H bar |
| `c2 = index 2` | Previous closed 1H bar |
| `window 1..30` | Last 30 closed 1H bars |

This convention is mandatory for:

- EMA / RSI / MACD signal checks
- Bollinger bandwidth percentile
- ATR spike guard

---

## 2. Required Inputs

| Data | Command / Tool | Output used |
|---|---|---|
| Instrument meta | `market instruments` / `market_get_instruments` | `ctVal`, `tickSz`, `lotSz`, `minSz`, `state` |
| Candles | `market candles` / `market_get_candles` | `close[c1]` |
| EMA | `indicator ema --params 20,60` / `market_get_indicator` | `ema20[c1]`, `ema60[c1]` |
| RSI | `indicator rsi --params 14` / `market_get_indicator` | `rsi[c1]` |
| MACD | `indicator macd --params 12,26,9` / `market_get_indicator` | `dif[c1]`, `dea[c1]`, `dif[c2]`, `dea[c2]` |
| BB | `indicator bb --params 20,2` / `market_get_indicator` | `upper[i]`, `middle[i]`, `lower[i]` |
| ATR | `indicator atr --params 14` / `market_get_indicator` | `atr[c1]`, `atr[1..30]` |
| Balance | `account balance USDT` / `account_get_balance` | available USDT |
| Max size | `account max-size` / `account_get_max_size` | exchange-side size cap |
| Position mode | `account config` / `account_get_config` | `posMode` |

---

## 3. Sentiment Scoring

```text
bullish_points =
  35 if ema20[c1] > ema60[c1]
  + 25 if dif[c1] > dea[c1] and dif[c2] <= dea[c2]
    else 10 if dif[c1] > dea[c1]
  + 15 if 50 <= rsi[c1] <= 70
  + 15 if close[c1] > bb_middle[c1]

bearish_points =
  35 if ema20[c1] < ema60[c1]
  + 25 if dif[c1] < dea[c1] and dif[c2] >= dea[c2]
    else 10 if dif[c1] < dea[c1]
  + 15 if 30 <= rsi[c1] < 50
  + 15 if close[c1] < bb_middle[c1]

sentiment_score = bullish_points - bearish_points
```

Classification:

| Score | Direction |
|---|---|
| `>= 50` | bullish |
| `<= -50` | bearish |
| otherwise | neutral / range |

---

## 4. Entry / Exit Conditions

### Long Entry

1. `ema20[c1] > ema60[c1]`
2. `40 <= rsi[c1] <= 70`
3. `dif[c1] > dea[c1] && dif[c2] <= dea[c2]`
4. `close[c1] > bb_middle[c1]`
5. no ATR spike guard

### Short Entry

1. `ema20[c1] < ema60[c1]`
2. `30 <= rsi[c1] <= 60`
3. `dif[c1] < dea[c1] && dif[c2] >= dea[c2]`
4. `close[c1] < bb_middle[c1]`
5. no ATR spike guard

### Position Policy

- Same-direction signal with existing position -> hold
- Opposite signal with existing position -> reverse only after current position is confirmed flat
- `allow_add = false`
- `allow_reduce = false`

---

## 5. Volatility-Adaptive Sizing

### BB Bandwidth Percentile

For each closed bar `i` in `1..30`:

```text
bb_bw[i] = (bb_upper[i] - bb_lower[i]) / bb_middle[i]
```

Current volatility percentile is the percentile rank of `bb_bw[c1]` within the closed 30-bar window.

### Allocation Table

| BB bandwidth percentile | Leverage | Position size |
|---|---|---|
| `0-25%` | `5x` | `15%` of available USDT |
| `25-50%` | `4x` | `12%` of available USDT |
| `50-75%` | `3x` | `8%` of available USDT |
| `75-100%` | `2x` | `5%` of available USDT |

Sizing rules:

- Entry sizing may use `tgtCcy=quote_ccy`
- TP/SL algo order sizing must use actual filled contracts
- Combined BTC + ETH allocation <= `30%` of available USDT

---

## 6. TP / SL

| Direction | Stop loss | Take profit |
|---|---|---|
| Long | `entry - 2 * ATR[c1]` | `entry + 3 * ATR[c1]` |
| Short | `entry + 2 * ATR[c1]` | `entry - 3 * ATR[c1]` |

Additional rules:

- Round `tpTriggerPx` and `slTriggerPx` to `tickSz`
- Use `tpOrdPx = -1` and `slOrdPx = -1` for market execution
- If TP/SL attach fails after one retry, flatten immediately

---

## 7. Hard Guards

### ATR Spike Guard

```text
atr_mean = mean(atr[1..30])
if atr[c1] > 3 * atr_mean:
  trade_blocked = true
```

### Size Guards

Block entry if any of the following is true:

- requested size > `account max-size`
- requested size < `minSz`
- requested size violates `lotSz`
- instrument `state` is not tradable

### Drawdown Guards

| Guard | Trigger | Action |
|---|---|---|
| Soft | drawdown from start equity >= `4%` | no new entries |
| Hard | drawdown from start equity >= `8%` | cancel algos, flat positions, stop automation |

### Cooldown Rule

- 2 stop-loss events on the same instrument within 24h -> skip the next full 1H cycle for that instrument
