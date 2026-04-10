"""Helper functions for IOPN validator metrics.

IOPN is a Cosmos-based blockchain with standard REST LCD endpoints.
API endpoints follow the Cosmos SDK pattern for staking and validator queries.
"""

import logging
from decimal import Decimal, InvalidOperation
from typing import Any, Dict, List, Optional

import requests

_logger = logging.getLogger(__name__)

# IOPN uses 18 decimal places (EVM-style).
IOPN_RPC_TIMEOUT = 40
IOPN_DECIMALS = Decimal("1e18")

BECH32_CHARSET = "qpzry9x8gf2tvdw0s3jn54khce6mua7l"
BECH32_CHARSET_MAP = {c: i for i, c in enumerate(BECH32_CHARSET)}
BECH32_GENERATOR = (0x3B6A57B2, 0x26508E6D, 0x1EA119FA, 0x3D4233DD, 0x2A1462B3)


def _call_iopn_lcd(
    base_url: str,
    path: str,
    params: Optional[Dict[str, Any]] = None,
    timeout: int = IOPN_RPC_TIMEOUT,
) -> Dict[str, Any]:
    """Invoke IOPN LCD REST endpoint and return the result payload.
    
    Args:
        base_url: Base URL of IOPN LCD endpoint (e.g., https://mainnet-rpc2.iopn.tech)
        path: API path (e.g., /cosmos/staking/v1beta1/validators)
        params: Query parameters dict
        timeout: Request timeout in seconds
        
    Returns:
        Parsed JSON response as dict
        
    Raises:
        LCDRequestError: On HTTP errors or parsing failures
    """
    from .subscription_helpers import LCDRequestError
    rpc_url = (base_url or "").strip().rstrip("/")
    if not rpc_url:
        raise LCDRequestError("IOPN RPC endpoint is not configured")
    
    # Build full URL. IOPN LCD can use two different path structures:
    # 1. Public IOPN mainnet: /blockchain/cosmos/... (e.g., https://mainnet-rpc2.iopn.tech)
    # 2. Private/custom chains: /api/cosmos/... (e.g., https://val4.autheo.testnet.zeeve.net/api)
    # 
    # Only prepend /blockchain/ for public IOPN (no /api/ path and no /blockchain already present)
    full_path = path.lstrip("/")
    base_has_blockchain = "/blockchain" in rpc_url.rstrip("/")
    base_has_api_path = "/api/" in rpc_url or rpc_url.endswith("/api")
    
    # For public IOPN without /blockchain or /api, prepend blockchain/
    # For private chains with /api, skip blockchain prefix
    if full_path.startswith("cosmos/") and not base_has_blockchain and not base_has_api_path:
        full_path = f"blockchain/{full_path}"
    url = f"{rpc_url}/{full_path}"
    
    try:
        response = requests.get(url, params=params, timeout=timeout)
    except requests.RequestException as exc:
        raise LCDRequestError(f"Failed to reach IOPN LCD: {exc}") from exc
    
    status = response.status_code
    
    # Handle 404 as a special case
    if status == 404:
        raise LCDRequestError("Validator not found", status=404)
    
    try:
        response.raise_for_status()
    except requests.HTTPError as exc:
        body_snippet = response.text[:200] if response.text else ""
        raise LCDRequestError(
            f"IOPN LCD request failed ({status}): {body_snippet}",
            status=status,
        ) from exc
    
    try:
        data = response.json()
    except ValueError as exc:
        raise LCDRequestError("Invalid JSON received from IOPN LCD") from exc
    
    return data or {}


def _convertbits(data: bytes, from_bits: int, to_bits: int, pad: bool = True) -> Optional[List[int]]:
    """General power-of-2 base conversion (adapted from BIP-0173 reference)."""
    acc = 0
    bits = 0
    ret: List[int] = []
    maxv = (1 << to_bits) - 1
    for value in data:
        if value < 0 or (value >> from_bits):
            return None
        acc = (acc << from_bits) | value
        bits += from_bits
        while bits >= to_bits:
            bits -= to_bits
            ret.append((acc >> bits) & maxv)
    if pad:
        if bits:
            ret.append((acc << (to_bits - bits)) & maxv)
    elif bits >= from_bits or ((acc << (to_bits - bits)) & maxv):
        return None
    return ret


