"""Shared helper functions for rollup management flows."""

# Rollup Deployment Flow:
# Step 1 - API Deploy Call: /api/v1/rollup/service/deploy
# Step 2 - Checkout Initialization: start_checkout()
# Step 3 - Payment Success (webhook): handle_invoice_payment_succeeded()
# Step 4 - Admin activates service: sets status='active'
# Step 5 - Stripe webhook reminders (will_be_due/overdue)

from __future__ import annotations

import json
import uuid
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from typing import Any, Dict, Iterable, List, Mapping, Sequence, Tuple
import logging
import stripe

from odoo import fields
from odoo.http import request

from odoo.addons.auth_module.utils import oauth as oauth_utils
from odoo.addons.zeeve_base.utils import base_utils

_logger = logging.getLogger(__name__)


class RollupError(Exception):
    """Generic exception that carries an HTTP status code."""

    def __init__(self, message: str, status: int = 400):
        super().__init__(message)
        self.status = status


@dataclass(frozen=True)
class CheckoutContext:
    """Dataclass holding validated checkout information."""

    amount_decimal: Decimal
    cancel_url: str
    currency: str
    deployment_token: str
    line_items: List[Dict[str, Any]]
    metadata: Dict[str, Any]
    service_name: str
    success_url: str
    mode: str = "payment"
    subscription_data: Dict[str, Any] | None = None
    deployment_payload: Dict[str, Any] = field(default_factory=dict)
    original_amount_decimal: Decimal | None = None
    discount_amount_decimal: Decimal | None = None
    discount_id: int | None = None
    discount_code: str | None = None
    discount_stripe_coupon_id: str | None = None
    is_odoo_managed: bool = False
    billing_duration: str | None = None

@staticmethod
def _image_url(record, field="logo"):
    """Return an absolute image URL for binary fields."""

    if not record or not getattr(record, field):
        return None
    base_url = request.httprequest.host_url.rstrip("/")
    return f"{base_url}/web/image/{record._name}/{record.id}/{field}"

def json_response(success: bool, data: Dict[str, Any] | None = None, error: str = "", status: int = 200):
    """Return a normalised JSON response used by the controllers."""

    payload = {
        "success": bool(success),
        "data": data or {},
        "message": error or "",
    }
    return oauth_utils.make_json_response(payload, status=status)


def serialize_node(node):
    """Transform a rollup.node record into a JSON friendly dict."""

    return {
        "id": node.id,
        "nodid": node.nodid,
        "name": node.node_name,
        "node_type": node.node_type,
        "status": node.status,
        "endpoint_url": node.endpoint_url,
        "metadata": node.metadata_json or {},
    }


def serialize_service(service):
    """Transform a rollup.service record into a JSON friendly dict."""

    return {
        "id": service.id,
        "service_id": service.service_id,
        "chain_id": service.chain_id,
        "name": service.name,
        "status": service.status,
        "subscription_status": service.subscription_status,
        "type": {
            "id": service.type_id.rollup_id,
            "name": service.type_id.name,
            "description": service.type_id.description,
        }
        if service.type_id
        else None,
        "node_count": service.node_count,
        "quantity": service.quantity,
        "regions": [
            {
                "id": region.id,
                "name": region.name,
                "country_id": region.country_id.id,
                "country_name": region.country_id.name,
            }
            for region in service.region_ids
        ],
        "inputs": service.inputs_json or {},
        # "metadata": service.metadata_json or {},
        "nodes": [serialize_node(node) for node in service.node_ids],
        "created_at": service.create_date.isoformat() if service.create_date else None,
    }


def get_stripe_client():
    """Reuse the subscription module's Stripe client helper."""

    return request.env["subscription.plan"]._get_stripe_client()


def start_checkout(user, payload: Dict[str, Any]) -> Tuple[Any, CheckoutContext, Dict[str, Any]]:
    """Backward compatible wrapper delegating to :mod:`deployment_utils`."""

    from . import deployment_utils  # Local import to avoid circular dependency

    return deployment_utils.start_checkout(user, payload)


