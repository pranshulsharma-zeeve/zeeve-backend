import base64
import re
import uuid
from datetime import datetime, timezone
from typing import Any, Dict, Optional, Tuple

import requests
from odoo import tools
from odoo.http import request
from odoo.exceptions import UserError

import logging

from .restake_helper import _get_config_param

_logger = logging.getLogger(__name__)

_URL_REGEX = re.compile(r"^https?:\/\/[A-Za-z0-9:/.\-?=]+[A-Za-z0-9]$")
_ENV_REGEX = re.compile(r"^[A-Za-z-]+$")
_INSTANCE_ID_REGEX = re.compile(r"^[A-Za-z0-9-]+$")
_ISO_DATETIME_REGEX = re.compile(r"^(\d{4}-\d{2}-\d{2})T(\d{2}:\d{2}:\d{2})(\.\d{1,6})?Z$")
_ANSIBLE_TIMEOUT = 180
_DEFAULT_ANSIBLE_CONNECT_TIMEOUT = 10
_ANSIBLE_URL_KEY = "etherlink.ansible.url"
_ANSIBLE_LEGACY_URL_KEY = "etherlink.ansible.old_url"
_LOKI_USERNAME_KEY = "etherlink.loki.username"
_LOKI_PASSWORD_KEY = "etherlink.loki.password"
_LOKI_ALLOWED_CONTAINERS_KEY = "etherlink.loki.allowed_containers"
_LOKI_LEGACY_NODE_IDS_KEY = "etherlink.loki.legacy_node_ids"
_LOKI_LEGACY_URL_KEY = "etherlink.loki.old_url"


def _is_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _is_non_empty_string(value: Any) -> bool:
    return isinstance(value, str) and bool(value.strip())

def _validate_int_range(
    value: Any,
    required_message: str,
    range_message: str,
    *,
    min_value: Optional[int] = None,
    max_value: Optional[int] = None,
    field_name: Optional[str] = None,
    coerce_string: bool = False,
) -> Tuple[bool, Optional[str]]:
    def _format_message(message: str) -> str:
        if not field_name:
            return message
        return f"{field_name}: {message}"

    if value is None:
        return False, _format_message(required_message)
    if coerce_string and isinstance(value, str):
        try:
            value = int(value.strip())
        except (TypeError, ValueError):
            return False, _format_message(range_message)
    if not _is_int(value):
        return False, _format_message(range_message)
    if min_value is not None and value < min_value:
        return False, _format_message(range_message)
    if max_value is not None and value > max_value:
        return False, _format_message(range_message)
    return True, None

def _get_positive_int_param(key: str, default: int) -> int:
    icp = request.env["ir.config_parameter"].sudo()
    raw_value = icp.get_param(key)
    if raw_value in (None, ""):
        return default
    try:
        parsed_value = int(str(raw_value).strip())
    except (TypeError, ValueError):
        _logger.warning("Invalid Etherlink config param %s=%r, falling back to %s", key, raw_value, default)
        return default
    if parsed_value <= 0:
        _logger.warning("Non-positive Etherlink config param %s=%r, falling back to %s", key, raw_value, default)
        return default
    return parsed_value


def get_ansible_read_timeout() -> int:
    return _get_positive_int_param("etherlink.ansible.timeout_seconds", _ANSIBLE_TIMEOUT)


def get_ansible_connect_timeout() -> int:
    return _get_positive_int_param(
        "etherlink.ansible.connect_timeout_seconds",
        _DEFAULT_ANSIBLE_CONNECT_TIMEOUT,
    )



def normalize_uuid(value: Any) -> Optional[str]:
    """Return canonical UUID string for value or None if invalid."""
    try:
        return str(uuid.UUID(str(value)))
    except (ValueError, TypeError, AttributeError):
        return None


def parse_iso8601_utc(value: Any) -> Optional[datetime]:
    """Parse ISO8601 timestamps that end with Z."""
    if not isinstance(value, str):
        return None
    trimmed = value.strip()
    if not trimmed or not _ISO_DATETIME_REGEX.match(trimmed):
        return None
    try:
        parsed = datetime.fromisoformat(trimmed.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)
