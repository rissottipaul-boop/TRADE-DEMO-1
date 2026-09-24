"""OKX crypto position sizer for spot and perpetual swap trades.

Calculates risk-based position sizes using Fixed Fractional, ATR-based,
or Kelly Criterion methods. Supports long/short positions with leverage,
OKX contract value normalization, and liquidation price estimation.
Outputs directly usable --sz values for okx CLI.
"""

from __future__ import annotations

import argparse
import json
import math
import os
import sys
from dataclasses import dataclass
from datetime import datetime


@dataclass
class SizingParameters:
    account_size: float
    entry_price: float | None = None
    stop_price: float | None = None
    risk_pct: float | None = None
    atr: float | None = None
    atr_multiplier: float = 2.0
    win_rate: float | None = None
    avg_win: float | None = None
    avg_loss: float | None = None
    max_position_pct: float | None = None
    # Crypto-specific
    side: str = "long"
    leverage: int = 1
    mmr: float = 0.004
    fee_rate: float = 0.0005
    contract_value: float | None = None
    inst_type: str = "swap"
    lot_size: float | None = None
    max_contracts: int | None = None
    inst_id: str | None = None


def validate_parameters(params: SizingParameters) -> None:
    if params.account_size <= 0:
        raise ValueError("account_size must be positive")
    if params.side not in ("long", "short"):
        raise ValueError("side must be 'long' or 'short'")
    if params.inst_type not in ("spot", "swap"):
        raise ValueError("inst_type must be 'spot' or 'swap'")
    if params.inst_type == "spot" and params.side == "short":
        raise ValueError("Spot does not support short positions. Use swap (perpetual) for shorting.")
    if params.inst_type == "spot" and params.leverage > 1:
        raise ValueError("Spot does not support leverage. Use swap (perpetual) for leveraged trading.")
    if params.entry_price is not None and params.entry_price <= 0:
        raise ValueError("entry_price must be positive")
    if params.stop_price is not None and params.entry_price is not None:
        if params.side == "long" and params.stop_price >= params.entry_price:
            raise ValueError("For long positions, stop_price must be below entry_price")
        if params.side == "short" and params.stop_price <= params.entry_price:
            raise ValueError("For short positions, stop_price must be above entry_price")
    if params.risk_pct is not None and params.risk_pct <= 0:
        raise ValueError("risk_pct must be positive")
    if params.atr is not None and params.atr <= 0:
        raise ValueError("atr must be positive")
    if params.leverage < 1:
        raise ValueError("leverage must be >= 1")
    if params.mmr < 0 or params.mmr >= 1:
        raise ValueError("mmr must be between 0 and 1")
    if params.fee_rate < 0 or params.fee_rate >= 1:
        raise ValueError("fee_rate must be between 0 and 1")
    if params.inst_type == "swap" and params.contract_value is None:
        # Allow Kelly budget mode without contract_value
        is_kelly_budget = params.win_rate is not None and params.entry_price is None
        if not is_kelly_budget:
            raise ValueError("contract_value is required for swap instruments (use --contract-value)")
    if params.contract_value is not None and params.contract_value <= 0:
        raise ValueError("contract_value must be positive")
    if params.win_rate is not None:
        if params.win_rate <= 0 or params.win_rate > 1.0:
            raise ValueError("win_rate must be between 0 (exclusive) and 1.0 (inclusive)")
    if params.avg_win is not None and params.avg_win <= 0:
        raise ValueError("avg_win must be positive")
    if params.avg_loss is not None and params.avg_loss <= 0:
        raise ValueError("avg_loss must be positive")


def floor_to_precision(value: float, precision: float) -> float:
    """Floor a value to the given precision step (e.g., lotSz).

    Uses decimal-aware rounding to avoid floating point artifacts
    like floor(0.3 / 0.1) * 0.1 producing 0.2.
    """
    if precision <= 0:
        return value
    # Use fixed-point notation to avoid scientific notation (e.g., 0.00001 → "1e-05")
    precision_str = f"{precision:.15f}".rstrip("0")
    if "." in precision_str:
        decimals = len(precision_str.split(".")[1])
    else:
        decimals = 0
    return round(math.floor(value / precision) * precision, decimals)


