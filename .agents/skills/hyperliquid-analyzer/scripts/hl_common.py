"""
hl_common.py — Hyperliquid API 共享工具函数
供 hl_whale_hunt.py 和 hl_whale_monitor.py 导入使用
"""

from __future__ import annotations

import asyncio
import json
import urllib.request

HL_API = "https://api.hyperliquid.xyz/info"

# 并发限制：最多同时发出 20 个请求，避免 API 限流
_SEMAPHORE: asyncio.Semaphore | None = None


def _get_semaphore() -> asyncio.Semaphore:
    global _SEMAPHORE
    if _SEMAPHORE is None:
        _SEMAPHORE = asyncio.Semaphore(20)
    return _SEMAPHORE


def _post(payload: dict) -> dict | list | None:
    data = json.dumps(payload).encode()
    req = urllib.request.Request(
        HL_API, data=data, headers={"Content-Type": "application/json"}
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            return json.loads(r.read())
    except Exception:
        return None


async def post_async(payload: dict) -> dict | list | None:
    async with _get_semaphore():
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(None, _post, payload)


async def get_top_coins(n: int) -> list[str]:
    raw = await post_async({"type": "metaAndAssetCtxs"})
    if not raw:
        return []
    pairs = list(zip(raw[0]["universe"], raw[1]))
    pairs.sort(
        key=lambda x: float(x[1].get("openInterest", 0)) * float(x[1].get("markPx", 0)),
        reverse=True,
    )
    return [p[0]["name"] for p in pairs[:n]]


async def get_addresses_from_trades(coin: str) -> set[str]:
    raw = await post_async({"type": "recentTrades", "coin": coin})
    if not raw:
        return set()
    addrs: set[str] = set()
    for trade in raw:
        for user in trade.get("users", []):
            addrs.add(user)
    return addrs
