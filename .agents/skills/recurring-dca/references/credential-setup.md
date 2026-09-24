# OKX Credential Setup Guide

OKX API credentials can be provided via environment variables or configuration files. The system checks sources in priority order.

## Priority Order

1. **Environment variables** (highest priority)
2. **`~/.oktrade.env` file**
3. **`~/.okx/config.toml` file** (lowest priority)

---

## Option 1: Environment Variables

```bash
export OKX_API_KEY="your-api-key"
export OKX_SECRET_KEY="your-secret-key"
export OKX_PASSPHRASE="your-passphrase"
export OKX_SIMULATED="false"  # or "true" for demo mode
```

**Persistent Setup (Bash/Zsh):**

Add to `~/.bashrc` or `~/.zshrc`:

```bash
export OKX_API_KEY="your-api-key"
export OKX_SECRET_KEY="your-secret-key"
export OKX_PASSPHRASE="your-passphrase"
export OKX_SIMULATED="false"
```

Then reload:
```bash
source ~/.bashrc
# or
source ~/.zshrc
```

---

## Option 2: `~/.oktrade.env` File

Create or edit `~/.oktrade.env`:

```bash
OKX_API_KEY="your-api-key"
OKX_SECRET_KEY="your-secret-key"
OKX_PASSPHRASE="your-passphrase"
OKX_SIMULATED="false"
```

**Optional settings:**
- `OKX_SIMULATED="true"` - Enable demo trading mode
- Lines starting with `#` are treated as comments

Example with comments:

```bash
# OKX API Configuration
# Get credentials from https://www.okx.com/account/my-api

OKX_API_KEY="your-api-key"
OKX_SECRET_KEY="your-secret-key"
OKX_PASSPHRASE="your-passphrase"

# Demo mode testing (set to "true" for simulated trading)
OKX_SIMULATED="false"
```

---

## Option 3: `~/.okx/config.toml` File

Create or edit `~/.okx/config.toml`:

```toml
# OKX API Configuration
# Get credentials from https://www.okx.com/account/my-api

[okx]
api_key = "your-api-key"
secret_key = "your-secret-key"
passphrase = "your-passphrase"
simulated = false
```

**Note:** TOML format uses `=` without `export` keyword. Keys can be quoted or unquoted.

---

## Demo Mode (Simulated Trading)

To test without real trades, enable simulated mode:

```bash
# Via environment variable
export OKX_SIMULATED=true

# Via ~/.oktrade.env
OKX_SIMULATED="true"

# Via ~/.okx/config.toml
simulated = true
```

When enabled, the `x-simulated-trading: 1` header is automatically added to all API requests.

---

## Getting Your Credentials

1. Log in to https://www.okx.com/
2. Navigate to **Account** → **API**
3. Click **Create API Key**
4. Set **Key Type** to **Trading**
5. Configure permissions:
   - **Permissions:** Trading (to create/manage DCA strategies)
   - **IP Whitelist:** Add your current IP or leave empty for development
6. Copy your **API Key**, **Secret Key**, and **Passphrase**
7. Store securely (never commit to version control)

---

## Verification

To verify credentials are loaded correctly, run:

```bash
python3 << 'PYEOF'
import os, sys
sys.path.insert(0, os.path.expanduser('${CLAUDE_SKILL_DIR}/scripts'))
from auth_helper import load_credentials

api_key, secret_key, passphrase, simulated = load_credentials()
print(f"API Key loaded: {api_key[:10]}...***")
print(f"Passphrase loaded: {passphrase[:3]}...***")
print(f"Simulated mode: {simulated}")
print("\nCredentials loaded successfully!")
PYEOF
```

---

## Security Best Practices

1. **Never commit credentials** to version control
2. **Use file permissions** to protect config files:
   ```bash
   chmod 600 ~/.oktrade.env
   chmod 700 ~/.okx/
   chmod 600 ~/.okx/config.toml
   ```
3. **IP Whitelist** your API key for production use
4. **Use read-only** permissions if only querying, not trading
5. **Rotate keys periodically** for security
6. **Use demo mode** (`OKX_SIMULATED=true`) for testing

---

## Troubleshooting

| Error | Cause | Solution |
|-------|-------|----------|
| "Credentials not found" | No valid credentials in any source | Set env vars or create config file |
| "Invalid signature" | Wrong secret key | Verify key in OKX dashboard |
| "Invalid passphrase" | Wrong passphrase | Confirm during API key creation |
| "API key does not have permission" | Permissions not set correctly | Grant Trading permissions in OKX dashboard |
| "IP address not in whitelist" | Request from unauthorized IP | Add your IP to whitelist or allow all |

---

## Environment Variable Precedence

When credentials are checked:

```python
1. Check OKX_API_KEY environment variable
2. If not set, check ~/.oktrade.env
3. If not set, check ~/.okx/config.toml
4. If none found, raise error
```

This means environment variables **always override** file-based configuration.
