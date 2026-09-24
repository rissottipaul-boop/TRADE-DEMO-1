"""Режим аккаунта OKX: параметры спот-ордеров (SPOT-TDMODE), проверка и смена режима (ACCT-SWITCH-CLI).

Биржа принимает спот-ордер только с tdMode, который подходит режиму аккаунта
(`acctLv` в GET /api/v5/account/config):
- 1 — Spot, 2 — Spot and futures: спот без маржи, tdMode=cash;
- 3 — Multi-currency margin, 4 — Portfolio margin: спот входит в единую маржу,
  tdMode=cross. С tdMode=cash биржа отвечает sCode 51000 «Parameter tdMode
  error» (demo 24.09, insights/okx-api.md §10 п. 22).

Заём мимо риск-ядра. В режимах 3–4 при autoLoan=true покупка больше availBal
молча берёт заём, и появляется плечо. Если заём возможен (autoLoan в режимах
3–4 или enableSpotBorrow), ордер сверяется с availBal до биржи: покупка —
по котируемой валюте (sz × px, у рыночной — с запасом), продажа — по базе.

Рыночная покупка в режиме cross. CCXT 4.5 (okx.create_order_request) для
маржинального спота не передаёт tgtCcy. Для рыночной покупки в режиме cross OKX
считает sz в котируемой валюте. Поэтому покупка уходит с tgtCcy=quote_ccy и
явной суммой cost = sz × px. Сам CCXT на цену не умножает: у okx по умолчанию
createMarketBuyOrderRequiresPrice=False, и он отправил бы количество базы как
сумму в USDT, обрезанную до шага цены (0.0002 BTC → «0»; тест
test_account_mode.CcxtRequestTest). Лимитные ордера и рыночная продажа — в
базовой валюте, как в режиме cash.

CLI (ACCT-SWITCH-CLI). У okx CLI 1.4.8 смены режима нет, есть только `account config`.

    python -m src.account_mode status                  # только чтение: acctLv и имя режима, autoLoan,
                                                       # enableSpotBorrow, posMode, tdMode спота, заём
    python -m src.account_mode precheck --acct-lv 2    # только чтение: можно ли перейти в режим 2
    python -m src.account_mode switch --acct-lv 2      # ТОЛЬКО demo: проверка и переключение
    python -m src.account_mode status --mode live      # --mode demo|live — после команды, как у src.ops

- status: GET account/config.
- precheck: GET account/set-account-switch-precheck?acctLv=N. «Можно переключать» —
  только при sCode "0" и пустом unmatchedInfoCheck. Иначе выход 1 и перечень
  блокеров: тип и суть каждого, позиции, posTierCheck.
- switch — только demo. При `--mode live` или OKX_MODE=live — отказ без обращения
  к бирже, выход 2: режим live-аккаунта меняет только человек в Web/App. Клиент
  без demo-заголовка x-simulated-trading — тоже отказ.
  Порядок:
  1. account/config: режим уже N → «уже N», выход 0 без POST;
  2. precheck: есть блокеры → выход 1 без POST;
  3. POST account/set-account-level {"acctLv": N};
  4. account/config, до 3 чтений с паузой 1 с: acctLv = N → выход 0, иначе выход 1.
- N — от 1 до 4, проверяется до сети. Других эндпоинтов команды не вызывают:
  ордеров не ставят, ботов, движок и preset (account-level-switch-preset) не трогают.
- Коды выхода:
  - 0 — прочитано, можно переключать или переключено;
  - 1 — блокеры или смена не подтвердилась;
  - 2 — код OKX ≠ 0, исключение CCXT, нет ключей, неверный N или switch вне demo.
  Ошибка описывается через errors.explain_error вместе с ответом OKX.

Не проверено на demo (insights/okx-api.md §10 п. 24). Поля ответа precheck взяты
из документации OKX (зеркало okx/ai-builder-openapi-md) и сверены с моделями
JKorf/OKX.Net. Живой ответ ещё не видели, поэтому разбор защитный:
- неизвестный или пустой sCode — блокер;
- posList у блокера — строки posId (так в документации) или объекты {posId, lever}
  (так в OKX.Net);
- null и "" вместо объектов и списков допустимы.
По документации первое включение режима делается в Web/App, иначе 51070. Сразу ли
account/config показывает новый режим, тоже не проверено.
"""
import argparse
import json
import logging
import sys
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Optional

