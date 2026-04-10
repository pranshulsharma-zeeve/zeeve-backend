"""Rollup data availability provider model."""

from odoo import fields, models


class RollupDataAvailability(models.Model):
    """Catalog of data availability solutions that rollups can rely on."""

    _name = "rollup.data.availability"
    _description = "Rollup Data Availability Provider"
    _order = "name"
    _inherit = ["mail.thread", "mail.activity.mixin"]
    _rec_name = "name"

    name = fields.Char(string="Provider Name", required=True, index=True)
    logo = fields.Binary(string="Logo", attachment=True)
    active = fields.Boolean(string="Active", default=True)
    da_active = fields.Boolean(string="Active", default=True)
    coming_soon = fields.Boolean(string="Coming Soon", help="Flag providers that are not yet available for provisioning.")
    type_ids = fields.Many2many(
        "rollup.type",
        "rollup_type_data_availability_rel",
        "data_availability_id",
        "type_id",
        string="Rollup Types",
        help="Rollup types that leverage this data availability provider.",
    )

    _sql_constraints = [
        (
            "rollup_data_availability_name_unique",
            "unique(name)",
            "Each data availability provider must have a unique name.",
        )
    ]