def _ensure_service_for_checkout(user, checkout_context: CheckoutContext):
    """Create or update a draft rollup service for the checkout session.

    Executed during :func:`start_checkout` so the draft ``rollup.service`` and
    its initial invoice exist before the user is redirected to Stripe.
    """

    metadata = dict(checkout_context.metadata or {})
    deployment_payload = dict(checkout_context.deployment_payload or {})
    if not deployment_payload:
        raise RollupError("Missing deployment metadata in checkout context.", status=400)

    rollup_type_id = deployment_payload.get("type_id")
    rollup_type = request.env["rollup.type"].sudo().browse(rollup_type_id)
    if not rollup_type.exists():
        raise RollupError("Rollup type not found.", status=404)

    region_ids = deployment_payload.get("region_ids") or []
    region_ids = _normalise_region_ids(region_ids)

    partner = user.partner_id
    if not partner:
        raise RollupError("User is missing an associated partner for invoicing.", status=400)

    deployment_token = metadata.get("deployment_token") or checkout_context.deployment_token
    Service = request.env["rollup.service"].sudo().with_context(rollup_auto_send_invoice=True)
    service = Service.browse()
    if deployment_token:
        service = Service.search([("deployment_token", "=", deployment_token)], limit=1)
        if service and service.customer_id != partner:
            raise RollupError("Deployment token already associated with another customer.", status=409)

    if not service:
        # User-requested change: Reuse existing record if it's draft and pending_payment
        service = Service.search([
            ("customer_id", "=", partner.id),
            ("type_id", "=", rollup_type.id),
            ("status", "=", "draft"),
            ("subscription_status", "=", "pending_payment")
        ], limit=1)

    autopay_enabled = metadata.get("autopay_enabled")
    if autopay_enabled is not None:
        autopay_enabled = str(autopay_enabled).lower() not in {"false", "0", "no"}
    else:
        autopay_enabled = True

    # The Super Admin (Company Owner) is the billing owner
    owner_partner_id = user.company_id.owner_id.partner_id.id or partner.id

    service_vals = {
        "name": deployment_payload.get("name") or checkout_context.service_name,
        "type_id": rollup_type.id,
        "customer_id": owner_partner_id,
        "company_id": user.company_id.id,
        "region_ids": [(6, 0, region_ids)],
        "inputs_json": deployment_payload,
        "deployment_token": deployment_token,
        "autopay_enabled": autopay_enabled,
        "is_odoo_managed": checkout_context.is_odoo_managed,
    }
    if checkout_context.original_amount_decimal is not None:
        service_vals["original_amount"] = float(checkout_context.original_amount_decimal)
    if checkout_context.discount_amount_decimal is not None:
        service_vals["discount_amount"] = float(checkout_context.discount_amount_decimal)
    else:
        service_vals.setdefault("discount_amount", 0.0)
    if checkout_context.discount_id:
        service_vals.update(
            {
                "discount_id": checkout_context.discount_id,
                "discount_code": checkout_context.discount_code,
            }
        )
    else:
        service_vals.update({"discount_id": False, "discount_code": False})

    metadata_update = {
        "deployment_token": deployment_token,
        "stripe_amount": metadata.get("amount"),
        "stripe_currency": metadata.get("currency"),
        "checkout_initialized_at": fields.Datetime.now().isoformat(),
    }

    if service:
        service.write(service_vals)
        service.write({"metadata_json": service._combined_metadata(metadata_update)})
        service._ensure_initial_invoice()
        return service

    service_vals["metadata_json"] = metadata_update
    service = Service.create(service_vals)

    # Auto-assign access to the operator who created it
    if user.company_role == 'operator':
        request.env['record.access'].sudo().create({
            'user_id': user.id,
            'module_name': 'rollup_management',
            'record_id': service.id
        })

    return service


def is_rollup_checkout_session(session_metadata: Dict[str, Any] | None) -> bool:
    """Return True when the Stripe session metadata corresponds to a rollup deployment.

    Used exclusively by the subscription webhook (`/api/stripe/webhook`) to
    detect rollup sessions during the ``checkout.session.completed`` callback.
    """
    _logger.debug("Checking if session is rollup: %s", session_metadata)
    metadata = session_metadata or {}
    return bool(metadata.get("rollup_type_id") and metadata.get("deployment_token"))


def is_rollup_metadata(metadata: Dict[str, Any] | None) -> bool:
    """Return True if metadata references a rollup service."""

    if not isinstance(metadata, dict):
        return False
    data = metadata
    rollup_keys = (
        "rollup_service_id",
        "rollup_service_uuid",
        "rollup_service_identifier",
        "rollup_id",
        "rollup_type_id",
        "deployment_token",
        "deploymentToken",
    )
    return any(data.get(key) for key in rollup_keys)


def _normalise_region_ids(region_ids_raw: Iterable[Any]) -> List[int]:
    region_ids: List[int] = []
    for region_id in region_ids_raw:
        try:
            region_ids.append(int(region_id))
        except (TypeError, ValueError) as exc:
            raise RollupError("Invalid region identifier provided.", status=400) from exc
    return region_ids


