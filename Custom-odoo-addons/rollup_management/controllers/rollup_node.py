"""REST API for rollup Node management."""

from __future__ import annotations

import base64
import json
import logging

from odoo import http
from odoo.http import request

from ...auth_module.utils import oauth as oauth_utils
from ..utils import deployment_utils, rollup_util as rollup_util
from odoo.tools import html2plaintext
from ...zeeve_base.utils import base_utils as base_utils
from ...access_rights.utils.access_manager import AccessManager

_logger = logging.getLogger(__name__)


class RollupNodeAPIController(http.Controller):
    """Controller exposing rollup Node management endpoints."""

    # ------------------------------------------------------------------
    # Routes
    # ------------------------------------------------------------------

    @http.route("/api/v1/rollup_node/overview", type="http", auth="public", methods=["GET", "OPTIONS"], csrf=False)
    def rollup_node_overview(self, **_kwargs):
        try:
            if request.httprequest.method == "OPTIONS":
                return oauth_utils.preflight_response(["GET"])

            user, resp = oauth_utils.require_user()
            if not user:
                return resp
            # -------------------------------
            # Parse and validate payload
            # -------------------------------
            required_fields = ["type", "service_id"]
            is_valid, error_msg = base_utils._validate_payload(_kwargs, required_fields)
            if not is_valid:
                return oauth_utils._json_response(False, {'error': error_msg}, status=400)
            service_identifier = _kwargs.get("service_id")
            rollup_type = _kwargs.get("type")

            # Check if this is a demo rollup request
            demo_service = rollup_util.get_demo_service_if_exists(rollup_type, request.env)
            is_demo = demo_service and service_identifier == demo_service.service_id

            # Build domain based on whether it's a demo or regular request
            domain = [
                ("type_id.name", "=", rollup_type),
                ("service_id", "=", service_identifier),
                ("status", "!=", "draft")
            ]
            if not is_demo:
                domain += AccessManager.get_company_domain(user, 'customer_id')

            rollup_service = request.env["rollup.service"].sudo().search(domain, limit=1)
            if not rollup_service:
                return oauth_utils._json_response(
                    False,
                    {'error': "Service not found for the provided identifier."},
                    status=404,
                )
            user_inputs_payload = rollup_service.inputs_json if isinstance(rollup_service.inputs_json, dict) else {}
            nodes_payload = rollup_service.get_rollup_nodes_overview()
            metadata_value = rollup_service.rollup_metadata
            try:
                metadata_value = json.loads(metadata_value) if isinstance(metadata_value, str) else metadata_value
            except ValueError:
                pass
            data = {
                "service_id": rollup_service.id,
                "service_name" : rollup_service.name,
                "status" : rollup_service.status,
                "chain_id" : rollup_service.chain_id,
                "user_inputs": user_inputs_payload,
                "nodes": nodes_payload,
                "rollup_metadata": metadata_value,
                "created_at": rollup_service.create_date,
            }
            return rollup_util.json_response(True, data)
        except Exception as e:
            _logger.error("Error in rollup_node_overview: %s", e)
            return oauth_utils._json_response(False, {'error': str(e)}, status=500)
        
    @http.route("/api/v1/rollup_node/blockchain_details", type="http", auth="public", methods=["GET", "OPTIONS"], csrf=False)
    def rollup_node_blockchain_details(self, **_kwargs):
        try:
            if request.httprequest.method == "OPTIONS":
                return oauth_utils.preflight_response(["GET"])

            user, resp = oauth_utils.require_user()
            if not user:
                return resp
            # -------------------------------
            # Parse and validate payload
            # -------------------------------
            required_fields = ["type", "service_id"]
            is_valid, error_msg = base_utils._validate_payload(_kwargs, required_fields)
            if not is_valid:
                return oauth_utils._json_response(False, {'error': error_msg}, status=400)

            service_identifier = _kwargs.get("service_id")
            rollup_type = _kwargs.get("type")
            
            # Check if this is a demo rollup request
            demo_service = rollup_util.get_demo_service_if_exists(rollup_type, request.env)
            is_demo = demo_service and service_identifier == demo_service.service_id

            # Build domain based on whether it's a demo or regular request
            domain = [
                ("status", "!=", "draft"),
                ("type_id.name", "=", rollup_type),
                ("service_id", "=", service_identifier),
            ]
            if not is_demo:
                domain.append(("company_id", "=", user.company_id.id))

            rollup_service = request.env["rollup.service"].sudo().search(domain, limit=1)
            if not rollup_service:
                return oauth_utils._json_response(
                    False,
                    {'error': "Service not found for the provided identifier."},
                    status=404,
                )
            rollup_type = rollup_service.type_id.name
            contracts_payload = []
            user_inputs_payload = rollup_service.inputs_json if isinstance(rollup_service.inputs_json, dict) else {}
            for attachment in rollup_service.artifacts:
                mimetype = (attachment.mimetype or "").lower()
                name = attachment.name or ""
                if name.strip().lower() == "genesis.json" and rollup_type != "zksync": # only zksync uses genesis.json
                    continue  # genesis config stays internal
                is_json = "json" in mimetype or name.lower().endswith(".json")
                content_value = None
                decoded_text = None
                if attachment.datas:
                    try:
                        decoded_text = base64.b64decode(attachment.datas).decode("utf-8")
                    except Exception:
                        decoded_text = attachment.datas
                if decoded_text is not None:
                    if is_json:
                        try:
                            content_value = json.loads(decoded_text)
                        except Exception:
                            content_value = decoded_text
                    else:
                        content_value = decoded_text
                contracts_payload.append({
                    "name": name,
                    "is_json": is_json,
                    "content": content_value,
                })
            data = {
                "service_id": rollup_service.id,
                "service_name" : rollup_service.name,
                "user_inputs": user_inputs_payload,
                "artifacts": contracts_payload
            }
            return rollup_util.json_response(True, data)
        except Exception as e:
            _logger.error("Error in rollup_node_blockchain_details: %s", e)
            return oauth_utils._json_response(False, {'error': str(e)}, status=500)
