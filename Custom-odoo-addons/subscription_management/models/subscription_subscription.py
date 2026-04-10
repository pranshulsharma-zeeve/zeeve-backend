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
from email.policy import default
from datetime import datetime, date, time as dt_time, timezone
import logging
import json
import datetime
from datetime import date, datetime, timedelta
import time as time_lib
from psycopg2 import errors
from dateutil.relativedelta import relativedelta
from odoo.tools.misc import formatLang, format_date, format_datetime, get_lang
from odoo import api, fields, models, _
import odoo.addons.decimal_precision as dp
from odoo.tools import float_is_zero
from odoo.tools.safe_eval import safe_eval
from odoo.exceptions import UserError, ValidationError
import uuid
import stripe
from ..utils import restake_helper
from ..utils.email_utils import (
    send_subscription_email,
    send_subscription_cancellation_emails,
)
from ...rollup_management.models.rollup_service import RollupService
from ...zeeve_base.utils import base_utils
from ..utils import mnemonic_service
from odoo.tools import float_compare
from decimal import Decimal, ROUND_HALF_UP

try:
    from ...data_importer.utils.subscription_utils import SubscriptionUtils as _DataImporterSubscriptionUtils
except ImportError:  # pragma: no cover - optional dependency
    _DataImporterSubscriptionUtils = None

_logger = logging.getLogger(__name__)