def _extract_discount_context(source: Mapping[str, Any] | None):
    """Parse discount metadata from a Stripe session/invoice payload."""

    metadata = source or {}
    Discount = request.env["subscription.discount"].sudo()

    discount_code = metadata.get("discount_code") or metadata.get("discountCode")
    discount_id_raw = metadata.get("discount_id") or metadata.get("discountId")
    discount_amount_raw = metadata.get("discount_amount") or metadata.get("discountAmount")
    original_amount_raw = metadata.get("original_amount") or metadata.get("originalAmount")

    discount_record = Discount.browse()
    if discount_id_raw:
        try:
            candidate = Discount.browse(int(discount_id_raw))
            if candidate.exists():
                discount_record = candidate
        except (TypeError, ValueError):
            discount_record = Discount.browse()
    if not discount_record and discount_code:
        discount_record = Discount.search([("code", "=", discount_code)], limit=1)

    discount_amount = 0.0
    if discount_amount_raw not in (None, ""):
        try:
            discount_amount = float(discount_amount_raw)
        except (TypeError, ValueError):
            discount_amount = 0.0

    original_amount = None
    if original_amount_raw not in (None, ""):
        try:
            original_amount = float(original_amount_raw)
        except (TypeError, ValueError):
            original_amount = None

    if discount_record and not discount_record.exists():
        discount_record = Discount.browse()

    resolved_code = discount_code or (discount_record.code if discount_record else None)

    return {
        "record": discount_record,
        "code": resolved_code,
        "amount": discount_amount,
        "original_amount": original_amount,
    }


def _stripe_get(source: Any, key: str, default: Any | None = None):
    """Return ``key`` from Stripe payloads supporting both dict/object access."""

    if not source:
        return default

    if isinstance(source, Mapping):
        return source.get(key, default)

    value = getattr(source, key, default)
    if value is not None:
        return value

    getter = getattr(source, "get", None)
    if callable(getter):
        try:
            return getter(key, default)
        except Exception:  # pylint: disable=broad-except
            return default
    return default


def _as_sequence(value: Any) -> List[Any]:
    """Return ``value`` as a list while handling Stripe helper objects."""

    if not value:
        return []
    if isinstance(value, (list, tuple, set)):
        return list(value)
    data = getattr(value, "data", None)
    if isinstance(data, Sequence) and not isinstance(data, (str, bytes)):
        return list(data)
    if isinstance(value, Mapping):
        data = value.get("data")
        if isinstance(data, Sequence) and not isinstance(data, (str, bytes)):
            return list(data)
    return [value]


def _extract_coupon_identifier(discount_entry: Any) -> str | None:
    """Return the coupon identifier from a Stripe discount entry."""

    if not discount_entry:
        return None

    coupon = None
    if isinstance(discount_entry, Mapping):
        coupon = discount_entry.get("coupon")
        promo_code = discount_entry.get("promotion_code")
    else:
        coupon = getattr(discount_entry, "coupon", None)
        promo_code = getattr(discount_entry, "promotion_code", None)

    coupon_id = None
    if isinstance(coupon, Mapping):
        coupon_id = coupon.get("id") or coupon.get("code") or coupon.get("name")
    elif isinstance(coupon, str):
        coupon_id = coupon
    elif coupon is not None:
        coupon_id = getattr(coupon, "id", None) or getattr(coupon, "code", None)

    if not coupon_id and isinstance(discount_entry, Mapping):
        coupon_id = discount_entry.get("id") or discount_entry.get("coupon_id")
    if not coupon_id and isinstance(promo_code, str):
        coupon_id = promo_code

    return coupon_id


