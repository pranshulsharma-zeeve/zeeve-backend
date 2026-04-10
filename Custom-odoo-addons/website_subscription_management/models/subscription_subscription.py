# -*- coding: utf-8 -*-
#################################################################################
#
#   Copyright (c) 2016-Present Webkul Software Pvt. Ltd. (<https://webkul.com/>)
#    See LICENSE file for full copyright and licensing details.
#################################################################################


from odoo import models,api,fields
import logging
_logger=logging.getLogger(__name__)
from datetime import date, datetime, timedelta
from dateutil.relativedelta import relativedelta
from odoo import exceptions


class subscription_subscription(models.Model):
    _inherit='subscription.subscription'

    source=fields.Selection(selection_add=[('website','Website')])
    date=datetime.today()

    date_table=None

    def get_expiry_date(self):
        Notification_day = self.sub_plan_id.notification_days #No. of days before you get notification
        if self.end_date:
            date_list = list(map(int,self.end_date.strftime("%Y-%m-%d").split("-")))
            date_N_days_ago = datetime(date_list[0],date_list[1],date_list[2]) - timedelta(days=Notification_day)
            date_table = date_N_days_ago.strftime ('%Y-%m-%d')

            condition = date_table<=datetime.now().strftime('%Y-%m-%d')
            return condition
    
class SaleOrder(models.Model):
    _inherit='sale.order'

    def _cart_update(self, product_id=None, line_id=None, add_qty=0, set_qty=0, **kwargs):
        res = super(SaleOrder, self)._cart_update(product_id=product_id,
                                                  line_id=line_id, add_qty=add_qty, set_qty=set_qty, kwargs=kwargs)
        for product in self.order_line:
            override = product.product_id.subscription_plan_id.override_product_price
            if line_id is not False and override:
                order = self.sudo().browse(self.id)
                order_line = self._cart_find_product_line(
                    product_id, line_id, **kwargs)[:1]
                product = self.env['product.product'].browse(int(product_id))
                if product.activate_subscription:
                    values = {
                        'price_unit':  product.product_tmpl_id.currency_id._convert(
                            product.subscription_plan_id.plan_amount +
                            product.price_extra, order.pricelist_id.currency_id, self.env.company,
                            fields.Date.today()
                        ),
                    }
                    order_line.write(values)
        return res

