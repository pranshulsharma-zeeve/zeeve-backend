"""Utility helpers orchestrating the rollup subscription lifecycle."""

from __future__ import annotations

from dataclasses import replace
from decimal import Decimal
from datetime import date, datetime, time, timedelta
from dateutil.relativedelta import relativedelta
from typing import Any, Dict, Iterable, Tuple, Optional
import logging

import stripe

from odoo import _, fields
from odoo.tools.misc import format_date
from odoo.http import request
from odoo.tools import float_is_zero, format_amount

from ...subscription_management.utils.email_utils import (
    build_support_info,
    get_backend_base_url,
    send_subscription_email
)

from . import rollup_util

_logger = logging.getLogger(__name__)


def _as_date(value: Any) -> date | None:
    """Coerce different Stripe timestamp/date payloads to :class:`date`."""

    if not value:
        return None
    if isinstance(value, date) and not isinstance(value, datetime):
        return value
    if isinstance(value, datetime):
        return value.date()
    try:
        # Stripe usually sends unix timestamps for due dates.
        timestamp = int(value)
        if timestamp:
            return datetime.utcfromtimestamp(timestamp).date()
    except (TypeError, ValueError, OverflowError):
        pass
    if isinstance(value, str):
        try:
            return fields.Date.from_string(value)
        except Exception:  # noqa: BLE001 - defensive guard for malformed payloads
            return None
    return None


def _lookup_service_from_invoice(invoice_payload: Dict[str, Any]):
    """Resolve the :class:`rollup.service` referenced by a Stripe invoice."""

    base_metadata = invoice_payload.get("metadata") or {}
    line_metadata = {}
    lines = []
    try:
        lines = (invoice_payload.get("lines") or {}).get("data") or []
        if lines:
            line_metadata = lines[0].get("metadata") or {}
    except Exception:  # pragma: no cover - defensive
        line_metadata = {}
        lines = []
    parent_metadata = (
        (invoice_payload.get("parent") or {})
        .get("subscription_details", {})
        .get("metadata", {})
    )
    metadata = dict(base_metadata)
    for source in (parent_metadata, line_metadata):
        for key, value in (source or {}).items():
            metadata.setdefault(key, value)

    Service = request.env["rollup.service"].sudo()
    _logger.info("Step--------------------------3 ")
    subscription_id = (
        invoice_payload.get("subscription")
        or (invoice_payload.get("parent") or {})
        .get("subscription_details", {})
        .get("subscription")
        or (
            lines
            and lines[0]
            .get("parent", {})
            .get("subscription_item_details", {})
            .get("subscription")
        )
    )
    return Service._find_from_stripe_invoice_payload(
        metadata=metadata,
        subscription_id=subscription_id,
        invoice_id=invoice_payload.get("id"),
        customer_id=invoice_payload.get("customer"),
        payment_intent=invoice_payload.get("payment_intent"),
    )


