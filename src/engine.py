"""Торговый движок Фазы 1: WS (public + private) + reconciler + хранилище + риск.

Запуск: python -m src.engine
Kill-switch: создать файл data/KILL (текст внутри — причина) или
`python -m src.ops kill`. Движок отменит все ордера, включая algo, и
заблокирует входы до ручного сброса (`python -m src.ops reset kill`).
Нативные grid- и DCA-боты OKX тоже останавливаются (stopType 2: монеты и
позиции остаются на счёте, FLEET-KILL-DCA).
Штатная остановка: файл data/STOP_ENGINE (или `ops/engine.ps1 stop`). Текст
обоих флагов — источник остановки — пишется в лог до удаления флага.

Владение kill-switch (ENGINE-KILL-REG): движок подписывается через реестр
connector.register_kill_callback, а в слот risk ставит единый диспетчер
connector.kill_switch_dispatch — как OrderRouter. Порядок создания движка и
роутера в одном процессе больше не важен: при kill вызываются все подписчики.
stop() отписывает движок и закрывает хранилище (WAL-чекпойнт).

Спот-ордера (ENGINE-TDMODE): tdMode по режиму аккаунта (src/account_mode.py,
как в OrderRouter): acctLv 1–2 → cash, 3–4 → cross; режим перечитывается раз в
5 мин. Если заём возможен (autoLoan в режимах 3–4 или enableSpotBorrow), ордер
больше availBal отклоняется до биржи. Режим или баланс не прочитаны — отказ.

Сон Windows (ENGINE-KEEPAWAKE): пока движок работает, он держит запрос
SetThreadExecutionState(ES_CONTINUOUS | ES_SYSTEM_REQUIRED) — система не уходит
в сон, экран гаснуть может; stop() снимает запрос. Настройки питания не меняются.
Пауза процесса (ENGINE-PAUSE-DETECT): между сверками прошло больше двух
интервалов — WARNING «пауза процесса N с» и счётчик pauses в статистике.
Ошибки циклов (ENGINE-ERR-CLASS): errors_exchange — сбой на стороне биржи или
сети (OKX 50001/50004/50013/50026, HTTP 5xx, таймауты и обрывы соединения),
errors_internal — всё остальное, в том числе 50011; errors — их сумма.

Блокирующие вызовы CCXT выполняются в потоках (asyncio.to_thread): троттлер
CCXT спит через time.sleep и иначе останавливал бы event loop вместе с WS-пингами.
"""
import asyncio
import json
import logging
import re
import sys
import time
from pathlib import Path
from typing import Any, Optional

import ccxt

from . import risk
from .account_mode import (
    AccountMode,
    check_no_borrow,
    describe_mode,
    fetch_account_mode,
    is_spot,
    spot_order_params,
)
from .config import load_settings
from .connector import (
    check_time_sync,
    create_exchange,
    emergency_stop,
    exp_time_ms,
    kill_switch_dispatch,
    new_client_order_id,
    okx_call,
    register_kill_callback,
    unregister_kill_callback,
)
from .errors import extract_error_code
from .reconciler import Reconciler, order_record_from_okx, to_float, trade_record_from_ws_order
from .storage import EquityRecord, OrderRecord, Storage
from .ws_client import OKXWebSocket, WSConfig, WSCredentials, ws_urls

log = logging.getLogger("okx.engine")

KILL_FLAG = Path("data/KILL")
STOP_FLAG = Path("data/STOP_ENGINE")
ORDER_TTL_MS = 5000  # expTime: биржа отбросит place, если он дошёл позже
# Режим аккаунта перечитывается не чаще раза в 5 мин, как в OrderRouter:
# человек может сменить его на ходу, а account/config — 5 запросов / 2 с
ACCOUNT_MODE_TTL_S = 300.0
FLAG_TEXT_LIMIT = 500  # символов текста флага в логе и в причине kill