import ccxt

from .config import MODES, default_mode, load_settings
from .connector import create_exchange
from .errors import explain_error, extract_error_code

ACCT_LV_NAMES = {"1": "Spot", "2": "Spot and futures", "3": "Multi-currency margin", "4": "Portfolio margin"}
CASH_LEVELS = frozenset({"1", "2"})
MARKET_BUY_BUFFER = 0.01  # рыночная покупка: цена может уйти от px, к стоимости +1%


def _flag(value: Any) -> bool:
    return value is True or str(value).strip().lower() == "true"


@dataclass(frozen=True)
class AccountMode:
    acct_lv: str
    auto_loan: bool = False
    spot_borrow: bool = False  # enableSpotBorrow: заём в режиме Spot

    @property
    def name(self) -> str:
        return ACCT_LV_NAMES.get(self.acct_lv, "?")

    @property
    def spot_td_mode(self) -> str:
        return "cash" if self.acct_lv in CASH_LEVELS else "cross"

    @property
    def borrow_reason(self) -> str:
        """Почему спот-ордер может взять заём; пустая строка — не может."""
        if self.acct_lv not in CASH_LEVELS and self.auto_loan:
            return f"acctLv={self.acct_lv} ({self.name}) и autoLoan=true"
        if self.spot_borrow:
            return "enableSpotBorrow=true"
        return ""

    @property
    def can_borrow(self) -> bool:
        return bool(self.borrow_reason)


def parse_account_mode(config: dict) -> AccountMode:
    """AccountMode из ответа account/config; неизвестный acctLv — ValueError."""
    acct_lv = str((config or {}).get("acctLv") or "")
    if acct_lv not in ACCT_LV_NAMES:
        raise ValueError(f"неизвестный режим аккаунта acctLv={acct_lv!r}")
    return AccountMode(acct_lv, _flag(config.get("autoLoan")), _flag(config.get("enableSpotBorrow")))


class OkxResponseError(RuntimeError):
    """Ответ OKX с code (или sCode) ≠ 0, который CCXT не превратил в исключение."""

    def __init__(self, endpoint: str, code: str, msg: str = ""):
        super().__init__(f"{endpoint}: code {code} {msg}".rstrip())
        self.code = code


def _response_data(response: Any, endpoint: str, required: bool = True) -> list[dict]:
    """data[] ответа OKX. code ≠ 0 — OkxResponseError, пустой data при required — RuntimeError.

    CCXT (okx.handle_errors) сам бросает исключение на любой code, кроме 0 и 2
    («частичный успех»), — code 2 ловится здесь. Нет поля code — как у фейков тестов — это 0.
    """
    response = response if isinstance(response, dict) else {}
    code = str(response.get("code") or "0")
    if code != "0":
        raise OkxResponseError(endpoint, code, str(response.get("msg") or ""))
    data = [row for row in response.get("data") or [] if isinstance(row, dict)]
    if required and not data:
        raise RuntimeError(f"{endpoint}: пустой ответ")
    return data


def fetch_account_config(exchange: Any) -> dict:
    """GET account/config -> data[0], поля как есть (acctLv, autoLoan, posMode…). Только чтение."""
    return _response_data(exchange.private_get_account_config(), "account/config")[0]


def fetch_account_mode(exchange: Any) -> AccountMode:
    """GET account/config -> AccountMode. Только чтение."""
    return parse_account_mode(fetch_account_config(exchange))


def spot_legs(inst_id: str) -> Optional[tuple[str, str]]:
    """(база, котируемая) спот-инструмента: BTC/USDT (CCXT) или BTC-USDT (OKX); иначе None."""
    if ":" in inst_id:
        return None
    parts = inst_id.split("/") if "/" in inst_id else inst_id.split("-")
    if len(parts) != 2 or not all(parts):
        return None
    return parts[0], parts[1]


def is_spot(inst_id: str) -> bool:
    return spot_legs(inst_id) is not None


