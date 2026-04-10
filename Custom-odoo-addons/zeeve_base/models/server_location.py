"""Model for server geographic locations."""

from odoo import fields, models


class ServerLocation(models.Model):
    """Server location configuration.

    Stores physical locations of servers to assist with deployment
    preferences and reporting.
    """

    _name = "server.location"
    _description = "Server Location"
    _rec_name = "continent_id"

    name = fields.Char(string="Name", required=True)
    country_id = fields.Many2one(
        "res.country", string="Country", required=True
    )
    continent_id = fields.Many2one(
        "res.country.group",
        string="Continent"
    )
