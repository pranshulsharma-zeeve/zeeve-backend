# -*- coding: utf-8 -*-
from unittest.mock import patch
from odoo.tests.common import TransactionCase
from odoo import fields

class TestSubscriptionEmails(TransactionCase):

    def setUp(self):
        super(TestSubscriptionEmails, self).setUp()
        self.partner = self.env['res.partner'].create({
            'name': 'Test Partner',
            'email': 'test@example.com'
        })
        self.protocol = self.env['protocol.master'].create({
            'name': 'Test Protocol',
            'short_name': 'TP',
            'image': 'R0lGODlhAQABAIAAAAAAAP///yH5BAEAAAAALAAAAAABAAEAAAIBRAA7',
        })
        self.plan = self.env['subscription.plan'].create({
            'name': 'Test Plan',
            'subscription_type': 'rpc',
            'protocol_id': self.protocol.id,
            'duration': 1,
            'unit': 'month',
        })
        self.subscription = self.env['subscription.subscription'].create({
            'customer_name': self.partner.id,
            'subscription_type': 'rpc',
            'protocol_id': self.protocol.id,
            'sub_plan_id': self.plan.id,
            'state': 'provisioning',
            'start_date': fields.Date.today(),
            'unit': 'month',
            'duration': 1,
            'price': 100.0,
        })

    def test_send_provisioning_mail_idempotency(self):
        """Verify that send_provisioning_mail only sends mail once."""
        # Reset the flag just in case
        self.subscription.provision_mail_sent = False
        
        with patch('odoo.addons.subscription_management.models.subscription_subscription.subscription_subscription.send_mail_template') as mock_send:
            # First call
            self.subscription.send_provisioning_mail()
            self.assertTrue(self.subscription.provision_mail_sent, "Flag should be set after first call")
            self.assertEqual(mock_send.call_count, 1, "Mail template should be called once")
            
            # Second call
            self.subscription.send_provisioning_mail()
            self.assertEqual(mock_send.call_count, 1, "Mail template should still be called only once")
            
            # Reset flag and call again
            self.subscription.provision_mail_sent = False
            self.subscription.send_provisioning_mail()
            self.assertEqual(mock_send.call_count, 2, "Mail template should be called again after resetting flag")