def start_checkout(user, payload: Dict[str, Any]) -> Tuple[Any, rollup_util.CheckoutContext, Dict[str, Any]]:
    """Create the Stripe checkout session for a rollup deployment request."""

    # Step 2: start_checkout
    # - Validate the deployment payload and build the checkout context
    # - Persist the provisional rollup service with status='draft' and subscription_status='pending_payment'
    # - Create the Stripe Checkout session and expose tracking metadata to the client

    _logger.info("Step 2: Initiating rollup checkout for user %s", user.id)
    checkout_context = rollup_util.prepare_checkout_context(user, payload)
    service = create_provisional_subscription(user, checkout_context)
    stripe_client = rollup_util.get_stripe_client()

    metadata = dict(checkout_context.metadata or {})
    partner = user.partner_id.sudo()
    valid_customer_id = _ensure_stripe_customer(stripe_client, partner)
    if valid_customer_id and not metadata.get("stripe_customer_id"):
        metadata["stripe_customer_id"] = valid_customer_id
    latest_invoice = service.sudo()._get_latest_invoice()
    metadata.update(
        {
            "rollup_service_id": str(service.id),
            "rollup_service_uuid": service.service_id,
            "rollup_name": service.name,
            "deployment_token": service.deployment_token or checkout_context.deployment_token,
        }
    )
    if checkout_context.discount_id:
        metadata.setdefault("discount_id", str(checkout_context.discount_id))
        metadata.setdefault("discount_code", checkout_context.discount_code)
    if checkout_context.discount_amount_decimal is not None:
        metadata.setdefault("discount_amount", str(checkout_context.discount_amount_decimal))
    if checkout_context.original_amount_decimal is not None:
        metadata.setdefault("original_amount", str(checkout_context.original_amount_decimal))
    if latest_invoice:
        metadata.setdefault("rollup_invoice_odoo_id", str(latest_invoice.id))
        metadata.setdefault(
            "rollup_invoice_number",
            latest_invoice.name or latest_invoice.ref or latest_invoice.display_name or "",
        )
    
    subscription_data = dict(checkout_context.subscription_data or {})
    subscription_data["metadata"] = metadata
    checkout_context = replace(checkout_context, metadata=metadata, subscription_data=subscription_data)

    if not stripe_client.api_key:
        raise rollup_util.RollupError("Stripe secret key is not configured.", status=500)

    session_params: Dict[str, Any] = {
        "success_url": checkout_context.success_url,
        "cancel_url": checkout_context.cancel_url,
        "payment_method_types": ["card"],
        "mode": checkout_context.mode,
        "client_reference_id": checkout_context.deployment_token,
        "line_items": checkout_context.line_items,
        "metadata": metadata,
    }

    if valid_customer_id:
        session_params["customer"] = valid_customer_id
    else:
        session_params["customer_email"] = partner.email or user.login

    if checkout_context.is_odoo_managed:
        session_params["mode"] = "payment"
        session_params["payment_intent_data"] = {
            "setup_future_usage": "off_session",
            "metadata": metadata,
        }
    elif checkout_context.mode == "subscription" and checkout_context.subscription_data:
        session_params["subscription_data"] = subscription_data

    if checkout_context.discount_stripe_coupon_id:
        session_params.setdefault("discounts", []).append(
            {"coupon": checkout_context.discount_stripe_coupon_id}
        )

    session_params["allow_promotion_codes"] = True

    try:
        checkout_session = stripe_client.checkout.Session.create(**session_params)
    except stripe_client.error.StripeError as exc:  # type: ignore[attr-defined]
        raise rollup_util.RollupError(f"Stripe error: {exc}", status=502) from exc

    metadata_update = {
        "stripe_session_id": getattr(checkout_session, "id", None),
        "stripe_checkout_status": getattr(checkout_session, "status", "created"),
        "stripe_checkout_url": getattr(checkout_session, "url", None),
        "rollup_payment_context": {
            "checkout_session": rollup_util._session_to_dict(checkout_session, recursive=True)
        },
    }
    if checkout_context.discount_id:
        metadata_update.update(
            {
                "discount_id": str(checkout_context.discount_id),
                "discount_code": checkout_context.discount_code,
            }
        )
    if checkout_context.discount_amount_decimal is not None:
        metadata_update["discount_amount"] = str(checkout_context.discount_amount_decimal)
    if checkout_context.original_amount_decimal is not None:
        metadata_update["original_amount"] = str(checkout_context.original_amount_decimal)

    service.sudo().write(
        {
            "stripe_session_id": metadata_update["stripe_session_id"],
            "metadata_json": service._combined_metadata(metadata_update),
        }
    )

    data = {
        "session_id": checkout_session.id,
        "checkout_url": checkout_session.url,
        "deployment_token": checkout_context.deployment_token,
        "amount": float(checkout_context.amount_decimal),
        "original_amount": float(checkout_context.original_amount_decimal or checkout_context.amount_decimal),
        "discount_amount": float(checkout_context.discount_amount_decimal or Decimal("0")),
        "discount_code": checkout_context.discount_code,
        "currency": checkout_context.currency,
        "service_id": service.id,
        "service_uuid": service.service_id,
    }
    _logger.info(
        "Step 2: Checkout session %s prepared for service %s", checkout_session.id, service.id
    )
    return checkout_session, checkout_context, data


def create_provisional_subscription(user, checkout_context: rollup_util.CheckoutContext):
    """Ensure a draft :class:`rollup.service` exists for the checkout session."""

    # Step 2 (cont.): create_provisional_subscription
    # - Persist the requested rollup service with Stripe metadata for reconciliation
    # - Flag the subscription_status as pending_payment while awaiting checkout completion
    # - Return the rollup.service record bound to the deployment token

    service = rollup_util._ensure_service_for_checkout(user, checkout_context)
    update_subscription_status(service, "pending_payment", reason="checkout_started")
    return service


def handle_invoice_will_be_due(event_payload: Dict[str, Any]):
    """Send reminder emails ahead of an upcoming renewal."""

    # Step 5: handle_invoice_will_be_due
    # - Map the Stripe invoice reminder to the matching rollup service
    # - Flag both technical and billing states as overdue when autopay is disabled
    # - Dispatch the reminder template with dynamic countdown details

    service = _lookup_service_from_invoice(event_payload)
    # Fetch hosted_invoice_url from Stripe
    hosted_invoice_url = None
    try:
        stripe_secret_key = request.env['ir.config_parameter'].sudo().get_param('stripe_secret_key')
        stripe.api_key = stripe_secret_key
        stripe_invoice = stripe.Invoice.retrieve(event_payload.get('id'))
        hosted_invoice_url = getattr(stripe_invoice, 'hosted_invoice_url', None)
    except Exception as exc:
        _logger.error("Could not fetch hosted_invoice_url for subscription invoice %s: %s", event_payload.get('id'), exc)
    if not service:
        _logger.info("Step 4: No rollup service matched for will_be_due event %s", event_payload.get("id"))
        return None

    service = service.sudo()
    due_date = _as_date(event_payload.get("due_date")) or _as_date(event_payload.get("next_payment_attempt"))
    if not due_date:
        due_date = service.next_billing_date
    today = fields.Date.context_today(service)
    days_to_due = (due_date - today).days if due_date else 0

    update_subscription_status(service, "overdue", reason="stripe_reminder", service_status="overdue")

    context = {
        "days_to_due": days_to_due,
        "stripe_invoice_url": event_payload.get("hosted_invoice_url") or event_payload.get("invoice_pdf"),
        "next_billing_date": due_date,
        "amount": service.type_id.cost or 0,
    }
    send_rollup_email("rollup_management.mail_template_rollup_subscription_reminder", service, context)

    metadata_update = {
        "last_stripe_invoice_id": event_payload.get("id") or service.last_stripe_invoice_id,
        "next_reminder_at": fields.Datetime.now().isoformat(),
    }
    service.write({
        "last_stripe_invoice_id": metadata_update["last_stripe_invoice_id"],
        "metadata_json": service._combined_metadata(metadata_update),
        "hosted_invoice_url": hosted_invoice_url,
    })
    _logger.info(
        "Step 4: Reminder dispatched for service %s | invoice=%s | days_to_due=%s",
        service.id,
        event_payload.get("id"),
        days_to_due,
    )
    return service


