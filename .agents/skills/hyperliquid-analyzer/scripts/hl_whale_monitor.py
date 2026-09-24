#!/usr/bin/env python3
"""
hl_whale_monitor.py — Hyperliquid 巨鲸持仓监控（完全自治）

用法:
  python3 hl_whale_monitor.py [选项]

选项:
  --min-account       巨鲸账户净值门槛 USD（默认 500000）
  --min-position      值得报警的仓位规模 USD（默认 200000）
  --change-threshold  加减仓触发比例（默认 0.30 = 30%）
  --top-coins         扫描高 OI 币种数（默认 20）
  --state-file        状态文件路径（默认 ./state/whale_positions.json）
  --dry-run           不发送消息，只打印到 stdout（测试用）

消息渠道（环境变量）:
  NOTIFY_CHANNEL=telegram    使用 Telegram Bot API
  TG_BOT_TOKEN=xxx           Telegram Bot Token
  TG_CHAT_ID=yyy             Telegram Chat ID（支持 group/channel）

  NOTIFY_CHANNEL=webhook     通用 Webhook（Slack / 企微 / 自定义）
  NOTIFY_WEBHOOK_URL=https://...  发送 JSON POST {"text": "..."}

  未设置或 NOTIFY_CHANNEL=none → 只打印到 stdout
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

from hl_common import get_addresses_from_trades, get_top_coins, post_async


# ─── 消息发送器 ───────────────────────────────────────────────────────────────

class NotifySender:
    def send(self, text: str):
        print(text)

    @staticmethod
    def from_env(dry_run: bool = False) -> NotifySender:
        if dry_run:
            return StdoutSender("[DRY-RUN] ")

        channel = os.environ.get("NOTIFY_CHANNEL", "none").lower()
        if channel == "telegram":
            token = os.environ.get("TG_BOT_TOKEN", "")
            chat_id = os.environ.get("TG_CHAT_ID", "")
            if token and chat_id:
                return TelegramSender(token, chat_id)
            print("[WARN] NOTIFY_CHANNEL=telegram 但 TG_BOT_TOKEN 或 TG_CHAT_ID 未设置，fallback 到 stdout", file=sys.stderr)
        elif channel == "webhook":
            url = os.environ.get("NOTIFY_WEBHOOK_URL", "")
            if url:
                return WebhookSender(url)
            print("[WARN] NOTIFY_CHANNEL=webhook 但 NOTIFY_WEBHOOK_URL 未设置，fallback 到 stdout", file=sys.stderr)

        return StdoutSender()


class StdoutSender(NotifySender):
    def __init__(self, prefix: str = ""):
        self.prefix = prefix

    def send(self, text: str):
        for line in text.split("\n"):
            print(f"{self.prefix}{line}")


class TelegramSender(NotifySender):
    def __init__(self, token: str, chat_id: str):
        self.token = token
        self.chat_id = chat_id

    def send(self, text: str):
        url = f"https://api.telegram.org/bot{self.token}/sendMessage"
        payload = json.dumps({
            "chat_id": self.chat_id,
            "text": text,
        }).encode()
        req = urllib.request.Request(url, data=payload,
                                     headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=10) as r:
                resp = json.loads(r.read())
                if not resp.get("ok"):
                    print(f"[WARN] Telegram 发送失败: {resp}", file=sys.stderr)
                    print(text)
        except Exception as e:
            print(f"[ERROR] Telegram 发送异常: {e}", file=sys.stderr)
            print(text)


class WebhookSender(NotifySender):
    def __init__(self, url: str):
        self.url = url

    def send(self, text: str):
        payload = json.dumps({"text": text}).encode()
        req = urllib.request.Request(self.url, data=payload,
                                     headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=10):
                pass
        except Exception as e:
            print(f"[ERROR] Webhook 发送异常: {e}", file=sys.stderr)
            print(text)


# ─── Portfolio 查询（返回 dict 格式，便于状态对比）────────────────────────────

async def get_portfolio(addr: str) -> dict | None:
    raw = await post_async({"type": "clearinghouseState", "user": addr})
    if not raw or "marginSummary" not in raw:
        return None
    acct_value = float(raw["marginSummary"].get("accountValue", 0))
    positions: dict[str, dict] = {}
    for p in raw.get("assetPositions", []):
        szi = float(p["position"]["szi"])
        if szi == 0:
            continue
        coin = p["position"]["coin"]
        entry_px = float(p["position"].get("entryPx") or 0)
        liq_raw = p["position"].get("liquidationPx")
        positions[coin] = {
            "side": "LONG" if szi > 0 else "SHORT",
            "size": abs(szi),
            "size_usd": abs(szi) * entry_px,
            "entry_px": entry_px,
            "upnl": float(p["position"].get("unrealizedPnl") or 0),
            # 统一存为 float 或 None，避免类型混乱
            "liq_px": float(liq_raw) if liq_raw else None,
        }
    return {
        "addr": addr,
        "account_value": acct_value,
        "positions": positions,
        "scanned_at": datetime.now(timezone.utc).isoformat(),
    }


# ─── 状态对比与信号生成 ────────────────────────────────────────────────────────

def detect_signals(old_state: dict, new_state: dict,
                   min_position: float, change_threshold: float) -> list[dict]:
    signals = []

    for addr, new_whale in new_state.items():
        old_whale = old_state.get(addr)
        acct = new_whale["account_value"]

        if old_whale is None:
            if new_whale["positions"]:
                signals.append({
                    "type": "new_whale",
                    "addr": addr,
                    "account_value": acct,
                    "positions": new_whale["positions"],
                })
            continue

        old_positions = old_whale.get("positions", {})
        new_positions = new_whale.get("positions", {})

        for coin in set(old_positions) | set(new_positions):
            old_pos = old_positions.get(coin)
            new_pos = new_positions.get(coin)

            if old_pos is None and new_pos is not None:
                if new_pos["size_usd"] >= min_position:
                    signals.append({"type": "new_position", "addr": addr,
                                    "account_value": acct, "coin": coin, **new_pos})
            elif old_pos is not None and new_pos is None:
                if old_pos["size_usd"] >= min_position:
                    signals.append({"type": "closed", "addr": addr,
                                    "account_value": acct, "coin": coin,
                                    "old_size_usd": old_pos["size_usd"],
                                    "side": old_pos["side"]})
            elif old_pos is not None and new_pos is not None:
                old_sz = old_pos["size"]
                if old_sz == 0:
                    continue
                change = (new_pos["size"] - old_sz) / old_sz
                if change >= change_threshold and new_pos["size_usd"] >= min_position:
                    signals.append({"type": "increased", "addr": addr,
                                    "account_value": acct, "coin": coin,
                                    "side": new_pos["side"],
                                    "old_size_usd": old_pos["size_usd"],
                                    "new_size_usd": new_pos["size_usd"],
                                    "change_pct": change})
                elif change <= -change_threshold and old_pos["size_usd"] >= min_position:
                    signals.append({"type": "decreased", "addr": addr,
                                    "account_value": acct, "coin": coin,
                                    "side": old_pos["side"],
                                    "old_size_usd": old_pos["size_usd"],
                                    "new_size_usd": new_pos["size_usd"],
                                    "change_pct": change})
    return signals


# ─── 消息格式化 ───────────────────────────────────────────────────────────────

def fmt_usd(v: float) -> str:
    if v >= 1_000_000:
        return f"${v/1_000_000:.2f}M"
    elif v >= 1_000:
        return f"${v/1_000:.1f}K"
    return f"${v:.0f}"


def fmt_addr(addr: str) -> str:
    return f"{addr[:6]}...{addr[-4:]}"


def format_message(signals: list[dict], scan_summary: dict, ts: str) -> str:
    by_type = {t: [s for s in signals if s["type"] == t]
               for t in ("new_position", "increased", "decreased", "closed", "new_whale")}

    lines = [f"HL 巨鲸信号 — {ts}", ""]

    if by_type["new_position"]:
        lines.append(f"[新开仓 {len(by_type['new_position'])}]")
        for s in by_type["new_position"]:
            liq = f"  清算价: {fmt_usd(s['liq_px'])}" if s.get("liq_px") else ""
            lines.append(f"  {fmt_addr(s['addr'])} ({fmt_usd(s['account_value'])})")
            lines.append(f"  {s['coin']} {s['side']} {fmt_usd(s['size_usd'])} @ ${s['entry_px']:,.2f}{liq}")
        lines.append("")

    if by_type["increased"]:
        lines.append(f"[加仓 {len(by_type['increased'])}]")
        for s in by_type["increased"]:
            lines.append(f"  {fmt_addr(s['addr'])}")
            lines.append(f"  {s['coin']} {s['side']} +{s['change_pct']*100:.0f}%  {fmt_usd(s['old_size_usd'])} -> {fmt_usd(s['new_size_usd'])}")
        lines.append("")

    if by_type["decreased"]:
        lines.append(f"[减仓 {len(by_type['decreased'])}]")
        for s in by_type["decreased"]:
            lines.append(f"  {fmt_addr(s['addr'])}")
            lines.append(f"  {s['coin']} {s['side']} -{abs(s['change_pct'])*100:.0f}%  {fmt_usd(s['old_size_usd'])} -> {fmt_usd(s['new_size_usd'])}")
        lines.append("")

    if by_type["closed"]:
        lines.append(f"[平仓 {len(by_type['closed'])}]")
        for s in by_type["closed"]:
            lines.append(f"  {fmt_addr(s['addr'])}")
            lines.append(f"  {s['coin']} {s['side']} 已平 (原 {fmt_usd(s['old_size_usd'])})")
        lines.append("")

    if by_type["new_whale"]:
        lines.append(f"[新巨鲸 {len(by_type['new_whale'])}]")
        for s in by_type["new_whale"]:
            pos_summary = ", ".join(
                f"{c} {p['side']} {fmt_usd(p['size_usd'])}"
                for c, p in list(s["positions"].items())[:3]
            )
            lines.append(f"  {fmt_addr(s['addr'])} ({fmt_usd(s['account_value'])})")
            lines.append(f"  {pos_summary}")
        lines.append("")

    lines.append("-" * 20)
    lines.append(
        f"扫描: {scan_summary['addresses']}地址 / {scan_summary['whales']}巨鲸 / {scan_summary['coins']}币种"
    )
    return "\n".join(lines)


# ─── 主流程 ───────────────────────────────────────────────────────────────────

async def main():
    parser = argparse.ArgumentParser(description="HL 巨鲸持仓监控")
    parser.add_argument("--min-account", type=float, default=500_000)
    parser.add_argument("--min-position", type=float, default=200_000)
    parser.add_argument("--change-threshold", type=float, default=0.30)
    parser.add_argument("--top-coins", type=int, default=20)
    parser.add_argument("--state-file", type=str,
                        default=str(Path.home() / ".hl-whale-monitor" / "whale_positions.json"))
    parser.add_argument("--dry-run", action="store_true",
                        help="不发送消息，只打印到 stdout（测试用）")
    args = parser.parse_args()

    sender = NotifySender.from_env(dry_run=args.dry_run)
    state_path = Path(args.state_file)
    try:
        state_path.parent.mkdir(parents=True, exist_ok=True)
    except OSError as e:
        print(f"[ERROR] 无法创建状态目录 {state_path.parent}: {e}", file=sys.stderr)
        sys.exit(1)

    ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")

    is_init = not state_path.exists()
    old_state: dict = {}
    if not is_init:
        try:
            old_state = json.loads(state_path.read_text())
        except Exception:
            is_init = True

    print(f"[{ts}] 开始扫描 Top {args.top_coins} 币种...", file=sys.stderr)
    coins = await get_top_coins(args.top_coins)

    addr_sets = await asyncio.gather(*[get_addresses_from_trades(c) for c in coins])
    all_addrs: set[str] = set()
    for s in addr_sets:
        all_addrs.update(s)
    print(f"[{ts}] 收集到 {len(all_addrs)} 个地址，查询 portfolio...", file=sys.stderr)

    portfolios = await asyncio.gather(*[get_portfolio(a) for a in all_addrs])
    new_state = {
        p["addr"]: p
        for p in portfolios
        if p and p["account_value"] >= args.min_account
    }
    scan_summary = {"addresses": len(all_addrs), "whales": len(new_state), "coins": len(coins)}
    print(f"[{ts}] 找到 {len(new_state)} 个巨鲸账户", file=sys.stderr)

    state_path.write_text(json.dumps(new_state, ensure_ascii=False, indent=2))

    if is_init:
        msg = (
            f"HL 巨鲸监控已启动 — {ts}\n"
            f"已记录 {len(new_state)} 个巨鲸账户基准状态\n"
            f"（扫描 {len(all_addrs)} 地址 / {len(coins)} 币种）\n"
            f"下次运行将开始检测持仓变化。"
        )
        sender.send(msg)
        return

    signals = detect_signals(old_state, new_state,
                             min_position=args.min_position,
                             change_threshold=args.change_threshold)
    if signals:
        sender.send(format_message(signals, scan_summary, ts))
        print(f"[{ts}] 发送了 {len(signals)} 条信号", file=sys.stderr)
    else:
        print(f"[{ts}] 无持仓变化，静默", file=sys.stderr)


if __name__ == "__main__":
    asyncio.run(main())
