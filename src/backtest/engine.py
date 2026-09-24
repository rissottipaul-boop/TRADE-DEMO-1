"""Событийный движок бэктеста спот-стратегий (insights/backtester-design.md §1, §3).

Модель исполнения — контракт anti-lookahead §1.1:
1. Бары — только подтверждённые (confirm=1 гарантирует data.py). Стратегия получает
   срез прогона; prepare() видит только его, on_bar() — только бары [0..i].
2. Решение — на закрытии бара i (on_bar), рыночный ордер исполняется по open(i+1)
   с проскальзыванием (§3.3). Исполнения по close сигнальной свечи нет.
3. Защитные ордера (стоп, тейк) — внутри бара по OHLC; «low раньше high»: сначала
   стоп (худший сценарий), исполнение по min(open, stop) минус проскальзывание.
   Тейк — лимит: исполнен, только если цена прошла его на ≥1 тик (limit_fill="through"),
   комиссия по умолчанию taker (консервативно, §3.1).
4. Прогрев: вход запрещён до trade_start >= strategy.warmup (embargo walk-forward);
   NaN-guard: вход запрещён, если любой входной индикатор на баре = NaN.
5. Каждый вход — через риск-ядро (SimRisk: тот же код src/risk.py, модельные часы):
   check_entry_allowed -> size_position -> register_entry; закрытие -> record_pnl;
   equity по close каждого бара -> update_equity (как engine.py в бою).
6. Комиссия покупки — в базовой валюте, продажи — в котируемой (как на OKX,
   demo-slippage.md §7.3 п.5); цена — к tickSz, объём — floor к lotSz (§3.2).

Ограничения v1: спот long-only, одна позиция на прогон, один инструмент. Точки
расширения: resting limit-ордера (GRID-BT-SIM) — рядом с _check_protective;
частичные выходы/доливки — Position.qty уже учитывает остаток.
"""
from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field
from typing import Any, Optional, Sequence

from src import risk
from src.backtest.data import (BAR_MS, Bar, InstrumentSpec, ceil_to_step, floor_to_step,
                               round_to_step)
from src.backtest.indicators import is_nan
from src.backtest.risk_sim import SimRisk


@dataclass(frozen=True)
class CostModel:
    """Издержки (§3): базовый tier спота OKX, проскальзывание 5 бп/сторону.

    Дефолт 5 бп подтверждён замерами demo-slippage.md (§5, §7.4: BTC-USDT до $5k —
    измерено ≤0.14 бп; дефолт оставлен как консервативный). Для низколиквидных альтов
    фиксированные бп не годятся (§8: BNT ≈40 бп = полуспред) — нужна spread-модель.
    """
    taker_fee: float = 0.0010
    maker_fee: float = 0.0008
    slippage_bps: float = 5.0
    limit_fill: str = "through"   # "through" — прошла лимит на ≥1 тик; "touch" — касание
    limit_fee: str = "taker"      # консервативный дефолт; "maker" — оптимистичнее

    def __post_init__(self) -> None:
        if self.limit_fill not in ("through", "touch"):
            raise ValueError(f"limit_fill: through|touch, получено {self.limit_fill!r}")
        if self.limit_fee not in ("taker", "maker"):
            raise ValueError(f"limit_fee: taker|maker, получено {self.limit_fee!r}")
        if min(self.taker_fee, self.maker_fee, self.slippage_bps) < 0:
            raise ValueError("издержки не могут быть отрицательными")

    @property
    def slip(self) -> float:
        return self.slippage_bps / 1e4

    def scaled(self, fee_mult: float = 1.0, slip_mult: float = 1.0) -> "CostModel":
        """Сценарии чувствительности §3.3: fee×0/×1/×1.5, проскальзывание ×2."""
        return CostModel(self.taker_fee * fee_mult, self.maker_fee * fee_mult,
                         self.slippage_bps * slip_mult, self.limit_fill, self.limit_fee)