def _bech32_polymod(values: List[int]) -> int:
    chk = 1
    for v in values:
        b = chk >> 25
        chk = (chk & 0x1FFFFFF) << 5 ^ v
        for i in range(5):
            chk ^= BECH32_GENERATOR[i] if (b >> i) & 1 else 0
    return chk


def _bech32_hrp_expand(hrp: str) -> List[int]:
    return [ord(x) >> 5 for x in hrp] + [0] + [ord(x) & 31 for x in hrp]


def _bech32_create_checksum(hrp: str, data: List[int]) -> List[int]:
    values = _bech32_hrp_expand(hrp) + data
    polymod = _bech32_polymod(values + [0, 0, 0, 0, 0, 0]) ^ 1
    return [(polymod >> 5 * (5 - i)) & 31 for i in range(6)]


def _bech32_encode(hrp: str, data: List[int]) -> Optional[str]:
    combined = data + _bech32_create_checksum(hrp, data)
    try:
        return hrp + "1" + "".join(BECH32_CHARSET[d] for d in combined)
    except Exception:
        return None


def _hex_to_iopn_valoper(address: str) -> Optional[str]:
    """Convert a 0x-prefixed hex address to iopnvaloper bech32 if possible."""
    if not address or not address.startswith("0x"):
        return address
    hex_part = address[2:]
    try:
        raw = bytes.fromhex(hex_part)
    except ValueError:
        return address
    five_bit = _convertbits(raw, 8, 5)
    if not five_bit:
        return address
    encoded = _bech32_encode("iopnvaloper", five_bit)
    return encoded or address


def _hex_to_iopn_delegator(address: str) -> Optional[str]:
    """Convert a valoper address (or 0x-prefixed hex) to iopn delegator bech32."""
    # First convert to valoper if needed
    valoper = _hex_to_iopn_valoper(address)
    if not valoper:
        return None
    
    # Now convert valoper to delegator using bech32 decode/encode
    from .subscription_helpers import _bech32_decode, _bech32_encode
    
    hrp, data = _bech32_decode(valoper)
    if not hrp or data is None:
        return None
    
    # Convert iopnvaloper -> iopn
    if hrp == "iopnvaloper":
        return _bech32_encode("iopn", data)
    
    return None


def _iopn_fetch_validator(
    valoper: str,
    base_url: str,
) -> Optional[Dict[str, Any]]:
    """Fetch a single IOPN validator definition by operator address.
    
    Args:
        valoper: Validator operator address (e.g., iopnvaloper1...)
        base_url: IOPN LCD base URL
        
    Returns:
        Validator object or None if not found
    """
    from .subscription_helpers import LCDRequestError
    normalized = _hex_to_iopn_valoper(valoper)
    try:
        result = _call_iopn_lcd(
            base_url,
            f"/cosmos/staking/v1beta1/validators/{normalized}",
        )
        return result.get("validator") if result else None
    except LCDRequestError as exc:
        if exc.status == 404:
            return None
        raise


def _iopn_fetch_staking_pool(base_url: str) -> Optional[int]:
    """Fetch total bonded tokens from staking pool."""
    try:
        payload = _call_iopn_lcd(base_url, "/cosmos/staking/v1beta1/pool")
        pool = payload.get("pool") or {}
        bonded = pool.get("bonded_tokens")
        return int(bonded) if bonded is not None else None
    except Exception:
        return None


