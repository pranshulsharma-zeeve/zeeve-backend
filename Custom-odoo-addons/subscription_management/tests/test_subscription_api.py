"""Tests for Subscription API endpoints."""

import base64
import json

from odoo.tests.common import HttpCase
from odoo.http import root
from odoo import fields


class TestSubscriptionAPI(HttpCase):
    """Validate subscription listing and detail APIs."""

    def setUp(self):
        super().setUp()
        # Create a test user and log in to retrieve a session token
        user = self.env["res.users"].sudo().create(
            {
                "name": "Test User",
                "login": "tester@example.com",
                "email": "tester@example.com",
                "password": "secret",
            }
        )
        user.partner_id.write({"email_verified": True})
        payload = json.dumps({"email": "tester@example.com", "password": "secret"})
        headers = {"Content-Type": "application/json"}
        resp = self.url_open("/api/v1/login", data=payload, headers=headers)
        data = json.loads(resp.data.decode())
        self.token = data["data"]["access_token"]
        self.auth_header = f"Bearer {self.token}"
        session = root.session_store.get(self.token)
        self.assertIsNotNone(session)
        self.assertEqual(session.get("uid"), user.id)

        # Create required objects for a subscription
        self.protocol = self.env["protocol.master"].sudo().create(
            {
                "name": "Ethereum",
                "image": base64.b64encode(b"img"),
                "short_name": "ETH",
                "is_rpc": True,
            }
        )

        self.plan = self.env["subscription.plan"].sudo().create(
            {
                "name": "Test Plan",
                "subscription_type": "rpc",
                "protocol_id": self.protocol.id,
                "duration": 1,
                "unit": "month",
                "plan_amount": 10.0,
            }
        )

        tmpl = self.env["product.template"].sudo().create(
            {
                "name": "Plan Product",
                "type": "service",
                "activate_subscription": True,
                "subscription_plan_id": self.plan.id,
                "uom_id": self.env.ref("uom.product_uom_unit").id,
                "uom_po_id": self.env.ref("uom.product_uom_unit").id,
                "list_price": 10.0,
            }
        )
        product = tmpl.product_variant_id

        self.subscription = self.env["subscription.subscription"].sudo().create(
            {
                "customer_name": user.partner_id.id,
                "subscription_type": "rpc",
                "protocol_id": self.protocol.id,
                "product_id": product.id,
                "sub_plan_id": self.plan.id,
                "payment_frequency": "monthly",
                "duration": 1,
                "unit": "month",
                "price": 10.0,
                "start_date": fields.Date.today(),
            }
        )
        self.network = self.env["zeeve.network.type"].sudo().create({"name": "Mainnet"})
        self.env["subscription.node"].sudo().create({
            "subscription_id": self.subscription.id,
            "node_name": "Primary Node",
            "node_type": "rpc",
            "network_selection_id": self.network.id,
            "software_update_rule": "auto",
        })

    def test_list_purchased_subscriptions(self):
        """Should return subscriptions for the logged-in user."""

        res = self.url_open(
            "/api/v1/list/node?nodeType=RPC",
            headers={"Authorization": self.auth_header},
        )
        self.assertEqual(res.status_code, 200)
        data = res.json()
        self.assertTrue(data["success"])
        self.assertEqual(len(data["data"]), 1)
        item = data["data"][0]
        primary_node = self.subscription.node_ids[:1]
        self.assertEqual(item["subscription_id"], self.subscription.subscription_id)
        self.assertEqual(item["node_id"], primary_node.node_identifier)
        self.assertEqual(item["protocol_id"], self.protocol.id)
        self.assertIn("nodes", item)
        self.assertEqual(len(item["nodes"]), 1)
        self.assertEqual(item["nodes"][0]["node_name"], "Primary Node")

    def test_node_details(self):
        """Should return subscription details by subscription identifier."""

        url = f"/api/v1/details/node?nodeType=RPC&subscription_id={self.subscription.subscription_id}"
        res = self.url_open(url, headers={"Authorization": self.auth_header})
        self.assertEqual(res.status_code, 200)
        data = res.json()
        self.assertTrue(data["success"])
        primary_node = self.subscription.node_ids[:1]
        self.assertEqual(data["data"]["subscription_id"], self.subscription.subscription_id)
        self.assertEqual(data["data"]["node_id"], primary_node.node_identifier)
        self.assertEqual(data["data"]["protocol_id"], self.protocol.id)
        self.assertIn("nodes", data["data"])
        self.assertEqual(len(data["data"]["nodes"]), 1)
        self.assertEqual(data["data"]["nodes"][0]["node_name"], "Primary Node")
