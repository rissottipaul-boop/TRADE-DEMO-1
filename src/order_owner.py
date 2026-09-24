"""Реестр владельцев ордеров (задача ORDER-OWNER-TAG).

    python -m src.order_owner              # таблица владельцев
    python -m src.order_owner new trd      # свежий clOrdId владельца (для okx CLI --clOrdId)
    python -m src.order_owner who <clOrdId> [--tag CLI]   # чей ордер

Метка владельца — ПРЕФИКС clOrdId (у algo-ордеров и ботов — algoClOrdId).
Только он надёжно доходит до биржи по всем каналам (insights/okx-api.md §10):
- CCXT 4.5 (ccxt/okx.py, create_order_request и sign): без переданного
  clOrdId сам ставит clOrdId = brokerId + uuid16 и tag = brokerId
  (`6b9ad766b55dBCDE`); переданный clOrdId уходит как есть, tag не трогается;
- okx CLI 1.4.8: `--clOrdId` у spot/swap/futures/option place (у `algo place`
  уходит как algoClOrdId), `--algoClOrdId` у `bot grid/dca create`. Флага
  `--tag` нет: tag всегда `CLI` (у MCP-ядра — `MCP`), а `--aiBuilderCode`
  подменяет tag кодом атрибуции OKX — для метки владельца не годится;
- ручной ордер человека из веб-интерфейса или приложения: clOrdId и tag пустые.

Правила префиксов:
- clOrdId — до 32 символов, только [a-z0-9] (connector.new_client_order_id);
- корень `bot` — код проекта, который пишет свои ордера в storage. Для
  реконсилятора и live-preflight это «свои» (reconciler.is_own_order =
  startswith("bot")). Движок ставит `bot` + uuid hex, подвладельцы — `bot` +
  НЕ-hex буква (g–z), поэтому их clOrdId не спутать с clOrdId движка;
- `botl*` — только live-карман (src.live_runner);
- остальные префиксы для реконсилятора внешние: тесты, агенты с CLI, человек.
Владелец по clOrdId — самый длинный зарегистрированный префикс.
"""
import argparse
import re
import sys
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Mapping, Optional

from .connector import new_client_order_id

OWN_ROOT = "bot"      # «свои» ордера кода проекта (= reconciler.OWN_CLORD_PREFIX)
LIVE_FAMILY = "botl"  # ордера live-кармана
MAX_CL_ORD_ID = 32
_CODE_RE = re.compile(r"^[a-z0-9]{2,8}$")
_HEX = frozenset("0123456789abcdef")

KINDS = ("engine", "strategy", "live", "test", "agent", "human")

# --- Коды владельцев (префиксы clOrdId) ---
ENGINE = "bot"          # движок: connector.new_client_order_id() по умолчанию
ROUTER = "botr"         # OrderRouter, стратегия не назвала владельца
DCA = "botsdca"         # DCA-бот demo (src.dca_bot)
DCA_DEMO_RUN = "bottdca"  # прогон DCA-бота src.dca_demo_run
LIVE_DCA = "botldca"    # рукав dca live-кармана (src.live_runner)
LOAD_TEST = "lt"        # src.load_test (P1-LOAD)
RATE_LIMIT_TEST = "rt"  # src.ratelimit_test (P1-RATELIMIT)
SMOKE_TEST = "smk"      # src.smoke_test
WS_DROP_TEST = "wsd"    # src.ws_drop_demo_run — ордеров не ставит, префикс зарезервирован
OKX_TRADER = "trd"      # агент OKX Trader (okx CLI)
PUMP = "pmp"            # агент Pump Risk Taker (okx CLI)
SENTINEL = "sen"        # агент Ops Sentinel (аварийные ордера)
EXECUTOR = "iex"        # агент Insight Executor (ручные проверки на demo)
HUNTER = "hnt"          # агент Crypto Insight Hunter (эксперименты)
HUMAN = "usr"           # человек — по желанию, если ставит ордер через CLI/скрипт


@dataclass(frozen=True)
class Owner:
    code: str    # префикс clOrdId
    owner: str   # кто ставит: модуль, скрипт или агент
    agent: str   # роль, которая отвечает за эти ордера (AGENTS.md §1)
    kind: str    # engine | strategy | live | test | agent | human
    note: str = ""

    @property
    def own(self) -> bool:
        """«Свой» для реконсилятора и live-preflight: код проекта, ордера в storage."""
        return self.code.startswith(OWN_ROOT)

    @property
    def live(self) -> bool:
        return self.code.startswith(LIVE_FAMILY)


