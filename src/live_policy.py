"""Периметр live-кармана: окно `ops/live-policy.json` и лимиты `ops/live-pocket.json`.

Оба файла меняет только человек. Live-процессы (src.live_preflight,
src.live_runner) читают их перед КАЖДЫМ входом: guard перехватывает только
вызовы инструментов агентов, а фоновый процесс, запущенный внутри окна, иначе
продолжил бы торговать после его закрытия (insights/business-plan.md §3, блокер №4).

Окно здесь строже, чем в guard: без `enabled_until` оно считается закрытым —
у live-окна всегда есть дата окончания (инвариант business-plan.md §7).
Шаблон кармана — business-plan.md §5 и ops/live-pocket.example.json.
"""
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

LIVE_POLICY_PATH = Path("ops/live-policy.json")
LIVE_POCKET_PATH = Path("ops/live-pocket.json")

# Рукава, которые умеет live_runner; остальные включаются своими задачами
# (NATIVE-GRID-WRAP, CARRY-IMPL, ...) — до этого карман с ними невалиден.
SUPPORTED_SLEEVES = ("dca",)
MIN_ORDER_USDT = 1.0        # как dca_bot.DEFAULT_MIN_NOTIONAL_USDT
MIN_DCA_INTERVAL_HOURS = 1.0


class PocketError(ValueError):
    """Файл политики или кармана отсутствует, не читается или невалиден."""


def read_json(path: Path) -> dict:
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise PocketError(f"нет файла {path}") from None
    except (OSError, ValueError) as exc:
        raise PocketError(f"{path} не читается: {exc}") from None
    if not isinstance(data, dict):
        raise PocketError(f"{path}: ожидается JSON-объект")
    return data


def live_window(policy: dict, now: Optional[datetime] = None) -> tuple[bool, str]:
    """(открыто ли live-окно, пояснение)."""
    if policy.get("live_enabled") is not True:
        return False, "live_enabled != true"
    until = policy.get("enabled_until")
    if not until:
        return False, "enabled_until не задан — у live-окна должна быть дата окончания"
    try:
        deadline = datetime.fromisoformat(str(until))
    except ValueError:
        return False, f"enabled_until не ISO-время: {until!r}"
    if deadline.tzinfo is None:
        deadline = deadline.replace(tzinfo=timezone.utc)
    if (now or datetime.now(timezone.utc)) >= deadline:
        return False, f"окно закрылось {deadline.isoformat()}"
    return True, f"окно открыто до {deadline.isoformat()}"


def _num(value: Any) -> Optional[float]:
    if isinstance(value, bool):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _validate_dca(cfg: dict) -> list[str]:
    errors = []
    inst = cfg.get("inst_id")
    if not (isinstance(inst, str) and inst.endswith("/USDT") and len(inst) > 5):
        errors.append("sleeves.dca.inst_id: ожидается спот-пара вида BTC/USDT")
    per_buy = _num(cfg.get("quote_per_buy_usdt"))
    if per_buy is None or per_buy < MIN_ORDER_USDT:
        errors.append(f"sleeves.dca.quote_per_buy_usdt должен быть >= {MIN_ORDER_USDT:g}")
    interval = _num(cfg.get("interval_hours"))
    if interval is None or interval < MIN_DCA_INTERVAL_HOURS:
        errors.append(f"sleeves.dca.interval_hours должен быть >= {MIN_DCA_INTERVAL_HOURS:g}")
    cap = _num(cfg.get("max_total_usdt"))
    if cap is None or cap <= 0:
        errors.append("sleeves.dca.max_total_usdt должен быть > 0")
    elif per_buy is not None and per_buy > cap:
        errors.append("sleeves.dca.quote_per_buy_usdt больше max_total_usdt")
    return errors


def validate_pocket(pocket: dict) -> list[str]:
    """Список ошибок кармана; пустой — карман валиден."""
    errors = []
    if pocket.get("example"):
        errors.append("это шаблон (example: true) — создайте ops/live-pocket.json со своими числами")
    if pocket.get("mode") != "live":
        errors.append('mode должен быть "live"')
    budget = _num(pocket.get("budget_usdt"))
    if budget is None or budget <= 0:
        errors.append("budget_usdt должен быть > 0")
    sleeves = pocket.get("sleeves")
    if not isinstance(sleeves, dict):
        return errors + ["sleeves: ожидается объект"]
    caps = 0.0
    for name, cfg in sleeves.items():
        if not isinstance(cfg, dict):
            errors.append(f"sleeves.{name}: ожидается объект")
            continue
        if not cfg.get("enabled"):
            continue
        if name not in SUPPORTED_SLEEVES:
            errors.append(f"sleeves.{name}: рукав пока не поддерживается live_runner "
                          f"(доступны: {', '.join(SUPPORTED_SLEEVES)})")
            continue
        errors += _validate_dca(cfg)
        caps += _num(cfg.get("max_total_usdt")) or 0.0
    if budget and caps > budget:
        errors.append(f"сумма лимитов рукавов {caps:g} больше budget_usdt {budget:g}")
    return errors


def load_pocket(path: Path = LIVE_POCKET_PATH) -> dict:
    """Валидный карман или PocketError со всеми ошибками."""
    pocket = read_json(path)
    errors = validate_pocket(pocket)
    if errors:
        raise PocketError("; ".join(errors))
    return pocket


def enabled_sleeve(pocket: dict, name: str) -> Optional[dict]:
    cfg = (pocket.get("sleeves") or {}).get(name)
    return cfg if isinstance(cfg, dict) and cfg.get("enabled") else None