def calculate_quantity(dollar_risk: float, risk_per_unit: float,
                       params: SizingParameters) -> tuple[float, str]:
    """Calculate position quantity based on instrument type.

    Returns (quantity, unit_label).
    For swap: integer contracts, directly usable as --sz.
    For spot: base currency amount floored to precision.
    """
    if params.inst_type == "swap":
        risk_per_contract = risk_per_unit * params.contract_value
        if risk_per_contract <= 0:
            return 0, "contracts"
        contracts = int(dollar_risk / risk_per_contract)
        return contracts, "contracts"
    else:
        if risk_per_unit <= 0:
            return 0.0, "units"
        units = dollar_risk / risk_per_unit
        if params.lot_size:
            units = floor_to_precision(units, params.lot_size)
        elif params.contract_value:
            units = floor_to_precision(units, params.contract_value)
        else:
            units = math.floor(units * 1e8) / 1e8
        return units, "units"


def calculate_fixed_fractional(params: SizingParameters) -> dict:
    """Fixed fractional position sizing.

    risk_per_unit = abs(entry - stop), direction-agnostic.
    """
    risk_per_unit = abs(params.entry_price - params.stop_price)
    dollar_risk = params.account_size * params.risk_pct / 100
    quantity, unit_label = calculate_quantity(dollar_risk, risk_per_unit, params)
    return {
        "method": "fixed_fractional",
        "quantity": quantity,
        "unit_label": unit_label,
        "risk_per_unit": round(risk_per_unit, 2),
        "dollar_risk": round(dollar_risk, 2),
        "stop_price": params.stop_price,
        "side": params.side,
    }


def calculate_atr_based(params: SizingParameters) -> dict:
    """ATR-based position sizing with directional stop."""
    stop_distance = params.atr * params.atr_multiplier
    if params.side == "long":
        stop_price = round(params.entry_price - stop_distance, 2)
    else:
        stop_price = round(params.entry_price + stop_distance, 2)

    risk_per_unit = stop_distance
    dollar_risk = params.account_size * params.risk_pct / 100
    quantity, unit_label = calculate_quantity(dollar_risk, risk_per_unit, params)
    return {
        "method": "atr_based",
        "quantity": quantity,
        "unit_label": unit_label,
        "risk_per_unit": round(risk_per_unit, 2),
        "dollar_risk": round(dollar_risk, 2),
        "stop_price": stop_price,
        "side": params.side,
        "atr": params.atr,
        "atr_multiplier": params.atr_multiplier,
    }


def calculate_kelly(params: SizingParameters) -> dict:
    """Kelly Criterion calculation with leverage adjustment.

    Kelly % = W - (1-W)/R
    Half Kelly = kelly_pct / 2
    Leverage-adjusted half Kelly = half_kelly / leverage
    """
    w = params.win_rate
    r = params.avg_win / params.avg_loss
    kelly_pct = max(0.0, w - (1 - w) / r) * 100
    half_kelly_pct = kelly_pct / 2

    result = {
        "method": "kelly",
        "kelly_pct": round(kelly_pct, 2),
        "half_kelly_pct": round(half_kelly_pct, 2),
    }

    if params.leverage > 1:
        adjusted = half_kelly_pct / params.leverage
        result["leverage_adjusted_half_kelly_pct"] = round(adjusted, 2)
        result["leverage_warning"] = (
            f"Kelly fraction divided by {params.leverage}x leverage. "
            "Leverage amplifies both gains and losses."
        )

    return result


