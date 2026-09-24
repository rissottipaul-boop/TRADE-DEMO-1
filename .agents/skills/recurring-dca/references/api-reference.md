# OKX Recurring Buy API Reference

Complete endpoint documentation for the OKX Trading Bot Recurring Buy API.

---

## Endpoint Summary

| Operation | Method | Endpoint | Purpose |
|-----------|--------|----------|---------|
| Create | POST | `/api/v5/tradingBot/recurring/order-algo` | Create a new DCA strategy |
| List pending | GET | `/api/v5/tradingBot/recurring/orders-algo-pending` | List all active strategies |
| Get details | GET | `/api/v5/tradingBot/recurring/orders-algo-details?algoId=...` | Fetch single strategy |
| List sub-orders | GET | `/api/v5/tradingBot/recurring/sub-orders?algoId=...` | List executed buys |
| History | GET | `/api/v5/tradingBot/recurring/orders-algo-history` | List completed/stopped strategies |
| Stop | POST | `/api/v5/tradingBot/recurring/stop-order-algo` | Stop a strategy (body is array) |
| Amend | POST | `/api/v5/tradingBot/recurring/amend-order-algo` | Update strategy properties |

---

## 1. Create Strategy

**Endpoint:** `POST /api/v5/tradingBot/recurring/order-algo`

### Request Body

```json
{
  "stgyName": "BTC Daily DCA",
  "recurringList": [
    {"ccy": "BTC", "ratio": "1"}
  ],
  "period": "daily",
  "amt": "50",
  "investmentCcy": "USDT",
  "tdMode": "cash",
  "recurringTime": "10",
  "timeZone": "8"
}
```

### Parameters

| Field | Type | Required | Description | Example |
|-------|------|----------|-------------|---------|
| `stgyName` | String | Yes | Strategy name (max 50 chars) | "BTC Daily" |
| `recurringList` | Array | Yes | Coin allocations with ratios | `[{"ccy":"BTC","ratio":"1"}]` |
| `period` | String | Yes | Frequency: hourly/daily/weekly/monthly | "daily" |
| `amt` | String | Yes | Amount per period in investment currency. Min: 10 USDT per coin | "100" |
| `investmentCcy` | String | Yes | Investment currency | "USDT" |
| `tdMode` | String | Yes | Trading mode: `cash` (spot, recommended) / `cross` / `isolated` | "cash" |
| `recurringTime` | String | Yes | Hour (0-23) | "10" |
| `timeZone` | String | Yes | UTC offset as string | "8" (UTC+8) |
| `recurringDay` | String | Conditional | Day 1-7 (Mon-Sun) for weekly or 1-28 for monthly | "1" |
| `recurringHour` | String | Conditional | Interval 1/4/8/12 for hourly | "4" |

### Response

```json
{
  "code": "0",
  "data": [
    {
      "algoId": "123456789",
      "stgyName": "BTC Daily DCA"
    }
  ]
}
```

---

## 2. List Pending Strategies

**Endpoint:** `GET /api/v5/tradingBot/recurring/orders-algo-pending`

### Query Parameters

| Field | Type | Description |
|-------|------|-------------|
| `algoId` | String | Optional: Filter by strategy ID |
| `stgyName` | String | Optional: Filter by strategy name |

### Response

```json
{
  "code": "0",
  "data": [
    {
      "algoId": "123456789",
      "stgyName": "BTC Daily DCA",
      "period": "daily",
      "amt": "50",
      "investmentCcy": "USDT",
      "recurringList": [
        {
          "ccy": "BTC",
          "ratio": "1"
        }
      ],
      "state": "running",
      "investmentAmt": "200",
      "totalPnl": "12.5",
      "pnlRatio": "0.0625",
      "recurringTime": "10",
      "timeZone": "8",
      "nextExecutionTime": "1704067200000"
    }
  ]
}
```

---

## 3. Get Strategy Details

**Endpoint:** `GET /api/v5/tradingBot/recurring/orders-algo-details?algoId=123456789`

### Query Parameters

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `algoId` | String | Yes | Strategy ID to fetch |

### Response Fields

| Field | Description |
|-------|-------------|
| `algoId` | Strategy ID |
| `stgyName` | Strategy name |
| `state` | Current state: running/stopped |
| `period` | Schedule: hourly/daily/weekly/monthly |
| `amt` | Per-period amount |
| `investmentCcy` | Investment currency |
| `recurringList` | Array of coin allocations |
| `investmentAmt` | Total invested so far |
| `totalPnl` | Profit/loss in investment currency |
| `pnlRatio` | Profit/loss ratio |
| `nextExecutionTime` | Timestamp of next buy (milliseconds) |
| `tdMode` | Trading mode |

