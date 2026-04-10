# -*- coding: utf-8 -*-
import stripe
import logging
from odoo import http, fields, _
from odoo.http import request,Response
from ...auth_module.utils import oauth as oauth_utils
from datetime import datetime, date
import traceback
import json
from ..utils import mnemonic_service
from ...rollup_management.utils import rollup_util
import uuid


_logger = logging.getLogger(__name__)
DURATION_TO_FREQUENCY = {
    'monthly': 'monthly',
    'quarterly': 'quarterly',
    'yearly': 'annually',
}
class OdooManagedBillingAPI(http.Controller):

    @http.route('/api/v2/create_checkout_session', type='http', auth='public', methods=['POST'], csrf=False)
    def create_checkout_session_v2(self, **kwargs):
        """
        V2 – Odoo Managed Billing
        Stripe only collects first payment + saves card
        """
        try:
            if request.httprequest.method == "OPTIONS":
                return oauth_utils.preflight_response(["GET", "POST"])

            # ---- Auth ----
            user, resp = oauth_utils.require_user()
            if not user:
                return resp
            # user = request.env['res.users'].sudo().search([('id','=',129)])
            request.update_env(user=user.id)

            stripe.api_key = request.env['ir.config_parameter'].sudo().get_param('stripe_secret_key')

            data = request.httprequest.get_json(force=True, silent=True) or {}
            required_fields = ['plan_type', 'protocol_id', 'subscription_type', 'duration']
            for field in required_fields:
                if field not in data:
                    return oauth_utils._json_response(
                        False, {'error': f'Missing required field: {field}'}, status=400
                    )
            duration = data.get('duration')
            # ---- Resolve Protocol & Plan ----
            protocol = request.env['protocol.master'].sudo().search(
                [('protocol_id', '=', data['protocol_id'])], limit=1
            )

            sub_plan = request.env['subscription.plan'].sudo().search([
                ('protocol_id', '=', protocol.id),
                ('subscription_type', '=', data['subscription_type']),
                ('name', '=', data['plan_type']),
                ('active', '=', True),
            ], limit=1)
            # sub_plan = request.env['subscription.plan'].sudo().search([
            #                 ('id', '=', 5),
            #             ], limit=1)
            if not sub_plan:
                return oauth_utils._json_response(False, {'error': 'Plan not found'}, status=404)

            # ---- Amount by Duration ----
            payment_frequency = DURATION_TO_FREQUENCY.get(duration)
            if not payment_frequency:
                return oauth_utils._json_response(
                    False, {'error': 'Invalid duration'}, status=400
                )

            if duration == 'monthly':
                plan_amount = sub_plan.amount_month
                duration_vals = {'duration': 1, 'unit': 'month'}

            elif duration == 'quarterly':
                plan_amount = sub_plan.amount_quarter
                duration_vals = {'duration': 3, 'unit': 'month'}

            elif duration == 'yearly':
                plan_amount = sub_plan.amount_year
                duration_vals = {'duration': 1, 'unit': 'year'}
            # ---- Check for Existing Active v2 Subscription (Proration) ----
            existing_sub = request.env['subscription.subscription'].sudo().search([
                ('customer_name', '=', user.partner_id.id),
                ('sub_plan_id', '=', sub_plan.id),
                ('payment_frequency', '=', payment_frequency),
                ('state', 'in', ['in_progress', 'provisioning']),
                ('is_odoo_managed', '=', True),
            ], limit=1)
            partner = user.partner_id
            print(existing_sub,'--------------90')
            if existing_sub:
                # Calculate proration
                if not existing_sub.stripe_start_date or not existing_sub.stripe_end_date:
                    return oauth_utils._json_response(False, {'error': 'Subscription dates not set for proration.'}, status=422)
                
                total_days = (existing_sub.stripe_end_date - existing_sub.stripe_start_date).days or 30
                days_left = max(0, (existing_sub.stripe_end_date - datetime.now()).days)
                
                desired_increase = int(data.get('quantity', 1))
                prorated_amount = round((plan_amount * desired_increase) * (days_left / total_days), 2)
                
                if prorated_amount <= 0:
                     existing_sub.sudo().write({
                        'pending_quantity_increase': desired_increase,
                        'pending_quantity_paid': True
                    })
                     return oauth_utils._json_response(True, {
                        'message': 'No prorated amount to charge. Quantity will update at next billing.',
                        'subscription_id': existing_sub.id
                    })

                # ---- Get Success and Cancel URLs (V1 Pattern) ----
                fe_url = request.env['ir.config_parameter'].sudo().get_param('frontend_url')
                node_path = 'full' if sub_plan.subscription_type == 'rpc' else sub_plan.subscription_type
                route = request.env['ir.config_parameter'].sudo().get_param('stripe_redirection_route') or 'manage/nodes/'
                base_success_url = f'{fe_url}/{route}{node_path}?status=success&session_id={{CHECKOUT_SESSION_ID}}'
                success_url = f"{base_success_url}&action=prorated_charge_for_qty_increase"
                cancel_url = f'{fe_url}/{route}{node_path}?status=cancel'

                charge_rec = request.env['subscription.prorated.charge'].sudo().search([
                    ('subscription_id', '=', existing_sub.id),
                    ('state', '=', 'draft')
                ], limit=1)
                
                if not charge_rec:
                    charge_rec = request.env['subscription.prorated.charge'].sudo().create({
                        'subscription_id': existing_sub.id,
                        'amount': prorated_amount,
                        'quantity_increase': desired_increase,
                        'state': 'draft',
                    })

                session = stripe.checkout.Session.create(
                    customer=partner.stripe_customer_id,
                    success_url=success_url,
                    cancel_url=cancel_url,
                    mode='payment',
                    payment_method_types=['card'],
                    line_items=[{
                        'price_data': {
                            'currency': 'usd',
                            'product_data': {'name': f"Prorated charge for {sub_plan.name} increase"},
                            'unit_amount': int(prorated_amount * 100),
                        },
                        'quantity': 1,
                    }],
                    payment_intent_data={
                        'setup_future_usage': 'off_session',
                        'metadata': {
                            'odoo_subscription_id': str(existing_sub.id),
                            'is_odoo_managed': 'true',
                            'action': 'prorated_charge_for_qty_increase',
                            'charge_id': str(charge_rec.id),
                            'quantity_increase': str(desired_increase),
                        }
                    }
                )
                
                charge_rec.write({
                    'session_id': session.id,
                    'checkout_url': session.url,
                })
                
                existing_sub.sudo().write({
                    'pending_quantity_increase': desired_increase,
                    'pending_quantity_paid': False,
                    'pending_quantity_prorated_amount': prorated_amount,
                })

                return oauth_utils._json_response(True, {
                    'checkout_url': session.url,
                    'session_id': session.id,
                    'subscription_id': existing_sub.id,
                    'amount': prorated_amount,
                    'status': 'proration_required'
                })
            
            # ---- Extract Node Metadata ----
            node_name = data.get('node_name')
            network_selection = data.get('network_selection')
            server_location_id = data.get('server_location_id')
            automatic_update = data.get('automatic_update', True)

            # ---- Create Draft Subscription (SOURCE OF TRUTH) ----
            subscription = request.env['subscription.subscription'].sudo().create({
                'customer_name': user.partner_id.id,
                'sub_plan_id': sub_plan.id,
                'protocol_id': protocol.id,
                'subscription_type': sub_plan.subscription_type,
                'payment_frequency': payment_frequency,
                'price': plan_amount,
                'state': 'draft',
                'stripe_status': 'draft',
                'is_odoo_managed': True,
                'metaData': {
                    'node_name': node_name,
                    'network_selection': network_selection,
                    'server_location_id': server_location_id,
                    'automatic_update': automatic_update,
                },
                **duration_vals
            })

            # ---- Resolve Network ID and Create Primary Node ----
            network_id = False
            if network_selection:
                network_rec = request.env['zeeve.network.type'].sudo().search([('name', '=', network_selection)], limit=1)
                network_id = network_rec.id if network_rec else False

            selected_location = request.env['server.location'].sudo().search([('name','=',server_location_id)], limit=1)

            node_vals = {
                'node_name': node_name or protocol.name,
                'network_selection_id': network_id,
                'server_location_id': selected_location.id if selected_location else False,
                'software_update_rule': 'auto' if automatic_update else 'manual',
                'node_type': sub_plan.subscription_type,
                'state': 'draft',
            }

            shardeum_password = data.get('password')
            if shardeum_password and sub_plan.subscription_type == 'validator' and protocol.name == 'Shardeum':
                encrypted_password = mnemonic_service.encrypt_data(request.env,{'shardeum_password':shardeum_password})
                node_vals.update({"validator_info": json.dumps({'shardeum_password' : encrypted_password})})

            subscription.create_primary_node(node_vals)

            # ---- Stripe Customer ----
            
            if not partner.stripe_customer_id:
                stripe_customer = stripe.Customer.create(
                    email=partner.email or user.login,
                    name=partner.name
                )
                partner.sudo().write({'stripe_customer_id': stripe_customer.id})

            # ---- Get Success and Cancel URLs (V1 Pattern) ----
            fe_url = request.env['ir.config_parameter'].sudo().get_param('frontend_url')
            node_path = 'full' if sub_plan.subscription_type == 'rpc' else sub_plan.subscription_type
            route = request.env['ir.config_parameter'].sudo().get_param('stripe_redirection_route') or 'manage/nodes/'
            success_url = f'{fe_url}/{route}{node_path}?status=success&session_id={{CHECKOUT_SESSION_ID}}'
            cancel_url = f'{fe_url}/{route}{node_path}?status=cancel'

            # ---- Checkout (NO BUSINESS METADATA) ----
            session = stripe.checkout.Session.create(
            customer=partner.stripe_customer_id,
            mode='payment',
            payment_method_types=['card'],
            line_items=[{
                'price_data': {
                    'currency': 'usd',
                    'product_data': {
                        'name': f"{sub_plan.name} ({duration})"
                    },
                    'unit_amount': int(plan_amount * 100),
                },
                'quantity': 1,
            }],
            payment_intent_data={
                'setup_future_usage': 'off_session',
                'metadata': {
                    'odoo_subscription_id': str(subscription.id),
                    'is_odoo_managed': 'true',
                }
            },
            success_url=success_url,
            cancel_url=cancel_url,
            )

            # subscription.sudo().write({'checkout_session_id': session.id})

            return oauth_utils._json_response(True, {
                'checkout_url': session.url,
                'subscription_id': subscription.id,
                'amount': plan_amount,
                'currency': 'usd',
                'status': 'created'
            })

        except Exception as e:
            _logger.exception("V2 checkout failed")
            return oauth_utils._json_response(False, {'error': str(e)}, status=500)

    @http.route('/api/v2/retry_create_checkout_session', type='http', auth='public', methods=['POST'], csrf=False)
    def retry_checkout_session_v2(self, **kwargs):
        """Recreate a Stripe Checkout Session for a Draft v2 subscription."""
        try:
            if request.httprequest.method == "OPTIONS":
                return oauth_utils.preflight_response(["POST"])
            
            # 1. Auth
            user, resp = oauth_utils.require_user()
            if not user:
                return resp
            # user = request.env['res.users'].sudo().search([('id','=',129)])
            request.update_env(user=user.id)
            stripe.api_key = request.env['ir.config_parameter'].sudo().get_param('stripe_secret_key')

            data = request.httprequest.get_json(force=True, silent=True) or {}
            subscription_id = data.get('subscription_id')
            if not subscription_id:
                return oauth_utils._json_response(False, {'error': 'Missing subscription_id'}, status=400)
            Subscription = request.env['subscription.subscription'].sudo()
            subscription = False
            subscription_id_clean = str(subscription_id).strip()
            if subscription_id_clean.isdigit():
                subscription = Subscription.search([
                    ('id', '=', int(subscription_id_clean)),
                    ('customer_name', '=', user.partner_id.id)
                ], limit=1)
            else:
                subscription = Subscription.search([
                    ('subscription_uuid', '=', subscription_id_clean),
                    ('customer_name', '=', user.partner_id.id)
                ], limit=1)
            if not subscription.exists() or subscription.customer_name.id != user.partner_id.id:
                return oauth_utils._json_response(False, {'error': 'Subscription not found or access denied'}, status=404)
            
            if subscription.state != 'draft':
                 return oauth_utils._json_response(False, {'error': 'Subscription is not in draft state'}, status=422)

            node_path = 'full' if subscription.subscription_type == 'rpc' else subscription.subscription_type
            fe_url = request.env['ir.config_parameter'].sudo().get_param('frontend_url')
            route = request.env['ir.config_parameter'].sudo().get_param('stripe_redirection_route') or 'manage/nodes/'
            success_url = f'{fe_url}/{route}{node_path}?status=success&session_id={{CHECKOUT_SESSION_ID}}'
            cancel_url = f'{fe_url}/{route}{node_path}?status=cancel'

            session = stripe.checkout.Session.create(
                customer=subscription.customer_name.stripe_customer_id,
                mode='payment',
                payment_method_types=['card'],
                line_items=[{
                    'price_data': {
                        'currency': 'usd',
                        'product_data': {'name': f"{subscription.sub_plan_id.name}"},
                        'unit_amount': int(subscription.price * 100),
                    },
                    'quantity': 1,
                }],
                payment_intent_data={
                    'setup_future_usage': 'off_session',
                    'metadata': {
                        'odoo_subscription_id': str(subscription.id),
                        'is_odoo_managed': 'true',
                    }
                },
                success_url=success_url,
                cancel_url=cancel_url,
            )

            return oauth_utils._json_response(True, {
                'checkout_url': session.url,
                'session_id': session.id,
                'subscription_id': subscription.id,
                'amount': subscription.price,
                'currency': 'usd',
                'status': 'created'
            })
        except Exception as e:
            _logger.exception("V2 retry checkout failed")
            return oauth_utils._json_response(False, {'error': str(e)}, status=500)

    @http.route('/api/v2/retry_prorated_charge', type='http', auth='public', methods=['POST'], csrf=False)
    def retry_prorated_charge_v2(self, **kwargs):
        """Retry a prorated charge payment for v2."""
        try:
            if request.httprequest.method == "OPTIONS":
                return oauth_utils.preflight_response(["POST"])

            # 1. Auth
            user, resp = oauth_utils.require_user()
            if not user:
                return resp
            request.update_env(user=user.id)
            stripe.api_key = request.env['ir.config_parameter'].sudo().get_param('stripe_secret_key')

            data = request.httprequest.get_json(force=True, silent=True) or {}
            charge_id = data.get('charge_id')
            if not charge_id:
                  return oauth_utils._json_response(False, {'error': 'Missing charge_id'}, status=400)

            charge = request.env['subscription.prorated.charge'].sudo().browse(int(charge_id))
            if not charge.exists() or charge.subscription_id.customer_name.id != user.partner_id.id:
                return oauth_utils._json_response(False, {'error': 'Charge not found or access denied'}, status=404)
            
            if charge.state != 'draft':
                 return oauth_utils._json_response(False, {'error': 'Charge is already processed'}, status=422)

            subscription = charge.subscription_id
            
            # ---- Get Success and Cancel URLs (V1 Pattern) ----
            fe_url = request.env['ir.config_parameter'].sudo().get_param('frontend_url')
            node_path = 'full' if subscription.subscription_type == 'rpc' else subscription.subscription_type
            route = request.env['ir.config_parameter'].sudo().get_param('stripe_redirection_route') or 'manage/nodes/'
            success_url = f'{fe_url}/{route}{node_path}?status=success&session_id={{CHECKOUT_SESSION_ID}}&action=prorated_charge_for_qty_increase'
            cancel_url = f'{fe_url}/{route}{node_path}?status=cancel'

            session = stripe.checkout.Session.create(
                customer=subscription.customer_name.stripe_customer_id,
                mode='payment',
                payment_method_types=['card'],
                line_items=[{
                    'price_data': {
                        'currency': 'usd',
                        'product_data': {'name': f"Prorated charge for {subscription.sub_plan_id.name} increase"},
                        'unit_amount': int(charge.amount * 100),
                    },
                    'quantity': 1,
                }],
                payment_intent_data={
                    'setup_future_usage': 'off_session',
                    'metadata': {
                        'odoo_subscription_id': str(subscription.id),
                        'is_odoo_managed': 'true',
                        'action': 'prorated_charge_for_qty_increase',
                        'charge_id': str(charge.id),
                        'quantity_increase': str(charge.quantity_increase),
                    }
                },
                success_url=success_url,
                cancel_url=cancel_url,
            )

            charge.write({
                'session_id': session.id,
                'checkout_url': session.url,
            })

            return oauth_utils._json_response(True, {
                'checkout_url': session.url,
                'session_id': session.id,
                'amount': charge.amount,
                'status': 'recreated'
            })
        except Exception as e:
            _logger.exception("V2 retry proration failed")
            return oauth_utils._json_response(False, {'error': str(e)}, status=500)

    @http.route('/api/v2/list_payment_methods', type='http', auth='public', methods=['GET'], csrf=False)
    def list_payment_methods_v2(self, **kwargs):
        """List all vaulted payment methods for the current user."""
        try:
            if request.httprequest.method == "OPTIONS":
                return oauth_utils.preflight_response(["GET"])

            user, resp = oauth_utils.require_user()
            if not user:
                return resp
            # user = request.env['res.users'].sudo().search([('id','=',)])
            request.update_env(user=user.id)
            pms = request.env['stripe.payment.method'].sudo().search([
                ('partner_id', '=', user.partner_id.id),
                ('active', '=', True)
            ])
            
            res = []
            for pm in pms:
                res.append({
                    'id': pm.id,
                    'stripe_pm_id': pm.stripe_payment_method_id,
                    'brand': pm.brand,
                    'last4': pm.last4,
                    'exp_month': pm.exp_month,
                    'exp_year': pm.exp_year,
                    'is_default': pm.is_default
                })
            
            return oauth_utils._json_response(True, {'payment_methods': res})
        except Exception as e:
            _logger.exception("V2 list payment methods failed")
            return oauth_utils._json_response(False, {'error': str(e)}, status=500)

    @http.route('/api/v2/set_default_payment_method', type='http', auth='public', methods=['POST'], csrf=False)
    def set_default_payment_method_v2(self, **kwargs):
        """Set the default payment method for a v2 subscription."""
        try:
            if request.httprequest.method == "OPTIONS":
                return oauth_utils.preflight_response(["POST"])

            user, resp = oauth_utils.require_user()
            if not user:
                return resp
            
            data = request.httprequest.get_json(force=True, silent=True) or {}
            subscription_id = data.get('subscription_id')
            pm_id = data.get('payment_method_id')

            if not subscription_id or not pm_id:
                return oauth_utils._json_response(False, {'error': 'Missing subscription_id or payment_method_id'}, status=400)

            subscription = request.env['subscription.subscription'].sudo().browse(int(subscription_id))
            if not subscription.exists() or subscription.customer_name.id != user.partner_id.id:
                return oauth_utils._json_response(False, {'error': 'Subscription not found or access denied'}, status=404)
            
            pm = request.env['stripe.payment.method'].sudo().browse(int(pm_id))
            if not pm.exists() or pm.partner_id.id != user.partner_id.id:
                return oauth_utils._json_response(False, {'error': 'Payment method not found or access denied'}, status=404)

            subscription.write({'payment_vault_id': pm.id})
            
            return oauth_utils._json_response(True, {'message': 'Default payment method updated successfully'})
        except Exception as e:
            _logger.exception("V2 set default payment method failed")
            return oauth_utils._json_response(False, {'error': str(e)}, status=500)

    @http.route('/api/v2/create_setup_session', type='http', auth='public', methods=['POST'], csrf=False)
    def create_setup_session_v2(self, **kwargs):
        """Create a Stripe Setup Checkout Session for a v2 subscription."""
        try:
            if request.httprequest.method == "OPTIONS":
                return oauth_utils.preflight_response(["POST"])

            user, resp = oauth_utils.require_user()
            if not user:
                return resp
            
            data = request.httprequest.get_json(force=True, silent=True) or {}
            subscription_id = data.get('subscription_id')
            
            if not subscription_id:
                return oauth_utils._json_response(False, {'error': 'Missing subscription_id'}, status=400)

            subscription = request.env['subscription.subscription'].sudo().browse(int(subscription_id))
            if not subscription.exists() or subscription.customer_name.id != user.partner_id.id:
                return oauth_utils._json_response(False, {'error': 'Subscription not found or access denied'}, status=404)

            stripe.api_key = request.env['ir.config_parameter'].sudo().get_param('stripe_secret_key')
            
            partner = user.partner_id
            if not partner.stripe_customer_id:
                 stripe_customer = stripe.Customer.create(
                    email=partner.email or user.login,
                    name=partner.name
                )
                 partner.sudo().write({'stripe_customer_id': stripe_customer.id})

            # ---- Get Success and Cancel URLs (V1 Pattern) ----
            fe_url = request.env['ir.config_parameter'].sudo().get_param('frontend_url')
            node_path = 'full' if subscription.subscription_type == 'rpc' else subscription.subscription_type
            route = request.env['ir.config_parameter'].sudo().get_param('stripe_redirection_route') or 'manage/nodes/'
            success_url = f'{fe_url}/{route}{node_path}?status=success&session_id={{CHECKOUT_SESSION_ID}}&action=payment_method_update'
            cancel_url = f'{fe_url}/{route}{node_path}?status=cancel'

            session = stripe.checkout.Session.create(
                customer=partner.stripe_customer_id,
                mode='setup',
                payment_method_types=['card'],
                success_url=success_url,
                cancel_url=cancel_url,
                metadata={
                    'odoo_subscription_id': str(subscription.id),
                    'is_odoo_managed': 'true',
                    'action': 'payment_method_update'
                }
            )

            return oauth_utils._json_response(True, {
                'checkout_url': session.url,
                'session_id': session.id
            })
        except Exception as e:
            _logger.exception("V2 create setup session failed")
            return oauth_utils._json_response(False, {'error': str(e)}, status=500)


    @http.route('/api/v2/subscription/success', type='http', auth='public', methods=['GET'], csrf=False)
    def subscription_success(self, **kwargs):
        """Success confirmation page - webhook handles the actual processing"""
        try:
            session_id = kwargs.get('session_id')
            subscription_id = kwargs.get('subscription_id')
            
            # Simple success page - webhook will handle the actual subscription processing
            html_content = f"""
            <!DOCTYPE html>
            <html>
            <head>
                <title>Subscription Success</title>
                <style>
                    body {{ font-family: Arial, sans-serif; text-align: center; padding: 50px; }}
                    .success {{ color: #28a745; font-size: 24px; margin-bottom: 20px; }}
                    .info {{ color: #666; font-size: 16px; }}
                </style>
            </head>
            <body>
                <div class="success">✅ Payment Successful!</div>
                <div class="info">Your subscription is being processed. You will receive a confirmation email shortly.</div>
                {f'<div class="info">Session ID: {session_id}</div>' if session_id else ''}
                {f'<div class="info">Subscription ID: {subscription_id}</div>' if subscription_id else ''}
            </body>
            </html>
            """
            return Response(html_content, content_type='text/html')
            
        except Exception as e:
            _logger.error("Error showing success page: %s", str(e))
            return Response("Success page error", status=500)

    @http.route('/api/v2/subscription/cancel', type='http', auth='public', methods=['GET'], csrf=False)
    def subscription_cancel(self, **kwargs):
        """Cancel confirmation page"""
        try:
            session_id = kwargs.get('session_id')
            
            # Simple cancel page
            html_content = f"""
            <!DOCTYPE html>
            <html>
            <head>
                <title>Subscription Cancelled</title>
                <style>
                    body {{ font-family: Arial, sans-serif; text-align: center; padding: 50px; }}
                    .cancel {{ color: #dc3545; font-size: 24px; margin-bottom: 20px; }}
                    .info {{ color: #666; font-size: 16px; }}
                </style>
            </head>
            <body>
                <div class="cancel">❌ Payment Cancelled</div>
                <div class="info">Your subscription was not created. You can try again anytime.</div>
                {f'<div class="info">Session ID: {session_id}</div>' if session_id else ''}
            </body>
            </html>
            """
            return Response(html_content, content_type='text/html')
            
        except Exception as e:
            _logger.error("Error showing cancel page: %s", str(e))
            return Response("Cancel page error", status=500)

    @http.route('/api/v2/subscriptions/<int:subscription_id>/cancel', type='http', auth='public', methods=['POST'], csrf=False)
    def cancel_subscription_v2(self, subscription_id, **kwargs):
        """Cancel an Odoo-managed subscription"""
        try:
            if request.httprequest.method == "OPTIONS":
                return oauth_utils.preflight_response(["POST"])
            user, resp = oauth_utils.require_user()
            if not user:
                return resp
            subscription = request.env['subscription.subscription'].sudo().browse(subscription_id)
            if not subscription.exists() or subscription.customer_name.id != user.partner_id.id:
                return oauth_utils._json_response(False, {'error': 'Subscription not found or access denied'}, status=404)
            
            payload = request.httprequest.get_json(force=True, silent=True) or {}
            reason = payload.get('reason') or payload.get('notes')
            
            subscription.action_cancel_v2(reason=reason)
            
            return oauth_utils._json_response(True, {'message': 'Subscription cancelled successfully'})
        except Exception as e:
            _logger.exception("V2 cancel subscription failed")
            return oauth_utils._json_response(False, {'error': str(e)}, status=500)

    @http.route('/api/v2/subscriptions/<int:subscription_id>/pause', type='http', auth='public', methods=['POST'], csrf=False)
    def pause_subscription_v2(self, subscription_id, **kwargs):
        """Pause an Odoo-managed subscription"""
        try:
            if request.httprequest.method == "OPTIONS":
                return oauth_utils.preflight_response(["POST"])
            user, resp = oauth_utils.require_user()
            if not user:
                return resp
            subscription = request.env['subscription.subscription'].sudo().browse(subscription_id)
            if not subscription.exists() or subscription.customer_name.id != user.partner_id.id:
                return oauth_utils._json_response(False, {'error': 'Subscription not found or access denied'}, status=404)
            
            subscription.pause_stripe_subscription()
            
            return oauth_utils._json_response(True, {'message': 'Subscription paused successfully'})
        except Exception as e:
            _logger.exception("V2 pause subscription failed")
            return oauth_utils._json_response(False, {'error': str(e)}, status=500)

    @http.route('/api/v2/subscriptions/<int:subscription_id>/resume', type='http', auth='public', methods=['POST'], csrf=False)
    def resume_subscription_v2(self, subscription_id, **kwargs):
        """Resume an Odoo-managed subscription"""
        try:
            if request.httprequest.method == "OPTIONS":
                return oauth_utils.preflight_response(["POST"])
            user, resp = oauth_utils.require_user()
            if not user:
                return resp
            subscription = request.env['subscription.subscription'].sudo().browse(subscription_id)
            if not subscription.exists() or subscription.customer_name.id != user.partner_id.id:
                return oauth_utils._json_response(False, {'error': 'Subscription not found or access denied'}, status=404)
            
            subscription.resume_stripe_subscription()
            
            return oauth_utils._json_response(True, {'message': 'Subscription resumed successfully'})
        except Exception as e:
            _logger.exception("V2 resume subscription failed")
            return oauth_utils._json_response(False, {'error': str(e)}, status=500)

    @http.route('/api/v2/cards/get_all', type='http', auth='public', methods=['GET'], csrf=False)
    def get_all_cards_v2(self, **kwargs):
        """Get all payment methods (cards) for the user (V2)."""
        try:
            if request.httprequest.method == "OPTIONS":
                return oauth_utils.preflight_response(["GET"])
                
            user, resp = oauth_utils.require_user()
            if not user:
                return resp
            request.update_env(user=user.id)
            
            stripe_secret_key = request.env['ir.config_parameter'].sudo().get_param('stripe_secret_key')
            if not stripe_secret_key:
                return oauth_utils._json_response(False, {'error': 'Stripe secret key not configured.'}, status=500)
            stripe.api_key = stripe_secret_key

            partner = user.partner_id
            stripe_customer_id = partner.stripe_customer_id
            
            if not stripe_customer_id:
                return oauth_utils._json_response(True, {
                    "cards": [],
                    "default_payment_method": None
                })
            
            # Get customer to find default payment method
            customer = stripe.Customer.retrieve(
                stripe_customer_id,
                expand=["invoice_settings.default_payment_method"]
            )

            if customer.get("deleted"):
                return oauth_utils._json_response(False, {'error': 'Stripe customer has been deleted'}, status=400)

            default_pm = None
            if customer.invoice_settings:
                default_pm = customer.invoice_settings.default_payment_method
            
            # List all payment methods
            payment_methods = stripe.PaymentMethod.list(
                customer=stripe_customer_id,
                type="card"
            )
            
            cards = [
                {
                    "id": pm.id,
                    "brand": pm.card.brand,
                    "last4": pm.card.last4,
                    "exp_month": pm.card.exp_month,
                    "exp_year": pm.card.exp_year,
                    "is_default": pm.id == default_pm
                }
                for pm in payment_methods.data
            ]
            
            return oauth_utils._json_response(True, {
                "cards": cards,
                "default_payment_method": default_pm
            })
            
        except stripe.error.StripeError as e:
            _logger.error("Stripe API Error in get_all_cards_v2: %s", str(e))
            return oauth_utils._json_response(False, {'error': f'Stripe Error: {str(e)}'}, status=502)
        except Exception as e:
            _logger.error("Error fetching cards v2: %s", str(e))
            return oauth_utils._json_response(False, {'error': str(e)}, status=500)

    @http.route('/api/v2/cards/add', type='http', auth='public', methods=['POST'], csrf=False)
    def add_card_v2(self, **kwargs):
        """Create SetupIntent for adding a new payment method (card) (V2)."""
        try:
            if request.httprequest.method == "OPTIONS":
                return oauth_utils.preflight_response(["POST"])
                
            user, resp = oauth_utils.require_user()
            if not user:
                return resp
            request.update_env(user=user.id)
            
            stripe_secret_key = request.env['ir.config_parameter'].sudo().get_param('stripe_secret_key')
            if not stripe_secret_key:
                return oauth_utils._json_response(False, {'error': 'Stripe secret key not configured.'}, status=500)
            stripe.api_key = stripe_secret_key

            partner = user.partner_id
            stripe_customer_id = partner.stripe_customer_id
            
            if not stripe_customer_id:
                try:
                    stripe_customer = stripe.Customer.create(
                        email=partner.email or user.login,
                        name=partner.name,
                        metadata={'user_id': user.id, 'partner_id': partner.id}
                    )
                    stripe_customer_id = stripe_customer.id
                    partner.sudo().write({'stripe_customer_id': stripe_customer_id})
                except Exception as e:
                    return oauth_utils._json_response(False, {'error': f'Failed to create customer: {str(e)}'}, status=500)
            
            # Create SetupIntent
            setup_intent = stripe.SetupIntent.create(
                customer=stripe_customer_id,
                payment_method_types=["card"],
                usage="off_session"
            )
            
            return oauth_utils._json_response(True, {
                "client_secret": setup_intent.client_secret,
                "setup_intent_id": setup_intent.id
            })
            
        except stripe.error.StripeError as e:
            _logger.error("Stripe API Error in add_card_v2: %s", str(e))
            return oauth_utils._json_response(False, {'error': f'Stripe Error: {str(e)}'}, status=502)
        except Exception as e:
            _logger.error("Error creating setup intent v2: %s", str(e))
            return oauth_utils._json_response(False, {'error': str(e)}, status=500)

    @http.route('/api/v2/cards/setup-intent', type='http', auth='public', methods=['POST'], csrf=False)
    def create_setup_intent_v2(self, **kwargs):
        """Alias for add_card_v2 to match v1 structure."""
        return self.add_card_v2(**kwargs)

    @http.route('/api/v2/verify_checkout_session', type='http', auth='public', methods=['POST'], csrf=False)
    def verify_checkout_session_v2(self, **kwargs):
        """
        Endpoint for the frontend to proactively trigger a status sync after a successful Stripe payment.
        V2 version supports Odoo-managed subscriptions and PI metadata fallback.
        """
        request_id = str(uuid.uuid4())
        try:
            if request.httprequest.method == "OPTIONS":
                return oauth_utils.preflight_response(["POST"])

            # Auth - using the pattern found in this file
            user, resp = oauth_utils.require_user()
            if not user:
                return resp
            # user = request.env['res.users'].sudo().search([('id','=',129)])
            request.update_env(user=user.id)

            stripe_secret_key = request.env['ir.config_parameter'].sudo().get_param('stripe_secret_key')
            if not stripe_secret_key:
                return oauth_utils._json_response(False, {'error': 'Stripe secret key not configured.'}, status=500)
            stripe.api_key = stripe_secret_key

            data = request.httprequest.get_json(force=True, silent=True) or {}
            session_id = data.get('session_id')
            if not session_id:
                 return oauth_utils._json_response(False, {'error': 'Missing session_id'}, status=400)

            # 4. Retrieve Stripe session
            try:
                session = stripe.checkout.Session.retrieve(session_id)
            except stripe.error.StripeError as e:
                _logger.error("Stripe Session Retrieval Error: %s", str(e))
                return oauth_utils._json_response(False, {'error': f'Stripe Error: {str(e)}'}, status=502)

            # 5. Check payment status
            if session.payment_status != 'paid':
                return oauth_utils._json_response(
                    False,
                    {
                        'status': 'unpaid',
                        'message': 'Checkout session is not paid yet.'
                    },
                    status=400
                )

            metadata = session.get('metadata', {}) or {}
            
            # Fallback to Payment Intent metadata if session metadata is empty (common in PI-based V2 checkouts)
            if not metadata and session.payment_intent:
                try:
                    payment_intent = stripe.PaymentIntent.retrieve(session.payment_intent)
                    metadata = payment_intent.get('metadata', {}) or {}
                except stripe.error.StripeError as e:
                    _logger.warning("Failed to retrieve PaymentIntent metadata: %s", str(e))

            # 6. Rollup flow
            if rollup_util.is_rollup_checkout_session(metadata):
                service, created, metadata_update, _ = rollup_util.finalize_deployment_from_session(
                    user, session
                )

                metadata_update = metadata_update or {}
                service = service.sudo()

                if service.status == "draft":
                    service.action_start_deployment(
                        metadata_update,
                        auto_activate=False
                    )
                else:
                    if metadata_update:
                        combined_metadata = service._combined_metadata(metadata_update)
                        service.write({
                            "metadata_json": combined_metadata
                        })
                        service._link_payment_logs_from_metadata(combined_metadata)

                service._handle_payment_post_activation()
                
                # Invalidate cache to get updated status from DB
                service._invalidate_cache()
                return oauth_utils._json_response(
                    True,
                    {
                        'status': service.status,
                        'service_id': service.id,
                        'session_id': session.id,
                        'message': 'Rollup service status synced successfully.'
                    }
                )

            # 7. Normal subscription flow
            # Support both 'odoo_subscription_id' (V2 Managed) and 'subscription_id' (V1/Legacy)
            subscription_id_meta = metadata.get('odoo_subscription_id') or metadata.get('subscription_id')

            if not subscription_id_meta:
                return oauth_utils._json_response(
                    False,
                    {'error': 'No subscription ID found in session metadata.'},
                    status=400
                )

            subscription = request.env['subscription.subscription'].sudo().browse(
                int(subscription_id_meta)
            )

            if not subscription.exists():
                return oauth_utils._json_response(
                    False,
                    {'error': 'Subscription not found.'},
                    status=404
                )

            if subscription.customer_name.id != user.partner_id.id:
                return oauth_utils._json_response(
                    False,
                    {'error': 'Access denied.'},
                    status=403
                )

            subscription.action_process_checkout_session(session)

            # CRITICAL: Commit transaction and refresh from DB
            request.env.cr.commit()
            subscription._invalidate_cache()
            
            # Re-fetch from database to get actual updated state
            subscription_refreshed = request.env['subscription.subscription'].sudo().browse(
                subscription.id
            )
            subscription_refreshed._invalidate_cache()

            return oauth_utils._json_response(
                True,
                {
                    'status': subscription_refreshed.state,
                    'subscription_id': subscription_refreshed.id,
                    'stripe_subscription_id': subscription_refreshed.stripe_subscription_id,
                    'session_id': session.id,
                    'message': 'Subscription status synced successfully.'
                }
            )

        except Exception as e:
            _logger.exception("Verify checkout session v2 failed")
            return oauth_utils._json_response(
                False,
                {'error': f'Internal Server Error: {str(e)}'},
                status=500
            )

        finally:
            _logger.info("[verify_checkout_session_v2][%s] Request finished", request_id)

    @http.route('/api/v2/cards/confirm', type='http', auth='public', methods=['POST'], csrf=False)
    def confirm_card_setup_v2(self, **kwargs):
        """Confirm card setup and return card details (V2)."""
        try:
            if request.httprequest.method == "OPTIONS":
                return oauth_utils.preflight_response(["POST"])
                
            user, resp = oauth_utils.require_user()
            if not user:
                return resp
            request.update_env(user=user.id)
            
            stripe_secret_key = request.env['ir.config_parameter'].sudo().get_param('stripe_secret_key')
            if not stripe_secret_key:
                return oauth_utils._json_response(False, {'error': 'Stripe secret key not configured.'}, status=500)
            stripe.api_key = stripe_secret_key

            data = request.httprequest.get_json(force=True, silent=True) or {}
            payment_method_id = data.get('payment_method_id')
            
            if not payment_method_id:
                return oauth_utils._json_response(False, {'error': 'Missing payment_method_id'}, status=400)

            partner = user.partner_id
            stripe_customer_id = partner.stripe_customer_id
            
            if not stripe_customer_id:
                return oauth_utils._json_response(False, {'error': 'No customer found'}, status=404)
            
            # Retrieve payment method
            payment_method = stripe.PaymentMethod.retrieve(payment_method_id)
            
            # Verify it belongs to the customer
            if payment_method.customer != stripe_customer_id:
                 return oauth_utils._json_response(False, {'error': 'Payment method does not belong to user'}, status=403)
            
            return oauth_utils._json_response(True, {
                "payment_method_id": payment_method.id,
                "brand": payment_method.card.brand,
                "last4": payment_method.card.last4,
                "exp_month": payment_method.card.exp_month,
                "exp_year": payment_method.card.exp_year
            })
            
        except stripe.error.StripeError as e:
            _logger.error("Stripe API Error in confirm_card_setup_v2: %s", str(e))
            return oauth_utils._json_response(False, {'error': f'Stripe Error: {str(e)}'}, status=502)
        except Exception as e:
            _logger.error("Error confirming card setup v2: %s", str(e))
            return oauth_utils._json_response(False, {'error': str(e)}, status=500)

    @http.route('/api/v2/cards/default', type='http', auth='public', methods=['PUT', 'POST'], csrf=False)
    def set_default_card_v2(self, **kwargs):
        """Set a payment method as default (V2)."""
        try:
            if request.httprequest.method == "OPTIONS":
                return oauth_utils.preflight_response(["PUT", "POST"])
                
            user, resp = oauth_utils.require_user()
            if not user:
                return resp
            request.update_env(user=user.id)
            
            stripe_secret_key = request.env['ir.config_parameter'].sudo().get_param('stripe_secret_key')
            if not stripe_secret_key:
                return oauth_utils._json_response(False, {'error': 'Stripe secret key not configured.'}, status=500)
            stripe.api_key = stripe_secret_key

            data = request.httprequest.get_json(force=True, silent=True) or {}
            payment_method_id = data.get('payment_method_id')
            
            if not payment_method_id:
                return oauth_utils._json_response(False, {'error': 'Missing payment_method_id'}, status=400)

            partner = user.partner_id
            stripe_customer_id = partner.stripe_customer_id
            
            if not stripe_customer_id:
                return oauth_utils._json_response(False, {'error': 'No customer found'}, status=404)
            
            # Update customer default payment method
            stripe.Customer.modify(
                stripe_customer_id,
                invoice_settings={"default_payment_method": payment_method_id}
            )
            
            # Update active subscriptions (both v1 and v2 if they use Stripe tracking)
            subscriptions = request.env['subscription.subscription'].sudo().search([
                ('customer_name', '=', partner.id),
                ('state', 'in', ['in_progress', 'provisioning']),
            ])
            
            for sub in subscriptions:
                if sub.stripe_subscription_id:
                    try:
                        stripe.Subscription.modify(
                            sub.stripe_subscription_id,
                            default_payment_method=payment_method_id
                        )
                    except Exception as e:
                        _logger.warning(f"Failed to update Stripe subscription {sub.id} default payment method: {e}")
            
            return oauth_utils._json_response(True, {
                "message": "Default payment method updated"
            })
            
        except stripe.error.StripeError as e:
            _logger.error("Stripe API Error in set_default_card_v2: %s", str(e))
            return oauth_utils._json_response(False, {'error': f'Stripe Error: {str(e)}'}, status=502)
        except Exception as e:
            _logger.error("Error setting default card v2: %s", str(e))
            return oauth_utils._json_response(False, {'error': str(e)}, status=500)

    @http.route('/api/v2/cards/remove', type='http', auth='public', methods=['DELETE', 'POST'], csrf=False)
    def remove_card_v2(self, **kwargs):
        """Remove a payment method (card) (V2)."""
        try:
            if request.httprequest.method == "OPTIONS":
                return oauth_utils.preflight_response(["DELETE", "POST"])
                
            user, resp = oauth_utils.require_user()
            if not user:
                return resp
            request.update_env(user=user.id)
            
            stripe_secret_key = request.env['ir.config_parameter'].sudo().get_param('stripe_secret_key')
            if not stripe_secret_key:
                return oauth_utils._json_response(False, {'error': 'Stripe secret key not configured.'}, status=500)
            stripe.api_key = stripe_secret_key

            data = request.httprequest.get_json(force=True, silent=True) or {}
            payment_method_id = data.get('payment_method_id') or kwargs.get('payment_method_id')
            
            if not payment_method_id:
                 return oauth_utils._json_response(False, {'error': 'Missing payment_method_id'}, status=400)

            partner = user.partner_id
            stripe_customer_id = partner.stripe_customer_id
            
            if not stripe_customer_id:
                return oauth_utils._json_response(False, {'error': 'No customer found'}, status=404)
            
            # Verify payment method belongs to customer
            payment_method = stripe.PaymentMethod.retrieve(payment_method_id)
            if payment_method.customer != stripe_customer_id:
                return oauth_utils._json_response(False, {'error': 'Payment method does not belong to user'}, status=403)
            
            # Detach payment method
            stripe.PaymentMethod.detach(payment_method_id)
            _logger.info(f"User {user.id} removed card {payment_method_id} via V2")
            
            return oauth_utils._json_response(True, {
                "message": "Payment method removed"
            })
            
        except stripe.error.StripeError as e:
            _logger.error("Stripe API Error in remove_card_v2: %s", str(e))
            return oauth_utils._json_response(False, {'error': f'Stripe Error: {str(e)}'}, status=502)
        except Exception as e:
            _logger.error("Error removing card v2: %s", str(e))
            return oauth_utils._json_response(False, {'error': str(e)}, status=500)
