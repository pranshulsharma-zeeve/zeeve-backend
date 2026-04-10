"""Subscription node model for tracking per-subscription infrastructure."""

from __future__ import annotations

import random
import string
import uuid

from odoo import api, fields, models,_
from odoo.tools.safe_eval import safe_eval
from odoo.exceptions import ValidationError
from ..utils.email_utils import (
    send_subscription_email,
    send_subscription_cancellation_emails,
)
import json
from ..utils import mnemonic_service
from odoo.exceptions import UserError, ValidationError


class SubscriptionNode(models.Model):
    """Represents an infrastructure node tied to a subscription."""

    _name = "subscription.node"
    _description = "Subscription Node"
    _inherit = ["mail.thread", "mail.activity.mixin"]
    _rec_name = "node_name"

    node_identifier = fields.Char(
        string="Node Identifier",
        default=lambda self: str(uuid.uuid4()),
        copy=False,
        required=True,
        index=True,
    )
    node_name = fields.Char(string="Node Name", required=True, tracking=True)
    subscription_id = fields.Many2one(
        "subscription.subscription",
        string="Subscription",
        ondelete="cascade",
        index=True,
        tracking=True
    )
    node_type = fields.Selection(
        selection=[
            ("rpc", "RPC"),
            ("archive", "Archive"),
            ("validator", "Validator"),
            ("other", "Other"),
        ],
        string="Node Type",
        default="other",
        required=True,
        tracking=True,
    )
    state = fields.Selection([('draft','Draft'),('requested', 'Requested'),
        ('provisioning', 'Provisioning'),
        ('in_grace','In grace'),
        ('syncing', 'Syncing'),
        ('ready', 'Ready'),
        ('suspended', 'Suspended'),
        ('cancellation_requested', 'Cancellation Requested'),
        ('closed', 'Closed'), # means subscription cancelled
        ('deleted', 'Deleted'),], default='draft', string='Node Status', tracking=True, copy=False)
    node_created_date = fields.Datetime(
        string="Node Created Date",
        compute="_compute_node_created_date",
        store=True,
        tracking=True,
        readonly=False
    )
    
    network_selection_id = fields.Many2one(
        "zeeve.network.type",
        string="Network Selection",
        ondelete="set null",
        tracking=True,
    )
    server_location_id = fields.Many2one(
        "server.location",
        string="Server Location",
        tracking=True,
    )
    software_update_rule = fields.Selection(
        [('auto', 'Automatically'), ('manual', 'Manually')],
        string="Node Software Update Rule",
        tracking=True,
    )
    endpoint_url = fields.Char(
        string="Endpoint URL",
        help="Public endpoint shared with the customer once the node is live.",
        tracking=True,
    )
    metadata_json = fields.Text(
        string="Metadata",
        default=dict,
        copy=False,
        help="Internal metadata captured during provisioning.",
        tracking=True
    )
    validator_info = fields.Text(
        string="Validator Info",
        default=dict,
        copy=False,
        help="Validator specific information.",
        tracking=True
    )
    is_vision_onboarded = fields.Boolean(string="Is Vision Onboarded", default=False,tracking=True)

    invoice_ids = fields.One2many(
        "account.move",
        "node_id",
        string="Invoices",
        readonly=True,
    )
    invoice_count = fields.Integer(
        string="Invoice Count",
        compute="_compute_invoice_count",
        readonly=True,
    )
    customer_email = fields.Char(
        string="Customer Email",
        related="subscription_id.customer_name.email",
        store=True,
        readonly=True,
        tracking=True
    )

    _sql_constraints = [
        ("subscription_node_identifier_unique", "unique(node_identifier)", "Node identifier must be unique."),
    ]

    @api.model_create_multi
    def create(self, vals_list):
        Subscription = self.env["subscription.subscription"].sudo()
        for vals in vals_list:
            vals.setdefault("node_identifier", str(uuid.uuid4()))
            subscription = False
            subscription_id = vals.get("subscription_id")
            if subscription_id:
                subscription = Subscription.browse(subscription_id)
                if not subscription.exists():
                    subscription = False
            if subscription:
                if not vals.get("node_name"):
                    vals["node_name"] = self._generate_node_name(subscription)
                if not vals.get("node_type") and subscription.subscription_type:
                    vals["node_type"] = subscription.subscription_type
        nodes = super().create(vals_list)
        for node in nodes:
            subscription = node.subscription_id
            if subscription:
                subscription._handle_post_node_creation(node)
        return nodes

    def _generate_node_name(self, subscription):
        """Generate a default node name using subscription data."""
        partner = subscription.customer_name
        protocol = subscription.protocol_id
        random_str = "".join(random.choices(string.ascii_lowercase + string.digits, k=3))

        customer_first = ""
        if partner:
            customer_first = (getattr(partner, "first_name", "") or "").strip()
            if len(customer_first) < 3:
                display = partner.display_name or partner.name or ""
                customer_first = (display or "")[:3]
        protocol_part = (protocol.name or "")[:3] if protocol else ""
        return f"{customer_first[:3]}{protocol_part}{random_str}"

    def _compute_invoice_count(self):
        data = self.env["account.move"].read_group(
            [("node_id", "in", self.ids)],
            ["node_id"],
            ["node_id"],
        )
        counts = {item["node_id"][0]: item["node_id_count"] for item in data}
        for node in self:
            node.invoice_count = counts.get(node.id, 0)

    @api.depends()
    def _compute_node_created_date(self):
        """Compute node_created_date to current datetime if not already set."""
        for node in self:
            if not node.node_created_date:
                node.node_created_date = fields.Datetime.now()

    def _map_tracking_value_to_state(self, value):
        """Return a valid state key based on a chatter tracking value."""
        if not value:
            return False
        state_field = self._fields.get("state")
        if not state_field:
            return False
        selection = state_field.selection or []
        normalized = str(value).strip().lower()
        if not normalized:
            return False
        for key, label in selection:
            if normalized == (key or "").lower():
                return key
            if normalized == (label or "").lower():
                return key
        return False

    def _get_previous_state_from_chatter(self):
        """Deduce the last non-cancellation state from tracking history."""
        self.ensure_one()
        Tracking = self.env["mail.tracking.value"].sudo()
        domain = [
            ("mail_message_id.model", "=", self._name),
            ("mail_message_id.res_id", "=", self.id),
            ("field_id.name", "=", "state"),
        ]
        entry = Tracking.search(domain, order="create_date desc, id desc", limit=1)
        if not entry:
            return False
        for candidate in (
            entry.old_value_char,
            getattr(entry, "old_value_text", False),
            getattr(entry, "old_value_integer", False),
            getattr(entry, "old_value_float", False),
        ):
            mapped = self._map_tracking_value_to_state(candidate)
            if mapped:
                return mapped
        return False

    def write(self, vals):
        """Send in-app notifications for node lifecycle state transitions."""
        previous_states = {node.id: node.state for node in self}
        res = super().write(vals)
        if 'state' in vals:
            ready_nodes = self.filtered(
                lambda node: node.state == 'ready'
                and previous_states.get(node.id) != 'ready'
            )
            for node in ready_nodes:
                subscription = node.subscription_id
                if not subscription:
                    continue
                protocol = subscription.protocol_id
                # Skip sending ready mail if protocol is OPN
                if protocol and protocol.name and protocol.name.upper() == 'OPN':
                    continue
                customer = subscription.customer_name
                plan = subscription.sub_plan_id
                context = {
                    'plan_details': {
                        'name': customer.name if customer else '',
                        'buyer_email_id': customer.email if customer else '',
                        'plan_name': plan.name if plan else '',
                        'protocol_name': protocol.name if protocol else '',
                        'subscription_start_date': subscription.stripe_start_date,
                        'node_type': subscription.subscription_type,
                        'subscription_end_date': subscription.stripe_end_date,
                        'syncingCompletionEta': 'TBA',
                        'subscription_cost': subscription.price,
                        'enabled_endpoints': {
                            'http': node.endpoint_url or 'TBA',
                            'ws': 'TBA',
                        },
                    },
                }
                # Determine email template based on subscription type
                template_key = 'validator_provisioning_complete' if subscription.subscription_type == 'validator' else 'node_ready'
                send_subscription_email(
                    subscription.env,
                    template_key,
                    record=subscription,
                    context=context,
                )
                if customer:
                    self.env['zeeve.notification'].sudo().notify_partner(
                        customer,
                        notification_type='node_ready',
                        title='Node is ready',
                        message='%s for %s is now ready.' % (
                            node.node_name or 'Your node',
                            protocol.name or subscription.name or 'your subscription',
                        ),
                        category='success',
                        payload={
                            'subscription_id': subscription.id,
                            'node_id': node.id,
                            'node_name': node.node_name or '',
                            'protocol_name': protocol.name if protocol else '',
                            'endpoint_url': node.endpoint_url or '',
                        },
                        action_url=node.endpoint_url or '/nodes',
                        reference_model='subscription.node',
                        reference_id=node.id,
                        dedupe_key='node_ready:%s' % node.id,
                    )
            cancellation_requested_nodes = self.filtered(
                lambda node: node.state == 'cancellation_requested'
                and previous_states.get(node.id) != 'cancellation_requested'
            )
            for node in cancellation_requested_nodes:
                subscription = node.subscription_id
                customer = subscription.customer_name if subscription else False
                protocol = subscription.protocol_id if subscription else False
                if not customer:
                    continue
                self.env['zeeve.notification'].sudo().notify_partner(
                    customer,
                    notification_type='node_cancellation_requested',
                    title='Cancellation requested',
                    message='We received your cancellation request for %s.' % (
                        node.node_name or protocol.name or subscription.name or 'your node'
                    ),
                    category='warning',
                    payload={
                        'subscription_id': subscription.id if subscription else False,
                        'node_id': node.id,
                        'node_name': node.node_name or '',
                        'protocol_name': protocol.name if protocol else '',
                        'state': node.state,
                    },
                    action_url='/nodes',
                    reference_model='subscription.node',
                    reference_id=node.id,
                    dedupe_key='node_cancellation_requested:%s:%s' % (node.id, node.write_date or ''),
                )
            cancelled_nodes = self.filtered(
                lambda node: node.state == 'closed'
                and previous_states.get(node.id) != 'closed'
            )
            for node in cancelled_nodes:
                subscription = node.subscription_id
                customer = subscription.customer_name if subscription else False
                protocol = subscription.protocol_id if subscription else False
                if not customer:
                    continue
                self.env['zeeve.notification'].sudo().notify_partner(
                    customer,
                    notification_type='node_cancelled',
                    title='Node cancelled',
                    message='%s has been cancelled successfully.' % (
                        node.node_name or protocol.name or subscription.name or 'Your node'
                    ),
                    category='info',
                    payload={
                        'subscription_id': subscription.id if subscription else False,
                        'node_id': node.id,
                        'node_name': node.node_name or '',
                        'protocol_name': protocol.name if protocol else '',
                        'state': node.state,
                    },
                    action_url='/nodes',
                    reference_model='subscription.node',
                    reference_id=node.id,
                    dedupe_key='node_cancelled:%s:%s' % (node.id, node.write_date or ''),
                )
        return res
    
    def action_reject_cancellation_request(self):
        """Restore the node to its prior state when cancellation is rejected."""
        state_labels = dict(self._fields['state'].selection)
        processed = []
        for node in self:
            if node.state != 'cancellation_requested':
                continue
            previous_state = node._get_previous_state_from_chatter()
            if not previous_state:
                raise UserError(_("Unable to determine the previous state for node %s.") % (node.node_name or node.id))
            node.write({'state': previous_state})
            restored_label = state_labels.get(previous_state, previous_state)
            node.message_post(body=_("Cancellation request rejected. State restored to %s.") % restored_label)
            subscription = node.subscription_id
            customer = subscription.customer_name if subscription else False
            protocol = subscription.protocol_id if subscription else False
            if customer:
                self.env['zeeve.notification'].sudo().notify_partner(
                    customer,
                    notification_type='node_cancellation_rejected',
                    title='Cancellation request rejected',
                    message='Your cancellation request for %s was rejected. Current state is %s.' % (
                        node.node_name or protocol.name or subscription.name or 'your node',
                        restored_label,
                    ),
                    category='info',
                    payload={
                        'subscription_id': subscription.id if subscription else False,
                        'node_id': node.id,
                        'node_name': node.node_name or '',
                        'protocol_name': protocol.name if protocol else '',
                        'restored_state': previous_state,
                    },
                    action_url='/nodes',
                    reference_model='subscription.node',
                    reference_id=node.id,
                    dedupe_key='node_cancellation_rejected:%s:%s' % (node.id, node.write_date or ''),
                )
            processed.append(restored_label)
        if not processed:
            return True
        record_id = self.env.context.get('active_id') or self[:1].id
        return {
            'type': 'ir.actions.act_window',
            'res_model': self._name,
            'view_mode': 'form',
            'res_id': record_id,
            'target': 'current',
            'effect': {
                'fadeout': 'slow',
                'message': _('Cancellation request rejected and node state restored.'),
                'type': 'rainbow_man',
            },
        }

    def action_view_node_invoices(self):
        """Open customer invoices linked to this node."""
        self.ensure_one()
        subscription = self.subscription_id
        action = self.env.ref('account.action_move_out_invoice_type').read()[0]
        action['domain'] = [
            ('move_type', 'in', ('out_invoice', 'out_refund')),
            ('node_id', '=', self.id),
        ]
        raw_context = action.get('context') or {}
        if isinstance(raw_context, str):
            try:
                context_dict = safe_eval(raw_context)
            except Exception:  # pragma: no cover
                context_dict = {}
        else:
            context_dict = dict(raw_context)
        context_dict.update({
            'default_move_type': 'out_invoice',
            'default_node_id': self.id,
            'default_subscription_id': subscription.id,
            'search_default_node_id': self.id,
        })
        product = self.env["product.product"].sudo().search([('name', 'ilike', self.subscription_id.subscription_type)], limit=1)

        if subscription:
            if subscription.discount_id:
                discount = subscription.discount_id.discount_value
            else:
                discount =  (subscription.discount_amount / subscription.original_price)*100 if subscription.original_price else 0.0
            partner = subscription.customer_name
            plan = subscription.sub_plan_id
            context_dict.update({
                'default_partner_id': partner.id if partner else False,
                'default_invoice_origin': subscription.subscription_ref or subscription.name,
                'default_currency_id': subscription.currency_id.id,
                'default_invoice_line_ids': [
                    (0, 0, {
                        'product_id': product.id if product else False,
                        'name': f"{plan.name if plan else ''} - {subscription.subscription_type or ''}".strip(),
                        'quantity': subscription.quantity or 1.0,
                        'price_unit': subscription.price or 0.0,
                        'discount': discount,
                        'discount_id': subscription.discount_id.id if subscription.discount_id else False,
                        'discount_code': subscription.discount_id.code if subscription.discount_id else '',
                        'tax_ids': [(6, 0, subscription.tax_id.ids)],
                    })
                ],
            })
        action['context'] = context_dict
        return action
    
    def decrypt_shardeum_password(self):
        try:
            self.ensure_one()
            encrypted_pass = self.validator_info.get('shardeum_password')
            decrypted_password = mnemonic_service.decrypt_data(self.env, encrypted_pass)
            return {
                'type': 'ir.actions.client',
                'tag': 'display_notification',
                'params': {
                    'title': 'Decrypted Password',
                    'message': f"The decrypted Shardeum password is: {decrypted_password}",
                    'sticky': False,
                    'type': 'success',
                }
            }
        except Exception as e:
            raise UserError(_("Failed to decrypt Shardeum password: %s") % e)

    def get_ready_at_from_chatter(self):
        self.ensure_one()
        Tracking = self.env["mail.tracking.value"].sudo()
        domain_common = [
            ("mail_message_id.model", "=", self._name),
            ("mail_message_id.res_id", "=", self.id),
            ("field_id.name", "=", "state"),
        ]
        tv = Tracking.search(domain_common + [("new_value_char", "=", "Ready")], order="create_date desc, id desc", limit=1)

        # fallback: sometimes selection stores the KEY (e.g. "ready") not label
        if not tv:
            tv = Tracking.search(domain_common + [("new_value_char", "=", "ready")], order="create_date desc, id desc", limit=1)

        return tv.mail_message_id.date if tv and tv.mail_message_id else False
    

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
