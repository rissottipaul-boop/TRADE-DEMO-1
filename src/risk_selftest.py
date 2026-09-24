"""Сценарный self-test риск-ядра src/risk.py (задача p1-risk-module).

Запуск из корня проекта: .venv\\Scripts\\python.exe -m src.risk_selftest

Сценарии:
1. Вход разрешён в норме.
2. size_position: BTC, equity 10000, риск 1%, вход 84000, стоп 82000.
3. record_pnl с убытком -6% -> дневной breaker блокирует check_entry_allowed.
4. trip_breaker(global) блокирует всё до reset_breaker('global').
5. Три убытка подряд по инструменту -> block_instrument (cooldown 24ч).
6. Состояние переживает рестарт (re-init на том же файле БД).
"""
import logging
import sys
from datetime import datetime, timezone
from pathlib import Path

from src import risk

logging.basicConfig(level=logging.CRITICAL)  # события риска не засоряют вывод

TEST_DB = Path("data/risk_selftest.db")
_passed = 0


def check(name: str, cond: bool, detail: str = "") -> None:
    global _passed
    if not cond:
        print(f"FAIL: {name} {detail}")
        sys.exit(1)
    _passed += 1
    print(f"OK: {name}")


def main() -> None:
    if TEST_DB.exists():
        TEST_DB.unlink()
    risk.init(TEST_DB)
    risk.update_equity(10000.0)

    # --- 1. Вход разрешён в норме ---
    allowed, reason = risk.check_entry_allowed("BTC-USDT-SWAP", "buy")
    check("1. вход разрешён в норме", allowed, reason)

    # --- 2. Сайзинг BTC: equity 10000, риск 1%, вход 84000, стоп 82000 ---
    # dollar_risk = 100, risk_per_contract = 2000*0.01 = 20 -> 5 контрактов,
    # но потолок 15% equity: floor(1500/(84000*0.01)) = 1 контракт.
    res = risk.size_position(equity=10000.0, entry=84000.0, stop=82000.0,
                             ct_val=0.01, lot_sz=0.01, min_sz=0.01, risk_pct=1.0)
    check("2a. dollar_risk = 1% equity", abs(res["dollar_risk"] - 100.0) < 1e-9,
          str(res))
    check("2b. size обрезан потолком 15% equity до 1 контракта", res["size"] == 1,
          str(res))
    check("2c. notional = 840 (<= 15% equity)", abs(res["notional"] - 840.0) < 1e-6,
          str(res))
    check("2d. warning о потолке присутствует",
          any("15" in w for w in res["warnings"]), str(res["warnings"]))
    # без потолка 15%: equity 40000, широкий стоп 7600 (риск 400 / 80 = 5 контрактов,
    # notional 4200 = 10.5% equity — под потолком)
    res2 = risk.size_position(equity=40000.0, entry=84000.0, stop=76000.0,
                              ct_val=0.01, lot_sz=0.01, min_sz=0.01, risk_pct=1.0)
    check("2e. equity 40000, стоп 76000 -> 5 контрактов без обрезки",
          res2["size"] == 5 and not res2["warnings"], str(res2))
    # стоп за ликвидацией запрещён
    check("2f. стоп ближе ликвидации (long)",
          risk.validate_stop_vs_liquidation(84000, 82000, 80000, "long"))
    check("2g. стоп за ликвидацией (long) -> False",
          not risk.validate_stop_vs_liquidation(84000, 81000, 82000, "long"))

    # --- 5. Три убытка подряд по инструменту -> блокировка 24ч ---
    # (выполняется до дневного лимита: убытки копятся в дневном PnL накопительно)
    now = datetime.now(tz=timezone.utc)
    blocked_event = None
    for i in range(3):
        events = risk.record_pnl("SOL-USDT-SWAP", -10.0, now)
        if "instrument_blocked" in events:
            blocked_event = i + 1
    check("5a. блокировка сработала ровно на 3-м убытке", blocked_event == 3,
          f"на сделке {blocked_event}")
    check("5b. is_instrument_blocked(SOL) == True",
          risk.is_instrument_blocked("SOL-USDT-SWAP"))
    allowed, reason = risk.check_entry_allowed("SOL-USDT-SWAP", "buy")
    check("5c. вход по заблокированному инструменту запрещён",
          not allowed and "SOL-USDT-SWAP" in reason, reason)
    allowed, _ = risk.check_entry_allowed("BTC-USDT-SWAP", "buy")
    check("5d. другой инструмент не затронут", allowed)

    # --- 3. Убыток -6% за день -> дневной breaker ---
    # day_pnl уже -30 (сценарий 5); добираем -600 -> -630 от 10000 = -6.3%
    events = risk.record_pnl("ETH-USDT-SWAP", -600.0, now)
    check("3a. record_pnl вернул daily_limit", "daily_limit" in events, str(events))
    allowed, reason = risk.check_entry_allowed("BTC-USDT-SWAP", "buy")
    check("3b. вход заблокирован дневным breaker", not allowed and "дневной" in reason,
          reason)
    st = risk.status()
    check("3c. status: daily_breaker=True, equity=9370",
          st["daily_breaker"] and abs(st["equity"] - 9370.0) < 1e-9, str(st["equity"]))

    # --- 4. Глобальный breaker: блокирует всё до ручного сброса ---
    risk.reset_breaker("daily", by="selftest")  # снимаем дневной, чтобы изолировать
    risk.trip_breaker("selftest: ручной трип", scope="global")
    allowed, reason = risk.check_entry_allowed("BTC-USDT-SWAP", "buy")
    check("4a. глобальный breaker блокирует вход", not allowed and "глобальный" in reason,
          reason)
    risk.reset_breaker("daily", by="selftest")  # дневной сброс не помогает
    allowed, _ = risk.check_entry_allowed("BTC-USDT-SWAP", "buy")
    check("4b. сброс daily не снимает global", not allowed)
    risk.reset_breaker("global", by="selftest")
    allowed, reason = risk.check_entry_allowed("BTC-USDT-SWAP", "buy")
    check("4c. после reset_breaker('global') вход разрешён", allowed, reason)

    # --- 6. Состояние переживает рестарт ---
    risk.trip_breaker("selftest: персистентность", scope="global")
    risk.init(TEST_DB)  # имитация рестарта процесса
    allowed, _ = risk.check_entry_allowed("BTC-USDT-SWAP", "buy")
    check("6a. global breaker переживает рестарт", not allowed)
    check("6b. блокировка инструмента переживает рестарт",
          risk.is_instrument_blocked("SOL-USDT-SWAP"))
    st = risk.status()
    check("6c. equity переживает рестарт (9370)",
          abs(st["equity"] - 9370.0) < 1e-9, str(st["equity"]))

    print(f"\nSELF-TEST PASSED: {_passed} проверок")


if __name__ == "__main__":
    main()
