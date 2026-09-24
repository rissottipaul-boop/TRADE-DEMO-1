"""Live-runner кармана (задача LIVE-RUNNER): торгует рукава из ops/live-pocket.json.

    python -m src.live_runner          # рукава кармана (сейчас — dca) до DONE/паузы/стопа
    python -m src.live_runner --once   # не больше одной покупки и выход — первый live-цикл

Фоном — `ops\\live.ps1 start|status|stop`.

Периметр проверяет сам runner, а не guard (insights/business-plan.md §3, §7):
1. старт — только при зелёном src.live_preflight (ключ суб-аккаунта без withdraw,
   окно с датой окончания, валидный карман, деньги аккаунта ≤ бюджета);
2. перед КАЖДОЙ покупкой — окно ops/live-policy.json, карман (рукав включён,
   параметры не менялись, потрачено + покупка ≤ max_total_usdt), флаги процесса;
3. интервал считается от последней покупки в live-хранилище — рестарт процесса
   не даёт лишней покупки;
4. ордера — только через OrderRouter (risk.check_entry_allowed); риск-ядро и
   хранилище — data/live/ (config.state_paths); equity — стоимость кармана;
   clOrdId — с префиксом владельца `botldca` (ORDER-OWNER-TAG, src/order_owner.py):
   в live-суб-аккаунте ордер без `botl*` — не от runner'а;
5. в ожидании каждые 30 с проверяются флаги: data/live/KILL — kill-switch
   (отмена всех ордеров и ботов аккаунта, блок входов до ручного сброса) и выход;
   data/live/STOP_RUNNER — штатный выход.

Коды выхода: 0 — штатно, 1 — пауза (периметр или риск-ядро отказали во входе),
2 — preflight не пройден, 3 — kill-switch.
"""
import argparse
import logging
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Optional

from . import order_owner, risk
from .config import StatePaths, load_settings, state_paths
from .connector import create_exchange
from .dca_bot import DCABot
from .live_policy import (
    LIVE_POCKET_PATH,
    LIVE_POLICY_PATH,
    PocketError,
    enabled_sleeve,
    live_window,
    load_pocket,
    read_json,
)
from .live_preflight import fetch_pocket_equity, run_preflight
from .order_router import OrderRouter
from .storage import Storage

log = logging.getLogger("okx.live_runner")

MODE = "live"
CHECK_EVERY_S = 30.0
STOP_FLAG_NAME = "STOP_RUNNER"
UNLIMITED_BUYS = 10**6  # число покупок ограничивает лимит рукава, а не счётчик


class StopRunner(Exception):
    """Остановка по флагу во время работы (KILL или STOP_RUNNER)."""


def dca_spent_usdt(storage: Storage, inst_id: str) -> float:
    """Потрачено рукавом: сумма покупок в live-хранилище (оно — только кармана)."""
    return sum(float(t["px"]) * float(t["sz"]) for t in storage.get_trades(inst_id, limit=100_000)
               if t["side"] == "buy")


def last_buy_ts(storage: Storage, inst_id: str) -> Optional[float]:
    buys = [float(t["ts"]) for t in storage.get_trades(inst_id, limit=100_000) if t["side"] == "buy"]
    return max(buys) if buys else None


class Perimeter:
    """Проверки live-периметра: файлы человека и флаги процесса."""

    def __init__(self, paths: StatePaths, pocket_path: Path = LIVE_POCKET_PATH,
                 policy_path: Path = LIVE_POLICY_PATH,
                 now: Callable[[], datetime] = lambda: datetime.now(timezone.utc)) -> None:
        self.paths = paths
        self.pocket_path = pocket_path
        self.policy_path = policy_path
        self.now = now
        self.stop_flag = paths.root / STOP_FLAG_NAME
        self.killed = False

    def check_flags(self) -> None:
        """KILL → kill-switch + StopRunner; STOP_RUNNER → StopRunner. Флаг снимается при обработке."""
        if self.paths.kill_flag.exists():
            reason = self.paths.kill_flag.read_text(encoding="utf-8").strip() or "file flag"
            log.critical("KILL (live) по флагу: %s", reason)
            self.paths.kill_flag.unlink(missing_ok=True)
            report = risk.kill_switch(flatten=False, by=f"live flag: {reason}")
            self.killed = True
            log.critical("KILL (live): отменено %d, не отменено %d",
                         len(report["cancelled"]), len(report["failed"]))
            raise StopRunner(f"kill: {reason}")
        if self.stop_flag.exists():
            reason = self.stop_flag.read_text(encoding="utf-8").strip() or "stop flag"
            self.stop_flag.unlink(missing_ok=True)
            raise StopRunner(f"stop: {reason}")

    def dca_gate(self, storage: Storage, sleeve: dict) -> Callable[[], tuple[bool, str]]:
        """before_buy для DCABot: все проверки заново перед каждой покупкой."""
        inst_id, per_buy = sleeve["inst_id"], float(sleeve["quote_per_buy_usdt"])

        def gate() -> tuple[bool, str]:
            self.check_flags()
            try:
                is_open, why = live_window(read_json(self.policy_path), self.now())
                if not is_open:
                    return False, f"live-окно: {why}"
                current = enabled_sleeve(load_pocket(self.pocket_path), "dca")
            except PocketError as exc:
                return False, f"карман/политика: {exc}"
            if current is None:
                return False, "рукав dca выключен в кармане"
            if current["inst_id"] != inst_id or float(current["quote_per_buy_usdt"]) != per_buy:
                return False, "параметры рукава dca изменились — перезапустите runner"
            spent, cap = dca_spent_usdt(storage, inst_id), float(current["max_total_usdt"])
            if spent + per_buy > cap + 1e-9:
                return False, f"лимит рукава: потрачено {spent:.2f} + {per_buy:.2f} > {cap:.2f} USDT"
            return True, "ok"

        return gate