def resolve_stripe_discount(source: Mapping[str, Any] | Any | None):
    """Inspect a Stripe invoice/session payload and derive discount context."""

    Discount = request.env["subscription.discount"].sudo()
    context = {
        "record": Discount.browse(),
        "code": None,
        "amount": 0.0,
        "original_amount": None,
    }

    if not source:
        return context

    def _to_amount(value: Any) -> float | None:
        if value in (None, "", False):
            return None
        try:
            if isinstance(value, (int, float)):
                return float(value) / 100.0
            return float(value)
        except (TypeError, ValueError):
            return None

    total_details = _stripe_get(source, "total_details") or {}
    amount_discount = _to_amount(_stripe_get(total_details, "amount_discount"))
    amount_subtotal = _to_amount(_stripe_get(total_details, "amount_subtotal"))
    amount_total = _to_amount(_stripe_get(total_details, "amount_total"))

    subtotal_candidates = [
        amount_subtotal,
        _to_amount(_stripe_get(source, "amount_subtotal")),
        _to_amount(_stripe_get(source, "subtotal")),
    ]
    total_candidates = [
        amount_total,
        _to_amount(_stripe_get(source, "amount_total")),
        _to_amount(_stripe_get(source, "total")),
        _to_amount(_stripe_get(source, "amount_due")),
        _to_amount(_stripe_get(source, "amount_paid")),
    ]

    subtotal_value = next((value for value in subtotal_candidates if value is not None), None)
    total_value = next((value for value in total_candidates if value is not None), None)

    coupon_candidates: List[str] = []
    discount_amount_entries = _as_sequence(_stripe_get(source, "total_discount_amounts"))
    discount_amount_total = 0.0
    for entry in discount_amount_entries:
        if isinstance(entry, Mapping):
            amount_candidate = _to_amount(entry.get("amount"))
            if amount_candidate is not None:
                discount_amount_total += amount_candidate
            coupon_id = _extract_coupon_identifier(entry.get("discount")) or _extract_coupon_identifier(entry)
        else:
            coupon_id = _extract_coupon_identifier(entry)
            amount_candidate = None
        if coupon_id:
            coupon_candidates.append(coupon_id)
    if amount_discount is None and discount_amount_total:
        amount_discount = discount_amount_total

    for discount_entry in _as_sequence(_stripe_get(source, "discounts")):
        coupon_id = _extract_coupon_identifier(discount_entry)
        if coupon_id:
            coupon_candidates.append(coupon_id)

    single_discount = _stripe_get(source, "discount")
    if single_discount:
        coupon_id = _extract_coupon_identifier(single_discount)
        if coupon_id:
            coupon_candidates.append(coupon_id)

    if amount_discount is None and subtotal_value is not None and total_value is not None:
        computed_discount = subtotal_value - total_value
        if computed_discount > 0:
            amount_discount = computed_discount

    original_amount = subtotal_value
    if original_amount is None and total_value is not None and amount_discount:
        original_amount = total_value + amount_discount

    discount_record = Discount.browse()
    discount_code = None
    for coupon_id in coupon_candidates:
        if not coupon_id:
            continue
        candidate = Discount.search([("stripe_coupon_id", "=", coupon_id)], limit=1)
        if not candidate:
            candidate = Discount.search([("code", "=", coupon_id)], limit=1)
        if candidate:
            discount_record = candidate
            discount_code = candidate.code or coupon_id
            break
        discount_code = discount_code or coupon_id

    context.update(
        {
            "record": discount_record,
            "code": discount_code,
            "amount": amount_discount or 0.0,
            "original_amount": original_amount,
        }
    )
    return context


def combine_discount_contexts(*contexts: Mapping[str, Any] | None):
    """Merge multiple discount contexts preferring real Stripe amounts."""

    Discount = request.env["subscription.discount"].sudo()
    record = Discount.browse()
    code = None
    amount = None
    original_amount = None

    for context in contexts:
        if not context:
            continue
        candidate_record = context.get("record") if isinstance(context, Mapping) else None
        if candidate_record and candidate_record.exists():
            record = candidate_record
        candidate_code = context.get("code") if isinstance(context, Mapping) else None
        if candidate_code:
            code = candidate_code
        candidate_amount = context.get("amount") if isinstance(context, Mapping) else None
        if candidate_amount not in (None, ""):
            amount = float(candidate_amount)
        candidate_original = context.get("original_amount") if isinstance(context, Mapping) else None
        if candidate_original not in (None, ""):
            original_amount = float(candidate_original)

    return {
        "record": record,
        "code": code,
        "amount": amount if amount is not None else 0.0,
        "original_amount": original_amount,
    }


