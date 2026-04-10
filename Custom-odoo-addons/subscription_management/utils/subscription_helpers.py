"""Helper functions shared by subscription controller endpoints."""

import base64
import binascii
import json
import logging
import os
import time
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple, Literal, Callable

import requests
from odoo.http import request  # type: ignore[import-not-found]
from datetime import datetime, timedelta, timezone
from odoo.fields import Datetime
from ...rollup_management.utils.deployment_utils import _as_date
from .flow_validator_metrics import fetch_flow_validator_metrics
from .iopn_utils import (
    _iopn_validator_summary,
    _iopn_validator_delegations,
    _iopn_reward_snapshot,
)

_logger = logging.getLogger(__name__)

BECH32_CHARSET = "qpzry9x8gf2tvdw0s3jn54khce6mua7l"
BECH32_CHARSET_MAP = {c: i for i, c in enumerate(BECH32_CHARSET)}
BECH32_GENERATOR = (0x3B6A57B2, 0x26508E6D, 0x1EA119FA, 0x3D4233DD, 0x2A1462B3)
LCD_PAGINATION_LIMIT = 200
LCD_PAGINATION_MAX_PAGES = 50
LCD_TIMEOUT = 40
AVALANCHE_RPC_SUFFIX = "/ext/bc/P"
AVALANCHE_NANO = 1_000_000_000
NEAR_ACCOUNTS_PAGE_LIMIT = 500
NEAR_MAX_DELEGATOR_SCAN = 10000
INJECTIVE_ATTO = 1_000_000_000_000_000_000
SKALE_WEI = 1_000_000_000_000_000_000
EWX_PLANCK = 1_000_000_000_000_000_000
DUNE_SKALE_QUERY_ID = 2999660
DUNE_STATUS_MAX_POLLS = 15
DUNE_STATUS_POLL_DELAY = 10
DUNE_DEFAULT_PERFORMANCE = "medium"
SUBSQUID_GRAPHQL_TIMEOUT = 30
SUBSQUID_DECIMALS = 1_000_000_000_000_000_000
THETA_WEI = 1_000_000_000_000_000_000
SOLANA_LAMPORTS = 1_000_000_000
COSMOS_UATOM = 1_000_000
SOLANA_STAKE_PROGRAM = "Stake11111111111111111111111111111111111111"
SOLANA_CONFIG_PROGRAM = "Config1111111111111111111111111111111111111"
SOLANA_INFLATION_REWARD_BATCH_SIZE = 100
COSMOS_REST_URLS = {
    "mainnet": "https://internal-coreum-mainnet-wkfg49.zeeve.net",
    "testnet": "https://rest.testcosmos.directory/coreumtestnet",
}

# Protocols that currently ship historical validator charts (rewards/stake/delegators)
SUPPORTED_VALIDATOR_HISTORY_PROTOCOLS = {"coreum", "avalanche", "near", "subsquid", "injective", "flow", "energyweb", "skale", "opn", "solana", "cosmos"}

# Protocols that expose performance snapshots (signed vs missed windows)
SUPPORTED_VALIDATOR_PERFORMANCE_PROTOCOLS = {"coreum", "avalanche", "near", "injective", "opn", "cosmos"}


def _fetch_bot_wallet_balances(
    network_type: str,
    bot_addresses: Any,
    timeout: int = 15,
) -> Dict[str, Any]:
    """Fetch the primary Cosmos wallet balance for validator automation bots."""
    if not bot_addresses:
        return {}

    normalized_network = (network_type or "").strip().lower()
    base_url = COSMOS_REST_URLS.get(normalized_network) or COSMOS_REST_URLS.get(network_type)
    if not base_url:
        _logger.debug("No REST endpoint configured for network type: %s", network_type)
        return {}

    def _clean_address(value: Any) -> Optional[str]:
        if value is None:
            return None
        trimmed = str(value).strip()
        return trimmed or None

    selected_address: Optional[str] = None
    if isinstance(bot_addresses, str):
        for part in bot_addresses.split(","):
            candidate = _clean_address(part)
            if candidate:
                selected_address = candidate
                break
    elif isinstance(bot_addresses, dict):
        if normalized_network:
            for key in (
                normalized_network,
                normalized_network.upper(),
                normalized_network.capitalize(),
            ):
                if key in bot_addresses:
                    candidate = _clean_address(bot_addresses[key])
                    if candidate:
                        selected_address = candidate
                        break
        if not selected_address:
            for value in bot_addresses.values():
                candidate = _clean_address(value)
                if candidate:
                    selected_address = candidate
                    break
    elif isinstance(bot_addresses, (list, tuple, set)):
        for value in bot_addresses:
            candidate = _clean_address(value)
            if candidate:
                selected_address = candidate
                break
    else:
        selected_address = _clean_address(bot_addresses)

    if not selected_address:
        return {}

    try:
        resp = requests.get(
            f"{base_url}/cosmos/bank/v1beta1/balances/{selected_address}",
            headers={"content-type": "application/json"},
            timeout=timeout,
        )
        resp.raise_for_status()
        payload = resp.json()
        balances = payload.get("balances") or []
        for entry in balances:
            if isinstance(entry, dict) and entry.get("denom") in {"ucore", "utestcore"}:
                return {
                    "denom": entry.get("denom"),
                    "amount": entry.get("amount"),
                }
        return {}
    except requests.RequestException as exc:
        _logger.warning(
            "Failed to fetch bot wallet balance for %s: %s",
            selected_address,
            exc,
        )
        return {"error": str(exc)}


class LCDRequestError(Exception):
    """Raised when fetching data from the Coreum LCD fails."""

    def __init__(self, message: str, status: Optional[int] = None):
        """Attach the HTTP status to the LCD error for easier debugging."""
        super().__init__(message)
        self.status = status


def _get_coreum_base_url() -> Optional[str]:
    """Return the Coreum LCD base URL from environment or configuration."""
    env_url = os.environ.get("COREUM_BASE_URL")
    if env_url:
        return env_url.rstrip("/")
    try:
        cfg = request.env["ir.config_parameter"].sudo()
        param = cfg.get_param("coreum.base.url")
        if param:
            return param.rstrip("/")
    except Exception:
        _logger.exception("Failed to read Coreum base URL from ir.config_parameter")
    return None


def _resolve_protocol_rpc_url(
    protocol_id: Optional[str] = None,
    protocol_name: Optional[str] = None,
) -> Tuple[Optional[Any], Optional[str]]:
    """Resolve protocol record and RPC base URL from protocol.master."""
    protocol_model = request.env["protocol.master"].sudo()
    record = None

    if protocol_id:
        record = protocol_model.search([("protocol_id", "=", protocol_id)], limit=1)

    if not record and protocol_name:
        record = protocol_model.search([("name", "ilike", protocol_name)], limit=1)

    if not record:
        return None, None

    base_url = (record.web_url or "").strip()
    if not base_url:
        return record, None

    return record, base_url.rstrip("/")


def _build_lcd_url(path: str, base_url: Optional[str] = None) -> str:
    """Build an absolute LCD URL resolving the configured base if needed."""
    resolved_base = base_url.rstrip("/") if base_url else _get_coreum_base_url()
    if not resolved_base:
        raise LCDRequestError("Coreum base URL is not configured")
    if path.startswith("http://") or path.startswith("https://"):
        return path
    return f"{resolved_base}/{path.lstrip('/')}"


def _normalize_protocol_name(protocol: Optional[str]) -> str:
    """Normalize protocol identifiers to a lowercase comparable key."""
    return (protocol or "").strip().lower()


def _xdcscan_normalize_validator_address(valoper: str) -> str:
    """Normalize validator identifiers to the 0x-prefixed format expected by XDCScan."""
    cleaned = (valoper or "").strip()
    if not cleaned:
        raise LCDRequestError("Validator identifier is required")
    lowered = cleaned.lower()
    if lowered.startswith("xdc") and len(cleaned) > 3:
        return f"0x{cleaned[3:]}"
    if lowered.startswith("0x") and len(cleaned) > 2:
        return cleaned
    raise LCDRequestError("Invalid XDC validator address")


def _xdcscan_fetch_masternode(valoper: str, rpc_base_url: Optional[str]) -> Dict[str, Any]:
    """Fetch masternode metadata from XDCScan for a validator."""
    base_url = (rpc_base_url or "").strip().rstrip("/")
    if not base_url:
        raise LCDRequestError("Protocol RPC endpoint is not configured")

    validator_address = _xdcscan_normalize_validator_address(valoper)
    url = f"{base_url}/masternode/{validator_address}"

    try:
        response = requests.get(url, timeout=LCD_TIMEOUT)
    except requests.RequestException as exc:
        raise LCDRequestError(f"Failed to reach XDCScan: {exc}") from exc

    if response.status_code == 404:
        raise LCDRequestError("Validator not found", status=404)

    try:
        response.raise_for_status()
    except requests.HTTPError as exc:
        body_snippet = ""
        try:
            body_snippet = response.text[:200]
        except Exception:
            body_snippet = ""
        raise LCDRequestError(
            f"XDCScan request failed ({response.status_code}): {body_snippet}",
            status=response.status_code,
        ) from exc

    try:
        payload = response.json()
    except ValueError as exc:
        raise LCDRequestError("Invalid JSON received from XDCScan") from exc

    if not isinstance(payload, dict):
        raise LCDRequestError("Invalid XDCScan response payload")

    return payload


def _xdc_address_to_xdc_prefix(address: Optional[str]) -> Optional[str]:
    """Convert a 0x-prefixed address to xdc-prefixed format."""
    if not address or not isinstance(address, str):
        return address
    cleaned = address.strip()
    if cleaned.lower().startswith("0x") and len(cleaned) > 2:
        return f"xdc{cleaned[2:]}"
    return cleaned


def _xdc_validator_summary(valoper: str, rpc_base_url: Optional[str]) -> Dict[str, Any]:
    """Build the summary payload for XDC validators."""
    try:
        payload = _xdcscan_fetch_masternode(valoper, rpc_base_url)
    except LCDRequestError as exc:
        _logger.warning("XDCScan fetch failed for %s: %s", valoper, exc)
        payload = {}

    rank_raw = payload.get("rank")
    validator_rank = _safe_int(rank_raw) if rank_raw not in (None, "") else None

    # Fetch the correct owner address via eth_getOwnerByCoinbase RPC
    owner_address = None
    try:
        xdc_rpc_url = request.env['ir.config_parameter'].sudo().get_param('xdc_explorer_url')
        if xdc_rpc_url:
            validator_address = _xdcscan_normalize_validator_address(valoper)
            rpc_payload = {
                "jsonrpc": "2.0",
                "id": 1001,
                "method": "eth_getOwnerByCoinbase",
                "params": [validator_address, "latest"],
            }
            resp = requests.post(xdc_rpc_url, json=rpc_payload, timeout=LCD_TIMEOUT)
            resp.raise_for_status()
            data = resp.json()
            result = data.get("result")
            if result and isinstance(result, str) and result != "0x":
                owner_address = _xdc_address_to_xdc_prefix(result)
    except Exception as exc:
        _logger.warning("Failed to fetch XDC owner via RPC for %s: %s", valoper, exc)

    if not owner_address:
        owner_address = _xdc_address_to_xdc_prefix(payload.get("owner"))

    return {
        "validator_rank": validator_rank,
        "owner_address": owner_address or "N/A",
        "staking_smart_contract_address": payload.get("smartContractAddress") or "N/A",
        "startDate": payload.get("updatedAt") or "N/A",
    }


def _ensure_trailing_path(base_url: str, suffix: str) -> str:
    """Guarantee that ``base_url`` ends with ``suffix`` exactly once."""
    trimmed = (base_url or "").strip()
    if not trimmed:
        return trimmed
    if trimmed.rstrip("/").endswith(suffix.lstrip("/")):
        return trimmed.rstrip("/")
    return f"{trimmed.rstrip('/')}{suffix}"


def _lcd_get_json(
    path: str,
    params: Optional[Dict[str, Any]] = None,
    allow_404: bool = False,
    timeout: int = LCD_TIMEOUT,
    base_url: Optional[str] = None,
) -> Optional[Dict[str, Any]]:
    """Perform a GET request against the LCD with consistent error handling."""
    url = _build_lcd_url(path, base_url=base_url)
    try:
        response = requests.get(url, params=params, timeout=timeout)
    except requests.RequestException as exc:
        raise LCDRequestError(f"Failed to reach Coreum LCD: {exc}") from exc

    if allow_404 and response.status_code == 404:
        return None

    try:
        response.raise_for_status()
    except requests.HTTPError as exc:  # pragma: no cover - network failures
        status = getattr(exc.response, "status_code", None)
        body_snippet = ""
        try:
            body_snippet = exc.response.text[:200] if exc.response is not None else ""
        except Exception:
            body_snippet = ""
        raise LCDRequestError(
            f"LCD request failed ({status}): {body_snippet}", status=status
        ) from exc

    try:
        return response.json()
    except ValueError as exc:
        raise LCDRequestError("Invalid JSON received from Coreum LCD") from exc


def _call_avalanche_rpc(
    base_url: str,
    method: str,
    params: Optional[Dict[str, Any]] = None,
    timeout: int = LCD_TIMEOUT,
) -> Dict[str, Any]:
    """Invoke Avalanche JSON-RPC endpoint and return the ``result`` payload."""
    rpc_url = base_url or ""
    rpc_url = _ensure_trailing_path(rpc_url, AVALANCHE_RPC_SUFFIX)
    if not rpc_url:
        raise LCDRequestError("Avalanche RPC endpoint is not configured")

    payload = {
        "jsonrpc": "2.0",
        "id": "odoo",
        "method": method,
        "params": params or {},
    }
    try:
        response = requests.post(rpc_url, json=payload, timeout=timeout)
    except requests.RequestException as exc:
        raise LCDRequestError(f"Failed to reach Avalanche RPC: {exc}") from exc

    status = response.status_code
    try:
        response.raise_for_status()
    except requests.HTTPError as exc:
        body_snippet = ""
        try:
            body_snippet = response.text[:200]
        except Exception:
            body_snippet = ""
        raise LCDRequestError(
            f"Avalanche RPC request failed ({status}): {body_snippet}", status=status
        ) from exc

    try:
        data = response.json()
    except ValueError as exc:
        raise LCDRequestError("Invalid JSON received from Avalanche RPC") from exc

    if isinstance(data, dict) and data.get("error"):
        error_obj = data["error"]
        if isinstance(error_obj, dict):
            message = error_obj.get("message") or error_obj.get("code") or "Unknown error"
        else:
            message = str(error_obj)
        raise LCDRequestError(f"Avalanche RPC error: {message}", status=status)

    if not isinstance(data, dict) or "result" not in data:
        raise LCDRequestError("Avalanche RPC response missing result", status=status)

    result = data.get("result") or {}
    if not isinstance(result, dict):
        raise LCDRequestError("Avalanche RPC result payload is invalid", status=status)
    return result


def _fetch_avalanche_chain_height(base_url: str) -> Optional[int]:
    """Return the current P-Chain block height using ``platform.getHeight``."""
    try:
        result = _call_avalanche_rpc(base_url, "platform.getHeight")
    except LCDRequestError as exc:
        _logger.warning("Failed to fetch Avalanche chain height: %s", str(exc))
        return None

    raw_height: Any = result.get("height")
    if raw_height is None:
        return None

    if isinstance(raw_height, str):
        trimmed = raw_height.strip()
        if trimmed.lower().startswith("0x"):
            try:
                return int(trimmed, 16)
            except ValueError:
                return None
        try:
            return int(trimmed.split(".", 1)[0])
        except ValueError:
            return None

    return _safe_int(raw_height)


def _call_near_rpc(
    base_url: str,
    method: str,
    params: Optional[Any] = None,
    timeout: int = LCD_TIMEOUT,
) -> Any:
    """Call the NEAR RPC endpoint and return the decoded JSON result."""
    rpc_url = (base_url or "").strip()
    if not rpc_url:
        raise LCDRequestError("NEAR RPC endpoint is not configured")

    payload = {
        "jsonrpc": "2.0",
        "id": "odoo",
        "method": method,
        "params": params if params is not None else [],
    }
    
    _logger.info(
        "NEAR RPC request: method=%s url=%s params=%s",
        method,
        rpc_url,
        json.dumps(params, default=str) if params else "[]",
    )
    
    try:
        response = requests.post(rpc_url, json=payload, timeout=timeout)
    except requests.RequestException as exc:
        _logger.error(
            "NEAR RPC connection failed: method=%s url=%s error=%s",
            method,
            rpc_url,
            str(exc),
        )
        raise LCDRequestError(f"Failed to reach NEAR RPC: {exc}") from exc

    status = response.status_code
    _logger.info(
        "NEAR RPC response: method=%s status=%s",
        method,
        status,
    )
    
    try:
        response.raise_for_status()
    except requests.HTTPError as exc:
        body_snippet = ""
        try:
            body_snippet = response.text[:500]
        except Exception:
            body_snippet = ""
        _logger.error(
            "NEAR RPC HTTP error: method=%s status=%s body=%s",
            method,
            status,
            body_snippet,
        )
        raise LCDRequestError(
            f"NEAR RPC request failed ({status}): {body_snippet}",
            status=status,
        ) from exc

    try:
        data = response.json()
    except ValueError as exc:
        _logger.error(
            "NEAR RPC invalid JSON: method=%s response=%s",
            method,
            response.text[:200],
        )
        raise LCDRequestError("Invalid JSON received from NEAR RPC") from exc

    if isinstance(data, dict) and data.get("error") is not None:
        error_obj = data.get("error")
        if isinstance(error_obj, dict):
            message = error_obj.get("message") or error_obj.get("code") or "Unknown error"
        else:
            message = str(error_obj)
        _logger.error(
            "NEAR RPC error response: method=%s error=%s",
            method,
            message,
        )
        raise LCDRequestError(f"NEAR RPC error: {message}", status=status)

    if isinstance(data, dict) and "result" in data:
        _logger.info(
            "NEAR RPC success: method=%s result_type=%s",
            method,
            type(data.get("result")).__name__,
        )
        return data.get("result")

    _logger.error(
        "NEAR RPC missing result: method=%s data=%s",
        method,
        json.dumps(data, default=str)[:200],
    )
    raise LCDRequestError("NEAR RPC response missing result", status=status)


def _near_decode_bytes(values: Any) -> Optional[str]:
    """Decode a NEAR RPC byte-array field into a UTF-8 string."""
    if not isinstance(values, (list, tuple)):
        return None
    try:
        byte_values = bytearray(int(v) & 0xFF for v in values)
    except (TypeError, ValueError):
        return None
    try:
        return byte_values.decode("utf-8")
    except UnicodeDecodeError:
        return None


def _near_amount_to_string(value: Any) -> str:
    """Coerce different numeric formats returned by NEAR into strings."""
    if value is None:
        return "0"
    if isinstance(value, str):
        return value
    if isinstance(value, (int, float)):
        try:
            return str(int(value))
        except (TypeError, ValueError):
            return str(value)
    if isinstance(value, (list, tuple)):
        for item in value:
            if item not in (None, "", []):
                return _near_amount_to_string(item)
        return "0"
    try:
        return str(value)
    except Exception:
        return "0"


def _near_parse_accounts_payload(payload: Any) -> List[Dict[str, str]]:
    """Parse the NEAR accounts payload into a simplified dict list."""
    decoded = _near_decode_bytes((payload or {}).get("result") if isinstance(payload, dict) else payload)
    if decoded is None:
        return []
    try:
        parsed = json.loads(decoded)
    except (TypeError, ValueError, json.JSONDecodeError):
        return []

    entries: List[Dict[str, str]] = []
    if isinstance(parsed, list):
        for item in parsed:
            if not isinstance(item, dict):
                continue
            account_id = item.get("account_id")
            if not account_id:
                continue
            stake = _near_amount_to_string(
                item.get("stake") or item.get("staked_balance") or item.get("amount")
            )
            reward = _near_amount_to_string(
                item.get("unstaked_balance") or item.get("reward")
            )
            entries.append(
                {
                    "account_id": account_id,
                    "stake": stake or "0",
                    "reward": reward or "0",
                }
            )
    elif isinstance(parsed, dict):
        for account_id, info in parsed.items():
            stake = "0"
            reward = "0"
            if isinstance(info, dict):
                stake = _near_amount_to_string(
                    info.get("stake")
                    or info.get("staked_balance")
                    or info.get("amount")
                )
                reward = _near_amount_to_string(
                    info.get("unstaked_balance")
                    or info.get("reward")
                )
            elif isinstance(info, (list, tuple)):
                stake = _near_amount_to_string(info[0] if len(info) > 0 else "0")
                reward = _near_amount_to_string(info[1] if len(info) > 1 else "0")
            else:
                stake = _near_amount_to_string(info)
            entries.append(
                {
                    "account_id": str(account_id),
                    "stake": stake or "0",
                    "reward": reward or "0",
                }
            )
    return entries


def _near_fetch_accounts_page(
    base_url: str,
    validator_id: str,
    from_index: int,
    limit: int,
) -> List[Dict[str, str]]:
    """Fetch a single page of delegators from the NEAR validator contract."""
    args = {
        "from_index": max(from_index, 0),
        "limit": max(limit, 1),
    }
    args_base64 = base64.b64encode(json.dumps(args).encode("utf-8")).decode("ascii")
    
    _logger.info(
        "NEAR fetching accounts page: validator=%s from_index=%s limit=%s",
        validator_id,
        from_index,
        limit,
    )
    
    result = _call_near_rpc(
        base_url,
        "query",
        {
            "request_type": "call_function",
            "account_id": validator_id,
            "method_name": "get_accounts",
            "args_base64": args_base64,
            "finality": "final",
        },
    )

    if not isinstance(result, dict):
        _logger.warning(
            "NEAR accounts RPC returned non-dict: validator=%s type=%s",
            validator_id,
            type(result).__name__,
        )
        return []
    
    accounts = _near_parse_accounts_payload(result)
    _logger.info(
        "NEAR accounts page fetched: validator=%s from_index=%s accounts_count=%s",
        validator_id,
        from_index,
        len(accounts),
    )
    return accounts


