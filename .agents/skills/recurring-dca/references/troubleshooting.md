# DCA Troubleshooting Guide

Common issues and solutions for the Recurring Buy (DCA) system.

---

## Authentication & Credentials

### Error: "Credentials not found"

**Symptoms:** Script fails immediately with credential error

**Causes:**
1. Missing or empty environment variables
2. Config file not found
3. Config file has syntax errors

**Solutions:**

1. Verify environment variables are set:
   ```bash
   echo $OKX_API_KEY
   echo $OKX_SECRET_KEY
   echo $OKX_PASSPHRASE
   ```

2. If empty, set them:
   ```bash
   export OKX_API_KEY="your-actual-key"
   export OKX_SECRET_KEY="your-actual-secret"
   export OKX_PASSPHRASE="your-actual-passphrase"
   ```

3. Check config file exists and is readable:
   ```bash
   cat ~/.oktrade.env
   # or
   cat ~/.okx/config.toml
   ```

4. Verify file permissions:
   ```bash
   ls -l ~/.oktrade.env
   # Should show -rw------- (600)
   ```

See `references/credential-setup.md` for full setup instructions.

---

### Error: "Invalid signature"

**Symptoms:** HTTP response with "invalid signature" or "authentication failed"

**Causes:**
1. Wrong secret key
2. System clock is skewed (> 5 seconds off)
3. Special characters in passphrase not escaped

**Solutions:**

1. Verify the exact secret key from OKX dashboard:
   ```bash
   # Open https://www.okx.com/account/my-api
   # Click "Show Secret Key" and copy exactly
   export OKX_SECRET_KEY="exact-value-from-dashboard"
   ```

2. Check system time is correct:
   ```bash
   date
   # Should be within 5 seconds of current time
   # If wrong, sync: ntpdate -s time.nist.gov
   ```

3. If passphrase has special characters, check it's not being mangled:
   ```bash
   # Good
   export OKX_PASSPHRASE="MyPass@123"

   # Bad (shell might interpret special chars)
   export OKX_PASSPHRASE=MyPass@123
   ```

---

### Error: "Invalid passphrase"

**Symptoms:** Authentication fails with passphrase error

**Solutions:**

1. Double-check passphrase in OKX dashboard
2. Ensure no leading/trailing spaces:
   ```bash
   export OKX_PASSPHRASE="exact-value"
   #                      ^ no spaces ^
   ```

3. If using config file, ensure quotes are correct:
   ```toml
   # Good
   passphrase = "MyPass@123"

   # Bad (missing quotes)
   passphrase = MyPass@123
   ```

---

## API Errors

### Error: "HTTP 401: Unauthorized"

**Symptoms:** API returns 401 Unauthorized

**Causes:**
1. Invalid or expired API key
2. API key doesn't have Trading Bot permission
3. API key IP whitelist doesn't include your IP

**Solutions:**

1. Verify API key in dashboard and regenerate if needed:
   ```bash
   # https://www.okx.com/account/my-api
   # Delete old key and create new one
   ```

2. Check Trading Bot permission is enabled:
   - Go to API Key Settings
   - Ensure "Trading Bot" permission is checked
   - May need to enable "Sub-account transfer" as well

3. Check IP whitelist:
   - If IP whitelist is configured, add your current IP
   - Or leave empty to allow all IPs (development only)
   - Get your IP: `curl https://api.ipify.org`

---

### Error: "HTTP 429: Too Many Requests"

**Symptoms:** Rate limit error after multiple requests

**Causes:**
1. Making > 10 requests/sec to same endpoint
2. No backoff between retries

**Solutions:**

1. Implement exponential backoff:
   ```python
   import time

   for attempt in range(5):
       try:
           result = okx_recurring_api(method, path, body)
           break
       except SystemExit:
           if attempt < 4:
               wait = 2 ** attempt  # 1, 2, 4, 8, 16 seconds
               print(f"Rate limited. Waiting {wait}s...")
               time.sleep(wait)
           else:
               raise
   ```

2. Batch operations when possible:
   - Don't create 20 strategies in quick succession
   - Space them out by 1-2 seconds minimum

3. Check rate limit in response headers:
   ```python
   # OKX includes rate limit info in response
   # Usually: 10 req/sec per endpoint
   ```

---

### Error: "API Error: insufficient balance"

**Symptoms:** Strategy creation fails with balance error

**Causes:**
1. Account doesn't have enough USDT or investment currency
2. Amount specified is too small (< 10 USDT per coin)
3. Simulated mode disabled but trying to use demo account

**Solutions:**

1. Check account balance:
   ```bash
   okx account balance
   # or check OKX dashboard
   ```

2. Ensure minimum amount:
   ```python
   # Minimum 10 USDT per coin
   # If investing 100 USDT across 2 coins: each gets 50 USDT (OK)
   # If investing 10 USDT across 2 coins: each gets 5 USDT (FAIL)

   coins = "BTC:0.7,ETH:0.3"  # 2 coins
   amount = "50"  # Min 20 USDT total (10 per coin)
   ```

3. For testing, use simulated mode:
   ```bash
   export OKX_SIMULATED=true
   # Test with demo account without real funds
   ```

---

### Error: "API Error: strategy not found"

**Symptoms:** Can't get details or stop strategy that doesn't exist

**Causes:**
1. Wrong algoId
2. Strategy already completed
3. Strategy was deleted

**Solutions:**

1. List all pending strategies:
   ```bash
   python3 << 'PYEOF'
   import os, sys
   sys.path.insert(0, os.path.expanduser('${CLAUDE_SKILL_DIR}/scripts'))
   from auth_helper import okx_recurring_api

   data = okx_recurring_api("GET", "/api/v5/tradingBot/recurring/orders-algo-pending")
   for s in data:
       print(f"ID: {s['algoId']} | Name: {s['stgyName']} | State: {s.get('state', 'unknown')}")
   PYEOF
   ```

