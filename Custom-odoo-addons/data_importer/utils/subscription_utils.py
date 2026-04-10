# -*- coding: utf-8 -*-
from datetime import datetime, time, timezone
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
import json
import logging
import re
from dateutil.relativedelta import relativedelta
from odoo import api, fields, models
import stripe

_logger = logging.getLogger(__name__)

_NUMERIC_CLEANER = re.compile(r"[^0-9.\-]")
_STRIPE_STATUS_VALUES = {
    'draft',
    'active',
    'canceled',
    'incomplete',
    'incomplete_expired',
    'past_due',
    'trialing',
    'unpaid',
}

_STRIPE_PRICE_CACHE = {}
_MIGRATION_PRICE_CACHE = {}
_SPECIAL_ITEM_OVERRIDES = {
    'FABRIC': {
        'protocol_name': 'Fabric',
        'subscription_type': 'rpc',
        'billing_cycle': 'monthly',
        'plan_name': 'Fabric Dedicated',
    },
    'BESU': {
        'protocol_name': 'Besu',
        'subscription_type': 'rpc',
        'billing_cycle': 'monthly',
        'plan_name': 'Besu Dedicated',
    },
    'ENERGYWEB_VALIDATOR_NODES_M_DEDICATED': {
        'protocol_name': 'Energyweb',
        'subscription_type': 'validator',
        'billing_cycle': 'monthly',
        'plan_name': 'Energyweb Validator Dedicated',
    },
    'COREUMXRPLRELAYER': {
        'protocol_name': 'Coreum',
        'subscription_type': 'rpc',
        'billing_cycle': 'monthly',
        'plan_name': 'Coreum XRPL Relayer',
    },
}