def get_ansible_base_url(network_uuid: Optional[str] = None) -> str:
    icp = request.env["ir.config_parameter"].sudo()
    ansible_key = _ANSIBLE_URL_KEY
    error_message = "Etherlink ansible URL is not configured"
    if network_uuid and _use_legacy_loki_flow(network_uuid):
        ansible_key = _ANSIBLE_LEGACY_URL_KEY
        error_message = "Legacy Etherlink ansible URL is not configured"
    ansible_base = icp.get_param(ansible_key)
    if not ansible_base:
        raise ValueError(error_message)
    return ansible_base.rstrip("/")


def _validate_url_field(
    value: Any,
    required_message: str,
    type_message: str,
    length_message: str,
) -> Tuple[bool, Optional[str]]:
    if value is None or (isinstance(value, str) and not value.strip()):
        return False, required_message
    if not isinstance(value, str):
        return False, type_message
    cleaned = value.strip()
    if len(cleaned) > 150:
        return False, length_message
    if not _URL_REGEX.match(cleaned):
        return False, (
            "URL must start with http:// or https://, may contain : / . - ? = and must end with a letter or digit"
        )
    return True, None


def validate_updation_log_payload(payload: Dict[str, Any]) -> Tuple[bool, Optional[str]]:
    """Validate payload for add-updation-log route."""
    if not isinstance(payload, dict):
        return False, "Invalid JSON payload"

    updated_at = payload.get("updatedAt")
    if not isinstance(updated_at, str) or not updated_at.strip():
        return False, "updatedAt is required"
    if not _ISO_DATETIME_REGEX.match(updated_at.strip()):
        return False, "updatedAt must be in the format YYYY-MM-DDTHH:mm:ss(.ffffff)Z"

    if not _is_non_empty_string(payload.get("protocolName")):
        return False, "protocolName is required"

    if not normalize_uuid(payload.get("nodeId")):
        return False, "nodeId must be a valid UUID"

    if "updatedConfig" not in payload or not payload.get("updatedConfig"):
        return False, "updatedConfig is required"

    if not _is_non_empty_string(payload.get("status")):
        return False, "status is required"

    return True, None