def _iopn_fetch_delegations(valoper: str, base_url: str) -> Dict[str, Any]:
    """Fetch delegations with total count and sum of amounts."""
    normalized = _hex_to_iopn_valoper(valoper)
    params = {
        "pagination.limit": 200,
        "pagination.count_total": "true",
    }
    payload = _call_iopn_lcd(
        base_url,
        f"/cosmos/staking/v1beta1/validators/{normalized}/delegations",
        params=params,
    )
    delegations = payload.get("delegation_responses") or []
    total_count = 0
    pagination = payload.get("pagination") or {}
    total_count = int(pagination.get("total") or len(delegations))

    total_amount = 0
    for delegation in delegations:
        balance = delegation.get("balance") or {}
        total_amount += int(balance.get("amount") or 0)

    return {
        "delegations": delegations,
        "total_count": total_count,
        "total_amount": total_amount,
    }


def _iopn_reward_snapshot(valoper: str, rpc_base_url: str) -> Dict[str, Any]:
    """Return outstanding rewards, total stake, and delegator count for IOPN.

    Includes a fallback to rpc2 host if the configured base does not respond.
    """
    normalized = _hex_to_iopn_valoper(valoper)

    def _to_iopn(amount: Any) -> float:
        try:
            return float(Decimal(str(amount)) / IOPN_DECIMALS)
        except (InvalidOperation, ZeroDivisionError):
            return 0.0

    def _with_fallback(call_fn):
        try:
            return call_fn(rpc_base_url)
        except Exception:
            # If base_url is mainnet-rpc, try mainnet-rpc2
            if "mainnet-rpc." in rpc_base_url and "mainnet-rpc2" not in rpc_base_url:
                alt = rpc_base_url.replace("mainnet-rpc.", "mainnet-rpc2.")
                return call_fn(alt)
            raise

    # Outstanding rewards
    outstanding = 0.0
    def _fetch_rewards(base):
        rewards_payload = _call_iopn_lcd(
            base,
            f"/cosmos/distribution/v1beta1/validators/{normalized}/outstanding_rewards",
        )
        rewards_field = rewards_payload.get("rewards")
        if isinstance(rewards_field, dict):
            rewards_field = rewards_field.get("rewards")
        if rewards_field and isinstance(rewards_field, list):
            first = rewards_field[0]
            amount_val = first.get("amount")
            denom = first.get("denom")
            amt = Decimal(str(amount_val)) if amount_val is not None else Decimal(0)
            if denom in {"uiopn", "wei"}:
                return float(amt / IOPN_DECIMALS)
            return float(amt)
        return 0.0

    try:
        outstanding = _with_fallback(_fetch_rewards)
    except Exception:
        outstanding = 0.0

    # Validator tokens
    tokens_main = 0.0
    def _fetch_tokens(base):
        vpayload = _call_iopn_lcd(
            base,
            f"/cosmos/staking/v1beta1/validators/{normalized}",
        )
        validator = vpayload.get("validator") or {}
        tokens_raw = validator.get("tokens")
        return _to_iopn(tokens_raw)

    try:
        tokens_main = _with_fallback(_fetch_tokens)
    except Exception:
        tokens_main = 0.0

    owned_stake = 0.0

    def _fetch_owned_stake(base):
        delegator_addr = _hex_to_iopn_delegator(valoper)
        if not delegator_addr:
            return 0.0

        paths = [
            f"/cosmos/staking/v1beta1/validators/{normalized}/delegations/{delegator_addr}",
            f"/cosmos/staking/v1beta1/delegators/{delegator_addr}/delegations/{normalized}",
        ]
        for path in paths:
            try:
                payload = _call_iopn_lcd(base, path)
            except Exception:
                continue
            delegation_response = payload.get("delegation_response") or payload
            balance = delegation_response.get("balance") if isinstance(delegation_response, dict) else {}
            amount_val = (balance or {}).get("amount") if isinstance(balance, dict) else None
            if amount_val not in (None, ""):
                return _to_iopn(amount_val)
        return 0.0

    try:
        owned_stake = _with_fallback(_fetch_owned_stake)
    except Exception:
        owned_stake = 0.0

    # Delegator count
    delegator_count = 0
    def _fetch_deleg_count(base):
        deleg_meta = _call_iopn_lcd(
            base,
            f"/cosmos/staking/v1beta1/validators/{normalized}/delegations",
            params={"pagination.limit": 1, "pagination.count_total": "true"},
        )
        pagination = deleg_meta.get("pagination") or {}
        return int(pagination.get("total") or 0)

    try:
        delegator_count = _with_fallback(_fetch_deleg_count)
    except Exception:
        delegator_count = 0

    # Calculate total rewards (owned + commission)
    owned_rewards = 0.0
    delegation_rewards = 0.0
    
    def _fetch_owned_rewards(base):
        delegator_addr = _hex_to_iopn_delegator(valoper)
        if not delegator_addr:
            return 0.0
        try:
            owned_payload = _call_iopn_lcd(
                base,
                f"/cosmos/distribution/v1beta1/delegators/{delegator_addr}/rewards/{normalized}",
            )
            rewards_field = owned_payload.get("rewards")
            if rewards_field and isinstance(rewards_field, list):
                first = rewards_field[0]
                amount_val = first.get("amount")
                denom = first.get("denom")
                amt = Decimal(str(amount_val)) if amount_val is not None else Decimal(0)
                if denom in {"uiopn", "wei"}:
                    return float(amt / IOPN_DECIMALS)
                return float(amt)
        except Exception:
            pass
        return 0.0
    
    def _fetch_commission(base):
        try:
            commission_payload = _call_iopn_lcd(
                base,
                f"/cosmos/distribution/v1beta1/validators/{normalized}/commission",
            )
            commission_field = (commission_payload.get("commission") or {}).get("commission")
            if commission_field and isinstance(commission_field, list):
                first = commission_field[0]
                amount_val = first.get("amount")
                denom = first.get("denom")
                amt = Decimal(str(amount_val)) if amount_val is not None else Decimal(0)
                if denom in {"uiopn", "wei"}:
                    return float(amt / IOPN_DECIMALS)
                return float(amt)
        except Exception:
            pass
        return 0.0
    
    try:
        owned_rewards = _with_fallback(_fetch_owned_rewards)
    except Exception:
        owned_rewards = 0.0
    
    try:
        delegation_rewards = _with_fallback(_fetch_commission)
    except Exception:
        delegation_rewards = 0.0
    
    total_rewards = owned_rewards + delegation_rewards

    return {
        "outstanding_rewards": outstanding,
        "total_rewards": total_rewards,
        "tokens": tokens_main,
        "owned_stake": owned_stake,
        "delegator_count": delegator_count,
    }


