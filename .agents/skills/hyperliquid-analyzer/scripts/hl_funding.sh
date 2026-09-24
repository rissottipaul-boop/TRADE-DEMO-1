#!/usr/bin/env bash
# hl_funding.sh — 查询 Hyperliquid 资金费率
# 用法: ./hl_funding.sh [top_n] [desc|asc|abs]
# 示例: ./hl_funding.sh 10 desc     # 按费率从高到低排
#       ./hl_funding.sh 10 abs      # 按绝对值排（多空都看）
#       ./hl_funding.sh 5 asc       # 最负费率（空方最拥挤）

set -euo pipefail

command -v jq >/dev/null 2>&1 || {
  echo "ERROR: 需要安装 jq: brew install jq (macOS) / apt install jq (Linux)" >&2
  exit 1
}

TOP_N="${1:-10}"
ORDER="${2:-desc}"

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

RESULT=$(echo "$RAW" | jq --argjson top "$TOP_N" --arg order "$ORDER" '
  [ .[0].universe, .[1] ] | transpose |
  map(select(.[1].funding != null) | {
    coin:        .[0].name,
    funding_8h:  (.[1].funding | tonumber),
    funding_ann: (.[1].funding | tonumber * 3 * 365 * 100 | round / 100),
    oi_usd_m:    ((.[1].openInterest | tonumber) *
                  ((.[1].markPx // .[1].oraclePx) | tonumber) / 1e6 |
                  round / 100),
    extreme:     (.[1].funding | tonumber | fabs > 0.0005)
  }) |
  if $order == "abs" then sort_by(.funding_8h | fabs) | reverse
  elif $order == "asc" then sort_by(.funding_8h)
  else sort_by(-.funding_8h) end |
  .[:$top]
')

echo ""
echo "Hyperliquid DEX — 资金费率 (8h周期)"
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
printf "%-6s %-4s %-12s %-12s %-10s %s\n" "排名" "资产" "费率(8h)" "年化" "持仓量(M)" "信号"
echo "------ ---- ------------ ------------ ---------- -------"

INDEX=1
while IFS=$'\t' read -r COIN RATE_8H RATE_ANN OI EXTREME; do
  # 判断方向
  RATE_VAL=$(echo "$RATE_8H" | awk '{printf "%.6f", $1}')
  if echo "$RATE_8H" | awk '{exit ($1 >= 0) ? 0 : 1}'; then
    SIGNAL="多方付费"
    PREFIX="+"
  else
    SIGNAL="空方付费"
    PREFIX=""
  fi

  if [ "$EXTREME" = "true" ]; then
    SIGNAL="$SIGNAL ⚠️ EXTREME"
  fi

  printf "%-6s %-6s %s%-10s %s%-10s \$%-9s %s\n" \
    "$INDEX" "$COIN" "$PREFIX" "$RATE_VAL" "$PREFIX" "$RATE_ANN%" "$OI" "$SIGNAL"
  INDEX=$((INDEX + 1))
done < <(echo "$RESULT" | jq -r '.[] | [.coin, .funding_8h, .funding_ann, .oi_usd_m, .extreme] | @tsv')

echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo "说明: 正费率=多方付费(多头拥挤) | 负费率=空方付费(空头拥挤)"
echo "      ⚠️ EXTREME: 费率>0.05%(8h)，可能是反转信号"
echo "      年化 = 8h费率 × 3 × 365"
echo "时间: $(date -u '+%Y-%m-%d %H:%M UTC')"
