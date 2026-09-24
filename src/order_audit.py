"""Аудит владельцев ордеров (задача ORDER-OWNER-TAG) — только чтение.

    python -m src.order_audit                # demo: активные ордера + история за 24 ч
    python -m src.order_audit --hours 72     # до 168 ч — orders-history, дольше (до 90 дней) — archive
    python -m src.order_audit --json         # машинный отчёт (для Ops Sentinel и PNL-LEDGER)
    python -m src.order_audit --verbose      # info и legacy — построчно, без сокращений

Что читает (только GET):
- orders-pending — все активные обычные ордера (connector.fetch_pending_orders);
- orders-algo-pending — активные algo-ордера (connector.fetch_pending_algos);
- orders-history (orders-history-archive для периода > 7 дней) по каждому instType.
  Листаем без `begin`: от новых к старым курсором `after` до первого ордера старше
  начала периода. С `begin` OKX отдаёт страницы от старых к новым, и `after`
  повторяет уже полученные (demo, 24.09) — см. insights/okx-api.md §10.
Суб-ордера нативных grid/DCA-ботов OKX в orders-history не попадают (demo, 24.09):
ботов этот аудит не проверяет.

Классификация — src.order_owner.classify:
- свой префикс clOrdId/algoClOrdId → группа владельца (ok);
- без clOrdId и tag → info «вероятно, ручной ордер человека»: пока человек
  торгует на том же demo-счёте, это не инцидент (OKB-SELL-OWNER, 24.09);
- неизвестный префикс; okx CLI или MCP без --clOrdId (tag CLI/MCP); код через
  CCXT без своего clOrdId (brokerId 6b9ad…); системный ордер OKX (ликвидация,
  ADL) → warning. Ордера без метки, созданные до order_owner.RULE_SINCE, — info
  с пометкой legacy (наследие до правила).
--mode live: торгует только код, ордер без меток — тоже warning.

Коды выхода: 0 — warning нет (info не в счёт), 1 — есть warning, 2 — ошибка чтения API.
"""
import argparse
import json
import logging
import sys
import time
from collections import Counter
from datetime import datetime
from typing import Any, Iterable, Optional

from . import order_owner
from .config import load_settings
from .connector import create_exchange, fetch_pending_algos, fetch_pending_orders

log = logging.getLogger("okx.order_audit")

INST_TYPES = ("SPOT", "MARGIN", "SWAP", "FUTURES", "OPTION")
HISTORY_HOURS = 7 * 24      # orders-history хранит 7 дней
ARCHIVE_HOURS = 90 * 24     # orders-history-archive — 3 месяца
PAGE_LIMIT = 100
MAX_PAGES = 200             # предохранитель: не больше 20 000 ордеров на instType
DEFAULT_HOURS = 24.0
LIST_LIMIT = 30             # строк на раздел без --verbose

DETAIL_KEYS = ("ordId", "algoId", "clOrdId", "algoClOrdId", "tag", "instId", "side", "ordType",
               "sz", "px", "state", "category", "cTime")


def _ms(value: Any) -> Optional[int]:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _short(exc: BaseException) -> str:
    return f"{type(exc).__name__}: {str(exc)[:300]}"


def fetch_history(exchange: Any, inst_type: str, begin_ms: int,
                  archive: bool = False) -> tuple[list[dict], bool]:
    """Завершённые ордера instType с cTime >= begin_ms. Возвращает (ордера, truncated).

    Без параметра begin: от новых к старым, курсор after = ordId последнего;
    стоп — короткая страница или ордер старше begin_ms.
    """
    fetch = (exchange.private_get_trade_orders_history_archive if archive
             else exchange.private_get_trade_orders_history)
    result: list[dict] = []
    after: Optional[str] = None
    for _ in range(MAX_PAGES):
        params = {"instType": inst_type, "limit": str(PAGE_LIMIT)}
        if after:
            params["after"] = after
        page = fetch(params).get("data") or []
        for order in page:
            created = _ms(order.get("cTime"))
            if created is not None and created < begin_ms:
                return result, False
            result.append(order)
        if len(page) < PAGE_LIMIT:
            return result, False
        after = page[-1]["ordId"]
    return result, True


