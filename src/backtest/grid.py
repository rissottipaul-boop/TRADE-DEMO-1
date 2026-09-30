"""Собственная grid-стратегия для бэктестера (GRID-BT-SIM / GRID-STRATEGY-DESIGN).

Спецификация: insights/grid-strategy-design.md
- Геометрическая сетка (runType=2): равные % шаги между min_px и max_px;
- Fee-floor pre-check (§5.3): fee_share = (2 * maker_fee) / step_pct <= 0.5 (пол шага >= 0.32%);
- Двусторонний hard stop (§3.1): проверка «low раньше high» (min_px, затем max_px);
  отмена уровней, рыночная ликвидация inventory, record_pnl(session_pnl), block_instrument(4ч);
- Изоляция входов (§6.1): register_entry вызывается 1 раз на старте сессии сетки;
  все обслуживающие перестановки уровней идут с count_as_entry=False.
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any, Optional

from src import risk
from src.backtest.data import Bar
from src.backtest.engine import Context, Fill, Strategy


@dataclass
class GridConfig:
    min_px: float
    max_px: float
    grid_num: int
    quote_sz: Optional[float] = None
    quote_per_grid: Optional[float] = None
    maker_fee: float = 0.0008
    fee_share_limit: float = 0.5
    hard_stop_enabled: bool = True
    risk_pct: float = risk.DEFAULT_RISK_PCT
    initial_purchase: bool = False
    tag: str = "grid"

    def __post_init__(self) -> None:
        if self.min_px <= 0 or self.max_px <= 0:
            raise ValueError(f"min_px ({self.min_px}) и max_px ({self.max_px}) должны быть > 0")
        if self.min_px >= self.max_px:
            raise ValueError(f"min_px ({self.min_px}) должен быть строго < max_px ({self.max_px})")
        if self.grid_num < 2:
            raise ValueError(f"grid_num ({self.grid_num}) должен быть >= 2")

        step_pct = math.log(self.max_px / self.min_px) / self.grid_num
        round_trip_fee = 2.0 * self.maker_fee
        fee_share = round_trip_fee / step_pct
        if fee_share > self.fee_share_limit:
            min_step = round_trip_fee / self.fee_share_limit
            raise ValueError(
                f"fee_share {fee_share:.4f} > limit {self.fee_share_limit:.4f}: "
                f"step_pct={step_pct*100:.3f}% слишком мал, необходим шаг >= {min_step*100:.3f}%"
            )

    @property
    def step_pct(self) -> float:
        return math.log(self.max_px / self.min_px) / self.grid_num

    @property
    def fee_share(self) -> float:
        return (2.0 * self.maker_fee) / self.step_pct

    @property
    def levels(self) -> list[float]:
        ratio = (self.max_px / self.min_px) ** (1.0 / self.grid_num)
        return [self.min_px * (ratio ** k) for k in range(self.grid_num + 1)]


class GridStrategy(Strategy):
    """Собственная реализация сетки для бэктестера."""
    name = "grid"

    def __init__(self, config: GridConfig, **params: Any):
        super().__init__(**params)
        self.config = config
        self.levels = config.levels
        self.initialized = False
        self.stopped = False
        self.stop_reason = ""
        self.arbitrage_count = 0
        self.total_realized_pnl = 0.0
        self.start_equity = 0.0
        self.quote_per_grid = 0.0
        self.active_levels: dict[int, str] = {}      # level_idx -> order_id
        self.level_buy_cost: dict[int, float] = {}   # level_idx -> cost in quote

    def check_hard_stop(self, bar: Bar, ctx: Context) -> bool:
        """Проверка hard stop до исполнения уровней на баре.
        Правило «low раньше high»: сначала стоп снизу (min_px), затем сверху (max_px).
        """
        if not self.config.hard_stop_enabled or self.stopped or not self.initialized:
            return False

        trigger = None
        stop_px = 0.0
        if bar.l < self.config.min_px:
            trigger = "min_px_breached"
            stop_px = self.config.min_px
        elif bar.h > self.config.max_px:
            trigger = "max_px_breached"
            stop_px = self.config.max_px

        if trigger:
            self.stopped = True
            self.stop_reason = trigger
            ctx.cancel_all_limits()
            self.active_levels.clear()
            ctx.liquidate_inventory(ref_px=stop_px, reason=f"hard_stop_{trigger}")
            session_pnl = ctx.equity - self.start_equity
            ctx.record_pnl(session_pnl)
            ctx.block_instrument(hours=4.0)
            return True

        return False

    def on_bar(self, ctx: Context) -> None:
        if self.stopped:
            return

        if not self.initialized:
            if not ctx.trading_allowed:
                return
            allowed, why = ctx.register_entry(self.config.risk_pct)
            if not allowed:
                return
            self.initialized = True
            self.start_equity = ctx.equity

            if self.config.quote_sz is not None:
                self.quote_per_grid = self.config.quote_sz / self.config.grid_num
            elif self.config.quote_per_grid is not None:
                self.quote_per_grid = self.config.quote_per_grid
            else:
                self.quote_per_grid = (ctx.cash * 0.15) / self.config.grid_num

            curr_px = ctx.bar.c
            for k, px in enumerate(self.levels):
                if px < curr_px:
                    qty = self.quote_per_grid / px
                    ok, why, order_id = ctx.place_limit(
                        side="buy", px=px, qty=qty,
                        tag=f"{self.config.tag}_buy_{k}",
                        count_as_entry=False
                    )
                    if ok and order_id:
                        self.active_levels[k] = order_id
                elif px > curr_px and self.config.initial_purchase:
                    qty = self.quote_per_grid / px
                    ok, why, order_id = ctx.place_limit(
                        side="sell", px=px, qty=qty,
                        tag=f"{self.config.tag}_sell_{k}",
                        count_as_entry=False
                    )
                    if ok and order_id:
                        self.active_levels[k] = order_id

    def on_fill(self, fill: Fill, ctx: Context) -> None:
        if self.stopped:
            return

        tag = fill.reason
        buy_prefix = f"{self.config.tag}_buy_"
        sell_prefix = f"{self.config.tag}_sell_"

        if tag.startswith(buy_prefix):
            k = int(tag[len(buy_prefix):])
            self.active_levels.pop(k, None)
            self.level_buy_cost[k] = fill.qty * fill.exec_px

            # Размещение парного ордера на продажу на уровень выше
            if k + 1 < len(self.levels):
                sell_px = self.levels[k + 1]
                sell_qty = fill.qty * (1.0 - self.config.maker_fee)
                ok, why, order_id = ctx.place_limit(
                    side="sell", px=sell_px, qty=sell_qty,
                    tag=f"{self.config.tag}_sell_{k+1}",
                    count_as_entry=False
                )
                if ok and order_id:
                    self.active_levels[k + 1] = order_id

        elif tag.startswith(sell_prefix):
            m = int(tag[len(sell_prefix):])
            self.active_levels.pop(m, None)
            k = m - 1
            self.arbitrage_count += 1

            buy_cost = self.level_buy_cost.pop(k, self.quote_per_grid)
            sell_proceeds = fill.qty * fill.exec_px - fill.fee
            arb_pnl = sell_proceeds - buy_cost
            self.total_realized_pnl += arb_pnl

            # Восстановление ордера на покупку на уровне k
            if 0 <= k < len(self.levels):
                buy_px = self.levels[k]
                buy_qty = self.quote_per_grid / buy_px
                ok, why, order_id = ctx.place_limit(
                    side="buy", px=buy_px, qty=buy_qty,
                    tag=f"{self.config.tag}_buy_{k}",
                    count_as_entry=False
                )
                if ok and order_id:
                    self.active_levels[k] = order_id

    def on_finish(self, ctx: Context) -> None:
        if self.initialized and not self.stopped:
            session_pnl = ctx.equity - self.start_equity
            ctx.record_pnl(session_pnl)


__all__ = ["GridConfig", "GridStrategy"]

