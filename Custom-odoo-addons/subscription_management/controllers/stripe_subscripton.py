# -*- coding: utf-8 -*-
import stripe
import json
import logging
import base64
from odoo import http, fields
from datetime import datetime, date
from odoo.http import request, Response
from ...auth_module.utils import oauth as oauth_utils
from ..utils import mnemonic_service
from ...rollup_management.utils import deployment_utils, rollup_util
import uuid
import traceback

_logger = logging.getLogger(__name__)

class StripeCheckoutAPI(http.Controller):

    @http.route('/api/v1/create_checkout_session', type='http', auth='public', methods=['POST'], csrf=False)
    def create_checkout_session(self, **kwargs):
        """
        Enhanced endpoint to create a Stripe Checkout Session for a subscription.
        Creates a draft subscription in Odoo and returns checkout URL.
        """
        try:
            if request.httprequest.method == "OPTIONS":
                return oauth_utils.preflight_response(["GET"])
            # Get Stripe configuration
            # 1. Auth
            user, resp = oauth_utils.require_user()
            if not user:
                return resp
            # user = request.env['res.users'].sudo().search([('id','=',72)])
            request.update_env(user=user.id)
            stripe_secret_key = request.env['ir.config_parameter'].sudo().get_param('stripe_secret_key')
            if not stripe_secret_key:
                return oauth_utils._json_response(False, {'error': 'Stripe secret key not configured.'}, status=500)
            
            stripe.api_key = stripe_secret_key

            # Parse request data
            data = request.httprequest.get_json(force=True, silent=True) or {}
            # Validate required fields
            required_fields = [ 'duration','plan_type', 'protocol_id', 'subscription_type']
            for field in required_fields:
                if field not in data:
                    return oauth_utils._json_response(False, {'error': f'Missing required field: {field}'}, status=400)
            
            # Handle discount code if provided
            discount_code = data.get('discount_code')
            discount = None
            if discount_code:
                discount, message = request.env['subscription.discount'].sudo().validate_discount_code(
                    discount_code, 
                    None,  # Will be set after we get the plan
                    data.get('protocol_id'),
                    data.get('amount', 0)
                )
                if not discount:
                    return oauth_utils._json_response(False, {'error': f'Invalid discount code: {message}'}, status=422)

            sub_plan_id = data.get('sub_plan_id')
            duration = data.get('duration')
            customer_email = user.login or user.partner_id.email
            user_id = user.id
            protocol_id = data.get('protocol_id')
            protocol = request.env['protocol.master'].sudo().search([('protocol_id', '=', protocol_id)], limit=1)
            # protocol = request.env['protocol.master'].sudo().search([('id', '=', protocol_id)], limit=1)
            # Get subscription plan
            sub_plan = request.env['subscription.plan'].sudo().search([
                ('protocol_id', '=', protocol.id),
                ('subscription_type', '=', data.get('subscription_type')),
                ('name', '=', data.get('plan_type')),
            ], limit=1)
            # sub_plan = request.env['subscription.plan'].sudo().search([
            #     ('id', '=', 5),
            # ], limit=1)
            _logger.debug('sub plan === %s', sub_plan)
            if not sub_plan.exists():
                return oauth_utils._json_response(False, {'error': f'Subscription plan with ID {sub_plan_id} not found.'}, status=404)

            if not sub_plan.active:
                return oauth_utils._json_response(False, {'error': 'Subscription plan is not active.'}, status=409)

            # Get Stripe price ID and amount based on duration
            stripe_price_id = None
            plan_amount = 0
            
            if duration == 'monthly':
                stripe_price_id = sub_plan.stripe_price_month_id
                plan_amount = sub_plan.amount_month
            elif duration == 'quarterly':
                stripe_price_id = sub_plan.stripe_price_quarter_id
                plan_amount = sub_plan.amount_quarter
            elif duration == 'yearly':
                stripe_price_id = sub_plan.stripe_price_year_id
                plan_amount = sub_plan.amount_year
            else:
                return oauth_utils._json_response(False, {'error': 'Invalid duration. Must be monthly, quarterly, or yearly.'}, status=400)

            if not stripe_price_id:
                return oauth_utils._json_response(False, {'error': f'Stripe price not configured for {duration} billing.'}, status=422)
            try:
                stripe.Price.retrieve(stripe_price_id)
            except stripe.error.InvalidRequestError:
                return oauth_utils._json_response(False, {
                    'error': f'Invalid Stripe Price ID: {stripe_price_id}'
                }, status=422)
            # Find or create customer
            customer = user_id
            if not customer:
                return oauth_utils._json_response(False, {'error': 'Failed to create customer.'}, status=500)
            user = request.env['res.users'].sudo().search([('id','=',user_id)])
            partner_id = user.partner_id
            stripe_customer_id = partner_id.stripe_customer_id

            if not stripe_customer_id:
                # Create new customer in Stripe only if not found
                try:
                    stripe_customer = stripe.Customer.create(
                        email=partner_id.email or user.login,
                        name=partner_id.name,
                    )
                    stripe_customer_id = stripe_customer.id
                    partner_id.sudo().write({'stripe_customer_id': stripe_customer_id})
                    _logger.info(f"Created new Stripe customer {stripe_customer_id} for partner {partner_id.id}")
                except Exception as e:
                    _logger.error("Failed to create Stripe customer: %s", str(e))
            else:
                _logger.info(f"Using existing Stripe customer ID {stripe_customer_id} for partner {partner_id.id}")
            selected_network = request.env['zeeve.network.type'].sudo().search([('name','=',data.get('network_selection'))])
            selected_location = request.env['server.location'].sudo().search([('name','=',data.get('server_location_id'))])
            # Calculate discount if applicable
            final_amount = plan_amount
            discount_amount = 0
            if discount:
                # Re-validate discount with the actual plan
                can_apply, message = discount.can_apply_to_subscription(sub_plan.id, protocol.id)
                if not can_apply:
                    return oauth_utils._json_response(False, {'error': f'Discount not applicable: {message}'}, status=422)
                
                discount_amount = discount.calculate_discount_amount(plan_amount)
                final_amount = plan_amount - discount_amount
            
            # Create draft subscription in Odoo
            existing_sub = request.env['subscription.subscription'].sudo().search([
                ('customer_name', '=', partner_id.id),
                ('sub_plan_id', '=', sub_plan.id),
                ('payment_frequency', '=', duration if duration != "yearly" else "annually"),
                ('stripe_status', 'in', ['active']),
            ], limit=1)
            # Set duration and unit based on frequency
            subscription_vals = {
                'customer_name': partner_id.id,
                'sub_plan_id': sub_plan.id,
                'price': final_amount,
                'original_price': plan_amount,
                'discount_amount': discount_amount,
                'discount_id': discount.id if discount else False,
                'discount_code': discount.code if discount else False,
                'state': 'draft',
                'stripe_status': 'draft',
                'source': 'so',
                'payment_frequency': duration if duration != "yearly" else "annually",
                'autopay_enabled': data.get('autopay_enabled', True),
                'subscription_type': sub_plan.subscription_type,
                'protocol_id': protocol.id,
                'quantity': data.get('quantity', 1),
                'start_date': date.today(),
                }
            node_vals = {
                'node_name': data.get('node_name'),
                'network_selection_id': selected_network.id if selected_network else False,
                'server_location_id': selected_location.id if selected_location else False,
                'software_update_rule': data.get('automatic_update', 'auto'),
                'node_type': sub_plan.subscription_type,
                'state': 'draft',
            }
            if duration == 'monthly':
                subscription_vals.update({'duration': 1, 'unit': 'month'})
            elif duration == 'quarterly':
                subscription_vals.update({'duration': 3, 'unit': 'month'})
            elif duration == 'yearly':
                subscription_vals.update({'duration': 1, 'unit': 'year'})
                        # Get success and cancel URLs
            FE_URL = request.env['ir.config_parameter'].sudo().get_param('frontend_url')
            # FE_URL = 'https://app.zeeve.net'
            node_path = 'full' if sub_plan.subscription_type == 'rpc' else sub_plan.subscription_type
            ROUTE = request.env['ir.config_parameter'].sudo().get_param('stripe_redirection_route') or 'manage/nodes/'
            success_url = f'{FE_URL}/{ROUTE}{node_path}?status=success&session_id={{CHECKOUT_SESSION_ID}}'
            cancel_url = f'{FE_URL}/{ROUTE}{node_path}?status=cancel'
            base_url = request.httprequest.host_url.rstrip('/')
            shardeum_password = data.get('password')
            if shardeum_password and sub_plan.subscription_type == 'validator' and protocol.name == 'Shardeum':
                encrypted_password = mnemonic_service.encrypt_data(request.env,{'shardeum_password':shardeum_password})
                node_vals.update({"validator_info": json.dumps({'shardeum_password' : encrypted_password})})
            # Set duration and unit based on frequency

            # Create the subscription
            if existing_sub and existing_sub.stripe_subscription_id:
                subscription = existing_sub
                try:
                    stripe_sub = stripe.Subscription.retrieve(existing_sub.stripe_subscription_id)
                    current_period_end = stripe_sub.get('current_period_end')
                    current_period_start = stripe_sub.get('current_period_start')

                    if not current_period_end or not current_period_start:
                        items = stripe_sub.get('items', {}).get('data', [])
                        if items and isinstance(items[0], dict):
                            current_period_end = items[0].get('current_period_end')
                            current_period_start = items[0].get('current_period_start')

                    if not current_period_end or not current_period_start:
                        return oauth_utils._json_response(False, {'error': f'current_period_end or current_period_start not'})

                    current_period_end = datetime.utcfromtimestamp(int(current_period_end))
                    current_period_start = datetime.utcfromtimestamp(int(current_period_start))
                    total_days = (current_period_end - current_period_start).days or 30
                    days_left = max(0, (current_period_end - datetime.utcnow()).days)
                except Exception as e:
                    total_days, days_left = 30, 15

                prorated_amount = round(plan_amount * (days_left / total_days), 2)
                varOcg = prorated_amount
                desired_increase = int(data.get('quantity', 1))

                if prorated_amount <= 0:
                    existing_sub.sudo().write({
                        'pending_quantity_increase': desired_increase,
                        'pending_quantity_paid': True
                    })
                    return oauth_utils._json_response(True, {
                        'message': 'No prorated amount to charge. Quantity will update at next billing.',
                        'subscription_id': existing_sub.id
                    })

                # Create one-time payment checkout for prorated amount
              
                if stripe_customer_id:
                    # Store in new model
                    charge_id =  request.env['subscription.prorated.charge'].sudo().search([('subscription_id','=',existing_sub.id),('state','=','draft')])
                    if not charge_id:
                        charge_id = request.env['subscription.prorated.charge'].sudo().create({
                            'subscription_id': existing_sub.id,
                            'amount': prorated_amount,
                            'quantity_increase': desired_increase,
                            'stripe_subscription_id': existing_sub.stripe_subscription_id,
                            'state': 'draft',
                        })
                    try:
                        # verify stored customer exists in Stripe
                        stripe.Customer.retrieve(partner_id.stripe_customer_id)
                        one_time_session = stripe.checkout.Session.create(
                            customer=partner_id.stripe_customer_id,
                            success_url=f"{success_url}&action=prorated_charge_for_qty_increase",
                            cancel_url=cancel_url,
                            payment_method_types=["card"],
                            allow_promotion_codes=True,
                            mode="payment",
                            line_items=[{
                                'price_data': {
                                    'currency': 'usd',
                                    'product_data': {'name': f'Prorated charge for {sub_plan.name}'},
                                    'unit_amount': int(prorated_amount * 100),
                                },
                                'quantity': 1,
                            }],
                            metadata={
                                'customer_id': str(partner_id.id),
                                'action': 'prorated_charge_for_qty_increase',
                                "charge_id":charge_id.id,
                                'subscription_id': str(existing_sub.id),
                                'quantity_increase': str(desired_increase),
                                'stripe_subscription_id': existing_sub.stripe_subscription_id
                            },
                            billing_address_collection='required'
                        )
                        _logger.info("Reusing Stripe customer %s for prorated payment.", partner_id.stripe_customer_id)
                    except stripe.error.InvalidRequestError:
                        # fallback to email if stored ID is invalid
                        _logger.warning("Stored Stripe customer invalid, falling back to email for prorated session.")
                        one_time_session = stripe.checkout.Session.create(
                            customer_email=customer_email,
                            success_url=success_url,
                            cancel_url=cancel_url,
                            payment_method_types=["card"],
                            allow_promotion_codes=True,
                            mode="payment",
                            line_items=[{
                                'price_data': {
                                    'currency': 'usd',
                                    'product_data': {'name': f'Prorated charge for {sub_plan.name}'},
                                    'unit_amount': int(prorated_amount * 100),
                                },
                                'quantity': 1,
                            }],
                            metadata={
                                'customer_id': str(partner_id.id),
                                "charge_id":charge_id.id,
                                'action': 'prorated_charge_for_qty_increase',
                                'subscription_id': str(existing_sub.id),
                                'quantity_increase': str(desired_increase),
                                'stripe_subscription_id': existing_sub.stripe_subscription_id
                            },
                            billing_address_collection='required'
                        )
                else:
                    # no stored Stripe ID → fallback to email
                    one_time_session = stripe.checkout.Session.create(
                        customer_email=customer_email,
                        success_url = f"{success_url}&action=prorated_charge_for_qty_increase",
                        cancel_url = cancel_url,
                        payment_method_types=["card"],
                        allow_promotion_codes=True,
                        mode="payment",
                        line_items=[{
                            'price_data': {
                                'currency': 'usd',
                                'product_data': {'name': f'Prorated charge for {sub_plan.name}'},
                                'unit_amount': int(prorated_amount * 100),
                            },
                            'quantity': 1,
                        }],
                        metadata={
                            'customer_id': str(partner_id.id),
                            'action': 'prorated_charge_for_qty_increase',
                            'subscription_id': str(existing_sub.id),
                            "charge_id":charge_id.id,
                            'quantity_increase': str(desired_increase),
                            'stripe_subscription_id': existing_sub.stripe_subscription_id
                        },
                        billing_address_collection='required'
                    )
                
                
                existing_sub.sudo().write({
                    'pending_quantity_increase': desired_increase,
                    'pending_quantity_paid': False,
                    'pending_quantity_prorated_amount': prorated_amount,
                })
                node_payload = dict(node_vals)
                if node_payload.get('software_update_rule') is None:
                    node_payload['software_update_rule'] = 'auto'
                existing_sub.create_primary_node(node_payload)
                if charge_id:
                    charge_id.write({
                    'session_id': one_time_session.id,
                    'checkout_url': one_time_session.url,
                    })
                return oauth_utils._json_response(True, {
                    'checkout_url': one_time_session.url,
                    'session_id': one_time_session.id,
                    'subscription_id': existing_sub.id,
                    'amount': prorated_amount,
                    'status': 'proration_required'
                })
            else:
                product = request.env["product.product"].sudo().search([('name', 'ilike', sub_plan.subscription_type)], limit=1)
                
                subscription = request.env['subscription.subscription'].sudo().create(subscription_vals)
                node_payload = dict(node_vals)
                if node_payload.get('software_update_rule') is None:
                    node_payload['software_update_rule'] = 'auto'
                subscription.create_primary_node(node_payload)
            
            # Prepare metadata for Stripe
            metadata = {
                'subscription_id': str(subscription.id),
                'customer_id': str(partner_id.id),
                'plan_id': str(sub_plan.id),
                'product_id': str(sub_plan.product_id.id) if sub_plan.product_id else '',
                'autopay_enabled': str(data.get('autopay_enabled', True)).lower(),
                'subscription_type': data.get('subscription_type', 'rpc'),
                'protocol_id': str(protocol.id),
                'network_type': selected_network.name if selected_network else '',
                'server_location_id': selected_location.name if selected_location else '',
                'automatic_update': str(node_vals.get('software_update_rule', 'auto')).lower(),
            }
            
            # Add discount information to metadata
            if discount:
                metadata.update({
                    'discount_id': str(discount.id),
                    'discount_code': discount.code,
                    'discount_amount': str(discount_amount),
                    'original_amount': str(plan_amount),
                })


            # Create Stripe Checkout Session
            if stripe_customer_id:
                checkout_session_data = {
                    'customer': stripe_customer_id,
                    'success_url': success_url,
                    'cancel_url': cancel_url,
                    'payment_method_types': ["card"],
                    'mode': "subscription",
                    'line_items': [{
                        "price": stripe_price_id,
                        "quantity": data.get('quantity', 1)
                    }],
                    'metadata': metadata,
                    'subscription_data': {
                        'metadata': metadata,
                        'trial_period_days': data.get('trial_days', 0) if data.get('trial_days', 0) > 0 else None,
                    },
                    'allow_promotion_codes': True,
                    'billing_address_collection': 'required',
                }
            else:
                checkout_session_data = {
                    'customer_email': customer_email,
                    'success_url': success_url,
                    'cancel_url': cancel_url,
                    'payment_method_types': ["card"],
                    'mode': "subscription",
                    'line_items': [{
                        "price": stripe_price_id,
                        "quantity": data.get('quantity', 1)
                    }],
                    'metadata': metadata,
                    'subscription_data': {
                        'metadata': metadata,
                        'trial_period_days': data.get('trial_days', 0) if data.get('trial_days', 0) > 0 else None,
                    },
                    'allow_promotion_codes': True,
                    'billing_address_collection': 'required',
                }

            
            if discount and discount.stripe_coupon_id:
                checkout_session_data.pop('allow_promotion_codes', None)
                checkout_session_data['discounts'] = [{'coupon': discount.stripe_coupon_id}]
            _logger.debug('checkout sesssion data === %s', checkout_session_data)
            checkout_session = stripe.checkout.Session.create(**checkout_session_data)

            _logger.info(f"Created checkout session {checkout_session.id} for subscription {subscription.id}")

            response_data = {
                'checkout_url': checkout_session.url,
                'session_id': checkout_session.id,
                'subscription_id': subscription.id,
                'amount': final_amount,
                'original_amount': plan_amount,
                'currency': 'usd',
                'status': 'created'
            }
            
            # Add discount information to response
            if discount:
                response_data.update({
                    'discount_applied': True,
                    'discount_code': discount.code,
                    'discount_amount': discount_amount,
                    'discount_type': discount.discount_type,
                    'discount_value': discount.discount_value,
                })
            else:
                response_data['discount_applied'] = False
            
            return oauth_utils._json_response(True, response_data)

        except stripe.error.StripeError as e:
            _logger.error("Stripe API Error: %s", str(e))
            return oauth_utils._json_response(False, {'error': f'Stripe Error: {str(e)}'}, status=502)
        except Exception as e:
            _logger.error("Internal Server Error: %s\n%s", str(e), traceback.format_exc())
            return oauth_utils._json_response(False, {'error': f'Internal Server Error: {str(e)}'}, status=500)

    @http.route('/api/v1/retry_create_checkout_session', type='http', auth='public', methods=['POST'], csrf=False)
    def retry_checkout_session(self, **kwargs):
        """
        Retry endpoint to recreate a Stripe Checkout Session for a Draft subscription.
        Takes subscription_id in body.
        """
        try:
            if request.httprequest.method == "OPTIONS":
                return oauth_utils.preflight_response(["POST"])
            
            # 1. Auth
            user, resp = oauth_utils.require_user()
            if not user:
                return resp
            # user = request.env['res.users'].sudo().search([('id','=',56)])
            request.update_env(user=user.id)
            
            stripe_secret_key = request.env['ir.config_parameter'].sudo().get_param('stripe_secret_key')
            if not stripe_secret_key:
                return oauth_utils._json_response(False, {'error': 'Stripe secret key not configured.'}, status=500)
            
            stripe.api_key = stripe_secret_key

            # Parse request data
            data = request.httprequest.get_json(force=True, silent=True) or {}
            subscription_id = data.get('subscription_id')

            if not subscription_id:
                return oauth_utils._json_response(False, {'error': 'Missing required field: subscription_id'}, status=400)

            # Fetch Subscription
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

            if not subscription.exists():
                return oauth_utils._json_response(False, {'error': 'Subscription not found or does not belong to user.'}, status=404)
            
            if subscription.state != 'draft':
                 return oauth_utils._json_response(False, {'error': f'Subscription is in {subscription.state} state, not draft. Cannot retry.'}, status=422)

            # Re-construct necessary data for Stripe Session
            sub_plan = subscription.sub_plan_id
            protocol = subscription.protocol_id
            partner_id = subscription.customer_name
            
            if not sub_plan.active:
                 return oauth_utils._json_response(False, {'error': 'Subscription plan is no longer active.'}, status=409)

            # Determine Price ID based on frequency
            duration = subscription.payment_frequency 
            
            stripe_price_id = None
            if subscription.payment_frequency == 'monthly':
                stripe_price_id = sub_plan.stripe_price_month_id
            elif subscription.payment_frequency == 'quarterly':
                stripe_price_id = sub_plan.stripe_price_quarter_id
            elif subscription.payment_frequency == 'annually' or subscription.payment_frequency == 'yearly':
                 stripe_price_id = sub_plan.stripe_price_year_id
            
            if not stripe_price_id:
                return oauth_utils._json_response(False, {'error': f'Stripe price not configured for {subscription.payment_frequency} billing.'}, status=422)

            # Customer
            stripe_customer_id = partner_id.stripe_customer_id
            if not stripe_customer_id:
                 stripe_customer = self._create_stripe_customer(partner_id, {})
                 if stripe_customer:
                    stripe_customer_id = stripe_customer['id']
                    partner_id.stripe_customer_id = stripe_customer_id
            FE_URL = request.env['ir.config_parameter'].sudo().get_param('frontend_url')
            node_path = 'full' if sub_plan.subscription_type == 'rpc' else sub_plan.subscription_type
            ROUTE = request.env['ir.config_parameter'].sudo().get_param('stripe_redirection_route') or 'manage/nodes/'
            success_url = f'{FE_URL}/{ROUTE}{node_path}?status=success&session_id={{CHECKOUT_SESSION_ID}}'
            cancel_url = f'{FE_URL}/{ROUTE}{node_path}?status=cancel'

            
            metadata = {
                'subscription_id': str(subscription.id),
                'customer_id': str(partner_id.id),
                'plan_id': str(sub_plan.id),
                'product_id': str(sub_plan.product_id.id) if sub_plan.product_id else '',
                'autopay_enabled': str(subscription.autopay_enabled).lower(),
                'subscription_type': subscription.subscription_type,
                'protocol_id': str(protocol.id),
            }
            
            
            discount = subscription.discount_id
            if discount:
                 metadata.update({
                    'discount_id': str(discount.id),
                    'discount_code': discount.code,
                    'discount_amount': str(subscription.discount_amount),
                    'original_amount': str(subscription.original_price),
                })
            
            checkout_session_data = {
                'customer': stripe_customer_id,
                'success_url': success_url,
                'cancel_url': cancel_url,
                'payment_method_types': ["card"],
                'mode': "subscription",
                'line_items': [{
                    "price": stripe_price_id,
                    "quantity": int(subscription.quantity)
                }],
                'metadata': metadata,
                'subscription_data': {
                    'metadata': metadata
                },
                'allow_promotion_codes': True,
                'billing_address_collection': 'required',
            }

            if not stripe_customer_id:
                 checkout_session_data.pop('customer')
                 checkout_session_data['customer_email'] = partner_id.email

            if discount and discount.stripe_coupon_id:
                checkout_session_data.pop('allow_promotion_codes', None)
                checkout_session_data['discounts'] = [{'coupon': discount.stripe_coupon_id}]
            
            checkout_session = stripe.checkout.Session.create(**checkout_session_data)
            
            _logger.info(f"Retried/Recreated checkout session {checkout_session.id} for subscription {subscription.id}")

            response_data = {
                'checkout_url': checkout_session.url,
                'session_id': checkout_session.id,
                'subscription_id': subscription.id,
                'amount': subscription.price,
                'original_amount': subscription.original_price,
                'currency': 'usd',
                'status': 'created'
            }
            if discount:
                response_data.update({
                    'discount_applied': True,
                    'discount_code': discount.code,
                    'discount_amount': subscription.discount_amount,
                })
            else:
                 response_data['discount_applied'] = False

            return oauth_utils._json_response(True, response_data)

        except stripe.error.StripeError as e:
            _logger.error("Stripe API Error: %s", str(e))
            return oauth_utils._json_response(False, {'error': f'Stripe Error: {str(e)}'}, status=502)
        except Exception as e:
            _logger.error("Internal Server Error: %s", str(e), traceback.format_exc())
            return oauth_utils._json_response(False, {'error': f'Internal Server Error: {str(e)}'}, status=500)
        except Exception as e:
            _logger.error("Internal Server Error: %s", str(e), traceback.format_exc())
            return oauth_utils._json_response(False, {'error': f'Internal Server Error: {str(e)}'}, status=500)

    @http.route('/api/v1/retry_prorated_charge', type='http', auth='public', methods=['POST'], csrf=False)
    def retry_prorated_charge(self, **kwargs):
        """
        Retry a prorated charge payment. 
        Recalculates the amount based on current time.
        Updates the charge record and returns new checkout URL.
        """
        try:
            if request.httprequest.method == "OPTIONS":
                return oauth_utils.preflight_response(["POST"])

            user, resp = oauth_utils.require_user()
            if not user:
                return resp
            request.update_env(user=user.id)
            
            stripe_secret_key = request.env['ir.config_parameter'].sudo().get_param('stripe_secret_key')
            stripe.api_key = stripe_secret_key

            data = request.httprequest.get_json(force=True, silent=True) or {}
            charge_id = data.get('charge_id')
            
            if not charge_id:
                  return oauth_utils._json_response(False, {'error': 'Missing required field: charge_id'}, status=400)

            # Fetch Charge
            charge = request.env['subscription.prorated.charge'].sudo().search([
                ('id', '=', charge_id),
                ('subscription_id.customer_name', '=', user.partner_id.id)
            ], limit=1)
            
            if not charge.exists():
                return oauth_utils._json_response(False, {'error': 'Charge not found or access denied.'}, status=404)
            
            if charge.state != 'draft':
                 return oauth_utils._json_response(False, {'error': f'Charge is in {charge.state} state. Cannot retry.'}, status=422)

            subscription = charge.subscription_id
            sub_plan = subscription.sub_plan_id
            partner_id = subscription.customer_name

            if not subscription.stripe_subscription_id:
                 return oauth_utils._json_response(False, {'error': 'Linked subscription has no Stripe ID.'}, status=500)
            
            try:
                stripe_sub = stripe.Subscription.retrieve(subscription.stripe_subscription_id)
                current_period_end = stripe_sub.get('current_period_end')
                current_period_start = stripe_sub.get('current_period_start')

                if not current_period_end or not current_period_start:
                    # fallback to items
                     items = stripe_sub.get('items', {}).get('data', [])
                     if items and isinstance(items[0], dict):
                        current_period_end = items[0].get('current_period_end')
                        current_period_start = items[0].get('current_period_start')
                
                if not current_period_end or not current_period_start:
                     return oauth_utils._json_response(False, {'error': 'Could not determine billing period from Stripe.'}, status=500)

                current_period_end = datetime.utcfromtimestamp(int(current_period_end))
                current_period_start = datetime.utcfromtimestamp(int(current_period_start))
                total_days = (current_period_end - current_period_start).days or 30
                days_left = max(0, (current_period_end - datetime.utcnow()).days)
                
            except Exception as e:
                _logger.error(f"Error retrieving stripe details for recalculation: {e}")
                total_days, days_left = 30, 15

            plan_amount = 0
            if subscription.payment_frequency == 'monthly':
                plan_amount = sub_plan.amount_month
            elif subscription.payment_frequency == 'quarterly':
                plan_amount = sub_plan.amount_quarter
            elif subscription.payment_frequency == 'annually':
                plan_amount = sub_plan.amount_year
            
            prorated_amount = round(plan_amount * (days_left / total_days), 2)
            desired_increase = charge.quantity_increase
            
            # If amount changed, update records
            if prorated_amount != charge.amount:
                 charge.sudo().write({'amount': prorated_amount})
                 subscription.sudo().write({'pending_quantity_prorated_amount': prorated_amount})
            
            if prorated_amount <= 0:
                return oauth_utils._json_response(False, {'error': 'Prorated amount is 0. Please wait for renewal.'}, status=422)


            FE_URL = request.env['ir.config_parameter'].sudo().get_param('frontend_url')
            node_path = 'full' if sub_plan.subscription_type == 'rpc' else sub_plan.subscription_type
            ROUTE = request.env['ir.config_parameter'].sudo().get_param('stripe_redirection_route') or 'manage/nodes/'
            success_url = f'{FE_URL}/{ROUTE}{node_path}?status=success&session_id={{CHECKOUT_SESSION_ID}}&action=prorated_charge_for_qty_increase'
            cancel_url = f'{FE_URL}/{ROUTE}{node_path}?status=cancel'

            customer_email = user.login or user.partner_id.email
            stripe_customer_id = partner_id.stripe_customer_id
            
            session_data = {
                'success_url': success_url,
                'cancel_url': cancel_url,
                'payment_method_types': ["card"],
                'allow_promotion_codes': True,
                'mode': "payment",
                'line_items': [{
                    'price_data': {
                        'currency': 'usd',
                        'product_data': {'name': f'Prorated charge for {sub_plan.name}'},
                        'unit_amount': int(prorated_amount * 100),
                    },
                    'quantity': 1,
                }],
                 'metadata': {
                    'customer_id': str(partner_id.id),
                    'action': 'prorated_charge_for_qty_increase',
                    'subscription_id': str(subscription.id),
                    'quantity_increase': str(desired_increase),
                    'stripe_subscription_id': subscription.stripe_subscription_id,
                    'charge_id': str(charge.id)
                },
                'billing_address_collection': 'required'
            }
            
            if stripe_customer_id:
                session_data['customer'] = stripe_customer_id
            else:
                 session_data['customer_email'] = customer_email
            
            new_session = stripe.checkout.Session.create(**session_data)
            
            # Update Charge Record
            charge.sudo().write({
                'session_id': new_session.id,
                'checkout_url': new_session.url
            })
            
            return oauth_utils._json_response(True, {
                'checkout_url': new_session.url,
                'session_id': new_session.id,
                'amount': prorated_amount,
                'message': 'Session recreated with recalculated amount.'
            })

        except stripe.error.StripeError as e:
            _logger.error("Stripe API Error: %s", str(e))
            return oauth_utils._json_response(False, {'error': f'Stripe Error: {str(e)}'}, status=502)
        except Exception as e:
            _logger.error("Internal Server Error: %s", str(e), traceback.format_exc())
            return oauth_utils._json_response(False, {'error': f'Internal Server Error: {str(e)}'}, status=500)

    def _find_or_create_customer(self, email, data):
        """Find existing customer or create new one"""
        try:
            # First try to find existing customer by email
            customer = request.env['res.partner'].sudo().search([
                ('email', '=', email),
                ('is_company', '=', False)
            ], limit=1)
            
            if customer:
                # Update Stripe customer ID if not set
                if not customer.stripe_customer_id:
                    stripe_customer = self._create_stripe_customer(customer, data)
                    if stripe_customer:
                        customer.stripe_customer_id = stripe_customer['id']
                return customer
            
            # Create new customer
            customer_vals = {
                'name': data.get('customer_name', email.split('@')[0]),
                'email': email,
                'is_company': False,
                'customer_rank': 1,
            }
            
            # Add additional fields if provided
            if 'customer_phone' in data:
                customer_vals['phone'] = data['customer_phone']
            if 'customer_address' in data:
                customer_vals['street'] = data['customer_address']
            if 'customer_city' in data:
                customer_vals['city'] = data['customer_city']
            if 'customer_country' in data:
                country = request.env['res.country'].sudo().search([('code', '=', data['customer_country'])], limit=1)
                if country:
                    customer_vals['country_id'] = country.id
            
            customer = request.env['res.partner'].sudo().create(customer_vals)
            
            # Create Stripe customer
            stripe_customer = self._create_stripe_customer(customer, data)
            if stripe_customer:
                customer.stripe_customer_id = stripe_customer['id']
            
            return customer
            
        except Exception as e:
            _logger.error("Error creating customer: %s", str(e))
            return None

    def _create_stripe_customer(self, customer, data):
        """Create Stripe customer"""
        try:
            stripe.api_key = request.env['ir.config_parameter'].sudo().get_param('stripe_secret_key')
            
            customer_data = {
                'email': customer.email,
                'name': customer.name,
                'metadata': {
                    'odoo_customer_id': str(customer.id),
                    'source': 'odoo_subscription'
                }
            }
            
            # Add address if available
            if customer.street or customer.city or customer.country_id:
                address = {}
                if customer.street:
                    address['line1'] = customer.street
                if customer.city:
                    address['city'] = customer.city
                if customer.country_id:
                    address['country'] = customer.country_id.code
                if customer.zip:
                    address['postal_code'] = customer.zip
                if address:
                    customer_data['address'] = address
            
            return stripe.Customer.create(customer_data)
            
        except Exception as e:
            _logger.error("Error creating Stripe customer: %s", str(e))
            return None

    @http.route('/api/v1/subscription/success', type='http', auth='public', methods=['GET'], csrf=False)
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

    @http.route('/api/v1/subscription/cancel', type='http', auth='public', methods=['GET'], csrf=False)
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

    @http.route('/api/v1/subscription/status/<int:subscription_id>', type='http', auth='public', methods=['GET'], csrf=False)
    def subscription_status(self, subscription_id, **kwargs):
        """Get subscription status"""
        try:
            user, resp = oauth_utils.require_user()
            if not user:
                return resp
            subscription = request.env['subscription.subscription'].sudo().browse(subscription_id)
            if not subscription.exists():
                return oauth_utils._json_response(False, {'error': 'Subscription not found'}, status=404)
            
            return oauth_utils._json_response(True, {
                'subscription_id': subscription.id,
                'name': subscription.name,
                'state': subscription.state,
                'stripe_subscription_id': subscription.stripe_subscription_id,
                'stripe_status': subscription.stripe_status,
                'autopay_enabled': subscription.autopay_enabled,
                'price': subscription.price,
                'currency': subscription.currency_id.name,
                'customer_name': subscription.customer_name.name,
                'customer_email': subscription.customer_name.email,
                'start_date': subscription.start_date.isoformat() if subscription.start_date else None,
                'end_date': subscription.end_date.isoformat() if subscription.end_date else None,
                'next_payment_date': subscription.next_payment_date.isoformat() if subscription.next_payment_date else None,
            })
            
        except Exception as e:
            _logger.error("Error getting subscription status: %s", str(e))
            return oauth_utils._json_response(False, {'error': str(e)}, status=500)

    @http.route('/api/v1/prorated_subscriptions', type='http', auth='public', methods=['GET'], csrf=False)
    def get_prorated_subscriptions(self, **kwargs):
        """
        Get all draft prorated charges for the logged-in user.
        """
        try:
            user, resp = oauth_utils.require_user()
            if not user:
                return resp
            
            # Find all subscriptions belonging to this user that have draft prorated charges
            # Actually, better to query the charges directly, but ensure subscription belongs to user.
            
            charges = request.env['subscription.prorated.charge'].sudo().search([
                ('subscription_id.customer_name', '=', user.partner_id.id),
                ('state', '=', 'draft')
            ])
            
            data = []
            for charge in charges:
                sub = charge.subscription_id
                data.append({
                    'charge_id': charge.id,
                    'subscription_id': sub.id,
                    'subscription_name': sub.name,
                    'plan_name': sub.sub_plan_id.name,
                    'protocol_name': sub.protocol_id.name,
                    'amount': charge.amount,
                    'quantity_increase': charge.quantity_increase,
                    'checkout_url': charge.checkout_url,
                    'session_id': charge.session_id,
                    'created_at': charge.create_date.isoformat() if charge.create_date else None
                })
                
            return oauth_utils._json_response(True, {'prorated_charges': data})

        except Exception as e:
            _logger.error("Error getting prorated subscriptions: %s", str(e), traceback.format_exc())
            return oauth_utils._json_response(False, {'error': f'Internal Server Error: {str(e)}'}, status=500)

    @http.route('/api/v1/create-invoice', type='http', auth='public', methods=['POST'], csrf=False)
    def create_test(self, **kwargs):
        """
        Enhanced endpoint to create a Stripe Checkout Session for a subscription.
        Creates a draft subscription in Odoo and returns checkout URL.
        """
        try:
            # Parse request data
            data = request.httprequest.get_json(force=True, silent=True) or {}
            id = data.get('id')
            key = data.get("key")
            _logger.info("----------------5-------------- %s", key)

            if key and key == "zeeve16102062":
                
                subscription = request.env['subscription.subscription'].sudo().search([
                        ('id', '=', int(id))
                    ], limit=1)
                _logger.info("----------------8-------------- %s", subscription)
                
                data = subscription.create_invoice()
                
                return oauth_utils._json_response(False,{"data":data}, status=400)
            elif key and key == "rollup_service":
                stripe_data = data.get("stripe_data")
                rollup_service = request.env['rollup.service'].sudo().search([
                        ('id', '=', int(id))
                    ], limit=1)
                data = rollup_service.create_invoice(stripe_data)
            
                return oauth_utils._json_response(True,{"data":data})
            else:
                return oauth_utils._json_response(False,{"data":False}, status=404)
        except Exception as e:
            _logger.error("Internal Server Error: %s", str(e),traceback.format_exc())
            return oauth_utils._json_response(False, {'error': f'Internal Server Error: {str(e)}'}, status=500)
    @http.route('/api/download_invoice/<int:attachment_id>', type='http', auth='public', methods=['GET'], csrf=False)
    def download_invoice(self, attachment_id, **kwargs):
        """
        Download PDF invoice by attachment_id.
        """
        attachment = request.env['ir.attachment'].sudo().browse(attachment_id)
        if not attachment.exists() or not attachment.datas:
            return request.not_found()

        pdf_content = base64.b64decode(attachment.datas)
        filename = attachment.name or f"Invoice_{attachment_id}.pdf"
        return request.make_response(
            pdf_content,
            headers=[
                ('Content-Type', 'application/pdf'),
                ('Content-Disposition', f'attachment; filename="{filename}"')
            ]
        )
    @http.route('/api/v1/my_invoices', type='http', auth='public', methods=['GET'], csrf=False)
    def get_user_invoices(self, **kwargs):
        """
        Return all invoices of the logged-in user (validated via OAuth)
        Each invoice includes PDF download link
        """
        # ✅ Validate user (requires access token)
        data = request.httprequest.get_json(force=True, silent=True) or {}
        if request.httprequest.method == "OPTIONS":
            return oauth_utils.preflight_response(["GET"])

        user, resp = oauth_utils.require_user()
        if not user:
            return resp

        try:
            partner = user.partner_id
            if not partner:
                return oauth_utils._json_response(False, {'error': f'No partner linked to this user'}, status=404)
            invoices = request.env['account.move'].sudo().search([
                ('partner_id', '=', partner.id)
            ])

            invoice_list = []
            backend_url = request.env['ir.config_parameter'].sudo().get_param('backend_url')
            for inv in invoices:    
                try:
                    attachment = request.env['ir.attachment'].sudo().search([('res_id','=',inv.id)],limit=1)
                    if attachment and attachment.id:
                        attachment_url = f"/api/download_invoice/{attachment.id}"
                    else:
                        attachment_url = False

                    invoice_list.append({
                        "invoice_id": inv.id,
                        "invoice_number": inv.name,
                        "date": inv.invoice_date,
                        "amount_total": inv.amount_total,
                        "state": inv.state,
                        "download_url": backend_url + attachment_url,
                        "product": inv.invoice_line_ids.mapped('product_id').name,
                        "node_id": inv.node_id.node_identifier if inv.node_id else None,
                        "service_id": inv.rollup_service_id.service_id if inv.rollup_service_id else None,
                    })

                except Exception as inv_err:
                    continue
            res = {
                "status": "success",
                "count": len(invoice_list),
                "invoices": invoice_list
            }
            return oauth_utils._json_response(True,{"data":res})

        except Exception as e:
            return oauth_utils._json_response(False, {'error': f'Internal Server Error: {str(e)}'}, status=500)

    @http.route('/api/v1/verify_checkout_session', type='http', auth='public', methods=['POST'], csrf=False)
    def verify_checkout_session(self, **kwargs):
        """
        Endpoint for the frontend to proactively trigger a status sync after a successful Stripe payment.
        Accepts session_id in the body.
        """
        request_id = str(uuid.uuid4())  # unique trace id

        try:
            if request.httprequest.method == "OPTIONS":
                return oauth_utils.preflight_response(["POST"])

            # 1. Auth
            user, resp = oauth_utils.require_user()
            if not user:
                return resp
            # user = request.env['res.users'].sudo().search([('id','=',63)])

            request.update_env(user=user.id)

            # 2. Get Stripe key
            stripe_secret_key = request.env['ir.config_parameter'].sudo().get_param('stripe_secret_key')

            if not stripe_secret_key:
                return oauth_utils._json_response(
                    False,
                    {'error': 'Stripe secret key not configured.'},
                    status=500
                )

            stripe.api_key = stripe_secret_key

            # 3. Parse request
            data = request.httprequest.get_json(force=True, silent=True) or {}
            session_id = data.get('session_id')
            if not session_id:
                return oauth_utils._json_response(
                    False,
                    {'error': 'Missing required field: session_id'},
                    status=400
                )

            # 4. Retrieve Stripe session
            try:
                session = stripe.checkout.Session.retrieve(session_id)


            except stripe.error.StripeError as e:
                _logger.error("Stripe Session Retrieval Error: %s", str(e))
                return oauth_utils._json_response(False, {'error': f'Stripe Error: {str(e)}'}, status=502)
            # _logger.debug('session: %s', session)
                _logger.error(
                    "[verify_checkout_session][%s] Stripe Session Retrieval Error session_id=%s error=%s",
                    request_id, session_id, str(e)
                )
                return oauth_utils._json_response(
                    False,
                    {'error': f'Stripe Error: {str(e)}'},
                    status=502
                )

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
                return oauth_utils._json_response(
                    True,
                    {
                        'status': service_refreshed.status,
                        'service_id': service_refreshed.id,
                        'session_id': session.id,
                        'message': 'Rollup service status synced successfully.'
                    }
                )

            # 7. Normal subscription flow
            subscription_id_meta = metadata.get('subscription_id')


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
            subscription_id_db = subscription.id
            
            # Re-fetch from database to get actual updated state
            subscription_refreshed = request.env['subscription.subscription'].sudo().browse(
                subscription_id_db
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

            return oauth_utils._json_response(
                False,
                {'error': f'Internal Server Error: {str(e)}'},
                status=500
            )

        finally:
            _logger.info("[verify_checkout_session][%s] Request finished", request_id)

    @http.route('/api/v1/cards/get_all', type='http', auth='public', methods=['GET'], csrf=False)
    def get_all_cards(self, **kwargs):
        """Get all payment methods (cards) for the user."""
        try:
            if request.httprequest.method == "OPTIONS":
                return oauth_utils.preflight_response(["GET"])
                
            # Auth
            user, resp = oauth_utils.require_user()
            if not user:
                return resp
            # user = request.env['res.users'].sudo().search([('id','=',56)])
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
            print(stripe_customer_id,'-------------------1131',customer)
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
            _logger.error("Stripe API Error in get_all_cards: %s", str(e))
            return oauth_utils._json_response(False, {'error': f'Stripe Error: {str(e)}'}, status=502)
        except Exception as e:
            _logger.error("Error fetching cards: %s", str(e))
            return oauth_utils._json_response(False, {'error': str(e)}, status=500)

    @http.route('/api/v1/cards/add', type='http', auth='public', methods=['POST'], csrf=False)
    def add_card(self, **kwargs):
        """Create SetupIntent for adding a new payment method (card)."""
        try:
            if request.httprequest.method == "OPTIONS":
                return oauth_utils.preflight_response(["POST"])
                
            # Auth
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
            
            # Get or create customer
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
            _logger.error("Stripe API Error in add_card: %s", str(e))
            return oauth_utils._json_response(False, {'error': f'Stripe Error: {str(e)}'}, status=502)
        except Exception as e:
            _logger.error("Error creating setup intent: %s", str(e))
            return oauth_utils._json_response(False, {'error': str(e)}, status=500)
    @http.route('/api/v1/cards/setup-intent', type='http', auth='public', methods=['POST'], csrf=False)
    def create_setup_intent(self, **kwargs):
        try:
            if request.httprequest.method == "OPTIONS":
                return oauth_utils.preflight_response(["POST"])

            # Auth
            user, resp = oauth_utils.require_user()
            if not user:
                return resp
            request.update_env(user=user.id)

            stripe_secret_key = request.env['ir.config_parameter'].sudo().get_param('stripe_secret_key')
            if not stripe_secret_key:
                return oauth_utils._json_response(False, {'error': 'Stripe secret key not configured'}, status=500)

            stripe.api_key = stripe_secret_key

            partner = user.partner_id
            stripe_customer_id = partner.stripe_customer_id

            if not stripe_customer_id:
                return oauth_utils._json_response(False, {'error': 'No Stripe customer found'}, status=404)

            # 🚀 CREATE SETUP INTENT
            setup_intent = stripe.SetupIntent.create(
                customer=stripe_customer_id,
                payment_method_types=["card"],
                usage="off_session"
            )

            return oauth_utils._json_response(True, {
                "client_secret": setup_intent.client_secret
            })

        except stripe.error.StripeError as e:
            _logger.error("Stripe error creating setup intent: %s", str(e))
            return oauth_utils._json_response(False, {'error': str(e)}, status=502)

        except Exception as e:
            _logger.error("Error creating setup intent: %s", str(e))
            return oauth_utils._json_response(False, {'error': str(e)}, status=500)

    @http.route('/api/v1/cards/confirm', type='http', auth='public', methods=['POST'], csrf=False)
    def confirm_card_setup(self, **kwargs):
        """Confirm card setup after Stripe redirect and return card details."""
        try:
            if request.httprequest.method == "OPTIONS":
                return oauth_utils.preflight_response(["POST"])
                
            # Auth
            user, resp = oauth_utils.require_user()
            if not user:
                return resp
            # user = request.env['res.users'].sudo().search([('id','=',56)])
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
            _logger.error("Stripe API Error in confirm_card_setup: %s", str(e))
            return oauth_utils._json_response(False, {'error': f'Stripe Error: {str(e)}'}, status=502)
        except Exception as e:
            _logger.error("Error confirming card setup: %s", str(e))
            return oauth_utils._json_response(False, {'error': str(e)}, status=500)

    @http.route('/api/v1/cards/default', type='http', auth='public', methods=['PUT', 'POST'], csrf=False)
    def set_default_card(self, **kwargs):
        """Set a payment method as default."""
        try:
            if request.httprequest.method == "OPTIONS":
                return oauth_utils.preflight_response(["PUT", "POST"])
                
            # Auth
            user, resp = oauth_utils.require_user()
            if not user:
                return resp

            # user = request.env['res.users'].sudo().search([('id','=',56)])
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
            
            # Update active subscriptions to use this method
            subscriptions = request.env['subscription.subscription'].sudo().search([
                ('customer_name', '=', partner.id),
                ('stripe_status', '=', 'active'),
                ('stripe_subscription_id', '!=', False)
            ])
            
            for sub in subscriptions:
                try:
                    stripe.Subscription.modify(
                        sub.stripe_subscription_id,
                        default_payment_method=payment_method_id
                    )
                except Exception as e:
                    _logger.warning(f"Failed to update subscription {sub.id} default payment method: {e}")
            
            payment_method = stripe.PaymentMethod.retrieve(payment_method_id)
            _logger.info(f"User {user.id} set default card: {payment_method.card.brand} {payment_method.card.last4}")
            
            return oauth_utils._json_response(True, {
                "message": "Default payment method updated"
            })
            
        except stripe.error.StripeError as e:
            _logger.error("Stripe API Error in set_default_card: %s", str(e))
            return oauth_utils._json_response(False, {'error': f'Stripe Error: {str(e)}'}, status=502)
        except Exception as e:
            _logger.error("Error setting default card: %s", str(e))
            return oauth_utils._json_response(False, {'error': str(e)}, status=500)

    @http.route('/api/v1/cards/remove', type='http', auth='public', methods=['DELETE', 'POST'], csrf=False)
    def remove_card(self, **kwargs):
        """Remove a payment method (card)."""
        try:
            if request.httprequest.method == "OPTIONS":
                return oauth_utils.preflight_response(["DELETE", "POST"])
                
            # Auth
            user, resp = oauth_utils.require_user()
            if not user:
                return resp
            # user = request.env['res.users'].sudo().search([('id','=',56)])
            request.update_env(user=user.id)
            
            stripe_secret_key = request.env['ir.config_parameter'].sudo().get_param('stripe_secret_key')
            if not stripe_secret_key:
                return oauth_utils._json_response(False, {'error': 'Stripe secret key not configured.'}, status=500)
            stripe.api_key = stripe_secret_key

            data = request.httprequest.get_json(force=True, silent=True) or {}
            payment_method_id = data.get('payment_method_id')
            if not payment_method_id:
                payment_method_id = kwargs.get('payment_method_id')
            
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
            _logger.info(f"User {user.id} removed card {payment_method_id}")
            
            return oauth_utils._json_response(True, {
                "message": "Payment method removed"
            })
            
        except stripe.error.StripeError as e:
            _logger.error("Stripe API Error in remove_card: %s", str(e))
            return oauth_utils._json_response(False, {'error': f'Stripe Error: {str(e)}'}, status=502)
    @http.route('/api/v1/subscription/change_frequency', type='http', auth='public', methods=['POST'], csrf=False)
    def change_subscription_frequency(self, **kwargs):
        """
        Change the billing frequency of a subscription or rollup service.
        Supports switching between 'month' and 'year'.
        """
        try:
            if request.httprequest.method == "OPTIONS":
                return oauth_utils.preflight_response(["POST"])

            # Auth
            # user, resp = oauth_utils.require_user()
            # if not user:
            #     return resp
            user = request.env['res.users'].sudo().search([('id','=',61)])
            request.update_env(user=user.id)

            # Data parsing
            data = request.httprequest.get_json(force=True, silent=True) or {}
            subscription_id = data.get('subscription_id')
            model_type = data.get('model', 'subscription') # 'subscription' or 'rollup'
            new_frequency = data.get('new_frequency') # 'month' or 'year'

            if not subscription_id or not new_frequency or new_frequency not in ['monthly', 'quarterly','yearly']:
                return oauth_utils._json_response(False, {'error': 'Invalid parameters. Required: subscription_id, new_frequency (month/year)'}, status=400)

            stripe_secret_key = request.env['ir.config_parameter'].sudo().get_param('stripe_secret_key')
            if not stripe_secret_key:
                return oauth_utils._json_response(False, {'error': 'Stripe secret key not configured.'}, status=500)
            stripe.api_key = stripe_secret_key

            stripe_sub_id = None
            new_price_id = None
            record = None

            if model_type == 'subscription':
                # Handle Standard Subscription
                record = request.env['subscription.subscription'].sudo().browse(int(subscription_id))
                if not record.exists() or record.customer_name.id != user.partner_id.id:
                     return oauth_utils._json_response(False, {'error': 'Subscription not found or access denied'}, status=404)
                
                stripe_sub_id = record.stripe_subscription_id
                
                # Get price from plan
                # if new_frequency == 'month':
                #     new_price_id = record.sub_plan_id.stripe_price_month_id
                # elif new_frequency == 'year':
                #     new_price_id = record.sub_plan_id.stripe_price_year_id
                
                # if not new_price_id:
                #      return oauth_utils._json_response(False, {'error': f'Plan does not support {new_frequency}ly billing'}, status=400)
                if new_frequency == 'monthly':
                    new_price_id = record.sub_plan_id.stripe_price_month_id
                    plan_amount = record.sub_plan_id.amount_month
                elif new_frequency == 'quarterly':
                    new_price_id = record.sub_plan_id.stripe_price_quarter_id
                    plan_amount = record.sub_plan_id.amount_quarter
                elif new_frequency == 'yearly':
                    new_price_id = record.sub_plan_id.stripe_price_year_id
                    plan_amount = record.sub_plan_id.amount_year
                else:
                    return oauth_utils._json_response(False, {'error': 'Invalid duration. Must be monthly, quarterly, or yearly.'}, status=400)

            elif model_type == 'rollup':
                # Handle Rollup Service
                record = request.env['rollup.service'].sudo().browse(int(subscription_id))
                if not record.exists() or record.customer_id.id != user.partner_id.id:
                     return oauth_utils._json_response(False, {'error': 'Rollup service not found or access denied'}, status=404)
                
                stripe_sub_id = record.stripe_subscription_id
                rollup_type = record.type_id
                
                # Logic to resolve price for Rollup
                # If rollup_type has specific logic for freq, use it. 
                # Currently rollup.type implies single frequency, so we might need to dynamically find/create price
                # utilizing logic similar to rollup_type._sync_with_stripe_product but for the NEW frequency.

                product_id = rollup_type.stripe_product_id
                if not product_id:
                     return oauth_utils._json_response(False, {'error': 'Rollup type not synced with Stripe'}, status=400)

                # Calculate expected amount
                base_cost = Decimal(str(rollup_type.cost or 0))
                if new_frequency == 'year':
                    # Annual price = Monthly * 12 (Simplistic assumption for now as per plan)
                    unit_amount_decimal = base_cost * 12
                else:
                    unit_amount_decimal = base_cost

                unit_amount = int((unit_amount_decimal * 100).quantize(Decimal("1"), rounding=ROUND_HALF_UP))
                currency = request.env["ir.config_parameter"].sudo().get_param("stripe_currency", "usd").lower()

                # Find existing price on Stripe that matches
                prices = stripe.Price.list(
                    product=product_id,
                    active=True,
                    currency=currency,
                    recurring={"interval": new_frequency},
                    limit=10 
                )
                
                for p in prices.data:
                    if p.unit_amount == unit_amount:
                        new_price_id = p.id
                        break
                
                # If not found, create it (Dynamic Price Creation)
                if not new_price_id:
                    try:
                        new_price = stripe.Price.create(
                            product=product_id,
                            unit_amount=unit_amount,
                            currency=currency,
                            recurring={"interval": new_frequency},
                            metadata={"odoo_generated": "true", "frequency_switch": "true"}
                        )
                        new_price_id = new_price.id
                    except Exception as e:
                        _logger.error(f"Failed to create dynamic price for rollup switch: {e}")
                        return oauth_utils._json_response(False, {'error': 'Could not generate price for new frequency'}, status=500)

            else:
                return oauth_utils._json_response(False, {'error': 'Invalid model type'}, status=400)

            if not stripe_sub_id:
                 return oauth_utils._json_response(False, {'error': 'Subscription not active on Stripe'}, status=400)

            # Update Stripe Subscription
            # Get subscription item to update
            sub = stripe.Subscription.retrieve(stripe_sub_id)
            item_id = sub['items']['data'][0]['id']
            # print(item_id,'=========',new_price_id)
            updated_sub = stripe.Subscription.modify(
                stripe_sub_id,
                items=[{
                    'id': item_id,
                    'price': new_price_id,
                }],
                proration_behavior='create_prorations', # Default behavior
            )
            if model_type == 'subscription':
                record.write({
                    'payment_frequency':new_frequency
                })
            # Update Odoo record locally to reflect change immediately (optional, webhooks will eventually sync)
            # For Subscription model, we might want to update a 'billing_cycle' field if it existed, but 
            # the primary 'unit' is on the plan, asking to switch frequency technically changes the plan context.
            # However, for this implementation we rely on stripe sync. 
            
            return oauth_utils._json_response(True, {
                "message": f"Frequency changed to {new_frequency}",
                "new_price_id": new_price_id,
                "stripe_status": updated_sub.status
            })

        except stripe.error.StripeError as e:
            _logger.error("Stripe API Error in change_frequency: %s", str(e))
            return oauth_utils._json_response(False, {'error': f'Stripe Error: {str(e)}'}, status=502)
        except Exception as e:
            _logger.error("Error changing frequency: %s", str(e))
            return oauth_utils._json_response(False, {'error': str(e)}, status=500)