def handle_invoice_overdue(event_payload: Dict[str, Any]):
    """Mark a subscription as overdue and notify the customer."""

    # Step 5: handle_invoice_overdue
    # - Resolve the service tied to the overdue invoice and flag it as overdue
    # - Notify the customer about the missed payment and share Stripe invoice links
    # - Preserve the invoice identifier for audit trails and potential follow-up suspension

    service = _lookup_service_from_invoice(event_payload)
    if not service:
        _logger.info("Step 5: No rollup service matched for invoice.overdue %s", event_payload.get("id"))
        return None

    service = service.sudo()
        # Fetch hosted_invoice_url from Stripe
    hosted_invoice_url = None
    try:
        stripe_secret_key = request.env['ir.config_parameter'].sudo().get_param('stripe_secret_key')
        stripe.api_key = stripe_secret_key
        stripe_invoice = stripe.Invoice.retrieve(event_payload.get('id'))
        hosted_invoice_url = getattr(stripe_invoice, 'hosted_invoice_url', None)
    except Exception as exc:
        _logger.error("Could not fetch hosted_invoice_url for subscription invoice %s: %s", event_payload.get('id'), exc)
    update_subscription_status(service, "overdue", reason="stripe_overdue", service_status="overdue")

    context = {
        "stripe_invoice_url": event_payload.get("hosted_invoice_url") or event_payload.get("invoice_pdf"),
        "next_billing_date": _as_date(event_payload.get("due_date")) or service.next_billing_date,
        "amount": service.type_id.cost or 0,
    }
    send_rollup_email("rollup_management.mail_template_rollup_subscription_due", service, context)

    metadata_update = {
        "last_stripe_invoice_id": event_payload.get("id") or service.last_stripe_invoice_id,
        "last_overdue_notice_at": fields.Datetime.now().isoformat(),
    }
    service.write({
        "last_stripe_invoice_id": metadata_update["last_stripe_invoice_id"],
        "metadata_json": service._combined_metadata(metadata_update),
        "hosted_invoice_url": hosted_invoice_url,
    })
    _logger.warning(
        "Step 5: Service %s flagged overdue from Stripe invoice %s",
        service.id,
        event_payload.get("id"),
    )
    return service


def handle_invoice_payment_succeeded(event_payload: Dict[str, Any]) -> Dict[str, Any] | None:
    """Finalize the renewal flow once Stripe confirms payment collection."""

    # Step 3: handle_invoice_payment_succeeded
    # - Map Stripe invoice success to the rollup service and reconcile accounting records
    # - Set the technical status to deploying while the platform provisions resources
    # - Transition subscription_status to active and dispatch transactional notifications
    _logger.info(
            "Step---------------------2"
        )
    service = _lookup_service_from_invoice(event_payload)
    if not service:
        _logger.info(
            "Step 6: No rollup service matched for invoice.payment_succeeded %s", event_payload.get("id")
        )
        return None

    service = service.sudo()
    _logger.info(
            "Step---------------------6 %s",service
        )
    invoice, payment = service.process_stripe_invoice_payment(event_payload, log_entry=None)

    if service.status not in {"active", "canceled", "archived"}:
        service.write({"status": "deploying"})

    update_subscription_status(service, "active", reason="payment_captured", service_status=None)

    metadata_update = {
        "last_stripe_invoice_id": event_payload.get("id") or service.last_stripe_invoice_id,
        "last_successful_payment_at": fields.Datetime.now().isoformat(),
    }
    service.write({
        "last_stripe_invoice_id": metadata_update["last_stripe_invoice_id"],
        "metadata_json": service._combined_metadata(metadata_update),
    })

    mail_context = {
        "stripe_invoice_url": event_payload.get("hosted_invoice_url") or event_payload.get("invoice_pdf"),
        "invoice": invoice,
        "payment": payment,
    }
    email_metadata: Dict[str, Any] = {}
    is_first_invoice = bool(invoice and service._is_first_paid_invoice(invoice))
    # Skip renewal mail for first invoice (initial activation)
    if (
        not is_first_invoice
        and send_rollup_email(
            "rollup_management.mail_template_rollup_subscription_renewed",
            service,
            mail_context,
        )
        and invoice
    ):
        email_metadata.update(
            {
                "subscription_renewal_last_invoice_id": invoice.id,
                "subscription_renewal_last_sent_at": fields.Datetime.now().isoformat(),
            }
        )
    admin_recipients = service._admin_recipient_payload()
    admin_kwargs = {}
    if admin_recipients.get("cc"):
        admin_kwargs["email_cc"] = ",".join(admin_recipients["cc"])
    if send_rollup_email(
        "rollup_management.mail_template_rollup_payment_success_admin",
        service,
        mail_context,
        email_to=",".join(admin_recipients.get("to", [])),
        **admin_kwargs,
    ) and invoice:
        email_metadata.update(
            {
                "subscription_payment_admin_last_invoice_id": invoice.id,
                "subscription_payment_admin_last_sent_at": fields.Datetime.now().isoformat(),
            }
        )
    if email_metadata:
        service.write({"metadata_json": service._combined_metadata(email_metadata)})

    _logger.info(
        "Step 6: Renewal completed for service %s | invoice=%s | payment=%s",
        service.id,
        invoice and invoice.id,
        payment and payment.id,
    )
    return {
        "service_id": service.id,
        "invoice_id": invoice.id if invoice else None,
        "payment_id": payment.id if payment else None,
    }