def prepare_checkout_context(user, payload: Dict[str, Any]) -> CheckoutContext:
    """Validate deploy payload and prepare Stripe checkout inputs.

    Called by :func:`start_checkout` for both the initial draft creation and
    any retry flows initiated from the deploy API.
    """
    is_odoo_managed = payload.get("is_managed_billing") or payload.get("is_odoo_managed")
    billing_duration = payload.get("billing_duration") or "month"

    required_fields = ["name", "region_ids"]
    is_valid, error_message = base_utils._validate_payload(payload, required_fields)
    if not is_valid:
        raise RollupError(error_message, status=400)

    rollup_identifier = payload.get("type_id") or payload.get("rollup_type_id")
    if not rollup_identifier:
        raise RollupError("Rollup type must be provided.", status=400)

    domain: list[tuple[str, str, Any]]
    try:
        rollup_identifier_int = int(rollup_identifier)
        domain = [("id", "=", rollup_identifier_int)]
    except (TypeError, ValueError):
        domain = [("rollup_id", "=", str(rollup_identifier))]

    rollup_type = request.env["rollup.type"].sudo().search(domain, limit=1)
    if not rollup_type.exists():
        raise RollupError("Rollup type not found.", status=404)

    region_ids_raw = payload.get("region_ids")
    if not isinstance(region_ids_raw, list) or not region_ids_raw:
        raise RollupError("At least one region must be selected.", status=400)

    region_ids = _normalise_region_ids(region_ids_raw)
    regions = request.env["server.location"].browse(region_ids)
    if len(regions) != len(region_ids):
        raise RollupError("One or more regions do not exist.", status=404)

    amount_value = rollup_type.cost
    try:
        amount_decimal = Decimal(str(amount_value))
    except (InvalidOperation, TypeError) as exc:
        raise RollupError("Invalid amount provided.", status=400) from exc

    if amount_decimal <= 0 and not is_odoo_managed:
        raise RollupError("Amount must be greater than zero.", status=400)

    # V2: Override amount based on billing duration
    if is_odoo_managed:
        duration_map = {
            "month": rollup_type.amount_month,
            "quarter": rollup_type.amount_quarter,
            "year": rollup_type.amount_year,
        }
        v2_amount = duration_map.get(billing_duration)
        if v2_amount is None:
            # Fallback to monthly if not specified or invalid
            v2_amount = rollup_type.amount_month
        print(v2_amount,'========',billing_duration)
        if not v2_amount or v2_amount <= 0:
            raise RollupError(f"Billing duration '{billing_duration}' is not configured or has an invalid amount for this rollup type.", status=400)
        
        amount_decimal = Decimal(str(v2_amount))
        original_amount_decimal = amount_decimal

    original_amount_decimal = amount_decimal
    currency = request.env["ir.config_parameter"].sudo().get_param("stripe_currency", "usd")
    deployment_token = payload.get("deployment_token") or str(uuid.uuid4())

    configuration = payload.get("configuration") or {}
    if configuration and not isinstance(configuration, dict):
        raise RollupError("Configuration must be provided as a JSON object.", status=400)

    additional_fields = {
        key: value
        for key, value in payload.items()
        if key
        not in {
            "type_id",
            "rollup_type_id",
            "name",
            "region_ids",
            "amount",
            "deployment_token",
            "configuration",
        }
    }
    deployment_payload = {
        "type_id": rollup_type.id,
        "rollup_type_id": rollup_type.rollup_id,
        "name": payload["name"],
        "region_ids": region_ids,
        "configuration": configuration,
        "core_components": additional_fields.get("core_components") or [],
        "nodes": additional_fields.get("nodes") or [],
        "extras": {
            key: value
            for key, value in additional_fields.items()
            if key not in {"core_components", "nodes"}
        },
    }

    discount_code = (payload.get("discount_code") or "").strip()
    discount_amount_decimal = Decimal("0")
    discount_record = None
    discount_coupon_id = None

    if discount_code:
        Discount = request.env["subscription.discount"].sudo()
        discount_record, message = Discount.validate_discount_code(
            discount_code,
            subscription_plan_id=None,
            protocol_id=None,
            amount=float(original_amount_decimal),
            scope="rollup",
        )
        if not discount_record:
            raise RollupError(f"Invalid discount code: {message}", status=400)

        discount_amount_decimal = Decimal(
            str(discount_record.calculate_discount_amount(float(original_amount_decimal)))
        )
        if discount_amount_decimal < 0:
            discount_amount_decimal = Decimal("0")
        if discount_amount_decimal > original_amount_decimal:
            discount_amount_decimal = original_amount_decimal

        discount_amount_decimal = discount_amount_decimal.quantize(
            Decimal("0.01"), rounding=ROUND_HALF_UP
        )

        amount_decimal = (original_amount_decimal - discount_amount_decimal).quantize(
            Decimal("0.01"), rounding=ROUND_HALF_UP
        )
        if amount_decimal < Decimal("0"):
            amount_decimal = Decimal("0")

        if not discount_record.stripe_coupon_id:
            raise RollupError(
                "Discount is not synced with Stripe. Please sync the coupon before using it.",
                status=400,
            )

        discount_coupon_id = discount_record.stripe_coupon_id

    httprequest = getattr(request, "httprequest", None)
    if httprequest and getattr(httprequest, "host_url", None):
        base_url = httprequest.host_url.rstrip("/")
    else:
        base_url = request.env["ir.config_parameter"].sudo().get_param("backend_url", "").rstrip("/")
    FE_URL = request.env['ir.config_parameter'].sudo().get_param('frontend_url') or base_url

    success_url = f"{FE_URL}/{rollup_type.name}?status=success&token={deployment_token}&session_id={{CHECKOUT_SESSION_ID}}"
    cancel_url = f"{FE_URL}/{rollup_type.name}?status=cancel"

    metadata = {
        "deployment_token": deployment_token,
        "rollup_type_id": str(rollup_type.id),
        "rollup_type_public_id": rollup_type.rollup_id,
        "rollup_name": payload["name"],
        "user_id": str(user.id),
        "amount": str(amount_decimal),
        "original_amount": str(original_amount_decimal),
        "currency": currency,
        "autopay_enabled": "true",
        "is_odoo_managed": "true" if is_odoo_managed else "false",
        "billing_duration": billing_duration if is_odoo_managed else "",
    }
    if rollup_type.stripe_product_id:
        metadata["stripe_product_id"] = rollup_type.stripe_product_id
    if rollup_type.stripe_price_id:
        metadata["stripe_price_id"] = rollup_type.stripe_price_id
    if rollup_type.payment_frequency:
        metadata["payment_frequency"] = rollup_type.payment_frequency

    if discount_record:
        metadata.update(
            {
                "discount_id": str(discount_record.id),
                "discount_code": discount_record.code,
                "discount_amount": str(discount_amount_decimal),
            }
        )

    interval = rollup_type.payment_frequency or "month"
    unit_amount = int((original_amount_decimal * 100).quantize(Decimal("1"), rounding=ROUND_HALF_UP))

    if rollup_type.stripe_price_id and not is_odoo_managed:
        line_items = [
            {
                "price": rollup_type.stripe_price_id,
                "quantity": 1,
            }
        ]
    else:
        price_data = {
            "currency": currency,
            "product_data": {
                "name": f"Rollup Deployment - {rollup_type.name}",
            },
            "unit_amount": unit_amount,
        }
        if not is_odoo_managed:
            price_data["recurring"] = {"interval": interval}

        line_items = [
            {
                "price_data": price_data,
                "quantity": 1,
            }
        ]

    subscription_data = {"metadata": metadata}

    return CheckoutContext(
        amount_decimal=amount_decimal,
        cancel_url=cancel_url,
        currency=currency,
        deployment_token=deployment_token,
        line_items=line_items,
        metadata=metadata,
        service_name=payload["name"],
        success_url=success_url,
        mode="subscription",
        subscription_data=subscription_data,
        deployment_payload=deployment_payload,
        original_amount_decimal=original_amount_decimal,
        discount_amount_decimal=discount_amount_decimal if discount_record else None,
        discount_id=discount_record.id if discount_record else None,
        discount_code=discount_record.code if discount_record else None,
        discount_stripe_coupon_id=discount_coupon_id,
        is_odoo_managed=bool(is_odoo_managed),
        billing_duration=billing_duration if is_odoo_managed else None,
    )


