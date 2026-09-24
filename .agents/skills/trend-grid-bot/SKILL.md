---
name: trend-grid-bot
description: Trend-adaptive contract grid bot strategy targeting 4%+/month returns with directional bias
trigger: grid bot|trend grid|trendgrid|그리드봇|그리드 전략
tools:
  - mcp__okx-trade__market_get_ticker
  - mcp__okx-trade__market_get_candles
  - mcp__okx-trade__market_get_indicator
  - mcp__okx-trade__market_get_instruments
  - mcp__okx-trade__market_get_funding_rate
  - mcp__okx-trade__account_get_balance
  - mcp__okx-trade__account_get_config
  - mcp__okx-trade__grid_create_order
  - mcp__okx-trade__grid_get_orders
  - mcp__okx-trade__grid_get_order_details
  - mcp__okx-trade__grid_get_sub_orders
  - mcp__okx-trade__grid_stop_order
  - mcp__okx-trade__futures_set_leverage
  - mcp__okx-trade__futures_get_leverage
---

# TrendGrid Bot - Trend-Adaptive Contract Grid Strategy

## Overview

추세 추종형 컨트랙트 그리드 봇. SuperTrend + EMA + MACD 기반으로 추세 방향을 판단하고,
방향성 있는 그리드(long/short)를 배치하여 **그리드 수익 + 추세 수익**을 동시에 포착한다.

**Target:** 월 4%+ (보수적), 실제 기대 8-15%/월 (변동성 정상 구간)

---

## Phase 1: Market Analysis (추세 판단)

아래 지표를 **반드시 모두** 수집한 뒤 종합 판단한다.

### 1-1. 지표 수집

```
필수 지표 (모두 병렬 호출):
- market_get_ticker        → instId: {PAIR}-SWAP         (현재가, 24h 변동)
- market_get_indicator     → supertrend, bar: 4H          (추세 방향 + 지지/저항)
- market_get_indicator     → ema, bar: 4H, params: [20,50](EMA 정배열/역배열)
- market_get_indicator     → macd, bar: 1Dutc             (장기 모멘텀)
- market_get_indicator     → rsi, bar: 4H                 (과매수/과매도)
- market_get_indicator     → bb, bar: 4H                  (변동성 밴드 → 그리드 범위)
- market_get_indicator     → bb, bar: 1Dutc               (일봉 밴드 → 외곽 범위)
- market_get_candles       → bar: 1D, limit: 14           (14일 고/저 → 범위 검증)
- market_get_funding_rate  → instId: {PAIR}-SWAP          (펀딩비 방향 확인)
```

### 1-2. 추세 스코어링 (Trend Score)

각 지표에 점수를 부여하여 합산한다 (-5 ~ +5):

| 지표 | Bullish (+) | Bearish (-) | 중립 (0) |
|------|------------|------------|---------|
| SuperTrend 4H | direction=-1 (UP): **+2** | direction=1 (DOWN): **-2** | - |
| EMA 20/50 | EMA20 > EMA50: **+1** | EMA20 < EMA50: **-1** | 교차 중: 0 |
| MACD Daily | DIF > DEA & MACD > 0: **+1** | DIF < DEA & MACD < 0: **-1** | 0 |
| RSI 4H | 40-65 (건전한 상승): **+0.5** | 35-60 (건전한 하락): **+0.5** | >75 or <25: **-0.5** (과열) |
| Funding Rate | < 0.01% (저렴): **+0.5** | > 0.05% (과열): **-0.5** | 0 |

**판정:**
- Score >= +3.0 → **LONG** (direction=long)
- Score <= -3.0 → **SHORT** (direction=short)
- -3.0 < Score < +3.0 → **NEUTRAL** (direction=neutral)

---

## Phase 2: Grid Parameter Calculation (그리드 파라미터 산출)

### 2-1. 그리드 범위 (Range)

추세 방향에 따라 비대칭 범위를 설정한다:

```
LONG 모드:
  minPx = max(SuperTrend_lowerBand, BB_4H_lower) × 0.995   # 약간의 여유
  maxPx = BB_4H_upper × 1.02                                # 상단 돌파 여유
  → 현재가가 범위의 하위 30-40% 지점에 위치하도록 조정

SHORT 모드:
  minPx = BB_4H_lower × 0.98
  maxPx = min(SuperTrend_upperBand, BB_4H_upper) × 1.005
  → 현재가가 범위의 상위 30-40% 지점에 위치하도록 조정

NEUTRAL 모드:
  minPx = BB_4H_lower × 0.99
  maxPx = BB_4H_upper × 1.01
  → 현재가가 범위의 중앙 부근에 위치
```