_STATUS_UNSET = object()
_NODE_STATUS_MAPPING = {
    "draft": "draft",
    "pending_payment": "draft",
    "active": "provisioning",
    "overdue": "suspended",
    "suspended": "suspended",
    "cancelled": "deleted",
}


def update_subscription_status(
    service,
    new_status: str,
    reason: str | None = None,
    service_status: str | None | object = _STATUS_UNSET,
) -> bool:
    """Transition the subscription status while optionally syncing the service state."""

    # - Guard repeated transitions and keep an audit trail in metadata and chatter
    # - Allow callers to explicitly request a technical status change when lifecycles align
    # - Return whether the subscription_status actually changed

    if not service:
        return False

    changed = False
    now = fields.Datetime.now()
    services = service if isinstance(service, Iterable) else [service]

    for record in services:
        record = record.sudo()
        previous = record.subscription_status
        if previous == new_status:
            continue

        write_vals: Dict[str, Any] = {"subscription_status": new_status}
        if service_status is _STATUS_UNSET:
            default_mapping = {
                "overdue": "overdue",
                "suspended": "suspended",
                "canceled": "canceled",
            }
            target_status = default_mapping.get(new_status)
        elif service_status:
            target_status = service_status
        else:
            target_status = None
        if target_status:
            write_vals.setdefault("status", target_status)
        record.write(write_vals)

        node_status = _NODE_STATUS_MAPPING.get(new_status)
        if node_status and hasattr(record, "_update_node_status"):
            try:
                record._update_node_status(node_status)
            except Exception:  # pragma: no cover - defensive logging
                _logger.exception(
                    "Failed to sync node status for rollup.service %s", record.id
                )

        metadata_update = {
            "subscription_status_last": new_status,
            "subscription_status_previous": previous,
            "subscription_status_reason": reason,
            "subscription_status_updated_at": now.isoformat(),
        }
        record.write({"metadata_json": record._combined_metadata(metadata_update)})

        label_map = dict(record._fields["subscription_status"].selection)
        label = label_map.get(new_status, new_status)
        reason_suffix = f" — {reason}" if reason else ""
        record.message_post(body=_("Subscription status changed to %s%s") % (label, reason_suffix))
        _logger.info(
            "rollup.service %s subscription status %s -> %s (%s)",
            record.id,
            previous,
            new_status,
            reason or "no-reason",
        )
        changed = True

    return changed


def send_rollup_email(
    template_xml_id: str,
    service,
    context: Dict[str, Any] | None = None,
    env=None,
    email_to: Optional[str] = None,
    email_cc: Optional[str] = None,
) -> bool:
    """Centralised helper to dispatch rollup subscription mail templates."""

    # - Resolve the requested mail template and merge dynamic context values
    # - Send the message immediately for transactional visibility and log outcomes
    # - Return whether the email template was successfully located

    env = env or (getattr(service, "env", None) if service else None) or (request.env if request else None)
    if env is None:
        _logger.warning("Step 8: Missing environment to send template %s", template_xml_id)
        return False

    template = env.ref(template_xml_id, raise_if_not_found=False)
    if not template:
        _logger.warning("Step 8: Template %s not found for service %s", template_xml_id, service and service.id)
        return False

    ctx = dict(context or {})
    ctx.setdefault("force_send", True)
    email_values = {'email_to': email_to or service.customer_id.email}
    if email_cc:
        email_values['email_cc'] = email_cc
    template.sudo().with_context(ctx).send_mail(service.id,email_values=email_values, force_send=True)
    _logger.info("Step 8: Email %s sent for service %s", template_xml_id, service.id)
    return True