def _iopn_validator_summary(
    valoper: str,
    rpc_base_url: str,
) -> Dict[str, Any]:
    """Build a normalized summary for IOPN validators.
    
    Fetches validator data from IOPN LCD and transforms it to the standard
    response format used by the subscription API.
    
    Args:
        valoper: Validator operator address
        rpc_base_url: IOPN LCD base URL
        
    Returns:
        Dictionary with validator summary data
        
    Raises:
        LCDRequestError: If validator not found or API error occurs
    """
    from .subscription_helpers import LCDRequestError, _safe_float, _safe_int
    normalized_valoper = _hex_to_iopn_valoper(valoper)
    validator = _iopn_fetch_validator(normalized_valoper, rpc_base_url)
    if not validator:
        raise LCDRequestError("Validator not found", status=404)
    
    # Extract basic validator info
    description = validator.get("description") or {}
    moniker = (description.get("moniker") or "").strip()
    details = (description.get("details") or "").strip()
    email = (description.get("security_contact") or "").strip()
    website = (description.get("website") or "").strip()
    
    # Extract staking amounts (in smallest unit)
    tokens = _safe_int(validator.get("tokens"))
    commission_rate = _safe_float(validator.get("commission", {}).get("commission_rates", {}).get("rate"))
    
    # Convert commission rate from decimal to percentage
    commission_pct = commission_rate * 100 if commission_rate else 0.0
    
    # Get validator status
    jailed = bool(validator.get("jailed", False))
    status = validator.get("status", "BOND_STATUS_UNBONDED")
    
    # Normalize status to active/inactive
    is_active = status == "BOND_STATUS_BONDED"
    status_label = "Active" if is_active else "Inactive"
    
    # Fetch outstanding rewards (cosmos distribution)
    outstanding_rewards = 0.0
    try:
        rewards_payload = _call_iopn_lcd(
            rpc_base_url,
            f"/cosmos/distribution/v1beta1/validators/{normalized_valoper}/outstanding_rewards",
        )
        rewards_field = rewards_payload.get("rewards")
        if isinstance(rewards_field, dict):
            rewards_field = rewards_field.get("rewards")
        if rewards_field and isinstance(rewards_field, list):
            first = rewards_field[0]
            amount_str = first.get("amount")
            denom = first.get("denom")
            amount_val = _safe_float(amount_str)
            if denom in {"uiopn", "wei"}:
                try:
                    amount_val = float(Decimal(str(amount_val)) / IOPN_DECIMALS)
                except (InvalidOperation, ZeroDivisionError):
                    pass
            outstanding_rewards = amount_val
    except Exception:
        outstanding_rewards = 0.0

    # Fetch delegations for count and total delegated stake
    delegations_info = _iopn_fetch_delegations(normalized_valoper, rpc_base_url)
    delegator_count = delegations_info.get("total_count", 0)
    total_delegated = delegations_info.get("total_amount", 0)

    # Owned stake approximated as tokens - delegated (cannot go below 0)
    owned_stake = max(tokens - total_delegated, 0)

    # Calculate owned and delegation rewards based on actual delegator rewards data
    owned_rewards = 0.0
    delegation_rewards = 0.0
    
    # Get validator's own delegator address from the delegations data
    # The validator's self-delegation is the first entry in the delegations response
    delegator_address = None
    if delegations_info.get("delegations"):
        first_delegation = delegations_info.get("delegations")[0]
        raw_delegation = first_delegation.get("delegation") or {}
        delegator_address = raw_delegation.get("delegator_address")
    
    # Try to fetch the validator operator's own delegator rewards
    if delegator_address:
        try:
            delegator_rewards_payload = _call_iopn_lcd(
                rpc_base_url,
                f"/cosmos/distribution/v1beta1/delegators/{delegator_address}/rewards",
            )
            
            # Parse delegator rewards for this validator
            delegator_rewards_list = delegator_rewards_payload.get("rewards") or []
            validator_delegator_reward = None
            
            for reward in delegator_rewards_list:
                if reward.get("validator_address") == normalized_valoper:
                    validator_delegator_reward = reward
                    break
            
            # Extract reward amount for this validator
            if validator_delegator_reward:
                reward_list = validator_delegator_reward.get("reward") or []
                for r in reward_list:
                    if r.get("denom") in {"uiopn", "wei"}:
                        amount_str = r.get("amount", "0")
                        # Handle amounts with decimal notation (e.g., "197224452557900.000000000000000000")
                        # Strip any trailing zeros after decimal to avoid precision issues
                        if "." in amount_str:
                            amount_str = amount_str.split(".")[0]
                        
                        try:
                            amount_val = Decimal(amount_str)
                            owned_rewards = float(amount_val / IOPN_DECIMALS)
                        except (InvalidOperation, ValueError, ZeroDivisionError):
                            owned_rewards = 0.0
                        break
            
            # If we didn't find rewards for this validator, owned_rewards stays 0.0
        except Exception as e:
            _logger.warning(f"Failed to fetch delegator rewards for {delegator_address}: {e}")
            # Fallback: If there's only one delegator (100% self-delegation),
            # assume all rewards minus commission belong to the validator
            if delegator_count == 1:
                owned_rewards = outstanding_rewards
            elif tokens > 0 and owned_stake > 0:
                owned_ratio = owned_stake / tokens
                owned_rewards = outstanding_rewards * owned_ratio
    else:
        # Fallback: calculate proportionally if delegator address not found
        if delegator_count == 1:
            # If only one delegator exists, assume it's self-delegation
            owned_rewards = outstanding_rewards
        elif tokens > 0 and owned_stake > 0:
            owned_ratio = owned_stake / tokens
            owned_rewards = outstanding_rewards * owned_ratio
    
    # Delegation rewards = total outstanding - validator's own rewards
    delegation_rewards = max(outstanding_rewards - owned_rewards, 0.0)
    
    # Calculate voting power percentage
    voting_power_pct = 0.0
    bonded_total = _iopn_fetch_staking_pool(rpc_base_url)
    if bonded_total and bonded_total > 0:
        voting_power_pct = float(tokens) / float(bonded_total) * 100.0
    
    # Calculate uptime percentage from signing info
    uptime_pct = 0.0
    try:
        # Extract consensus pubkey from validator for signing info lookup
        consensus_pubkey = validator.get("consensus_pubkey")
        if consensus_pubkey:
            pubkey_key = consensus_pubkey.get("key") if isinstance(consensus_pubkey, dict) else None
            if pubkey_key:
                consensus_address = _derive_consensus_address(pubkey_key, chain_prefix="iopnvalcons")
                if consensus_address:
                    # Fetch signing info
                    signing_data = _call_iopn_lcd(
                        rpc_base_url,
                        f"/cosmos/slashing/v1beta1/signing_infos/{consensus_address}",
                    )
                    signing_info = signing_data.get('val_signing_info', {})
                    
                    missed_blocks = int(signing_info.get('missed_blocks_counter', 0))
                    
                    # Fetch window size from slashing params
                    window_size = 0
                    try:
                        params_data = _call_iopn_lcd(
                            rpc_base_url,
                            "/cosmos/slashing/v1beta1/params",
                        )
                        params = params_data.get('params', {})
                        window_size = int(params.get('signed_blocks_window', 0))
                    except Exception:
                        pass
                    
                    # Calculate uptime: (produced blocks / total blocks) * 100
                    # produced_blocks = window_size - missed_blocks
                    if window_size > 0:
                        produced_blocks = max(window_size - missed_blocks, 0)
                        uptime_pct = (produced_blocks / window_size) * 100.0
    except Exception as e:
        _logger.warning(f"Failed to calculate uptime for {normalized_valoper}: {e}")
        # Uptime stays 0.0 on any error
    
    # Build summary response matching expected format
    summary = {
        "tokens": str(tokens),
        "ownedStake": str(owned_stake),
        "totalStake": str(tokens),
        "delegatorStake": str(total_delegated),
        "votingPowerPct": voting_power_pct,
        "commissionPct": commission_pct,
        "outstandingRewards": outstanding_rewards,
        "ownedRewards": owned_rewards,
        "delegationRewards": delegation_rewards,
        "totalRewards": outstanding_rewards,
        "status": "active" if is_active else "inactive",
        "statusLabel": status_label,
        "jailed": jailed,
        "connectionStatus": "unknown",  # Connection status not directly available
        "uptimePct": uptime_pct,
        "identity": moniker,
        "moniker": moniker,
        "description": details,
        "email": email,
        "website": website,
        "delegatorCount": delegator_count,
    }

    return summary


