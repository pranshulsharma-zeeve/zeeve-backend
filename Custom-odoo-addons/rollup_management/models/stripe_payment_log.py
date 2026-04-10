"""Extensions to the Stripe payment log for rollup specific data."""

from __future__ import annotations

import logging
from typing import Any, Dict

from odoo import api, fields, models

_logger = logging.getLogger(__name__)


class StripePaymentLog(models.Model):
    """Store references between Stripe events and rollup services."""

    _inherit = "stripe.payment.log"

    rollup_service_id = fields.Many2one(
        "rollup.service",
        string="Rollup Service",
        index=True,
        ondelete="set null",
    )
    rollup_type_id = fields.Many2one(
        "rollup.type",
        string="Rollup Type",
        related="rollup_service_id.type_id",
        store=True,
        readonly=True,
        index=True,
    )
    transaction_hash = fields.Char(string="Transaction Hash", index=True)

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------
    def _extract_event_payload(self, event_data: Any) -> Dict[str, Any]:
        """Return the payload dict from the event data if possible."""

        if isinstance(event_data, dict):
            return event_data.get("data", {}).get("object", {}) or {}
        return {}

    def _extract_event_metadata(self, event_payload: Dict[str, Any]) -> Dict[str, Any]:
        """Extract metadata dictionary from the Stripe payload."""

        metadata = event_payload.get("metadata") if isinstance(event_payload, dict) else {}
        return metadata if isinstance(metadata, dict) else {}

    def _prepare_rollup_values(self, event_type: str, event_data: Any, values: Dict[str, Any]):
        """Augment log creation values with rollup relevant info."""

        payload = self._extract_event_payload(event_data)
        metadata = self._extract_event_metadata(payload)

        payment_intent = metadata.get("payment_intent") or payload.get("payment_intent")
        if event_type.startswith("payment_intent"):
            payment_intent = payment_intent or payload.get("id")

        transaction_hash = (
            metadata.get("transaction_hash")
            or payload.get("transaction_hash")
            or payment_intent
        )
        if not transaction_hash:
            if event_type == "checkout.session.completed":
                transaction_hash = payload.get("payment_intent") or payload.get("id")
            elif event_type.startswith("invoice."):
                transaction_hash = payload.get("payment_intent") or payload.get("id")

        extracted_values: Dict[str, Any] = {}
        if transaction_hash:
            extracted_values.setdefault("transaction_hash", transaction_hash)
        if payment_intent:
            extracted_values.setdefault("stripe_payment_intent_id", payment_intent)

        stripe_customer = metadata.get("stripe_customer_id") or payload.get("customer")
        if stripe_customer:
            extracted_values.setdefault("stripe_customer_id", stripe_customer)

        stripe_subscription = metadata.get("stripe_subscription_id") or payload.get("subscription")
        if stripe_subscription:
            extracted_values.setdefault("stripe_subscription_id", stripe_subscription)

        stripe_invoice = metadata.get("stripe_invoice_id") or payload.get("invoice")
        if stripe_invoice:
            extracted_values.setdefault("stripe_invoice_id", stripe_invoice)

        for key, value in extracted_values.items():
            if value and not values.get(key):
                values[key] = value

        return payload, metadata

    def _find_rollup_service_from_payload(
        self, event_type: str, payload: Dict[str, Any], metadata: Dict[str, Any], values: Dict[str, Any]
    ):
        """Attempt to locate the related rollup service from payload metadata."""

        if values.get("rollup_service_id"):
            service = self.env["rollup.service"].browse(values["rollup_service_id"])
            if service.exists():
                return service

        service_model = self.env["rollup.service"].sudo()

        metadata_service_id = metadata.get("rollup_service_id") or payload.get("rollup_service_id")
        if metadata_service_id:
            try:
                service = service_model.browse(int(metadata_service_id))
                if service.exists():
                    return service
            except (TypeError, ValueError):
                _logger.debug("Invalid rollup_service_id in metadata: %s", metadata_service_id)

        search_candidates = []

        deployment_token = (
            metadata.get("deployment_token")
            or metadata.get("deploymentToken")
            or payload.get("client_reference_id")
        )
        if deployment_token:
            search_candidates.append(("deployment_token", deployment_token))

        payment_intent = values.get("stripe_payment_intent_id") or payload.get("payment_intent")
        if payment_intent:
            search_candidates.append(("stripe_payment_intent_id", payment_intent))

        session_id = metadata.get("stripe_session_id")
        if event_type == "checkout.session.completed":
            session_id = session_id or payload.get("id")
        if session_id:
            search_candidates.append(("stripe_session_id", session_id))

        subscription_id = (
            values.get("stripe_subscription_id")
            or metadata.get("stripe_subscription_id")
            or payload.get("subscription")
        )
        if subscription_id:
            search_candidates.append(("stripe_subscription_id", subscription_id))

        for key, identifier in search_candidates:
            if not identifier:
                continue
            if key in service_model._fields:
                service = service_model.search([(key, "=", identifier)], limit=1)
                if service:
                    return service
            try:
                service = service_model.search([("metadata_json", "contains", {key: identifier})], limit=1)
                if service:
                    return service
            except Exception:  # pylint: disable=broad-except
                _logger.debug("JSON search unsupported for %s; skipping fallback", key)
        return service_model.browse()

    # ------------------------------------------------------------------
    # Overrides
    # ------------------------------------------------------------------
    @api.model
    def create_log_entry(self, event_id, event_type, event_data, **kwargs):  # pylint: disable=arguments-differ
        """Create a log entry enriched with rollup references."""

        values = dict(kwargs)

        payload, metadata = self._prepare_rollup_values(event_type, event_data, values)

        rollup_service = self._find_rollup_service_from_payload(event_type, payload, metadata, values)
        if rollup_service:
            values["rollup_service_id"] = rollup_service.id

        log_entry = super().create_log_entry(event_id, event_type, event_data, **values)

        if rollup_service and not log_entry.rollup_service_id:
            log_entry.rollup_service_id = rollup_service.id

        if values.get("transaction_hash") and not log_entry.transaction_hash:
            log_entry.transaction_hash = values["transaction_hash"]

        return log_entry
