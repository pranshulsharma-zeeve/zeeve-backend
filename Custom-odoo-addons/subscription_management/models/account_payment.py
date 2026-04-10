# -*- coding: utf-8 -*-
"""Extend account.payment to link payments with subscriptions."""
from odoo import api, fields, models
from odoo.exceptions import AccessError

class AccountPayment(models.Model):
    _inherit = 'account.payment'

    subscription_id = fields.Many2one('subscription.subscription', string='Subscription')

    # def action_post(self):
    #     res = super().action_post()
    #     for payment in self:
    #         subscription = payment.subscription_id
    #         if subscription:
    #             subscription._register_payment(payment)
    #     return res
    
    def unlink(self):
        if self.env.su or self.env.context.get("allow_invoice_unlink"):
            return super().unlink()
        # Allowed groups
        allowed_groups = [
            'access_rights.group_admin',
            'access_rights.group_technical_manager'
        ]

        user = self.env.user

        # Check if user is in allowed groups
        print('-----------6830')
        if not user.has_group(allowed_groups[0]) and not user.has_group(allowed_groups[1]):
            raise AccessError("You are not allowed to delete invoices.")

        return super(AccountPayment, self).unlink()