def prepare_service_creation(checkout_session, user):
    """Generate create/write values when Stripe payment completes."""

    metadata = _session_metadata(checkout_session)

    service_model = request.env["rollup.service"].sudo()
    service = service_model.browse()

    metadata_discount_context = _extract_discount_context(metadata)
    session_discount_context = resolve_stripe_discount(checkout_session)
    discount_context = combine_discount_contexts(
        metadata_discount_context,
        session_discount_context,
    )

    rollup_service_id = metadata.get("rollup_service_id")
    if rollup_service_id:
        try:
            service = service_model.browse(int(rollup_service_id))
            if not service.exists():
                service = service_model.browse()
        except (TypeError, ValueError):
            service = service_model.browse()

    if not service:
        rollup_service_uuid = metadata.get("rollup_service_uuid")
        if rollup_service_uuid:
            service = service_model.search([("service_id", "=", rollup_service_uuid)], limit=1)

    if not service:
        deployment_token = metadata.get("deployment_token")
        if deployment_token:
            service = service_model.search([("deployment_token", "=", deployment_token)], limit=1)

    if service:
        deployment_payload = service.inputs_json or {}
        rollup_type = service.type_id
        if not rollup_type:
            raise RollupError("Rollup type linked to this service no longer exists.", status=404)
        region_ids = service.region_ids.ids
        service_vals = {
            "name": service.name or metadata.get("rollup_name") or "Rollup Service",
            "type_id": rollup_type.id,
            "region_ids": [(6, 0, region_ids)],
            "inputs_json": deployment_payload,
            "customer_id": service.customer_id.id,
            "stripe_session_id": _session_get(checkout_session, "id"),
            "stripe_payment_intent_id": _session_get(checkout_session, "payment_intent"),
            "stripe_subscription_id": _session_get(checkout_session, "subscription"),
            "stripe_customer_id": _session_get(checkout_session, "customer"),
            "deployment_token": service.deployment_token or metadata.get("deployment_token"),
        }
    else:
        payload_json = metadata.get("rollup_payload")
        if not payload_json:
            raise RollupError("Missing deployment context in Stripe metadata.", status=400)

        try:
            deployment_payload = json.loads(payload_json)
        except json.JSONDecodeError as exc:
            raise RollupError("Invalid deployment payload stored in Stripe metadata.", status=500) from exc

        rollup_type_id = deployment_payload.get("type_id")
        rollup_type = request.env["rollup.type"].browse(rollup_type_id)
        if not rollup_type.exists():
            raise RollupError("Rollup type referenced in payment does not exist anymore.", status=404)

        region_ids = deployment_payload.get("region_ids") or []
        region_ids = _normalise_region_ids(region_ids)
        regions = request.env["server.location"].browse(region_ids)
        if len(regions) != len(region_ids):
            raise RollupError("Region information stored in payment metadata is invalid.", status=404)

        service_vals = {
            "name": deployment_payload.get("name") or metadata.get("rollup_name") or "Rollup Service",
            "type_id": rollup_type.id,
            "region_ids": [(6, 0, region_ids)],
            "inputs_json": deployment_payload,
            "customer_id": user.partner_id.id,
            "stripe_session_id": _session_get(checkout_session, "id"),
            "stripe_payment_intent_id": _session_get(checkout_session, "payment_intent"),
            "stripe_subscription_id": _session_get(checkout_session, "subscription"),
            "stripe_customer_id": _session_get(checkout_session, "customer"),
            "deployment_token": metadata.get("deployment_token"),
        }

    if discount_context.get("original_amount") is not None:
        service_vals["original_amount"] = discount_context["original_amount"]
    elif metadata.get("amount"):
        try:
            service_vals.setdefault("original_amount", float(metadata.get("original_amount") or metadata.get("amount")))
        except (TypeError, ValueError):
            pass

    discount_record = discount_context.get("record")
    discount_amount_value = discount_context.get("amount", 0.0)
    discount_code_value = discount_context.get("code")

    if discount_record and discount_record.exists():
        service_vals.update(
            {
                "discount_id": discount_record.id,
                "discount_code": discount_code_value or discount_record.code,
                "discount_amount": discount_amount_value,
            }
        )
    else:
        service_vals.update(
            {
                "discount_id": False,
                "discount_code": False,
                "discount_amount": discount_amount_value,
            }
        )

    try:
        session_dict = _session_to_dict(checkout_session, recursive=True)
    except AttributeError:  # pragma: no cover - fallback when Stripe helper missing
        try:
            session_dict = _session_to_dict(checkout_session)
        except AttributeError:  # pragma: no cover - fallback to basic cast
            session_dict = _session_to_dict(checkout_session)

    metadata_update = {
        "stripe_session_id": _session_get(checkout_session, "id"),
        "stripe_payment_intent_id": _session_get(checkout_session, "payment_intent"),
        "stripe_customer_id": _session_get(checkout_session, "customer"),
        "stripe_subscription_id": _session_get(checkout_session, "subscription"),
        "stripe_invoice_id": _session_get(checkout_session, "invoice"),
        "stripe_currency": metadata.get("currency"),
        "stripe_amount": metadata.get("amount"),
        "deployment_token": metadata.get("deployment_token"),
        "stripe_payment_status": _session_get(checkout_session, "payment_status"),
        "stripe_checkout_status": _session_get(checkout_session, "status"),
        "confirmed_by": user.id,
        "rollup_payment_context": {
            "checkout_session": session_dict,
        },
    }
    if discount_context.get("amount") is not None:
        metadata_update["discount_amount"] = discount_context.get("amount")
    if discount_code_value:
        metadata_update["discount_code"] = discount_code_value
    if discount_record and discount_record.exists():
        metadata_update["discount_id"] = str(discount_record.id)
    if discount_context.get("original_amount") is not None:
        metadata_update.setdefault("original_amount", discount_context.get("original_amount"))

    return service_vals, metadata_update


