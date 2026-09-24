#!/usr/bin/env bash
# hl_whales.sh — 扫描 Hyperliquid 订单簿大单墙（巨鲸挂单快照）
# 用法: ./hl_whales.sh [min_usd] [COIN1 COIN2 ...]
# 示例: ./hl_whales.sh 500000 BTC ETH SOL    # 查 >50万美元挂单墙
#       ./hl_whales.sh                        # 默认 >20万，主流币

set -euo pipefail

command -v jq >/dev/null 2>&1 || {
  echo "ERROR: 需要安装 jq: brew install jq" >&2; exit 1
}

MIN_USD="${1:-200000}"
shift || true
COINS="${*:-BTC ETH SOL HYPE BNB XRP DOGE AVAX LINK TAO}"

MIN_FMT=$(echo "$MIN_USD" | awk '{
  if ($1>=1000000) printf "%.0fM", $1/1000000
  else printf "%.0fK", $1/1000
}')

echo ""
echo "Hyperliquid — 订单簿大单墙扫描 (挂单 > \$$MIN_FMT)"
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
printf "%-6s %-6s %-12s %-14s %-10s\n" "币种" "方向" "挂单额" "价格" "数量"
echo "------ ------ ------------ -------------- ----------"

for COIN in $COINS; do
  RAW=$(curl -s --max-time 8 https://api.hyperliquid.xyz/info \
    -X POST -H "Content-Type: application/json" \
    -d "{\"type\": \"l2Book\", \"coin\": \"$COIN\", \"nSigFigs\": 5}" 2>/dev/null)

  if [ -z "$RAW" ] || [ "$RAW" = "null" ]; then
    continue
  fi

  # 扫描 bid 和 ask 各 50 档，找超过阈值的大单
  echo "$RAW" | jq -r --argjson min "$MIN_USD" --arg coin "$COIN" '
    (
      [.levels[0][:50][] |
        (.px | tonumber) as $px |
        (.sz | tonumber) as $sz |
        ($px * $sz) as $usd |
        select($usd >= $min) |
        [$coin, "BID", ($usd|. * 100 | round / 100 | tostring), .px, .sz]
      ],
      [.levels[1][:50][] |
        (.px | tonumber) as $px |
        (.sz | tonumber) as $sz |
        ($px * $sz) as $usd |
        select($usd >= $min) |
        [$coin, "ASK", ($usd|. * 100 | round / 100 | tostring), .px, .sz]
      ]
    ) | .[] | @tsv
  ' | sort -t$'\t' -k3 -rn | while IFS=$'\t' read -r C SIDE USD PX SZ; do
    USD_FMT=$(echo "$USD" | awk '{
      if ($1>=1000000) printf "\$%.2fM", $1/1000000
      else printf "\$%.1fK", $1/1000
    }')
    PX_FMT=$(echo "$PX" | awk '{
      if ($1>=1000) printf "\$%\047.0f", $1
      else printf "\$%.4f", $1
    }')
    SZ_FMT=$(echo "$SZ" | awk '{printf "%.4f", $1}')
    SIDE_LABEL=$([ "$SIDE" = "BID" ] && echo "买墙 🟢" || echo "卖墙 🔴")

    printf "%-6s %-8s %-12s %-14s %-10s\n" \
      "$C" "$SIDE_LABEL" "$USD_FMT" "$PX_FMT" "$SZ_FMT"
  done
done

echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo "说明: 买墙=大量买单挂在盘口下方（支撑）| 卖墙=大量卖单挂在盘口上方（阻力）"
echo "时间: $(date -u '+%Y-%m-%d %H:%M UTC')"