@dataclass(frozen=True)
class Fill:
    ts: int
    side: str
    qty: float
    ref_px: float
    exec_px: float
    fee: float          # в котируемой валюте (для покупки — эквивалент комиссии в базе)
    slippage: float     # издержка проскальзывания в котируемой валюте
    liquidity: str      # taker | maker
    reason: str


@dataclass(frozen=True)
class Trade:
    inst_id: str
    entry_signal_ts: int   # open-время бара, на закрытии которого принят вход
    entry_ts: int          # open-время бара исполнения входа
    exit_ts: int           # open-время бара исполнения выхода; end_of_data — close последнего
    entry_px: float
    exit_px: float
    qty: float             # куплено (до комиссии в базе)
    qty_sold: float
    entry_cost: float
    proceeds: float
    fees: float
    slippage: float
    pnl: float             # net: после комиссий и проскальзывания
    pnl_pct: float         # pnl / entry_cost
    exit_reason: str       # signal | stop_loss | take_profit | end_of_data
    bars_held: int
    tag: str = ""          # тег входа стратегии
    exit_tag: str = ""     # тег выхода стратегии (для exit_reason=signal)

    @property
    def duration_h(self) -> float:
        return (self.exit_ts - self.entry_ts) / 3_600_000


@dataclass(frozen=True)
class Decision:
    """Журнал решений стратегии (основа slicing-проверки)."""
    ts: int
    action: str
    outcome: str


@dataclass
class Position:
    qty: float
    qty_bought: float
    entry_ts: int
    entry_idx: int
    entry_px: float
    entry_cost: float
    entry_fee: float
    entry_slip: float
    stop: Optional[float]
    take_profit: Optional[float]
    risk_pct: float
    tag: str
    signal_ts: int

    def unrealized_pct(self, price: float) -> float:
        return self.qty * price / self.entry_cost - 1.0


@dataclass(frozen=True)
class _Pending:
    side: str
    qty: float
    stop: Optional[float]
    take_profit: Optional[float]
    risk_pct: float
    tag: str
    signal_ts: int


@dataclass
class BacktestResult:
    strategy: str
    params: dict
    inst_id: str
    bar: str
    costs: CostModel
    initial_cash: float
    final_equity: float
    curve: list[tuple[int, float]]
    trades: list[Trade]
    fills: list[Fill]
    decisions: list[Decision]
    indicators: dict[str, list[float]]
    rejections: dict[str, int]
    risk_events: list[tuple[float, str]]
    trade_start_ts: Optional[int]
    end_ts: int
    benchmark_return: float
    exposure: float
    unfilled_at_end: int
    dust_qty: float
    dataset_id: str = ""
    states: list[tuple[int, bool, tuple]] = field(default_factory=list)

    def metrics(self) -> dict:
        from src.backtest.metrics import compute_metrics
        return compute_metrics(self.curve, self.trades, self.initial_cash,
                               benchmark=self.benchmark_return, exposure=self.exposure)


class Strategy:
    """Интерфейс стратегии.

    - warmup: баров прогрева (startup period, §1.1 п.3: не меньше max_window*2);
    - prepare(bars) -> {имя: список}: векторные индикаторы по срезу прогона; значение [i]
      обязано зависеть только от bars[:i+1] (проверяют analysis.lookahead_check /
      recursive_check — это главный канал lookahead, как populate_* у Freqtrade);
    - on_bar(ctx): решение на закрытии бара ctx.i через ctx.buy/close/set_stop/...;
    - entry_inputs: индикаторы NaN-guard (None — все из prepare).
    Экземпляр одноразовый: на каждый прогон — новый (фабрика в walk-forward/анализе).
    """
    name = "strategy"
    warmup = 0
    entry_inputs: Optional[tuple[str, ...]] = None

    def __init__(self, **params: Any):
        self.params = params

    def prepare(self, bars: Sequence[Bar]) -> dict[str, list[float]]:
        return {}

    def on_bar(self, ctx: "Context") -> None:
        raise NotImplementedError


