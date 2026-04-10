import json
from typing import List, Dict
from werkzeug.exceptions import HTTPException

from odoo import http
from odoo.http import request
from odoo.addons.web.controllers.binary import Binary
from odoo.tools import str2bool

from ...auth_module.utils import oauth as oauth_utils
from ..utils import base_utils as base_utils


class ProtocolAPIController(http.Controller):
    """REST API for protocol details listing."""

    @http.route(
        "/api/v1/details/protocols",
        type="http",
        auth="none",
        methods=["OPTIONS", "GET"],
        csrf=False,
    )
    def protocol_details(self, **kwargs):
        """Return protocols and their related plans filtered by node type.

        Requires ``Authorization`` header in the format ``Bearer <sid>``.

        :param node_type: RPC, Archive, or Validator
        :return: JSON response with success, data, and message keys
        """
        try:
            if request.httprequest.method == "OPTIONS":
                return oauth_utils.preflight_response()

             # 1. Auth
            user, resp = oauth_utils.require_user()
            if not user:
                return resp

            request.update_env(user=user.id)

            # -------------------------------
            # Parse and validate payload
            # -------------------------------
            required_fields = ["node_type"]
            is_valid, error_msg = base_utils._validate_payload(kwargs, required_fields)
            if not is_valid:
                return oauth_utils._json_response(False, {'error': error_msg}, status=400)

            node_field_map = {
                "RPC": "is_rpc",
                "Archive": "is_archive",
                "Validator": "is_validator",
            }

            node_type = kwargs.get("node_type")
            field_name = node_field_map.get(node_type)
            # Exclude IOPN here; it has a dedicated endpoint.
            domain = [("active", "=", True), ("name", "not ilike", "opn")]
            if field_name:
                domain.append((field_name, "=", True))

            protocols = request.env["protocol.master"].sudo().search(domain)

            results: List[dict] = []
            base_url = oauth_utils._get_image_url()
            for protocol in protocols:
                icon_url = base_utils.public_image_url(protocol, "image", size="64x64") if protocol.image else False

                plans_grouped: Dict[str, List[Dict]] = {}
                for plan in protocol.plan_ids:
                    if node_type and plan.subscription_type != node_type.lower():
                        continue
                    plan_data = {
                        "id": plan.id,
                        "subscription_type": plan.subscription_type,
                        "bandwidth": plan.bandwidth,
                        "domainCustomization": plan.domainCustomization,
                        "ipWhitelist": plan.ipWhitelist,
                        "monthlyLimit": plan.monthlyLimit,
                        "softwareUpgrades": plan.softwareUpgrades,
                        "support": plan.support,
                        "uptimeSLA": plan.uptimeSLA,
                        "amount_month": plan.amount_month,
                        "amount_quarter": plan.amount_quarter,
                        "amount_year": plan.amount_year,
                        "stripe_product_id": plan.stripe_product_id,
                        "stripe_price_month_id": plan.stripe_price_month_id,
                        "stripe_price_quarter_id": plan.stripe_price_quarter_id,
                        "stripe_price_year_id": plan.stripe_price_year_id,
                        "regions": [region.name for region in plan.region_ids],
                    }
                    if plan.name not in plans_grouped:
                        plans_grouped[plan.name] = []
                    plans_grouped[plan.name].append(plan_data)
                results.append(
                    {
                        protocol.name: {
                            "id": protocol.id,
                            "protocol_id": protocol.protocol_id,
                            "name": protocol.name,
                            "notes": protocol.notes,
                            "icon": icon_url,
                            "network_types": [network_type.name for network_type in protocol.network_type_ids],
                        },
                        "plans": plans_grouped,
                    }
                )

            return oauth_utils._json_response(True, results, error="Protocols fetched successfully.")

        except Exception as exc:
            return oauth_utils._json_response(False, error=str(exc), status=500)


    @http.route(
        "/api/v1/details/protocols/opn",
        type="http",
        auth="none",
        methods=["OPTIONS", "GET"],
        csrf=False,
    )
    def protocol_details_opn(self, **kwargs):
        """Return only the OPN protocol and its plans filtered by node type."""
        try:
            if request.httprequest.method == "OPTIONS":
                return oauth_utils.preflight_response()

            user, resp = oauth_utils.require_user()
            if not user:
                return resp

            request.update_env(user=user.id)

            required_fields = ["node_type"]
            is_valid, error_msg = base_utils._validate_payload(kwargs, required_fields)
            if not is_valid:
                return oauth_utils._json_response(False, {'error': error_msg}, status=400)

            node_field_map = {
                "RPC": "is_rpc",
                "Archive": "is_archive",
                "Validator": "is_validator",
            }

            node_type = kwargs.get("node_type")
            field_name = node_field_map.get(node_type)
            domain = [("active", "=", True), ("name", "ilike", "opn")]
            if field_name:
                domain.append((field_name, "=", True))

            protocols = request.env["protocol.master"].sudo().search(domain)

            if not protocols:
                return oauth_utils._json_response(True, [], error="No IOPN protocol found.")

            results: List[dict] = []
            base_url = oauth_utils._get_image_url()
            for protocol in protocols:
                icon_url = base_utils.public_image_url(protocol, "image", size="64x64") if protocol.image else False

                plans_grouped: Dict[str, List[Dict]] = {}
                for plan in protocol.plan_ids:
                    if node_type and plan.subscription_type != node_type.lower():
                        continue
                    plan_data = {
                        "id": plan.id,
                        "subscription_type": plan.subscription_type,
                        "bandwidth": plan.bandwidth,
                        "domainCustomization": plan.domainCustomization,
                        "ipWhitelist": plan.ipWhitelist,
                        "monthlyLimit": plan.monthlyLimit,
                        "softwareUpgrades": plan.softwareUpgrades,
                        "support": plan.support,
                        "uptimeSLA": plan.uptimeSLA,
                        "amount_month": plan.amount_month,
                        "amount_quarter": plan.amount_quarter,
                        "amount_year": plan.amount_year,
                        "stripe_product_id": plan.stripe_product_id,
                        "stripe_price_month_id": plan.stripe_price_month_id,
                        "stripe_price_quarter_id": plan.stripe_price_quarter_id,
                        "stripe_price_year_id": plan.stripe_price_year_id,
                        "regions": [region.name for region in plan.region_ids],
                    }
                    if plan.name not in plans_grouped:
                        plans_grouped[plan.name] = []
                    plans_grouped[plan.name].append(plan_data)
                results.append(
                    {
                        protocol.name: {
                            "id": protocol.id,
                            "protocol_id": protocol.protocol_id,
                            "name": protocol.name,
                            "notes": protocol.notes,
                            "icon": icon_url,
                            "network_types": [network_type.name for network_type in protocol.network_type_ids],
                        },
                        "plans": plans_grouped,
                    }
                )

            return oauth_utils._json_response(True, results, error="OPN protocol fetched successfully.")

        except Exception as exc:
            return oauth_utils._json_response(False, error=str(exc), status=500)



    @http.route('/api/v1/contact-us', csrf=False, auth='public', methods=['OPTIONS', 'POST'])
    def get_contact_us(self):
        try:
            if request.httprequest.method == "OPTIONS":
                return oauth_utils.preflight_response()

             # 1. Auth
            user, resp = oauth_utils.require_user()
            if not user:
                return resp

            request.update_env(user=user.id)

            # -------------------------------
            # Parse and validate payload
            # -------------------------------
            payload = request.httprequest.get_json(force=True, silent=True) or {}

            required_fields = ["name", "email", "message", "type"]
            is_valid, error_msg = base_utils._validate_payload(payload, required_fields)
            if not is_valid:
                return oauth_utils._json_response(False, {'error': error_msg}, status=400)
            country = request.env['res.country'].sudo().search([('name', '=', payload.get('country_id'))])
            contact = request.env['contact.us'].sudo().create({
                'name': payload.get('name'),
                'email': payload.get('email'),
                'comment': payload.get('message'),
                'type':payload.get('type'),
                'company_name':payload.get('company_name'),
                "country_id":country.id if country.id else False,
            })
            response=({
                'status': True,
                'message': "Your response is saved",
                'contact_id': contact.id
            })
            # Send email notifications to the user and admins.
            contact.send_contact_us_email()
            return oauth_utils._json_response(True, response)
        except Exception as e:
            return oauth_utils._json_response(False, {'error': str(e)}, status=500)

    @http.route('/api/v1/subscriptions-list', csrf=False, auth='public', methods=['OPTIONS', 'GET'])
    def all_subscriptions_list(self):
        try:

            if request.httprequest.method == "OPTIONS":
                    return oauth_utils.preflight_response()

                # 1. Auth
            user, resp = oauth_utils.require_user()
            if not user:
                return resp
            # user = request.env['res.users'].sudo().search([('id','=',62)])
            # Step 3: Fetch Data

            request.update_env(user=user.id)
            is_admin_user = user.has_group('access_rights.group_admin')
            sub_domain = []
            rollup_domain = []
            if is_admin_user:
                # Internal users can see all nodes of the specified type
                sub_domain = []
                rollup_domain = []
            else:
                # External users only see their own nodes
                sub_domain = [("subscription_id.customer_name", "=", user.partner_id.id)]
                rollup_domain = [("customer_id", "=", user.partner_id.id)]
            node_subscriptions = request.env['subscription.node'].sudo().search(sub_domain)
            rollups = request.env['rollup.service'].sudo().search(rollup_domain)

            # Step 4: Build structured response
            # Stripe status mapping
            status_map = {
                'trialing': 'active',
                'past_due': 'suspended',
            }
            subscriptions_data = []
            for node_subscription in node_subscriptions:
                subscription_status = status_map.get(node_subscription.subscription_id.stripe_status, node_subscription.subscription_id.stripe_status)
                draft_prorated = request.env['subscription.prorated.charge'].sudo().search([('state','=','draft'),('subscription_id','=',node_subscription.subscription_id.id)])

                if node_subscription.state in ['cancellation_requested', 'closed']:
                    subscription_status ='canceled' if node_subscription.state == 'closed' else node_subscription.state
                protocol = node_subscription.subscription_id.protocol_id
                protocol_image = False
                if protocol and protocol.image:
                    try:
                        protocol_image = base_utils.public_image_url(protocol, "image", size="128x128")
                    except Exception:
                        pass
                sub_data = {
                    'id': node_subscription.subscription_id.subscription_uuid,
                    'node_id': node_subscription.node_identifier,
                    'node_name': node_subscription.node_name,
                    'plan_name': f"{node_subscription.subscription_id.protocol_id.name} {node_subscription.node_type.replace('_', ' ').title()} Node",
                    'plan_type': node_subscription.subscription_id.sub_plan_id.name,
                    'node_status': node_subscription.state,
                    'subscription_status': subscription_status,
                    'next_billing_date': node_subscription.subscription_id.stripe_end_date,
                    'subscription_type': node_subscription.subscription_id.subscription_type,
                    'amount': node_subscription.subscription_id.price,
                    'payment_frequency': node_subscription.subscription_id.payment_frequency,
                    'node_status': node_subscription.state,
                    'logo': protocol_image,
                    'network_type': node_subscription.network_selection_id.name,
                    'protocol_id': protocol.protocol_id,
                    # 'nodes': subscription.serialize_nodes(),
                }
                if node_subscription.subscription_id.stripe_status in ['past_due', 'unpaid']:
                    sub_data['payment_link'] = node_subscription.subscription_id.hosted_invoice_url

                if node_subscription.node_type == 'rpc':
                    sub_data['endpoint'] = node_subscription.endpoint_url
                elif node_subscription.node_type == 'validator':
                    validator_info_raw = getattr(node_subscription, 'validator_info', {}) or {}
                    if isinstance(validator_info_raw, str):
                        try:
                            validator_info_raw = json.loads(validator_info_raw)
                        except ValueError:
                            validator_info_raw = {}
                    validator_address = validator_info_raw.get('validatorAddress') or validator_info_raw.get('validator_address')
                    if validator_address:
                        sub_data['validator_id'] = validator_address

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
                    sub_data["prorated_draft_order"] = prorated_orders
                subscriptions_data.append(sub_data)

            rollups_data = []
            for rollup in rollups:
                is_pending = rollup.subscription_status in ['draft', 'pending_payment']
                user_inputs_payload = rollup.inputs_json if isinstance(rollup.inputs_json, dict) else {}
                network_type = (
                    user_inputs_payload.get('network_type')
                    or user_inputs_payload.get('extras', {}).get('network_type')
                    or getattr(rollup, 'network_type', False)
                    or 'mainnet'
                )
                rollup_image = False
                if rollup.type_id and rollup.type_id.image:
                    try:
                        rollup_image = base_utils.public_image_url(rollup.type_id, "image", size="128x128")
                    except Exception:
                        pass
                
                # Extract explorer URL from rollup_metadata
                explorer_url = None
                if rollup.rollup_metadata:
                    try:
                        metadata = json.loads(rollup.rollup_metadata) if isinstance(rollup.rollup_metadata, str) else rollup.rollup_metadata
                        if isinstance(metadata, dict):
                            # Check if it's an Arbitrum rollup (check type_id name or rollup_id)
                            is_arbitrum = False
                            if rollup.type_id:
                                rollup_type_name = (rollup.type_id.name or '').lower()
                                rollup_type_id = (rollup.type_id.rollup_id or '').lower()
                                is_arbitrum = 'arbitrum' in rollup_type_name or 'arbitrum' in rollup_type_id
                            
                            # For Arbitrum, use l3.explorerUrl, otherwise use l2.explorerUrl
                            if is_arbitrum and metadata.get('l3', {}).get('explorerUrl'):
                                explorer_url = metadata['l3']['explorerUrl']
                            elif metadata.get('l2', {}).get('explorerUrl'):
                                explorer_url = metadata['l2']['explorerUrl']
                    except (json.JSONDecodeError, AttributeError, KeyError):
                        pass
                
                rollup_data = {
                    'id': rollup.service_id,
                    'node_name': rollup.name,
                    'subscription_status': rollup.subscription_status,
                    'plan_name': rollup.type_id.name.replace('-', ' ').title(),
                    'is_pending': is_pending,
                    'logo': rollup_image,
                    'network_type': network_type,
                    'node_count': rollup.node_count,
                }
                
                # Add explorer_url if available
                if explorer_url:
                    rollup_data['explorer_url'] = explorer_url
                
                if is_pending:
                    inputs = rollup.inputs_json or {}
                    rollup_data.update({
                        "type_id": inputs.get('type_id') or rollup.type_id.id,
                        "region_ids": inputs.get('region_ids') or rollup.region_ids.ids,
                        "network_type": inputs.get('network_type') or inputs.get('extras', {}).get('network_type', 'testnet'),
                        "configuration": inputs.get('configuration') or {},
                        "deployment_token": rollup.deployment_token
                    })
                else:
                    rollup_data.update({
                        'next_billing_date': rollup.next_billing_date,
                        'subscription_type': rollup.type_id.name.replace('-', ' ').title(),
                        'amount': rollup.type_id.cost,
                        'node_status': rollup.status,
                        'payment_frequency': 'monthly',
                    })
                    if rollup.subscription_status == 'overdue':
                        rollup_data['payment_link'] = rollup.hosted_invoice_url
                
                rollups_data.append(rollup_data)

            # Step 5: Final response object
            result = {
                'subscriptions': subscriptions_data,
                'rollups': rollups_data,
                'total_subscriptions': len(subscriptions_data),
                'total_rollups': len(rollups_data),
            }   
            return oauth_utils._json_response(True, result, error="Subscriptions fetched successfully.")
        except Exception as e:
            return oauth_utils._json_response(False, {'error': str(e)}, status=500)


