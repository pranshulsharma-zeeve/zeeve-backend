"""Rollup type definitions."""

import logging
import uuid
from decimal import Decimal, ROUND_HALF_UP

from odoo import _, api, fields, models
from odoo.exceptions import UserError

from ..utils import rollup_util

_logger = logging.getLogger(__name__)

class RollupType(models.Model):
    """Configuration model for supported rollup types."""

    _name = "rollup.type"
    _description = "Rollup Type"
    _inherit = ["mail.thread", "mail.activity.mixin"]
    _rec_name = "name"

    name = fields.Char(string="Name", required=True, index=True)
    image = fields.Image(string="Logo", required=True)

    description = fields.Text(string="Description")
    rollup_id = fields.Char(
        string="Rollup Identifier",
        default=lambda self: str(uuid.uuid4()),
        copy=False,
        required=True,
        index=True,
        readonly=True,
        help="Public UUID used when referencing the rollup type outside the database.",
    )
    related_product_id = fields.Many2one(
        "product.product",
        string="Related Product",
        ondelete="set null",
        help="Optional product that represents this rollup type in subscription flows.",
    )
    default_region_ids = fields.Many2many(
        "server.location",
        string="Default Regions",
        help="Regions suggested when deploying this rollup type.",
    )
    service_ids = fields.One2many(
        "rollup.service",
        "type_id",
        string="Services",
        readonly=True,
    )
    service_count = fields.Integer(
        string="Service Count",
        compute="_compute_service_count",
        readonly=True,
    )
    data_availability_ids = fields.Many2many(
        "rollup.data.availability",
        "rollup_type_data_availability_rel",
        "type_id",
        "data_availability_id",
        string="Data Availability Providers",
        help="Data availability committees or providers compatible with this rollup type.",
    )
    settlement_layer_ids = fields.Many2many(
        "rollup.settlement.layer",
        "rollup_type_settlement_layer_rel",
        "type_id",
        "settlement_layer_id",
        string="Settlement Layers",
        help="Settlement layers that this rollup type can anchor to.",
    )
    sequencer_ids = fields.Many2many(
        "rollup.sequencer",
        "rollup_type_sequencer_rel_m2m",
        "type_id",
        "sequencer_id",
        string="Sequencer Providers",
        help="Preferred sequencer providers for this rollup type.",
    )
    allow_custom_token = fields.Boolean(
        string="Allow Custom Token",
        help="Enable support for user defined gas/payment tokens during deployment.",
    )
    cost = fields.Float(
        string="Estimated Cost (Legacy)",
        help="High level cost indicator for deploying or running this rollup type.",
    )
    amount_month = fields.Float(string="Monthly Amount", help="Monthly cost for Odoo Managed Billing (V2).")
    amount_quarter = fields.Float(string="Quarterly Amount", help="Quarterly cost for Odoo Managed Billing (V2).")
    amount_year = fields.Float(string="Yearly Amount", help="Yearly cost for Odoo Managed Billing (V2).")

    payment_frequency = fields.Selection(
        selection=[
            ("day", "Daily"),
            ("week", "Weekly"),
            ("month", "Monthly"),
            ("year", "Yearly"),
        ],
        string="Payment Frequency",
        default="month",
        help="Recurring interval used when generating Stripe prices for this rollup type.",
    )
    docs = fields.Char(
        string="Documentation Link",
        help="Reference documentation for configuring and deploying this rollup type.",
    )
    stripe_product_id = fields.Char(
        string="Stripe Product",
        copy=False,
        index=True,
        help="Identifier of the Stripe product that represents this rollup type.",
        readonly=True,
    )
    stripe_price_id = fields.Char(
        string="Stripe Price",
        copy=False,
        help="Optional default Stripe price identifier for this rollup type.",
        readonly=True,
    )
    admin_channel_id = fields.Many2one(
        "zeeve.admin.channel",
        string="Admin Notification Channel",
        help="Recipients for ops notifications for this rollup type.",
    )

    _sql_constraints = [
        (
            "rollup_type_name_unique",
            "unique(name)",
            "Each rollup type must have a unique name.",
        )
    ]

    @api.depends("service_ids")
    def _compute_service_count(self):
        for rollup_type in self:
            rollup_type.service_count = len(rollup_type.service_ids)

    def _sync_with_stripe_product(self):
        """Create or refresh the associated Stripe product."""

        stripe_client = rollup_util.get_stripe_client()
        for rollup_type in self:
            metadata = {
                "odoo_rollup_type_id": str(rollup_type.id),
                "odoo_rollup_type_uuid": rollup_type.rollup_id,
            }

            try:
                amount = Decimal(str(rollup_type.cost or 0)).quantize(Decimal("0.01"))
                if amount <= 0:
                    raise UserError(_("Please configure a positive cost before syncing with Stripe."))

                interval = rollup_type.payment_frequency or "month"
                unit_amount = int((amount * 100).quantize(Decimal("1"), rounding=ROUND_HALF_UP))
                currency = (
                    rollup_type.env["ir.config_parameter"].sudo().get_param("stripe_currency", "usd").lower()
                )

                product = None
                if rollup_type.stripe_product_id:
                    try:
                        product = stripe_client.Product.retrieve(rollup_type.stripe_product_id)
                        product = stripe_client.Product.modify(
                            rollup_type.stripe_product_id,
                            name=rollup_type.name,
                            metadata=metadata,
                        )
                    except Exception as exc:  # pylint: disable=broad-except
                        error_code = getattr(exc, "code", "")
                        if error_code == "resource_missing":
                            product = None
                        else:
                            raise

                if not product:
                    product = stripe_client.Product.create(
                        name=rollup_type.name,
                        metadata=metadata,
                    )

                product_id = product.get("id") if isinstance(product, dict) else getattr(product, "id", None)

                price = None
                price_id = None
                if rollup_type.stripe_price_id:
                    try:
                        price = stripe_client.Price.retrieve(rollup_type.stripe_price_id)
                        price_id = price.get("id") if isinstance(price, dict) else getattr(price, "id", None)
                    except Exception as exc:  # pylint: disable=broad-except
                        error_code = getattr(exc, "code", "")
                        if error_code == "resource_missing":
                            price = None
                            price_id = None
                        else:
                            raise

                def _price_matches(stripe_price):
                    if not stripe_price:
                        return False
                    data = stripe_price if isinstance(stripe_price, dict) else stripe_price.to_dict()
                    if data.get("currency") != currency:
                        return False
                    if int(data.get("unit_amount", 0)) != unit_amount:
                        return False
                    recurring = data.get("recurring") or {}
                    return recurring.get("interval") == interval and data.get("product") == product_id

                if not _price_matches(price):
                    price = stripe_client.Price.create(
                        product=product_id,
                        unit_amount=unit_amount,
                        currency=currency,
                        recurring={"interval": interval},
                        metadata=metadata,
                    )
                    price_id = price.get("id") if isinstance(price, dict) else getattr(price, "id", None)
                    if product_id and price_id:
                        try:
                            stripe_client.Product.modify(product_id, default_price=price_id)
                        except Exception:  # noqa: BLE001 - Stripe may already reference this price
                            _logger.debug(
                                "Unable to set default price %s on product %s", price_id, product_id, exc_info=True
                            )

                updates = {
                    "stripe_product_id": product_id,
                    "stripe_price_id": price_id,
                }
                rollup_type.write(updates)
                _logger.info(
                    "Rollup type %s synchronised with Stripe product %s and price %s",
                    rollup_type.id,
                    product_id,
                    price_id,
                )
            except Exception as exc:  # pylint: disable=broad-except
                _logger.exception("Failed to sync rollup type %s with Stripe", rollup_type.id)
                message = getattr(exc, "user_message", None) or str(exc)
                raise UserError(_("Unable to sync this rollup type with Stripe: %s") % message) from exc

        return True

    def action_sync_with_stripe(self):
        """Button action to synchronise the Stripe product mapping."""

        self.ensure_one()
        return self._sync_with_stripe_product()
