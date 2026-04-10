from odoo import models, api
import logging

_logger = logging.getLogger(__name__)


class MailMail(models.Model):
    _inherit = 'mail.mail'
    
    # Odoo's default template XML IDs to block
    BLOCKED_TEMPLATE_XMLIDS = [
        'portal.mail_template_data_portal_welcome',
        'portal.portal_share_template',
        'auth_signup.mail_template_user_signup_account_created',
        'auth_signup.set_password_email',
        'base.mail_template_data_notification_email_default',
    ]
    
    def _is_blocked_template(self):
        """Check if email is from a blocked Odoo template (Odoo 18 compatible)"""
        try:
            # In Odoo 18, the field is 'template_id'
            if not self.template_id:
                return False
            
            template = self.template_id
            # Get template's XML ID
            template_xmlid = self.env['ir.model.data'].sudo().search([
                ('model', '=', 'mail.template'),
                ('res_id', '=', template.id)
            ], limit=1)
            
            if template_xmlid:
                full_xmlid = f"{template_xmlid.module}.{template_xmlid.name}"
                return full_xmlid in self.BLOCKED_TEMPLATE_XMLIDS
        except Exception as e:
            _logger.debug(f"Error checking blocked template: {e}")
        
        return False
    
    @api.model_create_multi
    def create(self, vals_list):
        """Block only Odoo's default system emails, allow custom emails"""
        filtered_vals = []
        
        for vals in vals_list:
            should_block = False
            
            # Block specific Odoo system emails (by checking template)
            # In Odoo 18, template reference might be 'template_id' or 'mail_template_id'
            template_id = vals.get('template_id') or vals.get('mail_template_id')
            if template_id:
                try:
                    template = self.env['mail.template'].sudo().browse(template_id)
                    if template.exists():
                        # Check if it's a blocked Odoo template
                        template_xmlid = self.env['ir.model.data'].sudo().search([
                            ('model', '=', 'mail.template'),
                            ('res_id', '=', template.id)
                        ], limit=1)
                        
                        if template_xmlid:
                            full_xmlid = f"{template_xmlid.module}.{template_xmlid.name}"
                            if full_xmlid in self.BLOCKED_TEMPLATE_XMLIDS:
                                _logger.info(f"🚫 Blocked Odoo default template: {full_xmlid}")
                                should_block = True
                except Exception as e:
                    _logger.debug(f"Error checking template: {e}")
            
            # Block portal invitation and system emails by subject
            subject = (vals.get('subject', '') or '').lower()
            if any(phrase in subject for phrase in [
                'invitation to portal',
                'set your password',
                'access portal',
            ]):
                _logger.info(f"🚫 Blocked Odoo system email: {vals.get('subject', 'No subject')}")
                should_block = True
            
            if not should_block:
                filtered_vals.append(vals)
        
        if not filtered_vals:
            _logger.info("All emails in batch were blocked")
            return self.env['mail.mail']
        
        return super(MailMail, self).create(filtered_vals)
    
    def send(self, auto_commit=False, raise_exception=False, post_send_callback=None):
        """Block Odoo system emails at send time"""
        try:
            emails_to_send = self.env['mail.mail']
            
            for mail in self:
                should_block = False
                
                # Check if from blocked template
                try:
                    if mail._is_blocked_template():
                        should_block = True
                except Exception as e:
                    _logger.debug(f"Error in _is_blocked_template: {e}")
                
                # Check subject for system email patterns
                subject = (mail.subject or '').lower()
                if any(phrase in subject for phrase in [
                    'invitation to portal',
                    'set your password',
                    'access portal',
                ]):
                    should_block = True
                
                if should_block:
                    _logger.info(f"🚫 Blocked email at send: {mail.subject}")
                    mail.sudo().write({'state': 'cancel', 'failure_reason': 'Blocked: Odoo system email'})
                else:
                    emails_to_send |= mail
            
            if emails_to_send:
                return super(MailMail, emails_to_send).send(
                    auto_commit=auto_commit,
                    raise_exception=raise_exception,
                    post_send_callback=post_send_callback
                )
            return True
        except Exception as e:
            _logger.error(f"Error in mail send: {e}")
            # Don't block the entire send, just log the error
            return super(MailMail, self).send(
                auto_commit=auto_commit,
                raise_exception=raise_exception,
                post_send_callback=post_send_callback
            )
