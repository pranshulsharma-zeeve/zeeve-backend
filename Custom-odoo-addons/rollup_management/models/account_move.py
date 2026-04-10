"""Account move and payment extensions for rollup services."""

from __future__ import annotations
from odoo import _, release
import base64
import logging
import os
import shutil
import sys

from odoo import _, api, fields, models
from odoo.tools.float_utils import float_is_zero
from odoo.exceptions import UserError
from odoo.tools.safe_eval import safe_eval
from odoo.tools import config as odoo_config

_logger = logging.getLogger(__name__)


class AccountMove(models.Model):
    """Link invoices to rollup services and expose payment shortcuts."""

    _inherit = "account.move"

    rollup_service_id = fields.Many2one(
        "rollup.service",
        string="Rollup Service",
        index=True,
        ondelete="set null",
    )
    stripe_invoice_id = fields.Char(string="Stripe Invoice ID", index=True)
    stripe_payment_intent_id = fields.Char(string="Stripe Payment Intent", index=True)
    stripe_transaction_reference = fields.Char(string="Transaction Reference", index=True)
    rollup_payment_ids = fields.One2many(
        "account.payment",
        "rollup_invoice_id",
        string="Rollup Payments",
        readonly=True,
    )
    rollup_service_identifier = fields.Char(
        related="rollup_service_id.service_id",
        string="Rollup Identifier",
        store=True,
        readonly=True,
    )
    rollup_service_name = fields.Char(
        related="rollup_service_id.name",
        string="Rollup Service Name",
        store=True,
        readonly=True,
    )
    rollup_type_id = fields.Many2one(
        related="rollup_service_id.type_id",
        string="Rollup Type",
        store=True,
        readonly=True,
    )
    rollup_type_code = fields.Char(
        related="rollup_service_id.type_id.rollup_id",
        string="Rollup Type Code",
        store=True,
        readonly=True,
    )
    rollup_payment_count = fields.Integer(
        string="Payments",
        compute="_compute_rollup_payment_count",
        readonly=True,
    )
    invoice_customer_email = fields.Char(
        string="Customer Email",
        compute="_compute_invoice_summary_fields",
        store=True,
        readonly=True,
    )
    invoice_start_date = fields.Datetime(
        string="Start Date",
        compute="_compute_invoice_summary_fields",
        store=True,
        readonly=True,
    )
    invoice_end_date = fields.Datetime(
        string="End Date",
        compute="_compute_invoice_summary_fields",
        store=True,
        readonly=True,
    )
    invoice_item_name = fields.Char(
        string="Item",
        compute="_compute_invoice_summary_fields",
        store=True,
        readonly=True,
    )
    invoice_protocol_or_rollup = fields.Char(
        string="Protocol / Rollup",
        compute="_compute_invoice_summary_fields",
        store=True,
        readonly=True,
    )
    invoice_quantity = fields.Float(
        string="Quantity",
        compute="_compute_invoice_summary_fields",
        store=True,
        readonly=True,
    )

    def _compute_rollup_payment_count(self):
        for move in self:
            move.rollup_payment_count = len(move.rollup_payment_ids)

    @api.depends(
        "partner_id.email",
        "subscription_id.customer_email",
        "subscription_id.stripe_start_date",
        "subscription_id.stripe_end_date",
        "subscription_id.protocol_id.name",
        "subscription_id.quantity",
        "rollup_service_id.customer_id.email",
        "rollup_service_id.create_date",
        "rollup_service_id.next_billing_date",
        "rollup_type_id.name",
        "invoice_line_ids.product_id",
        "invoice_line_ids.name",
        "invoice_line_ids.quantity",
        "invoice_line_ids.display_type",
    )
    def _compute_invoice_summary_fields(self):
        for move in self:
            subscription = move.subscription_id
            rollup = move.rollup_service_id
            line = next(
                (
                    line
                    for line in move.invoice_line_ids
                    if line.display_type not in {"line_section", "line_note"}
                ),
                False,
            )

            customer_email = False

            if subscription and subscription.customer_email:
                customer_email = subscription.customer_email
            elif rollup and rollup.customer_id and rollup.customer_id.email:
                customer_email = rollup.customer_id.email
            else:
                customer_email = move.partner_id.email

            move.invoice_customer_email = customer_email
            move.invoice_start_date = (
                subscription.stripe_start_date
                if subscription and subscription.stripe_start_date
                else rollup.create_date if rollup else False
            )
            move.invoice_end_date = (
                subscription.stripe_end_date
                if subscription and subscription.stripe_end_date
                else rollup.next_billing_date if rollup else False
            )
            move.invoice_item_name = (
                line.product_id.display_name
                if line and line.product_id
                else line.name if line else False
            )
            move.invoice_protocol_or_rollup = (
                subscription.protocol_id.name
                if subscription and subscription.protocol_id
                else rollup.type_id.name if rollup and rollup.type_id else False
            )
            move.invoice_quantity = (
                subscription.quantity
                if subscription and subscription.quantity
                else line.quantity if line else 0.0
            )


    def action_view_rollup_payments(self):
        self.ensure_one()

        # Odoo 17+ uses 'list' instead of 'tree'
        major = getattr(release, "version_info", (0,))[0] or 0
        use_list = major >= 17
        view_mode = 'list,form' if use_list else 'tree,form'

        action = {
            'type': 'ir.actions.act_window',
            'name': _('Rollup Payments'),
            'res_model': 'account.payment',
            'view_mode': view_mode,
            'domain': [('id', 'in', self.rollup_payment_ids.ids)],
            'context': {
                'default_rollup_invoice_id': self.id,
                'default_rollup_service_id': self.rollup_service_id.id,
                'default_partner_id': self.partner_id.id,
                'default_payment_type': 'inbound',
                'default_partner_type': 'customer',
            },
            'target': 'current',
        }

        # Optional: try to force specific views if available
        try:
            if use_list:
                list_view = self.env.ref('account.view_account_payment_list').id
                form_view = self.env.ref('account.view_account_payment_form').id
                action['views'] = [(list_view, 'list'), (form_view, 'form')]
            else:
                tree_view = self.env.ref('account.view_account_payment_tree').id
                form_view = self.env.ref('account.view_account_payment_form').id
                action['views'] = [(tree_view, 'tree'), (form_view, 'form')]
        except Exception:
            action.pop('views', None)  # fall back to default views

        return action


    @api.model_create_multi
    def create(self, vals_list):
        records = super().create(vals_list)
        for record, vals in zip(records, vals_list):
            if not record.rollup_service_id and vals.get("rollup_service_id"):
                continue
            if record.rollup_service_id:
                continue
            service = False
            if vals.get("rollup_service_id"):
                service = self.env["rollup.service"].browse(vals["rollup_service_id"])
            if not service and record.invoice_origin:
                service = self.env["rollup.service"].search([("service_id", "=", record.invoice_origin)], limit=1)
            if service:
                record.rollup_service_id = service.id
        return records

    def action_send_rollup_invoice_email(self):
        """Allow accountants to resend the rollup invoice email from the invoice form."""

        self.ensure_one()
        if self.move_type != 'out_invoice':
            raise UserError(_('Only customer invoices can be emailed.'))
        if not self.rollup_service_id:
            raise UserError(_('This invoice is not linked to a rollup service.'))

        service = self.rollup_service_id.sudo()
        payment = self.rollup_payment_ids[:1]
        service._send_invoice_email(self, payment=payment, force_send=True)

        message = _("Invoice email sent to %s") % (self.partner_id.email or self.partner_id.display_name)
        return {
            "type": "ir.actions.client",
            "tag": "display_notification",
            "params": {
                "title": _("Invoice Sent"),
                "message": message,
                "type": "success",
            },
        }

    def _ensure_rollup_invoice_pdf(self):
        """Generate a PDF attachment for rollup invoices using the dedicated report."""

        self.ensure_one()
        if not self.rollup_service_id:
            return

        if not self._ensure_wkhtmltopdf_binary():
            _logger.warning(
                "wkhtmltopdf binary not found for invoice %s; skipping custom PDF generation.",
                self.id,
            )
            return

        report = self.env.ref(
            "rollup_management.action_report_rollup_invoice", raise_if_not_found=False
        )
        if not report:
            return

        try:
            pdf_content, _ = self._render_rollup_invoice_pdf(report)
        except UserError as exc:  # Missing wkhtmltopdf or template errors
            _logger.warning("Unable to render rollup invoice PDF %s: %s", self.id, exc)
            return
        except Exception as exc:  # pylint: disable=broad-except
            _logger.exception("Unexpected error rendering rollup invoice %s", self.id)
            return

        if not pdf_content:
            return

        filename = self._get_invoice_report_filename()
        attachment_vals = {
            "name": filename,
            "type": "binary",
            "datas": base64.b64encode(pdf_content),
            "mimetype": "application/pdf",
            "res_model": "account.move",
            "res_id": self.id,
        }

        attachment = self.invoice_pdf_report_id.sudo()
        if attachment:
            attachment.write({
                "name": attachment_vals["name"],
                "datas": attachment_vals["datas"],
                "mimetype": attachment_vals["mimetype"],
            })
        else:
            attachment = self.env["ir.attachment"].sudo().create(attachment_vals)
            self.sudo().write({"invoice_pdf_report_id": attachment.id})

    def _render_rollup_invoice_pdf(self, report):
        """Render the rollup invoice report (isolated for easier testing)."""

        # Call the report engine with the proper report reference and target ids.
        docids = list(self.ids)
        return report._render_qweb_pdf(report.id, docids)

    def _ensure_wkhtmltopdf_binary(self):
        """Ensure wkhtmltopdf command is discoverable for report rendering."""

        if shutil.which("wkhtmltopdf"):
            return True

        Config = self.env["ir.config_parameter"].sudo()
        custom_path = Config.get_param("rollup_management.wkhtmltopdf_binary")

        candidates = []
        if custom_path:
            candidates.append(custom_path)
        venv_candidate = os.path.join(os.path.dirname(sys.executable), "wkhtmltopdf")
        candidates.append(venv_candidate)

        for candidate in candidates:
            if not candidate:
                continue
            bin_path = None
            if os.path.isfile(candidate) and os.access(candidate, os.X_OK):
                bin_path = os.path.dirname(candidate)
            elif os.path.isdir(candidate):
                executable = os.path.join(candidate, "wkhtmltopdf")
                if os.path.isfile(executable) and os.access(executable, os.X_OK):
                    bin_path = candidate
            if not bin_path:
                continue
            self._prepend_to_path(bin_path)
            # also hint Odoo's config for find_in_path fallback
            if not odoo_config.get("bin_path"):
                odoo_config["bin_path"] = bin_path
            if shutil.which("wkhtmltopdf"):
                return True

        return bool(shutil.which("wkhtmltopdf"))

    @staticmethod
    def _prepend_to_path(directory):
        """Add directory to PATH so Odoo's report engine can locate binaries."""

        if not directory:
            return
        path_parts = [part for part in os.environ.get("PATH", "").split(os.pathsep) if part]
        if directory in path_parts:
            return
        os.environ["PATH"] = os.pathsep.join([directory] + path_parts)

    def _get_invoice_legal_documents(self, filetype, allow_fallback=False):
        """Ensure rollup invoices always attempt to use the bespoke PDF template."""

        if filetype == "pdf":
            for move in self:
                if move.rollup_service_id:
                    move._ensure_rollup_invoice_pdf()
        return super()._get_invoice_legal_documents(filetype, allow_fallback=allow_fallback)

    def action_register_payment(self):
        """Inject rollup defaults when registering payments from the invoice."""

        self.ensure_one()
        if not self.rollup_service_id:
            return super().action_register_payment()

        ctx = dict(self.env.context)
        ctx.setdefault("default_rollup_invoice_id", self.id)
        ctx.setdefault("default_rollup_service_id", self.rollup_service_id.id)
        ctx.setdefault("default_partner_id", self.partner_id.id)
        ctx.setdefault("default_payment_type", "inbound")
        ctx.setdefault("default_partner_type", "customer")
        ctx.setdefault("active_model", "account.move")
        ctx.setdefault("active_ids", self.ids)
        return super(AccountMove, self.with_context(ctx)).action_register_payment()


