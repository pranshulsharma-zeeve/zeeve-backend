/** @odoo-module **/
/* Copyright (c) 2016-Present Webkul Software Pvt. Ltd. (<https://webkul.com/>) */
/* See LICENSE file for full copyright and licensing details. */

import { rpc } from "@web/core/network/rpc";
import { _t } from "@web/core/l10n/translation";

jQuery(function () {
    $(document).on('click', '#renew,.renew_table', function () {
        var rec_id = $('.rec_id').text();
        rpc('/website/json/controller', {
            'renew': rec_id,
        })
            .then(function (data) {
                location.reload();
            })

    });

    $('.js_main_product input').on('change', function () {
        var product_id = $(".product_id").attr('value');
        rpc('/check/product_variant/subscription', {
            'product_id': product_id,
        })
            .then(function (data) {
                if (data == false) {
                    // $('.css_quantity.input-group.oe_website_spinner').removeClass('hidden');
                    $('#subPlan_info').hide();

                }
                else {
                    $('#subPlan_info').show();
                    // $('.css_quantity.input-group.oe_website_spinner').addClass('hidden');
                    var dehighligh_element = $('.add_color').removeClass('add_color');
                    var highlight_element = Boolean($('[data-id=' + product_id + ']').addClass('add_color'));
                }
            })
    });

});