def wait_until(due_ts: float, perimeter: Perimeter, sleep: Callable[[float], None] = time.sleep,
               clock: Callable[[], float] = time.time, check_every: float = CHECK_EVERY_S) -> None:
    """Ждать до due_ts, проверяя флаги каждые check_every секунд."""
    while True:
        perimeter.check_flags()
        remaining = due_ts - clock()
        if remaining <= 0:
            return
        sleep(min(check_every, remaining))


def run(once: bool = False, exchange: Any = None, pocket_path: Path = LIVE_POCKET_PATH,
        policy_path: Path = LIVE_POLICY_PATH, sleep: Callable[[float], None] = time.sleep,
        clock: Callable[[], float] = time.time,
        now: Callable[[], datetime] = lambda: datetime.now(timezone.utc)) -> int:
    paths = state_paths(MODE)
    risk.init(paths.risk_db)
    if exchange is None:
        exchange = create_exchange(load_settings(MODE))

    report = run_preflight(MODE, exchange, pocket_path=pocket_path, policy_path=policy_path, now=now())
    if not report["ok"]:
        for check in report["checks"]:
            if check["status"] == "fail":
                log.error("preflight: %s — %s", check["name"], check["detail"])
        return 2

    sleeve = enabled_sleeve(load_pocket(pocket_path), "dca")
    if sleeve is None:
        log.info("в кармане нет включённых рукавов — выход")
        return 0

    router = OrderRouter(exchange, db_path=paths.bot_db, owner=order_owner.LIVE_DCA)
    storage = router.storage
    perimeter = Perimeter(paths, pocket_path, policy_path, now)
    inst_id = sleeve["inst_id"]
    interval_s = float(sleeve["interval_hours"]) * 3600
    try:
        last = last_buy_ts(storage, inst_id)
        if last is not None and clock() < last + interval_s:
            log.info("следующая покупка по графику через %.0f с", last + interval_s - clock())
            wait_until(last + interval_s, perimeter, sleep, clock)
        done = int(float(storage.get_ws_state("dca:buys_done") or 0))
        bot = DCABot(
            exchange, router,
            inst_id=inst_id,
            quote_per_buy_usdt=float(sleeve["quote_per_buy_usdt"]),
            interval_sec=interval_s,
            max_buys=done + 1 if once else UNLIMITED_BUYS,
            demo=False,
            storage=storage,
            max_total_usdt=float(sleeve["max_total_usdt"]),
            before_buy=perimeter.dca_gate(storage, sleeve),
            equity_fn=lambda: fetch_pocket_equity(exchange)[0],
            sleep=lambda seconds: wait_until(clock() + seconds, perimeter, sleep, clock),
            owner=order_owner.LIVE_DCA,
        )
        final = bot.run()
    except StopRunner as exc:
        log.warning("runner остановлен: %s", exc)
        return 3 if perimeter.killed else 0
    finally:
        router.close()
    log.info("рукав dca: %s, потрачено %.2f из %.2f USDT", final,
             dca_spent_usdt(storage, inst_id), float(sleeve["max_total_usdt"]))
    return 1 if final == DCABot.STATE_PAUSED else 0


def main(argv=None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    parser = argparse.ArgumentParser(prog="python -m src.live_runner", description=__doc__.splitlines()[0])
    parser.add_argument("--once", action="store_true", help="не больше одной покупки и выход")
    args = parser.parse_args(argv)
    return run(once=args.once)


if __name__ == "__main__":
    sys.exit(main())
