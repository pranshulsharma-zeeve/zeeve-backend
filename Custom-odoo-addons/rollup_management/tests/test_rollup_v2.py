"""Tests for Rollup V2 (Managed Billing)."""

from unittest.mock import MagicMock, patch
from odoo.tests.common import TransactionCase
from odoo.addons.rollup_management.utils import deployment_utils, rollup_util

class TestRollupV2(TransactionCase):
    """Validate V2 specific behaviors."""

    def setUp(self):
        super().setUp()
        country = self.env.ref("base.us")
        self.location = self.env["server.location"].create({
            "name": "V2 Region",
            "country_id": country.id,
        })
        self.rollup_type = self.env["rollup.type"].create({
            "name": "V2 Rollup",
            "default_region_ids": [(6, 0, [self.location.id])],
            "cost": 199.0,
            "amount_month": 150.0,
            "amount_quarter": 400.0,
            "amount_year": 1500.0,
            "payment_frequency": "month",
        })
        self.partner = self.env["res.partner"].create({
            "name": "V2 Tester",
            "email": "v2@example.com",
        })

    @patch("odoo.addons.rollup_management.utils.deployment_utils.get_stripe_client")
    def test_start_checkout_v2_parameters(self, mock_get_client):
        """V2 checkout session should use 'payment' mode and setup_future_usage."""

        client = MagicMock()
        client.api_key = "sk_test"
        client.checkout = MagicMock()
        checkout_session = MagicMock()
        checkout_session.id = "cs_v2_test"
        checkout_session.url = "https://stripe.example/v2"
        client.checkout.Session.create.return_value = checkout_session
        mock_get_client.return_value = client

        payload = {
            "type_id": self.rollup_type.id,
            "name": "V2 Deployment",
            "region_ids": [self.location.id],
            "configuration": {"foo": "bar"},
            "is_odoo_managed": True,
            "billing_duration": "month",
            "network_type": "testnet"
        }

        checkout_session_result, checkout_context, response_data = deployment_utils.start_checkout(self.env.user, payload)

        self.assertEqual(checkout_session_result, checkout_session)
        self.assertTrue(checkout_context.is_odoo_managed)
        self.assertEqual(checkout_context.billing_duration, "month")
        
        # Verify amount is taken from amount_month (150.0) instead of cost (199.0)
        self.assertEqual(float(response_data["amount"]), 150.0)

        session_kwargs = client.checkout.Session.create.call_args.kwargs
        self.assertEqual(session_kwargs["mode"], "payment")
        self.assertEqual(session_kwargs["payment_intent_data"]["setup_future_usage"], "off_session")
        self.assertEqual(session_kwargs["payment_intent_data"]["metadata"]["is_odoo_managed"], "true")
        self.assertEqual(session_kwargs["payment_intent_data"]["metadata"]["billing_duration"], "month")

        service = self.env["rollup.service"].search([("service_id", "=", response_data["service_uuid"])], limit=1)
        self.assertTrue(service.is_odoo_managed)

    @patch("odoo.addons.rollup_management.utils.deployment_utils.get_stripe_client")
    def test_start_checkout_v2_quarterly(self, mock_get_client):
        """V2 should honor quarterly billing duration."""

        client = MagicMock()
        client.api_key = "sk_test"
        client.checkout = MagicMock()
        client.checkout.Session.create.return_value = MagicMock(id="cs_v2_q", url="...")
        mock_get_client.return_value = client

        payload = {
            "type_id": self.rollup_type.id,
            "name": "V2 Quarterly",
            "region_ids": [self.location.id],
            "is_odoo_managed": True,
            "billing_duration": "quarter",
            "network_type": "mainnet"
        }

        _, _, response_data = deployment_utils.start_checkout(self.env.user, payload)
        self.assertEqual(float(response_data["amount"]), 400.0)