# --- Сон Windows (ENGINE-KEEPAWAKE) ---
# SetThreadExecutionState: флаги winbase.h. ES_DISPLAY_REQUIRED не ставим —
# экран может гаснуть. Запрос привязан к вызвавшему потоку и снимается при его
# завершении, поэтому ставится и снимается в потоке event loop (не to_thread).
ES_CONTINUOUS = 0x80000000
ES_SYSTEM_REQUIRED = 0x00000001


def _kernel32() -> Any:
    """kernel32 для SetThreadExecutionState; не Windows — None (тесты подменяют фейком)."""
    if sys.platform != "win32":
        return None
    import ctypes

    kernel32 = ctypes.windll.kernel32
    kernel32.SetThreadExecutionState.argtypes = [ctypes.c_uint32]
    kernel32.SetThreadExecutionState.restype = ctypes.c_uint32
    return kernel32


def set_thread_execution_state(flags: int) -> Optional[bool]:
    """SetThreadExecutionState(flags): None — не Windows, False — вызов не удался."""
    kernel32 = _kernel32()
    if kernel32 is None:
        return None
    return bool(kernel32.SetThreadExecutionState(flags))  # 0 — ошибка, иначе прежнее состояние


# --- Классы ошибок циклов движка (ENGINE-ERR-CLASS) ---
# Сторона биржи: OKX 50001 (техработы), 50013 (система занята) и те же классы
# CCXT — 50004 (таймаут эндпоинта, RequestTimeout) и 50026 (системная ошибка,
# ExchangeNotAvailable). 50011 (rate limit) — наша частота запросов: internal.
EXCHANGE_SIDE_CODES = frozenset({"50001", "50004", "50013", "50026"})
# Текст HTTP-ошибки CCXT: "okx GET https://… 503 Service Unavailable <тело>"
_HTTP_STATUS_RE = re.compile(r"^\S+ (?:GET|POST|PUT|DELETE|PATCH) \S+ (\d{3})\b")


def http_status(exc: BaseException) -> Optional[int]:
    """HTTP-статус ответа из исключения CCXT (текст или requests.HTTPError в __cause__)."""
    match = _HTTP_STATUS_RE.search(str(exc))
    if match:
        return int(match.group(1))
    status = getattr(getattr(exc.__cause__, "response", None), "status_code", None)
    return status if isinstance(status, int) else None


def classify_error(exc: BaseException) -> str:
    """'exchange' — сбой биржи или сети, 'internal' — всё остальное.

    Порядок: HTTP 5xx → код OKX (EXCHANGE_SIDE_CODES — exchange, любой другой —
    internal) → HTTP 4xx → класс: таймауты и обрывы соединения CCXT и Python —
    exchange, кроме rate limit и nonce (наша сторона). Неизвестное — internal:
    лишняя тревога лучше пропущенной ошибки кода.
    """
    status = http_status(exc)
    if status is not None and 500 <= status <= 599:
        return "exchange"
    code = extract_error_code(exc) if isinstance(exc, ccxt.BaseError) else None
    if code in EXCHANGE_SIDE_CODES:
        return "exchange"
    if code or status is not None:
        return "internal"
    if isinstance(exc, (ccxt.RateLimitExceeded, ccxt.DDoSProtection, ccxt.InvalidNonce)):
        return "internal"
    if isinstance(exc, (ccxt.NetworkError, TimeoutError, ConnectionError)):
        return "exchange"
    return "internal"


def read_flag_text(path: Path) -> str:
    """Текст файла-флага для лога; не бросает исключений.

    Флаг пишут человек и скрипты PowerShell: Set-Content в Windows PowerShell 5.1
    пишет в ANSI (cp1251), `>` и Out-File — в UTF-16 с BOM. Раньше read_text(utf-8)
    на таком флаге бросал UnicodeDecodeError: цикл флагов падал, движок выходил
    без kill-switch, а data/KILL оставался. Теперь: BOM UTF-16 → UTF-16, иначе
    UTF-8 (с BOM или без), не UTF-8 → cp1251. Флаг не прочитан (исчез, занят) —
    описание ошибки вместо текста: флаг всё равно обрабатывается.
    """
    try:
        raw = path.read_bytes()
    except OSError as exc:
        return f"<не прочитан: {type(exc).__name__}: {exc}>"
    if raw.startswith((b"\xff\xfe", b"\xfe\xff")):
        text = raw.decode("utf-16", errors="replace")
    else:
        try:
            text = raw.decode("utf-8-sig")
        except UnicodeDecodeError:
            text = raw.decode("cp1251", errors="replace")
    text = " ".join(text.split())  # одна строка лога
    return text if len(text) <= FLAG_TEXT_LIMIT else text[:FLAG_TEXT_LIMIT] + "…"


