# -*- coding: utf-8 -*-
"""Stripe-aware partner helpers."""

import logging

from odoo import fields, models

_logger = logging.getLogger(__name__)


class ResPartner(models.Model):
    _inherit = 'res.partner'

    stripe_customer_id = fields.Char(string='Stripe Customer ID', index=True, copy=False)

    def _prepare_stripe_billing_address_vals(self, stripe_address):
        """Map a Stripe-style address payload to ``res.partner`` fields."""

        self.ensure_one()
        stripe_address = stripe_address or {}
        if not isinstance(stripe_address, dict):
            return {}

        vals = {}
        line1 = (stripe_address.get('line1') or '').strip()
        line2 = (stripe_address.get('line2') or '').strip()
        city = (stripe_address.get('city') or '').strip()
        postal_code = (stripe_address.get('postal_code') or '').strip()
        state_value = (stripe_address.get('state') or '').strip()
        country_value = (stripe_address.get('country') or '').strip()

        if line1:
            vals['street'] = line1
        if line2:
            vals['street2'] = line2
        if city:
            vals['city'] = city
        if postal_code:
            vals['zip'] = postal_code

        country = False
        if country_value:
            country = self.env['res.country'].sudo().search([
                '|',
                ('code', '=', country_value.upper()),
                ('name', 'ilike', country_value),
            ], limit=1)
            if country:
                vals['country_id'] = country.id

        if state_value:
            state_domain = [
                '|',
                ('code', '=', state_value.upper()),
                ('name', 'ilike', state_value),
            ]
            if country:
                state_domain = [('country_id', '=', country.id)] + state_domain
            state = self.env['res.country.state'].sudo().search(state_domain, limit=1)
            if state:
                vals['state_id'] = state.id

        return vals

    def sync_stripe_customer_profile(
        self,
        customer_id=None,
        customer_name=None,
        customer_email=None,
        customer_phone=None,
        address=None,
    ):
        """Persist Stripe billing details on the partner."""

        self.ensure_one()
        vals = {}

        if customer_id and self.stripe_customer_id != customer_id:
            vals['stripe_customer_id'] = customer_id
        if customer_name and customer_name.strip() and self.name != customer_name.strip():
            vals['name'] = customer_name.strip()
        if customer_email and customer_email.strip() and self.email != customer_email.strip():
            vals['email'] = customer_email.strip()
        if customer_phone and customer_phone.strip() and self.phone != customer_phone.strip():
            vals['phone'] = customer_phone.strip()

        vals.update(self._prepare_stripe_billing_address_vals(address))

        if not vals:
            return False

        try:
            self.sudo().write(vals)
        except Exception as exc:  # pylint: disable=broad-except
            _logger.exception(
                "Failed syncing Stripe billing profile to partner %s: %s",
                self.id,
                exc,
            )
            return False
        return True
