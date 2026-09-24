#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
OKX Maker Entry Skill - CLI

Passive post-only entry for OKX USDT perpetuals. Places maker limit orders
across the top 40 book levels, reclaims tail orders into nearer gaps as price
moves, and exits when the target notional is filled.

Commands:
    init      Create ~/.okx/config.toml template.
    doctor    Verify credentials, one-way mode, and symbol tradability.
    preview   Feasibility + sample order precheck.
    start     Spawn a detached background runner.
    status    Read current task status.
    stop      Signal the background runner to cancel all orders and exit.
    run       (internal) Background runner entry.

All state is persisted as JSON under the skill state directory:
    Windows : %LOCALAPPDATA%\\okx-maker-entry-skill\\
    Unix    : ~/.local/state/okx-maker-entry-skill/

Dependencies:
    - Python 3.10+ (uses tomllib on 3.11+, tomli fallback otherwise)
    - ccxt (pip install ccxt)
"""

from __future__ import annotations

import argparse
import copy
import json
import math
import os
import re
import subprocess
import sys
import threading
import time
import traceback
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Optional

# ---------------------------------------------------------------------------
# Optional third-party imports
# ---------------------------------------------------------------------------
try:
    import tomllib  # Python 3.11+
except ImportError:  # pragma: no cover
    try:
        import tomli as tomllib  # type: ignore
    except ImportError:  # pragma: no cover
        tomllib = None  # type: ignore

try:
    import ccxt  # type: ignore
except ImportError:  # pragma: no cover
    ccxt = None  # type: ignore


# ===========================================================================
# Constants
# ===========================================================================
BAND_LEVELS = 40
DECISION_INTERVAL_MS = 500
ORDER_RECONCILE_INTERVAL_MS = 1000
DEPTH_LEVELS = 100
RECLAIM_DELAY_MS = 10_000
MAX_RECLAIM_OPEN_RATIO = 0.2
TAIL_REPRICE_DEFAULT_DELAY_MS = 15_000
TAIL_REPRICE_FAST_DELAY_MS = 10_000
SHAPE_MULTIPLIER = 3.0
LOCAL_REF_RATIO = 0.4
HEARTBEAT_INTERVAL_MS = 5_000
CONTROL_POLL_INTERVAL_MS = 100
STOP_FLAG_POLL_MS = 250
MARKET_DATA_RETRY_GRACE_MS = 5_000
POSITION_FETCH_MAX_CONSECUTIVE_FAILURES = 3
LARGE_NOTIONAL_WARNING_USDT = 10_000

REFRESH_INTERVAL_MS = DECISION_INTERVAL_MS
RECONCILE_INTERVAL_MS = ORDER_RECONCILE_INTERVAL_MS
RECLAIM_AFTER_SECONDS = RECLAIM_DELAY_MS // 1000

ORDER_SYNC_PAUSE_MESSAGE = (
    'Active exchange orders could not be reconciled. Task paused to avoid '
    'placing more orders. Check live orders and position before restarting.'
)
POSITION_FETCH_PAUSE_MESSAGE = (
    'Position data could not be fetched after multiple attempts. Task paused '
    'to avoid trading with stale position data.'
)
MARKET_DATA_RETRY_MESSAGE = (
    'Real-time market data is temporarily unavailable from REST polling. '
    'Retrying before pausing.'
)
MARKET_DATA_UNAVAILABLE_PAUSE_MESSAGE = (
    'Real-time market data remained unavailable from REST polling. Task '
    'paused to avoid trading on stale or missing order book data.'
)

ACTIVE_PHASES = {'starting', 'preflight', 'working', 'reclaiming'}
TERMINAL_PHASES = {'completed', 'stopped', 'failed', 'paused', 'stopped_unexpectedly'}


# ===========================================================================
# Utility helpers
# ===========================================================================
def now_iso() -> str:
    return datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%S.') + \
           f'{datetime.now(timezone.utc).microsecond // 1000:03d}Z'


def now_ms() -> int:
    return int(time.time() * 1000)


def to_number(value: Any, fallback: float = 0.0) -> float:
    try:
        result = float(value)
        if math.isfinite(result):
            return result
        return fallback
    except (TypeError, ValueError):
        return fallback


def to_nullable_number(value: Any) -> Optional[float]:
    try:
        result = float(value)
        if math.isfinite(result):
            return result
        return None
    except (TypeError, ValueError):
        return None


def normalize_precision_step(value: Any) -> float:
    try:
        numeric = float(value)
    except (TypeError, ValueError):
        return 0.0
    if not math.isfinite(numeric) or numeric <= 0:
        return 0.0
    if numeric >= 1 and numeric == int(numeric):
        return 10 ** -int(numeric)
    return numeric


def compute_spread_bps(best_bid: float, best_ask: float) -> Optional[float]:
    if not (math.isfinite(best_bid) and math.isfinite(best_ask)):
        return None
    if best_bid <= 0 or best_ask <= 0 or best_ask < best_bid:
        return None
    mid = (best_bid + best_ask) / 2
    if mid <= 0:
        return None
    return ((best_ask - best_bid) / mid) * 10_000


def compute_completion_ratio(filled: float, requested: float) -> float:
    if not math.isfinite(requested) or requested <= 0:
        return 1.0 if filled <= 0 else 0.0
    return min(1.0, max(0.0, filled / requested))


def describe_position_side(signed_contracts: float) -> str:
    if signed_contracts > 0:
        return 'LONG'
    if signed_contracts < 0:
        return 'SHORT'
    return 'FLAT'


def entry_side_to_order_side(side: str) -> str:
    return 'buy' if side == 'LONG' else 'sell'


def position_conflicts_with_entry(position_side: str, entry_side: str) -> bool:
    return (
        (position_side == 'LONG' and entry_side == 'SHORT')
        or (position_side == 'SHORT' and entry_side == 'LONG')
    )


def compact_market_symbol(market: Optional[dict]) -> str:
    if not market:
        return ''
    mid = (market.get('id') or '').strip().upper()
    if mid:
        return mid
    base = (market.get('base') or '').upper()
    quote = (market.get('quote') or '').upper()
    return '-'.join(p for p in (base, quote) if p)


def ensure_parent_dir(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)


def read_json_file(path: Path) -> Optional[Any]:
    try:
        with open(path, 'r', encoding='utf-8') as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return None


def write_json_file(path: Path, value: Any) -> None:
    ensure_parent_dir(path)
    with open(path, 'w', encoding='utf-8') as f:
        json.dump(value, f, indent=2, default=str)


def print_json(value: Any) -> None:
    sys.stdout.write(json.dumps(value, indent=2, default=str) + '\n')
    sys.stdout.flush()


def is_pid_running(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        if os.name == 'nt':
            import ctypes
            PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
            STILL_ACTIVE = 259
            handle = ctypes.windll.kernel32.OpenProcess(
                PROCESS_QUERY_LIMITED_INFORMATION, False, pid
            )
            if not handle:
                return False
            try:
                exit_code = ctypes.c_ulong()
                ok = ctypes.windll.kernel32.GetExitCodeProcess(handle, ctypes.byref(exit_code))
                if not ok:
                    return False
                return exit_code.value == STILL_ACTIVE
            finally:
                ctypes.windll.kernel32.CloseHandle(handle)
        else:
            os.kill(pid, 0)
            return True
    except (ProcessLookupError, PermissionError):
        return False
    except OSError:
        return False


# ===========================================================================
# OKX profile / config.toml
# ===========================================================================
CONFIG_TEMPLATE = """# OKX API Configuration
# Create API keys at https://www.okx.com/account/my-api
# Required permissions: Read + Trade
# IMPORTANT: Set position mode to "Net mode" (one-way) in OKX account settings.

[profiles.demo]
api_key = ""
secret_key = ""
passphrase = ""
demo = true
# site: "global" (default), "eea", or "us"
# site = "global"
# proxy_url = ""

