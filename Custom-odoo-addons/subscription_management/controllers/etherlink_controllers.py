# -*- coding: utf-8 -*-
"""
Etherlink Subscription Management API Endpoints
Provides REST API for subscription operations
"""

import json
import logging

import requests
from odoo import fields, http
from odoo.http import request

from ...auth_module.utils import oauth as oauth_utils
from ..utils import etherlink_utils

_logger = logging.getLogger(__name__)




class EtherlinkController(http.Controller):
    @staticmethod
    def _extract_ansible_error(response):
        """Return a readable upstream error message when available."""
        try:
            payload = response.json()
        except ValueError:
            payload = None

        if isinstance(payload, dict):
            for key in ("message", "error", "detail"):
                value = payload.get(key)
                if isinstance(value, str) and value.strip():
                    return value.strip()

        raw_text = (response.text or "").strip()
        if raw_text:
            return raw_text[:200]
        return "Unknown ansible server error"

    @staticmethod
    def _get_subscription_from_node(node_identifier: str):
        node_model = request.env["subscription.node"].sudo()
        subscription_model = request.env["subscription.subscription"].sudo()
        node = node_model.search([("node_identifier", "=", node_identifier)], limit=1)
        if node:
            return node.subscription_id, node
        subscription = subscription_model.search([("subscription_uuid", "=", node_identifier)], limit=1)
        return subscription, node_model.browse()

    @staticmethod
    def _parse_validator_info(node):
        validator_info = {}
        raw_validator_info = (node.validator_info or "").strip()
        if not raw_validator_info:
            return validator_info
        try:
            parsed = json.loads(raw_validator_info)
        except (TypeError, ValueError):
            _logger.warning("Invalid validator_info JSON for node %s", node.id)
            return validator_info
        if isinstance(parsed, dict):
            return parsed
        _logger.warning("validator_info is not a JSON object for node %s", node.id)
        return validator_info

    @http.route("/api/v1/node-config/update", type="http", auth="none", methods=["OPTIONS", "POST"], csrf=False)
    def update_node_config(self, **kwargs):
        """update-config API for Etherlink nodes."""
        try:
            if request.httprequest.method == "OPTIONS":
                return oauth_utils.preflight_response(["POST"])

            user, resp = oauth_utils.require_user()
            if not user:
                return resp

            payload = request.httprequest.get_json(force=True, silent=True) or {}
            if not isinstance(payload, dict):
                return oauth_utils._json_response(False, error="Invalid JSON payload", status=400)

            network_id = kwargs.get("nodeId")

            if not network_id:
                return oauth_utils._json_response(False, error="network Id is required", status=400)

            network_uuid = etherlink_utils.normalize_uuid(network_id)
            if not network_uuid:
                return oauth_utils._json_response(False, error="network Id must be a valid UUID", status=400)

            is_valid, validation_error = etherlink_utils.validate_node_config_payload(payload)
            if not is_valid:
                return oauth_utils._json_response(False, error=validation_error, status=400)

            subscription, _node = self._get_subscription_from_node(network_uuid)

            if not subscription:
                return oauth_utils._json_response(
                    False,
                    error="Network not found or maybe you are not authorized to update config",
                    status=404,
                )
            if subscription.customer_name.id != user.partner_id.id:
                return oauth_utils._json_response(
                    False,
                    error="You are not authorized to update config",
                    status=403,
                )

            _logger.debug("Forwarding Etherlink config update for %s", network_uuid)

            try:
                ansible_base = etherlink_utils.get_ansible_base_url(network_uuid)
            except ValueError as exc:
                _logger.error("%s", exc)
                return oauth_utils._json_response(
                    False,
                    error=str(exc),
                    status=500,
                )

            endpoint = f"{ansible_base}/receive-config"
            connect_timeout = etherlink_utils.get_ansible_connect_timeout()
            read_timeout = etherlink_utils.get_ansible_read_timeout()

            try:
                ansible_response = requests.post(
                    endpoint,
                    json=payload,
                    headers={"Content-Type": "application/json"},
                    timeout=(connect_timeout, read_timeout),
                )
            except requests.ReadTimeout as exc:
                _logger.warning(
                    "Etherlink Ansible timed out for %s "
                    "(connect_timeout=%ss read_timeout=%ss endpoint=%s): %s",
                    network_uuid,
                    connect_timeout,
                    read_timeout,
                    endpoint,
                    exc,
                )
                return oauth_utils._json_response(
                    False,
                    error=f"Ansible server timed out after {read_timeout} seconds",
                    status=504,
                    timeout=etherlink_utils._ANSIBLE_TIMEOUT,
                )
            except requests.RequestException as exc:
                _logger.exception(
                    "Failed to reach Etherlink Ansible server for %s via %s: %s",
                    network_uuid,
                    endpoint,
                    exc,
                )
                return oauth_utils._json_response(
                    False,
                    error="Failed to reach ansible server",
                    status=502,
                )

            if ansible_response.status_code >= 400:
                upstream_error = self._extract_ansible_error(ansible_response)
                _logger.error(
                    "Etherlink Ansible error (status=%s) body=%s",
                    ansible_response.status_code,
                    upstream_error,
                )
                return oauth_utils._json_response(
                    False,
                    error=upstream_error,
                    status=ansible_response.status_code,
                )

            try:
                ansible_payload = ansible_response.json()
            except ValueError:
                ansible_payload = {"raw": ansible_response.text}

            return oauth_utils._json_response(
                True,
                {"ansibleServerResponse": ansible_payload},
                error="Node configuration update forwarded successfully.",
            )

        except Exception as exc:  # pragma: no cover - unexpected messages
            _logger.exception("Error updating Etherlink node configuration: %s", exc)
            return oauth_utils._json_response(
                False,
                error="Failed to update node configuration",
                status=500,
            )

    @http.route("/api/v1/node-config/status", type="http", auth="none", methods=["OPTIONS", "GET"], csrf=False)
    def get_node_config_status(self, **kwargs):
        """Fetch async config update status for Etherlink nodes."""
        try:
            if request.httprequest.method == "OPTIONS":
                return oauth_utils.preflight_response(["GET"])

            user, resp = oauth_utils.require_user()
            if not user:
                return resp

            network_id = kwargs.get("nodeId")
            if not network_id:
                return oauth_utils._json_response(False, error="network Id is required", status=400)

            network_uuid = etherlink_utils.normalize_uuid(network_id)
            if not network_uuid:
                return oauth_utils._json_response(False, error="network Id must be a valid UUID", status=400)

            subscription, node = self._get_subscription_from_node(network_uuid)
            if not subscription:
                return oauth_utils._json_response(
                    False,
                    error="Network not found or maybe you are not authorized to view config status",
                    status=404,
                )
            if subscription.customer_name.id != user.partner_id.id:
                return oauth_utils._json_response(
                    False,
                    error="You are not authorized to view config status",
                    status=403,
                )
            if not node:
                return oauth_utils._json_response(False, error="Node not found", status=404)

            validator_info = self._parse_validator_info(node)
            endpoint_domain = (
                validator_info.get("endpoint_domain")
                or validator_info.get("endpointDomain")
                or validator_info.get("endpoint")
                or node.endpoint_url
            )
            endpoint_domain = str(endpoint_domain).strip() if endpoint_domain else ""
            if not endpoint_domain:
                return oauth_utils._json_response(
                    False,
                    error="endpoint_domain not found in validator info",
                    status=400,
                )

            icp = request.env["ir.config_parameter"].sudo()
            ansible_base = icp.get_param("etherlink.ansible.url")
            if not ansible_base:
                _logger.error("Etherlink Ansible URL is not configured")
                return oauth_utils._json_response(
                    False,
                    error="Etherlink ansible URL is not configured",
                    status=500,
                )

            endpoint = f"{ansible_base.rstrip('/')}/status/{endpoint_domain}"
            connect_timeout = etherlink_utils.get_ansible_connect_timeout()
            read_timeout = etherlink_utils.get_ansible_read_timeout()

            try:
                ansible_response = requests.get(
                    endpoint,
                    headers={"Content-Type": "application/json"},
                    timeout=(connect_timeout, read_timeout),
                )
            except requests.ReadTimeout as exc:
                _logger.warning(
                    "Etherlink Ansible status timed out for %s "
                    "(connect_timeout=%ss read_timeout=%ss endpoint=%s): %s",
                    network_uuid,
                    connect_timeout,
                    read_timeout,
                    endpoint,
                    exc,
                )
                return oauth_utils._json_response(
                    False,
                    error=f"Ansible server timed out after {read_timeout} seconds",
                    status=504,
                )
            except requests.RequestException as exc:
                _logger.exception(
                    "Failed to reach Etherlink Ansible status server for %s via %s: %s",
                    network_uuid,
                    endpoint,
                    exc,
                )
                return oauth_utils._json_response(
                    False,
                    error="Failed to reach ansible server",
                    status=502,
                )

            if ansible_response.status_code >= 400:
                upstream_error = self._extract_ansible_error(ansible_response)
                _logger.error(
                    "Etherlink Ansible status error (status=%s) body=%s",
                    ansible_response.status_code,
                    upstream_error,
                )
                return oauth_utils._json_response(
                    False,
                    error=upstream_error,
                    status=ansible_response.status_code,
                )

            try:
                ansible_payload = ansible_response.json()
            except ValueError:
                ansible_payload = {"raw": ansible_response.text}

            return oauth_utils._json_response(
                True,
                {
                    "endpointDomain": endpoint_domain,
                    "ansibleServerResponse": ansible_payload,
                },
                error="Node configuration status fetched successfully.",
            )

        except Exception as exc:  # pragma: no cover - unexpected messages
            _logger.exception("Error fetching Etherlink node configuration status: %s", exc)
            return oauth_utils._json_response(
                False,
                error="Failed to fetch node configuration status",
                status=500,
            )

    @http.route(
        "/api/v1/node-config/add-updation-log",
        type="http",
        auth="none",
        methods=["OPTIONS", "POST"],
        csrf=False,
    )
    def add_updation_log(self, **kwargs):
        """Add a log entry for node configuration updates."""
        try:
            if request.httprequest.method == "OPTIONS":
                return oauth_utils.preflight_response(["POST"])

            user, resp = oauth_utils.require_user()
            if not user:
                return resp


            payload = request.httprequest.get_json(force=True, silent=True) or {}
            if not isinstance(payload, dict):
                return oauth_utils._json_response(False, error="Invalid JSON payload", status=400)

            is_valid, validation_error = etherlink_utils.validate_updation_log_payload(payload)
            if not is_valid:
                return oauth_utils._json_response(False, error=validation_error, status=400)

            normalized_payload_email = payload.get("userEmail")
            user_email_normalized = user.partner_id.email
            if user_email_normalized and normalized_payload_email != user_email_normalized:
                return oauth_utils._json_response(
                    False,
                    error="You are not authorized to log updates for this email",
                    status=403,
                )

            node_uuid = etherlink_utils.normalize_uuid(payload.get("nodeId"))
            subscription, _node = self._get_subscription_from_node(node_uuid)
            if not subscription:
                return oauth_utils._json_response(False, error="Node not found", status=404)
            if subscription.customer_name.id != user.partner_id.id:
                return oauth_utils._json_response(
                    False,
                    error="You are not authorized to log updates for this node",
                    status=403,
                )

            updated_at_value = etherlink_utils.parse_iso8601_utc(payload.get("updatedAt"))
            if not updated_at_value:
                return oauth_utils._json_response(
                    False,
                    error="updatedAt must be in the format YYYY-MM-DDTHH:mm:ss(.ffffff)Z",
                    status=400,
                )

            updated_config_payload = payload.get("updatedConfig")
            if isinstance(updated_config_payload, (dict, list)):
                updated_config_serialized = json.dumps(updated_config_payload, separators=(",", ":"))
            else:
                updated_config_serialized = str(updated_config_payload)

            log_vals = {
                "subscription_id": subscription.id,
                "node_id": node_uuid,
                "protocol_name": str(payload.get("protocolName") or "").strip(),
                "user_email": str(payload.get("userEmail") or "").strip(),
                "user_id": user.id,
                "updated_at": fields.Datetime.to_string(updated_at_value),
                "updated_config": updated_config_serialized,
                "status": str(payload.get("status") or "").strip(),
            }
            request.env["etherlink.node.config.update"].sudo().create(log_vals)

            _logger.debug(
                "Logged Etherlink config update for node %s with status %s",
                node_uuid,
                log_vals["status"],
            )

            return oauth_utils._json_response(True, error="Log added successfully")

        except Exception as exc:  # pragma: no cover - unexpected messages
            _logger.exception("Error recording Etherlink node config log: %s", exc)
            return oauth_utils._json_response(
                False,
                error="Failed to add configuration log",
                status=500,
            )

    @http.route(
        "/api/v1/<string:network_id>/logs",
        type="http",
        auth="none",
        methods=["OPTIONS", "GET"],
        csrf=False,
    )
    def get_loki_logs(self, network_id, **kwargs):
        """Fetch Loki logs for a node."""
        try:
            if request.httprequest.method == "OPTIONS":
                return oauth_utils.preflight_response(["GET"])

            user, resp = oauth_utils.require_user()
            if not user:
                return resp

            network_uuid = etherlink_utils.normalize_uuid(network_id)
            if not network_uuid:
                return oauth_utils._json_response(False, error="network Id must be a valid UUID", status=400)

            agent_param = kwargs.get("agentId")

            agent_uuid = etherlink_utils.normalize_uuid(agent_param)
            if not agent_uuid:
                return oauth_utils._json_response(False, error="agentId must be a valid UUID", status=400)

            service_name =  kwargs.get("serviceName")

            service_name = str(service_name).strip() if service_name else None

            start_raw = kwargs.get("startTime")
            end_raw = kwargs.get("endTime")

            start_time, start_error = etherlink_utils._parse_timestamp_param(start_raw, "Start-Time")
            if start_error:
                return oauth_utils._json_response(False, error=start_error, status=400)
            end_time, end_error = etherlink_utils._parse_timestamp_param(end_raw, "End-Time")
            if end_error:
                return oauth_utils._json_response(False, error=end_error, status=400)

            if start_time is not None and end_time is not None and end_time <= start_time:
                return oauth_utils._json_response(
                    False,
                    error="end timestamp must not be before or equal to start time",
                    status=400,
                )

            subscription, _node = self._get_subscription_from_node(network_uuid)
            if not subscription:
                return oauth_utils._json_response(False, error="Node not found", status=404)
            if subscription.customer_name.id != user.partner_id.id:
                return oauth_utils._json_response(False, error="You are not authorized to view logs", status=403)

            try:
                log_payload = etherlink_utils._query_loki_logs(
                    network_uuid,
                    agent_uuid,
                    service_name,
                    start_time,
                    end_time,
                )
            except ValueError as exc:
                _logger.error("Loki configuration error: %s", exc)
                return oauth_utils._json_response(False, error=str(exc), status=500)
            except requests.HTTPError as exc:
                status = exc.response.status_code if exc.response else 502
                _logger.exception("Loki logs HTTP error for node %s: %s", network_uuid, exc)
                return oauth_utils._json_response(
                    False,
                    error="Failed to fetch logs from Loki",
                    status=status,
                )
            except requests.RequestException as exc:
                _logger.exception("Loki logs request error for node %s: %s", network_uuid, exc)
                return oauth_utils._json_response(
                    False,
                    error="Failed to reach Loki service",
                    status=502,
                )

            return oauth_utils._json_response(True, log_payload)

        except Exception as exc:  # pragma: no cover - unexpected messages
            _logger.exception("Unexpected error fetching Loki logs for node %s: %s", network_id, exc)
            return oauth_utils._json_response(False, error="Failed to fetch logs", status=500)

    @http.route(
        "/api/v1/<string:network_id>/services",
        type="http",
        auth="none",
        methods=["OPTIONS", "GET"],
        csrf=False,
    )
    def get_loki_services(self, network_id, **kwargs):
        """Fetch Loki services for a node."""
        try:
            if request.httprequest.method == "OPTIONS":
                return oauth_utils.preflight_response(["GET"])

            user, resp = oauth_utils.require_user()
            if not user:
                return resp
            # user = request.env['res.users'].sudo().search([("id","=",21)])

            network_uuid = etherlink_utils.normalize_uuid(network_id)
            if not network_uuid:
                return oauth_utils._json_response(False, error="network Id must be a valid UUID", status=400)

            agent_param = kwargs.get("agentId")

            agent_uuid = etherlink_utils.normalize_uuid(agent_param)
            if not agent_uuid:
                return oauth_utils._json_response(False, error="agentId must be a valid UUID", status=400)

            subscription, _node = self._get_subscription_from_node(network_uuid)
            if not subscription:
                return oauth_utils._json_response(False, error="Node not found", status=404)
            if subscription.customer_name.id != user.partner_id.id:
                return oauth_utils._json_response(False, error="You are not authorized to view services", status=403)

            try:
                services = etherlink_utils._query_loki_services(network_uuid, agent_uuid)
            except ValueError as exc:
                _logger.error("Loki configuration error: %s", exc)
                return oauth_utils._json_response(False, error=str(exc), status=500)
            except requests.HTTPError as exc:
                status = exc.response.status_code if exc.response else 502
                _logger.exception("Loki services HTTP error for node %s: %s", network_uuid, exc)
                return oauth_utils._json_response(
                    False,
                    error="Failed to fetch services from Loki",
                    status=status,
                )
            except requests.RequestException as exc:
                _logger.exception("Loki services request error for node %s: %s", network_uuid, exc)
                return oauth_utils._json_response(
                    False,
                    error="Failed to reach Loki service",
                    status=502,
                )

            return oauth_utils._json_response(True, {"services": services})

        except Exception as exc:  # pragma: no cover - unexpected messages
            _logger.exception("Unexpected error fetching Loki services for node %s: %s", network_id, exc)
            return oauth_utils._json_response(False, error="Failed to fetch services", status=500)