def _near_collect_delegator_metrics(
    base_url: str,
    validator_id: str,
    target_account_id: Optional[str] = None,
) -> Dict[str, Any]:
    """Aggregate delegator counts, stake totals, and rewards for a NEAR validator.

    If ``target_account_id`` is provided, also return that delegator's stake and reward
    when found in the iterator.
    """
    _logger.info(
        "NEAR collecting delegator metrics: validator=%s rpc_url=%s",
        validator_id,
        base_url,
    )
    
    delegator_count = 0
    delegator_stake = 0
    gross_rewards = 0
    net_rewards = 0
    target_normalized = _normalize_near_account_id(target_account_id)
    target_stake: Optional[int] = None
    target_reward: Optional[int] = None
    from_index = 0
    last_page_len = 0
    next_cursor: Optional[str] = None
    exhausted = False

    max_iterations = max(NEAR_MAX_DELEGATOR_SCAN // NEAR_ACCOUNTS_PAGE_LIMIT + 2, 1)
    for iteration in range(max_iterations):
        accounts = _near_fetch_accounts_page(
            base_url,
            validator_id,
            from_index,
            NEAR_ACCOUNTS_PAGE_LIMIT,
        )
        if not accounts:
            exhausted = True
            break

        last_page_len = len(accounts)
        for account in accounts:
            account_id = account.get("account_id")
            if not account_id or account_id == validator_id:
                continue

            stake_val = max(_safe_int(account.get("stake")), 0)
            reward_val = max(_safe_int(account.get("reward")), 0)
            
            delegator_count += 1
            delegator_stake += stake_val
            gross_rewards += reward_val
            net_rewards += reward_val

            if target_normalized and target_stake is None:
                account_normalized = _normalize_near_account_id(account_id)
                if account_normalized == target_normalized:
                    target_stake = stake_val
                    target_reward = reward_val

            if delegator_count >= NEAR_MAX_DELEGATOR_SCAN:
                next_cursor = str(from_index + NEAR_ACCOUNTS_PAGE_LIMIT)
                break

        if delegator_count >= NEAR_MAX_DELEGATOR_SCAN:
            break

        if last_page_len < NEAR_ACCOUNTS_PAGE_LIMIT:
            exhausted = True
            break

        from_index += NEAR_ACCOUNTS_PAGE_LIMIT

    if next_cursor is None and not exhausted and last_page_len == NEAR_ACCOUNTS_PAGE_LIMIT:
        next_cursor = str(from_index + NEAR_ACCOUNTS_PAGE_LIMIT)

    _logger.info(
        "NEAR delegator metrics collected: validator=%s count=%s stake=%s gross_rewards=%s net_rewards=%s",
        validator_id,
        delegator_count,
        delegator_stake,
        gross_rewards,
        net_rewards,
    )

    return {
        "count": delegator_count,
        "delegator_stake": max(delegator_stake, 0),
        "gross_rewards": max(gross_rewards, 0),
        "net_rewards": max(net_rewards, 0),
        "next_cursor": next_cursor,
        "target_stake": target_stake,
        "target_reward": target_reward,
    }

def _normalize_near_account_id(value: Optional[str]) -> Optional[str]:
    """Clean and normalize a NEAR account identifier for RPC calls."""
    if not isinstance(value, str):
        return None
    trimmed = value.strip()
    return trimmed.lower() if trimmed else None



def _near_validator_summary(
    valoper: str,
    rpc_base_url: str,
    delegation_address: Optional[str] = None,
) -> Dict[str, Any]:
    """Build a validator summary compatible payload for NEAR validators."""
    
    validators_payload = _call_near_rpc(rpc_base_url, "validators", [None])
    if not isinstance(validators_payload, dict):
        raise LCDRequestError("Invalid NEAR validators response")
    # _logger.info(
        # "NEAR validators RPC payload valoper=%s payload=%s",
        # valoper,
        # json.dumps(validators_payload, default=str),
    # )

    current_validators = validators_payload.get("current_validators") or []
    next_validators = validators_payload.get("next_validators") or []

    validator = None
    status: Literal["active", "inactive"] = "active"
    for item in current_validators:
        if isinstance(item, dict) and item.get("account_id") == valoper:
            validator = item
            status = "active"
            break
    if validator is None:
        for item in next_validators:
            if isinstance(item, dict) and item.get("account_id") == valoper:
                validator = item
                status = "inactive"
                break

    if not isinstance(validator, dict):
        raise LCDRequestError("Validator not found", status=404)

    stake_yocto = max(_safe_int(validator.get("stake")), 0)
    total_stake_yocto = _safe_int(validators_payload.get("total_stake"))
    if total_stake_yocto == 0 and current_validators:
        total_stake_yocto = sum(max(_safe_int(v.get("stake")), 0) for v in current_validators if isinstance(v, dict))

    voting_power_pct = _pct(stake_yocto, total_stake_yocto)

    produced_blocks = max(_safe_int(validator.get("num_produced_blocks")), 0)
    expected_blocks = max(_safe_int(validator.get("num_expected_blocks")), 0)
    uptime_pct = _pct(produced_blocks, expected_blocks)

    commission_pct = 0.0
    try:
        commission_payload = _call_near_rpc(
            rpc_base_url,
            "query",
            {
                "request_type": "call_function",
                "account_id": valoper,
                "method_name": "get_reward_fee_fraction",
                "args_base64": base64.b64encode(b"{}").decode("ascii"),
                "finality": "final",
            },
        )
        # _logger.info(
            # "NEAR commission RPC payload valoper=%s payload=%s",
            # valoper,
            # json.dumps(commission_payload, default=str),
        # )
        if isinstance(commission_payload, dict):
            decoded = _near_decode_bytes(commission_payload.get("result"))
            if decoded:
                try:
                    parsed = json.loads(decoded)
                    numerator = None
                    denominator = None
                    if isinstance(parsed, dict):
                        numerator = parsed.get("numerator")
                        denominator = parsed.get("denominator")
                    elif isinstance(parsed, (list, tuple)) and len(parsed) >= 2:
                        numerator, denominator = parsed[0], parsed[1]
                    if numerator is not None and denominator not in (None, 0, "0"):
                        numerator_val = float(numerator)
                        denominator_val = float(denominator)
                        if denominator_val:
                            commission_pct = (numerator_val / denominator_val) * 100
                except (TypeError, ValueError):
                    commission_pct = 0.0
    except LCDRequestError:
        commission_pct = 0.0
    
    reward_account = _normalize_near_account_id(delegation_address)
    if not reward_account and isinstance(validator, dict):
        for key in ("delegation_address", "delegationAddress", "reward_address", "rewardAddress"):
            reward_account = _normalize_near_account_id(validator.get(key))
            if reward_account:
                break


    delegator_metrics = _near_collect_delegator_metrics(
        rpc_base_url,
        valoper,
        target_account_id=reward_account,
    )
    delegator_stake = delegator_metrics.get("delegator_stake", 0)
    owned_stake = max(stake_yocto - delegator_stake, 0)
    
    # Extract reward metrics from delegator data
    gross_rewards = delegator_metrics.get("gross_rewards", 0)
    net_rewards = delegator_metrics.get("net_rewards", 0)
    
    # Total rewards across all delegators
    delegation_rewards = str(gross_rewards)
    total_rewards = str(gross_rewards)

    reward_balance_yocto = delegator_metrics.get("target_stake") if reward_account else None
    outstanding_rewards_value = reward_balance_yocto if reward_balance_yocto is not None else None
    summary = {
        "tokens": str(stake_yocto),
        "ownedStake": str(owned_stake),
        "totalStake": str(stake_yocto),
        "delegatorStake": str(delegator_stake),
        "votingPowerPct": voting_power_pct,
        "commissionPct": commission_pct,
        "outstandingRewards": str(outstanding_rewards_value),
        # "ownedRewards": "0",
        # "delegationRewards": delegation_rewards,
        # "totalRewards": total_rewards,
        # "totalUnstaked": "0",
        "status": status,
        "statusLabel": "Active" if status == "active" else "Inactive",
        "jailed": False,
        "connectionStatus": "unknown",
        "uptimePct": uptime_pct,
        "identity": None,
        "delegatorCount": delegator_metrics.get("count"),
        "nextCursor": delegator_metrics.get("next_cursor"),
    }

    return summary

def _extract_delegation_address(validator_info: Dict[str, Any]) -> Optional[str]:
    """Extract a delegation wallet address from stored validator metadata."""
    if not isinstance(validator_info, dict):
        return None

    candidates = [
        validator_info.get("delegation_address"),
        validator_info.get("delegationAddress"),
        validator_info.get("delegator_address"),
        validator_info.get("delegatorAddress"),
        validator_info.get("reward_address"),
        validator_info.get("rewardAddress"),
        validator_info.get("owner_address"),
        validator_info.get("ownerAddress"),
    ]

    for candidate in candidates:
        if isinstance(candidate, str):
            cleaned = candidate.strip()
            if cleaned:
                return cleaned

    return None

def _near_validator_delegations(
    valoper: str,
    rpc_base_url: str,
    cursor: Optional[str],
) -> Dict[str, Any]:
    """Return a paginated delegator list for a NEAR validator."""
    validators_payload = _call_near_rpc(rpc_base_url, "validators", [None])
    if not isinstance(validators_payload, dict):
        raise LCDRequestError("Invalid NEAR validators response")
    # _logger.info(
        # "NEAR validators RPC payload for delegations valoper=%s payload=%s",
        # valoper,
        # json.dumps(validators_payload, default=str),
    # )

    current_validators = validators_payload.get("current_validators") or []
    next_validators = validators_payload.get("next_validators") or []

    validator = None
    for item in current_validators + next_validators:
        if isinstance(item, dict) and item.get("account_id") == valoper:
            validator = item
            break

    if not isinstance(validator, dict):
        raise LCDRequestError("Validator not found", status=404)

    validator_stake = max(_safe_int(validator.get("stake")), 0)

    try:
        from_index = int(str(cursor)) if cursor is not None else 0
    except (TypeError, ValueError):
        from_index = 0
    if from_index < 0:
        from_index = 0

    accounts = _near_fetch_accounts_page(
        rpc_base_url,
        valoper,
        from_index,
        NEAR_ACCOUNTS_PAGE_LIMIT,
    )

    page_len = len(accounts)
    items: List[Dict[str, Any]] = []
    for account in accounts:
        account_id = account.get("account_id")
        if not account_id or account_id == valoper:
            continue
        amount_str = account.get("stake") or "0"
        amount_int = max(_safe_int(amount_str), 0)
        pct = _pct(amount_int, validator_stake) if validator_stake > 0 else 0.0
        items.append(
            {
                "delegatorAddress": account_id,
                "amount": str(amount_str),
                "denom": "NEAR",
                "pctOfValidator": pct,
            }
        )

    next_cursor = None
    if page_len == NEAR_ACCOUNTS_PAGE_LIMIT:
        next_cursor = str(from_index + NEAR_ACCOUNTS_PAGE_LIMIT)

    return {
        "items": items,
        "nextCursor": next_cursor,
    }


FLOW_NETWORK_CONFIG: Dict[str, Dict[str, str]] = {
    "mainnet": {
        "id_table": "0x8624b52f9ddcd04a",
        "staking_collection": "0x8d0e87b65159ae63",
        "ft_address": "0xf233dcee88fe0abe",
        "flow_address": "0x1654653399040a61",
    },
    "testnet": {
        "id_table": "0x9eca2b38b18b5dfe",
        "staking_collection": "0x95e019a17d0e23d7",
        "ft_address": "0x9a0766d93b6608b7",
        "flow_address": "0x7e60df042a9c0868",
    },
}

def _flow_resolve_network_name(network_hint: Optional[str], base_url: Optional[str]) -> str:
    """Infer the Flow network (mainnet/testnet) from hints or the URL."""
    normalized = (network_hint or "").strip().lower()
    if normalized in ("testnet", "test"):
        return "testnet"
    if normalized in ("mainnet", "main"):
        return "mainnet"
    url_lower = (base_url or "").strip().lower()
    if "test" in url_lower:
        return "testnet"
    return "mainnet"


def _flow_cadence_arg(arg_type: str, value: Any) -> Dict[str, Any]:
    """Build a Cadence script argument payload."""
    return {"type": arg_type, "value": value}


def _flow_cadence_reward_cut(id_table: str) -> str:
    """Cadence script fetching the validator reward cut fraction."""
    return f"""
    import FlowIDTableStaking from {id_table}
    access(all) fun main(): UFix64 {{
        return FlowIDTableStaking.getRewardCutPercentage()
    }}
    """


def _flow_cadence_node_info(id_table: str) -> str:
    """Cadence script returning node info used for rewards."""
    return f"""
    import FlowIDTableStaking from {id_table}
    access(all) fun main(nodeID: String): FlowIDTableStaking.NodeInfo {{
        return FlowIDTableStaking.NodeInfo(nodeID: nodeID)
    }}
    """


def _flow_cadence_total_with_delegators(id_table: str) -> str:
    """Cadence script returning stake total including delegators."""
    return f"""
    import FlowIDTableStaking from {id_table}
    access(all) fun main(nodeID: String): UFix64 {{
        return FlowIDTableStaking.NodeInfo(nodeID: nodeID).totalCommittedWithDelegators()
    }}
    """


def _flow_cadence_total_without_delegators(id_table: str) -> str:
    """Cadence script returning stake total excluding delegators."""
    return f"""
    import FlowIDTableStaking from {id_table}
    access(all) fun main(nodeID: String): UFix64 {{
        return FlowIDTableStaking.NodeInfo(nodeID: nodeID).totalCommittedWithoutDelegators()
    }}
    """


def _flow_cadence_wallet_balance(ft_addr: str, flow_addr: str) -> str:
    """Cadence script fetching the FLOW wallet balance."""
    return f"""
    import FungibleToken from {ft_addr}
    import FlowToken from {flow_addr}
    access(all) fun main(address: Address): UFix64 {{
        let acct = getAccount(address)
        let balanceRef = acct.capabilities.borrow<&{{FungibleToken.Balance}}>(/public/flowTokenBalance)
        if balanceRef == nil {{
            return 0.0
        }}
        return balanceRef!.balance
    }}
    """


def _flow_cadence_node_delegators(id_table: str) -> str:
    """Cadence script listing delegators that belong to a node record."""
    return f"""
    import FlowIDTableStaking from {id_table}
    access(all) fun main(nodeID: String): [FlowIDTableStaking.DelegatorInfo] {{
        let nodeInfo = FlowIDTableStaking.NodeInfo(nodeID: nodeID)
        let ids = nodeInfo.delegators
        let results: [FlowIDTableStaking.DelegatorInfo] = []
        var idx = 0
        while idx < ids.length {{
            let delegatorID = ids[idx]
            idx = idx + 1
            results.append(FlowIDTableStaking.DelegatorInfo(nodeID: nodeID, delegatorID: delegatorID))
        }}
        return results
    }}
    """


def _flow_raise_if_node_missing(exc: "LCDRequestError") -> None:
    """Convert Cadence NodeInfo failures into a 404-style LCD error."""
    message = str(exc).lower()
    if exc.status == 400 and "nodeinfo" in message and "flowidtablestaking" in message:
        raise LCDRequestError("Flow validator node was not found on this network", status=404) from exc


def _flow_build_script_url(rest_base_url: str) -> str:
    """Return the Flow REST endpoint for executing Cadence scripts."""
    base = (rest_base_url or "").strip().rstrip("/")
    if not base:
        raise LCDRequestError("Flow REST endpoint is not configured")
    if base.endswith("/v1"):
        return f"{base}/scripts?block_height=sealed"
    return f"{base}/v1/scripts?block_height=sealed"


def _flow_resolve_rest_endpoint(base_url: Optional[str], network_key: str) -> str:
    """Normalize Flow REST endpoints and fall back to defaults when needed."""
    normalized = (base_url or "").strip()
    if normalized and not normalized.lower().startswith("http"):
        normalized = f"https://{normalized}"
    if not normalized or "flowscan.io" in normalized.lower():
        fallback = _flow_resolve_protocol_master_url(network_key)
        if not fallback:
            raise LCDRequestError("Flow REST endpoint is not configured")
        if normalized and normalized.rstrip("/") != fallback.rstrip("/"):
            _logger.info(
                "Flow REST base overridden for %s: %s -> %s",
                network_key,
                normalized,
                fallback,
            )
        return fallback.rstrip("/")
    return normalized.rstrip("/")


def _flow_resolve_protocol_master_url(network_key: str) -> Optional[str]:
    """Fetch the Flow REST endpoint for the given network from protocol.master."""
    env = getattr(request, "env", None)
    if not env:
        return None

    try:
        protocol = (
            env["protocol.master"]
            .sudo()
            .search(
                ["|", ("short_name", "ilike", "flow"), ("name", "ilike", "flow")],
                limit=1,
            )
        )
    except Exception:
        _logger.exception("Failed to load Flow REST URLs from protocol.master")
        return None

    if not protocol:
        return None

    raw_value = (protocol.web_url_testnet if network_key == "testnet" else protocol.web_url) or ""
    cleaned = raw_value.strip()
    return cleaned or None


def _flow_build_script_payload(cadence: str, args: List[Dict[str, Any]]) -> Dict[str, Any]:
    """Encode a Cadence script and its arguments for submission."""
    script_b64 = base64.b64encode(cadence.encode("utf-8")).decode("ascii")
    encoded_args = [
        base64.b64encode(json.dumps(arg).encode("utf-8")).decode("ascii") for arg in args
    ]
    return {
        "script": script_b64,
        "arguments": encoded_args,
    }


def _flow_decode_cadence_json(body: Any) -> Any:
    """Decode Flow Cadence execution responses into JSON."""
    if isinstance(body, dict):
        if body.get("type") or (isinstance(body.get("value"), dict) and body["value"].get("type")):
            return body
        if isinstance(body.get("value"), str):
            encoded = body["value"]
        elif isinstance(body.get("result"), str):
            encoded = body["result"]
        elif isinstance(body.get("data"), str):
            encoded = body["data"]
        else:
            encoded = None
    elif isinstance(body, str):
        encoded = body
    else:
        encoded = None

    if not encoded:
        raise LCDRequestError("Unrecognized Flow scripts response")

    try:
        decoded = base64.b64decode(encoded)
    except (binascii.Error, ValueError) as exc:
        raise LCDRequestError("Invalid Flow scripts payload") from exc

    try:
        as_text = decoded.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise LCDRequestError("Invalid Flow scripts encoding") from exc

    stripped = as_text.strip()
    if not stripped:
        return None

    try:
        return json.loads(stripped)
    except json.JSONDecodeError:
        if stripped.startswith('"') and stripped.endswith('"'):
            return {"type": "String", "value": stripped[1:-1]}
        try:
            Decimal(stripped)
            return {"type": "String", "value": stripped}
        except (InvalidOperation, ValueError):
            raise LCDRequestError("Failed to parse Flow scripts response")


def _flow_exec_script(
    rest_base_url: str,
    cadence: str,
    args: Optional[List[Dict[str, Any]]] = None,
    timeout: int = LCD_TIMEOUT,
) -> Any:
    """Execute a Cadence script and gracefully fall back if needed."""
    url = _flow_build_script_url(rest_base_url)
    payload = _flow_build_script_payload(cadence, args or [])

    def _submit(script_payload: Dict[str, Any]) -> Any:
        try:
            response = requests.post(url, json=script_payload, timeout=timeout)
        except requests.RequestException as exc:
            raise LCDRequestError(f"Failed to reach Flow REST API: {exc}") from exc

        body_text = response.text
        try:
            response.raise_for_status()
        except requests.HTTPError as exc:
            snippet = body_text.strip() if body_text else ""
            parsed_error: Any = None
            if body_text:
                try:
                    parsed_error = json.loads(body_text)
                except ValueError:
                    parsed_error = None
            if isinstance(parsed_error, dict):
                candidate = parsed_error.get("message") or parsed_error.get("error")
                if isinstance(candidate, str):
                    snippet = candidate
            raise LCDRequestError(
                f"Flow scripts request failed ({response.status_code}): {snippet}",
                status=response.status_code,
            ) from exc

        try:
            return response.json()
        except ValueError:
            return body_text

    try:
        raw_body = _submit(payload)
    except LCDRequestError as exc:
        if "access(all)" not in cadence:
            raise
        fallback_cadence = cadence.replace("access(all)", "pub")
        fallback_payload = _flow_build_script_payload(fallback_cadence, args or [])
        raw_body = _submit(fallback_payload)

    return _flow_decode_cadence_json(raw_body)


def _flow_as_decimal(value: Any) -> Decimal:
    """Convert Flow numeric payloads into ``Decimal`` objects."""
    if value in (None, "", False):
        return Decimal("0")
    if isinstance(value, Decimal):
        return value
    if isinstance(value, bool):
        return Decimal("0")
    if isinstance(value, (int, float)):
        return Decimal(str(value))
    if isinstance(value, str):
        try:
            return Decimal(value.strip() or "0")
        except (InvalidOperation, ValueError):
            return Decimal("0")
    if isinstance(value, dict):
        if "value" in value:
            return _flow_as_decimal(value.get("value"))
        if "fields" in value:
            return _flow_as_decimal(value.get("value"))
    if isinstance(value, (list, tuple)):
        for item in value:
            dec = _flow_as_decimal(item)
            if dec:
                return dec
        return Decimal("0")
    return Decimal("0")


def _flow_unwrap_value(value: Any) -> Any:
    """Peel nested Cadence encoding layers until a raw value remains."""
    current = value
    seen = set()
    while isinstance(current, dict) and "value" in current:
        obj_id = id(current)
        if obj_id in seen:
            break
        seen.add(obj_id)
        current = current.get("value")
    return current


def _flow_decimal_to_string(value: Decimal) -> str:
    """Render a ``Decimal`` as a trimmed string."""
    if value is None:
        return "0"
    normalized = value.normalize()
    text = format(normalized, "f")
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    return text or "0"


def _flow_pct_decimal(part: Decimal, whole: Decimal) -> float:
    """Compute a percentage from two ``Decimal`` numbers."""
    if whole is None or whole <= 0:
        return 0.0
    pct = (part / whole) * Decimal("100")
    try:
        return float(pct.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))
    except InvalidOperation:
        return 0.0


def _flow_struct_get_field(struct_obj: Any, field_name: str) -> Any:
    """Retrieve a named field from a Cadence-encoded struct."""
    if not isinstance(struct_obj, dict):
        return None
    value_obj = struct_obj.get("value")
    fields = []
    if isinstance(value_obj, dict) and isinstance(value_obj.get("fields"), list):
        fields = value_obj["fields"]
    elif isinstance(struct_obj.get("fields"), list):
        fields = struct_obj["fields"]
    for field in fields:
        if field.get("name") == field_name:
            return field.get("value")
    return None


def _flow_as_int(value: Any) -> int:
    """Convert Flow numeric payloads into integers."""
    decimal_value = _flow_as_decimal(value)
    try:
        return int(decimal_value.to_integral_value(rounding=ROUND_HALF_UP))
    except (InvalidOperation, ValueError):
        return 0


def _flow_collect_delegators(payload: Any, node_id: str) -> List[Dict[str, Any]]:
    """Extract delegator entries for the provided node ID."""
    if isinstance(payload, dict):
        value = payload.get("value")
    else:
        value = payload
    entries = value if isinstance(value, list) else []
    node_lower = (node_id or "").lower()
    delegators: List[Dict[str, Any]] = []
    for entry in entries:
        fields = (entry.get("value") or {}).get("fields") if isinstance(entry, dict) else None
        if not isinstance(fields, list):
            continue

        def _pick(name: str) -> Any:
            for field in fields:
                if field.get("name") == name:
                    return field.get("value")
            return None

        delegator_node_raw = _pick("nodeID") or _pick("nodeId") or _pick("node_id")
        delegator_node_value = _flow_unwrap_value(delegator_node_raw)
        delegator_node_text = str(delegator_node_value or "").strip().lower()
        if not delegator_node_text or delegator_node_text != node_lower:
            continue

        delegator_entry = {
            "delegator_id": _flow_as_int(_pick("id") or _pick("delegatorID")),
            "node_id": delegator_node_value or "",
            "tokens_committed": _flow_as_decimal(_pick("tokensCommitted")),
            "tokens_staked": _flow_as_decimal(_pick("tokensStaked")),
            "tokens_rewarded": _flow_as_decimal(_pick("tokensRewarded")),
            "tokens_unstaked": _flow_as_decimal(_pick("tokensUnstaked")),
            "tokens_unstaking": _flow_as_decimal(_pick("tokensUnstaking")),
            "tokens_requested_to_unstake": _flow_as_decimal(_pick("tokensRequestedToUnstake")),
        }
        delegators.append(delegator_entry)
    return delegators


def _flow_normalize_owner_address(value: Optional[str]) -> Optional[str]:
    """Normalize Flow owner addresses to a canonical hex format."""
    if not isinstance(value, str):
        return None
    trimmed = value.strip()
    if not trimmed:
        return None
    if not trimmed.startswith("0x"):
        trimmed = f"0x{trimmed}"
    try:
        int(trimmed[2:], 16)
    except (ValueError, TypeError):
        return None
    return trimmed.lower()


def _flow_normalize_node_id(node_id: Optional[str]) -> str:
    """Normalize Flow node IDs for use inside Cadence scripts."""
    value = (node_id or "").strip().lower()
    if len(value) not in (64, 128):
        raise LCDRequestError("Invalid Flow node ID format", status=400)
    try:
        int(value, 16)
    except ValueError:
        raise LCDRequestError("Invalid Flow node ID format", status=400)
    return value


def _flow_fetch_validator_details(
    rest_base_url: str,
    node_id: str,
    network_hint: Optional[str],
    owner_address: Optional[str] = None,
) -> Dict[str, Any]:
    """Fetch the complete Flow validator detail bundle."""
    node_id = _flow_normalize_node_id(node_id)
    network_key = _flow_resolve_network_name(network_hint, rest_base_url)
    network_config = FLOW_NETWORK_CONFIG.get(network_key)
    if not network_config:
        raise LCDRequestError("Unsupported Flow network configuration")

    id_table = network_config["id_table"]
    staking_collection = network_config["staking_collection"]

    rest_endpoint = _flow_resolve_rest_endpoint(rest_base_url, network_key)

    def _node_script(callable_factory: Callable[[], Any]) -> Any:
        try:
            return callable_factory()
        except LCDRequestError as exc:
            _flow_raise_if_node_missing(exc)
            raise

    try:
        commission_raw = _flow_exec_script(rest_endpoint, _flow_cadence_reward_cut(id_table))
        commission_pct = _flow_as_decimal(commission_raw) * Decimal("100")

        total_with = _node_script(
            lambda: _flow_exec_script(
                rest_endpoint,
                _flow_cadence_total_with_delegators(id_table),
                [_flow_cadence_arg("String", node_id)],
            )
        )
        total_without = _node_script(
            lambda: _flow_exec_script(
                rest_endpoint,
                _flow_cadence_total_without_delegators(id_table),
                [_flow_cadence_arg("String", node_id)],
            )
        )

        total_stake = _flow_as_decimal(total_with)
        owned_stake = _flow_as_decimal(total_without)
        delegator_stake = max(total_stake - owned_stake, Decimal("0"))

        node_info = _node_script(
            lambda: _flow_exec_script(
                rest_endpoint,
                _flow_cadence_node_info(id_table),
                [_flow_cadence_arg("String", node_id)],
            )
        )
        tokens_rewarded = _flow_as_decimal(_flow_struct_get_field(node_info, "tokensRewarded"))

        wallet_balance = None
        owner_address_clean = _flow_normalize_owner_address(owner_address)
        print("Flow owner address normalized:", owner_address_clean)
        if owner_address_clean:
            try:
                wallet_payload = _flow_exec_script(
                    rest_endpoint,
                    _flow_cadence_wallet_balance(
                        network_config["ft_address"],
                        network_config["flow_address"],
                    ),
                    [_flow_cadence_arg("Address", owner_address_clean)],
                )
                wallet_balance = _flow_as_decimal(wallet_payload)
            except LCDRequestError:
                wallet_balance = None

        delegators: Optional[List[Dict[str, Any]]] = None
        try:
            delegator_payload = _flow_exec_script(
                rest_endpoint,
                _flow_cadence_node_delegators(id_table),
                [_flow_cadence_arg("String", node_id)],
            )
            print("Flow delegator payload:", delegator_payload)
            delegators = _flow_collect_delegators(delegator_payload, node_id)
        except LCDRequestError as delegator_error:
            _flow_raise_if_node_missing(delegator_error)
            delegators = None

        return {
            "node_id": node_id,
            "network_key": network_key,
            "commission_pct": commission_pct,
            "total_stake": total_stake,
            "owned_stake": owned_stake,
            "delegator_stake": delegator_stake,
            "total_rewards": tokens_rewarded,
            "wallet_balance": wallet_balance,
            "delegators": delegators or [],
            "delegation_count": len(delegators) if delegators is not None else None,
            "denom": "FLOW",
        }
    except LCDRequestError as exc:
        _flow_raise_if_node_missing(exc)
        raise


def _flow_summary_from_details(details: Dict[str, Any]) -> Dict[str, Any]:
    """Transform Flow detail payloads into summary data."""
    total_stake = details.get("total_stake") or Decimal("0")
    owned_stake = details.get("owned_stake") or Decimal("0")
    delegator_stake = details.get("delegator_stake") or Decimal("0")
    total_rewards = details.get("total_rewards") or Decimal("0")

    tokens_str = _flow_decimal_to_string(total_stake)
    summary: Dict[str, Any] = {
        "tokens": tokens_str,
        "ownedStake": _flow_decimal_to_string(owned_stake),
        "totalStake": tokens_str,
        "delegatorStake": _flow_decimal_to_string(delegator_stake),
        "votingPowerPct": None,
        "commissionPct": float(details.get("commission_pct") or 0),
        "outstandingRewards": _flow_decimal_to_string(total_rewards),
        "ownedRewards": _flow_decimal_to_string(total_rewards),
        "delegationRewards": _flow_decimal_to_string(total_rewards),
        "totalRewards": _flow_decimal_to_string(total_rewards),
        "status": "active",
        "statusLabel": "Active",
        "jailed": False,
        "connectionStatus": "unknown",
        "identity": None,
        "delegatorCount": details.get("delegation_count"),
    }
    wallet_balance = details.get("wallet_balance")
    if wallet_balance is not None:
        summary["walletBalance"] = _flow_decimal_to_string(wallet_balance)
    return summary


def _flow_delegations_from_details(details: Dict[str, Any]) -> Dict[str, Any]:
    """Transform Flow detail payloads into delegation lists."""
    delegators = details.get("delegators") or []
    total_stake = details.get("total_stake") or Decimal("0")
    denom = details.get("denom") or "FLOW"

    items: List[Dict[str, Any]] = []
    for delegator in delegators:
        tokens = delegator.get("tokens_staked") or delegator.get("tokens_committed") or Decimal("0")
        if not isinstance(tokens, Decimal):
            tokens = _flow_as_decimal(tokens)
        amount_str = _flow_decimal_to_string(tokens)
        pct = _flow_pct_decimal(tokens, total_stake)
        delegator_address = delegator.get("address") or delegator.get("delegator_id")
        items.append(
            {
                "delegatorAddress": str(delegator_address),
                "amount": amount_str,
                "denom": denom,
                "pctOfValidator": pct,
            }
        )

    return {
        "items": items,
        "nextCursor": None,
    }


def _pct(numerator: int, denominator: int) -> float:
    """Calculate percentage with two decimal precision."""
    if denominator == 0:
        return 0.0
    return round((numerator * 10000) / denominator) / 100


def _sum_coin_amounts(coins: Optional[Iterable[Any]]) -> int:
    """Sum a nested set of coin or reward amounts provided as dicts."""
    # _logger.info("Summing coin amounts from payload: %s", coins)
    total = 0
    if not coins:
        _logger.debug("No coins provided for summation")
        return total
    for coin in coins:
        _logger.debug("Processing coin entry: %s", coin)
        amount = 0
        if isinstance(coin, dict):
            _logger.debug("Processing coin dict: %s", coin)
            if "amount" in coin:
                _logger.debug("Adding coin amount: %s", coin.get("amount"))
                amount = coin.get("amount")
            elif "rewards" in coin:
                nested = coin.get("rewards")
                total += _sum_coin_amounts(nested)
                continue
            elif "reward" in coin:
                nested = coin.get("reward")
                total += _sum_coin_amounts(nested)
                continue
            else:
                _logger.debug("Skipping unrecognized coin structure: %s", coin)
                continue
        else:
            _logger.debug("Skipping non-dict coin structure: %s", coin)
            continue
        try:
            if isinstance(amount, str) and "." in amount:
                amount = amount.split(".", 1)[0]
            total += int(amount or 0)
        except (TypeError, ValueError):
            continue
    return total


def _micro_to_core_number(value: int) -> float:
    """Convert micro CORE amounts to floating point CORE."""
    return round(value / 1_000_000, 6)


def _micro_to_core_string(value: int) -> str:
    """Convert micro CORE amounts to human readable strings."""
    whole = value // 1_000_000
    remainder = value % 1_000_000
    if remainder == 0:
        return str(whole)
    frac = str(remainder).rjust(6, "0").rstrip("0")
    return f"{whole}.{frac}"


def _micro_to_nillion_number(value: int) -> float:
    """Convert micro Nillion amounts to floating point NIL."""
    return _micro_to_core_number(value)


def _micro_to_nillion_string(value: int) -> str:
    """Convert micro Nillion amounts to human readable NIL strings."""
    return _micro_to_core_string(value)


def _sum_tokens(validators: Iterable[Dict[str, Any]]) -> int:
    """Sum validator token counts from a paginated response."""
    total = 0
    for validator in validators:
        tokens = validator.get("tokens")
        try:
            total += int(str(tokens).split(".", 1)[0])
        except (TypeError, ValueError, AttributeError):
            continue
    return total


def _get_total_bonded_tokens(base_url: Optional[str] = None) -> int:
    """Return bonded validator tokens, preferring the staking pool aggregate endpoint."""
    try:
        pool_payload = _lcd_get_json(
            "/cosmos/staking/v1beta1/pool",
            base_url=base_url,
        ) or {}
        bonded_tokens = ((pool_payload.get("pool") or {}).get("bonded_tokens"))
        if bonded_tokens not in (None, ""):
            return _safe_int(bonded_tokens)
    except LCDRequestError:
        # Some chains may not expose /pool; fall back to validator pagination.
        pass

    total = 0
    page_key: Optional[str] = None
    for _ in range(LCD_PAGINATION_MAX_PAGES):
        params: Dict[str, Any] = {
            "status": "BOND_STATUS_BONDED",
            "pagination.limit": LCD_PAGINATION_LIMIT,
        }
        if page_key:
            params["pagination.key"] = page_key
        payload = _lcd_get_json(
            "/cosmos/staking/v1beta1/validators",
            params=params,
            base_url=base_url,
        )
        if not payload:
            break
        validators = payload.get("validators") or []
        total += _sum_tokens(validators)
        page_key = (payload.get("pagination") or {}).get("next_key")
        if not page_key:
            break
        time.sleep(0.2)
    return total


def _bech32_polymod(values: Iterable[int]) -> int:
    """Compute the Bech32 polymod checksum across the provided values."""
    chk = 1
    for v in values:
        top = chk >> 25
        chk = (chk & 0x1FFFFFF) << 5 ^ v
        for i in range(5):
            if (top >> i) & 1:
                chk ^= BECH32_GENERATOR[i]
    return chk


def _bech32_hrp_expand(hrp: str) -> List[int]:
    """Expand the Bech32 human readable part into integer codes."""
    return [ord(x) >> 5 for x in hrp] + [0] + [ord(x) & 31 for x in hrp]


def _verify_checksum(hrp: str, data: List[int]) -> bool:
    """Validate the checksum of a Bech32 string."""
    return _bech32_polymod(_bech32_hrp_expand(hrp) + data) == 1


def _create_checksum(hrp: str, data: List[int]) -> List[int]:
    """Compute the checksum tail for a Bech32 string."""
    values = _bech32_hrp_expand(hrp) + data
    polymod = _bech32_polymod(values + [0, 0, 0, 0, 0, 0]) ^ 1
    return [(polymod >> 5 * (5 - i)) & 31 for i in range(6)]


def _bech32_decode(value: str) -> Tuple[Optional[str], Optional[List[int]]]:
    """Decode a Bech32 string into its HRP and data bits."""
    if not value or any(ord(x) < 33 or ord(x) > 126 for x in value):
        return None, None
    lowered = value.lower()
    if lowered != value and value.upper() != value:
        return None, None
    pos = lowered.rfind("1")
    if pos < 1 or pos + 7 > len(lowered):
        return None, None
    hrp = lowered[:pos]
    data_part = lowered[pos + 1 :]
    try:
        data = [BECH32_CHARSET_MAP[c] for c in data_part]
    except KeyError:
        return None, None
    if not _verify_checksum(hrp, data):
        return None, None
    return hrp, data[:-6]


