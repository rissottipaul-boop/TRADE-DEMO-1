"""
OKX Recurring Buy API authentication and request helper.
Used by all DCA operations.
"""

import os
import json
import hmac
import hashlib
import base64
import urllib.request
import urllib.error
from datetime import datetime, timezone


def load_credentials():
    """Load OKX credentials: env vars > ~/.oktrade.env > ~/.okx/config.toml"""
    api_key = os.environ.get("OKX_API_KEY", "")
    secret_key = os.environ.get("OKX_SECRET_KEY", "")
    passphrase = os.environ.get("OKX_PASSPHRASE", "")
    simulated = os.environ.get("OKX_SIMULATED", "false").lower() == "true"

    if api_key and secret_key and passphrase:
        return api_key, secret_key, passphrase, simulated

    # Try ~/.oktrade.env
    env_path = os.path.expanduser("~/.oktrade.env")
    if os.path.exists(env_path):
        with open(env_path) as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#"):
                    continue
                line = line.removeprefix("export ").strip()
                if "=" in line:
                    k, v = line.split("=", 1)
                    k, v = k.strip(), v.strip().strip('"')
                    if k == "OKX_API_KEY" and not api_key:
                        api_key = v
                    if k == "OKX_SECRET_KEY" and not secret_key:
                        secret_key = v
                    if k == "OKX_PASSPHRASE" and not passphrase:
                        passphrase = v
                    if k == "OKX_SIMULATED" and v.lower() == "true":
                        simulated = True
        if api_key and secret_key and passphrase:
            return api_key, secret_key, passphrase, simulated

    # Try ~/.okx/config.toml
    toml_path = os.path.expanduser("~/.okx/config.toml")
    if os.path.exists(toml_path):
        with open(toml_path) as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#") or line.startswith("["):
                    continue
                if "=" in line:
                    k, v = line.split("=", 1)
                    k, v = k.strip(), v.strip().strip('"').strip("'")
                    if k == "api_key" and not api_key:
                        api_key = v
                    if k == "secret_key" and not secret_key:
                        secret_key = v
                    if k == "passphrase" and not passphrase:
                        passphrase = v
                    if k == "simulated" and v.lower() == "true":
                        simulated = True

    if not all([api_key, secret_key, passphrase]):
        print("ERROR: OKX credentials not found. Set OKX_API_KEY/SECRET_KEY/PASSPHRASE or create ~/.oktrade.env")
        raise SystemExit(1)
    return api_key, secret_key, passphrase, simulated


def okx_recurring_api(method, path, body=None, query=None):
    """Call OKX Recurring Buy API with HMAC-SHA256 signing."""
    api_key, secret_key, passphrase, simulated = load_credentials()

    # Build request URL
    sign_path = path
    url = "https://www.okx.com" + path
    if query:
        qs = "&".join(f"{k}={v}" for k, v in query.items())
        sign_path = f"{path}?{qs}"
        url = f"https://www.okx.com{path}?{qs}"

    body_str = json.dumps(body) if body is not None else ""

    # Timestamp (ISO 8601 with milliseconds)
    now = datetime.now(timezone.utc)
    ts = now.strftime("%Y-%m-%dT%H:%M:%S.") + f"{now.microsecond // 1000:03d}Z"

    # HMAC-SHA256 signature
    prehash = ts + method.upper() + sign_path + body_str
    mac = hmac.new(secret_key.encode(), prehash.encode(), hashlib.sha256)
    signature = base64.b64encode(mac.digest()).decode()

    headers = {
        "OK-ACCESS-KEY": api_key,
        "OK-ACCESS-SIGN": signature,
        "OK-ACCESS-TIMESTAMP": ts,
        "OK-ACCESS-PASSPHRASE": passphrase,
        "Content-Type": "application/json",
        "User-Agent": "okx-dca-cli/1.0",
    }
    if simulated:
        headers["x-simulated-trading"] = "1"

    data = body_str.encode() if body_str else None
    req = urllib.request.Request(url, data=data, headers=headers, method=method.upper())
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            result = json.loads(resp.read().decode())
    except urllib.error.HTTPError as e:
        err_body = e.read().decode() if e.fp else str(e)
        print(f"HTTP {e.code}: {err_body}")
        raise SystemExit(1)
    except Exception as e:
        print(f"Request failed: {e}")
        raise SystemExit(1)

    if result.get("code") != "0":
        print(f"API Error: code={result.get('code')}, msg={result.get('msg')}")
        if result.get("data"):
            for item in result["data"]:
                print(f"  sCode={item.get('sCode')}, sMsg={item.get('sMsg')}")
        raise SystemExit(1)

    return result.get("data", [])
