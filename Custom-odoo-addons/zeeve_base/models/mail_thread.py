from odoo import models, api
import logging

_logger = logging.getLogger(__name__)


class MailThread(models.AbstractModel):
    _inherit = 'mail.thread'
    
    @api.returns('mail.message', lambda value: value.id)
    def message_post(self, **kwargs):
        """Block messages that would send emails with 'odoo'"""
        
        body = (kwargs.get('body', '') or '').lower()
        subject = (kwargs.get('subject', '') or '').lower()
        
        # If contains 'odoo', disable email sending
        if 'odoo' in body or 'odoo' in subject:
            _logger.info(f"🚫 Blocked message_post with 'odoo'")
            kwargs['mail_auto_delete'] = True
            kwargs['email_from'] = False
            kwargs['subtype_xmlid'] = 'mail.mt_note'  # Internal note, no email
        
        return super(MailThread, self).message_post(**kwargs)
    
    def _notify_thread(self, message, msg_vals=False, **kwargs):
        """Block notifications containing 'odoo'"""
        
        body = (message.body or '').lower()
        subject = (message.subject or '').lower()
        
        if 'odoo' in body or 'odoo' in subject:
            _logger.info(f"🚫 Blocked notification with 'odoo'")
            return True
        
        return super(MailThread, self)._notify_thread(message, msg_vals, **kwargs)
