# -*- coding: utf-8 -*-
"""Extend account.move to link invoices with subscriptions."""
from odoo import fields, models, api, SUPERUSER_ID
from odoo.exceptions import AccessError
import base64
import logging

_logger = logging.getLogger(__name__)
class AccountMove(models.Model):
    _inherit = 'account.move'
    is_subscription = fields.Boolean(string="Is Subscription", copy=False)
    subscription_id = fields.Many2one('subscription.subscription', string='Subscription Id')
    node_id = fields.Many2one('subscription.node', string='Subscription Node')

    def action_send_email_invoice(self):
        """Send invoice email with PDF attachment for account.move"""
        template = self.env.ref('subscription_management.email_template_custom_invoice', raise_if_not_found=False)
        report = self.env.ref('subscription_management.action_report_custom_invoice_account', raise_if_not_found=False)
        for inv in self:
            attachments = []
            attachment_url = False

            if report:
                # reports = self.env['ir.actions.report']
                pdf_content, _ = report._render_qweb_pdf('subscription_management.action_report_custom_invoice_account',inv.id)
                attachment = self.env['ir.attachment'].sudo().create({
                    'name': f"Invoice_{inv.name}.pdf",
                    'type': 'binary',
                    'datas': base64.b64encode(pdf_content),
                    'res_model': 'account.move',
                    'res_id': inv.id,
                    'mimetype': 'application/pdf'
                })
                attachments.append((4, attachment.id))
                backend_url = (self.env['ir.config_parameter'].sudo().get_param('backend_url') or '').rstrip('/')
                download_path = f"/api/download_invoice/{attachment.id}"
                attachment_url = f"{backend_url}{download_path}" if backend_url else download_path

            if template:
                email_from= (self.env['res.company'].sudo().search([], limit=1).email_formatted or self.env['res.company'].sudo().search([], limit=1).email) or 'support@zeeve.io'
                template.sudo().with_context(attachment_url=attachment_url).send_mail(
                    inv.id,
                    email_values={'attachment_ids': attachments,'email_to': inv.partner_id.email,'email_from':email_from},
                    force_send=True
                )

    def unlink(self):
        if self.env.su or self.env.context.get("allow_invoice_unlink"):
            return super().unlink()
        # Allowed groups
        allowed_groups = [
            'access_rights.group_admin',
            'access_rights.group_technical_manager'
        ]

        user = self.env.user

        # Allow unlink for superuser/automated flows
        if user.id != SUPERUSER_ID:
            if not user.has_group(allowed_groups[0]) and not user.has_group(allowed_groups[1]):
                raise AccessError("You are not allowed to delete invoices.")

        return super(AccountMove, self).unlink()
class AccountMoveLine(models.Model):
    _inherit = 'account.move.line'

    discount_code = fields.Char(string='Discount Code')
    discount_id = fields.Many2one('subscription.discount')
    node_id = fields.Many2one('subscription.node', string='Subscription Node')


    def unlink(self):
        if not self:
            return super().unlink()
        if self.env.su or self.env.context.get("allow_invoice_unlink"):
            return super().unlink()
        allowed_groups = [
            'access_rights.group_admin',
            'access_rights.group_technical_manager'
        ]

        user = self.env.user

        if user.id != SUPERUSER_ID:
            if not user.has_group(allowed_groups[0]) and not user.has_group(allowed_groups[1]):
                raise AccessError("You are not allowed to delete invoices.")

        return super(AccountMoveLine, self).unlink()