class AccountPayment(models.Model):
    """Track payments originating from rollup invoices."""

    _inherit = "account.payment"

    rollup_service_id = fields.Many2one(
        "rollup.service",
        string="Rollup Service",
        index=True,
        ondelete="set null",
    )
    rollup_invoice_id = fields.Many2one(
        "account.move",
        string="Rollup Invoice",
        index=True,
        ondelete="set null",
    )
    stripe_payment_intent_id = fields.Char(string="Stripe Payment Intent", index=True)
    stripe_invoice_id = fields.Char(string="Stripe Invoice ID", index=True)
    transaction_hash = fields.Char(string="Transaction Hash", index=True)
    rollup_service_identifier = fields.Char(
        related="rollup_service_id.service_id",
        string="Rollup Identifier",
        store=True,
        readonly=True,
    )
    rollup_service_name = fields.Char(
        related="rollup_service_id.name",
        string="Rollup Service Name",
        store=True,
        readonly=True,
    )
    rollup_type_id = fields.Many2one(
        related="rollup_service_id.type_id",
        string="Rollup Type",
        store=True,
        readonly=True,
    )
    rollup_type_code = fields.Char(
        related="rollup_service_id.type_id.rollup_id",
        string="Rollup Type Code",
        store=True,
        readonly=True,
    )

    def _sync_rollup_links(self):
        """Ensure created or updated payments keep rollup relations in sync.

        Called after rollup payments are created, posted, or written to. When
        the payment wizard finishes reconciliation we also call this method to
        retro-fit rollup links on the freshly created account.payment.
        """
        try:
            for payment in self:
                sale_invoices = payment.rollup_invoice_id | payment.invoice_ids | payment.reconciled_invoice_ids
                sale_invoices = sale_invoices.filtered(lambda move: move.move_type in ('out_invoice', 'out_refund', 'out_receipt'))
                invoice = sale_invoices[:1]
                if not invoice and payment.move_id:
                    reconciled_moves = payment.move_id.line_ids.mapped('matched_debit_ids.debit_move_id.move_id')
                    reconciled_moves |= payment.move_id.line_ids.mapped('matched_credit_ids.credit_move_id.move_id')
                    invoice = reconciled_moves.filtered(
                        lambda move: move.move_type in ('out_invoice', 'out_refund', 'out_receipt') and move.rollup_service_id
                    )[:1]
                    sale_invoices |= invoice
                service = invoice.rollup_service_id if invoice else payment.rollup_service_id

                updates = {}
                if invoice and payment.rollup_invoice_id != invoice:
                    updates["rollup_invoice_id"] = invoice.id
                if service and payment.rollup_service_id != service:
                    updates["rollup_service_id"] = service.id
                if updates:
                    payment.with_context(skip_rollup_sync=True).sudo().write(updates)

        except Exception as exc:  # pylint: disable=broad-except
            _logger.exception("Failed to sync rollup links for payments: %s", exc)

    @api.model_create_multi
    def create(self, vals_list):
        records = super().create(vals_list)
        records._sync_rollup_links()
        return records

    def write(self, vals):
        res = super().write(vals)
        if not self.env.context.get("skip_rollup_sync"):
            self._sync_rollup_links()
        return res

    def _compute_state(self):
        """Ensure rollup payments mirror their invoice payment status."""

        super()._compute_state()
        for payment in self:
            if payment.state in {"paid"}:
                continue

            invoices = payment.rollup_invoice_id | payment.invoice_ids | payment.reconciled_invoice_ids
            invoices = invoices.filtered(lambda move: move.move_type in ("out_invoice", "out_refund", "out_receipt"))
            if not invoices:
                continue

            if not payment.rollup_service_id and all(not inv.rollup_service_id for inv in invoices):
                continue

            if all(inv.payment_state == "paid" for inv in invoices):
                payment.state = "paid"

    # def action_post(self):
    #     res = super().action_post()
    #     self._sync_rollup_links()
    #     self._update_rollup_status_from_payment()
    #     return res

    def _update_rollup_status_from_payment(self):
        """Update the rollup service when payments are posted and invoices paid.

        This runs after posting or manually writing rollup payments. Once the
        linked invoice is settled (including the in_payment + zero residual
        edge case) the rollup service transitions to deploying and mails go
        out.
        """

        for payment in self:
            invoices = payment.rollup_invoice_id | payment.invoice_ids | payment.reconciled_invoice_ids
            invoices = invoices.filtered(lambda move: move.move_type in ("out_invoice", "out_refund", "out_receipt"))
            invoice = invoices[:1]
            if not invoice:
                continue

            service = payment.rollup_service_id or invoice.rollup_service_id
            if service and payment.rollup_service_id != service:
                payment.with_context(skip_rollup_sync=True).sudo().write({"rollup_service_id": service.id})
            if not service:
                continue

            if invoice.state == "draft":
                invoice.sudo().action_post()

            if payment.rollup_invoice_id != invoice:
                payment.with_context(skip_rollup_sync=True).sudo().write({"rollup_invoice_id": invoice.id})

            invoice.invalidate_recordset(["payment_state", "amount_residual"])
            paid_enough = float_is_zero(
                invoice.amount_residual,
                precision_rounding=invoice.currency_id.rounding,
            )
            if invoice.payment_state == "paid" or (invoice.payment_state == "in_payment" and paid_enough):
                service._handle_invoice_paid(invoice, payment)

    def action_view_rollup_invoice(self):
        """Open the linked invoice in form view when triggered from the smart button."""

        self.ensure_one()
        if not self.rollup_invoice_id:
            return False
        action = self.env["ir.actions.actions"]._for_xml_id("account.action_move_out_invoice_type")
        form_view = self.env.ref("account.view_move_form")
        context = dict(self.env.context)
        if action.get("context"):
            if isinstance(action["context"], str):
                context.update(safe_eval(action["context"]))
            elif isinstance(action["context"], dict):
                context.update(action["context"])
        context.update(
            {
                "default_rollup_service_id": self.rollup_service_id.id,
                "default_move_type": "out_invoice",
            }
        )
        action.update(
            {
                "view_mode": "form",
                "views": [(form_view.id, "form")],
                "res_id": self.rollup_invoice_id.id,
                "context": context,
            }
        )
        return action

    def action_view_rollup_service(self):
        """Open the related rollup service."""

        self.ensure_one()
        if not self.rollup_service_id:
            return False
        action = self.env["ir.actions.actions"]._for_xml_id("rollup_management.action_rollup_service")
        form_view = self.env.ref("rollup_management.view_rollup_service_form")
        context = dict(self.env.context)
        if action.get("context"):
            if isinstance(action["context"], str):
                context.update(safe_eval(action["context"]))
            elif isinstance(action["context"], dict):
                context.update(action["context"])
        context.update({"default_rollup_service_id": self.rollup_service_id.id})
        action.update(
            {
                "view_mode": "form",
                "views": [(form_view.id, "form")],
                "res_id": self.rollup_service_id.id,
                "context": context,
            }
        )
        return action
