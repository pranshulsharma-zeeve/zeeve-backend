"""Flow validator metrics helpers.

Provides lightweight utilities to collect Flow validator rewards,
stake, and delegator statistics from the public REST API so that
cron jobs can persist historical snapshots alongside other
protocols.
"""

from __future__ import annotations

import base64
import json
import logging
from decimal import Decimal, InvalidOperation
from typing import Any, Dict, List, Optional

import requests

try:  # pragma: no cover - available during Odoo runtime
    from odoo.http import request  # type: ignore[import-not-found]
except Exception:  # pragma: no cover - fallback for tests/imports
    request = None  # type: ignore

_logger = logging.getLogger(__name__)

FLOW_NETWORKS: Dict[str, Dict[str, str]] = {
    "mainnet": {
        "id_table": "0x8624b52f9ddcd04a",
    },
    "testnet": {
        "id_table": "0x9eca2b38b18b5dfe",
    },
}

DEFAULT_OUTPUT: Dict[str, Any] = {
    "outstanding_rewards": 0.0,
    "tokens": 0.0,
    "delegator_count": 0,
    "delegator_count_all": 0,
    "delegator_reward_total": 0.0,
    "block_height": None,
}

FLOW_TIMEOUT = 30


class FlowValidatorMetricsError(Exception):
    """Raised when Flow metric sampling fails."""

    def __init__(self, message: str, reason: str = "flow_metrics_failed") -> None:
        super().__init__(message)
        self.reason = reason


def _normalize_network(network_hint: Optional[str], base_url: Optional[str]) -> str:
    normalized = (network_hint or "").strip().lower()
    if normalized in ("testnet", "test"):
        return "testnet"
    if normalized in ("mainnet", "main"):
        return "mainnet"
    if base_url and "test" in base_url.lower():
        return "testnet"
    return "mainnet"


def _resolve_rest_endpoint(rest_base_url: Optional[str], network_key: str) -> str:
    base = (rest_base_url or "").strip()
    if base and not base.lower().startswith("http"):
        base = f"https://{base}"
    if not base:
        base = _flow_url_from_protocol_master(network_key)
    if not base:
        raise FlowValidatorMetricsError("Flow REST endpoint is not configured")
    return base.rstrip("/")


def _flow_url_from_protocol_master(network_key: str) -> Optional[str]:
    """Load Flow REST endpoint from protocol.master for the requested network."""
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
        _logger.exception("Failed to resolve Flow URLs from protocol.master")
        return None

    if not protocol:
        return None

    raw = (protocol.web_url_testnet if network_key == "testnet" else protocol.web_url) or ""
    cleaned = raw.strip()
    return cleaned or None


def _normalize_node_id(node_id: str) -> str:
    value = (node_id or "").strip().lower()
    if len(value) not in (64, 128):
        raise FlowValidatorMetricsError("Invalid Flow node ID format", reason="invalid_node_id")
    try:
        int(value, 16)
    except ValueError as exc:
        raise FlowValidatorMetricsError("Invalid Flow node ID format", reason="invalid_node_id") from exc
    return value


def _cadence_node_info(id_table: str) -> str:
    return f"""
    import FlowIDTableStaking from {id_table}
    access(all) fun main(nodeID: String): FlowIDTableStaking.NodeInfo {{
        return FlowIDTableStaking.NodeInfo(nodeID: nodeID)
    }}
    """


def _cadence_node_and_delegator_agg(id_table: str) -> str:
    return f"""
    import FlowIDTableStaking from {id_table}

    access(all) struct Metrics {{
        access(all) let nodeTokensRewarded: UFix64
        access(all) let delegatorRewardTotal: UFix64
        access(all) let delegatorCountAll: Int
        access(all) let delegatorCountActive: Int

        init(
            nodeTokensRewarded: UFix64,
            delegatorRewardTotal: UFix64,
            delegatorCountAll: Int,
            delegatorCountActive: Int
        ) {{
            self.nodeTokensRewarded = nodeTokensRewarded
            self.delegatorRewardTotal = delegatorRewardTotal
            self.delegatorCountAll = delegatorCountAll
            self.delegatorCountActive = delegatorCountActive
        }}
    }}

    access(all) fun main(nodeID: String): Metrics {{
        let n = FlowIDTableStaking.NodeInfo(nodeID: nodeID)

        var total: UFix64 = 0.0
        var active: Int = 0
        let all: Int = n.delegators.length

        for delegatorID in n.delegators {{
            let d = FlowIDTableStaking.DelegatorInfo(nodeID: nodeID, delegatorID: delegatorID)
            if d.tokensCommitted > 0.0 || d.tokensStaked > 0.0 || d.tokensUnstaking > 0.0 {{
                active = active + 1
            }}
            total = total + d.tokensRewarded
        }}

        return Metrics(
            nodeTokensRewarded: n.tokensRewarded,
            delegatorRewardTotal: total,
            delegatorCountAll: all,
            delegatorCountActive: active
        )
    }}
    """


