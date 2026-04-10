# -*- coding: utf-8 -*-
"""Simplified helpers for importing Zoho invoices."""
from __future__ import annotations

import json
import logging
from decimal import Decimal
from typing import Any, Dict, List, Optional

from odoo import _, fields
from odoo.exceptions import UserError

_logger = logging.getLogger(__name__)


class InvoiceImportUtils:
    """Utility helpers to map a Zoho invoice row into Odoo."""

    @staticmethod
    def handle_invoice_row(env, row: Dict[str, Any]):
        cleaned = InvoiceImportUtils._normalize_row(row)
        invoice_code = cleaned.get("Invoice Number") or cleaned.get("Invoice ID")
        if not invoice_code:
            raise UserError(_("Missing Invoice ID/Number."))

        subscription, rollup_service = InvoiceImportUtils._locate_owner(env, cleaned)
        if not subscription and not rollup_service:
            raise UserError(
                _(
                    "Unable to link invoice %(invoice)s (reference %(reference)s) to any subscription/rollup."
                )
                % {
                    "invoice": invoice_code,
                    "reference": InvoiceImportUtils._primary_reference(cleaned) or _("unknown"),
                }
            )

        partner = subscription.customer_name if subscription else rollup_service.customer_id
        if not partner:
            raise UserError(
                _("Invoice %(invoice)s has no customer record to attach to.")
                % {"invoice": invoice_code}
            )

        company = (
            subscription.company_id
            or getattr(rollup_service, "company_id", False)
            or env.company
        )
        currency = InvoiceImportUtils._resolve_currency(env, cleaned.get("Currency Code"), company)
        journal = InvoiceImportUtils._resolve_sale_journal(env, company)

        line_vals = InvoiceImportUtils._build_invoice_line(env, cleaned, subscription, rollup_service)

        invoice_vals = {
            "move_type": "out_invoice",
            "partner_id": partner.id,
            "invoice_date": InvoiceImportUtils._to_date(cleaned.get("Invoice Date")),
            "invoice_date_due": InvoiceImportUtils._to_date(cleaned.get("Due Date"))
            or InvoiceImportUtils._to_date(cleaned.get("Expected Payment Date")),
            "ref": invoice_code,
            "currency_id": currency.id,
            "company_id": company.id,
            "journal_id": journal.id,
            "invoice_line_ids": [(0, 0, line_vals)],
            "narration": InvoiceImportUtils._build_narration(cleaned),
        }
        if subscription:
            invoice_vals.update({"subscription_id": subscription.id, "is_subscription": True})
        if rollup_service:
            invoice_vals["rollup_service_id"] = rollup_service.id
            invoice_vals.setdefault("invoice_origin", rollup_service.service_id)

        invoice = env["account.move"].with_company(company.id).sudo().create(invoice_vals)

        status = (cleaned.get("Invoice Status") or "").strip().lower()
        should_post = status not in {
            "draft",
            "void",
            "voided",
            "deleted",
            "cancelled",
            "canceled",
        }

        payment_message = ""
        if should_post:
            invoice.action_post()
            try:
                payment_message = InvoiceImportUtils._create_payment_if_needed(
                    env, invoice, cleaned, subscription, rollup_service
                )
            except Exception as exc:  # pylint: disable=broad-except
                _logger.exception("Failed to create payment for invoice %s", invoice_code)
                payment_message = _("Payment creation failed: %s") % exc

        owner_label = rollup_service.display_name if rollup_service else subscription.display_name
        message = _(
            "Invoice %(invoice)s created for %(partner)s (%(owner)s)."
        ) % {
            "invoice": invoice.name or invoice.display_name,
            "partner": partner.display_name,
            "owner": owner_label,
        }
        if payment_message:
            message += " " + payment_message

        status_label = "success" if should_post else "partial"
        return {
            "status": status_label,
            "message": message,
            "invoice_id": invoice.id,
        }

    # ------------------------------------------------------------------
    # Invoice helpers
    # ------------------------------------------------------------------
    @staticmethod
    def _normalize_row(row: Dict[str, Any]) -> Dict[str, Any]:
        cleaned = {}
        for key, value in (row or {}).items():
            cleaned[key] = value.strip() if isinstance(value, str) else value
        return cleaned

    @staticmethod
    def _primary_reference(row: Dict[str, Any]) -> Optional[str]:
        keys = [
            "CF.reference_id",
            "Reference ID",
            "Node ID",
            "node_id",
            "subscription_id",
        ]
        for key in keys:
            value = InvoiceImportUtils._clean_identifier(row.get(key))
            if value:
                return value
        return None

    @staticmethod
    def _locate_owner(env, row: Dict[str, Any]):
        Subscription = env["subscription.subscription"].sudo()
        RollupService = env["rollup.service"].sudo()

        sub_id = InvoiceImportUtils._normalize_numeric_identifier(row.get("Subscription ID"))
        subscription = False
        rollup_service = False

        # Prefer explicit Zoho identifiers when present.
        if sub_id and 'zoho_subscription_id' in Subscription._fields:
            subscription = Subscription.search([('zoho_subscription_id', '=', sub_id)], limit=1)
        if not subscription and sub_id and 'zoho_service_id' in RollupService._fields:
            rollup_service = RollupService.search([('zoho_service_id', '=', sub_id)], limit=1)
        print("subscc",sub_id,subscription)

        identifier_candidates: List[str] = []
        primary = InvoiceImportUtils._primary_reference(row)
        if primary:
            identifier_candidates.append(primary)
        identifier_candidates.extend(InvoiceImportUtils._extract_network_ids(row))
        if sub_id:
            identifier_candidates.append(sub_id)

        identifier_candidates = [ident for ident in identifier_candidates if ident]

        if not subscription:
            for ident in identifier_candidates:
                subscription = Subscription.search(
                    [
                        "|",
                        ("subscription_uuid", "=", ident),
                        ("subscription_ref", "=", ident),
                    ],
                    limit=1,
                )
                if subscription:
                    break

        if not rollup_service:
            for ident in identifier_candidates:
                rollup_service = RollupService.search([("service_id", "=", ident)], limit=1)
                if rollup_service:
                    break

        if not subscription and sub_id:
            subscription = Subscription.search(
                [
                    "|",
                    ("stripe_subscription_id", "=", sub_id),
                    ("subscription_ref", "=", sub_id),
                ],
                limit=1,
            )
        return subscription, rollup_service

    @staticmethod
    def _extract_network_ids(row: Dict[str, Any]) -> List[str]:
        raw_value = row.get("CF.updatedValue") or row.get("CF.Network_ids")
        if not raw_value:
            return []
        try:
            payload = json.loads(raw_value) if isinstance(raw_value, str) else raw_value
        except (json.JSONDecodeError, TypeError):
            return []
        items = payload if isinstance(payload, list) else payload.get("updatedValue") if isinstance(payload, dict) else []
        identifiers: List[str] = []
        for item in items:
            if isinstance(item, dict):
                ident = InvoiceImportUtils._clean_identifier(item.get("networkId"))
                if ident:
                    identifiers.append(ident)
        return identifiers

    @staticmethod
    def _build_invoice_line(env, row, subscription, rollup_service):
        product = False
        if subscription and subscription.sub_plan_id and subscription.sub_plan_id.product_id:
            product = subscription.sub_plan_id.product_id
        elif rollup_service:
            try:
                product = rollup_service._get_invoice_product()
            except Exception:  # pylint: disable=broad-except
                product = False
        if not product:
            raise UserError(_("Unable to resolve product for invoice line."))

        quantity = InvoiceImportUtils._to_float(row.get("Quantity"), default=1.0) or 1.0
        price = InvoiceImportUtils._to_float(row.get("Item Price"))
        if not price and row.get("Item Total"):
            price = InvoiceImportUtils._to_float(row.get("Item Total")) / quantity
        if not price and row.get("Total"):
            price = InvoiceImportUtils._to_float(row.get("Total")) / quantity
        price = price or product.lst_price or 0.0

        discount = InvoiceImportUtils._to_float(row.get("Discount(%)"))

        tax_ids = [(6, 0, product.taxes_id.ids)]
        is_inclusive_tax = row.get("Is Inclusive Tax")
        if is_inclusive_tax is not None and (str(is_inclusive_tax).lower() == "false" or is_inclusive_tax is False):
            tax_ids = [(6, 0, [])]
        return {
            "product_id": product.id,
            "quantity": quantity,
            "price_unit": price,
            "discount": discount,
            "name": row.get("Item Name") or product.name,
            "tax_ids": tax_ids,
        }

    @staticmethod
    def _build_narration(row: Dict[str, Any]) -> Optional[str]:
        notes = InvoiceImportUtils._clean_identifier(row.get("Notes"))
        terms = InvoiceImportUtils._clean_identifier(row.get("Terms & Conditions"))
        if notes and terms:
            return f"{notes}\n{terms}"
        return notes or terms

    # ------------------------------------------------------------------
    # Payment helpers
    # ------------------------------------------------------------------
    @staticmethod
    def _create_payment_if_needed(env, invoice, row, subscription, rollup_service):
        balance = InvoiceImportUtils._to_float(row.get("Balance"))
        status = (row.get("Invoice Status") or "").strip().lower()
        fully_paid = balance == 0 or status in {"closed", "paid", "succeeded"}
        if not fully_paid:
            return ""

        journal = invoice.journal_id or InvoiceImportUtils._resolve_payment_journal(env, invoice.company_id)
        payment_method_line = InvoiceImportUtils._find_payment_method_line(journal)

        payment_vals = {
            "payment_type": "inbound",
            "partner_type": "customer",
            "partner_id": invoice.partner_id.id,
            "amount": invoice.amount_total,
            "currency_id": invoice.currency_id.id,
            "date": InvoiceImportUtils._resolve_payment_date(row)
            or invoice.invoice_date
            or fields.Date.context_today(invoice),
            "journal_id": journal.id,
            "payment_method_line_id": payment_method_line.id,
            "company_id": invoice.company_id.id,
            "memo": _("Zoho - %s") % (
                InvoiceImportUtils._clean_identifier(row.get("Payment ID")) or invoice.payment_reference or invoice.name
            ),
        }
        if subscription:
            payment_vals["subscription_id"] = subscription.id
        if rollup_service:
            payment_vals["rollup_service_id"] = rollup_service.id
            payment_vals["rollup_invoice_id"] = invoice.id

        payment_env = env["account.payment"].with_company(invoice.company_id.id).sudo()
        payment = payment_env.create(payment_vals)
        payment.action_post()
        (payment.move_id.line_ids + invoice.line_ids).filtered(
            lambda line: line.account_id.account_type == "asset_receivable"
        ).reconcile()

        # Logging for payment and invoice state
        _logger.info(
            "Invoice %s posted, payment %s created and posted. Invoice state: %s, payment state: %s, payment amount: %s, invoice residual: %s",
            invoice.id,
            payment.id,
            invoice.state,
            payment.state,
            payment.amount,
            invoice.amount_residual,
        )
        if invoice.payment_state != "paid":
            _logger.warning(
                "Invoice %s is not marked as paid after payment. State: %s, Residual: %s",
                invoice.id,
                invoice.payment_state,
                invoice.amount_residual,
            )

        amount_display = f"{invoice.currency_id.symbol or invoice.currency_id.name} {invoice.amount_total:.2f}"
        return _("Payment posted for %(amount)s.") % {"amount": amount_display}

    @staticmethod
    def _resolve_payment_date(row):
        for key in ("Last Payment Date", "Payment Date", "Expected Payment Date"):
            value = InvoiceImportUtils._to_date(row.get(key))
            if value:
                return value
        return False

    # ------------------------------------------------------------------
    # Lookups
    # ------------------------------------------------------------------
    @staticmethod
    def _resolve_currency(env, currency_code, company):
        if currency_code:
            currency = env["res.currency"].sudo().search([("name", "=", currency_code)], limit=1)
            if currency:
                return currency
        return company.currency_id or env.company.currency_id

    @staticmethod
    def _resolve_sale_journal(env, company):
        journal = env["account.journal"].with_company(company.id).sudo().search(
            [("type", "=", "sale"), ("company_id", "=", company.id)], limit=1
        )
        if not journal:
            raise UserError(
                _("No sales journal configured for company %(company)s.") % {"company": company.display_name}
            )
        return journal

    @staticmethod
    def _resolve_payment_journal(env, company):
        journal = env["account.journal"].with_company(company.id).sudo().search(
            [("type", "in", ("bank", "cash")), ("company_id", "=", company.id)], limit=1
        )
        if not journal:
            raise UserError(
                _("No payment journal configured for company %(company)s.") % {"company": company.display_name}
            )
        return journal

    # ------------------------------------------------------------------
    # Formatting helpers
    # ------------------------------------------------------------------
    @staticmethod
    def _clean_identifier(value: Any) -> Optional[str]:
        if value in (None, "", False):
            return None
        text = str(value).strip()
        return text or None

    @staticmethod
    def _normalize_numeric_identifier(value: Any) -> Optional[str]:
        cleaned = InvoiceImportUtils._clean_identifier(value)
        if not cleaned:
            return None
        try:
            if any(sep in cleaned.lower() for sep in ("e", ".")) or cleaned.isdigit() is False:
                from decimal import Decimal

                normalized = format(Decimal(cleaned), 'f')
                normalized = normalized.rstrip('0').rstrip('.') if '.' in normalized else normalized
                return normalized or None
        except Exception:  # pylint: disable=broad-except
            return cleaned
        return cleaned

    @staticmethod
    def _to_float(value: Any, default: float = 0.0) -> float:
        if value in (None, "", False):
            return default
        try:
            return float(value)
        except (TypeError, ValueError):
            return default

    @staticmethod
    def _to_date(value: Any):
        if not value:
            return False
        try:
            return fields.Date.to_date(value)
        except Exception:  # pylint: disable=broad-except
            return False
    @staticmethod
    def _find_payment_method_line(journal):
        lines = journal.inbound_payment_method_line_ids
        if not lines:
            raise UserError(
                _("Journal %s does not have inbound payment methods configured.") % journal.display_name
            )
        manual = lines.filtered(lambda l: l.payment_method_id.code == 'manual')
        return manual[:1] or lines[:1]
