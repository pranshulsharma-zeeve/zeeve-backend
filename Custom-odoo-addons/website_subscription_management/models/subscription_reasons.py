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


class SubscriptionReason(models.Model):
    _inherit='subscription.reasons'

    
