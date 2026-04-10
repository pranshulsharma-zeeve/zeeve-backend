# -*- coding: utf-8 -*-
"""
Enhanced Stripe Webhook Controller
Handles all Stripe events and maintains comprehensive payment logs
"""
from datetime import datetime, date
import stripe
import json
import logging
import requests
from odoo import http, fields, SUPERUSER_ID
from odoo.http import request, Response
from ...auth_module.utils import oauth as oauth_utils
from ...rollup_management.utils import deployment_utils, rollup_util
import time
import psycopg2
from odoo.exceptions import UserError
from odoo.tools.misc import format_date

MAX_RETRIES = 3
RETRY_DELAY = 0.15  # seconds

_logger = logging.getLogger(__name__)


class EnhancedStripeWebhookController(http.Controller):
    def _push_user_notification(
        self,
        partner,
        *,
        notification_type,
        title,
        message,
        category='info',
        payload=None,
        action_url=None,
        reference_model=None,
        reference_id=None,
        dedupe_key=None,
    ):
        if not partner:
            return False
        request.env['zeeve.notification'].sudo().notify_partner(
            partner,
            notification_type=notification_type,
            title=title,
            message=message,
            category=category,
            payload=payload,
            action_url=action_url,
            reference_model=reference_model,
            reference_id=reference_id,
            dedupe_key=dedupe_key,
        )
        return True

    @staticmethod
    def _derive_subscription_status(subscription_data):
        pause_collection = subscription_data.get('pause_collection') or {}
        if pause_collection:
            return 'paused'
        return subscription_data.get('status')

    @staticmethod
    def _stripe_value(source, *path):
        """Safely read nested values from Stripe dict-like payloads."""

        current = source or {}
        for key in path:
            if isinstance(current, dict):
                current = current.get(key)
            else:
                current = getattr(current, key, None)
            if current in (None, False):
                break
        return current

    def _sync_partner_from_stripe_payload(self, partner, payload, stripe_customer_id=None):
        """Persist customer billing details from Stripe payloads onto ``res.partner``."""

        if not partner:
            return False

        address = (
            self._stripe_value(payload, 'customer_address')
            or self._stripe_value(payload, 'customer_details', 'address')
            or self._stripe_value(payload, 'billing_details', 'address')
            or self._stripe_value(payload, 'shipping', 'address')
        )
        customer_name = (
            self._stripe_value(payload, 'customer_name')
            or self._stripe_value(payload, 'customer_details', 'name')
            or self._stripe_value(payload, 'billing_details', 'name')
            or self._stripe_value(payload, 'shipping', 'name')
            or self._stripe_value(payload, 'name')
        )
        customer_email = (
            self._stripe_value(payload, 'customer_email')
            or self._stripe_value(payload, 'customer_details', 'email')
            or self._stripe_value(payload, 'billing_details', 'email')
            or self._stripe_value(payload, 'receipt_email')
            or self._stripe_value(payload, 'email')
        )
        customer_phone = (
            self._stripe_value(payload, 'customer_phone')
            or self._stripe_value(payload, 'customer_details', 'phone')
            or self._stripe_value(payload, 'billing_details', 'phone')
            or self._stripe_value(payload, 'shipping', 'phone')
            or self._stripe_value(payload, 'phone')
        )
        stripe_customer_id = stripe_customer_id or self._stripe_value(payload, 'customer')

        if not any([stripe_customer_id, customer_name, customer_email, customer_phone, address]):
            return False

        return partner.sudo().sync_stripe_customer_profile(
            customer_id=stripe_customer_id,
            customer_name=customer_name,
            customer_email=customer_email,
            customer_phone=customer_phone,
            address=address,
        )

    def _sync_node_state(self, subscription, values, previous_state=None):
        """Ensure latest node mirrors subscription state updates."""
        if not subscription or 'state' not in values:
            return
        new_state = values.get('state')
        if not new_state:
            return
        if new_state == 'provisioning' and previous_state not in {None, False, 'draft', 'requested'}:
            return
        try:
            latest_node = subscription.get_primary_node()
            if latest_node:
                latest_node.sudo().write({'state': new_state})
        except Exception as exc:  # pragma: no cover - best effort
            _logger.exception("Failed to sync node state for subscription %s: %s", subscription.id, exc)

    def _safe_update_subscription(self, subscription, values, stripe_subscription_id=None):
        """
        Safely update subscription with retries to avoid concurrent update errors.
        All writes to subscription.subscription must go through here.
        """
        attempts = 0
        while attempts < MAX_RETRIES:
            attempts += 1
            try:
                if subscription:
                    previous_state = subscription.state
                    subscription.sudo().write(values)
                    self._sync_node_state(subscription, values, previous_state=previous_state)
                    _logger.info("Updated subscription %s with values %s", subscription.id, values)
                    return subscription
                elif stripe_subscription_id:
                    sub = request.env['subscription.subscription'].sudo().search([
                        ('stripe_subscription_id', '=', stripe_subscription_id)
                    ], limit=1)
                    if sub:
                        previous_state = sub.state
                        sub.sudo().write(values)
                        self._sync_node_state(sub, values, previous_state=previous_state)
                        _logger.info("Updated subscription %s (via stripe_subscription_id)", sub.id)
                        return sub
                return None
            except Exception as exc:
                if self._is_serialization_failure(exc):
                    _logger.warning(
                        "Serialization failure on attempt %s while updating subscription %s: %s — retrying",
                        attempts, subscription and subscription.id or stripe_subscription_id, exc
                    )
                    try:
                        request.env.cr.rollback()
                    except Exception:
                        pass
                    time.sleep(RETRY_DELAY * attempts)
                    continue
                raise
        _logger.error("Exhausted retries updating subscription %s", subscription and subscription.id or stripe_subscription_id)
        return None
    def _send_subscription_email(self, subscription):
        """Send subscription confirmation email using template."""

        MailTemplate = request.env['mail.template']
        partner = subscription.customer_name
        if not partner:
            raise UserError("Subscription customer is required to send confirmation email.")

        template = MailTemplate.sudo().search([
            ('id', '=', request.env.ref('subscription_management.mail_template_user_node_subscription').id)
        ], limit=1)
        if not template:
            raise UserError("Subscription email template not found.")

        def _format_date(value):
            if not value:
                return ''
            if isinstance(value, datetime):
                value = value.date()
            if isinstance(value, date):
                try:
                    return format_date(request.env, value)
                except Exception:  # pragma: no cover - defensive
                    return fields.Date.to_string(value)
            return str(value)

        node_type_label = dict(subscription._fields['subscription_type'].selection).get(
            subscription.subscription_type,
            subscription.subscription_type or 'Node',
        )
        currency = subscription.currency_id or subscription.company_id.currency_id or request.env.company.currency_id
        plan_details = {
            'plan_name': subscription.sub_plan_id.name if subscription.sub_plan_id else (subscription.name or 'Custom Plan'),
            'protocol_name': subscription.protocol_id.name if subscription.protocol_id else 'N/A',
            'subscription_start_date': _format_date(subscription.stripe_start_date or subscription.start_date),
            'subscription_end_date': _format_date(subscription.stripe_end_date or subscription.end_date),
            'subscription_cost': f"{(subscription.price or 0.0):.2f}",
            'currency_symbol': (currency and (currency.symbol or currency.name)) or '',
            'node_type': node_type_label,
        }

        raw_name = partner.display_name or partner.name or ''
        name_parts = raw_name.split(' ', 1) if raw_name else []
        customer_payload = {
            'firstname': partner.first_name,
            'lastname': partner.last_name,
            'email': partner.email or partner.email_formatted or '',
            'name': raw_name or partner.email or 'Customer',
        }

        ctx = {
            'protocol_name': plan_details['protocol_name'],
            'plan_name': plan_details['plan_name'],
            'start_date': fields.Date.today(),
            'current_year': fields.Date.today().year,
            'verification_company': request.env.company,
            'planDetails': plan_details,
            'plan_details': plan_details,
            'customer_name': customer_payload,
            'name': customer_payload,
            'customer_firstname': customer_payload['firstname'],
            'customer_lastname': customer_payload['lastname'],
            'email_to': customer_payload['email'],
        }

        template.sudo().with_context(ctx).send_mail(partner.id, force_send=True)

    def _queue_subscription_email(self, subscription):
        """Send subscription email once the transaction commits successfully."""

        if not subscription or not subscription.id:
            return

        env = request.env
        subscription_id = subscription.id

        def _send_after_commit():
            try:
                record = env['subscription.subscription'].sudo().browse(subscription_id)
                # if record.exists():
                #     self._send_subscription_email(record)
            except Exception:  # pragma: no cover - ensure hook never crashes the request thread
                _logger.exception("Failed to send subscription email for %s", subscription_id)

        env.cr.postcommit.add(_send_after_commit)


    @http.route('/api/stripe/webhook', type='http', auth='none', methods=['POST'], csrf=False)
    def stripe_webhook(self):
        """
        Enhanced webhook endpoint to handle all Stripe events
        """
        original_uid = request.uid
        elevated = False
        try:
            if request.uid != SUPERUSER_ID:
                # Webhooks hit the endpoint without a logged-in user. Elevate to superuser
                # so chatter tracking and other sudo-only logic (mail logging, etc.) can run.
                request.update_env(user=SUPERUSER_ID)
                elevated = True
            # Get webhook configuration
            stripe_secret_key = request.env['ir.config_parameter'].sudo().get_param('stripe_secret_key')
            webhook_secret = request.env['ir.config_parameter'].sudo().get_param('stripe_webhook_secret')
            
            if not stripe_secret_key or not webhook_secret:
                _logger.error("Stripe configuration missing")
                return Response("Configuration error", status=500)

            # Get request data
            payload = request.httprequest.data
            sig_header = request.httprequest.headers.get('stripe-signature')

            # Verify webhook signature
            try:
                event = stripe.Webhook.construct_event(
                    payload, sig_header, webhook_secret
                )
            except ValueError as e:
                _logger.error("Invalid payload: %s", str(e))
                return Response("Invalid payload", status=400)
            except stripe.error.SignatureVerificationError as e:
                _logger.error("Invalid signature: %s", str(e))
                return Response("Invalid signature", status=400)

            # Process the event
            self._process_stripe_event(event)
            
            return Response("OK", status=200)

        except Exception as e:
            _logger.error("Webhook processing error: %s", str(e))
            return Response("Internal server error", status=500)
        finally:
            if elevated and original_uid and original_uid != request.uid:
                request.update_env(user=original_uid)

    def _process_stripe_event(self, event):
        """Process Stripe event and create log entry"""
        try:
            _logger.info("Processing Stripe event: %s (ID: %s)", event['type'], event['id'])
            # Create payment log entry
            log_entry = request.env['stripe.payment.log'].sudo().create_log_entry(
                event_id=event['id'],
                event_type=event['type'],
                event_data=event,
                stripe_created=date.today(),
            )
            # Process specific event types
            if event['type'] == 'checkout.session.completed':
                self._handle_checkout_session_completed(event, log_entry)
            elif event['type'] == 'customer.subscription.created':
                self._handle_subscription_created(event, log_entry)
            elif event['type'] == 'customer.subscription.updated':
                self._handle_subscription_updated(event, log_entry)
            elif event['type'] == 'customer.subscription.deleted':
                self._handle_subscription_deleted(event, log_entry)
            elif event['type'] == 'invoice.payment_succeeded':
                self._handle_invoice_payment_succeeded(event, log_entry)
            elif event['type'] == 'invoice.will_be_due':
                self._handle_invoice_will_be_due(event, log_entry)
            elif event['type'] == 'invoice.overdue':
                self._handle_invoice_overdue(event, log_entry)
            elif event['type'] == 'invoice.payment_failed':
                self._handle_invoice_payment_failed(event, log_entry)
            elif event['type'] == 'payment_intent.succeeded':
                self._handle_payment_intent_succeeded(event, log_entry)
            elif event['type'] == 'payment_intent.payment_failed':
                self._handle_payment_intent_failed(event, log_entry)
            elif event['type'] == 'invoice.created':
                self._handle_invoice_created(event, log_entry)
            elif event['type'] == 'invoice.updated':
                self._handle_invoice_updated(event, log_entry)
            else:
                _logger.info("Unhandled event type: %s", event['type'])

        except Exception as e:
            _logger.error("Error processing event %s: %s", event['id'], str(e))
            raise

    def _handle_checkout_session_completed(self, event, log_entry):
        """Handle checkout session completed event"""
        try:
            session = event['data']['object']
            subscription_id = session.get('subscription')
            customer_id = session.get('customer')
            metadata = session.get('metadata', {})
            subscription_id_meta = metadata.get('subscription_id')
            # if rollup_util.is_rollup_checkout_session(metadata):
            #     self._finalize_rollup_checkout(session, log_entry)
            #     return
            # Check if it's Odoo-managed (v2)
            if session.get('payment_intent'):
                _logger.info('Odoo-managed PI checkout session: %s', session['payment_intent'])
                pi = stripe.PaymentIntent.retrieve(session['payment_intent'])
                metadata = pi.get('metadata', {})
                _logger.info('Resolved PI metadata: %s', metadata)
            elif session.get('setup_intent'):
                si = stripe.SetupIntent.retrieve(session['setup_intent'])
                metadata = si.get('metadata', {})
                _logger.info('Resolved SI metadata: %s', metadata)

            if metadata.get('is_odoo_managed') == 'true':
                self._handle_odoo_managed_checkout(session, log_entry)
                return

            log_entry.write({
                'stripe_subscription_id': subscription_id,
                'stripe_customer_id': customer_id,
                'subscription_status': 'active',
            })
            if subscription_id or subscription_id_meta:
                # # Find or create subscription
                # subscription = self._find_or_create_subscription(
                #     subscription_id, customer_id, metadata
                # )
                subscription_id_meta = metadata.get('subscription_id')
                charge_id = metadata.get('charge_id')
                subscription = request.env['subscription.subscription'].sudo().search([
                    ('id', '=', subscription_id_meta)
                ], limit=1)
                action = metadata.get('action')
                # Handle one-time prorated charge
                if action == 'prorated_charge_for_qty_increase':
                    discount_code = None
                    discount_amount = 0
                    if subscription:
                        # Method 1 → session.discounts
                        discounts = session.get("discounts", [])
                        if discounts:
                            # promotion code string
                            discount_code = discounts[0].get("promotion_code")
                        # Method 2 → total_details.amount_discount
                        total_details = session.get("total_details", {})
                        if total_details:
                            discount_amount = total_details.get("amount_discount", 0)
                            if discount_amount:
                                discount_amount = discount_amount /100
                        stripe_secret_key = request.env['ir.config_parameter'].sudo().get_param('stripe_secret_key')
                        stripe.api_key = stripe_secret_key
                        stripe_subscription = stripe.Subscription.retrieve(subscription.stripe_subscription_id)
                        if stripe_subscription and stripe_subscription['items']['data']:
                            item = stripe_subscription['items']['data'][0]
                            new_qty = item['quantity'] + subscription.pending_quantity_increase
                            das = stripe.Subscription.modify(
                                stripe_subscription['id'],
                                items=[{
                                    'id': item['id'],
                                    'quantity': new_qty
                                }],
                                proration_behavior='none'
                            )
                            self._safe_update_subscription(subscription, {
                                    "pending_quantity_paid":True,
                                    'quantity': new_qty,
                                    'discount_amount': discount_amount,
                                    'discount_code': discount_code,
                                    })
                        try:
                            charge = request.env['subscription.prorated.charge'].sudo().search([
                            ('id', '=', charge_id),
                                ], limit=1)
                            if charge:
                                charge.sudo().write({
                                    "state":"paid"
                                })
                        except Exception as e:
                            _logger.error(f"Prorated charge retry error for subscription {subscription.id}-- {str(e)}")

                        try:
                            queue_env = request.env['subscription.invoice.queue'].sudo()
                            queue_env.create({
                                'subscription_id': subscription.id,
                                'stripe_event_id': event['id'],
                                'action': 'prorated_charge_for_qty_increase',
                            })
                        except Exception as e:
                            _logger.info("Required manual confirmation",str(e))
                            invoice = subscription.create_invoice()
                        _logger.info(f"✅ Prorated charge completed for subscription {subscription.id}")
                    return True
                
                if subscription:
                    log_entry.subscription_id = subscription.id
                    self._sync_partner_from_stripe_payload(
                        subscription.customer_name,
                        session,
                        stripe_customer_id=customer_id,
                    )
                    
                    # Handle discount usage if applied
                    discount_id = metadata.get('discount_id')
                    if discount_id:
                        discount = request.env['subscription.discount'].sudo().browse(int(discount_id))
                        if discount.exists():
                            # Record discount usage
                            discount.apply_discount()
                            _logger.info(f"Recorded discount usage for {discount.code}")
                    
                    #  Safe update instead of direct write
                    self._safe_update_subscription(subscription, {
                        'state': 'provisioning',
                        "stripe_subscription_id":subscription_id,
                        'stripe_status': 'active',
                        'subscribed_on': fields.Datetime.now(),
                    })
        except Exception as e:
            _logger.info(f"----231 {str(e)}")
    def _handle_odoo_managed_checkout(self, session, log_entry):
        """Handle checkout completion for Odoo-managed subscriptions (rollup + normal V2)"""

        # 🔥 ALWAYS read metadata from PaymentIntent or SetupIntent
        pi_id = session.get('payment_intent')
        si_id = session.get('setup_intent')

        stripe.api_key = request.env['ir.config_parameter'].sudo().get_param('stripe_secret_key')

        metadata = {}
        pm_id = None
        pi = None
        si = None
        # print(pi_id,'****************',si_id,'==================501')
        if pi_id:
            pi = stripe.PaymentIntent.retrieve(pi_id)
            metadata = pi.get('metadata', {})
            pm_id = pi.get('payment_method')
        elif si_id:
            si = stripe.SetupIntent.retrieve(si_id)
            metadata = si.get('metadata', {})
            pm_id = si.get('payment_method')

        # ---- Activate Rollup Service if present (early return) ----
        rollup_service_id = metadata.get('rollup_service_id')
        rollup_service_uuid = metadata.get('rollup_service_uuid')
        deployment_token = metadata.get('deployment_token')
        user_id = metadata.get('user_id')
        target_rollup = request.env['rollup.service'].sudo().browse()
        if rollup_service_id:
            try:
                target_rollup = request.env['rollup.service'].sudo().browse(int(rollup_service_id))
            except (TypeError, ValueError):
                pass

        if not target_rollup.exists() and deployment_token:
            target_rollup = request.env['rollup.service'].sudo().search([('deployment_token', '=', deployment_token)], limit=1)

        if not target_rollup.exists() and rollup_service_uuid:
            target_rollup = request.env['rollup.service'].sudo().search([('service_id', '=', rollup_service_uuid)], limit=1)

        if target_rollup.exists():
            try:
                self._sync_partner_from_stripe_payload(
                    target_rollup.customer_id,
                    session,
                    stripe_customer_id=session.get('customer'),
                )
                user = request.env.user
                if user_id:
                    try:
                        user = request.env['res.users'].sudo().browse(int(user_id))
                    except (TypeError, ValueError):
                        pass
                _s, _c, metadata_update, _ = rollup_util.finalize_deployment_from_session(user, session)

                if target_rollup.status == "draft":
                    target_rollup.action_start_deployment(metadata_update, auto_activate=False)
                else:
                    if metadata_update:
                        combined_metadata = target_rollup._combined_metadata(metadata_update)
                        target_rollup.write({"metadata_json": combined_metadata})

                deployment_utils.update_subscription_status(target_rollup, "active", reason="managed_billing_checkout_completed", service_status=None)
                target_rollup._handle_payment_post_activation()

                # Vault payment method for rollup
                if pm_id:
                    try:
                        partner = target_rollup.customer_id
                        if partner:
                            pm_data = stripe.PaymentMethod.retrieve(pm_id)
                            vault_rec = request.env['stripe.payment.method'].sudo().create_or_update_from_stripe(partner, pm_data)
                            if vault_rec:
                                target_rollup.write({'payment_vault_id': vault_rec.id})
                                _logger.info("Vaulted payment method %s for rollup %s", vault_rec.id, target_rollup.id)
                    except Exception as e:
                        _logger.error("Failed to vault payment method for rollup %s: %s", target_rollup.id, str(e))

                log_entry.write({'payment_status': 'succeeded'})
                _logger.info("✅ Activated rollup service %s via Managed Billing checkout", target_rollup.id)
            except Exception as e:
                _logger.error("Failed to activate rollup service in V2 flow: %s", str(e))
            return

        # ---- No rollup → process as normal V2 subscription ----
        subscription_id = metadata.get('odoo_subscription_id')
        subscription = request.env['subscription.subscription'].sudo().browse()
        if subscription_id:
            try:
                subscription = request.env['subscription.subscription'].sudo().browse(int(subscription_id))
                if not subscription.exists():
                    _logger.warning("Subscription %s not found for V2 checkout", subscription_id)
            except (TypeError, ValueError):
                pass

        # ---- Payment Method Vaulting ----
        vault_rec = None
        if pm_id:
            try:
                partner = request.env['res.partner'].sudo().browse()
                if subscription.exists():
                    partner = subscription.customer_name

                if partner:
                    self._sync_partner_from_stripe_payload(
                        partner,
                        pi if pi_id else si,
                        stripe_customer_id=session.get('customer'),
                    )
                    pm_data = stripe.PaymentMethod.retrieve(pm_id)
                    vault_rec = request.env['stripe.payment.method'].sudo().create_or_update_from_stripe(
                        partner, pm_data
                    )
                    if vault_rec:
                        if subscription.exists():
                            subscription.write({'payment_vault_id': vault_rec.id})
                        _logger.info("Vaulted payment method %s for partner %s", vault_rec.id, partner.id)
            except Exception as e:
                _logger.error("Failed to vault payment method in V2 flow: %s", str(e))

        # ---- Billing & Activation ----
        if subscription.exists():
            self._sync_partner_from_stripe_payload(
                subscription.customer_name,
                pi if pi_id else si or session,
                stripe_customer_id=session.get('customer'),
            )
            action = metadata.get('action')
            if action == 'prorated_charge_for_qty_increase':
                _logger.info("Handling v2 prorated charge for quantity increase")
                new_qty = subscription.quantity + subscription.pending_quantity_increase
                self._safe_update_subscription(subscription, {
                    'quantity': new_qty,
                    'pending_quantity_paid': True,
                })

                charge_id = metadata.get('charge_id')
                if charge_id:
                    try:
                        charge = request.env['subscription.prorated.charge'].sudo().browse(int(charge_id))
                        if charge.exists():
                            charge.write({'state': 'paid', 'payment_date': fields.Datetime.now()})
                    except Exception as e:
                        _logger.error("Failed to update proration charge record %s: %s", charge_id, str(e))

                try:
                    queue_env = request.env['subscription.invoice.queue'].sudo()
                    queue_env.create({
                        'subscription_id': subscription.id,
                        'stripe_event_id': log_entry.event_id,
                        'action': 'prorated_charge_for_qty_increase',
                    })
                    _logger.info("Prorated invoice queued for Odoo-managed subscription %s", subscription.id)
                except Exception as e:
                    _logger.error("Failed to queue prorated invoice for %s: %s", subscription.id, str(e))
                    subscription.create_invoice(action='prorated_charge_for_qty_increase')

            else:
                # Standard activation
                now = fields.Datetime.now()
                next_date = subscription._get_next_payment_date(now)

                # Calculate price from PI or Session
                amount_discount = 0.0
                amount_total = 0.0
                if session:
                    amount_total = session.get('amount_total', 0) / 100.0
                    total_details = session.get("total_details", {})
                    if total_details:
                        amount_discount = total_details.get("amount_discount", 0) / 100.0
                elif pi:
                    amount_total = pi.amount / 100.0

                original_price = amount_total + amount_discount

                update_vals = {
                    'state': 'provisioning',
                    'is_odoo_managed': True,
                    'payment_vault_id': vault_rec.id if vault_rec else False,
                    'stripe_start_date': now,
                    'stripe_end_date': next_date,
                    'last_charge_date': now,
                    'next_payment_date': next_date,
                    'stripe_customer_id': session.get('customer'),
                    'stripe_status': 'active',
                    'subscribed_on': now,
                    'price': amount_total,
                    'original_price': original_price,
                    'discount_amount': amount_discount,
                }
                updated_subscription = self._safe_update_subscription(subscription, update_vals)
                if updated_subscription and updated_subscription.state == 'provisioning':
                    updated_subscription.notify_customer_provisioning_started()

                # ---- Create primary node (V1 parity) ----
                try:
                    if not subscription.node_ids:
                        subscription.create_primary_node()
                        _logger.info("✅ Primary node created for V2 subscription %s", subscription.id)
                    else:
                        _logger.info("Node already exists for V2 subscription %s, skipping creation", subscription.id)
                except Exception as e:
                    _logger.error("Failed to create primary node for V2 subscription %s: %s", subscription.id, str(e))

                # ---- Send provisioning email (V1 parity) ----
                try:
                    subscription.send_provisioning_mail()
                except Exception as e:
                    _logger.warning("Failed to send provisioning mail for V2 subscription %s: %s", subscription.id, str(e))

                # Trigger main invoice generation via queue
                try:
                    queue_env = request.env['subscription.invoice.queue'].sudo()
                    queue_env.create({
                        'subscription_id': subscription.id,
                        'stripe_event_id': log_entry.event_id,
                        'action': 'normal',
                    })
                    _logger.info("Main invoice queued for Odoo-managed subscription %s", subscription.id)
                except Exception as e:
                    _logger.error("Failed to queue invoice for Odoo-managed subscription %s: %s", subscription.id, str(e))

            log_entry.write({
                'subscription_id': subscription.id,
                'payment_status': 'succeeded',
            })
            _logger.info(
                "✅ Odoo-managed subscription %s activated & payment method vaulted",
                subscription.id
            )
        else:
            log_entry.write({'payment_status': 'succeeded'})
            _logger.info("✅ Managed checkout completed for technical service (no billing subscription linked)")


    def _finalize_rollup_checkout(self, session, log_entry):
        """Finalize rollup deployment created via Stripe checkout."""

        metadata = session.get('metadata', {}) or {}
        user_id = metadata.get('user_id')
        if not user_id:
            _logger.warning(
                "Skipping rollup checkout finalization for session %s: missing user_id in metadata",
                session.get('id')
            )
            return

        try:
            user_id_int = int(user_id)
        except (TypeError, ValueError):
            _logger.warning(
                "Skipping rollup checkout finalization for session %s: invalid user_id %s",
                session.get('id'), user_id
            )
            return

        user = request.env['res.users'].sudo().browse(user_id_int)
        if not user.exists():
            _logger.warning(
                "Skipping rollup checkout finalization for session %s: user %s not found",
                session.get('id'), user_id
            )
            return

        original_uid = request.uid
        try:
            request.update_env(user=user.id)
            service, _created, metadata_update, _ = rollup_util.finalize_deployment_from_session(user, session)
        except rollup_util.RollupError as exc:
            _logger.error(
                "Failed to finalize rollup checkout session %s: %s",
                session.get('id'), exc
            )
            return
        except Exception as exc:  # pylint: disable=broad-except
            _logger.exception(
                "Unexpected error finalizing rollup checkout session %s: %s",
                session.get('id'), exc
            )
            return
        finally:
            request.update_env(user=original_uid)

        metadata_update = metadata_update or {}
        service = service.sudo()
        if service.status == "draft":
            service.action_start_deployment(metadata_update, auto_activate=False)
        else:
            if metadata_update:
                combined_metadata = service._combined_metadata(metadata_update)
                service.write({"metadata_json": combined_metadata})
                service._link_payment_logs_from_metadata(combined_metadata)

        service._handle_payment_post_activation()

        if not log_entry.rollup_service_id:
            log_entry.rollup_service_id = service.id

    def _handle_rollup_invoice_payment(self, invoice_data, log_entry):
        """Process Stripe invoice events that belong to rollup services."""

        _logger.info(
            "Stripe invoice.payment_succeeded received for rollup lookup-----1 | subscription=%s invoice=%s",
            invoice_data.get('subscription'),
            invoice_data.get('id'),
        )
        # result = deployment_utils.handle_invoice_payment_succeeded(invoice_data)
        subscription_id = invoice_data["parent"]["subscription_details"]["subscription"]

        amount = invoice_data.get('amount_paid', 0) / 100
        currency = invoice_data.get('currency', 'usd')

        service_id = deployment_utils._lookup_service_from_invoice(invoice_data)
        _logger.info(
            "Stripe -------------------  lookup-----2 | subscription=%s service_id=%s",
            invoice_data.get('subscription'),
            service_id,
        )

        log_entry.write({
            'stripe_subscription_id': subscription_id,
            'stripe_invoice_id': invoice_data['id'],
            'amount': amount,
            'currency': currency,
            'payment_status': 'succeeded',
        })
        updates = {
            'stripe_subscription_id': invoice_data.get('subscription'),
            'stripe_invoice_id': invoice_data.get('id'),
            'payment_status': 'succeeded',
            'rollup_service_id': service_id.id,
            'rollup_type_id': service_id.type_id.id
        }

        amount_paid = invoice_data.get('amount_paid', 0)
        try:
            updates['amount'] = float(amount_paid) / 100 if amount_paid else 0.0
        except (TypeError, ValueError):
            updates['amount'] = 0.0
        currency = invoice_data.get('currency') or 'usd'
        log_entry.sudo().write(updates)
        if service_id.id:
            self._sync_partner_from_stripe_payload(
                service_id.customer_id,
                invoice_data,
                stripe_customer_id=invoice_data.get('customer'),
            )
            _logger.info(
            "**************----1.1 | service=%s",service_id
        )
            log_entry.rollup_service_id = service_id.id
            try:
                base_url = request.env['ir.config_parameter'].sudo().get_param('backend_url')
                api_url = f"{base_url}/api/v1/create-invoice"
                payload = {"id": int(service_id.id),"key":"rollup_service","stripe_data": invoice_data}
                headers = {"Content-Type": "application/json"}
                _logger.info(
                    "API Called  for invoice-1 | subscription=%s invoice=%s",
                    invoice_data.get('subscription'),
                    invoice_data.get('id'),
                )

                invoice = requests.post(api_url, json=payload, headers=headers)
                _logger.info("Invoice %s created for Rollup subscription",invoice)

            except Exception as exc:  # pylint: disable=broad-except
                _logger.exception(
                    "Failed to record rollup payment for %s: %s",
                    invoice_data.get('id'),
                    exc,
                )
                # self._create_invoice_and_payment(service_id, invoice_data, log_entry)

                return False
        else:
            _logger.info(
            "**************----00 no rollup | service=%s",service_id
            )
            return False
        return True

    def _handle_rollup_invoice_failure(self, invoice_data, log_entry):
        """Attach failure context when a rollup charge is declined and save hosted_invoice_url."""

        metadata = invoice_data.get('metadata') or {}
        Service = request.env['rollup.service']
        # subscription_id = invoice_data["parent"]["subscription_details"]["subscription"]
        subscription_id = (
            invoice_data.get('parent', {}).get('subscription_details', {}).get('subscription')
            or (
                invoice_data.get('lines', {}).get('data', [])
                and invoice_data['lines']['data'][0].get('parent', {}).get('subscription_item_details', {}).get('subscription')
            )
        )
        _logger.warning(
            "Stripe invoice.payment_failed received for rollup lookup | subscription=%s invoice=%s",
            invoice_data.get('subscription'),
            invoice_data.get('id'),
        )
        service = Service._find_from_stripe_invoice_payload(
            metadata=metadata,
            subscription_id=invoice_data.get('subscription') or subscription_id,
            invoice_id=invoice_data.get('id'),
            customer_id=invoice_data.get('customer'),
            payment_intent=invoice_data.get('payment_intent'),
        )
        if not service:
            return False

        service = service.sudo()
        # Fetch hosted_invoice_url from Stripe
        hosted_invoice_url = None
        try:
            stripe_secret_key = request.env['ir.config_parameter'].sudo().get_param('stripe_secret_key')
            stripe.api_key = stripe_secret_key
            stripe_invoice = stripe.Invoice.retrieve(invoice_data.get('id'))
            hosted_invoice_url = getattr(stripe_invoice, 'hosted_invoice_url', None)
        except Exception as exc:
            _logger.error("Could not fetch hosted_invoice_url for rollup invoice %s: %s", invoice_data.get('id'), exc)

        # Save hosted_invoice_url in rollup service field
        if hosted_invoice_url:
            try:
                service.write({'hosted_invoice_url': hosted_invoice_url})
            except Exception as exc:
                _logger.error("Failed to save hosted_invoice_url in rollup service %s: %s", service.id, exc)
            invoice_data = dict(invoice_data)
            invoice_data['hosted_invoice_url'] = hosted_invoice_url

        try:
            service.process_failed_invoice_payment(invoice_data)
        except Exception as exc:  # pylint: disable=broad-except
            _logger.exception(
                "Failed to record rollup payment failure for %s: %s",
                invoice_data.get('id'),
                exc,
            )
            return False

        updates = {
            'stripe_subscription_id': invoice_data.get('subscription'),
            'stripe_invoice_id': invoice_data.get('id'),
            'payment_status': 'failed',
            'failure_reason': invoice_data.get('last_payment_error', {}).get('message', ''),
            'rollup_service_id': service.id,
        }
        self._sync_partner_from_stripe_payload(
            service.customer_id,
            invoice_data,
            stripe_customer_id=invoice_data.get('customer'),
        )
        log_entry.sudo().write(updates)
        return True

    def _handle_invoice_will_be_due(self, event, log_entry):
        """Handle Stripe automation reminders before an invoice is due."""

        # Step 4: webhook_handler.invoice.will_be_due
        # - Call deployment_utils.handle_invoice_will_be_due to trigger reminder emails
        # - Record the pending invoice metadata on the webhook log for auditing

        invoice_data = event['data']['object']
        service = deployment_utils.handle_invoice_will_be_due(invoice_data)
        if not service:
            return

        updates = {
            'stripe_invoice_id': invoice_data.get('id'),
            'stripe_subscription_id': invoice_data.get('subscription'),
        }
        if not log_entry.rollup_service_id:
            updates['rollup_service_id'] = service.id
        log_entry.sudo().write(updates)

    def _handle_invoice_overdue(self, event, log_entry):
        """Handle overdue Stripe invoices for rollup subscriptions."""

        # Step 5: webhook_handler.invoice.overdue
        # - Flag the service as overdue via deployment_utils and emit the due email
        # - Store invoice identifiers so operators can trace suspension follow-ups

        invoice_data = event['data']['object']
        service = deployment_utils.handle_invoice_overdue(invoice_data)
        if not service:
            return

        updates = {
            'stripe_invoice_id': invoice_data.get('id'),
            'stripe_subscription_id': invoice_data.get('subscription'),
            'payment_status': 'overdue',
        }
        if not log_entry.rollup_service_id:
            updates['rollup_service_id'] = service.id
        log_entry.sudo().write(updates)

    def _handle_subscription_created(self, event, log_entry):
        """Handle subscription created event"""
        subscription_data = event['data']['object']
        stripe_subscription_id = subscription_data['id']
        customer_id = subscription_data['customer']
        metadata = subscription_data.get('metadata', {})
        log_entry.write({
            'stripe_subscription_id': stripe_subscription_id,
            'stripe_customer_id': customer_id,
            'subscription_status': subscription_data['status'],
        })
        subscription_id_meta = metadata.get('subscription_id')
        stripe_payment_method_id = subscription_data.get('default_payment_method','')
        subscription = request.env['subscription.subscription'].sudo().search([
            ('id', '=', subscription_id_meta)
        ], limit=1)
        
        subscription = request.env['subscription.subscription'].sudo().search([
            ('id', '=', subscription_id_meta)
        ], limit=1)
        if subscription:
            # print('--------------281',subscription)
            log_entry.subscription_id = subscription.id
            self._sync_partner_from_stripe_payload(
                subscription.customer_name,
                subscription_data,
                stripe_customer_id=customer_id,
            )
            subscription_item = subscription_data['items']['data'][0]
            start_timestamp = subscription_item['current_period_start']
            end_timestamp = subscription_item['current_period_end']

            # Convert to datetime
            start_date = datetime.utcfromtimestamp(start_timestamp)
            end_date = datetime.utcfromtimestamp(end_timestamp)
            update_vals = {
                'stripe_status': subscription_data['status'],
                "stripe_subscription_id":stripe_subscription_id,
                'stripe_payment_method_id':stripe_payment_method_id,
                'stripe_start_date':start_date,
                'stripe_end_date':end_date,
                'stripe_customer_id': customer_id,
                'autopay_enabled': subscription_data.get('metadata', {}).get('autopay_enabled', 'true') == 'true',
            }
            if subscription.state in ('draft', 'requested'):
                update_vals['state'] = 'provisioning'
            data = self._safe_update_subscription(subscription, update_vals)
            if data and data.state == 'provisioning':
                data.notify_customer_provisioning_started()
            try:
                self._queue_subscription_email(subscription)
            except Exception as e:
                print('---------577',str(e))
        if rollup_util.is_rollup_metadata(subscription_data.get('metadata')):
            deployment_utils._sync_rollup_service_subscription(subscription_data, log_entry)
    def _handle_subscription_updated(self, event, log_entry):
        """Handle subscription updated event"""
        subscription_data = event['data']['object']
        stripe_subscription_id = subscription_data['id']
        
        log_entry.write({
            'stripe_subscription_id': stripe_subscription_id,
            'subscription_status': subscription_data['status'],
        })
        metadata = subscription_data.get('metadata', {})
        subscription_id_meta = metadata.get('subscription_id')
        subscription = request.env['subscription.subscription'].sudo().search([
            ('id', '=', subscription_id_meta)
        ], limit=1)
        
        if subscription:
            log_entry.subscription_id = subscription.id
            self._update_subscription_from_stripe(subscription, subscription_data,stripe_subscription_id)
        if rollup_util.is_rollup_metadata(subscription_data.get('metadata')):
            deployment_utils._sync_rollup_service_subscription(subscription_data, log_entry)

    def _handle_subscription_deleted(self, event, log_entry):
        """Handle subscription deleted event"""
        subscription_data = event['data']['object']
        stripe_subscription_id = subscription_data['id']
        metadata = subscription_data.get('metadata', {})
        subscription_id_meta = metadata.get('subscription_id')
        log_entry.write({
            'stripe_subscription_id': stripe_subscription_id,
            'subscription_status': 'canceled',
        })

        subscription = request.env['subscription.subscription'].sudo().search([
            ('id', '=', subscription_id_meta)
        ], limit=1)

        if subscription:
            log_entry.subscription_id = subscription.id
            self._safe_update_subscription(subscription, {
                'state': 'closed',
                "stripe_subscription_id":'',
                'stripe_status': 'canceled',
            })
        if rollup_util.is_rollup_metadata(subscription_data.get('metadata')):
            deployment_utils._sync_rollup_service_subscription(subscription_data, log_entry)

    def _handle_invoice_payment_succeeded(self, event, log_entry):
        """Handle invoice payment succeeded event.

        For rollup services this path is invoked by Stripe's
        ``invoice.payment_succeeded`` webhook which is responsible for
        autopay renewals.
        """
        # Step 6: webhook_handler.invoice.payment_succeeded
        # - Forward rollup invoices to deployment_utils for reconciliation and mail dispatch
        # - Synchronise payment metadata onto the webhook log for operator insight
        # - Fallback to legacy subscription billing when the event is not rollup related
        try:
            invoice_data = event['data']['object']
            discount_code = None
            discount_amount = 0
            # --- Method 1: From line items (most accurate) ---
            lines = invoice_data.get("lines", {}).get("data", [])

            if lines:
                line_item = lines[0]  # usually only one line item
                discount_amounts = line_item.get("discount_amounts", [])
                
                if discount_amounts:
                    discount_code = discount_amounts[0].get("discount")
                    discount_amount = discount_amounts[0].get("amount")

            # --- Fallback Method 2: From invoice-level fields ---
            if not discount_code:
                discounts = invoice_data.get("discounts", [])
                if discounts:
                    discount_code = discounts[0]

            if not discount_amount:
                totals = invoice_data.get("total_discount_amounts", [])
                if totals:
                    discount_amount = totals[0].get("amount")
            if self._handle_rollup_invoice_payment(invoice_data, log_entry):
                return
            subscription_id = invoice_data.get('subscription')
            amount = invoice_data.get('amount_paid', 0) / 100
            currency = invoice_data.get('currency', 'usd')

            # Get metadata from top-level
            metadata = invoice_data.get('metadata', {})

            # Try to get subscription_id from top-level metadata
            subscription_id_meta = metadata.get('subscription_id')
            stripe_subscription_id= invoice_data.get('subscription')

            # Fallback: get subscription_id from first line item metadata
            if not subscription_id_meta:
                lines = invoice_data.get('lines', {}).get('data', [])
                if lines:
                    subscription_id_meta = lines[0].get('metadata', {}).get('subscription_id')

            # Fallback: get subscription_id from parent subscription_details metadata
            if not subscription_id_meta:
                metadata = invoice_data.get('parent', {}).get('subscription_details', {}).get('metadata', {}) or {}
                subscription_id_meta = metadata.get('subscription_id') or metadata.get('odoo_subscription_id')

            log_entry.write({
                'stripe_subscription_id': subscription_id,
                'stripe_invoice_id': invoice_data['id'],
                'amount': amount,
                'currency': currency,
                'payment_status': 'succeeded',
            })
            if subscription_id_meta:
                subscription = request.env['subscription.subscription'].sudo().search([
                    ('id', '=', int(subscription_id_meta))
                ], limit=1)
                if not subscription:
                    subscription = request.env['subscription.subscription'].sudo().search([
                    ('stripe_subscription_id', '=', stripe_subscription_id)
                ], limit=1)
                discount = None
                if not subscription.discount_id and discount_code:
                    discount = request.env['subscription.discount'].sudo().search([
                        ('code', 'ilike', discount_code)
                    ], limit=1)
                # -----------------------
                # Prepare fields to update
                # -----------------------
                update_vals = {
                    'quantity': subscription.quantity,
                    'pending_quantity_increase': 0,
                    'pending_quantity_paid': False,
                    'pending_quantity_prorated_amount': 0.0,
                }

                # Add only if discount is present
                if discount:
                    update_vals.update({
                        'discount_amount': discount_amount,
                        'discount_id': discount.id,
                        'discount_code': discount.code,
                    })
                else:
                    update_vals.update({
                        'discount_amount': discount_amount /100,
                        'discount_id': False,
                        'discount_code': discount_code or False,
                    })
                # -----------------------
                # Safe update subscription
                # -----------------------
                try:
                    self._safe_update_subscription(subscription, update_vals)
                except Exception as e:
                    _logger.error("Failed to update subscription with discount: %s", e)
                except Exception as e:
                    _logger.error(f"Failed quantity update: {str(e)}")
                if subscription:
                    self._sync_partner_from_stripe_payload(
                        subscription.customer_name,
                        invoice_data,
                        stripe_customer_id=invoice_data.get('customer'),
                    )
                    if subscription.skip_trial_invoice and invoice_data.get('billing_reason') == 'subscription_create':
                        subscription.skip_trial_invoice = False
                        _logger.info(
                            "Skipping trial bootstrap invoice %s for subscription %s",
                            invoice_data['id'],
                            subscription.id,
                        )
                        return
                    log_entry.subscription_id = subscription.id
                    subscription.hosted_invoice_url = ""
                    try:
                        if subscription:
                            try:
                                queue_env = request.env['subscription.invoice.queue'].sudo()
                                queue_env.create({
                                    'subscription_id': subscription.id,
                                    'stripe_event_id': event['id'],
                                    'action': 'normal',
                                })
                            except Exception as e:
                                _logger.info("Required manual confirmation",str(e))
                                # invoice = subscription.create_invoice()
                            _logger.info("Invoice %s created for subscription")
                        # Refresh subscription term/dates from Stripe to exit trial state
                        if stripe_subscription_id:
                            try:
                                self._refresh_subscription_from_stripe(subscription, stripe_subscription_id)
                            except Exception as e:
                                _logger.error("Failed to refresh subscription from Stripe: %s", e)
                        self._push_user_notification(
                            subscription.customer_name,
                            notification_type='payment_success',
                            title='Payment received',
                            message='Your payment of %.2f %s for %s was successful.' % (
                                amount,
                                (currency or 'usd').upper(),
                                subscription.name or subscription.sub_plan_id.name or 'your subscription',
                            ),
                            category='success',
                            payload={
                                'subscription_id': subscription.id,
                                'invoice_id': invoice_data.get('id'),
                                'stripe_subscription_id': stripe_subscription_id,
                                'amount': amount,
                                'currency': (currency or 'usd').upper(),
                            },
                            action_url='/billing',
                            reference_model='subscription.subscription',
                            reference_id=subscription.id,
                            dedupe_key='stripe:invoice.payment_succeeded:%s' % invoice_data.get('id'),
                        )
                    except Exception as e:
                        print('---------303',str(e))
                    # self._create_invoice_and_payment(subscription, invoice_data, log_entry)
                else:
                    _logger.warning('Subscription not found for subscription_id_meta: %s', subscription_id_meta)
            else:
                _logger.warning('No subscription_id found in invoice metadata: %s', invoice_data['id'])

        except Exception as e:
            _logger.error('got error on handling ---_handle_invoice_payment_succeeded %s', str(e))


    def _handle_invoice_payment_failed(self, event, log_entry):
        """Handle invoice payment failed event and save hosted_invoice_url."""
        invoice_data = event['data']['object']
        subscription_id = (
            invoice_data.get('parent', {}).get('subscription_details', {}).get('subscription')
            or (
                invoice_data.get('lines', {}).get('data', [])
                and invoice_data['lines']['data'][0].get('parent', {}).get('subscription_item_details', {}).get('subscription')
            )
        )
        _logger.warning(
            "Stripe invoice.payment_failed received-<<<-------------->>> | subscription=%s invoice=%s",
            invoice_data.get('subscription'),
            subscription_id
        )
        if rollup_util.is_rollup_metadata(invoice_data.get('metadata')):
            if self._handle_rollup_invoice_failure(invoice_data, log_entry):
                return
        


        # Try to get Odoo subscription_id from line item metadata
        odoo_subscription_id = None
        lines = invoice_data.get('lines', {}).get('data', [])
        if lines:
            odoo_subscription_id = lines[0].get('metadata', {}).get('subscription_id')

        # Fallback: try parent subscription_details metadata
        if not odoo_subscription_id:
            odoo_subscription_id = invoice_data.get('parent', {}).get('subscription_details', {}).get('metadata', {}).get('subscription_id')
        # Fetch hosted_invoice_url from Stripe
        hosted_invoice_url = None
        try:
            stripe_secret_key = request.env['ir.config_parameter'].sudo().get_param('stripe_secret_key')
            stripe.api_key = stripe_secret_key
            stripe_invoice = stripe.Invoice.retrieve(invoice_data.get('id'))
            hosted_invoice_url = getattr(stripe_invoice, 'hosted_invoice_url', None)
        except Exception as exc:
            _logger.error("Could not fetch hosted_invoice_url for subscription invoice %s: %s", invoice_data.get('id'), exc)

        log_entry.write({
            'stripe_subscription_id': subscription_id,
            'stripe_invoice_id': invoice_data['id'],
            'payment_status': 'failed',
            'subscription_id': subscription_id,
            'failure_reason': invoice_data.get('last_payment_error', {}).get('message', ''),
        })

        if subscription_id:
            subscription = request.env['subscription.subscription'].sudo().search([
                '|', ('stripe_subscription_id', '=', subscription_id),
                ('id', '=', odoo_subscription_id),
            ], limit=1)
            if subscription:
                log_entry.subscription_id = subscription.id
                self._sync_partner_from_stripe_payload(
                    subscription.customer_name,
                    invoice_data,
                    stripe_customer_id=invoice_data.get('customer'),
                )
                # Save hosted_invoice_url in subscription field
                try:
                    subscription.write({'hosted_invoice_url': hosted_invoice_url})
                except Exception as exc:
                    _logger.error("Failed to save hosted_invoice_url in subscription %s: %s", subscription.id, exc)
                try:
                    subscription.send_payment_failed_notifications(
                        failure_reason=invoice_data.get('last_payment_error', {}).get('message'),
                        hosted_invoice_url=hosted_invoice_url,
                        invoice_payload=invoice_data,
                    )
                except Exception as exc:
                    _logger.exception(
                        "Failed to dispatch subscription payment failure mails | subscription=%s", subscription.id
                    )
                self._push_user_notification(
                    subscription.customer_name,
                    notification_type='payment_failed',
                    title='Payment failed',
                    message=invoice_data.get('last_payment_error', {}).get('message')
                    or 'We could not process the payment for your subscription.',
                    category='error',
                    payload={
                        'subscription_id': subscription.id,
                        'invoice_id': invoice_data.get('id'),
                        'hosted_invoice_url': hosted_invoice_url,
                    },
                    action_url=hosted_invoice_url or '/billing',
                    reference_model='subscription.subscription',
                    reference_id=subscription.id,
                    dedupe_key='stripe:invoice.payment_failed:%s' % invoice_data.get('id'),
                )
            else:
                subscription.write({'state': 'in_grace'})

    def _handle_payment_intent_succeeded(self, event, log_entry):
        """Handle payment intent succeeded event"""
        payment_intent = event['data']['object']
        amount = payment_intent.get('amount', 0) / 100
        currency = payment_intent.get('currency', 'usd')
        metadata = payment_intent.get('metadata', {}) or {}
        partner = request.env['res.partner'].sudo().browse()

        subscription_id = metadata.get('odoo_subscription_id') or metadata.get('subscription_id')
        if subscription_id:
            try:
                subscription = request.env['subscription.subscription'].sudo().browse(int(subscription_id))
                if subscription.exists():
                    partner = subscription.customer_name
            except (TypeError, ValueError):
                partner = request.env['res.partner'].sudo().browse()

        if not partner and payment_intent.get('customer'):
            partner = request.env['res.partner'].sudo().search([
                ('stripe_customer_id', '=', payment_intent.get('customer'))
            ], limit=1)

        self._sync_partner_from_stripe_payload(
            partner,
            payment_intent,
            stripe_customer_id=payment_intent.get('customer'),
        )
        
        log_entry.write({
            'stripe_payment_intent_id': payment_intent['id'],
            'amount': amount,
            'currency': currency,
            'payment_status': 'succeeded',
        })

    def _handle_payment_intent_failed(self, event, log_entry):
        """Handle payment intent failed event"""
        payment_intent = event['data']['object']
        
        log_entry.write({
            'stripe_payment_intent_id': payment_intent['id'],
            'payment_status': 'failed',
            'failure_reason': payment_intent.get('last_payment_error', {}).get('message', ''),
        })
        

    def _handle_invoice_created(self, event, log_entry):
        """Handle invoice created event"""
        invoice_data = event['data']['object']
        subscription_id = invoice_data.get('subscription')
        billing_reason = invoice_data.get('billing_reason')
        status = invoice_data.get('status')
        # print('-------------326',event,'=================')
        log_entry.write({
            'stripe_subscription_id': subscription_id,
            'stripe_invoice_id': invoice_data['id'],
        })
        if (
            status == 'draft'
            and billing_reason == 'subscription_cycle'
            and subscription_id
        ):
            subscription = request.env['subscription.subscription'].sudo().search([
                ('stripe_subscription_id', '=', subscription_id)
            ], limit=1)
            if not subscription:
                return
            self._sync_partner_from_stripe_payload(
                subscription.customer_name,
                invoice_data,
                stripe_customer_id=invoice_data.get('customer'),
            )
            _logger.info(
                "Stripe sent draft subscription_cycle invoice %s for subscription %s; awaiting Stripe auto-finalization.",
                invoice_data['id'],
                subscription.id,
            )

    def _handle_invoice_updated(self, event, log_entry):
        """Handle invoice updated event"""
        invoice_data = event['data']['object']
        subscription_id = invoice_data.get('subscription')

        log_entry.write({
            'stripe_subscription_id': subscription_id,
            'stripe_invoice_id': invoice_data['id'],
            'subscription_id': subscription_id,
        })

        if not subscription_id:
            return

        is_manual_collection = invoice_data.get('collection_method') == 'send_invoice'
        is_uncollectible = invoice_data.get('status') == 'uncollectible'
        if not (is_manual_collection or is_uncollectible):
            return

        subscription = request.env['subscription.subscription'].sudo().search([
            ('stripe_subscription_id', '=', subscription_id)
        ], limit=1)

        if not subscription:
            return
        log_entry.subscription_id = subscription.id
        self._sync_partner_from_stripe_payload(
            subscription.customer_name,
            invoice_data,
            stripe_customer_id=invoice_data.get('customer'),
        )

        if subscription.autopay_enabled and subscription.stripe_status != 'paused':
            return

        hosted_invoice_url = invoice_data.get('hosted_invoice_url')
        if not hosted_invoice_url:
            try:
                stripe_secret_key = request.env['ir.config_parameter'].sudo().get_param('stripe_secret_key')
                if stripe_secret_key:
                    stripe.api_key = stripe_secret_key
                    stripe_invoice = stripe.Invoice.retrieve(invoice_data.get('id'))
                    hosted_invoice_url = getattr(stripe_invoice, 'hosted_invoice_url', None)
            except Exception as exc:
                _logger.error(
                    "Could not fetch hosted_invoice_url for manual collection invoice %s: %s",
                    invoice_data.get('id'),
                    exc,
                )

        if not hosted_invoice_url:
            return

        should_notify = subscription.hosted_invoice_url != hosted_invoice_url
        try:
            subscription.write({'hosted_invoice_url': hosted_invoice_url})
        except Exception as exc:
            _logger.error("Failed to save hosted_invoice_url in subscription %s: %s", subscription.id, exc)
            return

        if not should_notify:
            return

        try:
            subscription.send_manual_payment_notifications(
                hosted_invoice_url=hosted_invoice_url,
                invoice_payload=invoice_data,
            )
        except Exception:
            _logger.exception(
                "Failed to dispatch subscription manual payment mails | subscription=%s",
                subscription.id,
            )
        self._push_user_notification(
            subscription.customer_name,
            notification_type='manual_payment_required',
            title='Manual payment required',
            message='Automatic charging is not active for %s. Please complete this invoice manually.' % (
                subscription.name or subscription.sub_plan_id.name or 'your subscription'
            ),
            category='warning',
            payload={
                'subscription_id': subscription.id,
                'invoice_id': invoice_data.get('id'),
                'hosted_invoice_url': hosted_invoice_url,
            },
            action_url=hosted_invoice_url or '/billing',
            reference_model='subscription.subscription',
            reference_id=subscription.id,
            dedupe_key='stripe:manual_payment_required:%s' % invoice_data.get('id'),
        )

    @staticmethod
    def _is_serialization_failure(exc):
        # Prefer explicit exception type if psycopg2 is available.
        try:
            return isinstance(exc, psycopg2.errors.SerializationFailure)
        except Exception:
            # Fallback: check message
            return 'could not serialize access' in str(exc).lower()

    def _find_or_create_subscription(self, stripe_subscription_id, stripe_customer_id, metadata):
        """Find or create subscription with retries and row locking to avoid concurrent update issues."""
        attempts = 0
        while attempts < MAX_RETRIES:
            attempts += 1
            try:
                # Try fast path: find by stripe_subscription_id
                subscription = request.env['subscription.subscription'].sudo().search([
                    ('stripe_subscription_id', '=', stripe_subscription_id)
                ], limit=1)
                if subscription:
                    _logger.info("Found subscription by stripe_subscription_id %s => %s", stripe_subscription_id, subscription.id)
                    return subscription

                # Ensure metadata has a subscription id or customer id
                sub_plan_id = metadata.get('subscription_id')
                customer = metadata.get('customer_id')

                if not sub_plan_id and not customer:
                    _logger.warning("No subscription_id or customer_id in metadata for Stripe subscription %s", stripe_subscription_id)
                    return None

                # If we have a customer id, lock the partner row before updating it
                partner = None
                if customer:
                    cr = request.env.cr
                    # Acquire FOR UPDATE lock on the partner row
                    cr.execute("SELECT id FROM res_partner WHERE id = %s FOR UPDATE", (int(customer),))
                    row = cr.fetchone()
                    if row:
                        partner = request.env['res.partner'].sudo().browse(row[0])
                    else:
                        _logger.error("Partner with ID %s not found", customer)
                        return None

                    # Update partner stripe_customer_id if missing
                    if not partner.stripe_customer_id:
                        try:
                            partner.write({'stripe_customer_id': stripe_customer_id})
                            _logger.info("Updated partner %s with stripe_customer_id %s", partner.id, stripe_customer_id)
                        except Exception as e:
                            _logger.exception("Failed to write stripe_customer_id on partner %s: %s", customer, e)
                            # Let retry handle transient serialization failures
                            raise

                # Now try to find existing subscription by id in metadata
                sub_sub_model = None
                if sub_plan_id:
                    # Lock subscription row if exists
                    cr = request.env.cr
                    cr.execute("SELECT id FROM subscription_subscription WHERE id = %s FOR UPDATE", (int(sub_plan_id),))
                    row = cr.fetchone()
                    if row:
                        sub_sub_model = request.env['subscription.subscription'].sudo().browse(row[0])

                subscription_vals = {
                    'customer_name': partner and partner.id or customer,
                    'stripe_subscription_id': stripe_subscription_id,
                    'stripe_customer_id': stripe_customer_id,
                    'source': 'stripe',
                }
                if 'product_id' in metadata:
                    subscription_vals['product_id'] = int(metadata['product_id'])
                if 'plan_id' in metadata:
                    subscription_vals['sub_plan_id'] = int(metadata['plan_id'])

                if sub_sub_model:
                    sub_sub_model.write(subscription_vals)
                    _logger.info("Updated existing subscription %s with Stripe data", sub_sub_model.id)
                    return sub_sub_model
                else:
                    # Create but check again unique constraint (race) by searching stripe_subscription_id
                    existing = request.env['subscription.subscription'].sudo().search(
                        [('stripe_subscription_id', '=', stripe_subscription_id)], limit=1)
                    if existing:
                        return existing
                    new_subscription = request.env['subscription.subscription'].sudo().create(subscription_vals)
                    new_subscription.create_primary_node({
                        'node_type': metadata.get('subscription_type'),
                    })
                    _logger.info("Created new subscription %s for Stripe subscription %s", new_subscription.id, stripe_subscription_id)
                    return new_subscription

            except Exception as exc:
                # If serialization issue, rollback and retry
                if self._is_serialization_failure(exc):
                    _logger.warning("Serialization failure on attempt %s for stripe_subscription %s: %s — retrying", attempts, stripe_subscription_id, exc)
                    try:
                        request.env.cr.rollback()
                    except Exception:
                        pass
                    time.sleep(RETRY_DELAY * attempts)
                    continue
                # For other exceptions, log and re-raise so caller can handle
                _logger.exception("Error in _find_or_create_subscription: %s", exc)
                raise
        # If all retries exhausted
        _logger.error("Exhausted retries creating/finding subscription for stripe_subscription_id %s", stripe_subscription_id)
        return None
    def _refresh_subscription_from_stripe(self, subscription, stripe_subscription_id):
        """Fetch latest subscription data from Stripe and synchronize key fields."""
        if not stripe_subscription_id or not subscription:
            return
        try:
            stripe_secret_key = request.env['ir.config_parameter'].sudo().get_param('stripe_secret_key')
            if not stripe_secret_key:
                _logger.warning("Stripe secret key not configured; cannot refresh subscription %s", subscription.id)
                return
            stripe.api_key = stripe_secret_key
            stripe_subscription = stripe.Subscription.retrieve(
                stripe_subscription_id,
                expand=['items']
            )
            if stripe_subscription:
                self._update_subscription_from_stripe(subscription, stripe_subscription, stripe_subscription_id)
        except Exception as exc:
            _logger.exception("Failed to refresh subscription %s from Stripe: %s", subscription.id, exc)

    def _update_subscription_from_stripe(self, subscription, subscription_data,stripe_subscription_id):
        """Update subscription from Stripe data"""
        # status_mapping = {
        #     'active': 'in_progress',
        #     'canceled': 'closed',
        #     'past_due': 'in_grace',
        #     'trialing': 'in_progress',
        #     'unpaid': 'in_grace',
        # }
        
        # odoo_status = status_mapping.get(subscription_data['status'], 'draft')
        
        # subscription.write({
        #     'stripe_status': subscription_data['status'],
        #     'state': 'in_progress',
        #     'autopay_enabled': subscription_data.get('metadata', {}).get('autopay_enabled', 'true') == 'true',
        # })
        subscription_item = subscription_data['items']['data'][0]
        start_timestamp = subscription_item['current_period_start']
        end_timestamp = subscription_item['current_period_end']

        # Convert to datetime
        start_date = datetime.utcfromtimestamp(start_timestamp)
        end_date = datetime.utcfromtimestamp(end_timestamp)
        stripe_payment_method_id = subscription_data.get('default_payment_method','')
        self._safe_update_subscription(subscription, {
                'stripe_status': subscription_data['status'],
                'stripe_start_date':start_date,
                'stripe_end_date':end_date,
                "stripe_subscription_id":stripe_subscription_id,
                'stripe_payment_method_id':stripe_payment_method_id,
                'state': 'provisioning',
                'autopay_enabled': subscription_data.get('metadata', {}).get('autopay_enabled', 'true') == 'true',
            })

    def _create_invoice_and_payment(self, subscription, invoice_data, log_entry):
        """Create custom Zeeve invoice from Stripe invoice event"""
        try:
            _logger.info(
                "Creating Zeeve invoice for subscription %s, Stripe invoice %s",
                subscription.id, invoice_data['id']
            )
            

            # Get invoice creation date from Stripe
            invoice_created_ts = invoice_data.get('created')
            invoice_date = datetime.utcfromtimestamp(invoice_created_ts).date() if invoice_created_ts else fields.Date.context_today(subscription)

            # Use due_date from Stripe if available; otherwise, fallback to invoice_date
            due_date_ts = invoice_data.get('due_date')
            due_date = datetime.utcfromtimestamp(due_date_ts).date() if due_date_ts else invoice_date
            # Find currency
            currency = request.env['res.currency'].sudo().search(
                [('name', '=', invoice_data.get('currency', 'USD').upper())], limit=1
            )
            if not currency:
                _logger.error("Currency %s not found", invoice_data.get('currency'))
                return

            # Extract discount info
            discount_amount = 0.0
            discount_code = None
            total_discounts = invoice_data.get("total_discount_amounts") or []
            if total_discounts:
                discount_amount = total_discounts[0].get("amount", 0) / 100.0

            # Try to get discount code from first line item metadata
            lines = invoice_data.get('lines', {}).get('data', [])
            if lines:
                discount_code = lines[0].get('metadata', {}).get('discount_code')

            # Prepare customer address safely
            cust_address = invoice_data.get('customer_address') or {}
            address_str = "%s, %s, %s, %s" % (
                cust_address.get('line1', ''),
                cust_address.get('city', ''),
                cust_address.get('state', ''),
                cust_address.get('postal_code', ''),
            )

            # Prepare invoice values
            invoice_vals = {
                "customer_id": getattr(subscription, 'customer_name', False) and subscription.customer_name.id or False,
                "customer_address": address_str,
                "subscription_ref": getattr(subscription, 'subscription_ref', ''),
                "currency_id": currency.id,
                "invoice_date": invoice_date,
                "due_date":due_date,
                "discount_amount": discount_amount,
                "discount_code": discount_code,
                "notes": "Thanks for your business.",
                "state":"paid",
                "name": invoice_data.get('number') or subscription.subscription_ref,
                "rollup_service_id": getattr(subscription, 'rollup_service_id', False) and subscription.service_id.id or False
            }

            # Create invoice
            invoice = request.env['zeeve.invoice'].sudo().create(invoice_vals)

            # Create invoice lines
            for line in lines:
                request.env['zeeve.invoice.line'].sudo().create({
                    "invoice_id": invoice.id,
                    "name": line.get('description', 'No Description'),
                    "quantity": line.get('quantity', 1),
                    "price_unit": line.get('amount', 0) / 100.0,
                })

            _logger.info("Created Zeeve invoice %s for subscription %s", invoice.id, subscription.id)

            # Update log entry
            log_entry.write({
                'invoice_id': invoice.id,
                'payment_status': 'succeeded',
            })

            # Send email with PDF
            try:
                invoice.action_send_email()
            except Exception as e:
                _logger.error("Error sending invoice email: %s", str(e))

            return invoice

        except Exception as e:
            _logger.error("Error creating Zeeve invoice for subscription %s: %s", getattr(subscription, 'id', 'N/A'), str(e))
            raise
