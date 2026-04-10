# -*- coding: utf-8 -*-
"""Helpers for importing rollup service data from external sources."""
import logging
from odoo import _, fields
from odoo.exceptions import UserError

_logger = logging.getLogger(__name__)

class RollupImportUtils:
    """Utility helpers to map a rollup service row into Odoo."""

    @staticmethod
    def handle_rollup_row(env, row):
        cleaned = RollupImportUtils._normalize_row(row)
        rollup_code = cleaned.get("SUBSCRIPTION_ID") or cleaned.get("Subscription#")
        if not rollup_code:
            raise UserError(_("Missing Rollup Subscription ID/Number."))

        # Find or create partner
        partner = env['res.partner'].sudo().search([
            ('email', '=', cleaned.get('Email'))
        ], limit=1)
        if not partner:
            partner = env['res.partner'].sudo().create({
                'name': cleaned.get('Customer Name'),
                'email': cleaned.get('Email'),
            })
            # Optionally create a user for this partner
            env['res.users'].sudo().create({
                'name': cleaned.get('Customer Name'),
                'login': cleaned.get('Email'),
                'email': cleaned.get('Email'),
                'partner_id': partner.id,
            })

        # Map plan_name to type_id (slug match)
        plan_name = cleaned.get('Plan Name') or ''
        slug_name = plan_name.strip().lower().replace(' ', '-').replace('_', '-')
        type_id = env['rollup.type'].sudo().search([
            ('name', '=', slug_name)
        ], limit=1)
        if not type_id:
            raise UserError(_("No rollup type found for plan name: %s (slug: %s)") % (plan_name, slug_name))

        # Only assign fields that exist in rollup.service
        rollup_vals = {
            'service_id': cleaned.get('reference_id'),
            'name': cleaned.get('Subscription#'),
            'customer_id': partner.id,
            'type_id': type_id.id,
            'status': cleaned.get('Status') or 'cancelled',
            'subscription_status': cleaned.get('Status') or 'cancelled',
            'autopay_enabled': False,
            'zoho_service_id': cleaned.get('SUBSCRIPTION_ID'),
            'next_billing_date': RollupImportUtils._to_date(cleaned.get('Next Billing On')),
            'original_amount': RollupImportUtils._to_float(str(cleaned.get('Amount')).replace('$','')),
            'inputs_json':{"Created On": cleaned.get('Created On'),"Activated On":cleaned.get('Activated On'),"Cancelled Date":cleaned.get('Cancelled Date'),"Last Billed On":cleaned.get('Last Billed On')},
            'metadata_json':cleaned.get('Metadata'),
        }
        # Remove empty fields
        rollup_vals = {k: v for k, v in rollup_vals.items() if v not in (None, '', [])}
        rollup_service = env['rollup.service'].sudo().create(rollup_vals)
        return rollup_service

    @staticmethod
    def _normalize_row(row):
        cleaned = {}
        for key, value in (row or {}).items():
            cleaned[key] = value.strip() if isinstance(value, str) else value
        return cleaned

    @staticmethod
    def _to_float(value, default=0.0):
        if value in (None, '', False):
            return default
        try:
            return float(value)
        except (TypeError, ValueError):
            return default

    @staticmethod
    def _to_date(value):
        if not value:
            return False
        try:
            return fields.Date.to_date(value)
        except Exception:
            return False
