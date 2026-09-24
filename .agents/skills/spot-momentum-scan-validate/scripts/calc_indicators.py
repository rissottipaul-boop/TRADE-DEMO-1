"""
量价共振指标计算脚本
输入: OKX OHLCV 数据列表（每条: [ts, open, high, low, close, vol, volCcy]）
输出: 各指标值字典
"""
import math


def ema(data, period):
    """计算指数移动平均（仅返回最终值，用于单值计算）"""
    if len(data) < period:
        return None
    k = 2.0 / (period + 1)
    result = sum(data[:period]) / period
    for v in data[period:]:
        result = v * k + result * (1 - k)
    return result


def ema_series(data, period):
    """
    计算完整 EMA 历史序列，返回与 data 等长的列表。
    前 period-1 个位置填 None（数据不足），第 period 个位置用 SMA 做种子，
    之后每步用标准 EMA 递推: EMA_t = close_t * k + EMA_{t-1} * (1-k)
    这是 TradingView / 大多数交易所使用的标准做法。
    """
    if len(data) < period:
        return [None] * len(data)
    k = 2.0 / (period + 1)
    result = [None] * (period - 1)
    seed = sum(data[:period]) / period
    result.append(seed)
    for v in data[period:]:
        result.append(v * k + result[-1] * (1 - k))
    return result


def calc_ma(closes, period=20):
    """计算简单移动平均"""
    if len(closes) < period:
        return None
    return sum(closes[-period:]) / period


def calc_macd(closes, fast=12, slow=26, signal=9):
    """
    用完整双 EMA 历史序列计算 MACD，与 TradingView 标准一致。

    步骤:
      1. 对全量 closes 分别计算 EMA(fast) 和 EMA(slow) 的完整历史序列
      2. MACD 线序列 = EMA_fast_series - EMA_slow_series（仅在两者都有值时）
      3. 对 MACD 线序列再做 EMA(signal) 得到信号线历史序列
      4. Histogram = MACD 线 - 信号线（取最新值）

    返回: (macd_line, signal_line, histogram) 均为最新一根 K 线的值
    """
    min_bars = slow + signal  # 最少需要 26+9=35 根 K 线才能算出完整信号线
    if len(closes) < min_bars:
        return None, None, None

    fast_series = ema_series(closes, fast)   # 长度 = len(closes)，前 fast-1 为 None
    slow_series = ema_series(closes, slow)   # 前 slow-1 为 None

    # 构建 MACD 线序列（只保留两者都非 None 的部分）
    macd_series = []
    for f, s in zip(fast_series, slow_series):
        if f is not None and s is not None:
            macd_series.append(f - s)

    # 需要至少 signal 根 MACD 值才能算信号线
    if len(macd_series) < signal:
        return None, None, None

    signal_series = ema_series(macd_series, signal)

    # 取最新值
    macd_val   = macd_series[-1]
    signal_val = signal_series[-1]

    if signal_val is None:
        return None, None, None

    histogram = macd_val - signal_val
    return macd_val, signal_val, histogram


def calc_rsi(closes, period=14):
    """
    用 Wilder 平滑法计算 RSI，与 TradingView 标准一致。

    标准做法：
      1. 第一个平均增益/减损 = 前 period 个变动的简单平均（SMA 做种子）
      2. 之后每步用 Wilder 平滑: avg_gain = (prev_avg_gain * (period-1) + gain) / period
      3. RSI = 100 - 100 / (1 + avg_gain / avg_loss)

    比简单滑动窗口版本更稳定，不会因窗口截断而产生跳变。
    """
    if len(closes) < period + 1:
        return None

    deltas = [closes[i] - closes[i - 1] for i in range(1, len(closes))]
    gains  = [max(d, 0) for d in deltas]
    losses = [max(-d, 0) for d in deltas]

    # 用前 period 根做 SMA 种子
    avg_gain = sum(gains[:period]) / period
    avg_loss = sum(losses[:period]) / period

    # Wilder 平滑递推其余每根
    for i in range(period, len(gains)):
        avg_gain = (avg_gain * (period - 1) + gains[i]) / period
        avg_loss = (avg_loss * (period - 1) + losses[i]) / period

    if avg_loss == 0:
        return 100.0
    rs = avg_gain / avg_loss
    return 100 - 100 / (1 + rs)


def calc_vol_ratio(volumes, period=20):
    """计算量比（当前K线量 / 近20根均量）"""
    if len(volumes) < period + 1:
        return None
    avg_vol = sum(volumes[-(period + 1):-1]) / period
    if avg_vol == 0:
        return None
    return volumes[-1] / avg_vol


