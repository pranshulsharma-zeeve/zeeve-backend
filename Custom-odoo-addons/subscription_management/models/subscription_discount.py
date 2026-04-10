# -*- coding: utf-8 -*-
##########################################################################
# Author      : Webkul Software Pvt. Ltd. (<https://webkul.com/>)
# Copyright(c): 2017-Present Webkul Software Pvt. Ltd.
# All Rights Reserved.
#
# This program is copyright property of the author mentioned above.
# You can`t redistribute it and/or modify it.
#
# You should have received a copy of the License along with this program.
# If not, see <https://store.webkul.com/license.html/>
##########################################################################

import logging
import re
import time
import stripe
from odoo import api, fields, models, _
from odoo.exceptions import ValidationError, UserError
from datetime import datetime,timezone
_logger = logging.getLogger(__name__)

_CODE_RE = re.compile(r'^[A-Za-z0-9_-]+$')

class SubscriptionDiscount(models.Model):
    """Model for managing subscription discounts with Stripe integration"""
    
    _name = "subscription.discount"
    _description = "Subscription Discount"
    _order = 'name'
    
    name = fields.Char(string='Discount Name', required=True, tracking=True)
    code = fields.Char(string='Discount Code', required=True, tracking=True, 
                      help="Unique discount code that customers will use")
    description = fields.Text(string='Description', help="Description of the discount")
    
    # Discount Configuration
    discount_type = fields.Selection([
        ('percentage', 'Percentage'),
        ('fixed_amount', 'Fixed Amount')
    ], string='Discount Type', required=True, default='percentage', tracking=True)
    
    discount_value = fields.Float(string='Discount Value', required=True, tracking=True,
                                 help="Percentage (0-100) or fixed amount in USD")
    
    # Validity
    active = fields.Boolean(string='Active', default=True, tracking=True)
    valid_from = fields.Datetime(string='Valid From', required=True, tracking=True)
    valid_until = fields.Datetime(string='Valid Until', tracking=True,
                                 help="Leave empty for no expiration")
    
    # Usage Limits
    usage_limit = fields.Integer(string='Usage Limit', tracking=True,
                                help="Maximum number of times this discount can be used. Leave empty for unlimited.")
    usage_count = fields.Integer(string='Usage Count', default=0, readonly=True,
                                help="Number of times this discount has been used")
    
    # Subscription Plan Association
    subscription_plan_ids = fields.Many2many(
        'subscription.plan', 
        'subscription_discount_plan_rel',
        'discount_id', 
        'plan_id',
        string='Applicable Plans',
        help="Subscription plans this discount applies to. Leave empty to apply to all plans."
    )
    
    # Protocol Association
    protocol_ids = fields.Many2many(
        'protocol.master',
        'subscription_discount_protocol_rel', 
        'discount_id',
        'protocol_id',
        string='Applicable Protocols',
        help="Protocols this discount applies to. Leave empty to apply to all protocols."
    )
    
    # Applicability Scope
    applicability_scope = fields.Selection(
        [
            ("subscription", "Network Subscriptions"),
            ("rollup", "Rollup Services"),
            ("both", "All Services"),
        ],
        string="Applies To",
        required=True,
        default="subscription",
        tracking=True,
        help="Control where this discount can be used. Choose 'Rollup Services' to allow rollup"
             " deployments to redeem the code, 'Network Subscriptions' for node subscriptions,"
             " or 'All Services' to share it between both flows.",
    )

    # Stripe Integration
    stripe_coupon_id = fields.Char(string='Stripe Coupon ID', readonly=True, copy=False,
                                  help="Stripe coupon ID for this discount")
    stripe_synced = fields.Boolean(string='Synced with Stripe', default=False, readonly=True)
    last_sync_date = fields.Datetime(string='Last Sync Date', readonly=True)
    
    # Additional Fields
    minimum_amount = fields.Float(string='Minimum Amount', 
                                 help="Minimum subscription amount required to use this discount")
    maximum_discount_amount = fields.Float(string='Maximum Discount Amount',
                                          help="Maximum discount amount (for percentage discounts)")
    
    # Stripe Duration Configuration
    duration_in_months = fields.Integer(
        string='Duration (Months)', 
        default=1, 
        required=True,
        tracking=True,
        help="Number of months the discount applies after redemption (1-60 months)"
    )
    
    # Computed Fields
    is_valid = fields.Boolean(string='Is Valid', compute='_compute_is_valid', store=True)
    remaining_usage = fields.Integer(string='Remaining Usage', compute='_compute_remaining_usage')
    stripe_promotion_code_id = fields.Char(string='Stripe Promotion Code ID', readonly=True, copy=False)
    stripe_synced = fields.Boolean()
    last_sync_date = fields.Datetime()

    @api.depends('active', 'valid_from', 'valid_until', 'usage_limit', 'usage_count')
    def _compute_is_valid(self):
        """Check if discount is currently valid"""
        now = fields.Datetime.now()
        for discount in self:
            if not discount.active:
                discount.is_valid = False
                continue
                
            if discount.valid_from and discount.valid_from > now:
                discount.is_valid = False
                continue
                
            if discount.valid_until and discount.valid_until < now:
                discount.is_valid = False
                continue
                
            if discount.usage_limit and discount.usage_count >= discount.usage_limit:
                discount.is_valid = False
                continue
                
            discount.is_valid = True
    
    @api.depends('usage_limit', 'usage_count')
    def _compute_remaining_usage(self):
        """Calculate remaining usage count"""
        for discount in self:
            if discount.usage_limit:
                discount.remaining_usage = max(0, discount.usage_limit - discount.usage_count)
            else:
                discount.remaining_usage = -1  # Unlimited
    
    @api.constrains('discount_value', 'discount_type')
    def _check_discount_value(self):
        """Validate discount value based on type"""
        for discount in self:
            if discount.discount_type == 'percentage':
                if discount.discount_value < 0 or discount.discount_value > 100:
                    raise ValidationError(_("Percentage discount must be between 0 and 100."))
            elif discount.discount_type == 'fixed_amount':
                if discount.discount_value <= 0:
                    raise ValidationError(_("Fixed amount discount must be greater than 0."))
    
    @api.constrains('code')
    def _check_code_unique(self):
        """Ensure discount code is unique"""
        for discount in self:
            if self.search_count([('code', '=', discount.code), ('id', '!=', discount.id)]) > 0:
                raise ValidationError(_("Discount code must be unique."))
    
    @api.constrains('valid_from', 'valid_until')
    def _check_validity_dates(self):
        """Validate validity date range"""
        for discount in self:
            if discount.valid_until and discount.valid_from and discount.valid_until <= discount.valid_from:
                raise ValidationError(_("Valid until date must be after valid from date."))
    
    @api.constrains('duration_in_months')
    def _check_duration_in_months(self):
        """Validate duration in months"""
        for discount in self:
            if discount.duration_in_months < 1:
                raise ValidationError(_("Duration in months must be at least 1."))
            if discount.duration_in_months > 60:
                raise ValidationError(_("Duration in months cannot exceed 60 (5 years)."))
    
    
    def _get_stripe_client(self):
        """Initialize Stripe with secret key from system parameters"""
        secret_key = self.env['ir.config_parameter'].sudo().get_param("stripe_secret_key")
        if not secret_key:
            raise UserError(_("Stripe secret key not configured."))
        stripe.api_key = secret_key
        return stripe

    def action_sync_with_stripe(self):
        """FINAL VERSION - NO MORE ERRORS - COPY-PASTE THIS"""
        for discount in self:
            try:
                stripe = discount._get_stripe_client()

                # ========= 1. COUPON =========
                coupon_data = {
                    'name': discount.name,
                    'metadata': {
                        'odoo_discount_id': str(discount.id),
                        'discount_type': discount.discount_type,
                        'discount_value': str(discount.discount_value),
                        'applicability_scope': discount.applicability_scope or '',
                    },
                }
                if discount.discount_type == 'percentage':
                    coupon_data['percent_off'] = float(discount.discount_value)
                else:
                    coupon_data['amount_off'] = int(discount.discount_value * 100)
                    coupon_data['currency'] = 'usd'

                coupon_data['duration'] = 'repeating'
                coupon_data['duration_in_months'] = max(1, discount.duration_in_months or 1)

                if discount.stripe_coupon_id:
                    # ONLY name + metadata → SAFE
                    stripe.Coupon.modify(
                        discount.stripe_coupon_id,
                        name=coupon_data['name'],
                        metadata=coupon_data['metadata']
                    )
                    coupon_id = discount.stripe_coupon_id
                else:
                    coupon_data['id'] = discount.code.upper()
                    coupon = stripe.Coupon.create(**coupon_data)
                    discount.stripe_coupon_id = coupon.id
                    coupon_id = coupon.id

                # ========= 2. PROMOTION CODE =========
     # ========= 2. PROMOTION CODE - ONLY ALLOWED FIELDS ON MODIFY =========
                promo_code_data = {
                    "active": True,
                    "metadata": {
                        "discount_type": discount.discount_type,
                        "discount_value": str(discount.discount_value),
                    }
                }

                if discount.valid_until:
                    valid_until_ts = int(discount.valid_until.timestamp())
                    current_ts = int(datetime.now(timezone.utc).timestamp())
                    if valid_until_ts > current_ts:
                        promo_code_data["expires_at"] = valid_until_ts

                existing_promo_codes = stripe.PromotionCode.list(code=discount.code, limit=1)

                if existing_promo_codes.data:
                    # ✅ Only update allowed fields
                    promo_code_id = existing_promo_codes.data[0].id
                    stripe.PromotionCode.modify(promo_code_id, **promo_code_data)
                    discount.stripe_promotion_code_id = promo_code_id
                else:
                    # ✅ Create new promo code (allowed to set code, coupon, max_redemptions)
                    new_promo_code_data = {
                        "coupon": coupon_id,
                        "code": discount.code,
                        "active": True,
                        "metadata": promo_code_data["metadata"],
                    }

                    if discount.valid_until and valid_until_ts > current_ts:
                        new_promo_code_data["expires_at"] = valid_until_ts
                    if discount.usage_limit:
                        new_promo_code_data["max_redemptions"] = discount.usage_limit

                    promo_code = stripe.PromotionCode.create(**new_promo_code_data)
                    discount.stripe_promotion_code_id = promo_code.id
                discount.write({'stripe_synced': True, 'last_sync_date': fields.Datetime.now()})
                print(f"FULL SUCCESS: {discount.code} synced!")
                _logger.info("SUCCESS: %s synced → %s", discount.code, coupon_id)

            except Exception as e:
                error_msg = str(e)
                if "unknown parameter" in error_msg.lower():
                    error_msg = "STOP! You have old code calling Coupon.modify() with promo fields!"
                raise UserError(error_msg)
    
    def action_unsync_from_stripe(self):
        """Remove discount from Stripe"""
        for discount in self:
            if discount.stripe_coupon_id:
                try:
                    stripe = self._get_stripe_client()
                    stripe.Coupon.delete(discount.stripe_coupon_id)
                    
                    discount.write({
                        'stripe_coupon_id': False,
                        'stripe_synced': False,
                        'last_sync_date': False
                    })
                    
                    _logger.info(f"Successfully removed discount {discount.name} from Stripe")
                    
                except stripe.error.StripeError as e:
                    _logger.error(f"Stripe error removing discount {discount.name}: {str(e)}")
                    raise UserError(_("Failed to remove from Stripe: %s") % str(e))
                except Exception as e:
                    _logger.error(f"Error removing discount {discount.name}: {str(e)}")
                    raise UserError(_("Failed to remove discount: %s") % str(e))
    
    def can_apply_to_subscription(self, subscription_plan_id=None, protocol_id=None):
        """Check if this discount can be applied to a specific subscription"""
        self.ensure_one()

        is_rollup = self.env["rollup.type"].sudo().search([("stripe_product_id", "=", subscription_plan_id)], limit=1)
        if is_rollup:
            if not self._is_applicable_for_scope("rollup"):
                return False, "Discount not applicable to rollup services"
        else:
            # for now apply to all rollups
            return True, "Valid"
            
        if not self.is_valid:
            return False, "Discount is not currently valid"


        # Check plan restrictions
        if self.subscription_plan_ids and subscription_plan_id not in self.subscription_plan_ids.ids:
            return False, "Discount not applicable to this subscription plan"

        # Check protocol restrictions
        if protocol_id and self.protocol_ids and protocol_id not in self.protocol_ids.ids:
            return False, "Discount not applicable to this protocol"

        return True, "Valid"

    def can_apply_to_rollup(self):
        """Check whether the discount can be redeemed for rollup services."""
        self.ensure_one()

        if not self.is_valid:
            return False, "Discount is not currently valid"

        if not self._is_applicable_for_scope("rollup"):
            return False, "Discount not applicable to rollup services"

        return True, "Valid"

    def _is_applicable_for_scope(self, scope):
        """Return whether the discount is allowed for the requested scope."""

        scope = scope or "subscription"
        allowed_map = {
            "subscription": {"subscription", "both"},
            "rollup": {"rollup", "both"},
            "both": {"subscription", "rollup", "both"},
        }
        allowed_values = allowed_map.get(scope, allowed_map["subscription"])
        return (self.applicability_scope or "subscription") in allowed_values

    def calculate_discount_amount(self, subscription_amount):
        """Calculate the discount amount for a given subscription amount"""
        self.ensure_one()
        
        if self.discount_type == 'percentage':
            discount_amount = (subscription_amount * self.discount_value) / 100
            if self.maximum_discount_amount and discount_amount > self.maximum_discount_amount:
                discount_amount = self.maximum_discount_amount
        else:
            discount_amount = self.discount_value
        
        return min(discount_amount, subscription_amount)  # Can't discount more than the amount
    
    def apply_discount(self):
        """Record usage of this discount"""
        self.ensure_one()
        self.usage_count += 1
        _logger.info(f"Applied discount {self.name} (usage count: {self.usage_count})")
    
    @api.model
    def get_available_discounts(
        self,
        subscription_plan_id=None,
        protocol_id=None,
        amount=0,
        scope="subscription",
    ):
        """Get all available discounts for a subscription"""
        domain = [('is_valid', '=', True)]

        if scope:
            scope_map = {
                "subscription": ["subscription", "both"],
                "rollup": ["rollup", "both"],
                "both": ["subscription", "rollup", "both"],
            }
            domain.append(('applicability_scope', 'in', scope_map.get(scope, scope_map['subscription'])))

        if subscription_plan_id:
            domain.extend([
                '|', ('subscription_plan_ids', '=', False),
                ('subscription_plan_ids', 'in', [subscription_plan_id])
            ])
        elif scope == "rollup":
            domain.append(('subscription_plan_ids', '=', False))

        if protocol_id:
            domain.extend([
                '|', ('protocol_ids', '=', False),
                ('protocol_ids', 'in', [protocol_id])
            ])
        elif scope == "rollup":
            domain.append(('protocol_ids', '=', False))
        
        if amount > 0:
            domain.append(('minimum_amount', '<=', amount))
        
        return self.search(domain)
    
    @api.model
    def validate_discount_code(
        self,
        code,
        subscription_plan_id=None,
        protocol_id=None,
        amount=0,
        scope="subscription",
    ):
        """Validate a discount code and return the discount if valid"""
        discount = self.search([('code', '=', code)], limit=1)

        if not discount:
            return None, "Invalid discount code"

        if scope == "rollup":
            can_apply, message = discount.can_apply_to_rollup()
        else:
            can_apply, message = discount.can_apply_to_subscription(subscription_plan_id, protocol_id)

        if not can_apply:
            return None, message

        if amount > 0 and discount.minimum_amount > amount:
            return None, f"Minimum amount required: ${discount.minimum_amount}"

        return discount, "Valid"
    
    

    @api.model
    def create(self, vals):
        if 'code' in vals and vals['code']:
            vals['code'] = vals['code'].strip().upper()
        return super(SubscriptionDiscount, self).create(vals)

    def write(self, vals):
        if 'code' in vals and vals.get('code'):
            vals['code'] = vals['code'].strip().upper()
        return super(SubscriptionDiscount, self).write(vals)


    @api.constrains('code')
    def _check_code_valid_and_unique(self):
        """1) Validate allowed characters
           2) Enforce case-insensitive uniqueness"""
        for rec in self:
            if not rec.code:
                continue
            if not _CODE_RE.match(rec.code):
                raise ValidationError(_("Discount code may contain only letters, digits, hyphen (-) and underscore (_)."))
            domain = [('id', '!=', rec.id), ('code', '=ilike', rec.code)]
            if self.search_count(domain) > 0:
                raise ValidationError(_("Discount code must be unique (case-insensitive)."))

    @api.constrains('valid_from', 'valid_until')
    def _check_validity_dates(self):
        """Validate validity date range and do not allow dates in the past."""
        now = fields.Datetime.now()
        for discount in self:
            if discount.valid_from and discount.valid_from < now:
                raise ValidationError(_("Valid From cannot be in the past. Please set a future date/time."))

            if discount.valid_until and discount.valid_until < now:
                raise ValidationError(_("Valid Until cannot be in the past."))

            if discount.valid_until and discount.valid_from and discount.valid_until <= discount.valid_from:
                raise ValidationError(_("Valid Until date must be after Valid From date."))

    @api.constrains('minimum_amount', 'maximum_discount_amount')
    def _check_min_max_amounts(self):
        """Prevent negative minimum/maximum amounts and ensure max is non-negative."""
        for rec in self:
            if rec.minimum_amount is not None and rec.minimum_amount < 0:
                raise ValidationError(_("Minimum Amount cannot be negative."))

            if rec.maximum_discount_amount is not None and rec.maximum_discount_amount < 0:
                raise ValidationError(_("Maximum Discount Amount cannot be negative."))

    @api.constrains('discount_value', 'discount_type')
    def _check_discount_value(self):
        for discount in self:
            if discount.discount_type == 'percentage':
                if discount.discount_value < 0 or discount.discount_value > 100:
                    raise ValidationError(_("Percentage discount must be between 0 and 100."))
            elif discount.discount_type == 'fixed_amount':
                if discount.discount_value <= 0:
                    raise ValidationError(_("Fixed amount discount must be greater than 0."))

    @api.constrains('duration_in_months')
    def _check_duration_in_months(self):
        for discount in self:
            if discount.duration_in_months < 1:
                raise ValidationError(_("Duration in months must be at least 1."))
            if discount.duration_in_months > 60:
                raise ValidationError(_("Duration in months cannot exceed 60 (5 years)."))
    @api.onchange('duration_in_months')
    def _onchange_duration_in_months(self):
        """Return a UI warning if user enters an invalid duration."""
        if self.duration_in_months is False or self.duration_in_months is None:
            return
        try:
            val = int(self.duration_in_months)
        except (ValueError, TypeError):
            return {
                'warning': {
                    'title': _("Invalid duration"),
                    'message': _("Please enter a valid integer for Duration (Months).")
                }
            }
        if val < 1 or val > 60:
            return {
                'warning': {
                    'title': _("Invalid duration"),
                    'message': _("Duration must be between 1 and 60 months.")
                }
            }

    @api.onchange('valid_from', 'valid_until')
    def _onchange_validity_dates(self):
        """Immediate UI warning if dates are in the past or range is invalid."""
        now = fields.Datetime.now()
        if self.valid_from and self.valid_from < now:
            return {
                'warning': {
                    'title': _("Invalid Valid From"),
                    'message': _("Valid From cannot be in the past.")
                }
            }
        if self.valid_until and self.valid_until < now:
            return {
                'warning': {
                    'title': _("Invalid Valid Until"),
                    'message': _("Valid Until cannot be in the past.")
                }
            }
        if self.valid_from and self.valid_until and self.valid_until <= self.valid_from:
            return {
                'warning': {
                    'title': _("Invalid date range"),
                    'message': _("Valid Until must be after Valid From.")
                }
            }

    def unlink(self):
        """Clean up Stripe coupons when deleting discounts.
           Note: server-side cannot show interactive confirm dialogs — see view suggestion below
        """
        for discount in self:
            if discount.stripe_coupon_id:
                try:
                    discount.action_unsync_from_stripe()
                except Exception:
                    _logger.exception("Failed to remove stripe coupon for %s during unlink", discount.code)
        return super(SubscriptionDiscount, self).unlink()