def _cadence_total_with_delegators(id_table: str) -> str:
    return f"""
    import FlowIDTableStaking from {id_table}
    access(all) fun main(nodeID: String): UFix64 {{
        return FlowIDTableStaking.NodeInfo(nodeID: nodeID).totalCommittedWithDelegators()
    }}
    """


def _cadence_total_without_delegators(id_table: str) -> str:
    return f"""
    import FlowIDTableStaking from {id_table}
    access(all) fun main(nodeID: String): UFix64 {{
        return FlowIDTableStaking.NodeInfo(nodeID: nodeID).totalCommittedWithoutDelegators()
    }}
    """


def _build_script_payload(cadence: str, args: Optional[List[Dict[str, Any]]] = None) -> Dict[str, Any]:
    script_b64 = base64.b64encode(cadence.encode("utf-8")).decode("ascii")
    encoded_args = []
    for arg in args or []:
        encoded = base64.b64encode(json.dumps(arg).encode("utf-8")).decode("ascii")
        encoded_args.append(encoded)
    return {"script": script_b64, "arguments": encoded_args}


def _decode_cadence_json(body: Any) -> Any:
    if isinstance(body, dict):
        if body.get("type") or (isinstance(body.get("value"), dict) and body["value"].get("type")):
            return body
        candidate = body.get("value") or body.get("result") or body.get("data")
    elif isinstance(body, str):
        candidate = body
    else:
        candidate = None

    if not candidate:
        raise FlowValidatorMetricsError("Unrecognized Flow scripts response", reason="flow_response_invalid")

    try:
        decoded = base64.b64decode(candidate)
    except (ValueError, TypeError) as exc:
        raise FlowValidatorMetricsError("Invalid Flow scripts payload", reason="flow_response_invalid") from exc

    try:
        text = decoded.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise FlowValidatorMetricsError("Invalid Flow scripts encoding", reason="flow_response_invalid") from exc

    stripped = text.strip()
    if not stripped:
        return None

    try:
        return json.loads(stripped)
    except json.JSONDecodeError:
        if stripped.startswith('"') and stripped.endswith('"'):
            return {"type": "String", "value": stripped[1:-1]}
        return {"type": "String", "value": stripped}


def _exec_script(rest_base: str, cadence: str, args: Optional[List[Dict[str, Any]]] = None) -> Any:
    url = f"{rest_base}/v1/scripts?block_height=sealed"
    payload = _build_script_payload(cadence, args)

    def _post(body: Dict[str, Any]) -> Any:
        response = requests.post(url, json=body, timeout=FLOW_TIMEOUT)
        try:
            response.raise_for_status()
        except requests.HTTPError as exc:
            snippet = response.text[:200]
            raise FlowValidatorMetricsError(
                f"Flow scripts request failed ({response.status_code}): {snippet}",
                reason="flow_rpc_error",
            ) from exc
        try:
            return response.json()
        except ValueError:
            return response.text

    try:
        raw = _post(payload)
    except FlowValidatorMetricsError as exc:
        if "access(all)" not in cadence:
            raise
        fallback = cadence.replace("access(all)", "pub")
        raw = _post(_build_script_payload(fallback, args))

    return _decode_cadence_json(raw)


def _as_decimal(value: Any) -> Decimal:
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
            return Decimal(value)
        except (InvalidOperation, ValueError):
            return Decimal("0")
    if isinstance(value, dict) and "value" in value:
        return _as_decimal(value.get("value"))
    return Decimal("0")


def _as_int(value: Any) -> int:
    try:
        dec = _as_decimal(value)
        return int(dec)
    except (ValueError, InvalidOperation):
        return 0


def _decimal_to_float(value: Decimal) -> float:
    try:
        return float(value)
    except (InvalidOperation, TypeError, ValueError):
        return 0.0


def _struct_get_field(struct_obj: Any, field_name: str) -> Any:
    if not isinstance(struct_obj, dict):
        return None
    container = struct_obj.get("value") or {}
    fields = container.get("fields") if isinstance(container, dict) else None
    if not isinstance(fields, list):
        fields = struct_obj.get("fields") if isinstance(struct_obj.get("fields"), list) else []
    for field in fields or []:
        if field.get("name") == field_name:
            return field.get("value")
    return None


def _cadence_array_length(value: Any) -> int:
    if isinstance(value, list):
        return len(value)
    if isinstance(value, dict):
        arr = value.get("value")
        if isinstance(arr, list):
            return len(arr)
    return 0


def _fetch_block_height(rest_base: str) -> Optional[str]:
    try:
        url = f"{rest_base}/v1/blocks?height=sealed&limit=1"
        response = requests.get(url, timeout=FLOW_TIMEOUT)
        response.raise_for_status()
        body = response.json()
    except (requests.RequestException, ValueError):
        return None

    block = None
    if isinstance(body, dict):
        blocks = body.get("blocks")
        if isinstance(blocks, list) and blocks:
            block = blocks[0]
    elif isinstance(body, list) and body:
        block = body[0]

    if isinstance(block, dict):
        header = block.get("header") or {}
        height = header.get("height") or block.get("height")
        return str(height) if height is not None else None
    return None