def spot_order_params(mode: AccountMode, ord_type: str, side: str, sz: float,
                      px: Optional[float]) -> dict:
    """Параметры CCXT create_order для спот-ордера в этом режиме аккаунта.

    Рыночная покупка в режиме cross без цены — ValueError: сумму в котируемой
    валюте не посчитать.
    """
    params: dict = {"tdMode": mode.spot_td_mode}
    if mode.spot_td_mode == "cross" and ord_type == "market" and side == "buy":
        if not px:
            raise ValueError("рыночная покупка в режиме cross: нужна цена, чтобы передать сумму в котируемой валюте")
        params.update(tgtCcy="quote_ccy", cost=sz * px)
    return params


def avail_balance(exchange: Any, ccy: str) -> float:
    """availBal валюты на торговом счёте: GET account/balance?ccy=… (только чтение)."""
    rows = (exchange.private_get_account_balance({"ccy": ccy}) or {}).get("data") or [{}]
    for detail in rows[0].get("details") or []:
        if detail.get("ccy") == ccy:
            return float(detail.get("availBal") or 0.0)
    return 0.0


def check_no_borrow(exchange: Any, mode: AccountMode, inst_id: str, side: str, ord_type: str,
                    sz: float, px: Optional[float]) -> tuple[bool, str]:
    """(True, "ok") — ордер не возьмёт заём; иначе (False, причина). Сеть — только если заём возможен."""
    if not mode.can_borrow:
        return True, "ok"
    legs = spot_legs(inst_id)
    if legs is None:
        return False, f"{inst_id}: не спот, проверка займа не поддержана"
    base, quote = legs
    if side == "buy":
        if not px:
            return False, f"заём возможен ({mode.borrow_reason}), а у покупки нет цены — стоимость не оценить"
        buffer = (1 + MARKET_BUY_BUFFER) if ord_type == "market" else 1.0
        need, ccy = sz * px * buffer, quote
    else:
        need, ccy = sz, base
    avail = avail_balance(exchange, ccy)
    if need > avail:
        return False, (f"ордер возьмёт заём ({mode.borrow_reason}): нужно {need:.8g} {ccy}, "
                       f"доступно {avail:.8g} {ccy}")
    return True, "ok"


# --- Проверка и смена режима аккаунта (ACCT-SWITCH-CLI) ---
#
# Эндпоинты — документация OKX (okx/ai-builder-openapi-md: preCheckAccountLevel.md,
# setAccountLevel.md); оба — 5 запросов/2 с на UID. CCXT 4.5.83:
# private_get_account_set_account_switch_precheck, private_post_account_set_account_level.

DEMO_HEADER = "x-simulated-trading"  # его ставит CCXT set_sandbox_mode(True) в exchange.headers
CONFIRM_ATTEMPTS = 3   # чтений account/config после POST, пока не покажет новый режим
CONFIRM_DELAY_S = 1.0  # пауза между ними: лимит account/config — 5 запросов/2 с

# sCode precheck — документация OKX и OKX.Net (AccountSwitchCheckResult)
PRECHECK_CODES = {
    "0": "все проверки пройдены",
    "1": "есть несовместимые условия (unmatchedInfoCheck)",
    "3": "не задано плечо кросс-позиций контрактов для нового режима (preset)",
    "4": "не пройдена проверка тиров позиций или маржи",
}
# unmatchedInfoCheck[].type — документация OKX и OKX.Net (UnmatchedInfoType)
UNMATCHED_TYPES = {
    "asset_validation": "не хватает активов для этого режима",
    "pending_orders": "активные ордера в стакане",
    "pending_algos": "активные algo-ордера и торговые боты (iceberg, TWAP, recurring buy и др.)",
    "isolated_margin": "изолированная маржа (quick margin, ручной перевод)",
    "isolated_contract": "изолированные контракты с ручным переводом маржи",
    "contract_long_short": "контрактные позиции в режиме long/short",
    "cross_margin": "кросс-маржинальные позиции",
    "cross_option_buyer": "кросс-позиции покупателя опционов",
    "isolated_option": "изолированные опционы (мешают только переходу в Spot)",
    "growth_fund": "позиции на пробные средства (trial funds)",
    "all_positions": "открытые позиции",
    "spot_lead_copy_only_simple_single": "лид-трейдеру копитрейдинга доступны только режимы 1 и 2",
    "stop_spot_custom": "кастомный копитрейдинг спота",
    "stop_futures_custom": "кастомный копитрейдинг контрактов",
    "lead_portfolio": "лид-трейдер не может перейти в Portfolio margin",
    "futures_smart_sync": "smart sync контрактов: в режим Spot нельзя",
    "vip_fixed_loan": "VIP-заём",
    "repay_borrowings": "непогашенные займы",
    "compliance_restriction": "маржинальная торговля недоступна по требованиям регулятора",
    "compliance_kyc2": "маржинальная торговля недоступна по требованиям регулятора, нужен KYC2",
}
# Подсказки к кодам смены режима, которых нет в errors.ERROR_MAP
SWITCH_HINTS = {
    "51070": ("первое включение режима OKX делает только в Web/App (Settings → Account mode; "
              "для demo — в режиме Demo trading), после этого режим переключается и через API"),
}
LIVE_REFUSAL = ("Отказ: switch работает только с demo-аккаунтом. Режим live-аккаунта меняет только "
                "человек в Web/App OKX; status и precheck с --mode live — только чтение.")


