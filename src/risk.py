"""Риск-ядро проекта (Фаза 3 roadmap, инсайт insights/risk-core.md).

Единственный компонент, обязательный ДО любой стратегии:
- fixed-fractional сайзинг (1% риска, жёсткий потолок 2%);
- portfolio heat <= 6% equity, позиция <= 15% equity;
- дневной лимит убытка -6% (автосброс 00:00 UTC);
- глобальный breaker -15% от high-water mark (только ручной сброс);
- блокировка инструмента после 3 убытков подряд (cooldown 24ч);
- системная пауза после 5 убытков подряд (24ч), лимит 10 входов/день;
- kill-switch (терминальный, сброс только вручную).

Equity и HWM ведёт только update_equity — баланс биржи по рынку, где PnL
открытых позиций уже учтён. record_pnl учитывает закрытую сделку в дневном PnL
и сериях убытков, но equity не меняет: иначе PnL считался бы дважды
(RISK-PNL-DOUBLE). Без свежего equity (EQUITY_MAX_AGE_S) вход запрещён:
breaker'ы по просадке без фида equity слепы.

Выход из позиции (ROUTER-EXIT) — не вход. check_exit_allowed не смотрит на
kill-switch, breaker'ы, свежесть equity и лимит входов: в аварии разрешены
выходы, отмены и чтение. Зато выход проходит только из зарегистрированной
позиции: сторона противоположная, размер не больше остатка. Остаток хранится
в risk_position_size. По исполнению выхода release_position уменьшает его, а у
нуля освобождает слот risk_open_risk.

Состояние — собственный SQLite-файл data/risk_state.db (переживает рестарт,
breaker не снимается перезапуском). Только stdlib. Сетевых вызовов нет:
kill_switch дёргает коннектор через колбэк set_order_canceller().
"""
import logging
import math
import sqlite3
import threading
import time
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Optional

log = logging.getLogger("okx.risk")

# --- Дефолты из insights/risk-core.md (§2.2, §3, §5) ---
DEFAULT_RISK_PCT = 1.0        # 1% equity на сделку
MAX_RISK_PCT = 2.0            # жёсткий потолок риска
MAX_PORTFOLIO_HEAT_PCT = 6.0  # сумма открытых рисков
MAX_POSITION_PCT = 15.0       # notional одной позиции от equity
MAX_OPEN_POSITIONS = 2        # макс. одновременных позиций
DAILY_LOSS_LIMIT_PCT = 6.0    # дневной лимит убытка от equity на 00:00 UTC
GLOBAL_DD_LIMIT_PCT = 15.0    # max drawdown от high-water mark
MAX_ENTRIES_PER_DAY = 10      # лимит входов в день
INST_LOSS_STREAK_BLOCK = 3    # серия убытков по инструменту -> блок
INST_BLOCK_HOURS = 24         # cooldown блокировки инструмента
SYS_LOSS_STREAK_PAUSE = 5     # серия убытков по системе -> пауза 24ч
SYS_PAUSE_HOURS = 24

# Свежесть equity (RISK-PNL-DOUBLE): вход запрещён, если update_equity не было
# дольше этого срока или не было вообще. 600 с — двойной запас к интервалу
# движка (engine._equity_interval = 300 с). Увеличение ослабляет защиту —
# только решение человека (AGENTS.md §2).
EQUITY_MAX_AGE_S = 600

# Потолок плеча SWAP и контрактных ботов (RISK-LEVERAGE-CAP, решение человека):
# инвариант business-plan.md §7 — не выше 3x, только isolated. При 3x ликвидация
# примерно в 30% от входа, дальше любого стопа проекта. Увеличение ослабляет
# защиту — только решение человека (AGENTS.md §2).
MAX_LEVERAGE = 3

_DB_PATH = Path("data/risk_state.db")


def _utc_now() -> float:
    return time.time()


def _utc_day(ts: float) -> str:
    return datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%d")


# Выход (ROUTER-EXIT): сторона выхода для стороны позиции
_EXIT_SIDE = {"buy": "sell", "sell": "buy"}


def _positive(value: Any) -> Optional[float]:
    """Конечное число > 0 или None: размер выхода, исполненный объём."""
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) and number > 0 else None


def _same_size(a: float, b: float) -> bool:
    """Размеры равны с точностью до погрешности float."""
    return math.isclose(a, b, rel_tol=1e-9, abs_tol=1e-12)