2. If strategy was stopped, check history:
   ```bash
   # Use orders-algo-history endpoint to find stopped/completed strategies
   ```

3. Copy the correct algoId and use it

---

## Strategy Execution Issues

### Strategy Not Executing (No Buys Happening)

**Symptoms:** Strategy is active but no orders are placed

**Causes:**
1. Timezone is wrong (buy happens at wrong UTC time)
2. Account is locked for maintenance
3. Trading is disabled on account
4. Insufficient balance when buy time comes

**Solutions:**

1. Verify timezone:
   ```python
   # If UTC+8 and recurringTime=10
   # Buy happens at 10:00 UTC+8 = 02:00 UTC

   # Current time in UTC:
   import datetime
   print(datetime.datetime.now(datetime.timezone.utc))

   # Check when next buy scheduled
   ```

2. Check account status:
   ```bash
   okx account info
   # Ensure account is active (not locked)
   ```

3. Ensure sufficient balance:
   ```bash
   # Check balance before buy time
   okx account balance
   ```

4. Check OKX status page:
   - https://status.okx.com
   - Ensure trading isn't under maintenance

---

### Strategy Executes but Orders Fill at Bad Prices

**Symptoms:** Recurring buys happen but at worse prices than market

**Causes:**
1. Using market orders during high volatility
2. Large order size causes slippage
3. Liquidity is low for the coin/pair

**Solutions:**

1. Check order details:
   ```bash
   python3 << 'PYEOF'
   import os, sys
   sys.path.insert(0, os.path.expanduser('${CLAUDE_SKILL_DIR}/scripts'))
   from auth_helper import okx_recurring_api

   data = okx_recurring_api("GET", "/api/v5/tradingBot/recurring/sub-orders",
                           query={"algoId": "your-algo-id"})
   for order in data:
       print(f"Price: {order['avgPx']} | Size: {order['sz']} | Filled: {order['fillSz']}")
   PYEOF
   ```

2. Reduce order size if slippage is high:
   - Create new strategy with smaller `amt`
   - More frequent smaller orders reduce slippage

3. For better prices, use limit orders instead of market:
   - This requires manual strategy creation
   - Not available through DCA API

---

## Data & Reporting Issues

### Lost algoId / Can't Find Strategy

**Symptoms:** Don't know the strategy ID, need to find it

**Solutions:**

1. List all active strategies:
   ```bash
   python3 << 'PYEOF'
   import os, sys
   sys.path.insert(0, os.path.expanduser('${CLAUDE_SKILL_DIR}/scripts'))
   from auth_helper import okx_recurring_api

   # Pending (active)
   data = okx_recurring_api("GET", "/api/v5/tradingBot/recurring/orders-algo-pending")
   print("ACTIVE STRATEGIES:")
   for s in data:
       print(f"  {s['algoId']}: {s['stgyName']}")

   # Completed/Stopped
   data = okx_recurring_api("GET", "/api/v5/tradingBot/recurring/orders-algo-history")
   print("\nCOMPLETED STRATEGIES:")
   for s in data:
       print(f"  {s['algoId']}: {s['stgyName']} ({s.get('state')})")
   PYEOF
   ```

2. Search by name:
   ```bash
   python3 << 'PYEOF'
   import os, sys
   sys.path.insert(0, os.path.expanduser('${CLAUDE_SKILL_DIR}/scripts'))
   from auth_helper import okx_recurring_api

   target_name = "BTC Daily DCA"

   # Check pending
   data = okx_recurring_api("GET", "/api/v5/tradingBot/recurring/orders-algo-pending")
   matches = [s for s in data if target_name.lower() in s['stgyName'].lower()]

   if matches:
       for m in matches:
           print(f"Found: {m['algoId']}")
   else:
       print("Not found in active strategies. Check history.")
   PYEOF
   ```

---

### Confused About PnL Reporting

**Symptoms:** Not sure how profit/loss is calculated

**Information:**

- `investmentAmt`: Total amount invested so far (sum of buys)
- `totalPnl`: Unrealized profit/loss in investment currency
- `pnlRatio`: Profit/loss as percentage of invested amount

**Example:**
```
Invested: 1000 USDT
Current Value: 1050 USDT
PnL: 50 USDT
PnL Ratio: 0.05 (5%)
```

---

## Debugging & Logging

### Enable Debug Output

Add logging to understand what's happening:

```python
import logging
logging.basicConfig(level=logging.DEBUG)

# Now all HTTP requests will be logged
```

### Save Response for Analysis

```python
import json

result = okx_recurring_api(...)

with open("/tmp/response.json", "w") as f:
    json.dump(result, f, indent=2)

print("Response saved to /tmp/response.json")
```

### Check OKX API Documentation

For detailed error codes and field definitions:
- **API Docs:** https://www.okx.com/docs/api/trading-bot
- **Status Page:** https://status.okx.com
- **Support:** https://www.okx.com/help

---

## Getting Help

### Information to Provide When Asking for Help

1. **Exact error message**
2. **algoId** (if strategy-related)
3. **Recent activity log** (orders executed, amounts, timestamps)
4. **Account info** (timezone, currency, mode: cross/isolated)
5. **Steps to reproduce**

### Resources

- OKX Trading Bot Documentation: https://www.okx.com/docs/api/trading-bot
- OKX Support: support@okx.com
- Check `auth_helper.py` for implementation details
- Check `indicators.py` for technical indicator logic
