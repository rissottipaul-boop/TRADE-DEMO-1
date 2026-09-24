"""Предстартовая проверка live-кармана (задача LIVE-PREFLIGHT) — только чтение.

    python -m src.live_preflight               # live: любой fail -> exit 1, запуск запрещён
    python -m src.live_preflight --mode demo   # те же поля на demo: live-критерии — справочно

Проверки (insights/business-plan.md §4, этап 0):
- у ключа нет права withdraw (`perm` в GET /api/v5/account/config);
- ключ выпущен на суб-аккаунт (`uid != mainUid`) — суб-аккаунт и есть потолок убытка;
- к ключу привязан IP (`ip`): до DEPLOY-VPS — предупреждение, при
  `require_ip_whitelist: true` в кармане — fail;
- спот-ордер не может взять заём: `autoLoan` в режимах `acctLv` 3–4 или
  `enableSpotBorrow` — fail (SPOT-TDMODE, src/account_mode.py);
- деньги суб-аккаунта (asset-valuation, USDT) не больше `budget_usdt` кармана +5%;
- дрейф часов, live-окно (`enabled_until` задан и не истёк), валидный `ops/live-pocket.json`;
- в аккаунте нет чужих ордеров (clOrdId не `bot*` — их ставит не код проекта).

uid, mainUid и IP в отчёт не попадают — только результат сравнения. Отчёт
дублируется в `<data_dir режима>/preflight_last.json`.
"""
import argparse
import json
import logging
import sys
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from .account_mode import parse_account_mode
from .config import load_settings, normalize_mode, state_paths
from .connector import check_time_sync, create_exchange, fetch_pending_orders
from .live_policy import (
    LIVE_POCKET_PATH,
    LIVE_POLICY_PATH,
    PocketError,
    live_window,
    read_json,
    validate_pocket,
)
from .reconciler import OWN_CLORD_PREFIX, is_own_order

log = logging.getLogger("okx.live_preflight")

BUDGET_TOLERANCE = 1.05   # деньги суб-аккаунта сверх бюджета: +5% (курсовые колебания)
UNDERFUNDED_RATIO = 0.5   # меньше половины бюджета — бюджет не доведён
DRIFT_WARN_MS = 1000
DRIFT_FAIL_MS = 5000
ACCT_LV = {"1": "spot", "2": "spot+futures", "3": "multi-currency margin", "4": "portfolio margin"}


@dataclass
class Check:
    name: str
    status: str   # ok | warn | fail | info
    detail: str


def _short(exc: Exception) -> str:
    return f"{type(exc).__name__}: {exc}"[:200]


