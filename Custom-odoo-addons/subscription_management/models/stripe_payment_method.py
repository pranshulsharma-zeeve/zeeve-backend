from odoo import api, fields, models, _
from ..utils import mnemonic_service

class StripePaymentMethod(models.Model):
    _name = "stripe.payment.method"
    _description = "Banking Security: Stripe Payment Method Vault"
    _order = "is_default desc, id desc"

    partner_id = fields.Many2one('res.partner', string="Customer", required=True, ondelete='cascade')
    stripe_payment_method_id = fields.Char(string="Stripe Payment Method ID", required=True, index=True)
    last4 = fields.Char(string="Last 4 Digits")
    brand = fields.Char(string="Brand")
    exp_month = fields.Integer(string="Expiration Month")
    exp_year = fields.Integer(string="Expiration Year")
    is_default = fields.Boolean(string="Default Method", default=False)
    active = fields.Boolean(string="Active", default=True)

    def name_get(self):
        result = []
        for rec in self:
            name = f"{rec.brand.capitalize()} **** {rec.last4}" if rec.brand and rec.last4 else self._decrypt_id(rec.stripe_payment_method_id)
            result.append((rec.id, name))
        return result

    def _get_key(self):
        return mnemonic_service.get_aes_key(self.env)

    def _encrypt_id(self, pm_id):
        key = self._get_key()
        if not key:
            return pm_id
        return mnemonic_service.encrypt_aes(pm_id, key)

    def _decrypt_id(self, encrypted_id):
        key = self._get_key()
        if not key:
            return encrypted_id
        return mnemonic_service.decrypt_aes(encrypted_id, key)

    @api.model
    def create_or_update_from_stripe(self, partner, pm_data):
        """
        Creates or updates a payment method vault record from Stripe data.
        :param partner: res.partner record
        :param pm_data: dict from Stripe PaymentMethod object
        """
        card = pm_data.get('card', {})
        pm_id_raw = pm_data['id']
        
        # 1. Encrypt ID for storage
        pm_id_enc = self._encrypt_id(pm_id_raw)

        vals = {
            'partner_id': partner.id,
            'stripe_payment_method_id': pm_id_enc,
            'last4': card.get('last4'),
            'brand': card.get('brand'),
            'exp_month': card.get('exp_month'),
            'exp_year': card.get('exp_year'),
            'active': True,
        }

        # 2. Search for existing record (needs decryption filter)
        existing = None
        candidates = self.search([
            ('partner_id', '=', partner.id)
        ])
        for cand in candidates:
            if self._decrypt_id(cand.stripe_payment_method_id) == pm_id_raw:
                existing = cand
                break
        
        if existing:
            existing.write(vals)
            return existing
        else:
            # If no default set yet, make this one default
            if not self.search_count([('partner_id', '=', partner.id), ('is_default', '=', True)]):
                vals['is_default'] = True
            return self.create(vals)