class HistoryView(Sequence):
    """Элементы [0..last] ряда (бары или индикатор): будущее — IndexError."""
    __slots__ = ("_bars", "_n")

    def __init__(self, bars: Sequence, last: int):
        self._bars = bars
        self._n = last + 1

    def __len__(self) -> int:
        return self._n

    def __getitem__(self, k):
        if isinstance(k, slice):
            return [self._bars[j] for j in range(*k.indices(self._n))]
        if k < 0:
            k += self._n
        if not 0 <= k < self._n:
            raise IndexError(f"anti-lookahead: бар {k} недоступен (известно {self._n})")
        return self._bars[k]


class Context:
    """Всё, что стратегия знает на закрытии бара i."""
    __slots__ = ("_bt", "i")

    def __init__(self, bt: "Backtest"):
        self._bt = bt
        self.i = -1

    @property
    def bar(self) -> Bar:
        return self._bt.bars[self.i]

    @property
    def bars(self) -> HistoryView:
        return HistoryView(self._bt.bars, self.i)

    @property
    def time_ms(self) -> int:
        return self.bar.ts + self._bt.bar_ms

    @property
    def inst_id(self) -> str:
        return self._bt.spec.inst_id

    @property
    def spec(self) -> InstrumentSpec:
        return self._bt.spec

    @property
    def position(self) -> Optional[Position]:
        return self._bt.pos

    @property
    def has_pending(self) -> bool:
        return self._bt.pending is not None

    @property
    def cash(self) -> float:
        return self._bt.cash

    @property
    def equity(self) -> float:
        return self._bt._equity_at(self.bar.c)

    @property
    def trading_allowed(self) -> bool:
        return self.i >= self._bt.trade_start

    def ind(self, name: str, lag: int = 0) -> float:
        if lag < 0:
            raise ValueError("anti-lookahead: lag < 0 — обращение к будущему")
        j = self.i - lag
        return self._bt.ind[name][j] if j >= 0 else float("nan")

    def series(self, name: str) -> HistoryView:
        """Индикатор как окно [0..i]: абсолютные индексы, будущее недоступно."""
        return HistoryView(self._bt.ind[name], self.i)

    def buy(self, *, stop: Optional[float] = None, take_profit: Optional[float] = None,
            equity_pct: Optional[float] = None, risk_pct: float = risk.DEFAULT_RISK_PCT,
            tag: str = "") -> tuple[bool, str]:
        """Рыночная покупка по open следующего бара. Размер: стоп -> risk.size_position
        (1% риска, потолок 15% notional); equity_pct -> доля equity (≤15%); оба -> минимум."""
        return self._bt._request_buy(self.i, stop, take_profit, equity_pct, risk_pct, tag)

    def close(self, tag: str = "signal") -> bool:
        """Рыночная продажа всей позиции по open следующего бара (выход риском не ограничен)."""
        return self._bt._request_close(self.i, tag)

    def set_stop(self, px: Optional[float]) -> None:
        self._bt._set_protective(self.i, "stop", px)

    def set_take_profit(self, px: Optional[float]) -> None:
        self._bt._set_protective(self.i, "take_profit", px)


