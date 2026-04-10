"""Investment analyzer helpers shared by subscription endpoints."""

from typing import Any, Dict, List, Optional

from odoo.http import request  

from .subscription_helpers import _normalize_protocol_name


INVESTMENT_ANALYZER_PROTOCOL_ALIASES = {
    "energy-web": "energyweb",
    "energy web": "energyweb",
}

INVESTMENT_ANALYZER_COMPOUNDING_PERIODS = {
    "daily": 1,
    "weekly": 7,
    "monthly": 30,
    "none": None,
}


def _normalize_investment_protocol_key(value: Optional[str]) -> str:
    """Normalize frontend protocol identifiers to a comparable backend key."""
    raw = _normalize_protocol_name(value)
    if not raw:
        return ""
    aliased = INVESTMENT_ANALYZER_PROTOCOL_ALIASES.get(raw, raw)
    return "".join(ch for ch in aliased if ch.isalnum())


def _resolve_investment_protocol_record(protocol_value: Optional[str]):
    """Resolve a protocol.master record for the investment analyzer."""
    normalized_key = _normalize_investment_protocol_key(protocol_value)
    if not normalized_key:
        return request.env["protocol.master"].sudo().browse()

    protocol_model = request.env["protocol.master"].sudo()
    record = protocol_model.search([("protocol_id", "=", normalized_key)], limit=1)
    if record:
        return record

    record = protocol_model.search([("name", "ilike", protocol_value)], limit=1)
    if record:
        return record

    for candidate in protocol_model.search([]):
        candidate_keys = {
            _normalize_investment_protocol_key(candidate.protocol_id),
            _normalize_investment_protocol_key(candidate.name),
        }
        candidate_keys.discard("")
        if normalized_key in candidate_keys:
            return candidate

    return protocol_model.browse()


def _clamp_percentage(value: float) -> float:
    """Clamp a percentage to the inclusive 0-100 range."""
    if value < 0:
        return 0.0
    if value > 100:
        return 100.0
    return value


def _coerce_float(
    value: Any,
    field_name: str,
    *,
    required: bool = True,
    minimum: Optional[float] = None,
    maximum: Optional[float] = None,
    default: Optional[float] = None,
) -> float:
    """Coerce a numeric request field and raise ValueError on invalid input."""
    if value in (None, ""):
        if required:
            raise ValueError(f"{field_name} is required")
        return float(default or 0)

    try:
        number = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{field_name} must be a valid number") from exc

    if minimum is not None and number < minimum:
        raise ValueError(f"{field_name} must be greater than or equal to {minimum}")
    if maximum is not None and number > maximum:
        raise ValueError(f"{field_name} must be less than or equal to {maximum}")
    return number


def _simulate_investment_projection(
    *,
    stake: float,
    price: float,
    commission: float,
    days: int,
    apr: float,
    compounding: str,
    slashing_probability: float = 0.0,
    downtime_percentage: float = 0.0,
    operating_cost_monthly: float = 0.0,
) -> Dict[str, Any]:
    """Simulate risk-adjusted staking returns using the validator-portfolio formula."""
    if stake <= 0:
        raise ValueError("stakeAmount must be greater than 0")
    if price < 0:
        raise ValueError("Token USD price must be non-negative")
    if commission < 0 or commission > 100:
        raise ValueError("commission must be between 0 and 100")
    if apr < 0:
        raise ValueError("apr must be greater than or equal to 0")
    if days <= 0:
        raise ValueError("days must be greater than 0")
    if operating_cost_monthly < 0:
        raise ValueError("operatingCostUsdMonthly must be greater than or equal to 0")

    period = INVESTMENT_ANALYZER_COMPOUNDING_PERIODS.get(compounding)
    if compounding not in INVESTMENT_ANALYZER_COMPOUNDING_PERIODS:
        raise ValueError("compounding must be one of: daily, weekly, monthly")

    slashing_penalty = _clamp_percentage(slashing_probability) / 100.0
    uptime_ratio = 1 - (_clamp_percentage(downtime_percentage) / 100.0)
    daily_rate = apr / 100.0 / 365.0
    operating_cost_per_day = operating_cost_monthly / 30.0

    balance = stake
    pending_rewards = 0.0
    cumulative_rewards = 0.0
    projection: List[Dict[str, Any]] = []

    for index in range(days):
        base_reward = balance * daily_rate * (1 - commission / 100.0)
        reward = base_reward * (1 - slashing_penalty) * uptime_ratio
        pending_rewards += reward
        cumulative_rewards += reward

        if period and (index + 1) % period == 0:
            balance += pending_rewards
            pending_rewards = 0.0

        rewards_usd = cumulative_rewards * price
        costs_usd = operating_cost_per_day * (index + 1)
        projection.append(
            {
                "day": index + 1,
                "tokens": round(cumulative_rewards, 6),
                "rewardsUsd": round(rewards_usd, 2),
                "costsUsd": round(costs_usd, 2),
                "netProfitUsd": round(rewards_usd - costs_usd, 2),
            }
        )

    latest_point = projection[-1]
    effective_apr = apr * (1 - slashing_penalty) * uptime_ratio

    return {
        "effectiveApr": round(effective_apr, 4),
        "estimatedRewardTokens": latest_point["tokens"],
        "estimatedRewardUsd": latest_point["rewardsUsd"],
        "estimatedCostsUsd": latest_point["costsUsd"],
        "netProfitUsd": latest_point["netProfitUsd"],
        "projection": projection,
        "assumptions": {
            "tokenPriceUsd": round(price, 8),
            "costPerDayUsd": round(operating_cost_per_day, 4),
            "network": "mainnet",
            "compounding": compounding,
        },
    }