---
name: congress-trades
description: >
  Use this skill when the user asks about stock trades made by US
  politicians, members of Congress, senators, or representatives.
  Covers any query about what specific politicians (like Nancy Pelosi,
  Dan Crenshaw, etc.) are buying or selling, whether politicians are
  trading on insider information, recent congressional trading
  activity, or tracking political stock disclosures. Also use for
  setting up alerts on new politician trades or syncing congressional
  trade data. Do NOT use for general stock market analysis, crypto, or
  trading strategies unrelated to politician activity.
---

# Congress Trades Tracker

Monitor US congressional stock trades via the Quiver Quant API. Syncs data to a local SQLite database and alerts on new significant trades.

## Requirements

- Python 3.10+ with `requests` (`pip install requests`)
- **QUIVER_API_KEY** environment variable (obtain at https://www.quiverquant.com/)

## Environment Variables

| Variable | Required | Default | Description |
|---|---|---|---|
| QUIVER_API_KEY | **Yes** | -- | Quiver Quant API token |
| CONGRESS_DB_PATH | No | `data/congress_trades.db` | SQLite database path |
| MIN_TRADE_AMOUNT | No | `15001` | Minimum trade dollar amount to trigger alerts |

Never hard-code API keys in scripts or crontab entries. Use shell profile, `.env` file, or secure environment injection.

## Execution Flow

When the user activates this skill, follow the steps below based on what they need.

### 1. First-Time Setup

If the user has not set up the tracker yet, guide them through:

1. **Check Python**: Verify Python 3.10+ is available.
2. **Install dependency**: `pip install requests`
3. **Set API key**: Ask the user for their Quiver Quant API key. Instruct them to set it:
   ```bash
   export QUIVER_API_KEY="<their-key>"
   ```
4. **Run initial sync**: Execute the scraper to populate the database:
   ```bash
   python3 scripts/scraper.py
   ```
5. **Confirm success**: Check that `data/congress_trades.db` was created and report the number of trades synced.
6. **Optional -- schedule cron**: Help the user set up a cron job for automatic syncing:
   ```bash
   crontab -e
   # Add (adjust paths). Every 30 min is sufficient — data updates once daily at most:
   */30 * * * * . "$HOME/.profile" && /usr/bin/python3 /path/to/scripts/scraper.py >> /path/to/logs/scraper.log 2>&1
   ```

### 2. Manual Sync

If the user asks to sync, refresh, or update trades:

1. Run the scraper: `python3 scripts/scraper.py`
2. Report the result: number of new trades found, total in database.

### 3. Query Trades

When the user wants to look up trades, use the SQLite database directly. The database is at the path defined by `CONGRESS_DB_PATH` (default: `data/congress_trades.db`).

**Database schema** (table `trades`):

| Column | Type | Description |
|---|---|---|
| representative | TEXT | Politician name |
| party | TEXT | D, R, or I |
| house | TEXT | House or Senate |
| ticker | TEXT | Stock ticker symbol |
| transaction_type | TEXT | Purchase, Sale, etc. |
| transaction_date | TEXT | Date of trade (YYYY-MM-DD) |
| report_date | TEXT | Date reported (YYYY-MM-DD) |
| range_amount | TEXT | Dollar range (e.g., "$1,001 - $15,000") |
| description | TEXT | Additional details |

#### Query by Politician

```sql
SELECT representative, ticker, transaction_type, range_amount, transaction_date
FROM trades
WHERE representative LIKE '%Pelosi%'
ORDER BY transaction_date DESC
LIMIT 20;
```

#### Query by Ticker

```sql
SELECT representative, party, transaction_type, range_amount, transaction_date
FROM trades
WHERE ticker = 'NVDA'
ORDER BY transaction_date DESC
LIMIT 20;
```

#### Query by Party

```sql
SELECT representative, ticker, transaction_type, range_amount, transaction_date
FROM trades
WHERE party = 'D'
ORDER BY transaction_date DESC
LIMIT 20;
```

#### Query by Date Range

```sql
SELECT representative, ticker, transaction_type, range_amount, transaction_date
FROM trades
WHERE transaction_date BETWEEN '2026-01-01' AND '2026-03-31'
ORDER BY transaction_date DESC;
```

#### Query by Transaction Type

```sql
SELECT representative, ticker, range_amount, transaction_date
FROM trades
WHERE transaction_type = 'Purchase'
ORDER BY transaction_date DESC
LIMIT 20;
```

#### Combined Query (e.g., Republican purchases in a date range)

```sql
SELECT representative, ticker, range_amount, transaction_date
FROM trades
WHERE party = 'R'
  AND transaction_type = 'Purchase'
  AND transaction_date >= '2026-01-01'
ORDER BY transaction_date DESC;
```

#### Summary Statistics

```sql
-- Most traded tickers
SELECT ticker, COUNT(*) as trade_count
FROM trades
GROUP BY ticker
ORDER BY trade_count DESC
LIMIT 10;

-- Most active politicians
SELECT representative, party, COUNT(*) as trade_count
FROM trades
GROUP BY representative
ORDER BY trade_count DESC
LIMIT 10;

-- Trades by party breakdown
SELECT party, transaction_type, COUNT(*) as count
FROM trades
GROUP BY party, transaction_type
ORDER BY party, count DESC;
```

### 4. Check Alerts

If the user asks about recent alerts or significant trades:

1. Check `data/pending_congress_alert.txt` for the latest undelivered alert.
2. Check `data/new_trades.json` for the last 50 alert records.
3. Present the alert content to the user.
4. Ask the user if they want to clear the alert. If yes, delete `data/pending_congress_alert.txt` to prevent re-alerting.

### 5. Change Configuration

If the user wants to adjust settings:

- **Threshold**: `export MIN_TRADE_AMOUNT=50001` (or any dollar amount)
- **Database path**: `export CONGRESS_DB_PATH=/custom/path/trades.db`
- **Cron frequency**: Edit crontab to change interval (e.g., `*/15` for every 15 minutes)

## Output Templates

### Trade List Output

When displaying trade results, use this format:

```
Congress Trades: [query description]
Found [N] trades.

| Politician | Party | Ticker | Type | Amount Range | Trade Date |
|---|---|---|---|---|---|
| Nancy Pelosi | D | NVDA | Purchase | $1,000,001 - $5,000,000 | 2026-02-10 |
| Dan Crenshaw | R | MSFT | Sale | $15,001 - $50,000 | 2026-02-09 |
```

### Alert Output

```
New Congress Trade Alert -- [N] significant trade(s) detected:

[green] PURCHASE: Nancy Pelosi (D) [Rep]
   $NVDA -- $1,000,001 - $5,000,000
   Trade: 2026-02-10 | Reported: 2026-02-14

[red] SALE: Dan Crenshaw (R) [Rep]
   $MSFT -- $15,001 - $50,000
   Trade: 2026-02-09 | Reported: 2026-02-14
```

### Summary Statistics Output

```
Congress Trades Summary:
- Total trades in database: [N]
- Date range: [earliest] to [latest]
- Most traded ticker: [TICKER] ([count] trades)
- Most active politician: [Name] ([count] trades)
- Party breakdown: D: [n], R: [n], I: [n]
```

### Setup Confirmation Output

```
Congress Trades Tracker -- Setup Complete
- Database: [path] ([N] trades synced)
- API: Quiver Quant connected
- Threshold: $[amount] minimum for alerts
- Cron: [status]
```

## Error Handling

When issues occur, diagnose and guide the user:

| Error | Cause | Resolution |
|---|---|---|
| `QUIVER_API_KEY environment variable is required` | API key not set | Ask user to set `export QUIVER_API_KEY="..."` |
| `401 Unauthorized` | Invalid or expired API key | Ask user to verify key at quiverquant.com |
| `403 Forbidden` | API plan does not include congress data | Suggest upgrading Quiver Quant plan |
| `Connection error` / `timeout` | Network issue or API down | Retry in a few minutes; check internet |
| `No trades returned` | API returned empty response | May be temporary; data updates on reporting schedule |
| Database locked | Concurrent access to SQLite | Ensure only one scraper instance runs at a time |
| `ModuleNotFoundError: requests` | Missing dependency | Run `pip install requests` |

## Security Notes

- API key is read from environment variable only — never store it in scripts or crontab
- Restrict file permissions on data directory: `chmod 700 data/`
- Only outbound connection: `api.quiverquant.com` (HTTPS)
