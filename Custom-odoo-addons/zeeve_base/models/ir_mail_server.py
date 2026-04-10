from odoo import models
import logging

_logger = logging.getLogger(__name__)


class IrMailServer(models.Model):
    _inherit = 'ir.mail_server'
    
    def send_email(self, message, mail_server_id=None, smtp_server=None, smtp_port=None,
                   smtp_user=None, smtp_password=None, smtp_encryption=None, smtp_debug=False,
                   smtp_session=None):
        """Final check: block at SMTP level if contains Odoo system email markers"""
        
        try:
            # Get email headers and body
            subject = message.get('Subject', '').lower() if message.get('Subject') else ''
            body = ''
            
            # Try to extract body from message
            for part in message.walk():
                if part.get_content_type() == 'text/plain':
                    body = part.get_payload(decode=True).decode('utf-8', errors='ignore').lower()
                    break
                elif part.get_content_type() == 'text/html':
                    body = part.get_payload(decode=True).decode('utf-8', errors='ignore').lower()
                    break
            
            # Only block if 'odoo' is in subject or actual email body, not in technical headers
            should_block = ('odoo' in subject) or ('odoo' in body and any(keyword in body for keyword in [
                'invitation to portal',
                'access portal',
                'powered by odoo',
            ]))
            
            if should_block:
                _logger.warning(f"🚫 BLOCKED AT SMTP LEVEL - Subject: '{message.get('Subject', 'No subject')}' from '{message.get('From')}' contains Odoo system email")
                # Return success but don't actually send
                return message['Message-Id']
            
            # Log custom emails that pass through
            if message.get('Subject'):
                _logger.info(f"✅ ALLOWED EMAIL - Subject: '{message.get('Subject')}' from '{message.get('From')}'")
            
            return super(IrMailServer, self).send_email(
                message, mail_server_id, smtp_server, smtp_port,
                smtp_user, smtp_password, smtp_encryption, smtp_debug, smtp_session
            )
        except Exception as e:
            _logger.error(f"Error in send_email: {e}")
            # On error, allow the email to go through to avoid breaking email flow
            return super(IrMailServer, self).send_email(
                message, mail_server_id, smtp_server, smtp_port,
                smtp_user, smtp_password, smtp_encryption, smtp_debug, smtp_session
            )
