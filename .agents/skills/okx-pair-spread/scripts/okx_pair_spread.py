#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
OKX Pair Spread Skill - CLI

Synchronous pair-spread open/close for OKX USDT perpetuals. Opens two legs
at once (one LONG, one SHORT) using market orders, with automatic rollback
if the second leg fails. Closes flexibly: full, per-leg USDT amount, or
single leg only. Stateless — close does not depend on any prior open.

Commands:
    init          Create ~/.okx/config.toml template.
    doctor        Verify credentials, one-way mode, and both symbols.
    status        Read live pair positions + ratio (stateless).
    open-preview  Feasibility + sample order precheck for both legs.
    open          Execute the pair open with rollback on failure.
    close-preview Validate close plan against live positions.
    close         Execute reduce-only market close per leg.
    watch-start   Start a detached local watch daemon.
    watch-list    List active and recent watches.
    watch-stop    Stop a running watch by ID.
    watch-status  Show full state + log tail for a watch.

The trading commands are stateless: they read live exchange state and
return a single JSON result synchronously. The watch commands are the
exception: they manage a detached local daemon and persisted state files
under ~/.okx/watches/.

Dependencies:
    - Python 3.10+ (uses tomllib on 3.11+, tomli fallback otherwise)
    - ccxt (pip install ccxt)
