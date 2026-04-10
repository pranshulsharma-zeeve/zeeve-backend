"""Tests for Protocol API endpoint."""

import base64
import json

from odoo.tests.common import HttpCase
from odoo.http import root


class TestProtocolAPI(HttpCase):
    """Validate protocol listing API behavior."""

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

    def test_list_protocols_rpc(self):
        """Should return only RPC-enabled protocols."""
        Protocol = self.env["protocol.master"].sudo()
        rpc_proto = Protocol.create(
            {
                "name": "Ethereum",
                "image": base64.b64encode(b"img"),
                "short_name": "ETH",
                "is_rpc": True,
            }
        )
        Protocol.create(
            {
                "name": "Bitcoin",
                "image": base64.b64encode(b"img"),
                "short_name": "BTC",
                "is_archive": True,
            }
        )
        res = self.url_open(
            "/api/v1/list/protocols?nodeType=RPC",
            headers={"Authorization": self.auth_header},
        )
        self.assertEqual(res.status_code, 200)
        data = res.json()
        self.assertTrue(data["success"])
        self.assertEqual(len(data["data"]), 1)
        self.assertEqual(data["data"][0]["id"], rpc_proto.id)

    def test_list_protocols_missing_token(self):
        """Missing token should return 401."""
        res = self.url_open("/api/v1/list/protocols?nodeType=RPC")
        self.assertEqual(res.status_code, 401)
        data = res.json()
        self.assertFalse(data["success"])

    def test_list_protocols_invalid_token(self):
        """Token without Bearer prefix should be invalid."""
        res = self.url_open(
            "/api/v1/list/protocols?nodeType=RPC",
            headers={"Authorization": self.token},
        )
        self.assertEqual(res.status_code, 401)
        data = res.json()
        self.assertEqual(data["error"], "Invalid access token")

    def test_list_protocols_expired_token(self):
        """Expired token should return 401 with proper message."""
        root.session_store.delete(self.token)
        res = self.url_open(
            "/api/v1/list/protocols?nodeType=RPC",
            headers={"Authorization": self.auth_header},
        )
        self.assertEqual(res.status_code, 401)
        data = res.json()
        self.assertEqual(data["error"], "Access token expired")