def _bech32_encode(hrp: str, data: List[int]) -> Optional[str]:
    """Encode Bech32 HRP and data bits back into a string."""
    if not hrp or any((ord(x) < 33 or ord(x) > 126) for x in hrp):
        return None
    checksum = _create_checksum(hrp, data)
    combined = data + checksum
    try:
        encoded = "".join(BECH32_CHARSET[d] for d in combined)
    except IndexError:
        return None
    return f"{hrp}1{encoded}"


def _valoper_to_delegator(valoper: str) -> Optional[str]:
    """Convert a valoper address into its delegator bech32 form."""
    hrp, data = _bech32_decode(valoper)
    if not hrp or data is None:
        return None
    base_prefix = "core"
    if hrp.endswith("valoper"):
        base_prefix = hrp[: -len("valoper")] or base_prefix
    elif hrp.endswith("val"):
        base_prefix = hrp[: -len("val")] or base_prefix
    return _bech32_encode(base_prefix, data)


def _is_valoper_address(value: str) -> bool:
    """Check if the provided address is a Bech32 valoper string."""
    hrp, data = _bech32_decode(value)
    # _logger.info("Bech32 decode valoper check value=%s hrp=%s data=%s", value, hrp, data)
    if not hrp or data is None:
        return False
    return hrp.endswith("valoper")


def _safe_int(value: Any) -> int:
    """Safely convert incoming values into integers."""
    try:
        if isinstance(value, str):
            return int(value.split(".", 1)[0])
        if isinstance(value, (int, float)):
            return int(value)
        return int(value or 0)
    except (TypeError, ValueError, InvalidOperation):
        return 0


def _safe_float(value: Any) -> float:
    """Safely convert incoming values into floats."""
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def _extract_delegation_balance_amount(payload: Optional[Dict[str, Any]]) -> int:
    """Extract a delegation balance amount from a staking response payload."""
    if not isinstance(payload, dict):
        return 0

    delegation_response = payload.get("delegation_response") or payload.get("delegationResponse") or payload
    if not isinstance(delegation_response, dict):
        return 0

    balance = delegation_response.get("balance") or {}
    if not isinstance(balance, dict):
        return 0
    return _safe_int(balance.get("amount"))


def _fetch_self_delegation_amount(
    valoper: str,
    rpc_base_url: str,
    *,
    request_fn: Callable[..., Dict[str, Any]],
    delegator_address: Optional[str] = None,
) -> int:
    """Fetch the validator's self-delegated stake in base units."""
    validator_delegator = delegator_address or _valoper_to_delegator(valoper)
    if not validator_delegator:
        return 0

    paths = [
        f"/cosmos/staking/v1beta1/validators/{valoper}/delegations/{validator_delegator}",
        f"/cosmos/staking/v1beta1/delegators/{validator_delegator}/delegations/{valoper}",
    ]
    for path in paths:
        try:
            payload = request_fn(
                path,
                allow_404=True,
                base_url=rpc_base_url,
                timeout=10,
            ) or {}
        except TypeError:
            try:
                payload = request_fn(path, params=None, timeout=10) or {}
            except Exception:
                continue
        except Exception:
            continue

        if payload:
            return _extract_delegation_balance_amount(payload)

    return 0


def _validator_status_map(status: Optional[str]) -> Dict[str, str]:
    """Map Cosmos validator status codes into UI friendly metadata."""
    mapping = {
        "BOND_STATUS_BONDED": {"status": "active", "label": "Bonded"},
        "BOND_STATUS_UNBONDED": {"status": "inactive", "label": "Unbonded"},
        "BOND_STATUS_UNBONDING": {"status": "inactive", "label": "Unbonding"},
    }
    return mapping.get(status or "", {"status": "inactive", "label": status or "Unknown"})


def _nano_to_avax_number(value: int) -> float:
    """Convert Avalanche nano-denominated values to floats."""
    if value == 0:
        return 0.0
    return round(value / AVALANCHE_NANO, 9)


def _nano_to_avax_string(value: int) -> str:
    """Convert Avalanche nano-denominated values to decimal strings."""
    whole = value // AVALANCHE_NANO
    remainder = value % AVALANCHE_NANO
    if remainder == 0:
        return str(whole)
    frac = str(remainder).rjust(9, "0").rstrip("0")
    return f"{whole}.{frac}"


def _yocto_to_near_number(value: int) -> float:
    """Convert NEAR yoctoNEAR-denominated values to floats."""
    if value == 0:
        return 0.0
    return value / 1e24


def _ewx_load_substrate(rpc_url: str):
    """Return a SubstrateInterface instance for the provided EWX RPC URL."""
    try:
        from substrateinterface import SubstrateInterface  # type: ignore
    except ImportError as exc:  # pragma: no cover - optional dep
        raise LCDRequestError("Missing substrate-interface dependency for EWX") from exc

    endpoint = (rpc_url or "").strip()
    if endpoint.startswith("http://"):
        endpoint = endpoint.replace("http://", "ws://", 1)
    elif endpoint.startswith("https://"):
        endpoint = endpoint.replace("https://", "wss://", 1)
    if not endpoint:
        raise LCDRequestError("EWX RPC endpoint is not configured")

    try:
        return SubstrateInterface(url=endpoint, ss58_format=42)
    except Exception as exc:  # pragma: no cover - network failures
        raise LCDRequestError(f"Failed to connect to EWX RPC: {exc}") from exc


def _ewx_query(substrate, pallet: str, storage: str, params: Optional[List[Any]] = None) -> Optional[Any]:
    """Safely query a storage item and return its value."""
    try:
        result = substrate.query(pallet, storage, params or [])
        return result.value if result is not None else None
    except Exception:
        return None


def _ewx_normalize_amount(value: Any) -> int:
    """Normalize EWX stake fields into integers."""
    try:
        if isinstance(value, str):
            return int(value, 0) if value.startswith("0x") else int(value)
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def _planck_to_ewt_string(value: int) -> str:
    """Convert planck-denominated amounts (1e18) to human EWT strings."""
    whole = value // EWX_PLANCK
    remainder = value % EWX_PLANCK
    if remainder == 0:
        return str(whole)
    frac = str(remainder).rjust(18, "0").rstrip("0")
    return f"{whole}.{frac}"


def _planck_to_ewt_number(value: int) -> float:
    """Convert planck-denominated amounts (1e18) to float EWT."""
    if value == 0:
        return 0.0
    try:
        return float(Decimal(value) / Decimal(EWX_PLANCK))
    except (InvalidOperation, ValueError):
        return 0.0


def _ewx_fetch_rewards(
    address: str,
    ewx_reward_url: Optional[str] = None,
    ewx_api: Optional[str] = None,
) -> Dict[str, Any]:
    """Fetch reward history for an EWX address from Subscan."""
    if not ewx_reward_url or not ewx_api:
        try:
            config = request.env['ir.config_parameter'].sudo()
            if not ewx_reward_url:
                ewx_reward_url = config.get_param('ewx_reward_url')
            if not ewx_api:
                ewx_api = config.get_param('energy_web_api_key')
        except RuntimeError:
            _logger.warning("EWX config not provided and request context unavailable for %s", address)
            return {}
    ewx_reward_url = (ewx_reward_url or "").strip() or None
    ewx_api = (ewx_api or "").strip() or None
    if not ewx_reward_url:
        _logger.warning("ewx_reward_url not configured for %s", address)
        return {}
    if not ewx_api:
        _logger.warning("energy_web_api_key not configured for %s", address)
        return {}
    api_base = ewx_reward_url
    url = f"{api_base}/api/scan/account/reward_slash"
    headers = {
        "Content-Type": "application/json",
        "x-api-key": ewx_api,
    }
    row = 100
    page = 0
    total_pages = None
    rewards: List[Dict[str, Any]] = []
    total_planck = 0

    while True:
        payload = {"address": address, "row": row, "page": page}
        try:
            resp = requests.post(url, json=payload, headers=headers, timeout=20)
        except Exception as exc:  # pragma: no cover - network
            _logger.warning("EWX reward fetch failed for %s on page %s: %s", address, page, exc)
            return {}

        if resp.status_code != 200:
            _logger.warning("EWX reward fetch HTTP %s for %s on page %s: %s ---%s", resp.status_code, address, page, resp.text,resp)
            return {}

        try:
            data = resp.json()
        except ValueError:
            _logger.warning("EWX reward fetch invalid JSON for %s on page %s: %s", address, page, resp.text)
            return {}

        if data.get("code") != 0:
            _logger.warning("EWX reward fetch non-zero code for %s on page %s: %s", address, page, data)
            return {}

        data_block = data.get("data") or {}
        page_rewards = data_block.get("list") or []

        if total_pages is None:
            total_count = _safe_int(data_block.get("count"))
            if total_count > 0:
                total_pages = (total_count + row - 1) // row

        if not isinstance(page_rewards, list) or not page_rewards:
            break

        for entry in page_rewards:
            if not isinstance(entry, dict):
                continue
            rewards.append(entry)
            total_planck += _ewx_normalize_amount(entry.get("amount"))

        if total_pages is not None and page + 1 >= total_pages:
            break

        if len(page_rewards) < row:
            break

        page += 1
        if page >= 1000:
            _logger.warning("EWX reward fetch hit pagination safety limit for %s", address)
            break


    return {"total_planck": total_planck, "rewards": rewards}


def _ewx_fetch_token_balance(
    address: str,
    ewx_reward_url: Optional[str] = None,
    ewx_api: Optional[str] = None,
) -> int:
    """Fetch native EWT balance for an EWX address from Subscan."""
    if not ewx_reward_url or not ewx_api:
        try:
            config = request.env['ir.config_parameter'].sudo()
            if not ewx_reward_url:
                ewx_reward_url = config.get_param('ewx_reward_url')
            if not ewx_api:
                ewx_api = config.get_param('energy_web_api_key')
        except RuntimeError:
            _logger.warning("EWX config not provided and request context unavailable for %s", address)
            return 0
    ewx_reward_url = (ewx_reward_url or "").strip() or None
    ewx_api = (ewx_api or "").strip() or None
    if not ewx_reward_url:
        _logger.warning("ewx_reward_url not configured for %s", address)
        return 0
    if not ewx_api:
        _logger.warning("energy_web_api_key not configured for %s", address)
        return 0

    api_base = ewx_reward_url
    url = f"{api_base}/api/scan/account/tokens"
    headers = {
        "Content-Type": "application/json",
        "x-api-key": ewx_api,
    }
    payload = {"address": address, "row": 25, "page": 0}

    try:
        resp = requests.post(url, json=payload, headers=headers, timeout=20)
    except Exception as exc:  # pragma: no cover - network
        _logger.warning("EWX token balance fetch failed for %s: %s", address, exc)
        return 0

    if resp.status_code != 200:
        _logger.warning("EWX token balance fetch HTTP %s for %s: %s", resp.status_code, address, resp.text)
        return 0

    try:
        data = resp.json()
    except ValueError:
        _logger.warning("EWX token balance fetch invalid JSON for %s: %s", address, resp.text)
        return 0

    if data.get("code") != 0:
        _logger.warning("EWX token balance fetch non-zero code for %s: %s", address, data)
        return 0

    native_tokens = (data.get("data") or {}).get("native") or []
    if not isinstance(native_tokens, list):
        return 0

    for token in native_tokens:
        if not isinstance(token, dict):
            continue
        symbol = str(token.get("symbol") or "").strip().upper()
        unique_id = str(token.get("unique_id") or "").strip().upper()
        if symbol == "EWT" or unique_id == "EWT":
            return _ewx_normalize_amount(token.get("balance"))

    if native_tokens and isinstance(native_tokens[0], dict):
        return _ewx_normalize_amount(native_tokens[0].get("balance"))

    return 0


def _ewx_collect_delegators(state: Any) -> Tuple[List[Dict[str, Any]], int]:
    """Extract delegator entries and total stake from delegator state."""
    delegations_raw = []
    total = 0
    count = 0
    items: List[Dict[str, Any]] = []

    if isinstance(state, dict):
        delegations_raw = state.get("delegations") or state.get("delegators") or []
        total_field = state.get("total") or state.get("total_stake")
        if total_field is not None:
            try:
                total = _ewx_normalize_amount(total_field)
            except Exception:
                total = 0

    for entry in delegations_raw if isinstance(delegations_raw, list) else []:
        if not isinstance(entry, dict):
            continue
        delegator = entry.get("owner") or entry.get("delegator") or entry.get("who")
        amount = _ewx_normalize_amount(entry.get("amount") or entry.get("value") or entry.get("bond"))
        total += amount if total == 0 else 0  # if total not supplied, accumulate
        count += 1
        items.append(
            {
                "delegatorAddress": delegator,
                "amount": amount,
            }
        )

    return items, total if total > 0 else sum(e["amount"] for e in items), count if count > 0 else len(items)


def _ewx_validator_summary(
    valoper: str,
    rpc_base_url: str,
    ewx_reward_url: Optional[str] = None,
    ewx_api: Optional[str] = None,
) -> Dict[str, Any]:
    """Build a normalized summary for EWX (Substrate) validators."""
    substrate = _ewx_load_substrate(rpc_base_url)
    # _logger.info("Loaded Substrate interface for EWX RPC at %s", substrate)
    # Parachain staking pallet naming convention
    pallet = "ParachainStaking"

    candidate_info = _ewx_query(substrate, pallet, "CandidateInfo", [valoper])
    # _logger.info("EWX candidate info for %s: %s", valoper, candidate_info)
    if not candidate_info:
        raise LCDRequestError("Validator not found", status=404)

    own_stake = _ewx_normalize_amount(candidate_info.get("bond") or candidate_info.get("self_bond"))
    nomination_count = _ewx_normalize_amount(candidate_info.get("nomination_count"))
    total_counted = _ewx_normalize_amount(candidate_info.get("total_counted"))
    status= str(candidate_info.get("status") or "").lower()

    # adding chatgpt logic
    top_nominations = _ewx_query(
    substrate,
    pallet,
    "TopNominations",
    [valoper]
) 
    # _logger.info("EWX top nominations for %s: %s", valoper, top_nominations)

    # _logger.info("ParachainStaking metadata storage functions: %s", substrate.get_metadata_storage_functions("ParachainStaking"))

    # Try to pull richer delegator state if exposed
    delegator_state = _ewx_query(substrate, pallet, "DelegatorState", [valoper]) or {}
    # _logger.info("EWX delegator state for %s: %s", valoper, delegator_state)
    delegations, delegator_total_state, delegator_count_state = _ewx_collect_delegators(delegator_state)

    total_stake = total_counted if total_counted > 0 else own_stake
    delegator_stake = max(total_stake - own_stake, 0)
    delegator_count = nomination_count

    if delegations:
        delegator_stake = delegator_total_state or delegator_stake
        total_stake = own_stake + delegator_stake
        delegator_count = max(delegator_count_state, delegator_count)

    pool = _ewx_query(substrate, pallet, "CandidatePool") or []
    total_all = 0
    for entry in pool if isinstance(pool, list) else []:
        if isinstance(entry, dict):
            total_all += _ewx_normalize_amount(entry.get("amount"))
    voting_power_pct = _pct(total_stake, total_all) if total_all > 0 else 0.0

    comm = _ewx_query(substrate, pallet, "DefaultCollatorCommission") or {}
    commission_raw = None
    if isinstance(comm, dict):
        commission_raw = comm.get("current")
    commission_pct = 0.0
    if commission_raw is not None:
        try:
            commission_pct = (float(commission_raw) / 1_000_000_000.0) * 100.0
        except (TypeError, ValueError):
            commission_pct = 0.0

    # Fetch rewards from Subscan
    rewards_data = _ewx_fetch_rewards(valoper, ewx_reward_url=ewx_reward_url, ewx_api=ewx_api)
    total_rewards_planck = _ewx_normalize_amount(rewards_data.get("total_planck"))
    balance_planck = _ewx_fetch_token_balance(valoper, ewx_reward_url=ewx_reward_url, ewx_api=ewx_api)
    # _logger.info("EWX rewards for %s: %s", valoper, rewards_data)
    # _logger.info("EWX total rewards planck for %s: %d", valoper, total_rewards_planck)
    total_rewards_str = _planck_to_ewt_string(total_rewards_planck)
    balance_str = _planck_to_ewt_string(balance_planck)

    summary = {
        "tokens": _planck_to_ewt_string(total_stake),
        "ownedStake": _planck_to_ewt_string(own_stake),
        "totalStake": _planck_to_ewt_string(total_stake),
        "delegatorStake": _planck_to_ewt_string(delegator_stake),
        "votingPowerPct": voting_power_pct,
        "commissionPct": commission_pct,
        "outstandingRewards": _planck_to_ewt_number(total_rewards_planck),
        "ownedRewards": total_rewards_str,
        "delegationRewards": "0",
        "totalRewards": total_rewards_str,
        "balance": balance_str,
        "status": status,
        "statusLabel": status.capitalize(),
        "jailed": False,
        "connectionStatus": "unknown",
        "identity": None,
        "delegatorCount": delegator_count,
    }
    return summary


def _ewx_validator_delegations(
    valoper: str,
    rpc_base_url: str,
    cursor: Optional[str],
) -> Dict[str, Any]:
    """Return delegations (nominations) for an EWX validator."""
    substrate = _ewx_load_substrate(rpc_base_url)
    pallet = "ParachainStaking"

    candidate_info = _ewx_query(substrate, pallet, "CandidateInfo", [valoper]) or {}
    own_stake = _ewx_normalize_amount(candidate_info.get("bond") or candidate_info.get("self_bond"))

    top_nominations = _ewx_query(substrate, pallet, "TopNominations", [valoper]) or {}
    # _logger.info("EWX top nominations for %s: %s", valoper, top_nominations)

    nominations_raw = top_nominations.get("nominations") if isinstance(top_nominations, dict) else []
    nominations = nominations_raw if isinstance(nominations_raw, list) else []
    delegator_total = _ewx_normalize_amount(top_nominations.get("total")) if isinstance(top_nominations, dict) else 0
    if delegator_total == 0 and nominations:
        delegator_total = sum(
            _ewx_normalize_amount(
                (entry or {}).get("amount") or (entry or {}).get("value") or (entry or {}).get("bond")
            )
            for entry in nominations
            if isinstance(entry, dict)
        )

    items: List[Dict[str, Any]] = []
    total_stake = own_stake + max(delegator_total, 0)
    for entry in nominations:
        if not isinstance(entry, dict):
            continue
        amount = _ewx_normalize_amount(entry.get("amount") or entry.get("value") or entry.get("bond"))
        delegator_address = entry.get("owner") or entry.get("delegator") or entry.get("who")
        pct = _pct(amount, total_stake) if total_stake > 0 else 0.0
        items.append(
            {
                "delegatorAddress": delegator_address,
                "amount": _planck_to_ewt_string(amount),
                "denom": "EWT",
                "pctOfValidator": pct,
            }
        )

    return {
        "items": items,
        "nextCursor": None,
        "delegatorCount": len(items),
    }


def _atto_to_inj_number(value: int) -> float:
    """Convert Injective atto-denominated values to floats."""
    if value == 0:
        return 0.0
    try:
        return float(Decimal(value) / Decimal(INJECTIVE_ATTO))
    except (InvalidOperation, ValueError):
        return 0.0


def _atto_to_inj_string(value: int) -> str:
    """Convert Injective atto-denominated values to decimal strings."""
    whole = value // INJECTIVE_ATTO
    remainder = value % INJECTIVE_ATTO
    if remainder == 0:
        return str(whole)
    frac = str(remainder).rjust(18, "0").rstrip("0")
    return f"{whole}.{frac}"


def _wei_to_skl_string(value: int) -> str:
    """Convert SKALE wei-denominated values to decimal strings."""
    whole = value // SKALE_WEI
    remainder = value % SKALE_WEI
    if remainder == 0:
        return str(whole)
    frac = str(remainder).rjust(18, "0").rstrip("0")
    return f"{whole}.{frac}"


def _wei_to_skl_number(value: int) -> float:
    """Convert SKALE wei-denominated values to floats."""
    if value == 0:
        return 0.0
    return round(value / SKALE_WEI, 6)


def _subsquid_int_to_string(value: int) -> str:
    """Convert Subsquid base unit amounts (18 decimals) to decimal strings."""
    whole = value // SUBSQUID_DECIMALS
    remainder = value % SUBSQUID_DECIMALS
    if remainder == 0:
        return str(whole)
    frac = str(remainder).rjust(18, "0").rstrip("0")
    return f"{whole}.{frac}"


def _subsquid_int_to_number(value: int) -> float:
    """Convert Subsquid base unit amounts to floats rounded for display."""
    if value == 0:
        return 0.0
    try:
        return float(Decimal(value) / Decimal(SUBSQUID_DECIMALS))
    except (InvalidOperation, ValueError):
        return 0.0


def _subsquid_resolve_graphql_url(rpc_base_url: Optional[str]) -> str:
    """Resolve the Subsquid GraphQL endpoint, appending /graphql when needed."""
    url = (rpc_base_url or "").strip()
    if not url:
        raise LCDRequestError("Subsquid GraphQL endpoint is not configured")

    normalized = url.rstrip("/")
    if "graphql" not in normalized.split("?")[0]:
        normalized = f"{normalized}/graphql"
    return normalized


def _subsquid_query_worker(peer_id: str, rpc_base_url: Optional[str]) -> Dict[str, Any]:
    """Fetch a Subsquid worker by peer ID from the GraphQL API."""
    if not peer_id:
        raise LCDRequestError("Subsquid peer ID is required")

    endpoint = _subsquid_resolve_graphql_url(rpc_base_url)
    query = """
        query workerByPeerId($peerId: String!) {
            workers(where: {peerId_eq: $peerId}, limit: 1) {
                id
                name
                peerId
                status
                online
                jailed
                dialOk
                jailReason
                statusHistory(orderBy: id_DESC, limit: 1) {
                    blockNumber
                    pending
                    timestamp
                }
                version
                createdAt
                uptime90Days
                uptime24Hours
                apr
                stakerApr
                totalDelegation
                capedDelegation
                delegationCount
                locked
                lockEnd
                owner {
                    id
                    type
                    owner {
                        id
                    }
                }
                bond
                claimableReward
                claimedReward
                totalDelegationRewards
                queries24Hours
                queries90Days
                scannedData24Hours
                scannedData90Days
                servedData24Hours
                servedData90Days
                storedData
                website
                email
                description
                dayUptimes {
                    timestamp
                    uptime
                }
                delegations(where: {deposit_gt: 0}) {
                    deposit
                    claimableReward
                    claimedReward
                    locked
                    lockEnd
                    owner {
                        id
                        type
                        owner {
                            id
                        }
                    }
                }
            }
        }
        """

    payload = {
        "query": query,
        "variables": {"peerId": peer_id},
    }

    try:
        resp = requests.post(
            endpoint,
            json=payload,
            headers={"Content-Type": "application/json"},
            timeout=SUBSQUID_GRAPHQL_TIMEOUT,
        )
        resp.raise_for_status()
        data = resp.json()
    except requests.RequestException as exc:
        raise LCDRequestError(f"Subsquid GraphQL request failed: {exc}")
    except ValueError as exc:
        raise LCDRequestError(f"Subsquid GraphQL invalid response: {exc}")

    if isinstance(data, dict) and data.get("errors"):
        _logger.warning("Subsquid GraphQL errors peer_id=%s errors=%s", peer_id, data.get("errors"))
        raise LCDRequestError("Subsquid GraphQL returned errors", status=502)

    workers = (data.get("data") or {}).get("workers") if isinstance(data, dict) else None
    if not workers:
        raise LCDRequestError("Validator not found", status=404)

    worker = workers[0] if isinstance(workers, list) else None
    if not isinstance(worker, dict):
        raise LCDRequestError("Subsquid worker payload is invalid")

    return worker


def _subsquid_validator_summary(peer_id: str, rpc_base_url: Optional[str]) -> Dict[str, Any]:
    """Build a normalized summary from Subsquid worker metadata."""
    worker = _subsquid_query_worker(peer_id, rpc_base_url)
    _logger.info("Fetched Subsquid worker for peer_id=%s: %s", peer_id, worker)
    bond_raw = max(_safe_int(worker.get("bond")), 0)
    delegation_raw = max(_safe_int(worker.get("totalDelegation")), 0)
    total_stake_raw = bond_raw + delegation_raw
    claimable_raw = max(_safe_int(worker.get("claimableReward")), 0)
    claimed_raw = max(_safe_int(worker.get("claimedReward")), 0)
    delegation_rewards_raw = max(_safe_int(worker.get("totalDelegationRewards")), 0)
    # total_rewards_raw = claimable_raw + claimed_raw + delegation_rewards_raw
    total_rewards_raw = claimable_raw + claimed_raw

    uptime_24h = _safe_float(worker.get("uptime24Hours"))
    uptime_90d = _safe_float(worker.get("uptime90Days"))
    status_value = str(worker.get("status") or "active").lower()
    status_label = status_value.capitalize()
    connection_status = "connected" if worker.get("online") is True else (
        "disconnected" if worker.get("online") is False else "unknown"
    )

 
    summary = {
        "tokens": _subsquid_int_to_string(total_stake_raw),
        "ownedStake": _subsquid_int_to_string(bond_raw),
        "totalStake": _subsquid_int_to_string(total_stake_raw),
        "delegatorStake": _subsquid_int_to_string(delegation_raw),
        "outstandingRewards": _subsquid_int_to_string(claimable_raw),
        "ownedRewards": _subsquid_int_to_string(claimable_raw),
        "delegationRewards": _subsquid_int_to_string(delegation_rewards_raw),
        # "delegationCapacity": delegation_capacity,
        "totalRewards": _subsquid_int_to_string(total_rewards_raw),
        "uptimePct": uptime_24h,
        "uptime24Hours": uptime_24h,
        "uptime90Days": uptime_90d,
        "status": status_value,
        "statusLabel": status_label,
        "jailed": bool(worker.get("jailed")),
        "connectionStatus": connection_status,
        "moniker": worker.get("name"),
        "peerId": worker.get("peerId"),
        "delegatorCount": worker.get("delegationCount"),
        "workerAPR": _safe_float(worker.get("apr")),
        "delegatorAPR": _safe_float(worker.get("stakerApr")),
        "website": worker.get("website"),
        "email": worker.get("email"),
        "description": worker.get("description"),
    }

    return summary


def _subsquid_validator_delegations(
    peer_id: str,
    rpc_base_url: Optional[str],
) -> Dict[str, Any]:
    """Return delegations for a Subsquid worker from GraphQL payload."""
    worker = _subsquid_query_worker(peer_id, rpc_base_url)
    delegations = worker.get("delegations") or []

    bond_raw = max(_safe_int(worker.get("bond")), 0)
    delegation_raw = max(_safe_int(worker.get("totalDelegation")), 0)
    total_stake_raw = bond_raw + delegation_raw

    items: List[Dict[str, Any]] = []
    for entry in delegations if isinstance(delegations, list) else []:
        if not isinstance(entry, dict):
            continue
        amount_raw = max(_safe_int(entry.get("deposit")), 0)
        if amount_raw <= 0:
            continue
        pct = _pct(amount_raw, total_stake_raw) if total_stake_raw > 0 else 0.0
        owner = entry.get("owner") or {}
        owner_id = None
        if isinstance(owner, dict):
            owner_id = owner.get("id")
        items.append(
            {
                "delegatorAddress": owner_id,
                "amount": _subsquid_int_to_string(amount_raw),
                "denom": "SQD",
                "pctOfValidator": pct,
            }
        )

    return {
        "items": items,
        "nextCursor": None,
        "delegatorCount": len(items),
    }


def _theta_normalize_amount(value: Any) -> int:
    """Convert Theta stake amounts into raw wei integers."""
    try:
        return int(str(value))
    except (TypeError, ValueError):
        return 0


def _theta_amount_to_string(value: int) -> str:
    """Convert Theta wei amounts to a decimal THETA string."""
    whole = value // THETA_WEI
    remainder = value % THETA_WEI
    if remainder == 0:
        return str(whole)
    frac = str(remainder).rjust(18, "0").rstrip("0")
    return f"{whole}.{frac}"


def _theta_amount_to_number(value: int) -> float:
    """Convert Theta wei amounts to float for display."""
    if value == 0:
        return 0.0
    try:
        return float(Decimal(value) / Decimal(THETA_WEI))
    except (InvalidOperation, ValueError):
        return 0.0


def _theta_fetch_stake_body(address: str, base_url: Optional[str]) -> Dict[str, Any]:
    """Fetch stake records for a Theta node/operator from explorer API."""
    resolved_base = (base_url or "").rstrip("/")
    if not resolved_base:
        raise LCDRequestError("Protocol RPC endpoint is not configured")

    url = f"{resolved_base}/api/stake/{address}"
    try:
        resp = requests.get(url, timeout=15)
        resp.raise_for_status()
    except requests.RequestException as exc:
        raise LCDRequestError(f"Theta stake request failed: {exc}") from exc

    try:
        payload = resp.json()
    except ValueError as exc:
        raise LCDRequestError("Invalid JSON received from Theta stake API") from exc

    body = payload.get("body") if isinstance(payload, dict) else None
    if not isinstance(body, dict):
        raise LCDRequestError("Theta stake response missing body")

    return body