def validate_node_config_payload(payload: Dict[str, Any]) -> Tuple[bool, Optional[str]]:
    if not isinstance(payload, dict):
        return False, "Invalid JSON payload"

    client_domain = payload.get("client_domain")
    if client_domain is None or (isinstance(client_domain, str) and not client_domain.strip()):
        return False, "client domain is required"
    if not isinstance(client_domain, str):
        return False, "client domain must be a string"

    config = payload.get("config")
    if not isinstance(config, dict):
        return False, "config is required"

    opentelemetry = config.get("opentelemetry")
    if not isinstance(opentelemetry, dict):
        return False, "config.opentelemetry is required"

    for field_name in ("enable", "debug", "trace_host_functions"):
        value = opentelemetry.get(field_name)
        if not isinstance(value, bool):
            return False, f"Invalid value for config.opentelemetry.{field_name}"

    environment_provided = "environment" in opentelemetry
    if not environment_provided:
        return False, "environment is required"
    environment = opentelemetry.get("environment")
    if environment is not None:
        if not isinstance(environment, str):
            return False, "environment must be a string"
        cleaned_env = environment.strip()
        if len(cleaned_env) > 30:
            return False, "Maximum 30 characters allowed"
        if not _ENV_REGEX.match(cleaned_env):
            return False, "Only alphabets and hyphens are allowed"

    for field_key, required_msg, type_msg in (
        ("url_traces", "url_traces is required", "url_traces must be a string"),
        ("url_logs", "url_logs is required", "url_logs must be a string"),
    ):
        ok, err = _validate_url_field(
            opentelemetry.get(field_key),
            required_msg,
            type_msg,
            "Maximum 150 characters allowed",
        )
        if not ok:
            return False, err

    headers = opentelemetry.get("headers")
    if headers is None:
        return False, "headers is required"
    if not isinstance(headers, list):
        return False, "headers must be an array"

    for field_key, required_msg, range_msg, min_val, max_val in (
        ("batch_traces", "batch_traces is required", "Must be between 400 and 10000 inclusive", 400, 10000),
        ("batch_logs", "batch_logs is required", "Must be between 400 and 10000 inclusive", 400, 10000),
        (
            "batch_timeout_ms",
            "batch_timeout_ms is required",
            "Must be between 500 and 10000 inclusive",
            500,
            10000,
        ),
    ):
        ok, err = _validate_int_range(
            opentelemetry.get(field_key),
            required_msg,
            range_msg,
            min_value=min_val,
            max_value=max_val,
            field_name=f"config.opentelemetry.{field_key}",
        )
        if not ok:
            return False, err

    gc_telemetry = opentelemetry.get("gc_telemetry")
    if not isinstance(gc_telemetry, dict):
        return False, "config.opentelemetry.gc_telemetry is required"
    if not isinstance(gc_telemetry.get("enable"), bool):
        return False, "Invalid value for config.opentelemetry.gc_telemetry.enable"
    ok, err = _validate_int_range(
        gc_telemetry.get("min_duration_ms"),
        "min_duration_ms is required",
        "Must be greater than or equal to 1",
        min_value=1,
        field_name="config.opentelemetry.gc_telemetry.min_duration_ms",
    )
    if not ok:
        return False, err

    instance_id = opentelemetry.get("instance_id")
    if instance_id is None or (isinstance(instance_id, str) and not instance_id.strip()):
        return False, "instance_id is required"
    if not isinstance(instance_id, str):
        return False, "Must be a string"
    cleaned_instance_id = instance_id.strip()
    if len(cleaned_instance_id) > 30:
        return False, "Maximum 30 characters allowed"
    if not _INSTANCE_ID_REGEX.match(cleaned_instance_id):
        return False, "Only letters, numbers, and hyphens are allowed"

    log_filter = config.get("log_filter")
    if not isinstance(log_filter, dict):
        return False, "config.log_filter is required"
    for field_key, required_msg, range_msg, min_val, max_val in (
        ("max_nb_blocks", "max_nb_blocks is required", "Must be between 100 and 3000000 inclusive", 100, 3000000),
        ("max_nb_logs", "max_nb_logs is required", "Must be between 1000 and 3000000 inclusive", 1000, 3000000),
        ("chunk_size", "chunk_size is required", "Must be between 1000 and 3000 inclusive", 1000, 3000),
    ):
        ok, err = _validate_int_range(
            log_filter.get(field_key),
            required_msg,
            range_msg,
            min_value=min_val,
            max_value=max_val,
            field_name=f"config.log_filter.{field_key}",
        )
        if not ok:
            return False, err

    observer_cfg = config.get("observer")
    if not isinstance(observer_cfg, dict):
        return False, "config.observer is required"
    evm_endpoint = observer_cfg.get("evm_node_endpoint")
    if evm_endpoint is None or (isinstance(evm_endpoint, str) and not evm_endpoint.strip()):
        return False, "evm node endpoint is required"
    if not isinstance(evm_endpoint, str):
        return False, "evm node endpoint is required"
    if not isinstance(observer_cfg.get("rollup_node_tracking"), bool):
        return False, "Invalid value for config.observer.rollup_node_tracking"

    experimental_features = config.get("experimental_features")
    if not isinstance(experimental_features, dict):
        return False, "config.experimental_features is required"
    if not isinstance(experimental_features.get("enable_websocket"), bool):
        return False, "Invalid value for config.experimental_features.enable_websocket"

    tx_pool = config.get("tx_pool")
    if not isinstance(tx_pool, dict):
        return False, "config.tx_pool is required"
    for field_key, required_msg, range_msg, min_val, max_val in (
        ("max_size", "max_size is required", "Must be between 1000 and 100000 inclusive", 1000, 100000),
        ("max_lifespan", "max_lifespan is required", "Must be between 4 and 100 inclusive", 4, 100),
        ("tx_per_addr_limit", "tx_per_addr_limit is required", "Must be between 16 and 10000 inclusive", 16, 10000),
    ):
        ok, err = _validate_int_range(
            tx_pool.get(field_key),
            required_msg,
            range_msg,
            min_value=min_val,
            max_value=max_val,
            field_name=f"config.tx_pool.{field_key}",
            coerce_string=True,
        )
        if not ok:
            return False, err

    if not isinstance(config.get("finalized_view"), bool):
        return False, "Invalid value for config.finalized_view"

    kernel_execution = config.get("kernel_execution")
    if not isinstance(kernel_execution, dict):
        return False, "config.kernel_execution is required"
    preimages_endpoint = kernel_execution.get("preimages_endpoint")
    if preimages_endpoint is None or (isinstance(preimages_endpoint, str) and not preimages_endpoint.strip()):
        return False, "preimages endpoint is required"
    if not isinstance(preimages_endpoint, str):
        return False, "preimages endpoint is required"

    return True, None