def calculate_liquidation_price(
    entry: float, contracts: float, contract_value: float,
    leverage: int, mmr: float, fee_rate: float, side: str,
) -> dict:
    """OKX isolated margin liquidation price estimation.

    Long:  liq = (margin - ctVal * |N| * entry) / (ctVal * |N| * (mmr + fee - 1))
    Short: liq = (margin + ctVal * |N| * entry) / (ctVal * |N| * (mmr + fee + 1))

    margin_balance = ctVal * |N| * entry / leverage (initial margin, no adjustments)
    """
    n = abs(contracts)
    if n == 0 or contract_value <= 0:
        return {"liquidation_price": None, "margin_mode": "isolated"}

    notional = contract_value * n * entry
    margin_balance = notional / leverage
    denom_base = contract_value * n

    if side == "long":
        denom = denom_base * (mmr + fee_rate - 1)
        if denom == 0:
            return {"liquidation_price": None, "margin_mode": "isolated"}
        liq = (margin_balance - denom_base * entry) / denom
    else:
        denom = denom_base * (mmr + fee_rate + 1)
        if denom == 0:
            return {"liquidation_price": None, "margin_mode": "isolated"}
        liq = (margin_balance + denom_base * entry) / denom

    liq = round(max(0, liq), 2)

    warnings = []
    warnings.append("Isolated margin estimate. Tier 1 (small position) defaults used.")
    warnings.append("Cross margin mode: liquidation price fluctuates with all positions' PnL.")
    warnings.append("Large positions have higher tiered MMR — actual liquidation price will be closer.")

    return {
        "liquidation_price": liq,
        "margin_mode": "isolated",
        "margin_required": round(margin_balance, 2),
        "notional": round(notional, 2),
        "warnings": warnings,
    }


def check_liquidation_vs_stop(liq_price: float | None, stop_price: float | None, side: str) -> str | None:
    """Check if stop-loss is beyond liquidation price (position will be liquidated first)."""
    if liq_price is None or stop_price is None:
        return None
    if side == "long" and stop_price < liq_price:
        return (
            f"CRITICAL: Stop ${stop_price:,.2f} is beyond liquidation ${liq_price:,.2f}! "
            "Position will be liquidated before stop triggers. "
            "Reduce leverage or tighten stop-loss."
        )
    if side == "short" and stop_price > liq_price:
        return (
            f"CRITICAL: Stop ${stop_price:,.2f} is beyond liquidation ${liq_price:,.2f}! "
            "Position will be liquidated before stop triggers. "
            "Reduce leverage or tighten stop-loss."
        )
    return None


def apply_constraints(quantity: float, params: SizingParameters) -> tuple[float, list[dict], str | None]:
    """Apply portfolio constraints. Strictest (minimum) wins."""
    constraints: list[dict] = []
    candidates = [quantity]
    binding: str | None = None

    if params.max_position_pct is not None and params.entry_price:
        if params.inst_type == "swap" and params.contract_value:
            max_value = params.account_size * params.max_position_pct / 100
            max_by_pos = int(max_value / (params.entry_price * params.contract_value))
        else:
            max_by_pos = params.account_size * params.max_position_pct / 100 / params.entry_price
            if params.lot_size:
                max_by_pos = floor_to_precision(max_by_pos, params.lot_size)
            elif params.contract_value:
                max_by_pos = floor_to_precision(max_by_pos, params.contract_value)
        constraints.append({
            "type": "max_position_pct",
            "limit": params.max_position_pct,
            "max_quantity": max_by_pos,
            "binding": False,
        })
        candidates.append(max_by_pos)

    if params.max_contracts is not None and params.inst_type == "swap":
        constraints.append({
            "type": "max_contracts",
            "limit": params.max_contracts,
            "max_quantity": params.max_contracts,
            "binding": False,
        })
        candidates.append(params.max_contracts)

    final = max(0, min(candidates)) if candidates else 0
    if params.inst_type == "swap":
        final = int(final)

    for c in constraints:
        if c["max_quantity"] == final and final < quantity:
            c["binding"] = True
            binding = c["type"]

    return final, constraints, binding