def _theta_fetch_account(address: str, base_url: Optional[str]) -> Dict[str, Any]:
    """Fetch Theta account balances and sequence from explorer API."""
    resolved_base = (base_url or "").rstrip("/")
    if not resolved_base:
        raise LCDRequestError("Protocol RPC endpoint is not configured")

    url = f"{resolved_base}/api/account/{address}"
    try:
        resp = requests.get(url, timeout=15)
        resp.raise_for_status()
    except requests.RequestException as exc:
        raise LCDRequestError(f"Theta account request failed: {exc}") from exc

    try:
        payload = resp.json()
    except ValueError as exc:
        raise LCDRequestError("Invalid JSON received from Theta account API") from exc

    if not isinstance(payload, dict):
        raise LCDRequestError("Theta account response invalid")

    return payload


def _theta_fetch_account_transactions(
    address: str,
    base_url: Optional[str],
    params: Dict[str, Any],
) -> Dict[str, Any]:
    """Fetch paginated Theta account transactions from explorer API."""
    resolved_base = (base_url or "").rstrip("/")
    if not resolved_base:
        raise LCDRequestError("Protocol RPC endpoint is not configured")

    safe_params: Dict[str, Any] = {}
    for key, value in (params or {}).items():
        if value is None:
            continue
        if key == "types" and isinstance(value, (list, tuple)):
            normalized_types = [str(item).strip() for item in value if str(item).strip()]
            if normalized_types:
                safe_params[key] = json.dumps(normalized_types)
            continue
        if isinstance(value, bool):
            safe_params[key] = str(value).lower()
            continue
        safe_params[key] = value

    url = f"{resolved_base}/api/accounttx/{address}"
    try:
        resp = requests.get(url, params=safe_params, timeout=15)
        resp.raise_for_status()
    except requests.RequestException as exc:
        raise LCDRequestError(f"Theta account transactions request failed: {exc}") from exc

    try:
        payload = resp.json()
    except ValueError as exc:
        raise LCDRequestError("Invalid JSON received from Theta account transactions API") from exc

    if not isinstance(payload, dict):
        raise LCDRequestError("Theta account transactions response invalid")

    return payload


def _theta_collect_delegations(body: Dict[str, Any], node_operator: str) -> Tuple[List[Dict[str, Any]], int, int, int]:
    """Normalize Theta delegations and compute stake totals."""
    records = body.get("holderRecords") if isinstance(body, dict) else []
    node_lower = (node_operator or "").lower()
    items_raw: List[Tuple[Optional[str], int]] = []
    total_stake_raw = 0
    operator_stake_raw = 0

    for entry in records if isinstance(records, list) else []:
        if not isinstance(entry, dict):
            continue
        amount_raw = _theta_normalize_amount(entry.get("amount"))
        source = (entry.get("source") or "")
        holder = (entry.get("holder") or "")
        total_stake_raw += amount_raw
        if isinstance(source, str) and source.lower() == node_lower:
            operator_stake_raw += amount_raw
        display_address = None
        if isinstance(source, str) and source.strip():
            display_address = source.strip()
        elif isinstance(holder, str) and holder.strip():
            display_address = holder.strip()
        items_raw.append((display_address, amount_raw))

    delegations: List[Dict[str, Any]] = []
    for address, amount_raw in items_raw:
        pct = _pct(amount_raw, total_stake_raw) if total_stake_raw > 0 else 0.0
        delegations.append(
            {
                "delegatorAddress": address,
                "amount": _theta_amount_to_string(amount_raw),
                "denom": "THETA",
                "pctOfValidator": pct,
            }
        )

    return delegations, total_stake_raw, operator_stake_raw, len(delegations)


def _theta_validator_summary(valoper: str, rpc_base_url: Optional[str]) -> Dict[str, Any]:
    """Build a normalized summary using Theta explorer stake data."""
    body = _theta_fetch_stake_body(valoper, rpc_base_url)
    delegations, total_stake_raw, operator_stake_raw, delegator_count = _theta_collect_delegations(body, valoper)
    theta_balance = None
    tfuel_balance = None
    sequence = None
    try:
        account_payload = _theta_fetch_account(valoper, rpc_base_url)
        account_body = account_payload.get("body") if isinstance(account_payload, dict) else None
        account_data = account_body if isinstance(account_body, dict) else account_payload if isinstance(account_payload, dict) else None
        balance = account_data.get("balance") if isinstance(account_data, dict) else None
        if isinstance(balance, dict):
            theta_balance = _theta_amount_to_string(_theta_normalize_amount(balance.get("thetawei")))
            tfuel_balance = _theta_amount_to_string(_theta_normalize_amount(balance.get("tfuelwei")))
        sequence = account_data.get("sequence") if isinstance(account_data, dict) else None
    except LCDRequestError as exc:
        _logger.warning("Theta account fetch failed for %s: %s", valoper, exc)
    summary = {
        "tokens": _theta_amount_to_string(total_stake_raw),
        "delegatorCount": delegator_count,
    }
    if theta_balance is not None:
        summary["thetaBalance"] = theta_balance
    if tfuel_balance is not None:
        summary["tfuelBalance"] = tfuel_balance
    if sequence is not None:
        summary["sequence"] = sequence
    return summary


def _theta_validator_delegations(
    valoper: str,
    rpc_base_url: Optional[str],
    cursor: Optional[str],
) -> Dict[str, Any]:
    """Return Theta delegations using explorer stake data."""
    body = _theta_fetch_stake_body(valoper, rpc_base_url)
    delegations, _, _, delegator_count = _theta_collect_delegations(body, valoper)

    return {
        "items": delegations,
        "nextCursor": None,
        "delegatorCount": delegator_count,
    }


def _avalanche_fetch_validator(node_id: str, base_url: str) -> Optional[Dict[str, Any]]:
    """Fetch a single Avalanche validator definition."""
    result = _call_avalanche_rpc(
        base_url,
        "platform.getCurrentValidators",
        params={"nodeIDs": [node_id], "includeDelegators": True},
    )
    # _logger.info(
        # "Avalanche RPC validators response node_id=%s validators=%s",
        # node_id,
        # json.dumps(result.get("validators"), default=str),
    # )
    validators = result.get("validators") or []
    for validator in validators:
        if validator.get("nodeID") == node_id:
            return validator
    return None


def _near_find_validator(
    valoper: str,
    base_url: str,
) -> Tuple[Dict[str, Any], Dict[str, Any]]:
    """Find NEAR validator in current validators and return validator dict with full payload."""
    validators_payload = _call_near_rpc(base_url, "validators", [None])
    if not isinstance(validators_payload, dict):
        raise LCDRequestError("Invalid NEAR validators response")

    current_validators = validators_payload.get("current_validators") or []
    
    validator = None
    for item in current_validators:
        if isinstance(item, dict) and item.get("account_id") == valoper:
            validator = item
            break

    if not isinstance(validator, dict):
        raise LCDRequestError("Validator not found", status=404)

    return validator


def _avalanche_summary_from_validator(validator: Dict[str, Any]) -> Dict[str, Any]:
    """Build a normalized summary from an Avalanche validator payload."""
    # _logger.info("Building Avalanche validator summary from payload: %s", validator)
    validator_stake_nano = _safe_int(validator.get("stakeAmount") or validator.get("weight"))
    delegator_weight_nano = _safe_int(validator.get("delegatorWeight"))
    commission_pct = _safe_float(validator.get("delegationFee"))
    uptime_pct = _safe_float(validator.get("uptime"))
    connected = bool(validator.get("connected"))
    potential_reward_nano = _safe_int(validator.get("potentialReward"))
    reward_owner = validator.get("rewardOwner") or {}
    addresses = reward_owner.get("addresses") or []
    identity = addresses[0] if addresses else ""

    # accruedDelegateeReward tracks delegation fees from delegators whose staking
    # period has already finished. Active delegators' fees come from their individual
    # potentialReward fields. Both must be summed for the true delegation fee total.
    accrued_delegatee_reward_nano = _safe_int(validator.get("accruedDelegateeReward"))

    delegator_entries = validator.get("delegators") or []
    active_delegator_reward_nano = 0
    delegator_weight_from_list = 0
    for item in delegator_entries:
        active_delegator_reward_nano += _safe_int(item.get("potentialReward"))
        delegator_weight_from_list += _safe_int(item.get("stakeAmount") or item.get("weight"))

    if delegator_weight_nano == 0 and delegator_entries:
        delegator_weight_nano = delegator_weight_from_list

    # Total delegation fee rewards = fees from completed delegations + fees from active delegations
    delegation_fee_total_nano = accrued_delegatee_reward_nano + active_delegator_reward_nano
    total_stake_nano = validator_stake_nano + delegator_weight_nano
    total_rewards_nano = potential_reward_nano + delegation_fee_total_nano
    start_date = _as_date(validator.get("startTime"))
    end_date = _as_date(validator.get("endTime"))

    summary = {
        "tokens": _nano_to_avax_string(total_stake_nano),
        "votingPowerPct": 0.0,
        "commissionPct": commission_pct,
        "outstandingRewards": _nano_to_avax_number(total_rewards_nano),
        "ownedRewards": _nano_to_avax_string(potential_reward_nano),
        "delegationRewards": _nano_to_avax_string(delegation_fee_total_nano),
        "totalRewards": _nano_to_avax_string(total_rewards_nano),
        "uptimePct": uptime_pct,
        "status": "active",
        "statusLabel": "Active",
        "jailed": False,
        "connectionStatus": "connected" if connected else "disconnected",
        "identity": identity,
        "startDate": start_date or validator.get("startTime"),
        "endDate": end_date or validator.get("endTime"),
    }
    return summary


def _avalanche_delegations_from_validator(
    validator: Dict[str, Any],
) -> List[Dict[str, Any]]:
    """Convert an Avalanche validator payload into delegation entries."""
    delegations = []
    validator_stake = max(_safe_int(validator.get("stakeAmount") or validator.get("weight")), 0)
    delegator_weight = _safe_int(validator.get("delegatorWeight"))
    delegator_total_from_list = 0
    delegator_items = validator.get("delegators") or []

    for item in delegator_items:
        amount_nano = _safe_int(item.get("stakeAmount") or item.get("weight"))
        delegator_total_from_list += amount_nano
        owner = item.get("rewardOwner") or {}
        addresses = owner.get("addresses") or []
        address = item.get("rewardAddress") or (addresses[0] if addresses else None)
        delegations.append(
            {
                "delegatorAddress": address,
                "amount": _nano_to_avax_string(amount_nano),
                "denom": "AVAX",
                "pctOfValidator": 0.0,  # updated below once totals are known
            }
        )

    if delegator_weight == 0 and delegator_total_from_list:
        delegator_weight = delegator_total_from_list

    total_stake = validator_stake + delegator_weight
    if total_stake > 0:
        for entry, item in zip(delegations, delegator_items):
            amount_nano = _safe_int(item.get("stakeAmount") or item.get("weight"))
            entry["pctOfValidator"] = _pct(amount_nano, total_stake)

    return delegations


def _injective_fetch_validator(valoper: str, base_url: str) -> Optional[Dict[str, Any]]:
    """Fetch a single Injective validator definition."""
    payload = _lcd_get_json(
        f"/cosmos/staking/v1beta1/validators/{valoper}",
        allow_404=True,
        base_url=base_url,
    )
    if payload and isinstance(payload.get("validator"), dict):
        return payload["validator"]
    return None


def _injective_validator_summary(valoper: str, rpc_base_url: str) -> Dict[str, Any]:
    """Build a normalized summary for Injective validators."""
    validator = _injective_fetch_validator(valoper, rpc_base_url)
    if not validator:
        raise LCDRequestError("Validator not found", status=404)

    tokens_atto = max(_safe_int(validator.get("tokens")), 0)
    total_bonded = _get_total_bonded_tokens(base_url=rpc_base_url)
    voting_power_pct = _pct(tokens_atto, total_bonded)

    commission_str = (
        ((validator.get("commission") or {}).get("commission_rates") or {}).get("rate")
    )
    try:
        commission_pct = float(commission_str) * 100 if commission_str is not None else 0.0
    except (TypeError, ValueError):
        commission_pct = 0.0

    outstanding_rewards_atto = 0
    try:
        outstanding_payload = _lcd_get_json(
            f"/cosmos/distribution/v1beta1/validators/{valoper}/outstanding_rewards",
            allow_404=True,
            base_url=rpc_base_url,
        ) or {}
        outstanding_rewards_atto = _sum_coin_amounts(outstanding_payload.get("rewards", {}).get("rewards", []))
    except LCDRequestError:
        outstanding_rewards_atto = 0

    commission_rewards_atto = 0
    try:
        commission_payload = _lcd_get_json(
            f"/cosmos/distribution/v1beta1/validators/{valoper}/commission",
            allow_404=True,
            base_url=rpc_base_url,
        ) or {}
        commission_rewards_atto = _sum_coin_amounts(
            ((commission_payload.get("commission") or {}).get("commission")) or []
        )
    except LCDRequestError:
        commission_rewards_atto = 0

    delegation_rewards_atto = max(outstanding_rewards_atto - commission_rewards_atto, 0)
    total_rewards_atto = commission_rewards_atto + delegation_rewards_atto

    status_meta = _validator_status_map(validator.get("status"))
    jailed = bool(validator.get("jailed"))
    identity = (validator.get("description") or {}).get("identity")

    uptime_pct = 0.0
    connection_status = "unknown"
    window_size = None
    try:
        slashing_payload = _lcd_get_json(
            "/cosmos/slashing/v1beta1/params",
            base_url=rpc_base_url,
        )
        window_size = _safe_int((slashing_payload.get("params") or {}).get("signed_blocks_window"))
    except LCDRequestError:
        window_size = None

    pubkey_info = validator.get("consensus_pubkey")
    pubkey = None
    if isinstance(pubkey_info, dict):
        pubkey = pubkey_info.get("key")
    elif isinstance(pubkey_info, str):
        pubkey = pubkey_info

    valcons_addr = None
    if pubkey:
        try:
            validator_set_payload = _lcd_get_json(
                "/cosmos/base/tendermint/v1beta1/validatorsets/latest",
                base_url=rpc_base_url,
            ) or {}
            for item in validator_set_payload.get("validators") or []:
                pub = item.get("pub_key") or {}
                if pub.get("key") == pubkey:
                    valcons_addr = item.get("address")
                    break
        except LCDRequestError:
            pass

    missed_counter = None
    if valcons_addr:
        page_key = None
        connection_status = "disconnected"
        for _ in range(20):
            params = {}
            if page_key:
                params["pagination.key"] = page_key
            signing_payload = _lcd_get_json(
                "/cosmos/slashing/v1beta1/signing_infos",
                params=params if params else None,
                base_url=rpc_base_url,
            )
            if not signing_payload:
                break
            for info in signing_payload.get("info") or []:
                if info.get("address") == valcons_addr:
                    missed_counter = _safe_int(info.get("missed_blocks_counter"))
                    connection_status = "connected"
                    break
            if connection_status == "connected":
                break
            page_key = (signing_payload.get("pagination") or {}).get("next_key")
            if not page_key:
                break

    if (
        window_size
        and missed_counter is not None
        and window_size > 0
        and missed_counter <= window_size
    ):
        uptime_pct = _pct(max(window_size - missed_counter, 0), window_size)

    delegator_count = 0
    try:
        params: Dict[str, Any] = {
            "pagination.count_total": "true",
            "pagination.limit": 1,
        }
        delegations_payload = _lcd_get_json(
            f"/cosmos/staking/v1beta1/validators/{valoper}/delegations",
            params=params,
            base_url=rpc_base_url,
            timeout=10,
        ) or {}
        delegator_count = (delegations_payload.get("pagination") or {}).get("total") or 0
    except LCDRequestError:
        pass

    summary = {
        "tokens": _atto_to_inj_string(tokens_atto),
        "votingPowerPct": voting_power_pct,
        "commissionPct": commission_pct,
        "outstandingRewards": _atto_to_inj_number(outstanding_rewards_atto),
        "ownedRewards": _atto_to_inj_string(commission_rewards_atto),
        "delegationRewards": _atto_to_inj_string(delegation_rewards_atto),
        "totalRewards": _atto_to_inj_string(total_rewards_atto),
        "uptimePct": uptime_pct,
        "status": status_meta["status"],
        "statusLabel": status_meta.get("label"),
        "jailed": jailed,
        "connectionStatus": connection_status,
        "identity": identity,
        "delegatorCount": delegator_count,
    }
    return summary


def _injective_validator_delegations(
    valoper: str,
    rpc_base_url: str,
    cursor: Optional[str],
) -> Dict[str, Any]:
    """Return a paginated delegator list for an Injective validator."""
    validator = _injective_fetch_validator(valoper, rpc_base_url)
    if not validator:
        raise LCDRequestError("Validator not found", status=404)

    validator_tokens = max(_safe_int(validator.get("tokens")), 0)

    params: Dict[str, Any] = {
        "pagination.limit": LCD_PAGINATION_LIMIT,
    }
    if cursor:
        params["pagination.key"] = cursor

    delegations_payload = _lcd_get_json(
        f"/cosmos/staking/v1beta1/validators/{valoper}/delegations",
        params=params if params else None,
        base_url=rpc_base_url,
    ) or {}

    items: List[Dict[str, Any]] = []
    for delegation in delegations_payload.get("delegation_responses") or []:
        balance = (delegation.get("balance") or {})
        amount_atto = _safe_int(balance.get("amount"))
        pct_of_validator = 0.0
        if validator_tokens > 0:
            pct_of_validator = _pct(amount_atto, validator_tokens)

        items.append(
            {
                "delegatorAddress": (delegation.get("delegation") or {}).get("delegator_address"),
                "amount": _atto_to_inj_string(amount_atto),
                "denom": "INJ",
                "pctOfValidator": pct_of_validator,
            }
        )

    return {
        "items": items,
        "nextCursor": (delegations_payload.get("pagination") or {}).get("next_key"),
    }


# ---------------------------------------------------------------------------
# Cosmos Hub helpers
# ---------------------------------------------------------------------------


def _uatom_to_atom_string(value: int) -> str:
    """Convert uatom amounts to human-readable ATOM strings."""
    whole = value // COSMOS_UATOM
    remainder = value % COSMOS_UATOM
    if remainder == 0:
        return str(whole)
    frac = str(remainder).rjust(6, "0").rstrip("0")
    return f"{whole}.{frac}"


def _uatom_to_atom_number(value: int) -> float:
    """Convert uatom amounts to float ATOM."""
    if value == 0:
        return 0.0
    return round(value / COSMOS_UATOM, 6)


def _cosmos_fetch_validator(valoper: str, base_url: str) -> Optional[Dict[str, Any]]:
    """Fetch a single Cosmos Hub validator definition."""
    payload = _lcd_get_json(
        f"/cosmos/staking/v1beta1/validators/{valoper}",
        allow_404=True,
        base_url=base_url,
    )
    if payload and isinstance(payload.get("validator"), dict):
        return payload["validator"]
    return None


def _cosmos_validator_summary(valoper: str, rpc_base_url: str) -> Dict[str, Any]:
    """Build a normalized summary for Cosmos Hub validators."""
    validator = _cosmos_fetch_validator(valoper, rpc_base_url)
    if not validator:
        raise LCDRequestError("Validator not found", status=404)

    tokens_uatom = max(_safe_int(validator.get("tokens")), 0)
    total_bonded = _get_total_bonded_tokens(base_url=rpc_base_url)
    voting_power_pct = _pct(tokens_uatom, total_bonded)

    description = validator.get("description") or {}
    moniker = (description.get("moniker") or "").strip()
    website = (description.get("website") or "").strip()
    identity = (description.get("identity") or "").strip()
    email = (description.get("security_contact") or "").strip()
    description_details = (description.get("details") or "").strip()

    commission_str = (
        ((validator.get("commission") or {}).get("commission_rates") or {}).get("rate")
    )
    try:
        commission_pct = float(commission_str) * 100 if commission_str is not None else 0.0
    except (TypeError, ValueError):
        commission_pct = 0.0

    outstanding_rewards_uatom = 0
    try:
        outstanding_payload = _lcd_get_json(
            f"/cosmos/distribution/v1beta1/validators/{valoper}/outstanding_rewards",
            allow_404=True,
            base_url=rpc_base_url,
        ) or {}
        outstanding_rewards_uatom = _sum_coin_amounts(
            outstanding_payload.get("rewards", {}).get("rewards", [])
        )
    except LCDRequestError:
        outstanding_rewards_uatom = 0

    commission_rewards_uatom = 0
    try:
        commission_payload = _lcd_get_json(
            f"/cosmos/distribution/v1beta1/validators/{valoper}/commission",
            allow_404=True,
            base_url=rpc_base_url,
        ) or {}
        commission_rewards_uatom = _sum_coin_amounts(
            ((commission_payload.get("commission") or {}).get("commission")) or []
        )
    except LCDRequestError:
        commission_rewards_uatom = 0

    # Fetch self-delegation rewards (validator's self-stake earnings)
    self_delegation_rewards_uatom = 0
    try:
        delegator_addr = _valoper_to_delegator(valoper)
        if delegator_addr:
            self_rewards_payload = _lcd_get_json(
                f"/cosmos/distribution/v1beta1/delegators/{delegator_addr}/rewards/{valoper}",
                allow_404=True,
                base_url=rpc_base_url,
            ) or {}
            self_delegation_rewards_uatom = _sum_coin_amounts(
                self_rewards_payload.get("rewards") or []
            )
    except (LCDRequestError, Exception):
        self_delegation_rewards_uatom = 0

    # Validator total earnings = commission + self-delegation rewards
    total_rewards_uatom = commission_rewards_uatom + self_delegation_rewards_uatom

    status_meta = _validator_status_map(validator.get("status"))
    jailed = bool(validator.get("jailed"))

    uptime_pct = 0.0
    connection_status = "unknown"
    window_size = None
    try:
        slashing_payload = _lcd_get_json(
            "/cosmos/slashing/v1beta1/params",
            base_url=rpc_base_url,
        )
        window_size = _safe_int((slashing_payload.get("params") or {}).get("signed_blocks_window"))
    except LCDRequestError:
        window_size = None

    pubkey_info = validator.get("consensus_pubkey")
    pubkey = None
    if isinstance(pubkey_info, dict):
        pubkey = pubkey_info.get("key")
    elif isinstance(pubkey_info, str):
        pubkey = pubkey_info

    valcons_addr = None
    if pubkey:
        try:
            validator_set_payload = _lcd_get_json(
                "/cosmos/base/tendermint/v1beta1/validatorsets/latest",
                base_url=rpc_base_url,
            ) or {}
            for item in validator_set_payload.get("validators") or []:
                pub = item.get("pub_key") or {}
                if pub.get("key") == pubkey:
                    valcons_addr = item.get("address")
                    break
        except LCDRequestError:
            pass

    missed_counter = None
    if valcons_addr:
        page_key = None
        connection_status = "disconnected"
        for _ in range(20):
            params = {}
            if page_key:
                params["pagination.key"] = page_key
            signing_payload = _lcd_get_json(
                "/cosmos/slashing/v1beta1/signing_infos",
                params=params if params else None,
                base_url=rpc_base_url,
            )
            if not signing_payload:
                break
            for info in signing_payload.get("info") or []:
                if info.get("address") == valcons_addr:
                    missed_counter = _safe_int(info.get("missed_blocks_counter"))
                    connection_status = "connected"
                    break
            if connection_status == "connected":
                break
            page_key = (signing_payload.get("pagination") or {}).get("next_key")
            if not page_key:
                break

    if (
        window_size
        and missed_counter is not None
        and window_size > 0
        and missed_counter <= window_size
    ):
        uptime_pct = _pct(max(window_size - missed_counter, 0), window_size)

    delegator_count = 0
    try:
        params: Dict[str, Any] = {
            "pagination.count_total": "true",
            "pagination.limit": 1,
        }
        delegations_payload = _lcd_get_json(
            f"/cosmos/staking/v1beta1/validators/{valoper}/delegations",
            params=params,
            base_url=rpc_base_url,
            timeout=10,
        ) or {}
        delegator_count = _safe_int(
            (delegations_payload.get("pagination") or {}).get("total")
        )
    except LCDRequestError:
        pass

    summary = {
        "tokens": _uatom_to_atom_string(tokens_uatom),
        "votingPowerPct": voting_power_pct,
        "commissionPct": commission_pct,
        "outstandingRewards": _uatom_to_atom_number(outstanding_rewards_uatom),
        "ownedRewards": _uatom_to_atom_string(self_delegation_rewards_uatom),
        "totalRewards": _uatom_to_atom_string(total_rewards_uatom),
        "commissionRewards": _uatom_to_atom_string(commission_rewards_uatom),
        "uptimePct": uptime_pct,
        "status": status_meta["status"],
        "statusLabel": status_meta.get("label"),
        "jailed": jailed,
        "connectionStatus": connection_status,
        "identity": identity,
        "moniker": moniker,
        "website": website,
        "email": email,
        "description": description_details,
        "delegatorCount": delegator_count,
    }
    return summary


def _cosmos_validator_delegations(
    valoper: str,
    rpc_base_url: str,
    cursor: Optional[str],
) -> Dict[str, Any]:
    """Return a paginated delegator list for a Cosmos Hub validator."""
    validator = _cosmos_fetch_validator(valoper, rpc_base_url)
    if not validator:
        raise LCDRequestError("Validator not found", status=404)

    validator_tokens = max(_safe_int(validator.get("tokens")), 0)

    params: Dict[str, Any] = {
        "pagination.limit": LCD_PAGINATION_LIMIT,
    }
    if cursor:
        params["pagination.key"] = cursor

    delegations_payload = _lcd_get_json(
        f"/cosmos/staking/v1beta1/validators/{valoper}/delegations",
        params=params if params else None,
        base_url=rpc_base_url,
    ) or {}

    items: List[Dict[str, Any]] = []
    for delegation in delegations_payload.get("delegation_responses") or []:
        balance = (delegation.get("balance") or {})
        amount_uatom = _safe_int(balance.get("amount"))
        pct_of_validator = 0.0
        if validator_tokens > 0:
            pct_of_validator = _pct(amount_uatom, validator_tokens)

        items.append(
            {
                "delegatorAddress": (delegation.get("delegation") or {}).get("delegator_address"),
                "amount": _uatom_to_atom_string(amount_uatom),
                "denom": "ATOM",
                "pctOfValidator": pct_of_validator,
            }
        )

    return {
        "items": items,
        "nextCursor": (delegations_payload.get("pagination") or {}).get("next_key"),
    }


def _cosmos_validator_delegations_paginated(
    valoper: str,
    rpc_base_url: str,
    page: int = 1,
    limit: int = 20,
) -> Dict[str, Any]:
    """Return Cosmos delegations using page/limit response semantics."""
    validator = _cosmos_fetch_validator(valoper, rpc_base_url)
    if not validator:
        raise LCDRequestError("Validator not found", status=404)

    validator_tokens = max(_safe_int(validator.get("tokens")), 0)
    safe_page = max(page, 1)
    safe_limit = max(limit, 1)
    offset = (safe_page - 1) * safe_limit

    params: Dict[str, Any] = {
        "pagination.limit": safe_limit,
        "pagination.offset": offset,
        "pagination.count_total": "true",
    }

    delegations_payload = _lcd_get_json(
        f"/cosmos/staking/v1beta1/validators/{valoper}/delegations",
        params=params,
        base_url=rpc_base_url,
    ) or {}

    delegators: List[Dict[str, Any]] = []
    for delegation in delegations_payload.get("delegation_responses") or []:
        balance = (delegation.get("balance") or {})
        amount_uatom = _safe_int(balance.get("amount"))
        pct_of_validator = 0.0
        if validator_tokens > 0:
            pct_of_validator = _pct(amount_uatom, validator_tokens)

        delegators.append(
            {
                "delegatorAddress": (delegation.get("delegation") or {}).get("delegator_address"),
                "amount": _uatom_to_atom_string(amount_uatom),
                "denom": "ATOM",
                "pctOfValidator": pct_of_validator,
            }
        )

    total_count = _safe_int((delegations_payload.get("pagination") or {}).get("total"))
    if total_count == 0 and offset == 0:
        total_count = len(delegators)

    return {
        "delegators": delegators,
        "totalDelegators": total_count,
        "page": safe_page,
        "limit": safe_limit,
    }


def _nillion_fetch_validator(valoper: str, base_url: str) -> Optional[Dict[str, Any]]:
    """Fetch a single Nillion validator definition."""
    payload = _lcd_get_json(
        f"/cosmos/staking/v1beta1/validators/{valoper}",
        allow_404=True,
        base_url=base_url,
    )
    if payload and isinstance(payload.get("validator"), dict):
        return payload["validator"]
    return None


