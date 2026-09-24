#!/usr/bin/env bash
# compare_funding.sh — HL vs OKX 资金费率对比（套利信号）
# 用法: ./compare_funding.sh [COIN1 COIN2 ...]
# 示例: ./compare_funding.sh BTC ETH SOL
# 依赖: jq + okx CLI (npm install -g @okx_ai/okx-trade-cli)

set -euo pipefail

command -v jq >/dev/null 2>&1 || {
  echo "ERROR: 需要安装 jq: brew install jq (macOS) / apt install jq (Linux)" >&2
  exit 1
}

OKX_AVAILABLE=true
command -v okx >/dev/null 2>&1 || {
  echo "提示: 未检测到 okx CLI，将仅显示 HL 数据"
  echo "安装: npm install -g @okx_ai/okx-trade-cli"
  echo ""
  OKX_AVAILABLE=false
}

COINS="${*:-BTC ETH SOL HYPE ARB}"

# 获取 HL 全量资金费率数据（一次 API 调用）
HL_RAW=$(curl -s --max-time 10 https://api.hyperliquid.xyz/info \
  -X POST -H "Content-Type: application/json" \
  -d '{"type": "metaAndAssetCtxs"}' 2>/dev/null)

if [ -z "$HL_RAW" ] || [ "$HL_RAW" = "null" ]; then
  echo "ERROR: 无法连接 Hyperliquid API" >&2
  exit 1
fi

echo ""
echo "HL vs OKX — 资金费率对比 (8h周期)"
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"

if [ "$OKX_AVAILABLE" = true ]; then
  printf "%-6s %-14s %-14s %-12s %-10s %s\n" \
    "资产" "HL费率(8h)" "OKX费率(8h)" "价差" "价差年化" "情绪信号"
  echo "------ -------------- -------------- ------------ ---------- -------"
else
  printf "%-6s %-14s %-12s %s\n" "资产" "HL费率(8h)" "年化" "说明"
  echo "------ -------------- ------------ --------"
fi

TOTAL_ARB_OPP=0

for COIN in $COINS; do
  # 提取 HL 资金费率
  HL_RATE=$(echo "$HL_RAW" | jq -r --arg c "$COIN" '
    [ .[0].universe, .[1] ] | transpose |
    map(select(.[0].name == $c)) |
    if length > 0 then .[0][1].funding // "N/A"
    else "N/A" end
  ')

  if [ "$HL_RATE" = "N/A" ] || [ "$HL_RATE" = "null" ]; then
    printf "%-6s %-14s\n" "$COIN" "未上市于HL"
    continue
  fi

  HL_RATE_FMT=$(echo "$HL_RATE" | awk '{printf "%+.5f%%", $1 * 100}')
  HL_ANN=$(echo "$HL_RATE" | awk '{printf "%+.1f%%", $1 * 3 * 365 * 100}')

  if [ "$OKX_AVAILABLE" = true ]; then
    # 获取 OKX 资金费率（CLI 输出为 key-value 文本格式，|| true 防止非零退出触发 set -e）
    OKX_RATE=$(okx --live market funding-rate "${COIN}-USDT-SWAP" 2>/dev/null | \
      awk '/^fundingRate/ {print $NF}' || true)

    if [ -z "$OKX_RATE" ] || [ "$OKX_RATE" = "N/A" ]; then
      printf "%-6s %-14s %-14s\n" "$COIN" "$HL_RATE_FMT" "OKX N/A"
      continue
    fi

    OKX_RATE_FMT=$(echo "$OKX_RATE" | awk '{printf "%+.5f%%", $1 * 100}')

    # 计算价差和套利年化收益
    SPREAD=$(echo "$HL_RATE $OKX_RATE" | awk '{printf "%.6f", $1 - $2}')
    SPREAD_ANN=$(echo "$SPREAD" | awk '{printf "%+.1f%%", $1 * 3 * 365 * 100}')
    SPREAD_FMT=$(echo "$SPREAD" | awk '{printf "%+.5f%%", $1 * 100}')

    # 多空拥挤信号：两所费率差反映情绪分歧
    ABS_SPREAD=$(echo "$SPREAD" | awk '{print ($1<0)?-$1:$1}')
    SIGNAL=$(echo "$SPREAD $ABS_SPREAD" | awk '{
      if ($2 < 0.00005) print "两所一致"
      else if ($1 > 0) print "OKX多头更拥挤 ⚠"
      else print "HL多头更拥挤 ⚠"
    }')

    if echo "$ABS_SPREAD" | awk '{exit ($1 >= 0.00005) ? 0 : 1}'; then
      TOTAL_ARB_OPP=$((TOTAL_ARB_OPP + 1))
    fi

    printf "%-6s %-14s %-14s %-12s %-10s %s\n" \
      "$COIN" "$HL_RATE_FMT" "$OKX_RATE_FMT" "$SPREAD_FMT" "$SPREAD_ANN" "$SIGNAL"
  else
    DIRECTION=$(echo "$HL_RATE" | awk '{print ($1 > 0) ? "多头付费" : "空头付费"}')
    printf "%-6s %-14s %-12s %s\n" "$COIN" "$HL_RATE_FMT" "$HL_ANN" "$DIRECTION"
  fi
done

echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"

if [ "$OKX_AVAILABLE" = true ]; then
  echo "分歧信号: $TOTAL_ARB_OPP 个币种两所费率差 >0.005%(8h)，情绪出现分歧"
  echo ""
  echo "解读指引:"
  echo "  - OKX多头更拥挤 → OKX 多头过热，拥挤一侧容易反转，在 OKX 追多需谨慎"
  echo "  - HL多头更拥挤  → HL 链上多头承压，可参考 OKX 方向做反向信号"
  echo "  - 两所方向相反  → 市场分歧最大，避免追势，等待方向确认"
  echo "  - 费率 >0.05%(8h) / 年化 >55%：极端拥挤，反转概率上升"
fi
echo "时间: $(date -u '+%Y-%m-%d %H:%M UTC')"
