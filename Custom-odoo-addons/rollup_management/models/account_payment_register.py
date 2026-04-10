"""Extend payment register wizard to carry rollup links onto payments."""

from __future__ import annotations

from odoo import models


class AccountPaymentRegister(models.TransientModel):
    _inherit = "account.payment.register"

    def _rollup_invoice_from_lines(self, lines):
        """Return the first rollup invoice detected on the provided lines."""

        if not lines:
            return self.env["account.move"]
        invoices = lines.mapped("move_id").filtered(
            lambda move: move.move_type in ("out_invoice", "out_refund", "out_receipt")
            and move.rollup_service_id
        )
        return invoices[:1]

    def _inject_rollup_links(self, payment_vals, lines):
        """Attach rollup metadata to payment creation values when relevant.

        Ensures payments born from the wizard already know which rollup invoice
        and service they belong to, avoiding post-processing gaps.
        """

        invoice = self._rollup_invoice_from_lines(lines)
        if not invoice:
            return payment_vals

        if not payment_vals.get("rollup_invoice_id"):
            payment_vals["rollup_invoice_id"] = invoice.id
        if invoice.rollup_service_id and not payment_vals.get("rollup_service_id"):
            payment_vals["rollup_service_id"] = invoice.rollup_service_id.id
        if invoice.rollup_service_id and not payment_vals.get("memo"):
            payment_vals["memo"] = invoice.rollup_service_id.service_id
        return payment_vals

    def _create_payment_vals_from_wizard(self, batch_result):
        payment_vals = super()._create_payment_vals_from_wizard(batch_result)
        lines = batch_result.get("lines") or self.line_ids
        return self._inject_rollup_links(payment_vals, lines)

    def _create_payment_vals_from_batch(self, batch_result):
        payment_vals = super()._create_payment_vals_from_batch(batch_result)
        lines = batch_result.get("lines")
        return self._inject_rollup_links(payment_vals, lines)

    def _reconcile_payments(self, to_process, edit_mode=False):
        """Let the standard wizard reconcile, then sync rollup bookkeeping.

        The wizard creates payments without rollup metadata; once the base
        reconciliation is done we attach rollup links and trigger the usual
        post-payment hooks so manual flows mirror the automated Stripe flow.
        """
        res = super()._reconcile_payments(to_process, edit_mode=edit_mode)
        payments = self.env["account.payment"].browse()
        for vals in to_process:
            payment = vals.get("payment")
            if not payment:
                continue
            payments |= payment

        if payments:
            payments._sync_rollup_links()
            payments._update_rollup_status_from_payment()
        return res
