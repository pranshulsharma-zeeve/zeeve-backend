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

from odoo import api, fields, models, _
# from odoo.exceptions import Warning


class ResConfigSettings(models.TransientModel):
    _inherit = 'res.config.settings'

    journal_id = fields.Many2one('account.journal', string='Payment Method',
                                 required=True, domain=[('type', 'in', ('sale',))])

    paid_subscription_journal = fields.Many2one('account.journal', string='Paid Payment Journal',
                                                domain=[('type', 'in', ('bank', 'cash'))])
    renewal_days = fields.Integer(
        "Renewal days", help="Enter the no of days to send alert message to user regarding subscription expiration...")
    invoice_generated = fields.Selection([('draft', 'Draft'), ('post', 'Post'), (
        'paid', 'Paid')], help="Generate Invoice in particular State automatically", default='draft')

    invoice_email = fields.Boolean(
        help='Send subscription based invoice to the Customer by Email.')

    trial_period_setting = fields.Selection([('one_time', 'Give Trial to one time'), (
        'product_based', 'Give Trial based on product')], help='Apply the Trial period policy.', default='one_time')
    
    # Stripe Configuration
    stripe_secret_key = fields.Char(string='Stripe Secret Key', help='Stripe secret key for API access')
    stripe_publishable_key = fields.Char(string='Stripe Publishable Key', help='Stripe publishable key for frontend')
    stripe_webhook_secret = fields.Char(string='Stripe Webhook Secret', help='Stripe webhook endpoint secret')
    stripe_webhook_url = fields.Char(string='Stripe Webhook URL', help='Webhook URL to configure in Stripe dashboard')
    stripe_currency = fields.Selection([
        ('usd', 'USD'),
        ('eur', 'EUR'),
        ('gbp', 'GBP'),
        ('cad', 'CAD'),
        ('aud', 'AUD'),
    ], string='Default Currency', default='usd', help='Default currency for Stripe payments')
    stripe_autopay_enabled = fields.Boolean(string='Enable Autopay by Default', default=True, 
                                          help='Enable autopay for new subscriptions by default')

    @api.onchange('invoice_generated')
    def _onchange_invoice_generated(self):
        if self.invoice_generated == 'draft':
            self.invoice_email = False

    def get_values(self):
        res = super(ResConfigSettings, self).get_values()
        IrDefault = self.env['ir.default'].sudo()
        res.update({
            'journal_id': IrDefault._get('res.config.settings', 'journal_id'),
            'invoice_generated': IrDefault._get('res.config.settings', 'invoice_generated'),
            'invoice_email': IrDefault._get('res.config.settings', 'invoice_email'),
            'renewal_days': IrDefault._get('res.config.settings', 'renewal_days'),
            'trial_period_setting': IrDefault._get('res.config.settings', 'trial_period_setting'),
            'paid_subscription_journal': IrDefault._get('res.config.settings', 'paid_subscription_journal'),
            'stripe_secret_key': self.env['ir.config_parameter'].sudo().get_param('stripe_secret_key'),
            'stripe_publishable_key': self.env['ir.config_parameter'].sudo().get_param('stripe_publishable_key'),
            'stripe_webhook_secret': self.env['ir.config_parameter'].sudo().get_param('stripe_webhook_secret'),
            'stripe_webhook_url': self.env['ir.config_parameter'].sudo().get_param('stripe_webhook_url'),
            'stripe_currency': self.env['ir.config_parameter'].sudo().get_param('stripe_currency', 'usd'),
            'stripe_autopay_enabled': self.env['ir.config_parameter'].sudo().get_param('stripe_autopay_enabled', 'True') == 'True',
        })
        return res

    def set_values(self):
        super(ResConfigSettings, self).set_values()
        IrDefault = self.env['ir.default'].sudo()
        IrDefault.set('res.config.settings', 'journal_id', self.journal_id.id)
        IrDefault.set('res.config.settings', 'invoice_generated',
                      self.invoice_generated)
        IrDefault.set('res.config.settings',
                      'invoice_email', self.invoice_email)
        IrDefault.set('res.config.settings', 'renewal_days', self.renewal_days)
        IrDefault.set('res.config.settings',
                      'trial_period_setting', self.trial_period_setting)
        IrDefault.set('res.config.settings', 'paid_subscription_journal',
                      self.paid_subscription_journal.id)
        
        # Set Stripe configuration
        self.env['ir.config_parameter'].sudo().set_param('stripe_secret_key', self.stripe_secret_key or '')
        self.env['ir.config_parameter'].sudo().set_param('stripe_publishable_key', self.stripe_publishable_key or '')
        self.env['ir.config_parameter'].sudo().set_param('stripe_webhook_secret', self.stripe_webhook_secret or '')
        self.env['ir.config_parameter'].sudo().set_param('stripe_webhook_url', self.stripe_webhook_url or '')
        self.env['ir.config_parameter'].sudo().set_param('stripe_currency', self.stripe_currency or 'usd')
        self.env['ir.config_parameter'].sudo().set_param('stripe_autopay_enabled', str(self.stripe_autopay_enabled))

        return True