def validate_acct_lv(value: Any) -> str:
    """acctLv от "1" до "4" строкой; иное — ValueError. Проверяется до обращения к бирже."""
    text = str(value).strip()
    if text not in ACCT_LV_NAMES:
        known = ", ".join(f"{lv} — {name}" for lv, name in ACCT_LV_NAMES.items())
        raise ValueError(f"acctLv должен быть от 1 до 4 ({known}), получено {value!r}")
    return text


def is_demo_client(exchange: Any) -> bool:
    """Клиент ходит в demo: CCXT set_sandbox_mode(True) ставит заголовок x-simulated-trading: 1."""
    headers = getattr(exchange, "headers", None)
    return isinstance(headers, dict) and str(headers.get(DEMO_HEADER)) == "1"


def describe_mode(mode: AccountMode) -> str:
    borrow = f"возможен: {mode.borrow_reason}" if mode.can_borrow else "невозможен"
    return f"acctLv {mode.acct_lv} ({mode.name}), tdMode спота {mode.spot_td_mode}, заём спот-ордером {borrow}"


@dataclass(frozen=True)
class SwitchPrecheck:
    """Разобранный ответ precheck (поля — по документации OKX, на demo не проверено)."""
    target: str                   # запрошенный acctLv
    s_code: str
    cur_acct_lv: str
    acct_lv: str
    blockers: tuple[str, ...]     # «тип: суть» каждого блокера
    positions: tuple[str, ...]    # posList: «posId ×плечо» кросс-позиций контрактов после перехода
    margin_before: str            # mgnBf одной строкой, "" — нет
    margin_after: str             # mgnAft
    raw: dict = field(default_factory=dict, compare=False)

    @property
    def ok(self) -> bool:
        return self.s_code == "0" and not self.blockers


def _items(value: Any) -> list:
    return value if isinstance(value, list) else []


def _pos_ids(value: Any) -> list[str]:
    """posList блокера: строки posId (документация OKX) или объекты {posId, lever} (OKX.Net)."""
    ids = []
    for pos in _items(value):
        pos_id = pos.get("posId") if isinstance(pos, dict) else pos
        if pos_id not in (None, ""):
            ids.append(str(pos_id))
    return ids


def _margin_text(value: Any) -> str:
    """mgnBf или mgnAft одной строкой; null, "" и {} — пустая строка."""
    if not isinstance(value, dict):
        return ""
    parts = [f"{key} {value[key]}" for key in ("acctAvailEq", "mgnRatio") if value.get(key) not in (None, "")]
    for row in _items(value.get("details")):
        if isinstance(row, dict):
            parts.append(f"{row.get('ccy') or '?'}: availEq {row.get('availEq') or '—'}, "
                         f"mgnRatio {row.get('mgnRatio') or '—'}")
    return "; ".join(parts)


