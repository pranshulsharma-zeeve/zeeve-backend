#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Quick Zoho → Odoo + Stripe Migration
=====================================
Simple script for migrating from Zoho Billing to Odoo with Stripe

Run from Odoo shell:
    exec(open('/path/to/zoho_quick_migrate.py').read())
"""

import stripe
import csv
import json
from datetime import datetime, timedelta

def migrate_from_zoho(env, contacts_csv, subscriptions_csv, dry_run=True, setup_payment_methods=True):
    """
    Complete Zoho to Odoo + Stripe migration
    
    Args:
        env: Odoo environment
        contacts_csv: Path to Contacts.csv from Zoho
        subscriptions_csv: Path to Subscriptions.csv from Zoho
        dry_run: If True, simulate without making changes
        setup_payment_methods: If True, create Setup Intents for cards
    """
    
    # Initialize Stripe
    stripe_key = env['ir.config_parameter'].sudo().get_param('stripe_secret_key')
    if not stripe_key:
        print("ERROR: Stripe secret key not configured!")
        return
    
    stripe.api_key = stripe_key
    
    # Test connection
    try:
        account = stripe.Account.retrieve()
        print(f"✓ Connected to Stripe: {account.business_profile.name}")
    except Exception as e:
        print(f"✗ Cannot connect to Stripe: {e}")
        return
    
    if dry_run:
        print("\n" + "=" * 80)
        print("DRY RUN MODE - No changes will be made")
        print("=" * 80 + "\n")
    
    stats = {
        'customers_created_odoo': 0,
        'customers_created_stripe': 0,
        'customers_existed': 0,
        'customers_errors': 0,
        'subscriptions_created_odoo': 0,
        'subscriptions_created_stripe': 0,
        'subscriptions_errors': 0,
        'payment_setups_created': 0,
    }
    
    customer_mapping = {}  # email -> (partner_id, stripe_customer_id)
    
    # ============================================================================
    # STEP 1: MIGRATE CUSTOMERS FROM ZOHO
    # ============================================================================
    print("\n" + "=" * 80)
    print("STEP 1: MIGRATING CUSTOMERS FROM ZOHO")
    print("=" * 80 + "\n")
    
    with open(contacts_csv, 'r', encoding='utf-8') as f:
        reader = csv.DictReader(f)
        
        for row in reader:
            try:
                email = row.get('EmailID', '').strip()
                if not email:
                    continue
                
                # Check if customer exists in Odoo
                partner = env['res.partner'].search([('email', '=', email)], limit=1)
                
                if partner:
                    print(f"⚠ Customer exists: {email}")
                    stats['customers_existed'] += 1
                    
                    # Ensure has Stripe customer
                    if not partner.stripe_customer_id and not dry_run:
                        stripe_customer = stripe.Customer.create(
                            name=partner.name,
                            email=email,
                            phone=partner.phone or row.get('Phone', ''),
                            metadata={
                                'odoo_partner_id': str(partner.id),
                                'zoho_customer_id': row.get('Customer ID', ''),
                                'source': 'zoho_migration'
                            }
                        )
                        partner.stripe_customer_id = stripe_customer.id
                        stats['customers_created_stripe'] += 1
                        print(f"  ✓ Added Stripe ID: {stripe_customer.id}")
                    
                    customer_mapping[email] = (partner.id, partner.stripe_customer_id)
                else:
                    # Create new customer in Odoo
                    if not dry_run:
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
                            country = env['res.country'].search([
                                '|', ('name', '=ilike', country_name), ('code', '=ilike', country_name)
                            ], limit=1)
                            if country:
                                partner_vals['country_id'] = country.id
                        
                        partner = env['res.partner'].create(partner_vals)
                        print(f"✓ Created in Odoo: {email} (ID: {partner.id})")
                        stats['customers_created_odoo'] += 1
                        
                        # Create in Stripe
                        stripe_customer = stripe.Customer.create(
                            name=partner.name,
                            email=email,
                            phone=partner.phone or partner.mobile,
                            address={
                                'line1': partner.street or '',
                                'city': partner.city or '',
                                'postal_code': partner.zip or '',
                                'country': _map_country_code(country_name) if country_name else 'US',
                            },
                            metadata={
                                'odoo_partner_id': str(partner.id),
                                'zoho_customer_id': row.get('Customer ID', ''),
                                'source': 'zoho_migration'
                            }
                        )
                        
                        partner.stripe_customer_id = stripe_customer.id
                        print(f"✓ Created in Stripe: {stripe_customer.id}")
                        stats['customers_created_stripe'] += 1
                        
                        customer_mapping[email] = (partner.id, stripe_customer.id)
                    else:
                        print(f"[DRY RUN] Would create: {email}")
                        stats['customers_created_odoo'] += 1
                        stats['customers_created_stripe'] += 1
                
                # Commit every 10 records
                if (stats['customers_created_odoo'] + stats['customers_existed']) % 10 == 0:
                    env.cr.commit()
                    
            except Exception as e:
                print(f"✗ Error: {email} - {str(e)}")
                stats['customers_errors'] += 1
    
    env.cr.commit()
    print(f"\n✓ Customer migration complete: {stats['customers_created_odoo']} created, {stats['customers_existed']} existed")
    
    # ============================================================================
    # STEP 2: MIGRATE SUBSCRIPTIONS FROM ZOHO
    # ============================================================================
    print("\n" + "=" * 80)
    print("STEP 2: MIGRATING SUBSCRIPTIONS FROM ZOHO")
    print("=" * 80 + "\n")
    
    # Parse Zoho subscriptions (group by Subscription ID)
    subscriptions_data = {}
    
    with open(subscriptions_csv, 'r', encoding='utf-8') as f:
        reader = csv.DictReader(f)
        
        for row in reader:
            sub_id = row.get('Subscription ID', '').strip()
            if not sub_id or sub_id in subscriptions_data:
                continue
            
            subscriptions_data[sub_id] = {
                'subscription_number': row.get('Subscription#', '').strip(),
                'customer_email': row.get('Customer Email', '').strip(),
                'customer_name': row.get('Customer Name', '').strip(),
                'start_date': row.get('Start Date', '').strip(),
                'next_billing': row.get('Next Billing Date', '').strip(),
                'status': row.get('Subscription Status', '').strip(),
                'frequency': row.get('Subscription Frequency', '').strip(),
                'total': float(row.get('Total', '0').strip() or '0'),
                'card_id': row.get('Card ID', '').strip(),
                'product_name': row.get('Product Name', '').strip(),
                'item_name': row.get('Item Name', '').strip(),
            }
    
    print(f"Found {len(subscriptions_data)} unique subscriptions\n")
    
    for sub_id, sub_data in subscriptions_data.items():
        try:
            email = sub_data['customer_email']
            if not email:
                continue
            
            # Get customer from Odoo
            partner = env['res.partner'].search([('email', '=', email)], limit=1)
            
            if not partner:
                print(f"⚠ Customer not found: {email} (run customer migration first)")
                stats['subscriptions_errors'] += 1
                continue
            
            # Check if subscription exists
            existing = env['subscription.subscription'].search([
                '|',
                ('name', '=', sub_data['subscription_number']),
                ('name', '=', sub_id)
            ], limit=1)
            
            if existing:
                print(f"⚠ Subscription exists: {sub_data['subscription_number']}")
                continue
            
            # Find or create plan
            # For simplicity, using a default plan - adjust based on your needs
            plan = env['subscription.plan'].search([('active', '=', True)], limit=1)
            
            if not plan:
                print(f"✗ No subscription plan found. Create plans first!")
                stats['subscriptions_errors'] += 1
                continue
            
            if not dry_run:
                # Create subscription in Odoo
                frequency = sub_data['frequency'].lower()
                payment_freq = 'monthly'
                duration = 1
                unit = 'month'
                
                if 'month' in frequency:
                    payment_freq, duration, unit = 'monthly', 1, 'month'
                elif 'quarter' in frequency:
                    payment_freq, duration, unit = 'quarterly', 3, 'month'
                elif 'year' in frequency:
                    payment_freq, duration, unit = 'annually', 1, 'year'
                
                # Map Zoho status to Odoo state
                state = 'draft'
                if sub_data['status'] in ['live', 'active']:
                    state = 'in_progress'
                elif sub_data['status'] == 'cancelled':
                    state = 'cancel'
                
                subscription = env['subscription.subscription'].create({
                    'name': sub_data['subscription_number'] or f"ZOHO-{sub_id}",
                    'customer_name': partner.id,
                    'sub_plan_id': plan.id,
                    'product_id': plan.product_id.id if plan.product_id else False,
                    'price': sub_data['total'],
                    'quantity': 1,
                    'start_date': _parse_date(sub_data['start_date']) or datetime.now().date(),
                    'duration': duration,
                    'unit': unit,
                    'payment_frequency': payment_freq,
                    'state': state,
                    'source': 'zoho_migration',
                    'autopay_enabled': True,  # Enable auto-deduction
                    'never_expires': True,
                    'num_billing_cycle': -1,
                })
                
                subscription.create_primary_node({
                    'node_type': subscription.subscription_type or plan.subscription_type,
                })
                
                print(f"✓ Created Odoo subscription: {subscription.id}")
                stats['subscriptions_created_odoo'] += 1
                
                # Create in Stripe
                if partner.stripe_customer_id and plan.stripe_price_month_id:
                    # Select price based on frequency
                    if payment_freq == 'monthly':
                        price_id = plan.stripe_price_month_id
                    elif payment_freq == 'quarterly':
                        price_id = plan.stripe_price_quarter_id
                    elif payment_freq == 'annually':
                        price_id = plan.stripe_price_year_id
                    else:
                        price_id = plan.stripe_price_month_id
                    
                    if price_id:
                        stripe_sub = stripe.Subscription.create(
                            customer=partner.stripe_customer_id,
                            items=[{'price': price_id, 'quantity': 1}],
                            metadata={
                                'odoo_subscription_id': str(subscription.id),
                                'zoho_subscription_id': sub_id,
                                'source': 'zoho_migration'
                            },
                            collection_method='charge_automatically',  # Auto-deduction
                        )
                        
                        subscription.write({
                            'stripe_subscription_id': stripe_sub.id,
                            'stripe_customer_id': partner.stripe_customer_id,
                            'stripe_status': stripe_sub.status,
                        })
                        
                        print(f"✓ Created Stripe subscription: {stripe_sub.id}")
                        stats['subscriptions_created_stripe'] += 1
                else:
                    print(f"⚠ Cannot create Stripe subscription: Missing customer or price ID")
            else:
                print(f"[DRY RUN] Would create subscription: {sub_data['subscription_number']}")
                stats['subscriptions_created_odoo'] += 1
                stats['subscriptions_created_stripe'] += 1
            
            # Commit every 5 subscriptions
            if stats['subscriptions_created_odoo'] % 5 == 0:
                env.cr.commit()
                
        except Exception as e:
            print(f"✗ Error: {sub_id} - {str(e)}")
            stats['subscriptions_errors'] += 1
    
    env.cr.commit()
    print(f"\n✓ Subscription migration complete: {stats['subscriptions_created_odoo']} created")
    
    # ============================================================================
    # STEP 3: SETUP PAYMENT METHODS
    # ============================================================================
    if setup_payment_methods and not dry_run:
        print("\n" + "=" * 80)
        print("STEP 3: SETTING UP PAYMENT METHOD COLLECTION")
        print("=" * 80 + "\n")
        print("⚠ IMPORTANT: Due to PCI compliance, credit cards cannot be directly migrated.")
        print("   Customers will receive emails to securely add their payment methods.\n")
        
        # Get customers with Stripe ID but no payment method
        partners = env['res.partner'].search([
            ('stripe_customer_id', '!=', False),
            ('email', '!=', False)
        ])
        
        for partner in partners:
            try:
                # Check if has payment method
                payment_methods = stripe.PaymentMethod.list(
                    customer=partner.stripe_customer_id,
                    type='card',
                    limit=1
                )
                
                if not payment_methods.data:
                    # Create Setup Intent
                    setup_intent = stripe.SetupIntent.create(
                        customer=partner.stripe_customer_id,
                        metadata={
                            'odoo_partner_id': str(partner.id),
                            'purpose': 'zoho_migration',
                        },
                        usage='off_session',  # For recurring billing
                        payment_method_types=['card'],
                    )
                    
                    # Store setup intent secret (to send to customer)
                    # You can email this or create a link
                    print(f"✓ Setup Intent: {partner.email} → {setup_intent.id}")
                    stats['payment_setups_created'] += 1
                    
                    # TODO: Send email with payment setup link
                    # _send_payment_setup_email(env, partner, setup_intent)
                    
            except Exception as e:
                print(f"✗ Error setting up payment for {partner.email}: {str(e)}")
        
        print(f"\n✓ Created {stats['payment_setups_created']} Setup Intents")
    
    # Print summary
    print("\n" + "=" * 80)
    print("MIGRATION SUMMARY")
    print("=" * 80)
    print(f"\nCUSTOMERS:")
    print(f"  Created in Odoo:   {stats['customers_created_odoo']}")
    print(f"  Created in Stripe: {stats['customers_created_stripe']}")
    print(f"  Already Existed:   {stats['customers_existed']}")
    print(f"  Errors:            {stats['customers_errors']}")
    print(f"\nSUBSCRIPTIONS:")
    print(f"  Created in Odoo:   {stats['subscriptions_created_odoo']}")
    print(f"  Created in Stripe: {stats['subscriptions_created_stripe']}")
    print(f"  Errors:            {stats['subscriptions_errors']}")
    print(f"\nPAYMENT METHODS:")
    print(f"  Setup Intents:     {stats['payment_setups_created']}")
    print("=" * 80 + "\n")
    
    if stats['payment_setups_created'] > 0:
        print("⚠ NEXT STEPS:")
        print("   1. Send payment setup emails to customers")
        print("   2. Customers add their cards via secure Stripe link")
        print("   3. Auto-deduction will work once cards are added")
        print("")


def _parse_date(date_str):
    """Parse date string"""
    if not date_str:
        return None
    
    for fmt in ['%Y-%m-%d', '%Y-%m-%d %H:%M:%S', '%m/%d/%Y', '%d/%m/%Y']:
        try:
            return datetime.strptime(date_str, fmt).date()
        except ValueError:
            continue
    return None


def _map_country_code(country_name):
    """Map country name to ISO code"""
    mapping = {
        'India': 'IN', 'U.S.A': 'US', 'USA': 'US', 'United States': 'US',
        'Switzerland': 'CH', 'Andorra': 'AD', 'UK': 'GB', 'United Kingdom': 'GB',
    }
    return mapping.get(country_name, 'US')


# Quick usage instructions
print("""
Zoho to Odoo + Stripe Migration - Quick Start
==============================================

From Odoo Shell, run:

# 1. Sync existing plans to Stripe first
plans = env['subscription.plan'].search([('active', '=', True)])
for plan in plans:
    if not plan.stripe_product_id:
        plan.action_sync_with_stripe()
env.cr.commit()

# 2. Migrate from Zoho (dry run first)
migrate_from_zoho(
    env,
    contacts_csv='/path/to/Contacts.csv',
    subscriptions_csv='/path/to/Subscriptions.csv',
    dry_run=True
)

# 3. Run for real
migrate_from_zoho(
    env,
    contacts_csv='/path/to/Contacts.csv',
    subscriptions_csv='/path/to/Subscriptions.csv',
    dry_run=False,
    setup_payment_methods=True
)

# 4. Verify
verify_zoho_migration(env)

""")
