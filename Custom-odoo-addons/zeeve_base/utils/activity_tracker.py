# -*- coding: utf-8 -*-
"""Helpers to assemble a user-facing activity tracker payload."""

from datetime import datetime, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from odoo import fields


def _to_datetime(env, value):
    """Normalize date/datetime values to a datetime object when possible."""
    if not value:
        return False
    if isinstance(value, str):
        try:
            return fields.Datetime.to_datetime(value)
        except Exception:  # pragma: no cover - defensive fallback
            return False
    if hasattr(value, "hour"):
        return value
    try:
        return fields.Datetime.to_datetime(value)
    except Exception:  # pragma: no cover - defensive fallback
        return False


def _get_output_timezone(env):
    """Resolve the output timezone from request/env context or server local time."""
    tz_name = (env.context.get("tz") if env else None) or ""
    if tz_name:
        try:
            return ZoneInfo(tz_name)
        except ZoneInfoNotFoundError:  # pragma: no cover - defensive fallback
            pass
    return datetime.now().astimezone().tzinfo


def _to_output_datetime(env, value):
    """Convert Odoo UTC-style datetimes into the requested output timezone."""
    normalized = _to_datetime(None, value)
    if not normalized:
        return False
    output_tz = _get_output_timezone(env)
    if normalized.tzinfo is None:
        normalized = normalized.replace(tzinfo=timezone.utc)
    return normalized.astimezone(output_tz).replace(tzinfo=None)


def _serialize_datetime(env, value):
    """Convert date/datetime to an API-friendly string in the output timezone."""
    normalized = _to_output_datetime(env, value)
    return fields.Datetime.to_string(normalized) if normalized else None


def _status_from_payment_log(record):
    if record.payment_status == "succeeded":
        return "success"
    if record.payment_status == "failed":
        return "failed"
    if record.payment_status in ("pending", "requires_action"):
        return "warning"
    return "info"


def _payment_log_title(record):
    mapping = {
        "invoice.payment_succeeded": "Payment succeeded",
        "invoice.payment_failed": "Payment failed",
        "payment_intent.succeeded": "Payment intent succeeded",
        "payment_intent.payment_failed": "Payment intent failed",
        "customer.subscription.created": "Subscription created in Stripe",
        "customer.subscription.updated": "Subscription updated in Stripe",
        "customer.subscription.deleted": "Subscription cancelled in Stripe",
        "checkout.session.completed": "Checkout completed",
    }
    return mapping.get(record.event_type, record.event_type.replace(".", " ").title())


def _validator_transaction_title(action):
    action_label = (action or "").replace("_", " ").replace("-", " ").strip()
    if not action_label:
        return "Validator transaction recorded"
    return "%s transaction recorded" % action_label.title()


def _subscription_amount_label(subscription):
    quantity = subscription.quantity or 1.0
    total_amount = (subscription.price or 0.0) * quantity
    currency_label = ""
    if subscription.currency_id:
        currency_label = subscription.currency_id.symbol or subscription.currency_id.name or ""
    return "%s%s" % (currency_label, total_amount)


def _subscription_node_summary(subscription, nodes):
    subscription_nodes = nodes.filtered(lambda node: node.subscription_id.id == subscription.id) if nodes else nodes
    node_names = [node.node_name for node in subscription_nodes if node.node_name]
    network_types = [
        node.network_selection_id.name
        for node in subscription_nodes
        if node.network_selection_id and node.network_selection_id.name
    ]
    return {
        "nodes": subscription_nodes,
        "node_names": node_names,
        "network_types": list(dict.fromkeys(network_types)),
    }


def _append_activity(env, bucket, **activity):
    """Append only activities with a usable timestamp."""
    timestamp = _to_output_datetime(env, activity.get("timestamp"))
    if not timestamp:
        return
    activity["timestamp"] = fields.Datetime.to_string(timestamp)
    bucket.append(activity)


