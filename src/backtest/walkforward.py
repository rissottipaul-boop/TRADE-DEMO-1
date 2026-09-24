"""Walk-forward (backtester-design.md §2): rolling 180/30/30 д, шаг 30 д, holdout 6 мес.

- Окна — по времени (bisect по open-time), а не по числу баров: дыры данных не
  сдвигают календарные границы.
- Train: перебор параметров, ранжирование по objective (Sharpe, затем net %).
  Valid: выбор одного набора из top_k train-кандидатов. Test: единственный прогон
  выбранного набора — только он идёт в OOS-отчёт (test не участвует в выборе).
- Purge: каждый сегмент — независимый прогон; позиция принудительно закрывается
  на последнем баре сегмента (end_of_data) и не переходит границу.
- Embargo: прогон сегмента начинается за warmup баров до его старта (прогрев
  индикаторов), торговля — только внутри сегмента. Первое окно начинается от
  origin = max warmup по сетке, чтобы embargo было полным.
- Holdout: последние holdout_days не входят ни в одно окно; касание — только явным
  флагом, фиксируется в data/market_data.db (holdout_touches).
- Честный подсчёт числа испытаний (§2.2 п.2): trials = |сетка| × число окон.
"""
from bisect import bisect_left
from dataclasses import dataclass, field, replace
from typing import Callable, Optional, Sequence

from src.backtest.data import BAR_MS, Bar, InstrumentSpec
from src.backtest.engine import Backtest, BacktestResult, CostModel, Strategy
from src.backtest.metrics import DAY_MS, compute_metrics


@dataclass(frozen=True)
class WalkForwardConfig:
    train_days: int = 180
    valid_days: int = 30
    test_days: int = 30
    step_days: int = 30
    holdout_days: int = 182
    top_k: int = 3
    objective: str = "sharpe"


@dataclass(frozen=True)
class Segment:
    start: int  # индекс бара, включительно
    end: int    # индекс бара, исключительно

    def __len__(self) -> int:
        return max(0, self.end - self.start)


@dataclass(frozen=True)
class Window:
    k: int
    train: Segment
    valid: Segment
    test: Segment


def holdout_start(ts_list: Sequence[int], bar_ms: int, holdout_days: int) -> tuple[int, int]:
    """(индекс первого бара holdout, его open-время): последние holdout_days до close
    последнего бара. Без holdout -> (len, close последнего бара)."""
    last_close = ts_list[-1] + bar_ms
    holdout_ts = last_close - holdout_days * DAY_MS if holdout_days > 0 else last_close
    return bisect_left(ts_list, holdout_ts), holdout_ts


def make_windows(ts_list: Sequence[int], bar_ms: int, cfg: WalkForwardConfig,
                 origin_idx: int = 0) -> tuple[list[Window], Optional[Segment]]:
    """Окна [train|valid|test], катящиеся с шагом step_days; test не заходит в holdout."""
    if not ts_list:
        return [], None
    h_idx, holdout_ts = holdout_start(ts_list, bar_ms, cfg.holdout_days)
    holdout = Segment(h_idx, len(ts_list)) if cfg.holdout_days > 0 else None
    if origin_idx >= len(ts_list):
        return [], holdout
    t0 = ts_list[origin_idx]
    windows: list[Window] = []
    k = 0
    while True:
        a = t0 + k * cfg.step_days * DAY_MS
        b = a + cfg.train_days * DAY_MS
        c = b + cfg.valid_days * DAY_MS
        d = c + cfg.test_days * DAY_MS
        if d > holdout_ts:
            break
        ia, ib, ic, id_ = (bisect_left(ts_list, x) for x in (a, b, c, d))
        windows.append(Window(k, Segment(ia, ib), Segment(ib, ic), Segment(ic, id_)))
        k += 1
    return windows, holdout


def run_segment(bars: Sequence[Bar], seg: Segment, strategy: Strategy, spec: InstrumentSpec,
                *, bar: str, costs: CostModel, initial_cash: float,
                embargo: Optional[int] = None, dataset_id: str = "") -> BacktestResult:
    """Прогон сегмента с embargo-прогревом перед ним и purge на его конце."""
    warm = strategy.warmup if embargo is None else embargo
    s0 = max(0, seg.start - warm)
    return Backtest(bars[s0:seg.end], strategy, spec, bar=bar, costs=costs,
                    initial_cash=initial_cash, trade_start=seg.start - s0,
                    dataset_id=dataset_id).run()


@dataclass
class ConcatResult:
    """OOS-конкатенация сегментов: equity сцепляется по доходностям, PnL сделок
    масштабируется к капиталу на старте сегмента."""
    curve: list[tuple[int, float]]
    trades: list
    initial_cash: float
    benchmark_return: float
    exposure: float

    @property
    def final_equity(self) -> float:
        return self.curve[-1][1] if self.curve else self.initial_cash

    def metrics(self) -> dict:
        return compute_metrics(self.curve, self.trades, self.initial_cash,
                               benchmark=self.benchmark_return, exposure=self.exposure)


