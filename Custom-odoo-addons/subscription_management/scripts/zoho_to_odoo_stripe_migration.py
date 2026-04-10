# -*- coding: utf-8 -*-
"""
Zoho Billing → Odoo + Stripe Migration
=======================================
Migrates customers and subscriptions from Zoho Billing to Odoo and Stripe

This script:
1. Creates Odoo users from Zoho Contacts.csv (if they don't exist)
2. Creates Stripe customers
3. Creates subscriptions in both Odoo and Stripe
4. Handles payment method migration
5. Enables auto-deduction like Zoho

Usage:
    python zoho_to_odoo_stripe_migration.py --mode [customers|subscriptions|all] [--dry-run]
"""

import stripe
import csv
import logging
import argparse
import sys
import os
from datetime import datetime, timedelta
from pathlib import Path

# Add Odoo path
ODOO_PATH = os.environ.get('ODOO_PATH', '/opt/odoo')
sys.path.append(ODOO_PATH)

import odoo
from odoo import api, SUPERUSER_ID
from odoo.tools import config

_logger = logging.getLogger(__name__)


class ZohoToOdooStripeMigration:
    """Migrate from Zoho Billing to Odoo + Stripe"""
    
    def __init__(self, db_name, stripe_api_key, dry_run=False):
        self.db_name = db_name
        self.dry_run = dry_run
        stripe.api_key = stripe_api_key
        
        self.registry = odoo.registry(db_name)
        
        # Statistics
        self.stats = {
            'customers_processed': 0,
            'customers_created_odoo': 0,
            'customers_created_stripe': 0,
            'customers_existed': 0,
            'customers_errors': 0,
            'subscriptions_processed': 0,
            'subscriptions_created_odoo': 0,
            'subscriptions_created_stripe': 0,
            'subscriptions_existed': 0,
            'subscriptions_errors': 0,
            'payment_methods_requested': 0,
        }
        
        # Cache for processed data
        self.processed_customers = {}  # email -> (partner_id, stripe_customer_id)
        self.processed_subscriptions = set()  # subscription_ids
    
    def migrate_customers_from_zoho(self, csv_file):
        """
        Import customers from Zoho Contacts.csv
        Creates both Odoo partners and Stripe customers
        """
        _logger.info("=" * 80)
        _logger.info("MIGRATING CUSTOMERS FROM ZOHO")
        _logger.info("=" * 80)
        
        with open(csv_file, 'r', encoding='utf-8') as f:
            reader = csv.DictReader(f)
            
            for row in reader:
                self.stats['customers_processed'] += 1
                
                try:
                    # Extract Zoho customer data
                    zoho_customer_id = row.get('Customer ID', '').strip()
                    customer_name = row.get('Customer Name', '').strip()
                    display_name = row.get('Display Name', '').strip()
                    email = row.get('EmailID', '').strip()
                    phone = row.get('Phone', '').strip()
                    mobile = row.get('MobilePhone', '').strip()
                    
                    # Address data
                    billing_address = row.get('Billing Address', '').strip()
                    billing_city = row.get('Billing City', '').strip()
                    billing_state = row.get('Billing State', '').strip()
                    billing_country = row.get('Billing Country', '').strip()
                    billing_zip = row.get('Billing Code', '').strip()
                    
                    # Validate
                    if not email:
                        _logger.warning(f"Skipping Zoho customer {zoho_customer_id}: No email")
                        self.stats['customers_errors'] += 1
                        continue
                    
                    # Skip if already processed
                    if email in self.processed_customers:
                        _logger.info(f"Already processed: {email}")
                        continue
                    
                    with self.registry.cursor() as cr:
                        env = api.Environment(cr, SUPERUSER_ID, {})
                        
                        # Check if customer exists in Odoo
                        partner = env['res.partner'].search([('email', '=', email)], limit=1)
                        
                        if partner:
                            _logger.info(f"Customer already exists in Odoo: {email}")
                            self.stats['customers_existed'] += 1
                            
                            # Check if has Stripe ID
                            if not partner.stripe_customer_id and not self.dry_run:
                                # Create Stripe customer for existing Odoo partner
                                stripe_customer = self._create_stripe_customer(
                                    name=partner.name or customer_name,
                                    email=email,
                                    phone=phone or mobile,
                                    metadata={
                                        'odoo_partner_id': str(partner.id),
                                        'zoho_customer_id': zoho_customer_id,
                                        'source': 'zoho_migration'
                                    }
                                )
                                
                                partner.write({'stripe_customer_id': stripe_customer.id})
                                _logger.info(f"  Added Stripe ID: {stripe_customer.id}")
                                self.stats['customers_created_stripe'] += 1
                            
                            # Cache for subscription processing
                            self.processed_customers[email] = (partner.id, partner.stripe_customer_id)
                        else:
                            # Create new customer in Odoo
                            if not self.dry_run:
                                partner_vals = {
                                    'name': customer_name or display_name,
                                    'email': email,
                                    'phone': phone,
                                    'mobile': mobile,
                                    'street': billing_address,
                                    'city': billing_city,
                                    'state_id': self._get_state_id(env, billing_state, billing_country),
                                    'country_id': self._get_country_id(env, billing_country),
                                    'zip': billing_zip,
                                    'customer_rank': 1,
                                    'is_company': row.get('Contact Type', '') == 'business',
                                }
                                
                                partner = env['res.partner'].create(partner_vals)
                                _logger.info(f"✓ Created Odoo partner: {email} (ID: {partner.id})")
                                self.stats['customers_created_odoo'] += 1
                                
                                # Create Stripe customer
                                stripe_customer = self._create_stripe_customer(
                                    name=partner.name,
                                    email=email,
                                    phone=phone or mobile,
                                    address={
                                        'line1': billing_address,
                                        'city': billing_city,
                                        'state': billing_state,
                                        'country': self._map_country_code(billing_country),
                                        'postal_code': billing_zip,
                                    },
                                    metadata={
                                        'odoo_partner_id': str(partner.id),
                                        'zoho_customer_id': zoho_customer_id,
                                        'source': 'zoho_migration',
                                        'migration_date': datetime.now().isoformat()
                                    }
                                )
                                
                                partner.write({'stripe_customer_id': stripe_customer.id})
                                _logger.info(f"✓ Created Stripe customer: {stripe_customer.id}")
                                self.stats['customers_created_stripe'] += 1
                                
                                # Cache
                                self.processed_customers[email] = (partner.id, stripe_customer.id)
                            else:
                                _logger.info(f"[DRY RUN] Would create: {email}")
                                self.stats['customers_created_odoo'] += 1
                                self.stats['customers_created_stripe'] += 1
                        
                        cr.commit()
                        
                except Exception as e:
                    _logger.error(f"Error processing customer {zoho_customer_id}: {str(e)}")
                    import traceback
                    _logger.error(traceback.format_exc())
                    self.stats['customers_errors'] += 1
                    continue
        
        self._print_customer_stats()
    
    def migrate_subscriptions_from_zoho(self, csv_file):
        """
        Import subscriptions from Zoho Subscriptions.csv
        Creates subscriptions in both Odoo and Stripe
        """
        _logger.info("=" * 80)
        _logger.info("MIGRATING SUBSCRIPTIONS FROM ZOHO")
        _logger.info("=" * 80)
        
        # Group subscriptions by Subscription ID (CSV has multiple rows per subscription)
        subscriptions_data = self._parse_zoho_subscriptions(csv_file)
        
        _logger.info(f"Found {len(subscriptions_data)} unique subscriptions to migrate")
        
        for sub_id, sub_data in subscriptions_data.items():
            self.stats['subscriptions_processed'] += 1
            
            try:
                email = sub_data['customer_email']
                customer_name = sub_data['customer_name']
                
                if not email:
                    _logger.warning(f"Skipping subscription {sub_id}: No customer email")
                    self.stats['subscriptions_errors'] += 1
                    continue
                
                # Skip if already processed
                if sub_id in self.processed_subscriptions:
                    continue
                
                self.processed_subscriptions.add(sub_id)
                
                with self.registry.cursor() as cr:
                    env = api.Environment(cr, SUPERUSER_ID, {})
                    
                    # Get or create customer
                    partner = env['res.partner'].search([('email', '=', email)], limit=1)
                    
                    if not partner:
                        _logger.warning(f"Customer not found for subscription {sub_id}: {email}")
                        _logger.info("  Run customer migration first or customer will be skipped")
                        self.stats['subscriptions_errors'] += 1
                        continue
                    
                    # Check if subscription already exists
                    existing_sub = env['subscription.subscription'].search([
                        '|',
                        ('name', '=', sub_data['subscription_number']),
                        ('name', '=', sub_id)
                    ], limit=1)
                    
                    if existing_sub:
                        _logger.info(f"Subscription already exists: {sub_id}")
                        self.stats['subscriptions_existed'] += 1
                        continue
                    
                    # Find or create subscription plan
                    plan = self._find_or_create_plan(env, sub_data)
                    
                    if not plan:
                        _logger.error(f"Could not find/create plan for subscription {sub_id}")
                        self.stats['subscriptions_errors'] += 1
                        continue
                    
                    # Create subscription in Odoo
                    if not self.dry_run:
                        subscription = self._create_odoo_subscription(env, partner, plan, sub_data)
                        _logger.info(f"✓ Created Odoo subscription: {subscription.id}")
                        self.stats['subscriptions_created_odoo'] += 1
                        
                        # Create subscription in Stripe
                        if partner.stripe_customer_id:
                            stripe_sub = self._create_stripe_subscription(
                                customer_id=partner.stripe_customer_id,
                                plan=plan,
                                subscription_data=sub_data,
                                odoo_subscription_id=subscription.id
                            )
                            
                            # Update Odoo subscription with Stripe details
                            subscription.write({
                                'stripe_subscription_id': stripe_sub.id,
                                'stripe_customer_id': partner.stripe_customer_id,
                                'stripe_status': stripe_sub.status,
                            })
                            
                            _logger.info(f"✓ Created Stripe subscription: {stripe_sub.id}")
                            self.stats['subscriptions_created_stripe'] += 1
                        else:
                            _logger.warning(f"Partner {email} has no Stripe ID, skipping Stripe subscription")
                    else:
                        _logger.info(f"[DRY RUN] Would create subscription for {email}: {sub_data['subscription_number']}")
                        self.stats['subscriptions_created_odoo'] += 1
                        self.stats['subscriptions_created_stripe'] += 1
                    
                    cr.commit()
                    
            except Exception as e:
                _logger.error(f"Error processing subscription {sub_id}: {str(e)}")
                import traceback
                _logger.error(traceback.format_exc())
                self.stats['subscriptions_errors'] += 1
                continue
        
        self._print_subscription_stats()
    
    def _parse_zoho_subscriptions(self, csv_file):
        """
        Parse Zoho Subscriptions CSV and group by Subscription ID
        Each subscription has multiple rows (one per item/plan/addon)
        """
        subscriptions = {}
        
        with open(csv_file, 'r', encoding='utf-8') as f:
            reader = csv.DictReader(f)
            
            for row in reader:
                sub_id = row.get('Subscription ID', '').strip()
                if not sub_id:
                    continue
                
                if sub_id not in subscriptions:
                    subscriptions[sub_id] = {
                        'subscription_id': sub_id,
                        'subscription_number': row.get('Subscription#', '').strip(),
                        'customer_name': row.get('Customer Name', '').strip(),
                        'customer_email': row.get('Customer Email', '').strip(),
                        'created_date': row.get('Created Date', '').strip(),
                        'start_date': row.get('Start Date', '').strip(),
                        'expiry_date': row.get('Expiry Date', '').strip(),
                        'next_billing_date': row.get('Next Billing Date', '').strip(),
                        'last_billing_date': row.get('Last Billing Date', '').strip(),
                        'subscription_status': row.get('Subscription Status', '').strip(),
                        'subscription_frequency': row.get('Subscription Frequency', '').strip(),
                        'currency': row.get('Currency Code', 'USD').strip(),
                        'total_amount': float(row.get('Total', '0').strip() or '0'),
                        'subtotal': float(row.get('SubTotal', '0').strip() or '0'),
                        'card_id': row.get('Card ID', '').strip(),
                        'items': [],
                        'metadata': row.get('CF.metaData', '').strip(),
                        'network_ids': row.get('CF.Network_ids', '').strip(),
                    }
                
                # Add line item if it's a plan (not addon)
                item_type = row.get('Line Item Type', '').strip()
                item_name = row.get('Item Name', '').strip()
                item_code = row.get('Item Code', '').strip()
                item_price = float(row.get('Item Price', '0').strip() or '0')
                quantity = float(row.get('Quantity', '1').strip() or '1')
                
                if item_type == 'Plan' and item_name:
                    subscriptions[sub_id]['main_plan'] = {
                        'name': item_name,
                        'code': item_code,
                        'price': item_price,
                        'quantity': quantity,
                    }
                
                subscriptions[sub_id]['items'].append({
                    'name': item_name,
                    'code': item_code,
                    'price': item_price,
                    'quantity': quantity,
                    'type': item_type,
                })
        
        return subscriptions
    
    def _create_odoo_subscription(self, env, partner, plan, sub_data):
        """Create subscription in Odoo"""
        
        # Parse dates
        start_date = self._parse_date(sub_data['start_date']) or datetime.now().date()
        next_billing = self._parse_date(sub_data['next_billing_date'])
        
        # Determine payment frequency
        frequency = sub_data['subscription_frequency'].lower()
        payment_frequency = 'monthly'
        duration = 1
        unit = 'month'
        
        if 'month' in frequency:
            payment_frequency = 'monthly'
            duration = 1
            unit = 'month'
        elif 'quarter' in frequency:
            payment_frequency = 'quarterly'
            duration = 3
            unit = 'month'
        elif 'year' in frequency or 'annual' in frequency:
            payment_frequency = 'annually'
            duration = 1
            unit = 'year'
        
        # Determine state based on Zoho status
        state = 'draft'
        if sub_data['subscription_status'] in ['live', 'active']:
            state = 'in_progress'
        elif sub_data['subscription_status'] == 'cancelled':
            state = 'cancel'
        elif 'trial' in sub_data['subscription_status']:
            state = 'in_progress'  # Will have trial in Stripe
        
        subscription_vals = {
            'name': sub_data['subscription_number'] or f"ZOHO-{sub_data['subscription_id']}",
            'customer_name': partner.id,
            'sub_plan_id': plan.id,
            'product_id': plan.product_id.id if plan.product_id else False,
            'price': sub_data['total_amount'],
            'quantity': sub_data.get('main_plan', {}).get('quantity', 1),
            'start_date': start_date,
            'duration': duration,
            'unit': unit,
            'payment_frequency': payment_frequency,
            'state': state,
            'source': 'zoho_migration',
            'autopay_enabled': True,  # Enable auto-deduction like Zoho
            'next_payment_date': next_billing,
            'never_expires': True,  # Like Zoho recurring
            'num_billing_cycle': -1,
        }
        
        subscription = env['subscription.subscription'].create(subscription_vals)
        subscription.create_primary_node({
            'node_type': subscription.subscription_type or plan.subscription_type,
        })
        return subscription
    
    def _create_stripe_customer(self, name, email, phone=None, address=None, metadata=None):
        """Create customer in Stripe"""
        customer_data = {
            'name': name,
            'email': email,
            'metadata': metadata or {},
            'preferred_locales': ['en'],
        }
        
        if phone:
            customer_data['phone'] = phone
        
        if address:
            customer_data['address'] = address
        
        return stripe.Customer.create(**customer_data)
    
    def _create_stripe_subscription(self, customer_id, plan, subscription_data, odoo_subscription_id):
        """Create subscription in Stripe"""
        
        # Get appropriate price ID based on frequency
        frequency = subscription_data['subscription_frequency'].lower()
        
        if 'month' in frequency:
            price_id = plan.stripe_price_month_id
        elif 'quarter' in frequency:
            price_id = plan.stripe_price_quarter_id
        elif 'year' in frequency or 'annual' in frequency:
            price_id = plan.stripe_price_year_id
        else:
            price_id = plan.stripe_price_month_id
        
        if not price_id:
            raise ValueError(f"Plan {plan.name} has no Stripe price for frequency: {frequency}")
        
        # Prepare subscription data
        stripe_sub_data = {
            'customer': customer_id,
            'items': [{
                'price': price_id,
                'quantity': int(subscription_data.get('main_plan', {}).get('quantity', 1))
            }],
            'metadata': {
                'odoo_subscription_id': str(odoo_subscription_id),
                'zoho_subscription_id': subscription_data['subscription_id'],
                'zoho_subscription_number': subscription_data['subscription_number'],
                'source': 'zoho_migration',
                'migration_date': datetime.now().isoformat(),
            },
            'collection_method': 'charge_automatically',  # Enable auto-deduction
            'days_until_due': None,  # Immediate payment
        }
        
        # Handle trial if subscription is in trial
        if 'trial' in subscription_data['subscription_status']:
            trial_end = self._parse_date(subscription_data.get('trial_end_date'))
            if trial_end and trial_end > datetime.now():
                days_left = (trial_end - datetime.now()).days
                if days_left > 0:
                    stripe_sub_data['trial_period_days'] = days_left
        
        # Handle cancelled subscriptions (create but immediately cancel)
        cancel_at_period_end = subscription_data['subscription_status'] == 'cancelled'
        if cancel_at_period_end:
            stripe_sub_data['cancel_at_period_end'] = True
        
        subscription = stripe.Subscription.create(**stripe_sub_data)
        
        return subscription
    
    def _find_or_create_plan(self, env, sub_data):
        """
        Find existing plan in Odoo or create new one
        Maps Zoho plans to Odoo subscription plans
        """
        main_plan = sub_data.get('main_plan', {})
        if not main_plan:
            _logger.warning(f"No main plan found in subscription data")
            return None
        
        plan_name = main_plan.get('name', '')
        plan_code = main_plan.get('code', '')
        plan_price = main_plan.get('price', 0)
        
        # Try to find existing plan by code or name
        plan = env['subscription.plan'].search([
            '|',
            ('name', '=ilike', plan_name),
            ('name', '=ilike', f'%{plan_code}%')
        ], limit=1)
        
        if plan:
            _logger.info(f"Found existing plan: {plan.name}")
            return plan
        
        # Create new plan if not found
        _logger.info(f"Creating new plan: {plan_name}")
        
        # Determine frequency and amounts
        frequency = sub_data['subscription_frequency'].lower()
        
        plan_vals = {
            'name': plan_name,
            'plan_amount': plan_price,
            'active': True,
            'never_expires': True,
            'num_billing_cycle': -1,
            'start_immediately': True,
            'override_product_price': True,
        }
        
        # Set duration based on frequency
        if 'month' in frequency:
            plan_vals.update({
                'duration': 1,
                'unit': 'month',
                'amount_month': plan_price,
            })
        elif 'quarter' in frequency:
            plan_vals.update({
                'duration': 3,
                'unit': 'month',
                'amount_quarter': plan_price,
            })
        elif 'year' in frequency or 'annual' in frequency:
            plan_vals.update({
                'duration': 1,
                'unit': 'year',
                'amount_year': plan_price,
            })
        
        plan = env['subscription.plan'].create(plan_vals)
        
        # Sync with Stripe
        try:
            plan.action_sync_with_stripe()
            _logger.info(f"✓ Synced new plan with Stripe: {plan.name}")
        except Exception as e:
            _logger.error(f"Error syncing plan with Stripe: {str(e)}")
            raise
        
        return plan
    
    def setup_payment_methods(self):
        """
        Setup payment method collection for migrated customers
        
        NOTE: Due to PCI compliance, we CANNOT directly migrate credit card numbers.
        Instead, we create Setup Intents for customers to re-enter their cards.
        """
        _logger.info("=" * 80)
        _logger.info("SETTING UP PAYMENT METHOD COLLECTION")
        _logger.info("=" * 80)
        
        with self.registry.cursor() as cr:
            env = api.Environment(cr, SUPERUSER_ID, {})
            
            # Get all customers migrated from Zoho without payment methods
            partners = env['res.partner'].search([
                ('stripe_customer_id', '!=', False),
            ])
            
            for partner in partners:
                try:
                    # Check if customer has payment method in Stripe
                    payment_methods = stripe.PaymentMethod.list(
                        customer=partner.stripe_customer_id,
                        type='card',
                        limit=1
                    )
                    
                    if not payment_methods.data:
                        # Create Setup Intent for this customer
                        if not self.dry_run:
                            setup_intent = stripe.SetupIntent.create(
                                customer=partner.stripe_customer_id,
                                metadata={
                                    'odoo_partner_id': str(partner.id),
                                    'purpose': 'zoho_migration_payment_method',
                                },
                                usage='off_session',  # For recurring payments
                            )
                            
                            _logger.info(f"✓ Created Setup Intent for {partner.email}: {setup_intent.client_secret}")
                            self.stats['payment_methods_requested'] += 1
                            
                            # TODO: Send email to customer with setup link
                            # You can create a custom email template in Odoo
                            self._send_payment_setup_email(env, partner, setup_intent)
                        else:
                            _logger.info(f"[DRY RUN] Would create Setup Intent for {partner.email}")
                            self.stats['payment_methods_requested'] += 1
                    
                except Exception as e:
                    _logger.error(f"Error setting up payment for {partner.email}: {str(e)}")
        
        _logger.info(f"\n✓ Created {self.stats['payment_methods_requested']} Setup Intents")
        _logger.info("  Customers will receive emails to add their payment methods")
    
    def _send_payment_setup_email(self, env, partner, setup_intent):
        """
        Send email to customer requesting payment method setup
        """
        # Create payment setup URL
        # You'll need to create a frontend page that handles the Setup Intent
        base_url = env['ir.config_parameter'].sudo().get_param('web.base.url')
        setup_url = f"{base_url}/payment/setup?client_secret={setup_intent.client_secret}"
        
        # Send email using Odoo mail template
        # You'll need to create this template in Odoo
        template = env.ref('subscription_management.mail_template_payment_setup', raise_if_not_found=False)
        
        if template:
            template.with_context(
                setup_url=setup_url,
                customer_name=partner.name,
            ).send_mail(partner.id, force_send=True)
            _logger.info(f"  Sent payment setup email to {partner.email}")
    
    def _create_stripe_customer(self, name, email, phone=None, address=None, metadata=None):
        """Create Stripe customer"""
        customer_data = {
            'name': name,
            'email': email,
            'metadata': metadata or {},
        }
        
        if phone:
            customer_data['phone'] = phone
        if address:
            customer_data['address'] = address
        
        return stripe.Customer.create(**customer_data)
    
    def _get_state_id(self, env, state_name, country_name):
        """Get Odoo state ID from name"""
        if not state_name:
            return False
        
        country_id = self._get_country_id(env, country_name)
        if country_id:
            state = env['res.country.state'].search([
                ('name', '=ilike', state_name),
                ('country_id', '=', country_id)
            ], limit=1)
            return state.id if state else False
        
        return False
    
    def _get_country_id(self, env, country_name):
        """Get Odoo country ID from name"""
        if not country_name:
            return False
        
        country = env['res.country'].search([
            '|',
            ('name', '=ilike', country_name),
            ('code', '=ilike', country_name)
        ], limit=1)
        
        return country.id if country else False
    
    def _map_country_code(self, country_name):
        """Map country name to ISO 2-letter code for Stripe"""
        country_map = {
            'India': 'IN',
            'U.S.A': 'US',
            'USA': 'US',
            'United States': 'US',
            'Switzerland': 'CH',
            'Andorra': 'AD',
            # Add more as needed
        }
        
        return country_map.get(country_name, 'US')  # Default to US
    
    def _parse_date(self, date_str):
        """Parse date string"""
        if not date_str:
            return None
        
        for fmt in ['%Y-%m-%d', '%Y-%m-%d %H:%M:%S', '%m/%d/%Y', '%d/%m/%Y']:
            try:
                return datetime.strptime(date_str, fmt)
            except ValueError:
                continue
        
        return None
    
    def _print_customer_stats(self):
        """Print customer statistics"""
        _logger.info("\n" + "=" * 80)
        _logger.info("CUSTOMER MIGRATION SUMMARY")
        _logger.info("=" * 80)
        _logger.info(f"Total Processed:      {self.stats['customers_processed']}")
        _logger.info(f"Created in Odoo:      {self.stats['customers_created_odoo']}")
        _logger.info(f"Created in Stripe:    {self.stats['customers_created_stripe']}")
        _logger.info(f"Already Existed:      {self.stats['customers_existed']}")
        _logger.info(f"Errors:               {self.stats['customers_errors']}")
        _logger.info("=" * 80 + "\n")
    
    def _print_subscription_stats(self):
        """Print subscription statistics"""
        _logger.info("\n" + "=" * 80)
        _logger.info("SUBSCRIPTION MIGRATION SUMMARY")
        _logger.info("=" * 80)
        _logger.info(f"Total Processed:      {self.stats['subscriptions_processed']}")
        _logger.info(f"Created in Odoo:      {self.stats['subscriptions_created_odoo']}")
        _logger.info(f"Created in Stripe:    {self.stats['subscriptions_created_stripe']}")
        _logger.info(f"Already Existed:      {self.stats['subscriptions_existed']}")
        _logger.info(f"Errors:               {self.stats['subscriptions_errors']}")
        _logger.info("=" * 80 + "\n")
    
    def run_migration(self, contacts_csv, subscriptions_csv, mode='all', setup_payments=True):
        """
        Run complete Zoho migration
        
        Args:
            contacts_csv: Path to Contacts.csv from Zoho
            subscriptions_csv: Path to Subscriptions.csv from Zoho
            mode: 'customers', 'subscriptions', or 'all'
            setup_payments: If True, create Setup Intents for payment methods
        """
        if self.dry_run:
            _logger.info("=" * 80)
            _logger.info("DRY RUN MODE - No actual changes will be made")
            _logger.info("=" * 80)
        
        # Validate Stripe connection
        try:
            account = stripe.Account.retrieve()
            _logger.info(f"✓ Connected to Stripe: {account.business_profile.name}")
        except Exception as e:
            _logger.error(f"✗ Cannot connect to Stripe: {e}")
            return False
        
        # Migrate customers
        if mode in ['customers', 'all']:
            self.migrate_customers_from_zoho(contacts_csv)
        
        # Migrate subscriptions
        if mode in ['subscriptions', 'all']:
            self.migrate_subscriptions_from_zoho(subscriptions_csv)
        
        # Setup payment methods
        if mode in ['customers', 'all'] and setup_payments:
            self.setup_payment_methods()
        
        # Print overall summary
        self._print_overall_summary()
        
        return True
    
    def _print_overall_summary(self):
        """Print overall summary"""
        _logger.info("\n" + "=" * 80)
        _logger.info("ZOHO → ODOO + STRIPE MIGRATION COMPLETE")
        _logger.info("=" * 80)
        _logger.info("\nCUSTOMERS:")
        _logger.info(f"  Processed:        {self.stats['customers_processed']}")
        _logger.info(f"  Created (Odoo):   {self.stats['customers_created_odoo']}")
        _logger.info(f"  Created (Stripe): {self.stats['customers_created_stripe']}")
        _logger.info(f"  Already Existed:  {self.stats['customers_existed']}")
        _logger.info(f"  Errors:           {self.stats['customers_errors']}")
        _logger.info("\nSUBSCRIPTIONS:")
        _logger.info(f"  Processed:        {self.stats['subscriptions_processed']}")
        _logger.info(f"  Created (Odoo):   {self.stats['subscriptions_created_odoo']}")
        _logger.info(f"  Created (Stripe): {self.stats['subscriptions_created_stripe']}")
        _logger.info(f"  Already Existed:  {self.stats['subscriptions_existed']}")
        _logger.info(f"  Errors:           {self.stats['subscriptions_errors']}")
        _logger.info("\nPAYMENT METHODS:")
        _logger.info(f"  Setup Intents:    {self.stats['payment_methods_requested']}")
        _logger.info("=" * 80 + "\n")