def _finalize_checkout_session(user, checkout_session):
    """Create the rollup service linked to a paid Stripe checkout session.

    Shared implementation for both the manual confirmation flow and the Stripe
    webhook so we keep a single entry point responsible for service creation
    and metadata synchronisation.
    """

    if _session_get(checkout_session, "payment_status") != "paid":
        raise RollupError("Stripe session is not paid yet.", status=400)

    service_vals, metadata_update = prepare_service_creation(checkout_session, user)

    service_model = request.env["rollup.service"]
    existing_service = service_model.browse()

    lookup_pairs = [
        ("stripe_session_id", _session_get(checkout_session, "id")),
        ("stripe_subscription_id", _session_get(checkout_session, "subscription")),
        ("stripe_payment_intent_id", _session_get(checkout_session, "payment_intent")),
    ]

    metadata = _session_metadata(checkout_session)

    rollup_service_id = metadata.get("rollup_service_id")
    if rollup_service_id:
        try:
            candidate = service_model.browse(int(rollup_service_id))
            if candidate.exists():
                return candidate, False, metadata_update, checkout_session
        except (TypeError, ValueError):
            pass

    rollup_service_uuid = metadata.get("rollup_service_uuid")
    if rollup_service_uuid:
        candidate = service_model.search([("service_id", "=", rollup_service_uuid)], limit=1)
        if candidate:
            return candidate, False, metadata_update, checkout_session

    deployment_token = metadata.get("deployment_token")
    if deployment_token:
        lookup_pairs.append(("deployment_token", deployment_token))

    for field_name, value in lookup_pairs:
        if not value:
            continue
        if field_name not in service_model._fields:
            continue
        existing_service = service_model.search([(field_name, "=", value)], limit=1)
        if existing_service:
            break
    if existing_service:
        return existing_service, False, metadata_update, checkout_session

    try:
        service = service_model.create(service_vals)
    except Exception as exc:  # pylint: disable=broad-except
        raise RollupError("Unable to create rollup service record.", status=500) from exc

    return service, True, metadata_update, checkout_session


