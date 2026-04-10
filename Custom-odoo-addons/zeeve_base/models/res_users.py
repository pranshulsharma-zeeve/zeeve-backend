from odoo import models, api
import logging

_logger = logging.getLogger(__name__)


class ResUsers(models.Model):
    _inherit = 'res.users'
    
    @api.model_create_multi
    def create(self, vals_list):
        """Block ALL emails on user creation"""
        self = self.with_context(
            mail_create_nolog=True,
            mail_notrack=True,
            no_reset_password=True,
            tracking_disable=True,
            mail_create_nosubscribe=True,
            no_mail=True,
            install_mode=True  # Prevents signup emails
        )
        return super(ResUsers, self).create(vals_list)
    
    def write(self, vals):
        """Block ALL emails on user updates"""
        self = self.with_context(
            mail_create_nolog=True,
            mail_notrack=True,
            tracking_disable=True,
            no_mail=True
        )
        return super(ResUsers, self).write(vals)
    
    def action_reset_password(self):
        """Block password reset email action"""
        _logger.info(f"🚫 Password reset action blocked for user: {self.name}")
        return {'type': 'ir.actions.act_window_close'}
    
    def action_send_reset_password_email(self):
        """Block send reset password email action"""
        _logger.info(f"🚫 Send reset password email action blocked for user: {self.name}")
        return {'type': 'ir.actions.act_window_close'}
    
    def _send_email(self):
        """Block all user emails"""
        return True
    
    @api.model
    def signup(self, values, token=None):
        """Block signup emails"""
        self = self.with_context(
            mail_create_nolog=True,
            no_reset_password=True,
            tracking_disable=True,
            no_mail=True,
            install_mode=True
        )
        return super(ResUsers, self).signup(values, token)
