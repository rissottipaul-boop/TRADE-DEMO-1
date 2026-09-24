"""Базовые стратегии проверки контура бэктестера (BT-IMPL): не кандидаты в paper.

- BuyAndHold: покупка на первом разрешённом баре, удержание до конца данных.
  Размер — потолок позиции риск-ядра (risk.MAX_POSITION_PCT = 15% equity), лимиты
  не ослабляются: это «buy&hold в рамках риск-контура», а рыночный benchmark
  (изменение цены за период) отчёт даёт отдельно.
- SmaCross: вход при пересечении SMA(fast) снизу вверх SMA(slow) на закрытии бара,
  выход — при обратном пересечении; опционально фиксированный стоп stop_pct.
"""
from typing import Optional, Sequence

from src import risk
from src.backtest.data import Bar
from src.backtest.engine import Context, Strategy
from src.backtest.indicators import crossed_above, crossed_below, sma


class BuyAndHold(Strategy):
    name = "buy_hold"

    def __init__(self, equity_pct: float = risk.MAX_POSITION_PCT):
        super().__init__(equity_pct=equity_pct)
        self.warmup = 0

    def on_bar(self, ctx: Context) -> None:
        if ctx.position is None and not ctx.has_pending and ctx.trading_allowed:
            ctx.buy(equity_pct=self.params["equity_pct"], tag="buy_hold")


class SmaCross(Strategy):
    name = "sma_cross"
    entry_inputs = ("sma_fast", "sma_slow")

    def __init__(self, fast: int = 50, slow: int = 200,
                 equity_pct: float = risk.MAX_POSITION_PCT, stop_pct: Optional[float] = None):
        if not 0 < fast < slow:
            raise ValueError("нужно 0 < fast < slow")
        super().__init__(fast=fast, slow=slow, equity_pct=equity_pct, stop_pct=stop_pct)
        self.warmup = 2 * slow  # §1.1 п.3: warmup = max_window * 2

    def prepare(self, bars: Sequence[Bar]) -> dict[str, list[float]]:
        closes = [b.c for b in bars]
        return {"sma_fast": sma(closes, self.params["fast"]),
                "sma_slow": sma(closes, self.params["slow"])}

    def on_bar(self, ctx: Context) -> None:
        f, s = ctx.series("sma_fast"), ctx.series("sma_slow")
        i = ctx.i
        if ctx.position is None:
            if ctx.trading_allowed and not ctx.has_pending and crossed_above(f, s, i):
                stop_pct = self.params["stop_pct"]
                stop = ctx.bar.c * (1 - stop_pct / 100.0) if stop_pct else None
                ctx.buy(stop=stop, equity_pct=self.params["equity_pct"], tag="sma_up")
        elif crossed_below(f, s, i):
            ctx.close("sma_down")
