"""Copy trading / лид-боты: готовность, доля, мониторинг (INSIGHTS-ALL-IMPL).

Контракт — insights/copy-trader-spec.md; факты — insights/copy-trading.md:
- доля лида Futures/Spot без уровня ≤ 10%, уровни 10/13/15/30%;
- сегмент Bot (лид-боты grid/DCA): плоские 30% на любом уровне;
- расчёт — понедельник 00:00 UTC+8, только чистая прибыль подписчика;
- порог счёта > 500 USDT; лимит ≤ 500 buy-ордеров/сутки (пекинские);
- carry из двух ног не копируется — запрет; spot_dca — предупреждение.

Модуль готовит и проверяет, НЕ заявляет: подача заявки лид-трейдера,
смена региона/KYC, публикация профиля — действия человека (AGENTS.md §2).
Сеть — только чтение (мониторинг через переданный exchange-объект).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Sequence

LEAD_BOT_RATIO = 0.30  # плоская доля сегмента Bot
LEAD_RATIOS = frozenset({0.0, 0.1, 0.2, 0.3})  # допустимые profitSharingRatio
ACCOUNT_FLOOR_USDT = 500.0  # порог — строго больше
BUY_LIMIT_PER_DAY = 500
BUY_WARN_AT = 450
BEIJING = timezone(timedelta(hours=8))

# Регионы без copy trading (§1 инсайта). UK → код GB; RU в списке НЕТ.
BAN_LIST = frozenset({"HK", "SG", "CU", "IR", "KP", "MY", "SY", "US", "CA",
                      "GB", "BD", "BO", "MT"})


@dataclass(frozen=True)
class LeadReadiness:
    """Входные флаги готовности к лидированию (§2 спеки)."""

    strategy_gate: bool  # гейт бэктеста + 14 дней demo пройдены
    live_track_days: int  # дней live-трека (цель 90–180 до заявки)
    account_equity_usdt: float  # > 500
    region_ok: bool  # страна KYC не в ban-листе (флаг от человека)
    sub_account_verified_by_human: bool  # открыт, ответ только из UI
    kyc_done_by_human: bool


def check_lead_readiness(r: LeadReadiness) -> list[str]:
    """[] = готов; иначе список блокеров (§3 пп.2–3). Fail-closed."""
    blockers: list[str] = []
    if not r.strategy_gate:
        blockers.append("стратегия не прошла гейт бэктеста + 14 дней demo")
    if r.live_track_days <= 0:
        blockers.append("нет live-трека (цель 90–180 дней до заявки)")
    if r.account_equity_usdt <= ACCOUNT_FLOOR_USDT:
        blockers.append(f"equity {r.account_equity_usdt}: порог — строго > 500 USDT")
    if not r.region_ok:
        blockers.append("регион в ban-листе или неизвестен (§1)")
    if not r.sub_account_verified_by_human:
        blockers.append("суб-аккаунт не подтверждён человеком из UI (fail-closed)")
    if not r.kyc_done_by_human:
        blockers.append("KYC не подтверждён человеком")
    return blockers


def is_banned_region(country_code: str) -> bool:
    """Страна в ban-листе copy trading (§1)."""
    return country_code.strip().upper() in BAN_LIST


def validate_lead_bot_ratio(value: float) -> float:
    """Только {0, 0.1, 0.2, 0.3}; до лидирования — 0."""
    if value not in LEAD_RATIOS:
        raise ValueError(f"profitSharingRatio {value}: допустимы {sorted(LEAD_RATIOS)}")
    return value


def validate_copy_strategy(kind: str) -> list[str]:
    """Пригодность стратегии к копированию; 'BLOCK: …' = запрет (§3 п.5)."""
    if kind == "carry":
        return ["BLOCK: carry из двух ног не копируется — у подписчика останется одна нога"]
    if kind == "spot_dca":
        return ["WARN: технически копируется, но подписчикам неинтересен"]
    if kind == "signal":
        return ["WARN: нет profitSharingRatio — лид-ботом не сделать (не подтверждено)"]
    if kind in ("grid", "contract_grid", "contract_dca", "spot_grid"):
        return []
    return [f"BLOCK: неизвестный вид стратегии {kind!r}"]


def is_blocked(notes: Sequence[str]) -> bool:
    """Есть ли запрет среди заметок validate_copy_strategy."""
    return any(n.startswith("BLOCK:") for n in notes)


def lead_economics(sub_aum: float, sub_return: float, ratio: float) -> float:
    """Доход лида = AUM × доходность × доля; при убытке подписчиков — 0 (§3 п.9)."""
    if sub_return <= 0:
        return 0.0
    return sub_aum * sub_return * ratio


def week_slice_start(ts: datetime) -> datetime:
    """Начало расчётной недели: понедельник 00:00 UTC+8 (§1)."""
    bj = ts.astimezone(BEIJING)
    monday = bj - timedelta(days=bj.weekday())
    return monday.replace(hour=0, minute=0, second=0, microsecond=0)


def profit_share_due(net_profits: Sequence[float], ratio: float) -> float:
    """Доля с чистой прибыли недели (взаимозачёт сделок, комиссии уже вычтены)."""
    total = sum(net_profits)
    if total <= 0:
        return 0.0
    return total * ratio


@dataclass
class BuyCounter:
    """Счётчик buy-ордеров лид-аккаунта в пекинских сутках (лимит 500)."""

    count: int = 0
    day: str = ""  # YYYY-MM-DD в UTC+8
    _now: Callable[[], datetime] = field(
        default=lambda: datetime.now(timezone.utc), repr=False, compare=False)

    def _today(self) -> str:
        return self._now().astimezone(BEIJING).strftime("%Y-%m-%d")

    def register_buy(self, n: int = 1) -> list[str]:
        """Учесть n покупок; ≤500 — [], превышение — ['BLOCK: …'] до 00:00 CST."""
        today = self._today()
        if today != self.day:
            self.day, self.count = today, 0
        self.count += n
        if self.count > BUY_LIMIT_PER_DAY:
            return [f"BLOCK: {self.count} > {BUY_LIMIT_PER_DAY} buy/сутки — "
                    "новые входы запрещены до 00:00 CST"]
        return []

    def pressure(self) -> list[str]:
        """Предупреждение мониторинга при ≥450 buy/сутки (§4 аномалии)."""
        if self._today() != self.day:
            return []
        if self.count >= BUY_WARN_AT:
            return [f"WARN: {self.count} buy/сутки — близко к лимиту "
                    f"{BUY_LIMIT_PER_DAY}"]
        return []


def monitor_subpositions(exchange: Any) -> dict:
    """Read-only срез current-subpositions; ошибка сети → verdict 'unknown'."""
    try:
        raw = exchange.fetch_copy_subpositions()
    except Exception as exc:  # сеть/авторизация — вердикт, не исключение наружу
        return {"verdict": "unknown", "error": f"{type(exc).__name__}: {exc}"}
    positions = raw if isinstance(raw, list) else raw.get("positions", [])
    aum = sum(float(p.get("notional", 0) or 0) for p in positions)
    followers = {str(p.get("followerId", "?")) for p in positions}
    no_stop = [p.get("subPosId", "?") for p in positions if not p.get("stopPx")]
    verdict = "warn" if no_stop else "ok"
    return {"verdict": verdict, "aum_usdt": aum,
            "followers": len(followers), "open": len(positions),
            "no_stop": no_stop}
