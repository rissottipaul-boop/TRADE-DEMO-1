"""Mean-reversion runner 1H: сигнал → риск → роутер (M2 teamwork, R2).

Закрывает AC R2 поверх существующих сигналов: оценка бара через
`src/mean_reversion` (entry/exit/stop), допуск входа через
`risk.check_entry_allowed`, сайзинг через `risk.size_position`, выставление
через `OrderRouter` (`place_order` на вход, `place_exit_order`/`settle_exits`
на выход). Сигнальная логика НЕ дублируется: дефолтный путь делегирует
`mean_reversion.check_entry/check_exit`, калиброванный путь зовёт те же
`backtest.meanrev.entry_signal/exit_signal/roi_required` с инжектированными
параметрами.

Честное предупреждение из insights/meanrev-backtest.md: дефолты гейт §6.2
НЕ прошли (WF OOS net −17.96%, PF 0.54, edge отрицателен до комиссий).
Поэтому live-вход на дефолтных параметрах ЗАПРЕЩЁН кодом (решение
`blocked` со stage `default_params_live`) — сначала калибровка
MEANREV-CALIB. `dry_run=True` по умолчанию; в dry-run ордера не выставляются
и состояние риск-ядра не меняется (только read-only проверки).

Метка владельца (AGENTS.md §6, src/order_owner.py): дефолт — `botmr`
(Mean Reversion, kind=strategy), зарегистрирован в реестре.
"""
from __future__ import annotations

import logging
import math
from dataclasses import dataclass
from typing import Any, Optional

from src import mean_reversion as mr
from src import order_owner, risk
from src.backtest import meanrev as bt
from src.backtest.data import Bar

log = logging.getLogger("okx.mr_trader")

DEFAULT_INST_ID = "BTC-USDT"  # спот, long-only, 1H (дизайн §1)
DEFAULT_OWNER = order_owner.MR  # "botmr" — Mean Reversion (kind=strategy)
MAX_BARS = 2000  # потолок буфера; индикаторы Уайлдера к этому моменту сошлись

__all__ = ["DEFAULT_INST_ID", "DEFAULT_OWNER", "MAX_BARS", "MRParams",
           "MRPosition", "MRTrader"]


@dataclass(frozen=True)
class MRParams:
    """Калибровочный хук MEANREV-CALIB: rsi_entry / stop_mult / roi-таблица."""

    rsi_entry: float = mr.RSI_ENTRY
    rsi_exit: float = mr.RSI_EXIT
    stop_mult: float = mr.ATR_MULT
    roi_table: tuple[tuple[int, float], ...] = bt.DEFAULT_ROI
    time_stop_min: float | None = bt.TIME_STOP_MIN
    fee_pct: float = 0.10  # taker-оценка выхода для нетто-ROI
    risk_pct: float = risk.DEFAULT_RISK_PCT
    exit_price_guard: bool = True  # False — вариант MEANREV-TEMA-CHECK без guard'а

    def is_default(self) -> bool:
        """Дефолты дизайна §0/§6.2 — live-вход на них запрещён (бэктест минусовой)."""
        return (self.rsi_entry == mr.RSI_ENTRY and self.rsi_exit == mr.RSI_EXIT
                and self.stop_mult == mr.ATR_MULT
                and tuple(self.roi_table) == tuple(bt.DEFAULT_ROI)
                and self.time_stop_min == bt.TIME_STOP_MIN)

    def uses_default_entry(self) -> bool:
        return self.rsi_entry == mr.RSI_ENTRY

    def uses_default_exit(self) -> bool:
        return (self.rsi_exit == mr.RSI_EXIT
                and tuple(self.roi_table) == tuple(bt.DEFAULT_ROI)
                and self.time_stop_min == bt.TIME_STOP_MIN
                and self.fee_pct == 0.10 and self.exit_price_guard is True)


@dataclass
class MRPosition:
    """Открытая позиция раннера (dry-run — виртуальная, live — через роутер)."""

    entry_px: float
    stop_px: float
    entry_minute: float  # bar.ts / 60000 сигнального бара
    sz: float


