"""Коннектор OKX через CCXT.

Практики из insights/okx-api.md:
- set_sandbox_mode(True) для демо (заголовок x-simulated-trading: 1)
- passphrase передаётся как password
- enableRateLimit=True — встроенный throttler CCXT (~110 мс)
- синхронизация времени через fetch_time() перед торговлей
- expTime — HTTP-ЗАГОЛОВОК запроса (дедлайн, unix ms). CCXT кладёт params в
  тело, где биржа его игнорирует, поэтому OKXExchange переносит его в заголовок
- ответ batch-операций проверяется по sCode/sMsg внутри data[] (code=2 — частичный успех)
"""
import functools
import json
import logging
import threading
import time
import uuid
from typing import Any, Callable, Collection, Optional

import ccxt

from .config import Settings
from .errors import explain_error, extract_error_code

log = logging.getLogger("okx.connector")

# orders-algo-pending принимает один ordType за запрос (исключение — пара conditional,oco)
ALGO_ORD_TYPES = ("conditional,oco", "trigger", "move_order_stop", "iceberg", "twap", "chase")
PAGE_LIMIT = 100  # максимум OKX для orders-pending / orders-algo-pending
CANCEL_BATCH = 20  # максимум OKX для cancel-batch-orders
CANCEL_ALGO_BATCH = 10  # максимум OKX для cancel-algos


class OKXExchange(ccxt.okx):
    """ccxt.okx, который отправляет expTime заголовком, а не в теле запроса."""

    def sign(self, path, api="public", method="GET", params={}, headers=None, body=None):
        exp_time = None
        if isinstance(params, dict) and "expTime" in params:
            params = dict(params)
            exp_time = params.pop("expTime")
        elif isinstance(params, list):
            # create_order в CCXT идёт через trade/batch-orders: params — список ордеров,
            # а заголовок один на запрос — берём самый ранний дедлайн
            stripped = []
            for entry in params:
                if isinstance(entry, dict) and "expTime" in entry:
                    entry = dict(entry)
                    value = entry.pop("expTime")
                    exp_time = value if exp_time is None else min(exp_time, value, key=int)
                stripped.append(entry)
            params = stripped
        request = super().sign(path, api, method, params, headers, body)
        if exp_time is not None:
            request["headers"] = {**(request.get("headers") or {}), "expTime": str(exp_time)}
        return request


def create_exchange(settings: Settings) -> OKXExchange:
    exchange = OKXExchange(
        {
            "apiKey": settings.api_key,
            "secret": settings.secret,
            "password": settings.passphrase,  # OKX passphrase = password в CCXT
            "hostname": settings.domain,  # регион: ключ другого региона даёт 50119
            "enableRateLimit": True,
            "options": {"defaultType": "spot"},
        }
    )
    if settings.is_demo:
        exchange.set_sandbox_mode(True)
    return exchange


def new_client_order_id(prefix: str = "bot") -> str:
    """clOrdId: до 32 символов, только буквы и цифры (требование OKX).

    Свой clOrdId позволяет найти ордер, если ответ на place потерялся (таймаут).
    """
    return (prefix + uuid.uuid4().hex)[:32]


def exp_time_ms(ttl_ms: int = 5000) -> str:
    """Дедлайн запроса для заголовка expTime: после него биржа отбросит place/amend."""
    return str(int(time.time() * 1000) + ttl_ms)


def okx_call(func: Callable[..., Any], *args: Any, **kwargs: Any) -> Any:
    """Обёртка вызова CCXT: логирует ошибку OKX через карту кодов и перевыбрасывает.

    Поведение не меняется: исключение всегда пробрасывается вызывающему,
    добавляется только распознавание sCode и русскоязычное пояснение.
    """
    try:
        return func(*args, **kwargs)
    except ccxt.ExchangeError as exc:
        code = extract_error_code(exc)
        log.error("%s", explain_error(code))
        raise


