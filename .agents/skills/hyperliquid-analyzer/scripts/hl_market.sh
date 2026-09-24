#!/usr/bin/env bash
# hl_market.sh — Hyperliquid 市场概览（24h 数据 + 趋势 + 风险）
# 用法: ./hl_market.sh [top_n] [COIN1 COIN2 ...]
# 示例: ./hl_market.sh 10              # 前10大资产概览
#       ./hl_market.sh 0 BTC ETH SOL  # 指定币种详细分析

set -euo pipefail

command -v jq >/dev/null 2>&1 || {
  echo "ERROR: 需要安装 jq: brew install jq (macOS) / apt install jq (Linux)" >&2
  exit 1
}

TOP_N="${1:-10}"
shift || true
SPECIFIC_COINS="$*"

RAW=$(curl -s --max-time 10 https://api.hyperliquid.xyz/info \
  -X POST -H "Content-Type: application/json" \
  -d '{"type": "metaAndAssetCtxs"}' 2>/dev/null)

if [ -z "$RAW" ] || [ "$RAW" = "null" ]; then
  echo "ERROR: 无法连接 Hyperliquid API" >&2
  exit 1
fi

echo "$RAW" | jq '.' >/dev/null 2>&1 || {
  echo "ERROR: API 响应格式异常" >&2
  exit 1
}

# 构建过滤器：按指定币种 or 按持仓量排名
if [ -n "$SPECIFIC_COINS" ]; then
  COINS_JSON=$(echo "$SPECIFIC_COINS" | jq -Rc '[split(" ") | .[]]')
  FILTER="map(select([.coin] | inside($COINS_JSON)))"
else
  FILTER="sort_by(-.oi_usd_m) | .[:$TOP_N]"
fi

RESULT=$(echo "$RAW" | jq --argjson top "$TOP_N" --argjson filter_coins "$([ -n "$SPECIFIC_COINS" ] && echo "$SPECIFIC_COINS" | jq -Rc '[split(" ") | .[]]' || echo 'null')" '
  [ .[0].universe, .[1] ] | transpose |
  map(select(.[1].markPx != null) | {
    coin:         .[0].name,
    mark_px:      (.[1].markPx | tonumber),
    oracle_px:    (.[1].oraclePx | tonumber),
    prev_day_px:  (.[1].prevDayPx | tonumber),
    change_pct:   (((.[1].markPx | tonumber) - (.[1].prevDayPx | tonumber)) /
                   (.[1].prevDayPx | tonumber) * 100 | . * 100 | round / 100),
    volume_m:     (.[1].dayNtlVlm | tonumber / 1e6 | . * 100 | round / 100),
    oi_usd_m:     ((.[1].openInterest | tonumber) * (.[1].markPx | tonumber) / 1e6 | . * 100 | round / 100),
    funding_8h:   (.[1].funding | tonumber),
    premium_pct:  (((.[1].markPx | tonumber) - (.[1].oraclePx | tonumber)) /
                   (.[1].oraclePx | tonumber) * 100 | . * 10000 | round / 10000),
    max_lev:      .[0].maxLeverage
  }) |
  if $filter_coins != null then map(select([.coin] | inside($filter_coins)))
  else sort_by(-.oi_usd_m) | .[:$top] end
')

echo ""
echo "Hyperliquid DEX — 市场概览"
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
printf "%-6s %-12s %-8s %-10s %-10s %-10s %-8s %s\n" \
  "资产" "价格" "24h变化" "24h量(M)" "持仓量(M)" "资金费率" "溢价" "趋势"
echo "------ ------------ -------- ---------- ---------- -------- ------ ----"

echo "$RESULT" | jq -r '.[] | [.coin, .mark_px, .change_pct, .volume_m, .oi_usd_m, .funding_8h, .premium_pct] | @tsv' | \
while IFS=$'\t' read -r COIN MARK CHNG VOL OI FUND PREM; do
  # 方向判断
  TREND=$(echo "$CHNG $FUND" | awk '{
    if ($1 > 1 && $2 > 0) print "🟢 强涨"
    else if ($1 > 0) print "↑ 涨"
    else if ($1 < -1 && $2 < 0) print "🔴 强跌"
    else if ($1 < 0) print "↓ 跌"
    else print "— 平"
  }')

  CHNG_FMT=$(echo "$CHNG" | awk '{printf "%+.2f%%", $1}')
  MARK_FMT=$(echo "$MARK" | awk '{
    if ($1 >= 1000) printf "%\047.2f", $1
    else printf "%.4f", $1
  }')
  FUND_FMT=$(echo "$FUND" | awk '{printf "%+.5f%%", $1 * 100}')
  PREM_FMT=$(echo "$PREM" | awk '{printf "%+.4f%%", $1}')

  printf "%-6s \$%-11s %-8s \$%-9s \$%-9s %-8s %-8s %s\n" \
    "$COIN" "$MARK_FMT" "$CHNG_FMT" "$VOL" "$OI" "$FUND_FMT" "$PREM_FMT" "$TREND"
done

# 市场整体判断
SUMMARY=$(echo "$RESULT" | jq -r '
  {
    bull: (map(select(.change_pct > 0)) | length),
    bear: (map(select(.change_pct < 0)) | length),
    total: length,
    avg_change: (map(.change_pct) | add / length | . * 100 | round / 100)
  } |
  "上涨: \(.bull)/\(.total) | 下跌: \(.bear)/\(.total) | 平均涨跌: \(.avg_change)%"
')

echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo "市场概况: $SUMMARY"

# 整体趋势判定
MARKET_TREND=$(echo "$RESULT" | jq -r '
  (map(select(.change_pct > 0)) | length) as $bull |
  length as $total |
  if $bull / $total >= 0.7 then "🟢 整体偏多"
  elif $bull / $total <= 0.3 then "🔴 整体偏空"
  else "⚖️ 多空分歧"
  end
')
echo "整体趋势: $MARKET_TREND"
echo "时间: $(date -u '+%Y-%m-%d %H:%M UTC')"