def _iopn_validator_delegations(
    valoper: str,
    rpc_base_url: str,
    cursor: Optional[str] = None,
) -> Dict[str, Any]:
    """Return a paginated delegator list for IOPN validators.
    
    Fetches all delegations for a validator and returns them with pagination support.
    
    Args:
        valoper: Validator operator address
        rpc_base_url: IOPN LCD base URL
        cursor: Pagination cursor (offset in this implementation)
        
    Returns:
        Dictionary with delegations list and next cursor
    """
    from .subscription_helpers import LCDRequestError, _pct, _safe_int
    normalized_valoper = _hex_to_iopn_valoper(valoper)
    # Fetch validator to get total stake
    validator = _iopn_fetch_validator(normalized_valoper, rpc_base_url)
    if not validator:
        raise LCDRequestError("Validator not found", status=404)
    
    validator_tokens = _safe_int(validator.get("tokens"))
    
    # Build pagination params
    params: Dict[str, Any] = {
        "pagination.limit": 100,
        "pagination.count_total": "true",
    }
    
    if cursor:
        try:
            offset = int(cursor)
            params["pagination.offset"] = offset
        except (ValueError, TypeError):
            pass
    
    # Fetch delegations
    result = _call_iopn_lcd(
        rpc_base_url,
        f"/cosmos/staking/v1beta1/validators/{normalized_valoper}/delegations",
        params=params,
    )
    
    items: List[Dict[str, Any]] = []
    delegations = result.get("delegation_responses", [])
    
    for delegation in delegations:
        balance = delegation.get("balance") or {}
        amount = _safe_int(balance.get("amount"))
        denom = balance.get("denom") or "IOPN"

        # Normalize denom
        if denom in {"uiopn", "wei"}:
            denom = "IOPN"

        # Calculate percentage of validator's total stake
        pct = _pct(amount, validator_tokens) if validator_tokens > 0 else 0.0

        # Delegator address can be nested under "delegation"
        raw_delegation = delegation.get("delegation") or {}
        delegator_address = raw_delegation.get("delegator_address") or delegation.get("delegator_address") or ""

        items.append({
            "delegatorAddress": delegator_address,
            "amount": str(amount),
            "denom": denom,
            "pctOfValidator": pct,
        })
    
    # Handle pagination
    next_cursor = None
    pagination = result.get("pagination", {})
    if pagination:
        next_key = pagination.get("next_key")
        if next_key:
            # For IOPN, we use offset-based pagination
            current_offset = _safe_int(params.get("pagination.offset", 0))
            next_offset = current_offset + len(delegations)
            next_cursor = str(next_offset)
    
    return {
        "items": items,
        "nextCursor": next_cursor,
    }


