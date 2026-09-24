"""Режим аккаунта OKX и параметры спот-ордеров (задача SPOT-TDMODE).

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
"""
from dataclasses import dataclass
from typing import Any, Optional

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


def fetch_account_mode(exchange: Any) -> AccountMode:
    """GET account/config -> AccountMode. Только чтение."""
    data = (exchange.private_get_account_config() or {}).get("data") or []
    if not data:
        raise RuntimeError("account/config: пустой ответ")
    return parse_account_mode(data[0])


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
