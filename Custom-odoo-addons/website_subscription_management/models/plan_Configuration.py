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


class Configuration(models.Model):
    _inherit='subscription.plan'

    notification_days=fields.Integer(string='Pre Notification Days',help='The Number of days before user got the alert mail about plan state' )
    renew_days=fields.Integer(string='Renew button days',help='The number of days before renew is apper in the view')