def calculate_position(params: SizingParameters) -> dict:
    """Main calculation entry point."""
    validate_parameters(params)

    is_kelly_mode = params.win_rate is not None
    has_entry = params.entry_price is not None

    result: dict = {
        "schema_version": "2.0",
        "parameters": {},
        "side": params.side,
        "inst_type": params.inst_type,
        "leverage": params.leverage,
    }
    if params.inst_id:
        result["inst_id"] = params.inst_id

    # Kelly budget mode (no entry price)
    if is_kelly_mode and not has_entry:
        kelly = calculate_kelly(params)
        result["mode"] = "budget"
        result["parameters"] = {
            "win_rate": params.win_rate,
            "avg_win": params.avg_win,
            "avg_loss": params.avg_loss,
            "account_size": params.account_size,
            "leverage": params.leverage,
        }
        result["calculations"] = {
            "kelly": kelly,
            "fixed_fractional": None,
            "atr_based": None,
        }
        effective_kelly = kelly["half_kelly_pct"]
        if params.leverage > 1 and "leverage_adjusted_half_kelly_pct" in kelly:
            effective_kelly = kelly["leverage_adjusted_half_kelly_pct"]
        budget = params.account_size * effective_kelly / 100
        result["recommended_risk_budget"] = round(budget, 2)
        result["recommended_risk_budget_pct"] = effective_kelly
        result["note"] = "To calculate contracts/units, re-run with --entry and --stop"
        return result

    # Position mode
    unit_label = "contracts" if params.inst_type == "swap" else "units"
    result["mode"] = unit_label
    result["parameters"] = {
        "entry_price": params.entry_price,
        "account_size": params.account_size,
        "side": params.side,
        "inst_type": params.inst_type,
        "leverage": params.leverage,
    }
    if params.contract_value:
        result["parameters"]["contract_value"] = params.contract_value

    calculations: dict = {
        "fixed_fractional": None,
        "atr_based": None,
        "kelly": None,
    }
    risk_quantity = 0
    stop_price = params.stop_price

    if is_kelly_mode:
        kelly = calculate_kelly(params)
        calculations["kelly"] = kelly
        effective_kelly = kelly["half_kelly_pct"]
        if params.leverage > 1 and "leverage_adjusted_half_kelly_pct" in kelly:
            effective_kelly = kelly["leverage_adjusted_half_kelly_pct"]
        budget = params.account_size * effective_kelly / 100
        if params.stop_price:
            risk_per_unit = abs(params.entry_price - params.stop_price)
            risk_quantity, _ = calculate_quantity(budget, risk_per_unit, params)
        elif params.contract_value:
            risk_quantity = int(budget / (params.entry_price * params.contract_value))
            result["kelly_no_stop_warning"] = (
                "No stop-loss specified. Contracts calculated from Kelly budget / notional per contract. "
                "Actual risk is undefined without a stop — your entire margin is at risk up to liquidation. "
                "Re-run with --stop for precise risk control."
            )
        else:
            risk_quantity = budget / params.entry_price
            result["kelly_no_stop_warning"] = (
                "No stop-loss specified. Units calculated from Kelly budget / entry price. "
                "Actual risk is undefined without a stop — your entire margin is at risk up to liquidation. "
                "Re-run with --stop for precise risk control."
            )
    elif params.atr is not None:
        atr_result = calculate_atr_based(params)
        calculations["atr_based"] = atr_result
        risk_quantity = atr_result["quantity"]
        stop_price = atr_result["stop_price"]
        result["parameters"]["risk_pct"] = params.risk_pct
    else:
        ff_result = calculate_fixed_fractional(params)
        calculations["fixed_fractional"] = ff_result
        risk_quantity = ff_result["quantity"]
        result["parameters"]["risk_pct"] = params.risk_pct

    result["parameters"]["stop_price"] = stop_price
    result["calculations"] = calculations

    # Apply constraints
    final_quantity, constraints, binding = apply_constraints(risk_quantity, params)
    result["constraints_applied"] = constraints
    result["final_quantity"] = final_quantity
    result["unit_label"] = unit_label

    # Notional and risk calculations
    if params.inst_type == "swap" and params.contract_value:
        notional = final_quantity * params.entry_price * params.contract_value
    else:
        notional = final_quantity * params.entry_price
    result["final_notional"] = round(notional, 2)

    # Margin (swap only — at 1x margin equals notional)
    if params.inst_type == "swap":
        result["margin_required"] = round(notional / params.leverage, 2)

    # Risk dollars
    if stop_price:
        risk_per_unit = abs(params.entry_price - stop_price)
        if params.inst_type == "swap" and params.contract_value:
            final_risk = final_quantity * risk_per_unit * params.contract_value
        else:
            final_risk = final_quantity * risk_per_unit
        result["final_risk_dollars"] = round(final_risk, 2)
        result["final_risk_pct"] = round(final_risk / params.account_size * 100, 2)
    elif params.atr:
        risk_per_unit = params.atr * params.atr_multiplier
        if params.inst_type == "swap" and params.contract_value:
            final_risk = final_quantity * risk_per_unit * params.contract_value
        else:
            final_risk = final_quantity * risk_per_unit
        result["final_risk_dollars"] = round(final_risk, 2)
        result["final_risk_pct"] = round(final_risk / params.account_size * 100, 2)
    else:
        result["final_risk_dollars"] = None
        result["final_risk_pct"] = None

    result["binding_constraint"] = binding

    # Zero quantity warning
    if final_quantity == 0:
        result["zero_quantity_warning"] = (
            "Calculated quantity is 0. Account size or risk budget is too small "
            "for this instrument at current entry/stop distance. "
            "Consider: increase account size, widen risk %, or use a lower-priced instrument."
        )

    # Liquidation price (swap with leverage only)
    if params.inst_type == "swap" and params.leverage > 1 and params.contract_value and final_quantity > 0:
        liq_result = calculate_liquidation_price(
            params.entry_price, final_quantity, params.contract_value,
            params.leverage, params.mmr, params.fee_rate, params.side,
        )
        result["liquidation"] = liq_result

        # Check stop vs liquidation
        if liq_result["liquidation_price"] and stop_price:
            warning = check_liquidation_vs_stop(
                liq_result["liquidation_price"], stop_price, params.side
            )
            if warning:
                result["liquidation_critical_warning"] = warning

    return result


