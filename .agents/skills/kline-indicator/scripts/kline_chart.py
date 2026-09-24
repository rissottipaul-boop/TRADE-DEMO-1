#!/usr/bin/env python3
"""
Chart Engine for OKX TradeKit (okx-trade-mcp / okx-trade-cli) K-Line Indicators Skill.
Generates multi-panel technical analysis charts.

Supports two output modes:
  1. Terminal (plotext) — renders directly in CLI
  2. PNG (matplotlib + mplfinance) — saves high-res image

Usage:
    python3 kline_chart.py --candles /tmp/candles.json --indicators /tmp/indicators.json --mode terminal
    python3 kline_chart.py --candles /tmp/candles.json --indicators /tmp/indicators.json --mode png --output /tmp/chart.png
    python3 kline_chart.py --candles /tmp/candles.json --indicators /tmp/indicators.json --mode both --output /tmp/chart.png

Dependencies:
  Terminal mode: plotext (pip install plotext)
  PNG mode: matplotlib, mplfinance (pip install matplotlib mplfinance)
"""

import json, sys, argparse, math
from datetime import datetime

# ============================================================
# Terminal Chart Engine (plotext)
# ============================================================

def render_terminal(candles, indicators, coin="BTC", bar="1H", width=160, last_n=80):
    """Render multi-panel chart in terminal using plotext."""
    try:
        import plotext as plt
    except ImportError:
        print("[WARN] plotext not installed. Run: pip install plotext")
        return False

    c_data = candles[-last_n:]
    n = len(c_data)
    ts   = list(range(n))
    opens  = [float(c[1]) for c in c_data]
    highs  = [float(c[2]) for c in c_data]
    lows   = [float(c[3]) for c in c_data]
    closes = [float(c[4]) for c in c_data]
    vols   = [float(c[5]) if len(c) > 5 else 0 for c in c_data]

    # Time labels
    dates = []
    for c in c_data:
        t = float(c[0])
        if t > 1e12: t /= 1000
        try: dates.append(datetime.fromtimestamp(t).strftime("%m/%d %H:%M"))
        except Exception: dates.append("")

    # Extract indicator data
    ind = indicators if indicators else {}
    trend = ind.get("trend", {})
    momentum = ind.get("momentum", {})
    vol_data = ind.get("volume", {})
    patterns = ind.get("patterns", {})
    divergence = ind.get("divergence", {})
    structure = ind.get("structure", {})

    # --- Compute overlay series for last_n candles ---
    # MA series (recalculate for display range)
    def sma_s(data, period):
        r = []
        for i in range(len(data)):
            if i < period - 1: r.append(None)
            else: r.append(sum(data[i-period+1:i+1]) / period)
        return r

    ma7 = sma_s(closes, 7)
    ma25 = sma_s(closes, 25)

    # Bollinger Bands
    bb_upper = []; bb_lower = []
    for i in range(len(closes)):
        if i < 19:
            bb_upper.append(None); bb_lower.append(None)
        else:
            subset = closes[i-19:i+1]
            m = sum(subset)/20
            sd = math.sqrt(sum((x-m)**2 for x in subset)/20)
            bb_upper.append(m + 2*sd); bb_lower.append(m - 2*sd)

    # RSI series
    def rsi_series(data, period=14):
        r = [None] * period
        gains = []; losses = []
        for i in range(1, len(data)):
            d = data[i] - data[i-1]
            gains.append(max(d,0)); losses.append(max(-d,0))
        if len(gains) < period: return [None]*len(data)
        ag = sum(gains[:period])/period; al = sum(losses[:period])/period
        if al == 0: r.append(100.0)
        else: r.append(100.0 - 100.0/(1.0+ag/al))
        for i in range(period, len(gains)):
            ag = (ag*(period-1)+gains[i])/period
            al = (al*(period-1)+losses[i])/period
            if al == 0: r.append(100.0)
            else: r.append(100.0 - 100.0/(1.0+ag/al))
        return r[:len(data)]

    rsi = rsi_series(closes)

    # MACD series
    def ema_s(data, period):
        r = []; k = 2.0/(period+1)
        if len(data) < period: return [None]*len(data)
        r = [None]*(period-1)
        val = sum(data[:period])/period; r.append(val)
        for i in range(period, len(data)):
            val = data[i]*k + val*(1-k); r.append(val)
        return r

    ema12 = ema_s(closes, 12); ema26 = ema_s(closes, 26)
    macd_line = []; macd_signal = []; macd_hist = []
    for f, s in zip(ema12, ema26):
        if f is not None and s is not None: macd_line.append(f-s)
        else: macd_line.append(None)
    valid_macd = [x for x in macd_line if x is not None]
    sig_s = ema_s(valid_macd, 9) if len(valid_macd) >= 9 else [None]*len(valid_macd)
    # Align signal back
    macd_signal = [None]*(len(macd_line)-len(sig_s)) + sig_s
    for m, s in zip(macd_line, macd_signal):
        if m is not None and s is not None: macd_hist.append(m-s)
        else: macd_hist.append(None)

    # Support/Resistance levels from indicator data
    sr_levels = []
    if structure:
        for sr in structure.get("support_resistance", [])[:6]:
            if sr.get("price"): sr_levels.append((sr["price"], sr["type"]))
        ns = structure.get("nearest_support")
        nr = structure.get("nearest_resistance")

    # Pattern annotations
    pat_markers = []
    if patterns and patterns.get("detected"):
        for p in patterns["detected"]:
            direction = "▲" if p["direction"] == "bullish" else ("▼" if p["direction"] == "bearish" else "◆")
            pat_markers.append(f'{direction} {p["name"]}')

    # ===== RENDER =====
    plt.clear_figure()
    plt.theme("dark")
    plt.plot_size(width, 50)
    plt.title(f"  {coin}-USDT  |  {bar}  |  {dates[0]} → {dates[-1]}  ")

    # --- Subplot 1: Candlestick + MA + BB + S/R (3/5 height) ---
    plt.subplot(4, 1, 1)
    plt.plot_size(width, 22)

    # Candle bodies as colored bars
    for i in range(n):
        color = "green" if closes[i] >= opens[i] else "red"
        # Wick (high-low)
        plt.plot([ts[i], ts[i]], [lows[i], highs[i]], color="gray")

    # Plot close as line (main price action)
    plt.plot(ts, closes, color="white", label="Close")

    # MA overlays
    ma7_clean = [(ts[i], ma7[i]) for i in range(n) if ma7[i] is not None]
    ma25_clean = [(ts[i], ma25[i]) for i in range(n) if ma25[i] is not None]
    if ma7_clean:
        plt.plot([x[0] for x in ma7_clean], [x[1] for x in ma7_clean], color="yellow", label="MA7")
    if ma25_clean:
        plt.plot([x[0] for x in ma25_clean], [x[1] for x in ma25_clean], color="cyan", label="MA25")

    # Bollinger Bands
    bbu = [(ts[i], bb_upper[i]) for i in range(n) if bb_upper[i] is not None]
    bbl = [(ts[i], bb_lower[i]) for i in range(n) if bb_lower[i] is not None]
    if bbu:
        plt.plot([x[0] for x in bbu], [x[1] for x in bbu], color="blue+", label="BB Up")
        plt.plot([x[0] for x in bbl], [x[1] for x in bbl], color="blue+", label="BB Lo")

    # S/R horizontal lines
    for price, stype in sr_levels[:4]:
        color = "green+" if stype == "support" else "red+"
        plt.hline(price, color)

    plt.ylabel("Price")

    # --- Subplot 2: MACD ---
    plt.subplot(4, 1, 2)
    plt.plot_size(width, 10)
    mh_ts = [ts[i] for i in range(n) if macd_hist[i] is not None]
    mh_vals = [macd_hist[i] for i in range(n) if macd_hist[i] is not None]
    if mh_ts:
        pos_t = [mh_ts[i] for i in range(len(mh_vals)) if mh_vals[i] >= 0]
        pos_v = [mh_vals[i] for i in range(len(mh_vals)) if mh_vals[i] >= 0]
        neg_t = [mh_ts[i] for i in range(len(mh_vals)) if mh_vals[i] < 0]
        neg_v = [mh_vals[i] for i in range(len(mh_vals)) if mh_vals[i] < 0]
        if pos_t: plt.bar(pos_t, pos_v, color="green", width=0.6)
        if neg_t: plt.bar(neg_t, neg_v, color="red", width=0.6)
    ml_ts = [ts[i] for i in range(n) if macd_line[i] is not None]
    ml_vals = [macd_line[i] for i in range(n) if macd_line[i] is not None]
    if ml_ts: plt.plot(ml_ts, ml_vals, color="cyan", label="MACD")
    ms_ts = [ts[i] for i in range(n) if macd_signal[i] is not None]
    ms_vals = [macd_signal[i] for i in range(n) if macd_signal[i] is not None]
    if ms_ts: plt.plot(ms_ts, ms_vals, color="orange", label="Signal")
    plt.ylabel("MACD")

    # --- Subplot 3: RSI ---
    plt.subplot(4, 1, 3)
    plt.plot_size(width, 8)
    rsi_ts = [ts[i] for i in range(n) if rsi[i] is not None]
    rsi_vals = [rsi[i] for i in range(n) if rsi[i] is not None]
    if rsi_ts: plt.plot(rsi_ts, rsi_vals, color="magenta", label="RSI14")
    plt.hline(70, "red")
    plt.hline(30, "green")
    plt.hline(50, "gray")
    plt.ylabel("RSI")
    plt.ylim(0, 100)

    # --- Subplot 4: Volume ---
    plt.subplot(4, 1, 4)
    plt.plot_size(width, 7)
    vol_colors = ["green" if closes[i] >= opens[i] else "red" for i in range(n)]
    # plotext bar with individual colors
    for i in range(n):
        plt.bar([ts[i]], [vols[i]], color=vol_colors[i], width=0.8)
    plt.ylabel("Vol")

    plt.show()

    # Print annotations below chart
    if pat_markers:
        print(f"\n  🔍 K线形态: {' | '.join(pat_markers[:5])}")
    if divergence and divergence.get("detected"):
        divs = [f'{"🟢" if "bullish" in d["type"] else "🔴"} {d["indicator"]} {d["type"]}' for d in divergence["detected"][:3]]
        print(f"  📊 背离信号: {' | '.join(divs)}")
    if structure:
        ms = structure.get("market_structure", {})
        trend_label = ms.get("trend", "N/A")
        ns = structure.get("nearest_support")
        nr = structure.get("nearest_resistance")
        print(f"  📐 市场结构: {trend_label}", end="")
        if ns: print(f"  |  支撑: ${ns['price']}", end="")
        if nr: print(f"  |  阻力: ${nr['price']}", end="")
        print()

    return True