OWNERS: tuple[Owner, ...] = (
    Owner(ENGINE, "Движок src/engine.py", "Insight Executor", "engine",
          "connector.new_client_order_id() по умолчанию; до ORDER-OWNER-TAG так же метил и OrderRouter"),
    Owner(ROUTER, "src/order_router.py — стратегия без своего владельца", "Insight Executor", "strategy"),
    Owner(DCA, "DCA-бот demo src/dca_bot.py", "Insight Executor", "strategy"),
    Owner(DCA_DEMO_RUN, "Прогон DCA-бота src/dca_demo_run.py", "Insight Executor", "test",
          "через OrderRouter: ордера пишутся в storage движка"),
    Owner(LIVE_DCA, "Рукав dca live-кармана src/live_runner.py", "Insight Executor", "live"),
    Owner(LOAD_TEST, "Нагрузочный тест src/load_test.py (P1-LOAD)", "Insight Executor", "test"),
    Owner(RATE_LIMIT_TEST, "Тест rate limit src/ratelimit_test.py (P1-RATELIMIT)", "Insight Executor", "test"),
    Owner(SMOKE_TEST, "Smoke-тест src/smoke_test.py", "Insight Executor", "test"),
    Owner(WS_DROP_TEST, "Обрыв WS src/ws_drop_demo_run.py", "Insight Executor", "test",
          "ордеров не ставит, префикс зарезервирован"),
    Owner(OKX_TRADER, "OKX Trader: okx --demo CLI", "OKX Trader", "agent"),
    Owner(PUMP, "Pump Risk Taker: okx --demo CLI", "Pump Risk Taker", "agent"),
    Owner(SENTINEL, "Ops Sentinel: аварийные ордера", "Ops Sentinel", "agent"),
    Owner(EXECUTOR, "Insight Executor: ручные проверки на demo", "Insight Executor", "agent"),
    Owner(HUNTER, "Crypto Insight Hunter: эксперименты на demo", "Crypto Insight Hunter", "agent"),
    Owner(HUMAN, "Человек: ордер через CLI или скрипт (по желанию)", "Human", "human",
          "ордера из веб-интерфейса и приложения OKX меток не имеют"),
)
_BY_CODE = {o.code: o for o in OWNERS}
# Коды с подвладельцами (сейчас только корень bot): их собственный clOrdId —
# код + uuid hex; код + не-hex буква — пространство подвладельцев
_PARENTS = frozenset(a.code for a in OWNERS
                     if any(b.code != a.code and b.code.startswith(a.code) for b in OWNERS))

# --- Маркеры каналов без метки владельца ---
CCXT_BROKER_ID = "6b9ad766b55dBCDE"  # ccxt 4.5.83 okx options.brokerId (сверяет тест)
CLI_TAG = "CLI"                      # okx CLI: tag = sourceTag
MCP_TAG = "MCP"                      # MCP-ядро okx trade kit: DEFAULT_SOURCE_TAG
# Ордера, которые ставит сама OKX: ликвидация, ADL, поставка, дельта-хедж
SYSTEM_CATEGORIES = frozenset({"adl", "full_liquidation", "partial_liquidation", "delivery", "ddh"})

# С этого момента правило AGENTS.md §6 действует. Ордера агентов и кода без
# метки, созданные раньше, — наследие: info, а не warning.
RULE_SINCE = datetime(2026, 9, 24, 10, 0, tzinfo=timezone(timedelta(hours=5)))
RULE_SINCE_MS = int(RULE_SINCE.timestamp() * 1000)

LEVEL_OK, LEVEL_INFO, LEVEL_WARNING = "ok", "info", "warning"