def check_time_sync(exchange: ccxt.okx, max_drift_ms: int = 5000) -> int:
    """Дрейф локальных часов относительно сервера OKX, мс (с поправкой на задержку сети).

    >30 с дрейфа — отказы API (50102/50112). Предупреждать заранее.
    """
    sent = exchange.milliseconds()
    server_ts = exchange.fetch_time()
    received = exchange.milliseconds()
    drift = (sent + received) // 2 - server_ts
    if abs(drift) > max_drift_ms:
        raise RuntimeError(
            f"Дрейф часов {drift} мс — синхронизируйте NTP перед торговлей"
        )
    return drift


def _paginate(fetch: Callable[[dict], dict], params: dict, id_key: str) -> list[dict]:
    """Листает orders-pending / orders-algo-pending курсором after (от новых к старым)."""
    result: list[dict] = []
    after: Optional[str] = None
    while True:
        page_params = dict(params, limit=str(PAGE_LIMIT))
        if after:
            page_params["after"] = after
        page = fetch(page_params).get("data") or []
        result.extend(page)
        if len(page) < PAGE_LIMIT:
            return result
        after = page[-1][id_key]


def fetch_pending_orders(exchange, inst_id: Optional[str] = None) -> list[dict]:
    """Все активные обычные ордера — сырые dict OKX (ordId, instId, state, accFillSz, ...)."""
    params = {"instId": inst_id} if inst_id else {}
    return _paginate(exchange.private_get_trade_orders_pending, params, "ordId")


def fetch_pending_algos(exchange, inst_id: Optional[str] = None) -> list[dict]:
    """Все активные algo-ордера: стопы, тейки, трейлинги, триггеры, iceberg, TWAP."""
    result: list[dict] = []
    for ord_type in ALGO_ORD_TYPES:
        params = {"ordType": ord_type}
        if inst_id:
            params["instId"] = inst_id
        try:
            result.extend(_paginate(exchange.private_get_trade_orders_algo_pending, params, "algoId"))
        except ccxt.BadRequest as exc:
            # тип может быть недоступен для аккаунта/региона — не повод прерывать kill-switch
            log.warning("orders-algo-pending ordType=%s: %s", ord_type, explain_error(extract_error_code(exc)))
    return result


def _chunks(items: list, size: int):
    for i in range(0, len(items), size):
        yield items[i:i + size]


def _collect_failures(response: dict, id_key: str, errors: list[dict]) -> None:
    for item in response.get("data") or []:
        if item.get("sCode") not in (None, "0"):
            errors.append({"id": item.get(id_key), "sCode": item.get("sCode"), "sMsg": item.get("sMsg")})


def cancel_all_orders(
    exchange,
    inst_id: Optional[str] = None,
    attempts: int = 3,
    backoff_s: float = 1.0,
    sleep: Callable[[float], None] = time.sleep,
) -> dict:
    """Отменяет ВСЕ активные ордера, включая algo — для kill-switch (risk-core.md §4).

    Без отмены algo остаточный стоп «выстрелит» в следующей сессии.
    Итог определяется повторным запросом: что осталось висеть — то не отменилось.
    Идемпотентно: нет ордеров → пустой отчёт. Ордера нативных grid/DCA-ботов
    OKX (tradingBot) сюда не входят — их останавливают через bot API.

    Возвращает {"cancelled": [ids], "failed": [ids], "errors": [...]}.
    """
    initial: set[str] = set()
    errors: list[dict] = []
    orders: list[dict] = []
    algos: list[dict] = []

    for attempt in range(attempts + 1):
        if attempt:
            sleep(backoff_s * 2 ** (attempt - 1))
        orders = fetch_pending_orders(exchange, inst_id)
        algos = fetch_pending_algos(exchange, inst_id)
        initial.update(o["ordId"] for o in orders)
        initial.update(a["algoId"] for a in algos)
        if not (orders or algos) or attempt == attempts:
            break  # всё отменено, либо попытки кончились (последний проход — только проверка)

        for chunk in _chunks(orders, CANCEL_BATCH):
            try:
                resp = exchange.private_post_trade_cancel_batch_orders(
                    [{"instId": o["instId"], "ordId": o["ordId"]} for o in chunk]
                )
                _collect_failures(resp, "ordId", errors)
            except ccxt.BaseError as exc:
                errors.append({"ids": [o["ordId"] for o in chunk], "error": str(exc)})

        for chunk in _chunks(algos, CANCEL_ALGO_BATCH):
            try:
                resp = exchange.private_post_trade_cancel_algos(
                    [{"instId": a["instId"], "algoId": a["algoId"]} for a in chunk]
                )
                _collect_failures(resp, "algoId", errors)
            except ccxt.BaseError as exc:
                errors.append({"ids": [a["algoId"] for a in chunk], "error": str(exc)})

    remaining = {o["ordId"] for o in orders} | {a["algoId"] for a in algos}
    if remaining:
        log.error("Не удалось отменить %d ордеров: %s", len(remaining), sorted(remaining))
    return {"cancelled": sorted(initial - remaining), "failed": sorted(remaining), "errors": errors}