**범위 검증:**
- 범위 폭 = (maxPx - minPx) / minPx × 100
- 최소 5%, 최대 15%. 벗어나면 조정
- 현재가가 범위 밖이면 범위를 현재가 기준으로 재조정

### 2-2. 그리드 수 (gridNum) & 간격

```
목표 그리드 간격: 0.15% ~ 0.30% (geometric 기준)

gridNum 계산:
  range_pct = (maxPx - minPx) / minPx × 100
  gridNum = round(range_pct / target_spacing)

  target_spacing 기준:
    변동성 높음 (BB bandwidth > 8%): 0.25-0.30% → 그리드 적게 (25-35개)
    변동성 보통 (BB bandwidth 4-8%): 0.18-0.25% → 그리드 보통 (35-50개)
    변동성 낮음 (BB bandwidth < 4%): 0.15-0.18% → 그리드 많이 (50-70개)

  BB bandwidth = (BB_upper - BB_lower) / BB_middle × 100

제한: gridNum은 최소 20, 최대 100
```

### 2-3. 레버리지 & 마진

```
레버리지 결정:
  Score 절대값 >= 4 (강한 추세): lever = "5"
  Score 절대값 3-4 (보통 추세):  lever = "3"
  NEUTRAL 모드:                  lever = "3"

마진 배분 (sz):
  전체 가용 USDT의 최대 30%를 단일 봇에 배분
  최소 $100, 권장 $300+
```

### 2-4. 리스크 관리

```
SL (손절):
  LONG:  slTriggerPx = minPx × 0.97  (범위 하단 -3%)
  SHORT: slTriggerPx = maxPx × 1.03  (범위 상단 +3%)
  slRatio 대안: "0.08" (마진 대비 8% 손실 시 청산)

TP (익절) - 선택적:
  강한 추세 시 TP 미설정 (추세 수익 극대화)
  NEUTRAL 시 tpRatio: "0.05" (5% 수익 시 종료)
```

---

## Phase 3: Deploy (봇 배포)

### 3-1. 사전 확인

```
1. account_get_config → posMode 확인 (net_mode 권장)
2. account_get_balance → USDT 가용 잔고 확인
3. grid_get_orders(algoOrdType="contract_grid", status="active")
   → 동일 페어에 기존 봇이 있으면 사용자에게 알림
4. market_get_instruments(instType="SWAP", instId="{PAIR}-SWAP")
   → minSz, ctVal, tickSz 확인
```

### 3-2. 주문 실행

```
grid_create_order:
  instId:      "{PAIR}-SWAP"          # e.g. "BTC-USDT-SWAP"
  algoOrdType: "contract_grid"
  maxPx:       "{계산된 상한가}"
  minPx:       "{계산된 하한가}"
  gridNum:     "{계산된 그리드 수}"
  runType:     "2"                     # geometric (비율 기반 등간격)
  direction:   "{long|short|neutral}"
  lever:       "{3|5}"
  sz:          "{배분 마진 USDT}"
  basePos:     true                    # 초기 포지션 즉시 오픈
  slTriggerPx: "{계산된 손절가}"       # 또는 slRatio 사용
  algoClOrdId: "tg-{PAIR}-{timestamp}" # 추적용 고유 ID
```

### 3-3. 배포 후 확인

```
1. grid_get_orders(algoOrdType="contract_grid") → 봇 활성 확인
2. grid_get_order_details(algoOrdType="contract_grid", algoId="{id}")
   → 설정값 + 초기 포지션 확인
3. grid_get_sub_orders(algoOrdType="contract_grid", algoId="{id}", type="live")
   → 대기 주문 정상 배치 확인
```

---

## Phase 4: Monitor & Rebalance (모니터링)

### 4-1. 정기 점검 (4시간 주기 권장)

```
점검 항목:
1. grid_get_order_details → 현재 PnL, 체결 횟수
2. market_get_indicator(supertrend, 4H) → 추세 변화 감지
3. market_get_indicator(ema, 4H, [20,50]) → EMA 정배열 유지 여부
4. market_get_ticker → 현재가 vs 그리드 범위 위치

리밸런싱 트리거:
- 추세 방향 반전 (SuperTrend 플립): 즉시 재배치
- 현재가가 그리드 범위의 상/하위 10%에 도달: 범위 조정
- 누적 수익률 목표 달성 (월 4%+): 사용자에게 보고
```

