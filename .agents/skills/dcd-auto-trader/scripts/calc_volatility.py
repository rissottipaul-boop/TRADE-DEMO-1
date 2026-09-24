#!/usr/bin/env python3
"""
DCD 双币赢 — 波动率预测引擎 v3

用法（由 auto-trader 调用）:
    python3 scripts/calc_volatility.py --price 67000 \
        --iv-annual 0.55 \
        --atr-fast 1200 --atr-slow 1100 \
        --bb-upper 68500 --bb-lower 65500 \
        --funding-rate 0.0003 \
        --high-30d 74000 \
        --price-7d-ago 71000

输出 JSON:
    {
      "vol_24h": 0.0522,
      "buy_dist": 0.052,
      "strike_suggestion": 63500,
      "components": {...},
      "adjustments": {...}
    }

版本历史:
    v1: 简单 HV 加权
    v2: 双速ATR + BB + 资金费率 + 置信缓冲
    v3: v2 + 回撤安全垫 + 暴跌动量 + 动态ATR权重
"""

import argparse
import json
import math
import sys


def calc_funding_adj(rate: float) -> float:
    """资金费率 → 情绪调整"""
    abs_rate = abs(rate)
    if abs_rate < 0.0001:      # < 0.01%
        return 0.0
    elif rate < -0.0001:       # 负费率
        return 0.003
    elif abs_rate <= 0.0003:   # 0.01-0.03%
        return 0.002
    elif abs_rate <= 0.0005:   # 0.03-0.05%
        return 0.005
    else:                      # > 0.05%
        return 0.008


def calc_vol_v3(
    price: float,
    iv_annual: float,
    atr_fast_1h: float,
    atr_slow_1h: float,
    bb_upper: float,
    bb_lower: float,
    funding_rate: float,
    high_30d: float,
    price_7d_ago: float,
    is_near_event: bool = False,
) -> dict:
    """
    v3 波动率预测引擎

    参数:
        price:         当前 BTC 价格
        iv_annual:     期权隐含波动率（年化，如 0.55 = 55%）
        atr_fast_1h:   ATR(7) 1H 原始值（美元）
        atr_slow_1h:   ATR(14) 1H 原始值（美元）
        bb_upper:      1H BB 上轨
        bb_lower:      1H BB 下轨
        funding_rate:  资金费率（如 0.0003 = 0.03%）
        high_30d:      30天内最高价
        price_7d_ago:  7天前价格
        is_near_event: 是否临近事件日

    返回:
        dict 含 vol_24h, components, adjustments 等
    """

    # ── A. IV 成分 ──
    iv_daily = iv_annual / math.sqrt(365)

    # ── B. 双速 ATR（1H → 日化）──
    atr_fast = atr_fast_1h * math.sqrt(24) / price
    atr_slow = atr_slow_1h * math.sqrt(24) / price
    atr_component = max(atr_fast, atr_slow * 0.9)

    # ── C. BB 成分（1H → 日化）──
    bb_std_1h = (bb_upper - bb_lower) / 4
    bb_component = bb_std_1h * math.sqrt(24) / price

    # ── D. 资金费率 ──
    funding_adj = calc_funding_adj(funding_rate)

    # ── E. 动态权重 ──
    # ATR 远超 HV 时，说明近期波动飙升，HV 滞后不可信
    if iv_daily > 0 and atr_component > 2 * iv_daily:
        w_iv, w_atr = 0.20, 0.55  # ATR 主导
    else:
        w_iv, w_atr = 0.45, 0.30  # 标准权重

    comp_iv = w_iv * iv_daily
    comp_atr = w_atr * atr_component
    comp_bb = 0.15 * bb_component
    comp_funding = 0.10 * funding_adj

    vol_base = comp_iv + comp_atr + comp_bb + comp_funding

    # ── F. 置信缓冲 ──
    vol_accel = abs(atr_fast - atr_slow) / atr_slow if atr_slow > 0 else 0
    if vol_accel > 0.3 or is_near_event:
        conf_buffer = 0.010   # +1.0%
    elif vol_accel > 0.1:
        conf_buffer = 0.005   # +0.5%
    else:
        conf_buffer = 0.003   # +0.3%

    # ── G. 回撤安全垫（v3 新增）──
    drawdown_30d = (price - high_30d) / high_30d  # 负值 = 回撤中
    abs_dd = abs(drawdown_30d)
    if abs_dd > 0.30:
        drawdown_adj = 0.040  # +4.0%
    elif abs_dd > 0.20:
        drawdown_adj = 0.025  # +2.5%
    elif abs_dd > 0.10:
        drawdown_adj = 0.015  # +1.5%
    else:
        drawdown_adj = 0.0

    # ── H. 暴跌动量（v3 新增）──
    drop_7d = (price - price_7d_ago) / price_7d_ago if price_7d_ago > 0 else 0
    if drop_7d < -0.10:
        momentum_adj = 0.030  # +3.0%
    elif drop_7d < -0.05:
        momentum_adj = 0.015  # +1.5%
    else:
        momentum_adj = 0.0

    # ── 最终波动率 ──
    vol_24h = vol_base + conf_buffer + drawdown_adj + momentum_adj

    return {
        "vol_24h": round(vol_24h, 6),
        "components": {
            "iv_daily": round(iv_daily, 6),
            "atr_fast": round(atr_fast, 6),
            "atr_slow": round(atr_slow, 6),
            "atr_component": round(atr_component, 6),
            "bb_component": round(bb_component, 6),
            "funding_adj": round(funding_adj, 6),
            "comp_iv": round(comp_iv, 6),
            "comp_atr": round(comp_atr, 6),
            "comp_bb": round(comp_bb, 6),
            "comp_funding": round(comp_funding, 6),
            "vol_base": round(vol_base, 6),
        },
        "adjustments": {
            "conf_buffer": round(conf_buffer, 4),
            "vol_accel": round(vol_accel, 4),
            "drawdown_30d": round(drawdown_30d, 4),
            "drawdown_adj": round(drawdown_adj, 4),
            "drop_7d": round(drop_7d, 4),
            "momentum_adj": round(momentum_adj, 4),
        },
        "weights": {
            "w_iv": w_iv,
            "w_atr": w_atr,
            "w_bb": 0.15,
            "w_funding": 0.10,
            "mode": "ATR主导" if w_atr == 0.55 else "标准",
        },
    }