def prepare_rollup_cancellation_context(
    service,
    *,
    cancellation_reason: str | None = None,
    cancellation_date: fields.Date | None = None,
    tenant: Dict[str, Any] | None = None,
    support_info: Dict[str, Any] | None = None,
    base_url: str | None = None,
) -> Dict[str, Any]:
    """Build the context expected by the rollup cancellation templates."""

    service = service.sudo()
    env = service.env
    partner = service.customer_id
    currency = service._get_currency()

    base_url_value = base_url or get_backend_base_url(env)
    support_payload = build_support_info(env, base_url=base_url_value, overrides=support_info)

    cancel_date_value = cancellation_date or fields.Date.context_today(service)
    cancel_date_display = ''
    if cancel_date_value:
        try:
            cancel_date_display = format_date(env, cancel_date_value)
        except Exception:  # pragma: no cover - fallback for unsupported locales
            cancel_date_display = fields.Date.to_string(cancel_date_value)

    plan_name = service.type_id.display_name or service.name or 'Rollup Subscription'
    subscription_identifier = (
        service.stripe_subscription_id
        or service.name
        or getattr(service, 'service_id', False)
        or ''
    )
    metadata_amount = 0.0
    if isinstance(service.metadata_json, dict):
        metadata_amount = service.metadata_json.get('subscription_amount') or 0.0

    price_value = service.type_id.cost or metadata_amount or 0.0
    try:
        price_value = float(price_value or 0.0)
    except (TypeError, ValueError):  # pragma: no cover - defensive cast
        price_value = 0.0

    currency_symbol = (currency and (currency.symbol or currency.name)) or ''
    invoice = service.invoice_ids.sorted('invoice_date', reverse=True)[0] if service.invoice_ids else False
    if invoice:
        currency = invoice.currency_id
        currency_symbol = currency.symbol or currency.name

    plan_details = {
        'Plan_Name': plan_name,
        'plan_name': plan_name,
        'subscription_id': subscription_identifier,
        'subscription_monthly_cost': f"{price_value:.2f}" if price_value else '0.00',
        'currency_symbol': currency_symbol,
        'currency': (currency and currency.name) or '',
        'cancellation_date': cancel_date_display,
        'status': service.subscription_status or service.status or 'canceled',
        'buyer_email_id': partner.email or '',
        'buyerEmail': partner.email or '',
    }
    if cancellation_reason:
        plan_details['cancellation_reason'] = cancellation_reason

    first_name = getattr(partner, 'first_name', False) or partner.name or partner.display_name or 'Customer'
    last_name = getattr(partner, 'last_name', False) or ''
    customer_payload = {
        'firstname': first_name,
        'lastname': last_name,
        'name': partner.display_name or partner.name or first_name,
    }

    context = {
        'name': customer_payload,
        'customer_name': customer_payload,
        'planDetails': plan_details,
        'plan_details': plan_details,
        'tenant': tenant or {},
        'supportInfo': support_payload,
        'support_info': support_payload,
        'baseUrl': base_url_value,
        'base_url': base_url_value,
        'email_to': partner.email or partner.email_formatted or '',
    }
    if cancellation_reason:
        context['cancellation_reason'] = cancellation_reason

    return context


def send_rollup_cancellation_emails(
    service,
    *,
    cancellation_reason: str | None = None,
    cancellation_date: fields.Date | None = None,
    tenant: Dict[str, Any] | None = None,
    support_info: Dict[str, Any] | None = None,
    base_url: str | None = None,
) -> bool:
    """Send rollup cancellation notifications to customer and admin."""

    context = prepare_rollup_cancellation_context(
        service,
        cancellation_reason=cancellation_reason,
        cancellation_date=cancellation_date,
        tenant=tenant,
        support_info=support_info,
        base_url=base_url,
    )

    customer_sent = send_rollup_email(
        "rollup_management.mail_template_rollup_subscription_cancelled_customer",
        service,
        context,
    )
    admin_recipients = service._admin_recipient_payload()
    admin_kwargs = {}
    if admin_recipients.get("cc"):
        admin_kwargs["email_cc"] = ",".join(admin_recipients["cc"])

    admin_sent = send_rollup_email(
        "rollup_management.mail_template_rollup_subscription_cancelled_admin",
        service,
        dict(context),
        email_to=",".join(admin_recipients.get("to", [])),
        **admin_kwargs,
    )

    return customer_sent and admin_sent

def _get_reminder_days(env, config_key: str = "reminder_days", default_value: str = "5,1") -> list[int]:
    Param = env["ir.config_parameter"].sudo()
    raw_value = Param.get_param(config_key, default_value) or default_value
    days: set[int] = set()
    for part in str(raw_value).split(","):
        try:
            days.add(int(part.strip()))
        except (TypeError, ValueError):
            continue
    if 0 not in days:
        days.add(0)
    return sorted(days, reverse=True)


def _get_formatted_amount(record) -> str | None:
    """Return a human-readable amount string for reminder templates."""

    amount_value = None
    currency = None

    if hasattr(record, "price"):
        amount_value = record.price
        # Multiply by quantity to get total amount
        if hasattr(record, "quantity") and record.quantity:
            amount_value = amount_value * record.quantity
        currency = getattr(record, "currency_id", None)
    elif getattr(record, "type_id", None) is not None:
        amount_value = getattr(record.type_id, "cost", None)
        currency = getattr(record, "currency_id", None)

    if amount_value is None:
        return None

    if not currency:
        company = getattr(record, "company_id", None) or record.env.company
        currency = getattr(company, "currency_id", None)

    if currency:
        try:
            return format_amount(record.env, amount_value, currency)
        except Exception:  # pragma: no cover - defensive fallback
            _logger.debug("Unable to format currency for %s", record, exc_info=True)

    try:
        return f"{float(amount_value):.2f}"
    except (TypeError, ValueError):  # pragma: no cover - defensive fallback
        return str(amount_value)


