# -*- coding: utf-8 -*-
#################################################################################
# Author      : Webkul Software Pvt. Ltd. (<https://webkul.com/>)
# Copyright(c): 2015-Present Webkul Software Pvt. Ltd.
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
#################################################################################
{
  "name"                 :  "Subscription Management",
  "summary"              :  """Subscription Management in Odoo facilitates the creation of subscription-based products in the Odoo.""",
  "category"             :  "Sales",
  "version"              :  "1.0.1",
  "sequence"             :  1,
  "author"               :  "Webkul Software Pvt. Ltd.",
  "license"              :  "Other proprietary",
  "website"              :  "https://store.webkul.com/Odoo-Subscription-Management.html",
  "description"          :  """Odoo Subscription Management
Subscription-based services in Odoo
Manage recurring bills in Odoo
Subscription management Software in Odoo
Subscription module for Odoo users
module for subscription management in Odoo
recurring billing management in Odoo
Subscription Module for Odoo
how to manage recurring services bills in Odoo
subscription services
subscription
Odoo subscription
manage subscription products in Odoo
Subscription products
Odoo Subscription Management
Odoo Website Subscription management
Odoo booking & reservation management
Odoo appointment management
Odoo website appointment management""",
  "live_test_url"        :  "http://odoodemo.webkul.com/?module=subscription_management",
  "depends"              :  [
                             'sale_management',
                             'zeeve_base',
                             'mail',
                             'account'
                            ],
  "data"                 :  [
                            #  'security/subscription_security.xml',
                             'security/ir.model.access.csv',
                             'report/invoice_report_template.xml',
                            'report/invoice_report.xml',
                             'data/automatic_invoice.xml',
                             'data/validator_performance_cron.xml',
                             'data/validator_performance_queue_cron.xml',
                             'data/validator_rewards_cron.xml',
                             'data/validator_rewards_queue_cron.xml',
                             'data/node_subscription_reminder_templates.xml',
                             'data/zeeve_mails.xml',
                             'data/zeeve_validator_mails.xml',
                             'data/subscription_mail_renewal.xml',
                            #'data/invoice_mail_template.xml',
                             'data/subscription_mails.xml',
                             'data/zoho_migration_mail_templates.xml',
                             'data/billing_cron.xml',
                             'wizard/sale_order_line_wizard_view.xml',
                             'wizard/cancel_reason_wizard_view.xml',
                             'wizard/message_wizard_view.xml',
                             'wizard/stripe_migration_wizard.xml',
                            'views/inherit_product_view.xml',
                             'views/subscription_plan_view.xml',
                             'views/mail_template_data.xml',
                             'views/subscription_subscription_view.xml',
                             'views/subscription_node_view.xml',
                             'views/subscription_sequence.xml',
                             'views/res_config_view.xml',
                             'views/subscription_reason_view.xml',
                             'views/inherit_protocol_master.xml',
                             'views/stripe_payment_log_views.xml',
                             'views/subscription_discount_views.xml',
                             'views/subscription_discount_integration.xml',
                            'views/invoice_subscription_queue.xml',
                            'views/inherit_account_move.xml',
                             'views/validator_performance_snapshot_views.xml',
                             'views/validator_performance_queue_views.xml',
                             'views/validator_rewards_snapshot_views.xml',
                             'views/validator_rewards_queue_views.xml',
                             "views/menu_access.xml",
                             'views/draft_prorated_view.xml',
                             'views/stripe_payment_method_views.xml',
                             'views/refund_invoice.xml',
                            ],
  "demo"                 :  ['data/subscription_management_data.xml'],
  "images"               :  ['static/description/Banner.png'],
  "application"          :  True,
  "installable"          :  True,
  "auto_install"         :  False,
  "price"                :  69,
  "currency"             :  "USD",
  "pre_init_hook"        :  "pre_init_check",
  'uninstall_hook'       :  "uninstall_hook",
}
