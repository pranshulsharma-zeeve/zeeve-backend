from odoo import models, api


class ResPartner(models.Model):
    _inherit = 'res.partner'
    
    @api.model_create_multi
    def create(self, vals_list):
        """Block all emails on partner creation"""
        self = self.with_context(
            mail_create_nolog=True,
            mail_notrack=True,
            tracking_disable=True,
            mail_create_nosubscribe=True,
            no_mail=True
        )
        return super(ResPartner, self).create(vals_list)
    
    def write(self, vals):
        """Block all emails on partner updates"""
        self = self.with_context(
            mail_create_nolog=True,
            mail_notrack=True,
            tracking_disable=True,
            no_mail=True
        )
        return super(ResPartner, self).write(vals)