def generate_markdown_report(result: dict) -> str:
    """Generate a markdown report from the calculation result."""
    lines = [
        "# Crypto Position Sizing Report",
        f"**Generated:** {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}",
    ]
    if result.get("inst_id"):
        lines.append(f"**Instrument:** {result['inst_id']}")
    lines += [
        f"**Mode:** {result['mode']}",
        f"**Side:** {result.get('side', 'long')}",
        f"**Instrument Type:** {result.get('inst_type', 'swap')}",
        "",
        "## Parameters",
    ]
    for k, v in result.get("parameters", {}).items():
        lines.append(f"- **{k}:** {v}")
    lines.append("")

    if result["mode"] == "budget":
        lines.append("## Kelly Criterion")
        kelly = result["calculations"]["kelly"]
        lines.append(f"- Full Kelly: {kelly['kelly_pct']}%")
        lines.append(f"- Half Kelly: {kelly['half_kelly_pct']}%")
        if "leverage_adjusted_half_kelly_pct" in kelly:
            lines.append(f"- Leverage-adjusted Half Kelly: {kelly['leverage_adjusted_half_kelly_pct']}%")
            lines.append(f"- {kelly['leverage_warning']}")
        lines.append(f"- **Recommended Risk Budget:** ${result['recommended_risk_budget']:,.2f}")
        lines.append(f"  ({result['recommended_risk_budget_pct']}% of account)")
        lines.append("")
        lines.append(f"*{result['note']}*")
    else:
        lines.append("## Calculations")
        for method, calc in result.get("calculations", {}).items():
            if calc:
                lines.append(f"### {method.replace('_', ' ').title()}")
                for k, v in calc.items():
                    if k != "method":
                        lines.append(f"- {k}: {v}")
                lines.append("")

        if result.get("constraints_applied"):
            lines.append("## Constraints")
            for c in result["constraints_applied"]:
                binding_label = " **[BINDING]**" if c.get("binding") else ""
                lines.append(f"- {c['type']}: limit={c.get('limit', 'N/A')}, "
                             f"max_quantity={c['max_quantity']}{binding_label}")
            lines.append("")

        lines.append("## Final Recommendation")
        unit = result.get("unit_label", "contracts")
        lines.append(f"- **{unit.title()}:** {result['final_quantity']}")
        lines.append(f"- **Side:** {result.get('side', 'long').title()}")
        if result.get("leverage", 1) > 1:
            lines.append(f"- **Leverage:** {result['leverage']}x")
        lines.append(f"- **Notional:** ${result['final_notional']:,.2f}")
        if result.get("margin_required"):
            lines.append(f"- **Margin Required:** ${result['margin_required']:,.2f}")
        if result.get("final_risk_dollars") is not None:
            lines.append(f"- **Risk:** ${result['final_risk_dollars']:,.2f} "
                         f"({result['final_risk_pct']}%)")
        if result.get("binding_constraint"):
            lines.append(f"- **Binding Constraint:** {result['binding_constraint']}")

        # Liquidation section
        if result.get("liquidation"):
            liq = result["liquidation"]
            lines.append("")
            lines.append("## Liquidation Estimate")
            if liq.get("liquidation_price"):
                lines.append(f"- **Est. Liquidation Price:** ${liq['liquidation_price']:,.2f} "
                             f"({liq['margin_mode']})")
            if liq.get("margin_required"):
                lines.append(f"- **Margin:** ${liq['margin_required']:,.2f}")
            if liq.get("warnings"):
                lines.append("")
                for w in liq["warnings"]:
                    lines.append(f"  > {w}")

        if result.get("liquidation_critical_warning"):
            lines.append("")
            lines.append(f"**{result['liquidation_critical_warning']}**")

        if result.get("zero_quantity_warning"):
            lines.append("")
            lines.append(f"**WARNING:** {result['zero_quantity_warning']}")

        if result.get("kelly_no_stop_warning"):
            lines.append("")
            lines.append(f"> **Warning:** {result['kelly_no_stop_warning']}")

    return "\n".join(lines) + "\n"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Calculate risk-based position sizes for OKX crypto trades (spot + swap)"
    )
    # Account
    parser.add_argument("--account-size", type=float, required=True,
                        help="Total account equity in USDT")
    # Trade params
    parser.add_argument("--entry", type=float, help="Entry price")
    parser.add_argument("--stop", type=float, help="Stop-loss price")
    parser.add_argument("--risk-pct", type=float,
                        help="Risk percentage per trade (e.g., 1.0 for 1%%)")
    parser.add_argument("--atr", type=float, help="ATR value (from okx market indicator)")
    parser.add_argument("--atr-multiplier", type=float, default=2.0,
                        help="ATR multiplier for stop distance (default: 2.0)")
    # Kelly
    parser.add_argument("--win-rate", type=float,
                        help="Historical win rate (0-1) for Kelly criterion")
    parser.add_argument("--avg-win", type=float, help="Average win amount for Kelly")
    parser.add_argument("--avg-loss", type=float, help="Average loss amount for Kelly")
    # Crypto-specific
    parser.add_argument("--side", choices=["long", "short"], default="long",
                        help="Trade direction (default: long)")
    parser.add_argument("--leverage", type=int, default=1,
                        help="Leverage multiplier (default: 1, range varies by instrument)")
    parser.add_argument("--mmr", type=float, default=0.004,
                        help="Maintenance margin rate (default: 0.004 = 0.4%%, Tier 1)")
    parser.add_argument("--fee-rate", type=float, default=0.0005,
                        help="Taker fee rate (default: 0.0005 = 0.05%%, VIP0)")
    parser.add_argument("--contract-value", type=float,
                        help="OKX ctVal per contract (e.g., 0.01 for BTC-USDT-SWAP)")
    parser.add_argument("--inst-type", choices=["spot", "swap"], default="swap",
                        help="Instrument type (default: swap)")
    parser.add_argument("--lot-size", type=float,
                        help="Minimum order precision for spot (lotSz from instruments API)")
    # Constraints
    parser.add_argument("--max-position-pct", type=float,
                        help="Max single position as %% of account")
    parser.add_argument("--max-contracts", type=int,
                        help="Max orderable quantity (from okx account max-size)")
    parser.add_argument("--inst-id", type=str,
                        help="Instrument ID for reports (e.g., BTC-USDT-SWAP)")
    # Output
    parser.add_argument("--output-dir", type=str, default="reports/",
                        help="Output directory for reports")
    return parser


