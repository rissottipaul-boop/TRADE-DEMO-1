"""Риск-ядро в бэктесте: ТОТ ЖЕ код src/risk.py, модельное время и in-memory SQLite.

Паритет с боем (backtester-design.md §5, п.1): лимиты, breaker'ы, серии убытков,
блокировки инструмента, лимит входов/день — логика _RiskCore без изменений.
Отличия только инфраструктурные:
- хранилище — одно соединение sqlite ':memory:' на прогон (без файла и fsync на
  каждый _set: иначе прогон 40k баров шёл бы минуты); подкласс переопределяет лишь _conn;
- часы — модельные: на время прогона risk._utc_now подменяется временем текущего
  бара (дневной rollover 00:00 UTC, cooldown 24ч, системная пауза, свежесть
  equity — в модельном времени). Подмена под блокировкой и снимается в finally.

Equity и HWM ядра ведёт только update_equity по close каждого бара, как движок
в бою (RISK-PNL-DOUBLE): record_pnl их не меняет, а вход без свежего equity
check_entry_allowed запрещает.

Бэктест — только офлайн-процесс: в процессе движка/live-раннера не запускать
(подмена часов глобальна для модуля src.risk на время прогона).

Сайзинг спота: risk.size_position считает целые «контракты» (math.floor). Для спота
с ct_val=1 это округлило бы BTC до целых монет (-> 0). Поэтому передаём
«контракт = 1 лот»: ct_val=lot_sz, lot_sz=1, min_sz=minSz/lotSz — та же формула,
лимиты (1%/2% риска, 15% notional, 30% анти-паттерн) не меняются.
"""
import logging
import math
import sqlite3
import threading
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator

from src import risk
from src.backtest.data import InstrumentSpec, floor_to_step

_RUN_LOCK = threading.RLock()


class _MemoryRiskCore(risk._RiskCore):
    """_RiskCore без файла: одно in-memory соединение на прогон."""

    def __init__(self) -> None:
        self._mem = sqlite3.connect(":memory:", check_same_thread=False)
        self._mem.row_factory = sqlite3.Row
        super().__init__(Path(":memory:"))

    @contextmanager
    def _conn(self) -> Iterator[sqlite3.Connection]:
        try:
            yield self._mem
        except BaseException:
            self._mem.rollback()
            raise
        else:
            self._mem.commit()


class SimRisk:
    """Обёртка риск-ядра для одного инструмента одного прогона."""

    def __init__(self, inst_id: str):
        self.inst_id = inst_id
        self.core = _MemoryRiskCore()
        self.now = 0.0                       # модельное время, секунды UTC
        self.events: list[tuple[float, str]] = []

    @contextmanager
    def activate(self) -> Iterator["SimRisk"]:
        """Подменяет часы риск-ядра модельным временем и глушит его WARNING-лог."""
        with _RUN_LOCK:
            orig_clock = risk._utc_now
            rlog = logging.getLogger("okx.risk")
            orig_level = rlog.level
            risk._utc_now = lambda: self.now
            rlog.setLevel(logging.ERROR)
            try:
                yield self
            finally:
                risk._utc_now = orig_clock
                rlog.setLevel(orig_level)

    # --- Вход ---

    def check_entry(self) -> tuple[bool, str]:
        return self.core.check_entry_allowed(self.inst_id, "buy")

    def size_by_stop(self, equity: float, entry: float, stop: float, spec: InstrumentSpec,
                     risk_pct: float) -> tuple[float, list[str]]:
        """Сайзинг риском через risk.size_position (контракт = 1 лот)."""
        min_lots = math.ceil(spec.min_sz / spec.lot_sz - 1e-9)
        res = self.core.size_position(equity=equity, entry=entry, stop=stop,
                                      ct_val=spec.lot_sz, lot_sz=1.0, min_sz=min_lots,
                                      risk_pct=risk_pct)
        return floor_to_step(res["size"] * spec.lot_sz, spec.lot_sz), list(res["warnings"])

    @staticmethod
    def size_by_notional(equity: float, price: float, equity_pct: float,
                         spec: InstrumentSpec) -> tuple[float, list[str]]:
        """Спот без стопа: доля equity, не выше потолка позиции risk.MAX_POSITION_PCT."""
        warnings: list[str] = []
        if equity_pct > risk.MAX_POSITION_PCT:
            warnings.append(f"equity_pct {equity_pct}% обрезан до потолка позиции "
                            f"{risk.MAX_POSITION_PCT}%")
            equity_pct = risk.MAX_POSITION_PCT
        qty = floor_to_step(equity * equity_pct / 100.0 / price, spec.lot_sz)
        if qty < spec.min_sz:
            warnings.append(f"размер {qty} < minSz {spec.min_sz}")
            qty = 0.0
        return qty, warnings

    def register_entry(self, risk_pct: float) -> None:
        self.core.register_entry(self.inst_id, "buy", risk_pct)

    # --- Выход и equity ---

    def record_pnl(self, pnl: float) -> list[str]:
        """Закрытие сделки: day_pnl и серии убытков. Equity ядра не меняется —
        её обновит update_equity на close бара (RISK-PNL-DOUBLE)."""
        events = self.core.record_pnl(self.inst_id, pnl,
                                      datetime.fromtimestamp(self.now, tz=timezone.utc))
        self.events.extend((self.now, e) for e in events)
        return events

    def update_equity(self, equity: float) -> list[str]:
        """Как движок в бою (engine.py -> risk.update_equity(totalEq)).

        Вызывается на каждом баре, в том числе с тем же значением: вызов продлевает
        свежесть equity (risk.EQUITY_MAX_AGE_S), без него check_entry_allowed
        отказал бы во входе после нескольких баров без движения equity.
        """
        events = self.core.update_equity(equity)
        self.events.extend((self.now, e) for e in events)
        return events

    # --- Диагностика ---

    def gating_state(self) -> tuple:
        """Всё, что влияет на будущие решения check_entry_allowed (для recursive-проверки)."""
        c, now = self.core, self.now

        def active(until: float) -> float:
            return until if until and until > now else 0.0

        # rollover ленивый (делают его check_entry/update_equity/record_pnl) — здесь
        # явно, чтобы entries_today не был «вчерашним» у копии без вызовов риск-ядра.
        # Эквивалентно: day_start_equity берётся из того же сохранённого equity.
        c._maybe_rollover_day()
        with c._conn() as conn:
            inst = tuple((r["inst_id"], int(r["loss_streak"] or 0),
                          active(r["blocked_until"] or 0.0)) for r in conn.execute(
                "SELECT inst_id, loss_streak, blocked_until FROM risk_instruments "
                "ORDER BY inst_id"))
            open_risk = tuple(r["inst_id"] for r in conn.execute(
                "SELECT inst_id FROM risk_open_risk ORDER BY inst_id"))
        return (int(c._get("entries_today")), int(c._get("global_loss_streak")),
                active(c._get("system_pause_until")), bool(c._get("daily_breaker")),
                bool(c._get("global_breaker")), bool(c._get("kill_active")), inst, open_risk)

    def status(self) -> dict:
        return self.core.status()

    def close(self) -> None:
        """Освободить in-memory базу прогона (события уже скопированы в self.events)."""
        self.core._mem.close()
