"""Tests for the rollup management module."""

from datetime import datetime, timezone
from unittest.mock import MagicMock, patch

from dateutil.relativedelta import relativedelta

from odoo import fields
from odoo.exceptions import UserError, ValidationError
from odoo.tests.common import TransactionCase

from odoo.addons.rollup_management.utils import deployment_utils, rollup_util


class TestRollupService(TransactionCase):
    """Validate core behaviours of rollup services and nodes."""

    def setUp(self):
        super().setUp()
        country = self.env.ref("base.us")
        self.location = self.env["server.location"].create({
            "name": "Test Region",
            "country_id": country.id,
        })
        self.rollup_type = self.env["rollup.type"].create({
            "name": "Test Rollup",
            "default_region_ids": [(6, 0, [self.location.id])],
            "cost": 199.0,
            "payment_frequency": "month",
        })
        product_template = self.env["product.template"].create({
            "name": "Rollup Product",
            "type": "service",
            "list_price": 199.0,
            "sale_ok": True,
        })
        self.product = product_template.product_variant_id
        self.rollup_type.related_product_id = self.product.id
        self.partner = self.env["res.partner"].create({
            "name": "Rollup Tester",
            "email": "tester@example.com",
        })

    def test_service_uuid_and_node_count(self):
        """Services and nodes should use UUID identifiers and compute node counts."""

        service = self.env["rollup.service"].create({
            "name": "QA Orbit",
            "type_id": self.rollup_type.id,
            "region_ids": [(6, 0, [self.location.id])],
            "customer_id": self.partner.id,
        })
        self.assertEqual(len(service.service_id), 36)

        node = self.env["rollup.node"].create({
            "service_id": service.id,
            "node_name": "Sequencer",
            "node_type": "sequencer",
            "status": "running",
            "endpoint_url": "https://example.invalid/rpc",
        })
        self.assertEqual(len(node.nodid), 36)

        service.invalidate_recordset(["node_count"])
        self.assertEqual(service.node_count, 1)

    def test_invoice_created_on_service_creation(self):
        """Creating a service should immediately generate a draft invoice."""

        service = self.env["rollup.service"].create({
            "name": "Invoice Check",
            "type_id": self.rollup_type.id,
            "region_ids": [(6, 0, [self.location.id])],
            "customer_id": self.partner.id,
        })

        self.assertEqual(service.invoice_count, 1)
        invoice = service.invoice_ids
        self.assertTrue(invoice)
        self.assertEqual(invoice.move_type, "out_invoice")
        self.assertEqual(invoice.state, "draft")
        self.assertEqual(invoice.partner_id, self.partner)
        self.assertAlmostEqual(invoice.amount_total, self.rollup_type.cost)
        self.assertEqual(service.metadata_json.get("rollup_invoice_odoo_id"), invoice.id)
        self.assertEqual(service.subscription_status, "pending_payment")

    def test_sync_with_stripe_requires_subscription_id(self):
        """Manual Stripe sync should require a linked subscription."""

        service = self.env["rollup.service"].create({
            "name": "Manual Sync", 
            "type_id": self.rollup_type.id,
            "region_ids": [(6, 0, [self.location.id])],
            "customer_id": self.partner.id,
        })

        with self.assertRaisesRegex(UserError, "Stripe subscription"):
            service.action_sync_with_stripe()

    def test_sync_with_stripe_updates_status_and_billing(self):
        """Manual sync should mirror Stripe status and billing information."""

        service = self.env["rollup.service"].create({
            "name": "Stripe Mirror",
            "type_id": self.rollup_type.id,
            "region_ids": [(6, 0, [self.location.id])],
            "customer_id": self.partner.id,
            "stripe_subscription_id": "sub_manual",
        })

        period_end = datetime(2025, 1, 10, 15, 30, tzinfo=timezone.utc)
        expected_date = fields.Datetime.context_timestamp(service, period_end).date()

        mock_client = MagicMock()
        mock_client.Subscription.retrieve.return_value = {
            "id": "sub_manual",
            "status": "past_due",
            "current_period_end": int(period_end.timestamp()),
        }

        with patch(
            "odoo.addons.rollup_management.models.rollup_service.rollup_util.get_stripe_client",
            return_value=mock_client,
        ):
            service.action_sync_with_stripe()

        self.assertEqual(service.subscription_status, "overdue")
        self.assertEqual(service.next_billing_date, expected_date)

    def test_rollup_type_sync_creates_product(self):
        """Synchronising a rollup type should create the product in Stripe."""

        rollup_type = self.rollup_type
        self.assertFalse(rollup_type.stripe_product_id)

        mock_client = MagicMock()
        mock_client.Product.create.return_value = {
            "id": "prod_sync",
            "default_price": None,
        }
        mock_client.Price.create.return_value = {
            "id": "price_sync",
            "currency": "usd",
            "unit_amount": 19900,
            "recurring": {"interval": "month"},
            "product": "prod_sync",
        }

        with patch(
            "odoo.addons.rollup_management.models.rollup_type.rollup_util.get_stripe_client",
            return_value=mock_client,
        ):
            rollup_type.action_sync_with_stripe()

        mock_client.Product.create.assert_called_once()
        _, create_kwargs = mock_client.Product.create.call_args
        metadata = create_kwargs.get("metadata", {})
        self.assertEqual(metadata.get("odoo_rollup_type_id"), str(rollup_type.id))
        mock_client.Price.create.assert_called_once()
        price_kwargs = mock_client.Price.create.call_args.kwargs
        self.assertEqual(price_kwargs["product"], "prod_sync")
        self.assertEqual(price_kwargs["unit_amount"], 19900)
        self.assertEqual(price_kwargs["currency"], "usd")
        self.assertEqual(price_kwargs["recurring"], {"interval": "month"})
        self.assertEqual(rollup_type.stripe_product_id, "prod_sync")
        self.assertEqual(rollup_type.stripe_price_id, "price_sync")

    @patch("odoo.addons.rollup_management.utils.deployment_utils.get_stripe_client")
    def test_start_checkout_creates_draft_service(self, mock_get_client):
        """Starting checkout should create a draft service and invoice before payment."""

        client = MagicMock()
        client.api_key = "sk_test"
        client.checkout = MagicMock()
        checkout_session = MagicMock()
        checkout_session.id = "cs_test_123"
        checkout_session.url = "https://stripe.example/checkout"
        checkout_session.status = "open"
        client.checkout.Session.create.return_value = checkout_session
        mock_get_client.return_value = client

        icp = self.env["ir.config_parameter"].sudo()
        icp.set_param("stripe_currency", "usd")
        icp.set_param("frontend_url", "https://frontend.example")

        payload = {
            "type_id": self.rollup_type.id,
            "name": "Draft Checkout",
            "region_ids": [self.location.id],
            "configuration": {"foo": "bar"},
        }

        checkout_session_result, checkout_context, response_data = deployment_utils.start_checkout(self.env.user, payload)

        self.assertEqual(checkout_session_result, checkout_session)
        self.assertEqual(checkout_context.metadata["rollup_service_uuid"], response_data["service_uuid"])
        session_kwargs = client.checkout.Session.create.call_args.kwargs
        self.assertTrue(session_kwargs.get("allow_promotion_codes"))

        service = self.env["rollup.service"].search([("service_id", "=", response_data["service_uuid"])], limit=1)
        self.assertTrue(service)
        self.assertEqual(service.status, "draft")
        self.assertEqual(service.stripe_session_id, "cs_test_123")
        invoice = service._get_latest_invoice()
        self.assertTrue(invoice)
        self.assertEqual(invoice.state, "draft")
        self.assertEqual(float(response_data["amount"]), float(self.rollup_type.cost))
        self.assertEqual(float(response_data["original_amount"]), float(self.rollup_type.cost))
        self.assertEqual(float(response_data["discount_amount"]), 0.0)
        self.assertIsNone(response_data["discount_code"])
        self.assertAlmostEqual(service.original_amount, self.rollup_type.cost)
        self.assertEqual(service.discount_amount, 0.0)
        self.assertFalse(service.discount_id)
        session_kwargs = client.checkout.Session.create.call_args.kwargs
        self.assertIn("customer_email", session_kwargs)
        self.assertNotIn("customer", session_kwargs)

    def test_create_invoice_uses_stripe_coupon(self):
        """Invoices created from Stripe data should mirror coupon discounts."""

        discount = self.env["subscription.discount"].create({
            "name": "Rollup Fifty Off",
            "code": "ROLL50",
            "discount_type": "fixed_amount",
            "discount_value": 50.0,
            "valid_from": fields.Datetime.now() - relativedelta(days=1),
            "applicability_scope": "rollup",
        })
        discount.write({
            "stripe_coupon_id": "ROLL50",
            "stripe_synced": True,
        })

        service = self.env["rollup.service"].create({
            "name": "Coupon Service",
            "type_id": self.rollup_type.id,
            "region_ids": [(6, 0, [self.location.id])],
            "customer_id": self.partner.id,
            "stripe_subscription_id": "sub_test_coupon",
        })

        stripe_invoice = {
            "id": "in_test_coupon",
            "amount_paid": 14900,
            "amount_due": 0,
            "amount_total": 14900,
            "total": 14900,
            "subtotal": 19900,
            "total_discount_amounts": [
                {
                    "amount": 5000,
                    "discount": {"coupon": {"id": "ROLL50"}},
                }
            ],
            "discounts": [
                {
                    "coupon": {"id": "ROLL50"},
                }
            ],
            "hosted_invoice_url": "https://stripe.example/invoices/in_test_coupon",
        }

        invoice = service.create_invoice(stripe_invoice)

        self.assertAlmostEqual(invoice.amount_total, 149.0, places=2)
        line = invoice.invoice_line_ids[:1]
        self.assertEqual(line.discount_id, discount)
        self.assertEqual(line.discount_code, discount.code)
        self.assertAlmostEqual(service.discount_amount, 50.0, places=2)
        self.assertEqual(service.discount_id, discount)
        self.assertEqual(service.metadata_json.get("discount_code"), discount.code)
        self.assertEqual(discount.usage_count, 1)

    @patch("odoo.addons.rollup_management.utils.deployment_utils.get_stripe_client")
    def test_start_checkout_applies_discount(self, mock_get_client):
        """Rollup checkout should honour rollup discounts and surface pricing metadata."""

        client = MagicMock()
        client.api_key = "sk_test"
        client.checkout = MagicMock()
        checkout_session = MagicMock()
        checkout_session.id = "cs_test_discount"
        checkout_session.url = "https://stripe.example/checkout"
        checkout_session.status = "open"
        client.checkout.Session.create.return_value = checkout_session
        mock_get_client.return_value = client

        icp = self.env["ir.config_parameter"].sudo()
        icp.set_param("stripe_currency", "usd")
        icp.set_param("frontend_url", "https://frontend.example")

        discount = self.env["subscription.discount"].create({
            "name": "Rollup Launch",
            "code": "ROLL20",
            "discount_type": "fixed_amount",
            "discount_value": 20.0,
            "valid_from": fields.Datetime.now(),
            "applicability_scope": "rollup",
            "stripe_coupon_id": "coupon_roll20",
            "stripe_synced": True,
        })

        payload = {
            "type_id": self.rollup_type.id,
            "name": "Discounted Checkout",
            "region_ids": [self.location.id],
            "configuration": {"foo": "bar"},
            "discount_code": "ROLL20",
        }

        checkout_session_result, checkout_context, response_data = deployment_utils.start_checkout(self.env.user, payload)

        final_amount = float(self.rollup_type.cost - 20.0)
        self.assertEqual(checkout_session_result, checkout_session)
        self.assertEqual(float(response_data["amount"]), final_amount)
        self.assertEqual(float(response_data["original_amount"]), float(self.rollup_type.cost))
        self.assertEqual(float(response_data["discount_amount"]), 20.0)
        self.assertEqual(response_data["discount_code"], "ROLL20")
        service = self.env["rollup.service"].search([("service_id", "=", response_data["service_uuid"])], limit=1)
        self.assertTrue(service)
        self.assertEqual(service.discount_id, discount)
        self.assertEqual(service.discount_code, "ROLL20")
        self.assertAlmostEqual(service.discount_amount, 20.0)
        self.assertAlmostEqual(service.original_amount, self.rollup_type.cost)
        invoice = service._get_latest_invoice()
        self.assertTrue(invoice)
        self.assertAlmostEqual(invoice.amount_total, final_amount)
        self.assertEqual(invoice.invoice_line_ids[:1].discount_id, discount)
        session_kwargs = client.checkout.Session.create.call_args.kwargs
        self.assertIn({"coupon": "coupon_roll20"}, session_kwargs.get("discounts", []))

    def test_start_checkout_rejects_unknown_discount(self):
        """Checkout should raise a rollup error when an invalid coupon code is supplied."""

        payload = {
            "type_id": self.rollup_type.id,
            "name": "Unknown Discount Checkout",
            "region_ids": [self.location.id],
            "discount_code": "DOESNOTEXIST",
        }

        with self.assertRaisesRegex(rollup_util.RollupError, "Invalid discount code"):
            deployment_utils.start_checkout(self.env.user, payload)

        service = self.env["rollup.service"].search([("name", "=", "Unknown Discount Checkout")])
        self.assertFalse(service)

    def test_start_checkout_rejects_unsynced_discount(self):
        """Coupons without a Stripe identifier should be rejected for rollup checkouts."""

        discount = self.env["subscription.discount"].create({
            "name": "Unsynced Rollup Coupon",
            "code": "ROLLUNSYNC",
            "discount_type": "percentage",
            "discount_value": 10.0,
            "valid_from": fields.Datetime.now(),
            "applicability_scope": "rollup",
        })

        payload = {
            "type_id": self.rollup_type.id,
            "name": "Unsynced Discount Checkout",
            "region_ids": [self.location.id],
            "discount_code": discount.code,
        }

        with self.assertRaisesRegex(rollup_util.RollupError, "Discount is not synced with Stripe"):
            deployment_utils.start_checkout(self.env.user, payload)

        service = self.env["rollup.service"].search([("name", "=", "Unsynced Discount Checkout")])
        self.assertFalse(service)

    @patch("odoo.addons.mail.models.mail_template.MailTemplate.send_mail")
    def test_manual_invoice_send_action(self, mock_send):
        """Manual invoice sending should dispatch the configured template."""

        service = self.env["rollup.service"].with_context(test_mail_silence=True).create({
            "name": "Manual Invoice",
            "type_id": self.rollup_type.id,
            "region_ids": [(6, 0, [self.location.id])],
            "customer_id": self.partner.id,
        })

        mock_send.reset_mock()
        action = service.action_send_invoice_email()
        self.assertEqual(action["type"], "ir.actions.client")
        mock_send.assert_called()

    @patch("odoo.addons.rollup_management.utils.deployment_utils.get_stripe_client")
    def test_start_checkout_reuses_existing_stripe_customer(self, mock_get_client):
        """Checkout should reuse the stored Stripe customer identifier when present."""

        client = MagicMock()
        client.api_key = "sk_test"
        client.checkout = MagicMock()
        checkout_session = MagicMock()
        checkout_session.id = "cs_test_reuse"
        checkout_session.url = "https://stripe.example/reuse"
        checkout_session.status = "open"
        client.checkout.Session.create.return_value = checkout_session
        mock_get_client.return_value = client

        self.partner.stripe_customer_id = "cus_existing"

        payload = {
            "type_id": self.rollup_type.id,
            "name": "Existing Customer",
            "region_ids": [self.location.id],
        }

        deployment_utils.start_checkout(self.env.user, payload)

        session_kwargs = client.checkout.Session.create.call_args.kwargs
        self.assertEqual(session_kwargs.get("customer"), "cus_existing")
        self.assertNotIn("customer_email", session_kwargs)

    @patch("odoo.addons.rollup_management.utils.deployment_utils.get_stripe_client")
    def test_start_checkout_reuses_draft_pending_record(self, mock_get_client):
        """Checkout should reuse an existing draft/pending_payment record for the same type."""

        client = MagicMock()
        client.api_key = "sk_test"
        client.checkout = MagicMock()
        checkout_session = MagicMock()
        checkout_session.id = "cs_reuse_1"
        client.checkout.Session.create.return_value = checkout_session
        mock_get_client.return_value = client

        # 1. Create initial draft/pending_payment record
        payload = {
            "type_id": self.rollup_type.id,
            "name": "First Attempt",
            "region_ids": [self.location.id],
            "configuration": {"foo": "bar"},
        }
        _, _, data1 = deployment_utils.start_checkout(self.env.user, payload)
        service1_uuid = data1["service_uuid"]
        service1 = self.env["rollup.service"].search([("service_id", "=", service1_uuid)])
        self.assertTrue(service1)
        self.assertEqual(service1.status, "draft")
        self.assertEqual(service1.subscription_status, "pending_payment")

        # 2. Call start_checkout again with DIFFERENT name but SAME type
        # It should reuse service1 but update its values
        payload2 = {
            "type_id": self.rollup_type.id,
            "name": "Second Attempt",
            "region_ids": [self.location.id],
            "configuration": {"foo": "baz"},
        }
        checkout_session2 = MagicMock()
        checkout_session2.id = "cs_reuse_2"
        client.checkout.Session.create.return_value = checkout_session2

        _, _, data2 = deployment_utils.start_checkout(self.env.user, payload2)
        service2_uuid = data2["service_uuid"]

        # Verify it's the same record
        self.assertEqual(service1_uuid, service2_uuid)
        service1.invalidate_recordset(["name", "inputs_json"])
        self.assertEqual(service1.name, "Second Attempt")
        self.assertEqual(service1.inputs_json["configuration"]["foo"], "baz")

        # Verify no new record was created for this user/type
        all_services = self.env["rollup.service"].search([
            ("customer_id", "=", self.partner.id),
            ("type_id", "=", self.rollup_type.id)
        ])
        self.assertEqual(len(all_services), 1)

    @patch("odoo.addons.mail.models.mail_template.MailTemplate.send_mail")
    def test_invoice_email_sent_on_creation(self, mock_send):
        """Creating a service should immediately email the draft invoice."""

        mock_send.reset_mock()
        with patch("odoo.addons.account.models.account_move.AccountMove._get_invoice_legal_documents") as mock_get_docs:
            mock_get_docs.return_value = {
                "filename": "INV_TEST.pdf",
                "filetype": "application/pdf",
                "content": b"PDF",
            }
            self.env["rollup.service"].create({
                "name": "Email Draft",
                "type_id": self.rollup_type.id,
                "region_ids": [(6, 0, [self.location.id])],
                "customer_id": self.partner.id,
            })

        self.assertTrue(mock_send.called)
        call_kwargs = mock_send.call_args.kwargs
        self.assertTrue(call_kwargs.get("email_values", {}).get("attachment_ids"))

    @patch("odoo.addons.mail.models.mail_template.MailTemplate.send_mail")
    def test_invoice_email_context_includes_checkout_url(self, mock_send):
        """The invoice email context should surface the checkout link while unpaid."""

        service = self.env["rollup.service"].with_context(
            rollup_auto_send_invoice=False, test_mail_silence=True
        ).create({
            "name": "Checkout Link",
            "type_id": self.rollup_type.id,
            "region_ids": [(6, 0, [self.location.id])],
            "customer_id": self.partner.id,
        })
        invoice = service._get_latest_invoice()
        service.write(
            {
                "metadata_json": service._combined_metadata(
                    {
                        "stripe_checkout_url": "https://stripe.test/pay",
                        "stripe_invoice_url": "https://portal.test/invoice",
                    }
                )
            }
        )

        context_unpaid = service._prepare_invoice_email_context(invoice)
        self.assertEqual(context_unpaid["default_checkout_url"], "https://stripe.test/pay")

        invoice.action_post()
        service._create_or_update_payment(
            invoice=invoice,
            amount=invoice.amount_total,
            currency_code=invoice.currency_id.name,
            payment_intent="pi_ctx",
            stripe_invoice_id="in_ctx",
        )
        invoice.invalidate_recordset(["payment_state"])
        context_paid = service._prepare_invoice_email_context(invoice)
        self.assertFalse(context_paid["default_checkout_url"])
        self.assertTrue(mock_send.called)

    def test_deployment_flow_updates_status(self):
        """Deployment helpers should move services through the lifecycle and create nodes."""

        service = self.env["rollup.service"].create({
            "name": "Flow Test",
            "type_id": self.rollup_type.id,
            "region_ids": [(6, 0, [self.location.id])],
            "inputs_json": {
                "nodes": [
                    {
                        "name": "RPC",
                        "type": "rpc",
                        "status": "running",
                        "endpoint": "https://rpc.invalid",
                        "metadata": {"provider": "test"},
                    }
                ]
            },
            "customer_id": self.partner.id,
        })

        self.assertEqual(service.status, "draft")
        service.action_start_deployment({"stripe_session_id": "sess_123"}, auto_activate=False)
        self.assertEqual(service.status, "deploying")

        service._complete_deployment({"provisioning_status": "completed"})
        self.assertEqual(service.status, "active")
        self.assertTrue(service.node_ids)
        self.assertEqual(service.node_count, 1)
        self.assertIn("provisioning_status", service.metadata_json)
        self.assertEqual(service.metadata_json.get("stripe_session_id"), "sess_123")

    def test_payment_logs_linked_on_metadata_update(self):
        """Stripe payment logs should link to services when metadata is provided."""

        event_payload = {
            "id": "evt_123",
            "type": "checkout.session.completed",
            "data": {
                "object": {
                    "id": "cs_test_456",
                    "payment_intent": "pi_test_789",
                    "metadata": {
                        "deployment_token": "deploy-token-1",
                    },
                }
            },
        }

        log = self.env["stripe.payment.log"].create_log_entry(
            event_id=event_payload["id"],
            event_type=event_payload["type"],
            event_data=event_payload,
            stripe_created=fields.Datetime.now(),
        )

        service = self.env["rollup.service"].create({
            "name": "Logging Test",
            "type_id": self.rollup_type.id,
            "region_ids": [(6, 0, [self.location.id])],
            "customer_id": self.partner.id,
        })

        metadata_update = {
            "stripe_session_id": "cs_test_456",
            "stripe_payment_intent_id": "pi_test_789",
            "deployment_token": "deploy-token-1",
        }

        service.action_start_deployment(metadata_update, auto_activate=False)

        log.invalidate_recordset(["rollup_service_id", "transaction_hash"])
        service.invalidate_recordset(["payment_log_count"])

        self.assertEqual(log.rollup_service_id, service)
        self.assertEqual(log.transaction_hash, "pi_test_789")
        self.assertEqual(service.payment_log_count, 1)

    def test_uuid_fields_validate_format(self):
        """UUID fields must reject non canonical values."""

        with self.assertRaises(ValidationError):
            self.env["rollup.service"].create({
                "name": "Invalid UUID Service",
                "type_id": self.rollup_type.id,
                "region_ids": [(6, 0, [self.location.id])],
                "customer_id": self.partner.id,
                "service_id": "1234",
            })

        with self.assertRaises(ValidationError):
            self.env["rollup.node"].create({
                "service_id": self.env["rollup.service"].create({
                    "name": "Valid Service",
                    "type_id": self.rollup_type.id,
                    "region_ids": [(6, 0, [self.location.id])],
                    "customer_id": self.partner.id,
                }).id,
                "node_name": "Invalid Node",
                "node_type": "sequencer",
                "status": "running",
                "nodid": "not-a-uuid",
            })

    @patch("odoo.addons.rollup_management.models.rollup_service.deployment_utils.get_stripe_client")
    def test_payment_context_processing_creates_log(self, mock_get_client):
        """Payment context stored in metadata should create logs, invoices, and payments."""

        client = MagicMock()
        client.api_key = "sk_test"
        client.Invoice.retrieve.return_value = {
            "hosted_invoice_url": "https://stripe.example/invoice",
            "invoice_pdf": "https://stripe.example/invoice.pdf",
        }
        mock_get_client.return_value = client

        service = self.env["rollup.service"].create({
            "name": "Payment Service",
            "type_id": self.rollup_type.id,
            "region_ids": [(6, 0, [self.location.id])],
            "customer_id": self.partner.id,
        })

        session_payload = {
            "id": "cs_test_payment",
            "payment_intent": "pi_test_payment",
            "subscription": "sub_test",
            "customer": "cus_test",
            "invoice": "in_test",
            "amount_total": int(self.rollup_type.cost * 100),
            "currency": "usd",
            "payment_status": "paid",
        }

        metadata_update = {
            "stripe_session_id": session_payload["id"],
            "stripe_payment_intent_id": session_payload["payment_intent"],
            "stripe_subscription_id": session_payload["subscription"],
            "stripe_customer_id": session_payload["customer"],
            "stripe_invoice_id": session_payload["invoice"],
            "stripe_amount": str(self.rollup_type.cost),
            "rollup_payment_context": {"checkout_session": session_payload},
        }

        service.action_start_deployment(metadata_update, auto_activate=False)
        service._handle_payment_post_activation()

        self.assertTrue(service.payment_log_ids)
        self.assertTrue(service.autopay_enabled)
        self.assertEqual(service.status, "deploying")
        self.assertEqual(service.metadata_json.get("rollup_payment_processed"), True)
        self.assertEqual(service.invoice_count, 1)
        self.assertEqual(service.payment_count, 1)
        invoice = service.invoice_ids
        payment = service.payment_ids
        self.assertEqual(payment.rollup_invoice_id, invoice)
        self.assertEqual(invoice.payment_state, "paid")
        self.assertEqual(service.metadata_json.get("rollup_invoice_odoo_id"), invoice.id)
        self.assertEqual(service.metadata_json.get("rollup_payment_odoo_id"), payment.id)
        self.assertIn("last_payment_email_sent_at", service.metadata_json)

        service.action_activate_service()
        self.assertEqual(service.status, "active")

    @patch("odoo.addons.rollup_management.models.rollup_service.deployment_utils.get_stripe_client")
    def test_admin_controls_update_service_metadata(self, mock_get_client):
        """Admin actions should sync with Stripe and track metadata locally."""

        client = MagicMock()
        client.api_key = "sk_test"
        client.Subscription.modify = MagicMock()
        client.error = MagicMock(StripeError=Exception)
        mock_get_client.return_value = client

        service = self.env["rollup.service"].create({
            "name": "Admin Control",
            "type_id": self.rollup_type.id,
            "region_ids": [(6, 0, [self.location.id])],
            "customer_id": self.partner.id,
            "stripe_subscription_id": "sub_test",
            "metadata_json": {"stripe_subscription_id": "sub_test"},
        })

        service.action_disable_autopay()
        self.assertFalse(service.autopay_enabled)
        self.assertEqual(service.metadata_json.get("autopay_enabled"), False)
        self.assertIn("autopay_updated_at", service.metadata_json)
        client.Subscription.modify.assert_called_with(
            "sub_test",
            collection_method="send_invoice",
            days_until_due=30,
            metadata={"autopay_enabled": "false"},
        )

        client.Subscription.modify.reset_mock()
        service.action_enable_autopay()
        self.assertTrue(service.autopay_enabled)
        self.assertEqual(service.metadata_json.get("autopay_enabled"), True)
        client.Subscription.modify.assert_called_with(
            "sub_test",
            collection_method="charge_automatically",
            metadata={"autopay_enabled": "true"},
        )

        client.Subscription.modify.reset_mock()
        service.action_pause_subscription()
        self.assertEqual(service.status, "paused")
        self.assertEqual(service.metadata_json.get("status_override"), "paused")
        self.assertIn("paused_at", service.metadata_json)
        client.Subscription.modify.assert_called_with(
            "sub_test", pause_collection={"behavior": "mark_uncollectible"}
        )

        client.Subscription.modify.reset_mock()
        service.action_resume_subscription()
        self.assertEqual(service.status, "active")
        self.assertEqual(service.metadata_json.get("status_override"), "active")
        self.assertIn("resumed_at", service.metadata_json)
        client.Subscription.modify.assert_called_with("sub_test", pause_collection="")

    def test_admin_activate_service_button(self):
        """Manual activation should update status and metadata."""

        service = self.env["rollup.service"].create({
            "name": "Admin Activate",
            "type_id": self.rollup_type.id,
            "region_ids": [(6, 0, [self.location.id])],
            "customer_id": self.partner.id,
        })

        service.write({"status": "deploying"})
        service.action_activate_service()

        self.assertEqual(service.status, "active")
        self.assertEqual(service.metadata_json.get("status_override"), "active")
        self.assertIn("admin_activated_at", service.metadata_json)

    def test_process_stripe_invoice_payment_creates_new_invoice(self):
        """Recurring Stripe invoices should create new Odoo invoices and payments."""

        service = self.env["rollup.service"].create({
            "name": "Recurring Billing",
            "type_id": self.rollup_type.id,
            "region_ids": [(6, 0, [self.location.id])],
            "customer_id": self.partner.id,
            "stripe_subscription_id": "sub_live",
            "stripe_customer_id": "cus_live",
        })

        now_dt = fields.Datetime.to_datetime(fields.Datetime.now())
        next_period_end = now_dt + relativedelta(months=1)
        invoice_payload = {
            "id": "in_test_recurring",
            "subscription": "sub_live",
            "customer": "cus_live",
            "payment_intent": "pi_test_recurring",
            "amount_paid": int(self.rollup_type.cost * 100),
            "currency": "usd",
            "created": int(now_dt.timestamp()),
            "due_date": int(now_dt.timestamp()),
            "hosted_invoice_url": "https://stripe.example/invoice",
            "invoice_pdf": "https://stripe.example/invoice.pdf",
            "number": "INV-ROLLUP-001",
            "status": "paid",
            "lines": {
                "data": [
                    {
                        "period": {
                            "start": int(now_dt.timestamp()),
                            "end": int(next_period_end.timestamp()),
                        }
                    }
                ]
            },
            "next_payment_attempt": int(next_period_end.timestamp()),
        }

        log_entry = self.env["stripe.payment.log"].create_log_entry(
            event_id="evt_autopay",
            event_type="invoice.payment_succeeded",
            event_data={"data": {"object": invoice_payload}},
            stripe_created=fields.Datetime.now(),
        )

        service.process_stripe_invoice_payment(invoice_payload, log_entry=log_entry)

        self.assertEqual(service.invoice_count, 2)
        recurring_invoice = service.invoice_ids.sorted(key=lambda inv: inv.create_date or inv.id)[-1]
        self.assertEqual(recurring_invoice.stripe_invoice_id, "in_test_recurring")
        self.assertEqual(recurring_invoice.currency_id.name, "USD")
        payment = service.payment_ids.filtered(lambda pay: pay.stripe_invoice_id == "in_test_recurring")
        self.assertTrue(payment)
        self.assertEqual(payment.currency_id.name, "USD")
        self.assertTrue(payment.payment_method_line_id)
        self.assertEqual(log_entry.rollup_service_id, service)
        metadata = service.metadata_json
        self.assertEqual(metadata.get("last_recurring_invoice_number"), "INV-ROLLUP-001")
        self.assertEqual(metadata.get("last_recurring_period_start"), now_dt.date().isoformat())
        self.assertEqual(metadata.get("last_recurring_period_end"), next_period_end.date().isoformat())
        self.assertEqual(metadata.get("next_recurring_billing_date"), next_period_end.date().isoformat())
        self.assertEqual(service.next_billing_date, next_period_end.date())

    def test_process_stripe_invoice_payment_records_discount(self):
        """Recurring Stripe invoices should capture discount usage and metadata."""

        discount = self.env["subscription.discount"].create({
            "name": "Rollup Discount",
            "code": "ROLLDISC",
            "discount_type": "fixed_amount",
            "discount_value": 10.0,
            "valid_from": fields.Datetime.now(),
            "applicability_scope": "rollup",
        })

        service = self.env["rollup.service"].create({
            "name": "Discounted Billing",
            "type_id": self.rollup_type.id,
            "region_ids": [(6, 0, [self.location.id])],
            "customer_id": self.partner.id,
            "stripe_subscription_id": "sub_disc",
            "stripe_customer_id": "cus_disc",
            "discount_id": discount.id,
            "discount_code": discount.code,
            "discount_amount": 10.0,
            "original_amount": self.rollup_type.cost,
        })

        now_dt = fields.Datetime.to_datetime(fields.Datetime.now())
        next_period_end = now_dt + relativedelta(months=1)
        final_amount = float(self.rollup_type.cost - 10.0)
        invoice_payload = {
            "id": "in_test_discount",
            "subscription": "sub_disc",
            "customer": "cus_disc",
            "payment_intent": "pi_test_discount",
            "amount_paid": int(final_amount * 100),
            "currency": "usd",
            "created": int(now_dt.timestamp()),
            "due_date": int(now_dt.timestamp()),
            "hosted_invoice_url": "https://stripe.example/discount",
            "invoice_pdf": "https://stripe.example/discount.pdf",
            "number": "INV-ROLLUP-DISC",
            "status": "paid",
            "metadata": {
                "discount_id": str(discount.id),
                "discount_code": discount.code,
                "discount_amount": "10.0",
                "original_amount": str(self.rollup_type.cost),
            },
            "lines": {
                "data": [
                    {
                        "period": {
                            "start": int(now_dt.timestamp()),
                            "end": int(next_period_end.timestamp()),
                        }
                    }
                ]
            },
            "total_discount_amounts": [{"amount": 1000}],
            "next_payment_attempt": int(next_period_end.timestamp()),
        }

        log_entry = self.env["stripe.payment.log"].create_log_entry(
            event_id="evt_discount",
            event_type="invoice.payment_succeeded",
            event_data={"data": {"object": invoice_payload}},
            stripe_created=fields.Datetime.now(),
        )

        service.process_stripe_invoice_payment(invoice_payload, log_entry=log_entry)

        invoice = service.invoice_ids.sorted(key=lambda inv: inv.create_date or inv.id)[-1]
        self.assertAlmostEqual(invoice.amount_total, final_amount)
        self.assertEqual(invoice.invoice_line_ids[:1].discount_id, discount)
        metadata = service.metadata_json
        self.assertEqual(metadata.get("discount_code"), "ROLLDISC")
        self.assertEqual(metadata.get("discount_amount"), 10.0)
        self.assertEqual(service.discount_id, discount)
        self.assertAlmostEqual(service.discount_amount, 10.0)
        self.assertAlmostEqual(service.original_amount, self.rollup_type.cost)
        refreshed_discount = discount.sudo().browse(discount.id)
        self.assertEqual(refreshed_discount.usage_count, 1)

    def test_multiple_recurring_invoices_create_unique_records(self):
        """Each recurring Stripe charge should create distinct invoices and payments."""

        service = self.env["rollup.service"].create({
            "name": "Recurring Billing",
            "type_id": self.rollup_type.id,
            "region_ids": [(6, 0, [self.location.id])],
            "customer_id": self.partner.id,
            "stripe_subscription_id": "sub_live",
            "stripe_customer_id": "cus_live",
        })

        self.assertEqual(service.invoice_count, 1)

        def _payload(invoice_suffix: str, start_dt, end_dt):
            return {
                "id": f"in_test_{invoice_suffix}",
                "subscription": "sub_live",
                "customer": "cus_live",
                "payment_intent": f"pi_test_{invoice_suffix}",
                "amount_paid": int(self.rollup_type.cost * 100),
                "currency": "usd",
                "created": int(start_dt.timestamp()),
                "due_date": int(start_dt.timestamp()),
                "hosted_invoice_url": f"https://stripe.example/{invoice_suffix}",
                "invoice_pdf": f"https://stripe.example/{invoice_suffix}.pdf",
                "number": f"INV-ROLLUP-00{invoice_suffix[-1]}",
                "status": "paid",
                "lines": {
                    "data": [
                        {
                            "period": {
                                "start": int(start_dt.timestamp()),
                                "end": int(end_dt.timestamp()),
                            }
                        }
                    ]
                },
                "next_payment_attempt": int(end_dt.timestamp()),
            }

        first_start = fields.Datetime.to_datetime(fields.Datetime.now())
        first_end = first_start + relativedelta(months=1)
        first_payload = _payload("recurring1", first_start, first_end)

        log_first = self.env["stripe.payment.log"].create_log_entry(
            event_id="evt_autopay_1",
            event_type="invoice.payment_succeeded",
            event_data={"data": {"object": first_payload}},
            stripe_created=fields.Datetime.now(),
        )

        service.process_stripe_invoice_payment(first_payload, log_entry=log_first)

        second_start = first_end
        second_end = second_start + relativedelta(months=1)
        second_payload = _payload("recurring2", second_start, second_end)

        log_second = self.env["stripe.payment.log"].create_log_entry(
            event_id="evt_autopay_2",
            event_type="invoice.payment_succeeded",
            event_data={"data": {"object": second_payload}},
            stripe_created=fields.Datetime.now(),
        )

        service.process_stripe_invoice_payment(second_payload, log_entry=log_second)

        self.assertEqual(service.invoice_count, 3)
        stripe_invoice_ids = {
            inv.stripe_invoice_id for inv in service.invoice_ids if inv.stripe_invoice_id
        }
        self.assertSetEqual(
            stripe_invoice_ids,
            {"in_test_recurring1", "in_test_recurring2"},
        )

        autopay_payments = service.payment_ids.filtered(
            lambda pay: pay.stripe_invoice_id in {"in_test_recurring1", "in_test_recurring2"}
        )
        self.assertEqual(len(autopay_payments), 2)
        self.assertTrue(all(pay.state == "posted" for pay in autopay_payments))

        metadata = service.metadata_json
        self.assertEqual(metadata.get("stripe_invoice_id"), "in_test_recurring2")
        self.assertEqual(
            metadata.get("next_recurring_billing_date"),
            second_end.date().isoformat(),
        )
        self.assertEqual(self.partner.stripe_customer_id, "cus_live")

    @patch("odoo.addons.rollup_management.utils.deployment_utils.send_rollup_email", return_value=True)
    def test_initial_manual_payment_skips_renewal_mail(self, mock_send):
        """First successful payment should not dispatch the renewal template."""

        service = self.env["rollup.service"].with_context(rollup_auto_send_invoice=False, test_mail_silence=True).create({
            "name": "Initial Payment",
            "type_id": self.rollup_type.id,
            "region_ids": [(6, 0, [self.location.id])],
            "customer_id": self.partner.id,
        })

        invoice = service._get_latest_invoice()
        invoice.action_post()
        payment = service._create_or_update_payment(
            invoice=invoice,
            amount=invoice.amount_total,
            currency_code=invoice.currency_id.name,
            payment_intent="pi_manual_initial",
            stripe_invoice_id="in_manual_initial",
        )

        mock_send.reset_mock()
        service._handle_invoice_paid(invoice, payment)

        templates = [call.args[0] for call in mock_send.call_args_list]
        self.assertNotIn("rollup_management.mail_template_rollup_subscription_renewed", templates)
        self.assertIn("rollup_management.mail_template_rollup_payment_success_admin", templates)

        second_invoice = service._create_invoice_from_amount(service.type_id.cost or 0.0)
        second_invoice.action_post()
        second_payment = service._create_or_update_payment(
            invoice=second_invoice,
            amount=second_invoice.amount_total,
            currency_code=second_invoice.currency_id.name,
            payment_intent="pi_manual_second",
            stripe_invoice_id="in_manual_second",
        )

        mock_send.reset_mock()
        service._handle_invoice_paid(second_invoice, second_payment)
        templates_second = [call.args[0] for call in mock_send.call_args_list]
        self.assertIn("rollup_management.mail_template_rollup_subscription_renewed", templates_second)
        self.assertIn("rollup_management.mail_template_rollup_payment_success_admin", templates_second)

    @patch("odoo.addons.rollup_management.utils.deployment_utils.send_rollup_email", return_value=True)
    def test_autopay_initial_invoice_skips_renewal_mail(self, mock_send):
        """Recurring Stripe webhook should suppress renewal mail on first invoice."""

        service = self.env["rollup.service"].with_context(rollup_auto_send_invoice=False, test_mail_silence=True).create({
            "name": "Autopay Flow",
            "type_id": self.rollup_type.id,
            "region_ids": [(6, 0, [self.location.id])],
            "customer_id": self.partner.id,
        })

        invoice = service._get_latest_invoice()
        invoice.action_post()
        invoice.write({"stripe_invoice_id": "in_auto_initial"})

        payload = {
            "id": "in_auto_initial",
            "subscription": "sub_auto",
            "customer": "cus_auto",
            "payment_intent": "pi_auto_initial",
            "amount_paid": int(service.type_id.cost * 100),
            "currency": "usd",
            "status": "paid",
            "billing_reason": "subscription_create",
            "metadata": {"rollup_service_id": str(service.id)},
        }

        mock_send.reset_mock()
        deployment_utils.handle_invoice_payment_succeeded(payload)

        templates = [call.args[0] for call in mock_send.call_args_list]
        self.assertNotIn("rollup_management.mail_template_rollup_subscription_renewed", templates)
        self.assertIn("rollup_management.mail_template_rollup_payment_success_admin", templates)
        self.assertEqual(self.partner.stripe_customer_id, "cus_auto")
        second_payload = dict(payload)
        second_payload.update(
            {
                "id": "in_auto_second",
                "payment_intent": "pi_auto_second",
                "billing_reason": "subscription_cycle",
            }
        )

        mock_send.reset_mock()
        deployment_utils.handle_invoice_payment_succeeded(second_payload)

        templates_second = [call.args[0] for call in mock_send.call_args_list]
        self.assertIn("rollup_management.mail_template_rollup_subscription_renewed", templates_second)
        self.assertIn("rollup_management.mail_template_rollup_payment_success_admin", templates_second)

    @patch("odoo.addons.mail.models.mail_template.MailTemplate.send_mail")
    def test_manual_payment_updates_service_status(self, mock_send):
        """UI registered payments should reconcile invoices and advance the service."""

        service = self.env["rollup.service"].with_context(rollup_auto_send_invoice=False, test_mail_silence=True).create({
            "name": "Manual Payment",
            "type_id": self.rollup_type.id,
            "region_ids": [(6, 0, [self.location.id])],
            "customer_id": self.partner.id,
        })
        invoice = service._get_latest_invoice()
        invoice.action_post()

        journal = self.env["account.journal"].search(
            [("type", "=", "bank"), ("company_id", "=", self.env.company.id)],
            limit=1,
        )
        payment_method_line = journal.inbound_payment_method_line_ids[:1]
        register = self.env["account.payment.register"].with_context(
            active_model="account.move",
            active_ids=invoice.ids,
        ).create(
            {
                "journal_id": journal.id,
                "payment_method_line_id": payment_method_line.id,
                "amount": invoice.amount_total,
                "payment_date": fields.Date.today(),
            }
        )
        payments = register._create_payments()
        payment = payments[:1]

        invoice.invalidate_recordset(["payment_state", "amount_residual"])
        self.assertEqual(invoice.payment_state, "paid")
        self.assertEqual(service.status, "deploying")
        service.invalidate_recordset(["subscription_status"])
        self.assertEqual(service.subscription_status, "active")
        self.assertEqual(payment.rollup_service_id, service)
        self.assertEqual(payment.rollup_invoice_id, invoice)
        self.assertTrue(service.metadata_json.get("deployment_notifications_sent_at"))
        self.assertEqual(
            service.metadata_json.get("subscription_renewal_last_invoice_id"),
            invoice.id,
        )
        self.assertEqual(
            service.metadata_json.get("subscription_payment_admin_last_invoice_id"),
            invoice.id,
        )
        mock_send.assert_called()

    @patch("odoo.addons.rollup_management.models.account_move.AccountMove._render_rollup_invoice_pdf")
    def test_rollup_invoice_pdf_generation_uses_custom_report(self, mock_render):
        """Rollup invoices should fall back to the bespoke report when generating PDFs."""

        mock_render.return_value = (b"%PDF-ROLLUP%", "pdf")
        service = self.env["rollup.service"].with_context(rollup_auto_send_invoice=False).create({
            "name": "PDF Layout",
            "type_id": self.rollup_type.id,
            "region_ids": [(6, 0, [self.location.id])],
            "customer_id": self.partner.id,
        })
        invoice = service._get_latest_invoice()
        invoice.invoice_pdf_report_id = False

        result = invoice._get_invoice_legal_documents("pdf", allow_fallback=True)
        self.assertTrue(result)
        self.assertTrue(invoice.invoice_pdf_report_id)
        self.assertEqual(invoice.invoice_pdf_report_id.mimetype, "application/pdf")
        mock_render.assert_called_once()
