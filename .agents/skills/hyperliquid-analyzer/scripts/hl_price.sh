#!/usr/bin/env bash
# hl_price.sh — 查询 Hyperliquid 实时中间价格
# 用法: ./hl_price.sh [COIN1 COIN2 ...]
# 示例: ./hl_price.sh BTC ETH SOL HYPE
# 不传参数则返回常用资产价格

set -euo pipefail

command -v jq >/dev/null 2>&1 || {
  echo "ERROR: 需要安装 jq: brew install jq (macOS) / apt install jq (Linux)" >&2
  exit 1
}

COINS="${*:-BTC ETH SOL HYPE ARB OP}"

RAW=$(curl -s --max-time 10 https://api.hyperliquid.xyz/info \
  -X POST -H "Content-Type: application/json" \
  -d '{"type": "allMids"}' 2>/dev/null)

if [ -z "$RAW" ] || [ "$RAW" = "null" ]; then
  echo "ERROR: 无法连接 Hyperliquid API，请检查网络" >&2
  exit 1
fi

# 验证是否为合法 JSON
echo "$RAW" | jq '.' >/dev/null 2>&1 || {
  echo "ERROR: API 响应格式异常，可能 API 已更新" >&2
  exit 1
}

echo ""
echo "Hyperliquid DEX — 实时价格"
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
printf "%-8s %s\n" "资产" "中间价"
echo "-------- ----------------"

for COIN in $COINS; do
  PRICE=$(echo "$RAW" | jq -r --arg c "$COIN" '.[$c] // empty' 2>/dev/null)
  if [ -z "$PRICE" ]; then
    printf "%-8s %s\n" "$COIN" "未上市"
  else
    # 格式化数字：大于1000加逗号，小数保留2-4位
    FORMATTED=$(echo "$PRICE" | awk '{
      val = $1 + 0
      if (val >= 1000) printf "%\047.2f", val
      else if (val >= 1) printf "%.4f", val
      else printf "%.6f", val
    }')
    printf "%-8s $%s\n" "$COIN" "$FORMATTED"
  fi
done

echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo "来源: Hyperliquid DEX 实时订单簿中间价"
echo "时间: $(date -u '+%Y-%m-%d %H:%M UTC')"