class _RiskCore:
    """Stateful ядро: вся персистентность в SQLite, переживает рестарт."""

    def __init__(self, db_path: Path):
        db_path.parent.mkdir(parents=True, exist_ok=True)
        self.db_path = db_path
        self._lock = threading.RLock()
        self._canceller: Optional[Callable[[bool], dict]] = None
        self._init_db()

    @contextmanager
    def _conn(self):
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        try:
            yield conn
            conn.commit()
        finally:
            conn.close()

    def _init_db(self) -> None:
        with self._lock, self._conn() as conn:
            conn.executescript("""
                CREATE TABLE IF NOT EXISTS risk_kv (
                    key TEXT PRIMARY KEY,
                    value TEXT
                );
                CREATE TABLE IF NOT EXISTS risk_events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    ts REAL NOT NULL,
                    event TEXT NOT NULL,
                    inst_id TEXT,
                    detail TEXT
                );
                CREATE INDEX IF NOT EXISTS idx_risk_events_ts ON risk_events(ts);
                CREATE TABLE IF NOT EXISTS risk_instruments (
                    inst_id TEXT PRIMARY KEY,
                    loss_streak INTEGER DEFAULT 0,
                    blocked_until REAL
                );
                CREATE TABLE IF NOT EXISTS risk_open_risk (
                    inst_id TEXT PRIMARY KEY,
                    side TEXT NOT NULL,
                    risk_pct REAL NOT NULL,
                    opened_at REAL NOT NULL
                );
                -- Остаток позиции для выхода (ROUTER-EXIT). Отдельная таблица, а не
                -- колонка risk_open_risk: процессы на старом коде пишут туда INSERT без
                -- списка колонок, и лишняя колонка сломала бы им register_entry.
                -- Запись действительна, пока слот с теми же side и opened_at на месте.
                CREATE TABLE IF NOT EXISTS risk_position_size (
                    inst_id TEXT PRIMARY KEY,
                    side TEXT NOT NULL,
                    opened_at REAL NOT NULL,
                    sz REAL NOT NULL,
                    pos_id TEXT NOT NULL
                );
            """)

    # --- Скалярное состояние ---

    def _get(self, key: str, default: float = 0.0) -> float:
        with self._conn() as conn:
            row = conn.execute("SELECT value FROM risk_kv WHERE key=?", (key,)).fetchone()
            return float(row["value"]) if row else default

    def _set(self, key: str, value: float) -> None:
        with self._conn() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO risk_kv VALUES (?, ?)", (key, str(value))
            )

    def _log_event(self, event: str, inst_id: Optional[str] = None, detail: str = "") -> None:
        log.warning("RISK EVENT: %s inst=%s %s", event, inst_id or "-", detail)
        with self._conn() as conn:
            conn.execute(
                "INSERT INTO risk_events (ts, event, inst_id, detail) VALUES (?, ?, ?, ?)",
                (_utc_now(), event, inst_id, detail),
            )

    # --- Дневной цикл (автосброс 00:00 UTC) ---

    def _maybe_rollover_day(self) -> None:
        today = _utc_day(_utc_now())
        stored = None
        with self._conn() as conn:
            row = conn.execute("SELECT value FROM risk_kv WHERE key='day_date'").fetchone()
            stored = row["value"] if row else None
        if stored != today:
            equity = self._get("equity")
            with self._conn() as conn:
                conn.execute("INSERT OR REPLACE INTO risk_kv VALUES ('day_date', ?)", (today,))
            self._set("day_pnl", 0.0)
            self._set("day_start_equity", equity)
            self._set("entries_today", 0.0)
            if self._get("daily_breaker"):
                self._set("daily_breaker", 0.0)
                self._log_event("daily_breaker_auto_reset", detail=f"day={today}")
            log.info("Дневной rollover 00:00 UTC: day_start_equity=%.2f", equity)

    # --- Публичное API (вызывается модульными обёртками) ---

    def update_equity(self, equity: float) -> list[str]:
        """Equity с биржи (totalEq или стоимость кармана) + проверка лимитов по нему.

        Единственный источник equity и HWM (RISK-PNL-DOUBLE): баланс по рынку уже
        содержит PnL открытых позиций, поэтому record_pnl equity не трогает.
        Нереализованный убыток (открытые позиции, grid-боты) останавливает входы
        через дневной лимит и глобальный breaker здесь же.

        Момент вызова пишется в risk_kv (equity_updated_at) и переживает рестарт:
        по нему check_entry_allowed запрещает вход при устаревшем equity.
        Нечисловое значение (NaN, inf) не принимается и свежесть не продлевает.
        """
        events: list[str] = []
        with self._lock:
            if not math.isfinite(equity):
                self._log_event("equity_invalid", detail=f"equity={equity!r} отброшено")
                return events
            self._maybe_rollover_day()
            self._set("equity", equity)
            self._set("equity_updated_at", _utc_now())
            hwm = self._get("hwm")
            if equity > hwm:
                hwm = equity
                self._set("hwm", hwm)
            day_start = self._get("day_start_equity")
            if day_start <= 0:
                day_start = equity
                self._set("day_start_equity", day_start)

            if (equity <= day_start * (1 - DAILY_LOSS_LIMIT_PCT / 100.0)
                    and not self._get("daily_breaker")):
                self.trip_breaker(
                    f"equity {equity:.2f} ({(equity / day_start - 1) * 100:.1f}% от начала дня)",
                    scope="daily")
                events.append("daily_limit")
            if (equity <= hwm * (1 - GLOBAL_DD_LIMIT_PCT / 100.0)
                    and not self._get("global_breaker")):
                self.trip_breaker(
                    f"drawdown {(equity / hwm - 1) * 100:.1f}% от HWM {hwm:.2f}", scope="global")
                events.append("global_breaker")
        return events

    def _equity_stale_reason(self) -> Optional[str]:
        """Причина запрета входа по свежести equity или None (RISK-PNL-DOUBLE).

        Equity меняет только update_equity, поэтому без свежего вызова глобальный
        breaker и дневной лимит по equity не увидят просадку. Отметка «из будущего»
        дальше EQUITY_MAX_AGE_S (часы переведены назад) тоже считается устаревшей.
        """
        updated_at = self._get("equity_updated_at")
        if updated_at <= 0:
            return ("equity ни разу не выставлялась: нет фида equity (update_equity по "
                    "балансу биржи) — без него breaker'ы по просадке не работают")
        age = _utc_now() - updated_at
        if age > EQUITY_MAX_AGE_S or age < -EQUITY_MAX_AGE_S:
            return (f"equity устарела: последний update_equity {age:.0f} с назад, допустимо "
                    f"до {EQUITY_MAX_AGE_S} с — проверьте фид equity (движок или бот, "
                    f"update_equity по балансу биржи)")
        return None

    def check_entry_allowed(self, inst_id: str, side: str) -> tuple[bool, str]:
        with self._lock:
            self._maybe_rollover_day()
            if self._get("kill_active"):
                return False, "kill_switch активен (сброс только вручную)"
            if self._get("global_breaker"):
                return False, "глобальный breaker: drawdown >= 15% от HWM, нужен ручной сброс"
            if self._get("daily_breaker"):
                return False, "дневной лимит убытка -6% исчерпан (сброс 00:00 UTC)"
            stale = self._equity_stale_reason()
            if stale:
                return False, stale
            pause_until = self._get("system_pause_until")
            if pause_until and _utc_now() < pause_until:
                return False, f"системная пауза после {SYS_LOSS_STREAK_PAUSE} убытков подряд (24ч)"
            if self._get("entries_today") >= MAX_ENTRIES_PER_DAY:
                return False, f"лимит входов в день ({MAX_ENTRIES_PER_DAY}) исчерпан"
            if self.is_instrument_blocked(inst_id):
                return False, f"инструмент {inst_id} заблокирован после серии убытков (cooldown {INST_BLOCK_HOURS}ч)"
            with self._conn() as conn:
                rows = conn.execute("SELECT inst_id, side, risk_pct FROM risk_open_risk").fetchall()
            if rows:
                if any(r["inst_id"] == inst_id for r in rows):
                    return False, f"по {inst_id} уже есть открытая позиция (хедж запрещён)"
                if len(rows) >= MAX_OPEN_POSITIONS:
                    return False, f"макс. одновременных позиций ({MAX_OPEN_POSITIONS}) достигнут"
                heat = sum(r["risk_pct"] for r in rows)
                if heat + DEFAULT_RISK_PCT > MAX_PORTFOLIO_HEAT_PCT:
                    return False, f"portfolio heat {heat:.1f}% + новый риск превысит {MAX_PORTFOLIO_HEAT_PCT}%"
            return True, "ok"

    def size_position(self, equity: float, entry: float, stop: float,
                      ct_val: float, lot_sz: float, min_sz: float,
                      risk_pct: float = DEFAULT_RISK_PCT) -> dict:
        warnings: list[str] = []
        if risk_pct > MAX_RISK_PCT:
            warnings.append(f"risk_pct {risk_pct}% обрезан до потолка {MAX_RISK_PCT}%")
            risk_pct = MAX_RISK_PCT
        if entry <= 0 or stop <= 0 or entry == stop:
            return {"size": 0.0, "dollar_risk": 0.0, "notional": 0.0,
                    "warnings": warnings + ["некорректные entry/stop"]}

        dollar_risk = equity * risk_pct / 100.0
        risk_per_contract = abs(entry - stop) * ct_val
        contracts = math.floor(dollar_risk / risk_per_contract)

        # Потолок позиции: notional <= 15% equity (resilience-trader Step 3.3)
        max_contracts_by_cap = math.floor((equity * MAX_POSITION_PCT / 100.0) / (entry * ct_val))
        if contracts > max_contracts_by_cap:
            warnings.append(f"размер обрезан потолком {MAX_POSITION_PCT}% equity "
                            f"({contracts} -> {max_contracts_by_cap} контрактов)")
            contracts = max_contracts_by_cap

        # floor к lotSz (position-sizer №3)
        if lot_sz > 0:
            contracts = math.floor(contracts / lot_sz) * lot_sz

        if contracts < min_sz:
            warnings.append(f"размер {contracts} < min_sz {min_sz}: вход по сайзингу невозможен")
            contracts = 0.0

        notional = contracts * ct_val * entry
        # Анти-паттерн rookie-airbag: notional не должен превышать 30% equity
        if notional > equity * 0.30:
            warnings.append("notional превышает 30% equity — ордер обязан быть отклонён")
            contracts = 0.0
            notional = 0.0

        return {"size": contracts, "dollar_risk": dollar_risk,
                "notional": notional, "warnings": warnings}

    def validate_stop_vs_liquidation(self, entry: float, stop: float,
                                     liq_price: float, side: str) -> bool:
        """Стоп обязан срабатывать раньше ликвидации (position-sizer №9)."""
        if side == "long":
            ok = liq_price < stop < entry
        elif side == "short":
            ok = entry < stop < liq_price
        else:
            ok = False
        if not ok:
            self._log_event("stop_beyond_liquidation",
                            detail=f"side={side} entry={entry} stop={stop} liq={liq_price}")
        return ok

    def register_entry(self, inst_id: str, side: str, risk_pct: float = DEFAULT_RISK_PCT) -> None:
        """Фиксация открытой позиции: portfolio heat + счётчик входов/день."""
        with self._lock:
            self._maybe_rollover_day()
            with self._conn() as conn:
                conn.execute(
                    "INSERT OR REPLACE INTO risk_open_risk VALUES (?, ?, ?, ?)",
                    (inst_id, side, risk_pct, _utc_now()),
                )
            self._set("entries_today", self._get("entries_today") + 1)

    def register_spot_buy(self, inst_id: str) -> None:
        """Доливка спот-позиции без плеча и стопа (DCA и подобные накопления).

        Освобождает слот risk_open_risk: спотовое усреднение одной позиции —
        не «хедж», иначе следующая покупка была бы отклонена check_entry_allowed.
        Это НЕ закрытие сделки, поэтому метод намеренно НЕ трогает:
        - серии убытков (per-instrument и глобальную global_loss_streak) —
          покупка не обнуляет серию и не отменяет системную паузу;
        - day_pnl, equity, hwm — реализованного PnL у покупки нет;
        - entries_today — счётчик входов уже инкрементировал register_entry
          (вызывается роутером на каждый ордер, включая покупки DCA).
        Реализованный PnL спотовой позиции фиксируется record_pnl при продаже
        (day_pnl и серии), equity — только update_equity по балансу.
        """
        with self._lock:
            self._maybe_rollover_day()
            with self._conn() as conn:
                cur = conn.execute("DELETE FROM risk_open_risk WHERE inst_id=?", (inst_id,))
            self._log_event("spot_buy", inst_id=inst_id,
                            detail=f"доливка спот-позиции, слот риска освобождён "
                                   f"(удалено записей: {cur.rowcount})")

    # --- Выход из позиции (ROUTER-EXIT) ---
    #
    # Остаток позиции хранится в risk_position_size и привязан к слоту
    # risk_open_risk по (inst_id, side, opened_at). Запись становится
    # недействительной, если по тому же inst_id прошёл новый register_entry
    # (другой opened_at) или слот удалили register_spot_buy или record_pnl.
    # Тогда остаток неизвестен, и выход через роутер отклоняется: это
    # безопасная сторона. pos_id отличает позицию от следующей на том же
    # inst_id, поэтому поздно исполненный старый выход новую позицию не тронет.

    def register_entry_size(self, inst_id: str, side: str, sz: Any) -> bool:
        """Размер входа для слота, который только что занял register_entry.

        Это верхняя граница позиции: размер ордера, а не исполнения. Выход
        сверяется с ней, а фактический объём ограничивает биржа: на споте —
        баланс и запрет займа, на контрактах — reduceOnly. Лимиты, счётчики и
        heat не трогает. False — слота с этой стороной нет или размер
        некорректен. Тогда размер не записан, и выход через роутер отклоняется.
        """
        size = _positive(sz)
        with self._lock:
            with self._conn() as conn:
                slot = conn.execute(
                    "SELECT side, opened_at FROM risk_open_risk WHERE inst_id=?", (inst_id,)
                ).fetchone()
                if size is None or slot is None or slot["side"] != side:
                    log.warning("register_entry_size %s %s sz=%r: слота с этой стороной нет или "
                                "размер некорректен — размер не записан", inst_id, side, sz)
                    return False
                conn.execute(
                    "INSERT OR REPLACE INTO risk_position_size "
                    "(inst_id, side, opened_at, sz, pos_id) VALUES (?, ?, ?, ?, ?)",
                    (inst_id, side, slot["opened_at"], size, uuid.uuid4().hex),
                )
            return True

    def open_position(self, inst_id: str) -> Optional[dict]:
        """Открытая позиция по inst_id или None, если слота нет.

        Поля слота risk_open_risk (side, risk_pct, opened_at) и остаток sz с
        pos_id из risk_position_size. sz и pos_id равны None, если остаток
        неизвестен: вход зарегистрирован без размера (движок, грид) или запись
        размера относится к прежнему слоту.
        """
        with self._conn() as conn:
            row = conn.execute(
                "SELECT o.inst_id, o.side, o.risk_pct, o.opened_at, s.sz, s.pos_id "
                "FROM risk_open_risk AS o LEFT JOIN risk_position_size AS s "
                "ON s.inst_id = o.inst_id AND s.side = o.side AND s.opened_at = o.opened_at "
                "WHERE o.inst_id=?",
                (inst_id,),
            ).fetchone()
        return dict(row) if row else None

    def check_exit_allowed(self, inst_id: str, side: str,
                           sz: Any = None) -> tuple[bool, str, Optional[dict]]:
        """Допуск ВЫХОДА из позиции — не входа.

        Kill-switch, breaker'ы, свежесть equity, системная пауза, лимит
        входов, блокировка инструмента, хедж и heat здесь НЕ проверяются: в
        аварии разрешены выходы, отмены и чтение. Поэтому выход не должен
        открывать или наращивать позицию, и проверяется другое:
        - по inst_id есть открытая позиция (слот risk_open_risk);
        - сторона выхода противоположна стороне позиции;
        - остаток известен, размер выхода больше нуля и не больше остатка.
        sz=None — выход на весь остаток. Возвращает (True, "ok", позиция
        open_position с полем exit_sz) или (False, причина, None).
        """
        with self._lock:
            pos = self.open_position(inst_id)
            if pos is None:
                return False, (f"по {inst_id} нет открытой позиции в риск-ядре — выход не может "
                               f"открыть позицию (вход — через place_order)"), None
            exit_side = _EXIT_SIDE.get(pos["side"])
            if exit_side is None:
                return False, (f"позиция {inst_id} зарегистрирована со стороной {pos['side']!r} — "
                               f"сторона выхода не определена"), None
            if side != exit_side:
                return False, (f"позиция {inst_id} — {pos['side']}, выход только {exit_side}: "
                               f"{side} открыл бы или нарастил позицию"), None
            remaining = pos["sz"]
            if remaining is None:
                return False, (f"остаток позиции {inst_id} неизвестен (вход зарегистрирован без "
                               f"размера) — размер выхода не проверить, закройте позицию вручную"), None
            if sz is None:
                exit_sz = remaining
            else:
                exit_sz = _positive(sz)
                if exit_sz is None:
                    return False, f"некорректный размер выхода {sz!r}", None
                if exit_sz > remaining and not _same_size(exit_sz, remaining):
                    return False, (f"размер выхода {exit_sz:g} больше остатка позиции {inst_id} "
                                   f"{remaining:g} — излишек открыл бы встречную позицию"), None
            return True, "ok", {**pos, "exit_sz": exit_sz}

    def release_position(self, inst_id: str, filled_sz: Any,
                         pos_id: Optional[str] = None) -> dict:
        """Исполненный ВЫХОД: уменьшить остаток, а у нуля освободить слот.

        filled_sz — исполненный объём выхода, а не размер ордера: слот
        освобождается по исполнению, а не по выставлению. Когда остаток доходит
        до нуля, позиция закрыта: слот risk_open_risk и запись размера
        удаляются, heat уменьшается, и по inst_id снова возможен вход. Иначе
        остаток уменьшается, а слот и его risk_pct остаются до полного выхода.
        pos_id — позиция, для которой проверялся выход. Если позиция сменилась,
        остаток неизвестен или слота нет, ничего не меняется: занятый слот лишь
        запрещает вход, это безопасная сторона. Серии убытков, day_pnl и equity
        не трогает, это делает record_pnl. Возвращает
        {released, remaining, closed, reason}.
        """
        size = _positive(filled_sz)
        with self._lock:
            pos = self.open_position(inst_id)
            skip = ""
            if size is None:
                skip = f"исполнено {filled_sz!r} — освобождать нечего"
            elif pos is None:
                skip = "слота нет — позиция уже закрыта"
            elif pos["sz"] is None:
                skip = "остаток неизвестен — слот не трогаем"
            elif pos_id is not None and pos["pos_id"] != pos_id:
                skip = "позиция сменилась после выставления выхода — новую не трогаем"
            if skip:
                if size is not None:
                    self._log_event("exit_release_skipped", inst_id=inst_id,
                                    detail=f"исполнено {size:g}: {skip}")
                return {"released": 0.0, "remaining": pos["sz"] if pos else None,
                        "closed": False, "reason": skip}
            closed = size > pos["sz"] or _same_size(size, pos["sz"])
            remaining = 0.0 if closed else pos["sz"] - size
            with self._conn() as conn:
                if closed:
                    conn.execute("DELETE FROM risk_open_risk WHERE inst_id=? AND opened_at=?",
                                 (inst_id, pos["opened_at"]))
                    conn.execute("DELETE FROM risk_position_size WHERE inst_id=?", (inst_id,))
                else:
                    conn.execute("UPDATE risk_position_size SET sz=? WHERE inst_id=? AND pos_id=?",
                                 (remaining, inst_id, pos["pos_id"]))
            self._log_event("position_closed" if closed else "position_reduced", inst_id=inst_id,
                            detail=f"выход исполнен {size:g}: остаток {pos['sz']:g} -> {remaining:g}")
            return {"released": min(size, pos["sz"]), "remaining": remaining,
                    "closed": closed, "reason": "ok"}

    def record_pnl(self, inst_id: str, pnl: float, closed_at: datetime) -> list[str]:
        """Результат закрытой сделки: дневной PnL, серии убытков, блокировки, пауза.

        Equity и HWM не меняет (RISK-PNL-DOUBLE): их ведёт update_equity по балансу
        биржи, где PnL сделки уже учтён — до закрытия как нереализованный, после
        как реализованный. Прибавка здесь учла бы его дважды: прибыль завышала бы
        HWM, убыток вычитался бы повторно, и глобальный breaker срабатывал бы
        раньше номинала. Дневной лимит здесь считается по day_pnl, глобальный
        breaker — по текущей equity из update_equity.
        """
        events: list[str] = []
        with self._lock:
            self._maybe_rollover_day()
            equity = self._get("equity")
            hwm = self._get("hwm")

            # Атрибуция PnL к дню закрытия (UTC)
            if _utc_day(closed_at.timestamp()) == _utc_day(_utc_now()):
                day_pnl = self._get("day_pnl") + pnl
                self._set("day_pnl", day_pnl)
            else:
                day_pnl = self._get("day_pnl")

            with self._conn() as conn:
                conn.execute("DELETE FROM risk_open_risk WHERE inst_id=?", (inst_id,))
                row = conn.execute(
                    "SELECT loss_streak FROM risk_instruments WHERE inst_id=?", (inst_id,)
                ).fetchone()
                prev_streak = row["loss_streak"] if row else 0
                inst_streak = prev_streak + 1 if pnl < 0 else 0
                conn.execute(
                    "INSERT OR REPLACE INTO risk_instruments (inst_id, loss_streak, blocked_until) "
                    "VALUES (?, ?, COALESCE((SELECT blocked_until FROM risk_instruments WHERE inst_id=?), NULL))",
                    (inst_id, inst_streak, inst_id),
                )

            self._log_event("pnl", inst_id=inst_id,
                            detail=f"pnl={pnl:.2f} equity={equity:.2f} day_pnl={day_pnl:.2f}")

            # Дневной лимит -6%
            day_start = self._get("day_start_equity")
            if (day_start > 0 and day_pnl <= -DAILY_LOSS_LIMIT_PCT / 100.0 * day_start
                    and not self._get("daily_breaker")):
                self.trip_breaker(
                    f"дневной убыток {day_pnl:.2f} ({day_pnl / day_start * 100:.1f}%)", scope="daily")
                events.append("daily_limit")

            # Глобальный breaker -15% от HWM — по текущей equity из update_equity
            if hwm > 0 and equity <= hwm * (1 - GLOBAL_DD_LIMIT_PCT / 100.0) \
                    and not self._get("global_breaker"):
                self.trip_breaker(
                    f"drawdown {(equity / hwm - 1) * 100:.1f}% от HWM {hwm:.2f}", scope="global")
                events.append("global_breaker")

            # Серия убытков по инструменту -> блокировка 24ч
            if pnl < 0:
                if inst_streak >= INST_LOSS_STREAK_BLOCK and not self.is_instrument_blocked(inst_id):
                    self.block_instrument(inst_id, INST_BLOCK_HOURS)
                    events.append("instrument_blocked")
                # Глобальная серия убытков -> системная пауза 24ч
                sys_streak = self._get("global_loss_streak") + 1
                self._set("global_loss_streak", sys_streak)
                if sys_streak >= SYS_LOSS_STREAK_PAUSE:
                    self._set("system_pause_until", _utc_now() + SYS_PAUSE_HOURS * 3600)
                    self._set("global_loss_streak", 0.0)
                    self._log_event("system_pause",
                                    detail=f"{SYS_LOSS_STREAK_PAUSE} убытков подряд, пауза {SYS_PAUSE_HOURS}ч")
                    events.append("system_pause")
            else:
                self._set("global_loss_streak", 0.0)
        return events

    # --- Breakers ---

    def trip_breaker(self, reason: str, scope: str = "global") -> None:
        with self._lock:
            key = "daily_breaker" if scope == "daily" else "global_breaker"
            if self._get(key):
                return  # идемпотентно
            self._set(key, 1.0)
            self._log_event(f"breaker_trip_{scope}", detail=reason)

    def reset_breaker(self, scope: str, by: str = "manual") -> None:
        with self._lock:
            if scope == "daily":
                self._set("daily_breaker", 0.0)
            elif scope == "global":
                self._set("global_breaker", 0.0)
            elif scope == "kill":
                self._set("kill_active", 0.0)
            else:
                raise ValueError(f"неизвестный scope: {scope}")
            self._log_event(f"breaker_reset_{scope}", detail=f"by={by}")

    def block_instrument(self, inst_id: str, hours: int = INST_BLOCK_HOURS) -> None:
        with self._lock:
            until = _utc_now() + hours * 3600
            with self._conn() as conn:
                conn.execute(
                    "INSERT INTO risk_instruments (inst_id, loss_streak, blocked_until) VALUES (?, 0, ?) "
                    "ON CONFLICT(inst_id) DO UPDATE SET blocked_until=excluded.blocked_until",
                    (inst_id, until),
                )
            self._log_event("instrument_blocked", inst_id=inst_id, detail=f"cooldown {hours}ч")

    def is_instrument_blocked(self, inst_id: str) -> bool:
        with self._conn() as conn:
            row = conn.execute(
                "SELECT blocked_until FROM risk_instruments WHERE inst_id=?", (inst_id,)
            ).fetchone()
        if not row or row["blocked_until"] is None:
            return False
        if _utc_now() >= row["blocked_until"]:
            with self._conn() as conn:
                conn.execute(
                    "UPDATE risk_instruments SET blocked_until=NULL WHERE inst_id=?", (inst_id,)
                )
            self._log_event("instrument_unblocked", inst_id=inst_id, detail="cooldown истёк")
            return False
        return True

    # --- Kill-switch ---

    def set_order_canceller(self, callback: Callable[[bool], dict]) -> None:
        """Колбэк коннектора: cancel_all(flatten) -> {cancelled: [...], failed: [...]}.

        Обязан отменять ВСЕ ордера, включая algo (orders-algo-pending)."""
        self._canceller = callback

    def kill_switch(self, flatten: bool = False, by: str = "manual") -> dict:
        with self._lock:
            self._set("kill_active", 1.0)
            report: dict[str, Any] = {"cancelled": [], "failed": [], "flattened": flatten}
            if self._canceller is None:
                report["failed"].append("коннектор не подключён (set_order_canceller не вызван)")
            else:
                try:
                    result = self._canceller(flatten)
                    report["cancelled"] = list(result.get("cancelled", []))
                    report["failed"] = list(result.get("failed", []))
                except Exception as exc:  # не глушим: фиксируем и эскалируем
                    report["failed"].append(f"исключение коннектора: {exc}")
            self._log_event(
                "kill_switch",
                detail=f"by={by} flatten={flatten} cancelled={len(report['cancelled'])} "
                       f"failed={len(report['failed'])}",
            )
            if report["failed"]:
                log.error("KILL-SWITCH: не отменено %s ордеров — требуется ручная проверка!",
                          report["failed"])
            return report

    # --- Диагностика ---

    def status(self) -> dict:
        with self._lock:
            self._maybe_rollover_day()
            equity = self._get("equity")
            hwm = self._get("hwm")
            updated_at = self._get("equity_updated_at")
            with self._conn() as conn:
                open_risk = [dict(r) for r in conn.execute(
                    "SELECT inst_id, side, risk_pct FROM risk_open_risk").fetchall()]
                instruments = [dict(r) for r in conn.execute(
                    "SELECT inst_id, loss_streak, blocked_until FROM risk_instruments").fetchall()]
            return {
                "equity": equity,
                # Свежесть фида equity: вход запрещён при возрасте > EQUITY_MAX_AGE_S
                "equity_updated_at": updated_at or None,
                "equity_age_s": _utc_now() - updated_at if updated_at else None,
                "hwm": hwm,
                "drawdown_pct": (equity / hwm - 1) * 100.0 if hwm > 0 else 0.0,
                "day_pnl": self._get("day_pnl"),
                "day_start_equity": self._get("day_start_equity"),
                "entries_today": int(self._get("entries_today")),
                "portfolio_heat_pct": sum(r["risk_pct"] for r in open_risk),
                "open_risk": open_risk,
                "daily_breaker": bool(self._get("daily_breaker")),
                "global_breaker": bool(self._get("global_breaker")),
                "kill_active": bool(self._get("kill_active")),
                "system_pause_until": self._get("system_pause_until") or None,
                "global_loss_streak": int(self._get("global_loss_streak")),
                "instruments": instruments,
            }