class SecureBinaryController(Binary):
    """Extended binary controller with token validation for secure image access."""

    @http.route([
        '/web/image',
        '/web/image/<string:xmlid>',
        '/web/image/<string:xmlid>/<string:filename>',
        '/web/image/<string:xmlid>/<int:width>x<int:height>',
        '/web/image/<string:xmlid>/<int:width>x<int:height>/<string:filename>',
        '/web/image/<string:model>/<int:id>/<string:field>',
        '/web/image/<string:model>/<int:id>/<string:field>/<string:filename>',
        '/web/image/<string:model>/<int:id>/<string:field>/<int:width>x<int:height>',
        '/web/image/<string:model>/<int:id>/<string:field>/<int:width>x<int:height>/<string:filename>',
        '/web/image/<int:id>',
        '/web/image/<int:id>/<string:filename>',
        '/web/image/<int:id>/<int:width>x<int:height>',
        '/web/image/<int:id>/<int:width>x<int:height>/<string:filename>',
        '/web/image/<int:id>-<string:unique>',
        '/web/image/<int:id>-<string:unique>/<string:filename>',
        '/web/image/<int:id>-<string:unique>/<int:width>x<int:height>',
        '/web/image/<int:id>-<string:unique>/<int:width>x<int:height>/<string:filename>',
    ], type='http', auth="public", methods=['GET', 'HEAD'], multilang=False, readonly=True)
    def content_image(self, **kw):
        """Validate signed image URLs and serve them without session-dependent access checks."""
        
        # Check if token parameter is provided
        token = kw.get('token')
        id = kw.get('id')
        
        if token and id:
            # Validate the encoded token
            try:
                secret = base_utils._get_jwt_secret()
                decoded_token = base_utils._decode_token(token, secret)
                
                if not decoded_token:
                    raise request.not_found("Invalid or expired token")
                
                # Verify attachment exists and has matching access_token
                attachment = request.env['ir.attachment'].sudo().browse(int(id))
                if not attachment.exists():
                    raise request.not_found("Attachment not found")
                
                if attachment.access_token != decoded_token:
                    raise request.not_found("Token mismatch")
                
                # Token validated - remove it from kwargs to avoid warning
                kw.pop('token', None)

                stream = request.env['ir.binary']._get_image_stream_from(
                    attachment,
                    'raw',
                    filename=kw.get('filename'),
                    filename_field=kw.get('filename_field', 'name'),
                    mimetype=kw.get('mimetype'),
                    width=int(kw.get('width', 0) or 0),
                    height=int(kw.get('height', 0) or 0),
                    crop=kw.get('crop', False),
                )
                stream.public = True

                send_file_kwargs = {'as_attachment': str2bool(kw.get('download', False), default=False)}
                if kw.get('unique'):
                    send_file_kwargs['immutable'] = True
                    send_file_kwargs['max_age'] = http.STATIC_CACHE_LONG
                if kw.get('nocache'):
                    send_file_kwargs['max_age'] = None

                return stream.get_response(**send_file_kwargs)
                
            except HTTPException:
                raise
            except Exception as e:
                raise request.not_found(f"Token validation error: {str(e)}")
        
        # Call parent's content_image method
        return super().content_image(**kw)
