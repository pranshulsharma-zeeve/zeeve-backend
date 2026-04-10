# -*- coding: utf-8 -*-
##########################################################################
# Author      : Webkul Software Pvt. Ltd. (<https://webkul.com/>)
# Copyright(c): 2017-Present Webkul Software Pvt. Ltd.
# All Rights Reserved.
#
#
#
# This program is copyright property of the author mentioned above.
# You can`t redistribute it and/or modify it.
#
#
# You should have received a copy of the License along with this program.
# If not, see <https://store.webkul.com/license.html/>
##########################################################################

import logging
from dateutil.relativedelta import relativedelta
from re import template
from datetime import datetime, timedelta
from odoo import _, api, fields, models
from odoo.exceptions import UserError, ValidationError

_logger = logging.getLogger(__name__)


class ProductProduct(models.Model):

    _inherit = "product.product"

    activate_subscription = fields.Boolean(
        string="Is Subscription Type", default=False)
    subscription_plan_id = fields.Many2one(
        comodel_name="subscription.plan", string="Subscription Plan")

    def write(self, vals):
        # subscription_plan_id = vals.get('subscription_plan_id',False)
        context = self._context
        return super(ProductProduct, self).write(vals)

    @api.onchange('subscription_plan_id')
    def on_change_subscription_plan(self):
        for current_rec in self:
            override = current_rec.subscription_plan_id.override_product_price
            if current_rec.subscription_plan_id:
                if override:
                    current_rec.lst_price = current_rec.subscription_plan_id.plan_amount
                current_rec.type = 'service'

    @api.onchange('activate_subscription')
    def on_change_activate_subscription(self):
        for current_rec in self:
            if current_rec.activate_subscription:
                current_rec.invoice_policy = 'delivery'
                current_rec.type = 'service'
            else:
                current_rec.invoice_policy = 'order'
                current_rec.subscription_plan_id = False


    @api.constrains('activate_subscription')
    def validate_product_type(self):
        for rec in self:
            if rec.activate_subscription:
                rec.type = 'service'

    @api.depends('list_price', 'price_extra')
    def _compute_product_lst_price(self):
        res = super(ProductProduct, self)._compute_product_lst_price()
        for product in self:
            override = product.subscription_plan_id.override_product_price
            if override and product.activate_subscription and product.subscription_plan_id:
                product.lst_price = product.subscription_plan_id.plan_amount+product.price_extra
        return res