# --- Нативные боты OKX (tradingBot): остановка для kill-switch ---
#
# Ордера grid- и DCA-ботов не видны в orders-pending — cancel_all_orders их не
# отменит, ботов останавливают через bot API (FLEET-KILL-DCA, FLEET-KILL-GRID-HARDEN).
# Общий порядок: список по курсору after (с повтором транзиентных ошибок) →
# фильтр algo_ids → stop пачками → итог по каждому боту по sCode. Список не
# получен — описание в failed: risk.kill_switch читает только cancelled/failed,
# и неостановленные боты не должны выглядеть «успехом».

GRID_BOT_TYPES = ("grid", "contract_grid")  # moon_grid: demo → 51000 «Parameter algoOrdType error»
GRID_API = ("private_get_tradingbot_grid_orders_algo_pending", "private_post_tradingbot_grid_stop_order_algo")
BOT_STOP_BATCH = 10  # максимум OKX для tradingBot/grid/stop-order-algo
DCA_BOT_TYPES = ("spot_dca", "contract_dca")
DCA_API = ("private_get_tradingbot_dca_ongoing_list", "private_post_tradingbot_dca_stop")
# sCode «бот уже остановлен» — не провал kill: 51291 «The bot doesn't exist or has already stopped»
BOT_ALREADY_STOPPED = frozenset({"51291"})
# sCode транзиентных отказов — повтор: 50011 rate limit, 50001 сервис недоступен, 50013 система занята
BOT_STOP_TRANSIENT = frozenset({"50011", "50001", "50013"})


def fetch_grid_bots(exchange, algo_ord_type: str) -> list[dict]:
    """Активные grid-боты одного типа: GET tradingBot/grid/orders-algo-pending (курсор after = algoId).

    Проверено на demo (FLEET-KILL-GRID-HARDEN, 24.09): один algoOrdType за запрос,
    limit ≤ 100 (101 → 51000 «Parameter limit error»), боты от новых к старым.
    """
    return _paginate(exchange.private_get_tradingbot_grid_orders_algo_pending,
                     {"algoOrdType": algo_ord_type}, "algoId")


def fetch_dca_bots(exchange, algo_ord_type: str) -> list[dict]:
    """Активные DCA-боты одного типа: GET tradingBot/dca/ongoing-list (курсор after = algoId).

    Проверено на demo (FLEET-KILL-DCA, 24.09): один algoOrdType за запрос,
    limit ≤ 100 (101 → 51000 «Parameter limit error»), боты от новых к старым.
    Остановленный бот пропадает из списка сразу после stop. При acctLv=1
    contract_dca отвечает code 0 и пустым списком.
    """
    return _paginate(exchange.private_get_tradingbot_dca_ongoing_list,
                     {"algoOrdType": algo_ord_type}, "algoId")


def _with_retry(call: Callable[[], Any], attempts: int, backoff_s: float,
                sleep: Callable[[float], None]) -> Any:
    """call() с повтором только транзиентных ошибок: ccxt.NetworkError — сеть,
    таймаут, 50011 (rate limit), 50001/50013 (сервис недоступен/занят)."""
    for attempt in range(attempts + 1):
        if attempt:
            sleep(backoff_s * 2 ** (attempt - 1))
        try:
            return call()
        except ccxt.NetworkError:
            if attempt == attempts:
                raise


