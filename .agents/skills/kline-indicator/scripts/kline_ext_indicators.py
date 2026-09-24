#!/usr/bin/env python3
"""
Extended Indicator Engine for OKX TradeKit (okx-trade-mcp / okx-trade-cli) K-Line Indicators Skill.
Computes technical indicators + candlestick patterns + divergence + support/resistance
from raw OHLCV candle data.

Usage:
    python3 kline_ext_indicators.py --candles /tmp/candles.json --mode full --output /tmp/indicators.json
    python3 kline_ext_indicators.py --candles /tmp/candles.json --mode category --categories trend,momentum,patterns
    python3 kline_ext_indicators.py --candles /tmp/candles.json --mode custom --indicators "RSI_14,MACD,BB_20_2"
    python3 kline_ext_indicators.py --list
    python3 kline_ext_indicators.py --count

Input: JSON array of candles [[ts, o, h, l, c, vol, ...], ...]
Output: JSON with indicator values grouped by category
Dependencies: Python 3.8+ (stdlib only, no external packages required)
"""

import json, math, sys, argparse
from typing import List, Dict, Optional, Tuple, Any
from collections import OrderedDict


# --- Core math helpers ---

def sma(data, period):
    if len(data) < period: return None
    return sum(data[-period:]) / period

def sma_series(data, period):
    result = []
    for i in range(len(data)):
        if i < period - 1: result.append(None)
        else: result.append(sum(data[i - period + 1:i + 1]) / period)
    return result

def ema(data, period):
    if len(data) < period: return None
    k = 2.0 / (period + 1)
    val = sum(data[:period]) / period
    for price in data[period:]: val = price * k + val * (1 - k)
    return val

def ema_series(data, period):
    if len(data) < period: return [None] * len(data)
    k = 2.0 / (period + 1)
    result = [None] * (period - 1)
    val = sum(data[:period]) / period
    result.append(val)
    for i in range(period, len(data)):
        val = data[i] * k + val * (1 - k)
        result.append(val)
    return result

def wma(data, period):
    if len(data) < period: return None
    weights = list(range(1, period + 1))
    return sum(d * w for d, w in zip(data[-period:], weights)) / sum(weights)