class subscription_subscription(models.Model):
    """Model representing a customer subscription.

    The model is extended to support additional business fields and to
    integrate with chatter and activities.
    """

    _name = "subscription.subscription"
    _inherit = ['mail.thread', 'mail.activity.mixin']
    _description = "Subscription"
    _order = 'id desc'

    hosted_invoice_url = fields.Char(
        string='Stripe Hosted Invoice URL',
        help='Direct payment link for failed Stripe invoices',
        copy=False,
    )

    def unlink(self):
        for current_rec in self:
            if current_rec.invoice_ids:
                for invoice_id in current_rec.invoice_ids:
                    if invoice_id.state not in ('draft', 'cancel'):
                        raise UserError(
                            _("You can't delete the record because its invoice is create."))
            if current_rec.state != 'draft':
                raise UserError(_('Subscriptions can only be deleted while in Draft state.'))
            super(subscription_subscription, current_rec).unlink()
        return True

    def _compute_payment_count(self):
        """Compute the number of payments linked to the subscription."""
        for rec in self:
            rec.payment_count = len(rec.payment_ids)

    def _compute_payment_log_count(self):
        """Compute the number of payment logs linked to the subscription."""
        for rec in self:
            rec.payment_log_count = len(rec.payment_log_ids)

    def _compute_validator_transaction_count(self):
        for rec in self:
            rec.validator_transaction_count = len(rec.validator_transaction_ids)

    def _compute_etherlink_log_count(self):
        """Compute the number of Etherlink config logs associated with the subscription."""
        log_model = self.env['etherlink.node.config.update'].sudo()
        counts = {}
        if self.ids:
            data = log_model.read_group(
                [('subscription_id', 'in', self.ids)],
                ['subscription_id'],
                ['subscription_id'],
            )
            counts = {
                item['subscription_id'][0]: item['subscription_id_count']
                for item in data
                if item.get('subscription_id')
            }
        for rec in self:
            rec.etherlink_log_count = counts.get(rec.id, 0)

    @api.depends("node_ids")
    def _compute_node_count(self):
        """Count nodes linked to each subscription."""
        for rec in self:
            rec.node_count = len(rec.node_ids)

    def _admin_recipient_payload(self):
        """Return the admin recipient mapping for this subscription."""
        self.ensure_one()
        channel_code = False
        if self.protocol_id and self.protocol_id.admin_channel_id:
            channel_code = self.protocol_id.admin_channel_id.code
        return base_utils._get_admin_recipients(self.env, channel_code=channel_code or None)

    def get_latest_node(self):
        """Return the latest node linked to this subscription."""
        self.ensure_one()
        nodes = self.node_ids
        if not nodes:
            return nodes
        return nodes.sorted(
            key=lambda node: (
                node.create_date or fields.Datetime.from_string("1970-01-01 00:00:00"),
                node.id,
            ),
            reverse=True,
        )[:1]

    def get_primary_node(self):
        """Backward-compatible alias for the latest node accessor."""
        return self.get_latest_node()

    def serialize_nodes(self):
        """Return a list of serialized node dictionaries for API responses."""
        self.ensure_one()
        nodes = self.node_ids.sorted(
            key=lambda node: (
                node.create_date or fields.Datetime.from_string("1970-01-01 00:00:00"),
                node.id,
            ),
            reverse=True,
        )
        serialized = []
        for node in nodes:
            serialized.append(
                {
                    "node_id": node.node_identifier,
                    "id": node.id,
                    "node_identifier": node.node_identifier,
                    "node_name": node.node_name,
                    "subscription_id": self.subscription_uuid,
                    "node_type": node.node_type,
                    "state": node.state,
                    "network_selection": {
                        "id": node.network_selection_id.id,
                        "name": node.network_selection_id.name,
                    }
                    if node.network_selection_id
                    else None,
                    "server_location": {
                        "id": node.server_location_id.id,
                        "name": node.server_location_id.name,
                    }
                    if node.server_location_id
                    else None,
                    "software_update_rule": node.software_update_rule,
                    "endpoint_url": node.endpoint_url,
                    "metadata": node.metadata_json or {},
                    "create_date": fields.Datetime.to_string(node.create_date) if node.create_date else None,
                }
            )
        return serialized

    def create_primary_node(self, node_vals=None):
        """Create a primary node for the subscription."""
        node_model = self.env["subscription.node"].sudo()
        node_vals = node_vals or {}
        for subscription in self:
            payload = dict(node_vals)
            
            # Fallback to metaData if certain fields are missing
            metadata = subscription.metaData or {}
            if isinstance(metadata, str):
                try:
                    metadata = json.loads(metadata)
                except Exception:
                    metadata = {}
            
            if not payload.get('node_name') and metadata.get('node_name'):
                payload['node_name'] = metadata['node_name']
            
            if not payload.get('network_selection_id') and metadata.get('network_selection'):
                network_rec = self.env['zeeve.network.type'].sudo().search([('name', '=', metadata['network_selection'])], limit=1)
                if network_rec:
                    payload['network_selection_id'] = network_rec.id
            
            if not payload.get('server_location_id') and metadata.get('server_location_id'):
                payload['server_location_id'] = int(metadata['server_location_id'])
            
            if not payload.get('software_update_rule') and 'automatic_update' in metadata:
                payload['software_update_rule'] = 'auto' if metadata['automatic_update'] else 'manual'

            payload.setdefault("subscription_id", subscription.id)
            payload.setdefault("node_type", subscription.subscription_type or "other")
            payload.setdefault("state", subscription.state or "draft")
            node = node_model.create(payload)
            subscription._handle_post_node_creation(node)

    def notify_customer_provisioning_started(self):
        """Create a single in-app notification when subscription provisioning begins."""
        notification_env = self.env["zeeve.notification"].sudo()

        for subscription in self:
            partner = subscription.customer_name
            if not partner:
                continue

            protocol_name = (
                subscription.protocol_id.name
                or subscription.sub_plan_id.name
                or subscription.name
                or "your subscription"
            )
            node_type_label = dict(subscription._fields["subscription_type"].selection).get(
                subscription.subscription_type,
                subscription.subscription_type or "service",
            )

            notification_env.notify_partner(
                partner,
                notification_type="subscription_provisioning_started",
                title="Subscription activated",
                message="%s for %s has been activated and provisioning has started." % (
                    node_type_label,
                    protocol_name,
                ),
                category="success",
                payload={
                    "subscription_id": subscription.id,
                    "subscription_type": subscription.subscription_type or "",
                    "protocol_name": subscription.protocol_id.name if subscription.protocol_id else "",
                    "plan_name": subscription.sub_plan_id.name if subscription.sub_plan_id else "",
                    "state": subscription.state or "",
                },
                action_url="/nodes",
                reference_model="subscription.subscription",
                reference_id=subscription.id,
                dedupe_key="subscription_provisioning_started:%s" % subscription.id,
            )

    def _handle_post_node_creation(self, node):
        """Execute post-processing whenever a node is created."""
        self.ensure_one()
        if not node or node.subscription_id != self:
            return
        is_coreum_validator = (
            self.subscription_type == "validator"
            and self.protocol_id
            and self.protocol_id.name == "Coreum"
        )
        if not is_coreum_validator:
            # Even when not Coreum, migrate legacy validator_info from subscription to node
            if self.validator_info and not node.validator_info:
                node.sudo().write({"validator_info": self.validator_info})
            return

        is_testnet = bool(
            node.network_selection_id
            and node.network_selection_id.name
            and node.network_selection_id.name.lower() == "testnet"
        )

        # If legacy validator info exists on the subscription record, reuse it before generating a new wallet.
        legacy_validator_info = (self.validator_info or "").strip()
        if legacy_validator_info and not node.validator_info:
            node.sudo().write({"validator_info": legacy_validator_info})
        else:
            self.action_generate_and_store_wallet(is_testnet, target_node=node)

    @api.onchange('customer_name')
    def oncahnage_customer_name(self):
        if self.customer_name:
            self.customer_billing_address = self.customer_name
    
    
    @api.depends('start_date', 'start_immediately', 'duration', 'unit','num_billing_cycle','never_expires')
    def get_end_date(self):
        for current_rec in self:
            end_date = False
            if current_rec.num_billing_cycle > 0:
                date = current_rec.start_date or current_rec.stripe_start_date
                if not date or isinstance(date, bool):
                    current_rec.end_date = False
                    continue
                base_duration = current_rec.duration or 0
                if not base_duration:
                    current_rec.end_date = False
                    continue
                duration = current_rec.num_billing_cycle * base_duration
                if current_rec.unit == 'day':
                    end_date = date + relativedelta(days=duration)
                elif current_rec.unit == 'month':
                    end_date = date + relativedelta(months=duration)
                elif current_rec.unit == 'year':
                    end_date = date + relativedelta(years=duration)
                elif current_rec.unit == 'week':
                    end_date = date + timedelta(weeks=duration)
            current_rec.end_date = end_date if not current_rec.never_expires else False
            if current_rec.never_expires:
                current_rec.num_billing_cycle = -1

    def is_paid_subscription(self):
        for obj in self:
            if any(invoice.payment_state != 'paid' for invoice in obj.invoice_ids):
                obj.is_paid = True
            else:
                obj.is_paid = False

    is_paid = fields.Boolean(string="Is Paid", compute="is_paid_subscription")
    subscription_ref = fields.Char(string="Subscription Ref#", readonly=True, copy=False)
    name = fields.Char(string='Name', readonly=True)
    node_ids = fields.One2many(
        "subscription.node",
        "subscription_id",
        string="Nodes",
        copy=False,
        tracking=True
    )
    node_count = fields.Integer(
        string="Node Count",
        compute="_compute_node_count",
        store=True,
        readonly=True,
        tracking=True
    )
    active = fields.Boolean(string="Active", default=True)
    customer_name = fields.Many2one(
        'res.partner', string="Customer Name", required=True,
        tracking=True)
    customer_email = fields.Char(
        string="Customer Email",
        related="customer_name.email",
        store=True,
        readonly=True,
        tracking=True
    )
    subscription_type = fields.Selection(
        [('rpc', 'RPC Nodes'), ('archive', 'Archive Node'), ('validator', 'Validator Node')],
        string="Subscription Type", tracking=True)
    protocol_id = fields.Many2one('protocol.master', string="Protocol", tracking=True)
    subscription_uuid = fields.Char(
        string="Subscription ID",
        copy=False,
        readonly=True,
        oldname="node_id",
        help="Unique public identifier for external systems.",
    )

    hide_next_payment_date = fields.Boolean(default=False)
    source = fields.Selection(
        [('so', 'Sale Order'), ('manual', 'Manual')], 'Related To', default="manual")
    # subscription_ref = fields.Char(string="Subscription Ref", copy=False)
    quantity = fields.Float(string='Quantity', digits=dp.get_precision(
        'Product Unit of Measure'), required=True, default=1.0,tracking=True)
    sub_plan_id = fields.Many2one(
        'subscription.plan',  string="Subscription Plan", required=True, tracking=True)
    payment_frequency = fields.Selection(
        [('monthly', 'Monthly'), ('quarterly', 'Quarterly'), ('annually', 'Annually')],
        string="Payment Frequency", tracking=True)
    subscribed_on = fields.Datetime(string="Subscribed On", readonly=True, tracking=True)
    duration = fields.Integer(string="Duration")
    renewal_days = fields.Integer(
        string="Renewal Days",  compute="_renewal_days")
    unit = fields.Selection([('week', 'Week(s)'), ('day', 'Day(s)'), (
        'month', 'Month(s)'), ('year', 'Year(s)')], string="Unit", required=True)
    price = fields.Float(string="Price",  required=True,tracking=True)
    start_date = fields.Date(string="Start Date")
    next_payment_date = fields.Datetime(
        string="Date of Next Payment", copy=False, tracking=True)
    never_expires = fields.Boolean(string="Never Expire", help="This Plan billing cycle never expire instead of specifying a number of billing cycles.", copy=False)
    state = fields.Selection([('draft','Draft'),('requested', 'Requested'),
        ('provisioning', 'Provisioning'),
        ('in_grace','In grace'),
        ('syncing', 'Syncing'),
        ('ready', 'Ready'),
        ('suspended', 'Suspended'),
        ('closed', 'Closed'), # means subscription cancelled
        ('deleted', 'Deleted'),], default='draft', string='Node Status', tracking=True, copy=False)
    reason = fields.Char(string="Reason", tracking=True)
    alert_call=fields.Boolean('Alert manage',default=True)
    invoice_ids = fields.One2many(
        "account.move", 'subscription_id', string='Invoices', readonly=True, copy=False)
    invoice_count = fields.Integer(readonly=True, string='Invoice Count')
    payment_ids = fields.One2many('account.payment', 'subscription_id', string="Payments", readonly=True)
    payment_count = fields.Integer(compute='_compute_payment_count', string='Payments')
    tax_id = fields.Many2many('account.tax', string='Taxes')
    num_billing_cycle = fields.Integer(string="No of Billing Cycle", default=1)
    start_immediately = fields.Char(string="Start")
    trial_duration = fields.Integer(string='Trial Duration',default=1)
    trial_duration_unit = fields.Selection([('week', 'Week(s)'), ('day', 'Day(s)'), ('month', 'Month(s)'), (
        'year', 'Year(s)')], string=' Trial Duration Unit', help="The trial unit specifpriceied in a plan. Specify  day, month, year.")
    trial_period = fields.Boolean(string="Plan has trial period", copy=False,
                                  help="A value indicating whether a subscription should begin with a trial period.")
    parent_subscription_id = fields.Many2one(
        'subscription.subscription',
        string="Parent Subscription",
        copy=False,
        help="Links to the subscription record that was renewed to create this one.",
    )
    customer_billing_address = fields.Many2one(
        'res.partner', string="Customer Invoice/Billing Address")
    old_customer_id = fields.Many2one("res.partner", string="Old Customer")
    end_date = fields.Date(compute="get_end_date", string="End Date")
    currency_id = fields.Many2one('res.currency', string='Currency',
                                  default=lambda self: self.env.user.company_id.currency_id)

    current_term_start = fields.Datetime(string="Current Term Starts", tracking=True)
    current_term_end = fields.Datetime(string="Current Term Ends", tracking=True)
    last_billing_on = fields.Datetime(string="Last Billing On", tracking=True)
    skip_trial_invoice = fields.Boolean(string="Skip Trial Invoice", default=False, copy=False)
    # Stripe Integration Fields
    company_id = fields.Many2one('res.company', default=lambda self: self.env.company, required=True)
    stripe_start_date = fields.Datetime(string="Stripe Start Date",tracking=True)
    stripe_end_date = fields.Datetime(string="Next Billing Date",tracking=True)
    stripe_subscription_id = fields.Char(string='Stripe Subscription ID', index=True, copy=False,tracking=True)
    stripe_customer_id = fields.Char(string='Stripe Customer ID', index=True, copy=False,tracking=True)
    stripe_payment_method_id = fields.Char(string='Stripe Payment Method ID', copy=False,tracking=True)
    stripe_price_id = fields.Char(
        string="Stripe Price ID",
        copy=False,
        help="Price identifier used when creating or updating the Stripe subscription",
        tracking=True
    )
    autopay_enabled = fields.Boolean(string='Autopay Enabled', default=True, tracking=True)
    stripe_status = fields.Selection([
        ('draft','Draft'),
        ('active', 'Active'),
        ('paused', 'Paused'),
        ('canceled', 'Canceled'),
        ('incomplete', 'Incomplete'),
        ('incomplete_expired', 'Incomplete Expired'),
        ('past_due', 'Past Due'),
        ('trialing', 'Trialing'),
        ('unpaid', 'Unpaid'),
    ], string='Stripe Status', copy=False,tracking=True)
    
    # Payment Logging
    payment_log_ids = fields.One2many('stripe.payment.log', 'subscription_id', string='Payment Logs')
    payment_log_count = fields.Integer(compute='_compute_payment_log_count', string='Payment Log Count')
    validator_transaction_ids = fields.One2many(
        'subscription.validator.transaction',
        'subscription_id',
        string='Validator Transactions',
        copy=False,
        readonly=True,
    )
    validator_transaction_count = fields.Integer(
        compute='_compute_validator_transaction_count',
        string='Validator Transactions',
    )
    etherlink_log_count = fields.Integer(
        compute='_compute_etherlink_log_count',
        string='Config Logs',
    )
    company_id = fields.Many2one('res.company', default=lambda self: self.env.company, required=True)
    # Discount Integration
    discount_id = fields.Many2one('subscription.discount', string='Applied Discount', copy=False, tracking=True)
    discount_code = fields.Char(string='Discount Code', copy=False, readonly=True, tracking=True)
    discount_amount = fields.Float(string='Discount Amount', copy=False, readonly=True)
    original_price = fields.Float(string='Original Price', copy=False, readonly=True, tracking=True)
    pending_quantity_increase = fields.Integer(string='Pending Quantity Increase', default=0, tracking=True)
    pending_quantity_paid = fields.Boolean(string='Pending Quantity Paid', default=False)
    pending_quantity_prorated_amount = fields.Float(string='Pending Prorated Amount')
    invoice_in_progress = fields.Boolean(default=False,string="Invoice flag")
    last_processed_event_id = fields.Char("Last Processed Event ID")
    # Odoo Managed Billing Fields
    is_odoo_managed = fields.Boolean(string="Is Odoo Managed", default=False, tracking=True)
    payment_vault_id = fields.Many2one('stripe.payment.method', string="Payment Method Vault", tracking=True)
    last_charge_date = fields.Datetime(string="Last Charge Date", tracking=True)
    charge_retry_count = fields.Integer(string="Charge Retry Count", default=0, tracking=True)
    _sql_constraints = [
        ('check_for_uniq_subscription', 'Check(1=1)',
         "You can't create Multiple Subscription for sale order with the same product and customer."),
    ]
    is_vision_onboarded = fields.Boolean(string="Is Vision Onboarded", default=False)
    validator_info = fields.Text(string="Validator Info", readonly=False)
    metaData = fields.Text(string="Metadata", default=dict, copy=False)
    zoho_subscription_id = fields.Char(string='Zoho Subscription ID', index=True)
    provision_mail_sent = fields.Boolean(
    string="Provision Mail Sent",
    default=False,
    copy=False,
)
    def write(self, vals):
        if vals.get('customer_name'):
            for current_rec in self:
                vals['old_customer_id'] = current_rec.customer_name.id
        return super(subscription_subscription, self).write(vals)
    
    @api.onchange('never_expires')
    def onchange_never_expires(self):
        if self.never_expires and self.num_billing_cycle !=-1:
            self.num_billing_cycle = -1
        else: 
            self.num_billing_cycle = 1

    @api.onchange('stripe_end_date')
    def onchange_stripe_end_date(self):
        for rec in self:
            rec.next_payment_date = rec.stripe_end_date.date() if rec.stripe_end_date else False

    def send_subscription_mail(self):
        template_id = self.env.ref(
            'subscription_management.subscription_management_mail_template')
        template_id.send_mail(self.id, force_send=True)
    
    def check_validation(self):
        if self.start_date < date.today():
            return "Please check your start date."
        if not self.active:
          return "You can't confirm an Inactive Subscription."
        if self.num_billing_cycle == 0 or self.num_billing_cycle < -1:
            return "Billing cycle should never be 0 or less except -1."
        if self.trial_period and self.trial_duration <= 0:
            return "Trial duration should never be 0 or less."
        if self.duration <=0 or self.quantity <= 0 or self.price <=0:
            return "Duration, quantity, and price should never be 0 or less."
        return False

    # ------------------------------------------------------------------
    # Helper methods
    # ------------------------------------------------------------------
    def _get_latest_invoice(self):
            """Return the most recent invoice linked to the subscription."""

            self.ensure_one()
            if not self.invoice_ids:
                return self.env['account.move']
            return self.invoice_ids.sorted(
                key=lambda inv: inv.invoice_date or inv.create_date or inv.id
            )[-1]

    def _get_next_payment_date(self, from_date):
        """Compute the next payment date based on the payment frequency."""
        if self.payment_frequency == 'monthly':
            delta = timedelta(days=30)
        elif self.payment_frequency == 'quarterly':
            delta = timedelta(days=90)
        elif self.payment_frequency == 'annually':
            delta = timedelta(days=365)
        else:
            delta = timedelta(0)
        return from_date + delta

    def _register_payment(self, payment):
        """Update subscription data when a payment is recorded."""
        payment_date = fields.Datetime.from_string(str(payment.date)) if payment.date else fields.Datetime.now()
        self.last_billing_on = payment_date
        self.current_term_start = payment_date
        self.next_payment_date = self._get_next_payment_date(payment_date)
        self.current_term_end = self.next_payment_date
        self.skip_trial_invoice = False
        if self.state == 'draft':
            self.state = 'in_progress'
        if self.next_payment_date and fields.Datetime.now() > self.next_payment_date:
            self.state = 'in_grace'

    @api.onchange('trial_period', 'trial_duration_unit', 'trial_duration')
    def onchange_trial_period(self):
        date = datetime.today().date()
        if self.trial_period:
            if self.trial_duration_unit == 'day':
                date = date + relativedelta(days=self.trial_duration)
            if self.trial_duration_unit == 'month':
                date = date + relativedelta(months=self.trial_duration)
            if self.trial_duration_unit == 'year':
                date = date + relativedelta(years=self.trial_duration)
            if self.trial_duration_unit == 'week':
                date = date + timedelta(weeks=self.trial_duration)
            if self.trial_duration_unit == 'hour':
                date = date + timedelta(hours=self.trial_duration)
        self.start_date = date


    def action_view_payments(self):
        """Smart button action to display related payments."""
        payments = self.mapped('payment_ids')
        action = self.env.ref('account.action_account_payments').read()[0]
        if len(payments) > 1:
            action['domain'] = [('id', 'in', payments.ids)]
        elif len(payments) == 1:
            form_view = [(self.env.ref('account.view_account_payment_form').id, 'form')]
            action['views'] = form_view + [(state, view) for state, view in action.get('views', []) if view != 'form']
            action['res_id'] = payments.id
        else:
            action = {'type': 'ir.actions.act_window_close'}
        return action

    def action_view_validator_transactions(self):
        self.ensure_one()
        action = self.env.ref(
            'subscription_management.action_subscription_validator_transactions',
            raise_if_not_found=False,
        )
        if action:
            result = action.read()[0]
            result['domain'] = [('subscription_id', '=', self.id)]
            raw_ctx = result.get('context') or {}
            if isinstance(raw_ctx, str):
                try:
                    ctx = safe_eval(raw_ctx)
                except Exception:  # pylint: disable=broad-except
                    ctx = {}
            elif isinstance(raw_ctx, dict):
                ctx = dict(raw_ctx)
            else:
                ctx = {}
            ctx.update({'default_subscription_id': self.id})
            result['context'] = ctx
            return result

        return {
            'type': 'ir.actions.act_window',
            'name': _('Validator Transactions'),
            'res_model': 'subscription.validator.transaction',
            'view_mode': 'tree,form',
            'domain': [('subscription_id', '=', self.id)],
            'context': {'default_subscription_id': self.id},
        }
    @api.model
    def send_mail_template(self, template_xml_id, ctx=None):
        """Helper function to send email using a mail template"""
        template = self.env.ref(template_xml_id)
        for record in self:
            if not template:
                _logger.warning("Mail template not found: %s", template_xml_id)
                continue
            context = dict(self.env.context or {}, **(ctx or {}))
            try:
                html = template._render_field('body_html', [record.id])[record.id]
                _logger.info("Mail render success for subscription %s", record.id)
            except Exception as e:
                _logger.error("Mail render failed for subscription %s: %s", record.id, e)
                continue
            try:
                template.with_context(context).send_mail(record.id, force_send=True)
                _logger.info("Mail sent successfully for subscription %s", record.id)
            except Exception as e:
                _logger.error("Mail send failed for subscription %s: %s", record.id, e)


    def write(self, vals):
        try:
            res = super(subscription_subscription, self).write(vals)
            for record in self:
                if 'state' in vals:
                    new_state = vals['state']
                    sub_type = record.subscription_type
                    template_map = {
                        'rpc': {
                            'syncing': 'subscription_management.mail_template_subscription_node_provisioning_complete',
                            'ready':'subscription_management.mail_template_subscription_node_ready'
                        },
                        'archive': {
                            'syncing': 'subscription_management.mail_template_subscription_node_provisioning_complete',
                            'ready':'subscription_management.mail_template_subscription_node_ready'
                        },
                        'validator': {
                            'syncing': 'subscription_management.mail_template_subscription_validator_provisioning_complete',
                            'ready':'subscription_management.mail_template_subscription_validator_provisioning_complete'
                        },
                    }

                    template_xml_id = template_map.get(sub_type, {}).get(new_state)
                    print('template_xml_id',template_xml_id)
                    if template_xml_id:
                        partner = record.customer_name
                        full_name = ''
                        if partner:
                            full_name = partner.display_name or partner.name or ''
                        first_piece = ''
                        second_piece = ''
                        if full_name:
                            parts = full_name.split(' ', 1)
                            first_piece = parts[0]
                            second_piece = parts[1] if len(parts) > 1 else ''
                        customer_name = {
                                'firstname': getattr(partner, 'first_name', False) or first_piece,
                                'lastname': getattr(partner, 'last_name', False) or second_piece,
                                'email': partner.email if partner else '',
                                'name': full_name or (partner.email if partner else ''),
                            }
                        def _format_datetime(value):
                            if not value:
                                return ''
                            if isinstance(value, datetime):
                                value = value.date()
                            if isinstance(value, date):
                                try:
                                    return format_date(self.env, value)
                                except Exception:  # pylint: disable=broad-except
                                    return fields.Date.to_string(value)
                            return str(value)

                        start_display = _format_datetime(record.stripe_start_date or record.start_date)
                        end_display = _format_datetime(record.stripe_end_date or record.end_date)
                        currency_label = ''
                        if record.currency_id:
                            currency_label = record.currency_id.symbol or record.currency_id.name or ''

                        ctx = {
                            'customer_name': customer_name,
                            'plan_details': {
                                    'name': customer_name.get('firstname') ,
                                    'buyer_email_id': record.customer_name.email,
                                    'plan_name': record.sub_plan_id.name,
                                    'protocol_name': record.protocol_id.name,
                                    'subscription_start_date': start_display,
                                    'node_type':record.subscription_type,
                                    'subscription_end_date': end_display,
                                    'ready_eta':'TBA',
                                    'subscription_cost': f"{((record.original_price - record.discount_amount) or 0.0):.2f}",
                                    'currency_symbol': currency_label,
                            },
                            'email_to': record.customer_name.email,
                        }
                        record.send_mail_template(template_xml_id, ctx)

            return res
        except Exception as e:
            print('----------------508',str(e))
    def send_provisioning_mail(self):
        try:
            for record in self:
                if record.provision_mail_sent:
                    _logger.info("Provisioning mail already sent for subscription %s, skipping", record.id)
                    continue
                if record.state:
                    new_state = record.state
                    sub_type = record.subscription_type
                    template_map = {
                        'rpc': {
                            'provisioning': 'subscription_management.mail_template_subscription_node_journey_subscription',
                        },
                        'archive': {
                            'provisioning': 'subscription_management.mail_template_subscription_node_journey_subscription',
                        },
                        'validator': {
                            'provisioning': 'subscription_management.mail_template_subscription_validator_node_journey_subscription'
                        },
                    }

                    template_xml_id = template_map.get(sub_type, {}).get(new_state)
                    if template_xml_id:
                        partner = record.customer_name
                        full_name = ''
                        if partner:
                            full_name = partner.display_name or partner.name or ''
                        first_piece = ''
                        second_piece = ''
                        if full_name:
                            parts = full_name.split(' ', 1)
                            first_piece = parts[0]
                            second_piece = parts[1] if len(parts) > 1 else ''
                        customer_name = {
                                'firstname': getattr(partner, 'first_name', False) or first_piece,
                                'lastname': getattr(partner, 'last_name', False) or second_piece,
                                'email': partner.email if partner else '',
                                'name': full_name or (partner.email if partner else ''),
                            }
                        def _format_datetime(value):
                            if not value:
                                return ''
                            if isinstance(value, datetime):
                                value = value.date()
                            if isinstance(value, date):
                                try:
                                    return format_date(self.env, value)
                                except Exception: 
                                    return fields.Date.to_string(value)
                            return str(value)

                        start_display = _format_datetime(record.stripe_start_date or record.start_date)
                        end_display = _format_datetime(record.stripe_end_date or record.end_date)
                        currency_label = ''
                        if record.currency_id:
                            currency_label = record.currency_id.symbol or record.currency_id.name or ''

                        ctx = {
                            'customer_name': customer_name,
                            'plan_details': {
                                    'name': customer_name.get('firstname') ,
                                    'buyer_email_id': record.customer_name.email,
                                    'plan_name': record.sub_plan_id.name,
                                    'protocol_name': record.protocol_id.name,
                                    'subscription_start_date': start_display,
                                    'node_type':record.subscription_type,
                                    'subscription_end_date': end_display,
                                    'ready_eta':'TBA',
                                    'subscription_cost': f"{((record.original_price - record.discount_amount) or 0.0):.2f}",
                                    'currency_symbol': currency_label,
                            },
                            'email_to': record.customer_name.email,
                        }
                        record.send_mail_template(template_xml_id, ctx)
                        record.provision_mail_sent = True
        except Exception as e:
            print('----------------508',str(e))
    def send_mail(self):
        try:
            for record in self:
                    if 'state' in vals:
                        new_state = vals['state']
                        sub_type = record.subscription_type
                        template_map = {
                            'rpc': {
                                'provisioning': 'subscription_management.mail_template_subscription_node_journey_subscription',
                                'syncing': 'subscription_management.mail_template_subscription_node_provisioning_complete',
                                'ready':'subscription_management.mail_template_subscription_node_ready'
                            },
                            'archive': {
                                'provisioning': 'subscription_management.mail_template_subscription_node_journey_subscription',
                                'syncing': 'subscription_management.mail_template_subscription_node_provisioning_complete',
                                'ready':'subscription_management.mail_template_subscription_node_ready'
                            },
                            'validator': {
                                'provisioning': 'subscription_management.mail_template_subscription_validator_node_journey_subscription',
                                'syncing': 'subscription_management.mail_template_subscription_validator_provisioning_complete',
                                'ready':'subscription_management.mail_template_subscription_validator_provisioning_complete'
                            },
                        }

                        template_xml_id = template_map.get(sub_type, {}).get(new_state)
                        print('template_xml_id',template_xml_id)
                        if template_xml_id:
                            partner = record.customer_name
                            full_name = ''
                            if partner:
                                full_name = partner.display_name or partner.name or ''
                            first_piece = ''
                            second_piece = ''
                            if full_name:
                                parts = full_name.split(' ', 1)
                                first_piece = parts[0]
                                second_piece = parts[1] if len(parts) > 1 else ''
                            customer_name = {
                                    'firstname': getattr(partner, 'first_name', False) or first_piece,
                                    'lastname': getattr(partner, 'last_name', False) or second_piece,
                                    'email': partner.email if partner else '',
                                    'name': full_name or (partner.email if partner else ''),
                                }
                            def _format_datetime(value):
                                if not value:
                                    return ''
                                if isinstance(value, datetime):
                                    value = value.date()
                                if isinstance(value, date):
                                    try:
                                        return format_date(self.env, value)
                                    except Exception:  # pylint: disable=broad-except
                                        return fields.Date.to_string(value)
                                return str(value)

                            start_display = _format_datetime(record.stripe_start_date or record.start_date)
                            end_display = _format_datetime(record.stripe_end_date or record.end_date)
                            currency_label = ''
                            if record.currency_id:
                                currency_label = record.currency_id.symbol or record.currency_id.name or ''

                            ctx = {
                                'customer_name': customer_name,
                                'plan_details': {
                                        'name': customer_name.get('firstname') ,
                                        'buyer_email_id': record.customer_name.email,
                                        'plan_name': record.sub_plan_id.name,
                                        'protocol_name': record.protocol_id.name,
                                        'subscription_start_date': start_display,
                                        'node_type':record.subscription_type,
                                        'subscription_end_date': end_display,
                                        'ready_eta':'TBA',
                                        'subscription_cost': f"{(record.price or 0.0):.2f}",
                                        'currency_symbol': currency_label,
                                },
                                'email_to': record.customer_name.email,
                            }
                            record.send_mail_template(template_xml_id, ctx)
        except Exception as e:
            print('----------------508',str(e))
    def action_view_etherlink_config_logs(self):
        """Open the Etherlink configuration log records linked to this subscription."""
        self.ensure_one()
        action = self.env.ref(
            'subscription_management.action_etherlink_node_config_updates',
            raise_if_not_found=False,
        )
        domain = [('subscription_id', '=', self.id)]
        context_updates = {
            'default_subscription_id': self.id,
            'default_node_id': self.subscription_uuid,
        }
        if self.protocol_id:
            context_updates['default_protocol_name'] = self.protocol_id.name
        if action:
            result = action.read()[0]
            result['domain'] = domain
            raw_ctx = result.get('context') or {}
            if isinstance(raw_ctx, str):
                try:
                    ctx = safe_eval(raw_ctx)
                except Exception:  # pylint: disable=broad-except
                    ctx = {}
            elif isinstance(raw_ctx, dict):
                ctx = dict(raw_ctx)
            else:
                ctx = {}
            ctx.update(context_updates)
            result['context'] = ctx
            return result

        return {
            'type': 'ir.actions.act_window',
            'name': _('Node Config Logs'),
            'res_model': 'etherlink.node.config.update',
            'view_mode': 'tree,form',
            'domain': domain,
            'context': context_updates,
        }

    def action_view_nodes(self):
        """Open a tree/form view filtered on the subscription nodes."""
        self.ensure_one()
        tree_view = self.env.ref(
            'subscription_management.view_subscription_node_tree',
            raise_if_not_found=False,
        )
        form_view = self.env.ref(
            'subscription_management.view_subscription_node_form',
            raise_if_not_found=False,
        )
        views = []
        if tree_view:
            views.append((tree_view.id, 'list'))
        if form_view:
            views.append((form_view.id, 'form'))
        action = {
            'name': _('Subscription Nodes'),
            'type': 'ir.actions.act_window',
            'res_model': 'subscription.node',
            'view_mode': 'tree,form',
            'domain': [('subscription_id', '=', self.id)],
            'context': {'default_subscription_id': self.id},
        }
        if views:
            action['views'] = views
            action['view_id'] = views[0][0]
        return action

    def cal_date_period(self, start_date, end_date, billing_cycle):
        
        date_diff = (end_date - start_date).days
        hour_diff = date_diff*24
        return [(start_date + relativedelta(hours=i)).strftime("%d/%m/%Y %H:%M:%S") for i in range(1, hour_diff, hour_diff//self.num_billing_cycle)][1:]
    
    def calculate_time_period(self):
        if self.unit == 'day':
            return relativedelta(days=self.duration)
        if self.unit == 'month':
            return relativedelta(months=self.duration)
        if self.unit == 'year':
            return relativedelta(years=self.duration)
        if self.unit == 'week':
            return timedelta(weeks=self.duration)


    def get_cancel_sub(self):
        for current_rec in self:
            if current_rec.state == 'draft':
                current_rec.state = 'cancel'
        return True

    def make_payment(self):
        # _logger.info("=================test===")
        journal_id = self.env["ir.default"]._get(
            'res.config.settings', 'journal_id')
        if not journal_id:
            raise UserError(_("Default Journal not found."))
        journal = self.env['account.journal'].browse(journal_id)
        for current_rec in self:
            if current_rec.invoice_ids:
                for invoice_id in current_rec.invoice_ids:
                    if invoice_id.amount_residual_signed > 0.0:
                        invoice_id.action_post()
                        # if not invoice_id.journal_id.default_credit_account_id:
                        # invoice_id.journal_id.default_credit_account_id =  self.env.ref('subscription_management.subscription_sale_journal').id
                        # _logger.info("================account===payment===%r",self.env['account.payment.method'].search([('payment_type', '=', 'inbound')], limit=1).name)
                        self.env['account.payment'].sudo().create({'journal_id': invoice_id.journal_id.id,'amount': invoice_id.amount_total, 'payment_type': 'inbound',
                                                                   'payment_method_line_id': self.env['account.payment.method'].sudo().search([('payment_type', '=', 'inbound')], limit=1).id, 'partner_type': 'customer', 'partner_id': invoice_id.partner_id.id, }).action_post()
                        invoice_id.payment_state = 'paid'
                        invoice_id.amount_residual = invoice_id.amount_total-invoice_id.amount_residual
                        invoice_id.amount_residual_signed = invoice_id.amount_total - \
                            invoice_id.amount_residual_signed
                        invoice_id._compute_payments_widget_reconciled_info()

        return True

    def reset_to_draft(self):
        for current_rec in self:
            if current_rec.state == 'cancel':
                current_rec.state = 'draft'
        return True

    def reset_to_close(self):
        for current_rec in self:
            if current_rec.state not in ['close', 'cancel', 'renewed']:
                if current_rec.invoice_ids:
                    self.pay_cancel_invoice()
                current_rec.state = 'close'
                current_rec.num_billing_cycle = current_rec.invoice_count
            if self._context.get('close_refund'):
                return current_rec.action_view_invoice()
        return True
    
    @api.constrains('trial_period')
    def _validate_triil_period(self):
        if self.trial_period:
            trial_period_setting = self.env['res.config.settings'].sudo().get_values()['trial_period_setting']
            if len(self.customer_name.all_subscription) > 1 and trial_period_setting == 'one_time':
                    raise ValidationError(_("Trial Period is allowed only once for a customer."))
            elif trial_period_setting == 'product_based' and len(self.env['res.partner'].sudo().browse(self.customer_name.id).all_subscription.filtered(lambda subscription: subscription.product_id.id == self.product_id.id))>1:
                raise ValidationError(_("Trial Period is allowed only once."))

    @api.model_create_multi
    def create(self, vals_list):
        for vals in vals_list:
            seq = self.env['ir.sequence'].next_by_code(
                'subscription.subscription')
            vals['name'] = seq
            vals['subscription_ref'] = seq
            if 'subscription_uuid' not in vals:
                vals['subscription_uuid'] = str(uuid.uuid4())
            # vals['subscribed_on'] = fields.Datetime.now()
            # if not vals.get('customer_billing_address'):
            #     vals['customer_billing_address'] = vals.get('customer_name')
        return super(subscription_subscription, self).create(vals_list)

    def _get_billing_company(self):
        """
        Identify the 'billing company' for this subscription.
        Always return the main 'Administration' company (ID 1).
        """
        self.ensure_one()
        # ID 1 is standard for "Administration" or "My Company" in Odoo.
        MainCompany = self.env['res.company'].sudo().browse(1)
        if MainCompany.exists():
            return MainCompany
        return self.env.ref('base.main_company', raise_if_not_found=False) or self.company_id

    def create_invoice(self, action=None):
        """Create invoice in Odoo after Stripe payment success"""
        for rec in self:
            _logger.info("create_invoice %s", rec.id)
            partner = rec.customer_name
            if not partner:
                raise UserError(_("No partner found for Stripe Customer ID: %s") % rec.stripe_customer_id)

            billing_company = rec._get_billing_company()
            
            # Check for sale journal in billing company
            journal = rec.env['account.journal'].sudo().search([
                ('type', '=', 'sale'),
                ('company_id', '=', billing_company.id)
            ], limit=1)
            if not journal:
                raise UserError(_("No sale journal found for company %s. Please configure one.") % billing_company.name)

            primary_node = rec.get_primary_node()
            # Get product based on subscription_type
            product = rec.env["product.product"].sudo().search([('name', 'ilike', rec.subscription_type)], limit=1)
            if not product:
                raise UserError(_("Product not found for subscription type: %s") % rec.subscription_type)

            # --- Stripe discount extraction from subscription fields ---
            discount_record = rec.discount_id
            discount_code_value = rec.discount_code
            discount_amount = rec.discount_amount or 0.0
            original_price = rec.original_price or product.lst_price
            if not rec.original_price and original_price:
                rec.sudo().write({'original_price': original_price})
            if not rec.discount_amount and discount_amount:
                rec.sudo().write({'discount_amount': discount_amount})
            price_unit = rec.price

            # Do NOT update discount fields here; only update during checkout/payment webhook
            Currency = rec.env["res.currency"].sudo()
            currency = Currency.search([("name", "=", "USD")], limit=1)


            # Create  Regular Invoice Creation with discount_id in line
            if not action or action != 'prorated_charge_for_qty_increase':
                if rec.discount_id:
                    discount = rec.discount_id.discount_value
                else:
                    discount = (rec.discount_amount / rec.original_price) * 100 if rec.original_price else 0.0

                nodes = rec.node_ids.sorted(lambda n: n.create_date or fields.Datetime.now())
                billable_qty = int(rec.quantity or 1)
                billable_consumed = 0

                line_commands = []
                for node in nodes:
                    line_is_billable = billable_consumed < billable_qty
                    if line_is_billable:
                        billable_consumed += 1
                    line_commands.append((0, 0, {
                        'product_id': product.id,
                        'quantity': 1,
                        'price_unit': price_unit if line_is_billable else 0.0,
                        'name': f"{rec.protocol_id.name}-{(node.node_name or node.node_identifier or '')}",
                        'discount': discount if line_is_billable else 0.0,
                        'discount_id': rec.discount_id.id if rec.discount_id and line_is_billable else False,
                        'discount_code': rec.discount_id.code if rec.discount_id and line_is_billable else '',
                        'node_id': node.id,
                    }))

                remaining_billable = billable_qty - billable_consumed
                if remaining_billable > 0:
                    base_label = rec.protocol_id.name or product.display_name or "Node"
                    placeholder_label = f"{base_label}-{(primary_node.node_name or primary_node.node_identifier or 'node')}" if primary_node else base_label
                    for idx in range(1, remaining_billable + 1):
                        line_commands.append((0, 0, {
                            'product_id': product.id,
                            'quantity': 1,
                            'price_unit': price_unit,
                            'name': f"{placeholder_label} - Reserved Slot {idx}",
                            'discount': discount,
                            'discount_id': rec.discount_id.id if rec.discount_id else False,
                            'discount_code': rec.discount_id.code if rec.discount_id else '',
                            'node_id': False,
                        }))

                invoice_vals = {
                    'move_type': 'out_invoice',
                    'partner_id': partner.id,
                    'invoice_date': fields.Date.today(),
                    'subscription_id': rec.id,
                    'node_id': primary_node.id if primary_node else False,
                    'currency_id': currency.id,
                    'company_id': billing_company.id,
                    'journal_id': journal.id,
                    'invoice_line_ids': line_commands,
                }
                invoice = rec.env['account.move'].with_company(billing_company).sudo().create(invoice_vals)
                invoice.action_post()
            # 🔹 PRORATED INVOICE CREATION
            elif action == 'prorated_charge_for_qty_increase':
                # Calculate prorated amount (example logic)
                # You can replace this with your own prorating formula
                prorated_amount = rec.pending_quantity_prorated_amount

                prorated_amount = rec.pending_quantity_prorated_amount
                discount_amount = rec.discount_amount or 0.0
                original_price = rec.original_price or product.lst_price
                price_unit = original_price
                if rec.discount_id:
                    discount = rec.discount_id.discount_value
                else:
                    discount = (rec.discount_amount / rec.original_price) * 100 if rec.original_price else 0.0

                prorated_invoice_vals = {
                    'move_type': 'out_invoice',
                    'partner_id': partner.id,
                    'currency_id': currency.id,
                    'invoice_date': fields.Date.today(),
                    'subscription_id': rec.id,
                    'node_id': primary_node.id if primary_node else False,
                    'company_id': billing_company.id,
                    'journal_id': journal.id,
                    'invoice_origin': f"Prorated Charge - {rec.stripe_subscription_id or ''}",
                    'invoice_line_ids': [(0, 0, {
                        'product_id': product.id,
                        'quantity': 1,
                        'price_unit': prorated_amount,
                        'name': f"{rec.protocol_id.name}-{(primary_node.node_name if primary_node else '')}",
                        'discount': discount,
                    })],
                }
                primary_node.sudo().update({'state': 'provisioning'})

                print(prorated_invoice_vals, '-prorated_invoice_vals')

                prorated_invoice = rec.env['account.move'].with_company(prorated_invoice_vals['company_id']).sudo().create(prorated_invoice_vals)
                prorated_invoice.action_post()

                invoice = prorated_invoice

            # Register Stripe Payment (mark as Paid)
            payment_vals = {
                'payment_type': 'inbound',
                'partner_type': 'customer',
                'subscription_id':rec.id,
                'partner_id': partner.id,
                'amount': invoice.amount_total,
                'payment_method_id': rec.env.ref('account.account_payment_method_manual_in').id,
                'journal_id': rec.env['account.journal'].search([('type', '=', 'bank')], limit=1).id,
                'date': fields.Date.today(),
                'memo': f"Stripe Payment - {rec.stripe_subscription_id or ''}",
                'currency_id': invoice.currency_id.id,
            }
            payment = rec.env['account.payment'].sudo().create(payment_vals)
            payment.action_post()
            rec._register_payment(payment)
            receivable_lines = (payment.move_id.line_ids + invoice.line_ids).filtered(
                lambda line: line.account_id.account_type == 'asset_receivable' and not line.reconciled
            )
            if receivable_lines:
                receivable_lines.reconcile()
            invoice.matched_payment_ids = [(4, payment.id)]
            create_date_local = fields.Datetime.context_timestamp(rec, rec.create_date)
            today_local = fields.Datetime.context_timestamp(rec, datetime.utcnow())
            is_new_subscription = create_date_local.date() == today_local.date()
            if not is_new_subscription:
                try:
                    invoice_url = invoice.get_portal_url()
                except Exception:
                    invoice_url = False
                renewal_context = rec._prepare_subscription_renewal_email_context(
                    invoice=invoice,
                    payment=payment,
                    invoice_url=invoice_url,
                )
                send_subscription_email(
                    rec.env,
                    'subscription_renewal',
                    record=rec,
                    context=renewal_context,
                )
            else:
                if action != 'prorated_charge_for_qty_increase':
                    admin_recipients = rec._admin_recipient_payload()
                    send_subscription_email(
                        rec.env,
                        'new_subscription_admin',
                        record=rec,
                        context={
                            'userDetails': {
                                'name': rec.customer_name.name,
                                'buyer_email_id': rec.customer_name.email,
                                'plan_type': rec.sub_plan_id.name,
                                'protocol': rec.protocol_id.name,
                                'start_date': format_date(rec.env, (rec.stripe_start_date.date() if isinstance(rec.stripe_start_date, datetime) else rec.stripe_start_date) or rec.start_date) if rec.stripe_start_date or rec.start_date else '',
                                'subscription_type':rec.subscription_type,
                                'end_date': format_date(rec.env, (rec.stripe_end_date.date() if isinstance(rec.stripe_end_date, datetime) else rec.stripe_end_date) or rec.end_date) if rec.stripe_end_date or rec.end_date else '',
                                'amount': f"{((original_price - discount_amount) or 0.0):.2f}",
                                'currency': (invoice.currency_id.symbol or invoice.currency_id.name) if invoice.currency_id else '' or '',
                            },
                            'admin_email_cc': ",".join(admin_recipients.get('cc', [])) if admin_recipients.get('cc') else "",
                        },
                        email_to=",".join(admin_recipients.get('to', [])),
                        email_cc=",".join(admin_recipients.get('cc', [])) if admin_recipients.get('cc') else None,
                    )
                    primary_node.sudo().update({'state': 'provisioning'})
                else:
                    try:
                        invoice_url = invoice.get_portal_url()
                    except Exception:
                        invoice_url = False
                    renewal_context = rec._prepare_subscription_renewal_email_context(
                        invoice=invoice,
                        payment=payment,
                        invoice_url=invoice_url,
                    )
                    send_subscription_email(
                        rec.env,
                        'subscription_renewal',
                        record=rec,
                        context=renewal_context,
                    )
            payment_category = 'new' if is_new_subscription else 'renewal'
            payment_category_label = 'New Subscription Payment' if payment_category == 'new' else 'Renewal Payment'

            payment_category = 'new' if is_new_subscription else 'renewal'
            payment_category_label = 'New Subscription Payment' if payment_category == 'new' else 'Renewal Payment'
            admin_recipients = rec._admin_recipient_payload()
            send_subscription_email(
                rec.env,
                'payment_success_admin',
                record=rec,
                context={
                    "stripe_invoice_url": "",
                    "invoice": invoice,
                    "payment": payment,
                    "payment_category": payment_category,
                    "payment_category_label": payment_category_label,
                    "admin_email_cc": ",".join(admin_recipients.get('cc', [])) if admin_recipients.get('cc') else "",
                },
                email_to=",".join(admin_recipients.get('to', [])),
                email_cc=",".join(admin_recipients.get('cc', [])) if admin_recipients.get('cc') else None,
            )
            try:
                invoice.action_send_email_invoice()
                if rec.state == 'provisioning' and action != 'prorated_charge_for_qty_increase':
                    # Send provisioning mail (now idempotent)
                    rec.send_provisioning_mail()
                    
            except Exception as e:
                print('---------------------462',str(e))
            rec.discount_amount = 0.0
            return invoice
    def action_process_checkout_session(self, session_data):
        """Process a completed Stripe checkout session locally to speed up state transitions.
        This logic should mirror what is done in the webhook handler.
        """
        self.ensure_one()
        metadata = session_data.get('metadata', {}) or {}
        subscription_id = session_data.get('subscription')
        customer_id = session_data.get('customer')
        action = metadata.get('action')

        _logger.info("Processing checkout session %s for subscription %s (action: %s)", session_data.get('id'), self.id, action)

        if action == 'prorated_charge_for_qty_increase':
            # Handle one-time prorated charge for quantity increase
            discount_code = None
            discount_amount = 0
            
            # Method 1 → session.discounts
            discounts = session_data.get("discounts", [])
            if discounts:
                discount_code = discounts[0].get("promotion_code")
            
            # Method 2 → total_details.amount_discount
            total_details = session_data.get("total_details", {})
            if total_details:
                discount_amount = total_details.get("amount_discount", 0)
                if discount_amount:
                    discount_amount = discount_amount / 100.0

            charge_id = metadata.get('charge_id')
            
            # We need to perform the actual quantity increase in Stripe too
            # but usually the webhook handler handles the heavy lifting.
            # Here we just sync the Odoo state if possible.
            
            if self.pending_quantity_increase:
                new_qty = self.quantity + self.pending_quantity_increase
                self.sudo().write({
                    "pending_quantity_paid": True,
                    'quantity': new_qty,
                    'discount_amount': discount_amount,
                    'discount_code': discount_code,
                })
                
            if charge_id:
                try:
                    charge = self.env['subscription.prorated.charge'].sudo().browse(int(charge_id))
                    if charge.exists() and charge.state == 'draft':
                        # Match webhook logic: modify Stripe subscription quantity
                        stripe_secret_key = self.env['ir.config_parameter'].sudo().get_param('stripe_secret_key')
                        if stripe_secret_key:
                            stripe.api_key = stripe_secret_key
                            stripe_sub = stripe.Subscription.retrieve(self.stripe_subscription_id)
                            if stripe_sub and stripe_sub.get('items') and stripe_sub['items'].get('data'):
                                item = stripe_sub['items']['data'][0]
                                new_qty = item['quantity'] + self.pending_quantity_increase
                                stripe.Subscription.modify(
                                    stripe_sub['id'],
                                    items=[{
                                        'id': item['id'],
                                        'quantity': new_qty
                                    }],
                                    proration_behavior='none'
                                )
                        charge.sudo().write({"state": "paid"})
                except Exception:
                    _logger.exception("Failed to update prorated charge or Stripe subscription %s", charge_id)

            # Trigger invoice creation if not already done
            try:
                queue_env = self.env['subscription.invoice.queue'].sudo()
                if not queue_env.search([('subscription_id', '=', self.id), ('stripe_event_id', '=', session_data.get('id'))]):
                    queue_env.create({
                        'subscription_id': self.id,
                        'stripe_event_id': session_data.get('id'),
                        'action': 'prorated_charge_for_qty_increase',
                    })
            except Exception:
                # If queue fails, try direct invoice creation as fallback
                if not self.invoice_ids.filtered(lambda i: i.invoice_origin and 'Prorated' in i.invoice_origin):
                    self.create_invoice(action='prorated_charge_for_qty_increase')
            
            return True

        # Normal subscription checkout
        discount_id = metadata.get('discount_id')
        if discount_id:
            try:
                discount = self.env['subscription.discount'].sudo().browse(int(discount_id))
                if discount.exists():
                    discount.apply_discount()
            except Exception:
                _logger.exception("Failed to record discount usage for %s", discount_id)

        # Update subscription state to provisioning
        if self.state in ('draft', 'requested', 'provisioning'):
            if self.state != 'provisioning':
                self.sudo().write({
                    'state': 'provisioning',
                    "stripe_subscription_id": subscription_id,
                    'stripe_status': 'active',
                    'subscribed_on': fields.Datetime.now(),
                })
            
            # Ensure primary node is also set to provisioning
            primary_node = self.get_primary_node()
            if primary_node and primary_node.state == 'draft':
                primary_node.sudo().write({'state': 'provisioning'})
                _logger.info("Synced primary node %s to provisioning for subscription %s", primary_node.id, self.id)

            self.notify_customer_provisioning_started()
        
        return True

    def _prepare_subscription_renewal_email_context(self, invoice=None, payment=None, invoice_url=None):
        self.ensure_one()
        invoice_record = invoice[:1] if invoice else self.invoice_ids[:1] or self._get_latest_invoice()
        payment_record = payment[:1] if payment else (self.payment_ids[:1] if self.payment_ids else False)

        currency = False
        if payment_record and payment_record.currency_id:
            currency = payment_record.currency_id
        elif invoice_record and invoice_record.currency_id:
            currency = invoice_record.currency_id
        elif self.company_id:
            currency = self.company_id.currency_id

        amount_value = 0.0
        if payment_record:
            amount_value = payment_record.amount or 0.0
        elif invoice_record:
            amount_value = invoice_record.amount_total or 0.0

        currency_symbol = ''
        if currency:
            currency_symbol = currency.symbol or currency.name or ''

        formatted_amount = f"{amount_value:.2f}"
        if currency_symbol:
            formatted_amount = ("%s %s" % (currency_symbol, f"{amount_value:.2f}")).strip()

        invoice_url_value = invoice_url
        if invoice_record and not invoice_url_value:
            try:
                invoice_url_value = invoice_record.get_portal_url()
            except Exception:
                invoice_url_value = False

        payment_reference = 'N/A'
        if payment_record:
            payment_reference = payment_record.name or payment_record.ref or 'N/A'

        customer_name = self.customer_name.name or 'there'
        customer_email = self.customer_name.email or ''
        next_billing_display = self.stripe_end_date or 'TBA'

        renewal_ctx = {
            'customer_name': customer_name,
            'invoice': invoice_record or False,
            'invoice_display_name': invoice_record.display_name if invoice_record else 'N/A',
            'payment': payment_record or False,
            'payment_reference': payment_reference,
            'formatted_amount': formatted_amount,
            'amount_value': amount_value,
            'currency_symbol': currency_symbol,
            'invoice_url': invoice_url_value or False,
            'next_billing_display': next_billing_display,
            'email_to': customer_email,
        }

        base_context = {
            'subscription_renewal_ctx': renewal_ctx,
            'invoice': invoice_record or False,
            'payment': payment_record or False,
            'stripe_invoice_url': invoice_url_value or False,
            'customer_name': customer_name,
        }
        return base_context

    def _prepare_subscription_payment_failed_context(
        self,
        *,
        failure_reason=None,
        hosted_invoice_url=None,
        invoice_payload=None,
    ):
        """Build the template context consumed by payment failure emails."""

        self.ensure_one()
        invoice_payload = invoice_payload or {}

        def _coerce_date(value):
            if not value:
                return None
            if isinstance(value, datetime):
                return value.date()
            if isinstance(value, date):
                return value
            try:
                return fields.Date.from_string(value)
            except Exception:
                try:
                    return datetime.fromtimestamp(int(value)).date()
                except Exception:  # pragma: no cover - fallback
                    return None

        due_date_value = (
            _coerce_date(invoice_payload.get('due_date'))
            or _coerce_date(invoice_payload.get('period_end'))
            or _coerce_date(invoice_payload.get('created'))
        )
        if not due_date_value:
            next_payment = self.next_payment_date or self.stripe_end_date
            if isinstance(next_payment, datetime):
                due_date_value = next_payment.date()
            else:
                due_date_value = next_payment

        due_date_display = ''
        if due_date_value:
            try:
                due_date_display = format_date(self.env, due_date_value)
            except Exception:  # pragma: no cover - fallback formatting
                due_date_display = fields.Date.to_string(due_date_value)

        grace_deadline = _coerce_date(invoice_payload.get('grace_period_end')) or due_date_value
        grace_deadline_display = ''
        if grace_deadline:
            try:
                grace_deadline_display = format_date(self.env, grace_deadline)
            except Exception:  # pragma: no cover - fallback formatting
                grace_deadline_display = fields.Date.to_string(grace_deadline)

        currency = self.currency_id or self.company_id.currency_id or self.env.company.currency_id
        currency_code = (invoice_payload.get('currency') or (currency and currency.name) or '').upper()
        Currency = self.env['res.currency'].sudo()
        if currency_code:
            currency = Currency.search([('name', '=', currency_code)], limit=1) or currency

        amount_raw = invoice_payload.get('amount_due')
        if amount_raw is None:
            amount_raw = invoice_payload.get('amount_remaining')
        if amount_raw is None:
            amount_raw = invoice_payload.get('total')
        amount_value = self.price or 0.0
        if amount_raw is not None:
            try:
                amount_value = float(amount_raw)
            except Exception:  # pragma: no cover
                amount_value = float(self.price or 0.0)
            if invoice_payload.get('currency'):
                amount_value = amount_value / 100.0

        formatted_amount = f"{amount_value:.2f}"
        if currency:
            try:
                formatted_amount = formatLang(self.env, amount_value, currency_obj=currency)
            except Exception:  # pragma: no cover - fallback formatting
                formatted_amount = "%s %.2f" % ((currency.symbol or currency.name or ''), amount_value)

        customer_email = self.customer_name.email or ''
        hosted_url = hosted_invoice_url or invoice_payload.get('hosted_invoice_url') or self.hosted_invoice_url
        failure_message = (
            failure_reason
            or (invoice_payload.get('last_payment_error') or {}).get('message')
            or invoice_payload.get('failure_reason')
            or _('Zeeve was unable to process the payment method on file.')
        )

        retry_message = _('Zeeve will retry the charge automatically shortly.')
        next_attempt = invoice_payload.get('next_payment_attempt')
        if next_attempt:
            try:
                retry_dt = datetime.fromtimestamp(int(next_attempt))
                retry_message = _(
                    'Zeeve will retry the charge on %s.'
                ) % format_date(self.env, retry_dt.date())
            except Exception:  # pragma: no cover
                retry_message = _('Zeeve will retry the charge again soon.')

        return {
            'email_to': customer_email,
            'customer_email': customer_email,
            'customer_name': self.customer_name.display_name or self.customer_name.name or 'there',
            'protocol_name': self.protocol_id.name or self.subscription_type or self.name,
            'plan_name': self.sub_plan_id.name or self.name,
            'next_billing_date': due_date_value,
            'due_date_display': due_date_display,
            'grace_deadline': grace_deadline,
            'grace_deadline_display': grace_deadline_display or 'the grace period',
            'suspension_deadline': grace_deadline,
            'invoice_url': hosted_url,
            'hosted_invoice_url': hosted_url,
            'stripe_invoice_id': invoice_payload.get('id'),
            'invoice_number': invoice_payload.get('number'),
            'error_message': failure_message,
            'retry_message': retry_message,
            'amount_value': amount_value,
            'formatted_amount': formatted_amount,
            'currency_symbol': currency and (currency.symbol or currency.name) or '',
            'today_date': fields.Date.context_today(self),
        }

    def send_payment_failed_notifications(
        self,
        *,
        failure_reason=None,
        hosted_invoice_url=None,
        invoice_payload=None,
    ):
        """Dispatch customer/admin payment failure templates."""

        for subscription in self:
            ctx_payload = subscription._prepare_subscription_payment_failed_context(
                failure_reason=failure_reason,
                hosted_invoice_url=hosted_invoice_url,
                invoice_payload=invoice_payload,
            )
            if not ctx_payload:
                continue

            try:
                send_subscription_email(
                    subscription.env,
                    'subscription_management.mail_template_subscription_payment_failed',
                    record=subscription,
                    context={'subscription_payment_failed_ctx': ctx_payload},
                    email_to=ctx_payload.get('email_to'),
                )
            except Exception:  # pragma: no cover - log & continue
                _logger.exception(
                    'Failed to send subscription payment failure mail | subscription=%s',
                    subscription.id,
                )

            admin_recipients = subscription._admin_recipient_payload()
            admin_to = ctx_payload.get('admin_email_to') or ','.join(admin_recipients.get('to', []))
            if not admin_to:
                admin_to = subscription.company_id.email or subscription.env.user.company_id.email or ''
            if not admin_to:
                continue

            admin_ctx = dict(ctx_payload)
            admin_ctx['email_to'] = admin_to
            admin_kwargs = {}
            if admin_recipients.get('cc'):
                admin_kwargs['email_cc'] = ','.join(admin_recipients['cc'])
            try:
                send_subscription_email(
                    subscription.env,
                    'subscription_management.mail_template_subscription_payment_failed_admin',
                    record=subscription,
                    context={'subscription_payment_failed_ctx': admin_ctx},
                    email_to=admin_to,
                    **admin_kwargs,
                )
            except Exception:  # pragma: no cover
                _logger.exception(
                    'Failed to send subscription payment failure admin mail | subscription=%s',
                    subscription.id,
                )

    def _prepare_subscription_manual_payment_context(
        self,
        *,
        hosted_invoice_url=None,
        invoice_payload=None,
    ):
        """Build the template context for manual-payment-required emails."""

        ctx_payload = self._prepare_subscription_payment_failed_context(
            failure_reason=None,
            hosted_invoice_url=hosted_invoice_url,
            invoice_payload=invoice_payload,
        )
        if not ctx_payload:
            return {}

        reason_label = _('autopay disabled')
        if self.stripe_status == 'paused':
            reason_label = _('subscription paused')

        ctx_payload.update({
            'error_message': _('Automatic payment is not active for this subscription.'),
            'retry_message': _('Automatic retry is disabled for this invoice. Please complete payment manually.'),
            'manual_payment_reason': reason_label,
        })
        return ctx_payload

    def send_manual_payment_notifications(
        self,
        *,
        hosted_invoice_url=None,
        invoice_payload=None,
    ):
        """Dispatch customer/admin manual payment required templates."""

        for subscription in self:
            ctx_payload = subscription._prepare_subscription_manual_payment_context(
                hosted_invoice_url=hosted_invoice_url,
                invoice_payload=invoice_payload,
            )
            if not ctx_payload:
                continue

            try:
                send_subscription_email(
                    subscription.env,
                    'subscription_management.mail_template_subscription_manual_payment_required',
                    record=subscription,
                    context={'subscription_manual_payment_ctx': ctx_payload},
                    email_to=ctx_payload.get('email_to'),
                )
            except Exception:  # pragma: no cover
                _logger.exception(
                    'Failed to send subscription manual payment mail | subscription=%s',
                    subscription.id,
                )

            admin_recipients = subscription._admin_recipient_payload()
            admin_to = ctx_payload.get('admin_email_to') or ','.join(admin_recipients.get('to', []))
            if not admin_to:
                admin_to = subscription.company_id.email or subscription.env.user.company_id.email or ''
            if not admin_to:
                continue

            admin_ctx = dict(ctx_payload)
            admin_ctx['email_to'] = admin_to
            admin_kwargs = {}
            if admin_recipients.get('cc'):
                admin_kwargs['email_cc'] = ','.join(admin_recipients['cc'])
            try:
                send_subscription_email(
                    subscription.env,
                    'subscription_management.mail_template_subscription_manual_payment_required_admin',
                    record=subscription,
                    context={'subscription_manual_payment_ctx': admin_ctx},
                    email_to=admin_to,
                    **admin_kwargs,
                )
            except Exception:  # pragma: no cover
                _logger.exception(
                    'Failed to send subscription manual payment admin mail | subscription=%s',
                    subscription.id,
                )

    def notify_unsubscribe_request(self, *, requested_by=None, node_identifier,reason=None):
        """Notify admins that the customer requested to unsubscribe."""

        template = self.env.ref(
            "subscription_management.mail_template_subscription_unsubscribe_request_admin",
            raise_if_not_found=False,
        )
        if not template:
            return False

        for subscription in self:
            partner = subscription.customer_name or requested_by and requested_by.partner_id
            if not partner:
                continue
            admin_payload = subscription._admin_recipient_payload()
            admin_to = ",".join(admin_payload.get("to", []))
            admin_cc = ",".join(admin_payload.get("cc", []))
            if not admin_to:
                cfg = subscription.env['zeeve.config'].sudo().search([], limit=1)
                admin_to = (cfg and cfg.admin_emails) or subscription.env.company.email or "support@zeeve.io"

            request_dt = fields.Datetime.context_timestamp(subscription, fields.Datetime.now())
            if request_dt:
                request_display = format_date(subscription.env, request_dt.date())
            else:
                request_display = format_date(subscription.env, fields.Date.context_today(subscription))

            base_url = subscription.env['ir.config_parameter'].sudo().get_param('backend_url') or subscription.env['ir.config_parameter'].sudo().get_param('web.base.url')
            record_url = False
            if base_url:
                base_url = base_url.rstrip('/')
                record_url = f"{base_url}/web#id={subscription.id}&model=subscription.subscription&view_type=form"
            node = self.env['subscription.node'].sudo().search([('node_identifier','=',node_identifier)],limit=1)
            node.update({'state':'cancellation_requested'})
            ctx_payload = {
                "customer_name": partner.display_name or partner.name or "Customer",
                "customer_email": partner.email or partner.email_formatted or '',
                "plan_name": subscription.sub_plan_id.name or subscription.name,
                "protocol_name": subscription.protocol_id.name or subscription.subscription_type or '',
                "subscription_type": subscription.subscription_type or 'Subscription',
                "identifier":  subscription.subscription_ref or subscription.name,
                "request_date": request_display,
                "reason": reason or '',
                "is_rollup": False,
                "node_identifier":node_identifier,
                "record_url": record_url,
            }
            send_ctx = {
                "unsubscribe_request_ctx": ctx_payload,
                "email_to": admin_to,
            }
            email_vals = {"email_to": admin_to}
            if admin_cc:
                email_vals["email_cc"] = admin_cc
            template.sudo().with_context(send_ctx).send_mail(
                partner.id,
                force_send=True,
                email_values=email_vals,
                raise_exception=False,
            )
        return True

            

    def _send_cancellation_notifications(
        self,
        cancellation_reason: str | None = None,
        cancellation_date: fields.Date | None = None,
        tenant: dict | None = None,
        support_info: dict | None = None,
        notification_mode: str = 'cancellation',
        quantity_delta: int | None = None,
        updated_quantity: int | None = None,
        previous_quantity: int | None = None,
    ) -> None:
        """Send customer and admin cancellation mails for each subscription."""

        for subscription in self:
            reason_value = cancellation_reason or subscription.reason or None
            date_value = cancellation_date or fields.Date.context_today(subscription)
            send_subscription_cancellation_emails(
                subscription,
                cancellation_reason=reason_value,
                cancellation_date=date_value,
                tenant=tenant,
                support_info=support_info,
                notification_mode=notification_mode,
                quantity_delta=quantity_delta,
                updated_quantity=updated_quantity,
                previous_quantity=previous_quantity,
            )

    def action_mail_test(self):
        for rec in self:
            invoice = rec.invoice_ids[:1]
            payment = rec.payment_ids[:1] if rec.payment_ids else False
            invoice_url = False
            if invoice:
                try:
                    invoice_url = invoice.get_portal_url()
                except Exception:  # pragma: no cover - defensive fallback
                    invoice_url = False
            renewal_context = rec._prepare_subscription_renewal_email_context(
                invoice=invoice,
                payment=payment,
                invoice_url=invoice_url,
            )
            send_subscription_email(
                rec.env,
                'subscription_renewal',
                record=rec,
                context=renewal_context,
            )
            # for new subscription purchased to admin
            # send_subscription_email(
            #     rec.env,
            #     'new_subscription_admin',
            #     record=rec,
            #     context={
            #         'userDetails': {
            #             'name': rec.customer_name.name,
            #             'buyer_email_id': rec.customer_name.email,
            #             'plan_type': rec.sub_plan_id.name,
            #             'protocol': rec.protocol_id.name,
            #             'start_date': rec.stripe_start_date,
            #             'subscription_type':rec.subscription_type,
            #             'end_date': rec.stripe_end_date,
            #             'amount': rec.price,}
            #     },
            # )
            # for new subscription purchased to user
            # send_subscription_email(
            #     rec.env,
            #     'node_subscription',
            #     record=rec,
            #     context={
            #         'plan_details': {
            #             'name': rec.customer_name.name,
            #             'buyer_email_id': rec.customer_name.email,
            #             'plan_name': rec.sub_plan_id.name,
            #             'protocol_name': rec.protocol_id.name,
            #             'subscription_start_date': rec.stripe_start_date,
            #             'node_type':rec.subscription_type,
            #             'subscription_end_date': rec.stripe_end_date,
            #             'ready_eta':'TBA',
            #             'subscription_cost': rec.price,}
            #     },
            # )
            # for node provisioning complete to user
            # send_subscription_email(
            #     rec.env,
            #     'node_provisioning_complete',
            #     record=rec,
            #     context={
            #         'plan_details': {
            #             'name': rec.customer_name.name,
            #             'buyer_email_id': rec.customer_name.email,
            #             'plan_name': rec.sub_plan_id.name,
            #             'protocol_name': rec.protocol_id.name,
            #             'subscription_start_date': rec.stripe_start_date,
            #             'node_type':rec.subscription_type,
            #             'subscription_end_date': rec.stripe_end_date,
            #             'syncingCompletionEta':'TBA',
            #             'subscription_cost': rec.price,}
            #     },
            # )
            # for node ready to user
            # send_subscription_email(
            #     rec.env,
            #     'node_ready',
            #     record=rec,
            #     context={
            #         'plan_details': {
            #             'name': rec.customer_name.name,
            #             'buyer_email_id': rec.customer_name.email,
            #             'plan_name': rec.sub_plan_id.name,
            #             'protocol_name': rec.protocol_id.name,
            #             'subscription_start_date': rec.stripe_start_date,
            #             'node_type':rec.subscription_type,
            #             'subscription_end_date': rec.stripe_end_date,
            #             'syncingCompletionEta':'TBA',
            #             'subscription_cost': rec.price,
            #             'enabled_endpoints':{
            #                 'http':'TBA',
            #                 'ws':'TBA'
            #             }
            #         }
            #     },
            # )
            # for validator node subscription to user
            # send_subscription_email(
            #     rec.env,
            #     'validator_node_subscription',
            #     record=rec,
            #     context={
            #         'mailType': 'subscription',
            #         'plan_details': {
            #             'name': rec.customer_name.name,
            #             'buyer_email_id': rec.customer_name.email,
            #             'plan_name': rec.sub_plan_id.name,
            #             'protocol_name': rec.protocol_id.name,
            #             'subscription_start_date': rec.stripe_start_date,
            #             'node_type':rec.subscription_type,
            #             'subscription_end_date': rec.stripe_end_date,
            #             'ready_eta':'TBA',
            #             'subscription_cost': rec.price,
            #             'automation_input_required':False,
            #             'automation_input_filled':False,}
            #         },
            # )
            # for validator node provisioning complete to user
            # send_subscription_email(
            #     rec.env,
            #     'validator_provisioning_complete',
            #     record=rec,
            #     context={
            #         'mailType': 'subscription',
            #         'plan_details': {
            #             'name': rec.customer_name.name,
            #             'buyer_email_id': rec.customer_name.email,
            #             'plan_name': rec.sub_plan_id.name,
            #             'protocol_name': rec.protocol_id.name,
            #             'subscription_start_date': rec.stripe_start_date,
            #             'node_type':rec.subscription_type,
            #             'subscription_end_date': rec.stripe_end_date,
            #             'ready_eta':'TBA',
            #             'subscription_cost': rec.price,
            #             'beam_based':True
            #             }
            #         },
            # )
    @api.onchange('sub_plan_id')
    def onchange_subscription_plan(self):
        if self.sub_plan_id:
            date = datetime.today()
            if self.sub_plan_id.trial_period:
                if self.sub_plan_id.trial_duration_unit == 'day':
                    date = date + \
                        relativedelta(days=self.sub_plan_id.trial_duration)
                if self.sub_plan_id.trial_duration_unit == 'month':
                    date = date + \
                        relativedelta(months=self.sub_plan_id.trial_duration)
                if self.sub_plan_id.trial_duration_unit == 'year':
                    date = date + \
                        relativedelta(years=self.sub_plan_id.trial_duration)
                if self.sub_plan_id.trial_duration_unit == 'week':
                    date = date + \
                        timedelta(weeks=self.sub_plan_id.trial_duration)
                if self.sub_plan_id.trial_duration_unit == 'hour':
                    date = date + \
                        timedelta(hours=self.sub_plan_id.trial_duration)
            self.trial_period = self.sub_plan_id.trial_period
            self.trial_duration_unit = self.sub_plan_id.trial_duration_unit
            self.trial_duration = self.sub_plan_id.trial_duration
            self.num_billing_cycle = self.sub_plan_id.num_billing_cycle
            self.duration = self.sub_plan_id.duration
            self.unit = self.sub_plan_id.unit
            self.start_date = date.date()
            # self.next_payment_date = date
            if not self.sub_plan_id.override_product_price:
                self.price = self.sub_plan_id.plan_amount
            if self.sub_plan_id.never_expires:
                self.never_expires = self.sub_plan_id.never_expires

    def renew_subscription(self):
        for current_rec in self:
            if current_rec.state in ['expired', 'close']:
                current_rec.create_subscription()
                wizard_id = self.env['subscription.message.wizard'].create(
                    {'message': 'Subscription Renewed.'})
                return {
                    'name': _("Message"),
                    'view_mode': 'form',
                    'view_id': False,
                    'view_type': 'form',
                    'res_model': 'subscription.message.wizard',
                    'res_id': wizard_id.id,
                    'type': 'ir.actions.act_window',
                    'nodestroy': True,
                    'target': 'new',
                }
            return False

    def create_subscription(self):
        date = datetime.today().date()
        res = self.copy()
        res.start_date = date
        res.parent_subscription_id = self.id
        self.state = "renewed"
        self.send_subscription_mail()
        return res

    def action_alert_mail(self):
        renewal_days = self.env["ir.default"]._get(
            'res.config.settings', 'renewal_days')
        
        template_id = self.env.ref(
            'subscription_management.subscription_management_alert_mail_template')
        subscription = self.env['subscription.subscription'].sudo().search([])
        for record in subscription:
            if record.end_date:
                if (((record.end_date-datetime.today().date()).days <= renewal_days) and record.state =='in_progress' and record.alert_call):
                    record.alert_call=False
                    template_id.send_mail(record.id, force_send=True)

    def _renewal_days(self):
        for day in self:
            day.renewal_days = self.env["ir.default"]._get(
                'res.config.settings', 'renewal_days')

    # Stripe Integration Methods
    def _get_stripe_secret_key(self):
        stripe_key = self.env['ir.config_parameter'].sudo().get_param('stripe_secret_key')
        if not stripe_key:
            raise UserError(_('Stripe secret key is not configured.'))
        return stripe_key

    def _get_manual_invoice_due_days(self):
        raw_value = self.env['ir.config_parameter'].sudo().get_param(
            'stripe_manual_invoice_due_days',
            '7',
        )
        try:
            due_days = int(str(raw_value).strip())
        except (TypeError, ValueError):
            due_days = 7
        return max(due_days, 1)

    def _get_customer_default_payment_method(self, subscription):
        customer_id = subscription.stripe_customer_id or subscription.customer_name.stripe_customer_id
        if not customer_id:
            return False
        customer = stripe.Customer.retrieve(customer_id)
        invoice_settings = customer.get('invoice_settings') or {}
        return invoice_settings.get('default_payment_method') or False

    def _set_latest_draft_invoice_manual_collection(self, subscription, due_days):
        invoices = stripe.Invoice.list(subscription=subscription.stripe_subscription_id, limit=5)
        for invoice in invoices.auto_paging_iter():
            if invoice.get('status') != 'draft':
                continue
            stripe.Invoice.modify(
                invoice['id'],
                collection_method='send_invoice',
                days_until_due=due_days,
                auto_advance=True,
            )
            _logger.info(
                "Updated Stripe draft invoice %s to send_invoice for subscription %s",
                invoice['id'],
                subscription.stripe_subscription_id,
            )
            return True
        return False

    def enable_autopay(self):
        """Enable autopay for the subscription"""
        for subscription in self:
            if subscription.stripe_subscription_id:
                try:
                    stripe.api_key = self._get_stripe_secret_key()
                    modify_kwargs = {
                        'collection_method': 'charge_automatically',
                        'days_until_due': None,
                        'metadata': {'autopay_enabled': 'true'},
                    }
                    default_payment_method = subscription.stripe_payment_method_id
                    if not default_payment_method:
                        default_payment_method = self._get_customer_default_payment_method(subscription)
                    if default_payment_method:
                        modify_kwargs['default_payment_method'] = default_payment_method
                    stripe.Subscription.modify(
                        subscription.stripe_subscription_id,
                        **modify_kwargs,
                    )
                    subscription.write({'autopay_enabled': True})
                    _logger.info(f"Autopay enabled for subscription {subscription.name}")
                except Exception as e:
                    _logger.error(f"Failed to enable autopay for subscription {subscription.name}: {str(e)}")
                    raise UserError(_("Failed to enable autopay: %s") % str(e))
            else:
                subscription.write({'autopay_enabled': True})

    def disable_autopay(self):
        """Disable autopay for the subscription"""
        for subscription in self:
            if subscription.stripe_subscription_id:
                try:
                    stripe.api_key = self._get_stripe_secret_key()
                    due_days = self._get_manual_invoice_due_days()
                    stripe.Subscription.modify(
                        subscription.stripe_subscription_id,
                        collection_method='send_invoice',
                        days_until_due=due_days,
                        metadata={'autopay_enabled': 'false'},
                    )
                    draft_invoice_updated = self._set_latest_draft_invoice_manual_collection(subscription, due_days)
                    subscription.write({'autopay_enabled': False})
                    _logger.info(
                        "Autopay disabled for subscription %s; Stripe collection switched to send_invoice "
                        "(days_until_due=%s, draft_invoice_updated=%s)",
                        subscription.name,
                        due_days,
                        draft_invoice_updated,
                    )
                except Exception as e:
                    _logger.error(f"Failed to disable autopay for subscription {subscription.name}: {str(e)}")
                    raise UserError(_("Failed to disable autopay: %s") % str(e))
            else:
                subscription.write({'autopay_enabled': False})

    def cancel_stripe_subscription(self):
        """Cancel the subscription in Stripe or decrement quantity when multiple seats exist. Also handles Odoo-managed subscriptions."""
        for subscription in self:
            # Handle Odoo-managed subscriptions
            if subscription.is_odoo_managed:
                subscription.write({
                    'state': 'closed',
                })
                _logger.info("Odoo-managed subscription %s cancelled (state set to closed)", subscription.id)
                subscription._send_cancellation_notifications(
                    cancellation_reason=_("Subscription cancelled by user"),
                    notification_mode='full_cancellation'
                )
                continue

            if not subscription.stripe_subscription_id:
                continue

            try:
                stripe.api_key = self.env['ir.config_parameter'].sudo().get_param('stripe_secret_key')
                if not stripe.api_key:
                    raise UserError(_('Stripe secret key is not configured.'))

                current_qty = int(subscription.quantity or 1)
                if current_qty > 1:
                    # Reduce subscription quantity by one instead of cancelling entirely.
                    existing = stripe.Subscription.retrieve(subscription.stripe_subscription_id, expand=['items'])
                    items = (existing.get('items') or {}).get('data') or []
                    if not items:
                        raise UserError(_('Stripe subscription %s has no items to update.') % subscription.stripe_subscription_id)
                    target_item = items[0]
                    new_qty = current_qty - 1
                    try:
                        updated = stripe.Subscription.modify(
                            subscription.stripe_subscription_id,
                            items=[{
                                'id': target_item['id'],
                                'quantity': new_qty,
                            }],
                            proration_behavior='none',
                        )
                    except stripe.error.StripeError as err:
                        raise UserError(_('Failed to reduce Stripe subscription quantity: %s') % err) from err

                    new_original_price = (subscription.price or 0.0) * new_qty
                    write_vals = {
                        'quantity': new_qty,
                        'original_price': new_original_price,
                    }
                    if updated:
                        write_vals['stripe_status'] = updated.get('status') or subscription.stripe_status
                    subscription.write(write_vals)
                    subscription._send_cancellation_notifications(
                        cancellation_reason=_("Subscription quantity reduced from %s to %s") % (current_qty, new_qty),
                        notification_mode='quantity_reduction',
                        quantity_delta=new_qty - current_qty,
                        updated_quantity=new_qty,
                        previous_quantity=current_qty,
                    )
                    _logger.info(
                        "Reduced Stripe subscription %s quantity from %s to %s instead of cancelling",
                        subscription.stripe_subscription_id,
                        current_qty,
                        new_qty,
                    )
                    continue

                # Fallback to full cancellation when only one seat remains.
                res = stripe.Subscription.delete(subscription.stripe_subscription_id)
                subscription.write({
                    'stripe_status': res.get('status') if isinstance(res, dict) else 'canceled',
                })
                _logger.info("Stripe subscription %s canceled", subscription.stripe_subscription_id)
                subscription._send_cancellation_notifications()
            except UserError:
                raise
            except Exception as e:
                _logger.error("Failed to cancel Stripe subscription %s: %s", subscription.stripe_subscription_id, str(e))
                raise UserError(_("Failed to cancel subscription: %s") % str(e))

    def pause_stripe_subscription(self):
        """Pause the subscription in Stripe or for Odoo-managed billing."""
        for subscription in self:
            # Handle Odoo-managed subscriptions
            if subscription.is_odoo_managed:
                subscription.write({'state': 'in_grace'})
                _logger.info("Odoo-managed subscription %s paused (state set to in_grace)", subscription.id)
                continue

            if not subscription.stripe_subscription_id:
                subscription.write({'stripe_status': 'paused'})
                continue
            try:
                stripe.api_key = self._get_stripe_secret_key()
                stripe.Subscription.modify(
                    subscription.stripe_subscription_id,
                    pause_collection={'behavior': 'mark_uncollectible'}
                )
                subscription.write({'stripe_status': 'paused'})
                _logger.info("Stripe subscription %s paused", subscription.stripe_subscription_id)
            except Exception as e:
                _logger.error("Failed to pause Stripe subscription %s: %s", subscription.stripe_subscription_id, str(e))
                raise UserError(_("Failed to pause subscription: %s") % str(e))

    def resume_stripe_subscription(self):
        """Resume the subscription in Stripe or for Odoo-managed billing."""
        for subscription in self:
            # Handle Odoo-managed subscriptions
            if subscription.is_odoo_managed:
                subscription.write({'state': 'in_progress'})
                _logger.info("Odoo-managed subscription %s resumed (state set to in_progress)", subscription.id)
                continue

            if not subscription.stripe_subscription_id:
                subscription.write({'stripe_status': 'active'})
                continue
            try:
                stripe.api_key = self._get_stripe_secret_key()
                stripe.Subscription.modify(
                    subscription.stripe_subscription_id,
                    pause_collection=None
                )
                subscription.write({'stripe_status': 'active'})
                _logger.info("Stripe subscription %s resumed", subscription.stripe_subscription_id)
            except Exception as e:
                _logger.error("Failed to resume Stripe subscription %s: %s", subscription.stripe_subscription_id, str(e))
                raise UserError(_("Failed to resume subscription: %s") % str(e))

    def action_cancel_v2(self, reason=None):
        """Cancel an Odoo-managed subscription (V2)"""
        for subscription in self:
            if not subscription.is_odoo_managed:
                # If it's v1, we can call the existing v1 logic or just log
                _logger.warning("action_cancel_v2 called on a non-Odoo-managed subscription %s", subscription.id)
                continue

            subscription.write({
                'state': 'closed',
                'reason': reason or 'Cancelled by user via API',
            })
            _logger.info("Odoo-managed subscription %s cancelled (state set to closed)", subscription.id)
            
            # Trigger notifications (similar to V1)
            try:
                subscription._send_cancellation_notifications(
                    cancellation_reason=reason or _("Subscription cancelled by user"),
                    notification_mode='full_cancellation'
                )
            except Exception as e:
                _logger.error("Failed to send cancellation notifications for subscription %s: %s", subscription.id, str(e))

    def action_view_payment_logs(self):
        """Smart button action to display payment logs"""
        logs = self.mapped('payment_log_ids')
        action = self.env.ref('subscription_management.action_stripe_payment_log').read()[0]
        if len(logs) > 1:
            action['domain'] = [('id', 'in', logs.ids)]
        elif len(logs) == 1:
            form_view = [(self.env.ref('subscription_management.stripe_payment_log_form_view').id, 'form')]
            action['views'] = form_view + [(state, view) for state, view in action.get('views', []) if view != 'form']
            action['res_id'] = logs.id
        else:
            action = {'type': 'ir.actions.act_window_close'}
        return action

    @staticmethod
    def _amount_to_minor_units(amount):
        if amount in (None, False):
            return 0
        quantized = Decimal(str(amount)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
        return int(quantized * 100)

    def _apply_stripe_subscription_payload(self, stripe_subscription):
        """Apply key fields from a Stripe subscription payload to this record."""
        self.ensure_one()
        if not stripe_subscription:
            return False
        items = (stripe_subscription.get('items') or {}).get('data') or []
        period_data = items[0] if items else {}
        start_ts = period_data.get('current_period_start') or stripe_subscription.get('current_period_start')
        end_ts = period_data.get('current_period_end') or stripe_subscription.get('current_period_end')
        quantity = stripe_subscription.get('quantity') 

        def _naive(ts):
            if not ts:
                return False
            dt = datetime.fromtimestamp(ts, timezone.utc)
            return dt.replace(tzinfo=None)

        start_dt = _naive(start_ts)
        end_dt = _naive(end_ts)

        vals = {
            'stripe_status': stripe_subscription.get('status'),
            'stripe_start_date': start_dt,
            'stripe_end_date': end_dt,
            'current_term_start': start_dt,
            'current_term_end': end_dt,
            'stripe_subscription_id': stripe_subscription.get('id'),
            'stripe_payment_method_id': stripe_subscription.get('default_payment_method'),
            'autopay_enabled': stripe_subscription.get('metadata', {}).get('autopay_enabled', 'true') == 'true',
            'quantity': quantity or self.quantity
        }
        print('vals ===',vals)

        self.sudo().write(vals)
        return True

    def action_sync_with_stripe(self):
        """Manually fetch the latest Stripe subscription data."""
        self.ensure_one()
        if not self.stripe_subscription_id:
            raise UserError(_('This subscription is not linked to a Stripe subscription.'))
        secret = self.env['ir.config_parameter'].sudo().get_param('stripe_secret_key')
        if not secret:
            raise UserError(_('Stripe secret key is not configured.'))
        stripe.api_key = secret
        try:
            stripe_subscription = stripe.Subscription.retrieve(
                self.stripe_subscription_id,
                expand=['items'],
            )
        except stripe.error.StripeError as err:
            raise UserError(_('Failed to retrieve Stripe subscription: %s') % err) from err

        self._apply_stripe_subscription_payload(stripe_subscription)
        return True

    def action_check_recurring_payment(self):
        """
        Manually trigger a check/process for recurring payment.
        For Odoo-managed, it triggers action_charge_subscription.
        For Stripe-managed, it syncs with Stripe.
        """
        self.ensure_one()
        if self.is_odoo_managed:
            # For testing, we trigger charge regardless of next_payment_date if requested
            _logger.info("Manual recurring payment check triggered for Odoo-managed subscription %s", self.id)
            success = self.action_charge_subscription()
            if success:
                return {
                    'type': 'ir.actions.client',
                    'tag': 'display_notification',
                    'params': {
                        'title': _('Success'),
                        'message': _('Payment processed successfully and end date updated.'),
                        'type': 'success',
                        'sticky': False,
                    }
                }
            else:
                raise UserError(_("Payment processing failed. Please check logs."))
        else:
            _logger.info("Manual recurring payment check triggered for Stripe-managed subscription %s", self.id)
            self.action_sync_with_stripe()
            return {
                'type': 'ir.actions.client',
                'tag': 'display_notification',
                'params': {
                    'title': _('Sync Complete'),
                    'message': _('Subscription data synchronized with Stripe.'),
                    'type': 'info',
                    'sticky': False,
                }
            }

    def action_sync_stripe_trial_subscription(self):
        """Create or update the linked Stripe subscription in trial mode."""
        secret = self.env['ir.config_parameter'].sudo().get_param('stripe_secret_key')
        if not secret:
            raise UserError(_('Stripe secret key is not configured.'))
        stripe.api_key = secret

        for subscription in self:
            partner = subscription.customer_name
            if not partner:
                raise UserError(_('Subscription %s has no customer.') % subscription.name)

            customer_record = None
            customer_payment_method_id = False
            payment_method_ready = False
            customer_id = subscription.stripe_customer_id or getattr(partner, 'stripe_customer_id', False)
            if not customer_id:
                payload = {
                    'email': partner.email,
                    'name': partner.display_name,
                }
                try:
                    created_customer = stripe.Customer.create(**payload)
                except stripe.error.StripeError as err:
                    raise UserError(_('Failed to create Stripe customer: %s') % err) from err
                customer_id = created_customer.get('id')
                if not customer_id:
                    raise UserError(_('Stripe did not return a customer id.'))
                partner.sudo().write({'stripe_customer_id': customer_id})
                customer_record = created_customer
            else:
                try:
                    customer_record = stripe.Customer.retrieve(customer_id)
                except stripe.error.StripeError:
                    customer_record = None

            if customer_record:
                invoice_settings = customer_record.get('invoice_settings') or {}
                customer_payment_method_id = invoice_settings.get('default_payment_method') or False
                if not customer_payment_method_id and _DataImporterSubscriptionUtils:
                    customer_record, payment_method_ready = _DataImporterSubscriptionUtils._ensure_payment_method_ready(customer_record)
                    if customer_record:
                        invoice_settings = customer_record.get('invoice_settings') or {}
                        customer_payment_method_id = invoice_settings.get('default_payment_method') or False
                else:
                    payment_method_ready = bool(customer_payment_method_id)
            else:
                payment_method_ready = False

            if subscription.autopay_enabled and not payment_method_ready:
                customer_ref = None
                if customer_record:
                    if hasattr(customer_record, 'get'):
                        customer_ref = customer_record.get('id')
                    else:
                        customer_ref = getattr(customer_record, 'id', None)
                customer_ref = customer_ref or customer_id or partner.display_name
                raise UserError(_(
                    'Stripe customer %s has no default payment method. Please attach a payment method before syncing the trial subscription.'
                ) % customer_ref)

            plan = subscription.sub_plan_id
            if not plan:
                raise UserError(_('Subscription %s has no plan assigned.') % subscription.name)

            billing_cycle = subscription.payment_frequency or 'monthly'
            plan_cycle_amount = plan.amount_month if billing_cycle == 'monthly' else \
                plan.amount_quarter if billing_cycle == 'quarterly' else \
                plan.amount_year if billing_cycle == 'annually' else plan.plan_amount

            plan_price_id = plan.stripe_price_month_id if billing_cycle == 'monthly' else \
                plan.stripe_price_quarter_id if billing_cycle == 'quarterly' else \
                plan.stripe_price_year_id if billing_cycle == 'annually' else None

            currency = subscription.currency_id or self.env.company.currency_id
            required_amount = subscription.price or 0.0
            price_id = subscription.stripe_price_id
            created_price = False

            if price_id:
                try:
                    fetched_price = stripe.Price.retrieve(price_id)
                    fetched_amount = fetched_price.get('unit_amount') or 0
                    fetched_currency = fetched_price.get('currency')
                    if fetched_amount != self._amount_to_minor_units(required_amount) or fetched_currency != (currency.name or 'USD').lower():
                        price_id = False
                except stripe.error.StripeError:
                    price_id = False

            if not price_id:
                if not plan_cycle_amount or float_compare(plan_cycle_amount, required_amount, precision_digits=2) != 0:
                    plan_price_id = None
                else:
                    price_id = plan_price_id
                    created_price = False

            if not price_id:
                if not plan.stripe_product_id:
                    raise UserError(_('Plan %s has no Stripe product configured.') % plan.name)
                try:
                    created_price = stripe.Price.create(
                        product=plan.stripe_product_id,
                        currency=(currency.name or 'USD').lower(),
                        unit_amount=self._amount_to_minor_units(required_amount),
                        recurring={
                            'interval': 'year' if billing_cycle == 'annually' else 'month',
                            'interval_count': 3 if billing_cycle == 'quarterly' else 1,
                        },
                        metadata={
                            'odoo_subscription_id': subscription.id,
                            'subscription_id': subscription.id,
                            'protocol_id': plan.protocol_id.id if plan.protocol_id else '',
                            'subscription_type': subscription.subscription_type or '',
                        },
                    )
                except stripe.error.StripeError as err:
                    raise UserError(_('Failed to create Stripe price: %s') % err) from err
                price_id = created_price.get('id')

            if not price_id:
                raise UserError(_('Unable to determine a Stripe price for subscription %s.') % subscription.name)

            if created_price:
                subscription.stripe_price_id = price_id

            trial_dt = subscription.stripe_end_date or subscription.next_payment_date
            if not trial_dt:
                raise UserError(_('Subscription %s has no next billing date.') % subscription.name)
            if isinstance(trial_dt, str):
                trial_dt = fields.Datetime.from_string(trial_dt)
            if not isinstance(trial_dt, datetime):
                trial_dt = fields.Datetime.from_string(trial_dt)
            trial_dt = trial_dt if trial_dt.tzinfo else trial_dt.replace(tzinfo=timezone.utc)
            trial_ts = int(trial_dt.timestamp())
            now_ts = int(datetime.now(timezone.utc).timestamp())
            if trial_ts <= now_ts:
                trial_ts = now_ts + 60

            metadata = {
                'odoo_subscription_id': subscription.id,
                'subscription_id': subscription.id,
                'odoo_partner_id': partner.id,
                'subscription_plan_id': plan.id,
                'protocol_id': plan.protocol_id.id if plan.protocol_id else '',
                'subscripton_type': subscription.subscription_type or '',
                'zoho_subscription_id': subscription.zoho_subscription_id or '',
            }

            stripe_subscription = None
            if subscription.stripe_subscription_id:
                try:
                    existing = stripe.Subscription.retrieve(subscription.stripe_subscription_id, expand=['items'])
                except stripe.error.StripeError as err:
                    raise UserError(_('Failed to retrieve Stripe subscription %s: %s') % (subscription.stripe_subscription_id, err)) from err
                items = existing.get('items', {}).get('data', [])
                target_item = items[0] if items else None
                if not target_item:
                    raise UserError(_('Stripe subscription %s has no items.') % subscription.stripe_subscription_id)
                modify_kwargs = {
                    'items': [{
                        'id': target_item['id'],
                        'price': price_id,
                        'quantity': int(subscription.quantity or 1),
                    }],
                    'trial_end': trial_ts,
                    'metadata': metadata,
                }
                if customer_payment_method_id:
                    modify_kwargs['default_payment_method'] = customer_payment_method_id
                    modify_kwargs['payment_settings'] = {'save_default_payment_method': 'on_subscription'}
                try:
                    stripe_subscription = stripe.Subscription.modify(
                        subscription.stripe_subscription_id,
                        **modify_kwargs,
                    )
                except stripe.error.StripeError as err:
                    raise UserError(_('Failed to update Stripe subscription %s: %s') % (subscription.stripe_subscription_id, err)) from err
            else:
                payload = {
                    'customer': customer_id,
                    'items': [{
                        'price': price_id,
                        'quantity': int(subscription.quantity or 1),
                    }],
                    'trial_end': trial_ts,
                    'metadata': metadata,
                    'collection_method': 'charge_automatically',
                }
                if customer_payment_method_id:
                    payload['default_payment_method'] = customer_payment_method_id
                    payload['payment_settings'] = {'save_default_payment_method': 'on_subscription'}
                try:
                    stripe_subscription = stripe.Subscription.create(**payload)
                    subscription.skip_trial_invoice = True
                except stripe.error.StripeError as err:
                    raise UserError(_('Failed to create Stripe subscription: %s') % err) from err

            if not stripe_subscription:
                continue

            stripe_default_pm = stripe_subscription.get('default_payment_method') or customer_payment_method_id

            update_vals = {
                'stripe_subscription_id': stripe_subscription.get('id'),
                'stripe_status': stripe_subscription.get('status'),
                'stripe_customer_id': stripe_subscription.get('customer', customer_id),
                'stripe_price_id': price_id if created_price else "",
            }
            if stripe_default_pm:
                update_vals['stripe_payment_method_id'] = stripe_default_pm
            current_start = stripe_subscription.get('current_period_start')
            current_end = stripe_subscription.get('current_period_end')
            if not current_start or not current_end:
                items = stripe_subscription.get('items', {}).get('data', [])
                first_item = items[0] if items else {}
                current_start = current_start or first_item.get('current_period_start')
                current_end = current_end or first_item.get('current_period_end')
            if current_start:
                update_vals['current_term_start'] = fields.Datetime.to_string(datetime.fromtimestamp(current_start, timezone.utc))
            if current_end:
                update_vals['current_term_end'] = fields.Datetime.to_string(datetime.fromtimestamp(current_end, timezone.utc))
            subscription.write(update_vals)
    
    def apply_discount(self, discount_code):
        """Apply a discount to the subscription"""
        self.ensure_one()
        
        if self.discount_id:
            raise UserError(_("A discount is already applied to this subscription."))
        
        # Validate discount code
        discount, message = self.env['subscription.discount'].validate_discount_code(
            discount_code, 
            self.sub_plan_id.id, 
            self.protocol_id.id if self.protocol_id else None,
            self.price
        )
        
        if not discount:
            raise UserError(_(message))
        
        # Calculate discount amount
        discount_amount = discount.calculate_discount_amount(self.price)
        final_price = self.price - discount_amount
        
        # Apply discount
        self.write({
            'discount_id': discount.id,
            'discount_code': discount.code,
            'discount_amount': discount_amount,
            'original_price': self.price,
            'price': final_price
        })
        
        # Record usage
        discount.apply_discount()
        
        _logger.info(f"Applied discount {discount.code} to subscription {self.name}: ${discount_amount} off")
        return True
    
    def remove_discount(self):
        """Remove applied discount from subscription"""
        self.ensure_one()
        
        if not self.discount_id:
            raise UserError(_("No discount applied to this subscription."))
        
        # Restore original price
        self.write({
            'price': self.original_price,
            'discount_id': False,
            'discount_code': False,
            'discount_amount': 0,
            'original_price': 0
        })
        
        _logger.info(f"Removed discount from subscription {self.name}")
        return True
    
    # -----------------------------
    # to store the wallet info in case of coreum
    # -----------------------------
    @api.model
    def _now_iso(self):
        return datetime.utcnow().replace(microsecond=0).isoformat() + "Z"

    def action_generate_and_store_wallet(self, testnet: bool = False, target_node=None):
        for rec in self:
            try:
                data = mnemonic_service.generate_mnemonic_and_address(rec,testnet=testnet)
            except Exception as e:
                raise UserError(_("Failed generating wallet: %s") % e)

            # Optional: bind AAD to the record (prevents copy/paste to other records)
            key = mnemonic_service.get_aes_key(rec.env)
            enc = mnemonic_service.encrypt_aes(data["mnemonic"], key)

            blob = {
                "wallet": data["address"],
                "mnemonic": enc,
                "created": self._now_iso(),
            }
            node_rec = None
            if target_node and target_node.subscription_id == rec:
                node_rec = target_node
            else:
                node_rec = rec.get_latest_node()

            if node_rec:
                node_rec.sudo().write({"validator_info": json.dumps(blob)})
            else:
                rec.sudo().write({"validator_info": json.dumps(blob)})

    def get_wallet_info(self, reveal: bool = True) -> dict:
        self.ensure_one()
        raw = (self.validator_info or "").strip()
        if not raw:
            return {}
        try:
            data = json.loads(raw)
        except Exception:
            raise UserError(_("Wallet info is not valid JSON"))

        if reveal:
            if not (self.env.is_superuser() or self.env.user.has_group("base.group_system")):
                raise UserError(_("You don't have permission to view the mnemonic."))
            key = self.env['ir.config_parameter'].sudo().get_param('mnemonic_encryption_key')
            try:
                data["mnemonic_plain"] = mnemonic_service.decrypt_mnemonic(data.get("mnemonic", ""),key)
            except Exception as e:
                raise UserError(_("Failed to decrypt mnemonic: %s") % e)
        return {
                'type': 'ir.actions.client',
                'tag': 'display_notification',
                'params': {
                    'title': 'Wallet Info',
                    'message': f"Wallet Info: {data}",
                    'sticky': False,
                    'type': 'success',
                }
            }

    def _run_restake_cycle(self):
        for subscription in self:
            metadata = subscription.metaData or {}
            restake_data = metadata.get('restake')
            if not restake_data or not restake_data.get('is_active'):
                continue

            pr_number = restake_data.get('github_pr_number')
            restake_data['is_pr_merged'] = restake_helper.check_pull_request_status(self.env, pr_number)

            try:
                interval = int(restake_data.get('interval', 0))
            except (TypeError, ValueError):
                interval = 0

            if interval <= 0:
                continue

            next_run = fields.Datetime.now() + timedelta(hours=interval)
            restake_data['next_run_time'] = fields.Datetime.to_string(next_run)
            metadata['restake'] = restake_data
            subscription.sudo().write({'metaData': metadata})
    def _get_next_payment_date(self, start_from=None):
        """Calculate the next payment date based on plan duration and unit."""
        self.ensure_one()
        if not start_from:
            start_from = self.next_payment_date or fields.Datetime.now()
        
        if not self.unit or not self.duration:
            return start_from

        duration = self.duration
        if self.unit == 'day':
            return start_from + relativedelta(days=duration)
        elif self.unit == 'month':
            return start_from + relativedelta(months=duration)
        elif self.unit == 'year':
            return start_from + relativedelta(years=duration)
        elif self.unit == 'week':
            return start_from + relativedelta(weeks=duration)
        elif self.unit == 'hour':
            return start_from + relativedelta(hours=duration)
        return start_from

    def action_charge_subscription(self):
        """Execute the actual Stripe charge for an Odoo-managed subscription."""
        self.ensure_one()
        
        if not self.is_odoo_managed or not self.payment_vault_id:
            return False

        stripe_secret_key = self.env['ir.config_parameter'].sudo().get_param('stripe_secret_key')
        if not stripe_secret_key:
            _logger.error("Aborting charge: Stripe secret key not configured.")
            return False
        
        stripe.api_key = stripe_secret_key
        qty = max(int(self.quantity), 1)
        amount_cents = int(self.price * 100 * qty)
        charge_description = f"Subscription {self.name} - Internal ID: {self.id}"
        try:
            # Create PaymentIntent for off-session charge
            # The payment method ID is stored encrypted in the vault; decrypt it before use.
            raw_pm_id = self.payment_vault_id._decrypt_id(self.payment_vault_id.stripe_payment_method_id)
            intent = stripe.PaymentIntent.create(
                amount=amount_cents,
                currency='usd',
                customer=self.stripe_customer_id,
                payment_method=raw_pm_id,
                off_session=True,
                confirm=True,
                description=charge_description,
                metadata={
                    'odoo_subscription_id': self.id,
                    'billing_type': 'odoo_managed_recurrence',
                }
            )
            
            if intent.status == 'succeeded':
                now = fields.Datetime.now()
                # Use current next_payment_date or stripe_end_date as the base for the next cycle
                base_date = self.next_payment_date or self.stripe_end_date or now
                next_date = self._get_next_payment_date(base_date)
                self.write({
                    'last_charge_date': now,
                    'next_payment_date': next_date,
                    'current_term_start': base_date,
                    'current_term_end': next_date,
                    'stripe_start_date': base_date,
                    'stripe_end_date': next_date,
                    'charge_retry_count': 0,
                })
                # Create and post invoice
                self.create_invoice()
                return True
            else:
                self.charge_retry_count += 1
                return False

        except stripe.error.CardError as e:
            # Error code will be e.code
            _logger.error("Stripe CardError for subscription %s: %s (Code: %s)", self.id, str(e), e.code)
            self.charge_retry_count += 1
            self.send_payment_failed_notifications(failure_reason=str(e))
            return False
        except stripe.error.StripeError as e:
            _logger.error("Stripe API error for subscription %s: %s", self.id, str(e))
            self.charge_retry_count += 1
            self.send_payment_failed_notifications(failure_reason=str(e))
            return False
        except Exception as e:
            _logger.error("Unexpected error during charge for subscription %s: %s", self.id, str(e))
            self.charge_retry_count += 1
            self.send_payment_failed_notifications(failure_reason=str(e))
            return False

    @api.model
    def send_payment_failed_notifications(self, failure_reason="General error"):
        """Send email notification to user about payment failure."""
        self.ensure_one()
        _logger.info("Attempting to send payment failure notification for subscription %s", self.id)
        template = self.env.ref('subscription_management.mail_template_payment_failed', raise_if_not_found=False)
        if template:
            template.with_context(failure_reason=failure_reason).send_mail(self.id, force_send=True)
            _logger.info("Sent payment failure notification for subscription %s", self.id)

    def run_subscription_billing_cron(self):
        """Find and charge all Odoo-managed subscriptions due for payment."""
        now = fields.Datetime.now()
        subscriptions = self.search([
            ('is_odoo_managed', '=', True),
            ('state', 'in', ['in_progress', 'provisioning']), # Allow provisioning to be charged if they started
            ('next_payment_date', '<=', now),
            # ('id','=',83),
            ('charge_retry_count', '<', 3), # Limit retries
        ])
        
        _logger.info("Cron found %s Odoo-managed subscriptions due for billing.", len(subscriptions))
        for sub in subscriptions:
            success = sub.action_charge_subscription()
            if not success and sub.charge_retry_count >= 3:
                sub.write({'stripe_status': 'past_due'})
                _logger.warning("Subscription %s marked as PAST_DUE after 3 failures.", sub.name)
            # Commit after each subscription to avoid rolling back successful charges on later errors
            self.env.cr.commit()

    @api.model
    def _cron_migrate_to_odoo_managed(self):
        """
        Cron job to process migration of Stripe-managed subscriptions to Odoo-managed.
        Finds subscriptions that are still managed by Stripe and transitions them.
        """
        subscriptions = self.search([
            ('is_odoo_managed', '=', False),
            ('stripe_subscription_id', '!=', False),
            ('stripe_status', 'in', ['active', 'past_due', 'trialing'])
        ], limit=50)

        if subscriptions:
            _logger.info("Migrating %s subscriptions to Odoo-managed via cron", len(subscriptions))
            subscriptions.action_migrate_to_odoo_managed()

    def action_migrate_to_odoo_managed(self):
        """
        Transition a Stripe-native subscription to Odoo-managed billing.
        1. Capture current PM.
        2. Make PM off-session ready (SetupIntent).
        3. Set Stripe to cancel at period end.
        4. Set Odoo to take over on that date.
        """

        success_count = 0
        failed_subs = []

        stripe_secret_key = self.env['ir.config_parameter'].sudo().get_param('stripe_secret_key')
        stripe.api_key = stripe_secret_key


        for rec in self:

            if rec.is_odoo_managed or not rec.stripe_subscription_id:
                continue

            # retry mechanism for postgres serialization error
            for attempt in range(3):
                try:

                    # 🔒 lock the record to avoid concurrent updates
                    self.env.cr.execute(
                        "SELECT id FROM subscription_subscription WHERE id=%s FOR UPDATE",
                        [rec.id]
                    )

                    # 1️⃣ Retrieve Stripe Subscription
                    stripe_sub = stripe.Subscription.retrieve(
                        rec.stripe_subscription_id,
                        expand=['default_payment_method']
                    )

                    pm = stripe_sub.get('default_payment_method')

                    if not pm:
                        customer = stripe.Customer.retrieve(rec.stripe_customer_id)
                        default_pm_id = customer.get('invoice_settings', {}).get('default_payment_method')
                        if default_pm_id:
                            pm = stripe.PaymentMethod.retrieve(default_pm_id)

                    if not pm:
                        failed_subs.append(f"{rec.name}: No payment method found in Stripe")
                        break

                    # ensure PM attached
                    try:
                        stripe.PaymentMethod.attach(pm.id, customer=rec.stripe_customer_id)
                    except Exception:
                        pass

                    # 2️⃣ Vault Payment Method in Odoo
                    vault_rec = self.env['stripe.payment.method'].sudo().create_or_update_from_stripe(
                        rec.customer_name, pm
                    )

                    # 3️⃣ Make PM off-session ready
                    stripe.SetupIntent.create(
                        customer=rec.stripe_customer_id,
                        payment_method=pm.id,
                        usage="off_session",
                        confirm=True,
                        automatic_payment_methods={
                            "enabled": True,
                            "allow_redirects": "never"
                        }
                    )

                    # 4️⃣ Ensure subscription uses this PM
                    stripe.Subscription.modify(
                        rec.stripe_subscription_id,
                        default_payment_method=pm.id,
                        payment_settings={"save_default_payment_method": "on_subscription"}
                    )

                    # 5️⃣ Cancel at period end
                    stripe.Subscription.modify(
                        rec.stripe_subscription_id,
                        cancel_at_period_end=True
                    )

                    # 6️⃣ Get next billing date
                    items = stripe_sub.get("items", {}).get("data", [])

                    if not items:
                        failed_subs.append(f"{rec.name}: No subscription items found")
                        break

                    period_end_ts = items[0].get("current_period_end")

                    if not period_end_ts:
                        failed_subs.append(f"{rec.name}: No current_period_end found")
                        break

                    period_end = datetime.utcfromtimestamp(period_end_ts)

                    # 7️⃣ Update Odoo
                    rec.write({
                        'is_odoo_managed': True,
                        'payment_vault_id': vault_rec.id,
                        'next_payment_date': period_end,
                    })

                    rec.message_post(body=_(
                        "Subscription converted to Odoo-managed billing.<br/>"
                        "Handover date (Next Payment Date): %s<br/>"
                        "Payment Method: %s<br/>"
                        "Off-session enabled in Stripe"
                    ) % (period_end, vault_rec.display_name))

                    success_count += 1
                    break

                except errors.SerializationFailure:
                    if attempt == 2:
                        failed_subs.append(f"{rec.name}: Concurrent update error")
                    else:
                        self.env.cr.rollback()
                        time_lib.sleep(1)

                except Exception as e:
                    failed_subs.append(f"{rec.name}: {str(e)}")
                    break

        msg = f"Successfully migrated {success_count} subscriptions."

        if failed_subs:
            msg += "\nFailed: " + ", ".join(failed_subs)

        wizard_id = self.env['subscription.message.wizard'].create({'message': msg})

        return {
            'name': _("Migration Result"),
            'view_mode': 'form',
            'res_model': 'subscription.message.wizard',
            'res_id': wizard_id.id,
            'type': 'ir.actions.act_window',
            'target': 'new',
        }
        


class SubscriptionInvoiceQueue(models.Model):
    _name = "subscription.invoice.queue"
    _description = "Subscription Invoice Queue"
    _order = "create_date asc"

    subscription_id = fields.Many2one("subscription.subscription", required=True, ondelete="cascade")
    stripe_event_id = fields.Char("Stripe Event ID", index=True)
    action = fields.Selection([
        ('normal', 'Normal Invoice'),
        ('prorated_charge_for_qty_increase', 'Prorated Charge'),
    ], default='normal', required=True)
    processed = fields.Boolean("Processed", default=False)
    error_message = fields.Text("Error Message")
    invoice_id = fields.Many2one("account.move", string="Created Invoice")
    attempt_count = fields.Integer("Attempt Count", default=0)
    
    @api.model
    def _process_pending_invoices(self):
        pending_records = self.sudo().search([('processed', '=', False)], limit=10)
        for rec in pending_records:
            try:
                rec.attempt_count += 1
                rec.subscription_id = rec.subscription_id.sudo()
                _logger.info(f"🧾 Processing queued invoice for subscription {rec.subscription_id.id}")
                
                invoice = rec.subscription_id.create_invoice(action=rec.action)
                rec.sudo().write({
                    'invoice_id': invoice.id,
                    'processed': True,
                    'error_message': False,
                })
                rec.env.cr.commit()
            except Exception as e:
                rec.error_message = f"{type(e).__name__}: {str(e)}"
                _logger.exception(f"Invoice queue failed for subscription {rec.subscription_id.id}")
                rec.env.cr.rollback()
                time_lib.sleep(1)
    
