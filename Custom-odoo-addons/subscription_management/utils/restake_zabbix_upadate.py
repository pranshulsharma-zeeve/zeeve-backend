#!/usr/bin/env python3
"""Invoke Zabbix API updates for Restake automation.

This script mirrors the legacy shell logic provided for updating Zabbix macros
and item status. It expects the following environment variables:

- ZABBIX_URL: Base URL for the Zabbix instance (without the trailing /api_jsonrpc.php)
- ZABBIX_BEARER_TOKEN: Bearer token used for authentication
- ZABBIX_HOSTNAME: Host name to target
- ZABBIX_MACRO_NAME: Macro to update (defaults to {$RESTART_DELAY})
- ZABBIX_ITEM_NAME: Item name (wildcard search) to toggle (defaults to
  "Docker Container Execution Script*")
- ZABBIX_MACRO_VALUE_TEMPLATE: Template for the macro value. The placeholders
  {interval}, {minimum_reward}, and {minimum_reward_micro} will be replaced using
  the command arguments/environment variables provided by the helper.
- RESTAKE_MINIMUM_REWARD_MICRO: Minimum reward in micro units (optional)

Usage:
    restake_zabbix_update.py <interval_hours> <minimum_reward>
"""

from __future__ import annotations

import os
import sys
from typing import Any, Dict

import requests


class ZabbixError(RuntimeError):
    """Raised when the Zabbix API responds with an error."""


def _get_env(name: str, required: bool = True, default: str | None = None) -> str:
    value = os.environ.get(name, default)
    if required and not value:
        raise ZabbixError(f"Environment variable '{name}' is required.")
    return value or ""


def _call_zabbix(endpoint: str, token: str, method: str, params: Dict[str, Any], request_id: int) -> Any:
    payload = {
        "jsonrpc": "2.0",
        "method": method,
        "params": params,
        "id": request_id,
    }
    headers = {
        "Content-Type": "application/json-rpc",
        "Authorization": f"Bearer {token}",
    }
    response = requests.post(endpoint, json=payload, headers=headers, timeout=30)
    response.raise_for_status()
    data = response.json()
    if "error" in data:
        error = data["error"]
        message = error.get("data") or error.get("message") or str(error)
        raise ZabbixError(message)
    return data.get("result")


def main(argv: list[str]) -> int:
    if len(argv) < 3:
        print("Usage: restake_zabbix_update.py <interval_hours> <minimum_reward>", file=sys.stderr)
        return 1

    interval_hours = argv[1]
    minimum_reward = argv[2]
    host_id = argv[3]
    minimum_reward_micro = os.environ.get("RESTAKE_MINIMUM_REWARD_MICRO", "")

    base_url = _get_env("ZABBIX_URL").rstrip("/")
    endpoint = f"{base_url}/api_jsonrpc.php"
    token = _get_env("ZABBIX_BEARER_TOKEN")

    macro_name = _get_env("ZABBIX_MACRO_NAME", required=False, default="{$RESTART_DELAY}")
    item_name = _get_env("ZABBIX_ITEM_NAME", required=False, default="Docker Container Execution Script*")
    macro_template = _get_env("ZABBIX_MACRO_VALUE_TEMPLATE", required=False, default="{interval}h")

    macro_value = macro_template
    macro_value = macro_value.replace("{interval}", interval_hours)
    macro_value = macro_value.replace("{minimum_reward}", minimum_reward)
    macro_value = macro_value.replace("{minimum_reward_micro}", minimum_reward_micro)

    try:
        # call host.update to set the macro
        _call_zabbix(
            endpoint,
            token,
            "host.update",
            {
                "hostid": host_id,
                "macros": [{"macro": macro_name, "value": macro_value}],
            },
            3,
        )
        #TODO call only for disable restake when disable

        # item_result = _call_zabbix(
        #     endpoint,
        #     token,
        #     "item.get",
        #     {
        #         "hostids": [host_id],
        #         "search": {"name": item_name},
        #         "searchWildcardsEnabled": True,
        #         "output": ["itemid", "name", "status"],
        #         "limit": 1,
        #     },
        #     5,
        # )

        # if not item_result:
        #     raise ZabbixError(f"No item found matching pattern: {item_name}")

        # item_id = item_result[0]["itemid"]
        # _call_zabbix(
        #     endpoint,
        #     token,
        #     "item.update",
        #     {
        #         "itemid": item_id,
        #         "status": 1,
        #     },
        #     6,
        # )
    except ZabbixError as exc:
        print(str(exc), file=sys.stderr)
        return 1
    except requests.RequestException as exc:
        print(f"Failed communicating with Zabbix: {exc}", file=sys.stderr)
        return 1

    print("Zabbix automation update completed successfully.")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))