def _send_reminders_for_records(
    records,
    template_xml_id: str,
    *,
    days_to_due: int,
    date_field: str = "next_billing_date",
) -> int:
    dispatched = 0
    for rec in records:
        rec = rec.sudo()
        context = {
            "next_billing_date": getattr(rec, date_field, None),
            "days_to_due": days_to_due,
        }
        amount_label = _get_formatted_amount(rec)
        if amount_label:
            context.setdefault("amount", amount_label)
        if hasattr(rec, "_send_subscription_notice_email"):
            sent = rec._send_subscription_notice_email(
                template_xml_id,
                invoice=None,
                extra_context=context,
                force_send=True,
                metadata_key=None,
                metadata_value=None,
            )
        else:
            email_to = rec.customer_name.email if rec.customer_name else None
            sent = send_rollup_email(template_xml_id, rec, context=context, email_to=email_to)
        if sent:
            dispatched += 1
    return dispatched


def cron_send_subscription_reminders(env=None) -> int:
    """Send renewal reminder emails for rollup services due in 5 or 1 days."""
    env = env or (request.env if request else None)
    if env is None:
        _logger.warning("Missing environment for reminder cron")
        return 0

    Service = env["rollup.service"].sudo()
    Subscription = env["subscription.subscription"].sudo()
    today = fields.Date.context_today(Service)
    dispatched = 0

    for offset in _get_reminder_days(env, "rollup_management.reminder_days", "5,1"):
        target_date = today + timedelta(days=offset)
        domain = [
            ("next_billing_date", "=", target_date),
            ("subscription_status", "in", ["active", "pending_payment", "overdue"]),
        ]
        services = Service.search(domain)
        dispatched += _send_reminders_for_records(
            services,
            "rollup_management.mail_template_rollup_subscription_reminder",
            days_to_due=offset,
        )
        start_dt = datetime.combine(target_date, time.min)
        end_dt = datetime.combine(target_date, time.max)

        node_domain = [
            ('stripe_end_date', '>=', fields.Datetime.to_string(start_dt)),
            ('stripe_end_date', '<=', fields.Datetime.to_string(end_dt)),
            ("stripe_status", "in", ["incomplete", "active", "past_due"]),
            ("stripe_subscription_id", "=like", "sub_%")
        ]
        nodes = Subscription.search(node_domain)
        dispatched += _send_reminders_for_records(
            nodes,
            "subscription_management.mail_template_node_subscription_reminder",
            days_to_due=offset,
            date_field="stripe_end_date",
        )

    if dispatched:
        _logger.info("Reminder cron dispatched %s subscription notices", dispatched)
    return dispatched




def cron_audit_overdue_subscriptions(env=None) -> int:
    """Daily safety net to escalate overdue subscriptions and resend notices."""

    # - Scan active and pending subscriptions whose billing date has elapsed
    # - Mark lingering records as overdue/suspended and trigger follow-up mail
    # - Return the number of services updated for observability in cron logs

    env = env or (request.env if request else None)
    if env is None:
        _logger.warning("Missing environment for overdue audit")
        return 0

    Service = env["rollup.service"].sudo()
    Subscription = env["subscription.subscription"].sudo()
    today = fields.Date.context_today(Service)

    candidates = Service.search([
        ("subscription_status", "in", ["pending_payment", "active", "overdue"]),
        ("next_billing_date", "!=", False),
        ("next_billing_date", "<", today),
    ])

    updated = 0
    for service in candidates:
        if service.subscription_status in {"pending_payment", "active"}:
            update_subscription_status(
                service,
                "overdue",
                reason="cron_overdue_audit",
                service_status=service.status,
            )
            send_rollup_email(
                "rollup_management.mail_template_rollup_subscription_due",
                service,
                {
                    "next_billing_date": service.next_billing_date,
                    "amount": service.type_id.cost or 0,
                },
            )
            updated += 1
        elif service.subscription_status == "overdue":
            update_subscription_status(
                service,
                "suspended",
                reason="cron_auto_suspend",
                service_status=service.status,
            )
            send_rollup_email(
                "rollup_management.mail_template_rollup_subscription_suspended",
                service,
                {"next_billing_date": service.next_billing_date},
            )
            updated += 1

    node_candidates = Subscription.search([
        ("stripe_status", "in", ["incomplete", "active", "past_due"]),
        ("stripe_end_date", "!=", False),
        ("stripe_end_date", "<", today),
        ("stripe_subscription_id", "=like", "sub_%")

    ])
    for node in node_candidates:
        send_subscription_email(
            env,
            "subscription_management.mail_template_node_subscription_due",
            record=node,
            context={"next_billing_date": node.stripe_end_date, "amount": node.price},
        )
        updated += 1

    if updated:
        _logger.warning("Step 9: Overdue audit escalated %s services", updated)
    else:
        _logger.info("Step 9: Overdue audit found no services to escalate")
    return updated