def _loki_base_url() -> str:
    """Return configured Loki base URL or raise ValueError."""
    icp = request.env["ir.config_parameter"].sudo()
    loki_base = icp.get_param("etherlink.loki.url")
    if not loki_base:
        raise ValueError("Loki base URL is not configured")
    return loki_base.rstrip("/")


def _get_loki_config_param(key: str, required: bool = True, default: Optional[str] = None) -> str:
    try:
        return _get_config_param(request.env, key, required=required, default=default)
    except UserError as exc:
        raise ValueError(tools.ustr(exc)) from exc


def _build_loki_headers(network_uuid: str) -> Dict[str, str]:
    username = _get_loki_config_param(_LOKI_USERNAME_KEY).strip()
    password = _get_loki_config_param(_LOKI_PASSWORD_KEY).strip()
    auth_value = base64.b64encode(f"{username}:{password}".encode("utf-8")).decode("ascii")
    return {
        "Authorization": f"Basic {auth_value}",
    }


def _get_optional_config_param(key: str) -> Optional[str]:
    icp = request.env["ir.config_parameter"].sudo()
    value = icp.get_param(key)
    if value and str(value).strip():
        return str(value).strip()
    return None


def _legacy_loki_base_url() -> str:
    loki_base = _get_optional_config_param(_LOKI_LEGACY_URL_KEY)
    if not loki_base:
        raise ValueError("Legacy Loki base URL is not configured")
    return loki_base.rstrip("/")


def _build_legacy_loki_headers(network_uuid: str) -> Dict[str, str]:
    return {
        "X-Scope-OrgID": network_uuid,
    }


def _get_legacy_loki_node_ids() -> set[str]:
    raw_value = _get_optional_config_param(_LOKI_LEGACY_NODE_IDS_KEY) or ""
    legacy_node_ids: set[str] = set()
    for raw_node_id in re.split(r"[\s,]+", raw_value):
        normalized_node_id = normalize_uuid(raw_node_id)
        if normalized_node_id:
            legacy_node_ids.add(normalized_node_id)
    return legacy_node_ids


def _use_legacy_loki_flow(network_uuid: str) -> bool:
    return network_uuid in _get_legacy_loki_node_ids()


def _get_allowed_loki_containers() -> set[str]:
    raw_value = _get_loki_config_param(_LOKI_ALLOWED_CONTAINERS_KEY, required=False, default="")
    return {
        container_name.strip()
        for container_name in raw_value.split(",")
        if container_name and container_name.strip()
    }


def _extract_allowed_loki_service_name(pod_name: str, allowed_services: set[str]) -> Optional[str]:
    pod_name = (pod_name or "").strip()
    if not pod_name:
        return None

    for service_name in sorted(allowed_services, key=len, reverse=True):
        marker = f"-{service_name}-"
        short_name_pattern = rf"^{re.escape(service_name)}-\d+$"

        if re.match(short_name_pattern, pod_name):
            return pod_name

        marker_index = pod_name.find(marker)
        if marker_index >= 0:
            extracted_name = pod_name[marker_index + 1 :]
            if re.match(short_name_pattern, extracted_name):
                return extracted_name

        if pod_name == service_name:
            return service_name

    return None


def _split_loki_service_name(service_name: Optional[str]) -> Tuple[Optional[str], Optional[str]]:
    service_name = (service_name or "").strip()
    if not service_name:
        return None, None

    match = re.match(r"^(?P<container>[A-Za-z0-9-]+)-(?P<pod_index>\d+)$", service_name)
    if not match:
        return service_name, None

    return match.group("container"), match.group("pod_index")


