# -*- coding: utf-8 -*-
"""
OPN Subscription Management API Endpoints
Provides REST API for OPN protocol subscriptions without payment flow
"""

import json
import logging
from datetime import datetime, timedelta

from odoo import http, fields
from odoo.http import request

from ...auth_module.utils import oauth as oauth_utils
from ...zeeve_base.utils import base_utils
from ..utils.email_utils import send_subscription_email
from odoo.tools.misc import  format_date


_logger = logging.getLogger(__name__)


class OPNSubscriptionController(http.Controller):
    """REST endpoints for OPN subscription management."""

    @staticmethod
    def _calculate_next_payment_date(payment_frequency):
        """Calculate next payment date based on payment frequency."""
        if payment_frequency == "monthly":
            return fields.Datetime.now() + timedelta(days=30)
        elif payment_frequency == "quarterly":
            return fields.Datetime.now() + timedelta(days=90)
        elif payment_frequency == "annually":
            return fields.Datetime.now() + timedelta(days=365)
        else:
            return fields.Datetime.now() + timedelta(days=30)

    @staticmethod
    def _check_opn_subscription_limit(env, user):
        """Check if user has reached their OPN subscription limit.
        
        Counts only active subscriptions (not closed or canceled).
        Returns (is_allowed, current_count, max_limit) tuple.
        """
        partner = user.partner_id
        if not partner:
            return False, 0, 0
        
        max_limit = partner.max_opn_subscriptions or 3
        
        # Count active OPN subscriptions (exclude closed and canceled states)
        active_opn_subs = env["subscription.subscription"].sudo().search_count([
            ("customer_name", "=", partner.id),
            ("protocol_id.name", "ilike", "opn"),
            ("state", "not in", ["closed", "canceled"]),
        ])
        
        is_allowed = active_opn_subs < max_limit
        return is_allowed, active_opn_subs, max_limit

    @http.route(
        "/api/v1/opn/subscribe",
        type="http",
        auth="none",
        methods=["OPTIONS", "POST"],
        csrf=False,
    )
    def subscribe_opn_node(self, **kwargs):
        """Subscribe to OPN node without payment.
        
        Creates an active subscription and provisioning node.
        Sends invoice email to user, node provisioning mail to admin, and new subscription mail to admin.
        
        Request body:
            {
                "duration": str (required) - "monthly", "quarterly", "annually",
                "plan_type": str (required) - plan name (e.g., "Advance"),
                "protocol_id": str (required) - protocol UUID or ID,
                "subscription_type": str (optional) - "validator" (default),
                "automatic_update": str (optional) - "auto" or "manual",
                "server_location_id": str (optional) - location code/name,
                "network_selection": str (optional) - network name (e.g., "Testnet"),
                "autopay_enabled": bool (optional) - default true,
                "quantity": int (optional) - default 1,
                "validator_info": dict (optional) - validator address info
            }
        """
        try:
            if request.httprequest.method == "OPTIONS":
                return oauth_utils.preflight_response(["POST"])

            # Authenticate user
            user, resp = oauth_utils.require_user()
            if not user:
                return resp
            # user = request.env["res.users"].sudo().search([("id", "=",18)])

            # Check OPN subscription limit
            is_allowed, current_count, max_limit = self._check_opn_subscription_limit(request.env, user)
            if not is_allowed:
                return oauth_utils._json_response(
                    False,
                    error=f"User has reached maximum OPN subscriptions limit ({current_count}/{max_limit})",
                    status=403
                )

            # Parse and validate payload
            payload = request.httprequest.get_json(force=True, silent=True) or {}
            if not isinstance(payload, dict):
                return oauth_utils._json_response(
                    False, error="Invalid JSON payload.", status=400
                )

            required_fields = ["protocol_id"]
            is_valid, error_msg = base_utils._validate_payload(payload, required_fields)
            if not is_valid:
                return oauth_utils._json_response(False, error=error_msg, status=400)

            # Extract payload values
            duration = ""  # Empty by default for OPN
            plan_type = "Advance"  # Default to Advance plan
            protocol_id_param = payload.get("protocol_id")
            subscription_type = payload.get("subscription_type", "validator").strip()
            automatic_update = payload.get("automatic_update", "auto").strip().lower()
            server_location_param = payload.get("server_location_id", "").strip()
            network_selection_param = payload.get("network_selection", "").strip()
            autopay_enabled = payload.get("autopay_enabled", True)
            quantity = payload.get("quantity", 1)
            validator_info = payload.get("validator_info", {})

            # Set default payment frequency (empty for OPN offline payments)
            payment_frequency = "monthly"  # No specific frequency for OPN

            # Find protocol by UUID or ID
            domain = [("protocol_id", "=", protocol_id_param)]
            # Try to match by integer ID if protocol_id_param is numeric
            try:
                protocol_id_int = int(protocol_id_param)
                domain = ["|", ("id", "=", protocol_id_int), ("protocol_id", "=", protocol_id_param)]
            except (ValueError, TypeError):
                # protocol_id_param is not numeric, search by protocol_id only
                pass
            
            protocol = request.env["protocol.master"].sudo().search(
                domain,
                limit=1
            )
            if not protocol or not protocol.exists():
                return oauth_utils._json_response(
                    False, error="Protocol not found", status=404
                )

            protocol_name = (protocol.name or "").strip().lower()
            if "opn" not in protocol_name:
                return oauth_utils._json_response(
                    False,
                    error="This endpoint is only for OPN protocol subscriptions",
                    status=400,
                )

            # Find plan by name and protocol
            plan = request.env["subscription.plan"].sudo().search(
                [
                    ("name", "ilike", plan_type),
                    ("protocol_id.id", "=", protocol.id)
                ],
                limit=1
            )
            if not plan or not plan.exists():
                return oauth_utils._json_response(
                    False, error=f"Subscription plan '{plan_type}' not found for {protocol.name}", status=404
                )

            # Get product from plan
            product = plan.product_id
            if not product or not product.exists():
                return oauth_utils._json_response(
                    False, error="Product not found for plan", status=404
                )

            # Find network selection by name
            network_selection_id = False
            if network_selection_param:
                network_selection = request.env["zeeve.network.type"].sudo().search(
                    [("name", "ilike", network_selection_param)],
                    limit=1
                )
                if network_selection and network_selection.exists():
                    network_selection_id = network_selection.id

            # Find server location by code/name
            server_location_id = False
            if server_location_param:
                server_location = request.env["server.location"].sudo().search(
                    [("name", "ilike", server_location_param)],
                    limit=1
                )
                if server_location and server_location.exists():
                    server_location_id = server_location.id

            # Set price to 0 for OPN offline payments
            plan_price = 0.0
            
            # Get company from user or use default
            company = user.company_id or request.env.company
            
            # Create subscription
            subscription_vals = {
                "customer_name": user.partner_id.id,
                "company_id": company.id,
                "protocol_id": protocol.id,
                "sub_plan_id": plan.id,
                "price": plan_price,
                "subscription_type": subscription_type,
                "payment_frequency": payment_frequency,
                "quantity": quantity,
                'source': 'so',
                "autopay_enabled": autopay_enabled,
                "stripe_status": "active",
                "subscribed_on": fields.Datetime.now(),
                "start_date": fields.Date.today(),
                # Calculate next payment date based on frequency
                "stripe_end_date": self._calculate_next_payment_date(payment_frequency),
                "current_term_start": fields.Date.today(),
                "current_term_end": self._calculate_next_payment_date(payment_frequency),
            }
            
            # Set empty duration for OPN
            subscription_vals.update({'duration': 0, 'unit': 'month'})

            subscription = request.env["subscription.subscription"].sudo().create(
                subscription_vals
            )

            # Create node
            node_vals = {
                "subscription_id": subscription.id,
                "node_type": subscription_type,
                "state": "provisioning",  # Set as provisioning
                "network_selection_id": network_selection_id,
                "server_location_id": server_location_id,
                "software_update_rule": automatic_update,
            }

            node = request.env["subscription.node"].sudo().create(node_vals)

            # Store validator info if provided
            if validator_info and isinstance(validator_info, dict):
                node.sudo().write({
                    "validator_info": json.dumps(validator_info)
                })

            # Get backend base URL for emails
            base_url = (
                request.env["ir.config_parameter"]
                .sudo()
                .get_param("backend_url", "")
            )

            # Send admin notification emails (no invoice/payment for offline payment)
            try:
                self._send_admin_subscription_notification(subscription, node, base_url)
                _logger.info(
                    "Admin notification sent for OPN subscription %s",
                    subscription.id
                )
            except Exception as mail_error:
                _logger.warning(
                    "Failed to send admin notification for OPN subscription %s: %s",
                    subscription.id,
                    str(mail_error)
                )

            # Prepare response
            response_payload = {
                "subscription_id": subscription.subscription_uuid or subscription.id,
                "subscription_odoo_id": subscription.id,
                "node_id": node.node_identifier,
                "node_odoo_id": node.id,
                "status": subscription.state,
                "node_status": node.state,
                "protocol_name": protocol.name,
                "plan_name": plan.name,
                "created_at": fields.Datetime.to_string(subscription.create_date),
            }

            return oauth_utils._json_response(
                True,
                response_payload,
                error="OPN subscription created successfully",
            )

        except Exception as exc:  # pragma: no cover
            _logger.exception("Error creating OPN subscription")
            return oauth_utils._json_response(
                False, error=str(exc), status=500
            )

    def _send_admin_subscription_notification(self, subscription, node, base_url):
        """Send OPN subscription notification to admin only (offline payment)."""
        subscription.ensure_one()
        partner = subscription.customer_name
        protocol = subscription.protocol_id
        plan = subscription.sub_plan_id

        # Get admin recipients
        admin_recipients = base_utils._get_admin_recipients(
            subscription.env,
            channel_code=protocol.admin_channel_id.code if protocol and protocol.admin_channel_id else None,
        )

        context = {
            'userDetails':{
                "subscription_name": subscription.name or f"OPN-{subscription.id}",
                "subscription_id": subscription.subscription_uuid or subscription.id,
                "node_id": node.node_identifier,
                "protocol": protocol.name if protocol else "OPN",
                "plan_type": plan.name if plan else "",
                "amount": f"{subscription.price:.2f}",
                "currency": (subscription.currency_id.symbol or subscription.currency_id.name) if subscription.currency_id else "USD",
                "name": partner.display_name or partner.name,
                "buyer_email_id": partner.email or partner.email_formatted or "",
                "subscription_status": subscription.state,
                "payment_type": "offline",
                'start_date': format_date(subscription.env, (subscription.stripe_start_date.date() if isinstance(subscription.stripe_start_date, datetime) else subscription.stripe_start_date) or subscription.start_date) if subscription.stripe_start_date or subscription.start_date else '',
                'subscription_type':subscription.subscription_type,
                'end_date': format_date(subscription.env, (subscription.stripe_end_date.date() if isinstance(subscription.stripe_end_date, datetime) else subscription.stripe_end_date) or subscription.end_date) if subscription.stripe_end_date or subscription.end_date else '',
                "base_url": base_url,
                "baseUrl": base_url,
            },
            'admin_email_cc': ",".join(admin_recipients.get('cc', [])) if admin_recipients.get('cc') else "",
        }

        # Send new subscription notification to admin
        send_subscription_email(
            subscription.env,
            "new_subscription_admin",
            record=subscription,
            context=context,
            email_to=",".join(admin_recipients.get("to", [])),
            email_cc=",".join(admin_recipients.get("cc", [])) if admin_recipients.get("cc") else None,
            force_send=True,
        )