def _error_items(exc: BaseException) -> list[dict]:
    """data[] ответа OKX из текста исключения CCXT («okx {json}») — sCode по каждому боту.

    CCXT бросает исключение на code 1 (все элементы запроса отклонены) и
    возвращает ответ на code 2 (частичный успех).
    """
    for arg in exc.args:
        if not isinstance(arg, str) or "{" not in arg:
            continue
        try:
            payload = json.loads(arg[arg.find("{"):])
        except ValueError:
            continue
        data = payload.get("data") if isinstance(payload, dict) else None
        if isinstance(data, list):
            return [item for item in data if isinstance(item, dict)]
    return []


def _list_bots(exchange, family: str, bot_types: tuple[str, ...], fetch: Callable[[Any, str], list[dict]],
               wanted: Optional[set], stop_type: str, attempts: int, backoff_s: float,
               sleep: Callable[[float], None], report: dict) -> list[tuple[str, str, Any]]:
    """Боты к остановке — [(algoId, algoOrdType, instId)]; уже останавливаемые — в skipped."""
    targets: list[tuple[str, str, Any]] = []
    for algo_ord_type in bot_types:
        try:
            bots = _with_retry(functools.partial(fetch, exchange, algo_ord_type), attempts, backoff_s, sleep)
        except Exception as exc:  # не глушим: список не получен — боты этого типа НЕ остановлены
            log.error("%s: список algoOrdType=%s не получен: %s", family, algo_ord_type, exc)
            report["failed"].append(f"{family}: список {algo_ord_type} не получен: {exc}")
            report["errors"].append({"algoOrdType": algo_ord_type, "error": str(exc)})
            continue
        for bot in bots:
            algo_id = bot.get("algoId")
            if not algo_id:  # непредвиденный ответ: такого бота не остановить — эскалация
                report["failed"].append(f"{family}: бот без algoId в списке {algo_ord_type}")
                report["errors"].append({"algoOrdType": algo_ord_type, "error": f"нет algoId: {bot}"})
                continue
            if wanted is not None and algo_id not in wanted:
                continue
            state = bot.get("state")
            if state == "stopping" or (state == "no_close_position" and stop_type == "2"):
                log.info("%s-бот %s (%s) уже в state=%s — stop не нужен", family, algo_id, algo_ord_type, state)
                report["skipped"].append(algo_id)
                continue
            targets.append((algo_id, algo_ord_type, bot.get("instId")))
    return targets


def _stop_bots(targets: list[tuple[str, str, Any]], send: Callable[[list], dict], chunk_size: int,
               family: str, attempts: int, backoff_s: float, sleep: Callable[[float], None],
               report: dict) -> None:
    """Останавливает targets пачками по chunk_size: send(chunk) → ответ OKX с data[].

    Итог по каждому боту — по sCode из ответа или из текста исключения CCXT:
    "0" → stopped; BOT_ALREADY_STOPPED → skipped; BOT_STOP_TRANSIENT или
    ccxt.NetworkError → повтор следующим проходом (backoff 1, 2, 4… с, до attempts
    раз); прочее → failed; бот без записи в ответе → failed (остановка не
    подтверждена). Сначала проход по всем ботам, повторы — после.
    """
    last_error: dict[str, dict] = {}
    for attempt in range(attempts + 1):
        if attempt:
            sleep(backoff_s * 2 ** (attempt - 1))
        retry: list[tuple[str, str, Any]] = []
        for chunk in _chunks(targets, chunk_size):
            exc_text: Optional[str] = None
            try:
                items = send(chunk).get("data") or []
            except ccxt.NetworkError as exc:  # транзиентная: повтор всей пачки
                for algo_id, algo_ord_type, _ in chunk:
                    last_error[algo_id] = {"id": algo_id, "algoOrdType": algo_ord_type, "error": str(exc)}
                retry.extend(chunk)
                continue
            except Exception as exc:  # отказ биржи: sCode по ботам — из текста исключения
                exc_text = str(exc)
                items = _error_items(exc)
                code = None if items else extract_error_code(exc)
                if code:  # ответ без data[]: общий код ошибки на всю пачку
                    items = [{"algoId": t[0], "sCode": code, "sMsg": exc_text} for t in chunk]
            by_id = {str(item.get("algoId")): item for item in items}
            for target in chunk:
                algo_id, algo_ord_type, _ = target
                item = by_id.get(algo_id)
                code = None if item is None or item.get("sCode") is None else str(item["sCode"])
                if item is not None and code in (None, "0"):
                    report["stopped"].append(algo_id)
                elif code in BOT_ALREADY_STOPPED:
                    log.info("%s-бот %s (%s) уже остановлен (%s)", family, algo_id, algo_ord_type, code)
                    report["skipped"].append(algo_id)
                elif code in BOT_STOP_TRANSIENT:
                    last_error[algo_id] = {"id": algo_id, "sCode": code, "sMsg": item.get("sMsg")}
                    retry.append(target)
                elif item is not None:
                    report["failed"].append(algo_id)
                    report["errors"].append({"id": algo_id, "sCode": code, "sMsg": item.get("sMsg")})
                else:
                    report["failed"].append(algo_id)
                    report["errors"].append({"id": algo_id, "algoOrdType": algo_ord_type,
                                             "error": exc_text or "нет в ответе биржи — остановка не подтверждена"})
        targets = retry
        if not targets:
            break
    for algo_id, _, _ in targets:  # транзиентная ошибка не ушла за attempts повторов
        report["failed"].append(algo_id)
        report["errors"].append(last_error[algo_id])


