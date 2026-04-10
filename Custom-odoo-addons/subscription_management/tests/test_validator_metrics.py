import base64
from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import patch

from odoo import fields  # type: ignore[import-not-found]
from odoo.tests.common import TransactionCase  # type: ignore[import-not-found]

from ..utils import subscription_helpers as helpers


class TestValidatorMetrics(TransactionCase):
    """Ensure validator history helpers honor protocol scoping."""

    def setUp(self):
        super().setUp()
        self.coreum_protocol = self._create_protocol("Coreum", "CORE")
        self.avalanche_protocol = self._create_protocol("Avalanche", "AVAX")

    def _create_protocol(self, name: str, short_name: str):
        return self.env["protocol.master"].create(
            {
                "name": name,
                "short_name": short_name,
                "image": base64.b64encode(name.encode("utf-8")),
                "web_url": f"https://{name.lower()}.example",
            }
        )

    def _mock_request(self):
        return patch(
            "odoo.addons.subscription_management.utils.subscription_helpers.request",
            SimpleNamespace(env=self.env),
        )

    def test_rewards_snapshots_filter_by_protocol(self):
        snapshot_vals = {
            "valoper": "shared-validator",
            "snapshot_date": fields.Datetime.now(),
            "total_stake": 100.0,
            "delegator_count": 5,
        }
        self.env["validator.rewards.snapshot"].create(
            {
                **snapshot_vals,
                "outstanding_rewards": 10.0,
                "protocol_id": self.coreum_protocol.id,
                "protocol_key": "coreum",
            }
        )
        self.env["validator.rewards.snapshot"].create(
            {
                **snapshot_vals,
                "outstanding_rewards": 25.0,
                "protocol_id": self.avalanche_protocol.id,
                "protocol_key": "avalanche",
            }
        )

        with self._mock_request():
            coreum_series = helpers._fetch_validator_rewards_with_period(
                "shared-validator",
                self.coreum_protocol.id,
                period_days=7,
            )
        with self._mock_request():
            avalanche_series = helpers._fetch_validator_rewards_with_period(
                "shared-validator",
                self.avalanche_protocol.id,
                period_days=7,
            )

        self.assertEqual(len(coreum_series["series"]), 1)
        self.assertEqual(coreum_series["series"][0]["value"], 10.0)
        self.assertEqual(len(avalanche_series["series"]), 1)
        self.assertEqual(avalanche_series["series"][0]["value"], 25.0)

    def test_stake_snapshots_filter_by_protocol(self):
        shared_date = fields.Datetime.now()
        self.env["validator.rewards.snapshot"].create(
            {
                "valoper": "stake-validator",
                "snapshot_date": shared_date,
                "outstanding_rewards": 0.0,
                "total_stake": 150.0,
                "delegator_count": 15,
                "protocol_id": self.coreum_protocol.id,
                "protocol_key": "coreum",
            }
        )
        self.env["validator.rewards.snapshot"].create(
            {
                "valoper": "stake-validator",
                "snapshot_date": shared_date,
                "outstanding_rewards": 0.0,
                "total_stake": 300.0,
                "delegator_count": 30,
                "protocol_id": self.avalanche_protocol.id,
                "protocol_key": "avalanche",
            }
        )

        with self._mock_request():
            coreum_series = helpers._fetch_validator_stake_delegator_with_period(
                "stake-validator",
                self.coreum_protocol.id,
                period_days=7,
            )
        with self._mock_request():
            avalanche_series = helpers._fetch_validator_stake_delegator_with_period(
                "stake-validator",
                self.avalanche_protocol.id,
                period_days=7,
            )

        self.assertEqual(coreum_series["tokens"][0]["value"], 150.0)
        self.assertEqual(coreum_series["delegatorCount"][0]["value"], 15)
        self.assertEqual(avalanche_series["tokens"][0]["value"], 300.0)
        self.assertEqual(avalanche_series["delegatorCount"][0]["value"], 30)

    def test_avalanche_performance_series_uses_snapshot_values(self):
        base_date = fields.Datetime.now() - timedelta(hours=1)
        self.env["validator.performance.snapshot"].create(
            {
                "valoper": "node-avalanche",
                "valcons_addr": "node-avalanche",
                "height": 10,
                "missed_counter": 5,
                "window_size": 100,
                "snapshot_date": base_date,
                "protocol_key": "avalanche",
                "protocol_id": self.avalanche_protocol.id,
            }
        )
        self.env["validator.performance.snapshot"].create(
            {
                "valoper": "node-avalanche",
                "valcons_addr": "node-avalanche",
                "height": 20,
                "missed_counter": 10,
                "window_size": 100,
                "snapshot_date": base_date + timedelta(minutes=30),
                "protocol_key": "avalanche",
                "protocol_id": self.avalanche_protocol.id,
            }
        )

        with self._mock_request():
            payload = helpers._fetch_validator_performance_with_period(
                "node-avalanche",
                "avalanche",
                "https://avax.example",
                period_days=1,
                protocol_record_id=self.avalanche_protocol.id,
            )

        self.assertEqual(len(payload["series"]), 2)
        self.assertEqual(payload["series"][0]["missed"], 5)
        self.assertEqual(payload["series"][1]["missed"], 10)