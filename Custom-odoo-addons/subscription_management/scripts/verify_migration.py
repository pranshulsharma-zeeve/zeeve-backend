#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Stripe Migration Verification Script
=====================================
Run this after migration to verify data integrity

Usage (from Odoo shell):
    exec(open('/path/to/verify_migration.py').read())
"""

import stripe
from datetime import datetime


def verify_stripe_migration(env):
    """
    Comprehensive verification of Stripe migration
    
    Args:
        env: Odoo environment
    """
    
    print("\n" + "=" * 80)
    print("STRIPE MIGRATION VERIFICATION")
    print("=" * 80 + "\n")
    
    # Initialize Stripe
    stripe_key = env['ir.config_parameter'].sudo().get_param('stripe_secret_key')
    if not stripe_key:
        print("✗ ERROR: Stripe secret key not configured!")
        return False
    
    stripe.api_key = stripe_key
    
    # Test Stripe connection
    try:
        account = stripe.Account.retrieve()
        print(f"✓ Connected to Stripe account: {account.business_profile.name}")
    except Exception as e:
        print(f"✗ Cannot connect to Stripe: {e}")
        return False
    
    print("\n" + "-" * 80)
    print("CUSTOMER VERIFICATION")
    print("-" * 80)
    
    # Check customers
    total_partners = env['res.partner'].search_count([])
    partners_with_email = env['res.partner'].search_count([('email', '!=', False)])
    partners_with_stripe = env['res.partner'].search_count([('stripe_customer_id', '!=', False)])
    
    print(f"Total partners in Odoo:           {total_partners}")
    print(f"Partners with email:              {partners_with_email}")
    print(f"Partners with Stripe ID:          {partners_with_stripe}")
    print(f"Migration coverage:               {(partners_with_stripe/partners_with_email*100):.1f}%")
    
    # Check for orphaned partners (email but no Stripe ID)
    orphaned = env['res.partner'].search([
        ('email', '!=', False),
        ('stripe_customer_id', '=', False),
        ('active', '=', True)
    ])
    
    if orphaned:
        print(f"\n⚠ WARNING: {len(orphaned)} active partners without Stripe ID")
        print("   First 5:")
        for partner in orphaned[:5]:
            print(f"   - {partner.email}")
    
    # Verify sample Stripe customers
    print("\n" + "-" * 80)
    print("STRIPE CUSTOMER SAMPLE CHECK")
    print("-" * 80)
    
    sample_partners = env['res.partner'].search([
        ('stripe_customer_id', '!=', False)
    ], limit=5)
    
    for partner in sample_partners:
        try:
            stripe_customer = stripe.Customer.retrieve(partner.stripe_customer_id)
            status = "✓" if stripe_customer.email == partner.email else "✗"
            print(f"{status} {partner.email} → {partner.stripe_customer_id}")
        except Exception as e:
            print(f"✗ {partner.email}: Error - {str(e)}")
    
    print("\n" + "-" * 80)
    print("SUBSCRIPTION VERIFICATION")
    print("-" * 80)
    
    # Check subscriptions
    total_subs = env['subscription.subscription'].search_count([])
    subs_with_stripe = env['subscription.subscription'].search_count([
        ('stripe_subscription_id', '!=', False)
    ])
    active_subs = env['subscription.subscription'].search_count([
        ('state', '=', 'in_progress')
    ])
    active_subs_with_stripe = env['subscription.subscription'].search_count([
        ('state', '=', 'in_progress'),
        ('stripe_subscription_id', '!=', False)
    ])
    
    print(f"Total subscriptions in Odoo:      {total_subs}")
    print(f"Active subscriptions:             {active_subs}")
    print(f"Subscriptions with Stripe ID:     {subs_with_stripe}")
    print(f"Active subs with Stripe ID:       {active_subs_with_stripe}")
    if active_subs > 0:
        print(f"Active migration coverage:        {(active_subs_with_stripe/active_subs*100):.1f}%")
    
    # Check for unmigrated active subscriptions
    unmigrated_active = env['subscription.subscription'].search([
        ('state', '=', 'in_progress'),
        ('stripe_subscription_id', '=', False)
    ])
    
    if unmigrated_active:
        print(f"\n⚠ WARNING: {len(unmigrated_active)} active subscriptions not migrated")
        print("   Reasons:")
        for sub in unmigrated_active[:5]:
            reasons = []
            if not sub.customer_name:
                reasons.append("no customer")
            elif not sub.customer_name.stripe_customer_id:
                reasons.append("customer not migrated")
            if not sub.sub_plan_id:
                reasons.append("no plan")
            elif not sub.sub_plan_id.stripe_product_id:
                reasons.append("plan not synced")
            
            print(f"   - Subscription {sub.id}: {', '.join(reasons)}")
    
    # Verify sample Stripe subscriptions
    print("\n" + "-" * 80)
    print("STRIPE SUBSCRIPTION SAMPLE CHECK")
    print("-" * 80)
    
    sample_subs = env['subscription.subscription'].search([
        ('stripe_subscription_id', '!=', False)
    ], limit=5)
    
    for sub in sample_subs:
        try:
            stripe_sub = stripe.Subscription.retrieve(sub.stripe_subscription_id)
            print(f"✓ Subscription {sub.id} → {sub.stripe_subscription_id} (Status: {stripe_sub.status})")
        except Exception as e:
            print(f"✗ Subscription {sub.id}: Error - {str(e)}")
    
    print("\n" + "-" * 80)
    print("SUBSCRIPTION PLAN VERIFICATION")
    print("-" * 80)
    
    # Check plans
    total_plans = env['subscription.plan'].search_count([('active', '=', True)])
    plans_with_product = env['subscription.plan'].search_count([
        ('active', '=', True),
        ('stripe_product_id', '!=', False)
    ])
    
    print(f"Total active plans:               {total_plans}")
    print(f"Plans with Stripe Product ID:     {plans_with_product}")
    
    plans_without_prices = env['subscription.plan'].search([
        ('active', '=', True),
        ('stripe_product_id', '!=', False),
        ('stripe_price_month_id', '=', False)
    ])
    
    if plans_without_prices:
        print(f"\n⚠ WARNING: {len(plans_without_prices)} plans missing monthly price")
        for plan in plans_without_prices:
            print(f"   - {plan.name}")
    
    print("\n" + "-" * 80)
    print("WEBHOOK CONFIGURATION")
    print("-" * 80)
    
    # Check webhook secret
    webhook_secret = env['ir.config_parameter'].sudo().get_param('stripe_webhook_secret')
    if webhook_secret:
        print("✓ Webhook secret configured")
    else:
        print("⚠ Webhook secret not configured (recommended for production)")
    
    # Check recent webhook logs
    recent_logs = env['stripe.payment.log'].search([], order='create_date desc', limit=5)
    
    if recent_logs:
        print(f"\nRecent webhook events (last 5):")
        for log in recent_logs:
            print(f"  {log.create_date} - {log.event_type} - {log.event_status}")
    else:
        print("\n⚠ No webhook logs found (webhooks may not be configured)")
    
    print("\n" + "-" * 80)
    print("PAYMENT LOG VERIFICATION")
    print("-" * 80)
    
    total_logs = env['stripe.payment.log'].search_count([])
    print(f"Total payment logs:               {total_logs}")
    
    # Group by event type
    if total_logs > 0:
        print("\nLogs by event type:")
        log_types = env['stripe.payment.log'].read_group(
            [],
            ['event_type'],
            ['event_type']
        )
        for log_type in log_types:
            print(f"  {log_type['event_type']}: {log_type['event_type_count']}")
    
    print("\n" + "=" * 80)
    print("VERIFICATION SUMMARY")
    print("=" * 80)
    
    issues = []
    warnings = []
    
    # Critical issues
    if not stripe_key:
        issues.append("Stripe API key not configured")
    
    if plans_with_product < total_plans:
        issues.append(f"{total_plans - plans_with_product} plans not synced with Stripe")
    
    # Warnings
    if not webhook_secret:
        warnings.append("Webhook secret not configured")
    
    if len(recent_logs) == 0:
        warnings.append("No webhook events received (check webhook configuration)")
    
    if len(orphaned) > 0:
        warnings.append(f"{len(orphaned)} active customers not migrated")
    
    if len(unmigrated_active) > 0:
        warnings.append(f"{len(unmigrated_active)} active subscriptions not migrated")
    
    if issues:
        print("\n❌ CRITICAL ISSUES:")
        for issue in issues:
            print(f"   - {issue}")
    else:
        print("\n✅ No critical issues found")
    
    if warnings:
        print("\n⚠ WARNINGS:")
        for warning in warnings:
            print(f"   - {warning}")
    else:
        print("\n✅ No warnings")
    
    print("\n" + "=" * 80)
    
    if not issues:
        print("✅ Migration verification PASSED")
        print("   Your Stripe integration is ready to use!")
        return True
    else:
        print("❌ Migration verification FAILED")
        print("   Please address the critical issues above")
        return False


def compare_stripe_vs_odoo(env):
    """
    Compare Stripe data with Odoo data
    Useful for finding discrepancies
    """
    
    print("\n" + "=" * 80)
    print("STRIPE vs ODOO DATA COMPARISON")
    print("=" * 80 + "\n")
    
    stripe_key = env['ir.config_parameter'].sudo().get_param('stripe_secret_key')
    if not stripe_key:
        print("ERROR: Stripe not configured")
        return
    
    stripe.api_key = stripe_key
    
    # Count Stripe customers
    stripe_customers = stripe.Customer.list(limit=100)
    stripe_customer_count = len(list(stripe_customers.auto_paging_iter()))
    
    # Count Odoo customers with Stripe ID
    odoo_customer_count = env['res.partner'].search_count([
        ('stripe_customer_id', '!=', False)
    ])
    
    print(f"Stripe customers:     {stripe_customer_count}")
    print(f"Odoo partners:        {odoo_customer_count}")
    print(f"Difference:           {abs(stripe_customer_count - odoo_customer_count)}")
    
    if stripe_customer_count != odoo_customer_count:
        print("\n⚠ Customer counts don't match - investigating...")
        
        # Find Stripe customers not in Odoo
        all_odoo_stripe_ids = set(env['res.partner'].search([
            ('stripe_customer_id', '!=', False)
        ]).mapped('stripe_customer_id'))
        
        print("\nStripe customers not in Odoo (first 5):")
        count = 0
        for customer in stripe_customers.auto_paging_iter():
            if customer.id not in all_odoo_stripe_ids:
                print(f"  - {customer.id}: {customer.email}")
                count += 1
                if count >= 5:
                    break
    
    print("\n" + "-" * 80)
    
    # Count Stripe subscriptions
    stripe_subs = stripe.Subscription.list(limit=100)
    stripe_sub_count = len(list(stripe_subs.auto_paging_iter()))
    
    # Count Odoo subscriptions with Stripe ID
    odoo_sub_count = env['subscription.subscription'].search_count([
        ('stripe_subscription_id', '!=', False)
    ])
    
    print(f"Stripe subscriptions: {stripe_sub_count}")
    print(f"Odoo subscriptions:   {odoo_sub_count}")
    print(f"Difference:           {abs(stripe_sub_count - odoo_sub_count)}")
    
    print("\n" + "=" * 80 + "\n")


# Quick usage instructions
print("""
Stripe Migration Verification
==============================

From Odoo Shell, run:

# 1. Verify migration
verify_stripe_migration(env)

# 2. Compare Stripe vs Odoo counts
compare_stripe_vs_odoo(env)

# 3. Check specific customer
partner = env['res.partner'].search([('email', '=', 'user@example.com')], limit=1)
if partner.stripe_customer_id:
    print(f"Stripe ID: {partner.stripe_customer_id}")
else:
    print("Not migrated to Stripe")

# 4. Check specific subscription
sub = env['subscription.subscription'].browse(123)
if sub.stripe_subscription_id:
    print(f"Stripe Subscription: {sub.stripe_subscription_id}")
    print(f"Status: {sub.stripe_status}")
else:
    print("Not migrated to Stripe")

""")

