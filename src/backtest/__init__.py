"""Бэктестер спот-стратегий OKX (задача BT-IMPL, дизайн insights/backtester-design.md).

Модули:
- data        — data/market_data.db, загрузка history-candles, gap-check, dataset_id (§4);
- indicators  — каузальные индикаторы (SMA/EMA/RSI/ATR/BB);
- risk_sim    — риск-ядро src/risk.py в модельном времени (паритет с боем);
- engine      — событийный движок: сигнал на close(t) -> исполнение open(t+1),
                стоп «low раньше high», комиссии и проскальзывание (§1, §3);
- metrics     — метрики отчёта §6.1 и гейт приёмки §6.2;
- walkforward — rolling 180/30/30 д, holdout 6 мес, purge/embargo (§2);
- analysis    — slicing (lookahead) и recursive проверки (§1.2);
- strategies  — базовые стратегии проверки контура (buy&hold, SMA-cross);
- report      — markdown-отчёт; CLI: python -m src.backtest --help.

Офлайн: ордеров не ставит, движок (src/engine.py) и его состояние не трогает.
"""
from src.backtest.data import Bar, InstrumentSpec, MarketDataStore, load_dataset
from src.backtest.engine import Backtest, BacktestResult, CostModel, Strategy, run_backtest

__all__ = ["Bar", "InstrumentSpec", "MarketDataStore", "load_dataset", "Backtest",
           "BacktestResult", "CostModel", "Strategy", "run_backtest"]