def collect(exchange: Any, begin_ms: int, inst_types: Iterable[str] = INST_TYPES,
            archive: bool = False) -> dict:
    """Все ордера для аудита: активные, algo, история. Ошибка источника — в errors."""
    orders: list[dict] = []
    errors: list[dict] = []
    truncated: list[str] = []
    counts = Counter()
    seen: set[str] = set()

    def add(items: list[dict], source: str, id_key: str) -> None:
        for item in items:
            key = f"{id_key}:{item.get(id_key)}"
            if key in seen:
                continue
            seen.add(key)
            orders.append(dict(item, _source=source))
            counts[source] += 1

    try:
        add(fetch_pending_orders(exchange), "pending", "ordId")
    except Exception as exc:  # не глушим: в отчёт и в код выхода 2
        log.error("orders-pending: %s", _short(exc))
        errors.append({"source": "orders-pending", "error": _short(exc)})
    try:
        add(fetch_pending_algos(exchange), "algo_pending", "algoId")
    except Exception as exc:
        log.error("orders-algo-pending: %s", _short(exc))
        errors.append({"source": "orders-algo-pending", "error": _short(exc)})
    endpoint = "orders-history-archive" if archive else "orders-history"
    for inst_type in inst_types:
        try:
            items, cut = fetch_history(exchange, inst_type, begin_ms, archive)
        except Exception as exc:
            log.error("%s %s: %s", endpoint, inst_type, _short(exc))
            errors.append({"source": f"{endpoint} {inst_type}", "error": _short(exc)})
            continue
        add(items, "history", "ordId")
        if cut:
            truncated.append(inst_type)
    return {"orders": orders, "errors": errors, "truncated": truncated, "counts": dict(counts)}


def _row(order: dict, att: order_owner.Attribution) -> dict:
    row = {k: order[k] for k in DETAIL_KEYS if order.get(k) not in (None, "")}
    row.update(source=order.get("_source", ""), status=att.status, level=att.level,
               marker=att.marker, reason=att.reason)
    if att.legacy:
        row["legacy"] = True
    return row


def audit(orders: list[dict], *, human_trades: bool = True,
          rule_since_ms: int = order_owner.RULE_SINCE_MS) -> dict:
    """Группировка по владельцам + списки info/legacy/warning (без сети)."""
    by_owner: dict[str, dict] = {}
    unmarked: list[dict] = []
    legacy: list[dict] = []
    warnings: list[dict] = []
    for order in orders:
        att = order_owner.classify(order, human_trades=human_trades, rule_since_ms=rule_since_ms)
        if att.owner is not None:
            group = by_owner.setdefault(att.owner.code, {
                "owner": att.owner.owner, "agent": att.owner.agent, "kind": att.owner.kind,
                "own": att.owner.own, "orders": 0, "active": 0, "states": Counter(),
                "inst_ids": Counter(), "first_ms": None, "last_ms": None,
            })
            group["orders"] += 1
            if order.get("_source") in ("pending", "algo_pending"):
                group["active"] += 1
            group["states"][order.get("state") or "?"] += 1
            group["inst_ids"][order.get("instId") or "?"] += 1
            created = _ms(order.get("cTime"))
            if created is not None:
                group["first_ms"] = created if group["first_ms"] is None else min(group["first_ms"], created)
                group["last_ms"] = created if group["last_ms"] is None else max(group["last_ms"], created)
            continue
        row = _row(order, att)
        if att.level == order_owner.LEVEL_WARNING:
            warnings.append(row)
        elif att.legacy:
            legacy.append(row)
        else:
            unmarked.append(row)
    for group in by_owner.values():
        group["states"] = dict(group["states"])
        group["inst_ids"] = dict(group["inst_ids"])
    owned = sum(g["orders"] for g in by_owner.values())
    return {
        "by_owner": dict(sorted(by_owner.items(), key=lambda kv: -kv[1]["orders"])),
        "unmarked": unmarked,
        "legacy": legacy,
        "warnings": warnings,
        "summary": {"total": len(orders), "ok": owned, "info": len(unmarked) + len(legacy),
                    "warning": len(warnings)},
    }


def run_audit(exchange: Any, hours: float = DEFAULT_HOURS, *, mode: str = "demo",
              now_ms: Optional[int] = None, inst_types: Iterable[str] = INST_TYPES) -> dict:
    """Полный отчёт: сбор (только GET) + классификация."""
    hours = max(0.0, min(float(hours), float(ARCHIVE_HOURS)))
    now_ms = int(time.time() * 1000) if now_ms is None else now_ms
    begin_ms = now_ms - int(hours * 3600 * 1000)
    archive = hours > HISTORY_HOURS
    data = collect(exchange, begin_ms, inst_types, archive)
    report = audit(data["orders"], human_trades=(mode != "live"))
    report.update(
        mode=mode,
        period={"begin_ms": begin_ms, "end_ms": now_ms, "hours": hours,
                "history": "orders-history-archive" if archive else "orders-history"},
        read=data["counts"],
        errors=data["errors"],
        truncated=data["truncated"],
        rule_since=order_owner.RULE_SINCE.isoformat(),
    )
    return report


def exit_code(report: dict) -> int:
    if report.get("errors"):
        return 2
    return 1 if report["summary"]["warning"] else 0


def _fmt_ms(ms: Optional[int]) -> str:
    if ms is None:
        return "?"
    return datetime.fromtimestamp(ms / 1000).astimezone().strftime("%Y-%m-%d %H:%M:%S")