def _parse_timestamp_param(value: Optional[str], label: str) -> Tuple[Optional[int], Optional[str]]:
    """Parse optional timestamp parameter ensuring it is an integer."""
    if value is None:
        return None, None
    string_value = str(value).strip()
    if not string_value or string_value.lower() == "undefined":
        return None, None
    try:
        parsed = int(string_value)
    except (TypeError, ValueError):
        return None, f"Incorrect {label}"
    return parsed, None


def _query_loki_logs(
    network_uuid: str,
    agent_id: str,
    service_name: Optional[str],
    start_time: Optional[int],
    end_time: Optional[int],
) -> list:
    use_legacy_flow = _use_legacy_loki_flow(network_uuid)
    if use_legacy_flow:
        loki_url = f"{_legacy_loki_base_url()}/loki/api/v1/query_range"
        filter_parts = [f'hostname="{agent_id}"']
        if service_name:
            filter_parts.append(f'unit="{service_name}"')
        headers = _build_legacy_loki_headers(network_uuid)
        _logger.info("Etherlink Loki logs request using legacy flow for node %s", network_uuid)
    else:
        loki_url = f"{_loki_base_url()}/loki/api/v1/query_range"
        container_name, pod_index = _split_loki_service_name(service_name)
        filter_parts = [
            f'agent_id="{agent_id}"',
            f'network_id="{network_uuid}"',
        ]
        if container_name:
            filter_parts.append(f'container="{container_name}"')
        if pod_index is not None:
            filter_parts.append(f'pod_index="{pod_index}"')
        headers = _build_loki_headers(network_uuid)
    loki_query = "{" + ",".join(filter_parts) + "}"
    params: Dict[str, object] = {"query": loki_query, "limit": 3000}
    if start_time is not None:
        params["start"] = start_time
    if end_time is not None:
        params["end"] = end_time

    response = requests.get(
        loki_url,
        headers=headers,
        params=params,
        timeout=_ANSIBLE_TIMEOUT,
    )
    response.raise_for_status()
    payload = response.json()
    result = payload.get("data", {}).get("result", []) or []
    return {
        "logs": result,
    }


def _query_loki_services(
    network_uuid: str,
    agent_id: Optional[str] = None,
) -> list:
    use_legacy_flow = _use_legacy_loki_flow(network_uuid)
    if use_legacy_flow:
        loki_url = f"{_legacy_loki_base_url()}/loki/api/v1/label/unit/values"
        params: Dict[str, object] = {
            "query": f'{{hostname="{(agent_id or "").strip()}"}}',
        }
        headers = _build_legacy_loki_headers(network_uuid)
        _logger.info("Etherlink Loki services request using legacy flow for node %s", network_uuid)
    else:
        loki_url = f"{_loki_base_url()}/loki/api/v1/label/pod/values"
        params = {
            "query": f'{{network_id="{network_uuid}"}}',
        }
        headers = _build_loki_headers(network_uuid)

    _logger.info(
        "Etherlink Loki services request | url=%s | params=%s",
        loki_url,
        params,
    )

    response = requests.get(
        loki_url,
        headers=headers,
        params=params or None,
        timeout=_ANSIBLE_TIMEOUT,
    )
    response.raise_for_status()
    payload = response.json()
    _logger.info(
        "Etherlink Loki services response | status=%s | payload_preview=%s",
        response.status_code,
        tools.ustr(payload)[:4000],
    )
    services = payload.get("data", []) or []
    if use_legacy_flow:
        return services
    allowed_services = _get_allowed_loki_containers()
    _logger.info(
        "Etherlink Loki services parsed | raw_services=%s | allowed_services=%s",
        services,
        allowed_services,
    )
    if not allowed_services:
        return services
    filtered_services = []
    for service_name in services:
        short_name = _extract_allowed_loki_service_name(service_name, allowed_services)
        if short_name:
            filtered_services.append(short_name)
    _logger.info(
        "Etherlink Loki services filtered | filtered_services=%s",
        filtered_services,
    )
    return filtered_services