class SubscriptionUtils:
    """Utility class for handling subscription data mapping and creation."""

    @staticmethod
    def _retrieve_stripe_customer(stripe_customer_id):
        """Return an existing Stripe customer when the identifier is valid."""
        if not stripe_customer_id:
            return None
        try:
            return stripe.Customer.retrieve(stripe_customer_id)
        except stripe.error.InvalidRequestError:
            _logger.warning("Stripe customer %s not found on Stripe.", stripe_customer_id)
            return None

    @staticmethod
    def _search_stripe_customer_by_metadata(zoho_customer_id):
        """Return a Stripe customer using Zoho identifier stored in metadata."""
        if not zoho_customer_id:
            return None
        try:
            results = stripe.Customer.search(
                query="metadata['zoho_customer_id']:'{}'".format(zoho_customer_id),
                limit=1,
            )
        except AttributeError:
            _logger.warning("Stripe library version does not support Customer.search API.")
            return None
        except stripe.error.StripeError as err:
            _logger.error(
                "Failed to search Stripe customer using Zoho ID %s: %s",
                zoho_customer_id,
                err,
            )
            return None

        return results.data[0] if getattr(results, 'data', None) else None

    @staticmethod
    def _search_stripe_customer_by_email(customer_email):
        """Return the first Stripe customer that matches the supplied email."""
        if not customer_email:
            return None
        try:
            results = stripe.Customer.list(email=customer_email, limit=1)
        except stripe.error.StripeError as err:
            _logger.error(
                "Failed to list Stripe customers for %s: %s",
                customer_email,
                err,
            )
            return None
        return results.data[0] if getattr(results, 'data', None) else None

    @staticmethod
    def _ensure_odoo_customer(env, customer_email, customer_name, stripe_customer_id=None):
        """Return partner, user, and Stripe customer id by relying on existing records."""
        Partners = env['res.partner'].sudo()
        Users = env['res.users'].sudo().with_context(no_reset_password=True)

        normalized_identifier = str(stripe_customer_id).strip() if stripe_customer_id else ''

        search_domain = []
        if customer_email:
            search_domain.append(('email', '=', customer_email))
        partner = Partners.search(search_domain, limit=1) if search_domain else None

        if not partner and normalized_identifier.startswith('cus_'):
            partner = Partners.search([('stripe_customer_id', '=', normalized_identifier)], limit=1)

        # if not partner and customer_name:
        #     partner = Partners.search([('name', 'ilike', customer_name)], limit=1)

        if not partner:
            _logger.warning(
                "Partner not found for email %s / name %s. Subscription row will be skipped.",
                customer_email,
                customer_name,
            )
            return None, None, None

        user = Users.search([('partner_id', '=', partner.id)], limit=1)
        if not user:
            _logger.warning(
                "No user record linked to partner %s (%s). Subsequent automation may depend on a user.",
                partner.id,
                customer_email,
            )

        partner_updates = {}
        if customer_email and partner.email != customer_email:
            partner_updates['email'] = customer_email
        if customer_name and partner.name != customer_name:
            partner_updates['name'] = customer_name
        if partner_updates:
            partner.write(partner_updates)

        stripe_customer_identifier = partner.stripe_customer_id

        if normalized_identifier.startswith('cus_') and not stripe_customer_identifier:
            stripe_customer_identifier = normalized_identifier

        stripe_customer = SubscriptionUtils._retrieve_stripe_customer(stripe_customer_identifier)

        if not stripe_customer and normalized_identifier and not normalized_identifier.startswith('cus_'):
            stripe_customer = SubscriptionUtils._search_stripe_customer_by_metadata(normalized_identifier)

        if not stripe_customer and customer_email:
            stripe_customer = SubscriptionUtils._search_stripe_customer_by_email(customer_email)

        if stripe_customer:
            stripe_customer_identifier = stripe_customer['id']
            if partner.stripe_customer_id != stripe_customer_identifier:
                partner.write({'stripe_customer_id': stripe_customer_identifier})
        else:
            _logger.warning(
                "Stripe customer not located for partner %s (email %s). Subscription row will not create Stripe subscription.",
                partner.id,
                customer_email,
            )

        stripe_customer_identifier = stripe_customer_identifier or None

        if not stripe_customer_identifier:
            payload = {}
            email_value = customer_email or partner.email
            name_value = customer_name or partner.display_name
            if email_value:
                payload['email'] = email_value
            if name_value:
                payload['name'] = name_value
            metadata = {}
            if normalized_identifier and not normalized_identifier.startswith('cus_'):
                metadata['zoho_customer_id'] = normalized_identifier
            metadata['odoo_partner_id'] = partner.id
            payload['metadata'] = metadata
            if not payload.get('email') and not payload.get('name'):
                payload['description'] = f"Odoo Partner {partner.id}"
            try:
                stripe_customer = stripe.Customer.create(**payload)
                stripe_customer_identifier = stripe_customer['id']
                partner.write({'stripe_customer_id': stripe_customer_identifier})
                _logger.info(
                    "Created Stripe customer %s for partner %s (%s).",
                    stripe_customer_identifier,
                    partner.id,
                    partner.email,
                )
            except stripe.error.StripeError as err:
                _logger.error(
                    "Failed to create Stripe customer for partner %s (%s): %s",
                    partner.id,
                    partner.email,
                    err,
                )
                stripe_customer_identifier = None

        return partner, user, stripe_customer_identifier

    @staticmethod
    def _ensure_payment_method_ready(stripe_customer):
        """Ensure Stripe customer has a default payment method when cards exist."""
        if not stripe_customer:
            return stripe_customer, False

        invoice_settings = stripe_customer.get('invoice_settings', {}) if isinstance(stripe_customer, dict) else {}
        default_pm = invoice_settings.get('default_payment_method')
        default_source = stripe_customer.get('default_source') if isinstance(stripe_customer, dict) else None
        if default_pm or default_source:
            return stripe_customer, True

        customer_id = stripe_customer.get('id')
        if not customer_id:
            return stripe_customer, False

        try:
            payment_methods = stripe.PaymentMethod.list(customer=customer_id, type='card', limit=1)
        except stripe.error.StripeError as err:
            _logger.error("Failed to list payment methods for customer %s: %s", customer_id, err)
            return stripe_customer, False

        if not payment_methods.data:
            _logger.warning("No payment methods available for customer %s.", customer_id)
            return stripe_customer, False

        payment_method_id = payment_methods.data[0]['id']
        try:
            stripe.Customer.modify(
                customer_id,
                invoice_settings={'default_payment_method': payment_method_id},
            )
            stripe_customer = stripe.Customer.retrieve(customer_id)
        except stripe.error.StripeError as err:
            _logger.error("Failed to set default payment method for customer %s: %s", customer_id, err)
            return stripe_customer, False

        return stripe_customer, True

    @staticmethod
    def _compute_billing_cycle_anchor(next_billing_date, payment_frequency=None):
        """Return a Stripe-compatible billing_cycle_anchor timestamp."""
        if not next_billing_date:
            return None

        try:
            date_obj = fields.Date.from_string(next_billing_date)
        except Exception:  # pylint: disable=broad-except
            _logger.warning("Unable to parse next billing date %s", next_billing_date)
            return None

        if not date_obj:
            return None

        original_date = date_obj
        anchor_dt = datetime.combine(date_obj, time.min, tzinfo=timezone.utc)
        now_utc = datetime.now(timezone.utc)
        if anchor_dt <= now_utc:
            months_step = {
                'monthly': 1,
                'quarterly': 3,
                'annually': 12,
            }.get(payment_frequency)
            if months_step:
                while anchor_dt <= now_utc:
                    date_obj += relativedelta(months=months_step)
                    anchor_dt = datetime.combine(date_obj, time.min, tzinfo=timezone.utc)
                _logger.info(
                    "Rolled forward next billing date %s to %s using %s frequency.",
                    fields.Date.to_string(original_date),
                    fields.Date.to_string(date_obj),
                    payment_frequency,
                )
            else:
                _logger.warning(
                    "Next billing date %s is not in the future; skipping Stripe billing_cycle_anchor.",
                    next_billing_date,
                )
                return None

        return int(anchor_dt.timestamp())

    @staticmethod
    def map_item_code_to_plan(env, item_code, unit_price=None, currency_code='usd', allow_create=False):
        """Map Zoho item codes to Odoo subscription plans and Stripe price IDs."""
        normalized_code = (item_code or '').strip()
        override = _SPECIAL_ITEM_OVERRIDES.get(normalized_code.upper())
        if not override and '_' in normalized_code:
            override = _SPECIAL_ITEM_OVERRIDES.get(normalized_code.split('_')[0].upper())
        if override:
            protocol_name = override.get('protocol_name')
            subscription_type = override.get('subscription_type', 'rpc')
            billing_cycle = override.get('billing_cycle', 'monthly')
            plan_name = override.get('plan_name') or protocol_name
        else:
            protocol_name, subscription_type, billing_cycle, plan_name = SubscriptionUtils._parse_item_code(normalized_code)

        protocol = env['protocol.master'].sudo().search([('name', 'ilike', protocol_name)], limit=1)
        if not protocol:
            raise ValueError(f"Protocol '{protocol_name}' not found.")

        plan = env['subscription.plan'].sudo().search([
            ('protocol_id', '=', protocol.id),
            ('subscription_type', '=', subscription_type),
            ('name', '=', plan_name),
        ], limit=1)
        stripe_price_id = None
        if not plan and allow_create and unit_price is not None:
            plan, stripe_price_id = SubscriptionUtils._create_dynamic_plan(
                env,
                protocol,
                plan_name,
                subscription_type,
                billing_cycle,
                unit_price,
                currency_code or 'usd',
            )
        if not plan:
            raise ValueError(f"Plan '{plan_name}' not found for protocol '{protocol_name}'.")

        stripe_price_id = stripe_price_id or (
            plan.stripe_price_month_id if billing_cycle == 'monthly' else
            plan.stripe_price_year_id if billing_cycle == 'annually' else
            plan.stripe_price_quarter_id if billing_cycle == 'quarterly' else None
        )
        if not stripe_price_id:
            raise ValueError(f"Stripe price ID not found for billing cycle '{billing_cycle}'.")

        return plan, stripe_price_id, protocol, billing_cycle

    @staticmethod
    def _create_dynamic_plan(env, protocol, plan_name, subscription_type, billing_cycle, unit_price, currency_code):
        """Create a subscription plan and Stripe price on the fly for special item codes."""
        Plan = env['subscription.plan'].sudo()
        duration = 1
        unit = 'month'
        if billing_cycle == 'annually':
            unit = 'year'
        elif billing_cycle == 'quarterly':
            unit = 'month'
            duration = 3

        amount_month = unit_price if billing_cycle == 'monthly' else 0.0
        amount_quarter = unit_price if billing_cycle == 'quarterly' else 0.0
        amount_year = unit_price if billing_cycle == 'annually' else 0.0

        plan_vals = {
            'name': plan_name,
            'subscription_type': subscription_type or 'rpc',
            'protocol_id': protocol.id,
            'duration': duration,
            'unit': unit,
            'plan_amount': unit_price,
            'amount_month': amount_month,
            'amount_quarter': amount_quarter,
            'amount_year': amount_year,
            'active': True,
            'override_product_price': True,
            'start_immediately': True,
        }
        plan = Plan.create(plan_vals)

        stripe.api_key = env['ir.config_parameter'].sudo().get_param('stripe_secret_key')
        product_id = plan.stripe_product_id
        if not product_id:
            try:
                product = stripe.Product.create(name=plan.name)
            except stripe.error.StripeError as err:
                _logger.error("Unable to create Stripe product for plan %s: %s", plan.name, err)
                raise ValueError(f"Failed to create Stripe product for plan {plan.name}") from err
            product_id = product['id'] if isinstance(product, dict) else getattr(product, 'id', None)
            if product_id:
                plan.stripe_product_id = product_id
            else:
                raise ValueError(f"Stripe did not return a product ID for plan {plan.name}")

        interval, interval_count = SubscriptionUtils._map_billing_cycle_to_stripe(billing_cycle)
        unit_amount_cents = SubscriptionUtils._to_minor_units(unit_price)
        try:
            price = stripe.Price.create(
                product=product_id,
                currency=(currency_code or 'usd').lower(),
                unit_amount=unit_amount_cents,
                recurring={'interval': interval, 'interval_count': interval_count},
                metadata={'odoo_migration': 'dynamic_plan'},
            )
        except stripe.error.StripeError as err:
            _logger.error("Unable to create Stripe price for plan %s: %s", plan.name, err)
            raise ValueError(f"Failed to create Stripe price for plan {plan.name}") from err
        price_id = price['id'] if isinstance(price, dict) else getattr(price, 'id', None)
        if not price_id:
            raise ValueError(f"Stripe did not return a price ID for plan {plan.name}")

        if billing_cycle == 'monthly':
            plan.stripe_price_month_id = price_id
        elif billing_cycle == 'quarterly':
            plan.stripe_price_quarter_id = price_id
        elif billing_cycle == 'annually':
            plan.stripe_price_year_id = price_id

        _logger.info(
            "Dynamically created plan %s (%s) with Stripe price %s for protocol %s.",
            plan.id,
            plan.name,
            price_id,
            protocol.name,
        )
        return plan, price_id

    @staticmethod
    def _parse_item_code(item_code):
        """Parse the item code to extract protocol, subscription type, billing cycle, and plan name."""
        parts = item_code.split('_')
        if len(parts) < 4:
            raise ValueError(f"Invalid item code format: {item_code}")

        protocol_name = parts[0].capitalize()
        subscription_type = 'validator' if 'VALIDATOR' in parts else 'rpc' if 'RPC' in parts else 'archive' if 'ARCHIVE' in parts else 'unknown'
        billing_cycle = 'monthly' if 'M' in parts else 'annually' if 'Y' in parts else 'quarterly' if 'Q' in parts else ''
        finalised = any('FINALISED' in part or 'FINALIZED' in part for part in parts)
        plan_name = 'Advance' if 'DEDICATED' in parts else 'Basic' if 'SHARED' in parts else 'Enterprise' if 'ENTERPRISE' in parts else 'unknown'
        if plan_name in {'Advance', 'Enterprise'} and finalised:
            plan_name = f"{plan_name} (Finalised View)"

        return protocol_name, subscription_type, billing_cycle, plan_name

    @staticmethod
    def _map_node_state(raw_state, default="draft"):
        """Return a valid node state value based on varied Zoho labels."""
        if not raw_state:
            return default
        normalized = str(raw_state).strip().lower()
        mapping = {
            "requested": "requested",
            "provisioning": "provisioning",
            "in_grace": "in_grace",
            "in grace": "in_grace",
            "syncing": "syncing",
            "in_progress": "syncing",
            "in progress": "syncing",
            "ready": "ready",
            "active": "ready",
            "live": "active",
            "trial": "syncing",
            "trialing": "syncing",
            "suspended": "suspended",
            "paused": "suspended",
            "cancelled": "canceled",
            "canceled": "canceled",
            "cancel": "closed",
            "closed": "closed",
            "expired": "closed",
            "deleted": "deleted",
            "draft": "draft",
        }
        return mapping.get(normalized, default)

    @staticmethod
    def _map_subscription_state(status_value):
        """Translate Zoho subscription status values to Odoo states."""
        return SubscriptionUtils._map_node_state(status_value, default="draft")

    @staticmethod
    def _map_stripe_status(status_value):
        """Return a valid Stripe status for the subscription selection field."""
        if not status_value:
            return None
        normalized = str(status_value).strip().lower()
        mapping = {
            'live': 'active',
            'canceled': 'canceled',
            'cancelled_by_customer': 'canceled',
            'cancelled by customer': 'canceled',
            'cancel': 'canceled',
            'trial': 'trialing',
            'trialling': 'trialing',
            'trialing': 'trialing',
        }
        candidate = mapping.get(normalized)
        if candidate is None:
            candidate = normalized.replace(" ", "_")
        if candidate in _STRIPE_STATUS_VALUES:
            return candidate
        _logger.warning(
            "Unsupported Stripe status '%s'; defaulting to 'draft'.",
            status_value,
        )
        return 'draft'

    @staticmethod
    def _to_float(value, default=0.0):
        """Best-effort conversion of mixed numeric strings to float."""
        if value in (None, ""):
            return default
        if isinstance(value, (int, float)):
            return float(value)
        cleaned = _NUMERIC_CLEANER.sub("", str(value))
        if cleaned in {"", ".", "-", "-."}:
            return default
        try:
            return float(cleaned)
        except (ValueError, TypeError):
            return default

    @staticmethod
    def _to_int(value, default=0):
        """Best-effort conversion of string quantities to integers."""
        if value in (None, ""):
            return default
        if isinstance(value, int):
            return value
        try:
            return int(float(str(value).strip()))
        except (ValueError, TypeError):
            return default

    @staticmethod
    def _to_minor_units(value):
        """Convert a decimal price to Stripe's integer minor units (cents)."""
        if value in (None, ""):
            return 0
        try:
            quantized = Decimal(str(value)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
            return int(quantized * 100)
        except (InvalidOperation, ValueError, TypeError):
            return 0

    @staticmethod
    def _get_stripe_price_data(stripe_price_id):
        """Retrieve and cache Stripe price metadata for comparisons."""
        if not stripe_price_id:
            return {}
        cached = _STRIPE_PRICE_CACHE.get(stripe_price_id)
        if cached:
            return cached
        try:
            price = stripe.Price.retrieve(stripe_price_id)
        except stripe.error.StripeError as err:
            _logger.error("Failed to retrieve Stripe price %s: %s", stripe_price_id, err)
            return {}

        if hasattr(price, 'to_dict_recursive'):
            price = price.to_dict_recursive()
        elif hasattr(price, 'to_dict'):
            price = price.to_dict()
        data = {
            'id': price.get('id'),
            'currency': price.get('currency'),
            'unit_amount': price.get('unit_amount'),
            'recurring': price.get('recurring') or {},
            'product': price.get('product'),
        }
        _STRIPE_PRICE_CACHE[stripe_price_id] = data
        return data

    @staticmethod
    def _map_billing_cycle_to_stripe(billing_cycle):
        """Return Stripe interval metadata for supported billing cycles."""
        mapping = {
            'monthly': ('month', 1),
            'annually': ('year', 1),
            'quarterly': ('month', 3),
        }
        return mapping.get(billing_cycle, ('month', 1))

    @staticmethod
    def _ensure_migration_price(plan, billing_cycle, currency_code, unit_amount_cents,
                                subscription_identifier=None, price_fallback=None):
        """Create or reuse a migration-only Stripe price for legacy subscriptions."""
        if not unit_amount_cents:
            return None

        product_id = plan.stripe_product_id
        if not product_id:
            product_id = (price_fallback or {}).get('product') if price_fallback else None
        if not product_id:
            _logger.error(
                "Unable to create migration price for plan %s: missing Stripe product ID.",
                plan.id,
            )
            return None

        interval, interval_count = SubscriptionUtils._map_billing_cycle_to_stripe(billing_cycle)
        currency_key = (currency_code or 'usd').lower()
        cache_key = (
            product_id,
            interval,
            interval_count,
            currency_key,
            unit_amount_cents,
        )
        cached_price = _MIGRATION_PRICE_CACHE.get(cache_key)
        if cached_price:
            return cached_price

        metadata = {
            'odoo_migration': 'legacy_price',
            'plan_id': str(plan.id),
            'subscription_identifier': subscription_identifier or '',
        }
        try:
            stripe_price = stripe.Price.create(
                product=product_id,
                currency=currency_key,
                unit_amount=unit_amount_cents,
                recurring={'interval': interval, 'interval_count': interval_count},
                metadata=metadata,
            )
        except stripe.error.StripeError as err:
            _logger.error(
                "Failed to create migration price for plan %s (%s): %s",
                plan.id,
                subscription_identifier,
                err,
            )
            return None

        price_id = stripe_price['id'] if isinstance(stripe_price, dict) else getattr(stripe_price, 'id', None)
        if not price_id:
            _logger.error("Stripe returned no price ID for migration entry %s.", subscription_identifier)
            return None

        _MIGRATION_PRICE_CACHE[cache_key] = price_id
        _logger.info(
            "Created migration price %s for subscription %s (plan %s, %s %s).",
            price_id,
            subscription_identifier,
            plan.id,
            unit_amount_cents,
            currency_code,
        )
        return price_id

    @staticmethod
    def _find_matching_subscription(env, zoho_subscription_id, plan, protocol, payment_frequency):
        """Locate an existing subscription with the same Zoho ID, plan, protocol, and frequency."""
        Subscription = env["subscription.subscription"].sudo()
        identifier = (zoho_subscription_id or "").strip()
        if not identifier:
            return Subscription.browse()

        candidates = Subscription.search([
            "|",
            ("zoho_subscription_id", "=", identifier),
            ("subscription_uuid", "=", identifier),
            ("stripe_status", "!=", "canceled"),
        ])
        payment_frequency = payment_frequency or ""
        for candidate in candidates:
            if candidate.sub_plan_id.id != plan.id:
                continue
            if candidate.protocol_id.id != protocol.id:
                continue
            if (candidate.payment_frequency or "") != payment_frequency:
                continue
            return candidate
        return Subscription.browse()

    @staticmethod
    def _update_stripe_subscription_quantity(env, subscription, stripe_price_id):
        """Update the Stripe subscription item quantity without charging immediately."""
        stripe_sub_id = subscription.stripe_subscription_id
        if not stripe_sub_id:
            return False, "No Stripe subscription ID stored on Odoo record."

        stripe.api_key = env['ir.config_parameter'].sudo().get_param('stripe_secret_key')
        try:
            stripe_subscription = stripe.Subscription.retrieve(stripe_sub_id, expand=['items'])
        except stripe.error.StripeError as err:
            _logger.error(
                "Failed to retrieve Stripe subscription %s for Odoo subscription %s: %s",
                stripe_sub_id,
                subscription.id,
                err,
            )
            return False, str(err)

        target_item = None
        items = stripe_subscription.get('items', {}).get('data', [])
        if stripe_price_id:
            for item in items:
                price = item.get('price') or {}
                if price.get('id') == stripe_price_id:
                    target_item = item
                    break
        if not target_item and items:
            target_item = items[0]

        if not target_item:
            return False, "Stripe subscription has no billable items to update."

        subscription_quantity = int(subscription.quantity or 1)
        try:
            updated = stripe.Subscription.modify(
                stripe_sub_id,
                items=[{
                    'id': target_item['id'],
                    'quantity': subscription_quantity,
                }],
                proration_behavior='none',
            )
        except stripe.error.StripeError as err:
            _logger.error(
                "Failed to update Stripe subscription %s quantity: %s",
                stripe_sub_id,
                err,
            )
            return False, str(err)

        write_vals = {}
        if updated.get('status'):
            write_vals['stripe_status'] = updated['status']
        current_period = updated.get('current_period_end')
        if current_period:
            write_vals['current_term_end'] = fields.Datetime.to_string(
                datetime.fromtimestamp(current_period, timezone.utc)
            )
        if updated.get('customer'):
            write_vals['stripe_customer_id'] = updated['customer']
        if write_vals:
            subscription.write(write_vals)
        return True, None

    @staticmethod
    def _parse_node_metadata(env, raw_metadata):
        """Parse the Node Metadata column into node value dictionaries."""
        if not raw_metadata:
            return []
        payload = raw_metadata
        if isinstance(payload, str):
            try:
                payload = json.loads(payload)
            except json.JSONDecodeError:
                _logger.warning("Skipping node metadata parsing due to invalid JSON payload: %s", raw_metadata)
                return []
        nodes = []
        if isinstance(payload, dict):
            if 'nodes' in payload and isinstance(payload['nodes'], list):
                nodes = payload['nodes']
            else:
                nodes = [payload]
        elif isinstance(payload, list):
            nodes = payload
        results = []
        Network = env['zeeve.network.type'].sudo()
        Location = env['server.location'].sudo()
        for entry in nodes:
            if not isinstance(entry, dict):
                continue
            data = {}
            data['node_name'] = entry.get('node_name') or entry.get('nodeName') or entry.get('name')
            identifier = entry.get('node_identifier') or entry.get('nodeIdentifier') or entry.get('nodeId') or entry.get('id')
            if identifier:
                data['node_identifier'] = identifier
            state_value = entry.get('state') or entry.get('status')
            if state_value:
                data['state'] = SubscriptionUtils._map_node_state(state_value)
            endpoint = entry.get('endpoint') or entry.get('endpoint_url')
            if endpoint:
                data['endpoint_url'] = endpoint
            network_name = entry.get('network') or entry.get('network_name') or entry.get('networkSelection')
            if network_name:
                network_rec = Network.search([('name', 'ilike', network_name)], limit=1)
                if network_rec:
                    data['network_selection_id'] = network_rec.id
            location_name = entry.get('server_location') or entry.get('serverLocation')
            if location_name:
                location_rec = Location.search([('name', 'ilike', location_name)], limit=1)
                if location_rec:
                    data['server_location_id'] = location_rec.id
            software_rule = entry.get('software_update_rule') or entry.get('softwareUpdateRule') or entry.get('softwareUpdates')
            if software_rule:
                normalized = str(software_rule).lower()
                data['software_update_rule'] = 'auto' if normalized in {'auto', 'automatic', 'automatically'} else 'manual'
            metadata_entry = entry.get('meta_data') or entry.get('metadata_json')
            if metadata_entry:
                try:
                    data['metadata_json'] = json.dumps(metadata_entry) if not isinstance(metadata_entry, str) else metadata_entry
                except Exception:  # pragma: no cover
                    data['metadata_json'] = str(metadata_entry)
            validator_info = entry.get('validator_info') or entry.get('validatorInfo')
            if validator_info:
                try:
                    data['validator_info'] = json.dumps(validator_info) if not isinstance(validator_info, str) else validator_info
                except Exception:  # pragma: no cover
                    data['validator_info'] = str(validator_info)
            results.append(data)
        return results

    @staticmethod
    def create_subscription(env, odoo_data, stripe_data, customer, node_vals=None, create_stripe=True):
        """Create an Odoo subscription, corresponding nodes, and optionally a Stripe subscription."""
        with env.cr.savepoint():
            # Create the subscription in Odoo
            subscription = env['subscription.subscription'].sudo().create(odoo_data)
            node_payloads = []
            if node_vals:
                if isinstance(node_vals, list):
                    node_payloads = node_vals
                else:
                    node_payloads = [node_vals]
            for payload in node_payloads:
                subscription.create_primary_node(payload)

        stripe_created = False
        stripe_error = None
        stripe_subscription = None

        stripe.api_key = env['ir.config_parameter'].sudo().get_param('stripe_secret_key')

        # Prepare metadata for Stripe
        first_node_payload = node_payloads[0] if node_payloads else {}
        network_type_value = ''
        server_location_value = ''
        automatic_update_value = 'auto'
        if first_node_payload.get('network_selection_id'):
            network_rec = env['zeeve.network.type'].sudo().browse(first_node_payload['network_selection_id'])
            if network_rec:
                network_type_value = network_rec.name
        if first_node_payload.get('server_location_id'):
            location_rec = env['server.location'].sudo().browse(first_node_payload['server_location_id'])
            if location_rec:
                server_location_value = location_rec.name
        if first_node_payload.get('software_update_rule'):
            automatic_update_value = str(first_node_payload.get('software_update_rule')).lower()

        metadata = {
            'subscription_id': str(subscription.id),
            'customer_id': str(customer.id),
            'plan_id': str(odoo_data['sub_plan_id']),
            'product_id': str(odoo_data['subscription_type']),
            'autopay_enabled': str(odoo_data.get('autopay_enabled', True)).lower(),
            'subscription_type': odoo_data['subscription_type'],
            'protocol_id': str(odoo_data['protocol_id']),
            'network_type': network_type_value,
            'server_location_id': server_location_value,
            'automatic_update': automatic_update_value,
        }
        if stripe_data.get('original_created_date'):
            metadata['imported_created_date'] = stripe_data['original_created_date']
        if stripe_data.get('next_billing_date'):
            metadata['imported_next_billing_date'] = stripe_data['next_billing_date']

        if not create_stripe:
            return subscription, stripe_created, stripe_error

        # Use existing Stripe customer ID if available, otherwise send customer email
        stripe_customer_id = stripe_data.get('customer')
        payment_method_ready = stripe_data.get('payment_method_ready', True)
        if not stripe_customer_id and stripe_data.get('customer_email'):
            partner = env['res.partner'].sudo().search([('email', '=', stripe_data['customer_email'])], limit=1)
            if partner and partner.stripe_customer_id:
                stripe_customer_id = partner.stripe_customer_id

        if not stripe_customer_id:
            stripe_error = "Missing Stripe customer identifier; skipped Stripe subscription creation."
            _logger.warning(
                "Missing Stripe customer identifier for Odoo subscription %s; skipped Stripe subscription creation.",
                subscription.id,
            )
            return subscription, stripe_created, stripe_error

        if not payment_method_ready:
            stripe_error = f"Stripe customer {stripe_customer_id} has no default payment method."
            _logger.warning(
                "Skipped Stripe subscription creation for Odoo subscription %s because no payment method is linked to customer %s.",
                subscription.id,
                stripe_customer_id,
            )
            return subscription, stripe_created, stripe_error

        # Directly create the subscription in Stripe without a checkout session
        stripe_subscription_data = {
            'customer': stripe_customer_id,
            'items': stripe_data['items'],
            'metadata': metadata,
            'collection_method': 'charge_automatically',
        }
        billing_cycle_anchor = stripe_data.get('billing_cycle_anchor')
        if billing_cycle_anchor:
            stripe_subscription_data['billing_cycle_anchor'] = billing_cycle_anchor
            stripe_subscription_data['proration_behavior'] = stripe_data.get('proration_behavior', 'none')
        trial_end = stripe_data.get('trial_end')
        if trial_end:
            stripe_subscription_data['trial_end'] = trial_end
            stripe_subscription_data.pop('billing_cycle_anchor', None)
            stripe_subscription_data.pop('proration_behavior', None)
        elif stripe_data.get('trial_days'):
            stripe_subscription_data['trial_period_days'] = stripe_data['trial_days']
            stripe_subscription_data.pop('billing_cycle_anchor', None)
            stripe_subscription_data.pop('proration_behavior', None)

        try:
            stripe_subscription = stripe.Subscription.create(**stripe_subscription_data)
        except stripe.error.StripeError as err:
            stripe_error = str(err)
            _logger.error(
                "Failed to create Stripe subscription for Odoo subscription %s: %s",
                subscription.id,
                err,
            )
            return subscription, stripe_created, stripe_error

        stripe_created = True
        if stripe_subscription:
            items = stripe_subscription.get('items')
            data = items.get('data')[0]
            current_start = data.get('current_period_start')
            current_end = data.get('current_period_end') 
            write_vals = {
                'stripe_subscription_id': stripe_subscription.id,
            }
            if stripe_subscription.get('status'):
                write_vals['stripe_status'] = stripe_subscription['status']
            if current_start:
                write_vals['current_term_start'] = fields.Datetime.to_string(
                    datetime.fromtimestamp(current_start, timezone.utc)
                )
            if current_end:
                write_vals['current_term_end'] = fields.Datetime.to_string(
                    datetime.fromtimestamp(current_end, timezone.utc)
                )
            if stripe_subscription.get('customer'):
                write_vals['stripe_customer_id'] = stripe_subscription['customer']
            subscription.write(write_vals)

        return subscription, stripe_created, stripe_error

    @staticmethod
    def handle_subscription_row(env, row):
        """Handle a single row of Zoho subscription data."""
        customer_email = row.get('Customer Email')
        customer_name = row.get('Customer Name')
        stripe_customer_id = row.get('Customer ID')
        stripe.api_key = env['ir.config_parameter'].sudo().get_param('stripe_secret_key')

        partner, _user, stripe_customer_id_val = SubscriptionUtils._ensure_odoo_customer(
            env, customer_email, customer_name, stripe_customer_id
        )
        customer_identifier = customer_email or customer_name or stripe_customer_id or 'unknown'
        if not partner:
            message = f"Partner lookup failed for {customer_identifier}."
            _logger.warning("Skipping row for %s because partner creation failed.", customer_identifier)
            return {
                'status': 'skipped',
                'message': message,
            }

        status_label = (row.get('Status') or row.get('Subscription Status') or '').strip()
        stripe_status = SubscriptionUtils._map_stripe_status(status_label)
        stripe_required = stripe_status == 'active'
        subscription_state = SubscriptionUtils._map_subscription_state(status_label)
        payment_mode = (row.get('Payment Mode') or '').strip().lower()
        helper_notes = []

        create_stripe = stripe_required
        if payment_mode == 'offline':
            stripe_required = False
            create_stripe = False
            helper_notes.append(
                f"Stripe skipped for {customer_identifier}: payment mode is offline."
            )

        payment_method_ready = True
        if create_stripe:
            if not stripe_customer_id_val:
                helper_notes.append(
                    f"Stripe skipped for {customer_identifier}: no Stripe customer linked to partner."
                )
                create_stripe = False
            else:
                stripe_customer_obj = SubscriptionUtils._retrieve_stripe_customer(stripe_customer_id_val)
                if not stripe_customer_obj:
                    helper_notes.append(
                        f"Stripe skipped for {customer_identifier}: customer {stripe_customer_id_val} not found on Stripe."
                    )
                    create_stripe = False
                else:
                    stripe_customer_obj, payment_method_ready = SubscriptionUtils._ensure_payment_method_ready(
                        stripe_customer_obj
                    )
                    stripe_customer_id_val = stripe_customer_obj.get('id', stripe_customer_id_val)
                    if not payment_method_ready:
                        helper_notes.append(
                            f"Stripe skipped for {customer_identifier}: no payment method on customer {stripe_customer_id_val}."
                        )
                        create_stripe = False

        subscription_identifier = (row.get('Subscription ID') or '').strip()
        if not subscription_identifier:
            subscription_identifier = (row.get('Node ID') or '').strip()

        protocol_str = (row.get('CF.protocolName') or '').strip()
        if not subscription_identifier and protocol_str:
            try:
                protocol_json = json.loads(protocol_str)
                subscription_identifier = protocol_json.get('updatedValue', [{}])[0].get('networkId')
            except (json.JSONDecodeError, TypeError, AttributeError):
                subscription_identifier = None

        network = row.get('Network Selection') or 'Testnet'
        next_billing_date = row.get('Next Billing Date')
        meta_data = row.get('MetaData')
        meta_network = None
        software_update_rule = None
        if meta_data:
            try:
                meta_json = json.loads(meta_data) if isinstance(meta_data, str) else meta_data
                nodes_section = meta_json.get('nodes') if isinstance(meta_json, dict) else None
                node_entry = nodes_section[0] if isinstance(nodes_section, list) and nodes_section else {}
                inputs = node_entry.get('inputs') if isinstance(node_entry, dict) else {}
                meta_network = inputs.get('network') if isinstance(inputs, dict) else None
                general_section = meta_json.get('general') if isinstance(meta_json, dict) else {}
                if isinstance(general_section, dict):
                    software_update_rule = general_section.get('softwareUpdates')
            except (json.JSONDecodeError, TypeError, AttributeError):
                meta_network = None

        resolved_network = (meta_network or network or '').strip()
        network_rec = env['zeeve.network.type'].sudo().search([('name', 'ilike', resolved_network or 'Testnet')], limit=1)
        node_metadata_raw = row.get('Node Metadata') or row.get('Node Metadata.'.strip())
        parsed_nodes = SubscriptionUtils._parse_node_metadata(env, node_metadata_raw)

        quantity = SubscriptionUtils._to_int(row.get('Quantity', 1), 1)
        unit_price = SubscriptionUtils._to_float(row.get('Item Price', 0.0), 0.0)
        total_price = unit_price * quantity

        currency_code_value = (row.get('Currency Code') or '').strip()
        currency = env['res.currency'].sudo().search([('name', 'ilike', currency_code_value)], limit=1)
        stripe_currency_code = (currency.name if currency and currency.name else currency_code_value or 'USD')
        stripe_currency_code = (stripe_currency_code or 'USD').strip() or 'USD'
        stripe_currency_code = stripe_currency_code.lower()

        # Map item code to plan and Stripe price ID (creating plans for special items if needed)
        item_code = row.get('Item Code')
        try:
            plan, stripe_price_id, protocol, billing_cycle = SubscriptionUtils.map_item_code_to_plan(
                env,
                item_code,
                unit_price=unit_price,
                currency_code=stripe_currency_code,
                allow_create=True,
            )
        except ValueError as err:
            _logger.error("Row skipped due to plan mapping error: %s", err)
            return {
                'status': 'error',
                'message': str(err),
            }

        billing_cycle_anchor = SubscriptionUtils._compute_billing_cycle_anchor(
            next_billing_date,
            payment_frequency=billing_cycle,
        )
        trial_end_ts = SubscriptionUtils._compute_billing_cycle_anchor(row.get('Trial End Date'))
        if trial_end_ts and billing_cycle_anchor and trial_end_ts < billing_cycle_anchor:
            trial_end_ts = billing_cycle_anchor
        if not trial_end_ts and billing_cycle_anchor:
            trial_end_ts = billing_cycle_anchor

        stripe_price_details = SubscriptionUtils._get_stripe_price_data(stripe_price_id)
        unit_amount_cents = SubscriptionUtils._to_minor_units(unit_price)
        if create_stripe and stripe_price_id and unit_amount_cents:
            default_amount = stripe_price_details.get('unit_amount') if stripe_price_details else None
            default_currency = (stripe_price_details.get('currency') or '').lower() if stripe_price_details else ''
            amount_mismatch = default_amount is not None and default_amount != unit_amount_cents
            currency_mismatch = bool(default_currency and stripe_currency_code and default_currency != stripe_currency_code)
            if amount_mismatch or currency_mismatch:
                migration_price_id = SubscriptionUtils._ensure_migration_price(
                    plan,
                    billing_cycle,
                    stripe_currency_code or default_currency or 'usd',
                    unit_amount_cents,
                    subscription_identifier or row.get('Subscription#'),
                    price_fallback=stripe_price_details,
                )
                if migration_price_id:
                    stripe_price_id = migration_price_id
                    helper_notes.append(
                        f"Applied migration Stripe price {migration_price_id} for subscription {subscription_identifier or row.get('Subscription#') or partner.display_name}."
                    )
                else:
                    helper_notes.append(
                        f"Failed to create migration Stripe price for subscription {subscription_identifier or row.get('Subscription#') or partner.display_name}; default plan price will be used."
                    )
        print("subscription_utils", subscription_state, "str", status_label)
        odoo_data = {
            'subscription_ref': row.get('Subscription#'),
            'name': row.get('Subscription#'),
            'customer_name': partner.id,
            'start_date': row.get('Start Date'),
            'stripe_end_date': row.get('Next Billing Date'),
            'price': unit_price,
            'original_price': total_price,
            'quantity': quantity,
            'subscription_type': plan.subscription_type,
            'stripe_subscription_id': row.get('Subscription ID'),
            'zoho_subscription_id': row.get('Subscription ID'),
            'stripe_price_id': stripe_price_id,
            'currency_id': currency.id if currency else False,
            'protocol_id': protocol.id,
            'subscription_uuid': row.get('Subscription ID'),
            'payment_frequency': billing_cycle,
            'stripe_start_date': row.get('Start Date'),
            'sub_plan_id': plan.id,
            'stripe_status': stripe_status,
            'metaData': row.get('MetaData', {}),
        }

        default_node_vals = {
            'node_name': row.get('Name') or row.get('Subscription#') or partner.display_name,
            'node_identifier': subscription_identifier,
            'network_selection_id': network_rec.id if network_rec else False,
            'server_location_id': False,
            'software_update_rule': 'auto' if str(software_update_rule or '').lower() in {'auto', 'automatic', 'automatically'} else 'manual',
            'state': "ready" if subscription_state == "active" else subscription_state,
        }

        node_payloads = []
        if parsed_nodes:
            for entry in parsed_nodes:
                payload = dict(entry)
                payload.setdefault('node_name', default_node_vals['node_name'])
                payload.setdefault('node_identifier', default_node_vals['node_identifier'])
                payload.setdefault('network_selection_id', network_rec.id if network_rec else False)
                payload.setdefault('server_location_id', False)
                payload.setdefault('software_update_rule', default_node_vals['software_update_rule'])
                payload.setdefault('state', SubscriptionUtils._map_node_state(payload.get('state'), default=subscription_state or 'draft'))
                node_payloads.append(payload)

        include_nodes = bool(node_payloads)

        if billing_cycle == 'monthly':
            odoo_data.update({'duration': 1, 'unit': 'month'})
        elif billing_cycle == 'quarterly':
            odoo_data.update({'duration': 3, 'unit': 'month'})
        elif billing_cycle == 'annually':
            odoo_data.update({'duration': 1, 'unit': 'year'})

        stripe_data = {
            'customer': stripe_customer_id_val if create_stripe else None,
            'customer_email': customer_email,
            'items': [
                {
                    'price': stripe_price_id,
                    'quantity': quantity,
                }
            ],
            'payment_method_ready': payment_method_ready,
            'billing_cycle_anchor': billing_cycle_anchor,
            'proration_behavior': 'none',
            'original_created_date': row.get('Created Date'),
            'next_billing_date': next_billing_date,
            'trial_end': trial_end_ts,
        }

        existing_subscription = SubscriptionUtils._find_matching_subscription(
            env,
            row.get('Subscription ID'),
            plan,
            protocol,
            billing_cycle,
        )
        if existing_subscription:
            updates = {}
            if quantity:
                updates['quantity'] = (existing_subscription.quantity or 0) + quantity
            if total_price:
                updates['price'] = (existing_subscription.price or 0.0) + total_price
            if row.get('Next Billing Date'):
                updates['stripe_end_date'] = row.get('Next Billing Date')
            if subscription_state and existing_subscription.state == 'draft':
                updates['state'] = "ready" if subscription_state == "active" else subscription_state
            if stripe_price_id and existing_subscription.stripe_price_id != stripe_price_id:
                updates['stripe_price_id'] = stripe_price_id

            if updates:
                existing_subscription.write(updates)
            if include_nodes:
                for payload in node_payloads:
                    existing_subscription.create_primary_node(payload)

            stripe_update_note = ""
            stripe_update_error = None
            if existing_subscription.stripe_subscription_id:
                updated, stripe_update_error = SubscriptionUtils._update_stripe_subscription_quantity(
                    env,
                    existing_subscription,
                    stripe_price_id,
                )
                if updated:
                    stripe_update_note = " Stripe subscription quantity updated without proration."
                elif stripe_update_error:
                    helper_notes.append(
                        f"Stripe update failed for {existing_subscription.subscription_uuid or existing_subscription.id}: {stripe_update_error}"
                    )
            else:
                helper_notes.append(
                    f"Odoo subscription {existing_subscription.subscription_uuid or existing_subscription.id} has no Stripe subscription ID."
                )

            message = (
                f"Updated subscription {existing_subscription.name}: +{quantity} quantity,"
                f" total price now {existing_subscription.price}."
            )
            if include_nodes and node_payloads:
                message += f" Added {len(node_payloads)} node(s)."
            if stripe_update_note:
                message += stripe_update_note
            if helper_notes:
                message += " " + " ".join(helper_notes)
            result_status = 'success'
            if stripe_required and (not existing_subscription.stripe_subscription_id or stripe_update_error):
                result_status = 'partial'
            return {
                'status': result_status,
                'message': message,
                'subscription_uuid': existing_subscription.id,
                'stripe_created': bool(existing_subscription.stripe_subscription_id),
            }
        subscription, stripe_created, stripe_error = SubscriptionUtils.create_subscription(
            env,
            odoo_data,
            stripe_data,
            partner,
            node_payloads if include_nodes else None,
            create_stripe=create_stripe,
        )

        if stripe_error:
            helper_notes.append(
                f"Stripe error for subscription {subscription.subscription_uuid or subscription.id}: {stripe_error}"
            )

        message = f"Odoo subscription {subscription.name} created for partner {partner.display_name}."
        if billing_cycle_anchor and next_billing_date:
            message += f" Stripe billing anchored to {next_billing_date}."
        if helper_notes:
            message += " " + " ".join(helper_notes)

        status = 'success'
        if not stripe_created and stripe_required:
            status = 'partial'

        return {
            'status': status,
            'message': message,
            'subscription_uuid': subscription.id,
            'stripe_created': stripe_created,
        }
