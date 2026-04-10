# -*- coding: utf-8 -*-
"""Utility helpers for subscription mail dispatch."""
from __future__ import annotations

from typing import Any, Dict, Optional, Union

from odoo import api, fields
from odoo.exceptions import UserError
from odoo.tools.misc import format_date

from ...zeeve_base.utils import base_utils


_TEMPLATE_MAP: Dict[str, str] = {
    'new_subscription_admin': 'subscription_management.mail_template_subscription_new_subscription_admin',
    'node_subscription': 'subscription_management.mail_template_subscription_node_journey_subscription',
    'node_provisioning_complete': 'subscription_management.mail_template_subscription_node_provisioning_complete',
    'node_ready': 'subscription_management.mail_template_subscription_node_ready',
    'subscription_active': 'subscription_management.mail_template_subscription_active',
    'payment_success_admin': 'subscription_management.mail_template_subscription_payment_success_admin',
    'validator_node_subscription': 'subscription_management.mail_template_subscription_validator_node_journey_subscription',
    'validator_provisioning_complete': 'subscription_management.mail_template_subscription_validator_provisioning_complete',
    'subscription_renewal': 'subscription_management.mail_template_subscription_node_renewed',
    'subscription_cancelled_customer': 'subscription_management.mail_template_subscription_cancelled_customer',
    'subscription_cancelled_admin': 'subscription_management.mail_template_subscription_cancelled_admin',
    'validator_staking': 'subscription_management.mail_template_validator_staking_admin',
}


def _resolve_template(env, template_key: str):
    """Return the mail.template record for a given key or xml id."""
    if not template_key:
        raise UserError('Template key is required to send a subscription email.')

    xml_id = template_key if '.' in template_key else _TEMPLATE_MAP.get(template_key)
    if not xml_id:
        raise UserError("Unknown subscription mail template key: %s" % template_key)

    template = env.ref(xml_id, raise_if_not_found=False)
    if template:
        template = template.sudo()
    if not template:
        raise UserError("Subscription mail template '%s' could not be located." % xml_id)
    return template


def send_subscription_email(
    env,
    template_key: str,
    record: Optional[api.model] = None,
    record_id: Optional[int] = None,
    model: Optional[str] = None,
    context: Optional[Dict[str, Any]] = None,
    email_to: Optional[str] = None,
    email_cc: Optional[str] = None,
    email_bcc: Optional[str] = None,
    force_send: bool = True,
) -> Union[int, bool]:
    """Send a subscription-related email using a reusable template helper."""
    send_env = env
    template = _resolve_template(send_env, template_key)

    if record is None:
        target_model = model or template.model_id.model
        if not target_model:
            raise UserError('Unable to determine the model for the subscription email.')
        if record_id is None:
            raise UserError('A record or record_id must be provided to send the email.')
        record = send_env[target_model].sudo().browse(record_id)
    elif record_id is None:
        record_id = record.id

    if record is not None:
        record = record.sudo()

    if not record or not record.exists():
        raise UserError('The target record for the subscription email could not be found.')

    send_ctx = dict(template.env.context or {})
    if context:
        send_ctx.update(context)

    email_values: Dict[str, Any] = {}
    if email_to:
        email_values['email_to'] = email_to
    if email_cc:
        email_values['email_cc'] = email_cc
    if email_bcc:
        email_values['email_bcc'] = email_bcc

    return template.with_context(send_ctx).send_mail(
        record_id,
        force_send=force_send,
        raise_exception=False,
        email_values=email_values or None,
    )


def get_backend_base_url(env) -> str:
    """Return the configured backend/base URL stripped of trailing slashes."""

    Param = env['ir.config_parameter'].sudo()
    base_url = (
        Param.get_param('backend_url')
        or Param.get_param('web.base.url')
        or ''
    )
    return base_url.rstrip('/')


