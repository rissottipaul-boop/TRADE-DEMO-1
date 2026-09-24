#!/usr/bin/env python3
"""
hl_whale_hunt.py — 从近期成交流收集地址，批量查 portfolio，找出巨鲸
用法: python3 hl_whale_hunt.py [min_account_usd] [top_n_coins]
示例: python3 hl_whale_hunt.py 500000 20   # 账户净值 >50万，扫前20币种
"""

from __future__ import annotations

import asyncio
import sys
from datetime import datetime, timezone

from hl_common import get_addresses_from_trades, get_top_coins, post_async

MIN_ACCOUNT = float(sys.argv[1]) if len(sys.argv) > 1 else 500_000
TOP_N_COINS = int(sys.argv[2]) if len(sys.argv) > 2 else 15


async def get_portfolio(addr: str) -> dict | None:
    raw = await post_async({"type": "clearinghouseState", "user": addr})
    if not raw or "marginSummary" not in raw:
        return None
    acct_value = float(raw["marginSummary"].get("accountValue", 0))
    positions = [
        {
            "coin": p["position"]["coin"],
            "side": "LONG" if float(p["position"]["szi"]) > 0 else "SHORT",
            "size": abs(float(p["position"]["szi"])),
            "entry_px": float(p["position"].get("entryPx") or 0),
            "upnl": float(p["position"].get("unrealizedPnl") or 0),
            "liq_px": float(p["position"]["liquidationPx"]) if p["position"].get("liquidationPx") else None,
        }
        for p in raw.get("assetPositions", [])
        if float(p["position"]["szi"]) != 0
    ]
    return {"addr": addr, "account_value": acct_value, "positions": positions}


async def main():
    print()
    print("Hyperliquid — 巨鲸猎手（成交流 → Portfolio 扫描）")
    print("━" * 70)

    print(f"[1/3] 获取持仓量最高的 {TOP_N_COINS} 个币种...")
    coins = await get_top_coins(TOP_N_COINS)
    print(f"      扫描: {' '.join(coins)}")

    print("[2/3] 并行获取近期成交，收集交易地址...")
    addr_sets = await asyncio.gather(*[get_addresses_from_trades(c) for c in coins])
    all_addrs: set[str] = set()
    for s in addr_sets:
        all_addrs.update(s)
    print(f"      收集到 {len(all_addrs)} 个唯一地址")

    print(f"[3/3] 并行查询 portfolio（筛选账户净值 > ${MIN_ACCOUNT:,.0f}）...")
    portfolios = await asyncio.gather(*[get_portfolio(a) for a in all_addrs])

    whales = [p for p in portfolios if p and p["account_value"] >= MIN_ACCOUNT]
    whales.sort(key=lambda x: x["account_value"], reverse=True)

    print()
    if not whales:
        print(f"  未找到账户净值 > ${MIN_ACCOUNT:,.0f} 的地址（尝试降低阈值）")
        print(f"  提示: python3 hl_whale_hunt.py 50000 {TOP_N_COINS}")
        return

    print(f"找到 {len(whales)} 个巨鲸账户:")
    print("━" * 70)

    for w in whales:
        addr = w["addr"]
        acct = w["account_value"]
        acct_fmt = f"${acct/1e6:.2f}M" if acct >= 1e6 else f"${acct/1e3:.1f}K"
        print(f"\n  {addr[:6]}...{addr[-4:]}  账户净值: {acct_fmt}")
        print(f"  完整地址: {addr}")

        if not w["positions"]:
            print("    （无未平仓仓位）")
            continue

        print(f"  {'币种':<6} {'方向':<6} {'数量':<10} {'开仓价':<12} {'清算价':<12} {'uPnL'}")
        print(f"  {'------':<6} {'------':<6} {'----------':<10} {'------------':<12} {'------------':<12} {'----------'}")
        for pos in sorted(w["positions"], key=lambda x: abs(x["upnl"]), reverse=True):
            liq_fmt = f"${pos['liq_px']:,.2f}" if pos["liq_px"] else "N/A"
            size_usd = pos["size"] * pos["entry_px"]
            print(
                f"  {pos['coin']:<6} {pos['side']:<6} "
                f"{pos['size']:.4f} (${size_usd/1e3:.1f}K)   "
                f"${pos['entry_px']:,.2f}      {liq_fmt:<12} "
                f"${'+' if pos['upnl']>=0 else ''}{pos['upnl']:,.2f}"
            )

    print()
    print("━" * 70)
    print(f"时间: {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}")
    print("注意: 地址来自近期成交流快照，非全量巨鲸")


asyncio.run(main())
