"""REST API for rollup management."""

from __future__ import annotations

import logging
import stripe
from datetime import datetime

from odoo import http
from odoo.http import request

from ...auth_module.utils import oauth as oauth_utils
from ..utils import deployment_utils, rollup_util as rollup_util
from odoo.tools import html2plaintext
from ...zeeve_base.utils import base_utils as base_utils
from ...access_rights.utils.access_manager import AccessManager

_logger = logging.getLogger(__name__)


class RollupAPIController(http.Controller):
    """Controller exposing rollup management endpoints."""

    # ------------------------------------------------------------------
    # Routes
    # ------------------------------------------------------------------
    @http.route("/api/v1/rollup/types", type="http", auth="public", methods=["GET", "OPTIONS"], csrf=False)
    def list_rollup_types(self, **_kwargs):
        if request.httprequest.method == "OPTIONS":
            return oauth_utils.preflight_response(["GET"])

        user, resp = oauth_utils.require_user()
        if not user:
            return resp
        
        rollup_types = request.env["rollup.type"].sudo().search([])
        data = [
            {
                "id": rollup_type.id,
                "rollup_id": rollup_type.rollup_id,
                "name": rollup_type.name,
                "data_availability": [
                    {
                        "id": da.id,
                        "name": da.name,
                        "comming_soon": da.coming_soon,
                        "logo_url": base_utils.public_image_url(da, "logo", size="64x64") if da.logo else False,
                        "active": da.da_active,
                    }
                    for da in rollup_type.data_availability_ids
                ],
                "settlement_layer": [
                    { "id": sl.id, "name" :sl.name,"active": sl.active } 
                    for sl in rollup_type.settlement_layer_ids],
                "sequencers": [
                    { "id": sq.id, "name" :sq.name,"active": sq.active } 
                    for sq in rollup_type.sequencer_ids],
                "default_regions": [
                    {
                        "id": region.id,
                        "name": region.name,
                        "country_id": region.country_id.id,
                        "country_name": region.country_id.name,
                    }
                    for region in rollup_type.default_region_ids
                ],
            }
            for rollup_type in rollup_types
        ]
        return rollup_util.json_response(True, {"types": data})
    
    @http.route("/api/v1/rollup/configuration", type="http", auth="public", methods=["GET", "OPTIONS"], csrf=False)
    def list_rollup_configuration(self, **_kwargs):
        if request.httprequest.method == "OPTIONS":
            return oauth_utils.preflight_response(["GET"])

        user, resp = oauth_utils.require_user()
        if not user:
            return resp
        # -------------------------------
        # Parse and validate payload
        # -------------------------------
        required_fields = ["type"]
        is_valid, error_msg = base_utils._validate_payload(_kwargs, required_fields)
        if not is_valid:
            return oauth_utils._json_response(False, {'error': error_msg}, status=400)

        rollup_types = request.env["rollup.type"].sudo().search([("name", "=", _kwargs.get("type"))])
        data = [
            {
                "id": rollup_type.id,
                "rollup_id": rollup_type.rollup_id,
                "name": rollup_type.name,
                "data_availability": [
                    {
                        "id": da.id,
                        "name": da.name,
                        "comming_soon": da.coming_soon,
                        "logo_url": base_utils.public_image_url(da, "logo", size="64x64") if da.logo else False,
                        "active": da.da_active,
                    }
                    for da in rollup_type.data_availability_ids
                ],
                "settlement_layer": [
                    { "id": sl.id, "name" :sl.name,"active": sl.active } 
                    for sl in rollup_type.settlement_layer_ids],
                "sequencers": [
                    {
                        "id": sq.id,
                        "name": sq.name,
                        "active": sq.sa_active,
                        "logo_url": base_utils.public_image_url(sq, "logo", size="64x64") if sq.logo else False,
                        "comming_soon": sq.coming_soon,
                    }
                    for sq in rollup_type.sequencer_ids
                ],
                "default_regions": [
                    {
                        "id": region.id,
                        "name": region.name,
                        "country_id": region.country_id.id,
                        "country_name": region.country_id.name,
                    }
                    for region in rollup_type.default_region_ids
                ],
            }
            for rollup_type in rollup_types
        ]
        return rollup_util.json_response(True, {"types": data})


    @http.route("/api/v1/rollup/services", type="http", auth="public", methods=["GET", "OPTIONS"], csrf=False)
    def list_services(self, **_kwargs):
        try:
            if request.httprequest.method == "OPTIONS":
                return oauth_utils.preflight_response(["GET"])

            user, resp = oauth_utils.require_user()
            if not user:
                return resp
            # -------------------------------
            # Parse and validate payload
            # -------------------------------
            required_fields = ["type"]
            is_valid, error_msg = base_utils._validate_payload(_kwargs, required_fields)
            if not is_valid:
                return oauth_utils._json_response(False, {'error': error_msg}, status=400)

            rollup_type = _kwargs.get("type")
            
            # Module access check
            if not AccessManager.check_module_access(user, 'rollup_management'):
                return rollup_util.json_response(False, error="Module access denied", status=403)

            domain = [("type_id.name", "=", rollup_type)]
            domain += AccessManager.get_company_domain(user, 'customer_id')
            
            record_domain = AccessManager.get_record_domain(user, 'rollup_management')
            if record_domain:
                domain += record_domain

            # Apply rollup type scoping for operators with specific access
            rollup_type_domain = AccessManager.get_rollup_type_domain(user)
            if rollup_type_domain:
                domain += rollup_type_domain

            services = request.env["rollup.service"].sudo().search(domain)
            
            # Add demo service if it exists for this rollup type
            demo_service = rollup_util.get_demo_service_if_exists(rollup_type, request.env)
            if demo_service:
                services = services | demo_service
            
            serialized = [rollup_util.serialize_service(service) for service in services]
            return rollup_util.json_response(True, {"services": serialized})
        except Exception as exc:  # pragma: no cover - unexpected messages
            return rollup_util.json_response(False, error=str(exc), status=500)

    @http.route("/api/v1/rollup/service/deploy", type="http", auth="public", methods=["POST", "OPTIONS"], csrf=False)
    def deploy_rollup(self, **_kwargs):
        """Kick off Stripe checkout when the frontend starts a deployment."""
        return self._handle_rollup_deploy(is_v2=False)

    @http.route("/api/v2/rollup/service/deploy", type="http", auth="public", methods=["POST", "OPTIONS"], csrf=False)
    def deploy_rollup_v2(self, **_kwargs):
        """Kick off Stripe checkout session for V2 (Managed Billing)."""
        return self._handle_rollup_deploy(is_v2=True)

    @http.route("/api/v2/rollup/retry_create_checkout_session", type="http", auth="public", methods=["POST", "OPTIONS"], csrf=False)
    def retry_rollup_v2(self, **_kwargs):
        """Retry a draft V2 deployment."""
        return self._handle_rollup_deploy(is_v2=True, is_retry=True)

    def _handle_rollup_deploy(self, is_v2=False, is_retry=False):
        try:
            if request.httprequest.method == "OPTIONS":
                return oauth_utils.preflight_response(["POST"])

            user, resp = oauth_utils.require_user()
            if not user:
                return resp
            # user = request.env['res.users'].sudo().search([('id','=',128)])
            payload = request.httprequest.get_json(force=True, silent=True) or {}
            if not isinstance(payload, dict):
                return rollup_util.json_response(False, error="Invalid JSON payload.", status=400)

            if is_v2:
                payload['is_odoo_managed'] = True
                
                # Proration logic removed as per requirements.

            required_fields = ["type_id", "name", "region_ids", "configuration", "network_type"]
            if is_retry:
                required_fields = ["deployment_token"]

            is_valid, error_message = base_utils._validate_payload(payload, required_fields)
            if not is_valid:
                return rollup_util.json_response(False, error=error_message, status=400)

            try:
                checkout_session, checkout_context, data = deployment_utils.start_checkout(user, payload)
            except rollup_util.RollupError as exc:
                if exc.status >= 500:
                    _logger.exception("Failed to create Stripe checkout session: %s", exc)
                return rollup_util.json_response(False, error=str(exc), status=exc.status)
            except Exception as exc:
                _logger.exception("Unexpected error creating checkout session: %s", exc)
                return rollup_util.json_response(False, error="Unexpected server error.", status=500)

            _logger.info(
                "Created rollup %s checkout session %s for user %s and token %s",
                "V2" if is_v2 else "V1",
                checkout_session.id,
                user.id,
                checkout_context.deployment_token,
            )

            return rollup_util.json_response(True, data)
        except Exception as exc:
            return rollup_util.json_response(False, error=str(exc), status=500)

    @http.route("/api/v1/rollup/deploy", type="http", auth="public", methods=["POST", "OPTIONS"], csrf=False)
    def deploy_service(self, **_kwargs):
        """Fallback endpoint used by the frontend to finalise a checkout."""

        try:
            if request.httprequest.method == "OPTIONS":
                return oauth_utils.preflight_response(["POST"])

            user, resp = oauth_utils.require_user()
            if not user:
                return resp

            payload = request.httprequest.get_json(force=True, silent=True) or {}
            if not isinstance(payload, dict):
                return rollup_util.json_response(False, error="Invalid JSON payload.", status=400)

            session_id = payload.get("stripe_session_id")
            if session_id:
                is_valid, error_message = base_utils._validate_payload(payload, ["stripe_session_id"])
                if not is_valid:
                    return rollup_util.json_response(False, error=error_message, status=400)

                # This branch is kept for backwards compatibility; in the
                # default flow Stripe now calls our webhook which triggers the
                # same finalisation logic server-side.
                try:
                    service, created, metadata_update, checkout_session = rollup_util.finalize_deployment(
                        user, payload
                    )
                except rollup_util.RollupError as exc:
                    if exc.status >= 500:
                        _logger.exception(
                            "Deployment finalization error for session %s: %s", session_id, exc
                        )
                    return rollup_util.json_response(False, error=str(exc), status=exc.status)
                except Exception as exc:  # pylint: disable=broad-except
                    _logger.exception(
                        "Unexpected error finalizing deployment for session %s: %s", session_id, exc
                    )
                    return rollup_util.json_response(False, error="Unexpected server error.", status=500)

                if not created:
                    _logger.info(
                        "Stripe session %s already processed for service %s",
                        checkout_session.id,
                        service.service_id,
                    )
                    return rollup_util.json_response(
                        True, {"service": rollup_util.serialize_service(service)}
                    )

                service.action_start_deployment(metadata_update, auto_activate=False)
                service._handle_payment_post_activation()

                return rollup_util.json_response(True, {"service": rollup_util.serialize_service(service)})

            else:
                required_fields = ["type_id", "name", "region_ids", "network_type"]
                is_valid, error_message = base_utils._validate_payload(payload, required_fields)
                if not is_valid:
                    return rollup_util.json_response(False, error=error_message, status=400)

            try:
                checkout_session, checkout_context, data = deployment_utils.start_checkout(user, payload)
            except rollup_util.RollupError as exc:
                if exc.status >= 500:
                    _logger.exception("Failed to create Stripe checkout session: %s", exc)
                return rollup_util.json_response(False, error=str(exc), status=exc.status)
            except Exception as exc:  # pylint: disable=broad-except
                _logger.exception("Unexpected error creating checkout session: %s", exc)
                return rollup_util.json_response(False, error="Unexpected server error.", status=500)

            _logger.info(
                "Created rollup checkout session %s for user %s and token %s",
                checkout_session.id,
                user.id,
                checkout_context.deployment_token,
            )

            return rollup_util.json_response(True, data)
        except Exception as exc:  # pragma: no cover - unexpected messages
            return rollup_util.json_response(False, error=str(exc), status=500)

    @http.route("/api/v1/rollup/nodes", type="http", auth="public", methods=["GET", "OPTIONS"], csrf=False)
    def list_nodes(self, **kwargs):
        if request.httprequest.method == "OPTIONS":
            return oauth_utils.preflight_response(["GET"])

        user, resp = oauth_utils.require_user()
        if not user:
            return resp

        params = {key: kwargs.get(key) for key in ("service_id")}
        required_fields = ["service_id"] if params.get("service_id") else []
        is_valid, error_message = base_utils._validate_payload(params, required_fields)
        if not is_valid:
            return rollup_util.json_response(False, error=error_message, status=400)

        service_identifier = params.get("service_id")

        service_model = request.env["rollup.service"].sudo()
        domain = []
        if service_identifier:
            domain = [("service_id", "=", service_identifier)]

        # Company access check
        domain += AccessManager.get_company_domain(user, 'customer_id')
        
        # Granular access check for operators
        rollup_type_domain = AccessManager.get_rollup_type_domain(user)
        if rollup_type_domain:
            domain += rollup_type_domain

        service = service_model.search(domain, limit=1)
        if not service:
            return rollup_util.json_response(False, error="Service not found.", status=404)

        nodes = [rollup_util.serialize_node(node) for node in service.node_ids]
        return rollup_util.json_response(True, nodes)