# ============================================================
# Helpers
# ============================================================

def dates_fmt(dt):
    try: return dt.strftime("%m/%d %H:%M")
    except Exception: return ""


# ============================================================
# PNG Chart Engine (matplotlib + mplfinance)
# ============================================================

def render_png(candles, indicators, coin="BTC", bar="1H", output="/tmp/chart.png", last_n=100):
    """Render professional multi-panel chart as PNG using matplotlib."""
    try:
        import matplotlib
        matplotlib.use('Agg')
        import matplotlib.pyplot as plt
        import matplotlib.dates as mdates
        from matplotlib.patches import FancyArrowPatch
        import matplotlib.ticker as ticker
    except ImportError:
        print("[WARN] matplotlib not installed. Run: pip install matplotlib")
        return False

    c_data = candles[-last_n:]
    n = len(c_data)

    # Parse data
    timestamps = []
    for c in c_data:
        t = float(c[0])
        if t > 1e12: t /= 1000
        try: timestamps.append(datetime.fromtimestamp(t))
        except Exception: timestamps.append(datetime.now())

    opens  = [float(c[1]) for c in c_data]
    highs  = [float(c[2]) for c in c_data]
    lows   = [float(c[3]) for c in c_data]
    closes = [float(c[4]) for c in c_data]
    vols   = [float(c[5]) if len(c) > 5 else 0 for c in c_data]

    ind = indicators if indicators else {}
    patterns = ind.get("patterns", {})
    divergence = ind.get("divergence", {})
    structure = ind.get("structure", {})

    # Compute indicator series
    def sma_s(data, p):
        r = []
        for i in range(len(data)):
            if i < p-1: r.append(None)
            else: r.append(sum(data[i-p+1:i+1])/p)
        return r

    def ema_s(data, p):
        if len(data) < p: return [None]*len(data)
        k = 2.0/(p+1); r = [None]*(p-1)
        val = sum(data[:p])/p; r.append(val)
        for i in range(p, len(data)): val = data[i]*k + val*(1-k); r.append(val)
        return r

    ma7 = sma_s(closes, 7)
    ma25 = sma_s(closes, 25)
    ma50 = sma_s(closes, 50)

    # Bollinger Bands
    bb_u = []; bb_l = []; bb_m = []
    for i in range(len(closes)):
        if i < 19: bb_u.append(None); bb_l.append(None); bb_m.append(None)
        else:
            s = closes[i-19:i+1]; m = sum(s)/20
            sd = math.sqrt(sum((x-m)**2 for x in s)/20)
            bb_u.append(m+2*sd); bb_l.append(m-2*sd); bb_m.append(m)

    # RSI
    def rsi_s(data, period=14):
        r = [None]*(period)
        gains=[]; losses=[]
        for i in range(1,len(data)):
            d=data[i]-data[i-1]; gains.append(max(d,0)); losses.append(max(-d,0))
        if len(gains)<period: return [None]*len(data)
        ag=sum(gains[:period])/period; al=sum(losses[:period])/period
        if al==0: r.append(100.0)
        else: r.append(100.0-100.0/(1.0+ag/al))
        for i in range(period,len(gains)):
            ag=(ag*(period-1)+gains[i])/period; al=(al*(period-1)+losses[i])/period
            if al==0: r.append(100.0)
            else: r.append(100.0-100.0/(1.0+ag/al))
        return r[:len(data)]
    rsi = rsi_s(closes)

    # MACD
    e12 = ema_s(closes, 12); e26 = ema_s(closes, 26)
    macd_l = [(e12[i]-e26[i]) if (e12[i] and e26[i]) else None for i in range(n)]
    valid_m = [x for x in macd_l if x is not None]
    sig_raw = ema_s(valid_m, 9) if len(valid_m)>=9 else [None]*len(valid_m)
    macd_sig = [None]*(n-len(sig_raw)) + sig_raw
    macd_h = [(macd_l[i]-macd_sig[i]) if (macd_l[i] and macd_sig[i]) else None for i in range(n)]

    # --- CREATE FIGURE ---
    fig = plt.figure(figsize=(20, 14), facecolor='#1a1a2e')
    fig.suptitle(f'{coin}-USDT  |  {bar}  |  Technical Analysis', fontsize=16, color='white', fontweight='bold', y=0.98)

    # GridSpec: main=50%, MACD=18%, RSI=16%, Vol=16%
    gs = fig.add_gridspec(4, 1, height_ratios=[5, 1.8, 1.6, 1.6], hspace=0.05)

    ax1 = fig.add_subplot(gs[0])  # Candlestick
    ax2 = fig.add_subplot(gs[1], sharex=ax1)  # MACD
    ax3 = fig.add_subplot(gs[2], sharex=ax1)  # RSI
    ax4 = fig.add_subplot(gs[3], sharex=ax1)  # Volume

    for ax in [ax1, ax2, ax3, ax4]:
        ax.set_facecolor('#16213e')
        ax.tick_params(colors='#8a8a8a', labelsize=8)
        ax.grid(True, alpha=0.15, color='#4a4a4a')
        for spine in ax.spines.values(): spine.set_color('#333355')

    xs = list(range(n))

    # === Panel 1: Candlestick ===
    for i in range(n):
        color = '#00e676' if closes[i] >= opens[i] else '#ff1744'
        body_lo = min(opens[i], closes[i]); body_hi = max(opens[i], closes[i])
        # Wick
        ax1.plot([xs[i], xs[i]], [lows[i], highs[i]], color=color, linewidth=0.8, alpha=0.8)
        # Body
        ax1.bar(xs[i], body_hi - body_lo, bottom=body_lo, color=color, width=0.6, edgecolor=color, linewidth=0.5)

    # MA lines
    def plot_series(ax, xs, data, color, label, lw=1.2):
        valid = [(xs[i], data[i]) for i in range(len(data)) if data[i] is not None]
        if valid: ax.plot([v[0] for v in valid], [v[1] for v in valid], color=color, linewidth=lw, label=label, alpha=0.85)

    plot_series(ax1, xs, ma7, '#ffeb3b', 'MA7')
    plot_series(ax1, xs, ma25, '#00bcd4', 'MA25')
    plot_series(ax1, xs, ma50, '#ff9800', 'MA50')

    # Bollinger Bands fill
    bbu_v = [(xs[i], bb_u[i]) for i in range(n) if bb_u[i]]
    bbl_v = [(xs[i], bb_l[i]) for i in range(n) if bb_l[i]]
    if bbu_v and bbl_v:
        bx = [v[0] for v in bbu_v]
        ax1.fill_between(bx, [v[1] for v in bbu_v], [bbl_v[i][1] for i in range(len(bbl_v))],
                         alpha=0.08, color='#2196f3')
        ax1.plot(bx, [v[1] for v in bbu_v], color='#2196f3', linewidth=0.8, alpha=0.5, linestyle='--')
        ax1.plot(bx, [v[1] for v in bbl_v], color='#2196f3', linewidth=0.8, alpha=0.5, linestyle='--')

    # Support/Resistance lines
    if structure:
        for sr in structure.get("support_resistance", [])[:6]:
            if sr.get("price"):
                color = '#00e676' if sr["type"] == "support" else '#ff1744'
                ax1.axhline(y=sr["price"], color=color, linewidth=0.8, linestyle=':', alpha=0.6)
                ax1.annotate(f'{"S" if sr["type"]=="support" else "R"} {sr["price"]:.1f}',
                    xy=(n-1, sr["price"]), fontsize=7, color=color, alpha=0.8,
                    xytext=(5, 0), textcoords='offset points')

    # Pattern markers
    if patterns and patterns.get("detected"):
        for p in patterns["detected"][:5]:
            marker = '▲' if p["direction"]=="bullish" else ('▼' if p["direction"]=="bearish" else '◆')
            color = '#00e676' if p["direction"]=="bullish" else ('#ff1744' if p["direction"]=="bearish" else '#ffeb3b')
            y_pos = lows[-1]*0.998 if p["direction"]=="bullish" else highs[-1]*1.002
            ax1.annotate(f'{marker} {p["name"]}', xy=(n-1, y_pos), fontsize=7, color=color,
                fontweight='bold', ha='right', xytext=(-5, -10 if p["direction"]=="bearish" else 10),
                textcoords='offset points')

    ax1.set_ylabel('Price', color='#8a8a8a', fontsize=9)
    ax1.legend(loc='upper left', fontsize=8, facecolor='#1a1a2e', edgecolor='#333355', labelcolor='#cccccc')
    plt.setp(ax1.get_xticklabels(), visible=False)

    # === Panel 2: MACD ===
    for i in range(n):
        if macd_h[i] is not None:
            color = '#00e676' if macd_h[i] >= 0 else '#ff1744'
            ax2.bar(xs[i], macd_h[i], color=color, width=0.6, alpha=0.7)
    plot_series(ax2, xs, macd_l, '#00bcd4', 'MACD', 1.0)
    plot_series(ax2, xs, macd_sig, '#ff9800', 'Signal', 1.0)
    ax2.axhline(y=0, color='#4a4a4a', linewidth=0.5)
    ax2.set_ylabel('MACD', color='#8a8a8a', fontsize=9)
    ax2.legend(loc='upper left', fontsize=7, facecolor='#1a1a2e', edgecolor='#333355', labelcolor='#cccccc')
    plt.setp(ax2.get_xticklabels(), visible=False)

    # Divergence arrows on MACD
    if divergence and divergence.get("detected"):
        for d in divergence["detected"]:
            if d["indicator"] == "MACD" and "bars_ago" in d:
                ba = d["bars_ago"]
                if isinstance(ba, (int, float)) and ba < n:
                    idx = n - 1 - int(ba)
                    if 0 <= idx < n and macd_l[idx] is not None:
                        color = '#00e676' if 'bullish' in d['type'] else '#ff1744'
                        marker = '▲' if 'bullish' in d['type'] else '▼'
                        ax2.annotate(f'{marker}', xy=(idx, macd_l[idx]), fontsize=12, color=color, ha='center',
                            fontweight='bold', xytext=(0, 15 if 'bullish' in d['type'] else -15),
                            textcoords='offset points',
                            arrowprops=dict(arrowstyle='->', color=color, lw=1.5))

    # === Panel 3: RSI ===
    plot_series(ax3, xs, rsi, '#e040fb', 'RSI14', 1.2)
    ax3.axhline(y=70, color='#ff1744', linewidth=0.8, linestyle='--', alpha=0.5)
    ax3.axhline(y=30, color='#00e676', linewidth=0.8, linestyle='--', alpha=0.5)
    ax3.axhline(y=50, color='#4a4a4a', linewidth=0.5, linestyle=':')
    ax3.fill_between(xs, 30, 70, alpha=0.05, color='#e040fb')
    ax3.set_ylim(0, 100)
    ax3.set_ylabel('RSI', color='#8a8a8a', fontsize=9)
    plt.setp(ax3.get_xticklabels(), visible=False)

    # RSI divergence arrows
    if divergence and divergence.get("detected"):
        for d in divergence["detected"]:
            if d["indicator"] == "RSI14" and "bars_ago" in d:
                ba = d["bars_ago"]
                if isinstance(ba, (int, float)) and ba < n:
                    idx = n - 1 - int(ba)
                    if 0 <= idx < n and rsi[idx] is not None:
                        color = '#00e676' if 'bullish' in d['type'] else '#ff1744'
                        marker = '▲' if 'bullish' in d['type'] else '▼'
                        ax3.annotate(f'{marker}', xy=(idx, rsi[idx]), fontsize=12, color=color, ha='center',
                            fontweight='bold', xytext=(0, 10 if 'bullish' in d['type'] else -10),
                            textcoords='offset points',
                            arrowprops=dict(arrowstyle='->', color=color, lw=1.5))

    # === Panel 4: Volume ===
    for i in range(n):
        color = '#00e67688' if closes[i] >= opens[i] else '#ff174488'
        ax4.bar(xs[i], vols[i], color=color, width=0.7)
    # Volume MA
    vol_ma = sma_s(vols, 20)
    plot_series(ax4, xs, vol_ma, '#ffeb3b', 'Vol MA20', 0.8)
    ax4.set_ylabel('Volume', color='#8a8a8a', fontsize=9)
    ax4.set_xlabel('Time', color='#8a8a8a', fontsize=9)

    # X-axis labels (show every nth date)
    step = max(1, n // 10)
    ax4.set_xticks([xs[i] for i in range(0, n, step)])
    ax4.set_xticklabels([dates_fmt(timestamps[i]) for i in range(0, n, step)], rotation=45, fontsize=7)

    # Info box
    last_p = closes[-1]; change = (closes[-1]/closes[-2]-1)*100 if len(closes)>=2 else 0
    info = f'Last: ${last_p:,.2f}  ({change:+.2f}%)'
    if rsi[-1]: info += f'  |  RSI: {rsi[-1]:.1f}'
    fig.text(0.5, 0.01, info, ha='center', fontsize=10, color='#cccccc',
        bbox=dict(boxstyle='round,pad=0.5', facecolor='#1a1a2e', edgecolor='#333355'))

    plt.savefig(output, dpi=150, bbox_inches='tight', facecolor='#1a1a2e', edgecolor='none')
    plt.close()
    print(f"Chart saved to {output}")
    return True


# ============================================================
# Main
# ============================================================

def main():
    parser = argparse.ArgumentParser(description="K-Line Chart Engine")
    parser.add_argument("--candles", required=True, help="Path to JSON candle data")
    parser.add_argument("--indicators", help="Path to JSON indicator data (from kline_ext_indicators.py)")
    parser.add_argument("--mode", choices=["terminal", "png", "both"], default="terminal",
                        help="Output mode: terminal (CLI display), png (save image), both")
    parser.add_argument("--output", default="/tmp/kline_chart.png", help="PNG output path")
    parser.add_argument("--coin", default="BTC", help="Coin symbol for title")
    parser.add_argument("--bar", default="1H", help="Timeframe for title")
    parser.add_argument("--last-n", type=int, default=80, help="Number of candles to display")
    parser.add_argument("--width", type=int, default=160, help="Terminal chart width")
    args = parser.parse_args()

    # Load candle data
    try:
        with open(args.candles) as f:
            content = f.read().strip()
            if not content:
                print("Error: Candle data file is empty. Check if data fetch succeeded."); sys.exit(1)
            raw = json.loads(content)
    except FileNotFoundError:
        print(f"Error: Candle file not found: {args.candles}"); sys.exit(1)
    except json.JSONDecodeError as e:
        print(f"Error: Invalid JSON in candle data: {e}"); sys.exit(1)
    candles = raw.get("data", raw.get("candles", raw)) if isinstance(raw, dict) else raw
    if not candles:
        print("Error: No candle data in file"); sys.exit(1)

    # Load indicator data
    indicators = {}
    if args.indicators:
        try:
            with open(args.indicators) as f:
                content = f.read().strip()
                if content:
                    indicators = json.loads(content)
        except (FileNotFoundError, json.JSONDecodeError) as e:
            print(f"[WARN] Could not load indicators: {e}")
            indicators = {}

    if args.mode in ("terminal", "both"):
        ok = render_terminal(candles, indicators, args.coin, args.bar, args.width, args.last_n)
        if not ok:
            print("Terminal rendering failed, trying PNG fallback...")
            args.mode = "png"

    if args.mode in ("png", "both"):
        render_png(candles, indicators, args.coin, args.bar, args.output, min(args.last_n + 20, 120))
        print(f"PNG chart: {args.output}")


if __name__ == "__main__":
    main()
