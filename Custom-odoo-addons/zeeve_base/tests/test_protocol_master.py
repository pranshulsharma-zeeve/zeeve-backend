"""Tests for ProtocolMaster model."""

import base64

from odoo.tests.common import TransactionCase


class TestProtocolMaster(TransactionCase):
    """Validate creation and archiving of protocol records."""

    def test_create_and_archive(self):
        """Protocols should be enabled by default and can be archived."""
        protocol = self.env["protocol.master"].create(
            {
                "name": "Ethereum",
                "image": base64.b64encode(b"test"),
                "short_name": "ETH",
            }
        )
        self.assertTrue(protocol.enabled)

        # Archive the protocol and ensure the flag reflects the change
        protocol.enabled = False
        self.assertFalse(protocol.enabled)