_core: Optional[_RiskCore] = None
_core_lock = threading.Lock()


def init(db_path: Path | str = _DB_PATH) -> None:
    """Инициализация модуля (путь к SQLite). Вызывается один раз на старте."""
    global _core
    with _core_lock:
        _core = _RiskCore(Path(db_path))


def _c() -> _RiskCore:
    if _core is None:
        init()
    return _core


# --- Модульная API-поверхность (контракт insights/risk-core.md §6) ---

def check_entry_allowed(inst_id: str, side: str) -> tuple[bool, str]:
    """Единая точка допуска перед каждым ордером (включая DCA/grid).

    Требует свежего equity: сначала update_equity по балансу, потом вход
    (EQUITY_MAX_AGE_S, RISK-PNL-DOUBLE)."""
    allowed, reason = _c().check_entry_allowed(inst_id, side)
    if not allowed:
        _c()._log_event("entry_denied", inst_id=inst_id, detail=f"side={side}: {reason}")
    return allowed, reason


def size_position(equity: float, entry: float, stop: float,
                  ct_val: float, lot_sz: float, min_sz: float,
                  risk_pct: float = DEFAULT_RISK_PCT) -> dict:
    return _c().size_position(equity, entry, stop, ct_val, lot_sz, min_sz, risk_pct)