def concat_results(results: Sequence[BacktestResult], initial_cash: float) -> ConcatResult:
    capital = initial_cash
    curve: list[tuple[int, float]] = []
    trades: list = []
    bench = 1.0
    exp_bars = exp_w = 0.0
    for r in results:
        if not r.curve:
            continue
        f = capital / r.initial_cash
        pts = [(ts, eq * f) for ts, eq in r.curve]
        if curve and pts and pts[0][0] <= curve[-1][0]:
            pts = pts[1:]  # стартовая точка сегмента совпадает с концом предыдущего
        curve.extend(pts)
        trades.extend(replace(t, entry_cost=t.entry_cost * f, proceeds=t.proceeds * f,
                              fees=t.fees * f, slippage=t.slippage * f, pnl=t.pnl * f)
                      for t in r.trades)
        capital = r.final_equity * f
        bench *= 1.0 + r.benchmark_return
        w = max(0, len(r.curve) - 1)
        exp_bars += r.exposure * w
        exp_w += w
    if not curve:
        curve = []
    return ConcatResult(curve, trades, initial_cash, bench - 1.0,
                        exp_bars / exp_w if exp_w else 0.0)


def _score(m: dict, objective: str) -> tuple[float, float]:
    v = m.get(objective)
    return (v if v is not None else float("-inf"), m["net_profit_pct"])


@dataclass
class WindowResult:
    window: Window
    params: dict
    train_score: tuple[float, float]
    valid_score: tuple[float, float]
    test: BacktestResult
    test_stress: BacktestResult
    candidates: int


@dataclass
class WalkForwardResult:
    strategy: str
    config: WalkForwardConfig
    windows: list[WindowResult]
    oos: ConcatResult
    oos_stress: ConcatResult
    trials: int
    holdout: Optional[Segment] = None
    holdout_result: Optional[BacktestResult] = None
    holdout_stress: Optional[BacktestResult] = None
    holdout_params: Optional[dict] = None
    notes: list[str] = field(default_factory=list)


def walk_forward(bars: Sequence[Bar], factory: Callable[..., Strategy],
                 param_grid: Sequence[dict], spec: InstrumentSpec, *, bar: str = "1H",
                 costs: Optional[CostModel] = None, stress_costs: Optional[CostModel] = None,
                 initial_cash: float = 10_000.0, cfg: WalkForwardConfig = WalkForwardConfig(),
                 touch_holdout: bool = False, dataset_id: str = "",
                 embargo: Optional[int] = None) -> WalkForwardResult:
    """embargo=None -> max warmup сетки; явное значение выравнивает окна разных стратегий."""
    costs = costs or CostModel()
    stress_costs = stress_costs or costs.scaled(slip_mult=2.0)
    grid = [dict(p) for p in (param_grid or [{}])]
    need = max(factory(**p).warmup for p in grid)
    embargo = need if embargo is None else max(embargo, need)
    ts_list = [b.ts for b in bars]
    windows, holdout = make_windows(ts_list, BAR_MS[bar], cfg, origin_idx=embargo)
    kw = dict(bar=bar, initial_cash=initial_cash, embargo=embargo, dataset_id=dataset_id)

    results: list[WindowResult] = []
    for w in windows:
        ranked = []
        for idx, p in enumerate(grid):
            m = run_segment(bars, w.train, factory(**p), spec, costs=costs, **kw).metrics()
            ranked.append((_score(m, cfg.objective), -idx, p))
        ranked.sort(key=lambda x: (x[0], x[1]), reverse=True)
        best = None
        for train_score, neg_idx, p in ranked[:max(1, cfg.top_k)]:
            m = run_segment(bars, w.valid, factory(**p), spec, costs=costs, **kw).metrics()
            cand = (_score(m, cfg.objective), neg_idx, p, train_score)
            if best is None or (cand[0], cand[1]) > (best[0], best[1]):
                best = cand
        valid_score, _, chosen, train_score = best
        test = run_segment(bars, w.test, factory(**chosen), spec, costs=costs, **kw)
        stress = run_segment(bars, w.test, factory(**chosen), spec, costs=stress_costs, **kw)
        results.append(WindowResult(w, chosen, train_score, valid_score, test, stress,
                                    min(len(grid), max(1, cfg.top_k))))

    wf = WalkForwardResult(
        strategy=factory(**grid[0]).name, config=cfg, windows=results,
        oos=concat_results([r.test for r in results], initial_cash),
        oos_stress=concat_results([r.test_stress for r in results], initial_cash),
        trials=len(grid) * len(windows), holdout=holdout)
    if not windows:
        wf.notes.append("нет ни одного полного окна: мало истории до holdout")
    if touch_holdout and holdout is not None and len(holdout) > 0 and results:
        params = results[-1].params  # набор последнего окна — самый свежий train
        wf.holdout_params = params
        wf.holdout_result = run_segment(bars, holdout, factory(**params), spec,
                                        costs=costs, **kw)
        wf.holdout_stress = run_segment(bars, holdout, factory(**params), spec,
                                        costs=stress_costs, **kw)
    return wf
