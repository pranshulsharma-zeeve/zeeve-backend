"""HTTP API endpoints for Subscription Management data.

The responses follow the same JSON structure used throughout the
``auth_module`` so that callers receive objects of the form::

    {"success": bool, "data": {}, "message": str}
"""

import json
import logging
from datetime import datetime, timedelta
from typing import Any, Dict, Iterable, List, Optional

import pytz

from odoo import fields, http
from odoo.exceptions import UserError
from odoo.http import request

from ...auth_module.utils import oauth as oauth_utils
from ...zeeve_base.utils import base_utils as base_utils
from ...zeeve_base.utils.reports.pricing import TokenPriceService, convert_raw_value
from ..utils.investment_utils import (
    _coerce_float,
    _resolve_investment_protocol_record,
    _simulate_investment_projection,
)
from ..utils.subscription_helpers import (
    LCDRequestError,
    _compute_validator_delegations,
    _compute_validator_summary,
    _extract_delegation_address,
    _extract_validator_address,
    _fetch_validator_performance_with_period,
    _fetch_validator_rewards_with_period,
    _fetch_validator_stake_delegator_with_period,
    _flow_extract_owner_address,
    _flow_normalize_owner_address,
    _is_valoper_address,
    _normalize_protocol_name,
    _resolve_protocol_rpc_url,
    _theta_fetch_account_transactions,
    _validator_delegations_page,
    SUPPORTED_VALIDATOR_HISTORY_PROTOCOLS,
    SUPPORTED_VALIDATOR_PERFORMANCE_PROTOCOLS,
)
from ..utils.email_utils import send_validator_staking_notification

_logger = logging.getLogger(__name__)


def _format_supported_protocols(protocol_keys: Iterable[str]) -> str:
    """Return a comma separated human friendly list of protocol names."""
    labels = []
    for key in sorted(set(protocol_keys)):
        if not key:
            continue
        labels.append(key.capitalize())
    return ", ".join(labels)