def main():
    """Main entry point"""
    parser = argparse.ArgumentParser(description='Migrate from Zoho Billing to Odoo + Stripe')
    parser.add_argument('--db', required=True, help='Odoo database name')
    parser.add_argument('--stripe-key', required=True, help='Stripe secret key')
    parser.add_argument('--mode', choices=['customers', 'subscriptions', 'all'],
                       default='all', help='Migration mode')
    parser.add_argument('--contacts-csv', default='Contacts.csv',
                       help='Path to Zoho Contacts CSV')
    parser.add_argument('--subscriptions-csv', default='Subscriptions.csv',
                       help='Path to Zoho Subscriptions CSV')
    parser.add_argument('--dry-run', action='store_true',
                       help='Dry run without making changes')
    parser.add_argument('--skip-payment-setup', action='store_true',
                       help='Skip payment method setup')
    parser.add_argument('--odoo-config', help='Odoo config file')
    
    args = parser.parse_args()
    
    # Configure logging
    logging.basicConfig(
        level=logging.INFO,
        format='%(asctime)s - %(levelname)s - %(message)s',
        handlers=[
            logging.FileHandler(f'zoho_migration_{datetime.now().strftime("%Y%m%d_%H%M%S")}.log'),
            logging.StreamHandler()
        ]
    )
    
    if args.odoo_config:
        config.parse_config(['-c', args.odoo_config])
    
    # Run migration
    migration = ZohoToOdooStripeMigration(
        db_name=args.db,
        stripe_api_key=args.stripe_key,
        dry_run=args.dry_run
    )
    
    success = migration.run_migration(
        contacts_csv=args.contacts_csv,
        subscriptions_csv=args.subscriptions_csv,
        mode=args.mode,
        setup_payments=not args.skip_payment_setup
    )
    
    sys.exit(0 if success else 1)


if __name__ == '__main__':
    main()