def _collect_totals(rest_base: str, id_table: str, node_id: str) -> Dict[str, Decimal]:
    totals_with = _as_decimal(
        _exec_script(
            rest_base,
            _cadence_total_with_delegators(id_table),
            [{"type": "String", "value": node_id}],
        )
    )
    totals_without = _as_decimal(
        _exec_script(
            rest_base,
            _cadence_total_without_delegators(id_table),
            [{"type": "String", "value": node_id}],
        )
    )
    return {
        "total": totals_with,
        "owned": totals_without,
        "delegator": max(totals_with - totals_without, Decimal("0")),
    }


def _fetch_metrics_bundle(
    rest_base: str,
    id_table: str,
    node_id: str,
) -> Dict[str, Any]:
    metrics_struct = None
    try:
        metrics_struct = _exec_script(
            rest_base,
            _cadence_node_and_delegator_agg(id_table),
            [{"type": "String", "value": node_id}],
        )
    except FlowValidatorMetricsError as exc:
        _logger.warning("Flow aggregated metrics failed: %s", exc)

    outstanding = None
    delegator_reward_total = None
    delegator_count_all = None
    delegator_count_active = None

    if metrics_struct:
        outstanding = _as_decimal(_struct_get_field(metrics_struct, "nodeTokensRewarded"))
        delegator_reward_total = _as_decimal(_struct_get_field(metrics_struct, "delegatorRewardTotal"))
        delegator_count_all = _as_int(_struct_get_field(metrics_struct, "delegatorCountAll"))
        delegator_count_active = _as_int(_struct_get_field(metrics_struct, "delegatorCountActive"))

    node_info = None
    if outstanding is None or delegator_count_active is None:
        node_info = _exec_script(
            rest_base,
            _cadence_node_info(id_table),
            [{"type": "String", "value": node_id}],
        )
        if outstanding is None:
            outstanding = _as_decimal(_struct_get_field(node_info, "tokensRewarded"))
        if delegator_count_active is None:
            delegators_field = _struct_get_field(node_info, "delegators")
            delegator_count_active = _cadence_array_length(delegators_field)
        if delegator_count_all is None:
            delegators_field = _struct_get_field(node_info, "delegators")
            delegator_count_all = _cadence_array_length(delegators_field)

    totals = _collect_totals(rest_base, id_table, node_id)
    block_height = _fetch_block_height(rest_base)

    return {
        "outstanding": outstanding or Decimal("0"),
        "delegator_reward_total": delegator_reward_total or Decimal("0"),
        "delegator_count_all": delegator_count_all or 0,
        "delegator_count_active": delegator_count_active or 0,
        "block_height": block_height,
        "total_stake": totals["total"],
        "owned_stake": totals["owned"],
    }


def fetch_flow_validator_metrics(
    node_id: str,
    rest_base_url: Optional[str] = None,
    network_hint: Optional[str] = None,
    owner_address: Optional[str] = None,
) -> Dict[str, Any]:
    """Return Flow validator stake/reward metrics suitable for snapshots."""
    del owner_address  # owner reserved for future wallet lookups

    if not node_id:
        return {"error": "node_id_missing", "note": "Flow node identifier is required"}

    try:
        normalized_node_id = _normalize_node_id(node_id)
        network_key = _normalize_network(network_hint, rest_base_url)
        config = FLOW_NETWORKS.get(network_key)
        if not config:
            raise FlowValidatorMetricsError("Unsupported Flow network", reason="unsupported_network")
        rest_base = _resolve_rest_endpoint(rest_base_url, network_key)
        bundle = _fetch_metrics_bundle(rest_base, config["id_table"], normalized_node_id)
    except FlowValidatorMetricsError as exc:
        return {"error": exc.reason, "note": str(exc)}
    except requests.RequestException as exc:
        _logger.warning("Flow metrics request error: %s", exc)
        return {"error": "flow_rpc_error", "note": str(exc)}
    except Exception as exc:
        _logger.exception("Unexpected Flow metrics failure for node_id=%s", node_id)
        return {"error": "flow_metrics_failed", "note": str(exc)}

    outstanding_rewards = _decimal_to_float(bundle["outstanding"])
    delegator_reward_total = _decimal_to_float(bundle["delegator_reward_total"])
    total_rewards = outstanding_rewards + delegator_reward_total
    
    return {
        "outstanding_rewards": outstanding_rewards,
        "total_rewards": total_rewards,
        "tokens": _decimal_to_float(bundle["total_stake"]),
        "owned_stake": _decimal_to_float(bundle["owned_stake"]),
        "delegator_count":int(bundle["delegator_count_all"] or 0) or int(bundle["delegator_count_active"] or 0),
        "delegator_count_all": int(bundle["delegator_count_all"] or 0),
        "delegator_reward_total": delegator_reward_total,
        "block_height": bundle["block_height"],
    }