class ProductTemplate(models.Model):

    _inherit = "product.template"

    activate_subscription = fields.Boolean(
        related='product_variant_ids.activate_subscription',
        string='Is Subscription Type', readonly=False)
    subscription_plan_id = fields.Many2one(
        comodel_name="subscription.plan",
        related="product_variant_ids.subscription_plan_id",
        string="Subscription Plan", readonly=False)

    @api.model
    def create(self, vals):
        if vals.get('activate_subscription'):
            vals['type'] = 'service'

        template_id = super(ProductTemplate, self).create(vals)

        related_vals = {}

        if vals.get('activate_subscription'):
            related_vals['activate_subscription'] = vals[
                'activate_subscription']

        if vals.get('subscription_plan_id'):
            related_vals['subscription_plan_id'] = vals['subscription_plan_id']
            subscription_plan_id = self.env['subscription.plan'].browse(
                vals['subscription_plan_id'])
            if subscription_plan_id.override_product_price:
                related_vals['list_price'] = subscription_plan_id.plan_amount
        if related_vals:
            template_id.write(related_vals)
        if template_id.activate_subscription:
            for product in template_id.product_variant_ids:
                product.write({'activate_subscription': True, 'subscription_plan_id':
                              template_id.subscription_plan_id.id if template_id.subscription_plan_id else False, 'type': 'service'})
        return template_id

    # @api.model
    def write(self, vals):
        if vals.get('activate_subscription'):
            vals['type'] = 'service'
        if vals.get('subscription_plan_id'):
            subscription_plan_id = self.env['subscription.plan'].browse(
                vals['subscription_plan_id'])
            if subscription_plan_id.override_product_price:
                vals['list_price'] = subscription_plan_id.plan_amount
        elif self.subscription_plan_id and self.subscription_plan_id.override_product_price:
            vals['list_price'] = self.subscription_plan_id.plan_amount

        res = super(ProductTemplate, self).write(vals)
        template_id = self

        # for rec in self:
        #     if self.subscription_plan_id  and vals.get('list_price'):
        #         rec.product_variant_ids.write({'subscription_plan_id': self.subscription_plan_id})
        #         rec.product_variant_ids.write({'list_price': vals['list_price']})
        return res

    @api.onchange('activate_subscription')
    def on_change_activate_subscription(self):
        for current_rec in self:
            if current_rec.activate_subscription:
                current_rec.invoice_policy = 'delivery'
                current_rec.type = 'service'
            else:
                current_rec.invoice_policy = 'order'

    @api.constrains('activate_subscription')
    def activate_subscription_check(self):
        if self.activate_subscription and self.type != 'service':
            raise UserError(_("Product type should be service for subscription product.."))
        for product in self.product_variant_ids:
            product.activate_subscription = self.activate_subscription

    @api.onchange('subscription_plan_id')
    def on_change_subscription_plan(self):
        for current_rec in self:
            if current_rec.subscription_plan_id.override_product_price:
                current_rec.list_price = current_rec.subscription_plan_id.plan_amount

    def _get_combination_info(self, combination=False, product_id=False, add_qty=1.0,parent_combination=False, only_template=False):
        res = super(ProductTemplate, self)._get_combination_info(
            combination=combination, product_id=product_id, add_qty=add_qty,
            parent_combination=parent_combination, only_template=only_template)
        pricelist = self.env.user.company_id.partner_id.property_product_pricelist
        if not product_id:
            product_id = res.get('product_template_id')
        if product_id:

            product = self.env['product.product'].sudo().browse(
                int(res['product_id']))
            quantity = self.env.context.get('quantity', add_qty)
            context = dict(self.env.context, quantity=quantity,
                           pricelist=pricelist.id if pricelist else False)
            if product.activate_subscription:

                override = product.subscription_plan_id.override_product_price
                if override:
                    product_template = self.with_context(context)
                    list_price = product_template.currency_id._convert(
                        product.subscription_plan_id.plan_amount +
                        product.price_extra, pricelist.currency_id, self.env.company,
                        fields.Date.today()
                    )
                    price = list_price
                    res.update({
                        'price': price,
                        'list_price': list_price,
                    })

        return res


# class SaleOrderLine(models.Model):
#     _inherit = 'sale.order.line'

#     def invoice_line_create(self, invoice_id, qty):
#         for current_rec in self:
#             if current_rec.product_id.activate_subscription:
#                 continue
#             else:
#                 return super(SaleOrderLine, current_rec).invoice_line_create(invoice_id, qty)

#     @api.onchange('product_id')
#     def _onchange_product_id(self):
#         res = {}
#         if self.product_id and self.product_id.activate_subscription and self.product_id.subscription_plan_id and not self.product_id.subscription_plan_id.active:
#             self.product_id = False
#             res['warning'] = {'title': _('Warning'), 'message': _('Plan inside selected product is inactive.')}
#         return res


# class SaleOrder(models.Model):
#     _inherit = 'sale.order'

#     def _get_subscription_count(self):
#         sub_obj = self.env['subscription.subscription']
#         for current_record in self:
#             current_record.subscription_count = sub_obj.search_count(
#                 [('so_origin', '=', current_record.id)])

#     def _get_invoiced(self):
#         res = super(SaleOrder, self)._get_invoiced()
#         for rec in self:
#             invoice_ids_sub = self.env['account.move'].search(
#                 [('ref', '=', rec.name)])
#             rec.invoice_ids = invoice_ids_sub.union(rec.invoice_ids)
#             rec.invoice_count = len(rec.invoice_ids)
#         return res

#     subscription_count = fields.Integer(
#         compute=_get_subscription_count, string="#Subscription")
#     subscription_ids = fields.One2many(
#         "subscription.subscription", 'so_origin', string="Subscription",
#         readonly=True, copy=False)

