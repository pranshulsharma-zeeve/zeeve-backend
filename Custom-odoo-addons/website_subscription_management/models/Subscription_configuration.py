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


class SubscriptionConfiguration(models.Model):
    _name='subscription.configuration'


    msg_for_draft=fields.Char(string='Message for Draft State',translate=True, required=True,default='Your Current Subscription is to be under progress.It will active soon.')
    msg_for_active=fields.Char(string='Message for Active State',translate=True,required=True,default='Your plan is active.Enjoy the service.')
    msg_for_cancel=fields.Char(string='Message for Cancel State',translate=True, required=True,default='Your plan is cancelled.You cannot access it anymore.')
    msg_for_expired=fields.Char(string='Message for Expired State',translate=True, required=True,default='This plan is expired.')
    msg_for_renewed=fields.Char(string='Message for renewed State',translate=True, required=True,default='This plan is renewed.')



    @api.model
    def create(self, vals):
        records = self.sudo().search([], limit=1)
        if records:
            raise exceptions.ValidationError('You cannot create more than one record')

        return super(SubscriptionConfiguration, self).create(vals)