def calc_strike_and_dist(
    price: float,
    vol_24h: float,
    event_mult: float,
    min_dist: float,
    expiry_days: int = 1,
) -> dict:
    """
    根据波动率 → 计算行权价和安全距离

    buy_dist = max(vol_24h × √到期天数 × 事件乘数, 最小安全距离)
    行权价 = round_down_500(price × (1 - buy_dist))
    """
    raw_dist = vol_24h * math.sqrt(expiry_days) * event_mult
    buy_dist = max(raw_dist, min_dist)
    strike_raw = price * (1 - buy_dist)
    strike = int(strike_raw // 500) * 500  # 向下取整到 500
    actual_dist = (price - strike) / price if price > 0 else 0

    return {
        "buy_dist": round(buy_dist, 6),
        "raw_dist": round(raw_dist, 6),
        "strike_suggestion": strike,
        "actual_dist": round(actual_dist, 6),
        "limited_by": "min_dist" if min_dist > raw_dist else "volatility",
    }


# ── 事件乘数表 ──

EVENT_MULTIPLIERS = {
    "FOMC":  {"day": 1.8, "before": 1.8, "after": 1.3},
    "CPI":   {"day": 1.5, "before": 1.5, "after": 1.2},
    "CME":   {"day": 1.5, "before": 1.5, "after": 1.3},
    "NFP":   {"day": 1.4, "before": 1.4, "after": 1.2},
    "MULTI": {"day": 1.8, "before": 1.8, "after": 1.5},
}


def get_event_mult(event_type: str, position: str):
    """
    获取事件乘数
    event_type: FOMC/CPI/CME/NFP/MULTI
    position: day/before/after
    返回 None = 不交易
    """
    if event_type not in EVENT_MULTIPLIERS:
        return 1.0
    return EVENT_MULTIPLIERS[event_type].get(position, 1.0)


# ── CLI ──

def main():
    parser = argparse.ArgumentParser(description="DCD 波动率预测引擎 v3")
    parser.add_argument("--price", type=float, required=True, help="当前 BTC 价格")
    parser.add_argument("--iv-annual", type=float, default=0.5, help="年化 IV（如 0.55）")
    parser.add_argument("--atr-fast", type=float, required=True, help="ATR(7) 1H 原始值（美元）")
    parser.add_argument("--atr-slow", type=float, required=True, help="ATR(14) 1H 原始值（美元）")
    parser.add_argument("--bb-upper", type=float, required=True, help="1H BB 上轨")
    parser.add_argument("--bb-lower", type=float, required=True, help="1H BB 下轨")
    parser.add_argument("--funding-rate", type=float, default=0.0, help="资金费率")
    parser.add_argument("--high-30d", type=float, required=True, help="30天最高价")
    parser.add_argument("--price-7d-ago", type=float, required=True, help="7天前价格")
    parser.add_argument("--event-mult", type=float, default=1.0, help="事件乘数")
    parser.add_argument("--min-dist", type=float, default=0.035, help="最小安全距离")
    parser.add_argument("--expiry-days", type=int, default=1, help="到期天数")
    parser.add_argument("--near-event", action="store_true", help="临近事件日")

    args = parser.parse_args()

    # 计算波动率
    vol_result = calc_vol_v3(
        price=args.price,
        iv_annual=args.iv_annual,
        atr_fast_1h=args.atr_fast,
        atr_slow_1h=args.atr_slow,
        bb_upper=args.bb_upper,
        bb_lower=args.bb_lower,
        funding_rate=args.funding_rate,
        high_30d=args.high_30d,
        price_7d_ago=args.price_7d_ago,
        is_near_event=args.near_event,
    )

    # 计算行权价
    strike_result = calc_strike_and_dist(
        price=args.price,
        vol_24h=vol_result["vol_24h"],
        event_mult=args.event_mult,
        min_dist=args.min_dist,
        expiry_days=args.expiry_days,
    )

    output = {**vol_result, **strike_result}
    print(json.dumps(output, indent=2, ensure_ascii=False))


if __name__ == "__main__":
    main()