def _stop_family(exchange, *, family: str, api: tuple[str, ...], bot_types: tuple[str, ...],
                 fetch: Callable[[Any, str], list[dict]], send: Callable[[list], dict], chunk_size: int,
                 stop_type: str, algo_ids: Optional[Collection[str]], attempts: int, backoff_s: float,
                 sleep: Callable[[float], None]) -> dict:
    """Общая остановка семейства ботов (grid / dca): отчёт {stopped, failed, skipped, errors}."""
    report: dict[str, list] = {"stopped": [], "failed": [], "skipped": [], "errors": []}
    if not all(callable(getattr(exchange, name, None)) for name in api):
        # Фейки чужих тестов без bot-методов — только errors. Настоящий клиент
        # ccxt без эндпоинтов (старая версия) — failed: боты переживут kill.
        log.error("клиент %s без tradingBot/%s API — %s-боты не остановлены",
                  type(exchange).__name__, family, family)
        report["errors"] = [{"algoOrdType": t, "error": f"клиент без tradingBot/{family} API"} for t in bot_types]
        if isinstance(exchange, ccxt.Exchange):
            report["failed"].append(f"{family}: у клиента ccxt нет tradingBot/{family} API")
        return report
    wanted = set(algo_ids) if algo_ids is not None else None
    targets = _list_bots(exchange, family, bot_types, fetch, wanted, stop_type,
                         attempts, backoff_s, sleep, report)
    _stop_bots(targets, send, chunk_size, family, attempts, backoff_s, sleep, report)
    if report["failed"]:
        log.error("Не удалось остановить %s-ботов: %s", family, report["failed"])
    return report


def stop_grid_bots(
    exchange,
    stop_type: str = "2",
    algo_ids: Optional[Collection[str]] = None,
    attempts: int = 2,
    backoff_s: float = 1.0,
    sleep: Callable[[float], None] = time.sleep,
) -> dict:
    """Останавливает нативные grid-боты OKX (grid и contract_grid) — для kill-switch.

    stop_type "2" (по умолчанию) — остановить, оставив базу (spot grid) или
    позицию (contract grid) — без flatten, KILL-FLATTEN-DEFAULT; "1" — продать
    базу / закрыть позицию по рынку. POST tradingBot/grid/stop-order-algo —
    пачка до 10 ботов; частичный успех — code 2 и sCode по каждому боту.

    algo_ids, skipped, повторы транзиентных ошибок и failed — как у stop_dca_bots.

    Возвращает {"stopped": [algoIds], "failed": [algoIds | описание шага],
    "skipped": [algoIds], "errors": [...]}.
    """
    def send(chunk: list) -> dict:
        return exchange.private_post_tradingbot_grid_stop_order_algo([
            {"algoId": algo_id, "instId": inst_id, "algoOrdType": algo_ord_type, "stopType": stop_type}
            for algo_id, algo_ord_type, inst_id in chunk
        ])

    return _stop_family(exchange, family="grid", api=GRID_API, bot_types=GRID_BOT_TYPES,
                        fetch=fetch_grid_bots, send=send, chunk_size=BOT_STOP_BATCH,
                        stop_type=stop_type, algo_ids=algo_ids, attempts=attempts,
                        backoff_s=backoff_s, sleep=sleep)