class MRTrader:
    """Бар-буфер 1H → mean_reversion → risk → OrderRouter.

    Решения `on_bar` (словарь, он же строка `trace`):
    - `warmup` — баров меньше WARMUP (200), сигналов нет;
    - `hold` — сигнала нет (`signal=False`) или позиция держится;
    - `signal` — dry-run вход: позиция открыта виртуально, `order=None`;
    - `entry` — live вход через роутер, `order` — ответ `place_order`;
    - `blocked` — сигнал был, входа нет: `stage`/`reason`
      (`check_entry_allowed`, `size_position`, `default_params_live`,
      `indicators`, `router:<stage>`);
    - `exit` — выход (`reason`: stop/roi/time_stop/signal); в dry-run `order=None`;
    - `exit_failed` — live-выход отклонён роутером, позиция остаётся открытой.
    """

    def __init__(self, router: Any = None, *, inst_id: str = DEFAULT_INST_ID,
                 params: Optional[MRParams] = None, dry_run: bool = True,
                 owner: str = DEFAULT_OWNER) -> None:
        # ORDER-OWNER-TAG: только корень bot* (ордера пишутся в storage
        # и сверяются реконсилятором) — ошибка конфигурации до любых действий.
        self.owner = order_owner.require(owner, own=True).code
        if not dry_run and router is None:
            raise ValueError("live режим (dry_run=False) требует router (OrderRouter)")
        self.router = router
        self.inst_id = inst_id
        self.params = params or MRParams()
        self.dry_run = dry_run
        self._bars: list[Bar] = []
        self.position: Optional[MRPosition] = None
        self.trace: list[dict] = []  # dry-run трассировка сигналов

    def on_bar(self, bar: Bar, *, equity: Optional[float] = None,
               ct_val: float = 1.0, lot_sz: float = 0.0,
               min_sz: float = 0.0) -> dict:
        """Оценить закрывшийся 1H-бар; equity — для сайзинга входа."""
        self._bars.append(bar)
        if len(self._bars) > MAX_BARS:
            del self._bars[:len(self._bars) - MAX_BARS]
        if len(self._bars) < mr.WARMUP:
            return self._record({"decision": "warmup", "bars": len(self._bars),
                                 "need": mr.WARMUP, "dry_run": self.dry_run})
        ind = mr.compute_indicators(self._bars)
        now_minute = bar.ts / 60_000.0
        if self.position is None:
            return self._maybe_enter(bar, ind, now_minute, equity, ct_val,
                                     lot_sz, min_sz)
        return self._maybe_exit(bar, ind, now_minute)

    def settle_exits(self) -> list[dict]:
        """Дочитать неисполненные выходы роутера; в dry-run выходов нет — []."""
        if self.dry_run or self.router is None:
            return []
        return self.router.settle_exits(self.inst_id)

    # --- Вход ---

    def _entry_signal(self, ind: mr.Indicators) -> bool:
        if self.params.uses_default_entry():
            return mr.check_entry(self._bars, ind, -1)
        return bt.entry_signal(ind.rsi, ind.bb_mid, self._bars,
                               len(self._bars) - 1, self.params.rsi_entry)

    def _maybe_enter(self, bar: Bar, ind: mr.Indicators, now_minute: float,
                     equity: Optional[float], ct_val: float,
                     lot_sz: float, min_sz: float) -> dict:
        if not self._entry_signal(ind):
            return self._record({"decision": "hold", "signal": False,
                                 "close": bar.c, "dry_run": self.dry_run})
        atr = ind.atr[-1]
        if not math.isfinite(atr) or atr <= 0:
            return self._record({"decision": "blocked", "signal": True,
                                 "stage": "indicators",
                                 "reason": f"ATR не сошёлся (atr={atr!r}) — вход пропущен",
                                 "dry_run": self.dry_run})
        allowed, reason = risk.check_entry_allowed(self.inst_id, "buy")
        if not allowed:
            return self._record({"decision": "blocked", "signal": True,
                                 "stage": "check_entry_allowed", "reason": reason,
                                 "dry_run": self.dry_run})
        if equity is None:
            return self._record({"decision": "blocked", "signal": True,
                                 "stage": "size_position",
                                 "reason": "нет equity для сайзинга — вход пропущен",
                                 "dry_run": self.dry_run})
        entry_px = bar.c
        stop_px = mr.stop_price(entry_px, atr, self.params.stop_mult)
        sizing = risk.size_position(equity, entry_px, stop_px, ct_val,
                                    lot_sz, min_sz, self.params.risk_pct)
        size = sizing["size"]
        if size <= 0:
            return self._record({"decision": "blocked", "signal": True,
                                 "stage": "size_position",
                                 "reason": ("сайзинг дал нулевой размер: "
                                            + "; ".join(sizing["warnings"])),
                                 "warnings": sizing["warnings"],
                                 "dry_run": self.dry_run})
        base: dict[str, Any] = {"signal": True, "entry_px": entry_px,
                                "stop_px": stop_px, "atr": atr, "sz": size,
                                "notional": sizing["notional"],
                                "warnings": sizing["warnings"],
                                "owner": self.owner, "dry_run": self.dry_run}
        if self.dry_run:
            # Виртуальная позиция для трассировки выходов; register_entry
            # НЕ зовём — dry-run не занимает слоты и heat риск-ядра.
            self.position = MRPosition(entry_px, stop_px, now_minute, size)
            return self._record({"decision": "signal", **base, "order": None,
                                 "note": "dry-run: ордер не выставлен"})
        if self.params.is_default():
            return self._record({"decision": "blocked", **base,
                                 "stage": "default_params_live",
                                 "reason": ("дефолтные параметры убыточны (бэктест WF OOS "
                                            "net −17.96%, PF 0.54) — live вход запрещён, "
                                            "нужна калибровка MEANREV-CALIB")})
        res = self.router.place_order(
            self.inst_id, "buy", "market", px=entry_px, sz=size,
            stop_px=stop_px, equity=equity, ct_val=ct_val, lot_sz=lot_sz,
            min_sz=min_sz, risk_pct=self.params.risk_pct, owner=self.owner)
        if not res.get("ok"):
            return self._record({"decision": "blocked", **base,
                                 "stage": "router:" + str(res.get("stage")),
                                 "reason": str(res.get("reason")), "order": res})
        self.position = MRPosition(entry_px, stop_px, now_minute, size)
        return self._record({"decision": "entry", **base, "order": res})

    # --- Выход ---

    def _exit_reason(self, pos: MRPosition, bar: Bar, ind: mr.Indicators,
                     now_minute: float) -> Optional[str]:
        if self.params.uses_default_exit():
            live = mr.LivePosition(entry_px=pos.entry_px, stop_px=pos.stop_px,
                                   entry_minute=pos.entry_minute)
            return mr.check_exit(live, bar, now_minute, self._bars, ind, -1)
        # Калиброванный путь: тот же приоритет §6.3, что в
        # mean_reversion.check_exit, но с инжектированными rsi_exit /
        # roi_table / time_stop / fee.
        if bar.l <= pos.stop_px:
            return "stop"
        minutes = now_minute - pos.entry_minute
        req = bt.roi_required(self.params.roi_table, minutes)
        net_pct = (bar.c * (1 - self.params.fee_pct / 100.0) / pos.entry_px
                   - 1.0) * 100.0
        if req is not None and net_pct >= req:
            return "roi"
        if (self.params.time_stop_min is not None
                and minutes >= self.params.time_stop_min):
            return "time_stop"
        if bt.exit_signal(ind.rsi, ind.bb_mid, self._bars,
                          len(self._bars) - 1, self.params.rsi_exit,
                          self.params.exit_price_guard):
            return "signal"
        return None

    def _maybe_exit(self, bar: Bar, ind: mr.Indicators,
                    now_minute: float) -> dict:
        pos = self.position
        assert pos is not None
        reason = self._exit_reason(pos, bar, ind, now_minute)
        if reason is None:
            return self._record({"decision": "hold", "signal": False,
                                 "position": {"entry_px": pos.entry_px,
                                              "stop_px": pos.stop_px,
                                              "sz": pos.sz},
                                 "dry_run": self.dry_run})
        base: dict[str, Any] = {"reason": reason, "exit_px": bar.c,
                                "entry_px": pos.entry_px, "sz": pos.sz,
                                "owner": self.owner, "dry_run": self.dry_run}
        if self.dry_run:
            self.position = None
            return self._record({"decision": "exit", **base, "order": None,
                                 "note": "dry-run: ордер не выставлен"})
        res = self.router.place_exit_order(self.inst_id, "sell", "market",
                                           sz=pos.sz, owner=self.owner)
        if not res.get("ok"):
            return self._record({"decision": "exit_failed", **base,
                                 "stage": "router:" + str(res.get("stage")),
                                 "router_reason": str(res.get("reason")),
                                 "order": res})
        if res.get("closed", True):
            self.position = None
        return self._record({"decision": "exit", **base, "order": res})

    def _record(self, decision: dict) -> dict:
        self.trace.append(decision)
        log.info("MR %s %s", self.inst_id, decision.get("decision"))
        return decision
