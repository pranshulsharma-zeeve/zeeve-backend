# -*- coding: utf-8 -*-
##########################################################################
# Author      : Webkul Software Pvt. Ltd. (<https://webkul.com/>)
# Copyright(c): 2017-Present Webkul Software Pvt. Ltd.
# All Rights Reserved.
#
#
#
# This program is copyright property of the author mentioned above.
# You can`t redistribute it and/or modify it.
#
#
# You should have received a copy of the License along with this program.
# If not, see <https://store.webkul.com/license.html/>
##########################################################################


import logging
_logger = logging.getLogger(__name__)
import stripe

from odoo import api, fields, models, _
from odoo.exceptions import ValidationError


class subscription_plan(models.Model):

    _name = "subscription.plan"
    _description = "Subscription Plan"

    name = fields.Char(string='Name', required=True)
    product_id = fields.Many2one('product.product', string='Product Variant')
    subscription_type = fields.Selection(
        [('rpc', 'RPC Nodes'), ('archive', 'Archive Node'), ('validator', 'Validator Node')],
        string='Subscription Type')
    protocol_id = fields.Many2one('protocol.master', string='Protocol')
    duration = fields.Integer(string='Plan Duration', required=True, default=1)
    unit = fields.Selection([('week','Week(s)'),('day','Day(s)'),('month','Month(s)'),('year','Year(s)')],string='Unit', required=True, default='day')
    plan_amount = fields.Float(string="Plan Price", required=True, default=1.0)
    amount_month = fields.Float(string='Amount Per Month (USD)')
    amount_quarter = fields.Float(string='Amount Per Quarter (USD)')
    amount_year = fields.Float(string='Amount Per Year (USD)')
    plan_description = fields.Text(string="Description")
    active = fields.Boolean(string="Active", default=True)
    override_product_price = fields.Boolean(string="Override Related products Price", help="Override the price of Product that are related to plan.", default="True")
    never_expires = fields.Boolean(string="Never Expire", help="This Plan billing cycle never expire instead of specifying a number of billing cycles.")
    num_billing_cycle = fields.Integer(string="Number Of Billing Cycles", help="Expire the plan after the given no. of Billing create", default=1)
    month_billing_day = fields.Integer(string="Billing day of month", help="The value that specifies the day of the month that the gateway will charge the subscription on every billing cycle")
    start_immediately = fields.Boolean(string="Start Immediately", help="This option helps to starts the subscription immediately.", default=True)
    trial_duration = fields.Integer(string='Trial Duration',default=1)
    trial_duration_unit = fields.Selection([('week','Week(s)'),('day','Day(s)'),('month','Month(s)'),('year','Year(s)')],string='Unit ', help="The trial unit specified in a plan. Specify day, month, year.")
    trial_period = fields.Boolean(string="Plan has trial period", help="A value indicating whether a subscription should begin with a trial period.")
    subscription_ids = fields.One2many('subscription.subscription', 'sub_plan_id', string="Subscriptions")
    subscrption_count = fields.Integer(string='#', compute="get_subscription_count")
    # product_ids = fields.One2many('product.product', 'subscription_plan_id', string="Products")
    # product_count = fields.Integer(string='# ', compute="get_product_count")
    region_ids = fields.Many2many('server.location', string='Regions Available')
    color = fields.Integer(string='Color Index')
    stripe_product_id = fields.Char("Stripe Product ID")
    stripe_price_month_id = fields.Char("Stripe Price (Monthly)")
    stripe_price_quarter_id = fields.Char("Stripe Price (Quarterly)")
    stripe_price_year_id = fields.Char("Stripe Price (Yearly)")

    bandwidth = fields.Char(string='Bandwidth')
    domainCustomization = fields.Boolean(string="Domain Customization", default=False)
    ipWhitelist = fields.Boolean(string="IP Whitelist", default=False)
    monthlyLimit = fields.Char(string='Monthly Limit')
    softwareUpgrades = fields.Char(string='Software Upgrades')
    support = fields.Char(string='Support')
    uptimeSLA = fields.Char(string='Uptime SLA')
    
    # Discount Association
    discount_ids = fields.Many2many(
        'subscription.discount',
        'subscription_plan_discount_rel',
        'plan_id',
        'discount_id',
        string='Available Discounts',
        help="Discounts available for this subscription plan"
    )
    
    @api.constrains('duration','trial_duration')
    def duration_check(self):
        for rec in self:
            if rec.duration < 0 or (rec.trial_duration and rec.trial_duration < 0) : 
                raise ValidationError('Duration Cannot be in negative')
            
    @api.model
    def default_get(self, fields):
        res = super().default_get(fields)
        if self.env.context.get('default_protocol_id'):
            res['protocol_id'] = self.env.context['default_protocol_id']
        return res
    @api.constrains('num_billing_cycle','trial_duration','duration')
    def _verify_num_billing_cycle(self):
        for plan in self:
            if not plan.never_expires and plan.num_billing_cycle == 0:
                raise ValidationError(_("Billing cycle never be 0."))
            if plan.trial_period and plan.trial_duration <= 0:
                raise ValidationError(_("Trial duration never be 0 or less."))
            if plan.duration <=0:
                raise ValidationError(_("Plan duration never be 0 or less."))
    
    def get_subscription_count(self):
        for obj in self:
            obj.subscrption_count = len(obj.subscription_ids.ids)

    
    # def get_product_count(self):
    #     for obj in self:
    #         obj.product_count = len(obj.product_ids.ids)


    
    @api.depends('name', 'duration', 'unit')
    def name_get(self):
        result = []
        for subscription in self:
            name = subscription.name + ' (' + str(subscription.duration) + ' ' + subscription.unit + ' )'
            result.append((subscription.id, name))
        return result

    @api.onchange('trial_period')
    def onchange_trial_period(self):
        if self.trial_period:
            self.start_immediately = False

    @api.onchange('start_immediately')
    def onchange_start_immediately(self):
        if self.start_immediately:
            self.trial_period = False
            self.trial_duration = 0
            self.trial_duration_unit = ""

    @api.onchange('never_expires')
    def onchange_never_expires(self):
        if self.never_expires and self.num_billing_cycle !=-1:
            self.num_billing_cycle = -1
        else: 
            self.num_billing_cycle = 1


    
    def action_view_subscription(self):
        subscription_ids = self.mapped('subscription_ids')
        action = self.env.ref('subscription_management.action_subscription').read()[0]
        action['context'] = {}
        if len(subscription_ids) > 1:
            action['domain'] = "[('id','in',%s)]" % subscription_ids.ids
        elif len(subscription_ids) == 1:
            action['views'] = [(self.env.ref('subscription_management.subscription_subscription_form_view').id, 'form')]
            action['res_id'] = subscription_ids.ids[0]
        else:
            action = {'type': 'ir.actions.act_window_close'}
        return action


    
    def action_view_products(self):
        product_ids = self.mapped('product_ids')
        action = self.env.ref('product.product_normal_action').read()[0]
        context = action.get('context')
        # _logger.info('============%r',context)
        action['domain'] = "[('id','in',%s)]" % product_ids.ids
        if len(product_ids) > 1:
            action['domain'] = "[('id','in',%s)]" % product_ids.ids
        elif len(product_ids) == 1:
            action['views'] = [(self.env.ref('product.product_template_form_view').id, 'form')]
            action['res_id'] = product_ids.ids[0]
        else:
            action = {'type': 'ir.actions.act_window_close'}
        return action
    
    def _get_stripe_client(self):
        """Initialize Stripe with secret key from system parameters"""
        secret_key = self.env['ir.config_parameter'].sudo().get_param("stripe_secret_key")
        stripe.api_key = secret_key
        return stripe
    
    def action_sync_with_stripe(self):
        stripe.api_key = self.env['ir.config_parameter'].sudo().get_param("stripe_secret_key")
        # print('api key ===',self.env['ir.config_parameter'].sudo().get_param("stripe_secret_key"))
        # stripe.api_key = 'sk_test_51S5rAOPyEmgoxfpFsYrRWeR2ZFXo9Fc5NAAB3lXsXxWmpdiucDteKdvEGQK1eyyzn23ZCI0znye7yD9dbRdGS4cq00hbLb90nG'

        for plan in self:
            # 1. Ensure product exists
            if not plan.stripe_product_id:
                product = stripe.Product.create(name=plan.name)
                plan.stripe_product_id = product.id

            # 2. Monthly price
            if plan.amount_month and not plan.stripe_price_month_id:
                price = stripe.Price.create(
                    unit_amount=int(plan.amount_month * 100),
                    currency="usd",
                    recurring={"interval": "month"},
                    product=plan.stripe_product_id,
                )
                plan.stripe_price_month_id = price.id

            # 3. Quarterly price
            if plan.amount_quarter and not plan.stripe_price_quarter_id:
                price = stripe.Price.create(
                    unit_amount=int(plan.amount_quarter * 100),
                    currency="usd",
                    recurring={"interval": "month", "interval_count": 3},
                    product=plan.stripe_product_id,
                )
                plan.stripe_price_quarter_id = price.id

            # 4. Yearly price
            if plan.amount_year and not plan.stripe_price_year_id:
                price = stripe.Price.create(
                    unit_amount=int(plan.amount_year * 100),
                    currency="usd",
                    recurring={"interval": "year"},
                    product=plan.stripe_product_id,
                )
                plan.stripe_price_year_id = price.id
class ProtocolMaster(models.Model):
    _inherit = "protocol.master"

    plan_ids = fields.One2many("subscription.plan", "protocol_id", string="Plans")
    plan_count = fields.Integer(string="Plans Count", compute="_compute_plan_count")

    @api.depends("plan_ids")
    def _compute_plan_count(self):
        for rec in self:
            rec.plan_count = len(rec.plan_ids)

    def action_view_plans(self):
        self.ensure_one()
        action = self.env.ref("subscription_management.action_subscription_plan").read()[0]
        action["domain"] = [("protocol_id", "=", self.id)]
        return action