def finalize_deployment(user, payload: Dict[str, Any]):
    """Validate checkout session, create service record, and return metadata.

    This path is used when the frontend posts the `stripe_session_id` back to
    `/api/v1/rollup/deploy` (manual confirmation fallback).  The webhook route
    normally calls :func:`finalize_deployment_from_session` directly.
    """

    session_id = payload.get("stripe_session_id")
    if not session_id:
        raise RollupError("Missing Stripe session identifier.", status=400)

    stripe_client = get_stripe_client()
    if not stripe_client.api_key:
        raise RollupError("Stripe secret key is not configured.", status=500)

    try:
        checkout_session = stripe_client.checkout.Session.retrieve(session_id)
    except stripe.error.StripeError as exc:  # type: ignore[attr-defined]
        raise RollupError(f"Stripe error: {exc}", status=502) from exc

    return _finalize_checkout_session(user, checkout_session)


def finalize_deployment_from_session(user, checkout_session):
    """Finalize a deployment directly from a Stripe checkout session object.

    Called by the Stripe webhook (``checkout.session.completed``) so that
    successful payments are reconciled server-side without depending on the
    client to poll for completion.
    """

    return _finalize_checkout_session(user, checkout_session)


def _session_get(checkout_session, key: str):
    """Return attribute from checkout session regardless of object/dict input."""

    if isinstance(checkout_session, Mapping):
        return checkout_session.get(key)
    return getattr(checkout_session, key, None)


def _session_metadata(checkout_session) -> Dict[str, Any]:
    """Safe metadata extraction supporting dict payloads."""

    metadata = _session_get(checkout_session, "metadata") or {}
    if isinstance(metadata, Mapping):
        return dict(metadata)
    return dict(metadata or {})


def _session_to_dict(checkout_session, recursive: bool = False) -> Dict[str, Any]:
    """Return dictionary representation of checkout session."""

    if isinstance(checkout_session, Mapping):
        return dict(checkout_session)

    method_names = ["to_dict_recursive", "to_dict"] if recursive else ["to_dict"]

    for method_name in method_names:
        method = getattr(checkout_session, method_name, None)
        if callable(method):
            try:
                return method()
            except TypeError:
                continue
    try:
        return dict(checkout_session)
    except TypeError:
        return {}


def _store_partner_stripe_customer(self, customer_id: str | None) -> bool:
    """Persist ``customer_id`` on the linked partner when absent."""

    if not customer_id:
        return False

    stored = False
    for service in self:
        partner = service.customer_id.sudo()
        if not partner or "stripe_customer_id" not in getattr(partner, "_fields", {}):
            continue

        try:
            current_value = partner.stripe_customer_id
        except AttributeError:  # pragma: no cover - defensive when field removed
            continue

        if current_value == customer_id:
            continue

        try:
            partner.write({"stripe_customer_id": customer_id})
        except Exception as exc:  # pylint: disable=broad-except
            _logger.exception(
                "Failed to store Stripe customer %s on partner %s: %s",
                customer_id,
                partner.id,
                exc,
            )
        else:
            stored = True

    return stored



def get_demo_service_if_exists(rollup_type: str, env):
    """
    Fetch demo service for a given rollup type if configured in system parameters.
    
    Args:
        rollup_type: The name of the rollup type (e.g., 'zksync', 'arbitrum', 'opstack', 'polygon')
        env: The Odoo environment object
    
    Returns:
        The demo service record if found and configured, None otherwise
    """
    # Get the demo service ID from system parameters
    demo_service_id = env["ir.config_parameter"].sudo().get_param(
        f"rollup.demo_{rollup_type}_service_id", ""
    )
    
    if not demo_service_id:
        return None
    
    # Search for the demo service
    demo_service = env["rollup.service"].sudo().search([
        ("service_id", "=", demo_service_id),
        ("type_id.name", "=", rollup_type),
        ("status", "!=", "draft")
    ], limit=1)
    
    return demo_service if demo_service else None

