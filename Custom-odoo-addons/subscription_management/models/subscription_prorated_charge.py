# -*- coding: utf-8 -*-
from odoo import models, fields

class SubscriptionProratedCharge(models.Model):
    _name = 'subscription.prorated.charge'
    _description = 'Subscription Prorated Charge'
    _order = 'id desc'

    subscription_id = fields.Many2one(
        'subscription.subscription', 
        string='Subscription', 
        required=True,
        ondelete='cascade'
    )
    session_id = fields.Char(string='Stripe Session ID')
    checkout_url = fields.Char(string='Checkout URL')
    amount = fields.Float(string='Amount')
    quantity_increase = fields.Integer(string='Quantity Increase')
    stripe_subscription_id = fields.Char(string='Stripe Subscription ID')
    state = fields.Selection([
        ('draft', 'Draft'),
        ('paid', 'Paid'),
        ('cancelled', 'Cancelled')
    ], string='Status', default='draft')
    payment_date = fields.Datetime(string='Payment Date')
