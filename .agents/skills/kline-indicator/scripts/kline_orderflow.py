#!/usr/bin/env python3
"""
Order Flow Analysis Engine for OKX TradeKit (okx-trade-mcp / okx-trade-cli) K-Line Indicators Skill.
Analyzes orderbook depth, trade flow, funding rate, and open interest
to produce actionable order flow signals with CLI-formatted output.
"""
import json, sys, math, argparse
from datetime import datetime, timezone

class OrderFlowEngine:
    """Order flow analysis engine with multi-dimensional scoring."""

    def __init__(self, orderbook, trades, funding, oi, coin="BTC"):
        self.coin = coin
        self.orderbook = orderbook
        self.trades = trades
        self.funding = funding
        self.oi = oi

    # ── Orderbook Analysis ──────────────────────────────────

    def analyze_orderbook(self):
        """Analyze order book depth, imbalance, spread, and walls."""
        result = {"bid_total": 0, "ask_total": 0, "imbalance": 0,
                  "spread": 0, "spread_pct": 0, "mid_price": 0,
                  "best_bid": 0, "best_ask": 0,
                  "depth_01": {"bid": 0, "ask": 0},
                  "depth_05": {"bid": 0, "ask": 0},
                  "depth_10": {"bid": 0, "ask": 0},
                  "bid_walls": [], "ask_walls": [],
                  "large_bids": [], "large_asks": []}

        bids = self._extract_levels(self.orderbook, "bids")
        asks = self._extract_levels(self.orderbook, "asks")
        if not bids or not asks:
            return result

        # Best bid/ask and spread
        best_bid = bids[0][0]
        best_ask = asks[0][0]
        mid_price = (best_bid + best_ask) / 2
        spread = best_ask - best_bid
        spread_pct = (spread / mid_price * 100) if mid_price > 0 else 0

        result["best_bid"] = best_bid
        result["best_ask"] = best_ask
        result["mid_price"] = mid_price
        result["spread"] = spread
        result["spread_pct"] = round(spread_pct, 4)

        # Total volume
        bid_total = sum(qty for _, qty in bids)
        ask_total = sum(qty for _, qty in asks)
        result["bid_total"] = round(bid_total, 4)
        result["ask_total"] = round(ask_total, 4)
        result["imbalance"] = round(bid_total / ask_total, 4) if ask_total > 0 else 0

        # Depth at percentage levels
        for pct_key, pct_val in [("depth_01", 0.001), ("depth_05", 0.005), ("depth_10", 0.01)]:
            bid_depth = sum(qty for p, qty in bids if p >= mid_price * (1 - pct_val))
            ask_depth = sum(qty for p, qty in asks if p <= mid_price * (1 + pct_val))
            result[pct_key] = {"bid": round(bid_depth, 4), "ask": round(ask_depth, 4)}

        # Large order / wall detection
        if bids:
            avg_bid_qty = bid_total / len(bids)
            for price, qty in bids:
                if qty > avg_bid_qty * 3:
                    result["large_bids"].append({"price": price, "qty": round(qty, 4), "ratio": round(qty / avg_bid_qty, 1)})
            # Top 3 bid walls (densest regions)
            result["bid_walls"] = sorted(result["large_bids"], key=lambda x: -x["qty"])[:3]

        if asks:
            avg_ask_qty = ask_total / len(asks)
            for price, qty in asks:
                if qty > avg_ask_qty * 3:
                    result["large_asks"].append({"price": price, "qty": round(qty, 4), "ratio": round(qty / avg_ask_qty, 1)})
            result["ask_walls"] = sorted(result["large_asks"], key=lambda x: -x["qty"])[:3]

        return result

    def _extract_levels(self, data, side):
        """Extract price levels from orderbook data."""
        levels = []
        raw = None
        if isinstance(data, dict):
            raw = data.get("data", data).get(side) if isinstance(data.get("data"), dict) else data.get(side)
            if raw is None and isinstance(data.get("data"), list) and data["data"]:
                raw = data["data"][0].get(side)
        if isinstance(data, list) and data:
            raw = data[0].get(side)
        if not raw:
            return levels
        for item in raw:
            if isinstance(item, (list, tuple)) and len(item) >= 2:
                try:
                    levels.append((float(item[0]), float(item[1])))
                except (ValueError, TypeError):
                    continue
        return levels

    # ── Trade Flow Analysis ─────────────────────────────────

    def analyze_trades(self):
        """Analyze recent trades for delta, CVD, aggression, and large orders."""
        result = {"buy_vol": 0, "sell_vol": 0, "delta": 0, "cvd": 0,
                  "buy_count": 0, "sell_count": 0, "total_count": 0,
                  "aggressor_ratio": 0, "trade_speed": 0,
                  "large_trades": [], "avg_trade_size": 0,
                  "vwap": 0, "last_price": 0}

        trades = self._extract_trades(self.trades)
        if not trades:
            return result

        total_count = len(trades)
        buy_vol = 0
        sell_vol = 0
        buy_count = 0
        sell_count = 0
        price_vol_sum = 0
        vol_sum = 0
        sizes = []

        for t in trades:
            price = t["price"]
            qty = t["qty"]
            side = t["side"]
            sizes.append(qty)
            price_vol_sum += price * qty
            vol_sum += qty
            if side == "buy":
                buy_vol += qty * price
                buy_count += 1
            else:
                sell_vol += qty * price
                sell_count += 1

        delta = buy_vol - sell_vol
        cvd = delta  # single snapshot CVD
        aggressor_ratio = (buy_count / total_count * 100) if total_count > 0 else 50
        vwap = (price_vol_sum / vol_sum) if vol_sum > 0 else 0

        result["buy_vol"] = round(buy_vol, 2)
        result["sell_vol"] = round(sell_vol, 2)
        result["delta"] = round(delta, 2)
        result["cvd"] = round(cvd, 2)
        result["buy_count"] = buy_count
        result["sell_count"] = sell_count
        result["total_count"] = total_count
        result["aggressor_ratio"] = round(aggressor_ratio, 2)
        result["vwap"] = round(vwap, 2)
        result["last_price"] = trades[-1]["price"] if trades else 0

        # Trade speed (trades per second)
        if len(trades) >= 2:
            ts_first = trades[0].get("ts", 0)
            ts_last = trades[-1].get("ts", 0)
            duration = abs(ts_last - ts_first) / 1000  # ms to seconds
            result["trade_speed"] = round(total_count / duration, 2) if duration > 0 else 0

        # Average and large trade detection
        avg_size = vol_sum / total_count if total_count > 0 else 0
        result["avg_trade_size"] = round(avg_size, 6)
        median_size = sorted(sizes)[len(sizes) // 2] if sizes else 0
        threshold = median_size * 5 if median_size > 0 else avg_size * 3
        for t in trades:
            if t["qty"] > threshold and threshold > 0:
                result["large_trades"].append({
                    "price": t["price"], "qty": round(t["qty"], 6),
                    "side": t["side"],
                    "ratio": round(t["qty"] / median_size, 1) if median_size > 0 else 0
                })

        return result

    def _extract_trades(self, data):
        """Extract trade records from trades API response."""
        trades = []
        raw = None
        if isinstance(data, dict):
            raw = data.get("data", [])
        elif isinstance(data, list):
            raw = data
        if not raw:
            return trades
        for item in raw:
            if not isinstance(item, dict):
                continue
            try:
                price = float(item.get("px", item.get("price", 0)))
                qty = float(item.get("sz", item.get("qty", item.get("size", 0))))
                side_raw = str(item.get("side", "")).lower()
                side = "buy" if side_raw == "buy" else "sell"
                ts = int(item.get("ts", 0))
                if price > 0 and qty > 0:
                    trades.append({"price": price, "qty": qty, "side": side, "ts": ts})
            except (ValueError, TypeError):
                continue
        return trades

    # ── Funding & OI Analysis ───────────────────────────────

    def analyze_funding(self):
        """Analyze funding rate data."""
        result = {"funding_rate": None, "funding_time": None, "annual_rate": None, "sentiment": "neutral"}
        raw = self.funding
        if isinstance(raw, dict):
            data = raw.get("data", [raw])
        elif isinstance(raw, list):
            data = raw
        else:
            return result
        if not data:
            return result
        item = data[0] if isinstance(data, list) else data
        try:
            rate = float(item.get("fundingRate", item.get("funding_rate", 0)))
            result["funding_rate"] = round(rate * 100, 4)  # percentage
            result["annual_rate"] = round(rate * 3 * 365 * 100, 2)  # annualized %
            ts = item.get("fundingTime", item.get("ts", ""))
            if ts:
                result["funding_time"] = ts
            if rate > 0.0005:
                result["sentiment"] = "bullish_crowd"
            elif rate < -0.0005:
                result["sentiment"] = "bearish_crowd"
            else:
                result["sentiment"] = "neutral"
        except (ValueError, TypeError):
            pass
        return result

    def analyze_open_interest(self):
        """Analyze open interest data."""
        result = {"oi": None, "oi_usd": None, "ts": None}
        raw = self.oi
        if isinstance(raw, dict):
            data = raw.get("data", [raw])
        elif isinstance(raw, list):
            data = raw
        else:
            return result
        if not data:
            return result
        item = data[0] if isinstance(data, list) else data
        try:
            oi = float(item.get("oi", item.get("oiCcy", 0)))
            result["oi"] = round(oi, 4)
            oi_usd_val = item.get("oiUsd")
            if oi_usd_val:
                result["oi_usd"] = round(float(oi_usd_val), 2)
            result["ts"] = item.get("ts", "")
        except (ValueError, TypeError):
            pass
        return result

    # ── Scoring Engine ──────────────────────────────────────

    def compute_score(self, ob, tf, fr, oi_data):
        """Compute composite order flow score (0-100)."""
        score = 50  # neutral baseline
        signals = []

        # Orderbook imbalance
        if ob["imbalance"] > 1.3:
            score += 8; signals.append(("bid_dominance", "+8", f"imbalance={ob['imbalance']:.2f}"))
        elif ob["imbalance"] > 1.1:
            score += 4; signals.append(("bid_lean", "+4", f"imbalance={ob['imbalance']:.2f}"))
        elif ob["imbalance"] < 0.7:
            score -= 8; signals.append(("ask_dominance", "-8", f"imbalance={ob['imbalance']:.2f}"))
        elif ob["imbalance"] < 0.9:
            score -= 4; signals.append(("ask_lean", "-4", f"imbalance={ob['imbalance']:.2f}"))

        # Spread tightness (tight = good liquidity)
        if ob["spread_pct"] < 0.01:
            score += 2; signals.append(("tight_spread", "+2", f"{ob['spread_pct']:.4f}%"))

        # Trade delta
        if tf["delta"] > 0:
            delta_strength = min(10, int(abs(tf["delta"]) / (max(tf["buy_vol"], tf["sell_vol"], 1)) * 20))
            score += delta_strength
            signals.append(("positive_delta", f"+{delta_strength}", f"${tf['delta']:,.0f}"))
        elif tf["delta"] < 0:
            delta_strength = min(10, int(abs(tf["delta"]) / (max(tf["buy_vol"], tf["sell_vol"], 1)) * 20))
            score -= delta_strength
            signals.append(("negative_delta", f"-{delta_strength}", f"${tf['delta']:,.0f}"))

        # Aggressor ratio
        if tf["aggressor_ratio"] > 60:
            score += 6; signals.append(("strong_buyers", "+6", f"{tf['aggressor_ratio']:.1f}%"))
        elif tf["aggressor_ratio"] > 55:
            score += 3; signals.append(("lean_buyers", "+3", f"{tf['aggressor_ratio']:.1f}%"))
        elif tf["aggressor_ratio"] < 40:
            score -= 6; signals.append(("strong_sellers", "-6", f"{tf['aggressor_ratio']:.1f}%"))
        elif tf["aggressor_ratio"] < 45:
            score -= 3; signals.append(("lean_sellers", "-3", f"{tf['aggressor_ratio']:.1f}%"))

        # Funding rate (contrarian)
        if fr["funding_rate"] is not None:
            rate = fr["funding_rate"]
            if rate < -0.01:
                score += 5; signals.append(("negative_funding", "+5", f"{rate:.4f}%"))
            elif rate > 0.05:
                score -= 5; signals.append(("high_funding", "-5", f"{rate:.4f}%"))

        # Bid/Ask walls
        if ob["bid_walls"] and not ob["ask_walls"]:
            score += 3; signals.append(("bid_walls_only", "+3", f"{len(ob['bid_walls'])} walls"))
        elif ob["ask_walls"] and not ob["bid_walls"]:
            score -= 3; signals.append(("ask_walls_only", "-3", f"{len(ob['ask_walls'])} walls"))

        score = max(0, min(100, score))
        if score >= 70:
            verdict = "strong_buy"
        elif score >= 58:
            verdict = "buy"
        elif score >= 42:
            verdict = "neutral"
        elif score >= 30:
            verdict = "sell"
        else:
            verdict = "strong_sell"

        return {"score": score, "verdict": verdict, "signals": signals}

    # ── CLI Output Formatter ────────────────────────────────

    def format_cli_output(self, ob, tf, fr, oi_data, scoring):
        """Format analysis results for CLI terminal output."""
        lines = []
        w = 56  # box width

        # Header
        lines.append("=" * w)
        lines.append(f"  {self.coin}-USDT Order Flow Analysis".center(w))
        lines.append(f"  {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S UTC')}".center(w))
        lines.append("=" * w)

        # Score bar
        score = scoring["score"]
        verdict_map = {"strong_buy": "STRONG BUY", "buy": "BUY",
                       "neutral": "NEUTRAL", "sell": "SELL", "strong_sell": "STRONG SELL"}
        verdict_cn = {"strong_buy": "Strong Buy", "buy": "Buy",
                      "neutral": "Neutral", "sell": "Sell", "strong_sell": "Strong Sell"}
        verdict = verdict_map.get(scoring["verdict"], "NEUTRAL")
        bar_len = 30
        filled = int(score / 100 * bar_len)
        bar = "[" + "#" * filled + "-" * (bar_len - filled) + "]"
        lines.append(f"\n  Score: {score}/100  {bar}  {verdict}")

        # Orderbook section
        lines.append(f"\n{'─' * w}")
        lines.append("  [Orderbook Depth]")
        lines.append(f"  Best Bid: ${ob['best_bid']:,.2f}  |  Best Ask: ${ob['best_ask']:,.2f}")
        lines.append(f"  Mid Price: ${ob['mid_price']:,.2f}  |  Spread: ${ob['spread']:,.2f} ({ob['spread_pct']:.4f}%)")
        lines.append(f"  Bid Total: {ob['bid_total']:,.4f}  |  Ask Total: {ob['ask_total']:,.4f}")
        imb = ob["imbalance"]
        imb_label = "BID > ASK" if imb > 1.05 else ("ASK > BID" if imb < 0.95 else "BALANCED")
        lines.append(f"  Imbalance Ratio: {imb:.4f}  ({imb_label})")

        # Depth levels
        lines.append(f"\n  Depth Levels (from mid):")
        for label, key in [("0.1%", "depth_01"), ("0.5%", "depth_05"), ("1.0%", "depth_10")]:
            d = ob[key]
            lines.append(f"    {label}: Bid={d['bid']:,.4f}  Ask={d['ask']:,.4f}")

        # Walls
        if ob["bid_walls"]:
            lines.append(f"\n  Bid Walls (support):")
            for wall in ob["bid_walls"][:3]:
                lines.append(f"    ${wall['price']:,.2f}  qty={wall['qty']:,.4f}  ({wall['ratio']:.1f}x avg)")
        if ob["ask_walls"]:
            lines.append(f"  Ask Walls (resistance):")
            for wall in ob["ask_walls"][:3]:
                lines.append(f"    ${wall['price']:,.2f}  qty={wall['qty']:,.4f}  ({wall['ratio']:.1f}x avg)")

        # Trade flow section
        lines.append(f"\n{'─' * w}")
        lines.append("  [Trade Flow]")
        lines.append(f"  Buy Volume:  ${tf['buy_vol']:>12,.2f}  ({tf['buy_count']} trades)")
        lines.append(f"  Sell Volume: ${tf['sell_vol']:>12,.2f}  ({tf['sell_count']} trades)")
        delta_sign = "+" if tf["delta"] >= 0 else ""
        lines.append(f"  Delta:       ${delta_sign}{tf['delta']:>11,.2f}  {'(net buying)' if tf['delta'] > 0 else '(net selling)' if tf['delta'] < 0 else '(balanced)'}")
        lines.append(f"  Aggressor:   {tf['aggressor_ratio']:>6.1f}% buyers  |  {100 - tf['aggressor_ratio']:.1f}% sellers")
        if tf["trade_speed"] > 0:
            lines.append(f"  Trade Speed: {tf['trade_speed']:.1f} trades/sec")
        if tf["vwap"] > 0:
            lines.append(f"  Trade VWAP:  ${tf['vwap']:,.2f}")

        # Large trades
        if tf["large_trades"]:
            lines.append(f"\n  Large Trades ({len(tf['large_trades'])} detected):")
            for lt in tf["large_trades"][:5]:
                side_icon = "B" if lt["side"] == "buy" else "S"
                lines.append(f"    [{side_icon}] ${lt['price']:,.2f}  qty={lt['qty']:.6f}  ({lt['ratio']:.1f}x median)")

        # Funding & OI section
        lines.append(f"\n{'─' * w}")
        lines.append("  [Derivatives]")
        if fr["funding_rate"] is not None:
            fr_label = "Long pay Short" if fr["funding_rate"] > 0 else "Short pay Long" if fr["funding_rate"] < 0 else "Neutral"
            lines.append(f"  Funding Rate: {fr['funding_rate']:.4f}%  ({fr_label})")
            if fr["annual_rate"] is not None:
                lines.append(f"  Annualized:   {fr['annual_rate']:.2f}%")
            lines.append(f"  Crowd Sentiment: {fr['sentiment'].replace('_', ' ').title()}")
        else:
            lines.append("  Funding Rate: N/A (spot only or data unavailable)")

        if oi_data["oi"] is not None:
            lines.append(f"  Open Interest: {oi_data['oi']:,.4f} {self.coin}")
            if oi_data["oi_usd"]:
                lines.append(f"  OI (USD):      ${oi_data['oi_usd']:,.2f}")
        else:
            lines.append("  Open Interest: N/A")

        # Scoring breakdown
        lines.append(f"\n{'─' * w}")
        lines.append("  [Signal Breakdown]")
        for sig_name, sig_val, sig_detail in scoring["signals"]:
            name_display = sig_name.replace("_", " ").title()
            lines.append(f"    {sig_val:>4}  {name_display:<24} {sig_detail}")

        # Summary
        lines.append(f"\n{'=' * w}")
        lines.append(f"  VERDICT: {verdict}  (Score: {score}/100)")
        if score >= 58:
            lines.append(f"  Order flow is bullish - net buying pressure detected")
        elif score <= 42:
            lines.append(f"  Order flow is bearish - net selling pressure detected")
        else:
            lines.append(f"  Order flow is mixed - no clear directional bias")
        lines.append("=" * w)

        return "\n".join(lines)

    # ── Run All ─────────────────────────────────────────────

    def run(self):
        """Run full order flow analysis and return results dict + CLI output."""
        ob = self.analyze_orderbook()
        tf = self.analyze_trades()
        fr = self.analyze_funding()
        oi_data = self.analyze_open_interest()
        scoring = self.compute_score(ob, tf, fr, oi_data)
        cli_output = self.format_cli_output(ob, tf, fr, oi_data, scoring)

        return {
            "orderbook": ob,
            "trade_flow": tf,
            "funding": fr,
            "open_interest": oi_data,
            "scoring": scoring,
            "cli_output": cli_output
        }


def _load_json(path):
    """Safely load JSON from file."""
    try:
        with open(path) as f:
            content = f.read().strip()
            if not content:
                return {}
            return json.loads(content)
    except FileNotFoundError:
        return {}
    except json.JSONDecodeError:
        return {}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Order Flow Analysis Engine")
    parser.add_argument("--orderbook", required=True, help="Path to orderbook JSON")
    parser.add_argument("--trades", required=True, help="Path to trades JSON")
    parser.add_argument("--funding", default=None, help="Path to funding rate JSON")
    parser.add_argument("--oi", default=None, help="Path to open interest JSON")
    parser.add_argument("--coin", default="BTC", help="Coin symbol")
    parser.add_argument("--output", default=None, help="Output JSON path")
    args = parser.parse_args()

    orderbook = _load_json(args.orderbook)
    trades = _load_json(args.trades)
    funding = _load_json(args.funding) if args.funding else {}
    oi = _load_json(args.oi) if args.oi else {}

    if not orderbook and not trades:
        print("Error: Both orderbook and trades data are empty. Check okx command and API responses.")
        sys.exit(1)

    engine = OrderFlowEngine(orderbook, trades, funding, oi, coin=args.coin)
    result = engine.run()

    # Print CLI formatted output to terminal
    print(result["cli_output"])

    # Save full JSON result if output path specified
    if args.output:
        json_result = {k: v for k, v in result.items() if k != "cli_output"}
        with open(args.output, "w") as f:
            json.dump(json_result, f, indent=2, default=str)
ORDERFLOW_ENGINE