class RiskRejected(Exception):
    """Ордер отклонён до биржи: риск-ядро или режим аккаунта (tdMode, скрытый заём)."""


class TradingEngine:
    def __init__(self, inst_ids: tuple[str, ...] = ("BTC-USDT",), inst_type: str = "SPOT"):
        self.settings = load_settings()
        self.ex = create_exchange(self.settings)
        self.db = Storage()
        self.inst_ids = tuple(inst_ids)
        self.inst_type = inst_type
        self.reconciler = Reconciler(self.ex, self.db, inst_type=inst_type)
        # Kill-switch (ENGINE-KILL-REG): подписка через реестр connector, в слот
        # risk — единый диспетчер (как OrderRouter). Прямая регистрация
        # колбэка движка перезаписывала бы подписку роутера в том же процессе.
        register_kill_callback(self._cancel_all_for_kill_switch)
        risk.set_order_canceller(kill_switch_dispatch)
        # Режим аккаунта для tdMode спота (ENGINE-TDMODE), кэш ACCOUNT_MODE_TTL_S
        self._account_mode: Optional[AccountMode] = None
        self._account_mode_at = 0.0

        urls = ws_urls(self.settings.domain, self.settings.is_demo)
        self.ws_public = OKXWebSocket(WSConfig(url=urls.public), on_message=self._on_public_message)
        self.ws_private = OKXWebSocket(
            WSConfig(url=urls.private),
            on_message=self._on_private_message,
            credentials=WSCredentials(
                self.settings.api_key, self.settings.secret, self.settings.passphrase
            ),
        )
        # Клиент business-эндпоинта создаётся лениво: каналы candle* на public
        # отклоняются ошибкой 60018 (insights/okx-api.md §4)
        self.ws_business: Optional[OKXWebSocket] = None

        self._running = False
        self._reconcile_interval = 60.0  # сек; заодно продлевает жизнь live-ключа (14 дней)
        self._equity_interval = 300.0
        self._tickers: dict[str, dict] = {}
        # errors = errors_exchange + errors_internal (поле сохранено для совместимости)
        self.stats = {"started_at": None, "reconciles": 0, "divergences": 0, "errors": 0,
                      "errors_exchange": 0, "errors_internal": 0, "pauses": 0}
        # Часы детектора пауз — настенные: они идут и во сне машины, а
        # монотонные на Windows сон могут не учитывать (asyncio.sleep тоже)
        self._clock = time.time
        self._keep_awake = False

    # --- Жизненный цикл ---

    async def start(self) -> None:
        self.stats["started_at"] = time.time()
        log.info("Starting engine (mode=%s, domain=%s, inst=%s)",
                 self.settings.mode, self.settings.domain, ",".join(self.inst_ids))
        self._acquire_keep_awake()

        drift =await asyncio.to_thread(check_time_sync, self.ex)
        log.info("Time drift: %+d ms", drift)
        await asyncio.to_thread(self.ex.load_markets)
        await self._log_account_mode()

        await self._reconcile()
        await self._update_equity()

        await self.ws_public.connect()
        for inst_id in self.inst_ids:
            await self.subscribe_market_data("books", inst_id)
            await self.subscribe_market_data("tickers", inst_id)
        await self.ws_private.connect()
        await self.ws_private.subscribe({"channel": "orders", "instType": self.inst_type})

        self._running = True
        log.info("Engine started")
        tasks = [asyncio.create_task(c) for c in (self._reconcile_loop(), self._equity_loop(), self._flag_loop())]
        try:
            # выход — по флагу STOP_ENGINE или если какой-то цикл упал непойманным исключением
            done, _ = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
            for t in done:
                if not t.cancelled() and t.exception():
                    raise t.exception()
        finally:
            for t in tasks:
                t.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)

    async def stop(self) -> None:
        self._running = False
        try:
            await self.ws_public.close()
            if self.ws_business is not None:
                await self.ws_business.close()
            await self.ws_private.close()
            self._save_stats()
        finally:
            # Даже если закрытие WS упало: отписка от kill-switch (симметрично
            # OrderRouter.close) и WAL-чекпойнт хранилища перед выходом
            # (ENGINE-STORAGE-CLOSE; Storage.close идемпотентен)
            unregister_kill_callback(self._cancel_all_for_kill_switch)
            self._release_keep_awake()  # не бросает исключений
            self.db.close()
        log.info("Engine stopped. Stats: %s", self.stats)

    def _acquire_keep_awake(self) -> None:
        """Windows: запрет сна системы на время работы движка (ENGINE-KEEPAWAKE)."""
        try:
            ok = set_thread_execution_state(ES_CONTINUOUS | ES_SYSTEM_REQUIRED)
        except Exception as exc:  # сон — не повод не запускать сверку и kill-switch
            log.warning("Запрет сна Windows не включён: %s", exc)
            return
        if ok is None:
            return  # не Windows — ничего не делаем
        if ok:
            self._keep_awake = True
            log.info("Запрет сна Windows включён: SetThreadExecutionState(ES_CONTINUOUS | "
                     "ES_SYSTEM_REQUIRED), экран может гаснуть; снимается при остановке движка")
        else:
            log.warning("Запрет сна Windows не включён: SetThreadExecutionState вернул 0 — "
                        "машина может уснуть, движок встанет")

    def _release_keep_awake(self) -> None:
        """Снять запрет сна: SetThreadExecutionState(ES_CONTINUOUS). Только если ставили."""
        if not self._keep_awake:
            return
        try:
            ok = set_thread_execution_state(ES_CONTINUOUS)
        except Exception as exc:
            log.warning("Запрет сна Windows не снят: %s (снимется при выходе процесса)", exc)
            return
        self._keep_awake = False
        if ok:
            log.info("Запрет сна Windows снят: SetThreadExecutionState(ES_CONTINUOUS)")
        else:
            log.warning("Запрет сна Windows не снят: SetThreadExecutionState вернул 0 "
                        "(снимется при выходе процесса)")

    async def subscribe_market_data(self, channel: str, inst_id: str) -> None:
        """Подписка на публичный канал маркет-данных.

        Каналы candle* идут через ws_urls(...).business — на public-эндпоинте
        OKX отклоняет их ошибкой 60018; остальное — на public.
        """
        args = {"channel": channel, "instId": inst_id}
        if channel.startswith("candle"):
            if self.ws_business is None:
                urls = ws_urls(self.settings.domain, self.settings.is_demo)
                self.ws_business = OKXWebSocket(
                    WSConfig(url=urls.business), on_message=self._on_public_message
                )
                await self.ws_business.connect()
            await self.ws_business.subscribe(args)
        else:
            await self.ws_public.subscribe(args)

    # --- WS-обработчики ---

    def _on_public_message(self, data: dict) -> None:
        arg = data.get("arg", {})
        if arg.get("channel") == "tickers":
            for t in data["data"]:
                self._tickers[t["instId"]] = t

    def _on_private_message(self, data: dict) -> None:
        if data.get("arg", {}).get("channel") != "orders":
            return
        for o in data["data"]:
            record = order_record_from_okx(o)
            self.db.upsert_order(record)
            trade = trade_record_from_ws_order(o)
            if trade:
                self.db.insert_trade(trade)
            log.info("Order %s %s %s: %s (filled %s/%s)",
                     record.ord_id, record.side, record.inst_id, record.state,
                     record.filled_sz, record.sz)

    # --- Периодические задачи ---

    async def _reconcile_loop(self) -> None:
        last = self._clock()
        while self._running:
            await asyncio.sleep(self._reconcile_interval)
            now = self._clock()
            self._check_pause(now - last)
            last = now
            try:
                await self._reconcile()
            except Exception as e:
                kind = self._count_error(e)
                log.error("Reconcile error (%s): %s", kind, e)

    def _check_pause(self, gap: float) -> None:
        """ENGINE-PAUSE-DETECT: между сверками больше двух интервалов — процесс стоял."""
        if gap > 2 * self._reconcile_interval:
            self.stats["pauses"] += 1
            log.warning("пауза процесса %.0f с: между сверками больше двух интервалов (%.0f с) — "
                        "сон машины или зависание процесса; пауз всего %d",
                        gap, 2 * self._reconcile_interval, self.stats["pauses"])

    def _count_error(self, exc: BaseException) -> str:
        """ENGINE-ERR-CLASS: учесть ошибку цикла в errors_exchange или errors_internal."""
        kind = classify_error(exc)
        self.stats[f"errors_{kind}"] += 1
        self.stats["errors"] = self.stats["errors_exchange"] + self.stats["errors_internal"]
        return kind

    async def _reconcile(self) -> None:
        """Сверка состояния с биржей; расхождения = WS что-то пропустил."""
        divergences = 0
        for inst_id in self.inst_ids:
            orders = await asyncio.to_thread(self.reconciler.sync_orders, inst_id)
            trades = await asyncio.to_thread(self.reconciler.sync_trades, inst_id)
            divergences += orders["missing_on_exchange"] + orders["missing_locally"] + orders["changed"]
            log.info("Reconcile %s: orders=%s trades=%s", inst_id, orders, trades)
        if self.inst_type != "SPOT":
            positions = await asyncio.to_thread(self.reconciler.sync_positions)
            log.info("Positions: %s", positions)

        self.stats["reconciles"] += 1
        if divergences and self._running:  # стартовая сверка — не рассинхрон
            self.stats["divergences"] += divergences
            log.warning("РАССИНХРОН WS/REST: %d (всего %d)", divergences, self.stats["divergences"])
        self._save_stats()

    def _save_stats(self) -> None:
        self.db.set_ws_state("engine_stats", {
            **self.stats,
            "ws_public_reconnects": self.ws_public.reconnects,
            "ws_business_reconnects": self.ws_business.reconnects if self.ws_business else 0,
            "ws_private_reconnects": self.ws_private.reconnects,
            "saved_at": time.time(),
        })

    async def _equity_loop(self) -> None:
        while self._running:
            await asyncio.sleep(self._equity_interval)
            try:
                await self._update_equity()
            except Exception as e:
                kind = self._count_error(e)
                log.error("Equity error (%s): %s", kind, e)

    async def _update_equity(self) -> None:
        """Equity = totalEq аккаунта (USD), а не только USDT-баланс."""
        resp = await asyncio.to_thread(self.ex.private_get_account_balance)
        account = (resp.get("data") or [{}])[0]
        total = to_float(account.get("totalEq"))
        usdt = next((d for d in account.get("details", []) if d.get("ccy") == "USDT"), {})
        self.db.record_equity(EquityRecord(
            ts=time.time(),
            total_eq=total,
            avail_eq=to_float(usdt.get("availBal")),
            upl=to_float(account.get("upl")),
        ))
        events = risk.update_equity(total)
        if events:
            log.critical("RISK BREAKER по equity %.2f: %s — новые входы заблокированы", total, events)

    async def _flag_loop(self) -> None:
        """Файлы-флаги: data/KILL — аварийная остановка торговли, data/STOP_ENGINE — штатный выход."""
        while self._running:
            if KILL_FLAG.exists():
                # текст флага — в лог ДО unlink: после удаления источник не восстановить
                reason = read_flag_text(KILL_FLAG) or "file flag"
                log.critical("KILL-флаг %s: %s", KILL_FLAG, reason)
                KILL_FLAG.unlink(missing_ok=True)
                report = await asyncio.to_thread(risk.kill_switch, False, f"flag: {reason}")
                log.critical("KILL-SWITCH: отменено %d, не отменено %d",
                             len(report["cancelled"]), len(report["failed"]))
            if STOP_FLAG.exists():
                # ENGINE-FLAG-LOG: кто и когда остановил (ops/engine.ps1 stop пишет
                # «ops/engine.ps1 stop <время>»); пустой флаг — источник не указан
                source = read_flag_text(STOP_FLAG) or "флаг пустой, источник не указан"
                log.info("STOP_ENGINE: штатная остановка по флагу %s: %s", STOP_FLAG, source)
                STOP_FLAG.unlink(missing_ok=True)
                return
            await asyncio.sleep(2)

    def _cancel_all_for_kill_switch(self, flatten: bool) -> dict:
        """Подписчик реестра kill-switch (вызывает connector.kill_switch_dispatch).

        connector.emergency_stop: отмена обычных и algo-ордеров, остановка grid-
        и DCA-ботов. Локальные ордера в storage поправит реконсиляция.
        """
        if flatten:
            log.error("flatten не реализован: позиции закрываются по стопам или вручную")
        return emergency_stop(self.ex)

    # --- Режим аккаунта (ENGINE-TDMODE) ---

    async def _get_account_mode(self) -> AccountMode:
        """Режим аккаунта: account/config, кэш ACCOUNT_MODE_TTL_S (как OrderRouter.account_mode)."""
        now = time.monotonic()
        if self._account_mode is None or now - self._account_mode_at > ACCOUNT_MODE_TTL_S:
            mode = await asyncio.to_thread(okx_call, fetch_account_mode, self.ex)
            if self._account_mode is not None and mode != self._account_mode:
                log.warning("режим аккаунта сменился: %s -> %s", self._account_mode, mode)
            self._account_mode, self._account_mode_at = mode, now
        return self._account_mode

    async def _log_account_mode(self) -> None:
        """При старте: режим аккаунта в лог. Сбой чтения — предупреждение, не остановка:
        сверке и equity режим не нужен, а спот-ордера без него отклоняются."""
        try:
            mode = await self._get_account_mode()
        except Exception as exc:
            log.warning("Режим аккаунта не прочитан (%s) — спот-ордера будут отклоняться, "
                        "пока account/config не прочитается", exc)
            return
        log.info("Режим аккаунта: %s", describe_mode(mode))

    async def _spot_params(self, inst_id: str, side: str, ord_type: str, sz: float,
                           px: Optional[float]) -> dict:
        """tdMode (и tgtCcy/cost рыночной покупки в режиме cross) для спот-ордера.

        Порядок и тексты отказов — как OrderRouter._spot_params: режим аккаунта →
        запрет скрытого займа (check_no_borrow) → spot_order_params. Любой отказ —
        RiskRejected до обращения к create_order. Не спот — {} (деривативы: tdMode
        задаёт вызывающий, как раньше).
        """
        if not is_spot(inst_id):
            return {}
        try:
            mode = await self._get_account_mode()
        except Exception as exc:  # без режима tdMode не выбрать — ордер не ставим
            raise RiskRejected(f"режим аккаунта не прочитан ({exc}) — tdMode спота не выбрать") from exc
        try:
            ok, reason = await asyncio.to_thread(
                check_no_borrow, self.ex, mode, inst_id, side, ord_type, sz, px)
        except Exception as exc:
            raise RiskRejected(f"availBal не прочитан ({exc}) — заём не исключить") from exc
        if not ok:
            raise RiskRejected(reason)
        try:
            return spot_order_params(mode, ord_type, side, sz, px)
        except ValueError as exc:  # рыночная покупка в режиме cross без цены
            raise RiskRejected(str(exc)) from exc

    # --- Ордера ---

    async def place_order(
        self,
        inst_id: str,
        side: str,
        ord_type: str,
        sz: float,
        px: Optional[float] = None,
        *,
        entry: Optional[bool] = None,
    ) -> OrderRecord:
        """Ставит ордер: риск-проверка входа → режим аккаунта (спот) → clOrdId + expTime → запись в БД.

        entry — открытие/наращивание позиции (проверяется риск-ядром). По
        умолчанию для спота buy = вход, sell = выход: выходы breaker'ами не
        блокируются, иначе при срабатывании защиты нельзя было бы закрыться.
        Учёт открытой позиции (risk.register_entry) — на стороне стратегии,
        после фактического исполнения.

        Спот (и вход, и выход): tdMode по режиму аккаунта и запрет скрытого
        займа (ENGINE-TDMODE, _spot_params). Рыночная покупка в режиме cross
        требует px: по нему считается сумма в котируемой валюте (tgtCcy=quote_ccy).
        """
        if ord_type not in ("limit", "market", "post_only"):
            raise ValueError(f"Unsupported ord_type: {ord_type}")
        if ord_type != "market" and not px:
            raise ValueError("px обязателен для limit/post_only")

        if entry is None:
            entry = side == "buy"
        if entry:
            allowed, reason = risk.check_entry_allowed(inst_id, side)
            if not allowed:
                raise RiskRejected(reason)
            await self._check_notional(inst_id, sz, px)
        mode_params = await self._spot_params(inst_id, side, ord_type, sz, px)

        cl_ord_id = new_client_order_id()
        params = {**mode_params, "clOrdId": cl_ord_id, "expTime": exp_time_ms(ORDER_TTL_MS)}
        ccxt_type = "limit" if ord_type == "post_only" else ord_type
        if ord_type == "post_only":
            params["postOnly"] = True

        try:
            order = await asyncio.to_thread(
                self.ex.create_order, inst_id, ccxt_type, side, sz, px, params
            )
            ord_id, raw = order["id"], order.get("info", {})
        except ccxt.NetworkError:
            # ответ потерян, но ордер мог встать; после expTime исход уже окончательный
            await asyncio.sleep(ORDER_TTL_MS / 1000)
            raw = await asyncio.to_thread(self._find_order_by_cl_id, inst_id, cl_ord_id)
            if raw is None:
                raise
            ord_id = raw["ordId"]
            log.warning("Ответ place потерян, ордер найден по clOrdId %s → %s", cl_ord_id, ord_id)

        now = time.time()
        record = OrderRecord(
            ord_id=ord_id, inst_id=inst_id, side=side, ord_type=ord_type, px=px, sz=sz,
            state="live", filled_sz=0.0, avg_px=0.0, fee=0.0, create_time=now,
            update_time=now, raw_json=json.dumps(raw, ensure_ascii=False, default=str),
            cl_ord_id=cl_ord_id,
        )
        # если WS уже прислал fill, upsert не откатит состояние (см. storage.py)
        self.db.upsert_order(record)
        log.info("Order placed: %s %s %s @ %s (ordId=%s)", side, sz, inst_id, px, ord_id)
        return record

    def _find_order_by_cl_id(self, inst_id: str, cl_ord_id: str) -> Optional[dict]:
        try:
            data = self.ex.private_get_trade_order({"instId": inst_id, "clOrdId": cl_ord_id}).get("data")
        except ccxt.OrderNotFound:
            return None
        return data[0] if data else None

    async def _check_notional(self, inst_id: str, sz: float, px: Optional[float]) -> None:
        """Последний рубеж от ошибок единиц (rookie-airbag): notional ≤ потолка позиции."""
        equity = risk.status()["equity"]
        if equity <= 0:
            raise RiskRejected("equity неизвестен — вход запрещён до первого обновления баланса")
        price = px or to_float(self._tickers.get(inst_id, {}).get("askPx"), None)
        if price is None:
            ticker = await asyncio.to_thread(self.ex.fetch_ticker, inst_id)
            price = ticker["ask"] or ticker["last"]
        notional = sz * price
        limit = equity * risk.MAX_POSITION_PCT / 100
        if notional > limit:
            raise RiskRejected(
                f"notional {notional:.2f} > {risk.MAX_POSITION_PCT}% equity ({limit:.2f})"
            )


async def main() -> None:
    engine = TradingEngine()
    try:
        await engine.start()
    finally:
        await engine.stop()


if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        log.info("Остановлено пользователем")
