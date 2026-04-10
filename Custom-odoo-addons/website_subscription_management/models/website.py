# -*- coding: utf-8 -*-
#################################################################################
#
#   Copyright (c) 2016-Present Webkul Software Pvt. Ltd. (<https://webkul.com/>)
#    See LICENSE file for full copyright and licensing details.
#################################################################################


from odoo import models,api,fields
import logging
_logger=logging.getLogger(__name__)
from datetime import datetime, timedelta
from odoo import exceptions

class Website(models.Model):
    _inherit='website'

    def get_subscription_count(self):
        if self._uid:
            user_id = self.env['res.users'].sudo().browse(self._uid)
            count=self.env['subscription.subscription'].sudo().search([('customer_name','=',user_id.partner_id.id)])
            return len(count)
        return 0



    def notification_mail_send(self):
        user_id = self.env['res.users'].sudo().browse(self._uid)
        mail_id=self.env['res.partner'].sudo().search([('id','=',user_id.partner_id.id)])
        template_id=self.env.ref('website_subscription_management.email_template_mail').sudo()
        template_id.email_to=mail_id
        template_id.send_mail(self.id,force_send=True)
        return True