def evaluate(mode: str, *, account_config: Optional[dict], equity_usdt: Optional[float],
             equity_source: str, drift_ms: Optional[int], drift_error: Optional[str],
             pocket: Optional[dict], pocket_error: Optional[str], policy: Optional[dict],
             foreign_orders: Optional[int], now: Optional[datetime] = None) -> list[Check]:
    """Чистая функция: данные биржи и файлов → список проверок.

    В demo критерии, которые имеют смысл только для live (окно, карман,
    суб-аккаунт, бюджет, IP), понижаются до info/warn — отчёт без запрета.
    """
    live = mode == "live"
    strict = "fail" if live else "info"
    checks: list[Check] = []

    # 1. Live-окно
    if policy is None:
        checks.append(Check("policy_window", strict, f"нет {LIVE_POLICY_PATH}"))
    else:
        is_open, why = live_window(policy, now)
        checks.append(Check("policy_window", "ok" if is_open else strict, why))

    # 2. Карман
    budget: Optional[float] = None
    if pocket is None:
        checks.append(Check("pocket", strict, pocket_error or f"нет {LIVE_POCKET_PATH}"))
    else:
        errors = validate_pocket(pocket)
        if errors:
            checks.append(Check("pocket", strict, "; ".join(errors)))
        else:
            budget = float(pocket["budget_usdt"])
            checks.append(Check("pocket", "ok", f"budget_usdt {budget:g}"))
    require_ip = bool(pocket and pocket.get("require_ip_whitelist"))

    # 3–6. Ключ и аккаунт
    if account_config is None:
        checks.append(Check("account_config", "fail", "GET account/config не удался — ключ, сеть или регион"))
    else:
        perm = str(account_config.get("perm") or "")
        if not perm:
            checks.append(Check("key_permissions", "warn", "биржа не вернула perm — проверьте права ключа вручную"))
        elif "withdraw" in perm.split(","):
            checks.append(Check("key_permissions", "fail" if live else "warn",
                                f"у ключа есть право withdraw ({perm}) — нужен ключ read + trade"))
        else:
            checks.append(Check("key_permissions", "ok", perm))

        uid, main_uid = account_config.get("uid"), account_config.get("mainUid")
        if not uid or not main_uid:
            checks.append(Check("sub_account", "warn", "биржа не вернула uid/mainUid"))
        elif uid == main_uid:
            checks.append(Check("sub_account", strict,
                                "ключ главного аккаунта — для live нужен ключ суб-аккаунта кармана"))
        else:
            checks.append(Check("sub_account", "ok", "ключ суб-аккаунта"))

        ips = [x for x in str(account_config.get("ip") or "").split(",") if x.strip()]
        if ips:
            checks.append(Check("ip_whitelist", "ok", f"к ключу привязано IP: {len(ips)}"))
        else:
            status = ("fail" if require_ip else "warn") if live else "info"
            checks.append(Check("ip_whitelist", status,
                                "IP не привязан — без статического IP (DEPLOY-VPS) ключ без whitelist"))

        acct_lv = str(account_config.get("acctLv") or "")
        checks.append(Check("account_mode", "info",
                            f"acctLv={acct_lv} ({ACCT_LV.get(acct_lv, '?')}), "
                            f"posMode={account_config.get('posMode')}"))
        # Заём мимо риск-ядра (SPOT-TDMODE): карман — потолок убытка, только если займа нет
        try:
            mode = parse_account_mode(account_config)
        except ValueError as exc:
            checks.append(Check("spot_borrow", strict, f"{exc} — tdMode спота не выбрать"))
        else:
            if mode.can_borrow:
                checks.append(Check("spot_borrow", "fail" if live else "warn",
                                    f"спот-ордер может взять заём: {mode.borrow_reason} — выключите "
                                    "автозаём, иначе убыток не ограничен деньгами кармана"))
            else:
                checks.append(Check("spot_borrow", "ok", f"займа нет, tdMode спота={mode.spot_td_mode}"))

    # 7. Деньги суб-аккаунта против бюджета
    if equity_usdt is None:
        checks.append(Check("equity_vs_budget", strict if live else "warn", "equity не получен"))
    elif budget is None:
        checks.append(Check("equity_vs_budget", "info", f"equity {equity_usdt:.2f} USDT ({equity_source})"))
    elif equity_usdt > budget * BUDGET_TOLERANCE:
        checks.append(Check("equity_vs_budget", strict,
                            f"в аккаунте {equity_usdt:.2f} USDT > budget_usdt {budget:g} +5% — "
                            f"потолок убытка не совпадает с карманом ({equity_source})"))
    elif equity_usdt < budget * UNDERFUNDED_RATIO:
        checks.append(Check("equity_vs_budget", "warn",
                            f"в аккаунте {equity_usdt:.2f} USDT < половины budget_usdt {budget:g}"))
    else:
        checks.append(Check("equity_vs_budget", "ok", f"{equity_usdt:.2f} / {budget:g} USDT ({equity_source})"))

    # 8. Часы
    if drift_error:
        checks.append(Check("time_sync", "fail", drift_error))
    elif drift_ms is None:
        checks.append(Check("time_sync", "warn", "дрейф не измерен"))
    else:
        status = "ok" if abs(drift_ms) <= DRIFT_WARN_MS else "warn"
        checks.append(Check("time_sync", status, f"дрейф {drift_ms} мс"))

    # 9. Чужие ордера
    if foreign_orders is None:
        checks.append(Check("foreign_orders", "warn", "активные ордера не получены"))
    elif foreign_orders:
        checks.append(Check("foreign_orders", "warn" if live else "info",
                            f"активных ордеров не от кода проекта (clOrdId не {OWN_CLORD_PREFIX}*): {foreign_orders}"))
    else:
        checks.append(Check("foreign_orders", "ok", "чужих активных ордеров нет"))
    return checks


