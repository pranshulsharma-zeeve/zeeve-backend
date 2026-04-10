"""Tests for Restake helper utilities."""
from unittest.mock import MagicMock, patch

from odoo import fields
from odoo.exceptions import UserError
from odoo.tests.common import TransactionCase

from ..utils import restake_helper


class TestRestakeHelper(TransactionCase):
    def setUp(self):
        super().setUp()
        self.partner = self.env['res.partner'].create({
            'name': 'Validator Owner',
            'email': 'owner@example.com',
        })
        self.protocol = self.env['protocol.master'].create({
            'name': 'Coreum',
            'short_name': 'CORE',
            'is_rpc': False,
        })
        self.plan = self.env['subscription.plan'].create({
            'name': 'Validator Plan',
            'subscription_type': 'validator',
            'protocol_id': self.protocol.id,
            'duration': 1,
            'unit': 'month',
            'plan_amount': 100.0,
        })
        product_template = self.env['product.template'].create({
            'name': 'Validator Product',
            'type': 'service',
            'list_price': 100.0,
            'activate_subscription': True,
            'subscription_plan_id': self.plan.id,
            'uom_id': self.env.ref('uom.product_uom_unit').id,
            'uom_po_id': self.env.ref('uom.product_uom_unit').id,
        })
        product = product_template.product_variant_id
        self.subscription = self.env['subscription.subscription'].create({
            'customer_name': self.partner.id,
            'subscription_type': 'validator',
            'protocol_id': self.protocol.id,
            'product_id': product.id,
            'sub_plan_id': self.plan.id,
            'payment_frequency': 'monthly',
            'duration': 1,
            'unit': 'month',
            'price': 100.0,
            'start_date': fields.Date.today(),
            'validator_info': "{\"validatorAddress\": \"corevaloper1test\", \"validatorName\": \"Test Coreum\", \"networkType\": \"testnet\"}",
        })
        self.network = self.env['zeeve.network.type'].create({'name': 'Testnet'})
        self.env['subscription.node'].create({
            'subscription_id': self.subscription.id,
            'node_type': 'validator',
            'node_name': 'Coreum Validator',
            'network_selection_id': self.network.id,
            'software_update_rule': 'auto',
        })
        config = self.env['ir.config_parameter'].sudo()
        config.set_param(restake_helper.GITHUB_ACCESS_TOKEN_KEY, 'token')
        config.set_param(restake_helper.GITHUB_USERNAME_KEY, 'fork-user')
        config.set_param(restake_helper.GITHUB_REPO_NAME_KEY, 'validator-registry')
        config.set_param(restake_helper.GITHUB_MAIN_OWNER_KEY, 'eco-stake')
        config.set_param(restake_helper.GITHUB_BASE_BRANCH_KEY, 'master')
        config.set_param(restake_helper.ZABBIX_URL_KEY, 'https://zabbix.local')
        config.set_param(restake_helper.ZABBIX_BEARER_TOKEN_KEY, 'token')

    @patch('subscription_management.utils.restake_helper.requests.post')
    @patch('subscription_management.utils.restake_helper.create_pull_request', return_value=42)
    @patch('subscription_management.utils.restake_helper.add_file')
    @patch('subscription_management.utils.restake_helper.create_branch')
    @patch('subscription_management.utils.restake_helper._get_repositories')
    @patch('subscription_management.utils.restake_helper._get_github_client')
    def test_enable_restake_success(self, mock_client, mock_repos, mock_branch, mock_add_file, mock_pr, mock_post):
        fork_repo = MagicMock()
        upstream_repo = MagicMock()
        mock_repos.return_value = (fork_repo, upstream_repo, 'fork-user', 'validator-registry', 'master')
        mock_client.return_value = MagicMock()
        response = MagicMock()
        response.json.return_value = {}
        response.raise_for_status.return_value = None
        mock_post.return_value = response

        result = restake_helper.enable_restake(
            self.env,
            host_id='12345',
            node_identifier=self.subscription.node_ids[:1].node_identifier,
            minimum_reward=10,
            interval=4,
            partner_id=self.partner.id,
            user_email=self.partner.email,
        )

        self.assertTrue(result['is_active'])
        self.assertEqual(result['interval'], 4)
        self.assertEqual(result['minimum_reward'], 10)
        self.assertEqual(result['github_pr_number'], 42)
        self.assertIn('restake', self.subscription.metaData)
        self.assertEqual(self.subscription.metaData['restake']['bot_address'], result['bot_address'])
        mock_branch.assert_called_once()
        self.assertEqual(mock_add_file.call_count, 2)
        mock_post.assert_called_once()
        _, kwargs = mock_post.call_args
        self.assertEqual(
            kwargs['json']['params']['macros'],
            [
                {"macro": "{$RESTAKE_INTERVAL}", "value": "4h"},
                {"macro": "{$RESTAKE_MIN_REWARD}", "value": "10"},
            ],
        )

    def test_enable_restake_invalid_owner(self):
        with self.assertRaises(UserError):
            restake_helper.enable_restake(
                self.env,
                host_id='12345',
                node_identifier=self.subscription.node_ids[:1].node_identifier,
                minimum_reward=1,
                interval=1,
                partner_id=9999,
                user_email='owner@example.com',
            )

    def test_enable_restake_rejects_fractional_reward(self):
        with self.assertRaises(UserError):
            restake_helper.enable_restake(
                self.env,
                host_id='12345',
                node_identifier=self.subscription.node_ids[:1].node_identifier,
                minimum_reward=0.5,
                interval=4,
                partner_id=self.partner.id,
                user_email=self.partner.email,
            )