---

## 4. List Sub-Orders

**Endpoint:** `GET /api/v5/tradingBot/recurring/sub-orders?algoId=123456789`

Returns individual buy orders executed by a DCA strategy.

### Query Parameters

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `algoId` | String | Yes | Strategy ID |
| `ordId` | String | Optional | Filter by order ID |
| `limit` | Integer | Optional | Max results (default 100) |

### Response

```json
{
  "code": "0",
  "data": [
    {
      "ordId": "987654321",
      "algoId": "123456789",
      "instId": "BTC-USDT",
      "ordType": "market",
      "sz": "0.001",
      "fillSz": "0.001",
      "avgPx": "45000",
      "cTime": "1704067200000",
      "state": "filled",
      "pnl": "100.5"
    }
  ]
}
```

---

## 5. History

**Endpoint:** `GET /api/v5/tradingBot/recurring/orders-algo-history`

Lists completed and stopped strategies with their final performance.

### Query Parameters

| Field | Type | Description |
|-------|------|-------------|
| `algoId` | String | Optional: Filter by ID |
| `stgyName` | String | Optional: Filter by name |
| `state` | String | Optional: completed/stopped |

### Response

Similar to pending list, but includes historical strategies with `state: "completed"` or `state: "stopped"`.

---

## 6. Stop Strategy

**Endpoint:** `POST /api/v5/tradingBot/recurring/stop-order-algo`

### Request Body

**IMPORTANT:** Body must be a JSON **array**, not an object.

```json
[
  {"algoId": "123456789"}
]
```

### Multiple Stop

To stop multiple strategies:

```json
[
  {"algoId": "123456789"},
  {"algoId": "987654321"},
  {"algoId": "456789123"}
]
```

### Response

```json
{
  "code": "0",
  "data": [
    {
      "algoId": "123456789",
      "stgyName": "BTC Daily DCA",
      "state": "stopped"
    }
  ]
}
```

---

## 7. Amend Strategy

**Endpoint:** `POST /api/v5/tradingBot/recurring/amend-order-algo`

### Request Body

```json
{
  "algoId": "123456789",
  "stgyName": "Updated Strategy Name"
}
```

### Amendable Fields

| Field | Description |
|-------|-------------|
| `stgyName` | Update strategy name |
| (Limited to name currently) | Other fields cannot be amended |

### Response

```json
{
  "code": "0",
  "data": [
    {
      "algoId": "123456789",
      "stgyName": "Updated Strategy Name"
    }
  ]
}
```

---

## Error Codes

| Code | Message | Solution |
|------|---------|----------|
| 0 | Success | None |
| 50001 | Account does not have trading bot permission | Contact OKX support |
| 50002 | Insufficient balance | Add funds to account |
| 50003 | Order already exists | Use different strategy name |
| 50004 | Invalid parameter | Check request parameters |
| 50005 | Strategy not found | Verify algoId exists |
| 50006 | Strategy already stopped | Cannot stop twice |
| 50007 | Invalid recurring time | Check time/timezone format |

---

## Rate Limits

- **Default:** ~10 requests per second per endpoint
- **Burst:** Up to 20 requests per second
- **Rate limit headers:** Check response headers for remaining quota

---

## Authentication

All requests require:

```
OK-ACCESS-KEY: {api-key}
OK-ACCESS-SIGN: {hmac-sha256-signature}
OK-ACCESS-TIMESTAMP: {iso-8601-timestamp}
OK-ACCESS-PASSPHRASE: {passphrase}
Content-Type: application/json
```

See `auth_helper.py` for implementation.

---

## Simulated Trading

To use demo/simulated mode, add header to all requests:

```
x-simulated-trading: 1
```

This is automatically added when `OKX_SIMULATED=true` environment variable is set.

---

## Timeouts and Retries

- **Connection timeout:** 30 seconds
- **Read timeout:** 30 seconds
- **Retry strategy:** Exponential backoff for rate limits (429)

---

## Timestamps

All timestamps in responses are **milliseconds since epoch** (Unix timestamp * 1000).

Convert to readable format:

```python
from datetime import datetime, timezone

timestamp_ms = 1704067200000
dt = datetime.fromtimestamp(timestamp_ms / 1000, tz=timezone.utc)
print(dt)  # 2024-01-01 10:00:00+00:00
```
