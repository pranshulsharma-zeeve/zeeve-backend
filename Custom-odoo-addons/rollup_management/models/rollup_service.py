"""Rollup service model."""

from __future__ import annotations

import base64
import json
import logging
import uuid
from datetime import date as pydate, datetime, timedelta, timezone
UTC = timezone.utc
from typing import Any, Dict, Iterable
from datetime import date
from odoo import _, api, fields, models
from odoo.exceptions import UserError, ValidationError,AccessError

from ..utils import deployment_utils, rollup_util
from odoo.tools import float_is_zero, formatLang
from odoo.tools.safe_eval import safe_eval
from odoo.tools.misc import format_date, format_datetime
from ...zeeve_base.utils import base_utils

_logger = logging.getLogger(__name__)


class RollupService(models.Model):
    """Service record representing a deployed rollup."""

    _name = "rollup.service"
    _description = "Rollup Service"
    _order = "create_date desc"
    _inherit = ["mail.thread", "mail.activity.mixin"]
    _rec_name = "name"

    service_id = fields.Char(
        string="Service Identifier",
        default=lambda self: str(uuid.uuid4()),
        copy=False,
        required=True,
        index=True,
        readonly=True,
        help="Public UUID used when referencing the service outside the database.",
    )
    chain_id = fields.Char(
        string="Chain Identifier",
        index=True,
        help="Optional identifier for the on-chain deployment (e.g. Chain ID or network slug).",
    )
    name = fields.Char(string="Rollup Name", required=True)
    customer_id = fields.Many2one(
        "res.partner",
        string="Customer",
        required=True,
        ondelete="restrict",
    )
    type_id = fields.Many2one(
        "rollup.type",
        string="Rollup Type",
        required=True,
        ondelete="restrict",
    )
    company_id = fields.Many2one(
        'res.company', 
        string='Company', 
        required=True, 
        default=lambda self: self.env.company
    )
    region_ids = fields.Many2many(
        "server.location",
        string="Regions",
        help="Regions selected by the user for this deployment.",
    )
    status = fields.Selection(
        selection=[
            ("draft", "Draft"),
            ("deploying", "Deploying"),
            ("active", "Active"),
            ("overdue", "Overdue"),
            ("suspended", "Suspended"),
            ("cancelled", "Cancelled"),
            ("paused", "Paused"),
            ("failed", "Failed"),
            ("archived", "Archived"),
        ],
        string="Status",
        required=True,
        default="draft",
    )
    subscription_status = fields.Selection(
        selection=[
            ("draft", "Draft"),
            ("pending_payment", "Pending Payment"),
            ("active", "Active"),
            ("overdue", "Overdue"),
            ("suspended", "Suspended"),
            ("cancelled", "Cancelled"),
        ],
        string="Subscription Status",
        default="draft",
        tracking=True,
    )
    inputs_json = fields.Json(
        string="User Inputs",
        default=dict,
        help="Payload received from the frontend when the service was requested.",
    )
    artifacts = fields.One2many(
        "ir.attachment",
        "res_id",
        string="Deployment Artifacts",
        domain=[("res_model", "=", "rollup.service")],
        context={"default_res_model": "rollup.service"},
        tracking=True,
        help="Generated artefacts such as configuration files or deployment manifests stored as attachments.",
    )
    metadata_json = fields.Json(
        string="System Metadata",
        default=dict,
        copy=False,
        help="System generated metadata (Stripe identifiers, deployment details, etc.).",
        readonly=True,
    )
    node_ids = fields.One2many(
        "rollup.node",
        "service_id",
        string="Nodes",
    )
    node_count = fields.Integer(
        string="Node Count",
        compute="_compute_node_count",
        store=True,
        readonly=True,
    )
    payment_log_ids = fields.One2many(
        "stripe.payment.log",
        "rollup_service_id",
        string="Stripe Payment Logs",
    )
    payment_log_count = fields.Integer(
        string="Payment Log Count",
        compute="_compute_payment_log_count",
        readonly=True,
    )
    invoice_ids = fields.One2many(
        "account.move",
        "rollup_service_id",
        string="Invoices",
        readonly=True,
    )
    invoice_count = fields.Integer(
        string="Invoice Count",
        compute="_compute_invoice_count",
        readonly=True,
    )
    payment_ids = fields.One2many(
        "account.payment",
        "memo",
        string="Payments",
        readonly=True,
    )
    payment_count = fields.Integer(
        string="Payment Count",
        compute="_compute_payment_count",
        readonly=True,
    )
    stripe_subscription_id = fields.Char(string="Stripe Subscription", copy=False, index=True)
    stripe_customer_id = fields.Char(string="Stripe Customer", copy=False, index=True)
    stripe_start_date = fields.Datetime(string="Stripe Start Date", tracking=True)
    stripe_end_date = fields.Datetime(string="Next Billing Date", tracking=True)
    stripe_invoice_id = fields.Char(string="Stripe Invoice", copy=False, index=True)
    last_stripe_invoice_id = fields.Char(string="Last Stripe Invoice", copy=False, index=True)
    autopay_enabled = fields.Boolean(string="Autopay Enabled", default=True, tracking=True)
    stripe_session_id = fields.Char(
        string="Stripe Checkout Session",
        copy=False,
        index=True,
        readonly=True,
        help="Identifier of the Stripe checkout session associated with this deployment.",
    )
    stripe_payment_intent_id = fields.Char(
        string="Stripe Payment Intent",
        copy=False,
        index=True,
        readonly=True,
        help="Identifier of the Stripe payment intent linked to the checkout session.",
    )
    discount_id = fields.Many2one(
        "subscription.discount",
        string="Applied Discount",
        copy=False,
        readonly=True,
    )
    discount_code = fields.Char(string="Discount Code", copy=False, readonly=True)
    discount_amount = fields.Float(string="Discount Amount", copy=False, readonly=True)
    original_amount = fields.Float(string="Original Amount", copy=False, readonly=True)
    deployment_token = fields.Char(
        string="Deployment Token",
        copy=False,
        index=True,
        readonly=True,
        help="Token provided by the frontend to correlate deployment callbacks.",
    )
    inputs_json_pretty = fields.Text(
        string="User Inputs (JSON)",
        compute="_compute_inputs_json_pretty",
        inverse="_inverse_inputs_json_pretty",
        store=True,
        tracking=True,
    )
    metadata_json_pretty = fields.Text(
        string="Metadata (JSON)",
        compute="_compute_metadata_json_pretty",
        readonly=True,
        tracking=True,
    )
    hosted_invoice_url = fields.Char(
        string='Stripe Hosted Invoice URL',
        help='Direct payment link for failed Stripe invoices',
        copy=False,
    )
    rollup_metadata = fields.Text(
        string="Rollup Metadata",
        help="Metadata related to Rollup deployment",
        store=True,
        tracking=True)

    _sql_constraints = [
        ("rollup_service_service_id_unique", "unique(service_id)", "Service identifier must be unique."),
        (
            "rollup_service_stripe_session_unique",
            "unique(stripe_session_id)",
            "Stripe checkout session must be unique.",
        ),
    ]
    next_billing_date = fields.Date(
        string="Next Billing Date",
        help="Next renewal date reported by Stripe for the linked subscription.",
        readonly=True,
    )
    service_created_date = fields.Datetime(
        string="Service Created Date",
        store=True,
        tracking=True,
        readonly=False
    )
    zoho_service_id = fields.Char(
        string="Zoho Service ID",
        index=True,)
    
    # Odoo Managed Billing Fields
    is_odoo_managed = fields.Boolean(string="Is Odoo Managed", default=False, tracking=True)
    payment_vault_id = fields.Many2one('stripe.payment.method', string="Payment Method Vault", tracking=True)
    last_charge_date = fields.Datetime(string="Last Charge Date", tracking=True)
    charge_retry_count = fields.Integer(string="Charge Retry Count", default=0, tracking=True)

    # Proration / Quantity Increase Fields
    quantity = fields.Float(string='Quantity', default=1.0, tracking=True)

    @api.model_create_multi
    def create(self, vals_list):
        for vals in vals_list:
            vals.setdefault("service_id", str(uuid.uuid4()))
        records = super().create(vals_list)
        # Set service_created_date to create_date
        for record in records:
            if record.create_date and not record.service_created_date:
                record.write({"service_created_date": record.create_date})
        # context_auto_send = self.env.context.get("rollup_auto_send_invoice", True)
        # if isinstance(context_auto_send, str):
        #     auto_send = context_auto_send.lower() not in {"false", "0", "no"}
        # else:
        #     auto_send = bool(context_auto_send)
        # records._ensure_initial_invoice(auto_send=auto_send)
        return records

    def write(self, vals):
        previous_subscription_statuses = {service.id: service.subscription_status for service in self}
        previous_statuses = {service.id: service.status for service in self}
        res = super().write(vals)
        if 'subscription_status' in vals or 'status' in vals:
            active_services = self.filtered(
                lambda service: (
                    service.subscription_status == 'active'
                    and previous_subscription_statuses.get(service.id) != 'active'
                )
            )
            for service in active_services:
                customer = service.customer_id
                if not customer:
                    continue
                self.env['zeeve.notification'].sudo().notify_partner(
                    customer,
                    notification_type='rollup_active',
                    title='Rollup active',
                    message='%s is now active.' % (
                        service.type_id.display_name or service.name or 'Your rollup service'
                    ),
                    category='success',
                    payload={
                        'rollup_service_id': service.id,
                        'service_id': service.service_id or '',
                        'rollup_name': service.name or '',
                        'subscription_status': service.subscription_status or '',
                        'status': service.status or '',
                    },
                    action_url='/rollups',
                    reference_model='rollup.service',
                    reference_id=service.id,
                    dedupe_key='rollup_active:%s:%s' % (service.id, service.write_date or ''),
                )
            cancelled_services = self.filtered(
                lambda service: (
                    service.subscription_status == 'cancelled'
                    and previous_subscription_statuses.get(service.id) != 'cancelled'
                ) or (
                    service.status == 'cancelled'
                    and previous_statuses.get(service.id) != 'cancelled'
                )
            )
            for service in cancelled_services:
                customer = service.customer_id
                if not customer:
                    continue
                self.env['zeeve.notification'].sudo().notify_partner(
                    customer,
                    notification_type='rollup_cancelled',
                    title='Rollup cancelled',
                    message='%s has been cancelled successfully.' % (
                        service.type_id.display_name or service.name or 'Your rollup service'
                    ),
                    category='info',
                    payload={
                        'rollup_service_id': service.id,
                        'service_id': service.service_id or '',
                        'rollup_name': service.name or '',
                        'subscription_status': service.subscription_status or '',
                        'status': service.status or '',
                    },
                    action_url='/rollups',
                    reference_model='rollup.service',
                    reference_id=service.id,
                    dedupe_key='rollup_cancelled:%s:%s' % (service.id, service.write_date or ''),
                )
        return res

    def init(self):
        """Backfill service_created_date for legacy records."""

        super().init()
        self.env.cr.execute(
            """
            UPDATE rollup_service
               SET service_created_date = create_date
             WHERE service_created_date IS NULL
               AND create_date IS NOT NULL
            """
        )

    def _anchor_local_date(self):
        """Return create_date as a local date (company/user tz) for 'monthiversary' anchoring."""
        self.ensure_one()
        if not self.create_date:
            return None
        # Convert create_date to the current environment timezone, then take the date part
        dt_local = fields.Datetime.context_timestamp(self, self.create_date)
        return dt_local.date()

    def _map_stripe_subscription_status(self, stripe_status):
        """Return the closest internal state for a Stripe subscription status."""
        self.ensure_one()
        mapping = {
            "active": "active",
            "trialing": "active",
            "past_due": "overdue",
            "unpaid": "overdue",
            "canceled": "cancelled",
            "incomplete": "pending_payment",
            "incomplete_expired": "pending_payment",
            "paused": "suspended",
        }
        return mapping.get(stripe_status, self.subscription_status or "pending_payment")

    def _apply_stripe_subscription_payload(self, subscription: Dict[str, Any] | None) -> bool:
        """Persist subscription status and billing data from a Stripe payload."""

        if not subscription:
            return False
        changed = False
        for service in self:
            updates: Dict[str, Any] = {}

            stripe_status = subscription.get("status")
            if stripe_status:
                mapped_status = service._map_stripe_subscription_status(stripe_status)
                if mapped_status and mapped_status != service.subscription_status:
                    updates["subscription_status"] = mapped_status

            subscription_item = subscription['items']['data'][0]
            period_end = subscription_item['current_period_end']

            period_start = subscription_item.get('current_period_start')
            period_end = subscription_item.get('current_period_end')

            if period_start:
                try:
                    start_date = datetime.fromtimestamp(period_start, tz=timezone.utc)
                except (TypeError, ValueError, OSError):
                    start_date = None
                if start_date and start_date != service.stripe_start_date:
                    updates["stripe_start_date"] = start_date

            if period_end:
                try:
                    next_date = datetime.fromtimestamp(period_end, tz=timezone.utc)
                except (TypeError, ValueError, OSError):
                    next_date = None
                if next_date and next_date != service.stripe_end_date:
                    updates["stripe_end_date"] = next_date
                if next_date and next_date != service.next_billing_date:
                    updates["next_billing_date"] = next_date

            subscription_id = subscription.get("id")
            if subscription_id and service.stripe_subscription_id != subscription_id:
                updates["stripe_subscription_id"] = subscription_id

            customer_id = subscription.get("customer")
            if customer_id and service.stripe_customer_id != customer_id:
                updates["stripe_customer_id"] = customer_id

            if updates:
                service.write(updates)
                rollup_util._store_partner_stripe_customer(self,updates.get("stripe_customer_id") or customer_id)
                changed = True
                _logger.info(
                    "Service %s synced from Stripe subscription %s | updates=%s",
                    service.id,
                    subscription_id or service.stripe_subscription_id,
                    updates,
                )

        return changed

    def _sync_with_stripe(self):
        """Fetch subscription details from Stripe and update billing metadata."""
        stripe_client = rollup_util.get_stripe_client()
        for service in self:
            if not service.stripe_subscription_id:
                raise UserError(
                    _("Service %s is not linked to a Stripe subscription.") % service.display_name
                )

            try:
                subscription = stripe_client.Subscription.retrieve(service.stripe_subscription_id)

            except Exception as exc:  # pylint: disable=broad-except
                _logger.exception(
                    "Unable to fetch Stripe subscription %s for service %s",
                    service.stripe_subscription_id,
                    service.id,
                )
                message = getattr(exc, "user_message", None) or str(exc)
                raise UserError(_("Unable to fetch the subscription from Stripe: %s") % message) from exc

            self._apply_stripe_subscription_payload(subscription)

        return True

    def action_sync_with_stripe(self):
        """UI handler to synchronise billing data with Stripe on demand."""
        self.ensure_one()
        return self._sync_with_stripe()

    @api.constrains("service_id")
    def _check_service_id_uuid(self):
        """Ensure that the public identifier is a valid UUID4 string."""

        for service in self:
            try:
                uuid_obj = uuid.UUID(str(service.service_id), version=4)
            except (ValueError, AttributeError, TypeError):
                raise ValidationError("Service ID must be a valid UUID4 value.")
            if str(uuid_obj) != service.service_id:
                raise ValidationError("Service ID must be a canonical UUID4 value.")

    @api.depends("node_ids")
    def _compute_node_count(self):
        for service in self:
            service.node_count = len(service.node_ids)

    def _compute_payment_log_count(self):
        for service in self:
            service.payment_log_count = len(service.payment_log_ids)

    def _compute_invoice_count(self):
        for service in self:
            service.invoice_count = len(service.invoice_ids)

    def _compute_payment_count(self):
        Payment = self.env['account.payment']
        for service in self:
            domain = [('rollup_service_id', '=', service.id)]
            if service.service_id:
                domain = ['|', ('rollup_service_id', '=', service.id), ('memo', '=', service.service_id)]
            service.payment_count = Payment.search_count(domain)

    @api.depends("inputs_json")
    def _compute_inputs_json_pretty(self):
        for service in self:
            service.inputs_json_pretty = service._format_json_value(service.inputs_json)

    def _inverse_inputs_json_pretty(self):
        for service in self:
            payload = service.inputs_json_pretty or "{}"
            if not payload.strip():
                service.inputs_json = {}
                continue
            try:
                parsed = json.loads(payload)
            except json.JSONDecodeError as exc:
                raise ValidationError(_("User Inputs JSON is invalid: %(error)s", error=str(exc))) from exc
            if not isinstance(parsed, dict):
                raise ValidationError(_("User Inputs JSON must be a JSON object."))
            service.inputs_json = parsed

    @api.depends("metadata_json")
    def _compute_metadata_json_pretty(self):
        for service in self:
            service.metadata_json_pretty = service._format_json_value(service.metadata_json)

    def _format_json_value(self, value):
        try:
            return json.dumps(value, indent=2, sort_keys=True) if value else "{}"
        except (TypeError, ValueError):
            return str(value or "{}")

    @api.onchange("type_id")
    def _onchange_type_id(self):
        if self.type_id and self.type_id.default_region_ids:
            self.region_ids = [(6, 0, self.type_id.default_region_ids.ids)]

    # ------------------------------------------------------------------
    # Accounting helpers
    # ------------------------------------------------------------------
    def _combined_metadata(self, metadata: Dict[str, Any]):
            """Return merged metadata dictionary without mutating current value."""

            current = dict(self.metadata_json or {})
            current.update(metadata or {})
            return current
    def _get_currency(self, currency_code: str | None = None):
        """Return the currency used for rollup invoices and payments."""

        Currency = self.env["res.currency"].sudo()
        if currency_code:
            currency = Currency.search([("name", "=", currency_code.upper())], limit=1)
            if currency:
                return currency
        usd = self.env.ref("base.USD", raise_if_not_found=False)
        if usd:
            return usd
        currency = Currency.search([("name", "=", "USD")], limit=1)
        if currency:
            return currency
        return self.env.company.currency_id

    def _get_stripe_payment_method_line(self, journal):
        """Return (or create) the payment method line representing Stripe."""

        PaymentMethodLine = self.env["account.payment.method.line"].sudo()
        if journal:
            payment_method_line = PaymentMethodLine.search(
                [
                    ("journal_id", "=", journal.id),
                    ("company_id", "=", self.env.company.id),
                    ("name", "ilike", "Stripe"),
                ],
                limit=1,
            )
        else:
            payment_method_line = PaymentMethodLine.search(
                [
                    ("company_id", "=", self.env.company.id),
                    ("name", "ilike", "Stripe"),
                ],
                limit=1,
            )
        if payment_method_line:
            return payment_method_line

        if not journal:
            return PaymentMethodLine.browse()

        manual_method = journal.inbound_payment_method_line_ids.mapped("payment_method_id")
        manual_method = manual_method.filtered(lambda method: method.code == "manual")[:1]
        if not manual_method:
            manual_method = self.env["account.payment.method"].sudo().search(
                [("code", "=", "manual"), ("payment_type", "=", "inbound")],
                limit=1,
            )
        if not manual_method:
            return PaymentMethodLine.browse()

        existing_line = PaymentMethodLine.search(
            [
                ("journal_id", "=", journal.id),
                ("payment_method_id", "=", manual_method.id),
                ("company_id", "=", self.env.company.id),
            ],
            limit=1,
        )
        if existing_line:
            if "stripe" not in (existing_line.name or "").lower():
                existing_line.write({"name": "Stripe"})
            return existing_line

        return PaymentMethodLine.create(
            {
                "name": "Stripe",
                "payment_method_id": manual_method.id,
                "journal_id": journal.id,
                "company_id": self.env.company.id,
            }
        )

    def _create_invoice_from_amount(
        self,
        amount: float,
        currency_code: str | None = None,
        invoice_date: Any | None = None,
        due_date: Any | None = None,
        stripe_invoice_id: str | None = None,
        invoice_number: str | None = None,
    ):
        """Create and post an invoice for the provided amount.

        Called when the recurring payment webhook (``invoice.payment_succeeded``)
        fires so that each Stripe charge has a matching `account.move` in Odoo.
        """

        self.ensure_one()
        invoice_vals = self._prepare_invoice_vals()
        currency = self._get_currency(currency_code)
        invoice_vals["currency_id"] = currency.id

        line_commands = []
        for _cmd, _unused, line_vals in invoice_vals.get("invoice_line_ids", []):
            line_dict = dict(line_vals)
            line_dict.update({"price_unit": amount, "quantity": line_dict.get("quantity", 1.0) or 1.0})
            line_commands.append((0, 0, line_dict))
        invoice_vals["invoice_line_ids"] = line_commands

        if invoice_date:
            invoice_vals["invoice_date"] = invoice_date
        if due_date:
            invoice_vals["invoice_date_due"] = due_date
        if invoice_number:
            invoice_vals.setdefault("ref", invoice_number)

        invoice = self.env["account.move"].sudo().create(invoice_vals)

        write_vals = {"rollup_service_id": self.id}
        if stripe_invoice_id:
            write_vals["stripe_invoice_id"] = stripe_invoice_id
        invoice.sudo().write(write_vals)
        return invoice
    def unlink(self):
        # Allow ONLY Admin (group_system)
        if self.env.user.has_group("access_rights.group_technical_manager"):

            raise AccessError(_("Only Admin users can delete Rollup Service records."))
        else:
            pass

        return super().unlink()
    def _ensure_initial_invoice(self, auto_send: bool = False):
        """Create the initial invoice for the service if it does not exist.

        Executed during checkout initialisation (`start_checkout`) and whenever
        the webhook needs to reconcile payments so every service has at least
        one invoice to attach Stripe transactions to.
        """

        invoices = self.env['account.move']
        created = self.env['account.move'].browse()
        for service in self:
            if service.invoice_ids:
                invoices |= service.invoice_ids.sorted(key=lambda inv: inv.invoice_date or inv.create_date or inv.id)[-1]
                continue
            invoice = service._create_invoice_from_amount(service.type_id.cost or 0.0)
            service.write(
                {
                    "metadata_json": service._combined_metadata(
                        {
                            "rollup_invoice_odoo_id": invoice.id,
                            "rollup_invoice_number": invoice.name,
                            "rollup_invoice_created_at": fields.Datetime.now().isoformat(),
                        }
                    )
                }
            )
            if service.subscription_status != 'active':
                deployment_utils.update_subscription_status(
                    service,
                    "pending_payment",
                    reason="initial_invoice_generated",
                )
            invoices |= invoice
            created |= invoice
        if auto_send:
            for invoice in created:
                invoice.rollup_service_id._send_invoice_email(invoice, force_send=True)
        return invoices

    def _prepare_invoice_vals(self):
        """Return invoice creation values for the current service."""

        self.ensure_one()
        product = self._get_invoice_product()
        journal = self.env['account.journal'].sudo().search([
            ('type', '=', 'sale'),
            ('company_id', '=', self.env.company.id),
        ], limit=1)
        currency = self._get_currency()

        original_amount = self.original_amount or self.type_id.cost or 0.0
        discount_amount = self.discount_amount or 0.0
        final_amount = max(original_amount - discount_amount, 0.0)

        line_vals = {
            'name': f"{self.name} - {self.type_id.name}",
            'quantity': 1.0,
            'price_unit': final_amount,
        }
        if self.discount_id:
            line_vals['discount_id'] = self.discount_id.id
            line_vals['discount_code'] = self.discount_code or self.discount_id.code
        if product:
            line_vals['product_id'] = product.id
            line_vals['product_uom_id'] = product.uom_id.id
            taxes = product.taxes_id.filtered(lambda tax: tax.company_id == self.env.company)
            line_vals['tax_ids'] = [(6, 0, taxes.ids)]
        else:
            line_vals['product_uom_id'] = self.env.ref('uom.product_uom_unit').id

        invoice_vals = {
            'move_type': 'out_invoice',
            'partner_id': self.customer_id.id,
            'invoice_origin': self.service_id,
            'invoice_line_ids': [(0, 0, line_vals)],
            'currency_id': currency.id,
            'invoice_user_id': self.env.user.id,
            'payment_reference': self.service_id,
            'rollup_service_id': self.id,
            'company_id': self.env.company.id,
        }
        if journal:
            invoice_vals['journal_id'] = journal.id
        return invoice_vals

    def _get_invoice_product(self):
        """Return the product used for invoicing rollup services."""

        self.ensure_one()
        product = self.type_id.related_product_id
        if product:
            return product
        product = self.env['product.product'].sudo().search([('default_code', '=', 'ROLLUP-SERVICE')], limit=1)
        if product:
            return product
        template = self.env['product.template'].sudo().create({
            'name': _('Rollup Service Subscription'),
            'type': 'service',
            'default_code': 'ROLLUP-SERVICE',
            'list_price': self.type_id.cost or 0.0,
            'sale_ok': True,
            'purchase_ok': False,
        })
        product = template.product_variant_id
        if product and not self.type_id.related_product_id:
            self.type_id.write({'related_product_id': product.id})
        return product

    def _prepare_invoice_email_context(self, invoice, payment=None, extra_context=None):
        """Build the email rendering context shared by invoice templates."""

        self.ensure_one()
        extra_context = dict(extra_context or {})
        currency = invoice.currency_id or self.env.company.currency_id
        metadata = dict(self.metadata_json or {})
        amount_display = formatLang(self.env, invoice.amount_total, currency_obj=currency)
        due_date = invoice.invoice_date_due or invoice.invoice_date
        invoice_url = (
            extra_context.get('default_invoice_url')
            or metadata.get('stripe_invoice_url')
            or invoice.get_portal_url()
        )
        payment_reference = payment.transaction_hash if payment else False
        payment_amount = amount_display if payment else False
        checkout_url = extra_context.get('default_checkout_url') or metadata.get('stripe_checkout_url')
        if invoice.payment_state == 'paid':
            checkout_url = False

        extra_context.update({
            'default_invoice_id': invoice.id,
            'default_invoice_number': invoice.name or invoice.display_name,
            'default_invoice_amount': amount_display,
            'default_invoice_residual': invoice.amount_residual,
            'default_invoice_currency': currency.name,
            'default_invoice_url': invoice_url,
            'default_invoice_due_date': due_date,
            'default_payment_state': invoice.payment_state,
            'default_payment_reference': payment_reference,
            'default_payment_amount': payment_amount,
            'default_payment_id': payment.id if payment else False,
            'default_checkout_url': checkout_url,
        })
        return extra_context

    def _prepare_deployment_mail_context(self, invoice=None, payment=None, invoice_url: str | None = None):
        """Prepare context values used when rendering deployment notifications."""

        self.ensure_one()
        invoice = invoice.sudo() if invoice else invoice
        payment = payment.sudo() if payment else payment

        base_context: Dict[str, Any] = {}
        if invoice:
            base_context = self._prepare_invoice_email_context(
                invoice,
                payment,
                {"default_invoice_url": invoice_url} if invoice_url else None,
            )
        elif invoice_url:
            base_context = {"default_invoice_url": invoice_url}

        company = self.env.company.sudo()
        due_date_value = base_context.get("default_invoice_due_date")
        due_date_display: str | bool = False
        if due_date_value:
            if isinstance(due_date_value, str):
                due_date_display = due_date_value
            else:
                try:
                    due_date_display = format_date(
                        self.env,
                        due_date_value,
                        lang_code=self.customer_id.lang or self.env.lang,
                    )
                except Exception:  # pylint: disable=broad-except
                    if hasattr(fields.Date, "to_string"):
                        try:
                            due_date_display = fields.Date.to_string(due_date_value)
                        except Exception:  # pylint: disable=broad-except
                            due_date_display = str(due_date_value)
                    else:
                        due_date_display = str(due_date_value)

        region_names = [name for name in self.region_ids.mapped("name") if name]

        company_logo = company.logo or b""
        if isinstance(company_logo, bytes):
            try:
                company_logo = company_logo.decode()
            except Exception:  # pylint: disable=broad-except
                company_logo = base64.b64encode(company_logo).decode()

        deployment_values = {
            "service_name": self.name,
            "service_identifier": self.service_id,
            "service_status": self.status,
            "rollup_type_name": self.type_id.display_name or "",
            "rollup_type_code": self.type_id.rollup_id or "",
            "customer_name": self.customer_id.display_name or "",
            "customer_email": self.customer_id.email or "",
            "company_name": company.name or "Zeeve",
            "company_email": company.email or "support@zeeve.io",
            "company_logo": company_logo,
            "company_website": company.website or "https://www.zeeve.io",
            "region_names": region_names,
            "stripe_subscription_id": self.stripe_subscription_id or "",
            "invoice_number": base_context.get("default_invoice_number"),
            "invoice_amount": base_context.get("default_invoice_amount"),
            "invoice_due": due_date_display,
            "invoice_url": base_context.get("default_invoice_url"),
            "checkout_url": base_context.get("default_checkout_url"),
            "payment_reference": base_context.get("default_payment_reference"),
            "payment_amount": base_context.get("default_payment_amount")
            or base_context.get("default_invoice_amount"),
            "payment_state": base_context.get("default_payment_state"),
            "invoice_currency": base_context.get("default_invoice_currency"),
        }

        base_context.setdefault("default_invoice_url", invoice_url or deployment_values["invoice_url"])
        base_context["rollup_deploy_mail_context"] = deployment_values
        return base_context

    def _prepare_rollup_subscription_mail_context(self, extra_values: Dict[str, Any] | None = None):
        """Build a safe context for rollup subscription notification templates."""

        self.ensure_one()
        extra_values = dict(extra_values or {})

        metadata = self.metadata_json if isinstance(self.metadata_json, dict) else {}
        invoice_payload: Dict[str, Any] = {}
        candidate_payload = extra_values.get("invoice_payload")
        if isinstance(candidate_payload, dict):
            invoice_payload = candidate_payload

        inputs = self.inputs_json if isinstance(self.inputs_json, dict) else {}
        invoice_metadata = invoice_payload.get("metadata") if isinstance(invoice_payload.get("metadata"), dict) else {}
        stripe_discount_context = rollup_util.resolve_stripe_discount(invoice_payload)
        discount_ctx_invoice = rollup_util.combine_discount_contexts(
            rollup_util._extract_discount_context(invoice_metadata),
            stripe_discount_context,
        )

        discount_field_context = {
            "record": self.discount_id or self.env["subscription.discount"].browse(),
            "code": self.discount_code,
            "amount": self.discount_amount,
            "original_amount": self.original_amount,
        }
        discount_ctx_service = rollup_util.combine_discount_contexts(
            rollup_util._extract_discount_context(metadata),
            discount_field_context,
            stripe_discount_context,
        )

        def _merge_dict(base: Dict[str, Any], candidate: Any):
            if isinstance(candidate, dict):
                for key, value in candidate.items():
                    if key not in base or base[key] in (None, "", False):
                        base[key] = value

        subscription_details: Dict[str, Any] = {}
        for candidate in (
            extra_values.get("subscription_details"),
            inputs.get("subscription_details") if isinstance(inputs, dict) else {},
            inputs.get("subscriptionDetails") if isinstance(inputs, dict) else {},
            metadata.get("subscription_details") if isinstance(metadata, dict) else {},
            metadata.get("subscriptionDetails") if isinstance(metadata, dict) else {},
        ):
            _merge_dict(subscription_details, candidate)

        rollup_status: Dict[str, Any] = {}
        for candidate in (
            extra_values.get("rollup_status"),
            metadata.get("rollup_status") if isinstance(metadata, dict) else {},
            metadata.get("rollupStatus") if isinstance(metadata, dict) else {},
            subscription_details.get("rollup_status") if isinstance(subscription_details.get("rollup_status"), dict) else {},
            subscription_details.get("rollupStatus") if isinstance(subscription_details.get("rollupStatus"), dict) else {},
        ):
            _merge_dict(rollup_status, candidate)

        def _first_value(*values):
            for value in values:
                if isinstance(value, str):
                    if value.strip():
                        return value.strip()
                elif value not in (None, False, ""):
                    return value
            return ""

        def _format_date_value(value):
            if not value and value not in (0, 0.0):
                return ""
            raw_value = value
            if isinstance(raw_value, (int, float)):
                try:
                    raw_value = datetime.utcfromtimestamp(raw_value)
                except (ValueError, TypeError, OSError):
                    return str(value)
            if isinstance(raw_value, datetime):
                raw_value = raw_value.date()
            if isinstance(raw_value, pydate):
                try:
                    return format_date(
                        self.env,
                        raw_value,
                        lang_code=self.customer_id.lang or self.env.lang,
                    )
                except Exception:  # pylint: disable=broad-except
                    if hasattr(fields.Date, "to_string"):
                        try:
                            return fields.Date.to_string(raw_value)
                        except Exception:  # pylint: disable=broad-except
                            return raw_value.isoformat()
                    return raw_value.isoformat()
            return str(raw_value)

        company = self.env.company.sudo()
        company_logo = extra_values.get("company_logo") or company.logo or b""
        if isinstance(company_logo, bytes):
            try:
                company_logo = company_logo.decode()
            except Exception:  # pylint: disable=broad-except
                company_logo = base64.b64encode(company_logo).decode()

        plan_name = _first_value(
            extra_values.get("plan_name"),
            subscription_details.get("plan_name"),
            subscription_details.get("planName"),
            metadata.get("plan_name") if isinstance(metadata, dict) else "",
            metadata.get("planName") if isinstance(metadata, dict) else "",
            self.type_id.display_name,
            self.type_id.name,
            "Rollup",
        )

        protocol_name = _first_value(
            extra_values.get("protocol_name"),
            subscription_details.get("protocol_name"),
            subscription_details.get("protocolName"),
            metadata.get("protocol_name") if isinstance(metadata, dict) else "",
            metadata.get("protocolName") if isinstance(metadata, dict) else "",
            self.type_id.display_name,
            self.type_id.name,
            "Rollup",
        )

        network_name = _first_value(
            extra_values.get("network_name"),
            subscription_details.get("network_name"),
            subscription_details.get("networkName"),
            metadata.get("network_name") if isinstance(metadata, dict) else "",
            metadata.get("networkName") if isinstance(metadata, dict) else "",
            self.chain_id,
            self.name,
        )

        subscription_start = _format_date_value(
            _first_value(
                extra_values.get("subscription_start"),
                subscription_details.get("create_date"),
                subscription_details.get("startDate"),
                metadata.get("subscription_start") if isinstance(metadata, dict) else "",
                metadata.get("start_date") if isinstance(metadata, dict) else "",
            )
        )
        subscription_end = _format_date_value(
            _first_value(
                extra_values.get("subscription_end"),
                subscription_details.get("end_date"),
                subscription_details.get("endDate"),
                metadata.get("subscription_end") if isinstance(metadata, dict) else "",
                metadata.get("end_date") if isinstance(metadata, dict) else "",
            )
        )
        renewal_date = _format_date_value(
            _first_value(
                subscription_details.get("next_billing_date"),
                extra_values.get("renewal_date"),
                subscription_details.get("renewalDate"),
                metadata.get("renewal_date") if isinstance(metadata, dict) else "",
            )
        )

        eta = _first_value(
            extra_values.get("eta"),
            rollup_status.get("eta"),
            metadata.get("provisioning_eta") if isinstance(metadata, dict) else "",
            metadata.get("eta") if isinstance(metadata, dict) else "",
        ) or "Soon"

        status_text = _first_value(
            extra_values.get("status_text"),
            rollup_status.get("status_text"),
            rollup_status.get("text"),
            rollup_status.get("status"),
        ) or "Provisioning (We will email you once the Rollup is up and running)"

        dashboard_url = _first_value(
            extra_values.get("dashboard_url"),
            metadata.get("dashboard_url") if isinstance(metadata, dict) else "",
            metadata.get("portal_dashboard_url") if isinstance(metadata, dict) else "",
            "https://app.zeeve.io/arbitrum-orbit",
        )

        docs_url = _first_value(
            extra_values.get("docs_url"),
            metadata.get("docs_url") if isinstance(metadata, dict) else "",
            "https://www.zeeve.io/docs",
        )

        support_email = _first_value(
            extra_values.get("support_email"),
            company.email,
            "support@zeeve.io",
        )

        support_url = _first_value(
            extra_values.get("support_url"),
            metadata.get("support_url") if isinstance(metadata, dict) else "",
            "https://www.zeeve.io/talk-to-an-expert/",
        )

        customer = self.customer_id.sudo()
        customer_name = _first_value(extra_values.get("customer_name"), customer.display_name)
        customer_firstname = _first_value(
            extra_values.get("customer_firstname"),
            getattr(customer, "firstname", False),
            (customer.name.split(" ")[0] if customer.name and " " in customer.name else customer.name),
            customer.email,
            "there",
        )
        customer_lastname = _first_value(
            extra_values.get("customer_lastname"),
            getattr(customer, "lastname", False),
            (" ".join(customer.name.split(" ")[1:]) if customer.name and " " in customer.name else ""),
        )
        customer_email = _first_value(extra_values.get("customer_email"), customer.email)
        customer_company = _first_value(extra_values.get("customer_company"), customer.parent_id.name if customer.parent_id else "")

        environment_name = _first_value(
            extra_values.get("environment_name"),
            metadata.get("environment_name") if isinstance(metadata, dict) else "",
            metadata.get("environment") if isinstance(metadata, dict) else "",
            network_name,
        )

        stripe_subscription = _first_value(
            extra_values.get("stripe_subscription_id"),
            metadata.get("stripe_subscription_id") if isinstance(metadata, dict) else "",
            self.stripe_subscription_id,
        )

        admin_emails_value = extra_values.get("admin_emails") or ""
        if isinstance(admin_emails_value, (list, tuple)):
            admin_emails_value = ",".join([email for email in admin_emails_value if email])

        email_to = _first_value(
            extra_values.get("email_to"),
            customer_email,
        )

        context = {
            "company_name": extra_values.get("company_name") or company.name or "Zeeve",
            "company_logo": company_logo,
            "company_email": extra_values.get("company_email") or company.email or "support@zeeve.io",
            "company_website": extra_values.get("company_website") or company.website or "https://www.zeeve.io",
            "service_name": extra_values.get("service_name") or self.name,
            "service_identifier": extra_values.get("service_identifier") or self.service_id,
            "service_status": extra_values.get("service_status") or self.status,
            "plan_name": plan_name,
            "protocol_name": protocol_name,
            "network_name": network_name,
            "environment_name": environment_name,
            "subscription_start": subscription_start,
            "subscription_end": subscription_end,
            "renewal_date": renewal_date,
            "status_text": status_text,
            "eta": eta,
            "dashboard_url": dashboard_url,
            "docs_url": docs_url,
            "support_email": support_email,
            "support_url": support_url,
            "customer_name": customer_name,
            "customer_firstname": customer_firstname,
            "customer_lastname": customer_lastname,
            "customer_email": customer_email,
            "customer_company": customer_company,
            "signature_name": extra_values.get("signature_name") or "Yuvraj Singh Negi",
            "signature_title": extra_values.get("signature_title") or "Lead, Customer Success",
            "admin_emails": admin_emails_value,
            "email_to": email_to,
            "stripe_subscription_id": stripe_subscription,
            "subscription_details": subscription_details,
            "rollup_status": rollup_status,
        }

        discount_code_value = discount_ctx_invoice.get("code") or discount_ctx_service.get("code")
        discount_amount_value = discount_ctx_invoice.get("amount") or discount_ctx_service.get("amount") or 0.0
        original_amount_value = discount_ctx_invoice.get("original_amount")
        if original_amount_value is None:
            original_amount_value = discount_ctx_service.get("original_amount")
        final_amount_value = None
        try:
            if original_amount_value not in (None, "") and discount_amount_value not in (None, ""):
                final_amount_value = max(float(original_amount_value) - float(discount_amount_value), 0.0)
        except (TypeError, ValueError):
            final_amount_value = None

        context.update(
            {
                "discount_code": discount_code_value,
                "discount_amount": discount_amount_value,
                "discount_original_amount": original_amount_value,
                "discount_final_amount": final_amount_value,
                "discount_context_invoice": discount_ctx_invoice,
                "discount_context_service": discount_ctx_service,
            }
        )

        return context
    
    def _send_subscription_notice_email(
        self,
        template_xml_id: str,
        invoice=None,
        extra_context: Dict[str, Any] | None = None,
        force_send: bool = False,
        metadata_key: str | None = None,
        metadata_value=None,
    ) -> bool:
        """Send subscription notices (reminder, due, overdue) with dedupe guards."""

        self.ensure_one()
        template = self.env.ref(template_xml_id, raise_if_not_found=False)
        if not template:
            _logger.warning("Template %s missing for rollup service %s", template_xml_id, self.id)
            return False

        metadata = dict(self.metadata_json or {})
        sentinel = metadata_value if metadata_value is not None else (invoice.id if invoice else True)
        if metadata_key and metadata.get(metadata_key) == sentinel:
            _logger.info("Skipping duplicate template %s for service %s", template_xml_id, self.id)
            return False

        context = dict(extra_context or {})
        attachments = []
        if invoice:
            invoice = invoice.sudo()
            invoice.invalidate_recordset(["payment_state", "amount_residual"])
            invoice_unpaid = (
                invoice.state == "draft"
                or invoice.payment_state not in {"paid", "in_payment"}
                or not float_is_zero(invoice.amount_residual, precision_rounding=invoice.currency_id.rounding)
            )
            if invoice_unpaid:
                try:
                    pdf_document = invoice._get_invoice_legal_documents("pdf", allow_fallback=True)
                except Exception as exc:  # pylint: disable=broad-except
                    _logger.warning("Unable to render invoice PDF for reminder on service %s: %s", self.id, exc)
                    pdf_document = None
                if pdf_document and pdf_document.get("content"):
                    Attachment = self.env["ir.attachment"].sudo()
                    filename = pdf_document.get("filename") or invoice._get_invoice_report_filename()
                    mimetype = pdf_document.get("filetype") or "application/pdf"
                    pdf_content = pdf_document.get("content")
                    pdf_bytes = pdf_content.encode() if isinstance(pdf_content, str) else pdf_content
                    datas = base64.b64encode(pdf_bytes or b"")
                    attachment = Attachment.search(
                        [
                            ("res_model", "=", "account.move"),
                            ("res_id", "=", invoice.id),
                            ("name", "=", filename),
                        ],
                        limit=1,
                    )
                    attachment_vals = {
                        "name": filename,
                        "type": "binary",
                        "datas": datas,
                        "res_model": "account.move",
                        "res_id": invoice.id,
                        "mimetype": mimetype if mimetype.startswith("application/") else "application/pdf",
                    }
                    if attachment:
                        attachment.write({"datas": datas, "mimetype": attachment_vals["mimetype"]})
                    else:
                        attachment = Attachment.create(attachment_vals)
                    attachments.append((4, attachment.id))
            else:
                _logger.debug(
                    "Skipping invoice attachment for service %s – invoice %s already settled",
                    self.id,
                    invoice.id,
                )
            context = self._prepare_invoice_email_context(invoice, extra_context=context)

        send_ctx = dict(context)
        send_ctx.setdefault("force_send", force_send)
        force_now = bool(force_send and not self.env.context.get("test_mail_silence"))
        template.sudo().with_context(send_ctx).send_mail(
            self.id,
            email_values={"attachment_ids": attachments} if attachments else None,
            force_send=force_now,
        )

        metadata_update = {
            "last_subscription_notice_template": template_xml_id,
        }
        if metadata_key:
            metadata_update[metadata_key] = sentinel
            metadata_update[f"{metadata_key}_at"] = fields.Datetime.now().isoformat()

        self.write({"metadata_json": self._combined_metadata(metadata_update)})
        return True


    def send_subscription_created_mail(self):
        """Notify the customer that their rollup subscription has been created."""

        self.ensure_one()
        template = self.env.ref(
            "rollup_management.mail_template_rollup_subscription_created",
            raise_if_not_found=False,
        )
        if not template:
            _logger.warning("Subscription created template missing for rollup service %s", self.id)
            return False

        context = self._prepare_rollup_subscription_mail_context()
        template.sudo().with_context(rollup_subscription_ctx=context).send_mail(self.id, force_send=True)
        return True

    def send_subscription_active_mail(self):
        try:
            """Notify the customer that their rollup subscription is active."""

            self.ensure_one()
            template = self.env.ref(
                "rollup_management.mail_template_rollup_subscription_active",
                raise_if_not_found=False,
            )
            if not template:
                _logger.warning("Subscription active template missing for rollup service %s", self.id)
                return False

            context = self._prepare_rollup_subscription_mail_context({"status_text": "Ready"})
            template.sudo().with_context(rollup_subscription_ctx=context).send_mail(self.id, force_send=True)
            return True

        except Exception as e:
            _logger.error("Error sending subscription active email for rollup service %s: %s", self.id, e)
            return False

    def notify_admin_new_subscription(self):
        """Notify administrators when a new rollup subscription is created."""

        self.ensure_one()
        template = self.env.ref(
            "rollup_management.mail_template_rollup_subscription_admin",
            raise_if_not_found=False,
        )
        if not template:
            _logger.warning("Admin subscription template missing for rollup service %s", self.id)
            return False

        recipients = self._admin_recipient_payload()
        admin_emails = recipients.get("to") or []
        cc_emails = recipients.get("cc") or []
        config = recipients.get("config")

        default_email_to = ",".join(admin_emails) or (self.env.company.email or "support@zeeve.io")
        context = self._prepare_rollup_subscription_mail_context(
            {
                "admin_emails": admin_emails,
                "email_to": default_email_to,
                "config": config,
                "admin_email_cc": ",".join(cc_emails) if cc_emails else "",
            }
        )

        send_context = {
            "rollup_subscription_ctx": context,
            "default_email_to": default_email_to,
        }
        if config:
            send_context["config"] = config

        email_values = {"email_to": default_email_to}
        if cc_emails:
            email_values["email_cc"] = ",".join(cc_emails)

        template.sudo().with_context(send_context).send_mail(
            self.id,
            email_values=email_values,
            force_send=True,
        )
        return True

    def _send_invoice_email(self, invoice, payment=None, force_send=False, bypass_dedup=False):
        """Send the rollup invoice email using the configured template.

        Called automatically after both the initial checkout webhook and any
        recurring invoice webhook to keep customers informed.
        """

        self.ensure_one()
        # Centralised de-duplication: skip if this invoice or payment was already emailed
        if not bypass_dedup:
            metadata = self.metadata_json if isinstance(self.metadata_json, dict) else {}
            last_invoice_id = metadata.get('last_invoice_email_invoice_id')
            last_payment_id = metadata.get('last_invoice_payment_id')
            if payment and payment.id and payment.id == last_payment_id:
                _logger.info("Skipping duplicate invoice email for payment %s on service %s", payment.id, self.id)
                return False
            if not payment and invoice and invoice.id and invoice.id == last_invoice_id:
                _logger.info("Skipping duplicate invoice email for invoice %s on service %s", invoice.id, self.id)
                return False

        template = self.env.ref('rollup_management.mail_template_rollup_invoice_customer', raise_if_not_found=False)
        if not template:
            _logger.warning('Invoice email template missing for rollup service %s', self.id)
            return False
        invoice = invoice.sudo()
        context = self._prepare_invoice_email_context(invoice, payment)
        attachments = []
        try:
            pdf_document = invoice._get_invoice_legal_documents('pdf', allow_fallback=True)
        except Exception as exc:  # pylint: disable=broad-except
            _logger.warning('Unable to generate invoice PDF for service %s: %s', self.id, exc)
            pdf_document = None

        if pdf_document and pdf_document.get('content'):
            Attachment = self.env['ir.attachment'].sudo()
            filename = pdf_document.get('filename') or invoice._get_invoice_report_filename()
            mimetype = pdf_document.get('filetype') or 'application/pdf'
            pdf_content = pdf_document.get('content')
            if isinstance(pdf_content, str):
                pdf_bytes = pdf_content.encode()
            else:
                pdf_bytes = pdf_content
            attachment = Attachment.search([
                ('res_model', '=', 'account.move'),
                ('res_id', '=', invoice.id),
                ('name', '=', filename),
            ], limit=1)
            datas = base64.b64encode(pdf_bytes or b'')
            attachment_vals = {
                'name': filename,
                'type': 'binary',
                'datas': datas,
                'res_model': 'account.move',
                'res_id': invoice.id,
                'mimetype': mimetype if mimetype.startswith('application/') else 'application/pdf',
            }
            if attachment:
                attachment.write({'datas': datas, 'mimetype': attachment_vals['mimetype']})
            else:
                attachment = Attachment.create(attachment_vals)
            attachments = [(4, attachment.id)]

        force_now = bool(force_send and not self.env.context.get('test_mail_silence'))
        template.sudo().with_context(context).send_mail(
            self.id,
            email_values={'attachment_ids': attachments},
            force_send=force_now,
        )

        timestamp = fields.Datetime.now().isoformat()
        metadata_update = {
            'last_invoice_email_sent_at': timestamp,
            'last_invoice_email_invoice_id': invoice.id,
        }
        if payment:
            metadata_update['last_payment_email_sent_at'] = timestamp
            metadata_update['last_invoice_payment_id'] = payment.id
        else:
            metadata_update['last_draft_invoice_email_sent_at'] = timestamp
        self.write({'metadata_json': self._combined_metadata(metadata_update)})
        return True

    def _create_or_update_payment(
        self,
        invoice,
        amount,
        currency_code,
        payment_intent,
        stripe_invoice_id,
    ):
        """Create or update the payment linked to the Stripe transaction.

        Used by both the checkout finalisation logic and the recurring invoice
        webhook (``invoice.payment_succeeded``) to mirror Stripe payments inside
        Odoo and reconcile them against the invoice created above.
        """

        self.ensure_one()
        Payment = self.env['account.payment'].sudo()
        reference = payment_intent or stripe_invoice_id or self.service_id
        domain = [('rollup_service_id', '=', self.id)]
        if reference:
            domain.append(('transaction_hash', '=', reference))
        payment = Payment.search(domain, limit=1)
        currency = invoice.currency_id
        currency = self._get_currency(currency_code or (currency and currency.name))
        amount = amount or invoice.amount_residual or invoice.amount_total
        if payment:
            updates = {}
            if stripe_invoice_id and not payment.stripe_invoice_id:
                updates['stripe_invoice_id'] = stripe_invoice_id
            if payment_intent and not payment.stripe_payment_intent_id:
                updates['stripe_payment_intent_id'] = payment_intent
            if reference and not payment.transaction_hash:
                updates['transaction_hash'] = reference
            if reference and not payment.payment_reference:
                updates['payment_reference'] = reference
            if not payment.memo and self.service_id:
                updates.setdefault('memo', self.service_id)
            if updates:
                payment.sudo().write(updates)
            return payment
        journal = invoice.journal_id or self.env['account.journal'].sudo().search([
            ('type', 'in', ('bank', 'cash')),
            ('company_id', '=', self.env.company.id),
        ], limit=1)
        if not journal:
            _logger.warning('No journal available to register payment for service %s', self.id)
            return Payment.browse()
        payment_vals = {
            'payment_type': 'inbound',
            'partner_type': 'customer',
            'partner_id': self.customer_id.id,
            'amount': amount,
            'currency_id': currency.id,
            'journal_id': journal.id,
            'date': fields.Date.context_today(self),
            'rollup_service_id': self.id,
            'rollup_invoice_id': invoice.id,
            'stripe_payment_intent_id': payment_intent,
            'stripe_invoice_id': stripe_invoice_id,
            'transaction_hash': reference,
            'payment_reference': reference,
            'memo': self.service_id,
        }
        if stripe_invoice_id:
            payment_vals.setdefault('payment_reference', f'Stripe {stripe_invoice_id}')
        payment_method_line = self._get_stripe_payment_method_line(journal)
        if payment_method_line:
            payment_vals["payment_method_line_id"] = payment_method_line.id

        payment = Payment.create(payment_vals)
        if payment.state != 'posted':
            payment.sudo().action_post()
        self._reconcile_invoice_payment(invoice, payment)
        return payment

    def _reconcile_invoice_payment(self, invoice, payment):
        """Reconcile the provided payment with the invoice (Odoo 18-safe).

        Shared utility for Stripe webhooks and manual flows to align the
        payment with the target invoice and keep rollup pointers in sync before
        we check whether the service can advance.
        """
        try:
            self.ensure_one()
            invoice = invoice.sudo()
            payment = payment.sudo()
            payment_move = payment.move_id

            # Target accounts to reconcile
            TARGET_TYPES = ('asset_receivable', 'liability_payable')

            # 1) Get open receivable/payable lines on the invoice
            inv_lines = invoice.line_ids.filtered(
                lambda l: l.account_id
                and l.account_id.account_type in TARGET_TYPES
                and not l.reconciled
            )
            if not inv_lines:
                return

            # If the invoice has multiple AR/AP lines (rare), reconcile per account
            if not payment_move:
                return

            service = payment.rollup_service_id or invoice.rollup_service_id
            updates = {}
            if invoice and payment.rollup_invoice_id != invoice:
                updates['rollup_invoice_id'] = invoice.id
            if service and payment.rollup_service_id != service:
                updates['rollup_service_id'] = service.id
            if updates:
                payment.with_context(skip_rollup_sync=True).write(updates)

            for acc in inv_lines.mapped('account_id'):
                inv_acc_lines = inv_lines.filtered(lambda l: l.account_id == acc)

                # 2) Find matching payment lines on the SAME account, still open
                pay_lines = payment_move.line_ids.filtered(
                    lambda l: l.account_id == acc
                    and not l.reconciled
                    and (l.debit or l.credit)
                )
                if not pay_lines:
                    continue

                # 3) Reconcile directly (server-side)
                (inv_acc_lines | pay_lines).reconcile()

            # 4) Refresh & trigger hook if fully paid
            invoice.invalidate_recordset(['payment_state', 'amount_residual'])
            if invoice.payment_state == 'paid':
                self._handle_invoice_paid(invoice, payment)
                if payment.state not in {'paid'}:
                    try:
                        payment.sudo().action_validate()
                    except Exception as exc:  # pylint: disable=broad-except
                        _logger.warning(
                            'Unable to validate payment %s after reconciliation: %s',
                            payment.id,
                            exc,
                        )
        except Exception:
            _logger.exception("Error reconciling invoice %s with payment %s", invoice.id, payment.id)


    def _handle_invoice_paid(self, invoice, payment=None):
        """Persist metadata and trigger deployment once the invoice is settled.

        Invoked by Stripe callbacks and the manual payment pipeline once the
        invoice residual hits zero. Keeps metadata coherent, pushes the service
        to deploying on first payment, and ensures customer/admin notifications
        are dispatched.
        """

        self.ensure_one()
        invoice = invoice.sudo()
        if invoice.payment_state != 'paid':
            return False

        payment = payment.sudo() if payment else payment
        is_first_invoice = self._is_first_paid_invoice(invoice)
        existing_metadata = dict(self.metadata_json or {})
        invoice_url_value = existing_metadata.get('stripe_invoice_url') or invoice.get_portal_url()
        timestamp = fields.Datetime.now().isoformat()
        metadata_update = {
            'rollup_invoice_odoo_id': invoice.id,
            'last_paid_invoice_id': invoice.id,
            'last_paid_invoice_number': invoice.name,
            'last_paid_invoice_at': timestamp,
            'stripe_payment_status': 'paid',
            'stripe_invoice_url': invoice_url_value,
        }
        if payment:
            metadata_update.update(
                {
                    'rollup_payment_odoo_id': payment.id,
                    'last_payment_reference': payment.transaction_hash or payment.payment_reference,
                    'last_payment_amount': payment.amount,
                }
            )

        # Dispatched via the enhanced _send_invoice_email which handles its own de-duplication
        self._send_invoice_email(invoice, payment=payment, force_send=True)

        status_before = self.status
        if status_before not in {"active", "cancelled", "archived"}:
            self.write({"status": "deploying"})
        deployment_utils.update_subscription_status(self, "active", reason="invoice_paid")

        payment_category = 'new' if is_first_invoice else 'renewal'
        payment_category_label = 'New Subscription Payment' if payment_category == 'new' else 'Renewal Payment'
        renewal_context = {
            "invoice": invoice,
            "payment": payment,
            "stripe_invoice_url": invoice_url_value,
            "payment_category": payment_category,
            "payment_category_label": payment_category_label,
        }
        metadata_email_update: Dict[str, Any] = {}
        renewal_key = "subscription_renewal_last_invoice_id"
        admin_recipients = self._admin_recipient_payload()
        admin_emails = admin_recipients.get("to") or []
        admin_email_kwargs = {}
        if admin_recipients.get("cc"):
            admin_email_kwargs["email_cc"] = ",".join(admin_recipients["cc"])

        if not is_first_invoice and existing_metadata.get(renewal_key) != invoice.id:
            # Skip renewal mail for first invoice (initial activation)
            if deployment_utils.send_rollup_email(
                "rollup_management.mail_template_rollup_subscription_renewed",
                self,
                renewal_context,
                email_to=",".join(admin_emails),
                **admin_email_kwargs,
            ):
                metadata_email_update.update(
                    {
                        renewal_key: invoice.id,
                        "subscription_renewal_last_sent_at": timestamp,
                    }
                )
        admin_key = "subscription_payment_admin_last_invoice_id"
        if existing_metadata.get(admin_key) != invoice.id:
            if deployment_utils.send_rollup_email(
                "rollup_management.mail_template_rollup_payment_success_admin",
                self,
                renewal_context,
                email_to=",".join(admin_emails),
                **admin_email_kwargs,
            ):
                
                metadata_email_update.update(
                    {
                        admin_key: invoice.id,
                        "subscription_payment_admin_last_sent_at": timestamp,
                    }
                )

        if metadata_email_update:
            metadata_update.update(metadata_email_update)

        self.write({'metadata_json': self._combined_metadata(metadata_update)})
        if status_before == 'draft':
            self.action_start_deployment(metadata_update, auto_activate=False)

        if self.status == 'deploying':
            self._send_deployment_notifications(invoice, payment, invoice_url=invoice_url_value)

        customer = self.customer_id
        if customer:
            notification_type = 'rollup_purchase_success' if is_first_invoice else 'rollup_payment_success'
            notification_title = 'Rollup purchased' if is_first_invoice else 'Recurring payment received'
            notification_message = (
                '%s has been purchased successfully.' % (
                    self.type_id.display_name or self.name or 'Your rollup service'
                )
                if is_first_invoice else
                'Recurring payment received successfully for %s.' % (
                    self.type_id.display_name or self.name or 'your rollup service'
                )
            )
            self.env['zeeve.notification'].sudo().notify_partner(
                customer,
                notification_type=notification_type,
                title=notification_title,
                message=notification_message,
                category='success',
                payload={
                    'rollup_service_id': self.id,
                    'service_id': self.service_id or '',
                    'rollup_name': self.name or '',
                    'invoice_id': invoice.id if invoice else False,
                    'invoice_number': invoice.name if invoice else '',
                    'payment_id': payment.id if payment else False,
                    'payment_category': payment_category,
                    'subscription_status': self.subscription_status or '',
                    'status': self.status or '',
                },
                action_url=invoice_url_value or '/rollups',
                reference_model='rollup.service',
                reference_id=self.id,
                dedupe_key='%s:%s' % (notification_type, invoice.id if invoice else self.id),
            )
        return True

    def _is_first_paid_invoice(self, invoice) -> bool:
        """Return ``True`` when the provided invoice is the first one paid."""

        self.ensure_one()
        if not invoice:
            return False

        invoice.invalidate_recordset(["payment_state", "amount_residual"])
        paid_invoices = self.invoice_ids.filtered(
            lambda inv: inv.payment_state in {"paid", "in_payment"}
            and float_is_zero(inv.amount_residual, precision_rounding=inv.currency_id.rounding)
        )
        return not paid_invoices.filtered(lambda inv: inv.id != invoice.id)

    def _get_latest_invoice(self):
        """Return the most recent invoice linked to the service."""

        self.ensure_one()
        if not self.invoice_ids:
            return self.env['account.move']
        return self.invoice_ids.sorted(key=lambda inv: inv.invoice_date or inv.create_date or inv.id)[-1]

    # ------------------------------------------------------------------
    # Deployment helpers
    # ------------------------------------------------------------------
    def action_start_deployment(self, metadata: Dict[str, Any] | None = None, auto_activate: bool = True):
        """Mark the service as deploying and optionally trigger provisioning."""

        metadata = metadata or {}
        for service in self:
            combined_metadata = service._combined_metadata(metadata)
            values = {"status": "deploying"}
            if combined_metadata:
                values["metadata_json"] = combined_metadata
            service.write(values)
            service._link_payment_logs_from_metadata(combined_metadata)
            service._queue_deployment(auto_activate=auto_activate)
        return True

    def action_mark_failed(self, error_message: str | None = None):
        """Mark the service as failed and append error message to metadata."""

        for service in self:
            metadata = service._combined_metadata({})
            if error_message:
                failure_log = metadata.get("failures", [])
                failure_log.append({
                    "message": error_message,
                    "logged_at": fields.Datetime.now().isoformat(),
                })
                metadata["failures"] = failure_log
            service.write({
                "status": "failed",
                "metadata_json": metadata,
            })
        return True

    def _queue_deployment(self, auto_activate: bool = True):
        """Hook for background provisioning.

        By default provisioning happens synchronously, but modules can
        override this method to push work to queues or background workers.
        """

        self.ensure_one()
        if auto_activate:
            self._complete_deployment()

    def _complete_deployment(self, additional_metadata: Dict[str, Any] | None = None):
        """Finalize deployment and mark the service as active."""

        additional_metadata = additional_metadata or {}
        additional_metadata.setdefault("provisioned_at", fields.Datetime.now().isoformat())
        metadata = self._combined_metadata(additional_metadata)
        # self._create_nodes_from_inputs()
        self.write({
            "status": metadata.get("status_override", "active"),
            "metadata_json": metadata,
        })
        self._link_payment_logs_from_metadata()
        if additional_metadata is not None:
            return True
        self._handle_payment_post_activation()
        return True

    def action_activate_service(self):
        """Allow administrators to manually promote a deployment to active."""

        for service in self:
            metadata_update = {
                "status_override": "active",
                "admin_activated_at": fields.Datetime.now().isoformat(),
                "admin_activated_by": self.env.user.id,
            }
            self.send_subscription_active_mail()
            service._complete_deployment(metadata_update)
        return True

    # ------------------------------------------------------------------
    # Payment integration helpers
    # ------------------------------------------------------------------

    def _handle_payment_post_activation(self):
        """Process payment context stored in metadata once deployment is active.

        Triggered immediately after ``checkout.session.completed`` finalises the
        service (via the Stripe webhook) and whenever an operator manually posts
        a payment that moves the deployment forward.
        """

        for service in self:
            metadata = service.metadata_json or {}
            if not isinstance(metadata, dict):
                continue

            payment_context = metadata.get("rollup_payment_context") or {}
            if not payment_context or metadata.get("rollup_payment_processed"):
                continue

            try:
                log = service._process_successful_payment(payment_context)
            except Exception as exc:  # pylint: disable=broad-except
                _logger.exception("Failed to process payment context for service %s: %s", service.id, exc)
                continue

            metadata_update = {
                "rollup_payment_processed": True,
            }
            if log:
                metadata_update.setdefault("last_payment_log_id", log.id)
            service.write({"metadata_json": service._combined_metadata(metadata_update)})

    def _process_successful_payment(self, payment_context: Dict[str, Any]):
        """Create payment logs, fetch invoice details, and send notifications.

        Called from :meth:`_handle_payment_post_activation` after the webhook or
        fallback API has confirmed the initial checkout session.
        """

        self.ensure_one()

        metadata = self.metadata_json if isinstance(self.metadata_json, dict) else {}

        session_data = payment_context.get("checkout_session") or {}
        if not isinstance(session_data, dict):
            session_data = {}

        session_id = session_data.get("id") or metadata.get("stripe_session_id")
        payment_intent = session_data.get("payment_intent") or metadata.get("stripe_payment_intent_id")
        subscription_id = session_data.get("subscription") or metadata.get("stripe_subscription_id")
        customer_id = session_data.get("customer") or metadata.get("stripe_customer_id")
        invoice_id = session_data.get("invoice") or metadata.get("stripe_invoice_id")
        amount_total = session_data.get("amount_total")
        currency = session_data.get("currency") or metadata.get("stripe_currency") or "usd"

        try:
            amount_float = float(amount_total) / 100 if amount_total else float(metadata.get("stripe_amount"))
        except (TypeError, ValueError):
            amount_float = 0.0

        payment_status = session_data.get("payment_status") or metadata.get("stripe_payment_status")
        log_status = "succeeded" if payment_status in {"paid", "succeeded"} else "pending"

        stripe_client = rollup_util.get_stripe_client()
        invoice_payload: Dict[str, Any] = {}
        invoice_url = None
        invoice_pdf = None

        if invoice_id and getattr(stripe_client, "api_key", None):
            try:
                invoice_payload = stripe_client.Invoice.retrieve(invoice_id)
                invoice_url = invoice_payload.get("hosted_invoice_url")
                invoice_pdf = invoice_payload.get("invoice_pdf")
            except stripe_client.error.StripeError as exc:  # type: ignore[attr-defined]
                _logger.warning("Unable to retrieve Stripe invoice %s: %s", invoice_id, exc)
            except Exception as exc:  # pylint: disable=broad-except
                _logger.warning("Unexpected error fetching invoice %s: %s", invoice_id, exc)

        invoice = self._ensure_initial_invoice()[:1]
        payment = self.env['account.payment'].browse()
        if invoice:
            invoice_updates = {
                'stripe_invoice_id': invoice_id or invoice.stripe_invoice_id,
                'stripe_payment_intent_id': payment_intent or invoice.stripe_payment_intent_id,
                'stripe_transaction_reference': payment_intent or invoice.stripe_transaction_reference,
                'rollup_service_id': self.id,
            }
            invoice.sudo().write(invoice_updates)
            if invoice.state == 'draft':
                invoice.action_post()
            payment = self._create_or_update_payment(
                invoice=invoice,
                amount=amount_float,
                currency_code=currency,
                payment_intent=payment_intent,
                stripe_invoice_id=invoice_id,
            )
            if payment:
                invoice.sudo().write({
                    'stripe_transaction_reference': payment.transaction_hash or invoice.stripe_transaction_reference,
                })

        autopay_enabled = self._compute_autopay_flag(invoice_payload)

        metadata_update = {
            "stripe_subscription_id": subscription_id,
            "stripe_customer_id": customer_id,
            "stripe_invoice_id": invoice_id,
            "stripe_invoice_url": invoice_url or (invoice and invoice.get_portal_url()),
            "stripe_invoice_pdf": invoice_pdf,
            "stripe_payment_status": payment_status,
            "rollup_invoice_odoo_id": invoice.id if invoice else False,
            "rollup_payment_odoo_id": payment.id if payment else False,
            "deployment_token": metadata.get("deployment_token") or session_data.get("client_reference_id"),
            "autopay_enabled": autopay_enabled,
        }

        self.write({
            "metadata_json": self._combined_metadata(metadata_update),
            "stripe_subscription_id": subscription_id,
            "stripe_customer_id": customer_id,
            "stripe_invoice_id": invoice_id,
            "stripe_session_id": session_id or self.stripe_session_id,
            "stripe_payment_intent_id": payment_intent or self.stripe_payment_intent_id,
            "deployment_token": metadata_update.get("deployment_token") or self.deployment_token,
            "autopay_enabled": autopay_enabled,
        })
        # Odoo Managed Billing: Initialize next_billing_date if unset
        if self.is_odoo_managed and not self.next_billing_date:
            from ..utils import deployment_utils
            now = fields.Datetime.now()
            next_date = deployment_utils._get_next_rollup_billing_date(self, now)
            self.write({'next_billing_date': next_date})
        rollup_util._store_partner_stripe_customer(self,customer_id)

        log_values = {
            "event_id": f"manual_{session_id or self.service_id}",
            "event_type": "checkout.session.completed",
            "stripe_subscription_id": subscription_id,
            "stripe_customer_id": customer_id,
            "stripe_payment_intent_id": payment_intent,
            "stripe_invoice_id": invoice_id,
            "transaction_hash": payment_intent,
            "amount": amount_float,
            "currency": currency,
            "payment_status": log_status,
            "rollup_service_id": self.id,
            "event_data": json.dumps(
                {
                    "checkout_session": session_data,
                    "invoice": invoice_payload,
                },
                default=str,
            ),
            "stripe_created": fields.Datetime.now(),
        }

        payment_log = self.env["stripe.payment.log"].sudo().create(log_values)
        log_updates = {}
        if invoice:
            log_updates["invoice_id"] = invoice.id
        if payment:
            log_updates["payment_id"] = payment.id
            if payment.transaction_hash and not payment_log.transaction_hash:
                log_updates["transaction_hash"] = payment.transaction_hash
        if log_updates:
            payment_log.write(log_updates)

        self._send_invoice_email(invoice, payment=payment, force_send=True)
        self._send_deployment_notifications(invoice, payment, invoice_url or (invoice and invoice.get_portal_url()))

        return payment_log

    def _compute_autopay_flag(self, invoice_payload: Dict[str, Any] | None = None) -> bool:
        """Infer whether Stripe is charging invoices automatically.

        Stripe marks recurring invoices with ``collection_method =
        'charge_automatically'`` when autopay is enabled and ``send_invoice``
        when manual collection is required. Persisting this flag keeps the Odoo
        status aligned with what Stripe will do on the next billing cycle.
        """

        if not invoice_payload:
            return True

        collection_method = invoice_payload.get("collection_method")
        if not collection_method and invoice_payload.get("auto_advance") is False:
            collection_method = "send_invoice"

        return collection_method != "send_invoice"

    def process_stripe_invoice_payment(self, invoice_payload: Dict[str, Any], log_entry=None):
        """Handle recurring Stripe invoice payments triggered by webhooks."""

        self.ensure_one()

        metadata = self.metadata_json if isinstance(self.metadata_json, dict) else {}

        amount_cents = (
            invoice_payload.get("amount_paid")
            or invoice_payload.get("amount_due")
            or invoice_payload.get("subtotal")
            or 0
        )
        try:
            amount = float(amount_cents) / 100 if isinstance(amount_cents, (int, float)) else float(amount_cents)
        except (TypeError, ValueError):
            amount = self.type_id.cost or 0.0

        currency_code = (invoice_payload.get("currency") or metadata.get("stripe_currency") or "USD").upper()
        stripe_invoice_id = invoice_payload.get("id")
        payment_intent = invoice_payload.get("payment_intent") or metadata.get("stripe_payment_intent_id")
        subscription_id = invoice_payload.get("subscription") or metadata.get("stripe_subscription_id")
        customer_id = invoice_payload.get("customer") or metadata.get("stripe_customer_id")
        invoice_url = invoice_payload.get("hosted_invoice_url")
        invoice_pdf = invoice_payload.get("invoice_pdf")
        invoice_number = invoice_payload.get("number")
        payment_status = invoice_payload.get("status") or invoice_payload.get("payment_status") or "paid"

        def _ts_to_date(timestamp):
            if not timestamp:
                return None
            try:
                return datetime.fromtimestamp(timestamp, UTC).date()
            except (TypeError, ValueError, OSError):
                return None

        created_ts = invoice_payload.get("created")
        invoice_date = _ts_to_date(created_ts) or fields.Date.context_today(self)

        due_ts = invoice_payload.get("due_date")
        due_date = _ts_to_date(due_ts) or invoice_date

        lines = invoice_payload.get("lines", {}).get("data", []) if invoice_payload.get("lines") else []
        period_start = None
        period_end = None
        for line in lines:
            period = line.get("period") if isinstance(line, dict) else None
            if not period:
                continue
            period_start = period_start or _ts_to_date(period.get("start"))
            period_end = period.get("end") and _ts_to_date(period.get("end")) or period_end
        next_attempt_ts = invoice_payload.get("next_payment_attempt")
        next_attempt_date = _ts_to_date(next_attempt_ts)
        if not next_attempt_date and period_end:
            next_attempt_date = period_end + timedelta(days=1)

        invoice_model = self.env["account.move"].sudo()
        _logger.info(
            "Step---------------------7 %s", invoice_model
        )
        invoice = invoice_model.search([("stripe_invoice_id", "=", stripe_invoice_id)], limit=1) if stripe_invoice_id else invoice_model.browse()
        _logger.info(
            "Step---------------------8 %s", invoice
        )
        if not invoice:
            _logger.info(
                "Step---------------------8 %s", invoice
            )
            invoice = self._create_invoice_from_amount(
                amount=amount,
                currency_code=currency_code,
                invoice_date=invoice_date,
                due_date=due_date,
                stripe_invoice_id=stripe_invoice_id,
                invoice_number=invoice_number,
            )
            _logger.info(
                "Step---------------------10 %s", invoice
            )

        else:
            updates = {"rollup_service_id": self.id}
            if payment_intent:
                updates["stripe_payment_intent_id"] = payment_intent
            invoice.sudo().write(updates)
            if invoice.state == "draft":
                invoice.action_post()


        # --- Stripe discount extraction patch ---
        # Extract discount context from invoice_payload (Stripe webhook)
        Discount = self.env["subscription.discount"].sudo()
        discount_ctx_invoice = {}
        discount_ctx_service = {}
        # Try to extract from invoice_payload
        total_discounts = invoice_payload.get("total_discount_amounts") or []
        discounts = invoice_payload.get("discounts") or []
        discount_code_value = None
        discount_record = Discount.browse()
        discount_amount_value = 0.0
        original_amount_value = None
        # Stripe promotion code/coupon extraction
        if total_discounts:
            try:
                discount_amount_value = float(total_discounts[0].get("amount", 0)) / 100.0
                discount_obj = total_discounts[0].get("discount")
                if discount_obj:
                    coupon_id = None
                    if isinstance(discount_obj, dict):
                        coupon_id = discount_obj.get("coupon") or discount_obj.get("id")
                    if coupon_id:
                        discount_record = Discount.search([("stripe_coupon_id", "=", coupon_id)], limit=1)
                        if discount_record:
                            discount_code_value = discount_record.code or coupon_id
                        else:
                            discount_code_value = coupon_id
            except Exception:
                discount_amount_value = 0.0
        # Fallback: check discounts array
        if not discount_record or not discount_record.exists():
            for d in discounts:
                coupon_id = None
                if isinstance(d, dict):
                    coupon_id = d.get("coupon") or d.get("id")
                if coupon_id:
                    candidate = Discount.search([("stripe_coupon_id", "=", coupon_id)], limit=1)
                    if candidate:
                        discount_record = candidate
                        discount_code_value = candidate.code or coupon_id
                        break
        # Fallback: check code in invoice metadata
        if not discount_code_value:
            discount_code_value = invoice_payload.get("discount_code")
        # Fallback: check service context
        if not discount_record or not discount_record.exists():
            discount_record = getattr(self, "discount_id", Discount.browse())
        if not discount_code_value:
            discount_code_value = getattr(self, "discount_code", None)
        # Original amount
        subtotal = invoice_payload.get("subtotal")
        if subtotal is not None:
            original_amount_value = float(subtotal) / 100.0
        if original_amount_value is None and discount_amount_value:
            original_amount_value = amount + discount_amount_value

        payment = self._create_or_update_payment(
            invoice=invoice,
            amount=amount,
            currency_code=currency_code,
            payment_intent=payment_intent,
            stripe_invoice_id=stripe_invoice_id,
        )

        if invoice and invoice.invoice_line_ids:
            for line in invoice.invoice_line_ids:
                line_updates: Dict[str, Any] = {}
                if discount_record and discount_record.exists():
                    line_updates["discount_id"] = discount_record.id
                elif discount_amount_value or discount_code_value is not None:
                    line_updates["discount_id"] = False
                if discount_code_value is not None:
                    line_updates["discount_code"] = discount_code_value or False
                elif "discount_id" in line_updates and line_updates["discount_id"] is False:
                    line_updates.setdefault("discount_code", False)
                if line_updates:
                    line.sudo().write(line_updates)

        autopay_enabled = self._compute_autopay_flag(invoice_payload)

        metadata_update = {
            "stripe_subscription_id": subscription_id or self.stripe_subscription_id,
            "stripe_customer_id": customer_id or self.stripe_customer_id,
            "stripe_invoice_id": stripe_invoice_id or invoice.stripe_invoice_id,
            "stripe_invoice_url": invoice_url or (invoice and invoice.get_portal_url()),
            "stripe_invoice_pdf": invoice_pdf,
            "stripe_payment_status": payment_status,
            "rollup_invoice_odoo_id": invoice.id if invoice else False,
            "rollup_payment_odoo_id": payment.id if payment else False,
            "last_recurring_payment_at": fields.Datetime.now().isoformat(),
            "last_recurring_payment_amount": amount,
            "autopay_enabled": autopay_enabled,
        }
        if discount_amount_value:
            metadata_update["discount_amount"] = discount_amount_value
        if discount_code_value:
            metadata_update["discount_code"] = discount_code_value
        if discount_record and discount_record.exists():
            metadata_update["discount_id"] = str(discount_record.id)
        if original_amount_value is not None:
            metadata_update.setdefault("original_amount", original_amount_value)
        if invoice_number:
            metadata_update["last_recurring_invoice_number"] = invoice_number
        if payment_intent:
            metadata_update["stripe_payment_intent_id"] = payment_intent
        if period_start:
            metadata_update["last_recurring_period_start"] = period_start.isoformat()
        if period_end:
            metadata_update["last_recurring_period_end"] = period_end.isoformat()
        if next_attempt_date:
            metadata_update["next_recurring_billing_date"] = next_attempt_date.isoformat()

        write_vals = {
            "metadata_json": self._combined_metadata(metadata_update),
            "stripe_subscription_id": metadata_update["stripe_subscription_id"],
            "stripe_customer_id": metadata_update["stripe_customer_id"],
            "stripe_invoice_id": metadata_update["stripe_invoice_id"],
            "autopay_enabled": autopay_enabled,
        }
        if next_attempt_date:
            write_vals["next_billing_date"] = next_attempt_date
        if payment_intent:
            write_vals["stripe_payment_intent_id"] = payment_intent
        if self.status in {"draft", "deploying"}:
            write_vals["status"] = "active"
        self.write(write_vals)
        rollup_util._store_partner_stripe_customer(self, metadata_update.get("stripe_customer_id") or customer_id)
        self._link_payment_logs_from_metadata(metadata_update)

        if log_entry:
            log_updates = {
                "stripe_invoice_id": stripe_invoice_id,
                "stripe_subscription_id": subscription_id,
                "amount": amount,
                "currency": currency_code.lower(),
                "payment_status": "succeeded",
            }
            if not log_entry.rollup_service_id:
                log_updates["rollup_service_id"] = self.id
            if invoice:
                log_updates["invoice_id"] = invoice.id
            if payment:
                log_updates["payment_id"] = payment.id
            log_entry.sudo().write(log_updates)

        _logger.info(
            "Rollup autopay invoice processed | service=%s invoice=%s payment=%s next_billing=%s",
            self.id,
            stripe_invoice_id,
            payment and payment.id,
            metadata_update.get("next_recurring_billing_date"),
        )
        self._send_invoice_email(invoice, payment=payment, force_send=True)
        return invoice, payment

    def _prepare_payment_failure_mail_context(self, invoice_payload: Dict[str, Any] | None = None) -> Dict[str, Any]:
        """Return template context for Stripe payment failures."""

        self.ensure_one()
        invoice_payload = invoice_payload or {}
        metadata = dict(self.metadata_json or {})

        due_date_value = deployment_utils._as_date(
            invoice_payload.get('next_payment_attempt') or invoice_payload.get('due_date')
        ) or self.next_billing_date
        due_date_display = ''
        if due_date_value:
            try:
                due_date_display = format_date(self.env, due_date_value)
            except Exception:  # pragma: no cover - formatting fallback
                due_date_display = fields.Date.to_string(due_date_value)

        suspension_deadline = (
            deployment_utils._as_date(invoice_payload.get('grace_period_end'))
            or due_date_value
            or self.next_billing_date
        )
        suspension_deadline_display = ''
        if suspension_deadline:
            try:
                suspension_deadline_display = format_date(self.env, suspension_deadline)
            except Exception:  # pragma: no cover - formatting fallback
                suspension_deadline_display = fields.Date.to_string(suspension_deadline)

        currency_code = (invoice_payload.get('currency') or '').upper()
        Currency = self.env['res.currency'].sudo()
        currency = False
        if currency_code:
            currency = Currency.search([('name', '=', currency_code)], limit=1)
        if not currency:
            currency = self.env.company.currency_id

        amount_raw = (
            invoice_payload.get('amount_due')
            if invoice_payload.get('amount_due') is not None
            else invoice_payload.get('amount_remaining')
        )
        if amount_raw is None:
            amount_raw = invoice_payload.get('total') or 0.0
        try:
            amount_value = float(amount_raw)
        except Exception:  # pragma: no cover - fallback when parsing fails
            amount_value = 0.0
        if invoice_payload.get('currency'):
            amount_value = amount_value / 100.0

        formatted_amount = ''
        if currency:
            try:
                formatted_amount = formatLang(self.env, amount_value, currency_obj=currency)
            except Exception:  # pragma: no cover - fallback formatting
                formatted_amount = "%s %.2f" % ((currency.symbol or currency.name or ''), amount_value)
        else:
            formatted_amount = f"{amount_value:.2f}"

        customer = self.customer_id
        customer_email = (
            self.env.context.get('force_rollup_payment_email')
            or customer.email
            or (customer.user_ids[:1].partner_id.email)
            or ''
        )
        customer_name = customer.display_name or customer.name or 'there'

        invoice_url = (
            invoice_payload.get('hosted_invoice_url')
            or metadata.get('stripe_invoice_url')
            or self.hosted_invoice_url
        )

        failure_reason = (
            (invoice_payload.get('last_payment_error') or {}).get('message')
            or invoice_payload.get('failure_reason')
            or invoice_payload.get('failure_message')
            or metadata.get('last_failed_payment_reason')
            or _('Stripe was unable to process the payment method on file.')
        )

        retry_message = _('Zeeve will retry the charge automatically shortly.')
        next_attempt = invoice_payload.get('next_payment_attempt')
        if next_attempt:
            try:
                retry_dt = datetime.fromtimestamp(int(next_attempt), tz=timezone.utc)
                retry_message = _(
                    'Zeeve will retry the charge on %s.'
                ) % format_date(self.env, retry_dt.date())
            except Exception:  # pragma: no cover - fallback text
                retry_message = _('Zeeve will retry the charge again soon.')

        invoice_identifier = invoice_payload.get('id') or metadata.get('stripe_invoice_id')
        invoice_number = invoice_payload.get('number') or metadata.get('last_paid_invoice_number')

        return {
            'email_to': customer_email,
            'customer_email': customer_email,
            'customer_name': customer_name,
            'protocol_name': self.type_id.name or self.name,
            'plan_name': self.type_id.name or self.name,
            'rollup_name': self.name,
            'next_billing_date': due_date_value,
            'due_date_display': due_date_display,
            'suspension_deadline': suspension_deadline,
            'suspension_deadline_display': suspension_deadline_display,
            'grace_period_end': suspension_deadline,
            'invoice_url': invoice_url,
            'hosted_invoice_url': invoice_url,
            'stripe_invoice_id': invoice_identifier,
            'invoice_number': invoice_number,
            'error_message': failure_reason,
            'retry_message': retry_message,
            'amount_value': amount_value,
            'formatted_amount': formatted_amount,
            'currency_symbol': currency and (currency.symbol or currency.name) or '',
        }

    def _admin_recipient_payload(self):
        """Return admin recipient information for this service."""
        self.ensure_one()
        channel_code = False
        if self.type_id and self.type_id.admin_channel_id:
            channel_code = self.type_id.admin_channel_id.code
        return base_utils._get_admin_recipients(self.env, channel_code=channel_code or None)

    def notify_unsubscribe_request(self, *, requested_by=None, reason=None):
        """Notify admins about a rollup unsubscribe request."""

        template = self.env.ref(
            "subscription_management.mail_template_subscription_unsubscribe_request_admin",
            raise_if_not_found=False,
        )
        if not template:
            return False

        for service in self:
            partner = service.customer_id or requested_by and requested_by.partner_id
            if not partner:
                continue
            admin_payload = service._admin_recipient_payload()
            admin_to = ",".join(admin_payload.get("to", []))
            admin_cc = ",".join(admin_payload.get("cc", []))
            if not admin_to:
                cfg = service.env['zeeve.config'].sudo().search([], limit=1)
                admin_to = (cfg and cfg.admin_emails) or service.env.company.email or "support@zeeve.io"

            request_dt = fields.Datetime.context_timestamp(service, fields.Datetime.now())
            if request_dt:
                request_display = format_date(service.env, request_dt.date())
            else:
                request_display = format_date(service.env, fields.Date.context_today(service))

            base_url = service.env['ir.config_parameter'].sudo().get_param('backend_url') or service.env['ir.config_parameter'].sudo().get_param('web.base.url')
            record_url = False
            if base_url:
                base_url = base_url.rstrip('/')
                record_url = f"{base_url}/web#id={service.id}&model=rollup.service&view_type=form"

            ctx_payload = {
                "customer_name": partner.display_name or partner.name or "Customer",
                "customer_email": partner.email or partner.email_formatted or '',
                "plan_name": service.type_id.display_name or service.name,
                "protocol_name": service.type_id.name or service.name,
                "subscription_type": "Rollup Service",
                "identifier": service.service_id or service.name,
                "request_date": request_display,
                "reason": reason or '',
                "is_rollup": True,
                "record_url": record_url,
            }
            send_ctx = {
                "unsubscribe_request_ctx": ctx_payload,
                "email_to": admin_to,
            }
            email_vals = {"email_to": admin_to}
            if admin_cc:
                email_vals["email_cc"] = admin_cc
            template.sudo().with_context(send_ctx).send_mail(
                partner.id,
                force_send=True,
                email_values=email_vals,
                raise_exception=False,
            )
            self.env['zeeve.notification'].sudo().notify_partner(
                partner,
                notification_type='rollup_cancellation_requested',
                title='Cancellation requested',
                message='We received your cancellation request for %s.' % (
                    service.type_id.display_name or service.name or 'your rollup service'
                ),
                category='warning',
                payload={
                    'rollup_service_id': service.id,
                    'service_id': service.service_id or '',
                    'rollup_name': service.name or '',
                    'reason': reason or '',
                },
                action_url='/rollups',
                reference_model='rollup.service',
                reference_id=service.id,
                dedupe_key='rollup_cancellation_requested:%s:%s' % (service.id, fields.Datetime.now()),
            )
        return True

    def send_payment_failure_notifications(self, invoice_payload: Dict[str, Any] | None = None):
        """Send customer and admin payment failure notifications."""

        for service in self:
            ctx_payload = service._prepare_payment_failure_mail_context(invoice_payload=invoice_payload)
            if not ctx_payload:
                continue

            try:
                deployment_utils.send_rollup_email(
                    'rollup_management.mail_template_rollup_payment_failed',
                    service,
                    context={'rollup_payment_failed_ctx': ctx_payload},
                    email_to=ctx_payload.get('email_to'),
                )
            except Exception:  # pragma: no cover - log and continue
                _logger.exception(
                    'Failed to send rollup payment failure mail to customer | service=%s',
                    service.id,
                )

            admin_recipient_payload = service._admin_recipient_payload()
            admin_to = ctx_payload.get('admin_email_to') or ','.join(admin_recipient_payload.get('to', []))
            if not admin_to:
                admin_to = service.env.user.company_id.email or ''
            if not admin_to:
                continue

            admin_ctx = dict(ctx_payload)
            admin_ctx['email_to'] = admin_to
            admin_kwargs = {}
            if admin_recipient_payload.get('cc'):
                admin_kwargs['email_cc'] = ','.join(admin_recipient_payload['cc'])
            try:
                deployment_utils.send_rollup_email(
                    'rollup_management.mail_template_rollup_payment_failed_admin',
                    service,
                    context={'rollup_payment_failed_ctx': admin_ctx},
                    email_to=admin_to,
                    **admin_kwargs,
                )
            except Exception:  # pragma: no cover - soft fail
                _logger.exception(
                    'Failed to send rollup payment failure mail to admin | service=%s',
                    service.id,
                )

    def process_failed_invoice_payment(self, invoice_payload: Dict[str, Any]):
        """Store failure context when Stripe reports an unsuccessful charge."""

        self.ensure_one()
        failure_reason = (
            invoice_payload.get("last_payment_error", {}) or {}
        ).get("message") or invoice_payload.get("failure_reason") or invoice_payload.get("failure_message")

        metadata_update = {
            "stripe_invoice_id": invoice_payload.get("id") or self.stripe_invoice_id,
            "stripe_payment_status": "failed",
            "last_failed_payment_at": fields.Datetime.now().isoformat(),
        }
        if failure_reason:
            metadata_update["last_failed_payment_reason"] = failure_reason

        self.write({"metadata_json": self._combined_metadata(metadata_update)})
        _logger.warning(
            "Rollup autopay failure recorded | service=%s invoice=%s reason=%s",
            self.id,
            invoice_payload.get("id"),
            failure_reason,
        )
        self.send_payment_failure_notifications(invoice_payload=invoice_payload)
        return True

    def _send_deployment_notifications(self, invoice, payment=None, invoice_url: str | None = None):
        """Send deployment confirmation emails to customer and admins.

        Triggered after the first successful payment (webhook or manual) so the
        provisioning and customer success teams are notified automatically.
        """

        metadata = dict(self.metadata_json or {})
        if (
            metadata.get('deployment_notifications_sent_at')
            and not self.env.context.get('force_rollup_deploy_email')
        ):
            return False


        if not invoice:
            invoice = self._ensure_initial_invoice()[:1]
        ctx = self._prepare_deployment_mail_context(invoice, payment, invoice_url)

        ctx.setdefault('default_invoice_url', invoice_url or (invoice and invoice.get_portal_url()))

        admin_recipients = self._admin_recipient_payload()
        admin_emails = admin_recipients.get('to') or []

        self.send_subscription_created_mail()
        self.notify_admin_new_subscription()

        metadata_flags = {
            'deployment_notifications_sent_at': fields.Datetime.now().isoformat(),
            'deployment_notifications_email_to': ','.join(admin_emails) if admin_emails else False,
        }
        if admin_recipients.get('cc'):
            metadata_flags['deployment_notifications_email_cc'] = ','.join(admin_recipients['cc'])
        self.write({'metadata_json': self._combined_metadata(metadata_flags)})
        return True


    @api.model
    def _find_from_stripe_subscription_payload(
        cls,
        subscription: Dict[str, Any] | None = None,
        metadata: Dict[str, Any] | None = None,
        subscription_id: str | None = None,
        customer_id: str | None = None,
    ):
        """Locate a rollup service referenced by a Stripe subscription payload."""

        subscription = subscription or {}
        raw_metadata = metadata or subscription.get("metadata") or {}
        metadata = raw_metadata if isinstance(raw_metadata, dict) else {}
        subscription_id = subscription_id or subscription.get("id")
        customer_id = customer_id or subscription.get("customer")

        Service = cls.sudo()

        identifier = metadata.get("rollup_service_id") or metadata.get("service_id")
        if identifier:
            try:
                service = Service.browse(int(identifier))
                if service.exists():
                    return service
            except (TypeError, ValueError):
                service = Service.search([("service_id", "=", str(identifier))], limit=1)
                if service:
                    return service

        uuid_identifier = metadata.get("rollup_service_uuid") or metadata.get("rollup_id")
        if uuid_identifier:
            service = Service.search([("service_id", "=", str(uuid_identifier))], limit=1)
            if service:
                return service

        deployment_token = metadata.get("deployment_token") or metadata.get("deploymentToken")
        if deployment_token:
            service = Service.search([("deployment_token", "=", deployment_token)], limit=1)
            if service:
                return service

        if subscription_id:
            service = Service.search([("stripe_subscription_id", "=", subscription_id)], limit=1)
            if service:
                return service
            try:
                service = Service.search([("metadata_json", "contains", {"stripe_subscription_id": subscription_id})], limit=1)
                if service:
                    return service
            except Exception:  # pylint: disable=broad-except
                pass

        return Service.browse()

    @api.model
    def _find_from_stripe_invoice_payload(
        cls,
        metadata: Dict[str, Any] | None = None,
        subscription_id: str | None = None,
        invoice_id: str | None = None,
        customer_id: str | None = None,
        payment_intent: str | None = None,
    ):
        """Locate the rollup service referenced within a Stripe invoice event."""

        metadata = metadata or {}
        Service = cls.sudo()

        service_identifier = metadata.get("rollup_service_id") or metadata.get("service_id")
        if service_identifier:
            try:
                service = Service.browse(int(service_identifier))
                if service.exists():
                    return service
            except (TypeError, ValueError):
                service = Service.search([("service_id", "=", str(service_identifier))], limit=1)
                if service:
                    return service

        uuid_identifier = (
            metadata.get("rollup_service_uuid")
            or metadata.get("rollup_id")
            or metadata.get("rollup_service_identifier")
        )
        if uuid_identifier:
            service = Service.search([("service_id", "=", str(uuid_identifier))], limit=1)
            if service:
                return service

        deployment_token = metadata.get("deployment_token") or metadata.get("deploymentToken")
        if deployment_token:
            service = Service.search([("deployment_token", "=", deployment_token)], limit=1)
            if service:
                return service

        if subscription_id:
            service = Service.search([("stripe_subscription_id", "=", subscription_id)], limit=1)
            if service:
                return service

        if payment_intent:
            service = Service.search([("stripe_payment_intent_id", "=", payment_intent)], limit=1)
            if service:
                return service
            try:
                service = Service.search([("metadata_json", "contains", {"stripe_payment_intent_id": payment_intent})], limit=1)
                if service:
                    return service
            except Exception:  # pylint: disable=broad-except
                pass

        if invoice_id:
            service = Service.search([("stripe_invoice_id", "=", invoice_id)], limit=1)
            if service:
                return service

        return Service.browse()

    def _create_nodes_from_inputs(self):
        """Create node records based on payload templates provided by the user."""

        self.ensure_one()
        if self.node_ids:
            return

        inputs = self.inputs_json or {}
        node_templates: Iterable[Dict[str, Any]] = inputs.get("nodes") or inputs.get("node_templates") or []
        if not node_templates:
            return

        node_model = self.env["rollup.node"].sudo()
        valid_types = {key for key, _label in node_model._fields["node_type"].selection}
        valid_statuses = {key for key, _label in node_model._fields["status"].selection}

        for index, template in enumerate(node_templates, start=1):
            template = template or {}
            node_type = template.get("type") or template.get("node_type") or "other"
            if node_type not in valid_types:
                node_type = "other"
            node_status = template.get("status") or "draft"
            if node_status not in valid_statuses:
                node_status = "draft"
            metadata = template.get("metadata")
            if not isinstance(metadata, dict):
                metadata = {}

            node_model.create({
                "service_id": self.id,
                "node_name": template.get("name")
                or template.get("node_name")
                or f"{self.name} Node {index}",
                "node_type": node_type,
                "status": node_status,
                "endpoint_url": template.get("endpoint") or template.get("endpoint_url"),
                "metadata_json": metadata,
            })

    def _update_node_status(self, target_status: str) -> bool:
        """Propagate lifecycle changes onto related rollup nodes."""

        Node = self.env["rollup.node"]
        valid_statuses = {value for value, _label in Node._fields["status"].selection}
        if target_status not in valid_statuses:
            return False

        updated = False
        for service in self:
            nodes = service.node_ids.sudo()
            if not nodes:
                continue
            nodes.write({"status": target_status})
            updated = True
        return updated

    def _get_primary_node_status(self):
        """Return the status of the first linked node (fallback to service status)."""

        self.ensure_one()
        node = self.node_ids[:1]
        return node.status if node else self.status

    def get_rollup_nodes_overview(self):
        """Return a serialized snapshot of linked rollup nodes."""

        self.ensure_one()
        nodes = self.node_ids.sorted(
            key=lambda node: (
                node.create_date or fields.Datetime.from_string("1970-01-01 00:00:00"),
                node.id,
            ),
            reverse=True,
        )
        overview = []
        for node in nodes:
            overview.append({
                "id": node.id,
                "nodid": node.nodid,
                "name": node.node_name,
                "node_type": node.node_type,
                "status": node.status,
                "endpoint_url": node.endpoint_url,
                "metadata": node.get_metadata_dict(),
                "created_at": fields.Datetime.to_string(node.node_created_date) if node.node_created_date else None,
            })
        return overview

    def action_archive(self):
        """Archive the service."""

        for service in self:
            metadata = service._combined_metadata({
                "archived_at": fields.Datetime.now().isoformat(),
            })
            service.write({
                "status": "archived",
                "metadata_json": metadata,
            })
        return True

    def action_view_invoices(self):
        """Open the invoices linked to the service."""

        self.ensure_one()
        action = self.env["ir.actions.actions"]._for_xml_id("account.action_move_out_invoice_type")
        action.setdefault("domain", [])
        action["domain"] = [("rollup_service_id", "=", self.id)]
        context = action.get("context") or {}
        if isinstance(context, str):
            try:
                context = safe_eval(context)
            except Exception:  # pylint: disable=broad-except
                context = {}
        if not isinstance(context, dict):
            context = {}
        context.update({
            "default_rollup_service_id": self.id,
            "default_move_type": "out_invoice",
            "default_partner_id": self.customer_id.id,
            "default_currency_id": self._get_currency().id,
        })
        invoice_vals = self._prepare_invoice_vals()
        default_lines = []
        for command in invoice_vals.get("invoice_line_ids", []):
            if command and command[0] == 0 and len(command) > 2:
                line_vals = dict(command[2])
                default_lines.append((0, 0, line_vals))
        if default_lines:
            context["default_invoice_line_ids"] = default_lines
        if invoice_vals.get("invoice_origin"):
            context["default_invoice_origin"] = invoice_vals["invoice_origin"]
        action["context"] = context
        return action

    def action_view_payments(self):
        """Open payments recorded for the service."""

        self.ensure_one()
        action = self.env["ir.actions.actions"]._for_xml_id("account.action_view_account_payment_tree_all")
        action.setdefault("domain", [])
        action["domain"] = [("rollup_service_id", "=", self.id)]
        action.setdefault("context", {})
        action["context"].update({
            "default_rollup_service_id": self.id,
            "default_partner_id": self.customer_id.id,
            "default_payment_type": "inbound",
            "default_partner_type": "customer",
        })
        return action

    def action_send_invoice_email(self):
        """Allow manual resending of the invoice email."""

        self.ensure_one()
        invoice = self._get_latest_invoice()
        if not invoice:
            raise UserError("No invoice available to send.")
        payment = self.payment_ids.filtered(lambda pay: pay.rollup_invoice_id == invoice)[:1]
        self._send_invoice_email(invoice, payment=payment, force_send=True, bypass_dedup=True)
        message = _("Invoice email sent to %s") % (self.customer_id.email or self.customer_id.display_name)
        return {
            "type": "ir.actions.client",
            "tag": "display_notification",
            "params": {
                "title": _("Invoice Sent"),
                "message": message,
                "type": "success",
            },
        }


    def _link_payment_logs_from_metadata(self, metadata: Dict[str, Any] | None = None):
        """Attach existing Stripe logs to the service when identifiers match."""

        payment_log_model = self.env["stripe.payment.log"].sudo()
        for service in self:
            reference_metadata = metadata if metadata is not None else service.metadata_json or {}
            if not isinstance(reference_metadata, dict):
                reference_metadata = {}

            identifiers = service._prepare_payment_log_search_domains(reference_metadata)
            if not identifiers:
                continue

            logs = payment_log_model.browse()
            for domain in identifiers:
                logs |= payment_log_model.search([("rollup_service_id", "=", False)] + domain)

            if not logs:
                continue

            payment_intent = reference_metadata.get("stripe_payment_intent_id")
            for log in logs:
                values: Dict[str, Any] = {"rollup_service_id": service.id}
                if payment_intent and not log.transaction_hash:
                    values["transaction_hash"] = payment_intent
                log.write(values)

    def _prepare_payment_log_search_domains(self, metadata: Dict[str, Any]):
        """Build candidate domains used to locate related payment logs."""

        domains: list[list[tuple[str, str, Any]]] = []
        payment_intent = metadata.get("stripe_payment_intent_id") or metadata.get("payment_intent_id")
        if payment_intent:
            domains.append([("stripe_payment_intent_id", "=", payment_intent)])
            domains.append([("transaction_hash", "=", payment_intent)])
            domains.append([("event_data", "ilike", payment_intent)])

        session_id = metadata.get("stripe_session_id")
        if session_id:
            domains.append([("event_data", "ilike", session_id)])

        deployment_token = metadata.get("deployment_token") or metadata.get("deploymentToken")
        if deployment_token:
            domains.append([("event_data", "ilike", deployment_token)])

        return domains
    
    def _has_completed_initial_payment(self) -> bool:
        """Return ``True`` once the first subscription invoice has been paid."""

        self.ensure_one()
        metadata = dict(self.metadata_json or {})
        if metadata.get("last_successful_payment_at") or metadata.get("last_recurring_payment_at"):
            return True

        paid_invoices = self.invoice_ids.filtered(
            lambda inv: inv.payment_state in {"paid", "in_payment"}
            and float_is_zero(inv.amount_residual, precision_rounding=inv.currency_id.rounding)
        )
        return bool(paid_invoices)

    


    def create_invoice(self, stripe_data):
        """Create invoice in Odoo after Stripe payment success"""
        try:
            for rec in self:
                invoice_payload = stripe_data or {}
                if (
                    invoice_payload.get("object") == "event"
                    and isinstance(invoice_payload.get("data"), dict)
                    and isinstance(invoice_payload["data"].get("object"), dict)
                ):
                    invoice_payload = invoice_payload["data"]["object"]

                partner = rec.customer_id
                if not partner:
                    raise UserError(_("No partner found for Stripe Customer ID: %s") % rec.stripe_customer_id)

                # Get product based on subscription_type
                product = rec.env["product.product"].sudo().search([('id', 'ilike', rec.type_id.related_product_id.id)], limit=1)
                if not product:
                    raise UserError(_("Product not found for subscription type: %s") % rec.subscription_type)

                # --- Stripe discount extraction ---
                Discount = rec.env["subscription.discount"].sudo()
                discount_record = Discount.browse()
                discount_code_value = None
                discount_amount = 0.0
                original_amount = None
                # Stripe webhook payload fields
                total_discounts = invoice_payload.get("total_discount_amounts") or []
                discounts = invoice_payload.get("discounts") or []
                # Try total_discount_amounts first
                if total_discounts:
                    try:
                        discount_amount = float(total_discounts[0].get("amount", 0)) / 100.0
                        discount_obj = total_discounts[0].get("discount")
                        coupon_id = None
                        if discount_obj:
                            if isinstance(discount_obj, dict):
                                coupon_id = discount_obj.get("coupon") or discount_obj.get("id")
                            else:
                                coupon_id = discount_obj
                        if coupon_id:
                            candidate = Discount.search([("stripe_coupon_id", "=", coupon_id)], limit=1)
                            if candidate:
                                discount_record = candidate
                                discount_code_value = candidate.code or coupon_id
                            else:
                                discount_code_value = coupon_id
                    except Exception:
                        discount_amount = 0.0
                # Fallback: check discounts array
                if not discount_record or not discount_record.exists():
                    for d in discounts:
                        coupon_id = None
                        if isinstance(d, dict):
                            coupon_id = d.get("coupon") or d.get("id")
                        elif isinstance(d, str):
                            coupon_id = d
                        if coupon_id:
                            candidate = Discount.search([("stripe_coupon_id", "=", coupon_id)], limit=1)
                            if candidate:
                                discount_record = candidate
                                discount_code_value = candidate.code or coupon_id
                                break
                            if not discount_code_value:
                                discount_code_value = coupon_id
                # Fallback: check code in invoice metadata
                if not discount_code_value:
                    discount_code_value = invoice_payload.get("discount_code")
                # Fallback: check service context
                if not discount_record or not discount_record.exists():
                    discount_record = getattr(rec, "discount_id", Discount.browse())
                if not discount_code_value:
                    discount_code_value = getattr(rec, "discount_code", None)
                # Original amount
                subtotal = invoice_payload.get("subtotal")
                if subtotal is not None:
                    original_amount = float(subtotal) / 100.0
                if original_amount is None and discount_amount:
                    original_amount = (invoice_payload.get("amount_paid") or invoice_payload.get("amount_due") or 0) / 100.0 + discount_amount

                # Calculate price_unit
                total_candidates = [
                    invoice_payload.get("amount_total"),
                    invoice_payload.get("total"),
                    invoice_payload.get("amount_due"),
                    invoice_payload.get("amount_paid"),
                ]
                price_unit = None
                for candidate in total_candidates:
                    if candidate in (None, "", False):
                        continue
                    try:
                        price_unit = (
                            float(candidate) / 100.0
                            if isinstance(candidate, (int, float))
                            else float(candidate)
                        )
                    except (TypeError, ValueError):
                        continue
                    else:
                        break
                if price_unit is None:
                    price_unit = max((original_amount or 0.0) - discount_amount, 0.0)
                if original_amount is None:
                    original_amount = price_unit + discount_amount
                else:
                    price_unit = original_amount

                # Create Invoice with discount_id in line
                invoice_vals = self._prepare_invoice_vals()
                currency = self._get_currency()
                invoice_vals["currency_id"] = currency.id
                discount_percent = 0.0
                if original_amount:
                    try:
                        discount_percent = max(0.0, min(100.0, (discount_amount / original_amount) * 100.0))
                    except Exception:  # noqa: BLE001 - safe guard against division errors
                        discount_percent = 0.0
                line_commands = []
                for _cmd, _unused, line_vals in invoice_vals.get("invoice_line_ids", []):
                    line_dict = dict(line_vals)
                    line_dict.update({
                        "price_unit": price_unit,
                        "quantity": line_dict.get("quantity", 1.0) or 1.0,
                        "discount": discount_percent,
                    })
                    if discount_record and discount_record.exists():
                        line_dict.update({
                            "discount_id": discount_record.id,
                            "discount_code": discount_code_value or discount_record.code,
                        })
                    elif discount_code_value is not None:
                        line_dict.update({
                            "discount_id": False,
                            "discount_code": discount_code_value or False,
                        })
                    line_commands.append((0, 0, line_dict))
                invoice_vals["invoice_line_ids"] = line_commands
                invoice_ctx = {"allow_invoice_unlink": True}
                invoice = rec.env['account.move'].with_company(invoice_vals['company_id']).sudo().with_context(invoice_ctx).create(invoice_vals)
                invoice.with_context(invoice_ctx).action_post()
                journal = rec.env['account.journal'].search([('type', '=', 'bank')], limit=1)

                service_updates = {
                    "original_amount": original_amount,
                    "discount_amount": discount_amount,
                    "stripe_invoice_id": invoice_payload.get("id") or rec.stripe_invoice_id,
                }
                if discount_record and discount_record.exists():
                    service_updates.update({
                        "discount_id": discount_record.id,
                        "discount_code": discount_code_value or discount_record.code,
                    })
                elif discount_code_value is not None:
                    service_updates.update({
                        "discount_id": False,
                        "discount_code": discount_code_value or False,
                    })
                rec.sudo().write(service_updates)
                if discount_record and discount_record.exists() and discount_amount:
                    discount_record.apply_discount()

                # Register Stripe Payment (mark as Paid)
                payment_vals = {
                    'payment_type': 'inbound',
                    'partner_type': 'customer',
                    'partner_id': partner.id,
                    'amount': invoice.amount_total,
                    'payment_method_id': rec.env.ref('account.account_payment_method_manual_in').id,
                    'journal_id': journal.id,
                    'date': fields.Date.today(),
                    'memo': f"Stripe Payment - {rec.stripe_subscription_id or ''}",
                    "rollup_service_id": rec.id,
                    "rollup_invoice_id": invoice.id,
                    "stripe_invoice_id": invoice_payload.get("id"),
                    "transaction_hash": invoice_payload.get("id"),
                    'currency_id': invoice.currency_id.id,
                }
                payment_vals.setdefault('payment_reference', f'Stripe {invoice_payload.get("id")}')
                payment_method_line = self._get_stripe_payment_method_line(journal)
                if payment_method_line:
                    payment_vals['payment_method_line_id'] = payment_method_line.id
                payment = rec.env['account.payment'].sudo().with_context(invoice_ctx).create(payment_vals)
                payment.with_context(invoice_ctx).action_post()
                (payment.move_id.line_ids + invoice.line_ids).filtered(
                    lambda line: line.account_id.account_type == 'asset_receivable'
                ).reconcile()
                invoice.matched_payment_ids = [(4, payment.id)]
                existing_metadata = dict(rec.metadata_json or {})

                metadata_updates = {
                    "discount_amount": discount_amount,
                    "original_amount": original_amount,
                }
                if invoice_payload.get("id"):
                    metadata_updates["stripe_invoice_id"] = invoice_payload["id"]
                    metadata_updates["last_stripe_invoice_id"] = invoice_payload["id"]
                if discount_code_value is not None:
                    metadata_updates["discount_code"] = discount_code_value
                if discount_record and discount_record.exists():
                    metadata_updates["discount_id"] = str(discount_record.id)

                # Payment timestamp logic
                create_date_local = fields.Datetime.context_timestamp(rec, rec.create_date)
                today_local = fields.Datetime.context_timestamp(rec, datetime.utcnow())
                is_new_subscription = create_date_local.date() == today_local.date()
                timestamp_now = fields.Datetime.now().isoformat()
                if is_new_subscription:
                    metadata_updates["last_successful_payment_at"] = timestamp_now
                else:
                    metadata_updates["last_recurring_payment_at"] = timestamp_now
                    metadata_updates["last_successful_payment_at"] = timestamp_now

                invoice_url_value = invoice_payload.get('hosted_invoice_url') or existing_metadata.get('stripe_invoice_url')
                if invoice_url_value:
                    metadata_updates["stripe_invoice_url"] = invoice_url_value

                mail_context = {
                    "stripe_invoice_url": invoice_url_value,
                    "invoice": invoice,
                    "payment": payment,
                    "payment_category": "new" if is_new_subscription else "renewal",
                    "payment_category_label": "New Subscription Payment" if is_new_subscription else "Renewal Payment",
                }

                # Dispatched via the enhanced _send_invoice_email which handles its own de-duplication
                rec._send_invoice_email(invoice, payment=payment, force_send=True)
                admin_recipients = self._admin_recipient_payload()
                admin_emails = admin_recipients.get('to') or []
                admin_email_kwargs = {}
                if admin_recipients.get('cc'):
                    admin_email_kwargs['email_cc'] = ','.join(admin_recipients['cc'])
                deployment_utils.send_rollup_email(
                    "rollup_management.mail_template_rollup_payment_success_admin",
                    rec,
                    mail_context,
                    email_to=",".join(admin_emails),
                    **admin_email_kwargs,
                )
                rec.write({'metadata_json': rec._combined_metadata({**invoice_payload, **metadata_updates})})
                rec.write({'status': 'deploying'})

                if is_new_subscription:
                    self._send_deployment_notifications(invoice, payment, invoice_url_value or (invoice and invoice.get_portal_url()))
                else:
                    deployment_utils.send_rollup_email(
                        "rollup_management.mail_template_rollup_subscription_renewed",
                        rec,
                        mail_context,
                    )
                return invoice
        except Exception as e:
            _logger.error(f"Failed to create invoice: {str(e)}")
    
    
    # ------------------------------------------------------------------
    # Stripe subscription controls
    # ------------------------------------------------------------------

    def cancel_stripe_rollup_subscription(self):
            """Cancel the Rollup subscription in Stripe"""
            for service in self:
                if service.stripe_subscription_id:
                    try:
                        stripe = rollup_util.get_stripe_client()
                        res  = stripe.Subscription.delete(service.stripe_subscription_id)
                        service.write({
                            'subscription_status': 'cancelled'
                        })
                        _logger.info(f"Stripe subscription {service.stripe_subscription_id} canceled")
                        deployment_utils.send_rollup_cancellation_emails(service)
                    except Exception as e:
                        _logger.error(f"Failed to cancel Stripe subscription {service.stripe_subscription_id}: {str(e)}")

    def action_disable_autopay(self):
        """Disable automatic payments for the linked Stripe subscription."""

        stripe_client = rollup_util.get_stripe_client()
        for service in self:
            metadata_update = {
                "autopay_enabled": False,
                "autopay_updated_at": fields.Datetime.now().isoformat(),
            }
            if not service.stripe_subscription_id:
                service.write(
                    {
                        "autopay_enabled": False,
                        "metadata_json": service._combined_metadata(metadata_update),
                    }
                )
                continue

            if not getattr(stripe_client, "api_key", None):
                raise UserError("Stripe secret key is not configured.")

            try:
                stripe_client.Subscription.modify(
                    service.stripe_subscription_id,
                    collection_method="send_invoice",
                    days_until_due=30,
                    metadata={"autopay_enabled": "false"},
                )
                service.write(
                    {
                        "autopay_enabled": False,
                        "metadata_json": service._combined_metadata(metadata_update),
                    }
                )
            except stripe_client.error.StripeError as exc:  # type: ignore[attr-defined]
                _logger.error("Failed to disable autopay for service %s: %s", service.id, exc)
                raise UserError(f"Failed to disable autopay: {exc}") from exc

        return True

    def action_enable_autopay(self):
        """Enable automatic payments for the linked Stripe subscription."""

        stripe_client = rollup_util.get_stripe_client()
        for service in self:
            metadata_update = {
                "autopay_enabled": True,
                "autopay_updated_at": fields.Datetime.now().isoformat(),
            }
            if not service.stripe_subscription_id:
                service.write(
                    {
                        "autopay_enabled": True,
                        "metadata_json": service._combined_metadata(metadata_update),
                    }
                )
                continue

            if not getattr(stripe_client, "api_key", None):
                raise UserError("Stripe secret key is not configured.")

            try:
                stripe_client.Subscription.modify(
                    service.stripe_subscription_id,
                    collection_method="charge_automatically",
                    metadata={"autopay_enabled": "true"},
                )
                service.write(
                    {
                        "autopay_enabled": True,
                        "metadata_json": service._combined_metadata(metadata_update),
                    }
                )
            except stripe_client.error.StripeError as exc:  # type: ignore[attr-defined]
                _logger.error("Failed to enable autopay for service %s: %s", service.id, exc)
                raise UserError(f"Failed to enable autopay: {exc}") from exc

        return True

    def action_pause_subscription(self):
        """Pause the Stripe subscription and update service status."""

        stripe_client = rollup_util.get_stripe_client()
        for service in self:
            metadata_update = {
                "status_override": "paused",
                "paused_at": fields.Datetime.now().isoformat(),
            }
            if not service.stripe_subscription_id:
                service.write(
                    {
                        "status": "paused",
                        "metadata_json": service._combined_metadata(metadata_update),
                    }
                )
                continue

            if not getattr(stripe_client, "api_key", None):
                raise UserError("Stripe secret key is not configured.")

            try:
                stripe_client.Subscription.modify(
                    service.stripe_subscription_id,
                    pause_collection={"behavior": "mark_uncollectible"},
                )
                service.write(
                    {
                        "status": "paused",
                        "metadata_json": service._combined_metadata(metadata_update),
                    }
                )
            except stripe_client.error.StripeError as exc:  # type: ignore[attr-defined]
                _logger.error("Failed to pause subscription for service %s: %s", service.id, exc)
                raise UserError(f"Failed to pause subscription: {exc}") from exc

        return True

    def action_resume_subscription(self):
        """Resume a previously paused Stripe subscription."""

        stripe_client = rollup_util.get_stripe_client()
        for service in self:
            metadata_update = {
                "status_override": "active",
                "resumed_at": fields.Datetime.now().isoformat(),
            }
            if not service.stripe_subscription_id:
                service.write(
                    {
                        "status": "active",
                        "metadata_json": service._combined_metadata(metadata_update),
                    }
                )
                continue

            if not getattr(stripe_client, "api_key", None):
                raise UserError("Stripe secret key is not configured.")

            try:
                stripe_client.Subscription.modify(
                    service.stripe_subscription_id,
                    pause_collection="",
                )
                service.write(
                    {
                        "status": "active",
                        "metadata_json": service._combined_metadata(metadata_update),
                    }
                )
            except stripe_client.error.StripeError as exc:  # type: ignore[attr-defined]
                _logger.error("Failed to resume subscription for service %s: %s", service.id, exc)
                raise UserError(f"Failed to resume subscription: {exc}") from exc

        return True

    @api.model
    def cron_check_overdue_subscriptions(self):
        """Delegate the overdue safety audit to the utility helpers."""

        return deployment_utils.cron_audit_overdue_subscriptions(self.env)
    
    @api.model
    def cron_send_subscription_reminders(self):
        """Delegate the overdue safety audit to the utility helpers."""

        return deployment_utils.cron_send_subscription_reminders(self.env)

    # ------------------------------------------------------------------
    # Odoo-Managed Recurrence (V2)
    # ------------------------------------------------------------------

    def action_charge_rollup(self):
        """Manual trigger for Odoo-managed charge."""
        self.ensure_one()
        return deployment_utils.action_charge_rollup(self)

    def action_migrate_to_odoo_managed(self):
        """Transition from Stripe-managed to Odoo-managed."""
        self.ensure_one()
        return deployment_utils.action_migrate_rollup_to_odoo_managed(self)

    @api.model
    def run_rollup_billing_cron(self):
        """Automated charge engine for all due rollups."""
        return deployment_utils.run_rollup_billing_cron(self.env)
    
    @api.model
    def cron_migrate_rollups_to_odoo_managed(self):
        """Automated batch migration."""
        return deployment_utils.cron_migrate_rollups_to_odoo_managed(self.env)