def main() -> None:
    parser = build_parser()
    args = parser.parse_args()

    # Validate mutual exclusivity
    if args.risk_pct is not None and args.win_rate is not None:
        parser.error("Use either --risk-pct mode OR --win-rate (Kelly) mode, not both")

    # Validate required combinations
    if args.win_rate is not None:
        if args.avg_win is None or args.avg_loss is None:
            parser.error("Kelly mode requires --win-rate, --avg-win, and --avg-loss")
    elif args.risk_pct is not None:
        if args.entry is None:
            parser.error("Risk-pct mode requires --entry")
        if args.stop is None and args.atr is None:
            parser.error("Risk-pct mode requires either --stop or --atr")
    else:
        parser.error("Must specify either --risk-pct or --win-rate mode")

    params = SizingParameters(
        account_size=args.account_size,
        entry_price=args.entry,
        stop_price=args.stop,
        risk_pct=args.risk_pct,
        atr=args.atr,
        atr_multiplier=args.atr_multiplier,
        win_rate=args.win_rate,
        avg_win=args.avg_win,
        avg_loss=args.avg_loss,
        max_position_pct=args.max_position_pct,
        side=args.side,
        leverage=args.leverage,
        mmr=args.mmr,
        fee_rate=args.fee_rate,
        contract_value=args.contract_value,
        inst_type=args.inst_type,
        lot_size=args.lot_size,
        max_contracts=args.max_contracts,
        inst_id=args.inst_id,
    )

    try:
        result = calculate_position(params)
    except ValueError as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)

    # Output
    os.makedirs(args.output_dir, exist_ok=True)
    timestamp = datetime.now().strftime("%Y-%m-%d_%H%M%S_%f")

    json_path = os.path.join(args.output_dir, f"position_sizer_{timestamp}.json")
    with open(json_path, "w") as f:
        json.dump(result, f, indent=2)
    print(f"JSON report: {json_path}")

    md_report = generate_markdown_report(result)
    md_path = os.path.join(args.output_dir, f"position_sizer_{timestamp}.md")
    with open(md_path, "w") as f:
        f.write(md_report)
    print(f"Markdown report: {md_path}")

    # Summary to stdout
    inst_label = f" [{result.get('inst_id', '')}]" if result.get("inst_id") else ""
    if result["mode"] == "budget":
        print(f"\nRecommended risk budget: ${result['recommended_risk_budget']:,.2f}")
        print(f"({result['recommended_risk_budget_pct']}% of account)")
    else:
        unit = result.get("unit_label", "contracts")
        print(f"\n{result.get('side', 'long').upper()}{inst_label} | {unit}: {result['final_quantity']}")
        print(f"Notional: ${result['final_notional']:,.2f}")
        if result.get("margin_required"):
            print(f"Margin: ${result['margin_required']:,.2f}")
        if result.get("final_risk_dollars") is not None:
            print(f"Risk: ${result['final_risk_dollars']:,.2f} ({result['final_risk_pct']}%)")
        if result.get("liquidation"):
            liq = result["liquidation"]
            if liq.get("liquidation_price"):
                print(f"Est. Liquidation: ${liq['liquidation_price']:,.2f} (isolated)")
        if result.get("liquidation_critical_warning"):
            print(f"\n*** {result['liquidation_critical_warning']}")
        if result.get("binding_constraint"):
            print(f"Binding: {result['binding_constraint']}")
        if result.get("zero_quantity_warning"):
            print(f"\n*** {result['zero_quantity_warning']}")
        if result.get("kelly_no_stop_warning"):
            print(f"\nWarning: {result['kelly_no_stop_warning']}")


if __name__ == "__main__":
    main()
