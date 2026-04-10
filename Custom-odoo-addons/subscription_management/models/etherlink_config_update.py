# -*- coding: utf-8 -*-
"""Model storing Etherlink node configuration update logs."""

from odoo import fields, models


class EtherlinkNodeConfigUpdate(models.Model):
    """Persistent log of node configuration updates triggered by users."""

    _name = "etherlink.node.config.update"
    _description = "Etherlink Node Configuration Update Log"
    _order = "id desc"
    _rec_name = "subscription_id"

    subscription_id = fields.Many2one(
        "subscription.subscription",
        string="Subscription",
        ondelete="set null",
        index=True,
    )
    node_id = fields.Char(string="Node ID", required=True, index=True)
    protocol_name = fields.Char(string="Protocol Name", required=True)
    user_email = fields.Char(string="User Email", required=True, index=True)
    user_id = fields.Many2one("res.users", string="User", ondelete="set null", index=True)
    updated_at = fields.Datetime(string="Updated At", required=True, index=True)
    updated_config = fields.Text(string="Updated Config", required=True)
    status = fields.Char(string="Status", required=True)