def _derive_consensus_address(pubkey_base64: str, chain_prefix: str = "iopnvalcons") -> Optional[str]:
    """
    Derive consensus address from base64 encoded Ed25519 public key.
    
    Args:
        pubkey_base64: Base64 encoded public key (e.g., "bamB7wm//BXdP+5cHKho+yQpXaR3enbrGLotpXAnOPE=")
        chain_prefix: Bech32 prefix for consensus address (default "iopnvalcons" for OPN)
    
    Returns:
        Consensus address string (e.g., "iopnvalcons1...") or None on error
    """
    try:
        import base64
        import hashlib
        
        if not pubkey_base64:
            _logger.warning("Empty pubkey provided for consensus address derivation")
            return None
        
        # Decode base64 public key to bytes
        pubkey_bytes = base64.b64decode(pubkey_base64)
        
        # Hash with SHA256 and take first 20 bytes
        address_bytes = hashlib.sha256(pubkey_bytes).digest()[:20]
        
        # Convert bytes to 5-bit groups for bech32
        converted = _convertbits(address_bytes, 8, 5)
        if not converted:
            _logger.warning("Failed to convert bits for consensus address")
            return None
        
        # Bech32 encode
        consensus_address = _bech32_encode(chain_prefix, converted)
        
        if not consensus_address:
            _logger.warning("Failed to bech32 encode consensus address")
            return None
        
        return consensus_address
    except Exception as e:
        _logger.error(f"Failed to derive consensus address from pubkey: {str(e)}", exc_info=True)
        return None


