"""Tests for ServerLocation model."""

from odoo.tests.common import TransactionCase


class TestServerLocation(TransactionCase):
    """Validate creation of server location records."""

    def test_create_location(self):
        """Creating a server location should link to a country."""
        country = self.env.ref("base.us")
        location = self.env["server.location"].create(
            {"name": "US East", "country_id": country.id}
        )
        self.assertEqual(location.country_id, country)
