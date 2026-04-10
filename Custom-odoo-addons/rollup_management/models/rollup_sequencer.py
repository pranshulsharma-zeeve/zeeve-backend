"""Rollup sequencer model."""

from odoo import fields, models


class RollupSequencer(models.Model):
    """Catalog of sequencer providers available to rollup types."""

    _name = "rollup.sequencer"
    _description = "Rollup sequencer"
    _order = "name"
    _inherit = ["mail.thread", "mail.activity.mixin"]
    _rec_name = "name"

    name = fields.Char(string="Sequencer Name", required=True, index=True)
    logo = fields.Binary(string="Logo", attachment=True)
    description = fields.Text(string="Description")
    sa_active = fields.Boolean(string="Active", default=True)
    active = fields.Boolean(string="Active", default=True)
    coming_soon = fields.Boolean(string="Coming Soon", help="Flag providers that are not yet available for provisioning.")
    type_ids = fields.Many2many(
        "rollup.type",
        "rollup_type_sequencer_rel_m2m",
        "sequencer_id",
        "type_id",
        string="Rollup Types",
        help="Rollup types that settle on this sequencer.",
    )

    _sql_constraints = [
        (
            "rollup_sequencer_name_unique",
            "unique(name)",
            "Each sequencer must have a unique name.",
        )
    ]