def parse_switch_precheck(item: dict, target: str) -> SwitchPrecheck:
    """data[0] precheck -> SwitchPrecheck. Всё непонятное — блокер: переключаться «на авось» нельзя."""
    target = validate_acct_lv(target)
    s_code = "" if item.get("sCode") is None else str(item["sCode"]).strip()
    blockers: list[str] = []
    for info in _items(item.get("unmatchedInfoCheck")):
        info = info if isinstance(info, dict) else {"type": str(info)}
        kind = str(info.get("type") or "?")
        text = f"{kind}: {UNMATCHED_TYPES.get(kind, 'тип не из документации OKX')}"
        ids = _pos_ids(info.get("posList"))
        if ids:
            text += f"; позиции {', '.join(ids)}"
        if info.get("totalAsset") not in (None, ""):
            text += f"; totalAsset {info['totalAsset']}"
        blockers.append(text)
    for tier in _items(item.get("posTierCheck")):
        tier = tier if isinstance(tier, dict) else {}
        blockers.append(f"posTierCheck: {tier.get('instType', '?')} {tier.get('instFamily', '?')}, "
                        f"позиция {tier.get('pos', '?')} при плече {tier.get('lever', '?')}, "
                        f"допустимо {tier.get('maxSz', '?')}")
    if s_code not in PRECHECK_CODES:
        blockers.append(f"sCode {s_code or '—'}: неизвестный код проверки, переключать нельзя")
    elif s_code != "0" and not blockers:
        blockers.append(f"sCode {s_code}: {PRECHECK_CODES[s_code]}")
    acct_lv = str(item.get("acctLv") or "")
    if acct_lv and acct_lv != target:
        blockers.append(f"acctLv {acct_lv}: ответ не для запрошенного режима {target}")
    positions = tuple(f"{pos.get('posId', '?')} ×{pos.get('lever', '?')}"
                      for pos in _items(item.get("posList")) if isinstance(pos, dict))
    return SwitchPrecheck(target=target, s_code=s_code, cur_acct_lv=str(item.get("curAcctLv") or ""),
                          acct_lv=acct_lv, blockers=tuple(blockers), positions=positions,
                          margin_before=_margin_text(item.get("mgnBf")),
                          margin_after=_margin_text(item.get("mgnAft")), raw=dict(item))


def precheck_switch(exchange: Any, target: Any) -> SwitchPrecheck:
    """GET account/set-account-switch-precheck?acctLv=N -> SwitchPrecheck. Только чтение.

    Неверный N — ValueError до сети. Ошибки OKX и CCXT не глушатся.
    """
    target = validate_acct_lv(target)
    response = exchange.private_get_account_set_account_switch_precheck({"acctLv": target})
    return parse_switch_precheck(_response_data(response, "account/set-account-switch-precheck")[0], target)


def _post_account_level(exchange: Any, target: str) -> str:
    """POST account/set-account-level {"acctLv": N} -> acctLv из ответа ("" — его нет).

    Меняет режим аккаунта: вызывается только из switch_account_level после всех
    проверок. Итог подтверждает повторное чтение account/config, поэтому пустой
    data — не ошибка; code или sCode ≠ 0 — OkxResponseError.
    """
    data = _response_data(exchange.private_post_account_set_account_level({"acctLv": target}),
                          "account/set-account-level", required=False)
    item = data[0] if data else {}
    s_code = str(item.get("sCode") or "0")
    if s_code != "0":
        raise OkxResponseError("account/set-account-level", s_code, str(item.get("sMsg") or ""))
    return str(item.get("acctLv") or "")


@dataclass(frozen=True)
class SwitchResult:
    status: str                   # already | blocked | switched | unconfirmed
    target: str
    before: AccountMode
    precheck: Optional[SwitchPrecheck] = None
    after: Optional[AccountMode] = None

    @property
    def exit_code(self) -> int:
        return 0 if self.status in ("already", "switched") else 1