def _nillion_validator_summary(valoper: str, rpc_base_url: str) -> Dict[str, Any]:
    """Build a normalized summary for Nillion validators."""
    validator = _nillion_fetch_validator(valoper, rpc_base_url)
    if not validator:
        raise LCDRequestError("Validator not found", status=404)

    tokens_micro = max(_safe_int(validator.get("tokens")), 0)
    total_bonded = _get_total_bonded_tokens(base_url=rpc_base_url)
    voting_power_pct = _pct(tokens_micro, total_bonded)

    commission_str = (
        ((validator.get("commission") or {}).get("commission_rates") or {}).get("rate")
    )
    try:
        commission_pct = float(commission_str) * 100 if commission_str is not None else 0.0
    except (TypeError, ValueError):
        commission_pct = 0.0

    outstanding_rewards_micro = 0
    try:
        outstanding_payload = _lcd_get_json(
            f"/cosmos/distribution/v1beta1/validators/{valoper}/outstanding_rewards",
            allow_404=True,
            base_url=rpc_base_url,
        ) or {}
        # _logger.info(
        #     "Nillion outstanding rewards payload raw: %s",
        #     json.dumps(outstanding_payload, default=str))
        outstanding_rewards_micro = _sum_coin_amounts(outstanding_payload.get("rewards", {}).get("rewards", []))
        # _logger.info(
        #     "Nillion outstanding rewards payload: %s",
        #     json.dumps(outstanding_rewards_micro, default=str))
    except LCDRequestError:
        outstanding_rewards_micro = 0

    commission_rewards_micro = 0
    try:
        commission_payload = _lcd_get_json(
            f"/cosmos/distribution/v1beta1/validators/{valoper}/commission",
            allow_404=True,
            base_url=rpc_base_url,
        ) or {}
        commission_rewards_micro = _sum_coin_amounts(
            ((commission_payload.get("commission") or {}).get("commission")) or []
        )
        # _logger.info(
        #     "Nillion commission rewards payload: %s",
        #     json.dumps(commission_rewards_micro, default=str))
    except LCDRequestError:
        commission_rewards_micro = 0

    delegation_rewards_micro = max(outstanding_rewards_micro - commission_rewards_micro, 0)
    total_rewards_micro = commission_rewards_micro + delegation_rewards_micro

    status_meta = _validator_status_map(validator.get("status"))
    jailed = bool(validator.get("jailed"))
    identity = (validator.get("description") or {}).get("identity")

    uptime_pct = 0.0
    connection_status = "unknown"
    window_size = None
    try:
        slashing_payload = _lcd_get_json(
            "/cosmos/slashing/v1beta1/params",
            base_url=rpc_base_url,
        )
        window_size = _safe_int((slashing_payload.get("params") or {}).get("signed_blocks_window"))
    except LCDRequestError:
        window_size = None

    pubkey_info = validator.get("consensus_pubkey")
    pubkey = None
    if isinstance(pubkey_info, dict):
        pubkey = pubkey_info.get("key")
    elif isinstance(pubkey_info, str):
        pubkey = pubkey_info

    valcons_addr = None
    if pubkey:
        try:
            validator_set_payload = _lcd_get_json(
                "/cosmos/base/tendermint/v1beta1/validatorsets/latest",
                base_url=rpc_base_url,
            ) or {}
            for item in validator_set_payload.get("validators") or []:
                pub = item.get("pub_key") or {}
                if pub.get("key") == pubkey:
                    valcons_addr = item.get("address")
                    break
        except LCDRequestError:
            pass

    missed_counter = None
    if valcons_addr:
        page_key = None
        connection_status = "disconnected"
        for _ in range(20):
            params = {}
            if page_key:
                params["pagination.key"] = page_key
            signing_payload = _lcd_get_json(
                "/cosmos/slashing/v1beta1/signing_infos",
                params=params if params else None,
                base_url=rpc_base_url,
            )
            if not signing_payload:
                break
            for info in signing_payload.get("info") or []:
                if info.get("address") == valcons_addr:
                    missed_counter = _safe_int(info.get("missed_blocks_counter"))
                    connection_status = "connected"
                    break
            if connection_status == "connected":
                break
            page_key = (signing_payload.get("pagination") or {}).get("next_key")
            if not page_key:
                break

    if (
        window_size
        and missed_counter is not None
        and window_size > 0
        and missed_counter <= window_size
    ):
        uptime_pct = _pct(max(window_size - missed_counter, 0), window_size)

    summary = {
        "tokens": _micro_to_nillion_string(tokens_micro),
        "votingPowerPct": voting_power_pct,
        "commissionPct": commission_pct,
        "outstandingRewards": _micro_to_nillion_number(outstanding_rewards_micro),
        "ownedRewards": _micro_to_nillion_string(commission_rewards_micro),
        "delegationRewards": _micro_to_nillion_string(delegation_rewards_micro),
        "totalRewards": _micro_to_nillion_string(total_rewards_micro),
        "uptimePct": uptime_pct,
        "status": status_meta["status"],
        "statusLabel": status_meta.get("label"),
        "jailed": jailed,
        "connectionStatus": connection_status,
        "identity": identity,
    }
    return summary


def _nillion_validator_delegations(
    valoper: str,
    rpc_base_url: str,
    cursor: Optional[str],
) -> Dict[str, Any]:
    """Return delegations for a Nillion validator."""
    validator = _nillion_fetch_validator(valoper, rpc_base_url)
    if not validator:
        raise LCDRequestError("Validator not found", status=404)

    validator_tokens = max(_safe_int(validator.get("tokens")), 0)

    params: Dict[str, Any] = {
        "pagination.limit": LCD_PAGINATION_LIMIT,
    }
    if cursor:
        params["pagination.key"] = cursor

    delegations_payload = _lcd_get_json(
        f"/cosmos/staking/v1beta1/validators/{valoper}/delegations",
        params=params if params else None,
        base_url=rpc_base_url,
    ) or {}

    items: List[Dict[str, Any]] = []
    for delegation in delegations_payload.get("delegation_responses") or []:
        balance = (delegation.get("balance") or {})
        amount_micro = _safe_int(balance.get("amount"))
        pct_of_validator = 0.0
        if validator_tokens > 0:
            pct_of_validator = _pct(amount_micro, validator_tokens)

        items.append(
            {
                "delegatorAddress": (delegation.get("delegation") or {}).get("delegator_address"),
                "amount": _micro_to_nillion_string(amount_micro),
                "denom": "NIL",
                "pctOfValidator": pct_of_validator,
            }
        )

    return {
        "items": items,
        "nextCursor": (delegations_payload.get("pagination") or {}).get("next_key"),
    }


def _dchain_fetch_validator(valoper: str, base_url: str) -> Optional[Dict[str, Any]]:
    """Fetch a single Dchain validator definition."""
    payload = _lcd_get_json(
        f"/cosmos/staking/v1beta1/validators/{valoper}",
        allow_404=True,
        base_url=base_url,
    )
    if payload and isinstance(payload.get("validator"), dict):
        return payload["validator"]
    return None


def _dchain_validator_summary(valoper: str, rpc_base_url: str) -> Dict[str, Any]:
    """Build a normalized summary for Dchain validators."""
    validator = _dchain_fetch_validator(valoper, rpc_base_url)
    if not validator:
        raise LCDRequestError("Validator not found", status=404)

    tokens_micro = max(_safe_int(validator.get("tokens")), 0)
    total_bonded = _get_total_bonded_tokens(base_url=rpc_base_url)
    voting_power_pct = _pct(tokens_micro, total_bonded)

    commission_str = (
        ((validator.get("commission") or {}).get("commission_rates") or {}).get("rate")
    )
    try:
        commission_pct = float(commission_str) * 100 if commission_str is not None else 0.0
    except (TypeError, ValueError):
        commission_pct = 0.0

    outstanding_rewards_micro = 0
    try:
        outstanding_payload = _lcd_get_json(
            f"/cosmos/distribution/v1beta1/validators/{valoper}/outstanding_rewards",
            allow_404=True,
            base_url=rpc_base_url,
        ) or {}
        rewards_field = outstanding_payload.get("rewards")
        if isinstance(rewards_field, dict):
            rewards_field = rewards_field.get("rewards")
        outstanding_rewards_micro = _sum_coin_amounts(rewards_field)
    except LCDRequestError:
        outstanding_rewards_micro = 0

    commission_rewards_micro = 0
    try:
        commission_payload = _lcd_get_json(
            f"/cosmos/distribution/v1beta1/validators/{valoper}/commission",
            allow_404=True,
            base_url=rpc_base_url,
        ) or {}
        commission_rewards_micro = _sum_coin_amounts(
            ((commission_payload.get("commission") or {}).get("commission")) or []
        )
    except LCDRequestError:
        commission_rewards_micro = 0

    delegation_rewards_micro = max(outstanding_rewards_micro - commission_rewards_micro, 0)
    total_rewards_micro = commission_rewards_micro + delegation_rewards_micro

    status_meta = _validator_status_map(validator.get("status"))
    jailed = bool(validator.get("jailed"))
    identity = (validator.get("description") or {}).get("identity")

    uptime_pct = 0.0
    connection_status = "unknown"
    window_size = None
    try:
        slashing_payload = _lcd_get_json(
            "/cosmos/slashing/v1beta1/params",
            base_url=rpc_base_url,
        )
        window_size = _safe_int((slashing_payload.get("params") or {}).get("signed_blocks_window"))
    except LCDRequestError:
        window_size = None

    pubkey_info = validator.get("consensus_pubkey")
    pubkey = None
    if isinstance(pubkey_info, dict):
        pubkey = pubkey_info.get("key")
    elif isinstance(pubkey_info, str):
        pubkey = pubkey_info

    valcons_addr = None
    if pubkey:
        try:
            validator_set_payload = _lcd_get_json(
                "/cosmos/base/tendermint/v1beta1/validatorsets/latest",
                base_url=rpc_base_url,
            ) or {}
            for item in validator_set_payload.get("validators") or []:
                pub = item.get("pub_key") or {}
                if pub.get("key") == pubkey:
                    valcons_addr = item.get("address")
                    break
        except LCDRequestError:
            pass

    missed_counter = None
    if valcons_addr:
        page_key = None
        connection_status = "disconnected"
        for _ in range(20):
            params = {}
            if page_key:
                params["pagination.key"] = page_key
            signing_payload = _lcd_get_json(
                "/cosmos/slashing/v1beta1/signing_infos",
                params=params if params else None,
                base_url=rpc_base_url,
            )
            if not signing_payload:
                break
            for info in signing_payload.get("info") or []:
                if info.get("address") == valcons_addr:
                    missed_counter = _safe_int(info.get("missed_blocks_counter"))
                    connection_status = "connected"
                    break
            if connection_status == "connected":
                break
            page_key = (signing_payload.get("pagination") or {}).get("next_key")
            if not page_key:
                break

    if (
        window_size
        and missed_counter is not None
        and window_size > 0
        and missed_counter <= window_size
    ):
        uptime_pct = _pct(max(window_size - missed_counter, 0), window_size)

    summary = {
        "tokens": _micro_to_core_string(tokens_micro),
        "votingPowerPct": voting_power_pct,
        "commissionPct": commission_pct,
        "outstandingRewards": _micro_to_core_number(outstanding_rewards_micro),
        "ownedRewards": _micro_to_core_string(commission_rewards_micro),
        "delegationRewards": _micro_to_core_string(delegation_rewards_micro),
        "totalRewards": _micro_to_core_string(total_rewards_micro),
        "uptimePct": uptime_pct,
        "status": status_meta["status"],
        "statusLabel": status_meta.get("label"),
        "jailed": jailed,
        "connectionStatus": connection_status,
        "identity": identity,
    }
    return summary


def _dchain_validator_delegations(
    valoper: str,
    rpc_base_url: str,
    cursor: Optional[str],
) -> Dict[str, Any]:
    """Return delegations for a Dchain validator."""
    validator = _dchain_fetch_validator(valoper, rpc_base_url)
    if not validator:
        raise LCDRequestError("Validator not found", status=404)

    validator_tokens = max(_safe_int(validator.get("tokens")), 0)

    params: Dict[str, Any] = {
        "pagination.limit": LCD_PAGINATION_LIMIT,
    }
    if cursor:
        params["pagination.key"] = cursor

    delegations_payload = _lcd_get_json(
        f"/cosmos/staking/v1beta1/validators/{valoper}/delegations",
        params=params if params else None,
        base_url=rpc_base_url,
    ) or {}

    items: List[Dict[str, Any]] = []
    for delegation in delegations_payload.get("delegation_responses") or []:
        balance = (delegation.get("balance") or {})
        amount_micro = _safe_int(balance.get("amount"))
        pct_of_validator = 0.0
        if validator_tokens > 0:
            pct_of_validator = _pct(amount_micro, validator_tokens)

        items.append(
            {
                "delegatorAddress": (delegation.get("delegation") or {}).get("delegator_address"),
                "amount": _micro_to_core_string(amount_micro),
                "denom": balance.get("denom") or "DCHAIN",
                "pctOfValidator": pct_of_validator,
            }
        )

    return {
        "items": items,
        "nextCursor": (delegations_payload.get("pagination") or {}).get("next_key"),
    }


# ---------------------------------------------------------------------------
# Solana helpers
# ---------------------------------------------------------------------------


def _call_solana_rpc(
    base_url: str,
    method: str,
    params: Optional[Any] = None,
    timeout: int = LCD_TIMEOUT,
) -> Any:
    """Call a Solana JSON-RPC endpoint and return the result payload."""
    rpc_url = (base_url or "").strip()
    if not rpc_url:
        raise LCDRequestError("Solana RPC endpoint is not configured")

    payload = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": method,
        "params": params if params is not None else [],
    }
    try:
        response = requests.post(rpc_url, json=payload, timeout=timeout)
        response.raise_for_status()
    except requests.RequestException as exc:
        raise LCDRequestError(f"Solana RPC request failed: {exc}") from exc

    try:
        data = response.json()
    except ValueError as exc:
        raise LCDRequestError("Invalid JSON received from Solana RPC") from exc

    if isinstance(data, dict) and "error" in data:
        err = data["error"]
        msg = err.get("message", str(err)) if isinstance(err, dict) else str(err)
        raise LCDRequestError(f"Solana RPC error: {msg}")

    if not isinstance(data, dict) or "result" not in data:
        raise LCDRequestError("Solana RPC response missing result")

    return data["result"]


def _lamports_to_sol_string(value: int) -> str:
    """Convert lamport amounts to human-readable SOL strings."""
    whole = value // SOLANA_LAMPORTS
    remainder = value % SOLANA_LAMPORTS
    if remainder == 0:
        return str(whole)
    frac = str(remainder).rjust(9, "0").rstrip("0")
    return f"{whole}.{frac}"


def _lamports_to_sol_number(value: int) -> float:
    """Convert lamport amounts to float SOL."""
    if value == 0:
        return 0.0
    return round(value / SOLANA_LAMPORTS, 9)


def _solana_find_validator(vote_pubkey: str, rpc_base_url: str) -> Tuple[Dict[str, Any], str, list, list]:
    """Find a Solana validator by vote pubkey and return (validator, status, current, delinquent)."""
    vote_accounts = _call_solana_rpc(rpc_base_url, "getVoteAccounts")
    current = vote_accounts.get("current", [])
    delinquent = vote_accounts.get("delinquent", [])

    for v in current:
        if v.get("votePubkey") == vote_pubkey:
            return v, "active", current, delinquent

    for v in delinquent:
        if v.get("votePubkey") == vote_pubkey:
            return v, "delinquent", current, delinquent

    raise LCDRequestError("Validator not found", status=404)


def _solana_fetch_stake_accounts(vote_pubkey: str, rpc_base_url: str) -> list:
    """Fetch all stake accounts delegated to a vote account."""
    params = [
        SOLANA_STAKE_PROGRAM,
        {
            "encoding": "jsonParsed",
            "filters": [
                {"memcmp": {"offset": 124, "bytes": vote_pubkey}}
            ],
        },
    ]
    return _call_solana_rpc(rpc_base_url, "getProgramAccounts", params)


def _solana_fetch_validator_metadata(node_pubkey: str, rpc_base_url: str) -> Dict[str, Any]:
    """Fetch validator name/description/website from the on-chain Config program."""
    try:
        accounts = _call_solana_rpc(
            rpc_base_url,
            "getProgramAccounts",
            [SOLANA_CONFIG_PROGRAM, {"encoding": "jsonParsed"}],
            timeout=60,
        )
    except LCDRequestError:
        return {}

    for acc in accounts if isinstance(accounts, list) else []:
        try:
            info = acc.get("account", {}).get("data", {}).get("parsed", {}).get("info", {})
            keys = info.get("keys", [])
            identity = None
            for k in keys:
                if k.get("signer"):
                    identity = k.get("pubkey")
                    break
            if identity == node_pubkey:
                config_data = info.get("configData", {})
                return {
                    "name": config_data.get("name"),
                    "description": config_data.get("details"),
                    "website": config_data.get("website"),
                    "iconUrl": config_data.get("iconUrl"),
                }
        except Exception:
            continue
    return {}


def _solana_validator_summary(vote_pubkey: str, rpc_base_url: str) -> Dict[str, Any]:
    """Build a normalized summary for a Solana validator."""
    validator, status, current, delinquent = _solana_find_validator(vote_pubkey, rpc_base_url)

    activated_stake = _safe_int(validator.get("activatedStake"))
    commission = _safe_int(validator.get("commission"))
    node_pubkey = validator.get("nodePubkey")

    # Fetch stake accounts to get delegator count
    stake_accounts = _solana_fetch_stake_accounts(vote_pubkey, rpc_base_url)
    delegator_count = 0
    for acc in stake_accounts:
        info = (acc.get("account", {}).get("data", {}).get("parsed", {}).get("info", {}))
        if info.get("stake", {}).get("delegation"):
            delegator_count += 1

    # Identify self-stake accounts (staker/withdrawer matches nodePubkey)
    self_stake_pubkeys: List[str] = []

    for acc in stake_accounts:
        parsed = acc.get("account", {}).get("data", {}).get("parsed", {})
        info = parsed.get("info", {})
        delegation = info.get("stake", {}).get("delegation")
        if not delegation:
            continue
        authorized = info.get("meta", {}).get("authorized", {})
        staker = authorized.get("staker")
        withdrawer = authorized.get("withdrawer")
        if staker == node_pubkey or withdrawer == node_pubkey:
            self_stake_pubkeys.append(acc["pubkey"])

    # Fetch commission earned directly from the vote account (single fast RPC call)
    commission_reward_lamports = 0
    reward_epoch = None
    try:
        vote_rewards = _call_solana_rpc(rpc_base_url, "getInflationReward", [[vote_pubkey]])
        _logger.info("Vote account rewards: %s", json.dumps(vote_rewards, default=str))
        if isinstance(vote_rewards, list) and vote_rewards and vote_rewards[0]:
            commission_reward_lamports = _safe_int(vote_rewards[0].get("amount"))
            reward_epoch = vote_rewards[0].get("epoch")
    except LCDRequestError:
        pass

    # Fetch self-stake rewards (usually 1-2 accounts, very fast)
    self_stake_reward = 0
    for i in range(0, len(self_stake_pubkeys), SOLANA_INFLATION_REWARD_BATCH_SIZE):
        batch = self_stake_pubkeys[i:i + SOLANA_INFLATION_REWARD_BATCH_SIZE]
        try:
            rewards = _call_solana_rpc(rpc_base_url, "getInflationReward", [batch])
        except LCDRequestError:
            rewards = [None] * len(batch)
        for r in (rewards or []):
            if not r:
                continue
            if reward_epoch is None:
                reward_epoch = r.get("epoch")
            self_stake_reward += _safe_int(r.get("amount"))

    total_reward = self_stake_reward + commission_reward_lamports


    summary: Dict[str, Any] = {
        "tokens": _lamports_to_sol_string(activated_stake),
        "commissionPct": commission,
        "totalRewards": _lamports_to_sol_string(int(total_reward)),
        "ownedRewards": _lamports_to_sol_string(int(self_stake_reward)),
        "outstandingRewards": _lamports_to_sol_string(int(commission_reward_lamports)),
        "Epoch": reward_epoch,
        "status": status,
        "statusLabel": status.capitalize(),
        "votePubkey": validator.get("votePubkey"),
        "nodePubkey": node_pubkey,
        "delegatorCount": delegator_count,
    }

    # Fetch on-chain validator metadata (name, description, website, iconUrl)
    metadata = _solana_fetch_validator_metadata(node_pubkey or "", rpc_base_url)
    if metadata:
        summary.update(metadata)

    return summary


def _solana_reward_snapshot(vote_pubkey: str, rpc_base_url: str) -> Dict[str, Any]:
    """Collect a reward snapshot for a Solana validator.

    Returns a dict compatible with the reward snapshot worker:
        outstanding_rewards, total_rewards, tokens, delegator_count, epoch.
    """
    try:
        validator, _status, _current, _delinquent = _solana_find_validator(vote_pubkey, rpc_base_url)
    except LCDRequestError as exc:
        return {"error": "rpc_error", "note": str(exc)}

    activated_stake = _safe_int(validator.get("activatedStake"))
    commission_pct = _safe_int(validator.get("commission"))
    node_pubkey = validator.get("nodePubkey")

    # Fetch stake accounts for delegator count and self-stake identification.
    stake_accounts = _solana_fetch_stake_accounts(vote_pubkey, rpc_base_url)
    delegator_count = 0
    self_stake_pubkeys: List[str] = []
    self_stake_lamports = 0
    for acc in stake_accounts:
        parsed = acc.get("account", {}).get("data", {}).get("parsed", {})
        info = parsed.get("info", {})
        delegation = info.get("stake", {}).get("delegation")
        if delegation:
            delegator_count += 1
        if not delegation:
            continue
        authorized = info.get("meta", {}).get("authorized", {})
        staker = authorized.get("staker")
        withdrawer = authorized.get("withdrawer")
        if staker == node_pubkey or withdrawer == node_pubkey:
            self_stake_pubkeys.append(acc["pubkey"])
            self_stake_lamports += _safe_int(delegation.get("stake"))

    # Commission reward comes from vote account inflation reward.
    commission_reward_lamports = 0
    reward_epoch = None
    try:
        vote_rewards = _call_solana_rpc(rpc_base_url, "getInflationReward", [[vote_pubkey]])
        if isinstance(vote_rewards, list) and vote_rewards and vote_rewards[0]:
            commission_reward_lamports = _safe_int(vote_rewards[0].get("amount"))
            reward_epoch = vote_rewards[0].get("epoch")
    except LCDRequestError:
        pass

    # Self-stake rewards are fetched from stake accounts controlled by node identity.
    self_stake_reward_lamports = 0
    for i in range(0, len(self_stake_pubkeys), SOLANA_INFLATION_REWARD_BATCH_SIZE):
        batch = self_stake_pubkeys[i : i + SOLANA_INFLATION_REWARD_BATCH_SIZE]
        try:
            rewards = _call_solana_rpc(rpc_base_url, "getInflationReward", [batch])
        except LCDRequestError:
            rewards = [None] * len(batch)
        for r in rewards or []:
            if not r:
                continue
            if reward_epoch is None:
                reward_epoch = r.get("epoch")
            amount = _safe_int(r.get("amount"))
            self_stake_reward_lamports += amount

    total_reward_lamports = self_stake_reward_lamports + commission_reward_lamports

    return {
        "outstanding_rewards": _lamports_to_sol_number(int(total_reward_lamports)),
        "total_rewards": _lamports_to_sol_number(int(total_reward_lamports)),
        "tokens": _lamports_to_sol_number(activated_stake),
        "owned_stake": _lamports_to_sol_number(self_stake_lamports),
        "delegator_count": delegator_count,
        "epoch": reward_epoch,
        "commission_pct": commission_pct,
    }


def _solana_validator_delegations(
    vote_pubkey: str,
    rpc_base_url: str,
    page: int = 1,
    limit: int = 20,
) -> Dict[str, Any]:
    """Return paginated delegations for a Solana validator."""
    stake_accounts = _solana_fetch_stake_accounts(vote_pubkey, rpc_base_url)

    all_delegations: List[Dict[str, Any]] = []
    for acc in stake_accounts:
        info = acc.get("account", {}).get("data", {}).get("parsed", {}).get("info", {})
        delegation = info.get("stake", {}).get("delegation")
        if not delegation:
            continue
        stake_lamports = _safe_int(delegation.get("stake"))
        activation_epoch = delegation.get("activationEpoch")
        all_delegations.append({
            "delegatorAddress": acc["pubkey"],
            "amount": _lamports_to_sol_string(stake_lamports),
            "denom": "SOL",
            "activationEpoch": activation_epoch,
        })

    total_count = len(all_delegations)

    start = (max(page, 1) - 1) * limit
    end = start + limit
    paginated = all_delegations[start:end]

    return {
        "delegators": paginated,
        "totalDelegators": total_count,
        "page": page,
        "limit": limit,
    }


def _validator_delegations_page(
    valoper: str,
    protocol_key: str,
    rpc_base_url: Optional[str],
    page: int = 1,
    limit: int = 20,
) -> Dict[str, Any]:
    """Return page/limit-based delegations for simplified validator delegation APIs."""
    if not rpc_base_url:
        raise LCDRequestError("Protocol RPC endpoint is not configured")

    if protocol_key == "solana":
        return _solana_validator_delegations(valoper, rpc_base_url, page=page, limit=limit)

    if protocol_key == "cosmos":
        return _cosmos_validator_delegations_paginated(
            valoper,
            rpc_base_url,
            page=page,
            limit=limit,
        )

    raise LCDRequestError(
        "This endpoint is only available for Solana and Cosmos validators",
        status=400,
    )


SKALE_VALIDATOR_SERVICE_ADDRESS = "0x840C8122433A5AA7ad60C1Bcdc36AB9DcCF761a5"
SKALE_DELEGATION_CONTROLLER_ADDRESS = "0x06dD71dAb27C1A3e0B172d53735f00Bf1a66Eb79"
SKALE_DELEGATION_PAGE_LIMIT = 50


# Precomputed function selectors (first 4 bytes of keccak256 signatures)
SKALE_FN = {
    "getValidatorId": "174e6832",
    "getValidator": "b5d89627",
    "getAndUpdateDelegatedToValidatorNow": "1d703812",
    "getDelegationsByValidatorLength": "3d42b1ce",
    "delegationsByValidator": "1d9c7f0a",
    "getDelegation": "0dd35701",
}


def _skale_pad_uint(value: int) -> bytes:
    try:
        return int(value).to_bytes(32, byteorder="big", signed=False)
    except (TypeError, ValueError):
        return (0).to_bytes(32, byteorder="big")


def _skale_pad_address(addr: str) -> bytes:
    clean = (addr or "").lower().strip()
    if clean.startswith("0x"):
        clean = clean[2:]
    clean = clean.rjust(40, "0")[-40:]
    try:
        b = bytes.fromhex(clean)
    except ValueError:
        b = b"\x00" * 20
    return b"\x00" * 12 + b


def _skale_build_call(selector_hex: str, args: List[bytes]) -> str:
    payload = bytes.fromhex(selector_hex)
    if args:
        payload += b"".join(args)
    return "0x" + payload.hex()


def _skale_eth_call(rpc_url: str, to_addr: str, data: str) -> bytes:
    """Execute eth_call and return raw bytes result."""
    payload = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "eth_call",
        "params": [
            {
                "to": to_addr,
                "data": data,
            },
            "latest",
        ],
    }
    try:
        resp = requests.post(rpc_url, json=payload, timeout=LCD_TIMEOUT)
        resp.raise_for_status()
    except requests.RequestException as exc:
        raise LCDRequestError(f"Failed to reach Skale RPC: {exc}") from exc
    try:
        body = resp.json()
    except ValueError as exc:
        raise LCDRequestError("Skale RPC returned non-JSON response") from exc
    if "error" in body:
        raise LCDRequestError(f"Skale RPC error: {body['error']}")
    result = body.get("result")
    if not isinstance(result, str):
        raise LCDRequestError("Skale RPC response missing result")
    result_hex = result[2:] if result.startswith("0x") else result
    try:
        return bytes.fromhex(result_hex)
    except ValueError as exc:
        raise LCDRequestError("Skale RPC result is not valid hex") from exc


def _skale_load_web3(rpc_url: str):
    """Return a Web3 instance connected to the provided RPC URL."""
    try:
        from web3 import Web3  # type: ignore
    except ImportError as exc:  # pragma: no cover - optional dep
        raise LCDRequestError("Missing skale dependency: install web3") from exc
    w3 = Web3(Web3.HTTPProvider(rpc_url))
    if not w3.is_connected():
        raise LCDRequestError("Skale RPC endpoint is unreachable")
    return w3


def _skale_load_abi() -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
    """Load Skale ValidatorService and DelegationController ABIs from skaleAbi.json."""
    base_dir = Path(__file__).resolve().parent.parent / "abi"
    abi_path = base_dir / "skaleAbi.json"
    try:
        with abi_path.open("r", encoding="utf-8") as fp:
            data = json.load(fp)
        return data["validator_service_abi"], data["delegation_controller_abi"]
    except FileNotFoundError as exc:
        raise LCDRequestError("Skale ABI file not found") from exc
    except (KeyError, json.JSONDecodeError) as exc:
        raise LCDRequestError("Skale ABI file is invalid or missing keys") from exc


def _skale_get_validator_id(rpc_url: str, validator_address: str) -> int:
    """Fetch validator ID for a given validator owner address."""
    w3 = _skale_load_web3(rpc_url)
    validator_abi, _ = _skale_load_abi()
    contract = w3.eth.contract(
        address=w3.to_checksum_address(SKALE_VALIDATOR_SERVICE_ADDRESS),
        abi=validator_abi,
    )
    try:
        val_id = contract.functions.getValidatorId(w3.to_checksum_address(validator_address)).call()
    except Exception as exc:
        raise LCDRequestError(f"Failed to fetch Skale validator ID: {exc}") from exc
    return int(val_id)


