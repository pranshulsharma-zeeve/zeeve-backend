# -*- coding: utf-8 -*-
"""
Stripe Payment Log Model
Tracks all payment events received from Stripe webhooks
"""

from odoo import api, fields, models, _
from odoo.exceptions import ValidationError
import json
import logging

_logger = logging.getLogger(__name__)


class StripePaymentLog(models.Model):
    _name = 'stripe.payment.log'
    _description = 'Stripe Payment Log'
    _order = 'create_date desc'
    _rec_name = 'event_id'

    # Stripe Event Information
    event_id = fields.Char(string='Stripe Event ID', required=True, index=True)
    event_type = fields.Char(string='Event Type', required=True, index=True)
    stripe_subscription_id = fields.Char(string='Stripe Subscription ID', index=True)
    stripe_customer_id = fields.Char(string='Stripe Customer ID', index=True)
    stripe_payment_intent_id = fields.Char(string='Stripe Payment Intent ID', index=True)
    stripe_invoice_id = fields.Char(string='Stripe Invoice ID', index=True)
    
    # Payment Information
    amount = fields.Float(string='Amount', digits=(16, 2))
    currency = fields.Char(string='Currency', default='usd')
    payment_status = fields.Selection([
        ('succeeded', 'Succeeded'),
        ('failed', 'Failed'),
        ('pending', 'Pending'),
        ('canceled', 'Canceled'),
        ('requires_action', 'Requires Action'),
    ], string='Payment Status')
    
    # Subscription Information
    subscription_status = fields.Selection([
        ('active', 'Active'),
        ('paused', 'Paused'),
        ('canceled', 'Canceled'),
        ('incomplete', 'Incomplete'),
        ('incomplete_expired', 'Incomplete Expired'),
        ('past_due', 'Past Due'),
        ('trialing', 'Trialing'),
        ('unpaid', 'Unpaid'),
    ], string='Subscription Status')
    
    # Odoo References
    subscription_id = fields.Many2one('subscription.subscription', string='Odoo Subscription')
    invoice_id = fields.Many2one('account.move', string='Odoo Invoice')
    payment_id = fields.Many2one('account.payment', string='Odoo Payment')
    
    # Event Details
    event_data = fields.Text(string='Event Data (JSON)')
    processed = fields.Boolean(string='Processed', default=False)
    processing_error = fields.Text(string='Processing Error')
    
    # Timestamps
    stripe_created = fields.Datetime(string='Stripe Created At')
    processed_at = fields.Datetime(string='Processed At')
    
    # Additional Fields
    description = fields.Text(string='Description')
    failure_reason = fields.Text(string='Failure Reason')
    webhook_attempt = fields.Integer(string='Webhook Attempt', default=1)
    
    @api.constrains('event_id')
    def _check_unique_event_id(self):
        for record in self:
            if self.search_count([('event_id', '=', record.event_id), ('id', '!=', record.id)]) > 0:
                raise ValidationError(_('Event ID must be unique.'))

    def process_event(self):
        """Process the Stripe event and update related Odoo records"""
        self.ensure_one()
        
        try:
            if self.processed:
                return True
                
            # Parse event data
            event_data = json.loads(self.event_data) if self.event_data else {}
            
            # Process based on event type
            if self.event_type == 'checkout.session.completed':
                self._process_checkout_session_completed(event_data)
            elif self.event_type == 'invoice.payment_succeeded':
                self._process_invoice_payment_succeeded(event_data)
            elif self.event_type == 'invoice.payment_failed':
                self._process_invoice_payment_failed(event_data)
            elif self.event_type == 'customer.subscription.created':
                self._process_subscription_created(event_data)
            elif self.event_type == 'customer.subscription.updated':
                self._process_subscription_updated(event_data)
            elif self.event_type == 'customer.subscription.deleted':
                self._process_subscription_deleted(event_data)
            elif self.event_type == 'payment_intent.succeeded':
                self._process_payment_intent_succeeded(event_data)
            elif self.event_type == 'payment_intent.payment_failed':
                self._process_payment_intent_failed(event_data)
            else:
                _logger.info(f"Unhandled event type: {self.event_type}")
                
            self.write({
                'processed': True,
                'processed_at': fields.Datetime.now(),
            })
            
        except Exception as e:
            _logger.error(f"Error processing Stripe event {self.event_id}: {str(e)}")
            self.write({
                'processing_error': str(e),
                'webhook_attempt': self.webhook_attempt + 1,
            })
            raise

    def _process_checkout_session_completed(self, event_data):
        """Process checkout.session.completed event"""
        session = event_data.get('data', {}).get('object', {})
        subscription_id = session.get('subscription')
        customer_id = session.get('customer')
        
        if subscription_id:
            # Find or create subscription record
            subscription = self.env['subscription.subscription'].sudo().search([
                ('stripe_subscription_id', '=', subscription_id)
            ], limit=1)
            
            if not subscription:
                # Create new subscription from metadata
                metadata = session.get('metadata', {})
                subscription = self._create_subscription_from_metadata(metadata, subscription_id, customer_id)
            
            if subscription:
                self.subscription_id = subscription.id
                update_vals = {
                    'stripe_subscription_id': subscription_id,
                    'stripe_customer_id': customer_id,
                    'subscribed_on': fields.Datetime.now(),
                }
                if subscription.state in ('draft', 'requested'):
                    update_vals['state'] = 'provisioning'
                subscription.write(update_vals)

    def _process_invoice_payment_succeeded(self, event_data):
        """Process invoice.payment_succeeded event"""
        invoice_data = event_data.get('data', {}).get('object', {})
        subscription_id = invoice_data.get('subscription')
        
        if subscription_id:
            subscription = self.env['subscription.subscription'].sudo().search([
                ('stripe_subscription_id', '=', subscription_id)
            ], limit=1)
            
            if subscription:
                self.subscription_id = subscription.id
                # Create or update invoice
                invoice = self._create_or_update_invoice(subscription, invoice_data)
                if invoice:
                    self.invoice_id = invoice.id
                    # Create payment record
                    payment = self._create_payment_record(subscription, invoice, invoice_data)
                    if payment:
                        self.payment_id = payment.id

    def _process_invoice_payment_failed(self, event_data):
        """Process invoice.payment_failed event"""
        invoice_data = event_data.get('data', {}).get('object', {})
        subscription_id = invoice_data.get('subscription')
        
        if subscription_id:
            subscription = self.env['subscription.subscription'].sudo().search([
                ('stripe_subscription_id', '=', subscription_id)
            ], limit=1)
            
            if subscription:
                self.subscription_id = subscription.id
                subscription.write({'state': 'in_grace'})

    def _process_subscription_created(self, event_data):
        """Process customer.subscription.created event"""
        subscription_data = event_data.get('data', {}).get('object', {})
        stripe_subscription_id = subscription_data.get('id')
        
        # Update subscription status
        subscription = self.env['subscription.subscription'].sudo().search([
            ('stripe_subscription_id', '=', stripe_subscription_id)
        ], limit=1)
        
        if subscription:
            self.subscription_id = subscription.id
            self._update_subscription_status(subscription, subscription_data)

    def _process_subscription_updated(self, event_data):
        """Process customer.subscription.updated event"""
        subscription_data = event_data.get('data', {}).get('object', {})
        stripe_subscription_id = subscription_data.get('id')
        
        subscription = self.env['subscription.subscription'].sudo().search([
            ('stripe_subscription_id', '=', stripe_subscription_id)
        ], limit=1)
        
        if subscription:
            self.subscription_id = subscription.id
            self._update_subscription_status(subscription, subscription_data)

    def _process_subscription_deleted(self, event_data):
        """Process customer.subscription.deleted event"""
        subscription_data = event_data.get('data', {}).get('object', {})
        stripe_subscription_id = subscription_data.get('id')
        
        subscription = self.env['subscription.subscription'].sudo().search([
            ('stripe_subscription_id', '=', stripe_subscription_id)
        ], limit=1)
        
        if subscription:
            self.subscription_id = subscription.id
            subscription.write({'state': 'closed'})

    def _process_payment_intent_succeeded(self, event_data):
        """Process payment_intent.succeeded event"""
        payment_intent = event_data.get('data', {}).get('object', {})
        # Handle successful payment
        pass

    def _process_payment_intent_failed(self, event_data):
        """Process payment_intent.payment_failed event"""
        payment_intent = event_data.get('data', {}).get('object', {})
        # Handle failed payment
        pass

    def _create_subscription_from_metadata(self, metadata, stripe_subscription_id, stripe_customer_id):
        """Create subscription from checkout session metadata"""
        # This would be implemented based on your specific metadata structure
        return None

    def _create_or_update_invoice(self, subscription, invoice_data):
        """Create or update invoice from Stripe invoice data"""
        # Implementation for creating/updating invoices
        return None

    def _create_payment_record(self, subscription, invoice, invoice_data):
        """Create payment record from Stripe payment data"""
        # Implementation for creating payment records
        return None

    def _update_subscription_status(self, subscription, subscription_data):
        """Update subscription status based on Stripe data"""
        print(subscription, subscription_data,'========subscription, subscription_dat======437')
        status = subscription_data.get('status')
        if status == 'active' and subscription.state in ('draft', 'requested'):
            subscription.write({'state': 'provisioning'})
        elif status == 'canceled':
            subscription.write({'state': 'closed'})
        elif status == 'past_due':
            subscription.write({'state': 'in_grace'})

    @api.model
    def create_log_entry(self, event_id, event_type, event_data, **kwargs):
        """Create a new log entry for a Stripe event"""
        values = {
            'event_id': event_id,
            'event_type': event_type,
            'event_data': json.dumps(event_data) if isinstance(event_data, dict) else str(event_data),
            'stripe_created': fields.Datetime.now(),
        }
        values.update(kwargs)
        
        log_entry = self.create(values)
        log_entry.process_event()
        return log_entry
