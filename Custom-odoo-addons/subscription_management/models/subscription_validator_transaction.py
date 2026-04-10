# -*- coding: utf-8 -*-
"""Model to log validator transaction actions triggered outside of Odoo."""

from odoo import fields, models, _


class SubscriptionValidatorTransaction(models.Model):
    """Stores transaction hashes/actions tied to a subscription."""

    _name = 'subscription.validator.transaction'
    _description = 'Validator Transaction'
    _order = 'id desc'

    subscription_id = fields.Many2one(
        'subscription.subscription',
        ondelete='cascade',
        index=True,
        string='Subscription',
    )
    node_id = fields.Many2one(
        'subscription.node',
        ondelete='cascade',
        index=True,
        string='Subscription Node',
    )
    transaction_hash = fields.Char(string='Transaction Hash', required=True, index=True)
    action = fields.Char(string='Action', required=True)
    notes = fields.Text(string='Notes')

    _sql_constraints = [
        (
            'unique_transaction_per_subscription',
            'unique(subscription_id, transaction_hash)',
            'This transaction hash already exists for the subscription.',
        )
    ]