def _fetch_opn_performance_data(
    valoper: str,
    rpc_base_url: str,
    validator_info_json: Optional[str] = None,
) -> Dict[str, Any]:
    """
    Fetch performance data for an OPN validator using consensus address derived from pubkey.
    
    The consensus address is derived from the Ed25519 public key stored in validator_info,
    avoiding the need for an extra API call to fetch validator details.
    
    Args:
        valoper: Validator operator address (iopnvaloper...)
        rpc_base_url: RPC endpoint base URL
        validator_info_json: JSON string containing validator info with pubkey
    
    Returns:
        Success: {'height': int, 'missedCounter': int, 'windowSize': int, 'valconsAddr': str}
        Error: {'error': str, 'note': str, 'series': []}
    """
    try:
        import json
        
        # Extract consensus pubkey from validator_info if provided
        consensus_pubkey = None
        if validator_info_json:
            try:
                validator_info = json.loads(validator_info_json)
                # Extract pubkey - try different possible field names
                if "@type" in validator_info and "key" in validator_info:
                    # Direct pubkey object format (from stored validator_info)
                    consensus_pubkey = validator_info.get("key")
                elif "consensus_pubkey" in validator_info:
                    # Nested pubkey object format (from API response)
                    pubkey_obj = validator_info.get("consensus_pubkey")
                    if isinstance(pubkey_obj, dict) and "key" in pubkey_obj:
                        consensus_pubkey = pubkey_obj.get("key")
            except json.JSONDecodeError as e:
                _logger.warning(f"Failed to parse validator_info JSON: {str(e)}")
        
        if not consensus_pubkey:
            return {
                'error': 'pubkey_not_found',
                'note': 'Unable to extract consensus pubkey from validator info',
                'series': []
            }
        
        # Derive consensus address from pubkey
        consensus_address = _derive_consensus_address(consensus_pubkey, chain_prefix="iopnvalcons")
        if not consensus_address:
            return {
                'error': 'address_derivation_failed',
                'note': 'Unable to derive consensus address from pubkey',
                'series': []
            }
        
        _logger.info(f"Derived consensus address {consensus_address} for validator {valoper}")
        
        # Fetch latest block height
        try:
            latest_data = _call_iopn_lcd(
                rpc_base_url,
                "cosmos/base/tendermint/v1beta1/blocks/latest",
                timeout=IOPN_RPC_TIMEOUT
            )
            height = int(latest_data.get('block', {}).get('header', {}).get('height', 0))
            if not height:
                return {
                    'error': 'invalid_height',
                    'note': 'Unable to parse block height from response',
                    'series': []
                }
        except Exception as e:
            _logger.warning(f"Failed to fetch latest block for OPN: {str(e)}")
            return {
                'error': 'lcd_unavailable',
                'note': f'Unable to fetch blockchain data: {str(e)}',
                'series': []
            }
        
        # Fetch signing info using consensus address
        try:
            signing_data = _call_iopn_lcd(
                rpc_base_url,
                f"cosmos/slashing/v1beta1/signing_infos/{consensus_address}",
                timeout=IOPN_RPC_TIMEOUT
            )
            signing_info = signing_data.get('val_signing_info', {})
            
            missed_counter = int(signing_info.get('missed_blocks_counter', 0))
            
            # Fetch slashing params to get the signing window size
            # OPN doesn't include window in val_signing_info, so we fetch it separately
            window_size = 0
            try:
                params_data = _call_iopn_lcd(
                    rpc_base_url,
                    "cosmos/slashing/v1beta1/params",
                    timeout=IOPN_RPC_TIMEOUT
                )
                params = params_data.get('params', {})
                window_size = int(params.get('signed_blocks_window', 0))
            except Exception as e:
                _logger.warning(f"Failed to fetch slashing params for window size: {str(e)}")
                window_size = 0
            
            _logger.info(
                f"OPN performance snapshot for {valoper}: "
                f"height={height}, missed={missed_counter}, window={window_size}"
            )
            
            return {
                'height': height,
                'missedCounter': missed_counter,
                'windowSize': window_size,
                'valconsAddr': consensus_address,
            }
        except Exception as e:
            _logger.warning(f"Failed to fetch signing info for {consensus_address}: {str(e)}")
            return {
                'error': 'signing_info_unavailable',
                'note': f'Unable to fetch signing information: {str(e)}',
                'series': []
            }
            
    except Exception as e:
        _logger.error(f"Unexpected error fetching OPN performance data for {valoper}: {str(e)}", exc_info=True)
        return {
            'error': 'internal_error',
            'note': str(e),
            'series': []
        }