def validate_registry(owners: tuple[Owner, ...] = OWNERS) -> list[str]:
    """Нарушения правил реестра; пустой список — реестр корректен (проверяет тест)."""
    problems: list[str] = []
    codes = [o.code for o in owners]
    for code in sorted({c for c in codes if codes.count(c) > 1}):
        problems.append(f"код {code!r} зарегистрирован дважды")
    for o in owners:
        if not _CODE_RE.match(o.code):
            problems.append(f"{o.code!r}: код — 2–8 символов [a-z0-9]")
        if o.kind not in KINDS:
            problems.append(f"{o.code!r}: неизвестный kind {o.kind!r}")
        if o.kind in ("engine", "strategy", "live") and not o.own:
            problems.append(f"{o.code!r}: код проекта ({o.kind}) обязан начинаться с {OWN_ROOT!r}")
        if o.kind in ("agent", "human") and o.own:
            problems.append(f"{o.code!r}: ордера {o.kind} не в storage — префикс {OWN_ROOT!r} запрещён")
        if (o.kind == "live") != o.live:
            problems.append(f"{o.code!r}: префикс {LIVE_FAMILY!r} — только у kind=live")
    for a in codes:
        for b in codes:
            # a + uuid hex не должен совпасть с префиксом b: следующий символ b — не hex
            if a != b and b.startswith(a) and b[len(a)] in _HEX:
                problems.append(f"{b!r} продолжает {a!r} hex-символом {b[len(a)]!r} — "
                                f"clOrdId {a!r}+uuid будет принят за {b!r}")
    return problems


def get(code: str) -> Owner:
    """Владелец по коду; неизвестный код — KeyError."""
    try:
        return _BY_CODE[code]
    except KeyError:
        raise KeyError(f"владелец {code!r} не зарегистрирован в src/order_owner.py") from None


def require(code: str, *, own: Optional[bool] = None) -> Owner:
    """Проверка кода для пути выставления: зарегистрирован и нужного класса.

    own=True — только корень bot* (ордера пишутся в storage и сверяются
    реконсилятором). Ошибка конфигурации — ValueError до обращения к бирже.
    """
    try:
        owner = get(code)
    except KeyError as exc:
        raise ValueError(str(exc)) from None
    if own is not None and owner.own != own:
        need = f"с корнем {OWN_ROOT!r}" if own else f"без корня {OWN_ROOT!r}"
        raise ValueError(f"владелец {code!r} не подходит: нужен код {need}")
    return owner


def new_cl_ord_id(code: str) -> str:
    """Свежий clOrdId владельца: код + uuid hex, всего 32 символа [a-z0-9]."""
    return new_client_order_id(get(code).code)


def owner_of(cl_ord_id: Optional[str]) -> Optional[Owner]:
    """Владелец по clOrdId/algoClOrdId: самый длинный зарегистрированный префикс.

    У кода с подвладельцами (корень bot) после префикса должен идти hex (uuid):
    `bots…` без регистрации — не движок, а неизвестный подвладелец (None).
    """
    if not cl_ord_id:
        return None
    best: Optional[Owner] = None
    for o in OWNERS:
        if cl_ord_id.startswith(o.code) and (best is None or len(o.code) > len(best.code)):
            best = o
    if best is not None and best.code in _PARENTS:
        rest = cl_ord_id[len(best.code):]
        if rest and rest[0] not in _HEX:
            return None
    return best


def is_owned_by(cl_ord_id: Optional[str], code: str) -> bool:
    """Ордер принадлежит именно этому владельцу (а не подвладельцу с тем же началом)."""
    owner = owner_of(cl_ord_id)
    return owner is not None and owner.code == code


@dataclass(frozen=True)
class Attribution:
    status: str    # owned | unmarked | system | cli | mcp | ccxt_default | unknown_prefix | unknown_tag
    level: str     # ok | info | warning
    owner: Optional[Owner]
    marker: str    # поле, по которому опознан: clOrdId | algoClOrdId | tag | category | ""
    value: str     # значение этого поля
    reason: str
    legacy: bool = False  # создан до RULE_SINCE — warning понижен до info


_LEGACY_STATUSES = frozenset({"cli", "mcp", "ccxt_default", "unknown_prefix", "unknown_tag"})


