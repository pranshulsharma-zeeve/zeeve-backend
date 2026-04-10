"""Rollup settlement layer model."""

from odoo import fields, models


class RollupSettlementLayer(models.Model):
    """Catalog of settlement layers that rollup types can settle on."""

    _name = "rollup.settlement.layer"
    _description = "Rollup Settlement Layer"
    _order = "name"
    _inherit = ["mail.thread", "mail.activity.mixin"]
    _rec_name = "name"

    name = fields.Char(string="Layer Name", required=True, index=True)
    description = fields.Text(string="Description")
    active = fields.Boolean(string="Active", default=True)
    type_ids = fields.Many2many(
        "rollup.type",
        "rollup_type_settlement_layer_rel",
        "settlement_layer_id",
        "type_id",
        string="Rollup Types",
        help="Rollup types that settle on this layer.",
    )

    _sql_constraints = [
        (
            "rollup_settlement_layer_name_unique",
            "unique(name)",
            "Each settlement layer must have a unique name.",
        )
    ]