def parse_candles(raw_candles):
    """
    解析 OKX market_get_candles 返回的 K 线数据。
    OKX 返回格式: [[ts, open, high, low, close, vol, volCcy], ...] 倒序（最新在前）
    """
    candles = sorted(raw_candles, key=lambda x: int(x[0]))  # 按时间升序
    opens   = [float(c[1]) for c in candles]
    highs   = [float(c[2]) for c in candles]
    lows    = [float(c[3]) for c in candles]
    closes  = [float(c[4]) for c in candles]
    volumes = [float(c[5]) for c in candles]
    return opens, highs, lows, closes, volumes


def analyze(inst_id, raw_candles, params=None):
    """
    分析单个币对，返回指标字典和信号判断。

    参数:
        inst_id: 交易对 ID，如 "BTC-USDT"
        raw_candles: OKX 返回的 K 线原始数据
        params: 策略参数字典，可选

    返回: dict
    """
    if params is None:
        params = {}

    price_change_min = params.get("price_change_min", 1.0)
    vol_ratio_min    = params.get("vol_ratio_min", 1.5)
    rsi_min          = params.get("rsi_min", 50)
    rsi_max          = params.get("rsi_max", 72)

    if len(raw_candles) < 30:
        return {"inst_id": inst_id, "signal": False, "reason": "数据不足"}

    opens, highs, lows, closes, volumes = parse_candles(raw_candles)

    # 计算各指标
    ma20       = calc_ma(closes, 20)
    _, _, macd_hist = calc_macd(closes)
    rsi        = calc_rsi(closes)
    vol_ratio  = calc_vol_ratio(volumes)
    current_close = closes[-1]
    current_open  = opens[-1]

    # 当前K线涨幅
    price_change_pct = (current_close - current_open) / current_open * 100 if current_open != 0 else 0

    # 信号强度评分 (0-10)
    score = 0
    if vol_ratio:
        score += min(vol_ratio / vol_ratio_min * 3, 5)  # 量比贡献最多5分
    score += min(price_change_pct / price_change_min * 3, 5)  # 涨幅贡献最多5分

    # 信号条件逐一检查
    checks = {
        "price_above_ma20": ma20 is not None and current_close > ma20,
        "macd_bullish":     macd_hist is not None and macd_hist > 0,
        "price_change_ok":  price_change_pct >= price_change_min,
        "vol_ratio_ok":     vol_ratio is not None and vol_ratio >= vol_ratio_min,
        "rsi_ok":           rsi is not None and rsi_min <= rsi <= rsi_max,
    }

    signal = all(checks.values())

    # 找出不满足条件的项
    failed = [k for k, v in checks.items() if not v]

    return {
        "inst_id":          inst_id,
        "signal":           signal,
        "score":            round(score, 2),
        "price_change_pct": round(price_change_pct, 4),
        "vol_ratio":        round(vol_ratio, 4) if vol_ratio else None,
        "rsi":              round(rsi, 2) if rsi else None,
        "macd_hist":        round(macd_hist, 8) if macd_hist else None,
        "ma20":             round(ma20, 6) if ma20 else None,
        "close_price":      current_close,
        "checks":           checks,
        "failed_checks":    failed,
        "reason":           "全部满足" if signal else f"未满足: {', '.join(failed)}",
    }


def floor_to_lot_size(qty, lot_sz):
    """将下单数量 floor 到最小交易单位"""
    if lot_sz <= 0:
        return qty
    decimals = len(str(lot_sz).rstrip("0").split(".")[-1]) if "." in str(lot_sz) else 0
    factor = 10 ** decimals
    return math.floor(qty * factor) / factor


if __name__ == "__main__":
    # 简单测试
    import json
    # 模拟 60 根 K 线数据
    import random
    random.seed(42)
    price = 100.0
    mock_candles = []
    for i in range(60):
        ts = str(1700000000000 + i * 3600000)
        o = price
        c = price * (1 + random.uniform(-0.02, 0.03))
        h = max(o, c) * (1 + random.uniform(0, 0.01))
        l = min(o, c) * (1 - random.uniform(0, 0.01))
        vol = random.uniform(1000, 5000)
        if i == 59:  # 最后一根放大量
            vol *= 2.5
            c = o * 1.025  # 涨2.5%
        mock_candles.append([ts, str(o), str(h), str(l), str(c), str(vol), str(vol * c)])
        price = c

    result = analyze("BTC-USDT", mock_candles)
    print(json.dumps(result, ensure_ascii=False, indent=2))