### 4-2. 리밸런싱 절차

```
1. grid_stop_order(stopType="1") → 기존 봇 종료 (포지션 청산)
2. Phase 1 재실행 → 새로운 추세 판단
3. Phase 2 재실행 → 새로운 파라미터 산출
4. Phase 3 재실행 → 새 봇 배포
```

---

## Phase 5: Performance Report (성과 보고)

### 보고 항목

```
grid_get_order_details 에서 추출:
- 그리드 수익 (gridProfit)
- 미실현 PnL (floatProfit)
- 총 수익률 = (gridProfit + floatProfit) / sz × 100
- 연환산 수익률 = 총 수익률 / 운영일수 × 365
- 체결 횟수 (filledCount)

grid_get_sub_orders(type="filled") 에서 추출:
- 최근 체결 내역
- 평균 체결 간격
- 일일 평균 체결 횟수
```

---

## Return Projection (수익 시뮬레이션)

### 기본 가정

```
투자금: $1,000 USDT 마진
페어: BTC-USDT-SWAP
레버리지: 5x → 실질 노출 $5,000
그리드 수: 40개 (geometric)
범위: 8% (예: $69,000 - $74,520)
그리드 간격: ≈ 0.20%
```

### 수익 계산

```
그리드 1회 체결 수익:
  = 그리드간격 × 레버리지 × (마진/그리드수)
  = 0.20% × 5 × ($1,000/40)
  = 0.20% × 5 × $25
  = $0.25 per round-trip

수수료 (maker 0.02% + taker 0.05% = 편도 ~0.035%):
  = 0.035% × 2 × 5 × $25 = $0.0875
  
순 수익/round-trip = $0.25 - $0.0875 = $0.1625

일일 평균 체결: 8-15 round-trips (BTC 일일 변동성 2-3% 기준)
일일 그리드 수익: $1.30 - $2.44
월간 그리드 수익: $39 - $73 (3.9% - 7.3%)

추세 수익 (direction=long, 월 5% 상승 시):
  = 5% × 5(lever) × 평균 포지션 비율(~50%)
  = 12.5% of margin = $125

총 월간 기대 수익: $164 - $198 (16.4% - 19.8%)
보수적 추정 (변동성 낮은 구간): $40-60 (4% - 6%)
```

---

## Quick Deploy Command Examples

### BTC Long Grid (현재 시장 기준)

```
현재 시장 상태 (2026-04-14):
  BTC: $74,605 | SuperTrend: UP | EMA 20>50 | MACD: Bullish
  Trend Score: +4.5 → LONG

추천 파라미터:
  instId:    "BTC-USDT-SWAP"
  minPx:     "71500"      (SuperTrend support $71,968 근처)
  maxPx:     "77000"      (BB upper + 여유)
  gridNum:   "35"
  runType:   "2"          (geometric)
  direction: "long"
  lever:     "5"
  sz:        "{가용잔고의 30%}"
  basePos:   true
  slTriggerPx: "69500"    (minPx -2.8%)
```

### ETH Long Grid

```
현재 시장 상태:
  ETH: $2,388 | SuperTrend: UP
  
추천 파라미터:
  instId:    "ETH-USDT-SWAP"
  minPx:     "2240"
  maxPx:     "2520"
  gridNum:   "35"
  runType:   "2"
  direction: "long"
  lever:     "5"
  sz:        "{가용잔고의 20%}"
  basePos:   true
  slTriggerPx: "2170"
```

---

## Risk Warnings

1. **레버리지 리스크**: 5x에서 범위 이탈 + SL 미작동 시 큰 손실 가능
2. **추세 반전 리스크**: Long 그리드 중 급락 시 그리드 수익 < 미실현 손실
3. **펀딩비**: Long 포지션은 양(+)의 펀딩비 지불 → 장기 보유 시 비용
4. **유동성 리스크**: 극단적 변동성에서 슬리피지 발생 가능
5. **항상 전체 자산의 30% 이하만 단일 봇에 배분할 것**

---

## Multi-Pair Portfolio (분산 배치)

고급 전략: 2-3개 페어에 분산하여 리스크 분산 + 수익 안정화

```
배분 예시 ($3,000 기준):
  BTC-USDT-SWAP: $1,000 (33%) - 안정적 추세 추종
  ETH-USDT-SWAP: $800  (27%) - 높은 변동성 활용
  SOL-USDT-SWAP: $500  (17%) - 고변동 고수익
  현금 유보:     $700  (23%) - 리밸런싱 + 기회 대비
```