def _skale_get_validator(rpc_url: str, validator_id: int) -> Dict[str, Any]:
    """Fetch validator tuple for a given validator ID."""
    w3 = _skale_load_web3(rpc_url)
    validator_abi, _ = _skale_load_abi()
    contract = w3.eth.contract(
        address=w3.to_checksum_address(SKALE_VALIDATOR_SERVICE_ADDRESS),
        abi=validator_abi,
    )
    try:
        decoded = contract.functions.getValidator(int(validator_id)).call()
        # _logger.info("Skale getValidator raw response: %s", decoded)
    except Exception as exc:
        raise LCDRequestError(f"Failed to fetch Skale validator: {exc}") from exc

    return {
        "name": decoded[0],
        "validatorAddress": decoded[1],
        "requestedAddress": decoded[2],
        "description": decoded[3],
        "feeRate": int(decoded[4]),
        "registrationTime": int(decoded[5]),
        "minimumDelegationAmount": int(decoded[6]),
        "acceptNewRequests": bool(decoded[7]),
    }


def _skale_get_delegated_total(rpc_url: str, validator_id: int) -> int:
    """Return total delegated amount (wei) to validator."""
    w3 = _skale_load_web3(rpc_url)
    _, delegation_abi = _skale_load_abi()
    contract = w3.eth.contract(
        address=w3.to_checksum_address(SKALE_DELEGATION_CONTROLLER_ADDRESS),
        abi=delegation_abi,
    )
    try:
        amount = contract.functions.getAndUpdateDelegatedToValidatorNow(int(validator_id)).call()
    except Exception as exc:
        raise LCDRequestError(f"Failed to fetch Skale delegated total: {exc}") from exc
    return int(amount)


def _skale_get_delegations(
    rpc_url: str,
    validator_id: int,
    start: int,
    limit: int,
) -> Tuple[List[Dict[str, Any]], Optional[str], int]:
    """Return paginated delegations for a validator."""
    w3 = _skale_load_web3(rpc_url)
    _, delegation_abi = _skale_load_abi()
    contract = w3.eth.contract(
        address=w3.to_checksum_address(SKALE_DELEGATION_CONTROLLER_ADDRESS),
        abi=delegation_abi,
    )
    try:
        length = int(contract.functions.getDelegationsByValidatorLength(int(validator_id)).call())
    except Exception as exc:
        raise LCDRequestError(f"Failed to fetch Skale delegations length: {exc}") from exc

    if start < 0:
        start = 0
    end = min(start + limit, length)
    items: List[Dict[str, Any]] = []
    for idx in range(start, end):
        try:
            delegation_id = int(
                contract.functions.delegationsByValidator(int(validator_id), idx).call()
            )
            delegation = contract.functions.getDelegation(delegation_id).call()
        except Exception as exc:
            raise LCDRequestError(f"Failed to fetch Skale delegation: {exc}") from exc

        items.append(
            {
                "delegationId": delegation_id,
                "holder": delegation[0],
                "validatorId": int(delegation[1]),
                "amount": int(delegation[2]),
                "delegationPeriod": int(delegation[3]),
                "created": int(delegation[4]),
                "info": delegation[7],
            }
        )

    next_cursor = str(end) if end < length else None
    return items, next_cursor, length


def _skale_get_owned_stake(rpc_url: str, validator_id: int, validator: Dict[str, Any]) -> int:
    """Best-effort self-stake lookup for Skale based on delegation holder address."""
    candidate_addresses = {
        str(address).strip().lower()
        for address in (
            validator.get("validatorAddress"),
            validator.get("requestedAddress"),
        )
        if address
    }
    if not candidate_addresses:
        return 0

    owned_stake = 0
    start = 0
    total_len = 1
    while start < total_len:
        items, _next_cursor, total_len = _skale_get_delegations(
            rpc_url,
            validator_id,
            start,
            SKALE_DELEGATION_PAGE_LIMIT,
        )
        for item in items:
            holder = str(item.get("holder") or "").strip().lower()
            if holder in candidate_addresses:
                owned_stake += _safe_int(item.get("amount"))
        start += SKALE_DELEGATION_PAGE_LIMIT

    return owned_stake


def _format_decimal_string(value: Any, precision: int = 6) -> str:
    """Return a normalized decimal string trimmed to ``precision`` places."""
    try:
        dec_value = Decimal(str(value))
    except (InvalidOperation, ValueError):
        return "0"
    try:
        quant = Decimal(10) ** -precision
        dec_value = dec_value.quantize(quant, rounding=ROUND_HALF_UP)
    except (InvalidOperation, ValueError):
        pass
    text = format(dec_value.normalize(), "f").rstrip("0").rstrip(".")
    return text or "0"


def _get_dune_api_key(env=None) -> Optional[str]:
    """Return the configured Dune API key.
    
    Args:
        env: Optional Odoo environment. If not provided, will try to get from request context.
    """
    try:
        # Use provided env or try to get from request context
        if env is None:
            try:
                if hasattr(request, 'env'):
                    env = request.env
            except (AttributeError, RuntimeError, NameError):
                pass
        
        # If still no env, try to get from registry (for cron jobs)
        if env is None:
            try:
                from odoo import registry, api
                import threading
                db_name = getattr(threading.current_thread(), 'dbname', None)
                if not db_name:
                    # Try to get from odoo.tools.config
                    from odoo.tools import config
                    db_name = config.get('db_name')
                
                if db_name:
                    _logger.info(f"Using database: {db_name} to fetch Dune API key")
                    reg = registry(db_name)
                    with reg.cursor() as cr:
                        env = api.Environment(cr, 1, {})  # SUPERUSER_ID = 1
                else:
                    _logger.warning("No database name found in thread or config")
                    return None
            except Exception as e:
                _logger.warning("Failed to get env from registry: %s", e)
                return None
        
        if env is not None:
            cfg = env["ir.config_parameter"].sudo()
            api_key = (cfg.get_param("DUNE_API_KEY") or "").strip()
            return api_key or None
        else:
            _logger.warning("No environment available to fetch Dune API key")
            return None
            
    except Exception:
        _logger.exception("Failed to read Dune API key from ir.config_parameter")
        return None


def _get_dune_api_base(env=None) -> Optional[str]:
    """Return the configured Dune API base URL.
    
    Args:
        env: Optional Odoo environment. If not provided, will try to get from request context.
    """
    try:
        # Use provided env or try to get from request context
        if env is None:
            try:
                if hasattr(request, 'env'):
                    env = request.env
            except (AttributeError, RuntimeError, NameError):
                pass
        
        # If still no env, try to get from registry (for cron jobs)
        if env is None:
            try:
                from odoo import registry, api
                import threading
                db_name = getattr(threading.current_thread(), 'dbname', None)
                if not db_name:
                    # Try to get from odoo.tools.config
                    from odoo.tools import config
                    db_name = config.get('db_name')
                
                if db_name:
                    _logger.info(f"Using database: {db_name} to fetch Dune API base URL")
                    reg = registry(db_name)
                    with reg.cursor() as cr:
                        env = api.Environment(cr, 1, {})  # SUPERUSER_ID = 1
                else:
                    _logger.warning("No database name found in thread or config")
                    return None
            except Exception as e:
                _logger.warning("Failed to get env from registry: %s", e)
                return None
        
        if env is not None:
            cfg = env["ir.config_parameter"].sudo()
            api_base = (cfg.get_param("DUNE_API_BASE") or "").strip()
            return api_base or None
        else:
            _logger.warning("No environment available to fetch Dune API base URL")
            return None
            
    except Exception:
        _logger.exception("Failed to read Dune API base URL from ir.config_parameter")
        return None


def _get_skale_dune_query_id() -> Optional[int]:
    """Resolve the Dune query ID used for Skale rewards lookups."""
    try:
        return int(str(DUNE_SKALE_QUERY_ID).strip())
    except (TypeError, ValueError):
        return None


def _dune_request(
    api_key: str,
    api_base: str,
    method: str,
    path: str,
    payload: Optional[Dict[str, Any]] = None,
) -> Optional[Dict[str, Any]]:
    """Perform a request to the Dune API with consistent error handling."""
    if not api_base:
        _logger.warning("Dune API base URL is not configured")
        return None
    url = f"{api_base}{path}"
    headers = {"X-DUNE-API-KEY": api_key}
    kwargs: Dict[str, Any] = {"headers": headers, "timeout": LCD_TIMEOUT}
    if payload is not None:
        kwargs["json"] = payload
    try:
        resp = requests.request(method, url, **kwargs)
        # _logger.info("Dune %s %s response HTTP %s", method, path, resp.status_code)
    except requests.RequestException as exc:
        _logger.warning("Dune %s %s failed: %s", method, path, exc)
        return None
    if resp.status_code >= 400:
        _logger.warning(
            "Dune %s %s HTTP %s: %s", method, path, resp.status_code, resp.text[:200]
        )
        return None
    try:
        return resp.json()
    except ValueError:
        _logger.warning("Dune %s %s returned invalid JSON: %s", method, path, resp.text[:200])
        return None


def _dune_execute_query(
    api_key: str,
    api_base: str,
    query_id: int,
    query_params: Optional[Dict[str, Any]],
    performance: str = DUNE_DEFAULT_PERFORMANCE,
) -> Optional[str]:
    """Execute a Dune query and return the execution ID."""
    payload: Dict[str, Any] = {
        "query_parameters": query_params or {},
        "performance": performance or DUNE_DEFAULT_PERFORMANCE,
    }
    data = _dune_request(api_key, api_base, "POST", f"/query/{query_id}/execute", payload)
    execution_id = (data or {}).get("execution_id")
    # _logger.info("Dune execute response: %s", json.dumps(data or {}, default=str))
    if not execution_id:
        _logger.warning("Dune execute did not return execution_id for query %s", query_id)
        return None
    return str(execution_id)


def _dune_poll_execution(
    api_key: str,
    api_base: str,
    execution_id: str,
    max_polls: int = DUNE_STATUS_MAX_POLLS,
    delay_seconds: int = DUNE_STATUS_POLL_DELAY,
) -> Optional[Dict[str, Any]]:
    """Poll Dune execution status until completion or timeout."""
    last_status: Optional[Dict[str, Any]] = None
    for _ in range(max_polls):
        status = _dune_request(api_key, api_base, "GET", f"/execution/{execution_id}/status", None) or {}
        # _logger.info("Dune execution status: %s", json.dumps(status, default=str))
        last_status = status
        state = str(status.get("state") or "").upper()
        if state == "QUERY_STATE_COMPLETED":
            return status
        if status.get("is_execution_finished") or state.startswith("QUERY_STATE_FAILED"):
            break
        time.sleep(delay_seconds)
    return last_status


def _dune_fetch_execution_results(api_key: str, api_base: str, execution_id: str) -> Optional[Dict[str, Any]]:
    """Fetch execution results from Dune."""
    return _dune_request(api_key, api_base, "GET", f"/execution/{execution_id}/results", None)


def _skale_parse_dune_rewards(rows: Any) -> Dict[str, Any]:
    """Normalize Dune rewards rows into a simple reward summary."""
    rewards_map: Dict[str, float] = {}
    for row in rows if isinstance(rows, list) else []:
        if not isinstance(row, dict):
            continue
        label = str(row.get("Rewards") or row.get("rewards") or "").strip().lower()
        if not label:
            continue
        rewards_map[label] = _safe_float(row.get("Value") or row.get("value"))

    claimed = rewards_map.get("rewards claimed", 0.0)
    unclaimed = rewards_map.get("rewards unclaimed", 0.0)
    total = rewards_map.get("rewards to date", claimed + unclaimed)
    total_usd = rewards_map.get("rewards to date in usd", 0.0)
    return {
        "claimed": claimed,
        "unclaimed": unclaimed,
        "total": total,
        "total_usd": total_usd,
    }


def _skale_fetch_rewards_from_dune(
    valoper: str,
    dune_api_key: Optional[str] = None,
    dune_api_base: Optional[str] = None,
) -> Dict[str, Any]:
    """Execute the configured Dune query to fetch Skale validator rewards.
    
    Args:
        valoper: Validator identifier
        dune_api_key: Pre-fetched Dune API key
        dune_api_base: Pre-fetched Dune API base URL
    """
    _logger.info(f"Fetching Skale rewards from Dune for validator {valoper}: key={'SET' if dune_api_key else 'NOT SET'}, base={'SET' if dune_api_base else 'NOT SET'}")
    
    if not dune_api_key:
        _logger.warning("Dune API key is not configured; skipping Skale rewards fetch")
        return {}
    
    if not dune_api_base:
        _logger.warning("Dune API base URL is not configured; skipping Skale rewards fetch")
        return {}

    query_id = _get_skale_dune_query_id()
    if not query_id:
        _logger.warning("Skale Dune query ID is not configured; skipping rewards fetch")
        return {}

    execution_id = _dune_execute_query(
        dune_api_key,
        dune_api_base,
        query_id,
        {"validator Id": valoper},
        performance=DUNE_DEFAULT_PERFORMANCE,
    )
    if not execution_id:
        return {}

    status = _dune_poll_execution(dune_api_key, dune_api_base, execution_id)
    state = str((status or {}).get("state") or "").upper()
    if state != "QUERY_STATE_COMPLETED":
        _logger.warning(
            "Dune execution for Skale rewards did not complete",
            extra={"valoper": valoper, "state": state, "execution_id": execution_id},
        )
        return {}

    results = _dune_fetch_execution_results(dune_api_key, dune_api_base, execution_id) or {}
    # _logger.info(
    #     "Dune execution results for Skale rewards: %s",
    #     json.dumps(results or {}, default=str),
    # )
    rows = ((results.get("result") or {}).get("rows")) or []
    return _skale_parse_dune_rewards(rows)


def _skale_fetch_rewards_from_model(valoper: str) -> Optional[Dict[str, Any]]:
    """Fetch the most recent SKALE rewards from the rewards model.
    
    Args:
        valoper: Validator identifier
    
    Returns:
        Dict with 'claimed', 'unclaimed', 'total', 'total_usd' or None if not found
    """
    try:
        # Get env from request context
        try:
            env = request.env
        except (AttributeError, RuntimeError, NameError):
            _logger.warning("No request context available to fetch SKALE rewards from model")
            return None
        
        # Fetch the most recent reward record for this validator
        reward_model = env['validator.rewards.snapshot'].sudo()
        reward = reward_model.search([
            ('valoper', '=', valoper),
            ('protocol_key', 'ilike', 'skale')
        ], order='snapshot_date desc', limit=1)
        
        if not reward:
            _logger.info(f"No cached rewards found for SKALE validator {valoper}")
            return None
        
        # Extract reward data - handle both claimed and total rewards
        oustanding_rewards = _safe_float(reward.outstanding_rewards or 0)
        total = _safe_float(reward.total_rewards or 0)
        
        _logger.info(f"Found stored SKALE rewards for validator {valoper}: , total={total}, unclaimed={oustanding_rewards}")
        
        return {
            'unclaimed': oustanding_rewards,
            'total': total,
        }
        
    except Exception as e:
        _logger.exception(f"Failed to fetch SKALE rewards from model for {valoper}: {e}")
        return None


def _skale_validator_summary(valoper: str, rpc_base_url: str) -> Dict[str, Any]:
    """Build a normalized summary for Skale validators."""
    validator_id = None
    try:
        validator_id = int(valoper, 0)
    except (TypeError, ValueError):
        pass

    if validator_id is None:
        validator_id = _skale_get_validator_id(rpc_base_url, valoper)
        if validator_id == 0:
            raise LCDRequestError("Validator not found", status=404)

    validator = _skale_get_validator(rpc_base_url, validator_id)
    # _logger.info("Skale validator data: %s", json.dumps(validator, default=str))
    total_delegated = _skale_get_delegated_total(rpc_base_url, validator_id)

    commission_pct = float(validator.get("feeRate", 0)) / 10
    tokens = _wei_to_skl_string(total_delegated)
    
    # Try to fetch rewards from model first (fast path)
    dune_rewards = _skale_fetch_rewards_from_model(valoper)
    
    # If no cached rewards, fall back to Dune API (slow path)
    if not dune_rewards:
        _logger.info(f"No cached rewards for SKALE validator {valoper}, falling back to Dune API")
        dune_api_key = None
        dune_api_base = None
        try:
            if hasattr(request, 'env'):
                dune_api_key = (request.env['ir.config_parameter'].sudo().get_param('DUNE_API_KEY') or '').strip() or None
                dune_api_base = (request.env['ir.config_parameter'].sudo().get_param('DUNE_API_BASE') or '').strip() or None
        except:
            pass
        dune_rewards = _skale_fetch_rewards_from_dune(valoper, dune_api_key, dune_api_base)
    
    outstanding_rewards = _safe_float((dune_rewards or {}).get("unclaimed"))
    total_rewards_str = _format_decimal_string((dune_rewards or {}).get("total"))
    
    # Fetch delegator count efficiently (only need total count, not actual delegations)
    delegator_count = None
    try:
        _, _, total_len = _skale_get_delegations(
            rpc_base_url,
            validator_id,
            start=0,
            limit=1,  # Minimal limit since we only need the count
        )
        delegator_count = total_len
    except Exception as e:
        _logger.warning(f"Failed to fetch SKALE delegator count for {valoper}: {e}")

    summary = {
        "tokens": tokens,
        # "votingPowerPct": 0.0,
        "commissionPct": commission_pct,
        "outstandingRewards": outstanding_rewards,
        "ownedRewards": total_rewards_str,
        # "delegationRewards": "0",
        "totalRewards": total_rewards_str,
        # "uptimePct": 0.0,
        "status": "unknown",
        "statusLabel": "unknown",
        "jailed": False,
        "connectionStatus": "unknown",
        "identity": None,
        "description": validator.get("description"),
        "moniker": validator.get("name"),
        "delegatorCount": delegator_count,
    }
    return summary


def _skale_validator_delegations(
    valoper: str,
    rpc_base_url: str,
    cursor: Optional[str],
) -> Dict[str, Any]:
    """Return a paginated delegator list for a Skale validator."""
    validator_id = None
    try:
        validator_id = int(valoper, 0)
    except (TypeError, ValueError):
        pass

    if validator_id is None:
        validator_id = _skale_get_validator_id(rpc_base_url, valoper)
        if validator_id == 0:
            raise LCDRequestError("Validator not found", status=404)

    total_delegated = _skale_get_delegated_total(rpc_base_url, validator_id)

    try:
        start = int(str(cursor)) if cursor is not None else 0
    except (TypeError, ValueError):
        start = 0
    if start < 0:
        start = 0

    delegation_items, next_cursor, total_len = _skale_get_delegations(
        rpc_base_url,
        validator_id,
        start,
        SKALE_DELEGATION_PAGE_LIMIT,
    )

    items: List[Dict[str, Any]] = []
    for entry in delegation_items:
        amount_wei = max(_safe_int(entry.get("amount")), 0)
        pct = _pct(amount_wei, total_delegated) if total_delegated > 0 else 0.0
        items.append(
            {
                "delegatorAddress": entry.get("holder"),
                "amount": _wei_to_skl_string(amount_wei),
                "denom": "SKL",
                "pctOfValidator": pct,
            }
        )

    return {
        "items": items,
        "nextCursor": next_cursor,
    }


def _extract_validator_address(subscription: Any) -> Tuple[Dict[str, Any], Optional[str]]:
    """Extract validator metadata and canonical address from a subscription."""
    raw_info = (getattr(subscription, "validator_info", "") or "").strip()
    validator_info: Dict[str, Any] = {}
    if raw_info:
        try:
            validator_info = json.loads(raw_info)
        except json.JSONDecodeError:
            validator_info = {}

    validator_address = (
        validator_info.get("validator_address")
        or validator_info.get("validatorAddress")
        or validator_info.get("valoper_address")
        or validator_info.get("valoper")
        or validator_info.get("node_id")
        or validator_info.get("nodeId")
        or validator_info.get("node_identifier")
        or validator_info.get("nodeIdentifier")
        or validator_info.get("validatorNodeId")
        or validator_info.get("peer_id")
        or validator_info.get("peerId")
        or validator_info.get("wallet")
        or validator_info.get("address")
    )

    if not validator_address:
        fallback = getattr(subscription, "validator_address", None)
        if fallback:
            validator_address = fallback

    return validator_info, validator_address


def _flow_extract_owner_address(validator_info: Dict[str, Any]) -> Optional[str]:
    """Resolve the Flow owner/delegation wallet address from stored info."""
    if not isinstance(validator_info, dict):
        return None
    candidates = [
        validator_info.get("ownerAddress"),
        validator_info.get("owner_address"),
        validator_info.get("delegationAddress"),
        validator_info.get("delegation_address"),
        validator_info.get("delegatorAddress"),
        validator_info.get("delegator_address"),
        validator_info.get("flowAddress"),
        validator_info.get("flow_address"),
    ]
    for candidate in candidates:
        normalized = _flow_normalize_owner_address(candidate if isinstance(candidate, str) else None)
        if normalized:
            return normalized
    wallet = _flow_normalize_owner_address(validator_info.get("wallet"))
    if wallet:
        return wallet
    address = _flow_normalize_owner_address(validator_info.get("address"))
    if address:
        return address
    return None


def _compute_validator_summary(
    valoper: str,
    protocol_key: str,
    rpc_base_url: Optional[str],
    flow_context: Optional[Dict[str, Any]] = None,
    delegation_address: Optional[str] = None,
) -> Dict[str, Any]:
    """Compute a normalized summary for validators across protocols."""
    if not valoper:
        raise LCDRequestError("Validator identifier is required")

    if protocol_key == "flow":
        if not rpc_base_url:
            raise LCDRequestError("Protocol RPC endpoint is not configured")
        network_hint = (flow_context or {}).get("network")
        owner_address = (flow_context or {}).get("owner_address")
        details = _flow_fetch_validator_details(
            rpc_base_url,
            valoper,
            network_hint,
            owner_address=owner_address,
        )
        return _flow_summary_from_details(details)

    if protocol_key == "avalanche":
        if not rpc_base_url:
            raise LCDRequestError("Protocol RPC endpoint is not configured")

        validator = _avalanche_fetch_validator(valoper, rpc_base_url)
        if not validator:
            raise LCDRequestError("Validator not found", status=404)

        summary = _avalanche_summary_from_validator(validator)
        # _logger.info(
            # "Avalanche validator summary payload valoper=%s summary=%s raw_validator=%s",
            # valoper,
            # json.dumps(summary, default=str),
            # json.dumps(validator, default=str),
        # )
        return summary

    if protocol_key == "subsquid":
        return _subsquid_validator_summary(valoper, rpc_base_url)

    if protocol_key == "injective":
        if not rpc_base_url:
            raise LCDRequestError("Protocol RPC endpoint is not configured")

        summary = _injective_validator_summary(valoper, rpc_base_url)
        return summary

    if protocol_key == "cosmos":
        if not rpc_base_url:
            raise LCDRequestError("Protocol RPC endpoint is not configured")
        return _cosmos_validator_summary(valoper, rpc_base_url)

    if protocol_key == "nillion":
        if not rpc_base_url:
            raise LCDRequestError("Protocol RPC endpoint is not configured")
        return _nillion_validator_summary(valoper, rpc_base_url)

    if protocol_key == "dchain":
        if not rpc_base_url:
            raise LCDRequestError("Protocol RPC endpoint is not configured")
        return _dchain_validator_summary(valoper, rpc_base_url)

    if protocol_key == "skale":
        if not rpc_base_url:
            raise LCDRequestError("Protocol RPC endpoint is not configured")
        # _logger.info(
        #     "Fetching SKALE validator summary valoper=%s rpc_base_url=%s",
        #     valoper,
        #     rpc_base_url,
        # )
        summary = _skale_validator_summary(valoper, rpc_base_url)
        return summary

    if protocol_key == "opn":
        if not rpc_base_url:
            raise LCDRequestError("Protocol RPC endpoint is not configured")
        return _iopn_validator_summary(valoper, rpc_base_url)

    if protocol_key == "near":
        if not rpc_base_url:
            raise LCDRequestError("Protocol RPC endpoint is not configured")
        # Validator address is already the NEAR account ID (e.g., bisontrails2.poolv1.near)
        # Delegator information is fetched from RPC APIs
        summary = _near_validator_summary(valoper, rpc_base_url,delegation_address=delegation_address)
    
        return summary

    if protocol_key == "energyweb":
        if not rpc_base_url:
            raise LCDRequestError("Protocol RPC endpoint is not configured")
        return _ewx_validator_summary(valoper, rpc_base_url)

    if protocol_key == "theta":
        summary = _theta_validator_summary(valoper, rpc_base_url)
        return summary

    if protocol_key == "xdc":
        return _xdc_validator_summary(valoper, rpc_base_url)

    if protocol_key == "solana":
        if not rpc_base_url:
            raise LCDRequestError("Protocol RPC endpoint is not configured")
        return _solana_validator_summary(valoper, rpc_base_url)

    validator_payload = _lcd_get_json(
        f"/cosmos/staking/v1beta1/validators/{valoper}",
        allow_404=True,
        base_url=rpc_base_url,
    )

    if not validator_payload or "validator" not in validator_payload:
        raise LCDRequestError("Validator not found", status=404)

    validator = validator_payload["validator"]

    description = validator.get("description") or {}
    website = (description.get("website") or "").strip()
    moniker = (description.get("moniker") or "").strip()
    email = (description.get("security_contact") or "").strip()
    description_details = (description.get("details") or "").strip()
    min_self_delegation_raw = validator.get("min_self_delegation")
    min_self_delegation = (
        str(min_self_delegation_raw).strip() if min_self_delegation_raw not in (None, "") else ""
    )

    commission_info = (validator.get("commission") or {}).get("commission_rates") or {}

    tokens_micro = _safe_int(validator.get("tokens"))
    total_bonded = _get_total_bonded_tokens(base_url=rpc_base_url)
    voting_power_pct = _pct(tokens_micro, total_bonded)
    tokens = _micro_to_core_string(tokens_micro)

    commission_str = (
        ((validator.get("commission") or {}).get("commission_rates") or {}).get("rate")
    )
    try:
        commission_pct = float(commission_str) * 100 if commission_str is not None else 0.0
    except (TypeError, ValueError):
        commission_pct = 0.0

    outstanding_payload = _lcd_get_json(
        f"/cosmos/distribution/v1beta1/validators/{valoper}/outstanding_rewards",
        allow_404=True,
        base_url=rpc_base_url,
    ) or {}
    outstanding_micro = _sum_coin_amounts(outstanding_payload.get("rewards", {}).get("rewards", []))
    outstanding_rewards = _micro_to_core_number(outstanding_micro)

    # owned rewards means self stake rewards and delegation reward is the commision earned 
    owned_rewards_micro = 0
    delegation_rewards_micro = 0

    delegator_addr = _valoper_to_delegator(valoper)
    if delegator_addr:
        try:
            owned_payload = _lcd_get_json(
                f"/cosmos/distribution/v1beta1/delegators/{delegator_addr}/rewards/{valoper}",
                allow_404=True,
                base_url=rpc_base_url,
            ) or {}
            owned_rewards_micro = _sum_coin_amounts(owned_payload.get("rewards"))
        except LCDRequestError:
            pass

    try:
        commission_payload = _lcd_get_json(
            f"/cosmos/distribution/v1beta1/validators/{valoper}/commission",
            allow_404=True,
            base_url=rpc_base_url,
        ) or {} 
        delegation_rewards_micro = _sum_coin_amounts(
            (((commission_payload.get("commission") or {}).get("commission")) or [])
        )
    except LCDRequestError:
        pass

    total_rewards_micro = owned_rewards_micro + delegation_rewards_micro

    jailed = bool(validator.get("jailed"))
    identity = (validator.get("description") or {}).get("identity")
    status_meta = _validator_status_map(validator.get("status"))

    uptime_pct = 0.0
    connection_status = "unknown"
    window_size = None
    try:
        slashing_payload = _lcd_get_json(
            "/cosmos/slashing/v1beta1/params",
            base_url=rpc_base_url,
        )
        window_size = _safe_int((slashing_payload.get("params") or {}).get("signed_blocks_window"))
    except LCDRequestError:
        window_size = None

    pubkey_info = validator.get("consensus_pubkey")
    pubkey = None
    if isinstance(pubkey_info, dict):
        pubkey = pubkey_info.get("key")
    elif isinstance(pubkey_info, str):
        pubkey = pubkey_info

    valcons_addr = None
    if pubkey:
        try:
            validator_set_payload = _lcd_get_json(
                "/cosmos/base/tendermint/v1beta1/validatorsets/latest",
                base_url=rpc_base_url,
            ) or {}
            for item in validator_set_payload.get("validators") or []:
                pub = item.get("pub_key") or {}
                if pub.get("key") == pubkey:
                    valcons_addr = item.get("address")
                    break
        except LCDRequestError:
            pass

    missed_counter = None
    if valcons_addr:
        page_key = None
        connection_status = "disconnected"
        for _ in range(20):
            params = {}
            if page_key:
                params["pagination.key"] = page_key
            signing_payload = _lcd_get_json(
                "/cosmos/slashing/v1beta1/signing_infos",
                params=params if params else None,
                base_url=rpc_base_url,
            )
            if not signing_payload:
                break
            for info in signing_payload.get("info") or []:
                if info.get("address") == valcons_addr:
                    missed_counter = _safe_int(info.get("missed_blocks_counter"))
                    connection_status = "connected"
                    break
            if connection_status == "connected":
                break
            page_key = (signing_payload.get("pagination") or {}).get("next_key")
            if not page_key:
                break

    if (
        window_size
        and missed_counter is not None
        and window_size > 0
        and missed_counter <= window_size
    ):
        uptime_pct = _pct(max(window_size - missed_counter, 0), window_size)

    delegator_count = 0
    try:
        params: Dict[str, Any] = {
            "pagination.count_total": "true",
            "pagination.limit": 1,
        }
        delegations_payload = _lcd_get_json(
            f"/cosmos/staking/v1beta1/validators/{valoper}/delegations",
            params=params,
            base_url=rpc_base_url,
            timeout=10,
        ) or {}
        delegator_count = (delegations_payload.get("pagination") or {}).get("total") or 0
    except LCDRequestError:
        pass

    summary = {
        "tokens": tokens,
        "votingPowerPct": voting_power_pct,
        "commissionPct": commission_pct,
        "commission": commission_info,
        "outstandingRewards": outstanding_rewards,
        "ownedRewards": _micro_to_core_string(owned_rewards_micro),
        "delegationRewards": _micro_to_core_string(delegation_rewards_micro),
        "totalRewards": _micro_to_core_string(total_rewards_micro),
        "uptimePct": uptime_pct,
        "status": status_meta["status"],
        "statusLabel": status_meta.get("label"),
        "email": email,
        "jailed": jailed,
        "connectionStatus": connection_status,
        "identity": identity,
        "website": website,
        "moniker": moniker,
        "description": description_details,
        "min_self_delegation": min_self_delegation,
        "delegatorCount": delegator_count,
    }
    

    return summary