#     def action_view_subscription(self):
#         subscription_ids = self.env['subscription.subscription'].search(
#             [('so_origin', '=', self.id)])
#         action = self.env.ref(
#             'subscription_management.action_subscription').read()[0]
#         action['context'] = {}
#         if len(subscription_ids) > 1:
#             action['domain'] = "[('id','in',%s)]" % subscription_ids.ids
#         elif len(subscription_ids) == 1:
#             action['views'] = [(self.env.ref(
#                 'subscription_management.subscription_subscription_form_view').id, 'form')]
#             action['res_id'] = subscription_ids.ids[0]
#         else:
#             action = {'type': 'ir.actions.act_window_close'}
#         return action

#     def action_view_invoice(self, invoices=False):
#         invoice_ids_sub = self.env['account.move'].search(
#             [('ref', '=', self.name)])
#         self.invoice_ids = invoice_ids_sub.union(self.invoice_ids)
#         return super(SaleOrder, self).action_view_invoice(invoices=invoices)

#     def action_confirm(self):
#         subscription = self.env['subscription.subscription'].sudo()
#         trial_period_setting = self.env['res.config.settings'].sudo().get_values()[
#             'trial_period_setting']
#         for order in self:
#             res = super(SaleOrder, order).action_confirm()
#             if res:
#                 for line in order.order_line:
#                     if line.product_id:
#                         if line.product_id.activate_subscription:
#                             date = order.date_order
#                             if line.product_id.subscription_plan_id.trial_period:
#                                 if line.product_id.subscription_plan_id.trial_duration_unit == 'day':
#                                     date = date + \
#                                         relativedelta(
#                                             days=line.product_id.subscription_plan_id.trial_duration)
#                                 if line.product_id.subscription_plan_id.trial_duration_unit == 'month':
#                                     date = date + \
#                                         relativedelta(
#                                             months=line.product_id.subscription_plan_id.trial_duration)
#                                 if line.product_id.subscription_plan_id.trial_duration_unit == 'year':
#                                     date = date + \
#                                         relativedelta(
#                                             years=line.product_id.subscription_plan_id.trial_duration)
#                                 if line.product_id.subscription_plan_id.trial_duration_unit == 'week':
#                                     date = date + \
#                                         timedelta(
#                                             weeks=line.product_id.subscription_plan_id.trial_duration)
#                             vals = {
#                                 'customer_name': order.partner_id.id,
#                                 'customer_billing_address': order.partner_invoice_id.id,
#                                 'source': 'so',
#                                 'so_origin': order.id,
#                                 'product_id': line.product_id.id,
#                                 'quantity': line.product_uom_qty,
#                                 'start_date': date,
#                                 'tax_id': [(6, 0, line.tax_id.ids)],
#                                 'sub_plan_id': line.product_id.subscription_plan_id.id,
#                                 'unit': line.product_id.subscription_plan_id.unit,
#                                 'duration': line.product_id.subscription_plan_id.duration,
#                                 'price': line.price_unit,
#                                 'currency_id': order.pricelist_id.currency_id.id,
#                                 'trial_period': line.product_id.subscription_plan_id.trial_period,
#                                 'trial_duration_unit': line.product_id.subscription_plan_id.trial_duration_unit,
#                                 'trial_duration': line.product_id.subscription_plan_id.trial_duration,
#                                 'num_billing_cycle': line.product_id.subscription_plan_id.num_billing_cycle or -1,
#                                 'never_expires': line.product_id.subscription_plan_id.never_expires,
#                                 'create_uid': self._uid
#                                 }
#                             if (len(self.partner_id.all_subscription) != 0 and trial_period_setting == 'one_time') or \
#                             (trial_period_setting == 'product_based' and self.partner_id.all_subscription.filtered(lambda subscription: subscription.product_id == line.product_id)):
#                                 if vals.get('trial_period'):
#                                     vals['trial_period'] = False
#                                     vals.pop('trial_duration_unit', None)
#                                     vals.pop('trial_duration', None)
#                                     vals.update({
#                                         'start_date': order.date_order,
#                                         'next_payment_date': order.date_order + relativedelta(days=1)
#                                     })
#                             subscription_id = subscription.create(vals)
#                             subscription_id.get_confirm_subscription()
#         return res

