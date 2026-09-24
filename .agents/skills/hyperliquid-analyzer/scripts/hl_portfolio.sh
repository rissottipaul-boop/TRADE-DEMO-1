#!/usr/bin/env bash
# hl_portfolio.sh — 查询 Hyperliquid 链上持仓（含清算价）
# 用法: ./hl_portfolio.sh [wallet_address]
# 钱包地址优先级: 命令行参数 > $HYPERLIQUID_WALLET_ADDRESS 环境变量

set -euo pipefail

command -v jq >/dev/null 2>&1 || {
  echo "ERROR: 需要安装 jq: brew install jq (macOS) / apt install jq (Linux)" >&2
  exit 1
}

WALLET="${1:-${HYPERLIQUID_WALLET_ADDRESS:-}}"

if [ -z "$WALLET" ]; then
  echo ""
  echo "需要提供 Hyperliquid 钱包地址 (0x...)"
  echo ""
  echo "方式一: ./hl_portfolio.sh 0xYOUR_WALLET_ADDRESS"
  echo "方式二: export HYPERLIQUID_WALLET_ADDRESS=0xYOUR_WALLET_ADDRESS"
  echo ""
  echo "注意: 仅读取链上公开数据，不涉及私钥或交易签名"
  exit 1
fi

RAW=$(curl -s --max-time 10 https://api.hyperliquid.xyz/info \
  -X POST -H "Content-Type: application/json" \
  -d "{\"type\": \"clearinghouseState\", \"user\": \"$WALLET\"}" 2>/dev/null)

if [ -z "$RAW" ] || [ "$RAW" = "null" ]; then
  echo "ERROR: 无法连接 Hyperliquid API" >&2
  exit 1
fi

echo "$RAW" | jq '.' >/dev/null 2>&1 || {
  echo "ERROR: API 响应格式异常" >&2
  exit 1
}

# 检查账户是否存在
ACCT_VALUE=$(echo "$RAW" | jq -r '.marginSummary.accountValue // "0"')

echo ""
echo "Hyperliquid DEX — 持仓汇总"
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
WALLET_SHORT="${WALLET:0:6}...${WALLET: -4}"
echo "钱包: $WALLET_SHORT"

MARGIN_USED=$(echo "$RAW" | jq -r '.marginSummary.totalMarginUsed // "0"')
echo "账户净值: \$$(echo "$ACCT_VALUE" | awk '{printf "%.2f", $1}')"
echo "已用保证金: \$$(echo "$MARGIN_USED" | awk '{printf "%.2f", $1}')"
echo ""

# 检查是否有持仓
HAS_POS=$(echo "$RAW" | jq '[.assetPositions[] | select(.position.szi != "0")] | length')

if [ "$HAS_POS" = "0" ]; then
  echo "当前无未平仓仓位"
  echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
  exit 0
fi

echo "未平仓仓位:"
printf "%-6s %-6s %-8s %-10s %-10s %-10s %-10s %s\n" \
  "资产" "方向" "数量" "开仓价" "清算价" "uPnL" "杠杆" "风险"
echo "------ ------ -------- ---------- ---------- ---------- ------ ------"

echo "$RAW" | jq -r '.assetPositions[] |
  select(.position.szi != "0") |
  [
    .position.coin,
    .position.szi,
    (.position.entryPx // "0"),
    (.position.liquidationPx // "N/A"),
    (.position.unrealizedPnl // "0"),
    (.position.leverage.value // "N/A"),
    (.position.returnOnEquity // "0")
  ] | @tsv' | \
while IFS=$'\t' read -r COIN SZI ENTRY_PX LIQ_PX UPNL LEVERAGE ROE; do
  SIZE=$(echo "$SZI" | awk '{printf "%.4f", ($1 < 0) ? -$1 : $1}')
  SIDE=$(echo "$SZI" | awk '{print ($1 > 0) ? "LONG" : "SHORT"}')
  UPNL_FMT=$(echo "$UPNL" | awk '{printf "%+.2f", $1}')
  ENTRY_FMT=$(echo "$ENTRY_PX" | awk '{printf "%.2f", $1}')

  # 风险评级（基于清算价与当前入场价的距离）
  if [ "$LIQ_PX" = "N/A" ] || [ "$LIQ_PX" = "null" ]; then
    RISK="N/A"
    LIQ_DISPLAY="N/A"
  else
    LIQ_DISPLAY=$(echo "$LIQ_PX" | awk '{printf "%.2f", $1}')
    DIST=$(echo "$ENTRY_PX $LIQ_PX" | awk '{
      d = ($1 - $2) / $1
      if (d < 0) d = -d
      printf "%.4f", d
    }')
    RISK=$(echo "$DIST" | awk '{
      if ($1 < 0.05) print "🔴 HIGH"
      else if ($1 < 0.15) print "🟡 MED"
      else print "🟢 LOW"
    }')
  fi

  printf "%-6s %-6s %-8s \$%-9s \$%-9s \$%-9s %-6sx %s\n" \
    "$COIN" "$SIDE" "$SIZE" "$ENTRY_FMT" "$LIQ_DISPLAY" "$UPNL_FMT" "$LEVERAGE" "$RISK"
done

TOTAL_UPNL=$(echo "$RAW" | jq '[.assetPositions[] |
  select(.position.szi != "0") |
  (.position.unrealizedPnl | tonumber)] | add // 0 | . * 100 | round / 100')

echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo "总未实现盈亏: \$$TOTAL_UPNL"
echo "风险说明: 清算价与入场价距离 <5%=高风险 | 5-15%=中风险 | >15%=低风险"
echo "时间: $(date -u '+%Y-%m-%d %H:%M UTC')"
