# -*- encoding: utf-8 -*-
import logging

from odoo import fields, models
from ..utils import base_utils

_logger = logging.getLogger(__name__)


class ContactUs(models.Model):
    _name = 'contact.us'
    _inherit = ['mail.thread', 'mail.activity.mixin']
    _description = 'Contact Us'

    name = fields.Char(string='Name',required=True)
    email = fields.Char(string='Email',required=True)
    comment = fields.Text(string='Comment')
    country_id = fields.Many2one('res.country')
    type = fields.Selection([('besu', 'HyperLedger Besu'),
                             ('fabric', 'HyperLedger Fabric'),
                             ("other", "Contact us")],
                            string=" Type")
    company_name = fields.Char(string='Company Name')


    def send_contact_us_email(self):
        try:
            """Send contact-us notifications to requester and configured admins."""
            template_user = self.env.ref('zeeve_base.email_template_contact_us', raise_if_not_found=False)
            template_admin = self.env.ref('zeeve_base.email_template_contact_us_admin', raise_if_not_found=False)

            admin_emails, config = base_utils._get_admin_email_list(self.env)
            ctx = {
                'config': config,
                'admin_emails': ','.join(admin_emails),
            }

            for record in self.sudo():
                if template_user and record.email:
                    try:
                        template_user.with_context(**ctx).send_mail(
                            record.id,
                            email_values={'email_to': record.email},
                            force_send=True,
                        )
                    except Exception:
                        _logger.exception("Failed to send contact-us email to %s", record.email)
                elif not record.email:
                    _logger.warning("Contact-us record %s missing requester email; skipping user notification", record.id)

                if template_admin and admin_emails:
                    try:
                        template_admin.with_context(**ctx).send_mail(
                            record.id,
                            email_values={'email_to': ','.join(admin_emails)},
                            force_send=True,
                        )
                    except Exception:
                        _logger.exception("Failed to notify admins for contact-us record %s", record.id)
                elif not admin_emails:
                    _logger.info("No admin emails configured; skipping admin notification for contact-us record %s", record.id)

        except Exception:
            _logger.exception("Failed to send contact-us email")