def _fmt_order(row: dict) -> str:
    oid = row.get("ordId") or f"algo {row.get('algoId')}"
    px = f" @{row['px']}" if row.get("px") else ""
    marks = " ".join(f"{k}={row[k]}" for k in ("clOrdId", "algoClOrdId", "tag") if row.get(k))
    return (f"{_fmt_ms(_ms(row.get('cTime')))} {row.get('instId', '?')} {row.get('side', '?')} "
            f"{row.get('ordType', '?')} {row.get('sz', '?')}{px} {row.get('state', '?')} "
            f"{oid} [{row.get('source')}] {marks}".rstrip())


def _section(title: str, rows: list[dict], verbose: bool, lines: list[str]) -> None:
    lines.append(f"{title} ({len(rows)})" + (":" if rows else ": нет"))
    shown = rows if verbose else rows[:LIST_LIMIT]
    for row in shown:
        lines.append(f"  {_fmt_order(row)}")
        lines.append(f"      {row['reason']}")
    if len(rows) > len(shown):
        lines.append(f"  … ещё {len(rows) - len(shown)} (--verbose)")


def render_text(report: dict, verbose: bool = False) -> str:
    period = report["period"]
    read = report.get("read", {})
    lines = [
        f"Аудит владельцев ордеров ({report['mode']}): {_fmt_ms(period['begin_ms'])} — "
        f"{_fmt_ms(period['end_ms'])}, {period['hours']:g} ч, {period['history']}",
        f"Прочитано: активных {read.get('pending', 0)}, algo {read.get('algo_pending', 0)}, "
        f"из истории {read.get('history', 0)}; ошибок чтения {len(report['errors'])}"
        + (f"; обрезано (больше {MAX_PAGES} страниц): {', '.join(report['truncated'])}"
           if report["truncated"] else ""),
        "Свои по префиксу clOrdId:" if report["by_owner"] else "Свои по префиксу clOrdId: нет",
    ]
    for code, group in report["by_owner"].items():
        insts = ", ".join(f"{k} {v}" for k, v in sorted(group["inst_ids"].items(), key=lambda kv: -kv[1]))
        states = ", ".join(f"{k} {v}" for k, v in sorted(group["states"].items()))
        lines.append(f"  {code:<8} {group['orders']:>5}  {group['owner']} — {insts}; {states}"
                     + (f"; активных {group['active']}" if group["active"] else "")
                     + f"; {_fmt_ms(group['first_ms'])} … {_fmt_ms(group['last_ms'])}")
    _section("INFO — без метки, вероятно ручной ордер человека", report["unmarked"], verbose, lines)
    legacy = report["legacy"]
    if verbose:
        _section(f"INFO legacy — без метки до правила ({report['rule_since']})", legacy, True, lines)
    else:
        kinds = Counter(f"{r['status']} ({r['marker']}={r.get(r['marker'], '')})"
                        if r["marker"] in ("tag",) else r["status"] for r in legacy)
        lines.append(f"INFO legacy — без метки до правила ({report['rule_since']}) ({len(legacy)})"
                     + (": " + ", ".join(f"{k} {v}" for k, v in kinds.most_common()) if legacy else ": нет"))
    _section("WARNING — ордер без метки владельца", report["warnings"], verbose, lines)
    for err in report["errors"]:
        lines.append(f"ОШИБКА чтения {err['source']}: {err['error']}")
    s = report["summary"]
    lines.append(f"Итог: всего {s['total']}, свои {s['ok']}, info {s['info']}, warning {s['warning']} "
                 f"→ exit {exit_code(report)}")
    return "\n".join(lines)


def main(argv: Optional[list[str]] = None) -> int:
    logging.basicConfig(level=logging.WARNING, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    parser = argparse.ArgumentParser(prog="python -m src.order_audit", description=__doc__.splitlines()[0])
    parser.add_argument("--hours", type=float, default=DEFAULT_HOURS,
                        help=f"период истории, ч (по умолчанию {DEFAULT_HOURS:g}; до {ARCHIVE_HOURS})")
    parser.add_argument("--mode", choices=("demo", "live"), default="demo",
                        help="счёт: demo (по умолчанию) или live-суб-аккаунт кармана")
    parser.add_argument("--json", action="store_true", help="отчёт JSON")
    parser.add_argument("--verbose", action="store_true", help="все строки info/legacy/warning")
    args = parser.parse_args(argv)
    try:
        exchange = create_exchange(load_settings(args.mode))
    except Exception as exc:
        print(f"Не удалось создать клиент {args.mode}: {_short(exc)}", file=sys.stderr)
        return 2
    report = run_audit(exchange, args.hours, mode=args.mode)
    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2))
    else:
        print(render_text(report, verbose=args.verbose))
    return exit_code(report)


if __name__ == "__main__":
    sys.exit(main())