def _ms(value: Any) -> Optional[int]:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def classify(order: Mapping[str, Any], *, human_trades: bool = True,
             rule_since_ms: int = RULE_SINCE_MS) -> Attribution:
    """Чей ордер — по сырому dict OKX (clOrdId, algoClOrdId, tag, category, cTime).

    human_trades=True (demo, где человек торгует на том же счёте): ордер без
    меток — info «вероятно, ручной ордер человека». False (live-суб-аккаунт
    кармана, где торгует только код) — warning.
    """
    category = (order.get("category") or "").strip()
    if category in SYSTEM_CATEGORIES:
        return Attribution("system", LEVEL_WARNING, None, "category", category,
                           f"ордер поставила OKX (category={category}) — проверить позицию и маржу")

    result: Optional[Attribution] = None
    for marker in ("clOrdId", "algoClOrdId"):
        value = order.get(marker) or ""
        owner = owner_of(value)
        if owner is not None:
            return Attribution("owned", LEVEL_OK, owner, marker, value, owner.owner)
        if value and result is None:
            if value.startswith(CCXT_BROKER_ID):
                result = Attribution("ccxt_default", LEVEL_WARNING, None, marker, value,
                                     "код через CCXT без своего clOrdId (CCXT подставил brokerId)")
            elif value.startswith(OWN_ROOT):
                result = Attribution("unknown_prefix", LEVEL_WARNING, None, marker, value,
                                     f"корень {OWN_ROOT}*, но подвладелец не зарегистрирован "
                                     "в src/order_owner.py")
            else:
                result = Attribution("unknown_prefix", LEVEL_WARNING, None, marker, value,
                                     "префикс clOrdId не зарегистрирован в src/order_owner.py")

    if result is None:
        tag = order.get("tag") or ""
        if not tag:
            if human_trades:
                return Attribution("unmarked", LEVEL_INFO, None, "", "",
                                   "без clOrdId и tag — вероятно, ручной ордер человека (веб/приложение)")
            return Attribution("unmarked", LEVEL_WARNING, None, "", "",
                               "без clOrdId и tag — ордер не от кода проекта")
        if tag in _BY_CODE:
            owner = _BY_CODE[tag]
            return Attribution("owned", LEVEL_OK, owner, "tag", tag, owner.owner)
        if tag == CLI_TAG:
            result = Attribution("cli", LEVEL_WARNING, None, "tag", tag,
                                 "okx CLI без префикса владельца: не передан --clOrdId (AGENTS.md §6)")
        elif tag == MCP_TAG:
            result = Attribution("mcp", LEVEL_WARNING, None, "tag", tag,
                                 "okx MCP без префикса владельца: не передан clOrdId")
        elif tag == CCXT_BROKER_ID:
            result = Attribution("ccxt_default", LEVEL_WARNING, None, "tag", tag,
                                 "код через CCXT без своего clOrdId (CCXT подставил brokerId)")
        else:
            result = Attribution("unknown_tag", LEVEL_WARNING, None, "tag", tag,
                                 "неизвестный tag, clOrdId пуст")

    created = _ms(order.get("cTime"))
    if result.status in _LEGACY_STATUSES and created is not None and created < rule_since_ms:
        return Attribution(result.status, LEVEL_INFO, None, result.marker, result.value,
                           result.reason + " — до правила ORDER-OWNER-TAG", legacy=True)
    return result


def _table() -> str:
    rows = [f"{'код':<8} {'kind':<9} {'свой':<5} владелец / агент"]
    for o in OWNERS:
        rows.append(f"{o.code:<8} {o.kind:<9} {'да' if o.own else '—':<5} {o.owner} / {o.agent}"
                    + (f" ({o.note})" if o.note else ""))
    return "\n".join(rows)


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m src.order_owner",
                                     description="Реестр владельцев ордеров (ORDER-OWNER-TAG)")
    sub = parser.add_subparsers(dest="cmd")
    new = sub.add_parser("new", help="напечатать свежий clOrdId владельца")
    new.add_argument("code", help="код владельца, например trd")
    who = sub.add_parser("who", help="чей ордер по clOrdId и tag")
    who.add_argument("cl_ord_id", nargs="?", default="")
    who.add_argument("--tag", default="")
    args = parser.parse_args(argv)
    if args.cmd == "new":
        try:
            print(new_cl_ord_id(args.code))
        except KeyError as exc:
            print(exc.args[0], file=sys.stderr)
            return 2
        return 0
    if args.cmd == "who":
        att = classify({"clOrdId": args.cl_ord_id, "tag": args.tag})
        who_str = f"{att.owner.code} — {att.owner.owner}" if att.owner else "владелец не опознан"
        print(f"{att.level}: {who_str}; {att.reason}")
        return 0
    print(_table())
    return 0


if __name__ == "__main__":
    sys.exit(main())