def build_support_info(env, *, base_url: Optional[str] = None, overrides: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """Assemble support metadata (logo, homepage, contacts, social links)."""

    overrides = overrides or {}
    company = env.user.company_id or env['res.company'].sudo().search([], limit=1)
    config = env['zeeve.config'].sudo().search([], limit=1)

    base_url_value = base_url or get_backend_base_url(env)
    logo_path = overrides.get('logoPath')
    if not logo_path and company and company.logo and base_url_value:
        logo_path = f"{base_url_value}/web/image/res.company/{company.id}/logo"
    if not logo_path:
        logo_path = 'https://demo.studiobrahma.in/zeeve/mailer/images/zeeve-logo2.png'

    homepage = overrides.get('homepageURL') or 'https://www.zeeve.io/'
    support_email = overrides.get('email')
    if not support_email:
        support_email = (
            (config and config.support_email)
            or (company and (company.email_formatted or company.email))
            or 'support@zeeve.io'
        )

    social_links = overrides.get('social')
    if social_links is None and config:
        social_links = {
            'twitter': config.twitter_url,
            'linkedin': config.linkedin_url,
            'telegram': config.telegram_url,
        }

    return {
        'logoPath': logo_path,
        'homepageURL': homepage,
        'email': support_email,
        'social': social_links,
    }


def prepare_subscription_cancellation_context(
    subscription,
    *,
    cancellation_reason: Optional[str] = None,
    cancellation_date: Optional[fields.Date] = None,
    tenant: Optional[Dict[str, Any]] = None,
    support_info: Optional[Dict[str, Any]] = None,
    base_url: Optional[str] = None,
    notification_mode: str = 'cancellation',
    quantity_delta: Optional[int] = None,
    updated_quantity: Optional[int] = None,
    previous_quantity: Optional[int] = None,
) -> Dict[str, Any]:
    """Return the mail context payload consumed by cancellation templates."""

    subscription.ensure_one()

    env = subscription.env
    partner = subscription.customer_name
    currency = subscription.currency_id or subscription.company_id.currency_id or env.company.currency_id
    base_url_value = (base_url or get_backend_base_url(env))

    cancel_date_value = cancellation_date or fields.Date.context_today(subscription)
    cancel_date_display = ''
    if cancel_date_value:
        try:
            cancel_date_display = format_date(env, cancel_date_value)
        except Exception:  # pragma: no cover - fallback to default formatting
            cancel_date_display = fields.Date.to_string(cancel_date_value)

    price_value = subscription.price or 0.0
    plan_name = subscription.sub_plan_id.name or subscription.name or 'Subscription'
    currency_symbol = (currency and (currency.symbol or currency.name)) or ''
    invoice = subscription.invoice_ids.sorted('invoice_date', reverse=True)[0] if subscription.invoice_ids else False
    if invoice:
        currency = invoice.currency_id
        currency_symbol = currency.symbol or currency.name

    frequency_value = (subscription.payment_frequency or '').strip().lower()
    frequency_label_map = {
        'monthly': 'Monthly',
        'quarterly': 'Quarterly',
        'annually': 'Yearly',
        'annual': 'Yearly',
        'yearly': 'Yearly',
    }
    frequency_label = frequency_label_map.get(frequency_value, 'Monthly')

    plan_details = {
        'Plan_Name': plan_name,
        'plan_name': plan_name,
        'subscription_id': subscription.name or subscription.subscription_ref or '',
        'subscription_monthly_cost': f"{price_value:.2f}" if price_value else '0.00',
        'subscription_cost_label': f"{frequency_label} Subscription Cost",
        'currency_symbol': currency_symbol,
        'currency': (currency and currency.name) or '',
        'cancellation_date': cancel_date_display,
        'status': subscription.stripe_status or subscription.state or 'canceled',
        'buyer_email_id': partner.email or '',
        'buyerEmail': partner.email or '',
        'protocol': subscription.protocol_id.name or '',
        'plan': subscription.sub_plan_id.name or ''
    }
    if cancellation_reason:
        plan_details['cancellation_reason'] = cancellation_reason

    first_name = getattr(partner, 'first_name', False) or partner.name or partner.display_name or 'Customer'
    last_name = getattr(partner, 'last_name', False) or ''
    customer_name_payload = {
        'firstname': first_name,
        'lastname': last_name,
        'name': partner.display_name or partner.name or first_name,
    }

    support_payload = build_support_info(env, base_url=base_url_value, overrides=support_info)

    is_quantity_reduction = notification_mode == 'quantity_reduction'
    base_context = {
        'name': customer_name_payload,
        'customer_name': customer_name_payload,
        'planDetails': plan_details,
        'plan_details': plan_details,
        'tenant': tenant or {},
        'supportInfo': support_payload,
        'support_info': support_payload,
        'baseUrl': base_url_value,
        'base_url': base_url_value,
        'email_to': partner.email or partner.email_formatted or '',
        'notification_mode': notification_mode,
        'is_quantity_reduction': is_quantity_reduction,
        'quantity_delta': quantity_delta,
        'updated_quantity': updated_quantity if updated_quantity is not None else subscription.quantity,
        'previous_quantity': previous_quantity if previous_quantity is not None else subscription.quantity,
    }
    if cancellation_reason:
        base_context['cancellation_reason'] = cancellation_reason

    return base_context


def send_subscription_cancellation_emails(
    subscription,
    *,
    cancellation_reason: Optional[str] = None,
    cancellation_date: Optional[fields.Date] = None,
    tenant: Optional[Dict[str, Any]] = None,
    support_info: Optional[Dict[str, Any]] = None,
    base_url: Optional[str] = None,
    notification_mode: str = 'cancellation',
    quantity_delta: Optional[int] = None,
    updated_quantity: Optional[int] = None,
    previous_quantity: Optional[int] = None,
) -> None:
    """Dispatch both customer and admin cancellation notifications."""

    subscription.ensure_one()

    context = prepare_subscription_cancellation_context(
        subscription,
        cancellation_reason=cancellation_reason,
        cancellation_date=cancellation_date,
        tenant=tenant,
        support_info=support_info,
        base_url=base_url,
        notification_mode=notification_mode,
        quantity_delta=quantity_delta,
        updated_quantity=updated_quantity,
        previous_quantity=previous_quantity,
    )

    send_subscription_email(
        subscription.env,
        'subscription_cancelled_customer',
        record=subscription,
        context=context,
    )
    admin_payload = subscription._admin_recipient_payload()
    admin_emails = admin_payload.get('to') or []
    admin_context = dict(context)
    send_subscription_email(
        subscription.env,
        'subscription_cancelled_admin',
        record=subscription,
        context=admin_context,
        email_to=','.join(admin_emails),
        email_cc=",".join(admin_payload.get('cc', [])) if admin_payload.get('cc') else False,
    )


def send_validator_staking_notification(
    env,
    node,
    *,
    action_type: str = 'staking',
    status: str = 'success',
    protocol_name: Optional[str] = None,
    error_message: Optional[str] = None,
    error_details: Optional[str] = None,
    updated_fields: Optional[list] = None,
    minimum_reward: Optional[str] = None,
    interval: Optional[str] = None,
    host_id: Optional[str] = None,
    base_url: Optional[str] = None,
) -> Union[int, bool]:
    """Send validator staking/restake configuration notification to admin.
    
    Args:
        env: Odoo environment
        node: subscription.node record
        action_type: 'staking' or 'restake' - the type of action
        status: 'success' or 'failed'
        protocol_name: Name of the protocol
        error_message: Error message if status is 'failed'
        error_details: Detailed error information
        updated_fields: List of dicts with 'field_name' and 'field_value' keys
        minimum_reward: Minimum reward threshold (for restake)
        interval: Restake interval (for restake)
        host_id: Host ID (for restake)
        base_url: Backend base URL
    
    Returns:
        Result from send_mail
    """
    node.ensure_one()
    
    subscription = node.subscription_id
    partner = subscription.customer_name
    protocol = subscription.protocol_id
    base_url_value = base_url or get_backend_base_url(env)
    
    validator_ctx = {
        'action_type': action_type,
        'status': status,
        'protocol_name': protocol_name or (protocol.name if protocol else 'N/A'),
        'node_id': node.node_identifier or 'N/A',
        'subscription_id': subscription.subscription_uuid or subscription.id or 'N/A',
        'validator_address': node.node_identifier or 'N/A',
        'customer_name': partner.display_name or partner.name or 'Customer',
        'customer_email': partner.email or partner.email_formatted or '',
        'timestamp': fields.Datetime.to_string(fields.Datetime.now()),
    }
    
    if error_message:
        validator_ctx['error_message'] = error_message
    if error_details:
        validator_ctx['error_details'] = error_details
    if updated_fields:
        validator_ctx['updated_fields'] = updated_fields
    if minimum_reward:
        validator_ctx['minimum_reward'] = minimum_reward
    if interval:
        validator_ctx['interval'] = interval
    if host_id:
        validator_ctx['host_id'] = host_id
    
    context = {
        'action_type': action_type,
        'validator_ctx': validator_ctx,
        'base_url': base_url_value,
        'baseUrl': base_url_value,
    }
    
    # Get admin recipients using the same method as subscription emails
    channel_code = None
    if protocol and protocol.admin_channel_id:
        channel_code = protocol.admin_channel_id.code
    
    admin_recipients = base_utils._get_admin_recipients(env, channel_code=channel_code)
    admin_emails = ','.join(admin_recipients.get('to', []))
    admin_cc = ','.join(admin_recipients.get('cc', [])) if admin_recipients.get('cc') else None
    
    return send_subscription_email(
        env,
        'validator_staking',
        record=node,
        model='subscription.node',
        context=context,
        email_to=admin_emails or env.company.email or 'support@zeeve.io',
        email_cc=admin_cc,
        force_send=True,
    )