class SubscriptionAPI(http.Controller):
    """REST endpoints for working with subscriptions."""

    @staticmethod
    def _normalize_identifier(*candidates: Optional[str]) -> str:
        """Return the first non-empty identifier from the provided candidates."""
        for candidate in candidates:
            if not candidate:
                continue
            value = str(candidate).strip()
            if value:
                return value
        return ""

    @staticmethod
    def _get_subscription_from_node(node_identifier: str):
        """Return (subscription, node) for a given node identifier."""
        node_model = request.env["subscription.node"].sudo()
        subscription_model = request.env["subscription.subscription"].sudo()
        empty_node = node_model.browse()
        if not node_identifier:
            return subscription_model.browse(), empty_node
        node = node_model.search([("node_identifier", "=", node_identifier)], limit=1)
        if node:
            return node.subscription_id, node
        subscription = subscription_model.search([("subscription_uuid", "=", node_identifier)], limit=1)
        return subscription, empty_node


    @http.route(
        "/api/v1/list/subscription",
        type="http",
        auth="none",
        methods=["OPTIONS", "GET"],
        csrf=False,
    )
    def list_nodes(self, **kwargs):
        """List purchased subscriptions filtered by node type.

        A valid ``Authorization`` header with the session SID must be provided
        (``Bearer <sid>``).
        """
        try:
            if request.httprequest.method == "OPTIONS":
                return oauth_utils.preflight_response()

            user, resp = oauth_utils.require_user()
            if not user:
                return resp
            # user = request.env['res.users'].sudo().search([('id', '=', 10)], limit=1)

            # -------------------------------
            # Parse and validate payload
            # -------------------------------
            required_fields = ["node_type", "page", "size"]
            is_valid, error_msg = base_utils._validate_payload(kwargs, required_fields)
            if not is_valid:
                return oauth_utils._json_response(False, {'error': error_msg}, status=400)

            node_type = kwargs.get("node_type")
            sub_type = node_type
            page = int(kwargs.get("page", 1))
            size = int(kwargs.get("size", 5))
            requested_status = (kwargs.get("status") or "").strip().lower()
            status_group_map = {
                "ready": ["ready"],
                "provisioning": ["requested", "provisioning", "in_grace", "cancellation_requested"],
                "draft": ["draft"],
                "complete_payment": ["draft"],
            }

            if requested_status and requested_status not in {**status_group_map, "others": []}:
                return oauth_utils._json_response(
                    False,
                    error="status must be one of: ready, provisioning, draft, complete_payment, others",
                    status=400,
                )

            # Check if user belongs to access_rights.group_admin
            is_admin_user = user.has_group('access_rights.group_admin')

            if is_admin_user:
                # Internal users can see all nodes of the specified type
                domain = []
            elif user.company_role:
                # Invited org member (admin/operator): see the company's nodes
                # Nodes belong to the owner's partner, not the operator's partner directly
                company_partner_ids = request.env['res.users'].sudo().search([
                    ('company_id', '=', user.company_id.id),
                    ('is_company_owner', '=', True)
                ]).mapped('partner_id.id')
                if not company_partner_ids:
                    # Fallback: all users in the company
                    company_partner_ids = request.env['res.users'].sudo().search([
                        ('company_id', '=', user.company_id.id)
                    ]).mapped('partner_id.id')
                domain = [("subscription_id.customer_name", "in", company_partner_ids)]
                # Apply node-level access restrictions for operators
                from ...access_rights.utils.access_manager import AccessManager
                node_type_domain = AccessManager.get_node_type_domain(user)
                if node_type_domain:
                    domain += node_type_domain
            else:
                # Regular user: own nodes only
                domain = [("subscription_id.customer_name", "=", user.partner_id.id)]
                # Apply node type scoping for operators with specific access
                from ...access_rights.utils.access_manager import AccessManager
                node_type_domain = AccessManager.get_node_type_domain(user)
                if node_type_domain:
                    domain += node_type_domain
            
            if sub_type:
                domain.append(("node_type", "=", sub_type))
                domain.append(("state", "not in", ["deleted", "closed"]))

            summary_domain = list(domain)
            filtered_domain = list(domain)
            grouped_statuses = {
                state
                for states in status_group_map.values()
                for state in states
            }
            if sub_type == "validator" and requested_status:
                if requested_status == "others":
                    filtered_domain.append(("state", "not in", list(grouped_statuses)))
                else:
                    filtered_domain.append(("state", "in", status_group_map[requested_status]))

            # CRITICAL: Invalidate subscription cache to ensure fresh reads
            request.env['subscription.subscription']._invalidate_cache()
            request.env['subscription.node']._invalidate_cache()

            node_model = request.env["subscription.node"].sudo()
            total = node_model.search_count(filtered_domain)
            status_summary = None
            if sub_type == "validator":
                summary_total = node_model.search_count(summary_domain)
                status_summary = {
                    "total": summary_total,
                    "ready": node_model.search_count(summary_domain + [("state", "in", status_group_map["ready"])]),
                    "provisioning": node_model.search_count(summary_domain + [("state", "in", status_group_map["provisioning"])]),
                    "draft": node_model.search_count(summary_domain + [("state", "in", status_group_map["draft"])]),
                    "others": node_model.search_count(
                        summary_domain + [("state", "not in", list(grouped_statuses))]
                    ),
                }
            nodes = node_model.search(
                filtered_domain,
                offset=(page - 1) * size,
                limit=size,
                order="create_date desc",
            )

            # Build a USD price map for protocols on this page (validator nodes only)
            price_service = TokenPriceService(request.env)
            page_protocols = []
            seen_protocol_ids = set()
            for _node in nodes:
                _proto = _node.subscription_id.protocol_id if _node.subscription_id else None
                if _proto and _proto.id not in seen_protocol_ids:
                    seen_protocol_ids.add(_proto.id)
                    page_protocols.append(_proto)
            protocol_prices = price_service.get_prices(page_protocols) if page_protocols else {}

            results: List[dict] = []
            base_url = oauth_utils._get_image_url()
            for sub in nodes:
                # Refresh subscription data from database on each iteration
                sub._invalidate_cache()
                sub.subscription_id._invalidate_cache()
                
                protocol = sub.subscription_id.protocol_id
                icon_url = base_utils.public_image_url(protocol, "image", size="64x64") if protocol.image else False
                draft_prorated = request.env['subscription.prorated.charge'].sudo().search([('state','=','draft'),('subscription_id','=',sub.subscription_id.id)])
                ready_dt = sub.get_ready_at_from_chatter()
                metaData = json.loads(sub.metadata_json) if sub.metadata_json else {}

                ready_display = False
                if ready_dt:
                    try:
                        if isinstance(ready_dt, str):
                            ready_dt = fields.Datetime.from_string(ready_dt)
                        ready_local = fields.Datetime.context_timestamp(request.env.user, ready_dt)
                        ready_display = ready_local.strftime("%Y-%m-%d %H:%M:%S")
                    except Exception:
                        ready_display = ready_dt
                payload = {
                    "protocol_name": protocol.name if protocol else "",
                    "status": sub.state,
                    "next_billing_date": sub.subscription_id.stripe_end_date.isoformat()
                    if sub.subscription_id.stripe_end_date
                    else False,
                    "created_on": fields.Datetime.to_string(sub.node_created_date or
                            sub.subscription_id.subscribed_on
                    ),
                    "protocol_id": protocol.protocol_id if protocol else False,
                    "protocol_logo": icon_url,
                    "subscription_id": sub.subscription_id.id,
                    "node_id": sub.node_identifier,
                    "plan_type": sub.subscription_id.sub_plan_id.name,
                    "node_type": sub.node_type,
                    "node_name": sub.node_name,
                    "network_type": sub.network_selection_id.name if sub.network_selection_id else "",
                    "endpoint":sub.endpoint_url if sub.endpoint_url else metaData.get("endpoint",""),
                    "ready_at": ready_display,
                    "api_key": metaData.get("api_key",""),
                    "subscription_status": "active" if sub.subscription_id.stripe_status == 'trialing' else sub.subscription_id.stripe_status,
                }
                # Include prorated_draft_order only when draft prorated charges exist
                prorated_orders = [{
                        "charge_id": rec.id,
                        "session_id": rec.session_id,
                        "quantity_increase": rec.quantity_increase,
                        "stripe_subscription_id": rec.stripe_subscription_id,
                        "state": rec.state,
                        "subscription_id": rec.subscription_id.id,
                    } for rec in draft_prorated]
                if prorated_orders:
                    payload["prorated_draft_order"] = prorated_orders
                if "layerzero" in (sub.node_name or "").lower():
                    payload["is_layer_zero"] = True

                # Attach latest reward snapshot data for ready validators
                if sub.state == 'ready' and sub.node_type == 'validator':
                    _, valoper_address = _extract_validator_address(sub)
                    if valoper_address:
                        snapshot = request.env['validator.rewards.snapshot'].sudo().search(
                            [('node_id', '=', sub.id), ('valoper', '=', valoper_address)],
                            order='snapshot_date desc',
                            limit=1,
                        )
                        if not snapshot:
                            # No snapshot yet — still surface network_apr from protocol master
                            payload['apr_pct'] = None
                            payload['network_apr'] = float(protocol.network_apr or 0.0) if protocol else None

                        if snapshot:
                            usd_price = protocol_prices.get(protocol.id) if protocol else None
                            stake_decimals = protocol.stake_decimals or 0 if protocol else 0
                            reward_decimals = protocol.reward_decimals or 0 if protocol else 0

                            _logger.info(
                                "Converting snapshot for valoper=%s protocol=%s "
                                "raw_stake=%s (decimals=%d) raw_rewards=%s (decimals=%d) usd_price=%s",
                                valoper_address,
                                protocol.name if protocol else "unknown",
                                snapshot.total_stake, stake_decimals,
                                snapshot.total_rewards, reward_decimals,
                                usd_price,
                            )

                            stake_tokens, stake_usd = convert_raw_value(
                                snapshot.total_stake, stake_decimals, usd_price
                            )
                            reward_tokens, reward_usd = convert_raw_value(
                                snapshot.total_rewards, reward_decimals, usd_price
                            )

                            _logger.info(
                                "Conversion result for valoper=%s: "
                                "stake_tokens=%s stake_usd=%s reward_tokens=%s reward_usd=%s",
                                valoper_address,
                                stake_tokens, stake_usd,
                                reward_tokens, reward_usd,
                            )

                            payload['total_stake'] = stake_tokens
                            payload['total_stake_usd'] = stake_usd
                            payload['total_rewards'] = reward_tokens
                            payload['total_rewards_usd'] = reward_usd
                            payload['commission_pct'] = snapshot.commission_pct

                            proto_key = _normalize_protocol_name(protocol.name or "") if protocol else ""
              
                            if proto_key in {"coreum", "cosmos", "injective"}:
                                payload['apr_pct'] = float(snapshot.apr_pct or 0.0)
                                payload['network_apr'] = float(snapshot.network_apr or 0.0)

                            elif proto_key in {"skale", "energyweb"}:
                                payload['network_apr'] = float(protocol.network_apr or 0.0)
                                oldest_snapshot = request.env['validator.rewards.snapshot'].sudo().search(
                                    [('node_id', '=', sub.id), ('valoper', '=', valoper_address)],
                                    order='snapshot_date asc',
                                    limit=1,
                                )
                                computed_apr = None
                                if oldest_snapshot and oldest_snapshot.id != snapshot.id:
                                    delta_rewards = (snapshot.total_rewards or 0.0) - (oldest_snapshot.total_rewards or 0.0)
                                    avg_stake = ((snapshot.total_stake or 0.0) + (oldest_snapshot.total_stake or 0.0)) / 2.0
                                    delta_days = (snapshot.snapshot_date - oldest_snapshot.snapshot_date).days
                                    if avg_stake > 0 and delta_days > 0 and delta_rewards > 0:
                                        computed_apr = round((delta_rewards / avg_stake) * (365.0 / delta_days) * 100.0, 4)
                                payload['apr_pct'] = computed_apr

                            elif proto_key == "avalanche":
                                # APR is pre-computed from potentialReward / stakeAmount over the staking period
                                payload['apr_pct'] = float(snapshot.apr_pct) if snapshot.apr_pct is not None else None
                                payload['network_apr'] = float(protocol.network_apr or 0.0) if protocol else None

                            elif proto_key == "subsquid":
                                payload['apr_pct'] = float(snapshot.apr_pct or 0.0)
                                payload['network_apr'] = float(protocol.network_apr or 0.0)

                            else:
                                payload['apr_pct'] = None
                                payload['network_apr'] = float(protocol.network_apr or 0.0) if protocol else None

                results.append(payload)
            pagination = {
                "page": page,
                "size": size,
                "total": total,
            }

            response_payload = {"list": results, "pagination": pagination}
            if status_summary is not None:
                response_payload["status_summary"] = status_summary

            return oauth_utils._json_response(
                True,
                response_payload,
                error="Subscriptions fetched successfully.",
            )

        except Exception as exc:  # pragma: no cover - unexpected messages
            return oauth_utils._json_response(False, error=str(exc), status=500)

    @http.route(
        "/api/v1/validator/trending-protocols",
        type="http",
        auth="none",
        methods=["OPTIONS", "GET"],
        csrf=False,
    )
    def trending_validator_protocols(self, **kwargs):
        """Return validator protocols sorted by network APR excluding the user's owned protocols."""
        try:
            if request.httprequest.method == "OPTIONS":
                return oauth_utils.preflight_response()

            user, resp = oauth_utils.require_user()
            if not user:
                return resp
            # user = request.env['res.users'].sudo().search([('id', '=', 11)], limit=1)

            owned_subscriptions = request.env["subscription.subscription"].sudo().search([
                ("customer_name", "=", user.partner_id.id),
                ("subscription_type", "=", "validator"),
                ("active", "=", True),
                ("protocol_id", "!=", False),
            ])
            owned_protocol_ids = set(owned_subscriptions.mapped("protocol_id").ids)

            domain = [
                ("active", "=", True),
                ("is_validator", "=", True),
                ("network_apr", ">", 0),
                ("name", "not ilike", "opn"),
            ]
            if owned_protocol_ids:
                domain.append(("id", "not in", list(owned_protocol_ids)))

            protocols = request.env["protocol.master"].sudo().search(
                domain,
                order="network_apr desc, name asc",
            )

            results: List[dict] = []
            for protocol in protocols:
                results.append(
                    {
                        "id": protocol.id,
                        "protocol_id": protocol.protocol_id,
                        "protocol_name": protocol.name,
                        "network_apr": float(protocol.network_apr or 0.0),
                        "network_types": [network_type.name for network_type in protocol.network_type_ids],
                    }
                )

            return oauth_utils._json_response(
                True,
                {"list": results},
                error="Trending validator protocols fetched successfully.",
            )

        except Exception as exc:  # pragma: no cover - unexpected messages
            _logger.exception("Error in trending_validator_protocols endpoint")
            return oauth_utils._json_response(False, error=str(exc), status=500)

    @http.route(
        "/api/v1/validator/transactions",
        type="http",
        auth="public",
        methods=["POST", "OPTIONS"],
        csrf=False,
    )
    def create_validator_transaction(self, **kwargs):
        """Record an on-chain transaction for a validator subscription."""
        try:
            if request.httprequest.method == "OPTIONS":
                return oauth_utils.preflight_response(["POST"])

            user, resp = oauth_utils.require_user()
            if not user:
                return resp

            identifier = self._normalize_identifier(kwargs.get("subscription_id"), kwargs.get("node_id"))
            if not identifier:
                return oauth_utils._json_response(
                    False,
                    error="subscription_id (legacy node_id) is required",
                    status=400,
                )

            payload = request.httprequest.get_json(force=True, silent=True) or {}

            # -------------------------------
            # Parse and validate payload
            # -------------------------------
            required_fields = ["transaction_hash", "action"]
            is_valid, error_msg = base_utils._validate_payload(payload, required_fields)
            if not is_valid:
                return oauth_utils._json_response(False, {'error': error_msg}, status=400)
            
            transaction_hash = payload.get("transaction_hash")
            action = payload.get("action")
            notes = payload.get("notes")

            subscription, selected_node = self._get_subscription_from_node(identifier)
            if not subscription or subscription.customer_name.id != user.partner_id.id:
                return oauth_utils._json_response(False, error="Subscription not found", status=404)

            tx = request.env["subscription.validator.transaction"].sudo().create({
                "subscription_id": subscription.id,
                "node_id": selected_node.id if selected_node else False,
                "transaction_hash": transaction_hash,
                "action": action,
                "notes": notes,
            })

            response_payload = {
                "id": tx.id,
                "transaction_hash": tx.transaction_hash,
                "action": tx.action,
                "notes": tx.notes,
                "created_at": fields.Datetime.to_string(tx.create_date),
            }
            return oauth_utils._json_response(True, data={"transaction": response_payload})

        except UserError as exc:
            return oauth_utils._json_response(False, error=exc.name or str(exc), status=400)
        except Exception as exc:  # pragma: no cover - unexpected messages
            _logger.exception("Error saving validator transaction for subscription identifier %s", identifier)
            return oauth_utils._json_response(False, error=str(exc), status=500)

    @http.route(
        "/api/v1/details/node",
        type="http",
        auth="none",
        methods=["OPTIONS", "GET"],
        csrf=False,
    )
    def node_details(self, **kwargs):
        """Return details of a specific purchased subscription."""
        try:
            if request.httprequest.method == "OPTIONS":
                return oauth_utils.preflight_response(["GET"])

            user, resp = oauth_utils.require_user()
            if not user:
                return resp
            # -------------------------------
            # Parse and validate payload
            # -------------------------------
            node_id = self._normalize_identifier(kwargs.get("subscription_id"), kwargs.get("node_id"))
            if not node_id:
                return oauth_utils._json_response(
                    False,
                    error="subscription_id (legacy node_id) is required",
                    status=400,
                )

            subscription, selected_node = self._get_subscription_from_node(node_id)
            
            # Check if user belongs to access_rights.group_admin (admin users can view any subscription)
            is_admin_user = user.has_group('access_rights.group_admin')
            
            if not subscription:
                return oauth_utils._json_response(False, error="Subscription not found", status=404)
            
            # Allow access if user is internal or owns the subscription
            if not is_admin_user and subscription.customer_name.id != user.partner_id.id:
                return oauth_utils._json_response(False, error="Subscription not found", status=404)

            # Granular access check for operators
            from ...access_rights.utils.access_manager import AccessManager
            node_type_domain = AccessManager.get_node_type_domain(user)
            if node_type_domain:
                access_allowed = request.env['subscription.node'].sudo().search_count([
                    ('id', '=', selected_node.id),
                ] + node_type_domain)
                if not access_allowed:
                    return oauth_utils._json_response(False, error="Permission denied", status=403)
            
            base_url = oauth_utils._get_image_url()
            protocol = subscription.protocol_id
            icon_url = (
                    f"{base_url}?model=protocol.master&id={protocol.id}&field=image"
                    if protocol.image
                    else False
                )
            logo = protocol.image.decode() if protocol and protocol.image else False
            metaData = json.loads(selected_node.metadata_json) if selected_node.metadata_json else {}
            validator_info = json.loads(selected_node.validator_info) if selected_node.validator_info else {}

            data = {
                "node_name": selected_node.node_name,
                "protocol_name": protocol.name if protocol else "",
                "status": selected_node.state,
                "next_billing_date": fields.Datetime.to_string(subscription.next_payment_date)
                if subscription.next_payment_date
                else False,
                "created_on": fields.Datetime.to_string(subscription.subscribed_on or subscription.create_date),
                "protocol_id": protocol.protocol_id if protocol else False,
                "protocol_logo": icon_url,
                "subscription_id": subscription.subscription_uuid,
                "node_id": selected_node.node_identifier,
                "plan_name": subscription.sub_plan_id.name if subscription.sub_plan_id else "",
                "payment_frequency": subscription.payment_frequency,
                "price": subscription.price,
                "duration": subscription.duration,
                "unit": subscription.unit,
                "isVisionOnboarded": selected_node.is_vision_onboarded,
                "network_type": selected_node.network_selection_id.name if selected_node.network_selection_id else "",
                "endpoint":metaData.get("endpoint",""),
                "api_key": metaData.get("api_key",""),
                "agent_id":metaData.get("agent_id",""),
                "validator_info": validator_info,
                "metadata": metaData
            }

            return oauth_utils._json_response(True, data)

        except Exception as exc:  # pragma: no cover - unexpected messages
            return oauth_utils._json_response(False, error=str(exc), status=500)
    
    @http.route(
        "/api/v1/delete/node",
        type="http",
        auth="none",
        methods=["OPTIONS", "GET"],
        csrf=False,
    )
    def delete_nodes(self, **kwargs):
        """Delete purchased subscription filtered by node type and protocolId.

        A valid ``Authorization`` header with the session SID must be provided
        (``Bearer <sid>``).
            """
        try:
            if request.httprequest.method == "OPTIONS":
                return oauth_utils.preflight_response()

            user, token_err = oauth_utils._user_from_token()
            if not user:
                messages = {
                    "missing": "Missing access token",
                    "expired": "Access token expired",
                    "invalid": "Invalid access token",
                }
                return oauth_utils._json_response(
                    False,
                    error=messages.get(token_err, "Invalid access token"),
                    status=401,
                )

            request.update_env(user=user.id)

            # -------------------------------
            # Parse and validate payload
            # -------------------------------
            required_fields = ["node_type"]
            is_valid, error_msg = base_utils._validate_payload(kwargs, required_fields)
            if not is_valid:
                return oauth_utils._json_response(False, {'error': error_msg}, status=400)

            node_map = {"RPC": "rpc", "Archive": "archive", "Validator": "validator"}
            node_type = kwargs.get("node_type")
            sub_type = node_map.get(node_type)
            node_identifier = self._normalize_identifier(kwargs.get("subscription_id"), kwargs.get("node_id"))
            if not node_identifier:
                return oauth_utils._json_response(
                    False,
                    error="subscription_id (legacy node_id) is required",
                    status=400,
                )

            subscription, _node = self._get_subscription_from_node(node_identifier)
            if not subscription or subscription.customer_name.id != user.partner_id.id:
                return oauth_utils._json_response(False, error="Subscription not found", status=404)
            if sub_type and subscription.subscription_type != sub_type:
                return oauth_utils._json_response(False, error="Subscription type mismatch", status=400)

            subscription.write({"active": False, "state": "ended"})
            results = {
                "subscription_id": subscription.subscription_uuid,
                "node_id": node_identifier,
            }

            return oauth_utils._json_response(True, results,error="Subscription deleted successfully")
        except Exception as exc:  # pragma: no cover - unexpected messages
            return oauth_utils._json_response(False, error=str(exc), status=500)

    def _validator_subscription_overview(self, user, node_id_param: Optional[str], cursor_param: Optional[str]):
        node_id = self._normalize_identifier(node_id_param)
        if not node_id:
            resp = oauth_utils._json_response(False, error="subscription_id (legacy node_id) is required")
            resp.status_code = 400
            return resp

        subscription, selected_node = self._get_subscription_from_node(node_id)
        
        # Check if user belongs to access_rights.group_admin (admin users can view any subscription)
        is_admin_user = user.has_group('access_rights.group_admin')
        
        if not subscription or subscription.subscription_type != "validator":
            resp = oauth_utils._json_response(False, error="Subscription not found")
            resp.status_code = 404
            return resp
        
        # Allow access if user is internal or owns the subscription
        if not is_admin_user and subscription.customer_name.id != user.partner_id.id:
            resp = oauth_utils._json_response(False, error="Subscription not found")
            resp.status_code = 404
            return resp

        # Granular access check for operators
        from ...access_rights.utils.access_manager import AccessManager
        node_type_domain = AccessManager.get_node_type_domain(user)
        if node_type_domain:
            access_allowed = request.env['subscription.node'].sudo().search_count([
                ('id', '=', selected_node.id),
            ] + node_type_domain)
            if not access_allowed:
                resp = oauth_utils._json_response(False, error="Permission denied")
                resp.status_code = 403
                return resp

        validator_info, validator_address = _extract_validator_address(selected_node)
        delegation_address = _extract_delegation_address(validator_info)
        if not validator_address:
            resp = oauth_utils._json_response(False, error="Validator address not configured")
            resp.status_code = 400
            return resp

        protocol = subscription.protocol_id
        if not protocol:
            resp = oauth_utils._json_response(False, error="Protocol not configured for subscription")
            resp.status_code = 400
            return resp

        network_selection = selected_node.network_selection_id if selected_node else False
        network_label = (network_selection.name or "").strip() if network_selection else ""
        network_name = network_label.lower()
        if network_name == "testnet":
            rpc_base_url = (protocol.web_url_testnet or "").strip()
        else:
            rpc_base_url = (protocol.web_url or "").strip()
        if rpc_base_url:
            rpc_base_url = rpc_base_url.rstrip("/")

        resolved_protocol_name = (protocol.name or "").strip()
        protocol_key = _normalize_protocol_name(resolved_protocol_name)

        if protocol_key == "coreum" and not _is_valoper_address(validator_address):
            resp = oauth_utils._json_response(False, error="Invalid Coreum validator address")
            resp.status_code = 400
            return resp

        if not rpc_base_url:
            resp = oauth_utils._json_response(False, error="Protocol RPC endpoint is not configured")
            resp.status_code = 400
            return resp

        flow_context = None
        if protocol_key == "flow":
            flow_context = {
                "owner_address": _flow_extract_owner_address(validator_info),
                "network": network_label or network_name,
            }

        try:
            summary = _compute_validator_summary(
                validator_address,
                protocol_key,
                rpc_base_url,
                flow_context=flow_context,
                delegation_address=delegation_address,
            )
            network_type = network_selection.name if network_selection else None
            summary["networkType"] = network_type
            delegations = _compute_validator_delegations(
                validator_address,
                protocol_key,
                rpc_base_url,
                cursor=cursor_param,
                flow_context=flow_context,
            )
        except LCDRequestError as exc:
            resp = oauth_utils._json_response(False, error=str(exc))
            resp.status_code = exc.status or 502
            return resp
        except Exception as exc:  # pragma: no cover - unexpected messages
            _logger.exception(
                "Unexpected error fetching validator overview",
                extra={
                    "node_id": node_id,
                    "subscription_id": subscription.id,
                    "protocol_key": protocol_key,
                },
            )
            resp = oauth_utils._json_response(False, error="Unexpected error fetching validator overview")
            resp.status_code = 500
            return resp

        response_payload = {
            "summary": summary,
            "delegations": delegations,
        }

        if protocol_key == "near":
            _logger.info(
                "NEAR validator overview response node_id=%s subscription_id=%s summary=%s delegations=%s",
                node_id,
                subscription.id,
                json.dumps(summary, default=str),
                json.dumps(delegations, default=str),
            )

        return oauth_utils._json_response(
            True,
            response_payload,
            error="Validator overview fetched successfully.",
        )

    @http.route(
        "/api/v1/subscriptions/summary",
        type="http",
        auth="none",
        methods=["OPTIONS", "GET"],
        csrf=False,
    )
    def subscription_summary(self, **kwargs):
        """Return subscription summary or validator overview when nodeId is provided."""

        try:
            if request.httprequest.method == "OPTIONS":
                return oauth_utils.preflight_response()

            user, resp = oauth_utils.require_user()
            if not user:
                return resp
            # user = request.env['res.users'].sudo().search([('id', '=', 10)], limit=1)


            node_id_param = (
                request.params.get("nodeId")
                or request.params.get("node_id")
                or kwargs.get("nodeId")
                or kwargs.get("node_id")
            )
            cursor_param = request.params.get("cursor") or kwargs.get("cursor")
            if node_id_param:
                return self._validator_subscription_overview(user, node_id_param, cursor_param)

            # -------------------------------
            # Parse and validate payload
            # -------------------------------
            required_fields = ["node_type"]
            is_valid, error_msg = base_utils._validate_payload(kwargs, required_fields)
            if not is_valid:
                return oauth_utils._json_response(False, {'error': error_msg}, status=400)

            subs = request.env["subscription.subscription"].sudo().search(
                [("customer_name", "=", user.partner_id.id), ("active", "=", True)],
                order="create_date desc",
            )

            data: List[dict] = []

            for sub in subs:
                plan = sub.sub_plan_id
                protocol = sub.protocol_id
                data.append(
                    {
                        "protocolType": sub.subscription_type or "",
                        "protocol_name": protocol.name if protocol else "",
                        "subscribe_more_url": "",
                        "update_subscription_url": "",
                        "subscription_details": {
                            "subscriptionId": sub.id,
                            "subscriptionStatus": sub.state,
                            "subscriptionModifiable": sub.state not in ("ended", "closed"),
                            "plan": {
                                "plan_code": plan.name if plan else "",
                                "total_quantity": sub.quantity or 0,
                                "available_quantity": sub.quantity or 0,
                            },
                            "addons": [],
                            "amount": sub.price or 0.0,
                            "next_billing_date": fields.Datetime.to_string(sub.next_payment_date)
                            if sub.next_payment_date
                            else "",
                        },
                    }
                )

            return oauth_utils._json_response(True, data=data)

        except Exception as exc:  # pragma: no cover - unexpected messages
            return oauth_utils._json_response(False, error=str(exc),status=500)


    @http.route(
        "/api/v1/validator/<string:valoper>/delegations",
        type="http",
        auth="none",
        methods=["OPTIONS", "GET"],
        csrf=False,
    )
    def validator_delegations(self, valoper, **kwargs):
        """Return delegators list streamed from Coreum LCD."""

        try:
            if request.httprequest.method == "OPTIONS":
                return oauth_utils.preflight_response()

            user, resp = oauth_utils.require_user()
            if not user:
                return resp

            protocol_id = (
                request.params.get("protocol_id")
                or request.params.get("protocolId")
                or kwargs.get("protocol_id")
                or kwargs.get("protocolId")
            )
            protocol_name = (
                request.params.get("protocol_name")
                or request.params.get("protocolName")
                or kwargs.get("protocol_name")
                or kwargs.get("protocolName")
            )

            if not protocol_id and not protocol_name:
                resp = oauth_utils._json_response(False, error="Protocol information is required")
                resp.status_code = 400
                return resp

            protocol_record, rpc_base_url = _resolve_protocol_rpc_url(protocol_id, protocol_name)
            if not protocol_record:
                resp = oauth_utils._json_response(False, error="Protocol not found")
                resp.status_code = 404
                return resp

            resolved_protocol_name = (protocol_record.name or protocol_name or "").strip()
            protocol_key = _normalize_protocol_name(resolved_protocol_name)

            if not rpc_base_url:
                resp = oauth_utils._json_response(
                    False, error="Protocol RPC endpoint is not configured"
                )
                resp.status_code = 400
                return resp

            if not valoper:
                resp = oauth_utils._json_response(False, error="Validator identifier is required")
                resp.status_code = 400
                return resp

            if protocol_key == "coreum" and not _is_valoper_address(valoper):
                resp = oauth_utils._json_response(False, error="Invalid Coreum validator address")
                resp.status_code = 400
                return resp

            cursor_param = request.params.get("cursor") or kwargs.get("cursor")
            flow_context = None
            if protocol_key == "flow":
                owner_param = (
                    request.params.get("owner_address")
                    or request.params.get("ownerAddress")
                    or kwargs.get("owner_address")
                    or kwargs.get("ownerAddress")
                )
                network_param = (
                    request.params.get("network")
                    or request.params.get("network_type")
                    or request.params.get("networkType")
                    or kwargs.get("network")
                    or kwargs.get("network_type")
                    or kwargs.get("networkType")
                )
                owner_value = _flow_normalize_owner_address(owner_param if isinstance(owner_param, str) else None)
                network_value = network_param.strip() if isinstance(network_param, str) else network_param
                flow_context = {
                    "owner_address": owner_value,
                    "network": network_value,
                }

            delegations = _compute_validator_delegations(
                valoper,
                protocol_key,
                rpc_base_url,
                cursor=cursor_param,
                flow_context=flow_context,
            )

            return oauth_utils._json_response(True, delegations)

        except LCDRequestError as exc:
            # _logger.warning("Validator delegations LCD error", extra={"valoper": valoper, "error": str(exc)})
            resp = oauth_utils._json_response(False, error=str(exc))
            resp.status_code = exc.status or 502
            return resp
        except Exception as exc:  # pragma: no cover - unexpected messages
            _logger.exception("Unexpected error fetching delegations", extra={"valoper": valoper})
            resp = oauth_utils._json_response(False, error="Unexpected error fetching delegations")
            resp.status_code = 500
            return resp

    @http.route(
        "/api/v1/validator/delegation",
        type="http",
        auth="none",
        methods=["OPTIONS", "GET"],
        csrf=False,
    )
    def solana_validator_delegation(self, **kwargs):
        """Return paginated Solana/Cosmos delegator list for a validator resolved via nodeId."""

        try:
            if request.httprequest.method == "OPTIONS":
                return oauth_utils.preflight_response()

            user, resp = oauth_utils.require_user()
            if not user:
                return resp
            # user= request.env['res.users'].sudo().search([('id', '=', 10)], limit=1)

            node_id_param = (
                request.params.get("nodeId")
                or request.params.get("node_id")
                or kwargs.get("nodeId")
                or kwargs.get("node_id")
            )
            if not node_id_param:
                resp = oauth_utils._json_response(False, error="nodeId is required")
                resp.status_code = 400
                return resp

            subscription, selected_node = self._get_subscription_from_node(
                self._normalize_identifier(node_id_param)
            )

            is_admin_user = user.has_group('access_rights.group_admin')

            if not subscription or subscription.subscription_type != "validator":
                resp = oauth_utils._json_response(False, error="Subscription not found")
                resp.status_code = 404
                return resp

            if not is_admin_user and subscription.customer_name.id != user.partner_id.id:
                resp = oauth_utils._json_response(False, error="Subscription not found")
                resp.status_code = 404
                return resp

            validator_info, validator_address = _extract_validator_address(selected_node)
            if not validator_address:
                resp = oauth_utils._json_response(False, error="Validator address not configured")
                resp.status_code = 400
                return resp

            protocol = subscription.protocol_id
            if not protocol:
                resp = oauth_utils._json_response(False, error="Protocol not configured for subscription")
                resp.status_code = 400
                return resp

            resolved_protocol_name = (protocol.name or "").strip()
            protocol_key = _normalize_protocol_name(resolved_protocol_name)

            if protocol_key not in {"solana", "cosmos"}:
                resp = oauth_utils._json_response(False, error="This endpoint is only available for Solana and Cosmos validators")
                resp.status_code = 400
                return resp

            if protocol_key == "cosmos" and not _is_valoper_address(validator_address):
                resp = oauth_utils._json_response(False, error="Invalid Cosmos validator address")
                resp.status_code = 400
                return resp

            network_selection = selected_node.network_selection_id if selected_node else False
            network_name = ((network_selection.name or "").strip().lower()) if network_selection else ""
            if network_name == "testnet":
                rpc_base_url = (protocol.web_url_testnet or "").strip()
            else:
                rpc_base_url = (protocol.web_url or "").strip()
            if rpc_base_url:
                rpc_base_url = rpc_base_url.rstrip("/")

            if not rpc_base_url:
                resp = oauth_utils._json_response(False, error="Protocol RPC endpoint is not configured")
                resp.status_code = 400
                return resp

            try:
                page = max(int(request.params.get("page") or kwargs.get("page") or 1), 1)
            except (TypeError, ValueError):
                page = 1
            try:
                limit = max(int(request.params.get("limit") or kwargs.get("limit") or 20), 1)
            except (TypeError, ValueError):
                limit = 20
            limit = min(limit, 100)

            delegations = _validator_delegations_page(
                validator_address,
                protocol_key,
                rpc_base_url,
                page=page,
                limit=limit,
            )

            return oauth_utils._json_response(True, delegations)

        except LCDRequestError as exc:
            resp = oauth_utils._json_response(False, error=str(exc))
            resp.status_code = exc.status or 502
            return resp
        except Exception as exc:
            _logger.exception("Unexpected error fetching validator delegations")
            resp = oauth_utils._json_response(False, error="Unexpected error fetching delegations")
            resp.status_code = 500
            return resp

    @http.route(
        "/api/v1/account/transactions",
        type="http",
        auth="none",
        methods=["OPTIONS", "GET"],
        csrf=False,
    )
    def account_transactions(self, **kwargs):
        """Return Theta account transactions for validator dashboards."""

        try:
            if request.httprequest.method == "OPTIONS":
                return oauth_utils.preflight_response()

            user, resp = oauth_utils.require_user()
            if not user:
                return resp
            # user = request.env['res.users'].sudo().search([('id', '=', 10)], limit=1)
            node_id_param = (
                request.params.get("nodeId")
                or request.params.get("node_id")
                or kwargs.get("nodeId")
                or kwargs.get("node_id")
            )
            # address_param = (
            #     request.params.get("address")
            #     or request.params.get("accountAddress")
            #     or request.params.get("wallet")
            #     or kwargs.get("address")
            #     or kwargs.get("accountAddress")
            #     or kwargs.get("wallet")
            # )
            protocol_id_param = (
                request.params.get("protocol_id")
                or request.params.get("protocolId")
                or kwargs.get("protocol_id")
                or kwargs.get("protocolId")
            )
            protocol_name_param = (
                request.params.get("protocol_name")
                or request.params.get("protocolName")
                or kwargs.get("protocol_name")
                or kwargs.get("protocolName")
            )

            subscription = None
            selected_node = None
            base_url = None
            protocol_key = None

            if node_id_param:
                subscription, selected_node = self._get_subscription_from_node(node_id_param)

                is_admin_user = user.has_group('access_rights.group_admin')

                if not subscription or subscription.subscription_type != "validator":
                    resp = oauth_utils._json_response(False, error="Subscription not found")
                    resp.status_code = 404
                    return resp

                if not is_admin_user and subscription.customer_name.id != user.partner_id.id:
                    resp = oauth_utils._json_response(False, error="Subscription not found")
                    resp.status_code = 404
                    return resp

                protocol = subscription.protocol_id
                if not protocol:
                    resp = oauth_utils._json_response(False, error="Protocol not configured for subscription")
                    resp.status_code = 400
                    return resp

                protocol_key = _normalize_protocol_name(protocol.name)

                network_selection = selected_node.network_selection_id if selected_node else False
                network_label = (network_selection.name or "").strip().lower() if network_selection else ""
                if network_label == "testnet":
                    base_url = (protocol.web_url_testnet or "").strip()
                else:
                    base_url = (protocol.web_url or "").strip()

                if base_url:
                    base_url = base_url.rstrip("/")

                _validator_info, validator_address = _extract_validator_address(selected_node)
                address_param = validator_address

            else:
                if not protocol_id_param and not protocol_name_param:
                    resp = oauth_utils._json_response(False, error="protocol_id or protocol_name is required")
                    resp.status_code = 400
                    return resp

                protocol_record, base_url = _resolve_protocol_rpc_url(protocol_id_param, protocol_name_param)
                if not protocol_record:
                    resp = oauth_utils._json_response(False, error="Protocol not found")
                    resp.status_code = 404
                    return resp

                protocol_key = _normalize_protocol_name(protocol_record.name or protocol_name_param)

                if base_url:
                    base_url = base_url.rstrip("/")

            if protocol_key != "theta":
                resp = oauth_utils._json_response(False, error="Account transactions are only supported for Theta")
                resp.status_code = 400
                return resp

            if not base_url:
                resp = oauth_utils._json_response(False, error="Protocol RPC endpoint is not configured")
                resp.status_code = 400
                return resp

            address_value = (address_param or "").strip()
            if not address_value:
                resp = oauth_utils._json_response(False, error="Account address is required")
                resp.status_code = 400
                return resp

            def _as_int(value: Any, default: int) -> int:
                try:
                    return int(value)
                except (TypeError, ValueError):
                    return default

            def _normalize_types(raw: Any) -> Optional[List[str]]:
                if raw is None:
                    return None
                if isinstance(raw, (list, tuple)):
                    values = [str(item).strip() for item in raw if str(item).strip()]
                    return values or None
                if isinstance(raw, str):
                    trimmed = raw.strip()
                    if not trimmed:
                        return None
                    try:
                        parsed = json.loads(trimmed)
                        if isinstance(parsed, (list, tuple)):
                            values = [str(item).strip() for item in parsed if str(item).strip()]
                            return values or None
                    except json.JSONDecodeError:
                        pass
                    if "," in trimmed:
                        values = [part.strip() for part in trimmed.split(",") if part.strip()]
                        return values or None
                    if trimmed.startswith("[") and trimmed.endswith("]"):
                        inner = trimmed[1:-1]
                        values = [part.strip().strip('"').strip("'") for part in inner.split(",") if part.strip()]
                        return values or None
                    return [trimmed]
                return [str(raw)]

            type_param = _as_int(request.params.get("type") or kwargs.get("type"), -1)
            page_number = max(_as_int(request.params.get("pageNumber") or kwargs.get("pageNumber"), 1), 1)
            limit_number = max(_as_int(request.params.get("limitNumber") or kwargs.get("limitNumber"), 20), 1)
            raw_is_equal = request.params.get("isEqualType") or kwargs.get("isEqualType")
            is_equal_type = True
            if raw_is_equal is not None:
                if isinstance(raw_is_equal, bool):
                    is_equal_type = raw_is_equal
                elif isinstance(raw_is_equal, str):
                    is_equal_type = raw_is_equal.strip().lower() in ("1", "true", "yes", "y")
                else:
                    is_equal_type = bool(raw_is_equal)

            types_param = request.params.get("types") or kwargs.get("types")
            types_list = _normalize_types(types_param)
            if types_list is None:
                types_list = ["0", "2", "8", "9"]

            query_params: Dict[str, Any] = {
                "type": type_param,
                "pageNumber": page_number,
                "limitNumber": limit_number,
                "isEqualType": str(is_equal_type).lower(),
            }
            if types_list:
                query_params["types"] = types_list

            payload = _theta_fetch_account_transactions(
                address_value,
                base_url,
                query_params,
            )

            return oauth_utils._json_response(
                True,
                payload,
                error="Account transactions fetched successfully.",
            )

        except LCDRequestError as exc:
            resp = oauth_utils._json_response(False, error=str(exc))
            resp.status_code = exc.status or 502
            return resp
        except Exception as exc:  # pragma: no cover - unexpected messages
            _logger.exception(
                "Unexpected error fetching account transactions",
                extra={
                    "node_id": node_id_param,
                    "protocol_id": protocol_id_param,
                },
            )
            resp = oauth_utils._json_response(False, error="Unexpected error fetching account transactions")
            resp.status_code = 500
            return resp

    @http.route(
        "/api/v1/validator/network/<string:network_id>/info",
        type="http",
        auth="none",
        methods=["OPTIONS", "GET"],
        csrf=False,
    )
    def validator_network_info(self, network_id, **kwargs):
        """Return validator metadata (network type, valoper) for the given subscription network."""

        try:
            # _logger.info("hello from validator_network_info")
            if request.httprequest.method == "OPTIONS":
                return oauth_utils.preflight_response()

            user, resp = oauth_utils.require_user()
            if not user:
                return resp

            # _logger.info(
                # "validator_network_info request",
                # extra={
                    # "network_id": network_id,
                    # "user_id": user.id,
                    # "partner_id": user.partner_id.id,
                # },
            # )

            subscription, selected_node = self._get_subscription_from_node(network_id)

            if (
                not subscription
                or subscription.subscription_type != "validator"
                or subscription.customer_name.id != user.partner_id.id
            ):
                # _logger.warning(
                    # "validator_network_info subscription not found",
                    # extra={
                        # "network_id": network_id,
                        # "partner_id": user.partner_id.id,
                    # },
                # )
                resp = oauth_utils._json_response(False, error="Subscription not found")
                resp.status_code = 404
                return resp

            raw_info = (selected_node.validator_info or "").strip()
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
                or validator_info.get("wallet")
                or validator_info.get("address")
            )

            target_node = selected_node or subscription.get_primary_node()
            network_type = None
            network_code = None
            if target_node and target_node.network_selection_id:
                network_type = target_node.network_selection_id.name
                network_code = target_node.network_selection_id.code

            response_payload = {
                "networkId": target_node.node_identifier if target_node else network_id,
                "networkType": network_type,
                "networkTypeCode": network_code,
                "subscriptionType": subscription.subscription_type,
                "validatorAddress": validator_address,
                "validatorInfo": validator_info,
            }

            # _logger.info(
                # "validator_network_info response",
                # extra={
                    # "network_id": network_id,
                    # "subscription_id": subscription.id,
                    # "has_validator_address": bool(validator_address),
                # },
            # )

            return oauth_utils._json_response(
                True,
                response_payload,
                error="Validator metadata fetched successfully.",
            )

        except Exception as exc:  # pragma: no cover - unexpected messages
            _logger.exception("Unexpected error fetching validator info", extra={"network_id": network_id})
            resp = oauth_utils._json_response(False, error="Unexpected error fetching validator metadata")
            resp.status_code = 500
            return resp


    @http.route(
        "/api/v1/update/validator-info",
        type="http",
        auth="none",
        methods=["OPTIONS", "POST"],
        csrf=False,
    )
    def update_validator_info(self, **kwargs):
        """Update validator information .

        A valid ``Authorization`` header with the session SID must be provided
        (``Bearer <sid>``).
        """
        try:
            if request.httprequest.method == "OPTIONS":
                return oauth_utils.preflight_response(["POST"])
            # -------------------------------
            # Authenticate User
            # -------------------------------
            user, resp = oauth_utils.require_user()
            if not user:
                return resp
            
            # -------------------------------
            # Parse and validate payload
            # -------------------------------

            payload = request.httprequest.get_json(force=True, silent=True) or {}
            if not isinstance(payload, dict):
                return oauth_utils._json_response(False, error="Invalid JSON payload.", status=400)

            required_fields = ["validatorInfo"]
            is_valid, error_message = base_utils._validate_payload(payload, required_fields)
            if not is_valid:
                return oauth_utils._json_response(False, error=error_message, status=400)
            
            # -------------------------------
            # Fetch and Update Subscription
            # -------------------------------
            subscription_identifier = self._normalize_identifier(payload.get("subscription_id"), payload.get("nodeId"))
            if not subscription_identifier:
                return oauth_utils._json_response(
                    False,
                    error="subscription_id (legacy node_id) is required",
                    status=400,
                )
            new_info = payload.get("validatorInfo")
            subscription, node = self._get_subscription_from_node(subscription_identifier)
            if not subscription or subscription.customer_name.id != user.partner_id.id:
                return oauth_utils._json_response(False, error="Subscription not found.", status=404)
            if subscription.subscription_type!="validator":
                return oauth_utils._json_response(False, error="Invalid subscription type.", status=400)
            
            protocol_name = subscription.protocol_id.name if subscription.protocol_id else None
            base_url = request.env['ir.config_parameter'].sudo().get_param('backend_url', '')
            existing_info = {}
            if node.validator_info:
                try:
                    existing_info = json.loads(node.validator_info)
                except Exception:
                    existing_info = {}

            merged_info = {**existing_info, **new_info}

            node.sudo().write({
                "validator_info": json.dumps(merged_info)
            })
            
            # Prepare updated fields for email notification
            updated_fields = []
            for key, value in new_info.items():
                updated_fields.append({
                    'field_name': key.replace('_', ' ').title(),
                    'field_value': str(value)
                })
            
            # Send success notification email
            try:
                send_validator_staking_notification(
                    request.env,
                    node,
                    action_type='staking',
                    status='success',
                    protocol_name=protocol_name,
                    updated_fields=updated_fields,
                    base_url=base_url,
                )
            except Exception as mail_error:
                _logger.warning("Failed to send validator update success notification email: %s", str(mail_error))

            return oauth_utils._json_response(True, error="Subscription updated successfully.",)

        except Exception as exc:  # pragma: no cover - unexpected messages
            return oauth_utils._json_response(False, error=str(exc), status=500)

    @http.route(
        "/api/v1/validator/performance",
        type="http",
        auth="none",
        methods=["OPTIONS", "GET"],
        csrf=False,
    )
    def validator_performance(self, **kwargs):
        """Return validator performance data for a specified period.
        
        Query Parameters:
            nodeId: Node identifier (required)
            period: Number of days (1, 7, or 30) - default 7
        """
        try:
            if request.httprequest.method == "OPTIONS":
                return oauth_utils.preflight_response()

            user, resp = oauth_utils.require_user()
            if not user:
                return resp
            # user=request.env['res.users'].sudo().search([('id','=',10)],limit=1)

            # Parse parameters
            node_id_param = (
                request.params.get("nodeId")
                or request.params.get("node_id")
                or kwargs.get("nodeId")
                or kwargs.get("node_id")
            )
            
            if not node_id_param:
                return oauth_utils._json_response(
                    False,
                    error="nodeId parameter is required",
                    status=400,
                )
            
            # node_id = self._normalize_identifier(node_id_param)
            
            # Parse and validate period parameter
            period_param = (
                request.params.get("period")
                or kwargs.get("period")
                or "7"
            )
            
            try:
                period = int(period_param)
            except (TypeError, ValueError):
                return oauth_utils._json_response(
                    False,
                    error="Invalid period parameter. Must be 1, 7, or 30",
                    status=400,
                )
            
            # Validate period is one of the allowed values
            if period not in (1, 7, 30):
                return oauth_utils._json_response(
                    False,
                    error="Period must be 1, 7, or 30 days",
                    status=400,
                )

            # Get subscription and node
            subscription, selected_node = self._get_subscription_from_node(node_id_param)
            
            # Check if user belongs to access_rights.group_admin
            is_admin_user = user.has_group('access_rights.group_admin')
            
            if not subscription or subscription.subscription_type != "validator":
                return oauth_utils._json_response(
                    False,
                    error="Validator subscription not found",
                    status=404,
                )
            
            # Allow access if user is admin or owns the subscription
            if not is_admin_user and subscription.customer_name.id != user.partner_id.id:
                return oauth_utils._json_response(
                    False,
                    error="Validator subscription not found",
                    status=404,
                )

            # Extract validator address
            validator_info, validator_address = _extract_validator_address(selected_node)
            if not validator_address:
                return oauth_utils._json_response(
                    False,
                    error="Validator address not configured",
                    status=400,
                )

            # Get protocol and check if it's Coreum
            protocol = subscription.protocol_id
            if not protocol:
                return oauth_utils._json_response(
                    False,
                    error="Protocol not configured for subscription",
                    status=400,
                )

            resolved_protocol_name = (protocol.name or "").strip()
            protocol_key = _normalize_protocol_name(resolved_protocol_name)

            if protocol_key not in SUPPORTED_VALIDATOR_PERFORMANCE_PROTOCOLS:
                supported = _format_supported_protocols(SUPPORTED_VALIDATOR_PERFORMANCE_PROTOCOLS)
                return oauth_utils._json_response(
                    False,
                    error=f"Performance data is only available for {supported} validators",
                    status=400,
                )

            # Validate Coreum-specific address format
            if protocol_key == "coreum" and not _is_valoper_address(validator_address):
                return oauth_utils._json_response(
                    False,
                    error="Invalid Coreum validator address",
                    status=400,
                )

            # Get RPC endpoint
            network_selection = selected_node.network_selection_id if selected_node else False
            network_name = (network_selection.name or "").strip().lower() if network_selection else ""
            
            if network_name == "testnet":
                rpc_base_url = (protocol.web_url_testnet or "").strip()
            else:
                rpc_base_url = (protocol.web_url or "").strip()
            
            if rpc_base_url:
                rpc_base_url = rpc_base_url.rstrip("/")

            if not rpc_base_url:
                return oauth_utils._json_response(
                    False,
                    error="Protocol RPC endpoint is not configured",
                    status=400,
                )

            # Fetch performance data
            try:
                _logger.info(
                    "Fetching validator performance")
                performance_data = _fetch_validator_performance_with_period(
                    validator_address,
                    protocol_key,
                    rpc_base_url,
                    period,
                    protocol.id,
                    selected_node.id if selected_node else None,
                )
                
                return oauth_utils._json_response(
                    True,
                    performance_data,
                    error="Performance data fetched successfully",
                )
                
            except Exception as exc:
                _logger.exception(
                    "Unexpected error fetching validator performance",
                    extra={
                        "node_id": node_id_param,
                        "subscription_id": subscription.id,
                        "period": period,
                    },
                )
                return oauth_utils._json_response(
                    False,
                    error="Unexpected error fetching performance data",
                    status=500,
                )

        except Exception as exc:
            _logger.exception("Error in validator_performance endpoint")
            return oauth_utils._json_response(False, error=str(exc), status=500)
    @http.route(
        "/api/v1/validator/rewards",
        type="http",
        auth="none",
        methods=["OPTIONS", "GET"],
        csrf=False,
    )
    def validator_rewards(self, **kwargs):
        """Return validator rewards data for a specified period.
        
        Query Parameters:
            nodeId: Node identifier (required)
            period: Number of days (1, 7, or 30) - default 7
        """
        try:
            if request.httprequest.method == "OPTIONS":
                return oauth_utils.preflight_response()

            user, resp = oauth_utils.require_user()
            if not user:
                return resp
            # user=request.env['res.users'].sudo().search([('id','=',10)],limit=1)
            # Parse parameters
            node_id_param = (
                request.params.get("nodeId")
                or request.params.get("node_id")
                or kwargs.get("nodeId")
                or kwargs.get("node_id")
            )
            
            if not node_id_param:
                return oauth_utils._json_response(
                    False,
                    error="nodeId parameter is required",
                    status=400,
                )
            
            # Parse and validate period parameter
            period_param = (
                request.params.get("period")
                or kwargs.get("period")
                or "7"
            )
            
            try:
                period = int(period_param)
            except (TypeError, ValueError):
                return oauth_utils._json_response(
                    False,
                    error="Invalid period parameter. Must be 1, 7, or 30",
                    status=400,
                )
            
            # Validate period is one of the allowed values
            if period not in (1, 7, 30):
                return oauth_utils._json_response(
                    False,
                    error="Period must be 1, 7, or 30 days",
                    status=400,
                )

            # Get subscription and node
            subscription, selected_node = self._get_subscription_from_node(node_id_param)
            
            # Check if user belongs to access_rights.group_admin
            is_admin_user = user.has_group('access_rights.group_admin')
            
            if not subscription or subscription.subscription_type != "validator":
                return oauth_utils._json_response(
                    False,
                    error="Validator subscription not found",
                    status=404,
                )
            
            # Allow access if user is admin or owns the subscription
            if not is_admin_user and subscription.customer_name.id != user.partner_id.id:
                return oauth_utils._json_response(
                    False,
                    error="Validator subscription not found",
                    status=404,
                )

            # Extract validator address
            validator_info, validator_address = _extract_validator_address(selected_node)
            if not validator_address:
                return oauth_utils._json_response(
                    False,
                    error="Validator address not configured",
                    status=400,
                )

            # Get protocol and check if it's Coreum
            protocol = subscription.protocol_id
            if not protocol:
                return oauth_utils._json_response(
                    False,
                    error="Protocol not configured for subscription",
                    status=400,
                )

            resolved_protocol_name = (protocol.name or "").strip()
            protocol_key = _normalize_protocol_name(resolved_protocol_name)

            if protocol_key not in SUPPORTED_VALIDATOR_HISTORY_PROTOCOLS:
                supported = _format_supported_protocols(SUPPORTED_VALIDATOR_HISTORY_PROTOCOLS)
                return oauth_utils._json_response(
                    False,
                    error=f"Rewards data is only available for {supported} validators",
                    status=400,
                )

            if protocol_key == "coreum" and not _is_valoper_address(validator_address):
                return oauth_utils._json_response(
                    False,
                    error="Invalid Coreum validator address",
                    status=400,
                )

            # Get RPC endpoint
            network_selection = selected_node.network_selection_id if selected_node else False
            network_name = (network_selection.name or "").strip().lower() if network_selection else ""
            
            if network_name == "testnet":
                rpc_base_url = (protocol.web_url_testnet or "").strip()
            else:
                rpc_base_url = (protocol.web_url or "").strip()
            
            if rpc_base_url:
                rpc_base_url = rpc_base_url.rstrip("/")

            if not rpc_base_url:
                return oauth_utils._json_response(
                    False,
                    error="Protocol RPC endpoint is not configured",
                    status=400,
                )

            # Fetch rewards data
            try:
                _logger.info(
                    "Fetching validator rewards for node_id=%s period=%d",
                    node_id_param,
                    period
                )
                rewards_data = _fetch_validator_rewards_with_period(
                    validator_address,
                    protocol.id,
                    period,
                    selected_node.id if selected_node else None,
                )
                
                return oauth_utils._json_response(
                    True,
                    rewards_data,
                    error="Rewards data fetched successfully",
                )
                
            except Exception as exc:
                _logger.exception(
                    "Unexpected error fetching validator rewards",
                    extra={
                        "node_id": node_id_param,
                        "subscription_id": subscription.id,
                        "period": period,
                    },
                )
                return oauth_utils._json_response(
                    False,
                    error="Unexpected error fetching rewards data",
                    status=500,
                )

        except Exception as exc:
            _logger.exception("Error in validator_rewards endpoint")
            return oauth_utils._json_response(False, error=str(exc), status=500)

    @http.route(
        "/api/v1/validator/stake-delegator-chart",
        type="http",
        auth="none",
        methods=["OPTIONS", "GET"],
        csrf=False,
    )
    def validator_stake_delegator_chart(self, **kwargs):
        """Return validator stake (tokens) and delegator count data for a specified period.
        
        Query Parameters:
            nodeId: Node identifier (required)
            period: Number of days (1, 7, or 30) - default 7
        """
        try:
            if request.httprequest.method == "OPTIONS":
                return oauth_utils.preflight_response()

            user, resp = oauth_utils.require_user()
            if not user:
                return resp
            # user=request.env['res.users'].sudo().search([('id','=',10)],limit=1)
            # Parse parameters
            node_id_param = (
                request.params.get("nodeId")
                or request.params.get("node_id")
                or kwargs.get("nodeId")
                or kwargs.get("node_id")
            )
            
            if not node_id_param:
                return oauth_utils._json_response(
                    False,
                    error="nodeId parameter is required",
                    status=400,
                )
            
            # Parse and validate period parameter
            period_param = (
                request.params.get("period")
                or kwargs.get("period")
                or "7"
            )
            
            try:
                period = int(period_param)
            except (TypeError, ValueError):
                return oauth_utils._json_response(
                    False,
                    error="Invalid period parameter. Must be 1, 7, or 30",
                    status=400,
                )
            
            # Validate period is one of the allowed values
            if period not in (1, 7, 30):
                return oauth_utils._json_response(
                    False,
                    error="Period must be 1, 7, or 30 days",
                    status=400,
                )

            # Get subscription and node
            subscription, selected_node = self._get_subscription_from_node(node_id_param)
            
            # Check if user belongs to access_rights.group_admin
            is_admin_user = user.has_group('access_rights.group_admin')
            
            if not subscription or subscription.subscription_type != "validator":
                return oauth_utils._json_response(
                    False,
                    error="Validator subscription not found",
                    status=404,
                )
            
            # Allow access if user is admin or owns the subscription
            if not is_admin_user and subscription.customer_name.id != user.partner_id.id:
                return oauth_utils._json_response(
                    False,
                    error="Validator subscription not found",
                    status=404,
                )

            # Extract validator address
            validator_info, validator_address = _extract_validator_address(selected_node)
            if not validator_address:
                return oauth_utils._json_response(
                    False,
                    error="Validator address not configured",
                    status=400,
                )

            # Get protocol and check if it's Coreum
            protocol = subscription.protocol_id
            if not protocol:
                return oauth_utils._json_response(
                    False,
                    error="Protocol not configured for subscription",
                    status=400,
                )

            resolved_protocol_name = (protocol.name or "").strip()
            protocol_key = _normalize_protocol_name(resolved_protocol_name)

            if protocol_key not in SUPPORTED_VALIDATOR_HISTORY_PROTOCOLS:
                supported = _format_supported_protocols(SUPPORTED_VALIDATOR_HISTORY_PROTOCOLS)
                return oauth_utils._json_response(
                    False,
                    error=f"Stake/delegator data is only available for {supported} validators",
                    status=400,
                )

            if protocol_key == "coreum" and not _is_valoper_address(validator_address):
                return oauth_utils._json_response(
                    False,
                    error="Invalid Coreum validator address",
                    status=400,
                )

            # Get RPC endpoint (not used for this endpoint, but keeping for consistency)
            network_selection = selected_node.network_selection_id if selected_node else False
            network_name = (network_selection.name or "").strip().lower() if network_selection else ""
            
            if network_name == "testnet":
                rpc_base_url = (protocol.web_url_testnet or "").strip()
            else:
                rpc_base_url = (protocol.web_url or "").strip()
            
            if rpc_base_url:
                rpc_base_url = rpc_base_url.rstrip("/")

            if not rpc_base_url:
                return oauth_utils._json_response(
                    False,
                    error="Protocol RPC endpoint is not configured",
                    status=400,
                )

            # Fetch stake and delegator count data
            try:
                _logger.info(
                    "Fetching validator stake/delegator chart for node_id=%s period=%d",
                    node_id_param,
                    period
                )
                stake_delegator_data = _fetch_validator_stake_delegator_with_period(
                    validator_address,
                    protocol.id,
                    period,
                    selected_node.id if selected_node else None,
                )
                
                return oauth_utils._json_response(
                    True,
                    stake_delegator_data,
                    error="Stake/delegator data fetched successfully",
                )
                
            except Exception as exc:
                _logger.exception(
                    "Unexpected error fetching validator stake/delegator data",
                    extra={
                        "node_id": node_id_param,
                        "subscription_id": subscription.id,
                        "period": period,
                    },
                )
                return oauth_utils._json_response(
                    False,
                    error="Unexpected error fetching stake/delegator data",
                    status=500,
                )

        except Exception as exc:
            _logger.exception("Error in validator_stake_delegator_chart endpoint")
            return oauth_utils._json_response(False, error=str(exc), status=500)

    @http.route(
        "/api/v1/validator/stake",
        type="http",
        auth="none",
        methods=["OPTIONS", "GET"],
        csrf=False,
    )
    def validator_stake_distribution(self, **kwargs):
        """Return aggregated validator stake distribution grouped by protocol for pie chart.

        For regular users, only their own validator nodes are included.
        Admin users see all validator nodes across the platform.

        Response payload is designed for a pie chart: each entry contains the
        protocol name, total USD stake, percentage share, and node count.
        """
        try:
            if request.httprequest.method == "OPTIONS":
                return oauth_utils.preflight_response()

            user, resp = oauth_utils.require_user()
            if not user:
                return resp
            # user=request.env['res.users'].sudo().search([('id','=',10)],limit=1)
            is_admin_user = user.has_group('access_rights.group_admin')

            # Build domain to fetch validator nodes
            if is_admin_user:
                domain = [
                    ("node_type", "=", "validator"),
                    ("state", "=", "ready"),
                    ("subscription_id.stripe_status", "in", ["active", "trialing"])
                ]
            else:
                domain = [
                    ("subscription_id.customer_name", "=", user.partner_id.id),
                    ("node_type", "=", "validator"),
                    ("state", "=", "ready"),
                    ("subscription_id.stripe_status", "in", ["active", "trialing"])
                ]

            nodes = request.env["subscription.node"].sudo().search(domain)

            # Collect unique protocols so we can fetch USD prices in one batch
            price_service = TokenPriceService(request.env)
            unique_protocols = []
            seen_protocol_ids: set = set()
            for node in nodes:
                proto = node.subscription_id.protocol_id if node.subscription_id else None
                if proto and proto.id not in seen_protocol_ids:
                    seen_protocol_ids.add(proto.id)
                    unique_protocols.append(proto)

            protocol_prices = price_service.get_prices(unique_protocols) if unique_protocols else {}

            # Accumulate USD stake per normalised protocol key
            protocol_groups: Dict[str, Dict] = {}

            for node in nodes:
                subscription = node.subscription_id
                if not subscription:
                    continue

                protocol = subscription.protocol_id
                if not protocol:
                    continue

                _, valoper_address = _extract_validator_address(node)
                if not valoper_address:
                    continue

                snapshot = request.env['validator.rewards.snapshot'].sudo().search(
                    [('node_id', '=', node.id), ('valoper', '=', valoper_address)],
                    order='snapshot_date desc',
                    limit=1,
                )
                if not snapshot:
                    continue

                usd_price = protocol_prices.get(protocol.id)
                stake_decimals = protocol.stake_decimals or 0

                _, stake_usd = convert_raw_value(snapshot.total_stake, stake_decimals, usd_price)

                # Skip nodes whose stake cannot be converted to USD
                if stake_usd is None:
                    continue

                stake_usd_float = float(stake_usd)

                protocol_key = _normalize_protocol_name(protocol.name or "")
                if not protocol_key:
                    continue

                if protocol_key not in protocol_groups:
                    protocol_groups[protocol_key] = {
                        "protocol_key": protocol_key,
                        "protocol_name": protocol.name or protocol_key.capitalize(),
                        "total_stake_usd": 0.0,
                        "node_count": 0,
                    }

                protocol_groups[protocol_key]["total_stake_usd"] += stake_usd_float
                protocol_groups[protocol_key]["node_count"] += 1

            total_stake_usd = sum(g["total_stake_usd"] for g in protocol_groups.values())

            breakdown = []
            for group in protocol_groups.values():
                percentage = (
                    round(group["total_stake_usd"] / total_stake_usd * 100, 2)
                    if total_stake_usd > 0
                    else 0.0
                )
                breakdown.append({
                    "protocol_key": group["protocol_key"],
                    "protocol_name": group["protocol_name"],
                    "total_stake_usd": round(group["total_stake_usd"], 6),
                    "percentage": percentage,
                    "node_count": group["node_count"],
                })

            # Sort largest slice first — friendlier for pie chart rendering
            breakdown.sort(key=lambda x: x["total_stake_usd"], reverse=True)

            return oauth_utils._json_response(
                True,
                {
                    "breakdown": breakdown,
                    "total_stake_usd": round(total_stake_usd, 6),
                },
                error="Validator stake distribution fetched successfully.",
            )

        except Exception as exc:
            _logger.exception("Error in validator_stake_distribution endpoint")
            return oauth_utils._json_response(False, error=str(exc), status=500)

    @http.route(
        "/api/v1/validator-overview/metrics",
        type="http",
        auth="none",
        methods=["OPTIONS", "GET"],
        csrf=False,
    )
    def validator_overview_metrics(self, **kwargs):
        """Return high-level portfolio metrics for a user's validator fleet.

        Metrics returned:
          - total_validators        : total validator nodes in scope
          - active_validators       : validator nodes that are not jailed
          - total_stake_usd         : cumulative stake in USD (from latest snapshots)
          - total_self_stake_usd    : cumulative self stake in USD (from latest snapshots)
          - total_rewards_usd       : cumulative rewards in USD (from latest snapshots)
          - total_subscription_cost : sum of all paid invoices (all-time) for the user's validator subscriptions
          - net_profit              : total_rewards_usd - total_subscription_cost

        Admin users see all validator nodes; regular users see only their own.
        """
        try:
            if request.httprequest.method == "OPTIONS":
                return oauth_utils.preflight_response()

            user, resp = oauth_utils.require_user()
            if not user:
                return resp
            # user=request.env['res.users'].sudo().search([('id','=',10)],limit=1)

            is_admin_user = user.has_group('access_rights.group_admin')

            # ------------------------------------------------------------------
            # 1. Fetch validator nodes in scope
            # ------------------------------------------------------------------
            if is_admin_user:
                node_domain = [
                    ("node_type", "=", "validator"),
                    ("state", "=", "ready"),
                    ("subscription_id.stripe_status", "in", ["active", "trialing"]),
                ]
                sub_domain = [
                    ("subscription_type", "=", "validator"),
                    ("stripe_status", "in", ["active", "trialing"]),
                ]
            else:
                node_domain = [
                    ("subscription_id.customer_name", "=", user.partner_id.id),
                    ("node_type", "=", "validator"),
                    ("state", "=", "ready"),
                    ("subscription_id.stripe_status", "in", ["active", "trialing"]),
                ]
                sub_domain = [
                    ("customer_name", "=", user.partner_id.id),
                    ("subscription_type", "=", "validator"),
                    ("stripe_status", "in", ["active", "trialing"]),
                ]

            nodes = request.env["subscription.node"].sudo().search(node_domain)

            # ------------------------------------------------------------------
            # 2. Batch-fetch USD prices for all unique protocols
            # ------------------------------------------------------------------
            price_service = TokenPriceService(request.env)
            unique_protocols = []
            seen_protocol_ids: set = set()
            for node in nodes:
                proto = node.subscription_id.protocol_id if node.subscription_id else None
                if proto and proto.id not in seen_protocol_ids:
                    seen_protocol_ids.add(proto.id)
                    unique_protocols.append(proto)

            protocol_prices = price_service.get_prices(unique_protocols) if unique_protocols else {}

            # ------------------------------------------------------------------
            # 3. Compute validator counts + stake/rewards from snapshots
            # ------------------------------------------------------------------
            total_validators = len(nodes)
            active_validators = 0
            total_stake_usd = 0.0
            total_self_stake_usd = 0.0
            total_rewards_usd = 0.0

            for node in nodes:
                # Active = not jailed
                is_jailed = False
                try:
                    if node.validator_info:
                        vi = json.loads(node.validator_info)
                        is_jailed = bool(vi.get("jailed", False))
                except Exception:
                    pass
                if not is_jailed:
                    active_validators += 1

                subscription = node.subscription_id
                if not subscription:
                    continue
                protocol = subscription.protocol_id
                if not protocol:
                    continue

                _, valoper_address = _extract_validator_address(node)
                if not valoper_address:
                    continue

                snapshot = request.env["validator.rewards.snapshot"].sudo().search(
                    [("node_id", "=", node.id), ("valoper", "=", valoper_address)],
                    order="snapshot_date desc",
                    limit=1,
                )
                if not snapshot:
                    continue

                usd_price = protocol_prices.get(protocol.id)
                stake_decimals = protocol.stake_decimals or 0
                reward_decimals = protocol.reward_decimals or 0
                _, stake_usd = convert_raw_value(snapshot.total_stake, stake_decimals, usd_price)
                _, self_stake_usd = convert_raw_value(snapshot.owned_stake or 0.0, stake_decimals, usd_price)
                _, reward_usd = convert_raw_value(snapshot.total_rewards, reward_decimals, usd_price)

                if stake_usd is not None:
                    total_stake_usd += float(stake_usd)
                if self_stake_usd is not None:
                    total_self_stake_usd += float(self_stake_usd)
                if reward_usd is not None:
                    total_rewards_usd += float(reward_usd)

            # ------------------------------------------------------------------
            # 4. Compute total subscription cost from all-time paid invoices
            # ------------------------------------------------------------------
            all_subs = nodes.mapped("subscription_id")
            all_invoices = request.env["account.move"].sudo().search([
                ("subscription_id", "in", all_subs.ids),
                ("move_type", "=", "out_invoice"),
                ("payment_state", "=", "paid"),
            ])
            _logger.info(
                "overview_metrics calculating subscription cost for %d paid invoices across %d ready validator subscriptions",
                len(all_invoices),
                len(all_subs),
            )

            total_subscription_cost = 0.0
            for inv in all_invoices:
                subscription = inv.subscription_id
                latest_node = subscription.get_primary_node() if subscription else request.env["subscription.node"].browse()
                entry_amount = float(inv.amount_total or 0.0)
                total_subscription_cost += entry_amount

                _logger.info(
                    "overview_metrics invoice cost entry invoice_id=%s subscription_id=%s subscription_uuid=%s node_id=%s node_identifier=%s node_name=%s amount_total=%s",
                    inv.id,
                    subscription.id if subscription else None,
                    subscription.subscription_uuid if subscription else None,
                    latest_node.id if latest_node else None,
                    latest_node.node_identifier if latest_node else None,
                    latest_node.node_name if latest_node else None,
                    entry_amount,
                )

            _logger.info(
                "overview_metrics total subscription cost=%s",
                round(total_subscription_cost, 6),
            )

            # ------------------------------------------------------------------
            # 5. Net profit
            # ------------------------------------------------------------------
            net_profit = total_rewards_usd - total_subscription_cost

            return oauth_utils._json_response(
                True,
                {
                    "total_validators": total_validators,
                    "active_validators": active_validators,
                    "total_stake_usd": round(total_stake_usd, 6),
                    "total_self_stake_usd": round(total_self_stake_usd, 6),
                    "total_rewards_usd": round(total_rewards_usd, 6),
                    "total_subscription_cost": round(total_subscription_cost, 6),
                    "net_profit": round(net_profit, 6),
                },
                error="Validator overview metrics fetched successfully.",
            )

        except Exception as exc:
            _logger.exception("Error in validator_overview_metrics endpoint")
            return oauth_utils._json_response(False, error=str(exc), status=500)

    @http.route(
        "/api/v1/validator/profit-trend",
        type="http",
        auth="none",
        methods=["OPTIONS", "GET"],
        csrf=False,
    )
    def validator_profit_trend(self, **kwargs):
        """Return cumulative profit trend series for the Profit Trend chart.

        Query Parameters:
            range: ``1D`` | ``1W`` | ``30D`` | ``ALL``  (default ``1W``)

        Response ``series`` shape::

            [{"label": str, "rewards": float, "subscriptionCost": float}, ...]

        Both ``rewards`` and ``subscriptionCost`` are cumulative USD totals up
        to each bucket timestamp, so both lines only ever trend upward.

        Admin users see aggregate data across all validator nodes on the platform.
        """
        try:
            if request.httprequest.method == "OPTIONS":
                return oauth_utils.preflight_response()

            user, resp = oauth_utils.require_user()
            if not user:
                return resp
            # user=request.env['res.users'].sudo().search([('id','=',10)],limit=1)
            range_param = (
                request.params.get("range")
                or kwargs.get("range")
                or "1W"
            ).upper()


            if range_param not in ("1D", "1W", "30D", "ALL"):
                return oauth_utils._json_response(
                    False,
                    error="range must be one of: 1D, 1W, 30D, ALL",
                    status=400,
                )

            is_admin_user = user.has_group("access_rights.group_admin")

            # ------------------------------------------------------------------
            # 1. Fetch validator subscriptions in scope
            # ------------------------------------------------------------------
            if is_admin_user:
                sub_domain = [
                    ("subscription_type", "=", "validator"),
                    ("stripe_status", "in", ["active", "trialing"]),
                ]
            else:
                sub_domain = [
                    ("customer_name", "=", user.partner_id.id),
                    ("subscription_type", "=", "validator"),
                    ("stripe_status", "in", ["active", "trialing"]),
                ]

            subs = request.env["subscription.subscription"].sudo().search(sub_domain)


            if not subs:
                return oauth_utils._json_response(
                    True,
                    {"series": []},
                    error="No validator subscriptions found",
                )

            # ------------------------------------------------------------------
            # 2. Fetch validator nodes + batch-price all protocols
            # ------------------------------------------------------------------
            nodes = request.env["subscription.node"].sudo().search([
                ("subscription_id", "in", subs.ids),
                ("node_type", "=", "validator"),
                ("state", "=", "ready"),
            ])

           

            price_service = TokenPriceService(request.env)
            unique_protocols = []
            seen_protocol_ids: set = set()
            for node in nodes:
                proto = node.subscription_id.protocol_id if node.subscription_id else None
                if proto and proto.id not in seen_protocol_ids:
                    seen_protocol_ids.add(proto.id)
                    unique_protocols.append(proto)

            protocol_prices = price_service.get_prices(unique_protocols) if unique_protocols else {}

           

            # ------------------------------------------------------------------
            # 3. Build (label_str, cutoff_datetime) pairs for the range
            # ------------------------------------------------------------------
            now = datetime.utcnow()
            bucket_pairs = []  # list of (label: str, cutoff: datetime)

            if range_param == "1D":
                today_midnight = datetime.combine(now.date(), datetime.min.time())
                for h in [0, 4, 8, 12, 16, 20]:
                    t = today_midnight + timedelta(hours=h)
                    bucket_pairs.append((t.strftime("%b %d, %H:%M"), t))
                # last bucket = current moment (captures all data through now)
                bucket_pairs.append((now.strftime("%b %d, %H:%M"), now))

            elif range_param == "1W":
                for i in range(6):
                    t = datetime.combine(
                        (now - timedelta(days=6 - i)).date(), datetime.min.time()
                    )
                    bucket_pairs.append((t.strftime("%b %d"), t))
                bucket_pairs.append((now.strftime("%b %d"), now))

            elif range_param == "30D":
                for i in range(29, -1, -1):
                    t = datetime.combine(
                        (now - timedelta(days=i)).date(), datetime.min.time()
                    )
                    bucket_pairs.append((t.strftime("%b %d"), t))
                bucket_pairs.append((now.strftime("%b %d"), now))

            else:  # ALL — monthly from earliest subscription date
                earliest = None
                for sub in subs:
                    candidate = sub.subscribed_on or sub.stripe_start_date or sub.create_date
                    if candidate:
                        if isinstance(candidate, str):
                            candidate = fields.Datetime.from_string(candidate)
                        if not isinstance(candidate, datetime):
                            candidate = datetime.combine(candidate, datetime.min.time())
                        if earliest is None or candidate < earliest:
                            earliest = candidate
                if earliest is None:
                    earliest = now - timedelta(days=180)

                

                cursor_year, cursor_month = earliest.year, earliest.month
                while True:
                    label_dt = datetime(cursor_year, cursor_month, 1)
                    if cursor_month == 12:
                        next_y, next_m = cursor_year + 1, 1
                    else:
                        next_y, next_m = cursor_year, cursor_month + 1
                    # end of this calendar month
                    end_of_month = datetime(next_y, next_m, 1) - timedelta(seconds=1)
                    # cap at now so the current month shows data through today
                    cutoff_dt = min(end_of_month, now)
                    bucket_pairs.append((label_dt.strftime("%b %Y"), cutoff_dt))
                    if cutoff_dt >= now:
                        break
                    cursor_year, cursor_month = next_y, next_m


            # ------------------------------------------------------------------
            # 4. Pre-fetch all paid customer invoices once (avoid N+1 queries)
            # ------------------------------------------------------------------
            ready_subs = nodes.mapped("subscription_id")
            all_invoices = request.env["account.move"].sudo().search([
                ("subscription_id", "in", ready_subs.ids),
                ("move_type", "=", "out_invoice"),
                ("payment_state", "=", "paid"),
            ])

          

            # ------------------------------------------------------------------
            # 5. Build series — cumulative rewards + cost per bucket
            # ------------------------------------------------------------------
            series = []
            for label, t in bucket_pairs:
                # --- cumulative rewards: latest snapshot per node up to t ---
                rewards_usd = 0.0
                for node in nodes:
                    snap = request.env["validator.rewards.snapshot"].sudo().search(
                        [
                            ("node_id", "=", node.id),
                            ("snapshot_date", "<=", fields.Datetime.to_string(t)),
                        ],
                        order="snapshot_date desc",
                        limit=1,
                    )
                    if not snap:
                        _logger.info(
                            "profit_trend [step 5] bucket=%s: no snapshot found for node_id=%s (skipped)",
                            label,
                            node.id,
                        )
                        continue
                    protocol = node.subscription_id.protocol_id if node.subscription_id else None
                    if not protocol:
                        continue
                    usd_price = protocol_prices.get(protocol.id)
                    reward_decimals = protocol.reward_decimals or 0
                    _, reward_usd = convert_raw_value(
                        snap.total_rewards, reward_decimals, usd_price
                    )
                  
                    if reward_usd is not None:
                        rewards_usd += float(reward_usd)

                # --- cumulative subscription cost: paid invoices up to t ----
                t_date = t.date()
                cost_usd = sum(
                    float(inv.amount_total)
                    for inv in all_invoices
                    if inv.invoice_date and inv.invoice_date <= t_date
                )

                _logger.info(
                    "profit_trend [step 5] bucket=%s cutoff=%s rewards_usd=%s cost_usd=%s",
                    label,
                    t,
                    round(rewards_usd, 2),
                    round(cost_usd, 2),
                )

                series.append({
                    "label": label,
                    "rewards": round(rewards_usd, 2),
                    "subscriptionCost": round(cost_usd, 2),
                })

            _logger.info(
                "profit_trend [complete]: returning %d data points series=%s",
                len(series),
                series,
            )

            return oauth_utils._json_response(
                True,
                {"series": series},
                error="Profit trend data fetched successfully",
            )

        except Exception as exc:
            _logger.exception("Error in validator_profit_trend endpoint")
            return oauth_utils._json_response(False, error=str(exc), status=500)
        
    @http.route(
        "/api/v1/validator/stake-history",
        type="http",
        auth="none",
        methods=["OPTIONS", "GET"],
        csrf=False,
    )
    def validator_stake_history(self, **kwargs):
        """Return total stake (USD) over time for the Stake History chart.

        Query Parameters:
            range: ``1D`` | ``1W`` | ``30D`` | ``ALL``  (default ``1W``)

        Response ``series`` shape::

            [{"label": str, "stake": float}, ...]

        Each point is the sum of the latest ``total_stake`` snapshot per node
        (converted to USD) up to that bucket's cutoff timestamp.
        """
        try:
            if request.httprequest.method == "OPTIONS":
                return oauth_utils.preflight_response()

            user, resp = oauth_utils.require_user()
            if not user:
                return resp

            range_param = (
                request.params.get("range")
                or kwargs.get("range")
                or "1W"
            ).upper()

            if range_param not in ("1D", "1W", "30D", "ALL"):
                return oauth_utils._json_response(
                    False,
                    error="range must be one of: 1D, 1W, 30D, ALL",
                    status=400,
                )

            is_admin_user = user.has_group("access_rights.group_admin")

            # ------------------------------------------------------------------
            # 1. Fetch validator subscriptions in scope
            # ------------------------------------------------------------------
            if is_admin_user:
                sub_domain = [
                    ("subscription_type", "=", "validator"),
                    ("stripe_status", "in", ["active", "trialing"]),
                ]
            else:
                sub_domain = [
                    ("customer_name", "=", user.partner_id.id),
                    ("subscription_type", "=", "validator"),
                    ("stripe_status", "in", ["active", "trialing"]),
                ]

            subs = request.env["subscription.subscription"].sudo().search(sub_domain)

            if not subs:
                return oauth_utils._json_response(
                    True,
                    {"series": []},
                    error="No validator subscriptions found",
                )

            # ------------------------------------------------------------------
            # 2. Fetch validator nodes + batch-price all protocols
            # ------------------------------------------------------------------
            nodes = request.env["subscription.node"].sudo().search([
                ("subscription_id", "in", subs.ids),
                ("node_type", "=", "validator"),
                ("state", "=", "ready"),
            ])

            _logger.info("stake_history [step 2]: %d nodes found", len(nodes))

            price_service = TokenPriceService(request.env)
            unique_protocols = []
            seen_protocol_ids: set = set()
            for node in nodes:
                proto = node.subscription_id.protocol_id if node.subscription_id else None
                if proto and proto.id not in seen_protocol_ids:
                    seen_protocol_ids.add(proto.id)
                    unique_protocols.append(proto)

            protocol_prices = price_service.get_prices(unique_protocols) if unique_protocols else {}

            _logger.info(
                "stake_history [step 2]: %d unique protocols, prices=%s",
                len(unique_protocols),
                protocol_prices,
            )

            # ------------------------------------------------------------------
            # 3. Build (label_str, cutoff_datetime) bucket pairs
            # ------------------------------------------------------------------
            now = datetime.utcnow()
            bucket_pairs = []  # list of (label: str, cutoff: datetime)

            if range_param == "1D":
                today_midnight = datetime.combine(now.date(), datetime.min.time())
                for h in [0, 4, 8, 12, 16, 20]:
                    t = today_midnight + timedelta(hours=h)
                    bucket_pairs.append((t.strftime("%b %d, %H:%M"), t))
                bucket_pairs.append((now.strftime("%b %d, %H:%M"), now))

            elif range_param == "1W":
                for i in range(6):
                    t = datetime.combine(
                        (now - timedelta(days=6 - i)).date(), datetime.min.time()
                    )
                    bucket_pairs.append((t.strftime("%b %d"), t))
                bucket_pairs.append((now.strftime("%b %d"), now))

            elif range_param == "30D":
                for i in range(29, -1, -1):
                    t = datetime.combine(
                        (now - timedelta(days=i)).date(), datetime.min.time()
                    )
                    bucket_pairs.append((t.strftime("%b %d"), t))
                bucket_pairs.append((now.strftime("%b %d"), now))

            else:  # ALL — monthly from earliest subscription date
                earliest = None
                for sub in subs:
                    candidate = sub.subscribed_on or sub.stripe_start_date or sub.create_date
                    if candidate:
                        if isinstance(candidate, str):
                            candidate = fields.Datetime.from_string(candidate)
                        if not isinstance(candidate, datetime):
                            candidate = datetime.combine(candidate, datetime.min.time())
                        if earliest is None or candidate < earliest:
                            earliest = candidate
                if earliest is None:
                    earliest = now - timedelta(days=180)

                cursor_year, cursor_month = earliest.year, earliest.month
                while True:
                    label_dt = datetime(cursor_year, cursor_month, 1)
                    if cursor_month == 12:
                        next_y, next_m = cursor_year + 1, 1
                    else:
                        next_y, next_m = cursor_year, cursor_month + 1
                    end_of_month = datetime(next_y, next_m, 1) - timedelta(seconds=1)
                    cutoff_dt = min(end_of_month, now)
                    bucket_pairs.append((label_dt.strftime("%b %Y"), cutoff_dt))
                    if cutoff_dt >= now:
                        break
                    cursor_year, cursor_month = next_y, next_m

            _logger.info(
                "stake_history [step 3]: %d buckets for range=%s",
                len(bucket_pairs),
                range_param,
            )

            # ------------------------------------------------------------------
            # 4. Build series — total stake USD per bucket
            # ------------------------------------------------------------------
            series = []
            for label, t in bucket_pairs:
                stake_usd = 0.0
                for node in nodes:
                    snap = request.env["validator.rewards.snapshot"].sudo().search(
                        [
                            ("node_id", "=", node.id),
                            ("snapshot_date", "<=", fields.Datetime.to_string(t)),
                        ],
                        order="snapshot_date desc",
                        limit=1,
                    )
                    if not snap:
                        _logger.info(
                            "stake_history [step 4] bucket=%s: no snapshot for node_id=%s (skipped)",
                            label,
                            node.id,
                        )
                        continue
                    protocol = node.subscription_id.protocol_id if node.subscription_id else None
                    if not protocol:
                        continue
                    usd_price = protocol_prices.get(protocol.id)
                    stake_decimals = protocol.stake_decimals or 0
                    _, s_usd = convert_raw_value(snap.total_stake, stake_decimals, usd_price)
                    if s_usd is not None:
                        stake_usd += float(s_usd)

                _logger.info(
                    "stake_history [step 4] bucket=%s cutoff=%s stake_usd=%s",
                    label,
                    t,
                    round(stake_usd, 2),
                )

                series.append({
                    "label": label,
                    "stake": round(stake_usd, 2),
                })

            _logger.info(
                "stake_history [complete]: returning %d data points series=%s",
                len(series),
                series,
            )

            return oauth_utils._json_response(
                True,
                {"series": series},
                error="Stake history data fetched successfully",
            )

        except Exception as exc:
            _logger.exception("Error in validator_stake_history endpoint")
            return oauth_utils._json_response(False, error=str(exc), status=500)

    @http.route(
        "/api/v1/validator/investment-analyzer",
        type="http",
        auth="none",
        methods=["OPTIONS", "POST"],
        csrf=False,
    )
    def validator_investment_analyzer(self, **kwargs):
        """Run the validator investment analyzer using user-supplied inputs."""
        try:
            if request.httprequest.method == "OPTIONS":
                return oauth_utils.preflight_response(["POST"])

            user, resp = oauth_utils.require_user()
            if not user:
                return resp
            # user=request.env['res.users'].sudo().search([('id','=',10)],limit=1)

            payload = request.httprequest.get_json(force=True, silent=True) or {}
            if not isinstance(payload, dict):
                return oauth_utils._json_response(False, error="Invalid JSON payload.", status=400)

            protocol_value = self._normalize_identifier(
                payload.get("protocol"),
                payload.get("protocolId"),
                payload.get("protocol_name"),
            )
            if not protocol_value:
                return oauth_utils._json_response(False, error="protocol is required", status=400)

            network = (payload.get("network") or "mainnet").strip().lower()
            if network != "mainnet":
                return oauth_utils._json_response(
                    False,
                    error="Only mainnet is supported for the investment analyzer right now",
                    status=400,
                )

            compounding = _normalize_protocol_name(payload.get("compounding") or payload.get("frequency") or "daily")
            if compounding not in {"daily", "weekly", "monthly"}:
                return oauth_utils._json_response(
                    False,
                    error="compounding must be one of: daily, weekly, monthly",
                    status=400,
                )

            stake_amount = _coerce_float(
                payload.get("stakeAmount", payload.get("stake")),
                "stakeAmount",
                minimum=0.00000001,
            )
            days = int(
                round(
                    _coerce_float(
                        payload.get("days"),
                        "days",
                        minimum=1,
                    )
                )
            )
            apr = _coerce_float(
                payload.get("apr"),
                "apr",
                minimum=0,
            )
            commission = _coerce_float(
                payload.get("commission", payload.get("commissionPct")),
                "commission",
                minimum=0,
                maximum=100,
            )
            slashing_probability = _coerce_float(
                payload.get("slashingProbability", payload.get("slashingProbabilityPct")),
                "slashingProbability",
                required=False,
                minimum=0,
                maximum=100,
                default=0,
            )
            downtime = _coerce_float(
                payload.get("downtime", payload.get("downtimePct")),
                "downtime",
                required=False,
                minimum=0,
                maximum=100,
                default=0,
            )
            operating_cost_monthly = _coerce_float(
                payload.get("operatingCost", payload.get("operatingCostUsdMonthly")),
                "operatingCostUsdMonthly",
                required=False,
                minimum=0,
                default=0,
            )

            protocol_record = _resolve_investment_protocol_record(protocol_value)
            if not protocol_record:
                return oauth_utils._json_response(
                    False,
                    error=f"Protocol not found for key: {protocol_value}",
                    status=404,
                )

            price_service = TokenPriceService(request.env)
            protocol_prices = price_service.get_prices([protocol_record])
            token_price_usd = protocol_prices.get(protocol_record.id)
            if token_price_usd is None:
                return oauth_utils._json_response(
                    False,
                    error=f"USD price is unavailable for protocol: {protocol_record.name}",
                    status=404,
                )

            simulation = _simulate_investment_projection(
                stake=stake_amount,
                price=float(token_price_usd),
                commission=commission,
                days=days,
                apr=apr,
                compounding=compounding,
                slashing_probability=slashing_probability,
                downtime_percentage=downtime,
                operating_cost_monthly=operating_cost_monthly,
            )

            response_payload = {
                "protocol": {
                    "id": protocol_record.protocol_id,
                    "name": protocol_record.name,
                    "network": network,
                },
                "inputs": {
                    "stakeAmount": stake_amount,
                    "days": days,
                    "apr": apr,
                    "commission": commission,
                    "compounding": compounding,
                    "slashingProbability": slashing_probability,
                    "downtime": downtime,
                    "operatingCostUsdMonthly": operating_cost_monthly,
                },
                **simulation,
            }

            return oauth_utils._json_response(
                True,
                response_payload,
                error="Investment analyzer result fetched successfully.",
            )
        except ValueError as exc:
            return oauth_utils._json_response(False, error=str(exc), status=400)
        except Exception as exc:
            _logger.exception("Error in validator_investment_analyzer endpoint")
            return oauth_utils._json_response(False, error=str(exc), status=500)