#     def _cart_update(self, product_id=None, line_id=None, add_qty=0, set_qty=0, **kwargs):
#         res = super(SaleOrder, self)._cart_update(product_id=product_id,
#                                                   line_id=line_id, add_qty=add_qty, set_qty=set_qty, kwargs=kwargs)

#         for product in self.order_line:
#             override = product.product_id.subscription_plan_id.override_product_price
#             if line_id is not False and override:
#                 order = self.sudo().browse(self.id)
#                 order_line = self._cart_find_product_line(
#                     product_id, line_id, **kwargs)[:1]
#                 product = self.env['product.product'].browse(int(product_id))
#                 if product.activate_subscription:
#                     values = {
#                         'price_unit':  product.product_tmpl_id.currency_id._convert(
#                             product.subscription_plan_id.plan_amount +
#                             product.price_extra, order.pricelist_id.currency_id, self.env.company,
#                             fields.Date.today()
#                         ),
#                     }
#                     order_line.write(values)
#         return res

#     @api.onchange('order_line')
#     def subscription_product_order_line(self):
#         for product in self.order_line:
#             override = product.product_id.subscription_plan_id.override_product_price
#             if override and product.product_id.activate_subscription:
#                 product.price_unit = product.product_id.subscription_plan_id.plan_amount + \
#                     product.product_id.price_extra


# class AccountInvoice(models.Model):
#     _inherit = "account.move"

#     is_subscription = fields.Boolean(string="Is Subscription", copy=False)

#     def make_payment(self, invoice_generated):
#         journal_id = self.env["ir.default"]._get(
#             'res.config.settings', 'paid_subscription_journal')
#         if not journal_id:
#             raise UserError(_("Default Journal not found."))
#         journal = self.env['account.journal'].browse(journal_id)

#         for invoice_id in self:
#             if invoice_id.amount_residual_signed > 0.0:
#                 invoice_id.action_post()
#                 if invoice_generated == 'paid':
#                     # if not invoice_id.journal_id.default_credit_account_id:
#                     # invoice_id.journal_id.default_credit_account_id =  self.env.ref('subscription_management.subscription_sale_journal').id
#                     payment = self.env['account.payment'].sudo().create(
#                         {
#                             'journal_id': journal.id,
#                             'amount': invoice_id.amount_total,
#                             'payment_type': 'inbound',
#                             'payment_method_id': self.env['account.payment.method'].sudo().search([('payment_type', '=', 'inbound')], limit=1).id,
#                             'partner_type': 'customer',
#                             'partner_id': invoice_id.partner_id.id,
#                         }
#                     )
#                     payment.action_post()
#                     invoice_id.payment_state = 'paid'
#                     invoice_id.amount_residual = invoice_id.amount_total-invoice_id.amount_residual
#                     invoice_id.amount_residual_signed = invoice_id.amount_total - \
#                         invoice_id.amount_residual_signed
#                     invoice_id._compute_payments_widget_reconciled_info()

#         return True


# class SaleAdvancePaymentInv(models.TransientModel):
#     _inherit = "sale.advance.payment.inv"

#     def _create_invoices(self, sale_orders):
#         res = super(SaleAdvancePaymentInv, self)._create_invoices(sale_orders)
#         for subsc in sale_orders.mapped('subscription_ids'):
#             if subsc.product_id.id in res.invoice_line_ids.mapped('product_id').ids:
#                 subsc.invoice_ids = subsc.invoice_ids.union(res)
#                 if subsc.num_billing_cycle > 1:
#                     start_date = datetime(year=subsc.start_date.year, month=subsc.start_date.month,
#                                           day=subsc.start_date.day, minute=0, hour=0, second=0)
#                     if not isinstance(start_date, datetime):
#                         start_date = datetime(*start_date.timetuple()[:6])
#                     end_date = datetime(year=subsc.end_date.year, month=subsc.end_date.month,
#                                         day=subsc.end_date.day, minute=0, hour=0, second=0)
#                     date_intervals = subsc.cal_date_period(
#                         start_date, end_date, subsc.num_billing_cycle)
#                     subsc.write({
#                         'next_payment_date': datetime.strptime(date_intervals[subsc.invoice_count-1], "%d/%m/%Y %H:%M:%S")
#                     })
#         return res