class Backtest:
    """Один прогон: срез баров × стратегия × инструмент × модель издержек."""

    def __init__(self, bars: Sequence[Bar], strategy: Strategy, spec: InstrumentSpec, *,
                 bar: str = "1H", costs: Optional[CostModel] = None,
                 initial_cash: float = 10_000.0, trade_start: Optional[int] = None,
                 dataset_id: str = "", record_state: bool = False):
        if not bars:
            raise ValueError("пустой ряд баров")
        self.bars = list(bars)  # копия среза: за его пределами стратегия данных не видит
        for prev, cur in zip(self.bars, self.bars[1:]):
            if cur.ts <= prev.ts:
                raise ValueError(f"бары не строго возрастают: {prev.ts} -> {cur.ts}")
        self.bar = bar
        self.bar_ms = BAR_MS[bar]
        self.strategy = strategy
        self.spec = spec
        self.costs = costs or CostModel()
        self.initial_cash = float(initial_cash)
        self.trade_start = max(strategy.warmup, trade_start or 0)
        self.dataset_id = dataset_id
        self.record_state = record_state
        self.sim = SimRisk(spec.inst_id)
        self.cash = self.initial_cash
        self.dust = 0.0
        self.pos: Optional[Position] = None
        self.pending: Optional[_Pending] = None
        self.ind: dict[str, list[float]] = {}
        self.entry_inputs: tuple[str, ...] = ()
        self.curve: list[tuple[int, float]] = []
        self.trades: list[Trade] = []
        self.fills: list[Fill] = []
        self.decisions: list[Decision] = []
        self.rejections: Counter = Counter()
        self.states: list[tuple[int, bool, tuple]] = []
        self.unfilled_at_end = 0
        self._bars_in_pos = 0
        self._done = False

    # --- Главный цикл ---

    def run(self) -> BacktestResult:
        if self._done:
            raise RuntimeError("Backtest.run() одноразовый: создайте новый прогон")
        self._done = True
        n = len(self.bars)
        ind = self.strategy.prepare(self.bars) or {}
        for name, vals in ind.items():
            if len(vals) != n:
                raise ValueError(f"индикатор {name}: длина {len(vals)} != {n} баров")
        self.ind = {k: list(v) for k, v in ind.items()}
        inputs = self.strategy.entry_inputs
        self.entry_inputs = tuple(self.ind) if inputs is None else tuple(inputs)
        missing = [x for x in self.entry_inputs if x not in self.ind]
        if missing:
            raise ValueError(f"entry_inputs без значений в prepare(): {missing}")

        ctx = Context(self)
        sim = self.sim
        try:
            with sim.activate():
                self._loop(ctx, sim)
                self._finish()
            return self._result()
        finally:
            sim.close()

    def _loop(self, ctx: "Context", sim: SimRisk) -> None:
        for i, b in enumerate(self.bars):
            ctx.i = i
            sim.now = b.ts / 1000.0
            if i == self.trade_start:
                self.curve.append((b.ts, self.cash))
                sim.update_equity(self.cash)
            if self.pending is not None:
                self._execute_pending(b, i)
            if self.pos is not None:
                self._check_protective(b, i)
            close_ts = b.ts + self.bar_ms
            sim.now = close_ts / 1000.0
            if i >= self.trade_start:
                equity = self._equity_at(b.c)
                self.curve.append((close_ts, equity))
                sim.update_equity(equity)
                if self.pos is not None:
                    self._bars_in_pos += 1
            self.strategy.on_bar(ctx)
            if self.record_state:
                self.states.append((b.ts, self.pos is None and self.pending is None,
                                    sim.gating_state()))

    def _finish(self) -> None:
        last = self.bars[-1]
        if self.pending is not None:
            self.unfilled_at_end += 1
            self.decisions.append(Decision(last.ts, "cancel_pending", "end_of_data"))
            self.pending = None
        if self.pos is not None:
            ref = last.c
            px = floor_to_step(ref * (1 - self.costs.slip), self.spec.tick_sz)
            self._close(px, ref, self.costs.taker_fee, "taker", "end_of_data",
                        last.ts + self.bar_ms, len(self.bars))
            if self.curve:
                self.curve[-1] = (self.curve[-1][0], self._equity_at(last.c))

    def _result(self) -> BacktestResult:
        ts0 = self.trade_start
        traded = self.trade_start < len(self.bars)
        bench = (self.bars[-1].c / self.bars[ts0].o - 1.0) if traded else 0.0
        n_trading = max(0, len(self.bars) - self.trade_start)
        return BacktestResult(
            strategy=self.strategy.name, params=dict(self.strategy.params),
            inst_id=self.spec.inst_id, bar=self.bar, costs=self.costs,
            initial_cash=self.initial_cash,
            final_equity=self.curve[-1][1] if self.curve else self.cash,
            curve=self.curve, trades=self.trades, fills=self.fills,
            decisions=self.decisions, indicators=self.ind,
            rejections=dict(self.rejections), risk_events=list(self.sim.events),
            trade_start_ts=self.bars[ts0].ts if traded else None,
            end_ts=self.bars[-1].ts + self.bar_ms, benchmark_return=bench,
            exposure=self._bars_in_pos / n_trading if n_trading else 0.0,
            unfilled_at_end=self.unfilled_at_end, dust_qty=self.dust,
            dataset_id=self.dataset_id, states=self.states)

    # --- Исполнение ---

    def _equity_at(self, price: float) -> float:
        qty = (self.pos.qty if self.pos is not None else 0.0) + self.dust
        return self.cash + qty * price

    def _execute_pending(self, b: Bar, i: int) -> None:
        o, self.pending = self.pending, None
        spec, costs = self.spec, self.costs
        ref = b.o
        if o.side == "sell":
            px = floor_to_step(ref * (1 - costs.slip), spec.tick_sz)
            self._close(px, ref, costs.taker_fee, "taker", "signal", b.ts, i, exit_tag=o.tag)
            return
        px = ceil_to_step(ref * (1 + costs.slip), spec.tick_sz)
        qty = o.qty
        if qty * px > self.cash:  # гэп между close(t) и open(t+1): берём сколько хватает
            qty = floor_to_step(self.cash / px, spec.lot_sz)
            if floor_to_step(qty * (1 - costs.taker_fee), spec.lot_sz) < spec.min_sz:
                self.rejections["insufficient_cash"] += 1
                self.decisions.append(Decision(b.ts, "fill_buy", "rejected:insufficient_cash"))
                return
        cost = qty * px
        fee_base = qty * costs.taker_fee
        self.cash -= cost
        self.pos = Position(qty=qty - fee_base, qty_bought=qty, entry_ts=b.ts, entry_idx=i,
                            entry_px=px, entry_cost=cost, entry_fee=fee_base * px,
                            entry_slip=qty * (px - ref), stop=o.stop,
                            take_profit=o.take_profit, risk_pct=o.risk_pct, tag=o.tag,
                            signal_ts=o.signal_ts)
        self.sim.register_entry(o.risk_pct)
        self.fills.append(Fill(b.ts, "buy", qty, ref, px, fee_base * px, qty * (px - ref),
                               "taker", o.tag or "entry"))

    def _check_protective(self, b: Bar, i: int) -> None:
        """Внутрибарные защитные ордера: «low раньше high» — стоп проверяется первым."""
        p, spec, costs = self.pos, self.spec, self.costs
        if p.stop is not None and b.l <= p.stop:
            ref = min(b.o, p.stop)  # гэп ниже стопа — исполнение по open, не по стопу
            px = floor_to_step(ref * (1 - costs.slip), spec.tick_sz)
            self._close(px, ref, costs.taker_fee, "taker", "stop_loss", b.ts, i)
            return
        if p.take_profit is not None:
            through = spec.tick_sz if costs.limit_fill == "through" else 0.0
            if b.h >= p.take_profit + through - spec.tick_sz * 1e-6:
                maker = costs.limit_fee == "maker"
                self._close(p.take_profit, p.take_profit,
                            costs.maker_fee if maker else costs.taker_fee,
                            "maker" if maker else "taker", "take_profit", b.ts, i)

    def _close(self, px: float, ref: float, fee_rate: float, liquidity: str, reason: str,
               ts: int, i: int, exit_tag: str = "") -> None:
        p, spec = self.pos, self.spec
        qty = floor_to_step(p.qty, spec.lot_sz)
        if qty < spec.min_sz:
            raise RuntimeError(f"позиция {p.qty} < minSz {spec.min_sz}: сайзинг обязан "
                               f"был это исключить")
        gross = qty * px
        fee = gross * fee_rate
        proceeds = gross - fee
        slip = qty * (ref - px)
        self.cash += proceeds
        self.dust += p.qty - qty
        pnl = proceeds - p.entry_cost
        self.trades.append(Trade(
            inst_id=spec.inst_id, entry_signal_ts=p.signal_ts, entry_ts=p.entry_ts,
            exit_ts=ts, entry_px=p.entry_px, exit_px=px, qty=p.qty_bought, qty_sold=qty,
            entry_cost=p.entry_cost, proceeds=proceeds, fees=p.entry_fee + fee,
            slippage=p.entry_slip + slip, pnl=pnl, pnl_pct=pnl / p.entry_cost,
            exit_reason=reason, bars_held=i - p.entry_idx, tag=p.tag, exit_tag=exit_tag))
        self.fills.append(Fill(ts, "sell", qty, ref, px, fee, slip, liquidity,
                               exit_tag or reason))
        self.pos = None
        self.sim.record_pnl(pnl)

    # --- Запросы стратегии ---

    def _reject(self, ts: int, category: str, detail: str = "") -> tuple[bool, str]:
        key = f"{category}: {detail}" if category == "risk" else category
        self.rejections[key] += 1
        reason = f"{category}: {detail}" if detail else category
        self.decisions.append(Decision(ts, "buy", "rejected:" + reason))
        return False, reason

    def _request_buy(self, i: int, stop: Optional[float], take_profit: Optional[float],
                     equity_pct: Optional[float], risk_pct: float,
                     tag: str) -> tuple[bool, str]:
        if stop is None and equity_pct is None:
            raise ValueError("buy(): нужен stop (сайзинг риском) или equity_pct")
        b, spec, costs = self.bars[i], self.spec, self.costs
        if i < self.trade_start:
            return self._reject(b.ts, "warmup")
        if self.pos is not None or self.pending is not None:
            return self._reject(b.ts, "in_position")
        for name in self.entry_inputs:
            if is_nan(self.ind[name][i]):
                return self._reject(b.ts, "nan_guard", name)
        allowed, why = self.sim.check_entry()
        if not allowed:
            return self._reject(b.ts, "risk", why)
        ref = b.c
        equity = self._equity_at(ref)
        warnings: list[str] = []
        qty: Optional[float] = None
        if stop is not None:
            stop = round_to_step(stop, spec.tick_sz)
            if stop >= ref:
                return self._reject(b.ts, "stop_not_below_price")
            qty, w = self.sim.size_by_stop(equity, ref, stop, spec, risk_pct)
            warnings += w
        if equity_pct is not None:
            q, w = self.sim.size_by_notional(equity, ref, equity_pct, spec)
            warnings += w
            qty = q if qty is None else min(qty, q)
        if take_profit is not None:
            take_profit = ceil_to_step(take_profit, spec.tick_sz)
            if take_profit <= ref:
                return self._reject(b.ts, "take_profit_not_above_price")
        # остаток после комиссии в базе обязан оставаться продаваемым (>= minSz)
        if not qty or floor_to_step(qty * (1 - costs.taker_fee), spec.lot_sz) < spec.min_sz:
            return self._reject(b.ts, "sizing", "; ".join(warnings) or "размер < minSz")
        self.pending = _Pending("buy", qty, stop, take_profit, risk_pct, tag, b.ts)
        self.decisions.append(Decision(b.ts, "buy", f"accepted:qty={qty!r}"))
        return True, "ok"

    def _request_close(self, i: int, tag: str) -> bool:
        if self.pos is None or self.pending is not None:
            return False
        b = self.bars[i]
        self.pending = _Pending("sell", self.pos.qty, None, None, 0.0, tag or "signal", b.ts)
        self.decisions.append(Decision(b.ts, "close", "accepted:" + (tag or "signal")))
        return True

    def _set_protective(self, i: int, kind: str, px: Optional[float]) -> None:
        if self.pos is None:
            return
        tick = self.spec.tick_sz
        if kind == "stop":
            self.pos.stop = None if px is None else round_to_step(px, tick)
            value = self.pos.stop
        else:
            self.pos.take_profit = None if px is None else ceil_to_step(px, tick)
            value = self.pos.take_profit
        self.decisions.append(Decision(self.bars[i].ts, "set_" + kind, repr(value)))


def run_backtest(bars: Sequence[Bar], strategy: Strategy, spec: InstrumentSpec,
                 **kwargs: Any) -> BacktestResult:
    return Backtest(bars, strategy, spec, **kwargs).run()


__all__ =  ["Backtest", "BacktestResult", "Context", "CostModel", "Decision", "Fill",
           "HistoryView", "Position", "Strategy", "Trade", "run_backtest"]
