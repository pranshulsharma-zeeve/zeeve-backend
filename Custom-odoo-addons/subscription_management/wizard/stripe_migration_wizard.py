# -*- coding: utf-8 -*-
"""
Stripe Migration Wizard
"""

import stripe
import logging
import os
from datetime import datetime
from odoo import models, fields, api, _
from odoo.exceptions import UserError

_logger = logging.getLogger(__name__)


class StripeDataMigration(models.TransientModel):
    """Wizard for migrating data from Zoho to Odoo + Stripe"""
    
    _name = 'stripe.data.migration'
    _description = 'Zoho to Odoo + Stripe Migration Wizard'
    
    migration_source = fields.Selection([
        ('zoho', 'Zoho Billing (Import from CSV)'),
        ('existing', 'Existing Odoo Data (Sync to Stripe)'),
    ], string='Migration Source', default='zoho', required=True)
    
    migration_type = fields.Selection([
        ('customers', 'Migrate Customers Only'),
        ('subscriptions', 'Migrate Subscriptions Only'),
        ('all', 'Migrate All (Customers + Subscriptions)'),
    ], string='Migration Type', default='all', required=True)
    
    dry_run = fields.Boolean(
        string='Dry Run', 
        default=True,
        help='If checked, no actual changes will be made to Stripe'
    )
    
    customer_filter = fields.Selection([
        ('all', 'All Customers'),
        ('without_stripe_id', 'Only customers without Stripe ID'),
        ('active_only', 'Active customers only'),
    ], string='Customer Filter', default='without_stripe_id')
    
    subscription_filter = fields.Selection([
        ('all', 'All Subscriptions'),
        ('active_only', 'Active subscriptions only'),
        ('without_stripe_id', 'Only subscriptions without Stripe ID'),
    ], string='Subscription Filter', default='without_stripe_id')
    
    # Zoho CSV file paths
    contacts_csv_path = fields.Char(
        string='Contacts CSV Path',
        help='Full path to Zoho Contacts.csv file',
        default='/home/shashank/odoo_project/odoo-stripe/Custom-odoo-addons/Contacts.csv'
    )
    subscriptions_csv_path = fields.Char(
        string='Subscriptions CSV Path',
        help='Full path to Zoho Subscriptions.csv file',
        default='/home/shashank/odoo_project/odoo-stripe/Custom-odoo-addons/Subscriptions.csv'
    )
    
    setup_payment_methods = fields.Boolean(
        string='Setup Payment Methods',
        default=True,
        help='Create Setup Intents for customers to add their payment methods'
    )
    
    create_missing_users = fields.Boolean(
        string='Create Missing Odoo Users',
        default=True,
        help='Create Odoo users for customers that don\'t exist yet (from Zoho CSV)'
    )
    
    # Statistics
    customers_processed = fields.Integer(readonly=True)
    customers_created = fields.Integer(readonly=True)
    customers_skipped = fields.Integer(readonly=True)
    customers_errors = fields.Integer(readonly=True)
    
    subscriptions_processed = fields.Integer(readonly=True)
    subscriptions_created = fields.Integer(readonly=True)
    subscriptions_skipped = fields.Integer(readonly=True)
    subscriptions_errors = fields.Integer(readonly=True)
    
    payment_setups_created = fields.Integer('Payment Setups Created', readonly=True)
    users_created_odoo = fields.Integer('Users Created in Odoo', readonly=True)
    
    log_text = fields.Text('Migration Log', readonly=True)
    state = fields.Selection([
        ('draft', 'Draft'),
        ('in_progress', 'In Progress'),
        ('done', 'Done'),
    ], default='draft', readonly=True)
    
    def _log(self, message):
        """Add message to log"""
        current_log = self.log_text or ''
        timestamp = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
        self.log_text = f"{current_log}\n[{timestamp}] {message}"
        _logger.info(message)
    
    def _init_stripe(self):
        """Initialize Stripe with API key"""
        stripe_key = self.env['ir.config_parameter'].sudo().get_param('stripe_secret_key')
        if not stripe_key:
            raise UserError(_('Stripe secret key is not configured. Please configure it in Settings.'))
        stripe.api_key = stripe_key
        
        # Test connection
        try:
            stripe.Account.retrieve()
            self._log("✓ Stripe connection validated successfully")
            return True
        except Exception as e:
            raise UserError(_(f'Cannot connect to Stripe: {str(e)}'))
    
    def action_start_migration(self):
        """Start the migration process"""
        self.ensure_one()
        
        # Update state
        self.write({'state': 'in_progress', 'log_text': ''})
        
        # Initialize Stripe
        self._init_stripe()
        
        if self.dry_run:
            self._log("=" * 80)
            self._log("DRY RUN MODE - No actual changes will be made to Stripe")
            self._log("=" * 80)
        
        try:
            # Migrate customers
            if self.migration_type in ['customers', 'all']:
                if self.migration_source == 'zoho':
                    self._import_from_zoho_contacts()
                else:
                    self._migrate_customers()
            
            # Migrate subscriptions
            if self.migration_type in ['subscriptions', 'all']:
                if self.migration_source == 'zoho':
                    self._import_from_zoho_subscriptions()
                else:
                    self._migrate_subscriptions()
            
            # Setup payment methods if requested
            if self.setup_payment_methods and self.migration_source == 'zoho':
                self._create_payment_setup_intents()
            
            # Update state
            self.write({'state': 'done'})
            self._print_summary()
            
            return {
                'type': 'ir.actions.client',
                'tag': 'display_notification',
                'params': {
                    'title': _('Migration Complete'),
                    'message': _(f'Migrated {self.customers_created} customers and {self.subscriptions_created} subscriptions'),
                    'type': 'success',
                    'sticky': False,
                }
            }
            
        except Exception as e:
            self._log(f"✗ Migration failed: {str(e)}")
            self.write({'state': 'draft'})
            raise UserError(_(f'Migration failed: {str(e)}'))
    
    def _migrate_customers(self):
        """Migrate customers to Stripe"""
        self._log("=" * 80)
        self._log("STARTING CUSTOMER MIGRATION")
        self._log("=" * 80)
        
        # Build domain
        domain = []
        if self.customer_filter == 'without_stripe_id':
            domain.append(('stripe_customer_id', '=', False))
        elif self.customer_filter == 'active_only':
            domain.append(('active', '=', True))
        
        partners = self.env['res.partner'].search(domain)
        
        for partner in partners:
            self.customers_processed += 1
            
            try:
                if not partner.email:
                    self._log(f"⚠ Skipping {partner.name}: No email")
                    self.customers_skipped += 1
                    continue
                
                if partner.stripe_customer_id:
                    self.customers_skipped += 1
                    continue
                
                if not self.dry_run:
                    stripe_customer = stripe.Customer.create(
                        name=partner.name or partner.email,
                        email=partner.email,
                        phone=partner.phone or partner.mobile,
                        metadata={
                            'odoo_partner_id': str(partner.id),
                            'source': 'migration'
                        }
                    )
                    
                    partner.sudo().write({'stripe_customer_id': stripe_customer.id})
                    self._log(f"✓ {partner.email} → {stripe_customer.id}")
                    self.customers_created += 1
                else:
                    self._log(f"[DRY RUN] {partner.email}")
                    self.customers_created += 1
                
                if self.customers_processed % 10 == 0:
                    self.env.cr.commit()
                    
            except Exception as e:
                self._log(f"✗ Error: {partner.email} - {str(e)}")
                self.customers_errors += 1
        
        self.env.cr.commit()
    
    def _import_from_zoho_contacts(self):
        """Import customers from Zoho Contacts.csv"""
        self._log("=" * 80)
        self._log("IMPORTING CUSTOMERS FROM ZOHO")
        self._log("=" * 80)
        
        if not self.contacts_csv_path or not os.path.exists(self.contacts_csv_path):
            raise UserError(_('Contacts CSV file not found. Please check the path.'))
        
        import csv
        
        with open(self.contacts_csv_path, 'r', encoding='utf-8') as f:
            reader = csv.DictReader(f)
            
            for row in reader:
                self.customers_processed += 1
                
                try:
                    email = row.get('EmailID', '').strip()
                    if not email:
                        self.customers_skipped += 1
                        continue
                    
                    # Check if partner exists
                    partner = self.env['res.partner'].search([('email', '=', email)], limit=1)
                    
                    if partner:
                        self._log(f"⚠ Customer exists: {email}")
                        self.customers_skipped += 1
                        
                        # Add Stripe customer if missing
                        if not partner.stripe_customer_id and not self.dry_run:
                            stripe_cust = stripe.Customer.create(
                                name=partner.name,
                                email=email,
                                phone=partner.phone,
                                metadata={
                                    'odoo_partner_id': str(partner.id),
                                    'zoho_customer_id': row.get('Customer ID', ''),
                                    'source': 'zoho_migration'
                                }
                            )
                            partner.write({'stripe_customer_id': stripe_cust.id})
                            self._log(f"  ✓ Added Stripe ID: {stripe_cust.id}")
                            self.customers_created += 1
                    else:
                        # Create new partner from Zoho data
                        if not self.dry_run and self.create_missing_users:
                            partner_vals = {
                                'name': row.get('Customer Name', row.get('Display Name', email)),
                                'email': email,
                                'phone': row.get('Phone', ''),
                                'mobile': row.get('MobilePhone', ''),
                                'street': row.get('Billing Address', ''),
                                'city': row.get('Billing City', ''),
                                'zip': row.get('Billing Code', ''),
                                'customer_rank': 1,
                                'is_company': row.get('Contact Type', '') == 'business',
                            }
                            
                            # Set country
                            country_name = row.get('Billing Country', '').strip()
                            if country_name:
                                country = self.env['res.country'].search([
                                    '|', ('name', '=ilike', country_name),
                                    ('code', '=ilike', country_name)
                                ], limit=1)
                                if country:
                                    partner_vals['country_id'] = country.id
                            
                            partner = self.env['res.partner'].create(partner_vals)
                            self._log(f"✓ Created Odoo partner: {email} (ID: {partner.id})")
                            self.users_created_odoo += 1
                            
                            # Create Stripe customer
                            stripe_cust = stripe.Customer.create(
                                name=partner.name,
                                email=email,
                                phone=partner.phone or partner.mobile,
                                metadata={
                                    'odoo_partner_id': str(partner.id),
                                    'zoho_customer_id': row.get('Customer ID', ''),
                                    'source': 'zoho_migration'
                                }
                            )
                            partner.write({'stripe_customer_id': stripe_cust.id})
                            self._log(f"✓ Created Stripe customer: {stripe_cust.id}")
                            self.customers_created += 1
                        else:
                            self._log(f"[DRY RUN] Would create: {email}")
                            self.users_created_odoo += 1
                            self.customers_created += 1
                    
                    if self.customers_processed % 10 == 0:
                        self.env.cr.commit()
                        
                except Exception as e:
                    self._log(f"✗ Error: {str(e)}")
                    self.customers_errors += 1
        
        self.env.cr.commit()
        self._log(f"✓ Customers: {self.customers_created} created, {self.users_created_odoo} new in Odoo")
    
    def _import_from_zoho_subscriptions(self):
        """Import subscriptions from Zoho Subscriptions.csv"""
        self._log("=" * 80)
        self._log("IMPORTING SUBSCRIPTIONS FROM ZOHO")
        self._log("=" * 80)
        
        if not self.subscriptions_csv_path or not os.path.exists(self.subscriptions_csv_path):
            raise UserError(_('Subscriptions CSV file not found. Please check the path.'))
        
        import csv
        
        # Parse and group subscriptions
        subs_data = {}
        with open(self.subscriptions_csv_path, 'r', encoding='utf-8') as f:
            reader = csv.DictReader(f)
            for row in reader:
                sub_id = row.get('Subscription ID', '').strip()
                if sub_id and sub_id not in subs_data:
                    subs_data[sub_id] = row
        
        self._log(f"Found {len(subs_data)} unique subscriptions")
        
        for sub_id, row in subs_data.items():
            self.subscriptions_processed += 1
            
            try:
                email = row.get('Customer Email', '').strip()
                if not email:
                    self.subscriptions_skipped += 1
                    continue
                
                # Get partner
                partner = self.env['res.partner'].search([('email', '=', email)], limit=1)
                if not partner:
                    self._log(f"⚠ Customer not found: {email}")
                    self.subscriptions_errors += 1
                    continue
                
                # Check if exists
                existing = self.env['subscription.subscription'].search([
                    '|', ('name', '=', row.get('Subscription#', '')),
                    ('name', '=', sub_id)
                ], limit=1)
                
                if existing:
                    self.subscriptions_skipped += 1
                    continue
                
                # Get plan (simplified - you may need better matching logic)
                plan = self.env['subscription.plan'].search([('active', '=', True)], limit=1)
                if not plan:
                    self._log("✗ No active plan found")
                    self.subscriptions_errors += 1
                    continue
                
                if not self.dry_run:
                    # Determine frequency
                    freq = row.get('Subscription Frequency', 'months').lower()
                    if 'month' in freq:
                        payment_freq, duration, unit = 'monthly', 1, 'month'
                        price_id = plan.stripe_price_month_id
                    elif 'quarter' in freq:
                        payment_freq, duration, unit = 'quarterly', 3, 'month'
                        price_id = plan.stripe_price_quarter_id
                    elif 'year' in freq:
                        payment_freq, duration, unit = 'annually', 1, 'year'
                        price_id = plan.stripe_price_year_id
                    else:
                        payment_freq, duration, unit = 'monthly', 1, 'month'
                        price_id = plan.stripe_price_month_id
                    
                    # Create Odoo subscription
                    subscription = self.env['subscription.subscription'].create({
                        'name': row.get('Subscription#', f'ZOHO-{sub_id}'),
                        'customer_name': partner.id,
                        'sub_plan_id': plan.id,
                        'product_id': plan.product_id.id if plan.product_id else False,
                        'price': float(row.get('Total', '0') or '0'),
                        'quantity': 1,
                        'start_date': fields.Date.today(),
                        'duration': duration,
                        'unit': unit,
                        'payment_frequency': payment_freq,
                        'state': 'in_progress',
                        'source': 'zoho_migration',
                        'autopay_enabled': True,
                        'never_expires': True,
                        'num_billing_cycle': -1,
                    })
                    subscription.create_primary_node({
                        'node_type': subscription.subscription_type or plan.subscription_type,
                    })
                    
                    self._log(f"✓ Created Odoo subscription: {subscription.id}")
                    self.subscriptions_created += 1
                    
                    # Create Stripe subscription
                    if partner.stripe_customer_id and price_id:
                        stripe_sub = stripe.Subscription.create(
                            customer=partner.stripe_customer_id,
                            items=[{'price': price_id}],
                            metadata={
                                'odoo_subscription_id': str(subscription.id),
                                'zoho_subscription_id': sub_id,
                            },
                            collection_method='charge_automatically',
                        )
                        
                        subscription.write({
                            'stripe_subscription_id': stripe_sub.id,
                            'stripe_customer_id': partner.stripe_customer_id,
                            'stripe_status': stripe_sub.status,
                        })
                        
                        self._log(f"✓ Created Stripe subscription: {stripe_sub.id}")
                else:
                    self._log(f"[DRY RUN] Would create subscription: {row.get('Subscription#')}")
                    self.subscriptions_created += 1
                
                if self.subscriptions_processed % 5 == 0:
                    self.env.cr.commit()
                    
            except Exception as e:
                self._log(f"✗ Error: {str(e)}")
                self.subscriptions_errors += 1
        
        self.env.cr.commit()
    
    def _create_payment_setup_intents(self):
        """Create Setup Intents for customers to add payment methods"""
        self._log("=" * 80)
        self._log("CREATING PAYMENT METHOD SETUP INTENTS")
        self._log("=" * 80)
        self._log("⚠ Note: Customers will need to securely add their cards via Stripe")
        
        partners = self.env['res.partner'].search([
            ('stripe_customer_id', '!=', False),
            ('email', '!=', False)
        ])
        
        for partner in partners:
            try:
                if not self.dry_run:
                    # Check if already has payment method
                    payment_methods = stripe.PaymentMethod.list(
                        customer=partner.stripe_customer_id,
                        type='card',
                        limit=1
                    )
                    
                    if not payment_methods.data:
                        # Create Setup Intent
                        setup_intent = stripe.SetupIntent.create(
                            customer=partner.stripe_customer_id,
                            usage='off_session',
                            metadata={
                                'odoo_partner_id': str(partner.id),
                                'purpose': 'zoho_migration'
                            }
                        )
                        
                        self._log(f"✓ Setup Intent created for: {partner.email}")
                        self.payment_setups_created += 1
                        
                        # TODO: Send email with setup link
                        # You can create a mail template for this
                else:
                    self._log(f"[DRY RUN] Would create Setup Intent for: {partner.email}")
                    self.payment_setups_created += 1
                    
            except Exception as e:
                self._log(f"✗ Error for {partner.email}: {str(e)}")
        
        self._log(f"\n✓ Created {self.payment_setups_created} Setup Intents")
        self._log("  Next: Send emails to customers with payment setup links")
    
    def _migrate_subscriptions(self):
        """Migrate subscriptions to Stripe"""
        self._log("=" * 80)
        self._log("STARTING SUBSCRIPTION MIGRATION")
        self._log("=" * 80)
        
        domain = []
        if self.subscription_filter == 'without_stripe_id':
            domain.append(('stripe_subscription_id', '=', False))
        elif self.subscription_filter == 'active_only':
            domain.append(('state', 'in', ['in_progress', 'draft']))
        
        subscriptions = self.env['subscription.subscription'].search(domain)
        
        for subscription in subscriptions:
            self.subscriptions_processed += 1
            
            try:
                partner = subscription.customer_name
                plan = subscription.sub_plan_id
                
                if not partner or not partner.stripe_customer_id:
                    self.subscriptions_skipped += 1
                    continue
                
                if not plan:
                    self.subscriptions_skipped += 1
                    continue
                
                # Get price ID
                price_id = self._get_price_for_subscription(subscription, plan)
                if not price_id:
                    self.subscriptions_skipped += 1
                    continue
                
                if not self.dry_run:
                    stripe_sub = stripe.Subscription.create(
                        customer=partner.stripe_customer_id,
                        items=[{'price': price_id, 'quantity': int(subscription.quantity or 1)}],
                        metadata={'odoo_subscription_id': str(subscription.id)}
                    )
                    
                    subscription.sudo().write({
                        'stripe_subscription_id': stripe_sub.id,
                        'stripe_customer_id': partner.stripe_customer_id,
                        'stripe_status': stripe_sub.status
                    })
                    
                    self._log(f"✓ Subscription {subscription.id} → {stripe_sub.id}")
                    self.subscriptions_created += 1
                else:
                    self._log(f"[DRY RUN] Subscription {subscription.id}")
                    self.subscriptions_created += 1
                
                if self.subscriptions_processed % 5 == 0:
                    self.env.cr.commit()
                    
            except Exception as e:
                self._log(f"✗ Error: {subscription.id} - {str(e)}")
                self.subscriptions_errors += 1
        
        self.env.cr.commit()
    
    def _get_price_for_subscription(self, subscription, plan):
        """Get appropriate Stripe price ID"""
        freq = subscription.payment_frequency or 'monthly'
        
        if 'month' in freq.lower():
            return plan.stripe_price_month_id
        elif 'quarter' in freq.lower():
            return plan.stripe_price_quarter_id
        elif 'year' in freq.lower() or 'annual' in freq.lower():
            return plan.stripe_price_year_id
        
        return plan.stripe_price_month_id
    
    def _print_summary(self):
        """Print summary"""
        self._log("\n" + "=" * 80)
        self._log("MIGRATION COMPLETE")
        self._log("=" * 80)
        if self.migration_type in ['customers', 'all']:
            self._log(f"Customers: {self.customers_created} created, {self.customers_errors} errors")
        if self.migration_type in ['subscriptions', 'all']:
            self._log(f"Subscriptions: {self.subscriptions_created} created, {self.subscriptions_errors} errors")
        self._log("=" * 80)
    
    def action_validate_migration(self):
        """Validate before migration"""
        self.ensure_one()
        
        issues = []
        
        # Check Stripe key
        if not self.env['ir.config_parameter'].sudo().get_param('stripe_secret_key'):
            issues.append("Stripe secret key not configured")
        
        # Check plans
        plans_without_stripe = self.env['subscription.plan'].search([
            ('active', '=', True),
            ('stripe_product_id', '=', False)
        ])
        
        if plans_without_stripe:
            issues.append(f"{len(plans_without_stripe)} plans need Stripe sync")
        
        if issues:
            raise UserError(_('\n'.join(['Validation failed:'] + issues)))
        
        return {
            'type': 'ir.actions.client',
            'tag': 'display_notification',
            'params': {
                'title': _('Validation Passed'),
                'message': _('Ready to migrate'),
                'type': 'success',
            }
        }
    
    def action_sync_plans_with_stripe(self):
        """Sync all plans to Stripe"""
        self.ensure_one()
        
        plans = self.env['subscription.plan'].search([('active', '=', True)])
        
        for plan in plans:
            try:
                plan.action_sync_with_stripe()
                self._log(f"✓ Synced plan: {plan.name}")
            except Exception as e:
                self._log(f"✗ Error: {plan.name} - {str(e)}")
        
        self.env.cr.commit()
        
        return {
            'type': 'ir.actions.client',
            'tag': 'display_notification',
            'params': {
                'title': _('Plans Synced'),
                'message': _(f'{len(plans)} plans synced'),
                'type': 'success',
            }
        }
