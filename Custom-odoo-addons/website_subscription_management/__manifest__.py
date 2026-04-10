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
  "name"                 :  "Website Subscription Management",
  "summary"              :  """Website Subscription Management:The module facilitates the customers to purchase a subscription plan for products directly from the Odoo website""",
  "category"             :  "Website",
  "version"              :  "1.0.0",
  "author"               :  "Webkul Software Pvt. Ltd.",
  "license"              :  "Other proprietary",
  "website"              :  "https://store.webkul.com/Odoo-Website-Subscription-Management.html",
  "description"          :  """website subscription : This module helps to purchase and manage the subscription plans.""",
  "live_test_url"        :  "http://odoodemo.webkul.com/?module=website_subscription_management&custom_url=/shop/",
  "depends"              :  [
                             'website_sale',
                             'subscription_management',
                             'mail',
                             'website_sale_comparison',
                             'website_sale_wishlist',
                            ],
  "data"                 :  [
                             'security/ir.model.access.csv',
                             'views/views.xml',
                             'views/templates.xml',
                            ],
  "assets": {
        'web.assets_frontend': [
            "/website_subscription_management/static/src/js/jquery.js",
            "/website_subscription_management/static/src/css/style.css",
        ],
    },
  "demo"                 :  ['demo/demo.xml'],
  "images"               :  ['static/description/Banner.png'],
  "application"          :  True,
  "pre_init_check"       :  "pre_init_check",
  "price"                :  60,
  "currency"             :  "USD",
}