def _sync_rollup_service_subscription( subscription_data, log_entry):
        """Mirror Stripe subscription changes onto the linked rollup service."""

        Service = request.env['rollup.service']
        service = Service._find_from_stripe_subscription_payload(subscription=subscription_data)
        if not service:
            return False

        service = service.sudo()
        try:
            updated = service._apply_stripe_subscription_payload(subscription_data)
        except Exception as exc:  # pylint: disable=broad-except
            _logger.exception(
                "Failed to sync rollup service %s from subscription %s",
                service.id,
                subscription_data.get('id'),
            )
            return False

        if log_entry and not log_entry.rollup_service_id:
            log_entry.rollup_service_id = service.id

        return updated


def _validate_or_clear_stripe_customer(stripe_client, partner):
    """Return a valid Stripe customer ID or None. If the stored one is missing/deleted, clear it."""
    cust_id = (partner.stripe_customer_id or "").strip()
    if not cust_id:
        return None
    try:
        cust = stripe_client.Customer.retrieve(cust_id)
        # If Stripe returns a deleted object: {"id": "...", "deleted": True}
        if getattr(cust, "deleted", False):
            partner.sudo().write({"stripe_customer_id": False})
            return None
        return cust_id
    except stripe_client.error.InvalidRequestError as exc:  # no such customer / resource_missing
        if getattr(exc, "code", "") == "resource_missing":
            partner.sudo().write({"stripe_customer_id": False})
            return None
        # Unknown validation error; propagate so caller can decide
        raise


def _ensure_stripe_customer(stripe_client, partner):
    """Ensure a Stripe customer exists for the given partner and return its ID."""
    try:
        cust_id = _validate_or_clear_stripe_customer(stripe_client, partner)
        if cust_id:
            return cust_id

        # Create new customer
        _logger.info("Creating new Stripe customer for partner %s (%s)", partner.name, partner.id)
        customer_data = {
            "email": partner.email,
            "name": partner.name,
            "metadata": {"odoo_partner_id": str(partner.id), "source": "odoo_managed_rollup"},
        }

        # Add address if available
        if partner.street or partner.city or partner.country_id:
            address = {}
            if partner.street:
                address["line1"] = partner.street
            if partner.city:
                address["city"] = partner.city
            if partner.country_id:
                address["country"] = partner.country_id.code
            if partner.zip:
                address["postal_code"] = partner.zip
            if address:
                customer_data["address"] = address

        stripe_customer = stripe_client.Customer.create(**customer_data)
        cust_id = stripe_customer.id
        partner.sudo().write({"stripe_customer_id": cust_id})
        return cust_id
    except Exception as exc:
        _logger.error("Failed to ensure/create Stripe customer for partner %s: %s", partner.id, exc)
        return None

# ------------------------------------------------------------------
# Odoo-Managed Rollup Billing Engine
# ------------------------------------------------------------------

def action_charge_rollup(service) -> bool:
    """Execute the actual Stripe charge for an Odoo-managed rollup service."""
    if not service.is_odoo_managed or not service.payment_vault_id:
        _logger.warning("Rollup %s is not Odoo-managed or has no vaulted payment method.", service.id)
        return False

    stripe_secret_key = service.env['ir.config_parameter'].sudo().get_param('stripe_secret_key')
    if not stripe_secret_key:
        _logger.error("Stripe secret key not configured.")
        return False
    
    stripe.api_key = stripe_secret_key
    
    # Resolve amount based on frequency
    freq = service.type_id.payment_frequency or 'month'
    if freq == 'month':
        amount = service.type_id.amount_month
    elif freq == 'quarter':
        amount = service.type_id.amount_quarter
    elif freq == 'year':
        amount = service.type_id.amount_year
    else:
        amount = service.type_id.cost or 0

    if amount <= 0:
        _logger.warning("Rollup %s has zero cost, skipping charge.", service.id)
        return True

    # Resolve customer ID with fallback to partner
    stripe_customer_id = service.stripe_customer_id
    if not stripe_customer_id or str(stripe_customer_id).lower() in ('false', 'none', ''):
        stripe_customer_id = service.customer_id.stripe_customer_id
        if stripe_customer_id and str(stripe_customer_id).lower() not in ('false', 'none', ''):
            service.write({'stripe_customer_id': stripe_customer_id})
    
    if not stripe_customer_id or str(stripe_customer_id).lower() in ('false', 'none', ''):
        _logger.error("Rollup %s has no valid Stripe customer ID.", service.id)
        return False

    try:
        # Decrypt the vaulted payment method ID
        pm_id_enc = service.payment_vault_id.stripe_payment_method_id
        pm_id_raw = service.payment_vault_id._decrypt_id(pm_id_enc)

        # Create PaymentIntent for off-session charge
        intent = stripe.PaymentIntent.create(
            amount=int(amount * 100),
            currency='usd',
            customer=stripe_customer_id,
            payment_method=pm_id_raw,
            off_session=True,
            confirm=True,
            metadata={
                'rollup_service_id': str(service.id),
                'billing_type': 'odoo_managed_rollup_recurrence',
            }
        )
        
        if intent.status == 'succeeded':
            now = fields.Datetime.now()
            next_date = _get_next_rollup_billing_date(service, now)
            
            service.write({
                'last_charge_date': now,
                'next_billing_date': next_date,
                'charge_retry_count': 0,
                'subscription_status': 'active',
            })
            
            # Create invoice (model's method expects dict/object)
            service.create_invoice(intent)
            
            _logger.info("Successfully charged Odoo-managed rollup %s. Next date: %s", service.id, next_date)
            return True
        else:
            _logger.warning("PaymentIntent status for rollup %s: %s", service.id, intent.status)
            service.write({'charge_retry_count': service.charge_retry_count + 1})
            return False

    except stripe.error.CardError as e:
        _logger.error("Card error for rollup %s: %s", service.id, str(e))
        service.write({'charge_retry_count': service.charge_retry_count + 1})
        # Trigger failure notification
        try:
             send_rollup_email(
                "rollup_management.mail_template_rollup_subscription_due",
                service,
                {"next_billing_date": service.next_billing_date, "error_msg": str(e)},
            )
        except Exception:
            _logger.warning("Failed to send failure email for rollup %s", service.id)
        return False
    except Exception as e:
        _logger.exception("Billing failed for rollup %s: %s", service.id, str(e))
        service.write({'charge_retry_count': service.charge_retry_count + 1})
        return False