def fetch_pocket_equity(exchange: Any) -> tuple[Optional[float], str]:
    """Стоимость кармана в USDT и источник: asset-valuation (все счета аккаунта
    ключа, включая купленные монеты), иначе totalEq торгового счёта.

    Та же величина идёт в риск-ядро live-runner'а: USDT-баланс для этого не годится —
    каждая DCA-покупка выглядела бы просадкой. Средства нативных ботов в
    asset-valuation отдельного поля не имеют — вопрос EQUITY-TOTAL.
    """
    try:
        valuation = (exchange.private_get_asset_asset_valuation({"ccy": "USDT"}).get("data") or [{}])[0]
        return float(valuation["totalBal"]), "asset-valuation: все счета аккаунта ключа"
    except Exception as exc:
        log.warning("asset-valuation недоступен (%s) — беру totalEq торгового счёта", _short(exc))
    try:
        balance = (exchange.private_get_account_balance().get("data") or [{}])[0]
        return float(balance["totalEq"]), "account/balance totalEq: только торговый счёт"
    except Exception as exc:
        log.error("account/balance: %s", _short(exc))
    return None, ""


def collect(exchange: Any) -> dict:
    """Сетевые чтения (только GET). Ошибки — в поля *_error, не исключениями."""
    data: dict[str, Any] = {"account_config": None, "equity_usdt": None, "equity_source": "",
                            "drift_ms": None, "drift_error": None, "foreign_orders": None}
    try:
        data["account_config"] = (exchange.private_get_account_config().get("data") or [None])[0]
    except Exception as exc:
        log.error("account/config: %s", _short(exc))
    data["equity_usdt"], data["equity_source"] = fetch_pocket_equity(exchange)
    try:
        data["drift_ms"] = check_time_sync(exchange, max_drift_ms=DRIFT_FAIL_MS)
    except Exception as exc:
        data["drift_error"] = _short(exc)
    try:
        pending = fetch_pending_orders(exchange)
        data["foreign_orders"] = sum(1 for o in pending if not is_own_order(o.get("clOrdId")))
    except Exception as exc:
        log.error("orders-pending: %s", _short(exc))
    return data


def run_preflight(mode: str = "live", exchange: Any = None,
                  pocket_path: Path = LIVE_POCKET_PATH, policy_path: Path = LIVE_POLICY_PATH,
                  now: Optional[datetime] = None, save: bool = True) -> dict:
    """Полный прогон: файлы + биржа → отчёт {mode, ts, ok, checks}."""
    mode = normalize_mode(mode)
    checks: list[Check] = []
    try:
        policy: Optional[dict] = read_json(policy_path)
    except PocketError as exc:
        policy, checks = None, [Check("policy_file", "fail" if mode == "live" else "info", str(exc))]
    try:
        pocket: Optional[dict] = read_json(pocket_path)
        pocket_error = None
    except PocketError as exc:
        pocket, pocket_error = None, str(exc)

    if exchange is None:
        try:
            exchange = create_exchange(load_settings(mode))
        except Exception as exc:
            checks.append(Check("keys", "fail", _short(exc)))
    data = collect(exchange) if exchange is not None else {
        "account_config": None, "equity_usdt": None, "equity_source": "",
        "drift_ms": None, "drift_error": "биржа недоступна", "foreign_orders": None}
    checks += evaluate(mode, pocket=pocket, pocket_error=pocket_error, policy=policy, now=now, **data)

    report = {
        "mode": mode,
        "ts": (now or datetime.now(timezone.utc)).isoformat(),
        "ok": not any(c.status == "fail" for c in checks),
        "checks": [asdict(c) for c in checks],
    }
    if save:
        path = state_paths(mode).root / "preflight_last.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    return report


def main(argv=None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    parser = argparse.ArgumentParser(prog="python -m src.live_preflight", description=__doc__.splitlines()[0])
    parser.add_argument("--mode", choices=("live", "demo"), default="live")
    args = parser.parse_args(argv)
    report = run_preflight(args.mode)
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