def hma(data, period):
    half = max(period // 2, 1); sqrt_p = max(int(math.sqrt(period)), 1)
    vals = []
    for i in range(len(data)):
        w_h = wma(data[:i+1], half); w_f = wma(data[:i+1], period)
        if w_h is not None and w_f is not None: vals.append(2 * w_h - w_f)
    if len(vals) < sqrt_p: return None
    return wma(vals, sqrt_p)

def dema(data, period):
    e1 = ema(data, period); es = ema_series(data, period)
    v = [x for x in es if x is not None]; e2 = ema(v, period) if len(v) >= period else None
    if e1 is None or e2 is None: return None
    return 2 * e1 - e2

def tema(data, period):
    e1s = ema_series(data, period); v1 = [x for x in e1s if x is not None]
    e2s = ema_series(v1, period) if len(v1) >= period else []; v2 = [x for x in e2s if x is not None] if e2s else []
    e1 = ema(data, period); e2 = ema(v1, period) if len(v1) >= period else None; e3 = ema(v2, period) if len(v2) >= period else None
    if e1 is None or e2 is None or e3 is None: return None
    return 3 * e1 - 3 * e2 + e3

def kama_val(data, period=10, fast=2, slow=30):
    if len(data) < period + 1: return None
    direction = abs(data[-1] - data[-period - 1])
    volatility = sum(abs(data[i] - data[i-1]) for i in range(-period, 0))
    er = direction / volatility if volatility else 0
    sc = (er * (2.0/(fast+1) - 2.0/(slow+1)) + 2.0/(slow+1)) ** 2
    val = sum(data[:period]) / period
    for price in data[period:]: val = val + sc * (price - val)
    return val

def stdev(data, period):
    if len(data) < period: return None
    subset = data[-period:]; avg = sum(subset)/period
    return math.sqrt(sum((x-avg)**2 for x in subset)/period)

def true_range(h, l, prev_c):
    return max(h - l, abs(h - prev_c), abs(l - prev_c))

def atr_val(highs, lows, closes, period):
    if len(closes) < period + 1: return None
    trs = [true_range(highs[i], lows[i], closes[i-1]) for i in range(1, len(closes))]
    if len(trs) < period: return None
    return sum(trs[-period:]) / period


# --- Alpha factor building blocks ---

def delay(data, d):
    """Value d periods ago."""
    if len(data) <= d: return None
    return data[-d-1]

def delta_s(data, d):
    """x[t] - x[t-d]."""
    if len(data) <= d: return None
    return data[-1] - data[-d-1]

def ts_sum(data, d):
    if len(data) < d: return None
    return sum(data[-d:])

def ts_mean(data, d):
    if len(data) < d: return None
    return sum(data[-d:]) / d

def ts_stddev(data, d):
    return stdev(data, d)

def ts_rank(data, d):
    """Percentile rank of current value within last d values (0~1)."""
    if len(data) < d: return None
    window = data[-d:]
    val = data[-1]
    return sum(1 for x in window if x <= val) / d

def ts_min(data, d):
    if len(data) < d: return None
    return min(data[-d:])

def ts_max(data, d):
    if len(data) < d: return None
    return max(data[-d:])

def ts_argmin(data, d):
    """Days since min in last d values (0 = today is min)."""
    if len(data) < d: return None
    window = data[-d:]
    return d - 1 - window.index(min(window))

def ts_argmax(data, d):
    """Days since max in last d values (0 = today is max)."""
    if len(data) < d: return None
    window = data[-d:]
    return d - 1 - window.index(max(window))

def ts_corr(x, y, d):
    """Pearson correlation of last d values."""
    if len(x) < d or len(y) < d: return None
    xw, yw = x[-d:], y[-d:]
    mx, my = sum(xw)/d, sum(yw)/d
    cov = sum((xw[i]-mx)*(yw[i]-my) for i in range(d))/d
    sx = math.sqrt(sum((v-mx)**2 for v in xw)/d)
    sy = math.sqrt(sum((v-my)**2 for v in yw)/d)
    if sx == 0 or sy == 0: return 0
    return max(-1, min(1, cov / (sx * sy)))

def ts_cov(x, y, d):
    if len(x) < d or len(y) < d: return None
    xw, yw = x[-d:], y[-d:]
    mx, my = sum(xw)/d, sum(yw)/d
    return sum((xw[i]-mx)*(yw[i]-my) for i in range(d))/d

def decay_linear(data, d):
    """Linearly decaying weighted average (recent weight higher)."""
    if len(data) < d: return None
    weights = list(range(1, d+1))
    window = data[-d:]
    return sum(w*v for w, v in zip(weights, window)) / sum(weights)

def ts_product(data, d):
    if len(data) < d: return None
    result = 1.0
    for v in data[-d:]:
        result *= v
        if abs(result) > 1e20: return None
    return result

def returns_series(closes):
    """Return series: (c[i]-c[i-1])/c[i-1]."""
    return [0.0] + [(closes[i]-closes[i-1])/closes[i-1] if closes[i-1] != 0 else 0 for i in range(1, len(closes))]

def adv_series(volumes, d):
    """Rolling average volume series."""
    result = []
    for i in range(len(volumes)):
        if i < d - 1: result.append(sum(volumes[:i+1])/(i+1))
        else: result.append(sum(volumes[i-d+1:i+1])/d)
    return result

def vwap_full(highs, lows, closes, volumes):
    """Per-bar typical price (proxy for VWAP)."""
    return [(highs[i]+lows[i]+closes[i])/3 for i in range(len(closes))]

def signed_power(x, a):
    if x is None: return None
    return math.copysign(abs(x)**a, x) if x != 0 else 0

def log_safe(x):
    return math.log(x) if x and x > 0 else 0

def ts_rank_series(data, d):
    """Full ts_rank series. None-safe."""
    result = []
    for i in range(len(data)):
        if i < d - 1 or data[i] is None: result.append(None)
        else:
            window = [data[j] for j in range(i-d+1, i+1) if data[j] is not None]
            if len(window) < 2: result.append(None)
            else: result.append(sum(1 for x in window if x <= data[i]) / len(window))
    return result

def delta_series(data, d):
    """Full delta series. None-safe."""
    result = [None]*d
    for i in range(d, len(data)):
        if data[i] is None or data[i-d] is None: result.append(None)
        else: result.append(data[i]-data[i-d])
    return result

def ts_corr_series(x, y, d):
    """Full correlation series. None-safe."""
    result = []
    for i in range(len(x)):
        if i < d - 1: result.append(None)
        else:
            xw = x[i-d+1:i+1]; yw = y[i-d+1:i+1]
            if any(v is None for v in xw) or any(v is None for v in yw):
                result.append(None); continue
            mx = sum(xw)/d; my = sum(yw)/d
            cov = sum((xw[j]-mx)*(yw[j]-my) for j in range(d))/d
            sx = math.sqrt(sum((v-mx)**2 for v in xw)/d)
            sy = math.sqrt(sum((v-my)**2 for v in yw)/d)
            if sx == 0 or sy == 0: result.append(0)
            else: result.append(max(-1, min(1, cov/(sx*sy))))
    return result


class IndicatorEngine:
    DEFAULT_PERIODS = [5, 7, 9, 10, 14, 20, 21, 25, 30, 50, 100, 200]

    def __init__(self, candles, periods=None):
        self.periods = periods or self.DEFAULT_PERIODS
        self.ts = [float(c[0]) for c in candles]
        self.opens = [float(c[1]) for c in candles]
        self.highs = [float(c[2]) for c in candles]
        self.lows = [float(c[3]) for c in candles]
        self.closes = [float(c[4]) for c in candles]
        self.volumes = [float(c[5]) if len(c) > 5 else 0.0 for c in candles]
        self.n = len(candles)

    def _s(self, val, d=6):
        if val is None or (isinstance(val, float) and (math.isnan(val) or math.isinf(val))): return None
        return round(val, d)

    # ---- Trend ----
    def compute_trend(self):
        r = {}; c = self.closes
        for p in self.periods:
            r[f"sma_{p}"] = self._s(sma(c,p)); r[f"ema_{p}"] = self._s(ema(c,p))
            r[f"wma_{p}"] = self._s(wma(c,p)); r[f"dema_{p}"] = self._s(dema(c,p))
            r[f"tema_{p}"] = self._s(tema(c,p)); r[f"hma_{p}"] = self._s(hma(c,p))
            if p <= 30: r[f"kama_{p}"] = self._s(kama_val(c,p))
        for mult in [2,3]:
            for p in [7,10,14,20]:
                a = atr_val(self.highs,self.lows,c,p)
                if a:
                    hl2 = (self.highs[-1]+self.lows[-1])/2
                    up,lo = hl2+mult*a, hl2-mult*a
                    d = "up" if c[-1]>up else ("down" if c[-1]<lo else "up")
                    r[f"supertrend_{p}_{mult}"] = {"value": self._s(lo if d=="up" else up), "direction": d}
        if self.n >= 52:
            h,l = self.highs,self.lows
            r["ichimoku"] = {"tenkan": self._s((max(h[-9:])+min(l[-9:]))/2), "kijun": self._s((max(h[-26:])+min(l[-26:]))/2),
                "senkou_a": self._s(((max(h[-9:])+min(l[-9:]))/2+(max(h[-26:])+min(l[-26:]))/2)/2),
                "senkou_b": self._s((max(h[-52:])+min(l[-52:]))/2), "chikou": self._s(c[-1])}
        sar = self._parabolic_sar()
        if sar is not None: r["parabolic_sar"] = self._s(sar)
        for p in [14,20,25,50]:
            if len(self.highs) >= p+1:
                rh = self.highs[-(p+1):]; rl = self.lows[-(p+1):]
                r[f"aroon_up_{p}"] = self._s(rh.index(max(rh))/p*100)
                r[f"aroon_down_{p}"] = self._s(rl.index(min(rl))/p*100)
                r[f"aroon_osc_{p}"] = self._s(r[f"aroon_up_{p}"]-(r[f"aroon_down_{p}"] or 0))
        for p in [7,14,20,25]:
            adx = self._adx(p)
            if adx: r[f"adx_{p}"] = self._s(adx["adx"]); r[f"plus_di_{p}"] = self._s(adx["plus_di"]); r[f"minus_di_{p}"] = self._s(adx["minus_di"])
        for p in [7,14,21,28]:
            if len(c) >= p+1:
                vm_p = sum(abs(self.highs[i]-self.lows[i-1]) for i in range(-p,0))
                vm_m = sum(abs(self.lows[i]-self.highs[i-1]) for i in range(-p,0))
                tr_s = sum(true_range(self.highs[i],self.lows[i],c[i-1]) for i in range(-p,0))
                if tr_s > 0: r[f"vortex_pos_{p}"] = self._s(vm_p/tr_s); r[f"vortex_neg_{p}"] = self._s(vm_m/tr_s)
        for p in [14,20,30]:
            shift = p//2+1
            if len(c) >= p+shift:
                s = sma(c[:-shift], p)
                if s: r[f"dpo_{p}"] = self._s(c[-shift]-s)
        return r

    # ---- Momentum ----
    def compute_momentum(self):
        r = {}; c = self.closes; h,l,v = self.highs,self.lows,self.volumes
        for p in self.periods:
            rv = self._rsi(c,p)
            if rv is not None: r[f"rsi_{p}"] = self._s(rv)
        for p in [5,9,14,21]:
            stoch = self._stochastic(p)
            if stoch: r[f"stoch_k_{p}"] = self._s(stoch["k"]); r[f"stoch_d_{p}"] = self._s(stoch["d"])
        for p in [14,21]:
            sr = self._stoch_rsi(p)
            if sr is not None: r[f"stoch_rsi_{p}"] = self._s(sr)
        for p in [7,14,21,28]:
            if len(h) >= p:
                hh = max(h[-p:]); ll = min(l[-p:])
                r[f"williams_r_{p}"] = self._s((hh-c[-1])/(hh-ll)*-100 if hh!=ll else -50)
        for p in [7,14,20,50]:
            cci = self._cci(p)
            if cci is not None: r[f"cci_{p}"] = self._s(cci)
        for p in [5,10,14,20,50]:
            if len(c)>p and c[-p-1]!=0: r[f"roc_{p}"] = self._s((c[-1]-c[-p-1])/c[-p-1]*100)
        for p in [5,10,14,20]:
            if len(c)>p:
                r[f"momentum_{p}"] = self._s(c[-1]-c[-p-1])
                if c[-p-1]!=0: r[f"momentum_pct_{p}"] = self._s((c[-1]-c[-p-1])/c[-p-1]*100)
        for fast,slow,sig in [(12,26,9),(5,35,5),(8,17,9)]:
            m = self._macd(c,fast,slow,sig)
            if m: lb = f"{fast}_{slow}_{sig}"; r[f"macd_{lb}"] = self._s(m["macd"]); r[f"macd_signal_{lb}"] = self._s(m["signal"]); r[f"macd_hist_{lb}"] = self._s(m["hist"])
        for p in [12,15,18]:
            trix = self._trix(c,p)
            if trix is not None: r[f"trix_{p}"] = self._s(trix)
        kst = self._kst(c)
        if kst: r["kst"] = self._s(kst["kst"]); r["kst_signal"] = self._s(kst["signal"])
        uo = self._ultimate_osc()
        if uo is not None: r["ultimate_osc"] = self._s(uo)
        for p in [10,14]:
            rvi = self._rvi(p)
            if rvi is not None: r[f"rvi_{p}"] = self._s(rvi)
        return r

    # ---- Volatility ----
    def compute_volatility(self):
        r = {}; c = self.closes
        for p in [10,20,30,50]:
            for mult in [1.5,2.0,2.5,3.0]:
                mid = sma(c,p); sd = stdev(c,p)
                if mid and sd:
                    u,lo = mid+mult*sd, mid-mult*sd; lb = f"{p}_{mult}"
                    r[f"bb_upper_{lb}"] = self._s(u); r[f"bb_mid_{lb}"] = self._s(mid); r[f"bb_lower_{lb}"] = self._s(lo)
                    r[f"bb_width_{lb}"] = self._s((u-lo)/mid*100 if mid else 0)
                    r[f"bb_pctb_{lb}"] = self._s((c[-1]-lo)/(u-lo) if u!=lo else 0.5)
        for p in [10,20,30]:
            mid = ema(c,p); a = atr_val(self.highs,self.lows,c,p)
            if mid and a: r[f"keltner_upper_{p}"] = self._s(mid+1.5*a); r[f"keltner_mid_{p}"] = self._s(mid); r[f"keltner_lower_{p}"] = self._s(mid-1.5*a)
        for p in [10,20,30,50]:
            if len(self.highs)>=p:
                u = max(self.highs[-p:]); lo = min(self.lows[-p:])
                r[f"donchian_upper_{p}"] = self._s(u); r[f"donchian_lower_{p}"] = self._s(lo); r[f"donchian_mid_{p}"] = self._s((u+lo)/2)
        for p in self.periods:
            a = atr_val(self.highs,self.lows,c,p)
            if a: r[f"atr_{p}"] = self._s(a); r[f"natr_{p}"] = self._s(a/c[-1]*100) if c[-1] else None
        if len(c)>=2: r["true_range"] = self._s(true_range(self.highs[-1],self.lows[-1],c[-2]))
        for p in [10,20,30]:
            if len(c)>=p+1:
                rets = [math.log(c[i]/c[i-1]) for i in range(-p,0) if c[i-1]>0]
                if len(rets)>=p:
                    mr = sum(rets)/len(rets); var = sum((x-mr)**2 for x in rets)/(len(rets)-1)
                    r[f"hist_vol_{p}"] = self._s(math.sqrt(var)*math.sqrt(365)*100)
        return r

    # ---- Volume ----
    def compute_volume(self):
        r = {}; c,v = self.closes,self.volumes
        if not any(vol>0 for vol in v): return {"warning": "No volume data"}
        obv = 0.0; obv_list = [0.0]
        for i in range(1,len(c)):
            if c[i]>c[i-1]: obv+=v[i]
            elif c[i]<c[i-1]: obv-=v[i]
            obv_list.append(obv)
        r["obv"] = self._s(obv)
        for p in [10,20,50]:
            s = sma(obv_list,p)
            if s: r[f"obv_sma_{p}"] = self._s(s)
        tv = sum(v)
        if tv>0: r["vwap"] = self._s(sum((self.highs[i]+self.lows[i]+c[i])/3*v[i] for i in range(len(c)))/tv)
        for p in [7,14,20]:
            mfi = self._mfi(p)
            if mfi is not None: r[f"mfi_{p}"] = self._s(mfi)
        for p in [10,20,30]:
            cmf = self._cmf(p)
            if cmf is not None: r[f"cmf_{p}"] = self._s(cmf)
        ad = 0.0
        for i in range(len(c)):
            hl = self.highs[i]-self.lows[i]
            if hl!=0: ad += ((c[i]-self.lows[i])-(self.highs[i]-c[i]))/hl*v[i]
        r["ad_line"] = self._s(ad)
        ad_s = []; ad2 = 0.0
        for i in range(len(c)):
            hl = self.highs[i]-self.lows[i]
            if hl!=0: ad2+=((c[i]-self.lows[i])-(self.highs[i]-c[i]))/hl*v[i]
            ad_s.append(ad2)
        fe = ema(ad_s,3); se = ema(ad_s,10)
        if fe and se: r["chaikin_osc"] = self._s(fe-se)
        for p in [7,14,21]:
            if len(self.highs)>=p+1:
                eom = []
                for i in range(1,len(self.highs)):
                    dm = ((self.highs[i]+self.lows[i])/2)-((self.highs[i-1]+self.lows[i-1])/2)
                    br = v[i]/(self.highs[i]-self.lows[i]) if self.highs[i]!=self.lows[i] else 0
                    eom.append(dm/br if br else 0)
                s = sma(eom,p)
                if s: r[f"eom_{p}"] = self._s(s)
        for p in [2,13,21]:
            if len(c)>=2:
                fi = [(c[i]-c[i-1])*v[i] for i in range(1,len(c))]
                e = ema(fi,p) if len(fi)>=p else None
                if e: r[f"force_index_{p}"] = self._s(e)
        pvt = 0.0
        for i in range(1,len(c)):
            if c[i-1]!=0: pvt+=((c[i]-c[i-1])/c[i-1])*v[i]
        r["pvt"] = self._s(pvt)
        for p in [5,10,20,50]:
            vs = sma(v,p)
            if vs and vs>0: r[f"vol_sma_{p}"] = self._s(vs); r[f"vol_ratio_{p}"] = self._s(v[-1]/vs)
        if len(v)>=20:
            avg_v = sum(v[-20:])/20
            if avg_v>0: r["relative_volume"] = self._s(v[-1]/avg_v)
        return r

    # ---- Custom / Derived ----
    def compute_custom(self):
        r = {}; c,h,l,o = self.closes,self.highs,self.lows,self.opens
        if self.n>=2:
            hac = (o[-1]+h[-1]+l[-1]+c[-1])/4; hao = (o[-2]+c[-2])/2
            r["heikin_ashi"] = {"open":self._s(hao),"high":self._s(max(h[-1],hao,hac)),"low":self._s(min(l[-1],hao,hac)),"close":self._s(hac)}
        if self.n>=1:
            pp_h,pp_l,pp_c = h[-1],l[-1],c[-1]; pp = (pp_h+pp_l+pp_c)/3; diff = pp_h-pp_l
            r["pivot_classic"] = {"pp":self._s(pp),"r1":self._s(2*pp-pp_l),"r2":self._s(pp+diff),"r3":self._s(pp_h+2*(pp-pp_l)),"s1":self._s(2*pp-pp_h),"s2":self._s(pp-diff),"s3":self._s(pp_l-2*(pp_h-pp))}
            r["pivot_fibonacci"] = {"pp":self._s(pp),"r1":self._s(pp+.382*diff),"r2":self._s(pp+.618*diff),"r3":self._s(pp+diff),"s1":self._s(pp-.382*diff),"s2":self._s(pp-.618*diff),"s3":self._s(pp-diff)}
            wpp = (pp_h+pp_l+2*pp_c)/4
            r["pivot_woodie"] = {"pp":self._s(wpp),"r1":self._s(2*wpp-pp_l),"r2":self._s(wpp+diff),"s1":self._s(2*wpp-pp_h),"s2":self._s(wpp-diff)}
            r["pivot_camarilla"] = {"r1":self._s(pp_c+1.1/12*diff),"r2":self._s(pp_c+1.1/6*diff),"r3":self._s(pp_c+1.1/4*diff),"r4":self._s(pp_c+1.1/2*diff),"s1":self._s(pp_c-1.1/12*diff),"s2":self._s(pp_c-1.1/6*diff),"s3":self._s(pp_c-1.1/4*diff),"s4":self._s(pp_c-1.1/2*diff)}
        if self.n>=50:
            fh = max(h[-50:]); fl = min(l[-50:]); fd = fh-fl
            r["fib_retracement"] = {"high":self._s(fh),"low":self._s(fl),"0.236":self._s(fh-.236*fd),"0.382":self._s(fh-.382*fd),"0.5":self._s(fh-.5*fd),"0.618":self._s(fh-.618*fd),"0.786":self._s(fh-.786*fd)}
        for p in [13,26]:
            e = ema(c,p)
            if e: r[f"elder_ray_bull_{p}"] = self._s(h[-1]-e); r[f"elder_ray_bear_{p}"] = self._s(l[-1]-e)
        mi = self._mass_index(25)
        if mi: r["mass_index_25"] = self._s(mi)
        green = red = 0
        for i in range(self.n-1,-1,-1):
            if c[i]>=o[i]:
                if red>0: break
                green+=1
            else:
                if green>0: break
                red+=1
        r["consecutive_green"] = green; r["consecutive_red"] = red
        if self.n>=50: r["high_50"] = self._s(max(h[-50:])); r["low_50"] = self._s(min(l[-50:]))
        if self.n>=200: r["high_200"] = self._s(max(h[-200:])); r["low_200"] = self._s(min(l[-200:]))
        return r

    # ============================================================
    # NEW: Candlestick Pattern Recognition (30+ patterns)
    # ============================================================
    def compute_patterns(self):
        r = {"detected": [], "summary": {"bullish": 0, "bearish": 0, "neutral": 0}}
        o, h, l, c, v = self.opens, self.highs, self.lows, self.closes, self.volumes
        n = self.n
        if n < 5: return r

        def body(i): return abs(c[i] - o[i])
        def upper_shadow(i): return h[i] - max(o[i], c[i])
        def lower_shadow(i): return min(o[i], c[i]) - l[i]
        def is_bull(i): return c[i] > o[i]
        def is_bear(i): return c[i] < o[i]
        def candle_range(i): return h[i] - l[i] if h[i] != l[i] else 0.0001
        def avg_body(lookback=10):
            start = max(0, n - lookback - 1)
            bodies = [body(i) for i in range(start, n - 1)]
            return sum(bodies) / len(bodies) if bodies else 0.0001
        def avg_range(lookback=10):
            start = max(0, n - lookback - 1)
            ranges = [candle_range(i) for i in range(start, n - 1)]
            return sum(ranges) / len(ranges) if ranges else 0.0001

        ab = avg_body(); ar = avg_range()
        i = n - 1  # current candle

        def add(name, direction, reliability, desc=""):
            r["detected"].append({"name": name, "direction": direction, "reliability": reliability, "description": desc})
            r["summary"][direction] += 1

        # --- Single candle patterns ---
        # Doji
        if body(i) <= ar * 0.1:
            if lower_shadow(i) > body(i) * 2 and upper_shadow(i) > body(i) * 2:
                add("long_legged_doji", "neutral", "medium", "Long-Legged Doji: indecision")
            elif lower_shadow(i) > body(i) * 3 and upper_shadow(i) < body(i):
                add("dragonfly_doji", "bullish", "high", "Dragonfly Doji: bullish reversal signal")
            elif upper_shadow(i) > body(i) * 3 and lower_shadow(i) < body(i):
                add("gravestone_doji", "bearish", "high", "Gravestone Doji: bearish reversal signal")
            else:
                add("doji", "neutral", "low", "Doji: indecision candle")

        # Hammer / Hanging Man
        if lower_shadow(i) >= body(i) * 2 and upper_shadow(i) <= body(i) * 0.5 and body(i) > ar * 0.2:
            if i >= 5 and c[i] < min(c[i-3:i]):
                add("hammer", "bullish", "high", "Hammer: bullish reversal at bottom")
            elif i >= 5 and c[i] > max(c[i-3:i]):
                add("hanging_man", "bearish", "medium", "Hanging Man: bearish reversal at top")

        # Inverted Hammer / Shooting Star
        if upper_shadow(i) >= body(i) * 2 and lower_shadow(i) <= body(i) * 0.5 and body(i) > ar * 0.2:
            if i >= 5 and c[i] < min(c[i-3:i]):
                add("inverted_hammer", "bullish", "medium", "Inverted Hammer: potential bullish reversal")
            elif i >= 5 and c[i] > max(c[i-3:i]):
                add("shooting_star", "bearish", "high", "Shooting Star: bearish reversal at top")

        # Marubozu (full body, minimal shadows)
        if body(i) > ab * 1.5 and upper_shadow(i) < body(i) * 0.1 and lower_shadow(i) < body(i) * 0.1:
            if is_bull(i): add("bullish_marubozu", "bullish", "high", "Bullish Marubozu: strong buying pressure")
            else: add("bearish_marubozu", "bearish", "high", "Bearish Marubozu: strong selling pressure")

        # Spinning Top
        if body(i) < ab * 0.5 and upper_shadow(i) > body(i) and lower_shadow(i) > body(i) and body(i) > ar * 0.05:
            add("spinning_top", "neutral", "low", "Spinning Top: indecision")

        # Belt Hold
        if body(i) > ab * 1.2:
            if is_bull(i) and lower_shadow(i) < body(i) * 0.05:
                add("bullish_belt_hold", "bullish", "medium", "Bullish Belt Hold: opening at low, strong close")
            elif is_bear(i) and upper_shadow(i) < body(i) * 0.05:
                add("bearish_belt_hold", "bearish", "medium", "Bearish Belt Hold: opening at high, strong sell")

        # --- Two candle patterns ---
        if n >= 2:
            j = i - 1  # previous candle

            # Bullish Engulfing
            if is_bear(j) and is_bull(i) and o[i] <= c[j] and c[i] >= o[j] and body(i) > body(j):
                add("bullish_engulfing", "bullish", "high", "Bullish Engulfing: strong reversal signal")

            # Bearish Engulfing
            if is_bull(j) and is_bear(i) and o[i] >= c[j] and c[i] <= o[j] and body(i) > body(j):
                add("bearish_engulfing", "bearish", "high", "Bearish Engulfing: strong reversal signal")

            # Bullish Harami
            if is_bear(j) and is_bull(i) and body(j) > ab * 1.0 and o[i] > c[j] and c[i] < o[j] and body(i) < body(j) * 0.5:
                add("bullish_harami", "bullish", "medium", "Bullish Harami: potential reversal")

            # Bearish Harami
            if is_bull(j) and is_bear(i) and body(j) > ab * 1.0 and o[i] < c[j] and c[i] > o[j] and body(i) < body(j) * 0.5:
                add("bearish_harami", "bearish", "medium", "Bearish Harami: potential reversal")

            # Piercing Line
            if is_bear(j) and is_bull(i) and o[i] < l[j] and c[i] > (o[j] + c[j]) / 2 and c[i] < o[j]:
                add("piercing_line", "bullish", "high", "Piercing Line: bullish reversal")

            # Dark Cloud Cover
            if is_bull(j) and is_bear(i) and o[i] > h[j] and c[i] < (o[j] + c[j]) / 2 and c[i] > o[j]:
                add("dark_cloud_cover", "bearish", "high", "Dark Cloud Cover: bearish reversal")

            # Tweezer Bottom
            if abs(l[j] - l[i]) < ar * 0.05 and is_bear(j) and is_bull(i):
                add("tweezer_bottom", "bullish", "medium", "Tweezer Bottom: support confirmation")

            # Tweezer Top
            if abs(h[j] - h[i]) < ar * 0.05 and is_bull(j) and is_bear(i):
                add("tweezer_top", "bearish", "medium", "Tweezer Top: resistance confirmation")

        # --- Three candle patterns ---
        if n >= 3:
            k = i - 2  # two candles ago

            # Morning Star
            if is_bear(k) and body(k) > ab and body(i-1) < ab * 0.3 and is_bull(i) and body(i) > ab * 0.5 and c[i] > (o[k] + c[k]) / 2:
                add("morning_star", "bullish", "high", "Morning Star: strong bullish reversal (3-candle)")

            # Evening Star
            if is_bull(k) and body(k) > ab and body(i-1) < ab * 0.3 and is_bear(i) and body(i) > ab * 0.5 and c[i] < (o[k] + c[k]) / 2:
                add("evening_star", "bearish", "high", "Evening Star: strong bearish reversal (3-candle)")

            # Three White Soldiers
            if all(is_bull(x) for x in [k, i-1, i]) and c[i-1] > c[k] and c[i] > c[i-1] and all(body(x) > ab * 0.5 for x in [k, i-1, i]):
                if upper_shadow(k) < body(k) * 0.3 and upper_shadow(i-1) < body(i-1) * 0.3 and upper_shadow(i) < body(i) * 0.3:
                    add("three_white_soldiers", "bullish", "high", "Three White Soldiers: strong bullish continuation")

            # Three Black Crows
            if all(is_bear(x) for x in [k, i-1, i]) and c[i-1] < c[k] and c[i] < c[i-1] and all(body(x) > ab * 0.5 for x in [k, i-1, i]):
                if lower_shadow(k) < body(k) * 0.3 and lower_shadow(i-1) < body(i-1) * 0.3 and lower_shadow(i) < body(i) * 0.3:
                    add("three_black_crows", "bearish", "high", "Three Black Crows: strong bearish continuation")

            # Three Inside Up (Bullish Harami + confirmation)
            if is_bear(k) and is_bull(i-1) and body(i-1) < body(k) * 0.5 and o[i-1] > c[k] and c[i-1] < o[k] and is_bull(i) and c[i] > o[k]:
                add("three_inside_up", "bullish", "high", "Three Inside Up: confirmed bullish reversal")

            # Three Inside Down
            if is_bull(k) and is_bear(i-1) and body(i-1) < body(k) * 0.5 and o[i-1] < c[k] and c[i-1] > o[k] and is_bear(i) and c[i] < o[k]:
                add("three_inside_down", "bearish", "high", "Three Inside Down: confirmed bearish reversal")

        # Pattern score
        bull_score = sum(2 if p["reliability"] == "high" else 1 for p in r["detected"] if p["direction"] == "bullish")
        bear_score = sum(2 if p["reliability"] == "high" else 1 for p in r["detected"] if p["direction"] == "bearish")
        r["pattern_score"] = {"bullish": bull_score, "bearish": bear_score, "net": bull_score - bear_score}
        return r

    # ============================================================
    # NEW: Divergence Detection (RSI, MACD, OBV)
    # ============================================================
    def compute_divergence(self):
        r = {"detected": [], "summary": "no_divergence"}
        c, h, l = self.closes, self.highs, self.lows
        if self.n < 30: return r

        def find_swing_highs(data, window=5):
            swings = []
            for i in range(window, len(data) - window):
                if data[i] == max(data[i-window:i+window+1]):
                    swings.append((i, data[i]))
            return swings

        def find_swing_lows(data, window=5):
            swings = []
            for i in range(window, len(data) - window):
                if data[i] == min(data[i-window:i+window+1]):
                    swings.append((i, data[i]))
            return swings

        def check_divergence(price_data, indicator_data, name):
            divs = []
            price_highs = find_swing_highs(price_data)
            price_lows = find_swing_lows(price_data)
            ind_highs = find_swing_highs(indicator_data)
            ind_lows = find_swing_lows(indicator_data)

            # Bearish divergence: price makes higher high, indicator makes lower high
            if len(price_highs) >= 2 and len(ind_highs) >= 2:
                ph1, ph2 = price_highs[-2], price_highs[-1]
                # Find closest indicator highs
                ih_candidates = [(abs(ih[0] - ph2[0]), ih) for ih in ind_highs if abs(ih[0] - ph2[0]) <= 8]
                ih_prev_candidates = [(abs(ih[0] - ph1[0]), ih) for ih in ind_highs if abs(ih[0] - ph1[0]) <= 8]
                if ih_candidates and ih_prev_candidates:
                    ih2 = min(ih_candidates)[1]
                    ih1 = min(ih_prev_candidates)[1]
                    if ph2[1] > ph1[1] and ih2[1] < ih1[1]:
                        strength = abs(ih1[1] - ih2[1]) / max(abs(ih1[1]), 0.001)
                        divs.append({"type": "bearish_divergence", "indicator": name,
                            "description": f"Price higher high but {name} lower high",
                            "strength": self._s(min(strength * 100, 100)), "bars_ago": self.n - 1 - ph2[0]})

            # Bullish divergence: price makes lower low, indicator makes higher low
            if len(price_lows) >= 2 and len(ind_lows) >= 2:
                pl1, pl2 = price_lows[-2], price_lows[-1]
                il_candidates = [(abs(il[0] - pl2[0]), il) for il in ind_lows if abs(il[0] - pl2[0]) <= 8]
                il_prev_candidates = [(abs(il[0] - pl1[0]), il) for il in ind_lows if abs(il[0] - pl1[0]) <= 8]
                if il_candidates and il_prev_candidates:
                    il2 = min(il_candidates)[1]
                    il1 = min(il_prev_candidates)[1]
                    if pl2[1] < pl1[1] and il2[1] > il1[1]:
                        strength = abs(il2[1] - il1[1]) / max(abs(il1[1]), 0.001)
                        divs.append({"type": "bullish_divergence", "indicator": name,
                            "description": f"Price lower low but {name} higher low",
                            "strength": self._s(min(strength * 100, 100)), "bars_ago": self.n - 1 - pl2[0]})

            # Hidden bullish: price higher low, indicator lower low (trend continuation)
            if len(price_lows) >= 2 and len(ind_lows) >= 2:
                pl1, pl2 = price_lows[-2], price_lows[-1]
                il_candidates = [(abs(il[0] - pl2[0]), il) for il in ind_lows if abs(il[0] - pl2[0]) <= 8]
                il_prev_candidates = [(abs(il[0] - pl1[0]), il) for il in ind_lows if abs(il[0] - pl1[0]) <= 8]
                if il_candidates and il_prev_candidates:
                    il2 = min(il_candidates)[1]
                    il1 = min(il_prev_candidates)[1]
                    if pl2[1] > pl1[1] and il2[1] < il1[1]:
                        divs.append({"type": "hidden_bullish_divergence", "indicator": name,
                            "description": f"Price higher low but {name} lower low (trend continuation)",
                            "strength": "medium", "bars_ago": self.n - 1 - pl2[0]})

            # Hidden bearish: price lower high, indicator higher high (trend continuation)
            if len(price_highs) >= 2 and len(ind_highs) >= 2:
                ph1, ph2 = price_highs[-2], price_highs[-1]
                ih_candidates = [(abs(ih[0] - ph2[0]), ih) for ih in ind_highs if abs(ih[0] - ph2[0]) <= 8]
                ih_prev_candidates = [(abs(ih[0] - ph1[0]), ih) for ih in ind_highs if abs(ih[0] - ph1[0]) <= 8]
                if ih_candidates and ih_prev_candidates:
                    ih2 = min(ih_candidates)[1]
                    ih1 = min(ih_prev_candidates)[1]
                    if ph2[1] < ph1[1] and ih2[1] > ih1[1]:
                        divs.append({"type": "hidden_bearish_divergence", "indicator": name,
                            "description": f"Price lower high but {name} higher high (trend continuation)",
                            "strength": "medium", "bars_ago": self.n - 1 - ph2[0]})
            return divs

        # RSI divergence
        rsi_vals = []
        for i in range(15, len(c) + 1):
            rv = self._rsi(c[:i], 14)
            rsi_vals.append(rv if rv is not None else 50)
        if len(rsi_vals) >= 30:
            r["detected"].extend(check_divergence(c[-len(rsi_vals):], rsi_vals, "RSI14"))

        # MACD divergence
        fast_s = ema_series(c, 12); slow_s = ema_series(c, 26)
        macd_vals = []
        for f, s in zip(fast_s, slow_s):
            if f is not None and s is not None: macd_vals.append(f - s)
        if len(macd_vals) >= 30:
            r["detected"].extend(check_divergence(c[-len(macd_vals):], macd_vals, "MACD"))

        # OBV divergence
        obv_vals = [0.0]
        for i in range(1, len(c)):
            if c[i] > c[i-1]: obv_vals.append(obv_vals[-1] + self.volumes[i])
            elif c[i] < c[i-1]: obv_vals.append(obv_vals[-1] - self.volumes[i])
            else: obv_vals.append(obv_vals[-1])
        if len(obv_vals) >= 30:
            r["detected"].extend(check_divergence(c, obv_vals, "OBV"))

        # Summary
        bull_divs = [d for d in r["detected"] if "bullish" in d["type"]]
        bear_divs = [d for d in r["detected"] if "bearish" in d["type"]]
        if bull_divs and not bear_divs: r["summary"] = "bullish_divergence"
        elif bear_divs and not bull_divs: r["summary"] = "bearish_divergence"
        elif bull_divs and bear_divs: r["summary"] = "mixed_divergence"
        r["divergence_score"] = {"bullish": len(bull_divs), "bearish": len(bear_divs)}
        return r

    # ============================================================
    # NEW: Support/Resistance & Market Structure
    # ============================================================
    def compute_structure(self):
        r = {}; c, h, l = self.closes, self.highs, self.lows
        if self.n < 20: return r

        # --- Swing Highs/Lows detection ---
        swing_highs = []; swing_lows = []
        window = 5
        for i in range(window, self.n - window):
            if h[i] == max(h[i-window:i+window+1]): swing_highs.append({"index": i, "price": self._s(h[i])})
            if l[i] == min(l[i-window:i+window+1]): swing_lows.append({"index": i, "price": self._s(l[i])})
        r["swing_highs"] = swing_highs[-5:] if swing_highs else []
        r["swing_lows"] = swing_lows[-5:] if swing_lows else []

        # --- Market Structure (HH, HL, LH, LL) ---
        structure = []
        if len(swing_highs) >= 2:
            sh = swing_highs
            for i in range(1, len(sh)):
                if sh[i]["price"] > sh[i-1]["price"]: structure.append("HH")
                else: structure.append("LH")
        if len(swing_lows) >= 2:
            sl = swing_lows
            for i in range(1, len(sl)):
                if sl[i]["price"] > sl[i-1]["price"]: structure.append("HL")
                else: structure.append("LL")

        recent = structure[-6:] if structure else []
        r["market_structure"] = {"recent_swings": recent}
        hh_hl = sum(1 for s in recent if s in ["HH", "HL"])
        lh_ll = sum(1 for s in recent if s in ["LH", "LL"])
        if hh_hl > lh_ll + 1: r["market_structure"]["trend"] = "uptrend"
        elif lh_ll > hh_hl + 1: r["market_structure"]["trend"] = "downtrend"
        else: r["market_structure"]["trend"] = "ranging"

        # --- Support/Resistance Levels ---
        levels = []
        # From swing points
        for sh in swing_highs[-10:]:
            levels.append({"price": sh["price"], "type": "resistance", "source": "swing_high", "strength": 1})
        for sl in swing_lows[-10:]:
            levels.append({"price": sl["price"], "type": "support", "source": "swing_low", "strength": 1})

        # Cluster nearby levels (within 0.5% of each other)
        clustered = []
        used = set()
        for i, lv in enumerate(levels):
            if i in used: continue
            cluster = [lv]
            for j, lv2 in enumerate(levels):
                if j <= i or j in used: continue
                if lv["price"] and lv2["price"] and abs(lv["price"] - lv2["price"]) / lv["price"] < 0.005:
                    cluster.append(lv2); used.add(j)
            used.add(i)
            avg_price = self._s(sum(x["price"] for x in cluster) / len(cluster))
            strength = len(cluster)
            ctype = "resistance" if sum(1 for x in cluster if x["type"] == "resistance") > len(cluster) / 2 else "support"
            clustered.append({"price": avg_price, "type": ctype, "strength": strength, "touches": len(cluster)})

        # Sort by proximity to current price
        cur = c[-1]
        clustered.sort(key=lambda x: abs(x["price"] - cur) if x["price"] else float('inf'))
        r["support_resistance"] = clustered[:10]

        # Nearest support and resistance
        supports = [x for x in clustered if x["type"] == "support" and x["price"] < cur]
        resistances = [x for x in clustered if x["type"] == "resistance" and x["price"] > cur]
        supports.sort(key=lambda x: cur - x["price"])
        resistances.sort(key=lambda x: x["price"] - cur)
        r["nearest_support"] = supports[0] if supports else None
        r["nearest_resistance"] = resistances[0] if resistances else None

        # --- Trendline approximation ---
        if len(swing_lows) >= 2:
            sl = swing_lows[-2:]
            if sl[1]["index"] > sl[0]["index"]:
                slope = (sl[1]["price"] - sl[0]["price"]) / (sl[1]["index"] - sl[0]["index"])
                projected = sl[1]["price"] + slope * (self.n - 1 - sl[1]["index"])
                r["ascending_trendline"] = {"slope_per_bar": self._s(slope), "current_value": self._s(projected),
                    "price_above": c[-1] > projected if projected else None}

        if len(swing_highs) >= 2:
            sh = swing_highs[-2:]
            if sh[1]["index"] > sh[0]["index"]:
                slope = (sh[1]["price"] - sh[0]["price"]) / (sh[1]["index"] - sh[0]["index"])
                projected = sh[1]["price"] + slope * (self.n - 1 - sh[1]["index"])
                r["descending_trendline"] = {"slope_per_bar": self._s(slope), "current_value": self._s(projected),
                    "price_below": c[-1] < projected if projected else None}

        # --- Price position analysis ---
        if self.n >= 50:
            h50 = max(h[-50:]); l50 = min(l[-50:])
            r["price_position"] = {"pct_from_50_high": self._s((c[-1] - h50) / h50 * 100),
                "pct_from_50_low": self._s((c[-1] - l50) / l50 * 100),
                "range_position": self._s((c[-1] - l50) / (h50 - l50) * 100) if h50 != l50 else 50}

        return r

    # ============================================================
    # NEW: Advanced Volatility Models
    # ============================================================
    def compute_advanced_volatility(self):
        r = {}; o, h, l, c = self.opens, self.highs, self.lows, self.closes
        n = self.n

        for p in [10, 20, 30]:
            if n < p + 1: continue

            # Parkinson volatility (uses high-low range)
            pk_sum = sum(math.log(h[i] / l[i]) ** 2 for i in range(-p, 0) if l[i] > 0 and h[i] > 0)
            parkinson = math.sqrt(pk_sum / (4 * p * math.log(2))) * math.sqrt(365) * 100
            r[f"parkinson_vol_{p}"] = self._s(parkinson)

            # Garman-Klass volatility (uses OHLC)
            gk_sum = 0
            for i in range(-p, 0):
                if l[i] > 0 and o[i] > 0 and h[i] > 0:
                    gk_sum += 0.5 * math.log(h[i] / l[i]) ** 2 - (2 * math.log(2) - 1) * math.log(c[i] / o[i]) ** 2
            garman_klass = math.sqrt(gk_sum / p) * math.sqrt(365) * 100
            r[f"garman_klass_vol_{p}"] = self._s(garman_klass)

            # Yang-Zhang volatility (most efficient estimator)
            log_oc = [math.log(o[i] / c[i-1]) for i in range(-p+1, 0) if c[i-1] > 0 and o[i] > 0]
            log_co = [math.log(c[i] / o[i]) for i in range(-p, 0) if o[i] > 0 and c[i] > 0]
            log_rs = []
            for i in range(-p, 0):
                if o[i] > 0 and h[i] > 0 and l[i] > 0 and c[i] > 0:
                    log_rs.append(math.log(h[i]/o[i]) * math.log(h[i]/c[i]) + math.log(l[i]/o[i]) * math.log(l[i]/c[i]))
            if log_oc and log_co and log_rs:
                n_oc = len(log_oc); n_co = len(log_co)
                var_o = sum((x - sum(log_oc)/n_oc)**2 for x in log_oc) / (n_oc - 1) if n_oc > 1 else 0
                var_c = sum((x - sum(log_co)/n_co)**2 for x in log_co) / (n_co - 1) if n_co > 1 else 0
                var_rs = sum(log_rs) / len(log_rs) if log_rs else 0
                k = 0.34 / (1.34 + (p + 1) / (p - 1))
                yz_var = var_o + k * var_c + (1 - k) * var_rs
                yang_zhang = math.sqrt(max(yz_var, 0)) * math.sqrt(365) * 100
                r[f"yang_zhang_vol_{p}"] = self._s(yang_zhang)

            # Close-to-close (standard) for comparison
            rets = [math.log(c[i]/c[i-1]) for i in range(-p, 0) if c[i-1] > 0]
            if len(rets) >= p:
                mr = sum(rets)/len(rets)
                cc_var = sum((x - mr)**2 for x in rets) / (len(rets) - 1)
                r[f"close_to_close_vol_{p}"] = self._s(math.sqrt(cc_var) * math.sqrt(365) * 100)

        # Volatility Cone (percentile of current vol vs historical)
        if n >= 60:
            windows = [10, 20, 30]
            for w in windows:
                all_vols = []
                for start in range(0, n - w):
                    rets = [math.log(c[start+i+1]/c[start+i]) for i in range(w) if c[start+i] > 0]
                    if len(rets) >= w:
                        mr = sum(rets)/len(rets)
                        v = math.sqrt(sum((x-mr)**2 for x in rets)/(len(rets)-1)) * math.sqrt(365) * 100
                        all_vols.append(v)
                if all_vols:
                    current_vol = all_vols[-1] if all_vols else 0
                    sorted_v = sorted(all_vols)
                    pct = sum(1 for v in sorted_v if v <= current_vol) / len(sorted_v) * 100
                    r[f"vol_percentile_{w}"] = self._s(pct)
                    r[f"vol_cone_{w}"] = {"current": self._s(current_vol), "min": self._s(sorted_v[0]),
                        "p25": self._s(sorted_v[len(sorted_v)//4]), "median": self._s(sorted_v[len(sorted_v)//2]),
                        "p75": self._s(sorted_v[3*len(sorted_v)//4]), "max": self._s(sorted_v[-1]),
                        "percentile": self._s(pct)}

        # Volatility regime
        if f"close_to_close_vol_10" in r and f"close_to_close_vol_30" in r:
            short_vol = r["close_to_close_vol_10"]; long_vol = r["close_to_close_vol_30"]
            if short_vol and long_vol and long_vol > 0:
                ratio = short_vol / long_vol
                if ratio > 1.3: regime = "expanding"
                elif ratio < 0.7: regime = "contracting"
                else: regime = "stable"
                r["volatility_regime"] = {"short_long_ratio": self._s(ratio), "regime": regime}

        return r

    # ============================================================
    # WorldQuant Alpha 101 Factors
    # Time-series factors: single-asset OHLCV only
    # Cross-sectional factors: marked _CS_, need multi-asset scan
    # ============================================================
    def compute_alpha101(self):
        """WorldQuant 101 Alphas — time-series subset (~40 factors)."""
        r = {"_type": "time_series", "_skipped_cs": "Cross-sectional factors (Alpha#1,#5,#10,#11,#13,#21,#22,#27,#30,#31,#36,#37,#39,#40,#47,#48,#55-#95,#97,#99,#100) require scan mode with multi-asset data."}
        o, h, l, c, v = self.opens, self.highs, self.lows, self.closes, self.volumes
        n = self.n
        if n < 30: return r
        ret = returns_series(c)
        vw = vwap_full(h, l, c, v)
        adv5 = adv_series(v, 5)
        adv10 = adv_series(v, 10)
        adv20 = adv_series(v, 20)
        log_v = [log_safe(x) for x in v]

        # Alpha#2: -1 * delta(log(volume), 2) * correlation(close, volume, 6)
        if n >= 6:
            d = delta_s(log_v, 2); cr = ts_corr(c, v, 6)
            if d is not None and cr is not None: r["a101_002"] = self._s(-1*d*cr)

        # Alpha#3: -1 * correlation(rank(open), rank(volume), 10) — use ts_rank as proxy
        if n >= 20:
            o_rk = ts_rank_series(o, 10); v_rk = ts_rank_series(v, 10)
            o_clean = [x for x in o_rk if x is not None]; v_clean = [x for x in v_rk if x is not None]
            if len(o_clean) >= 10 and len(v_clean) >= 10:
                r["a101_003"] = self._s(-1*ts_corr(o_clean, v_clean, 10))

        # Alpha#4: -1 * ts_rank(rank(low), 9)
        if n >= 18:
            l_rk = ts_rank_series(l, 9)
            l_clean = [x for x in l_rk if x is not None]
            if len(l_clean) >= 9: r["a101_004"] = self._s(-1*ts_rank(l_clean, 9))

        # Alpha#6: -1 * correlation(open, volume, 10)
        if n >= 10:
            cr = ts_corr(o, v, 10)
            if cr is not None: r["a101_006"] = self._s(-1*cr)

        # Alpha#7: if adv20 < volume then (-1*ts_rank(|delta(close,7)|,60)*sign(delta(close,7))) else -1
        if n >= 60:
            if adv20[-1] < v[-1]:
                dc7 = delta_s(c, 7)
                if dc7 is not None:
                    abs_dc = [abs(c[i]-c[i-7]) for i in range(7, n)]
                    if len(abs_dc) >= 60:
                        r["a101_007"] = self._s(-1*ts_rank(abs_dc, 60)*math.copysign(1, dc7))
            else: r["a101_007"] = -1.0

        # Alpha#8: -1 * rank(sum(returns, 5) * open_5ago - delay(close - open, 10))
        if n >= 15:
            ret_sum5 = ts_sum(ret, 5)
            d_co = (c[-11]-o[-11]) if n > 11 else 0
            if ret_sum5 is not None:
                val = ret_sum5 * o[-6] - d_co if n > 6 else ret_sum5
                r["a101_008"] = self._s(-1*val)

        # Alpha#9: conditional delta
        if n >= 6:
            deltas = [c[i]-c[i-1] for i in range(1, n)]
            if len(deltas) >= 5:
                mn = min(deltas[-5:]); mx = max(deltas[-5:])
                d1 = deltas[-1]
                if mn > 0: r["a101_009"] = self._s(d1)
                elif mx < 0: r["a101_009"] = self._s(d1)
                else: r["a101_009"] = self._s(-1*d1)

        # Alpha#12: sign(delta(volume,1)) * (-1 * delta(close,1))
        if n >= 2:
            dv = v[-1]-v[-2]; dc = c[-1]-c[-2]
            r["a101_012"] = self._s(math.copysign(1, dv)*(-1*dc))

        # Alpha#14: -1 * rank(delta(returns,3)) * correlation(open, volume, 10)
        if n >= 14:
            dr3 = delta_s(ret, 3); cr = ts_corr(o, v, 10)
            if dr3 is not None and cr is not None:
                r["a101_014"] = self._s(-1*dr3*cr)

        # Alpha#15: -1 * sum(rank(correlation(rank(high), rank(volume), 3)), 3)
        if n >= 15:
            h_rk = ts_rank_series(h, 5); v_rk = ts_rank_series(v, 5)
            corr_s = ts_corr_series([x or 0 for x in h_rk], [x or 0 for x in v_rk], 3)
            valid = [x for x in corr_s if x is not None]
            if len(valid) >= 3: r["a101_015"] = self._s(-1*sum(valid[-3:]))

        # Alpha#16: -1 * rank(covariance(rank(high), rank(volume), 5))
        if n >= 15:
            h_rk = ts_rank_series(h, 5); v_rk = ts_rank_series(v, 5)
            h_clean = [x or 0 for x in h_rk]; v_clean = [x or 0 for x in v_rk]
            cv = ts_cov(h_clean, v_clean, 5)
            if cv is not None: r["a101_016"] = self._s(-1*cv)

        # Alpha#17: ts_rank(close,10) * ts_rank(delta(delta(close,1),1),1) * ts_rank(volume/adv20,5)
        if n >= 20:
            trc = ts_rank(c, 10)
            dd = [c[i]-2*c[i-1]+c[i-2] for i in range(2, n)]
            trv = ts_rank([v[i]/adv20[i] if adv20[i] > 0 else 1 for i in range(n)], 5)
            if trc and dd and trv: r["a101_017"] = self._s(trc*(dd[-1] if dd else 0)*trv)

        # Alpha#18: -1 * (stddev(|close-open|,5) + (close-open) + corr(close,open,10))
        if n >= 10:
            co = [abs(c[i]-o[i]) for i in range(n)]
            sd = stdev(co, 5); cr = ts_corr(c, o, 10)
            if sd is not None and cr is not None:
                r["a101_018"] = self._s(-1*(sd + (c[-1]-o[-1]) + cr))

        # Alpha#19: -sign(close-delay(close,7)+delta(close,7)) * (1+ts_rank(1+sum(returns,250),250))
        if n >= 250:
            dc7 = delta_s(c, 7); sgn = math.copysign(1, (c[-1]-c[-8]) + dc7) if dc7 else 0
            rs = ts_sum(ret, 250)
            if rs is not None:
                plus_rs = [1+sum(ret[max(0,i-249):i+1]) for i in range(n)]
                trk = ts_rank(plus_rs, min(250, len(plus_rs)))
                if trk: r["a101_019"] = self._s(-sgn*(1+trk))

        # Alpha#20: (open-delay(high,1)) * (open-delay(close,1)) * (open-delay(low,1))
        if n >= 2:
            r["a101_020"] = self._s((o[-1]-h[-2])*(o[-1]-c[-2])*(o[-1]-l[-2]))

        # Alpha#23: if sma(high,20) < high then -delta(high,2) else 0
        if n >= 22:
            sm = sma(h, 20)
            if sm is not None:
                if sm < h[-1]:
                    dh = delta_s(h, 2)
                    r["a101_023"] = self._s(-1*dh) if dh is not None else 0
                else: r["a101_023"] = 0.0

        # Alpha#24: conditional on sma(close,100) trend
        if n >= 100:
            sm100 = sma(c, 100); sm100_prev = sma(c[:-100], 100) if n > 200 else sm100
            if sm100 and sm100_prev:
                chg = (sm100 - sm100_prev) / (c[-101] if n > 100 and c[-101] != 0 else 1)
                if chg <= 0.05:
                    r["a101_024"] = self._s(-1*(c[-1] - ts_min(c, 100)))
                else:
                    dc3 = delta_s(c, 3)
                    r["a101_024"] = self._s(-1*dc3) if dc3 is not None else None

        # Alpha#25: rank(-returns * adv20 * vwap * (high - close))
        if n >= 20:
            val = -ret[-1] * adv20[-1] * vw[-1] * (h[-1] - c[-1])
            r["a101_025"] = self._s(val)

        # Alpha#26: -ts_max(corr(ts_rank(vol,5), ts_rank(high,5), 5), 3)
        if n >= 15:
            vr = ts_rank_series(v, 5); hr = ts_rank_series(h, 5)
            cs = ts_corr_series([x or 0 for x in vr], [x or 0 for x in hr], 5)
            valid = [x for x in cs if x is not None]
            if len(valid) >= 3: r["a101_026"] = self._s(-1*max(valid[-3:]))

        # Alpha#28: scale(corr(adv20, low, 5) + (high+low)/2 - close)
        if n >= 25:
            cr = ts_corr(adv20, l, 5)
            if cr is not None:
                r["a101_028"] = self._s(cr + (h[-1]+l[-1])/2 - c[-1])

        # Alpha#29: log(ts_product(rank(rank(scale(log(sum(ts_min(rank(x),2),1))))), 1), 5) simplified
        # min(rank(rank(scale(...))),5) + ts_rank(delay(-returns,6),5)
        if n >= 12:
            dr6 = [-ret[i-6] for i in range(7, n)]
            if len(dr6) >= 5: r["a101_029"] = self._s(ts_rank(dr6, 5))

        # Alpha#32: scale(sma(close,7)-close) + 20*scale(corr(vwap, delay(close,5), 230))
        if n >= 230:
            sm7 = sma(c, 7)
            dc5 = c[:-5] if n > 5 else c
            cr = ts_corr(vw[-230:], dc5[-230:], min(230, len(dc5)))
            if sm7 is not None and cr is not None:
                r["a101_032"] = self._s((sm7-c[-1])/abs(sm7) + 20*cr)

        # Alpha#33: rank(-(1 - open/close))
        if n >= 1 and c[-1] != 0:
            r["a101_033"] = self._s(-(1 - o[-1]/c[-1]))

        # Alpha#34: rank(1-rank(stddev(ret,2)/stddev(ret,5)) + 1-rank(delta(close,1)))
        if n >= 10:
            sd2 = stdev(ret, 2); sd5 = stdev(ret, 5)
            dc1 = delta_s(c, 1)
            if sd2 is not None and sd5 is not None and sd5 != 0 and dc1 is not None:
                r["a101_034"] = self._s((1 - sd2/sd5) + (1 - dc1/c[-1] if c[-1] else 0))

        # Alpha#35: ts_rank(volume,32) * (1-ts_rank(close+high-low,16)) * (1-ts_rank(returns,32))
        if n >= 32:
            trv = ts_rank(v, 32)
            chl = [c[i]+h[i]-l[i] for i in range(n)]
            trchl = ts_rank(chl, 16)
            trr = ts_rank(ret, 32)
            if trv and trchl and trr: r["a101_035"] = self._s(trv*(1-trchl)*(1-trr))

        # Alpha#38: -ts_rank(close,10) * rank(close/open)
        if n >= 10:
            trc = ts_rank(c, 10)
            if trc and o[-1] != 0: r["a101_038"] = self._s(-trc*(c[-1]/o[-1]))

        # Alpha#41: sqrt(high*low) - vwap
        r["a101_041"] = self._s(math.sqrt(h[-1]*l[-1]) - vw[-1]) if h[-1]*l[-1] >= 0 else None

        # Alpha#42: rank(vwap-close) / rank(vwap+close)
        if vw[-1]+c[-1] != 0:
            r["a101_042"] = self._s((vw[-1]-c[-1]) / (vw[-1]+c[-1]))

        # Alpha#43: ts_rank(vol/adv20, 20) * ts_rank(-delta(close,7), 8)
        if n >= 27:
            vr = [v[i]/adv20[i] if adv20[i] > 0 else 1 for i in range(n)]
            trv = ts_rank(vr, 20)
            ndc7 = [-1*(c[i]-c[i-7]) for i in range(7, n)]
            trn = ts_rank(ndc7, 8) if len(ndc7) >= 8 else None
            if trv and trn: r["a101_043"] = self._s(trv*trn)

        # Alpha#44: -corr(high, rank(volume), 5)
        if n >= 10:
            vr = ts_rank_series(v, 5)
            vc = [x or 0 for x in vr]
            cr = ts_corr(h, vc, 5)
            if cr is not None: r["a101_044"] = self._s(-1*cr)

        # Alpha#45: -rank(sma(delay(close,5),20)) * corr(close,volume,2) * rank(corr(sum(close,5),sum(close,20),2))
        if n >= 30:
            dc5_s = c[:-5] if n > 5 else c
            sm = sma(dc5_s, 20)
            cr1 = ts_corr(c, v, 2)
            sc5 = [sum(c[max(0,i-4):i+1]) for i in range(n)]
            sc20 = [sum(c[max(0,i-19):i+1]) for i in range(n)]
            cr2 = ts_corr(sc5, sc20, 2)
            if sm and cr1 is not None and cr2 is not None:
                r["a101_045"] = self._s(-sm/c[-1]*cr1*cr2)

        # Alpha#46: conditional momentum
        if n >= 21:
            m1 = (c[-11]-c[-21])/10; m2 = (c[-1]-c[-11])/10
            if 0.25 < m1 - m2: r["a101_046"] = -1.0
            elif m1 - m2 < 0: r["a101_046"] = 1.0
            else: r["a101_046"] = self._s(-1*(c[-1]-c[-2]))

        # Alpha#49: conditional momentum 2
        if n >= 21:
            m1 = (c[-11]-c[-21])/10; m2 = (c[-1]-c[-11])/10
            if m1 - m2 < -0.1*c[-1]/(c[-1] if c[-1] else 1): r["a101_049"] = 1.0
            else: r["a101_049"] = self._s(-1*(c[-1]-c[-2]))

        # Alpha#50: -ts_max(rank(corr(rank(vol),rank(vwap),5)), 5)
        if n >= 15:
            vr = ts_rank_series(v, 5); wr = ts_rank_series(vw, 5)
            cs = ts_corr_series([x or 0 for x in vr], [x or 0 for x in wr], 5)
            valid = [x for x in cs if x is not None]
            if len(valid) >= 5: r["a101_050"] = self._s(-1*max(valid[-5:]))

        # Alpha#51: conditional momentum 3
        if n >= 21:
            m1 = (c[-11]-c[-21])/10; m2 = (c[-1]-c[-11])/10
            if m1 - m2 < -0.05: r["a101_051"] = 1.0
            else: r["a101_051"] = self._s(-1*(c[-1]-c[-2]))

        # Alpha#52: ts_min(low,5) delta + ts_rank(ret,240) * ts_rank(volume,5)
        if n >= 240:
            ml5 = ts_min(l, 5); ml5_prev = ts_min(l[:-5], 5) if n > 10 else ml5
            if ml5 and ml5_prev:
                ret_s = [sum(ret[max(0,i-239):i+1])-sum(ret[max(0,i-19):i+1]) for i in range(n)]
                trr = ts_rank(ret_s, min(20, len(ret_s)))
                trv = ts_rank(v, 5)
                if trr and trv:
                    r["a101_052"] = self._s((-ml5+ml5_prev)*trr*trv)

        # Alpha#53: -delta((close-low-(high-close))/(close-low+0.001), 9)
        if n >= 10:
            ratio = [(c[i]-l[i]-(h[i]-c[i]))/(c[i]-l[i]+0.001) for i in range(n)]
            d = delta_s(ratio, 9)
            if d is not None: r["a101_053"] = self._s(-1*d)

        # Alpha#54: (-1*(low-close)*(open^5)) / ((low-high)*(close^5)+0.001)
        denom = (l[-1]-h[-1])*(c[-1]**5) + 0.001
        if abs(denom) > 1e-10:
            r["a101_054"] = self._s((-1*(l[-1]-c[-1])*(o[-1]**5)) / denom)

        # Alpha#96: simplified — ts_argmax of correlation
        if n >= 20:
            cr_s = ts_corr_series(ts_rank_series(c, 5), ts_rank_series(v, 5), 5)
            valid = [x or 0 for x in cr_s[-10:]]
            if len(valid) >= 5:
                r["a101_096"] = self._s(-1*ts_argmax(valid, 5)) if ts_argmax(valid, 5) is not None else None

        # Alpha#101: (close - open) / ((high - low) + 0.001)
        r["a101_101"] = self._s((c[-1]-o[-1]) / ((h[-1]-l[-1])+0.001))

        # --- Summary ---
        ts_count = sum(1 for k, v in r.items() if k.startswith("a101_") and v is not None)
        r["_meta"] = {"ts_computed": ts_count, "cs_pending": "~60 factors need scan mode", "total_defined": 101}
        return r

    # ============================================================
    # WorldQuant Alpha 191 Factors
    # Time-series factors: single-asset OHLCV only
    # ============================================================
    def compute_alpha191(self):
        """WorldQuant 191 Alphas — time-series subset (~80 factors)."""
        r = {"_type": "time_series", "_skipped_cs": "Cross-sectional factors require scan mode with multi-asset rank data."}
        o, h, l, c, v = self.opens, self.highs, self.lows, self.closes, self.volumes
        n = self.n
        if n < 30: return r
        ret = returns_series(c)
        vw = vwap_full(h, l, c, v)
        adv20 = adv_series(v, 20)
        log_v = [log_safe(x) for x in v]

        # Alpha191#1: -CORR(RANK(DELTA(LOG(VOLUME),1)), RANK((CLOSE-OPEN)/OPEN), 6)
        if n >= 8:
            dlv = delta_series(log_v, 1)
            co = [(c[i]-o[i])/o[i] if o[i] != 0 else 0 for i in range(n)]
            dlv_rk = ts_rank_series([x or 0 for x in dlv], 5)
            co_rk = ts_rank_series(co, 5)
            d1 = [x or 0 for x in dlv_rk]; d2 = [x or 0 for x in co_rk]
            if len(d1) >= 6 and len(d2) >= 6:
                cr = ts_corr(d1, d2, 6)
                if cr is not None: r["a191_001"] = self._s(-1*cr)

        # Alpha191#2: -DELTA((CLOSE-LOW-(HIGH-CLOSE))/(HIGH-LOW+0.001), 1)
        if n >= 2:
            ratio = [(c[i]-l[i]-(h[i]-c[i]))/(h[i]-l[i]+0.001) for i in range(n)]
            r["a191_002"] = self._s(-1*(ratio[-1]-ratio[-2]))

        # Alpha191#3: SUM(CLOSE==DELAY(CLOSE,1)?0:CLOSE-(CLOSE>DELAY(CLOSE,1)?MIN(LOW,DELAY(CLOSE,1)):MAX(HIGH,DELAY(CLOSE,1))), 6)
        if n >= 7:
            s = 0
            for i in range(-6, 0):
                pc = c[i-1]
                if c[i] == pc: s += 0
                elif c[i] > pc: s += c[i] - min(l[i], pc)
                else: s += c[i] - max(h[i], pc)
            r["a191_003"] = self._s(s)

        # Alpha191#4: conditional volume-price
        if n >= 9:
            cv8 = ts_corr(c[-8:], v[-8:], 8) if n >= 8 else 0
            sm2 = sma(v, 2)
            if cv8 is not None and sm2 is not None:
                if cv8 < 0 and sm2 > 0: r["a191_004"] = self._s(-1*ts_rank(v, 5))
                else: r["a191_004"] = self._s(max(0, ts_rank(c, 5) or 0) - 0.5)

        # Alpha191#5: -ts_max(corr(ts_rank(vol,5), ts_rank(high,5), 5), 3)
        if n >= 15:
            vr = ts_rank_series(v, 5); hr = ts_rank_series(h, 5)
            cs = ts_corr_series([x or 0 for x in vr], [x or 0 for x in hr], 5)
            valid = [x for x in cs if x is not None]
            if len(valid) >= 3: r["a191_005"] = self._s(-1*max(valid[-3:]))

        # Alpha191#6: -CORR(OPEN, VOLUME, 10) * RANK(ABS(DELTA(CLOSE,1)))
        if n >= 10:
            cr = ts_corr(o, v, 10)
            dc = abs(c[-1]-c[-2]) if n >= 2 else 0
            if cr is not None: r["a191_006"] = self._s(-cr*dc)

        # Alpha191#7: if adv20<vol: -ts_rank(|delta(close,7)|,60)*sign(delta(close,7)) else -1
        if n >= 60:
            if adv20[-1] < v[-1]:
                dc7 = delta_s(c, 7)
                if dc7 is not None:
                    abs_dc = [abs(c[i]-c[i-7]) for i in range(7, n)]
                    if len(abs_dc) >= 60:
                        r["a191_007"] = self._s(-1*ts_rank(abs_dc, 60)*math.copysign(1, dc7))
            else: r["a191_007"] = -1.0

        # Alpha191#8: -(RANK(SUM(OPEN,5)*SUM(RETURN,5) - DELAY(SUM(OPEN,5)*SUM(RETURN,5),10)))
        if n >= 15:
            so5 = ts_sum(o, 5); sr5 = ts_sum(ret, 5)
            if so5 is not None and sr5 is not None:
                cur = so5*sr5
                so5p = sum(o[-15:-10]); sr5p = sum(ret[-15:-10])
                prev = so5p*sr5p
                r["a191_008"] = self._s(-(cur-prev))

        # Alpha191#10: conditional delta
        if n >= 6:
            deltas = [c[i]-c[i-1] for i in range(1, n)]
            if len(deltas) >= 5:
                mn = min(deltas[-5:]); mx = max(deltas[-5:])
                if mn > 0: r["a191_010"] = self._s(deltas[-1])
                elif mx < 0: r["a191_010"] = self._s(deltas[-1])
                else: r["a191_010"] = self._s(-deltas[-1])

        # Alpha191#11: SUM((CLOSE-LOW-(HIGH-CLOSE))/(HIGH-LOW)*VOLUME, 6)
        if n >= 6:
            s = sum(((c[i]-l[i]-(h[i]-c[i]))/(h[i]-l[i]+0.001))*v[i] for i in range(-6, 0))
            r["a191_011"] = self._s(s)

        # Alpha191#12: (OPEN-SMA(VWAP,10))/SMA(VWAP,10)*RANK
        if n >= 10:
            sm = sma(vw, 10)
            if sm and sm != 0: r["a191_012"] = self._s((o[-1]-sm)/sm)

        # Alpha191#13: (HIGH*LOW)^0.5 - VWAP
        r["a191_013"] = self._s(math.sqrt(h[-1]*l[-1]) - vw[-1]) if h[-1]*l[-1] >= 0 else None

        # Alpha191#14: CLOSE - DELAY(CLOSE, 5)
        if n >= 6: r["a191_014"] = self._s(c[-1]-c[-6])

        # Alpha191#15: OPEN/DELAY(CLOSE,1) - 1
        if n >= 2 and c[-2] != 0: r["a191_015"] = self._s(o[-1]/c[-2]-1)

        # Alpha191#19: (CLOSE - DELAY(CLOSE,5))/DELAY(CLOSE,5)
        if n >= 6 and c[-6] != 0: r["a191_019"] = self._s((c[-1]-c[-6])/c[-6])

        # Alpha191#20: (CLOSE - DELAY(CLOSE,6))/DELAY(CLOSE,6)
        if n >= 7 and c[-7] != 0: r["a191_020"] = self._s((c[-1]-c[-7])/c[-7])

        # Alpha191#21: REGBETA(MEAN(CLOSE,6),sequence,6) — linear regression slope
        if n >= 6:
            mc = [sma(c[:i+1], min(6,i+1)) for i in range(n)]
            if len(mc) >= 6:
                y = mc[-6:]; x_vals = list(range(6))
                mx = 2.5; my = sum(y)/6
                num = sum((x_vals[i]-mx)*(y[i]-my) for i in range(6))
                den = sum((x_vals[i]-mx)**2 for i in range(6))
                if den != 0: r["a191_021"] = self._s(num/den)

        # Alpha191#22: SMA((CLOSE-MEAN(CLOSE,6))/MEAN(CLOSE,6) - DELAY(...),3)
        if n >= 12:
            ratio_s = [(c[i]-sma(c[:i+1],min(6,i+1)))/(sma(c[:i+1],min(6,i+1))+0.001) for i in range(n)]
            delta_r = [ratio_s[i]-ratio_s[i-3] if i >= 3 else 0 for i in range(n)]
            r["a191_022"] = self._s(sma(delta_r, 3))

        # Alpha191#23: SMA(CLOSE>DELAY(CLOSE,1)?STD(CLOSE,20):0, 20)
        if n >= 40:
            vals = []
            for i in range(20, n):
                if c[i] > c[i-1]:
                    sd = stdev(c[:i+1], 20)
                    vals.append(sd if sd else 0)
                else: vals.append(0)
            if vals: r["a191_023"] = self._s(sum(vals[-20:])/20 if len(vals) >= 20 else sum(vals)/len(vals))

        # Alpha191#24: SMA(CLOSE-DELAY(CLOSE,5), 5)
        if n >= 10:
            diffs = [c[i]-c[i-5] for i in range(5, n)]
            if len(diffs) >= 5: r["a191_024"] = self._s(sum(diffs[-5:])/5)

        # Alpha191#27: WMA(CLOSE-DELAY(CLOSE,3)/DELAY(CLOSE,3)*100 + ..., 12)
        if n >= 15 and c[-4] != 0:
            roc3 = (c[-1]-c[-4])/c[-4]*100
            r["a191_027"] = self._s(roc3)

        # Alpha191#28: 3*SMA((CLOSE-TSMIN(LOW,9))/(TSMAX(HIGH,9)-TSMIN(LOW,9)+0.001)*100, 3) - 2*SMA(SMA(...,3),3)
        if n >= 15:
            raw = []
            for i in range(9, n):
                lo = min(l[i-8:i+1]); hi = max(h[i-8:i+1])
                raw.append((c[i]-lo)/(hi-lo+0.001)*100)
            if len(raw) >= 9:
                sm1 = sma(raw, 3); sm2 = sma(raw[:-3], 3) if len(raw) > 6 else sm1
                if sm1 is not None and sm2 is not None:
                    r["a191_028"] = self._s(3*sm1-2*sm2)

        # Alpha191#29: (CLOSE-DELAY(CLOSE,6))/DELAY(CLOSE,6)*VOLUME
        if n >= 7 and c[-7] != 0: r["a191_029"] = self._s((c[-1]-c[-7])/c[-7]*v[-1])

        # Alpha191#31: (CLOSE-MEAN(CLOSE,12))/MEAN(CLOSE,12)*100
        if n >= 12:
            m12 = sma(c, 12)
            if m12 and m12 != 0: r["a191_031"] = self._s((c[-1]-m12)/m12*100)

        # Alpha191#32: -SUM(RANK(CORR(VWAP,DELAY(CLOSE,1),4)),8)/8
        if n >= 14:
            dc1 = [0]+[c[i-1] for i in range(1, n)]
            cs = ts_corr_series(vw, dc1, 4)
            valid = [x for x in cs[-8:] if x is not None]
            if valid: r["a191_032"] = self._s(-sum(valid)/len(valid))

        # Alpha191#33: ((-TSMIN(LOW,5))+DELAY(TSMIN(LOW,5),5)) * RANK(SUM(RET,240)-SUM(RET,20))/220 * TSRANK(VOLUME,5)
        if n >= 240:
            ml5 = ts_min(l, 5)
            ml5_d = min(l[-10:-5]) if n >= 10 else ml5
            if ml5 and ml5_d:
                ret_diff = (ts_sum(ret, 240) or 0) - (ts_sum(ret, 20) or 0)
                trv = ts_rank(v, 5)
                if trv: r["a191_033"] = self._s((-ml5+ml5_d)*(ret_diff/220)*trv)

        # Alpha191#34: MEAN(CLOSE, 12) / CLOSE
        if n >= 12:
            m12 = sma(c, 12)
            if m12 and c[-1] != 0: r["a191_034"] = self._s(m12/c[-1])

        # Alpha191#35: MIN(RANK(DECAYLINEAR(DELTA(OPEN,1),15)), RANK(DECAYLINEAR(CORR(VOLUME,OPEN*0.65+CLOSE*0.35,17),7)))
        if n >= 20:
            do_s = delta_series(o, 1); do_clean = [x or 0 for x in do_s]
            dl1 = decay_linear(do_clean, 15)
            mix = [o[i]*0.65+c[i]*0.35 for i in range(n)]
            cs = ts_corr_series(v, mix, 17)
            cs_clean = [x or 0 for x in cs]
            dl2 = decay_linear(cs_clean, 7)
            if dl1 is not None and dl2 is not None: r["a191_035"] = self._s(min(dl1, dl2))

        # Alpha191#37: -CORR(OPEN, VOLUME, 10) * RANK(ABS(CLOSE-VWAP))
        if n >= 10:
            cr = ts_corr(o, v, 10)
            if cr is not None: r["a191_037"] = self._s(-cr*abs(c[-1]-vw[-1]))

        # Alpha191#38: -TSRANK(CLOSE, 10) * RANK(CLOSE/OPEN)
        if n >= 10:
            trc = ts_rank(c, 10)
            if trc and o[-1] != 0: r["a191_038"] = self._s(-trc*(c[-1]/o[-1]))

        # Alpha191#39: -RANK(DELTA(CLOSE,2)) * DECAYLINEAR(CORR(VWAP,VOLUME,8),4) * (1-RANK(DECAYLINEAR(RET,5)))
        if n >= 15:
            dc2 = delta_s(c, 2)
            cs = ts_corr_series(vw, v, 8); cs_clean = [x or 0 for x in cs]
            dl1 = decay_linear(cs_clean, 4)
            dl2 = decay_linear(ret, 5)
            if dc2 is not None and dl1 is not None and dl2 is not None:
                r["a191_039"] = self._s(-dc2*dl1*(1-dl2))

        # Alpha191#43: SUM(CLOSE>DELAY(CLOSE,1)?VOL:CLOSE<DELAY(CLOSE,1)?-VOL:0, 6)
        if n >= 7:
            s = 0
            for i in range(-6, 0):
                if c[i] > c[i-1]: s += v[i]
                elif c[i] < c[i-1]: s -= v[i]
            r["a191_043"] = self._s(s)

        # Alpha191#44: TSRANK(DECAYLINEAR(CORR(LOW,MEAN(VOLUME,10),7),6),4) + TSRANK(DECAYLINEAR(DELTA(VWAP,3),10),15)
        if n >= 25:
            mv10 = [sma(v[:i+1], min(10,i+1)) for i in range(n)]
            cs = ts_corr_series(l, mv10, 7); cs_clean = [x or 0 for x in cs]
            dl1 = decay_linear(cs_clean, 6)
            dv3 = delta_series(vw, 3); dv_clean = [x or 0 for x in dv3]
            dl2 = decay_linear(dv_clean, 10)
            if dl1 is not None and dl2 is not None:
                r["a191_044"] = self._s((dl1 or 0)+(dl2 or 0))

        # Alpha191#45: RANK(DELTA(CLOSE*0.6+OPEN*0.4, 1)) * RANK(CORR(VWAP, MEAN(VOLUME,150),15))
        if n >= 150:
            mix = [c[i]*0.6+o[i]*0.4 for i in range(n)]
            dm = delta_s(mix, 1)
            mv150 = [sma(v[:i+1], min(150,i+1)) for i in range(n)]
            cr = ts_corr(vw, mv150, 15)
            if dm is not None and cr is not None: r["a191_045"] = self._s(dm*cr)

        # Alpha191#46: (MEAN(CLOSE,3)+MEAN(CLOSE,6)+MEAN(CLOSE,12)+MEAN(CLOSE,24))/(4*CLOSE)
        if n >= 24:
            m3 = sma(c,3); m6 = sma(c,6); m12 = sma(c,12); m24 = sma(c,24)
            if all(x is not None for x in [m3,m6,m12,m24]) and c[-1] != 0:
                r["a191_046"] = self._s((m3+m6+m12+m24)/(4*c[-1]))

        # Alpha191#49: SUM(HIGH+LOW >= DELAY(HIGH,1)+DELAY(LOW,1) ? 0 : MAX(ABS(HIGH-DELAY(HIGH,1)), ABS(LOW-DELAY(LOW,1))), 12)
        if n >= 13:
            s = 0
            for i in range(-12, 0):
                if h[i]+l[i] >= h[i-1]+l[i-1]: s += 0
                else: s += max(abs(h[i]-h[i-1]), abs(l[i]-l[i-1]))
            r["a191_049"] = self._s(s)

        # Alpha191#50: SUM(HL_condition, 12) / SUM(..., 12) — DI-like
        if n >= 13:
            up = 0; down = 0
            for i in range(-12, 0):
                if h[i]+l[i] > h[i-1]+l[i-1]: up += max(abs(h[i]-h[i-1]), abs(l[i]-l[i-1]))
                elif h[i]+l[i] < h[i-1]+l[i-1]: down += max(abs(h[i]-h[i-1]), abs(l[i]-l[i-1]))
            total = up + down
            if total > 0: r["a191_050"] = self._s((up-down)/total)

        # Alpha191#51: SUM condition similar to #49 but different threshold
        if n >= 13:
            s = 0
            for i in range(-12, 0):
                if h[i]+l[i] <= h[i-1]+l[i-1]: s += 0
                else: s += max(abs(h[i]-h[i-1]), abs(l[i]-l[i-1]))
            r["a191_051"] = self._s(s)

        # Alpha191#53: COUNT(CLOSE>DELAY(CLOSE,1), 12) / 12 * 100
        if n >= 13:
            cnt = sum(1 for i in range(-12, 0) if c[i] > c[i-1])
            r["a191_053"] = self._s(cnt/12*100)

        # Alpha191#54: -RANK(STD(ABS(CLOSE-OPEN)) + (CLOSE-OPEN) + CORR(CLOSE,OPEN,10))
        if n >= 10:
            co = [abs(c[i]-o[i]) for i in range(n)]
            sd = stdev(co, 5); cr = ts_corr(c, o, 10)
            if sd is not None and cr is not None:
                r["a191_054"] = self._s(-(sd + (c[-1]-o[-1]) + cr))

        # Alpha191#55: SUM(16*(CLOSE-DELAY(CLOSE,1)+(CLOSE-OPEN)/2+DELAY(CLOSE,1)-DELAY(OPEN,1)) / (ABS(HIGH-DELAY(CLOSE,1))>ABS(LOW-DELAY(CLOSE,1))?...), 20)
        # Simplified: Williams AD-like
        if n >= 21:
            s = 0
            for i in range(-20, 0):
                true_range_val = max(h[i]-l[i], abs(h[i]-c[i-1]), abs(l[i]-c[i-1]))
                if true_range_val > 0:
                    s += (c[i]-c[i-1]+(c[i]-o[i])/2+(c[i-1]-o[i-1]))/true_range_val
            r["a191_055"] = self._s(s)

        # Alpha191#56: OPEN^0.1 * RANK(VWAP-MIN(VWAP,12)) * ... simplified
        if n >= 12:
            mv12 = ts_min(vw, 12)
            if mv12: r["a191_056"] = self._s((o[-1]**0.1) * (vw[-1]-mv12))

        # Alpha191#58: COUNT(CLOSE>DELAY(CLOSE,1), 20) / 20 * 100
        if n >= 21:
            cnt = sum(1 for i in range(-20, 0) if c[i] > c[i-1])
            r["a191_058"] = self._s(cnt/20*100)

        # Alpha191#59: SUM(CLOSE==DELAY(CLOSE,1)?0:CLOSE-MAX/MIN(...), 20)
        if n >= 21:
            s = 0
            for i in range(-20, 0):
                if c[i] != c[i-1]:
                    if c[i] > c[i-1]: s += c[i] - min(l[i], c[i-1])
                    else: s += c[i] - max(h[i], c[i-1])
            r["a191_059"] = self._s(s)

        # Alpha191#60: SUM((2*CLOSE-LOW-HIGH)/(HIGH-LOW+0.001)*VOLUME, 20)
        if n >= 20:
            s = sum((2*c[i]-l[i]-h[i])/(h[i]-l[i]+0.001)*v[i] for i in range(-20, 0))
            r["a191_060"] = self._s(s)

        # Alpha191#61: MAX(RANK(DECAYLINEAR(DELTA(VWAP,1),12)), RANK(DECAYLINEAR(RANK(CORR(LOW,MEAN(VOL,80),8)),17)))
        if n >= 80:
            dv = delta_series(vw, 1); dv_clean = [x or 0 for x in dv]
            dl1 = decay_linear(dv_clean, 12)
            mv80 = [sma(v[:i+1], min(80,i+1)) for i in range(n)]
            cs = ts_corr_series(l, mv80, 8); cs_clean = [x or 0 for x in cs]
            dl2 = decay_linear(cs_clean, 17)
            if dl1 is not None and dl2 is not None: r["a191_061"] = self._s(max(dl1, dl2))

        # Alpha191#62: -CORR(HIGH, RANK(VOLUME), 5)
        if n >= 10:
            vr = ts_rank_series(v, 5)
            cr = ts_corr(h, [x or 0 for x in vr], 5)
            if cr is not None: r["a191_062"] = self._s(-cr)

        # Alpha191#64: MAX(RANK(DECAYLINEAR(CORR(RANK(VWAP),RANK(VOLUME),4),4)), RANK(DECAYLINEAR(MAX(CORR(RANK(CLOSE),RANK(MEAN(VOL,60)),4),13),14)))
        if n >= 60:
            vwrk = ts_rank_series(vw, 5); vrk = ts_rank_series(v, 5)
            cs1 = ts_corr_series([x or 0 for x in vwrk], [x or 0 for x in vrk], 4)
            dl1 = decay_linear([x or 0 for x in cs1], 4)
            mv60 = [sma(v[:i+1], min(60,i+1)) for i in range(n)]
            crk = ts_rank_series(c, 5); mrk = ts_rank_series(mv60, 5)
            cs2 = ts_corr_series([x or 0 for x in crk], [x or 0 for x in mrk], 4)
            dl2 = decay_linear([x or 0 for x in cs2], 14)
            if dl1 is not None and dl2 is not None: r["a191_064"] = self._s(max(dl1, dl2))

        # Alpha191#65: MEAN(CLOSE, 6) / CLOSE
        if n >= 6:
            m6 = sma(c, 6)
            if m6 and c[-1] != 0: r["a191_065"] = self._s(m6/c[-1])

        # Alpha191#68: SMA(((HIGH+LOW)/2-(DELAY(HIGH,1)+DELAY(LOW,1))/2)*(HIGH-LOW)/VOLUME, 15)
        if n >= 16:
            vals = []
            for i in range(1, n):
                hl_mid = (h[i]+l[i])/2; prev_hl = (h[i-1]+l[i-1])/2
                rng = h[i]-l[i]
                vals.append((hl_mid-prev_hl)*rng/(v[i]+0.001))
            if len(vals) >= 15: r["a191_068"] = self._s(sma(vals, 15))

        # Alpha191#71: (CLOSE-MEAN(CLOSE,24))/MEAN(CLOSE,24)*100 * (TSRANK(CLOSE,24))
        if n >= 24:
            m24 = sma(c, 24); trc = ts_rank(c, 24)
            if m24 and m24 != 0 and trc: r["a191_071"] = self._s((c[-1]-m24)/m24*100*trc)

        # Alpha191#72: SMA(MAX(HIGH-DELAY(CLOSE,1),0), 15) / SMA(ABS(HIGH-DELAY(CLOSE,1)), 15) * 100
        if n >= 16:
            pos = [max(h[i]-c[i-1], 0) for i in range(1, n)]
            total = [abs(h[i]-c[i-1]) for i in range(1, n)]
            sp = sma(pos, 15); st = sma(total, 15)
            if sp is not None and st is not None and st != 0: r["a191_072"] = self._s(sp/st*100)

        # Alpha191#74: RANK(CORR(SUM(LOW*0.35+VWAP*0.65, 20), SUM(MEAN(VOLUME,40), 20), 7))
        if n >= 60:
            mix = [l[i]*0.35+vw[i]*0.65 for i in range(n)]
            sum_mix = [sum(mix[max(0,i-19):i+1]) for i in range(n)]
            mv40 = [sma(v[:i+1], min(40,i+1)) for i in range(n)]
            sum_mv = [sum(mv40[max(0,i-19):i+1]) for i in range(n)]
            cr = ts_corr(sum_mix, sum_mv, 7)
            if cr is not None: r["a191_074"] = self._s(cr)

        # Alpha191#75: COUNT(CLOSE>OPEN && BANCHMARK..., 50) simplified as bullish candle ratio
        if n >= 50:
            cnt = sum(1 for i in range(-50, 0) if c[i] > o[i])
            r["a191_075"] = self._s(cnt/50*100)

        # Alpha191#77: MIN(RANK(DECAYLINEAR((HIGH+LOW)/2+HIGH-CLOSE-LOW,20)), RANK(DECAYLINEAR(CORR(((HIGH+LOW)/2),MEAN(VOLUME,40),3),6)))
        if n >= 40:
            raw1 = [(h[i]+l[i])/2+h[i]-c[i]-l[i] for i in range(n)]
            dl1 = decay_linear(raw1, 20)
            mv40 = [sma(v[:i+1], min(40,i+1)) for i in range(n)]
            hl2 = [(h[i]+l[i])/2 for i in range(n)]
            cs = ts_corr_series(hl2, mv40, 3)
            dl2 = decay_linear([x or 0 for x in cs], 6)
            if dl1 is not None and dl2 is not None: r["a191_077"] = self._s(min(dl1, dl2))

        # Alpha191#78: ((HIGH+LOW+CLOSE)/3 - MA(((HIGH+LOW+CLOSE)/3),12)) / MA(ABS(...),12)*100
        if n >= 12:
            tp = [(h[i]+l[i]+c[i])/3 for i in range(n)]
            ma_tp = sma(tp, 12)
            if ma_tp:
                diffs = [abs(tp[i]-sma(tp[:i+1], min(12,i+1))) for i in range(n)]
                ma_diff = sma(diffs, 12)
                if ma_diff and ma_diff != 0:
                    r["a191_078"] = self._s((tp[-1]-ma_tp)/ma_diff*100)

        # Alpha191#80: (VOLUME-DELAY(VOLUME,5))/DELAY(VOLUME,5)*100
        if n >= 6 and v[-6] != 0: r["a191_080"] = self._s((v[-1]-v[-6])/v[-6]*100)

        # Alpha191#81: SMA(VOLUME, 21)
        if n >= 21: r["a191_081"] = self._s(sma(v, 21))

        # Alpha191#83: -RANK(COV(RANK(HIGH), RANK(VOLUME), 5))
        if n >= 10:
            hr = ts_rank_series(h, 5); vr = ts_rank_series(v, 5)
            cv = ts_cov([x or 0 for x in hr], [x or 0 for x in vr], 5)
            if cv is not None: r["a191_083"] = self._s(-cv)

        # Alpha191#84: SUM(CLOSE>DELAY(CLOSE,1)?VOL:(CLOSE<DELAY(CLOSE,1)?-VOL:0), 20)
        if n >= 21:
            s = 0
            for i in range(-20, 0):
                if c[i] > c[i-1]: s += v[i]
                elif c[i] < c[i-1]: s -= v[i]
            r["a191_084"] = self._s(s)

        # Alpha191#85: TSRANK(VOLUME/MEAN(VOLUME,20), 20) * TSRANK(-DELTA(CLOSE,7), 8)
        if n >= 27:
            vr = [v[i]/(adv20[i] if adv20[i]>0 else 1) for i in range(n)]
            trv = ts_rank(vr, 20)
            ndc = [-1*(c[i]-c[i-7]) for i in range(7, n)]
            trn = ts_rank(ndc, 8) if len(ndc) >= 8 else None
            if trv and trn: r["a191_085"] = self._s(trv*trn)

        # Alpha191#88: (CLOSE-DELAY(CLOSE,20))/DELAY(CLOSE,20)*100
        if n >= 21 and c[-21] != 0: r["a191_088"] = self._s((c[-1]-c[-21])/c[-21]*100)

        # Alpha191#89: 2*(SMA(CLOSE,13)-SMA(CLOSE,27)) / SMA(CLOSE,10)
        if n >= 27:
            s13 = sma(c, 13); s27 = sma(c, 27); s10 = sma(c, 10)
            if all(x is not None for x in [s13,s27,s10]) and s10 != 0:
                r["a191_089"] = self._s(2*(s13-s27)/s10)

        # Alpha191#90: -RANK(CORR(RANK(VWAP), RANK(VOLUME), 5))
        if n >= 10:
            vwrk = ts_rank_series(vw, 5); vrk = ts_rank_series(v, 5)
            cr = ts_corr([x or 0 for x in vwrk], [x or 0 for x in vrk], 5)
            if cr is not None: r["a191_090"] = self._s(-cr)

        # Alpha191#91: -RANK(CLOSE-MAX(CLOSE,5)) * RANK(CORR(MEAN(VOLUME,40),LOW,5))
        if n >= 40:
            mc5 = ts_max(c, 5)
            mv40 = [sma(v[:i+1], min(40,i+1)) for i in range(n)]
            cr = ts_corr(mv40, l, 5)
            if mc5 and cr is not None: r["a191_091"] = self._s(-(c[-1]-mc5)*cr)

        # Alpha191#93: SUM(OPEN>=DELAY(OPEN,1)?0:MAX(OPEN-LOW, OPEN-DELAY(OPEN,1)), 20)
        if n >= 21:
            s = 0
            for i in range(-20, 0):
                if o[i] < o[i-1]: s += max(o[i]-l[i], o[i]-o[i-1])
            r["a191_093"] = self._s(s)

        # Alpha191#94: SUM(CLOSE>DELAY(CLOSE,1)?VOL:-VOL, 30)
        if n >= 31:
            s = sum(v[i] if c[i] > c[i-1] else -v[i] for i in range(-30, 0))
            r["a191_094"] = self._s(s)

        # Alpha191#95: STD(AMOUNT, 20) — using volume*close as proxy for amount
        if n >= 20:
            amounts = [c[i]*v[i] for i in range(n)]
            sd = stdev(amounts, 20)
            if sd is not None: r["a191_095"] = self._s(sd)

        # Alpha191#96: SMA(SMA((CLOSE-TSMIN(LOW,9))/(TSMAX(HIGH,9)-TSMIN(LOW,9)+0.001)*100, 3), 3)
        if n >= 15:
            raw = []
            for i in range(9, n):
                lo = min(l[i-8:i+1]); hi = max(h[i-8:i+1])
                raw.append((c[i]-lo)/(hi-lo+0.001)*100)
            if len(raw) >= 6:
                sm1_vals = [sum(raw[max(0,i-2):i+1])/min(3,i+1) for i in range(len(raw))]
                sm2 = sma(sm1_vals, 3)
                if sm2 is not None: r["a191_096"] = self._s(sm2)

        # Alpha191#98: STD(VOLUME, 10) / MEAN(VOLUME, 10) * 100
        if n >= 10:
            sd = stdev(v, 10); mn = sma(v, 10)
            if sd is not None and mn and mn != 0: r["a191_098"] = self._s(sd/mn*100)

        # Alpha191#101: (CLOSE-OPEN)/((HIGH-LOW)+0.001)
        r["a191_101"] = self._s((c[-1]-o[-1])/((h[-1]-l[-1])+0.001))

        # Alpha191#102: SMA(MAX(VOLUME-DELAY(VOLUME,1), 0), 6) / SMA(ABS(VOLUME-DELAY(VOLUME,1)), 6) * 100
        if n >= 7:
            pos = [max(v[i]-v[i-1], 0) for i in range(1, n)]
            tot = [abs(v[i]-v[i-1]) for i in range(1, n)]
            sp = sma(pos, 6); st = sma(tot, 6)
            if sp is not None and st is not None and st != 0: r["a191_102"] = self._s(sp/st*100)

        # Alpha191#104: -DELTA(CORR(HIGH, VOLUME, 5), 5) * RANK(STD(CLOSE, 20))
        if n >= 15:
            cs = ts_corr_series(h, v, 5)
            valid = [x or 0 for x in cs]
            if len(valid) >= 6:
                d5 = valid[-1] - valid[-6]
                sd = stdev(c, 20)
                if sd is not None: r["a191_104"] = self._s(-d5*sd)

        # Alpha191#105: -CORR(RANK(OPEN), RANK(VOLUME), 10)
        if n >= 15:
            ork = ts_rank_series(o, 5); vrk = ts_rank_series(v, 5)
            cr = ts_corr([x or 0 for x in ork], [x or 0 for x in vrk], 10)
            if cr is not None: r["a191_105"] = self._s(-cr)

        # Alpha191#107: (-RANK(OPEN-DELAY(HIGH,1))) * RANK(OPEN-DELAY(CLOSE,1)) * RANK(OPEN-DELAY(LOW,1))
        if n >= 2:
            r["a191_107"] = self._s(-(o[-1]-h[-2])*(o[-1]-c[-2])*(o[-1]-l[-2]))

        # Alpha191#108: RANK(HIGH-MIN(HIGH,2)) * RANK(CORR(VWAP, MEAN(VOLUME,120),6))
        if n >= 120:
            mh2 = ts_min(h, 2)
            mv120 = [sma(v[:i+1], min(120,i+1)) for i in range(n)]
            cr = ts_corr(vw, mv120, 6)
            if mh2 and cr is not None: r["a191_108"] = self._s((h[-1]-mh2)*cr)

        # Alpha191#110: SUM(MAX(0,HIGH-DELAY(CLOSE,1)),20)/SUM(MAX(0,DELAY(CLOSE,1)-LOW),20)*100
        if n >= 21:
            up = sum(max(0, h[i]-c[i-1]) for i in range(-20, 0))
            dn = sum(max(0, c[i-1]-l[i]) for i in range(-20, 0))
            if dn != 0: r["a191_110"] = self._s(up/dn*100)

        # Alpha191#150: (CLOSE+HIGH+LOW)/3 * VOLUME
        r["a191_150"] = self._s((c[-1]+h[-1]+l[-1])/3*v[-1])

        # Alpha191#155: SMA(VOLUME, 13) - SMA(VOLUME, 27) — volume MACD-like
        if n >= 27:
            s13 = sma(v, 13); s27 = sma(v, 27)
            if s13 is not None and s27 is not None: r["a191_155"] = self._s(s13-s27)

        # Alpha191#158: (HIGH-SMA(CLOSE,15))-(LOW-SMA(CLOSE,15))
        if n >= 15:
            sm = sma(c, 15)
            if sm: r["a191_158"] = self._s((h[-1]-sm)-(l[-1]-sm))

        # Alpha191#160: SMA(CLOSE<=DELAY(CLOSE,1)?STD(CLOSE,20):0, 20)
        if n >= 40:
            vals = []
            for i in range(20, n):
                if c[i] <= c[i-1]:
                    sd = stdev(c[:i+1], 20)
                    vals.append(sd if sd else 0)
                else: vals.append(0)
            if len(vals) >= 20: r["a191_160"] = self._s(sum(vals[-20:])/20)

        # Alpha191#170: RANK(1/CLOSE) * VOLUME / MEAN(VOLUME,20) * ((HIGH*RANK(HIGH-CLOSE))/(SUM(HIGH,5)/5)) - RANK(VWAP-DELAY(VWAP,5))
        if n >= 20:
            vr = v[-1]/(adv20[-1] if adv20[-1]>0 else 1)
            sh5 = sum(h[-5:])/5
            if sh5 != 0 and c[-1] != 0:
                part1 = (1/c[-1]) * vr * (h[-1]*(h[-1]-c[-1])/sh5)
                part2 = (vw[-1]-vw[-6]) if n >= 6 else 0
                r["a191_170"] = self._s(part1-part2)

        # Alpha191#176: DECAYLINEAR(RANK(CORR(RANK(VOL),RANK(VWAP),6))*RANK(CORR(RANK(CLOSE),RANK(MEAN(VOL,10)),10)),12)
        if n >= 25:
            vrk = ts_rank_series(v, 5); wk = ts_rank_series(vw, 5)
            cs1 = ts_corr_series([x or 0 for x in vrk], [x or 0 for x in wk], 6)
            crk = ts_rank_series(c, 5)
            mv10 = [sma(v[:i+1], min(10,i+1)) for i in range(n)]
            mrk = ts_rank_series(mv10, 5)
            cs2 = ts_corr_series([x or 0 for x in crk], [x or 0 for x in mrk], 10)
            prod = [(cs1[i] or 0)*(cs2[i] or 0) for i in range(n)]
            dl = decay_linear(prod, 12)
            if dl is not None: r["a191_176"] = self._s(dl)

        # Alpha191#178: (CLOSE-DELAY(CLOSE,1))/DELAY(CLOSE,1)*VOLUME
        if n >= 2 and c[-2] != 0: r["a191_178"] = self._s((c[-1]-c[-2])/c[-2]*v[-1])

        # Alpha191#184: RANK(CORR(DELAY(OPEN-CLOSE,1), CLOSE, 200)) + RANK(OPEN-CLOSE)
        if n >= 200:
            doc = [o[i]-c[i] for i in range(n)]
            doc_d1 = [0]+doc[:-1]
            cr = ts_corr(doc_d1, c, 200)
            if cr is not None: r["a191_184"] = self._s(cr + (o[-1]-c[-1]))

        # Alpha191#185: RANK(-(1-OPEN/CLOSE)^2)
        if c[-1] != 0:
            r["a191_185"] = self._s(-((1-o[-1]/c[-1])**2))

        # Alpha191#187: SUM(OPEN<=DELAY(OPEN,1)?0:MAX(HIGH-OPEN,OPEN-DELAY(OPEN,1)), 20)
        if n >= 21:
            s = 0
            for i in range(-20, 0):
                if o[i] > o[i-1]: s += max(h[i]-o[i], o[i]-o[i-1])
            r["a191_187"] = self._s(s)

        # Alpha191#188: ((HIGH-LOW-SMA(HIGH-LOW,11))/SMA(HIGH-LOW,11))*100
        if n >= 11:
            hl = [h[i]-l[i] for i in range(n)]
            sm = sma(hl, 11)
            if sm and sm != 0: r["a191_188"] = self._s((hl[-1]-sm)/sm*100)

        # Alpha191#189: MEAN(ABS(CLOSE-MEAN(CLOSE,6)), 6)
        if n >= 12:
            m6 = sma(c, 6)
            if m6:
                diffs = [abs(c[i]-sma(c[:i+1], min(6,i+1))) for i in range(n)]
                r["a191_189"] = self._s(sma(diffs, 6))

        # Alpha191#190: LOG((COUNT(CLOSE/DELAY(CLOSE)-1>((CLOSE/DELAY(CLOSE,19))^(1/20)-1),20)+1)...) simplified
        # COUNT of days where daily ret > geometric avg ret over 20 days
        if n >= 21:
            geo_ret = (c[-1]/c[-21])**(1/20)-1 if c[-21] != 0 else 0
            cnt = sum(1 for i in range(-20, 0) if c[i-1] != 0 and (c[i]/c[i-1]-1) > geo_ret)
            r["a191_190"] = self._s(log_safe(cnt+1))

        # Alpha191#191: CORR(MEAN(VOLUME,20), LOW, 5) + (HIGH+LOW)/2 - CLOSE
        if n >= 25:
            mv20 = [sma(v[:i+1], min(20,i+1)) for i in range(n)]
            cr = ts_corr(mv20, l, 5)
            if cr is not None: r["a191_191"] = self._s(cr + (h[-1]+l[-1])/2 - c[-1])

        # --- Summary ---
        ts_count = sum(1 for k, val in r.items() if k.startswith("a191_") and val is not None)
        r["_meta"] = {"ts_computed": ts_count, "cs_pending": "~90 factors need scan mode", "total_defined": 191}
        return r

    # ============================================================
    # Cross-Sectional Alpha Computation (for scan mode)
    # ============================================================
    @staticmethod
    def compute_alpha_cross_sectional(all_assets):
        """Compute cross-sectional alpha factors from multi-asset data.
        Args: all_assets = [{"symbol": "BTC-USDT", "closes": [...], "volumes": [...], ...}, ...]
        Returns: {symbol: {"a101_cs_001": val, ...}}
        """
        if not all_assets or len(all_assets) < 5:
            return {"_error": "Need >= 5 assets for cross-sectional rank. Use scan mode."}
        n_assets = len(all_assets)
        result = {a["symbol"]: {} for a in all_assets}

        # Helper: cross-sectional rank
        def cs_rank(values):
            """Rank values across assets (0~1)."""
            sorted_v = sorted(range(len(values)), key=lambda i: values[i])
            ranks = [0.0] * len(values)
            for rank_idx, orig_idx in enumerate(sorted_v):
                ranks[orig_idx] = (rank_idx + 1) / len(values)
            return ranks

        # Compute per-asset metrics
        rets_1d = []; rets_5d = []; rets_10d = []
        vol_ratios = []; price_to_high = []; close_open = []
        for a in all_assets:
            cl = a.get("closes", [])
            vl = a.get("volumes", [])
            ol = a.get("opens", []); hl = a.get("highs", [])
            if len(cl) >= 11:
                rets_1d.append((cl[-1]-cl[-2])/cl[-2] if cl[-2] else 0)
                rets_5d.append((cl[-1]-cl[-6])/cl[-6] if cl[-6] else 0)
                rets_10d.append((cl[-1]-cl[-11])/cl[-11] if cl[-11] else 0)
            else:
                rets_1d.append(0); rets_5d.append(0); rets_10d.append(0)
            if len(vl) >= 20:
                avg_v = sum(vl[-20:])/20
                vol_ratios.append(vl[-1]/avg_v if avg_v > 0 else 1)
            else: vol_ratios.append(1)
            if len(hl) >= 20:
                high20 = max(hl[-20:])
                price_to_high.append(cl[-1]/high20 if high20 else 1)
            else: price_to_high.append(1)
            if ol: close_open.append((cl[-1]-ol[-1])/ol[-1] if ol[-1] else 0)
            else: close_open.append(0)

        # Cross-sectional ranks
        rk_ret1 = cs_rank(rets_1d)
        rk_ret5 = cs_rank(rets_5d)
        rk_ret10 = cs_rank(rets_10d)
        rk_vol = cs_rank(vol_ratios)
        rk_pth = cs_rank(price_to_high)
        rk_co = cs_rank(close_open)

        for i, a in enumerate(all_assets):
            sym = a["symbol"]
            # Alpha101 CS#1: rank(ts_argmax(SignedPower((ret<0?std:close),2),5)) - 0.5
            result[sym]["a101_cs_001"] = round(rk_ret1[i] - 0.5, 6)
            # Alpha101 CS#5: -ts_max(corr(rank_ret, rank_vol, 6), 3) proxy
            result[sym]["a101_cs_005"] = round(-rk_ret5[i]*rk_vol[i], 6)
            # Alpha101 CS#10: rank(max(0, ret_1d)*ret_1d - rank_vol)
            result[sym]["a101_cs_010"] = round(rk_ret1[i]*max(0, rets_1d[i]) - rk_vol[i], 6)
            # Alpha101 CS#11: (rank(ts_max(vwap-close,3)) + rank(ts_min(vwap-close,3))) * rank(delta(volume,3))
            result[sym]["a101_cs_011"] = round(rk_pth[i]*rk_vol[i], 6)
            # General momentum-volume cross rank
            result[sym]["a101_cs_mom_vol"] = round(rk_ret5[i]*0.6 + rk_vol[i]*0.4 - 0.5, 6)
            # Alpha191 cross-sectional
            result[sym]["a191_cs_ret_rank"] = round(rk_ret10[i], 6)
            result[sym]["a191_cs_vol_rank"] = round(rk_vol[i], 6)
            result[sym]["a191_cs_reversal"] = round(1-rk_ret1[i], 6)
            result[sym]["a191_cs_co_rank"] = round(rk_co[i], 6)

        return result

    # --- Private helpers ---
    def _rsi(self, data, period):
        if len(data)<period+1: return None
        gains=[]; losses=[]
        for i in range(1,len(data)):
            d=data[i]-data[i-1]; gains.append(max(d,0)); losses.append(max(-d,0))
        ag=sum(gains[:period])/period; al=sum(losses[:period])/period
        for i in range(period,len(gains)): ag=(ag*(period-1)+gains[i])/period; al=(al*(period-1)+losses[i])/period
        if al==0: return 100.0
        return 100.0-100.0/(1.0+ag/al)

    def _stochastic(self, period, sk=3, sd=3):
        h,l,c = self.highs,self.lows,self.closes
        if len(c)<period: return None
        ks=[]
        for i in range(period-1,len(c)):
            hh=max(h[i-period+1:i+1]); ll=min(l[i-period+1:i+1])
            ks.append((c[i]-ll)/(hh-ll)*100 if hh!=ll else 50)
        k = sma(ks,sk) if len(ks)>=sk else (ks[-1] if ks else None)
        ksm = sma_series(ks,sk); vk = [x for x in ksm if x is not None]
        d = sma(vk,sd) if len(vk)>=sd else None
        if k is None: return None
        return {"k":k,"d":d}

    def _stoch_rsi(self, period):
        rsi_vals=[]
        for i in range(period+1,len(self.closes)+1):
            r=self._rsi(self.closes[:i],period)
            if r is not None: rsi_vals.append(r)
        if len(rsi_vals)<period: return None
        recent=rsi_vals[-period:]; mx=max(recent); mn=min(recent)
        if mx==mn: return 0.5
        return (rsi_vals[-1]-mn)/(mx-mn)

    def _cci(self, period):
        h,l,c=self.highs,self.lows,self.closes
        if len(c)<period: return None
        tp=[(h[i]+l[i]+c[i])/3 for i in range(len(c))]
        tp_s=sma(tp,period)
        if not tp_s: return None
        md=sum(abs(tp[i]-tp_s) for i in range(-period,0))/period
        if md==0: return 0
        return (tp[-1]-tp_s)/(0.015*md)

    def _macd(self, data, fast, slow, sig):
        fe=ema(data,fast); se=ema(data,slow)
        if fe is None or se is None: return None
        ml=fe-se; fs=ema_series(data,fast); ss=ema_series(data,slow)
        mv=[f-s for f,s in zip(fs,ss) if f is not None and s is not None]
        sl=ema(mv,sig) if len(mv)>=sig else None
        return {"macd":ml,"signal":sl,"hist":ml-sl if sl else None}

    def _trix(self, data, period):
        e1=[x for x in ema_series(data,period) if x is not None]
        e2=[x for x in ema_series(e1,period) if x is not None] if len(e1)>=period else []
        e3=[x for x in ema_series(e2,period) if x is not None] if len(e2)>=period else []
        if len(e3)<2 or e3[-2]==0: return None
        return (e3[-1]-e3[-2])/e3[-2]*100

    def _kst(self, data):
        if len(data)<30: return None
        def roc_sma(d,rp,sp):
            rocs=[(d[i]-d[i-rp])/d[i-rp]*100 for i in range(rp,len(d)) if d[i-rp]!=0]
            return sma(rocs,sp) if len(rocs)>=sp else None
        r1,r2,r3,r4=roc_sma(data,10,10),roc_sma(data,15,10),roc_sma(data,20,10),roc_sma(data,30,15)
        if any(x is None for x in [r1,r2,r3,r4]): return None
        kv=r1+r2*2+r3*3+r4*4
        return {"kst":kv,"signal":kv*0.9}

    def _ultimate_osc(self, p1=7, p2=14, p3=28):
        h,l,c=self.highs,self.lows,self.closes
        if len(c)<p3+1: return None
        bp=[]; trl=[]
        for i in range(1,len(c)):
            tl=min(l[i],c[i-1]); bp.append(c[i]-tl); trl.append(max(h[i],c[i-1])-tl)
        def avg(d,t,p):
            if len(d)<p: return None
            s=sum(d[-p:]); tt=sum(t[-p:])
            return s/tt if tt else 0
        a1,a2,a3=avg(bp,trl,p1),avg(bp,trl,p2),avg(bp,trl,p3)
        if any(x is None for x in [a1,a2,a3]): return None
        return 100*(4*a1+2*a2+a3)/7

    def _rvi(self, period):
        o,h,l,c=self.opens,self.highs,self.lows,self.closes
        if len(c)<period+3: return None
        nums=[]; dens=[]
        for i in range(3,len(c)):
            n=(c[i]-o[i])+2*(c[i-1]-o[i-1])+2*(c[i-2]-o[i-2])+(c[i-3]-o[i-3])
            d=(h[i]-l[i])+2*(h[i-1]-l[i-1])+2*(h[i-2]-l[i-2])+(h[i-3]-l[i-3])
            nums.append(n/6); dens.append(d/6)
        if len(nums)<period: return None
        n=sum(nums[-period:])/period; d=sum(dens[-period:])/period
        return n/d if d else 0

    def _mfi(self, period):
        h,l,c,v=self.highs,self.lows,self.closes,self.volumes
        if len(c)<period+1: return None
        tp=[(h[i]+l[i]+c[i])/3 for i in range(len(c))]
        pf=nf=0.0
        for i in range(-period,0):
            mf=tp[i]*v[i]
            if tp[i]>tp[i-1]: pf+=mf
            elif tp[i]<tp[i-1]: nf+=mf
        if nf==0: return 100.0
        return 100.0-100.0/(1.0+pf/nf)

    def _cmf(self, period):
        h,l,c,v=self.highs,self.lows,self.closes,self.volumes
        if len(c)<period: return None
        mfv=vs=0.0
        for i in range(-period,0):
            hl=h[i]-l[i]; mfm=((c[i]-l[i])-(h[i]-c[i]))/hl if hl else 0
            mfv+=mfm*v[i]; vs+=v[i]
        return mfv/vs if vs else 0

    def _adx(self, period):
        h,l,c=self.highs,self.lows,self.closes
        if len(c)<period+1: return None
        pdm=[]; mdm=[]; trs=[]
        for i in range(1,len(c)):
            up=h[i]-h[i-1]; down=l[i-1]-l[i]
            pdm.append(max(up,0) if up>down else 0); mdm.append(max(down,0) if down>up else 0)
            trs.append(true_range(h[i],l[i],c[i-1]))
        if len(trs)<period: return None
        atv=sum(trs[:period])/period; pd=sum(pdm[:period])/period; md=sum(mdm[:period])/period
        for i in range(period,len(trs)):
            atv=(atv*(period-1)+trs[i])/period; pd=(pd*(period-1)+pdm[i])/period; md=(md*(period-1)+mdm[i])/period
        if atv==0: return None
        pdi=pd/atv*100; mdi=md/atv*100
        dx=abs(pdi-mdi)/(pdi+mdi)*100 if pdi+mdi>0 else 0
        return {"adx":dx,"plus_di":pdi,"minus_di":mdi}

    def _parabolic_sar(self, af_s=0.02, af_step=0.02, af_max=0.2):
        h,l=self.highs,self.lows
        if len(h)<3: return None
        bull=True; sar=l[0]; ep=h[0]; af=af_s
        for i in range(1,len(h)):
            ps=sar; sar=ps+af*(ep-ps)
            if bull:
                sar=min(sar,l[i-1]); sar=min(sar,l[i-2]) if i>=2 else sar
                if h[i]>ep: ep=h[i]; af=min(af+af_step,af_max)
                if l[i]<sar: bull=False; sar=ep; ep=l[i]; af=af_s
            else:
                sar=max(sar,h[i-1]); sar=max(sar,h[i-2]) if i>=2 else sar
                if l[i]<ep: ep=l[i]; af=min(af+af_step,af_max)
                if h[i]>sar: bull=True; sar=ep; ep=h[i]; af=af_s
        return sar

    def _mass_index(self, period):
        h,l=self.highs,self.lows
        if len(h)<period+18: return None
        hl=[h[i]-l[i] for i in range(len(h))]
        s1=[x for x in ema_series(hl,9) if x is not None]
        s2=[x for x in ema_series(s1,9) if x is not None] if len(s1)>=9 else []
        if len(s1)<period or len(s2)<period: return None
        return sum(a/b for a,b in zip(s1[-period:],s2[-period:]) if b!=0)

    # ---- Main entry ----
    def compute_all(self):
        r = OrderedDict()
        r["trend"]=self.compute_trend(); r["momentum"]=self.compute_momentum()
        r["volatility"]=self.compute_volatility(); r["volume"]=self.compute_volume()
        r["custom"]=self.compute_custom()
        r["patterns"]=self.compute_patterns()
        r["divergence"]=self.compute_divergence()
        r["structure"]=self.compute_structure()
        r["advanced_volatility"]=self.compute_advanced_volatility()
        r["alpha101"]=self.compute_alpha101()
        r["alpha191"]=self.compute_alpha191()
        total=sum(self._cnt(v) for v in r.values() if isinstance(v,dict))
        r["meta"]={"total_indicators":total,"periods_used":self.periods,"candle_count":self.n}
        return r

    def compute_categories(self, cats):
        m={"trend":self.compute_trend,"momentum":self.compute_momentum,"volatility":self.compute_volatility,
           "volume":self.compute_volume,"custom":self.compute_custom,
           "patterns":self.compute_patterns,"divergence":self.compute_divergence,
           "structure":self.compute_structure,"advanced_volatility":self.compute_advanced_volatility,
           "alpha101":self.compute_alpha101,"alpha191":self.compute_alpha191}
        r=OrderedDict()
        for c in cats:
            if c in m: r[c]=m[c]()
        total=sum(self._cnt(v) for v in r.values() if isinstance(v,dict))
        r["meta"]={"total_indicators":total,"categories":cats}
        return r

    def _cnt(self, d, depth=0):
        c=0
        for v in d.values():
            if isinstance(v,dict): c+=self._cnt(v,depth+1)
            elif isinstance(v,list): c+=len(v)
            elif v is not None: c+=1
        return c

    @classmethod
    def list_all(cls):
        return {
            "trend": "SMA/EMA/WMA/DEMA/TEMA/HMA/KAMA x12 + SuperTrend x8 + Ichimoku(5) + SAR + Aroon x4 + ADX x4 + Vortex x4 + DPO x3",
            "momentum": "RSI x12 + Stoch x4 + StochRSI x2 + Williams%R x4 + CCI x4 + ROC x5 + Momentum x8 + MACD x3 + TRIX x3 + KST + UltOsc + RVI x2",
            "volatility": "BB x16 + Keltner x3 + Donchian x4 + ATR/NATR x12 + TR + HistVol x3",
            "volume": "OBV + VWAP + MFI x3 + CMF x3 + A/D + Chaikin + EoM x3 + Force x3 + PVT + VolRatio x4",
            "custom": "HeikinAshi + Pivot x4 + FibRetrace + ElderRay + MassIdx + ConsecCandles + PriceLevels",
            "patterns": "30+ candlestick patterns: Doji(4), Hammer, Hanging Man, Shooting Star, Marubozu, Engulfing, Harami, Piercing, Dark Cloud, Morning/Evening Star, Three Soldiers/Crows, etc.",
            "divergence": "RSI/MACD/OBV regular + hidden divergence detection (bullish & bearish)",
            "structure": "Swing highs/lows, market structure (HH/HL/LH/LL), auto S/R levels, trendlines, price position",
            "advanced_volatility": "Parkinson, Garman-Klass, Yang-Zhang volatility + volatility cone/percentile + regime detection",
            "alpha101": "WorldQuant Alpha#101 ~40 time-series factors (002-101): momentum, mean-reversion, volume-price, volatility-adjusted. CS factors in scan mode.",
            "alpha191": "WorldQuant Alpha#191 ~80 time-series factors (001-191): extended momentum, volume dynamics, price structure, decay-weighted. CS factors in scan mode."
        }


def main():
    parser = argparse.ArgumentParser(description="Extended Indicator Engine")
    parser.add_argument("--candles", help="Path to JSON candle data file")
    parser.add_argument("--mode", choices=["full","category","custom"], default="full")
    parser.add_argument("--categories", help="Comma-separated categories (trend,momentum,volatility,volume,custom,patterns,divergence,structure,advanced_volatility,alpha101,alpha191)")
    parser.add_argument("--indicators", help="Comma-separated indicator names")
    parser.add_argument("--periods", help="Comma-separated periods (default: 5,7,9,10,14,20,21,25,30,50,100,200)")
    parser.add_argument("--output", help="Output JSON file (default: stdout)")
    parser.add_argument("--list", action="store_true", help="List indicators")
    parser.add_argument("--count", action="store_true", help="Show counts")
    args = parser.parse_args()
    if args.list: print(json.dumps(IndicatorEngine.list_all(),indent=2)); return
    if args.count:
        for k,v in IndicatorEngine.list_all().items(): print(f"{k}: {v}")
        return
    if not args.candles: parser.error("--candles required")
    try:
        with open(args.candles) as f:
            content = f.read().strip()
            if not content:
                print(json.dumps({"error":"Candle data file is empty. Check if data fetch succeeded."})); sys.exit(1)
            raw = json.loads(content)
    except FileNotFoundError:
        print(json.dumps({"error":f"Candle data file not found: {args.candles}"})); sys.exit(1)
    except json.JSONDecodeError as e:
        print(json.dumps({"error":f"Invalid JSON in candle data: {str(e)[:200]}"})); sys.exit(1)
    candles = raw.get("data",raw.get("candles",raw)) if isinstance(raw,dict) else raw
    if not candles: print(json.dumps({"error":"No candle data in file. API may have returned empty result."})); sys.exit(1)
    periods = [int(p.strip()) for p in args.periods.split(",")] if args.periods else None
    engine = IndicatorEngine(candles, periods)
    if args.mode=="full": result=engine.compute_all()
    elif args.mode=="category": result=engine.compute_categories([c.strip() for c in (args.categories or "trend").split(",")])
    elif args.mode=="custom":
        all_data=engine.compute_all(); result={}
        if args.indicators:
            names=[n.strip().lower() for n in args.indicators.split(",")]
            for cat,vals in all_data.items():
                if cat=="meta": continue
                if isinstance(vals,dict):
                    for k,v in vals.items():
                        if any(nm in k.lower() for nm in names): result[k]=v
        else: result=all_data
    else: result=engine.compute_all()
    output=json.dumps(result,indent=2,default=str)
    if args.output:
        with open(args.output,"w") as f: f.write(output)
        print(f"Written to {args.output}")
    else: print(output)

if __name__=="__main__": main()
EXT_INDICATORS_ENGINE