def _compute_validator_delegations(
    valoper: str,
    protocol_key: str,
    rpc_base_url: Optional[str],
    cursor: Optional[str] = None,
    flow_context: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Compute delegations for validators across supported protocols."""
    if not valoper:
        raise LCDRequestError("Validator identifier is required")

    if protocol_key == "flow":
        if not rpc_base_url:
            raise LCDRequestError("Protocol RPC endpoint is not configured")
        network_hint = (flow_context or {}).get("network")
        owner_address = (flow_context or {}).get("owner_address")
        details = _flow_fetch_validator_details(
            rpc_base_url,
            valoper,
            network_hint,
            owner_address=owner_address,
        )
        return _flow_delegations_from_details(details)

    if protocol_key == "avalanche":
        if not rpc_base_url:
            raise LCDRequestError("Protocol RPC endpoint is not configured")

        validator = _avalanche_fetch_validator(valoper, rpc_base_url)
        if not validator:
            raise LCDRequestError("Validator not found", status=404)

        items = _avalanche_delegations_from_validator(validator)
        # _logger.info(
            # "Avalanche validator delegations payload valoper=%s items=%s raw_validator=%s",
            # valoper,
            # json.dumps(items, default=str),
            # json.dumps(validator, default=str),
        # )
        return {
            "items": items,
            "nextCursor": None,
        }

    if protocol_key == "subsquid":
        if not rpc_base_url:
            raise LCDRequestError("Protocol RPC endpoint is not configured")
        return _subsquid_validator_delegations(valoper, rpc_base_url)

    if protocol_key == "injective":
        if not rpc_base_url:
            raise LCDRequestError("Protocol RPC endpoint is not configured")
        return _injective_validator_delegations(valoper, rpc_base_url, cursor)

    if protocol_key == "cosmos":
        return {
            "items": [],
            "nextCursor": None,
        }
    if protocol_key == "nillion":
        if not rpc_base_url:
            raise LCDRequestError("Protocol RPC endpoint is not configured")
        return _nillion_validator_delegations(valoper, rpc_base_url, cursor)

    if protocol_key == "dchain":
        if not rpc_base_url:
            raise LCDRequestError("Protocol RPC endpoint is not configured")
        return _dchain_validator_delegations(valoper, rpc_base_url, cursor)

    if protocol_key == "skale":
        if not rpc_base_url:
            raise LCDRequestError("Protocol RPC endpoint is not configured")
        return _skale_validator_delegations(valoper, rpc_base_url, cursor)

    if protocol_key == "near":
        if not rpc_base_url:
            raise LCDRequestError("Protocol RPC endpoint is not configured")
        return _near_validator_delegations(valoper, rpc_base_url, cursor)

    if protocol_key == "energyweb":
        if not rpc_base_url:
            raise LCDRequestError("Protocol RPC endpoint is not configured")
        return _ewx_validator_delegations(valoper, rpc_base_url, cursor)

    if protocol_key == "theta":
        return _theta_validator_delegations(valoper, rpc_base_url, cursor)

    if protocol_key == "xdc":
        return {
            "items": [],
            "nextCursor": None,
        }

    if protocol_key == "solana":
        return {
            "items": [],
            "nextCursor": None,
        }

    if protocol_key == "opn":
        if not rpc_base_url:
            raise LCDRequestError("Protocol RPC endpoint is not configured")
        return _iopn_validator_delegations(valoper, rpc_base_url, cursor)

    validator_payload = _lcd_get_json(
        f"/cosmos/staking/v1beta1/validators/{valoper}",
        allow_404=True,
        base_url=rpc_base_url,
    )
    if not validator_payload or "validator" not in validator_payload:
        raise LCDRequestError("Validator not found", status=404)

    validator = validator_payload["validator"]
    validator_tokens = max(_safe_int(validator.get("tokens")), 0)

    params: Dict[str, Any] = {}
    if cursor:
        params["pagination.key"] = cursor

    delegations_payload = _lcd_get_json(
        f"/cosmos/staking/v1beta1/validators/{valoper}/delegations",
        params=params if params else None,
        base_url=rpc_base_url,
    ) or {}

    parameters: Dict[str, Any] = {
    "pagination.count_total": "true",
    "pagination.limit": 1,  # keep the response small
    }
    delegations_pay = _lcd_get_json(
    f"/cosmos/staking/v1beta1/validators/{valoper}/delegations",
    params=parameters,
    base_url=rpc_base_url,
    ) or {}
    total_delegators = (delegations_pay.get("pagination") or {}).get("total")
    _logger.info(
        "Total delegators for validator %s: %s",
        valoper,
        total_delegators,
        delegations_pay
    )
    


    items = []
    for delegation in delegations_payload.get("delegation_responses") or []:
        balance = (delegation.get("balance") or {})
        amount_micro = _safe_int(balance.get("amount"))
        denom = balance.get("denom") or ""
        if denom == "ucore":
            denom = "core"

        pct_of_validator = 0.0
        if validator_tokens > 0:
            pct_of_validator = _pct(amount_micro, validator_tokens)

        items.append(
            {
                "delegatorAddress": (delegation.get("delegation") or {}).get("delegator_address"),
                "amount": _micro_to_core_string(amount_micro),
                "denom": denom,
                "pctOfValidator": pct_of_validator,
            }
        )

    return {
        "items": items,
        "nextCursor": (delegations_payload.get("pagination") or {}).get("next_key"),
    }


def _fetch_coreum_performance_data(valoper: str, rpc_base_url: str) -> Dict[str, Any]:
    """
    Fetch performance data for a Coreum validator.
    Returns dict with performance data or error information.
    Never raises exceptions - returns error dict instead.
    Single attempt with 10 second timeout - no retries.
    
    Args:
        valoper: Validator operator address (valoper...)
        rpc_base_url: RPC endpoint base URL
    
    Returns:
        Success: {'height': int, 'missedCounter': int, 'windowSize': int, 'valconsAddr': str}
        Error: {'error': str, 'note': str, 'series': []}
    """
    try:
        # Fetch latest block height
        try:
            latest_resp = requests.get(
                f"{rpc_base_url}/cosmos/base/tendermint/v1beta1/blocks/latest",
                timeout=10
            )
            latest_resp.raise_for_status()
            latest_data = latest_resp.json()
            height = int(latest_data['block']['header']['height'])
        except Exception as e:
            _logger.warning(f"Failed to fetch latest block: {str(e)}")
            return {
                'error': 'lcd_unavailable',
                'note': 'Unable to fetch blockchain data',
                'series': []
            }
        
        # Fetch slashing parameters
        try:
            slashing_resp = requests.get(
                f"{rpc_base_url}/cosmos/slashing/v1beta1/params",
                timeout=10
            )
            slashing_resp.raise_for_status()
            slashing_data = slashing_resp.json()
            window_size = int(slashing_data['params']['signed_blocks_window'])
        except Exception as e:
            _logger.warning(f"Failed to fetch slashing params: {str(e)}")
            return {
                'error': 'lcd_unavailable',
                'note': 'Unable to fetch slashing parameters',
                'series': []
            }
        
        # Get validator consensus pubkey
        try:
            validator_payload = _lcd_get_json(
                f"/cosmos/staking/v1beta1/validators/{valoper}",
                allow_404=True,
                base_url=rpc_base_url,
                timeout=10,
            )
            
            if not validator_payload or "validator" not in validator_payload:
                return {
                    'error': 'validator_not_found',
                    'note': 'Validator not found',
                    'series': []
                }
            
            validator = validator_payload["validator"]
            pubkey_info = validator.get("consensus_pubkey")
            pubkey_b64 = None
            
            if isinstance(pubkey_info, dict):
                pubkey_b64 = pubkey_info.get("key")
            elif isinstance(pubkey_info, str):
                pubkey_b64 = pubkey_info
            
            if not pubkey_b64:
                return {
                    'error': 'validator_not_active',
                    'note': 'Validator consensus key not found',
                    'series': []
                }
        except Exception as e:
            _logger.warning(f"Failed to fetch validator info: {str(e)}")
            return {
                'error': 'validator_not_found',
                'note': 'Unable to fetch validator information',
                'series': []
            }
        
        # Map pubkey to valcons address
        try:
            valset_resp = requests.get(
                f"{rpc_base_url}/cosmos/base/tendermint/v1beta1/validatorsets/latest",
                timeout=10
            )
            valset_resp.raise_for_status()
            valset_data = valset_resp.json()
            
            valcons_addr = None
            for val in valset_data.get('validators', []):
                pub = val.get('pub_key') or {}
                if pub.get('key') == pubkey_b64:
                    valcons_addr = val.get('address')
                    break
            
            if not valcons_addr:
                return {
                    'error': 'validator_not_active',
                    'note': 'Validator not in active set',
                    'series': []
                }
        except Exception as e:
            _logger.warning(f"Failed to fetch validator set: {str(e)}")
            return {
                'error': 'lcd_unavailable',
                'note': 'Unable to fetch validator set',
                'series': []
            }
        
        # Find signing info with pagination
        try:
            missed_counter = None
            page_key = None
            max_pages = 20
            
            for page in range(max_pages):
                params = {}
                if page_key:
                    params['pagination.key'] = page_key
                
                signing_resp = requests.get(
                    f"{rpc_base_url}/cosmos/slashing/v1beta1/signing_infos",
                    params=params,
                    timeout=10
                )
                signing_resp.raise_for_status()
                signing_data = signing_resp.json()
                
                # Find matching signing info
                for info in signing_data.get('info', []):
                    if info.get('address') == valcons_addr:
                        missed_counter = int(info.get('missed_blocks_counter', 0))
                        break
                
                if missed_counter is not None:
                    break
                
                # Check for next page
                page_key = signing_data.get('pagination', {}).get('next_key')
                if not page_key:
                    break
            
            if missed_counter is None:
                return {
                    'error': 'signing_info_unavailable',
                    'note': 'Signing information not available',
                    'series': []
                }
        except Exception as e:
            _logger.warning(f"Failed to fetch signing info: {str(e)}")
            return {
                'error': 'lcd_unavailable',
                'note': 'Unable to fetch signing information',
                'series': []
            }
        
        # Success - return data
        return {
            'height': height,
            'missedCounter': missed_counter,
            'windowSize': window_size,
            'valconsAddr': valcons_addr
        }
        
    except Exception as e:
        _logger.error(f"Unexpected error in performance fetch: {str(e)}", exc_info=True)
        return {
            'error': 'unknown_error',
            'note': f'Unexpected error: {str(e)}',
            'series': []
        }


def _fetch_cosmos_performance_data(valoper: str, rpc_base_url: str) -> Dict[str, Any]:
    """Fetch Cosmos Hub validator performance using Cosmos slashing endpoints.

    Cosmos Hub exposes the same slashing/signing_infos APIs as Coreum, so this
    delegates directly to ``_fetch_coreum_performance_data``.

    Returns:
        Success: {'height': int, 'missedCounter': int, 'windowSize': int, 'valconsAddr': str}
        Error:   {'error': str, 'note': str, 'series': []}
    """
    return _fetch_coreum_performance_data(valoper, rpc_base_url)


def _fetch_injective_performance_data(valoper: str, rpc_base_url: str) -> Dict[str, Any]:
    """Fetch Injective validator performance using Cosmos slashing endpoints."""
    # Injective exposes the same Cosmos slashing APIs; reuse the Coreum logic.
    return _fetch_coreum_performance_data(valoper, rpc_base_url)


def _fetch_avalanche_performance_data(valoper: str, rpc_base_url: str) -> Dict[str, Any]:
    """Build a pseudo window snapshot for Avalanche validators using uptime."""
    try:
        validator = _avalanche_fetch_validator(valoper, rpc_base_url)
    except LCDRequestError as exc:
        return {
            "error": "rpc_error",
            "note": str(exc),
            "series": [],
        }

    if not validator:
        return {
            "error": "validator_not_found",
            "note": "Validator not found",
            "series": [],
        }

    uptime_value = _safe_float(validator.get("uptime"))
    uptime_pct = uptime_value * 100 if uptime_value <= 1 else uptime_value
    uptime_pct = max(min(uptime_pct, 100.0), 0.0)

    window_size = 100
    signed_blocks = int(round(window_size * (uptime_pct / 100.0)))
    signed_blocks = max(min(signed_blocks, window_size), 0)
    missed_blocks = window_size - signed_blocks

    chain_height = _fetch_avalanche_chain_height(rpc_base_url)
    height_source = validator.get("endTime") or validator.get("startTime")
    height = chain_height if chain_height and chain_height > 0 else _safe_int(height_source)
    if height <= 0:
        height = int(time.time())

    return {
        "height": height,
        "missedCounter": missed_blocks,
        "windowSize": window_size,
        "valconsAddr": validator.get("nodeID"),
        "uptimePct": uptime_pct,
    }


def _fetch_near_performance_data(valoper: str, rpc_base_url: str) -> Dict[str, Any]:
    """Fetch NEAR validator performance data for snapshot."""
    try:
        # Find validator and get performance metrics
        validator = _near_find_validator(valoper, rpc_base_url)
        _logger.info(
            "NEAR validator performance data valoper=%s validator=%s",
            valoper,
            json.dumps(validator or {}, default=str),
        )
        produced_blocks = max(_safe_int(validator.get("num_produced_blocks")), 0)
        expected_blocks = max(_safe_int(validator.get("num_expected_blocks")), 0)
        _logger.info(
            "NEAR validator blocks valoper=%s produced=%d expected=%d",
            valoper,
            produced_blocks,
            expected_blocks,
        )
        # Calculate missed blocks (handle edge case where expected is 0)
        missed_blocks = max(expected_blocks - produced_blocks, 0)
        
        # Get network block height
        status_result = _call_near_rpc(rpc_base_url, "status", [])

        sync_info = status_result.get("sync_info", {}) if isinstance(status_result, dict) else {}
        block_height = _safe_int(sync_info.get("latest_block_height")) if isinstance(sync_info, dict) else 0
        
        return {
            "height": block_height,
            "missedCounter": missed_blocks,
            "windowSize": 0,
            "valconsAddr": valoper,
            "expectedBlocks": expected_blocks,
            'producedBlocks': produced_blocks,
        }
    except LCDRequestError as exc:
        return {
            "error": "rpc_error",
            "note": str(exc),
            "series": [],
        }


def _collect_validator_performance_snapshot(
    protocol_key: str,
    valoper: str,
    rpc_base_url: str,
    validator_info_json: Optional[str] = None,
) -> Dict[str, Any]:
    """Protocol-aware dispatcher for performance snapshot jobs.
    
    Args:
        protocol_key: Protocol name (e.g., "coreum", "opn", "injective")
        valoper: Validator operator address
        rpc_base_url: RPC endpoint base URL
        validator_info_json: Optional JSON string containing validator info with pubkey (used for OPN)
    
    Returns:
        Dict with performance data or error info
    """
    from . import iopn_utils

    normalized_key = _normalize_protocol_name(protocol_key)
    if normalized_key == "coreum":
        return _fetch_coreum_performance_data(valoper, rpc_base_url)

    if normalized_key == "cosmos":
        return _fetch_cosmos_performance_data(valoper, rpc_base_url)

    if normalized_key == "opn":
        return iopn_utils._fetch_opn_performance_data(valoper, rpc_base_url, validator_info_json)

    if normalized_key == "injective":
        return _fetch_injective_performance_data(valoper, rpc_base_url)

    if normalized_key == "avalanche":
        return _fetch_avalanche_performance_data(valoper, rpc_base_url)

    if normalized_key == "near":
        return _fetch_near_performance_data(valoper, rpc_base_url)

    return {
        "error": "protocol_not_supported",
        "note": f"Performance snapshots are not enabled for {protocol_key or 'unknown'}",
        "series": [],
    }


def _fetch_validator_performance_with_period(
    valoper: str,
    protocol_key: str,
    rpc_base_url: str,
    period_days: int = 7,
    protocol_record_id: Optional[int] = None,
    node_id: Optional[int] = None,
) -> Dict[str, Any]:
    """
    Fetch validator performance data for a specified period.
    
    Args:
        valoper: Validator operator address
        rpc_base_url: RPC endpoint base URL
        period_days: Number of days (1, 7, or 30)
        protocol_record_id: Optional protocol ID for filtering
        node_id: Optional node ID for filtering
    
    Returns:
        Dict with performance data: {series, latestHeight, valconsAddr, windowSize}
    """
    _logger.info(
        "Fetching validator performance for period=%d days protocol=%s valoper=%s node_id=%s",
        period_days,
        protocol_key,
        valoper,
        node_id,
    )

    try:
        
        normalized_protocol = _normalize_protocol_name(protocol_key)
        dispatcher_key = normalized_protocol or (protocol_key or "")

        # Calculate date range using day boundaries (midnight) to ensure we capture
        # all snapshots from the start of the period day, not just from the exact time
        today = datetime.now().date()
        from_date = datetime.combine(today - timedelta(days=period_days), datetime.min.time())
        domain = [
            ('valoper', '=', valoper),
            ('snapshot_date', '>=', from_date),
        ]
        if node_id:
            domain.append(('node_id', '=', node_id))
        if normalized_protocol:
            domain.append(('protocol_key', '=', normalized_protocol))
        if protocol_record_id:
            domain.append(('protocol_id', '=', protocol_record_id))

        # Query snapshots from database
        snapshots = request.env['validator.performance.snapshot'].sudo().search(
            domain,
            order='snapshot_date asc',
        )
        _logger.info(
            "Found %d performance snapshots for valoper=%s protocol=%s from_date=%s",
            len(snapshots),
            valoper,
            normalized_protocol,
            str(from_date),
        )

        series: List[Dict[str, Any]] = []
        if snapshots:
            if normalized_protocol in {"coreum", "injective"} and len(snapshots) > 1:
                # Cosmos chains (Coreum/Injective) store cumulative counters; derive per-window deltas
                for i in range(1, len(snapshots)):
                    prev = snapshots[i - 1]
                    curr = snapshots[i]
                    snapshot_window_size = curr.window_size or prev.window_size or 0

                    delta_missed = (curr.missed_counter or 0) - (prev.missed_counter or 0)
                    missed = max(0, delta_missed)
                    signed = max(0, snapshot_window_size - missed)

                    series.append(
                        {
                            'height': curr.height,
                            'signed': signed,
                            'missed': missed,
                        }
                    )
            elif normalized_protocol == "near":
                # NEAR protocols store expected/produced blocks directly
                for snapshot in snapshots:
                    series.append(
                        {
                            'height': snapshot.height,
                            'expected_blocks': snapshot.expected_blocks,
                            'produced_blocks': snapshot.produced_blocks,
                            'missed':snapshot.missed_counter or 0,
                        }
                    )
            else:
                # Non-Coreum protocols store per-snapshot values directly
                for snapshot in snapshots:
                    window_size = snapshot.window_size or 0
                    missed = max(snapshot.missed_counter or 0, 0)
                    signed = max(window_size - missed, 0)
                    series.append(
                        {
                            'height': snapshot.height,
                            'signed': signed,
                            'missed': missed,
                        }
                    )

            latest = snapshots[-1]
            return {
                'series': series,
                'latestHeight': latest.height,
                'valconsAddr': latest.valcons_addr,
                'windowSize': latest.window_size,
            }

        # No snapshots - fetch real-time data as fallback
        result = _collect_validator_performance_snapshot(dispatcher_key, valoper, rpc_base_url)

        if 'error' in result:
            return {
                'series': [],
                'error': result['error'],
                'note': result.get('note'),
            }

        if normalized_protocol == "near":
            series = [{
                'height': result['height'],
                'expected_blocks': result.get('expectedBlocks', 0),
                'produced_blocks': result.get('producedBlocks', 0),
                'missed': result.get('missedCounter', 0),
            }]
        else:
            series = [{
                'height': result['height'],
                'signed': max(0, result['windowSize'] - result['missedCounter']),
                'missed': result['missedCounter'],
            }]
        return {
            'series': series,
            'latestHeight': result['height'],
            'valconsAddr': result.get('valconsAddr'),
            'windowSize': result['windowSize'],
        }
    
    except Exception as e:
        _logger.error(
            "Error fetching performance data for period=%d: %s",
            period_days,
            str(e),
            exc_info=True,
        )
        return {
            'series': [],
            'error': 'internal_error',
            'note': 'Error fetching performance data'
        }


def _get_network_apr(rpc_base_url: str) -> float:
    """Calculate network-wide staking APR from on-chain inflation and bonded ratio.

    Uses the native bond denom supply only (fetched via /supply/by_denom) so that
    IBC tokens and smart-contract tokens with vastly different amounts don't distort
    the bonded ratio.
    """
    try:
        # 1. Inflation (a decimal like 0.07 for 7% annual)
        inflation_payload = _lcd_get_json(
            "/cosmos/mint/v1beta1/inflation",
            base_url=rpc_base_url,
            timeout=10,
        ) or {}
        inflation = float(inflation_payload.get("inflation", 0))

        # 2. Native bond denom (e.g. "ucore", "uatom")
        params_payload = _lcd_get_json(
            "/cosmos/staking/v1beta1/params",
            base_url=rpc_base_url,
            timeout=10,
        ) or {}
        bond_denom = ((params_payload.get("params") or {}).get("bond_denom") or "").strip()
        if not bond_denom:
            _logger.warning("Could not determine bond_denom for network APR calculation")
            return 0.0

        # 3. Total supply of the native bond denom only
        supply_payload = _lcd_get_json(
            "/cosmos/bank/v1beta1/supply/by_denom",
            params={"denom": bond_denom},
            base_url=rpc_base_url,
            timeout=10,
        ) or {}
        total_supply_micro = _safe_int(
            (supply_payload.get("amount") or {}).get("amount")
        )

        # 4. Total bonded tokens (from staking pool, also in native denom)
        total_bonded_micro = _get_total_bonded_tokens(base_url=rpc_base_url)

        _logger.info(
            "Network APR inputs: inflation=%s bond_denom=%s total_supply=%s total_bonded=%s",
            inflation, bond_denom, total_supply_micro, total_bonded_micro,
        )

        if not total_supply_micro or not total_bonded_micro:
            return 0.0

        bonded_ratio = total_bonded_micro / total_supply_micro
        if bonded_ratio == 0:
            return 0.0

        network_apr = (inflation / bonded_ratio) * 100
        _logger.info(
            "Network APR result: bonded_ratio=%s network_apr=%s%%",
            round(bonded_ratio, 6), round(network_apr, 4),
        )
        return network_apr
    except Exception:
        _logger.exception("Failed to calculate network APR")
        return 0.0


def _get_validator_apr(commission_pct: float, network_apr: float) -> float:
    """Derive validator-level APR after deducting commission.

    Args:
        commission_pct: Commission in percent (e.g. 5.0 for 5%).
        network_apr: Network-wide APR in percent.
    """
    return network_apr * (1 - commission_pct / 100)


def _fetch_validator_outstanding_rewards(valoper: str, rpc_base_url: str) -> Dict[str, Any]:
    """
    Fetch outstanding rewards, total stake, delegator count, commission, and APR for a validator.
    Returns dict with rewards data or error information.
    Never raises exceptions - returns error dict instead.
    Single attempt with 10 second timeout - no retries.
    
    Args:
        valoper: Validator operator address (valoper...)
        rpc_base_url: RPC endpoint base URL
    
    Returns:
        Success: {'outstanding_rewards': float, 'tokens': float, 'delegator_count': int,
                  'commission_pct': float, 'apr_pct': float, 'network_apr': float}
        Error: {'error': str, 'note': str}
    """
    try:
        # Fetch outstanding rewards with 10 second timeout
        outstanding_payload = _lcd_get_json(
            f"/cosmos/distribution/v1beta1/validators/{valoper}/outstanding_rewards",
            allow_404=True,
            base_url=rpc_base_url,
            timeout=10,
        ) or {}
        
        rewards_field = outstanding_payload.get("rewards")
        if isinstance(rewards_field, dict):
            rewards_field = rewards_field.get("rewards")
        
        outstanding_rewards_micro = _sum_coin_amounts(rewards_field)
        
        # Convert from micro to main token units
        outstanding_rewards = _micro_to_core_number(outstanding_rewards_micro)
        
        # Fetch validator details to get total stake and commission
        tokens = 0.0
        owned_stake = 0.0
        commission_pct = 0.0
        try:
            validator_payload = _lcd_get_json(
                f"/cosmos/staking/v1beta1/validators/{valoper}",
                allow_404=True,
                base_url=rpc_base_url,
                timeout=10,
            )
            
            if validator_payload and "validator" in validator_payload:
                validator = validator_payload["validator"]
                tokens_micro = _safe_int(validator.get("tokens"))
                tokens = _micro_to_core_number(tokens_micro)
                owned_stake = _micro_to_core_number(
                    _fetch_self_delegation_amount(
                        valoper,
                        rpc_base_url,
                        request_fn=_lcd_get_json,
                    )
                )
                commission_str = (
                    ((validator.get("commission") or {}).get("commission_rates") or {})
                    .get("rate")
                )
                try:
                    commission_pct = float(commission_str) * 100 if commission_str is not None else 0.0
                except (TypeError, ValueError):
                    commission_pct = 0.0
        except Exception as e:
            _logger.warning(f"Failed to fetch validator tokens/commission for {valoper}: {str(e)}")
            tokens = 0.0
        
        # Fetch delegator count
        delegator_count = 0
        try:
            params: Dict[str, Any] = {
                "pagination.count_total": "true",
                "pagination.limit": 1,  # keep the response small
            }
            delegations_payload = _lcd_get_json(
                f"/cosmos/staking/v1beta1/validators/{valoper}/delegations",
                params=params,
                base_url=rpc_base_url,
                timeout=10,
            ) or {}
            delegator_count = (delegations_payload.get("pagination") or {}).get("total") or 0
        except Exception as e:
            _logger.warning(f"Failed to fetch delegator count for {valoper}: {str(e)}")
            delegator_count = 0
        
        # Calculate total rewards (owned + commission)
        owned_rewards_micro = 0
        delegation_rewards_micro = 0
        
        delegator_addr = _valoper_to_delegator(valoper)
        if delegator_addr:
            try:
                owned_payload = _lcd_get_json(
                    f"/cosmos/distribution/v1beta1/delegators/{delegator_addr}/rewards/{valoper}",
                    allow_404=True,
                    base_url=rpc_base_url,
                    timeout=10,
                ) or {}
                owned_rewards_micro = _sum_coin_amounts(owned_payload.get("rewards"))
            except Exception as e:
                _logger.warning(f"Failed to fetch owned rewards for {valoper}: {str(e)}")
        
        try:
            commission_payload = _lcd_get_json(
                f"/cosmos/distribution/v1beta1/validators/{valoper}/commission",
                allow_404=True,
                base_url=rpc_base_url,
                timeout=10,
            ) or {}
            delegation_rewards_micro = _sum_coin_amounts(
                (commission_payload.get("commission") or {}).get("commission") or []
            )
        except Exception as e:
            _logger.warning(f"Failed to fetch commission for {valoper}: {str(e)}")
        
        total_rewards_micro = owned_rewards_micro + delegation_rewards_micro
        total_rewards = _micro_to_core_number(total_rewards_micro)

        # Calculate APR
        network_apr = _get_network_apr(rpc_base_url)
        apr_pct = _get_validator_apr(commission_pct, network_apr)

        return {
            'outstanding_rewards': outstanding_rewards,
            'total_rewards': total_rewards,
            'tokens': tokens,
            'owned_stake': owned_stake,
            'delegator_count': delegator_count,
            'commission_pct': commission_pct,
            'apr_pct': apr_pct,
            'network_apr': network_apr,
        }
        
    except LCDRequestError as e:
        _logger.warning(f"LCD request failed for {valoper}: {str(e)}")
        return {
            'error': 'lcd_unavailable',
            'note': 'Unable to fetch rewards data'
        }
    except Exception as e:
        _logger.error(f"Error fetching rewards for {valoper}: {str(e)}")
        return {
            'error': 'request_failed',
            'note': str(e)
        }


def _fetch_cosmos_reward_snapshot(valoper: str, rpc_base_url: str) -> Dict[str, Any]:
    """Fetch Cosmos Hub outstanding rewards, total stake, total rewards, and delegator count.

    Mirrors the data collected by ``_cosmos_validator_summary`` but returns the
    flat snapshot dict expected by the rewards queue writer::

        {
            "outstanding_rewards": float,   # unconverted staking rewards (ATOM)
            "total_rewards": float,          # commission + self-delegation rewards (ATOM)
            "tokens": float,                 # total bonded stake (ATOM)
            "delegator_count": int,
            "commission_pct": float,         # validator commission rate (%)
            "apr_pct": float,                # validator APR after commission (%)
            "network_apr": float,            # gross network APR (%)
        }

    Returns an error dict on failure so the queue can mark the record as
    failed rather than crashing the whole batch.
    """
    try:
        # --- outstanding rewards -------------------------------------------
        outstanding_payload = _lcd_get_json(
            f"/cosmos/distribution/v1beta1/validators/{valoper}/outstanding_rewards",
            allow_404=True,
            base_url=rpc_base_url,
            timeout=10,
        ) or {}
        rewards_field = outstanding_payload.get("rewards")
        if isinstance(rewards_field, dict):
            rewards_field = rewards_field.get("rewards")
        outstanding_rewards_uatom = _sum_coin_amounts(rewards_field)
        outstanding_rewards = _uatom_to_atom_number(outstanding_rewards_uatom)

        # --- total stake (tokens) and commission ----------------------------
        tokens = 0.0
        owned_stake = 0.0
        commission_pct = 0.0
        try:
            validator_payload = _lcd_get_json(
                f"/cosmos/staking/v1beta1/validators/{valoper}",
                allow_404=True,
                base_url=rpc_base_url,
                timeout=10,
            )
            if validator_payload and "validator" in validator_payload:
                validator = validator_payload["validator"]
                tokens_uatom = _safe_int(validator.get("tokens"))
                tokens = _uatom_to_atom_number(tokens_uatom)
                owned_stake = _uatom_to_atom_number(
                    _fetch_self_delegation_amount(
                        valoper,
                        rpc_base_url,
                        request_fn=_lcd_get_json,
                    )
                )
                commission_str = (
                    ((validator.get("commission") or {}).get("commission_rates") or {})
                    .get("rate")
                )
                try:
                    commission_pct = float(commission_str) * 100 if commission_str is not None else 0.0
                except (TypeError, ValueError):
                    commission_pct = 0.0
        except Exception as exc:
            _logger.warning("Failed to fetch Cosmos validator tokens for %s: %s", valoper, exc)

        # --- delegator count -----------------------------------------------
        delegator_count = 0
        try:
            params: Dict[str, Any] = {
                "pagination.count_total": "true",
                "pagination.limit": 1,
            }
            delegations_payload = _lcd_get_json(
                f"/cosmos/staking/v1beta1/validators/{valoper}/delegations",
                params=params,
                base_url=rpc_base_url,
                timeout=10,
            ) or {}
            delegator_count = _safe_int(
                (delegations_payload.get("pagination") or {}).get("total")
            )
        except Exception as exc:
            _logger.warning("Failed to fetch Cosmos delegator count for %s: %s", valoper, exc)

        # --- total rewards (self-delegation + commission) ------------------
        owned_rewards_uatom = 0
        commission_rewards_uatom = 0

        delegator_addr = _valoper_to_delegator(valoper)
        if delegator_addr:
            try:
                owned_payload = _lcd_get_json(
                    f"/cosmos/distribution/v1beta1/delegators/{delegator_addr}/rewards/{valoper}",
                    allow_404=True,
                    base_url=rpc_base_url,
                    timeout=10,
                ) or {}
                owned_rewards_uatom = _sum_coin_amounts(owned_payload.get("rewards"))
            except Exception as exc:
                _logger.warning("Failed to fetch Cosmos self-delegation rewards for %s: %s", valoper, exc)

        try:
            commission_payload = _lcd_get_json(
                f"/cosmos/distribution/v1beta1/validators/{valoper}/commission",
                allow_404=True,
                base_url=rpc_base_url,
                timeout=10,
            ) or {}
            commission_rewards_uatom = _sum_coin_amounts(
                (commission_payload.get("commission") or {}).get("commission") or []
            )
        except Exception as exc:
            _logger.warning("Failed to fetch Cosmos commission for %s: %s", valoper, exc)

        total_rewards_uatom = owned_rewards_uatom + commission_rewards_uatom
        total_rewards = _uatom_to_atom_number(total_rewards_uatom)

        # --- APR -----------------------------------------------------------
        network_apr = _get_network_apr(rpc_base_url)
        apr_pct = _get_validator_apr(commission_pct, network_apr)

        return {
            "outstanding_rewards": outstanding_rewards,
            "total_rewards": total_rewards,
            "tokens": tokens,
            "owned_stake": owned_stake,
            "delegator_count": delegator_count,
            "commission_pct": commission_pct,
            "apr_pct": apr_pct,
            "network_apr": network_apr,
        }

    except LCDRequestError as exc:
        _logger.warning("LCD request failed for Cosmos validator %s: %s", valoper, exc)
        return {
            "error": "lcd_unavailable",
            "note": "Unable to fetch Cosmos rewards data",
        }
    except Exception as exc:
        _logger.error("Error fetching Cosmos rewards snapshot for %s: %s", valoper, exc)
        return {
            "error": "request_failed",
            "note": str(exc),
        }


def _fetch_injective_reward_snapshot(valoper: str, rpc_base_url: str) -> Dict[str, Any]:
    """Fetch Injective outstanding rewards, total stake, delegator count, and APR."""
    try:
        outstanding_payload = _lcd_get_json(
            f"/cosmos/distribution/v1beta1/validators/{valoper}/outstanding_rewards",
            allow_404=True,
            base_url=rpc_base_url,
            timeout=10,
        ) or {}

        rewards_field = outstanding_payload.get("rewards")
        if isinstance(rewards_field, dict):
            rewards_field = rewards_field.get("rewards")

        outstanding_rewards_atto = _sum_coin_amounts(rewards_field)
        outstanding_rewards = _atto_to_inj_number(outstanding_rewards_atto)

        tokens = 0.0
        owned_stake = 0.0
        commission_pct = 0.0
        try:
            validator_payload = _lcd_get_json(
                f"/cosmos/staking/v1beta1/validators/{valoper}",
                allow_404=True,
                base_url=rpc_base_url,
                timeout=10,
            )

            if validator_payload and "validator" in validator_payload:
                validator = validator_payload["validator"]
                tokens_atto = _safe_int(validator.get("tokens"))
                tokens = _atto_to_inj_number(tokens_atto)
                owned_stake = _atto_to_inj_number(
                    _fetch_self_delegation_amount(
                        valoper,
                        rpc_base_url,
                        request_fn=_lcd_get_json,
                    )
                )
                commission_str = (
                    ((validator.get("commission") or {}).get("commission_rates") or {})
                    .get("rate")
                )
                try:
                    commission_pct = float(commission_str) * 100 if commission_str is not None else 0.0
                except (TypeError, ValueError):
                    commission_pct = 0.0
        except Exception as exc:
            _logger.warning("Failed to fetch Injective validator tokens for %s: %s", valoper, str(exc))
            tokens = 0.0

        delegator_count = 0
        try:
            params: Dict[str, Any] = {
                "pagination.count_total": "true",
                "pagination.limit": 1,
            }
            delegations_payload = _lcd_get_json(
                f"/cosmos/staking/v1beta1/validators/{valoper}/delegations",
                params=params,
                base_url=rpc_base_url,
                timeout=10,
            ) or {}
            delegator_count = _safe_int((delegations_payload.get("pagination") or {}).get("total"))
        except Exception as exc:
            _logger.warning("Failed to fetch Injective delegator count for %s: %s", valoper, str(exc))
            delegator_count = 0

        # Calculate total rewards (owned + commission)
        owned_rewards_atto = 0
        delegation_rewards_atto = 0
        
        delegator_addr = _valoper_to_delegator(valoper)
        if delegator_addr:
            try:
                owned_payload = _lcd_get_json(
                    f"/cosmos/distribution/v1beta1/delegators/{delegator_addr}/rewards/{valoper}",
                    allow_404=True,
                    base_url=rpc_base_url,
                    timeout=10,
                ) or {}
                owned_rewards_atto = _sum_coin_amounts(owned_payload.get("rewards"))
            except Exception as exc:
                _logger.warning("Failed to fetch Injective owned rewards for %s: %s", valoper, str(exc))
        
        try:
            commission_payload = _lcd_get_json(
                f"/cosmos/distribution/v1beta1/validators/{valoper}/commission",
                allow_404=True,
                base_url=rpc_base_url,
                timeout=10,
            ) or {}
            delegation_rewards_atto = _sum_coin_amounts(
                (commission_payload.get("commission") or {}).get("commission") or []
            )
        except Exception as exc:
            _logger.warning("Failed to fetch Injective commission for %s: %s", valoper, str(exc))
        
        total_rewards_atto = owned_rewards_atto + delegation_rewards_atto
        total_rewards = _atto_to_inj_number(total_rewards_atto)

        # --- APR -----------------------------------------------------------
        network_apr = _get_network_apr(rpc_base_url)
        apr_pct = _get_validator_apr(commission_pct, network_apr)

        return {
            "outstanding_rewards": outstanding_rewards,
            "total_rewards": total_rewards,
            "tokens": tokens,
            "owned_stake": owned_stake,
            "delegator_count": delegator_count,
            "commission_pct": commission_pct,
            "apr_pct": apr_pct,
            "network_apr": network_apr,
        }
    except LCDRequestError as exc:
        _logger.warning("LCD request failed for Injective %s: %s", valoper, str(exc))
        return {
            "error": "lcd_unavailable",
            "note": "Unable to fetch rewards data",
        }
    except Exception as exc:
        _logger.error("Error fetching Injective rewards for %s: %s", valoper, str(exc))
        return {
            "error": "request_failed",
            "note": str(exc),
        }


def _build_avalanche_reward_snapshot(validator: Dict[str, Any]) -> Dict[str, Any]:
    """Normalize Avalanche validator payload into rewards snapshot metrics."""
    validator_stake = max(_safe_int(validator.get("stakeAmount") or validator.get("weight")), 0)
    delegator_weight = max(_safe_int(validator.get("delegatorWeight")), 0)
    total_stake = validator_stake + delegator_weight
    commission_pct = _safe_float(validator.get("delegationFee"))

    # accruedDelegateeReward holds delegation fees from already-completed delegations.
    # Active delegators' fees are summed from their individual potentialReward fields.
    accrued_delegatee_reward = max(_safe_int(validator.get("accruedDelegateeReward")), 0)

    delegators = validator.get("delegators") or []
    active_delegator_reward = 0
    for entry in delegators if isinstance(delegators, list) else []:
        active_delegator_reward += max(_safe_int(entry.get("potentialReward")), 0)

    potential_reward = max(_safe_int(validator.get("potentialReward")), 0)
    delegation_fee_total = accrued_delegatee_reward + active_delegator_reward
    total_rewards = potential_reward + delegation_fee_total

    # APR: annualise the validator's own staking reward over the staking period.
    # apr_pct = (potentialReward / stakeAmount) * (365 / period_days) * 100
    apr_pct = None
    try:
        start_time = _safe_int(validator.get("startTime"))
        end_time = _safe_int(validator.get("endTime"))
        if validator_stake > 0 and potential_reward > 0 and start_time > 0 and end_time > start_time:
            period_days = (end_time - start_time) / 86400.0
            if period_days > 0:
                apr_pct = round(
                    (_nano_to_avax_number(potential_reward) / _nano_to_avax_number(validator_stake))
                    * (365.0 / period_days)
                    * 100.0,
                    4,
                )
    except Exception:
        _logger.debug("Failed to compute Avalanche APR for snapshot", exc_info=True)

    return {
        "outstanding_rewards": _nano_to_avax_number(total_rewards),
        "total_rewards": _nano_to_avax_number(total_rewards),
        "tokens": _nano_to_avax_number(total_stake),
        "owned_stake": _nano_to_avax_number(validator_stake),
        "delegator_count": len(delegators) if isinstance(delegators, list) else 0,
        "commission_pct": commission_pct,
        "apr_pct": apr_pct,
    }


def _fetch_near_reward_data(valoper: str, rpc_base_url: str, delegation_address: Optional[str] = None) -> Dict[str, Any]:
    """Fetch NEAR validator reward data for snapshot."""
    try:
        # Find validator and get stake
        validator = _near_find_validator(valoper, rpc_base_url)
        stake_yocto = max(_safe_int(validator.get("stake")), 0)

        # Fetch commission via smart contract call (mirrors _near_validator_summary)
        commission_pct = 0.0
        try:
            commission_payload = _call_near_rpc(
                rpc_base_url,
                "query",
                {
                    "request_type": "call_function",
                    "account_id": valoper,
                    "method_name": "get_reward_fee_fraction",
                    "args_base64": base64.b64encode(b"{}").decode("ascii"),
                    "finality": "final",
                },
            )
            if isinstance(commission_payload, dict):
                decoded = _near_decode_bytes(commission_payload.get("result"))
                if decoded:
                    try:
                        parsed = json.loads(decoded)
                        numerator = None
                        denominator = None
                        if isinstance(parsed, dict):
                            numerator = parsed.get("numerator")
                            denominator = parsed.get("denominator")
                        elif isinstance(parsed, (list, tuple)) and len(parsed) >= 2:
                            numerator, denominator = parsed[0], parsed[1]
                        if numerator is not None and denominator not in (None, 0, "0"):
                            numerator_val = float(numerator)
                            denominator_val = float(denominator)
                            if denominator_val:
                                commission_pct = (numerator_val / denominator_val) * 100
                    except (TypeError, ValueError):
                        commission_pct = 0.0
        except LCDRequestError:
            commission_pct = 0.0

        # Resolve reward account (mirrors _near_validator_summary logic)
        reward_account = _normalize_near_account_id(delegation_address)

        # Collect delegator metrics
        delegator_metrics = _near_collect_delegator_metrics(rpc_base_url, valoper, target_account_id=reward_account)
        delegator_count = delegator_metrics.get("count", 0)
        target_stake = max(_safe_int(delegator_metrics.get("target_stake")), 0) if reward_account else 0
        owned_stake = target_stake if reward_account else 0

        reward_balance_yocto = target_stake if reward_account else None
        outstanding_rewards_value = reward_balance_yocto if reward_balance_yocto is not None else None

        # Convert yoctoNEAR to NEAR
        outstanding_rewards = _yocto_to_near_number(outstanding_rewards_value if outstanding_rewards_value is not None else 0)
        tokens = _yocto_to_near_number(stake_yocto)

        return {
            "outstanding_rewards": outstanding_rewards,
            "total_rewards": outstanding_rewards,
            "tokens": tokens,
            "owned_stake": _yocto_to_near_number(owned_stake),
            "delegator_count": delegator_count,
            "commission_pct": commission_pct,
        }
    except LCDRequestError as exc:
        return {
            "error": "rpc_error",
            "note": str(exc),
        }


def _subsquid_reward_snapshot(peer_id: str, rpc_base_url: Optional[str]) -> Dict[str, Any]:
    """Normalize Subsquid rewards data for snapshot persistence."""
    try:
        worker = _subsquid_query_worker(peer_id, rpc_base_url)
    except LCDRequestError as exc:
        return {
            "error": "rpc_error",
            "note": str(exc),
        }
    except Exception as exc:  # pragma: no cover - defensive logging
        _logger.error("Unexpected Subsquid snapshot failure for %s: %s", peer_id, exc, exc_info=True)
        return {
            "error": "internal_error",
            "note": "Failed to fetch Subsquid rewards",
        }

    bond_raw = max(_safe_int(worker.get("bond")), 0)
    delegation_raw = max(_safe_int(worker.get("totalDelegation")), 0)
    claimable_raw = max(_safe_int(worker.get("claimableReward")), 0)

    delegator_count = max(_safe_int(worker.get("delegationCount")), 0)
    if not delegator_count:
        delegations = worker.get("delegations")
        if isinstance(delegations, list):
            delegator_count = sum(
                1
                for entry in delegations
                if isinstance(entry, dict) and max(_safe_int(entry.get("deposit")), 0) > 0
            )

    outstanding_rewards = _subsquid_int_to_number(claimable_raw)
    apr_pct = _safe_float(worker.get("apr"))
    return {
        "outstanding_rewards": outstanding_rewards,
        "total_rewards": outstanding_rewards,
        "tokens": _subsquid_int_to_number(bond_raw + delegation_raw),
        "owned_stake": _subsquid_int_to_number(bond_raw),
        "delegator_count": delegator_count,
        "apr_pct": apr_pct,
    }


def _fetch_ewx_reward_snapshot(
    valoper: str,
    rpc_base_url: str,
    ewx_reward_url: Optional[str] = None,
    ewx_api: Optional[str] = None,
) -> Dict[str, Any]:
    """Fetch EnergyWeb (EWX) validator reward data for snapshot."""
    _logger.info("Fetching EWX reward snapshot for validator %s via %s", valoper, rpc_base_url)
    try:
        # Get validator summary which contains all the reward data
        summary = _ewx_validator_summary(
            valoper,
            rpc_base_url,
            ewx_reward_url=ewx_reward_url,
            ewx_api=ewx_api,
        )
        
        # Extract the relevant fields from the summary
        # outstandingRewards is already a float from _planck_to_ewt_number
        outstanding_rewards = _safe_float(summary.get("outstandingRewards", 0.0))
        
        # Parse tokens from string to float
        tokens_str = summary.get("tokens", "0")
        try:
            tokens = float(tokens_str) if isinstance(tokens_str, str) else _safe_float(tokens_str)
        except (TypeError, ValueError):
            tokens = 0.0

        owned_stake = _safe_float(summary.get("ownedStake", 0.0))
        
        # Get delegator count
        delegator_count = _safe_int(summary.get("delegatorCount", 0))

        # Get commission percentage
        commission_pct = _safe_float(summary.get("commissionPct", 0.0))

        _logger.info(
            "EWX reward snapshot fetched for %s: outstanding_rewards=%s, tokens=%s, delegator_count=%s",
            valoper, outstanding_rewards, tokens, delegator_count,
        )
        return {
            "outstanding_rewards": outstanding_rewards,
            "total_rewards": outstanding_rewards,
            "tokens": tokens,
            "owned_stake": owned_stake,
            "delegator_count": delegator_count,
            "commission_pct": commission_pct,
        }
    except LCDRequestError as exc:
        _logger.warning("EWX request failed for %s: %s", valoper, str(exc))
        return {
            "error": "rpc_error",
            "note": str(exc),
        }
    except Exception as exc:
        _logger.error("Unexpected error fetching EWX rewards for %s: %s", valoper, str(exc), exc_info=True)
        return {
            "error": "internal_error",
            "note": "Failed to fetch EWX rewards",
        }

def _skale_reward_snapshot(
    valoper: str,
    rpc_base_url: str,
    dune_api_key: Optional[str] = None,
    dune_api_base: Optional[str] = None,
) -> Dict[str, Any]:
    """Fetch Skale rewards snapshot: outstanding rewards, total stake, delegator count.
    
    Args:
        valoper: Validator identifier
        rpc_base_url: RPC endpoint URL
        dune_api_key: Pre-fetched Dune API key
        dune_api_base: Pre-fetched Dune API base URL
    """
    try:
        # Resolve validator id (numeric) from provided identifier/address
        validator_id: Optional[int] = None
        try:
            validator_id = int(valoper, 0)
        except (TypeError, ValueError):
            validator_id = None

        if validator_id is None:
            validator_id = _skale_get_validator_id(rpc_base_url, valoper)
            if validator_id == 0:
                return {
                    "error": "validator_not_found",
                    "note": "Validator not found",
                }

        # Validator details for commission
        validator = _skale_get_validator(rpc_base_url, validator_id)
        commission_pct = float(validator.get("feeRate", 0)) / 10
        owned_stake_wei = _skale_get_owned_stake(rpc_base_url, validator_id, validator)

        # Total stake (wei -> SKL)
        total_delegated_wei = _skale_get_delegated_total(rpc_base_url, validator_id)
        tokens = _wei_to_skl_number(total_delegated_wei)

        # Outstanding rewards via Dune helper (unclaimed field)
        dune_rewards = _skale_fetch_rewards_from_dune(valoper, dune_api_key, dune_api_base) or {}
        outstanding_rewards = _safe_float(dune_rewards.get("unclaimed"))
        total_rewards = _safe_float(dune_rewards.get("total"))

        # Delegator count from delegation controller length
        _, _, total_len = _skale_get_delegations(
            rpc_base_url,
            validator_id,
            0,
            1,  # minimal fetch; length carries total delegations
        )

        return {
            "outstanding_rewards": outstanding_rewards,
            "total_rewards": total_rewards,
            "tokens": tokens,
            "owned_stake": _wei_to_skl_number(owned_stake_wei),
            "delegator_count": total_len,
            "commission_pct": commission_pct,
        }
    except LCDRequestError as exc:
        return {
            "error": "rpc_error",
            "note": str(exc),
        }
    except Exception as exc:
        _logger.error("Error fetching Skale rewards snapshot for %s: %s", valoper, exc, exc_info=True)
        return {
            "error": "request_failed",
            "note": str(exc),
        }
def _collect_validator_reward_snapshot(
    protocol_key: str,
    valoper: str,
    rpc_base_url: str,
    dune_api_key: Optional[str] = None,
    dune_api_base: Optional[str] = None,
    network_key: Optional[str] = None,
    owner_address: Optional[str] = None,
    ewx_reward_url: Optional[str] = None,
    ewx_api: Optional[str] = None,
) -> Dict[str, Any]:
    """Protocol-aware dispatcher used by reward snapshot jobs.
    
    Args:
        protocol_key: Protocol identifier
        valoper: Validator identifier
        rpc_base_url: RPC endpoint URL
        dune_api_key: Dune API key for Skale (pre-fetched in main thread)
        dune_api_base: Dune API base URL for Skale (pre-fetched in main thread)
        network_key: Network key for Flow
        owner_address: Owner address for Flow
        ewx_reward_url: EWX reward API base URL (pre-fetched in main thread)
        ewx_api: EWX Subscan API key (pre-fetched in main thread)
    """

    normalized_key = _normalize_protocol_name(protocol_key)
    if normalized_key == "flow":
        return fetch_flow_validator_metrics(
            valoper,
            rest_base_url=rpc_base_url,
            network_hint=network_key,
            owner_address=owner_address,
        )

    if normalized_key == "coreum":
        return _fetch_validator_outstanding_rewards(valoper, rpc_base_url)

    if normalized_key == "cosmos":
        return _fetch_cosmos_reward_snapshot(valoper, rpc_base_url)

    if normalized_key == "opn":
        return _iopn_reward_snapshot(valoper, rpc_base_url)

    if normalized_key == "injective":
        return _fetch_injective_reward_snapshot(valoper, rpc_base_url)

    if normalized_key == "avalanche":
        try:
            validator = _avalanche_fetch_validator(valoper, rpc_base_url)
        except LCDRequestError as exc:
            return {
                "error": "rpc_error",
                "note": str(exc),
            }

        if not validator:
            return {
                "error": "validator_not_found",
                "note": "Validator not found",
            }

        return _build_avalanche_reward_snapshot(validator)

    if normalized_key == "near":
        return _fetch_near_reward_data(valoper, rpc_base_url, delegation_address=owner_address)

    if normalized_key == "subsquid":
        return _subsquid_reward_snapshot(valoper, rpc_base_url)

    if normalized_key == "skale":
        return _skale_reward_snapshot(valoper, rpc_base_url, dune_api_key, dune_api_base)
    if normalized_key == "energyweb":
        return _fetch_ewx_reward_snapshot(
            valoper,
            rpc_base_url,
            ewx_reward_url=ewx_reward_url,
            ewx_api=ewx_api,
        )

    if normalized_key == "solana":
        return _solana_reward_snapshot(valoper, rpc_base_url)

    return {
        "error": "protocol_not_supported",
        "note": f"Rewards snapshots are not enabled for {protocol_key or 'unknown'}",
    }


def _fetch_validator_rewards_with_period(
    valoper: str,
    protocol_record_id: Optional[int],
    period_days: int = 7,
    node_id: Optional[int] = None,
) -> Dict[str, Any]:
    """
    Fetch validator rewards data for a specified period.
    
    Args:
        valoper: Validator operator address
        protocol_record_id: Optional protocol record ID for filtering
        period_days: Number of days (1, 7, or 30)
        node_id: Optional node ID for filtering
    
    Returns:
        Dict with rewards data: {series: [{date, value}], note?}
    """
    _logger.info(
        "Fetching validator rewards for period=%d days valoper=%s node_id=%s",
        period_days,
        valoper,
        node_id,
    )
    
    try:
        # Calculate date range using day boundaries (midnight) to ensure we capture
        # all snapshots from the start of the period day, not just from the exact time
        today = datetime.now().date()
        from_date = datetime.combine(today - timedelta(days=period_days), datetime.min.time())
        domain = [
            ('valoper', '=', valoper),
            ('snapshot_date', '>=', from_date),
        ]
        if node_id:
            domain.append(('node_id', '=', node_id))
        if protocol_record_id:
            domain.append(('protocol_id', '=', protocol_record_id))
        
        # Query snapshots from database
        snapshots = request.env['validator.rewards.snapshot'].sudo().search(
            domain,
            order='snapshot_date asc'
        )
        
        _logger.info("Found %d rewards snapshots for valoper=%s protocol_id=%s from_date=%s",
            len(snapshots),
            valoper,
            protocol_record_id,
            str(from_date),
        )
        
        if len(snapshots) == 0:
            # No snapshots found - return empty with note
            return {
                'series': [],
                'note': 'No historical rewards found'
            }
        
        # Build series from snapshots
        series = []
        
        for snapshot in snapshots:
            date_str = snapshot.snapshot_date.isoformat() if snapshot.snapshot_date else None
            
            entry = {
                'date': date_str,
                'value': snapshot.outstanding_rewards
            }
            if snapshot.epoch:
                entry['epoch'] = snapshot.epoch
            series.append(entry)
        
        return {
            'series': series
        }
    
    except Exception as e:
        _logger.error(
            "Error fetching rewards data for period=%d: %s",
            period_days,
            str(e),
            exc_info=True,
        )
        return {
            'series': [],
            'error': 'internal_error',
            'note': 'Error fetching rewards data'
        }


def _fetch_validator_stake_delegator_with_period(
    valoper: str,
    protocol_record_id: Optional[int],
    period_days: int = 7,
    node_id: Optional[int] = None,
) -> Dict[str, Any]:
    """
    Fetch validator stake (tokens) and delegator count data for a specified period.
    
    Args:
        valoper: Validator operator address
        protocol_record_id: Optional protocol record ID for filtering
        period_days: Number of days (1, 7, or 30)
        node_id: Optional node ID for filtering
    
    Returns:
        Dict with stake/delegator data
    """
    _logger.info(
        "Fetching validator stake and delegator history for period=%d days valoper=%s protocol_id=%s node_id=%s",
        period_days,
        valoper,
        protocol_record_id,
        node_id,
    )

    try:
        # Calculate date range using day boundaries (midnight) to ensure we capture
        # all snapshots from the start of the period day, not just from the exact time
        today = datetime.now().date()
        from_date = datetime.combine(today - timedelta(days=period_days), datetime.min.time())
        domain = [
            ('valoper', '=', valoper),
            ('snapshot_date', '>=', from_date),
        ]
        if node_id:
            domain.append(('node_id', '=', node_id))
        if protocol_record_id:
            domain.append(('protocol_id', '=', protocol_record_id))

        snapshots = request.env['validator.rewards.snapshot'].sudo().search(
            domain,
            order='snapshot_date asc'
        )

        _logger.info(
            "Found %d stake snapshots for valoper=%s protocol_id=%s from_date=%s",
            len(snapshots),
            valoper,
            protocol_record_id,
            str(from_date),
        )

        if not snapshots:
            return {
                'tokens': [],
                'delegatorCount': [],
                'note': 'No historical stake data found'
            }

        tokens_series = []
        delegator_count_series = []
        for snapshot in snapshots:
            date_str = snapshot.snapshot_date.isoformat() if snapshot.snapshot_date else None
            token_entry = {
                'date': date_str,
                'value': snapshot.total_stake,
            }
            delegator_entry = {
                'date': date_str,
                'value': snapshot.delegator_count,
            }
            if snapshot.epoch:
                token_entry['epoch'] = snapshot.epoch
                delegator_entry['epoch'] = snapshot.epoch
            tokens_series.append(token_entry)
            delegator_count_series.append(delegator_entry)

        return {
            'tokens': tokens_series,
            'delegatorCount': delegator_count_series
        }

    except Exception as e:
        _logger.error(
            "Error fetching stake/delegator data for period=%d: %s",
            period_days,
            str(e),
            exc_info=True,
        )
        return {
            'tokens': [],
            'delegatorCount': [],
            'error': 'internal_error',
            'note': 'Error fetching stake/delegator data'
        }