def stop_dca_bots(
    exchange,
    stop_type: str = "2",
    algo_ids: Optional[Collection[str]] = None,
    attempts: int = 2,
    backoff_s: float = 1.0,
    sleep: Callable[[float], None] = time.sleep,
) -> dict:
    """Останавливает нативные DCA-боты OKX (spot_dca и contract_dca) — для kill-switch.

    Без этого шага DCA-боты переживают kill-switch и продолжают докупать
    страховочными ордерами.

    stop_type "2" (по умолчанию) — остановить, оставив монеты (spot_dca) или
    позицию (contract_dca); "1" — продать монеты / закрыть позицию по рынку.
    Дефолт "2" — тот же, что у stop_grid_bots, и соответствует решению
    KILL-FLATTEN-DEFAULT (kill-switch без flatten; рыночное закрытие — отдельное
    явное решение оператора). Внимание: остановленный бот снимает и свой SL.

    algo_ids — остановить только перечисленные algoId (точечная остановка,
    проверка на demo); None — все активные боты обоих типов.

    skipped — боты, которые останавливать не нужно: state "stopping" (остановка
    уже идёт), "no_close_position" при stop_type "2" (уже остановлен, позиция
    оставлена; закрыть её — только явным "1", см. .agents/skills/okx-cex-bot/SKILL.md
    «DCA Bot — Stop») и sCode 51291 — бот закончил сам (SL) между списком и stop
    или stop уже дошёл, а ответ потерялся.

    Изоляция ошибок: POST tradingBot/dca/stop — один бот за запрос, отказ по
    боту не мешает остальным. Транзиентные ошибки (ccxt.NetworkError и sCode
    50011/50001/50013) повторяются до attempts раз с backoff 1, 2, 4… с
    (risk-core.md §4 п.5): сначала проход по всем ботам, повторы — после.
    Не получен список одного типа — описание в failed: боты этого типа не
    остановлены, и kill-switch обязан это показать.

    Возвращает {"stopped": [algoIds], "failed": [algoIds | описание шага],
    "skipped": [algoIds], "errors": [...]}.
    """
    def send(chunk: list) -> dict:
        (algo_id, algo_ord_type, _), = chunk
        return exchange.private_post_tradingbot_dca_stop(
            {"algoId": algo_id, "algoOrdType": algo_ord_type, "stopType": stop_type})

    return _stop_family(exchange, family="dca", api=DCA_API, bot_types=DCA_BOT_TYPES,
                        fetch=fetch_dca_bots, send=send, chunk_size=1,
                        stop_type=stop_type, algo_ids=algo_ids, attempts=attempts,
                        backoff_s=backoff_s, sleep=sleep)


