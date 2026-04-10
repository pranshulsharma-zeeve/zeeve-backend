# -*- coding: utf-8 -*-
#################################################################################
#
#   Copyright (c) 2016-Present Webkul Software Pvt. Ltd. (<https://webkul.com/>)
#    See LICENSE file for full copyright and licensing details.
#################################################################################

from odoo.http import  request
import logging

_logger=logging.getLogger(__name__)
from odoo import http
from odoo import fields
from odoo.addons.website.controllers.main import Website
from odoo.addons.website_sale.controllers.main import WebsiteSale
from datetime import datetime, timedelta
from odoo.addons.portal.controllers import portal


class CustomerPortal(portal.CustomerPortal):

    def _prepare_home_portal_values(self, counters):
        values = super()._prepare_home_portal_values(counters)
        website = request.env['website'].get_current_website()
        if 'subscription_count' in counters:
            values['subscription_count'] = website.get_subscription_count()
        return values


class Website(WebsiteSale):

    record_id=None
    @http.route()
    def shop_payment(self, **post):
        order = request.website.sale_get_order()
        for product in order.order_line:
            override = product.product_id.subscription_plan_id.override_product_price
            if override:
                if product.product_id.activate_subscription:
                    values = {
                        'price_unit':  product.product_id.product_tmpl_id.currency_id._convert(
                            product.product_id.subscription_plan_id.plan_amount +
                            product.product_id.price_extra, order.pricelist_id.currency_id, request.env.company,
                            fields.Date.today()
                        ),
                    }
                    product.write(values)
        res = super(Website,self).shop_payment(**post)

        return res

    @http.route('/my/subscriptions', auth='user',website=True,type='http')
    def subscr_table(self, **kw):

        user_id = request.env['res.users'].sudo().browse(request.uid)
        subscriptions=request.env['subscription.subscription'].sudo().search([('customer_name','=',user_id.partner_id.id)])
        return request.render('website_subscription_management.portal_my_subscription',{'subscriptions':subscriptions})


    @http.route('/my/subscriptions/<model("subscription.subscription"):myid>/', auth='user',website=True,type='http')
    def subscr_detail_refresh(self, myid,**kw):
        if myid:
            done=myid.renew_subscription()
            return request.redirect('/my/subscriptions')

    @http.route('/my/subscriptions/<model("subscription.subscription"):myid>/', auth='user',website=True,type='http')
    def subscr_detail(self,myid,**kw):
        if myid:
           date=None
           select=dict(myid._fields['source'].selection).get(myid.source)
           N = myid.sub_plan_id.notification_days #No. of days before you get notification
           if myid.end_date:
            date_list=list(map(int,myid.end_date.strftime("%Y-%m-%d").split("-")))
            date_N_days_ago = datetime(date_list[0],date_list[1],date_list[2]) - timedelta(days=N)
            date=date_N_days_ago.strftime('%Y-%m-%d')
            condition=date==datetime.now().strftime('%Y-%m-%d')

            if date==datetime.now().strftime('%Y-%m-%d'):
                request.env['website'].sudo().notification_mail_send()
           else:
             condition=False
           reason_record=request.env['subscription.reasons'].sudo().search([])
           subscription_message=request.env['subscription.configuration'].sudo().search([])
        return request.render('website_subscription_management.my_subscription_detail',{'detail':myid,'select':select,'condition':condition,'reasons':reason_record,'message':subscription_message})


    @http.route('/website/json/controller',type='json',website=True,auth='user')
    def json_controller(self,renew,**kw):
        if renew:
            done=request.env['subscription.subscription'].sudo().browse([int(renew)]).renew_subscription()
            return True
        else:
            return False




    @http.route('/example/reason',methods=['POST'],type='http',auth='user',website=True,csrf=False)
    def render_detail(self,**kw):
        subscription_obj=request.env['subscription.subscription'].sudo().browse(int(kw['sub_plan_record_id']))

        subscription_obj.reason = kw['reason_id'] + kw['message'] if kw['message'] else ""
        if subscription_obj.state in ['in_progress']:
            subscription_obj.state='close'
            return request.redirect('/my/subscriptions')

        elif subscription_obj.state in ['draft']:
            if subscription_obj.get_cancel_sub():
                return request.redirect('/my/subscriptions')


    # @http.route()
    # def cart_update(self, product_id, add_qty=1, set_qty=0, **kw):
    #     if request.env['product.product'].sudo().browse([int(product_id)]).activate_subscription:
    #         add_qty=0
    #         set_qty=1
    #     values=super(Website,self).cart_update(product_id, add_qty=add_qty, set_qty=set_qty, **kw)
    #     return values

    @http.route()
    def address(self, **kw):
        order = request.website.sale_get_order()
        if bool(order.order_line.mapped('product_id').filtered(lambda p: p.activate_subscription == True)) and request.env.user.id == request.website.user_id.id:
            return request.redirect('/web/login?redirect=/shop/cart')

        values=super(Website,self).address(**kw)
        return values


    @http.route('/check/product_variant/subscription',type='json',website=True,auth='public')
    def json_count_div(self,product_id,**kw):
        if request.env['product.product'].sudo().browse([int(product_id)]).activate_subscription:
            return True
        else:
            return False