def validate_stop_vs_liquidation(entry: float, stop: float, liq_price: float,
                                 side: str) -> bool:
    return _c().validate_stop_vs_liquidation(entry, stop, liq_price, side)


def check_leverage(lever: Any) -> tuple[bool, str]:
    """Плечо SWAP-ордера или контрактного бота: от 1 до MAX_LEVERAGE (RISK-LEVERAGE-CAP).

    Возвращает (False, причина) для нечисла, NaN, inf, плеча меньше 1 и выше потолка.
    Строка ("3", как в ответах OKX) допустима."""
    try:
        value = float(lever)
    except (TypeError, ValueError):
        return False, f"плечо {lever!r} — не число"
    if not math.isfinite(value) or value < 1:
        return False, f"плечо {lever!r} вне диапазона 1…{MAX_LEVERAGE}"
    if value > MAX_LEVERAGE:
        return False, f"плечо {value:g}x выше потолка {MAX_LEVERAGE}x (risk.MAX_LEVERAGE)"
    return True, "ok"


def record_pnl(inst_id: str, pnl: float, closed_at: datetime) -> list[str]:
    """Закрытая сделка: day_pnl, серии убытков, блокировки. Equity и HWM не меняет."""
    return _c().record_pnl(inst_id, pnl, closed_at)