def switch_account_level(exchange: Any, target: Any, *, progress: Callable[[str], None] = lambda line: None,
                         attempts: int = CONFIRM_ATTEMPTS, delay_s: float = CONFIRM_DELAY_S,
                         sleep: Callable[[float], None] = time.sleep) -> SwitchResult:
    """Смена режима demo-аккаунта: account/config → precheck → POST set-account-level → account/config.

    До сети: неверный N — ValueError, клиент без demo-заголовка — PermissionError.
    Режим уже N — already (без precheck и POST); блокеры precheck — blocked (без
    POST). После POST режим перечитывается до attempts раз с паузой delay_s:
    совпал — switched, нет — unconfirmed. progress получает строки хода работы.
    Ошибки OKX и CCXT не глушатся.
    """
    target = validate_acct_lv(target)
    if not is_demo_client(exchange):
        raise PermissionError("клиент без demo-заголовка x-simulated-trading: 1 — режим live-аккаунта "
                              "меняет только человек в Web/App")
    before = fetch_account_mode(exchange)
    progress(f"account/config: {describe_mode(before)}")
    if before.acct_lv == target:
        return SwitchResult("already", target, before)
    check = precheck_switch(exchange, target)
    for line in render_precheck(check):
        progress(line)
    if not check.ok:
        return SwitchResult("blocked", target, before, check)
    answer = _post_account_level(exchange, target)
    progress(f"account/set-account-level: принят, acctLv в ответе {answer or '—'}")
    after = before
    for attempt in range(max(1, attempts)):
        if attempt:
            sleep(delay_s)
        after = fetch_account_mode(exchange)
        if after.acct_lv == target:
            break
    progress(f"account/config: {describe_mode(after)}")
    return SwitchResult("switched" if after.acct_lv == target else "unconfirmed", target, before, check, after)


# --- Вывод и CLI ---

def _flag_text(value: Any) -> str:
    return "—" if value in (None, "") else str(_flag(value)).lower()


def render_status(config: dict, mode: str) -> list[str]:
    """Строки `status` по data[0] account/config; неизвестный acctLv — ValueError."""
    acct = parse_account_mode(config)
    borrow = f"возможен: {acct.borrow_reason}" if acct.can_borrow else "невозможен"
    return [f"Режим аккаунта [{mode}]: acctLv {acct.acct_lv} ({acct.name})",
            f"  autoLoan: {_flag_text(config.get('autoLoan'))}",
            f"  enableSpotBorrow: {_flag_text(config.get('enableSpotBorrow'))}",
            f"  posMode: {config.get('posMode') or '—'}",
            f"  tdMode спота: {acct.spot_td_mode}",
            f"  заём спот-ордером: {borrow}"]


def render_precheck(check: SwitchPrecheck) -> list[str]:
    cur = check.cur_acct_lv or "?"
    lines = [f"precheck: acctLv {cur} ({ACCT_LV_NAMES.get(cur, '?')}) → {check.target} "
             f"({ACCT_LV_NAMES[check.target]}), sCode {check.s_code or '—'}: "
             f"{PRECHECK_CODES.get(check.s_code, 'неизвестный код')}"]
    if check.cur_acct_lv == check.target:
        lines.append(f"  режим уже {check.target}")
    if check.positions:
        lines.append("  кросс-позиции контрактов (posId ×плечо после перехода): " + ", ".join(check.positions))
    if check.margin_before:
        lines.append(f"  маржа до: {check.margin_before}")
    if check.margin_after:
        lines.append(f"  маржа после: {check.margin_after}")
    if check.blockers:
        lines.append(f"  блокеры ({len(check.blockers)}):")
        lines += [f"    - {blocker}" for blocker in check.blockers]
    # поля ответа на demo ещё не проверены — сырой ответ нужен для insights/okx-api.md
    lines.append(f"  ответ OKX: {json.dumps(check.raw, ensure_ascii=False, sort_keys=True)}")
    return lines


def describe_error(exc: BaseException) -> str:
    """Ошибка OKX/CCXT: errors.explain_error по коду, подсказка смены режима, текст исключения."""
    text = f"{type(exc).__name__}: {str(exc)[:400]}"
    if not isinstance(exc, (ccxt.BaseError, OkxResponseError)):
        return text
    code = exc.code if isinstance(exc, OkxResponseError) else extract_error_code(exc)
    return " | ".join(part for part in (explain_error(code), SWITCH_HINTS.get(code or ""), text) if part)


def _exchange(mode: str) -> Any:
    return create_exchange(load_settings(mode))


