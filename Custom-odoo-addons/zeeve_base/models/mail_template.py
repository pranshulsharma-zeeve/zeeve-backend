from odoo import models, api
import logging

_logger = logging.getLogger(__name__)


class MailTemplate(models.Model):
    _inherit = 'mail.template'
    
    # Odoo's default template XML IDs to block
    BLOCKED_TEMPLATE_XMLIDS = [
        'portal.mail_template_data_portal_welcome',
        'portal.portal_share_template',
        'auth_signup.mail_template_user_signup_account_created',
        'auth_signup.set_password_email',
        'base.mail_template_data_notification_email_default',
    ]
    
    def _is_blocked_template(self):
        """Check if this is an Odoo default template that should be blocked"""
        try:
            # Get template's XML ID
            template_xmlid = self.env['ir.model.data'].sudo().search([
                ('model', '=', 'mail.template'),
                ('res_id', '=', self.id)
            ], limit=1)
            
            if template_xmlid:
                full_xmlid = f"{template_xmlid.module}.{template_xmlid.name}"
                return full_xmlid in self.BLOCKED_TEMPLATE_XMLIDS
        except Exception as e:
            _logger.debug(f"Error checking blocked template: {e}")
        
        return False
    
    def send_mail(self, res_id, force_send=False, raise_exception=False, email_values=None, notif_layout=None):
        """Block only Odoo's default system templates, allow custom templates"""
        
        # Only block if it's an Odoo default template
        if self._is_blocked_template():
            _logger.info(f"🚫 Blocked Odoo default template: {self.name}")
            return False
        
        # Allow all custom templates - call parent with only supported parameters
        try:
            return super(MailTemplate, self).send_mail(
                res_id,
                force_send=force_send,
                raise_exception=raise_exception,
                email_values=email_values
            )
        except TypeError:
            # If that fails, try without email_values
            try:
                return super(MailTemplate, self).send_mail(
                    res_id,
                    force_send=force_send,
                    raise_exception=raise_exception
                )
            except Exception as e:
                _logger.error(f"Error sending mail: {e}")
                raise