def trip_breaker(reason: str, scope: str = "global") -> None:
    _c().trip_breaker(reason, scope)


def reset_breaker(scope: str, by: str = "manual") -> None:
    _c().reset_breaker(scope, by)


def block_instrument(inst_id: str, hours: int = INST_BLOCK_HOURS) -> None:
    _c().block_instrument(inst_id, hours)


def is_instrument_blocked(inst_id: str) -> bool:
    return _c().is_instrument_blocked(inst_id)


def kill_switch(flatten: bool = False, by: str = "manual") -> dict:
    return _c().kill_switch(flatten, by)


def set_order_canceller(callback: Callable[[bool], dict]) -> None:
    _c().set_order_canceller(callback)


def update_equity(equity: float) -> list[str]:
    """Equity по балансу биржи — единственный источник equity и HWM; продлевает
    свежесть equity для check_entry_allowed. Может сработать breaker."""
    return _c().update_equity(equity)


def register_entry(inst_id: str, side: str, risk_pct: float = DEFAULT_RISK_PCT) -> None:
    _c().register_entry(inst_id, side, risk_pct)


def register_spot_buy(inst_id: str) -> None:
    """Доливка спот-позиции (DCA): освобождает слот risk_open_risk без побочки
    на серии убытков, day_pnl и equity. Счётчик entries_today учитывает покупку
    через register_entry в OrderRouter."""
    _c().register_spot_buy(inst_id)


