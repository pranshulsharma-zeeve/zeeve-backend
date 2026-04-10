"""Models for protocol configuration."""

from odoo import fields, models, api
import uuid

class ProtocolMaster(models.Model):
    """Protocol configuration master.

    This model stores supported blockchain protocols and metadata used
    for node deployments within the platform.
    """

    _name = "protocol.master"
    _description = "Protocol Master"

    name = fields.Char(string="Protocol Name", required=True)
    protocol_id = fields.Char(string="Protocol Id")
    image = fields.Image(string="Image", required=True)
    short_name = fields.Char(string="Short Name", required=True)
    token_symbol = fields.Char(string="Token Symbol", help="Display symbol for pricing output, e.g., ETH")
    price_coingecko_id = fields.Char(
        string="CoinGecko Asset ID",
        help="Identifier used to fetch live USD pricing from CoinGecko (e.g., ethereum, coreum)."
    )
    reward_decimals = fields.Integer(
        string="Reward Decimals",
        default=18,
        help="Number of decimals to remove from stored rewards before converting to tokens (e.g., 18 for wei)."
    )
    stake_decimals = fields.Integer(
        string="Stake Decimals",
        default=0,
        help="Number of decimals to remove from stored stake amounts before converting to tokens."
    )
    web_url = fields.Char(string="Web URL")
    web_url_testnet = fields.Char(string="Testnet Web URL")
    active = fields.Boolean(string="Enabled", default=True)
    admin_channel_id = fields.Many2one(
        "zeeve.admin.channel",
        string="Admin Notification Channel",
        help="Override admin recipients when notifying about this protocol.",
    )

    # Availability node types
    is_rpc = fields.Boolean(string="RPC")
    is_archive = fields.Boolean(string="Archive")
    is_validator = fields.Boolean(string="Validator")

    network_type_ids = fields.Many2many(
        "zeeve.network.type",
        "protocol_network_type_rel",
        "protocol_id",
        "network_type_id",
        string="Network Types",
        help="Network categories supported by this protocol.",
    )

    notes = fields.Text(string="Notes")
    network_apr = fields.Float(
        string="Network APR",
        digits=(6, 4),
        help="Current annualised percentage return for the network (e.g., 5.25 means 5.25%)."
    )

    # Product Template link
    product_tmpl_id = fields.Many2one(
        "product.template", string="Product Template", ondelete="cascade"
    )

    @api.model
    def create(self, vals):
        if not vals.get("protocol_id"):
            vals["protocol_id"] = str(uuid.uuid4())

        # auto create product template if not linked
        # if not vals.get("product_tmpl_id") and vals.get("name"):
        #     tmpl = self.env["product.template"].create({
        #         "name": vals["name"] + " Node",
        #         "type": "service",
        #         "sale_ok": True,
        #         "purchase_ok": False,
        #     })
        #     vals["product_tmpl_id"] = tmpl.id

        return super().create(vals)