"""

from __future__ import annotations

import argparse
import json
import math
import os
import re
import sys
import time
import traceback
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
# Entry imbalance thresholds — match trading_toolbox pair-spread-executor.ts
ENTRY_IMBALANCE_FLOOR_USDT = 10.0
ENTRY_IMBALANCE_RATIO = 0.20
# Large-notional precheck warning threshold
LARGE_NOTIONAL_WARNING_USDT = 10_000
# Rough buffer used when auto-suggesting leverage from balance
LEVERAGE_AUTO_BUFFER = 1.2


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




def print_json(value: Any) -> None:
    sys.stdout.write(json.dumps(value, indent=2, default=str) + '\n')
    sys.stdout.flush()




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
            f'Run "python okx_pair_spread.py init" to create a config template, '
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

    def place_market_order(self, request: dict) -> dict:
        """Place a market order. Pass `reduceOnly=True` in request to close a position."""
        try:
            return self._place_order_internal(request, 'market', {})
        except Exception as error:
            raise self._normalize_error(error, 'Failed to place market order')

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
# Pair spread — input parsing
# ===========================================================================
def _positive_number(flag: str, raw: Any) -> float:
    try:
        value = float(raw)
    except (TypeError, ValueError):
        raise ValueError(f'{flag} must be a positive number, got: {raw!r}')
    if not math.isfinite(value) or value <= 0:
        raise ValueError(f'{flag} must be a positive number, got: {raw!r}')
    return value


def _optional_positive_number(flag: str, raw: Any) -> Optional[float]:
    if raw is None or (isinstance(raw, str) and not raw.strip()):
        return None
    return _positive_number(flag, raw)


def parse_open_input(args: argparse.Namespace) -> dict:
    if not args.longInstId:
        raise ValueError('--longInstId is required.')
    if not args.shortInstId:
        raise ValueError('--shortInstId is required.')
    entry = _positive_number('--entryNotionalUsdtPerLeg', args.entryNotionalUsdtPerLeg)
    target_leverage = _optional_positive_number('--targetLeverage', args.targetLeverage)
    profile = normalize_profile_name(args.profile or 'demo')
    long_inst = args.longInstId.strip()
    short_inst = args.shortInstId.strip()
    if long_inst.upper() == short_inst.upper():
        raise ValueError('longInstId and shortInstId must be different instruments.')
    return {
        'profile': profile,
        'longInstId': long_inst,
        'shortInstId': short_inst,
        'entryNotionalUsdtPerLeg': entry,
        'targetLeverage': target_leverage,
    }


def parse_close_input(args: argparse.Namespace) -> dict:
    if not args.longInstId:
        raise ValueError('--longInstId is required.')
    if not args.shortInstId:
        raise ValueError('--shortInstId is required.')
    profile = normalize_profile_name(args.profile or 'demo')
    long_inst = args.longInstId.strip()
    short_inst = args.shortInstId.strip()
    if long_inst.upper() == short_inst.upper():
        raise ValueError('longInstId and shortInstId must be different instruments.')
    close_all = bool(getattr(args, 'closeAll', False))
    long_close = _optional_positive_number('--longCloseUsdt', args.longCloseUsdt)
    short_close = _optional_positive_number('--shortCloseUsdt', args.shortCloseUsdt)
    if close_all and (long_close is not None or short_close is not None):
        raise ValueError('--closeAll cannot be combined with --longCloseUsdt / --shortCloseUsdt.')
    if not close_all and long_close is None and short_close is None:
        raise ValueError(
            'Specify either --closeAll or at least one of --longCloseUsdt / --shortCloseUsdt.'
        )
    return {
        'profile': profile,
        'longInstId': long_inst,
        'shortInstId': short_inst,
        'closeAll': close_all,
        'longCloseUsdt': long_close,
        'shortCloseUsdt': short_close,
    }


# ===========================================================================
# Pair spread — helpers
# ===========================================================================
def compute_ratio(long_pos: dict, short_pos: dict) -> Optional[float]:
    long_mark = to_nullable_number(long_pos.get('markPrice'))
    short_mark = to_nullable_number(short_pos.get('markPrice'))
    if not long_mark or not short_mark or short_mark <= 0:
        return None
    return long_mark / short_mark


def compute_combined_pnl(long_pos: dict, short_pos: dict) -> float:
    return (
        to_number(long_pos.get('unrealizedPnlUsdt'), 0.0)
        + to_number(short_pos.get('unrealizedPnlUsdt'), 0.0)
    )


def compute_pair_closable_notional(long_pos: dict, short_pos: dict) -> float:
    """Only meaningful when directions match the pair intent."""
    if long_pos.get('side') != 'LONG' or short_pos.get('side') != 'SHORT':
        return 0.0
    return min(
        to_number(long_pos.get('absNotionalUsdt'), 0.0),
        to_number(short_pos.get('absNotionalUsdt'), 0.0),
    )


def directions_match_pair(long_pos: dict, short_pos: dict) -> bool:
    return long_pos.get('side') == 'LONG' and short_pos.get('side') == 'SHORT'


def fetch_both_positions(adapter: 'OkxTradingAdapter', long_inst: str, short_inst: str) -> tuple[dict, dict]:
    long_pos = adapter.fetch_position_snapshot(long_inst)
    short_pos = adapter.fetch_position_snapshot(short_inst)
    return long_pos, short_pos


def leg_summary_view(position: dict) -> dict:
    """Trim a position snapshot to the fields the SKILL reports."""
    return {
        'symbol': position.get('symbol'),
        'side': position.get('side'),
        'signedContracts': position.get('signedContracts'),
        'absNotionalUsdt': position.get('absNotionalUsdt'),
        'markPrice': position.get('markPrice'),
        'leverage': position.get('leverage'),
        'marginMode': position.get('marginMode'),
        'unrealizedPnlUsdt': position.get('unrealizedPnlUsdt'),
    }


# ===========================================================================
# Pair spread — status
# ===========================================================================
def build_pair_status(adapter: 'OkxTradingAdapter', input_data: dict) -> dict:
    long_pos, short_pos = fetch_both_positions(
        adapter, input_data['longInstId'], input_data['shortInstId']
    )
    warnings: list[str] = []
    direction_ok = directions_match_pair(long_pos, short_pos)
    if long_pos.get('side') == 'FLAT' and short_pos.get('side') == 'FLAT':
        warnings.append('Neither leg has an open position.')
    elif long_pos.get('side') == 'FLAT':
        warnings.append(f'Long leg {long_pos.get("symbol")} has no open position.')
    elif short_pos.get('side') == 'FLAT':
        warnings.append(f'Short leg {short_pos.get("symbol")} has no open position.')
    elif not direction_ok:
        warnings.append(
            f'Directional mismatch: long leg {long_pos.get("symbol")} is actually '
            f'{long_pos.get("side")}, short leg {short_pos.get("symbol")} is actually '
            f'{short_pos.get("side")}. Swap --longInstId and --shortInstId if you meant '
            f'the opposite pairing.'
        )
    return {
        'profile': input_data['profile'],
        'longLeg': leg_summary_view(long_pos),
        'shortLeg': leg_summary_view(short_pos),
        'currentRatio': compute_ratio(long_pos, short_pos),
        'combinedUnrealizedPnlUsdt': compute_combined_pnl(long_pos, short_pos),
        'pairClosableNotionalUsdt': compute_pair_closable_notional(long_pos, short_pos),
        'directionMatches': direction_ok,
        'warnings': warnings,
    }


# ===========================================================================
# Pair spread — open preview + execution
# ===========================================================================
def _leg_conflict_orders(adapter: 'OkxTradingAdapter', symbol: str) -> list[dict]:
    try:
        open_orders = adapter.fetch_open_orders(symbol)
    except Exception:
        return []
    return [
        {
            'id': order['id'],
            'side': order['side'],
            'price': order['price'],
            'remaining': order['remaining'],
            'reduceOnly': order['reduceOnly'],
        }
        for order in open_orders
        if not order['reduceOnly']
    ]


def _validate_market_sample(
    adapter: 'OkxTradingAdapter',
    symbol: str,
    side: str,
    notional_usdt: float,
    margin_mode: str,
) -> dict:
    """Run OKX precheck with a small sample market order for the given side."""
    try:
        book = adapter.fetch_book_ticker(symbol)
        reference = book['ask'] if side == 'buy' else book['bid']
        if not reference or reference <= 0:
            reference = book.get('mark') or 0
        if not reference or reference <= 0:
            return {'ok': False, 'message': 'No reference price available for sample order.'}
        sample_notional = max(min(notional_usdt * 0.02, 200.0), 50.0)
        amount = adapter.amount_from_notional(symbol, sample_notional, reference)
        return adapter.validate_order({
            'symbol': symbol,
            'side': side,
            'amount': amount,
            'price': reference,
            'reduceOnly': False,
            'marginMode': margin_mode,
            'positionSide': 'net',
            'orderType': 'market',
        })
    except Exception as error:
        return {'ok': False, 'message': str(error)}


def build_open_preview(adapter: 'OkxTradingAdapter', input_data: dict) -> dict:
    long_pos, short_pos = fetch_both_positions(
        adapter, input_data['longInstId'], input_data['shortInstId']
    )
    long_symbol = long_pos['symbol']
    short_symbol = short_pos['symbol']

    long_conflicts = _leg_conflict_orders(adapter, long_symbol)
    short_conflicts = _leg_conflict_orders(adapter, short_symbol)

    warnings: list[str] = []
    block_reason: Optional[str] = None

    # Reverse exposure detection — block.
    if long_pos['side'] == 'SHORT' and long_pos['absNotionalUsdt'] > 0:
        block_reason = (
            f'{long_symbol} already has an opposing SHORT position ({long_pos["absNotionalUsdt"]:.2f} '
            f'USDT). Flatten it before opening this pair spread.'
        )
    elif short_pos['side'] == 'LONG' and short_pos['absNotionalUsdt'] > 0:
        block_reason = (
            f'{short_symbol} already has an opposing LONG position ({short_pos["absNotionalUsdt"]:.2f} '
            f'USDT). Flatten it before opening this pair spread.'
        )

    if long_pos['side'] == 'LONG' and long_pos['absNotionalUsdt'] > 0:
        warnings.append(
            f'{long_symbol} already has {long_pos["absNotionalUsdt"]:.2f} USDT of existing LONG '
            f'exposure. Opening will stack on top of it.'
        )
    if short_pos['side'] == 'SHORT' and short_pos['absNotionalUsdt'] > 0:
        warnings.append(
            f'{short_symbol} already has {short_pos["absNotionalUsdt"]:.2f} USDT of existing SHORT '
            f'exposure. Opening will stack on top of it.'
        )

    if long_conflicts:
        warnings.append(
            f'{long_symbol}: {len(long_conflicts)} conflicting non-reduce-only order(s) will be '
            f'cancelled on start.'
        )
    if short_conflicts:
        warnings.append(
            f'{short_symbol}: {len(short_conflicts)} conflicting non-reduce-only order(s) will be '
            f'cancelled on start.'
        )

    current_long_leverage = long_pos.get('leverage')
    current_short_leverage = short_pos.get('leverage')
    target_leverage = input_data.get('targetLeverage')
    effective_leverage = max(
        1.0,
        target_leverage
        or current_long_leverage
        or current_short_leverage
        or 1.0,
    )
    will_set_leverage = target_leverage is not None and (
        target_leverage != current_long_leverage or target_leverage != current_short_leverage
    )
    if will_set_leverage:
        warnings.append(
            f'Both legs will have leverage set to {target_leverage}x before placing orders.'
        )

    per_leg_notional = input_data['entryNotionalUsdtPerLeg']
    total_exposure = 2.0 * per_leg_notional
    estimated_required_margin = total_exposure / effective_leverage

    available_usdt: Optional[float] = None
    estimated_max_per_leg: Optional[float] = None
    try:
        funding = adapter.fetch_account_funding_snapshot()
        available_usdt = funding.get('availableUsdt')
        if available_usdt is not None:
            estimated_max_per_leg = (available_usdt * effective_leverage) / 2.0
    except Exception:
        warnings.append(
            'Unable to read available margin. Feasibility is estimated from leverage only.'
        )

    if (
        block_reason is None
        and available_usdt is not None
        and estimated_max_per_leg is not None
        and estimated_max_per_leg + 1e-9 < per_leg_notional
    ):
        block_reason = (
            f'Insufficient margin. Estimated max per-leg size is '
            f'{estimated_max_per_leg:.2f} USDT at {effective_leverage:g}x leverage. '
            f'Reduce entryNotionalUsdtPerLeg or increase leverage.'
        )

    if (
        block_reason is None
        and per_leg_notional >= LARGE_NOTIONAL_WARNING_USDT
    ):
        warnings.append(
            'Large per-leg notional: market orders will cross the book and may '
            'incur meaningful slippage on illiquid instruments.'
        )

    # Order validation — sample market orders on both legs.
    margin_mode = long_pos.get('marginMode') or short_pos.get('marginMode') or 'cross'
    long_val = _validate_market_sample(
        adapter, long_symbol, 'buy', per_leg_notional, margin_mode
    )
    short_val = _validate_market_sample(
        adapter, short_symbol, 'sell', per_leg_notional, margin_mode
    )
    validation_items = [
        {
            'id': 'long-leg-market',
            'label': f'Long leg market buy on {long_symbol}',
            'ok': bool(long_val.get('ok')),
            'message': long_val.get('message') or '',
        },
        {
            'id': 'short-leg-market',
            'label': f'Short leg market sell on {short_symbol}',
            'ok': bool(short_val.get('ok')),
            'message': short_val.get('message') or '',
        },
    ]
    validation_passed = all(item['ok'] for item in validation_items)
    validation = {
        'environment': input_data['profile'],
        'checkedAt': now_iso(),
        'passed': validation_passed,
        'items': validation_items,
        'message': (
            'Both sample market orders passed OKX precheck.'
            if validation_passed
            else 'At least one sample market order failed OKX precheck.'
        ),
    }

    can_start = block_reason is None and validation_passed
    if not can_start and block_reason is None and not validation_passed:
        block_reason = validation.get('message') or 'Sample order precheck failed.'

    return {
        'profile': input_data['profile'],
        'entryNotionalUsdtPerLeg': per_leg_notional,
        'longLeg': {
            'symbol': long_symbol,
            'currentPosition': long_pos,
            'conflictOrders': long_conflicts,
        },
        'shortLeg': {
            'symbol': short_symbol,
            'currentPosition': short_pos,
            'conflictOrders': short_conflicts,
        },
        'currentRatio': compute_ratio(long_pos, short_pos),
        'combinedUnrealizedPnlUsdt': compute_combined_pnl(long_pos, short_pos),
        'feasibility': {
            'currentLongLeverage': current_long_leverage,
            'currentShortLeverage': current_short_leverage,
            'targetLeverage': target_leverage,
            'effectiveLeverage': effective_leverage,
            'willSetLeverageOnStart': will_set_leverage,
            'marginMode': margin_mode,
            'availableUsdt': available_usdt,
            'estimatedRequiredMarginUsdt': estimated_required_margin,
            'estimatedMaxNotionalPerLegUsdt': estimated_max_per_leg,
            'totalExposureUsdt': total_exposure,
        },
        'validation': validation,
        'warnings': warnings,
        'behaviorSummary': (
            'Market-order both legs in sequence. If the second leg fails, the '
            'first leg is rolled back with a reduce-only market order. '
            'An imbalance larger than max(10 USDT, 20% of per-leg notional) '
            'produces a non-fatal warning after execution.'
        ),
        'canStart': can_start,
        'blockReason': block_reason,
    }


def _place_leg_market(
    adapter: 'OkxTradingAdapter',
    symbol: str,
    side: str,
    notional_usdt: float,
    margin_mode: str,
    reduce_only: bool = False,
) -> dict:
    """Place a market order sized to the requested notional. Returns order snapshot
    including filledAmount / filledNotional / avgFillPrice."""
    book = adapter.fetch_book_ticker(symbol)
    reference = book['ask'] if side == 'buy' else book['bid']
    if not reference or reference <= 0:
        reference = book.get('mark') or 0
    if not reference or reference <= 0:
        raise RuntimeError(f'No reference price available for {symbol} market order.')
    amount = adapter.amount_from_notional(symbol, notional_usdt, reference)
    order = adapter.place_market_order({
        'symbol': symbol,
        'side': side,
        'amount': amount,
        'reduceOnly': reduce_only,
        'marginMode': margin_mode,
        'positionSide': 'net',
    })
    return order


def _order_fill_summary(order: dict, requested_notional: float) -> dict:
    avg_price = to_nullable_number(order.get('averagePrice'))
    filled = to_number(order.get('filled'), 0.0)
    filled_notional = to_number(order.get('filledNotional'), 0.0)
    if filled_notional <= 0 and avg_price is not None:
        filled_notional = filled * avg_price
    return {
        'orderId': order.get('id'),
        'side': order.get('side'),
        'status': order.get('status'),
        'requestedNotionalUsdt': requested_notional,
        'filledAmount': filled,
        'filledNotionalUsdt': filled_notional,
        'avgFillPrice': avg_price,
    }


def _cancel_conflict_orders(
    adapter: 'OkxTradingAdapter',
    symbol: str,
    orders: list[dict],
) -> int:
    if not orders:
        return 0
    count = 0
    for order in orders:
        try:
            adapter.cancel_order(symbol, order['id'])
            count += 1
        except Exception:
            continue
    return count


def execute_open(adapter: 'OkxTradingAdapter', input_data: dict, preview: dict) -> dict:
    long_symbol = preview['longLeg']['symbol']
    short_symbol = preview['shortLeg']['symbol']
    per_leg_notional = input_data['entryNotionalUsdtPerLeg']
    margin_mode = preview['feasibility'].get('marginMode') or 'cross'
    target_leverage = input_data.get('targetLeverage')

    cancelled_long = _cancel_conflict_orders(
        adapter, long_symbol, preview['longLeg']['conflictOrders']
    )
    cancelled_short = _cancel_conflict_orders(
        adapter, short_symbol, preview['shortLeg']['conflictOrders']
    )

    if target_leverage is not None and preview['feasibility']['willSetLeverageOnStart']:
        try:
            adapter.set_leverage(long_symbol, target_leverage, margin_mode)
        except Exception as error:
            return {
                'ok': False,
                'endReason': 'entry_failed',
                'message': f'Failed to set leverage on long leg: {error}',
                'longLeg': {'fill': None, 'finalPosition': None},
                'shortLeg': {'fill': None, 'finalPosition': None},
                'rollbackPerformed': False,
                'cancelledConflictOrders': {
                    'long': cancelled_long,
                    'short': cancelled_short,
                },
            }
        try:
            adapter.set_leverage(short_symbol, target_leverage, margin_mode)
        except Exception as error:
            return {
                'ok': False,
                'endReason': 'entry_failed',
                'message': f'Failed to set leverage on short leg: {error}',
                'longLeg': {'fill': None, 'finalPosition': None},
                'shortLeg': {'fill': None, 'finalPosition': None},
                'rollbackPerformed': False,
                'cancelledConflictOrders': {
                    'long': cancelled_long,
                    'short': cancelled_short,
                },
            }

    # Leg 1: long (market buy).
    try:
        long_order = _place_leg_market(
            adapter, long_symbol, 'buy', per_leg_notional, margin_mode, reduce_only=False
        )
    except Exception as error:
        return {
            'ok': False,
            'endReason': 'entry_failed',
            'message': f'Long leg market order failed before any exposure: {error}',
            'longLeg': {'fill': None, 'finalPosition': adapter.fetch_position_snapshot(long_symbol)},
            'shortLeg': {'fill': None, 'finalPosition': adapter.fetch_position_snapshot(short_symbol)},
            'rollbackPerformed': False,
            'cancelledConflictOrders': {
                'long': cancelled_long,
                'short': cancelled_short,
            },
        }
    long_fill = _order_fill_summary(long_order, per_leg_notional)

    # Leg 2: short (market sell). Rollback long leg on failure.
    try:
        short_order = _place_leg_market(
            adapter, short_symbol, 'sell', per_leg_notional, margin_mode, reduce_only=False
        )
    except Exception as error:
        # Attempt reduce-only rollback of the long leg.
        rollback_error: Optional[str] = None
        try:
            rollback_order = _place_leg_market(
                adapter,
                long_symbol,
                'sell',
                long_fill['filledNotionalUsdt'] or per_leg_notional,
                margin_mode,
                reduce_only=True,
            )
            _ = _order_fill_summary(rollback_order, long_fill['filledNotionalUsdt'] or per_leg_notional)
        except Exception as rollback_exc:
            rollback_error = str(rollback_exc)

        long_final = adapter.fetch_position_snapshot(long_symbol)
        short_final = adapter.fetch_position_snapshot(short_symbol)
        rollback_successful = (
            rollback_error is None
            and long_final.get('side') == 'FLAT'
        )
        end_reason = 'entry_failed' if rollback_successful else 'entry_rollback_failed'
        message_parts = [f'Short leg market order failed: {error}']
        if rollback_successful:
            message_parts.append('Long leg was rolled back to FLAT.')
        elif rollback_error:
            message_parts.append(f'Long leg rollback also failed: {rollback_error}')
        else:
            message_parts.append(
                f'Long leg rollback completed but residual exposure remains: '
                f'{long_final.get("side")} {long_final.get("absNotionalUsdt"):.2f} USDT. '
                f'Manual action required.'
            )
        followup_hint = None
        if not rollback_successful:
            followup_hint = (
                f'⚠️  Pair open aborted with residual exposure. Check {long_symbol} and '
                f'{short_symbol} positions manually, and flatten whichever leg is still open.'
            )
        return {
            'ok': False,
            'endReason': end_reason,
            'message': ' '.join(message_parts),
            'longLeg': {
                'fill': long_fill,
                'finalPosition': long_final,
            },
            'shortLeg': {
                'fill': None,
                'finalPosition': short_final,
            },
            'rollbackPerformed': True,
            'rollbackSuccessful': rollback_successful,
            'cancelledConflictOrders': {
                'long': cancelled_long,
                'short': cancelled_short,
            },
            'followupHint': followup_hint,
        }
    short_fill = _order_fill_summary(short_order, per_leg_notional)

    long_final = adapter.fetch_position_snapshot(long_symbol)
    short_final = adapter.fetch_position_snapshot(short_symbol)

    imbalance = abs(long_fill['filledNotionalUsdt'] - short_fill['filledNotionalUsdt'])
    imbalance_threshold = max(
        ENTRY_IMBALANCE_FLOOR_USDT,
        per_leg_notional * ENTRY_IMBALANCE_RATIO,
    )
    warnings: list[str] = []
    if imbalance > imbalance_threshold:
        warnings.append(
            f'Entry imbalance {imbalance:.2f} USDT exceeds the threshold '
            f'{imbalance_threshold:.2f} USDT. Both legs are open; you may want '
            f'to manually trim the larger leg or add to the smaller one.'
        )
        end_reason = 'imbalance_warning'
    else:
        end_reason = 'completed'

    followup_hint = (
        f'Pair spread opened: long {long_symbol} / short {short_symbol}. '
        f'Monitor via "status" and close via "close" when ready. '
        f'This skill does NOT auto-monitor ratio — you (or Claude via /loop) '
        f'are responsible for calling close when a trigger hits.'
    )

    return {
        'ok': True,
        'endReason': end_reason,
        'message': (
            'Both legs filled successfully.'
            if end_reason == 'completed'
            else 'Both legs filled, but imbalance exceeds the safety threshold.'
        ),
        'longLeg': {'fill': long_fill, 'finalPosition': long_final},
        'shortLeg': {'fill': short_fill, 'finalPosition': short_final},
        'entryImbalanceNotionalUsdt': imbalance,
        'entryImbalanceThresholdUsdt': imbalance_threshold,
        'currentRatio': compute_ratio(long_final, short_final),
        'combinedUnrealizedPnlUsdt': compute_combined_pnl(long_final, short_final),
        'rollbackPerformed': False,
        'cancelledConflictOrders': {
            'long': cancelled_long,
            'short': cancelled_short,
        },
        'warnings': warnings,
        'followupHint': followup_hint,
    }


# ===========================================================================
# Pair spread — close preview + execution
# ===========================================================================
def build_close_preview(adapter: 'OkxTradingAdapter', input_data: dict) -> dict:
    long_pos, short_pos = fetch_both_positions(
        adapter, input_data['longInstId'], input_data['shortInstId']
    )
    long_symbol = long_pos['symbol']
    short_symbol = short_pos['symbol']
    long_abs = to_number(long_pos.get('absNotionalUsdt'), 0.0)
    short_abs = to_number(short_pos.get('absNotionalUsdt'), 0.0)
    margin_mode = long_pos.get('marginMode') or short_pos.get('marginMode') or 'cross'

    warnings: list[str] = []
    block_reason: Optional[str] = None
    direction_ok = directions_match_pair(long_pos, short_pos)

    if not direction_ok:
        block_reason = (
            f'Direction mismatch: long leg {long_symbol} is actually '
            f'{long_pos.get("side")}, short leg {short_symbol} is actually '
            f'{short_pos.get("side")}. Confirm which symbol is LONG and which '
            f'is SHORT, then rerun with the parameters swapped if needed.'
        )

    # Build plan
    close_all = input_data['closeAll']
    if close_all:
        long_close_usdt = long_abs if long_abs > 0 else 0.0
        short_close_usdt = short_abs if short_abs > 0 else 0.0
    else:
        long_close_usdt = input_data['longCloseUsdt'] or 0.0
        short_close_usdt = input_data['shortCloseUsdt'] or 0.0

    if (
        block_reason is None
        and long_close_usdt <= 0
        and short_close_usdt <= 0
    ):
        block_reason = 'No legs to close (both amounts are 0).'

    if (
        block_reason is None
        and long_close_usdt > 0
        and long_close_usdt - 1e-6 > long_abs
    ):
        block_reason = (
            f'Long close amount {long_close_usdt:.2f} USDT exceeds current long '
            f'position {long_abs:.2f} USDT.'
        )
    if (
        block_reason is None
        and short_close_usdt > 0
        and short_close_usdt - 1e-6 > short_abs
    ):
        block_reason = (
            f'Short close amount {short_close_usdt:.2f} USDT exceeds current short '
            f'position {short_abs:.2f} USDT.'
        )

    # Single-leg warning
    if long_close_usdt > 0 and short_close_usdt <= 0:
        remaining_short = short_abs
        if remaining_short > 0:
            warnings.append(
                f'⚠️  Only closing the long leg. {short_symbol} will retain '
                f'{remaining_short:.2f} USDT of SHORT exposure — naked, no hedge.'
            )
    if short_close_usdt > 0 and long_close_usdt <= 0:
        remaining_long = long_abs
        if remaining_long > 0:
            warnings.append(
                f'⚠️  Only closing the short leg. {long_symbol} will retain '
                f'{remaining_long:.2f} USDT of LONG exposure — naked, no hedge.'
            )

    # Partial close note
    if (
        not close_all
        and 0 < long_close_usdt < long_abs - 1e-6
    ):
        warnings.append(
            f'Partial close on long leg: {long_close_usdt:.2f} of '
            f'{long_abs:.2f} USDT. Remaining exposure will continue to carry risk.'
        )
    if (
        not close_all
        and 0 < short_close_usdt < short_abs - 1e-6
    ):
        warnings.append(
            f'Partial close on short leg: {short_close_usdt:.2f} of '
            f'{short_abs:.2f} USDT. Remaining exposure will continue to carry risk.'
        )

    # USDT-to-contracts conversion caveat
    if long_close_usdt > 0 or short_close_usdt > 0:
        warnings.append(
            'Close amounts in USDT are converted to contracts at mark price. '
            'Small residual amounts may remain after execution.'
        )

    def build_leg_plan(symbol: str, order_side: str, close_usdt: float) -> Optional[dict]:
        if close_usdt <= 0:
            return None
        try:
            book = adapter.fetch_book_ticker(symbol)
            reference = book['ask'] if order_side == 'buy' else book['bid']
            if not reference or reference <= 0:
                reference = book.get('mark') or 0
            estimated_amount = None
            if reference and reference > 0:
                try:
                    estimated_amount = adapter.amount_from_notional(symbol, close_usdt, reference)
                except Exception:
                    estimated_amount = None
        except Exception:
            reference = None
            estimated_amount = None
        return {
            'symbol': symbol,
            'orderSide': order_side,
            'requestedNotionalUsdt': close_usdt,
            'estimatedAmount': estimated_amount,
            'referencePrice': reference,
        }

    plan = {
        'longClose': build_leg_plan(long_symbol, 'sell', long_close_usdt) if direction_ok or long_close_usdt <= 0 else None,
        'shortClose': build_leg_plan(short_symbol, 'buy', short_close_usdt) if direction_ok or short_close_usdt <= 0 else None,
    }

    can_close = block_reason is None

    return {
        'profile': input_data['profile'],
        'longLeg': {
            'symbol': long_symbol,
            'currentPosition': long_pos,
        },
        'shortLeg': {
            'symbol': short_symbol,
            'currentPosition': short_pos,
        },
        'currentRatio': compute_ratio(long_pos, short_pos),
        'combinedUnrealizedPnlUsdt': compute_combined_pnl(long_pos, short_pos),
        'pairClosableNotionalUsdt': compute_pair_closable_notional(long_pos, short_pos),
        'directionMatches': direction_ok,
        'closeAll': close_all,
        'plan': plan,
        'marginMode': margin_mode,
        'warnings': warnings,
        'canClose': can_close,
        'blockReason': block_reason,
    }


def execute_close(adapter: 'OkxTradingAdapter', input_data: dict, preview: dict) -> dict:
    long_symbol = preview['longLeg']['symbol']
    short_symbol = preview['shortLeg']['symbol']
    plan = preview['plan']
    margin_mode = preview.get('marginMode') or 'cross'

    long_result: Optional[dict] = None
    short_result: Optional[dict] = None
    long_error: Optional[str] = None
    short_error: Optional[str] = None

    if plan.get('longClose'):
        try:
            order = _place_leg_market(
                adapter,
                long_symbol,
                'sell',
                plan['longClose']['requestedNotionalUsdt'],
                margin_mode,
                reduce_only=True,
            )
            long_result = _order_fill_summary(order, plan['longClose']['requestedNotionalUsdt'])
        except Exception as error:
            long_error = str(error)

    if plan.get('shortClose'):
        try:
            order = _place_leg_market(
                adapter,
                short_symbol,
                'buy',
                plan['shortClose']['requestedNotionalUsdt'],
                margin_mode,
                reduce_only=True,
            )
            short_result = _order_fill_summary(order, plan['shortClose']['requestedNotionalUsdt'])
        except Exception as error:
            short_error = str(error)

    long_final = adapter.fetch_position_snapshot(long_symbol)
    short_final = adapter.fetch_position_snapshot(short_symbol)

    any_error = long_error is not None or short_error is not None
    message_parts: list[str] = []
    if long_error:
        message_parts.append(f'Long leg close failed: {long_error}')
    if short_error:
        message_parts.append(f'Short leg close failed: {short_error}')
    if not any_error:
        message_parts.append('Close orders placed successfully.')

    # Followup hint — call out residual exposure explicitly.
    followup_lines: list[str] = []
    long_remaining = to_number(long_final.get('absNotionalUsdt'), 0.0)
    short_remaining = to_number(short_final.get('absNotionalUsdt'), 0.0)
    if long_remaining > 1e-6:
        followup_lines.append(
            f'{long_symbol} still holds {long_remaining:.2f} USDT ({long_final.get("side")}).'
        )
    if short_remaining > 1e-6:
        followup_lines.append(
            f'{short_symbol} still holds {short_remaining:.2f} USDT ({short_final.get("side")}).'
        )
    if followup_lines:
        if long_remaining > 1e-6 and short_remaining <= 1e-6:
            followup_lines.append(
                'You now hold a naked long leg with no hedge. Consider closing it '
                'or confirming it matches your intent.'
            )
        elif short_remaining > 1e-6 and long_remaining <= 1e-6:
            followup_lines.append(
                'You now hold a naked short leg with no hedge. Consider closing it '
                'or confirming it matches your intent.'
            )
        followup_hint = ' '.join(followup_lines)
    else:
        followup_hint = 'Both legs are flat. Pair spread closed cleanly.'

    return {
        'ok': not any_error,
        'message': ' '.join(message_parts),
        'longLeg': {
            'fill': long_result,
            'error': long_error,
            'finalPosition': long_final,
        },
        'shortLeg': {
            'fill': short_result,
            'error': short_error,
            'finalPosition': short_final,
        },
        'combinedUnrealizedPnlUsdt': compute_combined_pnl(long_final, short_final),
        'followupHint': followup_hint,
    }


# ===========================================================================
# CLI command handlers
# ===========================================================================
CONFIG_TEMPLATE_INIT_HINT = (
    'Fill in your OKX API credentials (api_key, secret_key, passphrase) '
    'for the demo and/or live profile, then run doctor to verify.'
)


def print_json(value: Any) -> None:
    sys.stdout.write(json.dumps(value, indent=2, default=str) + '\n')
    sys.stdout.flush()


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
        'message': f'Config template created at {config_path}. {CONFIG_TEMPLATE_INIT_HINT}',
    })
    return 0


def _doctor_failure_body(
    profile_name: str,
    config_path: Path,
    python_version: str,
    message: str,
    long_inst: Optional[str],
    short_inst: Optional[str],
) -> dict:
    return {
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
        'longSymbolChecked': long_inst,
        'longSymbolTradable': False,
        'shortSymbolChecked': short_inst,
        'shortSymbolTradable': False,
        'leverage': None,
        'marginMode': 'unknown',
        'warnings': [],
        'message': message,
    }


def run_doctor(args: argparse.Namespace) -> int:
    profile_name = normalize_profile_name(args.profile or 'demo')
    long_inst = (args.longInstId or '').strip() or None
    short_inst = (args.shortInstId or '').strip() or None
    config_path = get_okx_config_path()
    python_version = '.'.join(str(v) for v in sys.version_info[:3])
    warnings: list[str] = []

    try:
        document = read_okx_config(config_path)
        profiles = document.get('profiles') or {}
        if profile_name not in profiles:
            print_json(_doctor_failure_body(
                profile_name, config_path, python_version,
                f'Profile "{profile_name}" was not found in {config_path}.',
                long_inst, short_inst,
            ))
            return 1
        credentials = load_okx_profile(profile_name, config_path)
        adapter = OkxTradingAdapter(credentials)
        try:
            connection = adapter.test_connection()
            long_normalized = None
            long_tradable = False
            if long_inst:
                long_normalized = adapter.normalize_symbol(long_inst)
                adapter.get_symbol_trading_rules(long_normalized)
                long_tradable = True
            short_normalized = None
            short_tradable = False
            if short_inst:
                short_normalized = adapter.normalize_symbol(short_inst)
                adapter.get_symbol_trading_rules(short_normalized)
                short_tradable = True
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
                'longSymbolChecked': long_normalized or long_inst,
                'longSymbolTradable': long_tradable,
                'shortSymbolChecked': short_normalized or short_inst,
                'shortSymbolTradable': short_tradable,
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
                or (long_inst and not long_tradable)
                or (short_inst and not short_tradable)
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


def run_status(args: argparse.Namespace) -> int:
    # Reuse open-input validation for basic field checks (just profile + symbols).
    profile = normalize_profile_name(args.profile or 'demo')
    if not args.longInstId or not args.shortInstId:
        raise ValueError('status requires --longInstId and --shortInstId.')
    long_inst = args.longInstId.strip()
    short_inst = args.shortInstId.strip()
    if long_inst.upper() == short_inst.upper():
        raise ValueError('longInstId and shortInstId must be different instruments.')
    input_data = {
        'profile': profile,
        'longInstId': long_inst,
        'shortInstId': short_inst,
    }
    credentials = load_okx_profile(profile)
    adapter = OkxTradingAdapter(credentials)
    try:
        # Normalize symbols via the adapter so reports use canonical IDs.
        input_data['longInstId'] = adapter.normalize_symbol(long_inst)
        input_data['shortInstId'] = adapter.normalize_symbol(short_inst)
        status = build_pair_status(adapter, input_data)
        print_json(status)
        return 0
    finally:
        try:
            adapter.close()
        except Exception:
            pass


def run_open_preview(args: argparse.Namespace) -> int:
    input_data = parse_open_input(args)
    credentials = load_okx_profile(input_data['profile'])
    adapter = OkxTradingAdapter(credentials)
    try:
        input_data['longInstId'] = adapter.normalize_symbol(input_data['longInstId'])
        input_data['shortInstId'] = adapter.normalize_symbol(input_data['shortInstId'])
        preview = build_open_preview(adapter, input_data)
        print_json({
            'profile': input_data['profile'],
            'longInstId': preview['longLeg']['symbol'],
            'shortInstId': preview['shortLeg']['symbol'],
            'entryNotionalUsdtPerLeg': input_data['entryNotionalUsdtPerLeg'],
            'targetLeverage': input_data.get('targetLeverage'),
            'preview': preview,
            'validation': preview['validation'],
        })
        if not preview['canStart']:
            return 1
        return 0
    finally:
        try:
            adapter.close()
        except Exception:
            pass


def run_open(args: argparse.Namespace) -> int:
    input_data = parse_open_input(args)
    credentials = load_okx_profile(input_data['profile'])
    adapter = OkxTradingAdapter(credentials)
    try:
        input_data['longInstId'] = adapter.normalize_symbol(input_data['longInstId'])
        input_data['shortInstId'] = adapter.normalize_symbol(input_data['shortInstId'])
        preview = build_open_preview(adapter, input_data)
        if not preview['canStart']:
            print_json({
                'ok': False,
                'profile': input_data['profile'],
                'preview': preview,
                'message': preview.get('blockReason') or 'Pair open blocked by preview.',
            })
            return 1
        result = execute_open(adapter, input_data, preview)
        response = {
            'profile': input_data['profile'],
            'longInstId': preview['longLeg']['symbol'],
            'shortInstId': preview['shortLeg']['symbol'],
            'entryNotionalUsdtPerLeg': input_data['entryNotionalUsdtPerLeg'],
            'targetLeverage': input_data.get('targetLeverage'),
            'result': result,
        }
        print_json(response)
        return 0 if result.get('ok') else 1
    finally:
        try:
            adapter.close()
        except Exception:
            pass


def run_close_preview(args: argparse.Namespace) -> int:
    input_data = parse_close_input(args)
    credentials = load_okx_profile(input_data['profile'])
    adapter = OkxTradingAdapter(credentials)
    try:
        input_data['longInstId'] = adapter.normalize_symbol(input_data['longInstId'])
        input_data['shortInstId'] = adapter.normalize_symbol(input_data['shortInstId'])
        preview = build_close_preview(adapter, input_data)
        print_json({
            'profile': input_data['profile'],
            'longInstId': preview['longLeg']['symbol'],
            'shortInstId': preview['shortLeg']['symbol'],
            'closeAll': input_data['closeAll'],
            'longCloseUsdt': input_data['longCloseUsdt'],
            'shortCloseUsdt': input_data['shortCloseUsdt'],
            'preview': preview,
        })
        if not preview['canClose']:
            return 1
        return 0
    finally:
        try:
            adapter.close()
        except Exception:
            pass


def run_close(args: argparse.Namespace) -> int:
    input_data = parse_close_input(args)
    credentials = load_okx_profile(input_data['profile'])
    adapter = OkxTradingAdapter(credentials)
    try:
        input_data['longInstId'] = adapter.normalize_symbol(input_data['longInstId'])
        input_data['shortInstId'] = adapter.normalize_symbol(input_data['shortInstId'])
        preview = build_close_preview(adapter, input_data)
        if not preview['canClose']:
            print_json({
                'ok': False,
                'profile': input_data['profile'],
                'preview': preview,
                'message': preview.get('blockReason') or 'Pair close blocked by preview.',
            })
            return 1
        result = execute_close(adapter, input_data, preview)
        response = {
            'profile': input_data['profile'],
            'longInstId': preview['longLeg']['symbol'],
            'shortInstId': preview['shortLeg']['symbol'],
            'closeAll': input_data['closeAll'],
            'longCloseUsdt': input_data['longCloseUsdt'],
            'shortCloseUsdt': input_data['shortCloseUsdt'],
            'result': result,
        }
        print_json(response)
        return 0 if result.get('ok') else 1
    finally:
        try:
            adapter.close()
        except Exception:
            pass


# ===========================================================================
# Watch manager (background stop-loss / take-profit daemon)
# ===========================================================================
#
# A watch is a detached background Python process that polls pair positions
# at fixed intervals and fires a reduce-only close when any user-defined
# trigger is hit. State lives in ~/.okx/watches/<watchId>.{json,log}.
#
# The agent (Claude, OpenClaw, etc.) orchestrates watches via four CLI
# commands: watch-start, watch-list, watch-stop, watch-status. A fifth
# hidden subcommand `_watch-run` is the daemon body — only invoked by
# watch-start's subprocess spawn.
#
# Detachment survives the parent shell / agent closing. The daemon dies
# when the machine shuts down, the user logs out, or the OS kills it.
# It is NOT a system service and does NOT restart on crash.
# ---------------------------------------------------------------------------

WATCH_MIN_POLL_SECONDS = 1.0
WATCH_MAX_POLL_SECONDS = 60.0
WATCH_DEFAULT_POLL_SECONDS = 1.0
WATCH_MAX_CONSECUTIVE_ERRORS = 5
WATCH_STOP_WAIT_SECONDS = 10.0
WATCH_LIST_DEFAULT_HISTORY = 20


def watches_dir() -> Path:
    return Path.home() / '.okx' / 'watches'


def watch_state_path(watch_id: str) -> Path:
    return watches_dir() / f'{watch_id}.json'


def watch_log_path(watch_id: str) -> Path:
    return watches_dir() / f'{watch_id}.log'


def watch_stop_flag_path(watch_id: str) -> Path:
    return watches_dir() / f'{watch_id}.stop'


def new_watch_id() -> str:
    import secrets
    stamp = datetime.now(timezone.utc).strftime('%Y%m%d-%H%M%S')
    suffix = secrets.token_hex(3)
    return f'watch-{stamp}-{suffix}'


def load_watch_state(path: Path) -> dict:
    with open(path, 'r', encoding='utf-8') as f:
        return json.load(f)


def save_watch_state(path: Path, state: dict) -> None:
    """Atomic write: tmp file + rename."""
    ensure_parent_dir(path)
    tmp_path = path.with_suffix(path.suffix + '.tmp')
    with open(tmp_path, 'w', encoding='utf-8') as f:
        json.dump(state, f, indent=2, default=str)
        f.flush()
        try:
            os.fsync(f.fileno())
        except Exception:
            pass
    os.replace(tmp_path, path)


def append_watch_log(log_path: Path, message: str) -> None:
    try:
        ensure_parent_dir(log_path)
        with open(log_path, 'a', encoding='utf-8') as f:
            f.write(f'[{now_iso()}] {message}\n')
    except Exception:
        pass  # logging must never crash the daemon


def pid_alive(pid: int) -> bool:
    if not pid or pid <= 0:
        return False
    try:
        if sys.platform == 'win32':
            import ctypes
            PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
            STILL_ACTIVE = 259
            handle = ctypes.windll.kernel32.OpenProcess(
                PROCESS_QUERY_LIMITED_INFORMATION, False, int(pid)
            )
            if not handle:
                return False
            try:
                exit_code = ctypes.c_ulong()
                ok = ctypes.windll.kernel32.GetExitCodeProcess(
                    handle, ctypes.byref(exit_code)
                )
                if not ok:
                    return False
                return exit_code.value == STILL_ACTIVE
            finally:
                ctypes.windll.kernel32.CloseHandle(handle)
        else:
            os.kill(pid, 0)
            return True
    except (OSError, ProcessLookupError, PermissionError):
        return False


def reconcile_watch_status(state: dict) -> dict:
    """If stored status is 'running' but pid is dead, flip to 'crashed'."""
    if state.get('status') == 'running':
        pid = state.get('pid', 0)
        if pid and not pid_alive(int(pid)):
            state['status'] = 'crashed'
            state['endReason'] = state.get('endReason') or 'process died without updating state'
            state['stoppedAt'] = state.get('stoppedAt') or now_iso()
    return state


def trigger_hit(trigger: dict, ratio: Optional[float], pnl: Optional[float]) -> bool:
    kind = trigger.get('kind')
    op = trigger.get('op')
    threshold = trigger.get('value')
    if threshold is None:
        return False
    if kind == 'ratio':
        current = ratio
    elif kind == 'pnl':
        current = pnl
    else:
        return False
    if current is None:
        return False
    try:
        threshold = float(threshold)
        current = float(current)
    except (TypeError, ValueError):
        return False
    if op == 'lte':
        return current <= threshold
    if op == 'gte':
        return current >= threshold
    return False


def describe_trigger(trigger: dict) -> str:
    sym = {'lte': '<=', 'gte': '>='}.get(trigger.get('op', ''), '?')
    return f'{trigger.get("kind")} {sym} {trigger.get("value")}'


def describe_action(action: dict) -> str:
    if action.get('type') == 'closeAll':
        return 'closeAll (both legs)'
    parts = []
    if action.get('longCloseUsdt'):
        parts.append(f'long {action["longCloseUsdt"]} USDT')
    if action.get('shortCloseUsdt'):
        parts.append(f'short {action["shortCloseUsdt"]} USDT')
    return 'partial: ' + ', '.join(parts) if parts else 'partial: (empty)'


def parse_watch_start_input(args: argparse.Namespace) -> dict:
    profile = normalize_profile_name(args.profile or 'demo')
    if not args.longInstId:
        raise ValueError('--longInstId is required.')
    if not args.shortInstId:
        raise ValueError('--shortInstId is required.')
    long_inst = args.longInstId.strip()
    short_inst = args.shortInstId.strip()
    if long_inst.upper() == short_inst.upper():
        raise ValueError('longInstId and shortInstId must be different instruments.')

    poll_raw = args.pollIntervalSeconds
    if poll_raw is None or (isinstance(poll_raw, str) and not poll_raw.strip()):
        poll = WATCH_DEFAULT_POLL_SECONDS
    else:
        try:
            poll = float(poll_raw)
        except (TypeError, ValueError):
            raise ValueError(f'--pollIntervalSeconds must be a number, got: {poll_raw!r}')
    if not math.isfinite(poll):
        raise ValueError('--pollIntervalSeconds must be finite.')
    if poll < WATCH_MIN_POLL_SECONDS:
        raise ValueError(
            f'--pollIntervalSeconds must be >= {WATCH_MIN_POLL_SECONDS} seconds.'
        )
    if poll > WATCH_MAX_POLL_SECONDS:
        raise ValueError(
            f'--pollIntervalSeconds must be <= {WATCH_MAX_POLL_SECONDS} seconds.'
        )

    triggers: list[dict] = []
    tid = 0

    def add_trigger(kind: str, op: str, value: Optional[float]) -> None:
        nonlocal tid
        if value is None:
            return
        tid += 1
        triggers.append({
            'id': f'T{tid}',
            'kind': kind,
            'op': op,
            'value': float(value),
        })

    ratio_lte = _optional_positive_number('--ratioStopLte', args.ratioStopLte)
    ratio_gte = _optional_positive_number('--ratioStopGte', args.ratioStopGte)
    # PnL can be negative, so use a looser parser
    def parse_number_flag(flag: str, raw: Any) -> Optional[float]:
        if raw is None or (isinstance(raw, str) and not raw.strip()):
            return None
        try:
            v = float(raw)
        except (TypeError, ValueError):
            raise ValueError(f'{flag} must be a number, got: {raw!r}')
        if not math.isfinite(v):
            raise ValueError(f'{flag} must be a finite number, got: {raw!r}')
        return v

    pnl_lte = parse_number_flag('--pnlStopLte', args.pnlStopLte)
    pnl_gte = parse_number_flag('--pnlStopGte', args.pnlStopGte)

    add_trigger('ratio', 'lte', ratio_lte)
    add_trigger('ratio', 'gte', ratio_gte)
    add_trigger('pnl', 'lte', pnl_lte)
    add_trigger('pnl', 'gte', pnl_gte)

    if not triggers:
        raise ValueError(
            'At least one trigger is required: --ratioStopLte / --ratioStopGte / '
            '--pnlStopLte / --pnlStopGte.'
        )

    long_close = _optional_positive_number('--longCloseUsdt', args.longCloseUsdt)
    short_close = _optional_positive_number('--shortCloseUsdt', args.shortCloseUsdt)

    if long_close is None and short_close is None:
        action = {'type': 'closeAll'}
    else:
        action = {
            'type': 'partial',
            'longCloseUsdt': long_close,
            'shortCloseUsdt': short_close,
        }

    return {
        'profile': profile,
        'longInstId': long_inst,
        'shortInstId': short_inst,
        'pollIntervalSeconds': poll,
        'triggers': triggers,
        'action': action,
    }


def run_watch_start(args: argparse.Namespace) -> int:
    cfg = parse_watch_start_input(args)
    credentials = load_okx_profile(cfg['profile'])
    adapter = OkxTradingAdapter(credentials)
    try:
        long_inst = adapter.normalize_symbol(cfg['longInstId'])
        short_inst = adapter.normalize_symbol(cfg['shortInstId'])
        cfg['longInstId'] = long_inst
        cfg['shortInstId'] = short_inst
        # Preflight: must have matching pair positions
        status = build_pair_status(adapter, {
            'profile': cfg['profile'],
            'longInstId': long_inst,
            'shortInstId': short_inst,
        })
        if status['longLeg']['side'] == 'FLAT' or status['shortLeg']['side'] == 'FLAT':
            print_json({
                'ok': False,
                'message': (
                    'Cannot start watch: one or both legs are FLAT. Open the pair first.'
                ),
                'status': status,
            })
            return 1
        if not status['directionMatches']:
            print_json({
                'ok': False,
                'message': (
                    'Cannot start watch: direction mismatch. Swap --longInstId / '
                    '--shortInstId.'
                ),
                'status': status,
            })
            return 1
        # Preflight: check if any trigger is already satisfied
        already_hit = [
            t for t in cfg['triggers']
            if trigger_hit(
                t, status['currentRatio'], status['combinedUnrealizedPnlUsdt']
            )
        ]
    finally:
        try:
            adapter.close()
        except Exception:
            pass

    if already_hit:
        print_json({
            'ok': False,
            'message': (
                'Cannot start watch: one or more triggers are already satisfied at '
                'start time. Re-check your thresholds, close the pair manually, or '
                'wait for the market to move.'
            ),
            'alreadyHit': [
                {
                    'trigger': t,
                    'describe': describe_trigger(t),
                } for t in already_hit
            ],
            'currentRatio': status['currentRatio'],
            'combinedUnrealizedPnlUsdt': status['combinedUnrealizedPnlUsdt'],
        })
        return 1

    # Write initial state file
    watch_id = new_watch_id()
    state_path = watch_state_path(watch_id)
    log_path = watch_log_path(watch_id)
    ensure_parent_dir(state_path)

    initial_state = {
        'watchId': watch_id,
        'pid': 0,
        'parentPid': os.getpid(),
        'startedAt': now_iso(),
        'stoppedAt': None,
        'profile': cfg['profile'],
        'longInstId': long_inst,
        'shortInstId': short_inst,
        'pollIntervalSeconds': cfg['pollIntervalSeconds'],
        'triggers': cfg['triggers'],
        'action': cfg['action'],
        'status': 'starting',
        'lastTickAt': None,
        'lastTick': {
            'currentRatio': status['currentRatio'],
            'combinedUnrealizedPnlUsdt': status['combinedUnrealizedPnlUsdt'],
            'longSide': status['longLeg']['side'],
            'shortSide': status['shortLeg']['side'],
            'longNotionalUsdt': status['longLeg']['absNotionalUsdt'],
            'shortNotionalUsdt': status['shortLeg']['absNotionalUsdt'],
        },
        'tickCount': 0,
        'errorCount': 0,
        'consecutiveErrorCount': 0,
        'lastError': None,
        'firedTrigger': None,
        'closeResult': None,
        'endReason': None,
    }
    save_watch_state(state_path, initial_state)
    # Defensive: clear any stale sentinel from a previous incarnation
    try:
        watch_stop_flag_path(watch_id).unlink()
    except FileNotFoundError:
        pass
    except Exception:
        pass
    append_watch_log(log_path, f'watch created: {watch_id}')
    append_watch_log(
        log_path,
        f'triggers: ' + '; '.join(f'{t["id"]}={describe_trigger(t)}' for t in cfg['triggers']),
    )
    append_watch_log(log_path, f'action: {describe_action(cfg["action"])}')
    append_watch_log(log_path, f'poll interval: {cfg["pollIntervalSeconds"]}s')

    # Spawn detached child
    import subprocess
    script_path = os.path.abspath(__file__)
    child_cmd = [
        sys.executable,
        script_path,
        '_watch-run',
        '--watchId', watch_id,
    ]
    popen_kwargs = dict(
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        close_fds=True,
    )
    if sys.platform == 'win32':
        DETACHED_PROCESS = 0x00000008
        CREATE_NEW_PROCESS_GROUP = 0x00000200
        CREATE_NO_WINDOW = 0x08000000
        popen_kwargs['creationflags'] = (
            DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP | CREATE_NO_WINDOW
        )
    else:
        popen_kwargs['start_new_session'] = True

    try:
        child = subprocess.Popen(child_cmd, **popen_kwargs)
    except Exception as spawn_err:
        initial_state['status'] = 'crashed'
        initial_state['endReason'] = f'failed to spawn daemon: {spawn_err}'
        initial_state['stoppedAt'] = now_iso()
        save_watch_state(state_path, initial_state)
        append_watch_log(log_path, f'SPAWN FAILED: {spawn_err}')
        print_json({'ok': False, 'message': str(spawn_err), 'watchId': watch_id})
        return 1

    append_watch_log(log_path, f'spawned daemon pid={child.pid}')

    print_json({
        'ok': True,
        'watchId': watch_id,
        'pid': child.pid,
        'statePath': str(state_path),
        'logPath': str(log_path),
        'profile': cfg['profile'],
        'longInstId': long_inst,
        'shortInstId': short_inst,
        'pollIntervalSeconds': cfg['pollIntervalSeconds'],
        'triggers': cfg['triggers'],
        'action': cfg['action'],
        'baselineRatio': status['currentRatio'],
        'baselinePnl': status['combinedUnrealizedPnlUsdt'],
        'message': (
            f'Watch {watch_id} started. Use watch-list to monitor, '
            f'watch-stop --watchId {watch_id} to cancel.'
        ),
    })
    return 0


def run_watch_list(args: argparse.Namespace) -> int:
    dir_path = watches_dir()
    if not dir_path.exists():
        print_json({'count': 0, 'watches': []})
        return 0
    entries = []
    for path in sorted(dir_path.glob('watch-*.json')):
        try:
            state = load_watch_state(path)
        except Exception as err:
            entries.append({
                'watchId': path.stem,
                'status': 'unreadable',
                'error': str(err),
            })
            continue
        state = reconcile_watch_status(state)
        # Persist the reconciled status so subsequent lists are consistent
        if state.get('status') == 'crashed':
            try:
                save_watch_state(path, state)
            except Exception:
                pass
        pid = state.get('pid', 0) or 0
        entries.append({
            'watchId': state.get('watchId'),
            'status': state.get('status'),
            'profile': state.get('profile'),
            'longInstId': state.get('longInstId'),
            'shortInstId': state.get('shortInstId'),
            'startedAt': state.get('startedAt'),
            'stoppedAt': state.get('stoppedAt'),
            'lastTickAt': state.get('lastTickAt'),
            'tickCount': state.get('tickCount', 0),
            'pollIntervalSeconds': state.get('pollIntervalSeconds'),
            'triggerCount': len(state.get('triggers') or []),
            'pid': pid,
            'pidAlive': pid_alive(int(pid)) if pid else False,
            'lastRatio': (state.get('lastTick') or {}).get('currentRatio'),
            'lastPnl': (state.get('lastTick') or {}).get('combinedUnrealizedPnlUsdt'),
            'firedTrigger': (state.get('firedTrigger') or {}).get('id'),
            'endReason': state.get('endReason'),
        })
    # Order: running first, then most-recent ended
    def sort_key(e):
        status = e.get('status') or ''
        is_running = 0 if status == 'running' else 1
        return (is_running, -(now_ms() - 0) if is_running == 0 else 0, e.get('stoppedAt') or '', e.get('startedAt') or '')
    entries.sort(
        key=lambda e: (
            0 if e.get('status') == 'running' else 1,
            -(int(datetime.fromisoformat((e.get('startedAt') or '1970-01-01T00:00:00.000Z').replace('Z', '+00:00')).timestamp()) if e.get('startedAt') else 0),
        )
    )
    running = [e for e in entries if e.get('status') == 'running']
    print_json({
        'count': len(entries),
        'runningCount': len(running),
        'watches': entries,
    })
    return 0


def run_watch_status(args: argparse.Namespace) -> int:
    if not args.watchId:
        raise ValueError('--watchId is required.')
    path = watch_state_path(args.watchId.strip())
    if not path.exists():
        print_json({'ok': False, 'message': f'Watch {args.watchId} not found.'})
        return 1
    state = load_watch_state(path)
    state = reconcile_watch_status(state)
    if state.get('status') == 'crashed':
        try:
            save_watch_state(path, state)
        except Exception:
            pass
    # Tail last N log lines for convenience
    log_path = watch_log_path(args.watchId.strip())
    tail: list[str] = []
    if log_path.exists():
        try:
            with open(log_path, 'r', encoding='utf-8') as f:
                lines = f.readlines()
                tail = [ln.rstrip('\n') for ln in lines[-20:]]
        except Exception:
            tail = []
    print_json({
        'ok': True,
        'state': state,
        'logTail': tail,
        'statePath': str(path),
        'logPath': str(log_path),
    })
    return 0


def run_watch_stop(args: argparse.Namespace) -> int:
    if not args.watchId:
        raise ValueError('--watchId is required.')
    watch_id = args.watchId.strip()
    path = watch_state_path(watch_id)
    if not path.exists():
        print_json({'ok': False, 'message': f'Watch {watch_id} not found.'})
        return 1
    state = load_watch_state(path)
    state = reconcile_watch_status(state)
    if state.get('status') != 'running':
        print_json({
            'ok': True,
            'watchId': watch_id,
            'status': state.get('status'),
            'message': f'Watch was not running (status={state.get("status")}); nothing to stop.',
            'state': state,
        })
        return 0
    # Signal stop via a sentinel file. Do NOT mutate the state JSON here:
    # the daemon owns that file and would race with this writer.
    flag_path = watch_stop_flag_path(watch_id)
    try:
        ensure_parent_dir(flag_path)
        flag_path.touch()
    except Exception as err:
        print_json({'ok': False, 'message': f'failed to create stop flag: {err}'})
        return 1
    append_watch_log(watch_log_path(watch_id), 'stop requested by watch-stop command')
    # Wait up to WATCH_STOP_WAIT_SECONDS for daemon to update status
    deadline = time.time() + WATCH_STOP_WAIT_SECONDS
    final = state
    while time.time() < deadline:
        time.sleep(0.2)
        try:
            current = load_watch_state(path)
        except Exception:
            continue
        if current.get('status') != 'running':
            final = current
            break
        # If daemon died without updating, reconcile
        pid = current.get('pid', 0) or 0
        if pid and not pid_alive(int(pid)):
            current = reconcile_watch_status(current)
            save_watch_state(path, current)
            final = current
            break
    else:
        # Daemon did not honor stop. Try to kill hard.
        pid = state.get('pid', 0) or 0
        if pid and pid_alive(int(pid)):
            try:
                if sys.platform == 'win32':
                    import ctypes
                    PROCESS_TERMINATE = 0x0001
                    h = ctypes.windll.kernel32.OpenProcess(PROCESS_TERMINATE, False, int(pid))
                    if h:
                        ctypes.windll.kernel32.TerminateProcess(h, 1)
                        ctypes.windll.kernel32.CloseHandle(h)
                else:
                    import signal
                    os.kill(int(pid), signal.SIGTERM)
            except Exception:
                pass
            append_watch_log(watch_log_path(watch_id), f'force-killed pid={pid}')
        try:
            final = load_watch_state(path)
        except Exception:
            pass
        final = reconcile_watch_status(final)
        final['endReason'] = final.get('endReason') or 'force-killed after stop timeout'
        final['status'] = 'stopped' if final.get('status') == 'running' else final.get('status')
        final['stoppedAt'] = final.get('stoppedAt') or now_iso()
        save_watch_state(path, final)

    # Sentinel served its purpose; remove it so it won't haunt anything later.
    try:
        flag_path.unlink()
    except FileNotFoundError:
        pass
    except Exception:
        pass

    print_json({
        'ok': True,
        'watchId': watch_id,
        'status': final.get('status'),
        'endReason': final.get('endReason'),
        'closeResult': final.get('closeResult'),
        'state': final,
    })
    return 0


def run_watch_daemon(args: argparse.Namespace) -> int:
    """Daemon body — invoked as a subprocess by watch-start. Not user-facing."""
    watch_id = args.watchId.strip() if args.watchId else ''
    if not watch_id:
        return 1
    state_path = watch_state_path(watch_id)
    log_path = watch_log_path(watch_id)

    def log(msg: str) -> None:
        append_watch_log(log_path, msg)

    try:
        state = load_watch_state(state_path)
    except Exception as err:
        log(f'FATAL: cannot load state file: {err}')
        return 1

    state['pid'] = os.getpid()
    state['status'] = 'running'
    save_watch_state(state_path, state)
    log(f'daemon running pid={os.getpid()}')

    try:
        credentials = load_okx_profile(state['profile'])
        adapter = OkxTradingAdapter(credentials)
    except Exception as err:
        state['status'] = 'crashed'
        state['endReason'] = f'adapter init failed: {err}'
        state['stoppedAt'] = now_iso()
        state['lastError'] = str(err)
        save_watch_state(state_path, state)
        log(f'FATAL: adapter init: {err}')
        return 1

    try:
        long_inst = state['longInstId']
        short_inst = state['shortInstId']
        try:
            long_inst = adapter.normalize_symbol(long_inst)
            short_inst = adapter.normalize_symbol(short_inst)
        except Exception as err:
            state['status'] = 'crashed'
            state['endReason'] = f'symbol normalization failed: {err}'
            state['stoppedAt'] = now_iso()
            state['lastError'] = str(err)
            save_watch_state(state_path, state)
            log(f'FATAL: symbol normalize: {err}')
            return 1

        input_data = {
            'profile': state['profile'],
            'longInstId': long_inst,
            'shortInstId': short_inst,
        }
        poll_seconds = float(state.get('pollIntervalSeconds') or WATCH_DEFAULT_POLL_SECONDS)
        consecutive_errors = 0

        stop_flag = watch_stop_flag_path(watch_id)

        while True:
            # Check stop sentinel file. We do NOT reload state JSON here:
            # the parent's watch-stop only touches the sentinel, never writes
            # state, so there is no race with our save_watch_state() calls.
            if stop_flag.exists():
                state['status'] = 'stopped'
                state['endReason'] = 'stop_requested'
                state['stoppedAt'] = now_iso()
                save_watch_state(state_path, state)
                log('stop flag detected, exiting cleanly')
                return 0

            try:
                status = build_pair_status(adapter, input_data)
                consecutive_errors = 0
            except Exception as err:
                consecutive_errors += 1
                state['errorCount'] = int(state.get('errorCount', 0)) + 1
                state['consecutiveErrorCount'] = consecutive_errors
                state['lastError'] = str(err)
                save_watch_state(state_path, state)
                log(f'tick error {consecutive_errors}/{WATCH_MAX_CONSECUTIVE_ERRORS}: {err}')
                if consecutive_errors >= WATCH_MAX_CONSECUTIVE_ERRORS:
                    state['status'] = 'errored'
                    state['endReason'] = (
                        f'{WATCH_MAX_CONSECUTIVE_ERRORS} consecutive errors'
                    )
                    state['stoppedAt'] = now_iso()
                    save_watch_state(state_path, state)
                    log('too many consecutive errors, exiting')
                    return 1
                time.sleep(poll_seconds)
                continue

            ratio = status.get('currentRatio')
            pnl = status.get('combinedUnrealizedPnlUsdt')
            long_side = status.get('longLeg', {}).get('side')
            short_side = status.get('shortLeg', {}).get('side')

            state['tickCount'] = int(state.get('tickCount', 0)) + 1
            state['lastTickAt'] = now_iso()
            state['lastTick'] = {
                'currentRatio': ratio,
                'combinedUnrealizedPnlUsdt': pnl,
                'longSide': long_side,
                'shortSide': short_side,
                'longNotionalUsdt': status.get('longLeg', {}).get('absNotionalUsdt'),
                'shortNotionalUsdt': status.get('shortLeg', {}).get('absNotionalUsdt'),
            }
            state['consecutiveErrorCount'] = 0

            if long_side == 'FLAT' and short_side == 'FLAT':
                state['status'] = 'positions_flat'
                state['endReason'] = 'both legs flat (closed externally)'
                state['stoppedAt'] = now_iso()
                save_watch_state(state_path, state)
                log('positions flat, exiting')
                return 0

            fired: Optional[dict] = None
            for trigger in state.get('triggers', []):
                if trigger_hit(trigger, ratio, pnl):
                    fired = trigger
                    break

            if fired is None:
                save_watch_state(state_path, state)
                log(
                    f'tick #{state["tickCount"]} ratio={ratio} pnl={pnl} no-hit'
                )
                time.sleep(poll_seconds)
                continue

            # Trigger fired — execute close atomically
            log(
                f'TRIGGER FIRE {fired["id"]}: {describe_trigger(fired)} '
                f'(actual ratio={ratio} pnl={pnl})'
            )
            state['firedTrigger'] = dict(fired, actualRatio=ratio, actualPnl=pnl)
            save_watch_state(state_path, state)

            action = state.get('action') or {'type': 'closeAll'}
            close_input = {
                'profile': state['profile'],
                'longInstId': long_inst,
                'shortInstId': short_inst,
                'closeAll': action.get('type') == 'closeAll',
                'longCloseUsdt': action.get('longCloseUsdt'),
                'shortCloseUsdt': action.get('shortCloseUsdt'),
            }
            try:
                close_preview = build_close_preview(adapter, close_input)
                if not close_preview.get('canClose'):
                    raise RuntimeError(
                        f'close preview blocked: {close_preview.get("blockReason")}'
                    )
                close_result = execute_close(adapter, close_input, close_preview)
                state['closeResult'] = close_result
                if close_result.get('ok'):
                    state['status'] = 'triggered'
                    state['endReason'] = (
                        f'trigger {fired["id"]} fired, close executed'
                    )
                else:
                    state['status'] = 'errored'
                    state['endReason'] = (
                        f'trigger {fired["id"]} fired but close returned not-ok: '
                        f'{close_result.get("message") or close_result.get("endReason")}'
                    )
                state['stoppedAt'] = now_iso()
                save_watch_state(state_path, state)
                log(
                    f'close executed: ok={close_result.get("ok")} '
                    f'endReason={close_result.get("endReason")}'
                )
                return 0 if close_result.get('ok') else 1
            except Exception as err:
                state['status'] = 'errored'
                state['endReason'] = f'trigger {fired["id"]} fired but close failed: {err}'
                state['lastError'] = str(err)
                state['stoppedAt'] = now_iso()
                save_watch_state(state_path, state)
                log(f'CLOSE FAILED: {err}')
                return 1
    except Exception as err:
        try:
            state['status'] = 'crashed'
            state['endReason'] = f'uncaught: {err}'
            state['lastError'] = str(err) + '\n' + traceback.format_exc()
            state['stoppedAt'] = now_iso()
            save_watch_state(state_path, state)
        except Exception:
            pass
        log(f'CRASHED: {err}\n{traceback.format_exc()}')
        return 1
    finally:
        try:
            adapter.close()
        except Exception:
            pass


# ===========================================================================
# Entry point
# ===========================================================================
def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog='okx-pair-spread',
        description='OKX Pair Spread - Synchronous pair open/close for USDT perpetuals',
    )
    subparsers = parser.add_subparsers(dest='command')

    subparsers.add_parser('init', help='Create ~/.okx/config.toml template.')

    doctor = subparsers.add_parser('doctor', help='Verify credentials and both symbols.')
    doctor.add_argument('--profile', default='demo')
    doctor.add_argument('--longInstId', default=None)
    doctor.add_argument('--shortInstId', default=None)

    status = subparsers.add_parser('status', help='Read live pair positions and ratio.')
    status.add_argument('--profile', default='demo')
    status.add_argument('--longInstId', default=None)
    status.add_argument('--shortInstId', default=None)

    open_fields = [
        ('--profile', 'demo'),
        ('--longInstId', None),
        ('--shortInstId', None),
        ('--entryNotionalUsdtPerLeg', None),
        ('--targetLeverage', None),
    ]
    for name in ('open-preview', 'open'):
        sub = subparsers.add_parser(name)
        for flag, default in open_fields:
            sub.add_argument(flag, default=default)

    close_fields = [
        ('--profile', 'demo'),
        ('--longInstId', None),
        ('--shortInstId', None),
        ('--longCloseUsdt', None),
        ('--shortCloseUsdt', None),
    ]
    for name in ('close-preview', 'close'):
        sub = subparsers.add_parser(name)
        for flag, default in close_fields:
            sub.add_argument(flag, default=default)
        sub.add_argument('--closeAll', action='store_true')

    # ---- Watch (background stop-loss / take-profit daemon) ----
    watch_start = subparsers.add_parser(
        'watch-start',
        help='Start a background watch daemon that closes on trigger hit.',
    )
    watch_start.add_argument('--profile', default='demo')
    watch_start.add_argument('--longInstId', default=None)
    watch_start.add_argument('--shortInstId', default=None)
    watch_start.add_argument('--pollIntervalSeconds', default=None)
    watch_start.add_argument('--ratioStopLte', default=None)
    watch_start.add_argument('--ratioStopGte', default=None)
    watch_start.add_argument('--pnlStopLte', default=None)
    watch_start.add_argument('--pnlStopGte', default=None)
    watch_start.add_argument('--longCloseUsdt', default=None)
    watch_start.add_argument('--shortCloseUsdt', default=None)

    watch_list = subparsers.add_parser(
        'watch-list',
        help='List active and recent watches.',
    )
    watch_list.add_argument('--profile', default=None)

    watch_stop = subparsers.add_parser(
        'watch-stop',
        help='Stop a running watch by ID.',
    )
    watch_stop.add_argument('--watchId', required=True)

    watch_status = subparsers.add_parser(
        'watch-status',
        help='Show full state + log tail for a specific watch.',
    )
    watch_status.add_argument('--watchId', required=True)

    # Hidden daemon subcommand — only invoked by watch-start as a subprocess.
    watch_run = subparsers.add_parser('_watch-run')
    watch_run.add_argument('--watchId', required=True)

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
        if command == 'status':
            return run_status(args)
        if command == 'open-preview':
            return run_open_preview(args)
        if command == 'open':
            return run_open(args)
        if command == 'close-preview':
            return run_close_preview(args)
        if command == 'close':
            return run_close(args)
        if command == 'watch-start':
            return run_watch_start(args)
        if command == 'watch-list':
            return run_watch_list(args)
        if command == 'watch-stop':
            return run_watch_stop(args)
        if command == 'watch-status':
            return run_watch_status(args)
        if command == '_watch-run':
            return run_watch_daemon(args)
        print_json({
            'ok': False,
            'message': (
                'Usage: okx_pair_spread.py <init|doctor|status|open-preview|open|'
                'close-preview|close|watch-start|watch-list|watch-stop|watch-status> '
                '[options]'
            ),
        })
        return 1
    except Exception as error:
        print_json({'ok': False, 'message': str(error), 'trace': traceback.format_exc()})
        return 1


if __name__ == '__main__':
    sys.exit(main())