def emergency_stop(exchange, include_bots: bool = True,
                   sleep: Callable[[float], None] = time.sleep) -> dict:
    """Всё для kill-switch: отмена ордеров (включая algo) + остановка grid- и DCA-ботов.

    Каждый шаг изолирован: исключение в отмене ордеров или в остановке одного
    семейства ботов попадает в failed/errors и не мешает остальным шагам —
    аварийная остановка должна дойти до конца. Боты останавливаются со
    stopType "2" (без flatten, монеты/позиции остаются — KILL-FLATTEN-DEFAULT).

    Формат совместим с колбэком risk.set_order_canceller: {"cancelled", "failed", ...};
    bots_stopped — algoId всех остановленных ботов (grid + DCA); failed — id и
    описания шагов, которые не выполнены (в т.ч. не получен список DCA-ботов).
    sleep — пауза backoff для повторов отмены ордеров и остановки DCA (тесты).
    """
    try:
        report = cancel_all_orders(exchange, sleep=sleep)
    except Exception as exc:  # не глушим: фиксируем и всё равно останавливаем ботов
        log.error("emergency_stop: отмена ордеров упала: %s", exc)
        report = {"cancelled": [], "failed": [f"отмена ордеров: {exc}"],
                  "errors": [{"step": "cancel_all_orders", "error": str(exc)}]}
    if include_bots:
        report["bots_stopped"] = []
        stoppers = (("grid", functools.partial(stop_grid_bots, sleep=sleep)),
                    ("dca", functools.partial(stop_dca_bots, sleep=sleep)))
        for family, stopper in stoppers:
            try:
                bots = stopper(exchange)
            except Exception as exc:  # не глушим: фиксируем и идём к следующему семейству
                log.error("emergency_stop: остановка %s-ботов упала: %s", family, exc)
                report["failed"].append(f"остановка {family}-ботов: {exc}")
                report["errors"].append({"step": f"stop_{family}_bots", "error": str(exc)})
                continue
            report["bots_stopped"] += bots["stopped"]
            report["cancelled"] += bots["stopped"]
            report["failed"] += bots["failed"]
            report["errors"] += bots["errors"]
    return report


# --- Реестр колбэков kill-switch (задача KILL-CALLBACK-OWNER) ---
#
# Проблема: risk.set_order_canceller хранит ОДИН слот — при одновременной
# работе engine и OrderRouter последняя регистрация перезаписывала предыдущую
# (так делал engine.py до ENGINE-KILL-REG). Решение: список подписчиков здесь, в risk регистрируется
# единый диспетчер kill_switch_dispatch, который вызывает ВСЕХ подписчиков.
# Подписчик — callable(flatten: bool) -> {"cancelled": [...], "failed": [...]}
# (тот же контракт, что у risk.set_order_canceller).

KillCallback = Callable[[bool], dict]
_kill_callbacks: list[KillCallback] = []
_kill_lock = threading.Lock()


def register_kill_callback(callback: KillCallback) -> None:
    """Подписать колбэк на kill-switch.

    Идемпотентно: один и тот же callable дважды не добавляется. Для
    bound-методов дедупликация по (объект, функция): повторная регистрация
    self._cancel_all_orders того же экземпляра роутера не дублирует вызов.
    """
    with _kill_lock:
        if callback not in _kill_callbacks:
            _kill_callbacks.append(callback)


def unregister_kill_callback(callback: KillCallback) -> None:
    """Отписать колбэк (пересоздание владельца, тесты). Нет подписки — no-op."""
    with _kill_lock:
        if callback in _kill_callbacks:
            _kill_callbacks.remove(callback)


def registered_kill_callbacks() -> tuple[KillCallback, ...]:
    """Снимок текущих подписчиков — для диагностики и тестов."""
    with _kill_lock:
        return tuple(_kill_callbacks)


def kill_switch_dispatch(flatten: bool = False) -> dict:
    """Единый колбэк для risk.set_order_canceller: вызывает ВСЕХ подписчиков.

    Исключение одного подписчика фиксируется в failed и не мешает остальным —
    аварийная отмена не должна зависеть от чужого падения. Отчёты сливаются:
    cancelled/failed/bots_stopped — объединение без дублей, errors — все.
    Пустой реестр — failed с пояснением (аналог «коннектор не подключён»).
    """
    with _kill_lock:
        subscribers = list(_kill_callbacks)
    report: dict[str, Any] = {"cancelled": [], "failed": [], "errors": []}
    if not subscribers:
        report["failed"].append(
            "kill_switch: нет подписчиков (register_kill_callback не вызван)")
        return report
    for callback in subscribers:
        try:
            part = callback(flatten)
        except Exception as exc:  # не глушим: фиксируем и идём к следующему
            log.error("kill_switch: подписчик %r упал: %s", callback, exc)
            report["failed"].append(f"исключение подписчика: {exc}")
            continue
        for key in ("cancelled", "failed", "bots_stopped"):
            for item in part.get(key) or []:
                if item not in report.setdefault(key, []):
                    report[key].append(item)
        report["errors"].extend(part.get("errors") or [])
    report["cancelled"] = sorted(report["cancelled"], key=str)
    return report