def _get_next_rollup_billing_date(service, from_dt):
    """Calculate the next billing date based on frequency."""
    freq = service.type_id.payment_frequency or 'month'
    start_date = from_dt.date() if isinstance(from_dt, datetime) else from_dt
    
    if freq == 'day':
        return start_date + timedelta(days=1)
    elif freq == 'week':
        return start_date + timedelta(weeks=1)
    elif freq == 'month':
        return start_date + relativedelta(months=1)
    elif freq == 'quarter':
        return start_date + relativedelta(months=3)
    elif freq == 'year':
        return start_date + relativedelta(years=1)
    return start_date + relativedelta(months=1)

def run_rollup_billing_cron(env) -> int:
    """Cron entry point for Odoo-managed rollup billing."""
    Service = env["rollup.service"].sudo()
    today = fields.Date.context_today(Service)
    
    # Process active or recently due services
    services = Service.search([
        ('is_odoo_managed', '=', True),
        ('subscription_status', 'in', ['active', 'pending_payment', 'overdue']),
        ('next_billing_date', '<=', today),
        ('charge_retry_count', '<', 3),
    ])
    print(services,'-----------1065')
    _logger.info("Rollup Billing Cron: %s services due.", len(services))
    success_count = 0
    for service in services:
        if action_charge_rollup(service):
            success_count += 1
        env.cr.commit()
    return success_count

# ------------------------------------------------------------------
# Migration Logic
# ------------------------------------------------------------------

def action_migrate_rollup_to_odoo_managed(service) -> bool:
    """Transition a Stripe-native rollup to Odoo-managed billing."""
    if service.is_odoo_managed or not service.stripe_subscription_id:
        return False

    stripe_secret_key = service.env['ir.config_parameter'].sudo().get_param('stripe_secret_key')
    stripe.api_key = stripe_secret_key

    try:
        # 1. Retrieve Stripe Subscription
        stripe_sub = stripe.Subscription.retrieve(
            service.stripe_subscription_id,
            expand=['default_payment_method']
        )
        
        # 2. Extract and vault PM
        pm = stripe_sub.get('default_payment_method')
        if pm:
            partner = service.customer_id
            pm_rec = service.env['stripe.payment.method'].sudo().create_or_update_from_stripe(partner, pm)
            service.payment_vault_id = pm_rec.id

        # 3. Cancel Stripe recurring auto-billing
        stripe.Subscription.modify(
            service.stripe_subscription_id,
            cancel_at_period_end=True
        )

        # 4. Take over in Odoo
        period_end_dt = datetime.fromtimestamp(stripe_sub.current_period_end)
        service.write({
            'is_odoo_managed': True,
            'stripe_customer_id': stripe_sub.customer,
            'next_billing_date': period_end_dt.date(),
            'subscription_status': 'active',
            'charge_retry_count': 0,
        })
        
        _logger.info("Rollup %s migrated to Odoo-managed. Takeover on %s", service.id, period_end_dt.date())
        return True

    except Exception as e:
        _logger.exception("Migration failed for rollup %s: %s", service.id, str(e))
        return False

def cron_migrate_rollups_to_odoo_managed(env) -> int:
    """Batch migration task."""
    Service = env["rollup.service"].sudo()
    services = Service.search([
        ('is_odoo_managed', '=', False),
        ('stripe_subscription_id', '!=', False),
        ('subscription_status', '=', 'active'),
    ], limit=50)
    
    migrated = 0
    for service in services:
        if action_migrate_rollup_to_odoo_managed(service):
            migrated += 1
        env.cr.commit()
    return migrated