def get_user_activity_payload(env, user, limit=100, include_activities=False):
    """Return activity summary, and optionally timeline data, for the user."""
    limit = max(1, min(int(limit or 100), 500))
    partner = user.partner_id
    activities = []

    last_login_value = getattr(user, "login_date", False) or getattr(user, "jwt_refresh_token_issued_at", False)
    if last_login_value:
        _append_activity(
            env,
            activities,
            id="login-last",
            category="security",
            activity_type="login",
            resource_name="Account",
            title="Last login",
            description="User authenticated successfully.",
            status="success",
            timestamp=last_login_value,
            metadata={},
        )
    password_changed_at = getattr(user, "password_changed_at", False)
    if password_changed_at:
        _append_activity(
            env,
            activities,
            id="password-change-last",
            category="security",
            activity_type="password_changed",
            resource_name="Account",
            title="Password changed",
            description="Password updated successfully.",
            status="success",
            timestamp=password_changed_at,
            metadata={},
        )

    subscriptions = env["subscription.subscription"].sudo().search([("customer_name", "=", partner.id)]) \
        if "subscription.subscription" in env else env["res.users"].browse()

    subscription_ids = subscriptions.ids if subscriptions else []
    nodes = env["subscription.node"].sudo().search([("subscription_id", "in", subscription_ids)]) \
        if subscription_ids and "subscription.node" in env else env["res.users"].browse()

    for subscription in subscriptions:
        node_summary = _subscription_node_summary(subscription, nodes)
        primary_node = node_summary["nodes"][:1]
        primary_node = primary_node[0] if primary_node else False
        plan_name = subscription.sub_plan_id.name if subscription.sub_plan_id else (subscription.name or "Subscription")
        protocol_name = subscription.protocol_id.name if subscription.protocol_id else "protocol"
        amount_label = _subscription_amount_label(subscription)
        node_count = len(node_summary["node_names"])
        node_label = "node" if node_count == 1 else "nodes"
        node_names_label = ", ".join(node_summary["node_names"]) if node_summary["node_names"] else "No nodes"
        created_node_name = primary_node.node_name if primary_node and primary_node.node_name else "No node"
        created_network_type = (
            primary_node.network_selection_id.name
            if primary_node and primary_node.network_selection_id and primary_node.network_selection_id.name
            else "Unknown network"
        )
        resource_name = f"{subscription.protocol_id.name} {subscription.subscription_type.replace('_', ' ').title()} Node"
        subscription_metadata = {
                "state": subscription.state,
                "stripe_status": subscription.stripe_status,
                "plan_name": subscription.sub_plan_id.name if subscription.sub_plan_id else None,
                "protocol_name": subscription.protocol_id.name if subscription.protocol_id else None,
                "subscription_type": subscription.subscription_type,
                "node_names": [created_node_name] if primary_node else [],
                "network_types": [created_network_type] if primary_node else [],
                "amount": subscription.price,
                "currency": subscription.currency_id.name if subscription.currency_id else None,
        }
        _append_activity(
            env,
            activities,
            id="subscription-%s-created" % subscription.id,
            category="subscription",
            activity_type="subscription_created",
            title="Subscription created",
            description="%s plan of %s with amount %s was created for %s: %s." % (
                plan_name,
                protocol_name,
                amount_label,
                node_label,
                node_names_label,
            ),
            status="success",
            timestamp=subscription.subscribed_on or subscription.create_date,
            subscription_id=subscription.id,
            subscription_uuid=subscription.subscription_uuid,
            metadata=subscription_metadata,
            resource_name=resource_name,
        )
        if (
            subscription.source == "so"
            and subscription.state in ("draft", "requested")
            and not subscription.subscribed_on
            and (subscription.stripe_status or "draft") in ("draft", "incomplete", "incomplete_expired", "unpaid")
        ):
            _append_activity(
                env,
                activities,
                id="subscription-%s-payment-incomplete" % subscription.id,
                category="subscription",
                activity_type="subscription_payment_incomplete",
                title="Subscription payment not completed",
                description="%s plan of %s with amount %s was initiated for %s: %s, but payment was not completed." % (
                    plan_name,
                    protocol_name,
                    amount_label,
                    node_label,
                    node_names_label,
                ),
                status="failed" if subscription.stripe_status in ("incomplete_expired", "unpaid") else "warning",
                timestamp=subscription.create_date,
                subscription_id=subscription.id,
                subscription_uuid=subscription.subscription_uuid,
                metadata=dict(subscription_metadata, checkout_incomplete=True),
                resource_name=resource_name,
            )
        if subscription.last_billing_on:
            _append_activity(
                env,
                activities,
                id="subscription-%s-last-billing" % subscription.id,
                category="billing",
                activity_type="billing_charge",
                title="Recurring billing processed",
                description="Recurring billing of %s was recorded for %s: %s." % (
                    amount_label,
                    node_label,
                    node_names_label,
                ),
                status="success",
                timestamp=subscription.last_billing_on,
                subscription_id=subscription.id,
                subscription_uuid=subscription.subscription_uuid,
                metadata={
                    "next_payment_date": _serialize_datetime(env, subscription.next_payment_date),
                    "current_term_end": _serialize_datetime(env, subscription.current_term_end),
                    "node_names": node_summary["node_names"],
                    "network_types": node_summary["network_types"],
                    "amount": (subscription.price or 0.0) * (subscription.quantity or 1.0),
                    "currency": subscription.currency_id.name if subscription.currency_id else None,
                },
                resource_name=resource_name,
            )

    # if "account.move" in env and subscription_ids:
    #     invoices = env["account.move"].sudo().search(
    #         [("subscription_id", "in", subscription_ids)],
    #         order="invoice_date desc, create_date desc, id desc",
    #     )
    #     for invoice in invoices:
    #         _append_activity(
    #             activities,
    #             id="invoice-%s" % invoice.id,
    #             category="billing",
    #             activity_type="invoice_generated",
    #             title="Invoice generated",
    #             description="Invoice %s was generated." % (invoice.name or invoice.ref or invoice.id),
    #             status="success" if invoice.state == "posted" else "info",
    #             timestamp=invoice.invoice_date or invoice.create_date,
    #             subscription_id=invoice.subscription_id.id,
    #             subscription_uuid=invoice.subscription_id.subscription_uuid if invoice.subscription_id else None,
    #             metadata={
    #                 "invoice_id": invoice.id,
    #                 "invoice_name": invoice.name,
    #                 "invoice_state": invoice.state,
    #                 "payment_state": invoice.payment_state,
    #                 "amount_total": invoice.amount_total,
    #                 "currency": invoice.currency_id.name if invoice.currency_id else None,
    #             },
    #         )

    # if "account.payment" in env and subscription_ids:
    #     payments = env["account.payment"].sudo().search(
    #         [("subscription_id", "in", subscription_ids)],
    #         order="date desc, create_date desc, id desc",
    #     )
    #     for payment in payments:
    #         _append_activity(
    #             activities,
    #             id="payment-%s" % payment.id,
    #             category="billing",
    #             activity_type="payment_recorded",
    #             title="Payment recorded",
    #             description="Payment %s was recorded." % (payment.name or payment.ref or payment.id),
    #             status="success" if payment.state == "posted" else "info",
    #             timestamp=payment.date or payment.create_date,
    #             subscription_id=payment.subscription_id.id,
    #             subscription_uuid=payment.subscription_id.subscription_uuid if payment.subscription_id else None,
    #             metadata={
    #                 "payment_id": payment.id,
    #                 "payment_name": payment.name,
    #                 "payment_state": payment.state,
    #                 "amount": payment.amount,
    #                 "currency": payment.currency_id.name if payment.currency_id else None,
    #             },
    #         )

    if "stripe.payment.log" in env and subscription_ids:
        payment_logs = env["stripe.payment.log"].sudo().search(
            [
                ("subscription_id", "in", subscription_ids),
                ("event_type", "=", "invoice.payment_failed"),
            ],
            order="stripe_created desc, create_date desc, id desc",
        )
        for payment_log in payment_logs:
            subscription = payment_log.subscription_id
            subscription_node_summary = _subscription_node_summary(subscription, nodes) if subscription else {
                "node_names": [],
                "network_types": [],
            }
            node_count = len(subscription_node_summary["node_names"])
            node_label = "node" if node_count == 1 else "nodes"
            node_names_label = (
                ", ".join(subscription_node_summary["node_names"])
                if subscription_node_summary["node_names"]
                else "No nodes"
            )
            _append_activity(
                env,
                activities,
                id="stripe-log-%s" % payment_log.id,
                category="billing",
                activity_type="payment_event",
                title="Recurring payment failed",
                description=(
                    payment_log.failure_reason
                    or payment_log.description
                    or "Recurring billing failed for %s: %s." % (node_label, node_names_label)
                ),
                status="failed",
                timestamp=payment_log.stripe_created or payment_log.create_date,
                subscription_id=payment_log.subscription_id.id if payment_log.subscription_id else None,
                subscription_uuid=payment_log.subscription_id.subscription_uuid if payment_log.subscription_id else None,
                metadata={
                    "event_id": payment_log.event_id,
                    "event_type": payment_log.event_type,
                    "payment_status": payment_log.payment_status,
                    "subscription_status": payment_log.subscription_status,
                    "amount": payment_log.amount,
                    "currency": payment_log.currency,
                    "failure_reason": payment_log.failure_reason,
                },
                resource_name=(
                    f"{subscription.protocol_id.name} {subscription.subscription_type.replace('_', ' ').title()} Node"
                    if subscription and subscription.protocol_id and subscription.subscription_type
                    else None
                ),
            )

    if "subscription.validator.transaction" in env and subscription_ids:
        validator_transactions = env["subscription.validator.transaction"].sudo().search(
            [("subscription_id", "in", subscription_ids)],
            order="create_date desc, id desc",
        )
        for tx in validator_transactions:
            subscription = tx.subscription_id
            node = tx.node_id
            _append_activity(
                activities,
                id="validator-transaction-%s" % tx.id,
                category="validator",
                activity_type="validator_transaction",
                title=_validator_transaction_title(tx.action),
                description=tx.notes or "Validator transaction %s was recorded." % (tx.transaction_hash or tx.id),
                status="info",
                timestamp=tx.create_date,
                subscription_id=subscription.id if subscription else None,
                subscription_uuid=subscription.subscription_uuid if subscription else None,
                node_id=node.id if node else None,
                metadata={
                    "transaction_id": tx.id,
                    "transaction_hash": tx.transaction_hash,
                    "action": tx.action,
                    "notes": tx.notes,
                    "node_name": node.node_name if node else None,
                    "protocol_name": subscription.protocol_id.name if subscription and subscription.protocol_id else None,
                },
                resource_name=(node.node_name if node else (subscription.name if subscription else "validator")),
            )

    if "etherlink.node.config.update" in env and subscription_ids:
        config_updates = env["etherlink.node.config.update"].sudo().search(
            [("subscription_id", "in", subscription_ids)],
            order="updated_at desc, id desc",
        )
        for config_update in config_updates:
            node = env["subscription.node"].sudo().search([("node_identifier", "=", config_update.node_id)], limit=1)
            _append_activity(
                env,
                activities,
                id="config-update-%s" % config_update.id,
                category="infrastructure",
                activity_type="config_updated",
                title="Node configuration updated",
                description="Configuration was updated for node %s." % config_update.node_id,
                status="success" if config_update.status == "success" else "info",
                timestamp=config_update.updated_at,
                subscription_id=config_update.subscription_id.id,
                subscription_uuid=config_update.subscription_id.subscription_uuid if config_update.subscription_id else None,
                node_id=config_update.node_id,
                metadata={
                    "protocol_name": config_update.protocol_name,
                    "user_email": config_update.user_email,
                    "status": config_update.status,
                },
                resource_name=node.node_name,

            )

    activities.sort(key=lambda item: item["timestamp"] or "", reverse=True)
    total_activity_count = len(activities)
    activities = activities[:limit]

    last_payment_success = next(
        (item for item in activities if item["category"] == "billing" and item["status"] == "success"),
        None,
    )
    last_payment_failure = next(
        (item for item in activities if item["activity_type"] == "stripe_event" and item["status"] == "failed"),
        None,
    )
    last_subscription = next(
        (item for item in activities if item["activity_type"] == "subscription_created"),
        None,
    )
    next_billing_subscription = subscriptions.sorted(
        key=lambda sub: sub.next_payment_date or sub.stripe_end_date or fields.Datetime.now(),
        reverse=False,
    )[:1] if subscriptions else False
    ready_nodes = nodes.filtered(lambda node: node.state == "ready") if nodes else nodes
    active_nodes = nodes.filtered(lambda node: node.state not in ("closed", "deleted")) if nodes else nodes
    last_node_created = nodes.sorted(
        key=lambda node: node.node_created_date or node.create_date or fields.Datetime.now(),
        reverse=True,
    )[:1] if nodes else False
    last_ready_candidates = []
    for node in nodes:
        ready_at = node.get_ready_at_from_chatter() if hasattr(node, "get_ready_at_from_chatter") else False
        if ready_at:
            last_ready_candidates.append((ready_at, node))
    last_ready_candidates.sort(key=lambda item: item[0], reverse=True)
    last_ready_at = last_ready_candidates[0][0] if last_ready_candidates else False

    summary = {
        "last_login_at": _serialize_datetime(env, last_login_value),
        "last_subscribed_at": last_subscription["timestamp"] if last_subscription else None,
        "last_payment_at": last_payment_success["timestamp"] if last_payment_success else None,
        "last_failed_payment_at": last_payment_failure["timestamp"] if last_payment_failure else None,
        "next_billing_at": _serialize_datetime(
            env,
            next_billing_subscription.next_payment_date or next_billing_subscription.stripe_end_date
        ) if next_billing_subscription else None,
        "active_subscription_count": len(subscriptions.filtered(lambda sub: sub.state not in ("closed", "deleted"))),
        "total_activity_count": total_activity_count,
        "total_subscription_count": len(subscriptions),
        "last_invoice_at": next(
            (item["timestamp"] for item in activities if item["activity_type"] == "invoice_generated"),
            None,
        ),
        "last_config_update_at": next(
            (item["timestamp"] for item in activities if item["activity_type"] == "config_updated"),
            None,
        ),
        "total_node_count": len(nodes),
        "active_node_count": len(active_nodes),
        "ready_node_count": len(ready_nodes),
        "last_node_created_at": _serialize_datetime(
            env,
            last_node_created.node_created_date or last_node_created.create_date
        ) if last_node_created else None,
        "last_node_ready_at": _serialize_datetime(env, last_ready_at),
    }

    if include_activities:
        return {
            "summary": summary,
            "activities": activities,
        }
    return summary
