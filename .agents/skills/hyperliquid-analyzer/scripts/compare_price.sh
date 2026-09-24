#!/usr/bin/env bash
# compare_price.sh — HL vs OKX 价格价差监控（跨所套利信号）
# 用法: ./compare_price.sh [COIN1 COIN2 ...]
# 示例: ./compare_price.sh BTC ETH SOL
# 依赖: jq + okx CLI (npm install -g @okx_ai/okx-trade-cli)

set -euo pipefail

command -v jq >/dev/null 2>&1 || {
  echo "ERROR: 需要安装 jq: brew install jq (macOS) / apt install jq (Linux)" >&2
  exit 1
}

OKX_AVAILABLE=true
command -v okx >/dev/null 2>&1 || {
  echo "提示: 未检测到 okx CLI，仅显示 HL 价格"
  echo "安装: npm install -g @okx_ai/okx-trade-cli"
  echo ""
  OKX_AVAILABLE=false
}

COINS="${*:-BTC ETH SOL}"

# 一次获取 HL 所有中间价
HL_MID=$(curl -s --max-time 10 https://api.hyperliquid.xyz/info \
  -X POST -H "Content-Type: application/json" \
  -d '{"type": "allMids"}' 2>/dev/null)

# 同时获取 HL metaAndAssetCtxs 用于 mark/oracle 对比
HL_META=$(curl -s --max-time 10 https://api.hyperliquid.xyz/info \
  -X POST -H "Content-Type: application/json" \
  -d '{"type": "metaAndAssetCtxs"}' 2>/dev/null)

if [ -z "$HL_MID" ] || [ -z "$HL_META" ]; then
  echo "ERROR: 无法连接 Hyperliquid API" >&2
  exit 1
fi

echo ""
echo "HL vs OKX — 价格价差分析"
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"

if [ "$OKX_AVAILABLE" = true ]; then
  printf "%-6s %-12s %-12s %-10s %-12s %-12s %s\n" \
    "资产" "HL价格" "OKX价格" "价差%" "HL标记价" "HL预言机" "溢价%"
  echo "------ ------------ ------------ ---------- ------------ ------------ ------"
else
  printf "%-6s %-12s %-12s %s\n" "资产" "HL标记价" "HL预言机" "溢价%"
  echo "------ ------------ ------------ ------"
fi

for COIN in $COINS; do
  HL_PRICE=$(echo "$HL_MID" | jq -r --arg c "$COIN" '.[$c] // "N/A"')

  if [ "$HL_PRICE" = "N/A" ] || [ "$HL_PRICE" = "null" ]; then
    printf "%-6s 未上市于HL\n" "$COIN"
    continue
  fi

  # 获取 mark price 和 oracle price
  HL_MARK=$(echo "$HL_META" | jq -r --arg c "$COIN" '
    [ .[0].universe, .[1] ] | transpose |
    map(select(.[0].name == $c)) |
    if length > 0 then .[0][1].markPx // "N/A" else "N/A" end
  ')
  HL_ORACLE=$(echo "$HL_META" | jq -r --arg c "$COIN" '
    [ .[0].universe, .[1] ] | transpose |
    map(select(.[0].name == $c)) |
    if length > 0 then .[0][1].oraclePx // "N/A" else "N/A" end
  ')

  # HL mark vs oracle 溢价
  if [ "$HL_MARK" != "N/A" ] && [ "$HL_ORACLE" != "N/A" ]; then
    PREMIUM=$(echo "$HL_MARK $HL_ORACLE" | awk '{printf "%+.4f%%", ($1-$2)/$2*100}')
  else
    PREMIUM="N/A"
  fi

  HL_FMT=$(echo "$HL_PRICE" | awk '{
    if ($1 >= 1000) printf "$%\047.2f", $1
    else printf "$%.4f", $1
  }')
  HL_MARK_FMT=$(echo "$HL_MARK" | awk '{
    if ($1 >= 1000) printf "$%\047.2f", $1
    else printf "$%.4f", $1
  }')
  HL_ORA_FMT=$(echo "$HL_ORACLE" | awk '{
    if ($1 >= 1000) printf "$%\047.2f", $1
    else printf "$%.4f", $1
  }')

  if [ "$OKX_AVAILABLE" = true ]; then
    # 优先尝试永续合约价格（更接近 HL 的 perp 对比），fallback 到现货
    OKX_PRICE=$(okx --live market ticker "${COIN}-USDT-SWAP" 2>/dev/null | awk '/^last / {print $NF}' || true)
    if [ -z "$OKX_PRICE" ]; then
      OKX_PRICE=$(okx --live market ticker "${COIN}-USDT" 2>/dev/null | awk '/^last / {print $NF}' || true)
    fi

    if [ -n "$OKX_PRICE" ]; then
      SPREAD=$(echo "$HL_PRICE $OKX_PRICE" | awk '{if($2==0) print "N/A"; else printf "%+.4f%%", ($1-$2)/$2*100}')
      OKX_FMT=$(echo "$OKX_PRICE" | awk '{
        if ($1 >= 1000) printf "$%\047.2f", $1
        else printf "$%.4f", $1
      }')

      printf "%-6s %-12s %-12s %-10s %-12s %-12s %s\n" \
        "$COIN" "$HL_FMT" "$OKX_FMT" "$SPREAD" "$HL_MARK_FMT" "$HL_ORA_FMT" "$PREMIUM"
    else
      printf "%-6s %-12s %-12s %-10s %-12s %-12s %s\n" \
        "$COIN" "$HL_FMT" "OKX N/A" "N/A" "$HL_MARK_FMT" "$HL_ORA_FMT" "$PREMIUM"
    fi
  else
    printf "%-6s %-12s %-12s %s\n" "$COIN" "$HL_MARK_FMT" "$HL_ORA_FMT" "$PREMIUM"
  fi
done

echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo "说明:"
echo "  价差%  = (HL价格 - OKX价格) / OKX价格 × 100%"
echo "  溢价%  = (HL标记价 - HL预言机) / HL预言机 × 100%（内部价格偏离）"
echo "  正溢价 → DEX相对CEX乐观，可能回调；负溢价 → DEX相对悲观"
echo "  价差 > 0.1% 时可考虑跨所价差策略（需考虑手续费和滑点）"
echo "时间: $(date -u '+%Y-%m-%d %H:%M UTC')"