def register_entry_size(inst_id: str, side: str, sz: Any) -> bool:
    """Размер входа для слота register_entry (ROUTER-EXIT): верхняя граница
    позиции, с которой сверяется выход. Лимиты, счётчики и heat не трогает."""
    return _c().register_entry_size(inst_id, side, sz)


def open_position(inst_id: str) -> Optional[dict]:
    """Слот risk_open_risk и остаток sz с pos_id (None — остаток неизвестен) или None."""
    return _c().open_position(inst_id)


def check_exit_allowed(inst_id: str, side: str,
                       sz: Any = None) -> tuple[bool, str, Optional[dict]]:
    """Допуск выхода (ROUTER-EXIT): позиция есть, сторона противоположна,
    размер не больше остатка. Kill-switch, breaker'ы и свежесть equity выход не
    блокируют. Отказы пишутся в журнал риск-событий."""
    allowed, reason, position = _c().check_exit_allowed(inst_id, side, sz)
    if not allowed:
        _c()._log_event("exit_denied", inst_id=inst_id, detail=f"side={side} sz={sz!r}: {reason}")
    return allowed, reason, position


def release_position(inst_id: str, filled_sz: Any, pos_id: Optional[str] = None) -> dict:
    """Исполненный выход (ROUTER-EXIT): остаток уменьшается на filled_sz, у нуля
    слот освобождается. Это не record_pnl: серии и day_pnl не меняет."""
    return _c().release_position(inst_id, filled_sz, pos_id)


def status() -> dict:
    return _c().status()