[profiles.live]
api_key = ""
secret_key = ""
passphrase = ""
demo = false
# site = "global"
# proxy_url = ""
"""


def get_okx_config_path() -> Path:
    return Path.home() / '.okx' / 'config.toml'


def normalize_profile_name(value: str) -> str:
    normalized = (value or '').strip().lower()
    if normalized not in ('demo', 'live'):
        raise ValueError(f'Unsupported profile "{value}". Use demo or live.')
    return normalized


def read_okx_config(config_path: Optional[Path] = None) -> dict:
    path = config_path or get_okx_config_path()
    if tomllib is None:
        raise RuntimeError(
            'TOML support is unavailable. Use Python 3.11+ or install tomli '
            '(pip install tomli).'
        )
    try:
        with open(path, 'rb') as f:
            return tomllib.load(f)
    except FileNotFoundError:
        raise RuntimeError(
            f'Unable to read OKX config at {path}: file not found.\n'
            f'Run "python okx_maker_entry.py init" to create a config template, '
            f'then fill in your OKX API credentials.'
        )
    except Exception as error:
        raise RuntimeError(f'Unable to read OKX config at {path}: {error}')


def load_okx_profile(profile_name: str, config_path: Optional[Path] = None) -> dict:
    path = config_path or get_okx_config_path()
    document = read_okx_config(path)
    profiles = document.get('profiles') or {}
    raw = profiles.get(profile_name)
    if not raw:
        raise RuntimeError(
            f'Profile "{profile_name}" was not found in {path}. '
            f'Add a [profiles.{profile_name}] section with api_key, secret_key, and passphrase.'
        )
    api_key = (raw.get('api_key') or '').strip()
    secret_key = (raw.get('secret_key') or '').strip()
    passphrase = (raw.get('passphrase') or '').strip()
    if not api_key or not secret_key or not passphrase:
        raise RuntimeError(
            f'Profile "{profile_name}" in {path} is missing api_key, secret_key, or passphrase. '
            f'Create API keys at OKX: Profile -> API -> Create API Key (permissions: Read + Trade).'
        )
    site_raw = (raw.get('site') or '').strip().lower()
    site = site_raw if site_raw in ('eea', 'us') else 'global'
    proxy_url = (raw.get('proxy_url') or '').strip() or None
    return {
        'config_path': str(path),
        'default_profile': document.get('default_profile'),
        'environment': profile_name,
        'api_key': api_key,
        'secret_key': secret_key,
        'passphrase': passphrase,
        'demo': bool(raw.get('demo')),
        'site': site,
        'proxy_url': proxy_url,
    }


# ===========================================================================
# State store
# ===========================================================================
def get_default_state_dir() -> Path:
    if os.name == 'nt':
        base = os.environ.get('LOCALAPPDATA', '').strip()
        if base:
            return Path(base) / 'okx-maker-entry-skill'
        return Path.home() / 'AppData' / 'Local' / 'okx-maker-entry-skill'
    xdg = os.environ.get('XDG_STATE_HOME', '').strip()
    if xdg:
        return Path(xdg) / 'okx-maker-entry-skill'
    return Path.home() / '.local' / 'state' / 'okx-maker-entry-skill'


class StateStore:
    def __init__(self, base_dir: Optional[Path] = None) -> None:
        self.state_dir = base_dir or get_default_state_dir()
        self.runner_pid_file = self.state_dir / 'runner.pid'
        self.task_file = self.state_dir / 'task.json'
        self.status_file = self.state_dir / 'status.json'
        self.stop_flag_file = self.state_dir / 'stop.flag'

    def ensure_dir(self) -> None:
        self.state_dir.mkdir(parents=True, exist_ok=True)

    def read_job(self) -> Optional[dict]:
        return read_json_file(self.task_file)

    def write_job(self, job: dict) -> None:
        write_json_file(self.task_file, job)

    def read_status(self) -> Optional[dict]:
        return read_json_file(self.status_file)

    def write_status(self, status: dict) -> None:
        write_json_file(self.status_file, status)

    def read_runner_pid(self) -> Optional[int]:
        try:
            raw = self.runner_pid_file.read_text(encoding='utf-8').strip()
            pid = int(raw)
            return pid if pid > 0 else None
        except (FileNotFoundError, ValueError):
            return None

    def write_runner_pid(self, pid: Optional[int]) -> None:
        self.ensure_dir()
        if pid is None:
            try:
                self.runner_pid_file.unlink()
            except FileNotFoundError:
                pass
            return
        self.runner_pid_file.write_text(str(pid), encoding='utf-8')

    def has_stop_flag(self) -> bool:
        return self.stop_flag_file.exists()

    def create_stop_flag(self) -> None:
        self.ensure_dir()
        self.stop_flag_file.write_text('', encoding='utf-8')

    def clear_stop_flag(self) -> None:
        try:
            self.stop_flag_file.unlink()
        except FileNotFoundError:
            pass

    def read_normalized_status(self) -> Optional[dict]:
        status = self.read_status()
        if not status:
            return None
        pid = status.get('pid') or self.read_runner_pid()
        if pid and status.get('phase') in ACTIVE_PHASES and not is_pid_running(int(pid)):
            return self.mark_unexpected_stop(status, pid)
        return status

    def reject_if_active_task_exists(self) -> None:
        status = self.read_normalized_status()
        if status and status.get('phase') in ACTIVE_PHASES:
            raise RuntimeError(
                f'Another Maker Entry task is already active: '
                f'{status.get("taskId") or "unknown task"}.'
            )

    def mark_unexpected_stop(self, status: dict, pid: Optional[int]) -> dict:
        warnings = list(status.get('warnings') or [])
        if 'Runner exited unexpectedly.' not in warnings:
            warnings.append('Runner exited unexpectedly.')
        next_status = dict(status)
        next_status.update({
            'phase': 'stopped_unexpectedly',
            'currentAction': 'Runner exited unexpectedly before the task reported completion.',
            'warnings': warnings,
            'updatedAt': now_iso(),
            'pid': pid,
        })
        self.write_status(next_status)

        job = self.read_job()
        if job and job.get('snapshot', {}).get('status') in ACTIVE_PHASES:
            failed = copy.deepcopy(job)
            failed['status'] = 'failed'
            failed['updatedAt'] = next_status['updatedAt']
            snap = failed['snapshot']
            snap['status'] = 'failed'
            snap['currentAction'] = next_status['currentAction']
            snap['warnings'] = warnings
            snap['activeWorkingOrders'] = 0
            snap['openWorkingNotionalUsdt'] = 0
            snap['reclaiming'] = False
            snap['pauseReason'] = None
            self.write_job(failed)

        self.write_runner_pid(None)
        return next_status


# ===========================================================================
# OKX trading adapter (wraps ccxt)
# ===========================================================================
REFERENCE_SYMBOL = 'BTC/USDT:USDT'


def _is_supported_swap_market(market: Optional[dict]) -> bool:
    if not market:
        return False
    return bool(
        market.get('swap') is True
        and market.get('linear') is True
        and market.get('quote') == 'USDT'
        and market.get('settle') == 'USDT'
    )


def _parse_margin_mode(value: Any) -> str:
    normalized = str(value or '').lower()
    if normalized == 'cross':
        return 'cross'
    if normalized == 'isolated':
        return 'isolated'
    return 'unknown'


def _site_to_hostname(site: str) -> str:
    if site == 'eea':
        return 'my.okx.com'
    if site == 'us':
        return 'app.okx.com'
    return 'www.okx.com'


def _okx_status_is_terminal(status: str) -> bool:
    normalized = (status or '').lower()
    return normalized in ('closed', 'canceled')


class OkxTradingAdapter:
    """Thin wrapper over ccxt.okx that normalizes contracts-vs-base units,
    tracks one-way mode detection, and surfaces order precheck results."""

    def __init__(self, credentials: dict) -> None:
        if ccxt is None:
            raise RuntimeError(
                f'The ccxt package is required. Install it with: '
                f'"{sys.executable}" -m pip install ccxt'
            )
        self.environment = credentials['environment']
        self.client = ccxt.okx({
            'apiKey': credentials['api_key'],
            'secret': credentials['secret_key'],
            'password': credentials['passphrase'],
            'enableRateLimit': True,
            'timeout': 15000,
        })
        self.client.options.update({
            'defaultType': 'swap',
            'defaultSubType': 'linear',
            'defaultMarginMode': 'cross',
            'timeDifference': 0,
            'adjustForTimeDifference': True,
        })
        self.client.hostname = _site_to_hostname(credentials.get('site') or 'global')
        if credentials.get('proxy_url'):
            self.client.proxy_url = credentials['proxy_url']
        if credentials.get('demo'):
            if hasattr(self.client, 'set_sandbox_mode'):
                self.client.set_sandbox_mode(True)
        self._markets_loaded = False
        self._account_config_entry: Optional[dict] = None
        self._has_synced_time_difference = False

    # -- connection / metadata ------------------------------------------------
    def ensure_markets_loaded(self) -> None:
        if self._markets_loaded:
            return
        self._with_timestamp_retry(self.client.load_markets)
        self._markets_loaded = True

    def close(self) -> None:
        try:
            if hasattr(self.client, 'close'):
                self.client.close()
        except Exception:
            pass

    def test_connection(self) -> dict:
        try:
            self.ensure_markets_loaded()
            server_time_offset_ms: Optional[int] = None
            try:
                if hasattr(self.client, 'fetch_time'):
                    server_time = self.client.fetch_time()
                    if isinstance(server_time, (int, float)):
                        server_time_offset_ms = int(server_time - now_ms())
            except Exception:
                server_time_offset_ms = None

            config_entry = self._get_account_config_entry()
            pos_mode = str(config_entry.get('posMode') or '')
            acct_lv = str(config_entry.get('acctLv') or '')
            one_way_mode = pos_mode != 'long_short_mode'
            can_trade_futures = acct_lv != '1'
            leverage = self._get_reference_leverage()
            margin_mode = self._get_reference_margin_mode()

            self._with_timestamp_retry(
                lambda: self.client.fetch_balance({'type': 'swap'})
            )

            if not can_trade_futures:
                message = (
                    'OKX API credentials are valid, but the account is in Spot mode '
                    'and cannot trade swaps.'
                )
            elif not one_way_mode:
                message = (
                    'OKX API credentials are valid, but the account is not in one-way '
                    'net position mode.'
                )
            else:
                env_label = 'OKX demo' if self.environment == 'demo' else 'OKX live'
                if self._should_skip_order_precheck(config_entry):
                    message = (
                        f'{env_label} connection check passed. Preview will use a '
                        f'compatibility fallback because OKX Futures mode does not '
                        f'support order precheck.'
                    )
                else:
                    message = f'{env_label} connection check passed.'

            return {
                'exchange': 'okx',
                'environment': self.environment,
                'oneWayMode': one_way_mode,
                'canRead': True,
                'canTrade': True,
                'canTradeFutures': can_trade_futures,
                'serverTimeOffsetMs': server_time_offset_ms,
                'marginMode': margin_mode,
                'leverage': leverage,
                'message': message,
            }
        except Exception as error:
            raise self._normalize_error(error, 'OKX connection check failed')

    def normalize_symbol(self, symbol: str) -> str:
        self.ensure_markets_loaded()
        trimmed = (symbol or '').strip()
        if not trimmed:
            raise ValueError('Provide an instrument ID such as BTC-USDT-SWAP.')
        market = None
        try:
            market = self.client.market(trimmed)
        except Exception:
            market = None
        if market is None:
            upper = trimmed.upper()
            markets_by_id = getattr(self.client, 'markets_by_id', None) or {}
            markets = getattr(self.client, 'markets', None) or {}
            raw = markets_by_id.get(upper)
            candidate_symbol = None
            if isinstance(raw, dict):
                candidate_symbol = raw.get('symbol')
            elif isinstance(raw, list) and raw:
                candidate_symbol = raw[0].get('symbol') if isinstance(raw[0], dict) else None
            if candidate_symbol:
                market = markets.get(candidate_symbol)
            elif '/' not in upper and upper.endswith('USDTSWAP'):
                base = upper[:-8]
                market = markets.get(f'{base}/USDT:USDT')
            elif '-' not in upper and upper.endswith('USDT'):
                base = upper[:-4]
                raw2 = markets_by_id.get(f'{base}-USDT-SWAP')
                candidate2 = None
                if isinstance(raw2, dict):
                    candidate2 = raw2.get('symbol')
                elif isinstance(raw2, list) and raw2:
                    candidate2 = raw2[0].get('symbol') if isinstance(raw2[0], dict) else None
                if candidate2:
                    market = markets.get(candidate2)
        if not _is_supported_swap_market(market):
            raise ValueError(
                f'Unsupported OKX instrument: {symbol}. Only USDT perpetual swaps are supported.'
            )
        return market['symbol']

    def get_symbol_trading_rules(self, symbol: str) -> dict:
        normalized = self.normalize_symbol(symbol)
        market = self._require_market(normalized)
        contract_size = self._get_contract_size(market)
        min_exchange_amount = to_number((market.get('limits') or {}).get('amount', {}).get('min'), 0)
        min_amount = min_exchange_amount * contract_size
        amount_step = normalize_precision_step(
            (market.get('precision') or {}).get('amount')
        ) * contract_size
        min_notional = max(0.0, to_number((market.get('limits') or {}).get('cost', {}).get('min'), 0))
        price_tick = max(normalize_precision_step((market.get('precision') or {}).get('price')), 0.0)
        return {
            'symbol': normalized,
            'minAmount': min_amount,
            'amountStep': amount_step,
            'minNotional': min_notional,
            'priceTick': price_tick,
            'contractSize': contract_size,
        }

    # -- positions / balance --------------------------------------------------
    def fetch_position_snapshot(self, symbol: str) -> dict:
        try:
            normalized = self.normalize_symbol(symbol)
            market = self._require_market(normalized)
            position = None
            try:
                if hasattr(self.client, 'fetch_position'):
                    position = self._with_timestamp_retry(
                        lambda: self.client.fetch_position(normalized)
                    )
            except Exception:
                position = None
            if position is None:
                try:
                    positions = self._with_timestamp_retry(
                        lambda: self.client.fetch_positions([normalized])
                    ) or []
                    position = positions[0] if positions else None
                except Exception:
                    position = None

            if not position:
                book = self.fetch_book_ticker(normalized)
                return {
                    'symbol': normalized,
                    'markPrice': book['mark'],
                    'signedContracts': 0.0,
                    'signedNotionalUsdt': 0.0,
                    'absNotionalUsdt': 0.0,
                    'side': 'FLAT',
                    'leverage': self._get_reference_leverage() or 1.0,
                    'marginMode': self._get_reference_margin_mode(),
                    'unrealizedPnlUsdt': 0.0,
                }

            snapshot = self._map_position_snapshot(
                position,
                market,
                self._get_reference_leverage() or 1.0,
                self._get_reference_margin_mode(),
            )
            # Fallback: OKX sometimes returns an empty markPx for flat or newly
            # opened instruments. Use mid-book as a sane default so downstream
            # consumers don't see markPrice=0.
            if not snapshot.get('markPrice'):
                try:
                    book = self.fetch_book_ticker(normalized)
                    fallback_mark = book.get('mark') or 0
                    if fallback_mark:
                        snapshot['markPrice'] = fallback_mark
                        if snapshot.get('signedContracts'):
                            snapshot['signedNotionalUsdt'] = (
                                snapshot['signedContracts'] * fallback_mark
                            )
                            snapshot['absNotionalUsdt'] = abs(
                                snapshot['signedNotionalUsdt']
                            )
                except Exception:
                    pass
            return snapshot
        except Exception as error:
            raise self._normalize_error(error, 'Failed to fetch current position')

    def fetch_account_funding_snapshot(self) -> dict:
        try:
            balance = self._with_timestamp_retry(
                lambda: self.client.fetch_balance({'type': 'swap'})
            ) or {}
            free = balance.get('free') or {}
            usdt_block = balance.get('USDT') or {}
            info = balance.get('info') or {}
            available = (
                to_nullable_number(free.get('USDT'))
                or to_nullable_number(usdt_block.get('free'))
                or to_nullable_number(info.get('availEq'))
            )
            total_block = balance.get('total') or {}
            total = (
                to_nullable_number(total_block.get('USDT'))
                or to_nullable_number(usdt_block.get('total'))
                or to_nullable_number(info.get('totalEq'))
            )
            return {'availableUsdt': available, 'totalUsdt': total}
        except Exception as error:
            raise self._normalize_error(error, 'Failed to fetch account balance')

    # -- orders ---------------------------------------------------------------
    def fetch_open_orders(self, symbol: str) -> list[dict]:
        try:
            normalized = self.normalize_symbol(symbol)
            market = self._require_market(normalized)
            orders = self._with_timestamp_retry(
                lambda: self.client.fetch_open_orders(normalized)
            ) or []
            result = []
            for order in orders:
                info = order.get('info') or {}
                amount = self._from_exchange_amount(market, to_number(order.get('amount'), 0))
                filled = self._from_exchange_amount(market, to_number(order.get('filled'), 0))
                remaining = self._from_exchange_amount(market, to_number(order.get('remaining'), 0))
                result.append({
                    'id': str(order.get('id')),
                    'symbol': order.get('symbol') or normalized,
                    'side': order.get('side') or 'buy',
                    'price': to_number(order.get('price'), 0),
                    'amount': amount,
                    'filled': filled,
                    'remaining': remaining,
                    'reduceOnly': bool(info.get('reduceOnly')),
                    'status': str(order.get('status') or 'open'),
                })
            return result
        except Exception as error:
            raise self._normalize_error(error, 'Failed to fetch open orders')

    def fetch_open_orders_detailed(self, symbol: str) -> list[dict]:
        try:
            normalized = self.normalize_symbol(symbol)
            orders = self._with_timestamp_retry(
                lambda: self.client.fetch_open_orders(normalized)
            ) or []
            return [self._map_order(order, normalized) for order in orders]
        except Exception as error:
            raise self._normalize_error(error, 'Failed to fetch open orders (detailed)')

    def fetch_book_ticker(self, symbol: str) -> dict:
        try:
            normalized = self.normalize_symbol(symbol)
            order_book = self.fetch_order_book_depth(normalized, 5)
            mark_price = None
            try:
                if hasattr(self.client, 'fetch_mark_price'):
                    mark_price = self.client.fetch_mark_price(normalized)
            except Exception:
                mark_price = None
            bids = order_book['bids']
            asks = order_book['asks']
            bid = to_number(bids[0]['price'], 0) if bids else 0.0
            ask = to_number(asks[0]['price'], bid) if asks else bid
            fallback = (bid + ask) / 2 if bid > 0 and ask > 0 else (bid or ask)
            mark = to_number((mark_price or {}).get('markPrice'), fallback)
            return {'bid': bid, 'ask': ask, 'mark': mark}
        except Exception as error:
            raise self._normalize_error(error, 'Failed to fetch order book top of book')

    def fetch_order_book_depth(self, symbol: str, depth_levels: int) -> dict:
        try:
            normalized = self.normalize_symbol(symbol)
            market = self._require_market(normalized)
            order_book = self._with_timestamp_retry(
                lambda: self.client.fetch_order_book(normalized, depth_levels)
            ) or {'bids': [], 'asks': []}
            return {
                'symbol': normalized,
                'bids': [
                    {
                        'price': to_number(level[0], 0),
                        'amount': self._from_exchange_amount(market, to_number(level[1], 0)),
                    }
                    for level in (order_book.get('bids') or [])
                ],
                'asks': [
                    {
                        'price': to_number(level[0], 0),
                        'amount': self._from_exchange_amount(market, to_number(level[1], 0)),
                    }
                    for level in (order_book.get('asks') or [])
                ],
            }
        except Exception as error:
            raise self._normalize_error(error, 'Failed to fetch order book depth')

    def list_tradable_symbols(self) -> list[dict]:
        try:
            self.ensure_markets_loaded()
            items = []
            for market in (self.client.markets or {}).values():
                if not _is_supported_swap_market(market):
                    continue
                items.append({
                    'symbol': compact_market_symbol(market),
                    'normalizedSymbol': market['symbol'],
                    'base': (market.get('base') or '').upper(),
                    'quote': 'USDT',
                    'marketType': 'perpetual',
                })
            items.sort(key=lambda item: item['symbol'])
            return items
        except Exception as error:
            raise self._normalize_error(error, 'Failed to list tradable symbols')

    def set_leverage(self, symbol: str, leverage: float, margin_mode: str = 'cross') -> float:
        try:
            normalized = self.normalize_symbol(symbol)
            params = {'mgnMode': 'isolated' if margin_mode == 'isolated' else 'cross'}
            response = self._with_timestamp_retry(
                lambda: self.client.set_leverage(leverage, normalized, params)
            ) or {}
            data = response.get('data') if isinstance(response, dict) else None
            first = data[0] if isinstance(data, list) and data else {}
            return max(1.0, to_number(
                first.get('lever') if isinstance(first, dict) else None,
                to_number(response.get('leverage') if isinstance(response, dict) else None, leverage),
            ))
        except Exception as error:
            raise self._normalize_error(error, 'Failed to set leverage')

    def amount_from_notional(self, symbol: str, notional_usdt: float, reference_price: float) -> float:
        if not math.isfinite(reference_price) or reference_price <= 0:
            raise ValueError('Reference price must be positive.')
        base_amount = abs(notional_usdt) / reference_price
        return self.amount_to_precision(symbol, base_amount)

    def amount_to_precision(self, symbol: str, amount: float) -> float:
        normalized = self.normalize_symbol(symbol)
        market = self._require_market(normalized)
        exchange_amount = self._to_exchange_amount(market, abs(amount))
        precise_exchange = float(self.client.amount_to_precision(normalized, exchange_amount))
        precise_base = self._from_exchange_amount(market, precise_exchange)
        if not math.isfinite(precise_base) or precise_base <= 0:
            raise ValueError('Unable to compute a valid order amount.')
        return precise_base

    def price_to_precision(self, symbol: str, price: float) -> float:
        normalized = self.normalize_symbol(symbol)
        precise = float(self.client.price_to_precision(normalized, price))
        if not math.isfinite(precise) or precise <= 0:
            raise ValueError('Unable to compute a valid order price.')
        return precise

    def validate_order(self, request: dict) -> dict:
        try:
            normalized = self.normalize_symbol(request['symbol'])
            config_entry = self._get_account_config_entry()
            if self._should_skip_order_precheck(config_entry):
                return {
                    'ok': True,
                    'message': (
                        'Skipped OKX order precheck because this OKX account is in '
                        'Futures mode, where the precheck endpoint is unavailable.'
                    ),
                }
            payload = self._build_precheck_request(normalized, request)
            response = self._with_timestamp_retry(
                lambda: self.client.private_post_trade_order_precheck(payload)
            ) or {}
            response_code = str(response.get('code') or '')
            data = response.get('data') if isinstance(response, dict) else None
            first = data[0] if isinstance(data, list) and data else {}
            order_code = str(first.get('sCode') or response_code or '0')
            order_message = str(first.get('sMsg') or response.get('msg') or 'OK')
            if (response_code and response_code != '0') or (order_code and order_code != '0'):
                return {'ok': False, 'message': order_message or 'OKX order precheck failed.'}
            return {'ok': True, 'message': 'OKX order precheck passed.'}
        except Exception as error:
            normalized_err = self._normalize_error(error, 'OKX order precheck failed')
            return {'ok': False, 'message': str(normalized_err)}

    def place_limit_post_only_order(self, request: dict) -> dict:
        try:
            return self._place_order_internal(request, 'limit', {'postOnly': True})
        except Exception as error:
            message = str(error)
            if re.search(r'post.?only|maker|cancel maker|would immediately', message, re.IGNORECASE):
                raise RuntimeError(f'POST_ONLY_REJECTED:{message}')
            raise self._normalize_error(error, 'Failed to place post-only limit order')

    def fetch_order(self, symbol: str, order_id: str) -> dict:
        try:
            normalized = self.normalize_symbol(symbol)
            order = self._with_timestamp_retry(
                lambda: self.client.fetch_order(order_id, normalized)
            )
            return self._map_order(order, normalized)
        except Exception as error:
            raise self._normalize_error(error, 'Failed to fetch order')

    def cancel_order(self, symbol: str, order_id: str) -> None:
        try:
            normalized = self.normalize_symbol(symbol)
            self._with_timestamp_retry(
                lambda: self.client.cancel_order(order_id, normalized)
            )
        except Exception as error:
            message = str(error)
            if re.search(
                r'order does not exist|already canceled|already filled|not exist|order not found',
                message,
                re.IGNORECASE,
            ):
                return
            raise self._normalize_error(error, 'Failed to cancel order')

    def cancel_orders(self, symbol: str, order_ids: list[str]) -> None:
        for order_id in order_ids:
            try:
                self.cancel_order(symbol, order_id)
            except Exception:
                pass

    # -- internals ------------------------------------------------------------
    def _place_order_internal(self, request: dict, order_type: str, extra_params: dict) -> dict:
        normalized = self.normalize_symbol(request['symbol'])
        market = self._require_market(normalized)
        exchange_amount = self._to_exchange_amount(market, request['amount'])
        price = request.get('price')
        if price is not None and order_type != 'market':
            price = self.price_to_precision(normalized, price)
        params = {
            'tdMode': 'isolated' if request.get('marginMode') == 'isolated' else 'cross',
            'posSide': request.get('positionSide') or 'net',
            'reduceOnly': bool(request.get('reduceOnly')),
        }
        params.update(extra_params or {})
        order = self._with_timestamp_retry(
            lambda: self.client.create_order(
                normalized,
                order_type,
                request['side'],
                exchange_amount,
                None if order_type == 'market' else price,
                params,
            )
        )
        return self._map_order(order, normalized)

    def _build_precheck_request(self, symbol: str, request: dict) -> dict:
        market = self._require_market(symbol)
        exchange_amount = self._to_exchange_amount(market, request['amount'])
        price = request.get('price')
        if price is not None and request.get('orderType') != 'market':
            price = self.price_to_precision(symbol, price)
        ord_type = request.get('orderType') or 'limit_post_only'
        if ord_type == 'market':
            ord_str = 'market'
        elif ord_type == 'limit_ioc':
            ord_str = 'ioc'
        else:
            ord_str = 'post_only'
        payload = {
            'instId': market.get('id'),
            'tdMode': 'isolated' if request.get('marginMode') == 'isolated' else 'cross',
            'posSide': request.get('positionSide') or 'net',
            'side': request['side'],
            'ordType': ord_str,
            'sz': self.client.amount_to_precision(symbol, exchange_amount),
            'reduceOnly': bool(request.get('reduceOnly')),
        }
        if price is not None and ord_type != 'market':
            payload['px'] = self.client.price_to_precision(symbol, price)
        return payload

    def _get_account_config_entry(self) -> dict:
        if self._account_config_entry is not None:
            return self._account_config_entry
        try:
            response = self._with_timestamp_retry(
                lambda: self.client.private_get_account_config()
            ) or {}
            data = response.get('data') if isinstance(response, dict) else None
            entry = data[0] if isinstance(data, list) and data else {}
            self._account_config_entry = entry if isinstance(entry, dict) else {}
        except Exception:
            self._account_config_entry = {}
        return self._account_config_entry

    def _should_skip_order_precheck(self, config_entry: dict) -> bool:
        return str(config_entry.get('acctLv') or '') == '2'

    def _get_reference_margin_mode(self) -> str:
        try:
            if not hasattr(self.client, 'fetch_leverage'):
                return 'cross'
            response = self._with_timestamp_retry(
                lambda: self.client.fetch_leverage(REFERENCE_SYMBOL, {'mgnMode': 'cross'})
            )
            if not response:
                return 'cross'
            info = response.get('info') if isinstance(response, dict) else {}
            mgn = (info or {}).get('mgnMode') if isinstance(info, dict) else None
            if mgn is None and isinstance(response, dict):
                mgn = response.get('marginMode')
            return _parse_margin_mode(mgn)
        except Exception:
            return 'cross'

    def _get_reference_leverage(self) -> Optional[float]:
        try:
            if not hasattr(self.client, 'fetch_leverage'):
                return None
            response = self._with_timestamp_retry(
                lambda: self.client.fetch_leverage(REFERENCE_SYMBOL, {'mgnMode': 'cross'})
            )
            if not response:
                return None
            return to_nullable_number(response.get('leverage') if isinstance(response, dict) else None)
        except Exception:
            return None

    def _map_position_snapshot(self, position: dict, market: dict, fallback_leverage: float, fallback_margin_mode: str) -> dict:
        info = position.get('info') if isinstance(position.get('info'), dict) else position
        info = info or {}
        pos_raw = info.get('pos')
        if isinstance(pos_raw, (int, float, str)):
            try:
                signed_contracts_raw = float(pos_raw)
            except (TypeError, ValueError):
                signed_contracts_raw = to_number(position.get('contracts'), 0)
        else:
            signed_contracts_raw = to_number(position.get('contracts'), 0)
        contract_size = self._get_contract_size(market)
        signed_base_amount = signed_contracts_raw * contract_size
        side = describe_position_side(signed_base_amount)
        mark_price = to_number(info.get('markPx') or position.get('markPrice'), 0)
        signed_notional = signed_base_amount * mark_price
        leverage = max(1.0, to_number(info.get('lever') or position.get('leverage'), fallback_leverage))
        margin_mode = _parse_margin_mode(info.get('mgnMode') or position.get('marginMode') or fallback_margin_mode)
        return {
            'symbol': market['symbol'],
            'markPrice': mark_price,
            'signedContracts': signed_base_amount,
            'signedNotionalUsdt': signed_notional,
            'absNotionalUsdt': abs(signed_notional),
            'side': side,
            'leverage': leverage,
            'marginMode': margin_mode,
            'unrealizedPnlUsdt': to_number(info.get('upl') or position.get('unrealizedPnl'), 0),
        }

    def _map_order(self, order: dict, normalized_symbol: str) -> dict:
        market = self._require_market(normalized_symbol)
        info = order.get('info') or {}
        filled = self._from_exchange_amount(market, to_number(order.get('filled'), 0))
        remaining = self._from_exchange_amount(market, to_number(order.get('remaining'), 0))
        average_price = to_number(order.get('average') or info.get('avgPx'), float('nan'))
        price = to_number(order.get('price') or info.get('px'), float('nan'))
        if math.isfinite(average_price):
            effective_average = average_price
        elif math.isfinite(price):
            effective_average = price
        else:
            effective_average = None
        status = str(order.get('status') or info.get('state') or 'open')
        adjusted_remaining = 0.0 if _okx_status_is_terminal(status) and remaining < 1e-12 else remaining
        return {
            'id': str(order.get('id')),
            'symbol': order.get('symbol') or normalized_symbol,
            'side': order.get('side') or 'buy',
            'status': status,
            'filled': filled,
            'remaining': adjusted_remaining,
            'averagePrice': effective_average,
            'price': price if math.isfinite(price) else None,
            'reduceOnly': bool(info.get('reduceOnly')),
            'filledNotional': abs(filled * effective_average) if effective_average else 0.0,
        }

    def _require_market(self, symbol: str) -> dict:
        markets = getattr(self.client, 'markets', None) or {}
        market = markets.get(symbol)
        if not _is_supported_swap_market(market):
            raise ValueError(f'Unsupported OKX instrument: {symbol}.')
        return market

    def _get_contract_size(self, market: dict) -> float:
        contract_size = to_number(market.get('contractSize'), 1)
        return contract_size if contract_size > 0 else 1.0

    def _to_exchange_amount(self, market: dict, normalized_base_amount: float) -> float:
        if not market.get('contract'):
            return abs(normalized_base_amount)
        return abs(normalized_base_amount) / self._get_contract_size(market)

    def _from_exchange_amount(self, market: dict, exchange_amount: float) -> float:
        if not market.get('contract'):
            return abs(exchange_amount)
        return abs(exchange_amount) * self._get_contract_size(market)

    def _normalize_error(self, error: Exception, context: str) -> Exception:
        message = str(error) if error else context
        env_label = 'OKX demo' if self.environment == 'demo' else 'OKX live'
        if self._is_timestamp_error(error):
            return RuntimeError('Local system time appears out of sync with OKX. Sync the clock and retry.')
        if re.search(r'api.?key|secret|passphrase|signature|auth', message, re.IGNORECASE):
            return RuntimeError(
                f'{env_label} authentication failed. Check api_key, secret_key, passphrase, and API permissions.'
            )
        if re.search(r'network|timeout|timed out|fetch failed|econnreset|enotfound', message, re.IGNORECASE):
            return RuntimeError(f'Could not reach {env_label}. Check network or proxy settings.')
        if re.search(r'long_short_mode|posmode|posmode does not match|hedge|Parameter posSide error', message, re.IGNORECASE):
            return RuntimeError(
                'The OKX account is not in one-way net position mode, or the order parameters are '
                'incompatible with the current position mode.'
            )
        if re.search(r'insufficient|margin|balance|equity', message, re.IGNORECASE):
            return RuntimeError(f'{context}: insufficient margin or balance.')
        return RuntimeError(f'{context}: {message}')

    def _is_timestamp_error(self, error: Exception) -> bool:
        message = str(error) if error else ''
        return bool(re.search(
            r'timestamp|nonce|50102|request expired|expired timestamp|clock',
            message,
            re.IGNORECASE,
        ))

    def _with_timestamp_retry(self, operation: Callable[[], Any]) -> Any:
        try:
            return operation()
        except Exception as error:
            if not self._is_timestamp_error(error):
                raise
            self._sync_time_difference(force=True)
            return operation()

    def _sync_time_difference(self, force: bool = False) -> None:
        if not hasattr(self.client, 'load_time_difference'):
            return
        if not force and self._has_synced_time_difference:
            return
        try:
            self.client.load_time_difference()
            self._has_synced_time_difference = True
        except Exception:
            pass


# ===========================================================================
# Maker entry preview / validation
# ===========================================================================
def get_effective_min_tradable_notional(trading_rules: dict, reference_price: float, fallback_price: float) -> float:
    price = reference_price if reference_price > 0 else fallback_price
    min_amount = trading_rules.get('minAmount') or 0
    amount_floor = min_amount * price if (min_amount > 0 and price > 0) else 0
    return max(1.0, trading_rules.get('minNotional') or 0, amount_floor)


def _pick_validation_levels(depth: dict, side: str) -> list[dict]:
    levels = (depth['bids'] if side == 'LONG' else depth['asks'])[:5]
    return [{'index': idx + 1, 'price': level['price']} for idx, level in enumerate(levels)]


def build_maker_entry_preview(adapter: OkxTradingAdapter, job_input: dict) -> dict:
    normalized_symbol = adapter.normalize_symbol(job_input['symbol'])
    current_position = adapter.fetch_position_snapshot(normalized_symbol)
    open_orders = adapter.fetch_open_orders(normalized_symbol)
    book = adapter.fetch_book_ticker(normalized_symbol)
    rules = adapter.get_symbol_trading_rules(normalized_symbol)

    conflict_orders = [order for order in open_orders if not order['reduceOnly']]
    reference_price = book['bid'] if job_input['side'] == 'LONG' else book['ask']
    effective_min = get_effective_min_tradable_notional(rules, reference_price, book['mark'])

    warnings: list[str] = []
    block_reason: Optional[str] = None
    available_usdt: Optional[float] = None
    estimated_max_notional: Optional[float] = None
    current_leverage = current_position.get('leverage')
    current_margin_mode = current_position.get('marginMode') or 'cross'
    controls = job_input.get('controls') or {}
    target_leverage = controls.get('targetLeverage')
    effective_leverage = max(1.0, target_leverage or current_leverage or 1)
    estimated_required_margin = (
        job_input['entryNotionalUsdt'] / effective_leverage
        if job_input['entryNotionalUsdt'] > 0 else None
    )
    will_set_leverage_on_start = (
        target_leverage is not None and target_leverage != current_position.get('leverage')
    )

    if conflict_orders:
        warnings.append(
            f'Detected {len(conflict_orders)} conflicting non-reduce-only open orders. '
            f'Start will cancel them first.'
        )
    if position_conflicts_with_entry(current_position['side'], job_input['side']):
        block_reason = (
            'The current position is in the opposite direction. Flatten it before starting Maker Entry.'
        )
        warnings.append(block_reason)
    elif current_position['side'] == job_input['side'] and current_position['absNotionalUsdt'] > 0:
        warnings.append(
            f'Existing {job_input["side"]} exposure will be treated as background position. '
            f'This task adds {job_input["entryNotionalUsdt"]:.2f} USDT.'
        )

    if job_input['entryNotionalUsdt'] < effective_min:
        warnings.append(
            'Requested entry notional is below the current effective minimum tradable slice '
            'and may finish as a dust remainder.'
        )
    if target_leverage is not None and will_set_leverage_on_start:
        warnings.append(f'Start will attempt to set leverage to {target_leverage}x before placing orders.')

    try:
        funding = adapter.fetch_account_funding_snapshot()
        available_usdt = funding.get('availableUsdt')
        if available_usdt is not None:
            estimated_max_notional = available_usdt * effective_leverage
    except Exception:
        warnings.append(
            'Unable to read available margin. Feasibility is estimated from leverage and trading rules only.'
        )

    if (
        block_reason is None
        and available_usdt is not None
        and estimated_max_notional is not None
        and estimated_max_notional + 1e-9 < job_input['entryNotionalUsdt']
    ):
        block_reason = (
            f'Estimated maximum supported task size is {estimated_max_notional:.2f} USDT. '
            f'Reduce notional or increase leverage.'
        )
        warnings.append(block_reason)

    # Rolling-execution heuristic: OKX enforces per-instrument / per-leverage
    # concurrent same-side exposure caps that are not discoverable until the
    # runner hits them at place-time. For large entries warn up-front so the
    # user understands the rolling fill -> place cycle behaviour.
    if (
        block_reason is None
        and job_input['entryNotionalUsdt'] >= LARGE_NOTIONAL_WARNING_USDT
    ):
        warnings.append(
            'Large notional entry: OKX enforces per-instrument concurrent '
            'same-side exposure limits based on leverage and account equity. '
            'The executor will run in a rolling "place -> fill -> place" cycle '
            'rather than a single batch. Monitor via `status` during execution.'
        )

    # Unguarded large-notional warning: remind the user to set a slippage guard
    # when running sizeable orders without protection.
    if (
        block_reason is None
        and job_input['entryNotionalUsdt'] >= LARGE_NOTIONAL_WARNING_USDT
        and controls.get('maxAdverseMovePct') is None
    ):
        warnings.append(
            'No slippage guard: maxAdverseMovePct is not set. A large entry '
            'without a guard can keep chasing the book in a fast market. '
            'Consider re-running with --maxAdverseMovePct to auto-pause on '
            'adverse price moves.'
        )

    return {
        'jobKind': 'maker_entry',
        'accountEnvironment': job_input['profile'],
        'runMode': 'live',
        'controls': controls,
        'feasibility': {
            'currentLeverage': current_leverage,
            'currentMarginMode': current_margin_mode,
            'targetLeverage': target_leverage,
            'effectiveLeverage': effective_leverage,
            'willSetLeverageOnStart': will_set_leverage_on_start,
            'availableUsdt': available_usdt,
            'estimatedRequiredMarginUsdt': estimated_required_margin,
            'estimatedMaxNotionalUsdt': estimated_max_notional,
        },
        'currentPosition': current_position,
        'requestedEntry': {
            'side': job_input['side'],
            'entryNotionalUsdt': job_input['entryNotionalUsdt'],
        },
        'conflictOrders': [
            {
                'id': order['id'],
                'side': order['side'],
                'price': order['price'],
                'remaining': order['remaining'],
                'reduceOnly': order['reduceOnly'],
            }
            for order in conflict_orders
        ],
        'effectiveMinTradableNotionalUsdt': effective_min,
        'bandLevels': BAND_LEVELS,
        'refreshIntervalMs': REFRESH_INTERVAL_MS,
        'reconcileIntervalMs': RECONCILE_INTERVAL_MS,
        'reclaimAfterSeconds': RECLAIM_AFTER_SECONDS,
        'warnings': warnings,
        'behaviorSummary': (
            'Passive maker entry across the current 1-40 levels. Once requested notional is '
            'allocated, tail orders are pulled forward into nearer gaps.'
        ),
        'canStart': block_reason is None,
        'blockReason': block_reason,
    }


def validate_maker_entry(adapter: OkxTradingAdapter, job_input: dict, preview: dict) -> dict:
    if not preview['canStart']:
        raise RuntimeError(
            preview.get('blockReason') or 'Maker Entry cannot start with the current position state.'
        )
    symbol = preview['currentPosition']['symbol']
    depth = adapter.fetch_order_book_depth(symbol, 100)
    sample_levels = _pick_validation_levels(depth, job_input['side'])
    reference_price = (
        sample_levels[0]['price']
        if sample_levels
        else adapter.fetch_book_ticker(symbol)['mark']
    )
    effective_min = preview.get('effectiveMinTradableNotionalUsdt') or 50
    validation_notional = min(
        max(job_input['entryNotionalUsdt'] * 0.02, effective_min, 50),
        200,
    )
    amount = adapter.amount_from_notional(symbol, validation_notional, reference_price)
    items = []
    for level in sample_levels:
        result = adapter.validate_order({
            'symbol': symbol,
            'side': entry_side_to_order_side(job_input['side']),
            'amount': amount,
            'price': level['price'],
            'reduceOnly': False,
            'marginMode': preview['feasibility']['currentMarginMode'],
            'positionSide': 'net',
            'orderType': 'limit_post_only',
        })
        items.append({
            'id': f'maker-level-{level["index"]}',
            'label': f'Maker level {level["index"]}',
            'ok': bool(result.get('ok')),
            'message': result.get('message') or '',
        })
    passed = len(items) > 0 and all(item['ok'] for item in items)
    return {
        'environment': job_input['profile'],
        'runMode': 'live',
        'checkedAt': now_iso(),
        'passed': passed,
        'items': items,
        'message': (
            'All sample maker orders passed OKX validation.'
            if passed else 'Some sample maker orders failed OKX validation.'
        ),
    }


def create_initial_snapshot(task_id: str, preview: dict) -> dict:
    requested = preview['requestedEntry']['entryNotionalUsdt']
    return {
        'jobId': task_id,
        'status': 'preflight',
        'currentPosition': preview['currentPosition'],
        'requestedEntry': preview['requestedEntry'],
        'filledEntryNotionalUsdt': 0.0,
        'openWorkingNotionalUsdt': 0.0,
        'unplacedNotionalUsdt': requested,
        'unfilledNotionalUsdt': requested,
        # Kept for backward compatibility: currently equal to unplacedNotional.
        'remainingNotionalUsdt': requested,
        'completionRatio': 0.0,
        'avgFillPrice': None,
        'currentAction': 'Waiting to start.',
        'warnings': list(preview.get('warnings') or []),
        'pauseReason': None,
        'activeWorkingOrders': 0,
        'bandStartLevel': 0,
        'bandEndLevel': preview.get('bandLevels') or BAND_LEVELS,
        'bandNearPrice': None,
        'bandFarPrice': None,
        'lastPlacementLevels': [],
        'lastPlacementOrderCount': 0,
        'lastPlacementNotionalUsdt': None,
        'reclaiming': False,
        'lastReclaimOrderCount': 0,
        'lastReclaimReason': None,
        'effectiveMinTradableNotionalUsdt': preview.get('effectiveMinTradableNotionalUsdt'),
        'lastBookBid': None,
        'lastBookAsk': None,
    }


def create_maker_entry_job(job_input: dict, preview: dict, validation: dict, task_id: Optional[str] = None) -> dict:
    task_id = task_id or str(uuid.uuid4())
    created_at = now_iso()
    return {
        'id': task_id,
        'jobKind': 'maker_entry',
        'profile': job_input['profile'],
        'symbol': preview['currentPosition']['symbol'],
        'status': 'preflight',
        'input': job_input,
        'preview': preview,
        'snapshot': create_initial_snapshot(task_id, preview),
        'result': {
            'executionEnvironment': job_input['profile'],
            'isSimulated': False,
            'validationStatus': validation,
            'requestedEntryNotionalUsdt': job_input['entryNotionalUsdt'],
            'filledEntryNotionalUsdt': 0.0,
            'completionRatio': 0.0,
            'avgFillPrice': None,
            'cancelCount': 0,
            'reclaimCount': 0,
            'placedOrderCount': 0,
            'endReason': 'stopped',
            'pauseReason': None,
            'recommendationText': 'Task has not started yet.',
        },
        'createdAt': created_at,
        'updatedAt': created_at,
    }


def idle_status() -> dict:
    return {
        'taskId': None,
        'profile': None,
        'instId': None,
        'side': None,
        'phase': 'idle',
        'currentAction': 'No active Maker Entry task.',
        'bestBid': None,
        'bestAsk': None,
        'filledEntryNotionalUsdt': 0.0,
        'openWorkingNotionalUsdt': 0.0,
        'unplacedNotionalUsdt': 0.0,
        'unfilledNotionalUsdt': 0.0,
        'remainingNotionalUsdt': 0.0,
        'completionRatio': 0.0,
        'activeWorkingOrders': 0,
        'warnings': [],
        'updatedAt': None,
        'pid': None,
    }


def job_to_status_view(job: dict, pid: Optional[int]) -> dict:
    snap = job['snapshot']
    filled = snap.get('filledEntryNotionalUsdt', 0.0)
    working = snap.get('openWorkingNotionalUsdt', 0.0)
    target = job['input']['entryNotionalUsdt']
    # Derived if the snapshot writer didn't set the new fields (older state).
    unplaced = snap.get('unplacedNotionalUsdt')
    if unplaced is None:
        unplaced = max(0.0, target - filled - working)
    unfilled = snap.get('unfilledNotionalUsdt')
    if unfilled is None:
        unfilled = max(0.0, target - filled)
    return {
        'taskId': job['id'],
        'profile': job['profile'],
        'instId': job['symbol'],
        'side': job['input']['side'],
        'phase': snap['status'],
        'currentAction': snap.get('currentAction'),
        'bestBid': snap.get('lastBookBid'),
        'bestAsk': snap.get('lastBookAsk'),
        'filledEntryNotionalUsdt': filled,
        'openWorkingNotionalUsdt': working,
        'unplacedNotionalUsdt': unplaced,
        'unfilledNotionalUsdt': unfilled,
        'remainingNotionalUsdt': snap.get('remainingNotionalUsdt', unplaced),
        'completionRatio': snap.get('completionRatio', 0.0),
        'activeWorkingOrders': snap.get('activeWorkingOrders', 0),
        'warnings': list(snap.get('warnings') or []),
        'updatedAt': job.get('updatedAt'),
        'pid': pid,
    }


# ===========================================================================
# Maker entry executor
# ===========================================================================
class ControlledStop(Exception):
    def __init__(self, reason: str, pause_reason: Optional[str] = None,
                 end_message: Optional[str] = None, warning_message: Optional[str] = None) -> None:
        super().__init__(end_message or reason)
        self.reason = reason  # 'paused' | 'stopped'
        self.pause_reason = pause_reason
        self.end_message = end_message
        self.warning_message = warning_message


def _append_warning(warnings: list[str], warning: Optional[str]) -> list[str]:
    if not warning or warning in warnings:
        return warnings
    return warnings + [warning]


class RuntimeControl:
    def __init__(self) -> None:
        self.state = 'running'  # 'running' | 'stop_requested'
        self.current_order_ids: list[str] = []


class MakerEntryExecutor:
    def __init__(
        self,
        adapter: OkxTradingAdapter,
        control: RuntimeControl,
        on_job_update: Callable[[dict, str], None],
    ) -> None:
        self.adapter = adapter
        self.control = control
        self.on_job_update = on_job_update

    def execute(self, job: dict) -> dict:
        working_job = copy.deepcopy(job)
        metrics = {
            'cancelCount': 0,
            'reclaimCount': 0,
            'placedOrderCount': 0,
            'filledBaseAmount': 0.0,
            'filledQuoteAmount': 0.0,
        }
        active_orders: dict[str, dict] = {}
        trading_rules = self.adapter.get_symbol_trading_rules(job['symbol'])
        last_position = working_job['preview']['currentPosition']
        last_reconcile_at = 0
        outside_band_reclaim_eligible_since: Optional[int] = None
        tail_gap_reclaim_eligible_since: Optional[int] = None
        loop_seq = 0
        order_seq_box = [0]

        def allocate_local_order_seq() -> int:
            order_seq_box[0] += 1
            return order_seq_box[0]

        last_market_context: Optional[dict] = None
        position_fetch_failures = 0
        market_data_unavailable_since: Optional[int] = None
        market_data_retry_published = False
        placement_capacity = {
            'maxConcurrentEntryAmount': None,
            'warningMessage': None,
        }
        started_at = now_ms()
        reference_adverse_price: Optional[float] = None

        try:
            working_job['snapshot']['status'] = 'preflight'
            working_job['snapshot']['currentAction'] = (
                'Preflight complete. Monitoring the book and placing maker orders.'
            )
            self._publish(working_job, 'Preflight complete')

            while True:
                self._assert_control_state()
                now = now_ms()
                loop_seq += 1

                if now - last_reconcile_at >= ORDER_RECONCILE_INTERVAL_MS:
                    self._reconcile_active_orders(job, active_orders, metrics)
                    try:
                        last_position = self.adapter.fetch_position_snapshot(job['symbol'])
                        position_fetch_failures = 0
                    except Exception:
                        position_fetch_failures += 1
                        if position_fetch_failures >= POSITION_FETCH_MAX_CONSECUTIVE_FAILURES:
                            raise ControlledStop(
                                'paused', 'order_sync',
                                POSITION_FETCH_PAUSE_MESSAGE, POSITION_FETCH_PAUSE_MESSAGE,
                            )
                    last_reconcile_at = now

                try:
                    market = self._load_market_context(job, active_orders)
                    market_data_unavailable_since = None
                    market_data_retry_published = False
                except ControlledStop as stop:
                    if stop.reason == 'paused' and stop.pause_reason == 'market_data_unavailable':
                        failure_observed_at = now_ms()
                        if market_data_unavailable_since is None:
                            market_data_unavailable_since = failure_observed_at
                        if failure_observed_at - market_data_unavailable_since < MARKET_DATA_RETRY_GRACE_MS:
                            filled_entry_notional = min(
                                job['input']['entryNotionalUsdt'],
                                metrics['filledQuoteAmount'],
                            )
                            open_working_notional = self._compute_open_working_notional(active_orders)
                            remaining_notional = max(
                                0,
                                job['input']['entryNotionalUsdt']
                                - filled_entry_notional - open_working_notional,
                            )
                            completion = compute_completion_ratio(
                                filled_entry_notional, job['input']['entryNotionalUsdt']
                            )
                            grace_remaining = max(
                                1,
                                math.ceil(
                                    (MARKET_DATA_RETRY_GRACE_MS
                                     - (failure_observed_at - market_data_unavailable_since)) / 1000
                                ),
                            )
                            snap = working_job['snapshot']
                            phase = (
                                'preflight'
                                if snap['status'] == 'preflight' and not active_orders
                                else 'working'
                            )
                            snap['status'] = phase
                            snap['currentPosition'] = last_position
                            snap['filledEntryNotionalUsdt'] = filled_entry_notional
                            snap['openWorkingNotionalUsdt'] = open_working_notional
                            snap['unplacedNotionalUsdt'] = remaining_notional
                            snap['unfilledNotionalUsdt'] = max(
                                0.0,
                                job['input']['entryNotionalUsdt'] - filled_entry_notional,
                            )
                            snap['remainingNotionalUsdt'] = remaining_notional
                            snap['completionRatio'] = completion
                            snap['avgFillPrice'] = (
                                metrics['filledQuoteAmount'] / metrics['filledBaseAmount']
                                if metrics['filledBaseAmount'] > 0 else None
                            )
                            if active_orders:
                                snap['currentAction'] = (
                                    f'Market data temporarily unavailable. Holding {len(active_orders)} '
                                    f'working orders and retrying ({grace_remaining}s grace remaining).'
                                )
                            else:
                                snap['currentAction'] = (
                                    f'{MARKET_DATA_RETRY_MESSAGE} ({grace_remaining}s grace remaining).'
                                )
                            snap['pauseReason'] = None
                            snap['activeWorkingOrders'] = len(active_orders)
                            snap['reclaiming'] = False
                            if not market_data_retry_published:
                                self._publish(working_job, 'Market data temporarily unavailable')
                                market_data_retry_published = True
                            self._sleep(DECISION_INTERVAL_MS)
                            continue
                    raise

                last_market_context = market
                if reference_adverse_price is None:
                    reference_adverse_price = (
                        market['bestAsk'] if job['input']['side'] == 'LONG' else market['bestBid']
                    )

                effective_min = get_effective_min_tradable_notional(
                    trading_rules,
                    market['bandNearPrice']
                    or (market['bestBid'] if job['input']['side'] == 'LONG' else market['bestAsk'])
                    or last_position['markPrice'],
                    last_position['markPrice'],
                )
                filled_entry_notional = min(
                    job['input']['entryNotionalUsdt'], metrics['filledQuoteAmount']
                )
                open_working_notional = self._compute_open_working_notional(active_orders)
                remaining_notional = max(
                    0,
                    job['input']['entryNotionalUsdt']
                    - filled_entry_notional - open_working_notional,
                )
                completion = compute_completion_ratio(
                    filled_entry_notional, job['input']['entryNotionalUsdt']
                )

                snap = working_job['snapshot']
                snap['status'] = 'working'
                snap['currentPosition'] = last_position
                snap['filledEntryNotionalUsdt'] = filled_entry_notional
                snap['openWorkingNotionalUsdt'] = open_working_notional
                snap['unplacedNotionalUsdt'] = remaining_notional
                snap['unfilledNotionalUsdt'] = max(
                    0.0,
                    job['input']['entryNotionalUsdt'] - filled_entry_notional,
                )
                snap['remainingNotionalUsdt'] = remaining_notional
                snap['completionRatio'] = completion
                snap['avgFillPrice'] = (
                    metrics['filledQuoteAmount'] / metrics['filledBaseAmount']
                    if metrics['filledBaseAmount'] > 0 else None
                )
                _active_count = len(active_orders)
                _active_word = 'order' if _active_count == 1 else 'orders'
                snap['currentAction'] = (
                    f'{_active_count} working maker {_active_word} live.'
                )
                snap['pauseReason'] = None
                snap['activeWorkingOrders'] = len(active_orders)
                snap['bandStartLevel'] = 1 if market['bandLevels'] else 0
                snap['bandEndLevel'] = len(market['bandLevels'])
                snap['bandNearPrice'] = market['bandNearPrice']
                snap['bandFarPrice'] = market['bandFarPrice']
                snap['reclaiming'] = False
                snap['effectiveMinTradableNotionalUsdt'] = effective_min
                snap['lastBookBid'] = market['bestBid']
                snap['lastBookAsk'] = market['bestAsk']
                self._publish(working_job, 'Execution snapshot updated')

                if filled_entry_notional + 1e-6 >= job['input']['entryNotionalUsdt']:
                    self._cancel_active_orders(working_job, active_orders, metrics)
                    return self._finish_job(
                        working_job, last_position, metrics, 'completed', None,
                    )

                if (
                    remaining_notional > 0
                    and remaining_notional < effective_min
                    and open_working_notional <= 1e-6
                ):
                    self._cancel_active_orders(working_job, active_orders, metrics)
                    snap['currentAction'] = (
                        'Remaining notional fell below the effective minimum tradable slice.'
                    )
                    self._publish(working_job, 'Dust remainder')
                    return self._finish_job(
                        working_job, last_position, metrics, 'dust_remainder', None,
                    )

                self._assert_execution_guards(
                    job, market, started_at, now, reference_adverse_price
                )

                placement = self._place_new_maker_orders(
                    working_job, active_orders, market, last_position,
                    trading_rules, remaining_notional, placement_capacity, metrics,
                    allocate_local_order_seq,
                )

                if placement['placedOrderCount'] > 0:
                    snap['status'] = 'working'
                    placed_count = placement['placedOrderCount']
                    placed_levels = placement['placedLevels']
                    active_count = len(active_orders)
                    order_word = 'order' if placed_count == 1 else 'orders'
                    level_word = 'level' if len(placed_levels) == 1 else 'levels'
                    active_word = 'order' if active_count == 1 else 'orders'
                    levels_str = ', '.join(str(lv) for lv in placed_levels)
                    snap['currentAction'] = (
                        f'Placed {placed_count} new maker {order_word} at '
                        f'{level_word} {levels_str}. '
                        f'{active_count} working {active_word} active.'
                    )
                    snap['lastPlacementLevels'] = placement['placedLevels']
                    snap['lastPlacementOrderCount'] = placement['placedOrderCount']
                    snap['lastPlacementNotionalUsdt'] = placement['placedNotionalUsdt']
                    snap['activeWorkingOrders'] = len(active_orders)
                    self._publish(working_job, 'Placed maker orders')

                if placement['capacityLimited']:
                    if placement['placedOrderCount'] == 0:
                        snap['currentAction'] = (
                            f'Exchange concurrent exposure limit reached. Holding '
                            f'{len(active_orders)} working maker orders until fills free capacity.'
                        )
                    snap['warnings'] = _append_warning(
                        snap['warnings'],
                        placement['capacityLimitMessage'] or placement_capacity['warningMessage'],
                    )
                    self._publish(working_job, 'Exchange concurrent exposure limit reached')

                latest_open_notional = self._compute_open_working_notional(active_orders)
                latest_filled_notional = min(
                    job['input']['entryNotionalUsdt'], metrics['filledQuoteAmount']
                )
                allocation_tolerance = max(0.5, min(effective_min, 5))
                allocation_complete = (
                    job['input']['entryNotionalUsdt']
                    - latest_filled_notional - latest_open_notional
                    <= allocation_tolerance
                )
                tail_reprice_active = allocation_complete and bool(active_orders)
                outside_band_demand = self._compute_outside_band_demand_notional(
                    active_orders, market, job['input']['side']
                )
                tail_gap_demand = (
                    self._compute_tail_gap_demand_notional(
                        active_orders, market, job['input']['side']
                    ) if tail_reprice_active else 0
                )

                reclaim_budget = 0.0
                reclaim_reason: Optional[str] = None
                if outside_band_demand > 0 and active_orders:
                    if outside_band_reclaim_eligible_since is None:
                        outside_band_reclaim_eligible_since = now
                    if now - outside_band_reclaim_eligible_since >= RECLAIM_DELAY_MS:
                        reclaim_budget = min(
                            outside_band_demand, latest_open_notional * MAX_RECLAIM_OPEN_RATIO
                        )
                        reclaim_reason = 'outside_band_reclaim' if reclaim_budget > 0 else None
                else:
                    outside_band_reclaim_eligible_since = None

                if reclaim_reason is None and tail_reprice_active and tail_gap_demand > 0 and active_orders:
                    if tail_gap_reclaim_eligible_since is None:
                        tail_gap_reclaim_eligible_since = now
                    tail_delay = self._compute_tail_reprice_check_delay_ms(
                        active_orders, tail_gap_demand, latest_open_notional
                    )
                    if now - tail_gap_reclaim_eligible_since >= tail_delay:
                        reclaim_budget = min(tail_gap_demand, latest_open_notional)
                        reclaim_reason = 'tail_gap_reclaim' if reclaim_budget > 0 else None
                elif not tail_reprice_active or tail_gap_demand <= 0:
                    tail_gap_reclaim_eligible_since = None

                if reclaim_reason and reclaim_budget > 0:
                    reclaimed = self._reclaim_far_orders(
                        working_job, active_orders, market,
                        job['input']['side'], reclaim_budget, reclaim_reason, metrics,
                    )
                    if reclaimed['reclaimedOrders'] > 0:
                        snap['status'] = 'reclaiming'
                        snap['currentAction'] = (
                            f'Reclaimed {reclaimed["reclaimedOrders"]} tail maker orders to refill nearer levels.'
                            if reclaim_reason == 'tail_gap_reclaim'
                            else f'Reclaimed {reclaimed["reclaimedOrders"]} far maker orders that drifted outside the current band.'
                        )
                        snap['reclaiming'] = True
                        snap['lastReclaimOrderCount'] = reclaimed['reclaimedOrders']
                        snap['lastReclaimReason'] = self._describe_reclaim_reason(reclaim_reason)
                        self._publish(working_job, 'Reclaimed far orders')
                        if reclaim_reason == 'outside_band_reclaim':
                            outside_band_reclaim_eligible_since = now
                        else:
                            tail_gap_reclaim_eligible_since = now

                self._sleep(DECISION_INTERVAL_MS)
        except ControlledStop as stop:
            self._cancel_active_orders(working_job, active_orders, metrics, swallow=True)
            try:
                final_position = self.adapter.fetch_position_snapshot(job['symbol'])
            except Exception:
                final_position = working_job['snapshot']['currentPosition']
            pause_reason = stop.pause_reason if stop.reason == 'paused' else None
            snap = working_job['snapshot']
            snap['status'] = 'paused' if stop.reason == 'paused' else 'stopped'
            snap['currentPosition'] = final_position
            snap['currentAction'] = stop.end_message or (
                'Task paused.' if stop.reason == 'paused' else 'Task stopped.'
            )
            snap['activeWorkingOrders'] = 0
            snap['openWorkingNotionalUsdt'] = 0
            snap['reclaiming'] = False
            snap['pauseReason'] = pause_reason
            snap['warnings'] = _append_warning(snap['warnings'], stop.warning_message)
            self._publish(working_job, 'Task interrupted')
            return self._finish_job(
                working_job, final_position, metrics, stop.reason, pause_reason,
            )
        except Exception as error:
            self._cancel_active_orders(working_job, active_orders, metrics, swallow=True)
            try:
                final_position = self.adapter.fetch_position_snapshot(job['symbol'])
            except Exception:
                final_position = working_job['snapshot']['currentPosition']
            snap = working_job['snapshot']
            snap['status'] = 'failed'
            snap['currentPosition'] = final_position
            snap['currentAction'] = str(error) or 'Unknown error'
            snap['activeWorkingOrders'] = 0
            snap['openWorkingNotionalUsdt'] = 0
            snap['reclaiming'] = False
            snap['pauseReason'] = None
            snap['warnings'] = snap['warnings'] + ['Task stopped because of an unexpected error.']
            self._publish(working_job, 'Task failed')
            return self._finish_job(working_job, final_position, metrics, 'error', None)

    # -- placement ------------------------------------------------------------
    def _place_new_maker_orders(
        self,
        job: dict,
        active_orders: dict[str, dict],
        market: dict,
        current_position: dict,
        trading_rules: dict,
        remaining_notional: float,
        placement_capacity: dict,
        metrics: dict,
        allocate_local_order_seq: Callable[[], int],
    ) -> dict:
        if remaining_notional <= 0 or not market['bandLevels']:
            return {
                'placedLevels': [],
                'placedOrderCount': 0,
                'placedNotionalUsdt': 0.0,
                'capacityLimited': False,
                'capacityLimitMessage': None,
            }

        placed_levels: list[int] = []
        placed_order_count = 0
        placed_notional = 0.0
        capacity_limited = False
        capacity_limit_message: Optional[str] = None
        available_budget = remaining_notional
        same_side_position_amount = self._compute_same_side_position_amount(
            current_position, job['input']['side']
        )
        open_working_amount = self._compute_open_working_amount(active_orders)
        order_side = entry_side_to_order_side(job['input']['side'])

        for level in market['bandLevels']:
            if available_budget <= 0:
                break

            available_concurrent = self._compute_available_placement_amount(
                placement_capacity['maxConcurrentEntryAmount'],
                same_side_position_amount,
                open_working_amount,
            )
            if (
                placement_capacity['maxConcurrentEntryAmount'] is not None
                and available_concurrent <= 1e-12
            ):
                capacity_limited = True
                capacity_limit_message = placement_capacity['warningMessage']
                break

            addable_amount = level['addableAmount']
            if addable_amount <= 0:
                continue

            if placement_capacity['maxConcurrentEntryAmount'] is None:
                capped = addable_amount
            else:
                capped = min(addable_amount, available_concurrent)
            if capped <= 0:
                capacity_limited = True
                capacity_limit_message = placement_capacity['warningMessage']
                break

            desired_notional = min(available_budget, capped * level['price'])
            slice_result = self._quantize_tradable_slice(
                job['symbol'], desired_notional, level['price'], trading_rules,
            )
            if not slice_result:
                continue

            try:
                order = self.adapter.place_limit_post_only_order({
                    'symbol': job['symbol'],
                    'side': order_side,
                    'amount': slice_result['amount'],
                    'price': level['price'],
                    'reduceOnly': False,
                    'marginMode': job['preview']['feasibility']['currentMarginMode'],
                    'positionSide': 'net',
                })
                metrics['placedOrderCount'] += 1
                placed_order_count += 1
                placed_levels.append(level['index'])
                placed_notional += slice_result['placedNotionalUsdt']
                available_budget = max(0.0, available_budget - slice_result['placedNotionalUsdt'])

                has_terminal = order['status'] in ('closed', 'canceled') or order['remaining'] <= 0
                has_known_execution = order['filled'] > 0 or order['filledNotional'] > 0
                if has_terminal and has_known_execution:
                    metrics['filledBaseAmount'] += order['filled']
                    metrics['filledQuoteAmount'] += order['filledNotional']
                else:
                    tracked_remaining = (
                        order['remaining'] if order['remaining'] > 0 else slice_result['amount']
                    )
                    active_orders[order['id']] = {
                        'id': order['id'],
                        'localOrderSeq': allocate_local_order_seq(),
                        'levelIndex': level['index'],
                        'orderSide': order_side,
                        'price': level['price'],
                        'amount': slice_result['amount'],
                        'remainingAmount': tracked_remaining,
                        'requestedNotionalUsdt': slice_result['placedNotionalUsdt'],
                        'countedFilledAmount': 0.0,
                        'countedFilledNotionalUsdt': 0.0,
                        'reduceOnly': False,
                        'submittedAt': now_iso(),
                        'placedAt': now_ms(),
                    }
                    open_working_amount += tracked_remaining
            except Exception as error:
                if self._is_post_only_rejected(error):
                    continue
                placement_limit = self._extract_placement_limit(
                    error, trading_rules, level['price']
                )
                if placement_limit:
                    placement_capacity['maxConcurrentEntryAmount'] = placement_limit['maxConcurrentEntryAmount']
                    placement_capacity['warningMessage'] = placement_limit['warningMessage']
                    capacity_limited = True
                    capacity_limit_message = placement_limit['warningMessage']
                    break
                raise

        self.control.current_order_ids = list(active_orders.keys())
        return {
            'placedLevels': placed_levels,
            'placedOrderCount': placed_order_count,
            'placedNotionalUsdt': placed_notional,
            'capacityLimited': capacity_limited,
            'capacityLimitMessage': capacity_limit_message,
        }

    # -- band / reclaim computations -----------------------------------------
    def _compute_outside_band_demand_notional(
        self, active_orders: dict[str, dict], market: dict, side: str
    ) -> float:
        if not active_orders or not market['bandLevels']:
            return 0.0
        outside = self._get_orders_outside_band(active_orders, market, side)
        if not outside:
            return 0.0
        return sum(level['addableAmount'] * level['price'] for level in market['bandLevels'])

    def _get_orders_outside_band(
        self, active_orders: dict[str, dict], market: dict, side: str
    ) -> list[dict]:
        near_price = market.get('bandNearPrice')
        far_price = market.get('bandFarPrice')
        if not near_price or not far_price:
            return []
        result = []
        for order in active_orders.values():
            if side == 'LONG':
                if order['price'] < far_price or order['price'] > near_price:
                    result.append(order)
            else:
                if order['price'] > far_price or order['price'] < near_price:
                    result.append(order)
        return result

    def _get_orders_inside_band(
        self, active_orders: dict[str, dict], market: dict, side: str
    ) -> list[dict]:
        outside = self._get_orders_outside_band(active_orders, market, side)
        if not outside:
            return list(active_orders.values())
        outside_ids = {id(o) for o in outside}
        return [o for o in active_orders.values() if id(o) not in outside_ids]

    def _compute_tail_gap_demand_notional(
        self, active_orders: dict[str, dict], market: dict, side: str
    ) -> float:
        if not active_orders or not market['bandLevels']:
            return 0.0
        inside = self._get_orders_inside_band(active_orders, market, side)
        if not inside:
            return 0.0
        if side == 'LONG':
            farthest_inside_price = min(o['price'] for o in inside)
        else:
            farthest_inside_price = max(o['price'] for o in inside)
        total = 0.0
        for level in market['bandLevels']:
            if level['addableAmount'] <= 0:
                continue
            if not self._is_level_closer_than_price(level['price'], farthest_inside_price, side):
                continue
            total += level['addableAmount'] * level['price']
        return total

    @staticmethod
    def _is_level_closer_than_price(level_price: float, reference_price: float, side: str) -> bool:
        return (level_price > reference_price) if side == 'LONG' else (level_price < reference_price)

    @staticmethod
    def _compute_tail_reprice_check_delay_ms(
        active_orders: dict[str, dict],
        tail_gap_demand: float,
        open_working_notional: float,
    ) -> int:
        if len(active_orders) <= 3:
            return TAIL_REPRICE_FAST_DELAY_MS
        if open_working_notional > 0 and tail_gap_demand / open_working_notional >= 0.25:
            return TAIL_REPRICE_FAST_DELAY_MS
        return TAIL_REPRICE_DEFAULT_DELAY_MS

    def _reclaim_far_orders(
        self,
        job: dict,
        active_orders: dict[str, dict],
        market: dict,
        side: str,
        reclaim_budget: float,
        reclaim_reason: str,
        metrics: dict,
    ) -> dict:
        if reclaim_budget <= 0 or not active_orders:
            return {'reclaimedOrders': 0, 'reclaimedNotionalUsdt': 0.0}

        candidates = sorted(
            active_orders.values(),
            key=lambda o: o['price'] if side == 'LONG' else -o['price'],
        )
        outside_band = self._get_orders_outside_band(active_orders, market, side)
        outside_ids = {id(o) for o in outside_band}
        inside_band = [o for o in candidates if id(o) not in outside_ids]
        ordered_candidates = (
            outside_band + inside_band if reclaim_reason == 'tail_gap_reclaim' else outside_band
        )

        reclaimed_orders = 0
        reclaimed_notional = 0.0
        for candidate in ordered_candidates:
            if reclaimed_notional >= reclaim_budget:
                break
            remaining_ratio = (
                candidate['remainingAmount'] / candidate['amount']
                if candidate['amount'] > 0 else 0
            )
            candidate_notional = candidate['requestedNotionalUsdt'] * max(0, min(1, remaining_ratio))
            if candidate_notional <= 0:
                continue
            try:
                self.adapter.cancel_order(job['symbol'], candidate['id'])
            except Exception:
                pass
            if candidate['id'] in active_orders:
                del active_orders[candidate['id']]
            reclaimed_orders += 1
            reclaimed_notional += candidate_notional
            metrics['cancelCount'] += 1
            metrics['reclaimCount'] += 1

        self.control.current_order_ids = list(active_orders.keys())
        return {'reclaimedOrders': reclaimed_orders, 'reclaimedNotionalUsdt': reclaimed_notional}

    def _reconcile_active_orders(
        self, job: dict, active_orders: dict[str, dict], metrics: dict,
    ) -> None:
        if not active_orders:
            return
        try:
            open_orders = self.adapter.fetch_open_orders_detailed(job['symbol'])
            tracked_ids = set(active_orders.keys())
            open_by_id = {o['id']: o for o in open_orders}
            missing_ids = [oid for oid in tracked_ids if oid not in open_by_id]
            closed_orders: list[dict] = []
            for oid in missing_ids:
                try:
                    closed_orders.append(self.adapter.fetch_order(job['symbol'], oid))
                except Exception:
                    continue
            exchange_orders = [o for o in open_orders if o['id'] in tracked_ids] + closed_orders
        except Exception:
            order_ids = list(active_orders.keys())
            if order_ids:
                try:
                    self.adapter.cancel_orders(job['symbol'], order_ids)
                except Exception:
                    pass
            active_orders.clear()
            self.control.current_order_ids = []
            raise ControlledStop(
                'paused', 'order_sync', ORDER_SYNC_PAUSE_MESSAGE, ORDER_SYNC_PAUSE_MESSAGE
            )

        latest_by_id = {o['id']: o for o in exchange_orders}
        for order_id in list(active_orders.keys()):
            active_order = active_orders[order_id]
            latest = latest_by_id.get(order_id)
            if not latest:
                del active_orders[order_id]
                continue

            delta_filled = max(0.0, latest['filled'] - active_order['countedFilledAmount'])
            delta_notional = max(
                0.0, latest['filledNotional'] - active_order['countedFilledNotionalUsdt']
            )
            if delta_filled > 0 or delta_notional > 0:
                metrics['filledBaseAmount'] += delta_filled
                metrics['filledQuoteAmount'] += delta_notional
                active_order['countedFilledAmount'] = latest['filled']
                active_order['countedFilledNotionalUsdt'] = latest['filledNotional']

            active_order['remainingAmount'] = latest['remaining']
            if latest.get('price') is not None:
                active_order['price'] = latest['price']

            if (
                latest['status'] in ('closed', 'canceled')
                or latest['remaining'] <= 0
            ):
                del active_orders[order_id]

        self.control.current_order_ids = list(active_orders.keys())

    def _load_market_context(self, job: dict, active_orders: dict[str, dict]) -> dict:
        try:
            raw_depth = self.adapter.fetch_order_book_depth(job['symbol'], DEPTH_LEVELS)
        except Exception:
            raw_depth = None
        if not raw_depth or not raw_depth['bids'] or not raw_depth['asks']:
            raise ControlledStop(
                'paused', 'market_data_unavailable',
                MARKET_DATA_UNAVAILABLE_PAUSE_MESSAGE, MARKET_DATA_UNAVAILABLE_PAUSE_MESSAGE,
            )
        own_by_price = self._aggregate_own_amount_by_price(active_orders)
        raw_bids = raw_depth['bids']
        raw_asks = raw_depth['asks']
        best_bid = raw_bids[0]['price'] if raw_bids else 0
        best_ask = raw_asks[0]['price'] if raw_asks else 0
        band_display = (raw_bids if job['input']['side'] == 'LONG' else raw_asks)[:BAND_LEVELS]
        raw_side = raw_bids if job['input']['side'] == 'LONG' else raw_asks
        raw_by_price = {self._price_key(lv['price']): lv['amount'] for lv in raw_side}

        raw_band_levels = []
        for index, level in enumerate(band_display):
            own_amount = own_by_price.get(self._price_key(level['price']), 0.0)
            raw_amount = raw_by_price.get(self._price_key(level['price']), 0.0)
            external_amount = max(0.0, raw_amount - own_amount)
            raw_band_levels.append({
                'index': index + 1,
                'price': level['price'],
                'externalAmount': external_amount,
                'displayedAmount': level['amount'],
                'localRef': 0.0,
                'targetDisplayedAmount': 0.0,
                'addableAmount': 0.0,
            })

        band_levels = []
        for index, level in enumerate(raw_band_levels):
            start = max(0, index - 2)
            end = min(len(raw_band_levels), index + 3)
            window = raw_band_levels[start:end]
            local_ref = max((entry['externalAmount'] for entry in window), default=0.0)
            target_displayed = max(
                level['externalAmount'],
                min(level['externalAmount'] * SHAPE_MULTIPLIER, local_ref * LOCAL_REF_RATIO),
            )
            updated = dict(level)
            updated['localRef'] = local_ref
            updated['targetDisplayedAmount'] = target_displayed
            updated['addableAmount'] = max(0.0, target_displayed - level['displayedAmount'])
            band_levels.append(updated)

        spread_bps = compute_spread_bps(best_bid, best_ask)
        if best_bid <= 0 or best_ask <= 0 or not band_levels:
            raise ControlledStop(
                'paused', 'market_data_unavailable',
                MARKET_DATA_UNAVAILABLE_PAUSE_MESSAGE, MARKET_DATA_UNAVAILABLE_PAUSE_MESSAGE,
            )
        return {
            'bestBid': best_bid,
            'bestAsk': best_ask,
            'spreadBps': spread_bps,
            'bandLevels': band_levels,
            'bandNearPrice': band_levels[0]['price'] if band_levels else None,
            'bandFarPrice': band_levels[-1]['price'] if band_levels else None,
        }

    @staticmethod
    def _aggregate_own_amount_by_price(active_orders: dict[str, dict]) -> dict[str, float]:
        result: dict[str, float] = {}
        for order in active_orders.values():
            key = f'{order["price"]:.12f}'
            result[key] = result.get(key, 0.0) + order['remainingAmount']
        return result

    @staticmethod
    def _price_key(price: float) -> str:
        return f'{price:.12f}'

    @staticmethod
    def _compute_open_working_notional(active_orders: dict[str, dict]) -> float:
        total = 0.0
        for order in active_orders.values():
            if order['amount'] <= 0 or order['requestedNotionalUsdt'] <= 0:
                continue
            remaining_ratio = max(0.0, min(1.0, order['remainingAmount'] / order['amount']))
            total += order['requestedNotionalUsdt'] * remaining_ratio
        return total

    @staticmethod
    def _compute_open_working_amount(active_orders: dict[str, dict]) -> float:
        return sum(max(0.0, o['remainingAmount']) for o in active_orders.values())

    @staticmethod
    def _compute_same_side_position_amount(position: dict, side: str) -> float:
        if side == 'LONG':
            return max(0.0, position['signedContracts'])
        return max(0.0, -position['signedContracts'])

    @staticmethod
    def _compute_available_placement_amount(
        max_concurrent: Optional[float],
        same_side_position: float,
        open_working: float,
    ) -> float:
        if max_concurrent is None:
            return float('inf')
        return max(0.0, max_concurrent - same_side_position - open_working)

    def _quantize_tradable_slice(
        self, symbol: str, desired_notional: float, reference_price: float, trading_rules: dict,
    ) -> Optional[dict]:
        if not math.isfinite(desired_notional) or desired_notional <= 0:
            return None
        if not math.isfinite(reference_price) or reference_price <= 0:
            return None
        try:
            amount = self.adapter.amount_from_notional(symbol, desired_notional, reference_price)
        except Exception:
            return None
        placed_notional = abs(amount * reference_price)
        if amount <= 0 or placed_notional <= 0:
            return None
        if trading_rules['minAmount'] > 0 and amount + 1e-12 < trading_rules['minAmount']:
            return None
        if trading_rules['minNotional'] > 0 and placed_notional + 1e-9 < trading_rules['minNotional']:
            return None
        return {'amount': amount, 'placedNotionalUsdt': placed_notional}

    # -- guards / lifecycle --------------------------------------------------
    def _assert_execution_guards(
        self, job: dict, market: dict, started_at: int, now: int,
        reference_adverse_price: Optional[float],
    ) -> None:
        controls = job['input'].get('controls') or {}
        max_run_minutes = controls.get('maxRunMinutes')
        if max_run_minutes and now - started_at >= max_run_minutes * 60_000:
            message = f'Task runtime exceeded {max_run_minutes} minute(s). Maker Entry paused automatically.'
            raise ControlledStop('paused', 'runtime_limit', message, message)

        max_adverse = controls.get('maxAdverseMovePct')
        if not max_adverse or reference_adverse_price is None or reference_adverse_price <= 0:
            return
        if job['input']['side'] == 'LONG':
            tripped = market['bestAsk'] >= reference_adverse_price * (1 + max_adverse / 100)
        else:
            tripped = market['bestBid'] <= reference_adverse_price * (1 - max_adverse / 100)
        if tripped:
            message = (
                f'Price moved adversely by more than {max_adverse}% from the initial reference. '
                f'Maker Entry paused automatically.'
            )
            raise ControlledStop('paused', 'slippage_guard', message, message)

    def _finish_job(
        self,
        job: dict,
        final_position: dict,
        metrics: dict,
        end_reason: str,
        pause_reason: Optional[str],
    ) -> dict:
        filled_entry = min(job['input']['entryNotionalUsdt'], metrics['filledQuoteAmount'])
        controls = job['input'].get('controls') or {}
        try:
            live_position = self.adapter.fetch_position_snapshot(job['symbol'])
        except Exception:
            live_position = final_position
        final_position = live_position
        avg_fill_price = (
            metrics['filledQuoteAmount'] / metrics['filledBaseAmount']
            if metrics['filledBaseAmount'] > 0 else None
        )
        final_snapshot = {
            'symbol': final_position.get('symbol'),
            'side': final_position.get('side'),
            'signedContracts': final_position.get('signedContracts'),
            'absNotionalUsdt': final_position.get('absNotionalUsdt'),
            'markPrice': final_position.get('markPrice'),
            'leverage': final_position.get('leverage'),
            'marginMode': final_position.get('marginMode'),
            'unrealizedPnlUsdt': final_position.get('unrealizedPnlUsdt'),
            'taskAvgFillPrice': avg_fill_price,
            'taskFilledNotionalUsdt': filled_entry,
        }
        followup_hint = self._describe_followup_hint(
            end_reason, final_position, filled_entry, job['input']['entryNotionalUsdt']
        )
        result = {
            'executionEnvironment': job['preview']['accountEnvironment'],
            'isSimulated': False,
            'validationStatus': (job.get('result') or {}).get('validationStatus'),
            'requestedEntryNotionalUsdt': job['input']['entryNotionalUsdt'],
            'filledEntryNotionalUsdt': filled_entry,
            'completionRatio': compute_completion_ratio(filled_entry, job['input']['entryNotionalUsdt']),
            'avgFillPrice': avg_fill_price,
            'cancelCount': metrics['cancelCount'],
            'reclaimCount': metrics['reclaimCount'],
            'placedOrderCount': metrics['placedOrderCount'],
            'endReason': end_reason,
            'pauseReason': pause_reason,
            'recommendationText': self._describe_end_reason(end_reason, pause_reason, controls),
            'finalPosition': final_snapshot,
            'followupHint': followup_hint,
        }
        status = {
            'paused': 'paused',
            'stopped': 'stopped',
            'error': 'failed',
        }.get(end_reason, 'completed')

        finished = copy.deepcopy(job)
        finished['status'] = status
        finished['result'] = result
        snap = finished['snapshot']
        snap['status'] = status
        snap['currentPosition'] = final_position
        snap['filledEntryNotionalUsdt'] = filled_entry
        snap['openWorkingNotionalUsdt'] = 0
        unfilled_final = max(0.0, job['input']['entryNotionalUsdt'] - filled_entry)
        snap['unplacedNotionalUsdt'] = unfilled_final
        snap['unfilledNotionalUsdt'] = unfilled_final
        snap['remainingNotionalUsdt'] = unfilled_final
        snap['completionRatio'] = result['completionRatio']
        snap['avgFillPrice'] = result['avgFillPrice']
        snap['currentAction'] = self._describe_end_reason(end_reason, pause_reason, controls)
        snap['activeWorkingOrders'] = 0
        snap['reclaiming'] = False
        snap['pauseReason'] = pause_reason
        finished['updatedAt'] = now_iso()
        self._publish(finished, 'Task finished')
        return finished

    @staticmethod
    def _describe_end_reason(end_reason: str, pause_reason: Optional[str], controls: dict) -> str:
        if end_reason == 'completed':
            return 'Requested entry notional was fully filled with maker orders.'
        if end_reason == 'dust_remainder':
            return 'The remaining notional fell below the effective minimum tradable slice.'
        if end_reason == 'paused':
            if pause_reason == 'slippage_guard':
                return (
                    f'Price moved adversely by more than {controls.get("maxAdverseMovePct") or "--"}%. '
                    f'Maker Entry paused automatically.'
                )
            if pause_reason == 'order_sync':
                return ORDER_SYNC_PAUSE_MESSAGE
            if pause_reason == 'market_data_unavailable':
                return MARKET_DATA_UNAVAILABLE_PAUSE_MESSAGE
            if pause_reason == 'runtime_limit':
                return (
                    f'Task runtime exceeded {controls.get("maxRunMinutes") or "--"} minute(s). '
                    f'Maker Entry paused automatically.'
                )
            return 'Task paused.'
        if end_reason == 'stopped':
            return 'Task stopped.'
        return 'Execution ended with an unexpected error.'

    @staticmethod
    def _describe_followup_hint(
        end_reason: str,
        final_position: dict,
        filled_entry: float,
        requested: float,
    ) -> str:
        side = (final_position or {}).get('side') or 'FLAT'
        abs_notional = (final_position or {}).get('absNotionalUsdt') or 0.0
        holds_position = side != 'FLAT' and abs_notional > 1e-6
        if end_reason == 'completed':
            return (
                f'Target reached. You now hold a {side} position of about '
                f'{abs_notional:.2f} USDT. When you want to close it, place a '
                f'reduce-only order manually or run a reverse Maker Entry task.'
            )
        if end_reason == 'dust_remainder':
            return (
                f'Filled {filled_entry:.2f} of {requested:.2f} USDT. Remaining '
                f'amount is below the minimum tradable slice and was dropped. '
                f'Current position: {side} {abs_notional:.2f} USDT.'
            )
        if holds_position:
            return (
                f'Task ended early. You still hold a partially filled {side} '
                f'position of about {abs_notional:.2f} USDT '
                f'(filled {filled_entry:.2f} of {requested:.2f} USDT target). '
                f'Decide whether to keep, top up, or reduce-only close it.'
            )
        return (
            f'Task ended with no open exposure on this instrument. '
            f'Filled {filled_entry:.2f} of {requested:.2f} USDT target.'
        )

    @staticmethod
    def _describe_reclaim_reason(reclaim_reason: str) -> str:
        if reclaim_reason == 'tail_gap_reclaim':
            return 'Tail maker orders were moved forward to refill nearer levels.'
        return 'Active orders drifted outside the current 1-40 level band.'

    @staticmethod
    def _is_post_only_rejected(error: Exception) -> bool:
        return str(error).startswith('POST_ONLY_REJECTED:')

    @staticmethod
    def _extract_placement_limit(error: Exception, trading_rules: dict, reference_price: float) -> Optional[dict]:
        raw_message = str(error)
        if not re.search(r'51004|maximum position amount|pending (buy|sell) orders', raw_message, re.IGNORECASE):
            return None
        match = re.search(r'more than ([\d,]+(?:\.\d+)?)\s*\(contracts\)', raw_message, re.IGNORECASE)
        if not match:
            return None
        try:
            max_contracts = float(match.group(1).replace(',', ''))
        except ValueError:
            return None
        if max_contracts <= 0:
            return None
        max_concurrent = max_contracts * trading_rules.get('contractSize', 1.0)
        estimated_concurrent_notional = max_concurrent * max(0.0, reference_price)
        if estimated_concurrent_notional > 0:
            warning = (
                f'OKX capped concurrent same-side exposure to about {estimated_concurrent_notional:.2f} USDT '
                f'at the current leverage. The executor will wait for fills or cancels before placing more orders.'
            )
        else:
            warning = (
                'OKX capped concurrent same-side exposure at the current leverage. '
                'The executor will wait for fills or cancels before placing more orders.'
            )
        return {
            'maxConcurrentEntryAmount': max_concurrent,
            'warningMessage': warning,
            'rawMessage': raw_message,
        }

    def _cancel_active_orders(
        self, job: dict, active_orders: dict[str, dict], metrics: dict, swallow: bool = False
    ) -> None:
        if not active_orders:
            self.control.current_order_ids = []
            return
        for order in list(active_orders.values()):
            try:
                self.adapter.cancel_order(job['symbol'], order['id'])
            except Exception:
                if not swallow:
                    raise
            metrics['cancelCount'] += 1
        active_orders.clear()
        self.control.current_order_ids = []

    def _assert_control_state(self) -> None:
        if self.control.state == 'stop_requested':
            raise ControlledStop('stopped', None, 'Task stopped.')

    def _sleep(self, duration_ms: int) -> None:
        remaining = duration_ms
        while remaining > 0:
            self._assert_control_state()
            chunk = min(remaining, CONTROL_POLL_INTERVAL_MS)
            time.sleep(chunk / 1000.0)
            remaining -= chunk

    def _publish(self, job: dict, message: str) -> None:
        job['updatedAt'] = now_iso()
        self.on_job_update(job, message)


# ===========================================================================
# Background runner
# ===========================================================================
def _prepare_job_for_execution(
    job: dict, adapter: OkxTradingAdapter, store: StateStore,
) -> dict:
    working = copy.deepcopy(job)
    conflict_ids = [o['id'] for o in working['preview'].get('conflictOrders') or []]
    if conflict_ids:
        adapter.cancel_orders(working['symbol'], conflict_ids)
        working['snapshot']['currentAction'] = (
            f'Canceled {len(conflict_ids)} conflicting non-reduce-only open order(s) before execution.'
        )
        working['updatedAt'] = now_iso()

    target_leverage = (working['input'].get('controls') or {}).get('targetLeverage')
    feas = working['preview']['feasibility']
    if (
        target_leverage is not None
        and feas.get('willSetLeverageOnStart')
        and target_leverage != feas.get('currentLeverage')
    ):
        applied = adapter.set_leverage(
            working['symbol'], target_leverage, feas.get('currentMarginMode') or 'cross'
        )
        if abs(applied - target_leverage) > 1e-9:
            raise RuntimeError(
                f'Leverage mismatch: requested {target_leverage}x but exchange applied {applied}x. '
                f'This may be due to exchange limits or existing positions. '
                f'Aborting to avoid incorrect margin calculations.'
            )
        working['snapshot']['currentAction'] = f'Applied leverage {applied}x before execution.'
        working['updatedAt'] = now_iso()

    position = adapter.fetch_position_snapshot(working['symbol'])
    open_orders = adapter.fetch_open_orders(working['symbol'])
    working['preview']['currentPosition'] = position
    working['preview']['conflictOrders'] = [
        {
            'id': o['id'], 'side': o['side'], 'price': o['price'],
            'remaining': o['remaining'], 'reduceOnly': o['reduceOnly'],
        }
        for o in open_orders if not o['reduceOnly']
    ]
    working['snapshot']['currentPosition'] = position
    working['snapshot']['warnings'] = list(working['preview'].get('warnings') or [])
    working['snapshot']['currentAction'] = 'Preflight actions finished. Starting Maker Entry.'
    working['updatedAt'] = now_iso()
    return working


def _start_stop_flag_watcher(control: RuntimeControl, store: StateStore) -> Callable[[], None]:
    stop_event = threading.Event()

    def watch() -> None:
        while not stop_event.is_set():
            try:
                if control.state == 'running' and store.has_stop_flag():
                    control.state = 'stop_requested'
            except Exception:
                pass
            stop_event.wait(STOP_FLAG_POLL_MS / 1000.0)

    thread = threading.Thread(target=watch, daemon=True)
    thread.start()

    def dispose() -> None:
        stop_event.set()

    return dispose


def run_maker_entry_task(task_id: str) -> None:
    store = StateStore()
    store.ensure_dir()
    existing = store.read_job()
    if not existing or existing.get('id') != task_id:
        raise RuntimeError(f'Task {task_id} was not found in {store.task_file}.')

    profile = load_okx_profile(existing['profile'])
    adapter = OkxTradingAdapter(profile)
    control = RuntimeControl()
    dispose_watcher = _start_stop_flag_watcher(control, store)

    def publish(job: dict, _message: str) -> None:
        try:
            store.write_job(job)
            store.write_status({
                **job_to_status_view(job, os.getpid()),
                'updatedAt': job.get('updatedAt'),
            })
        except Exception:
            pass

    try:
        store.write_runner_pid(os.getpid())
        store.write_status({
            **job_to_status_view(existing, os.getpid()),
            'phase': 'starting',
            'currentAction': 'Runner started. Applying preflight actions before Maker Entry begins.',
            'updatedAt': now_iso(),
        })
        prepared = _prepare_job_for_execution(existing, adapter, store)
        store.write_job(prepared)
        store.write_status(job_to_status_view(prepared, os.getpid()))

        executor = MakerEntryExecutor(adapter, control, publish)
        final_job = executor.execute(prepared)
        store.write_job(final_job)
        final_status = job_to_status_view(final_job, None)
        final_status['updatedAt'] = now_iso()
        final_status['pid'] = None
        store.write_status(final_status)
    except Exception as error:
        current = store.read_job() or existing
        failed = copy.deepcopy(current)
        failed['status'] = 'failed'
        failed['updatedAt'] = now_iso()
        snap = failed['snapshot']
        snap['status'] = 'failed'
        snap['currentAction'] = str(error) or 'Unknown runner error'
        warnings = list(snap.get('warnings') or [])
        if 'Runner failed before execution completed.' not in warnings:
            warnings.append('Runner failed before execution completed.')
        snap['warnings'] = warnings
        snap['activeWorkingOrders'] = 0
        snap['openWorkingNotionalUsdt'] = 0
        snap['reclaiming'] = False
        snap['pauseReason'] = None
        if failed.get('result'):
            failed['result']['endReason'] = 'error'
            failed['result']['pauseReason'] = None
            failed['result']['recommendationText'] = 'Execution ended with an unexpected error.'
        store.write_job(failed)
        fail_status = job_to_status_view(failed, None)
        fail_status['phase'] = 'failed'
        fail_status['pid'] = None
        fail_status['updatedAt'] = failed['updatedAt']
        store.write_status(fail_status)
        raise
    finally:
        dispose_watcher()
        store.write_runner_pid(None)
        store.clear_stop_flag()
        try:
            adapter.close()
        except Exception:
            pass


# ===========================================================================
# CLI commands
# ===========================================================================
def parse_maker_entry_input(args: argparse.Namespace) -> dict:
    profile = normalize_profile_name(args.profile or 'demo')
    inst_id = (args.instId or '').strip() if args.instId else ''
    if not inst_id:
        raise ValueError('Missing required argument --instId <BTC-USDT-SWAP>.')
    side_raw = (args.side or '').strip().upper()
    if side_raw not in ('LONG', 'SHORT'):
        raise ValueError('Missing or invalid --side LONG|SHORT.')
    entry_notional = to_nullable_number(args.entryNotionalUsdt)
    if entry_notional is None or entry_notional <= 0:
        raise ValueError('Missing or invalid --entryNotionalUsdt.')

    def parse_optional_positive(value: Any) -> Optional[float]:
        if value is None or value == '':
            return None
        numeric = to_nullable_number(value)
        if numeric is None or numeric <= 0:
            raise ValueError(f'Invalid numeric argument: {value}')
        return numeric

    return {
        'jobKind': 'maker_entry',
        'profile': profile,
        'symbol': inst_id,
        'side': side_raw,
        'entryNotionalUsdt': entry_notional,
        'runMode': 'live',
        'controls': {
            'targetLeverage': parse_optional_positive(args.targetLeverage),
            'maxAdverseMovePct': parse_optional_positive(args.maxAdverseMovePct),
            'maxRunMinutes': parse_optional_positive(args.maxRunMinutes),
        },
    }


def run_init() -> int:
    config_path = get_okx_config_path()
    if config_path.exists():
        print_json({
            'ok': True,
            'configPath': str(config_path),
            'created': False,
            'message': (
                f'Config file already exists at {config_path}. '
                f'Edit it to update your API credentials.'
            ),
        })
        return 0
    config_path.parent.mkdir(parents=True, exist_ok=True)
    config_path.write_text(CONFIG_TEMPLATE, encoding='utf-8')
    print_json({
        'ok': True,
        'configPath': str(config_path),
        'created': True,
        'message': (
            f'Config template created at {config_path}. '
            f'Fill in your OKX API credentials (api_key, secret_key, passphrase) '
            f'for the demo and/or live profile, then run doctor to verify.'
        ),
    })
    return 0


def run_doctor(args: argparse.Namespace) -> int:
    profile_name = normalize_profile_name(args.profile or 'demo')
    inst_id = args.instId
    config_path = get_okx_config_path()
    python_version = '.'.join(str(v) for v in sys.version_info[:3])
    warnings: list[str] = []
    try:
        document = read_okx_config(config_path)
        profiles = document.get('profiles') or {}
        if profile_name not in profiles:
            print_json({
                'profile': profile_name,
                'pythonVersion': python_version,
                'configPath': str(config_path),
                'profileExists': False,
                'okxSite': 'global',
                'exchange': 'okx',
                'oneWayMode': False,
                'canRead': False,
                'canTrade': False,
                'canTradeFutures': False,
                'symbolChecked': inst_id,
                'symbolTradable': False,
                'leverage': None,
                'marginMode': 'unknown',
                'warnings': warnings,
                'message': f'Profile "{profile_name}" was not found in {config_path}.',
            })
            return 1
        credentials = load_okx_profile(profile_name, config_path)
        adapter = OkxTradingAdapter(credentials)
        try:
            connection = adapter.test_connection()
            symbol_tradable = False
            normalized_symbol = None
            if inst_id:
                normalized_symbol = adapter.normalize_symbol(inst_id)
                adapter.get_symbol_trading_rules(normalized_symbol)
                symbol_tradable = True
            if sys.version_info < (3, 10):
                warnings.append('Python 3.10+ is recommended.')
            report = {
                'profile': profile_name,
                'pythonVersion': python_version,
                'configPath': str(config_path),
                'profileExists': True,
                'okxSite': credentials.get('site') or 'global',
                'exchange': 'okx',
                'oneWayMode': connection['oneWayMode'],
                'canRead': connection['canRead'],
                'canTrade': connection['canTrade'],
                'canTradeFutures': connection['canTradeFutures'],
                'symbolChecked': normalized_symbol or inst_id,
                'symbolTradable': symbol_tradable,
                'leverage': connection['leverage'],
                'marginMode': connection['marginMode'],
                'warnings': warnings,
                'message': connection['message'],
            }
            print_json(report)
            if (
                not report['oneWayMode']
                or not report['canRead']
                or not report['canTrade']
                or (inst_id and not symbol_tradable)
            ):
                return 1
            return 0
        finally:
            try:
                adapter.close()
            except Exception:
                pass
    except Exception as error:
        print_json({
            'ok': False,
            'message': str(error),
            'profile': profile_name,
            'configPath': str(config_path),
            'pythonVersion': python_version,
        })
        return 1


def run_preview(args: argparse.Namespace) -> int:
    job_input = parse_maker_entry_input(args)
    credentials = load_okx_profile(job_input['profile'])
    adapter = OkxTradingAdapter(credentials)
    try:
        preview = build_maker_entry_preview(adapter, job_input)
        validation = validate_maker_entry(adapter, job_input, preview)
        print_json({
            'profile': job_input['profile'],
            'instId': preview['currentPosition']['symbol'],
            'side': job_input['side'],
            'preview': preview,
            'validation': validation,
        })
        if not preview['canStart'] or not validation['passed']:
            return 1
        return 0
    finally:
        try:
            adapter.close()
        except Exception:
            pass


def run_start(args: argparse.Namespace) -> int:
    job_input = parse_maker_entry_input(args)
    store = StateStore()
    store.reject_if_active_task_exists()
    store.clear_stop_flag()

    credentials = load_okx_profile(job_input['profile'])
    adapter = OkxTradingAdapter(credentials)
    try:
        preview = build_maker_entry_preview(adapter, job_input)
        validation = validate_maker_entry(adapter, job_input, preview)
        if not preview['canStart'] or not validation['passed']:
            print_json({
                'ok': False,
                'profile': job_input['profile'],
                'instId': preview['currentPosition']['symbol'],
                'side': job_input['side'],
                'preview': preview,
                'validation': validation,
                'message': preview.get('blockReason') or validation.get('message'),
            })
            return 1

        job = create_maker_entry_job(job_input, preview, validation)
        store.write_job(job)

        script_path = os.path.abspath(__file__)
        popen_kwargs: dict = {'stdin': subprocess.DEVNULL, 'stdout': subprocess.DEVNULL, 'stderr': subprocess.DEVNULL}
        if os.name == 'nt':
            # CREATE_NEW_PROCESS_GROUP | DETACHED_PROCESS | CREATE_NO_WINDOW
            popen_kwargs['creationflags'] = 0x00000008 | 0x00000200 | 0x08000000
        else:
            popen_kwargs['start_new_session'] = True
        child = subprocess.Popen(
            [sys.executable, script_path, 'run', '--taskId', job['id']],
            **popen_kwargs,
        )
        store.write_runner_pid(child.pid)

        status = job_to_status_view(job, child.pid)
        status['phase'] = 'starting'
        status['currentAction'] = 'Runner launching in the background.'
        status['updatedAt'] = now_iso()
        store.write_status(status)

        response = {
            'taskId': job['id'],
            'profile': job_input['profile'],
            'instId': preview['currentPosition']['symbol'],
            'side': job_input['side'],
            'preview': preview,
            'validation': validation,
            'status': status,
        }
        print_json(response)
        return 0
    finally:
        try:
            adapter.close()
        except Exception:
            pass


def run_status() -> int:
    store = StateStore()
    status = store.read_normalized_status() or idle_status()
    if status.get('taskId') and status.get('phase') in TERMINAL_PHASES:
        job = store.read_job()
        if job and job.get('id') == status['taskId'] and job.get('result'):
            result = job['result']
            out = dict(status)
            out['result'] = {
                'requestedEntryNotionalUsdt': result.get('requestedEntryNotionalUsdt'),
                'filledEntryNotionalUsdt': result.get('filledEntryNotionalUsdt'),
                'completionRatio': result.get('completionRatio'),
                'avgFillPrice': result.get('avgFillPrice'),
                'placedOrderCount': result.get('placedOrderCount'),
                'cancelCount': result.get('cancelCount'),
                'reclaimCount': result.get('reclaimCount'),
                'endReason': result.get('endReason'),
                'pauseReason': result.get('pauseReason'),
                'recommendationText': result.get('recommendationText'),
                'finalPosition': result.get('finalPosition'),
                'followupHint': result.get('followupHint'),
            }
            print_json(out)
            return 0
    print_json(status)
    return 0


def run_stop() -> int:
    store = StateStore()
    status = store.read_normalized_status() or idle_status()
    if status.get('taskId') and status.get('phase') in ACTIVE_PHASES:
        store.create_stop_flag()
        next_status = dict(status)
        next_status['currentAction'] = (
            'Stop requested. Waiting for the runner to unwind open orders.'
        )
        next_status['updatedAt'] = now_iso()
        store.write_status(next_status)
        print_json(next_status)
        return 0
    out = dict(status)
    if not out.get('currentAction'):
        out['currentAction'] = 'No active Maker Entry task is running.'
    print_json(out)
    return 0


def run_run(args: argparse.Namespace) -> int:
    task_id = (args.taskId or '').strip()
    if not task_id:
        raise ValueError('Internal run command requires --taskId.')
    run_maker_entry_task(task_id)
    return 0


# ===========================================================================
# Entry point
# ===========================================================================
def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog='okx-maker-entry',
        description='OKX Maker Entry - Passive post-only entry for USDT perpetuals',
    )
    subparsers = parser.add_subparsers(dest='command')

    subparsers.add_parser('init', help='Create ~/.okx/config.toml template.')

    doctor = subparsers.add_parser('doctor', help='Verify credentials, one-way mode, symbol.')
    doctor.add_argument('--profile', default='demo')
    doctor.add_argument('--instId', default=None)

    common_fields = [
        ('--profile', 'demo'),
        ('--instId', None),
        ('--side', None),
        ('--entryNotionalUsdt', None),
        ('--targetLeverage', None),
        ('--maxAdverseMovePct', None),
        ('--maxRunMinutes', None),
    ]
    for name in ('preview', 'start'):
        sub = subparsers.add_parser(name)
        for flag, default in common_fields:
            sub.add_argument(flag, default=default)

    subparsers.add_parser('status', help='Read current task status.')
    subparsers.add_parser('stop', help='Stop the background runner.')

    run_cmd = subparsers.add_parser('run', help='(internal) Background runner entry.')
    run_cmd.add_argument('--taskId', default=None)

    return parser


def main(argv: Optional[list[str]] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    command = args.command or 'help'
    try:
        if command == 'init':
            return run_init()
        if command == 'doctor':
            return run_doctor(args)
        if command == 'preview':
            return run_preview(args)
        if command == 'start':
            return run_start(args)
        if command == 'status':
            return run_status()
        if command == 'stop':
            return run_stop()
        if command == 'run':
            return run_run(args)
        print_json({
            'ok': False,
            'message': (
                'Usage: okx_maker_entry.py <init|doctor|preview|start|status|stop> '
                '[--profile demo|live] [--instId BTC-USDT-SWAP] [--side LONG|SHORT] '
                '[--entryNotionalUsdt 1000]'
            ),
        })
        return 1
    except Exception as error:
        print_json({'ok': False, 'message': str(error), 'trace': traceback.format_exc()})
        return 1


if __name__ == '__main__':
    sys.exit(main())