def _acct_lv_arg(value: str) -> str:
    try:
        return validate_acct_lv(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(str(exc)) from None


def cmd_status(args: argparse.Namespace) -> int:
    for line in render_status(fetch_account_config(_exchange(args.mode)), args.mode):
        print(line)
    return 0


def cmd_precheck(args: argparse.Namespace) -> int:
    check = precheck_switch(_exchange(args.mode), args.acct_lv)
    for line in render_precheck(check):
        print(line)
    print("Итог: можно переключать" if check.ok else f"Итог: переключать нельзя, блокеров {len(check.blockers)}")
    return 0 if check.ok else 1


def _switch_summary(result: SwitchResult) -> str:
    name = ACCT_LV_NAMES[result.target]
    if result.status == "already":
        return f"Итог: режим уже {result.target} ({name}), переключать нечего, POST не отправлялся"
    if result.status == "blocked":
        return f"Итог: не переключено, блокеров {len(result.precheck.blockers)}; POST не отправлялся"
    if result.status == "switched":
        return f"Итог: режим переключён на {result.target} ({name}) и подтверждён account/config"
    return (f"Итог: POST принят, но account/config показывает acctLv {result.after.acct_lv}, "
            f"переключение не подтверждено; повторите позже: python -m src.account_mode status")


def cmd_switch(args: argparse.Namespace) -> int:
    if args.mode != "demo":
        print(LIVE_REFUSAL, file=sys.stderr)
        return 2
    exchange = _exchange(args.mode)
    print(f"Смена режима demo-аккаунта на acctLv {args.acct_lv} ({ACCT_LV_NAMES[args.acct_lv]})")
    try:
        result = switch_account_level(exchange, args.acct_lv, progress=print,
                                      attempts=CONFIRM_ATTEMPTS, delay_s=CONFIRM_DELAY_S)
    except PermissionError as exc:
        print(f"Отказ: {exc}", file=sys.stderr)
        return 2
    print(_switch_summary(result))
    return result.exit_code


def main(argv: Optional[list[str]] = None) -> int:
    logging.basicConfig(level=logging.WARNING, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    for stream in (sys.stdout, sys.stderr):  # → и × в консоли Windows с cp1251
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(prog="python -m src.account_mode", description=__doc__.splitlines()[0])
    mode_opt = argparse.ArgumentParser(add_help=False)
    mode_opt.add_argument("--mode", choices=MODES, default=None,
                          help="demo|live — чьи ключи (по умолчанию OKX_MODE, иначе demo)")
    level_opt = argparse.ArgumentParser(add_help=False)
    level_opt.add_argument("--acct-lv", dest="acct_lv", required=True, type=_acct_lv_arg, metavar="N",
                           help="режим: 1 — Spot, 2 — Spot and futures, 3 — Multi-currency margin, "
                                "4 — Portfolio margin")
    sub = parser.add_subparsers(dest="cmd", required=True)
    sub.add_parser("status", parents=[mode_opt],
                   help="режим аккаунта, tdMode спота, возможен ли заём (только чтение)",
                   description="Только чтение: GET account/config. Выход: 0 — прочитано, 2 — ошибка.",
                   ).set_defaults(func=cmd_status)
    sub.add_parser("precheck", parents=[level_opt, mode_opt],
                   help="можно ли перейти в режим N (только чтение)",
                   description="Только чтение: GET account/set-account-switch-precheck. "
                               "Выход: 0 — можно переключать, 1 — есть блокеры (перечень в выводе), 2 — ошибка.",
                   ).set_defaults(func=cmd_precheck)
    sub.add_parser("switch", parents=[level_opt, mode_opt],
                   help="перейти в режим N — только demo: precheck, POST, проверка",
                   description="Только demo (--mode live — отказ, выход 2). account/config → precheck → "
                               "POST account/set-account-level → account/config. Выход: 0 — переключено "
                               "или режим уже N; 1 — блокеры (POST не отправлялся) или смена не "
                               "подтвердилась; 2 — ошибка.",
                   ).set_defaults(func=cmd_switch)
    args = parser.parse_args(argv)
    try:
        args.mode = args.mode or default_mode()
        return args.func(args)
    except Exception as exc:  # не глушим: понятное сообщение и выход 2
        print(f"Ошибка {args.cmd} [{args.mode or '?'}]: {describe_error(exc)}", file=sys.stderr)
        if args.cmd == "switch":
            print("Режим после сбоя проверьте: python -m src.account_mode status", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
