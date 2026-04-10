# Rollup Management Module

## Key Domain Models

- **rollup.service** – Tracks the lifecycle of each deployed rollup, keeps an immutable public `service_id`, the Stripe linkage metadata, and exposes smart buttons for nodes and payment logs.
- **rollup.type** – Describes each supported rollup flavour including the fixed monthly cost used during checkout, default regions, and related infrastructure catalogues.
- **rollup.node** – Materialises the nodes that are spawned for a service once deployment completes, mirroring user-provided templates.

## Step-Oriented Subscription Flow

The numbered comments in code trace the entire deployment and renewal journey:

1. **Step 1 – `RollupAPIController.deploy_rollup`**: validates the payload for `/api/v1/rollup/service/deploy` and hands off to the helper layer.
2. **Step 2 – `deployment_utils.start_checkout`**: assembles Stripe checkout metadata, updates the draft service, and returns the hosted payment URL.
3. **Step 3 – `deployment_utils.create_provisional_subscription`**: ensures a `rollup.service` record exists and marks it `pending_payment`.
4. **Step 4 – `deployment_utils.handle_invoice_will_be_due`** (via webhook handler) sends the shared reminder template with `${days_to_due}`.
5. **Step 5 – `deployment_utils.handle_invoice_overdue`** cascades the overdue state and issues the due-notice email.
6. **Step 6 – `deployment_utils.handle_invoice_payment_succeeded`** reconciles invoices/payments and triggers renewal and admin alerts.
7. **Step 7 – `deployment_utils.update_subscription_status`** centralises transitions and metadata logging.
8. **Step 8 – `deployment_utils.send_rollup_email`** keeps mail dispatch consistent across reminders, dues, suspensions, and renewals.
9. **Step 9 – `deployment_utils.cron_audit_overdue_subscriptions`** acts as the daily safety net when Stripe webhooks are delayed.

## API & Backoffice Deployment Flow

1. **Checkout kick-off (API)** – Authenticated clients call `POST /api/v1/rollup/service/deploy` which hands off to Step 2. The helper persists or reuses a `rollup.service`, stamps `subscription_status='pending_payment'`, drafts the initial invoice, and returns the Stripe Checkout URL plus deployment token.
2. **Invoice emailing** – `_ensure_initial_invoice` auto-mails the draft invoice (unless silenced via context) so operators and customers see the amount due immediately, regardless of whether the service was created from the API or manually in the back office.
3. **Payment capture** – Stripe webhooks invoke `deployment_utils.handle_invoice_payment_succeeded` which reconciles the invoice/payment, updates `subscription_status='active'`, stores the latest Stripe identifiers, and records that the renewal/admin mails were sent for the specific invoice.
4. **Manual payments** – Accounting staff can create services directly in Odoo, post the draft invoice, and register a payment. `_handle_invoice_paid` performs the same state transition (`subscription_status='active'` and service `status` moving forward from `draft`) and reuses the renewal/admin mail templates so non-Stripe flows stay in sync.
5. **Reminders & overdue handling** – Stripe automation hooks (`invoice.will_be_due`, `invoice.overdue`) and the daily cron both call `deployment_utils.update_subscription_status` so the badge on the form view reflects `pending_payment`, `overdue`, or `suspended`. The cron escalates lingering overdue services to suspended and issues the suspension mail as a safety net if Stripe fails to deliver.
6. **Suspension & cancellations** – Support agents can set the subscription status on the form view (with chatter logging) which calls the same helper, mirroring suspensions to the service `status='overdue'` and cancellations to `status='archived'` for downstream automations.

## Automated Billing & Notification Flow

1. **Checkout Initiation** – `RollupAPIController.deploy_rollup` validates the payload and builds a checkout session through `deployment_utils.start_checkout`, seeding metadata with the deployment token and Stripe IDs.
2. **Service Creation** – When Stripe confirms the session, `deployment_utils.finalize_deployment` creates the `rollup.service`, persists user inputs, and immediately calls `_ensure_initial_invoice` to draft/post the first customer invoice priced from the related `rollup.type`.
3. **Invoice Dispatch** – The newly created invoice triggers `_send_invoice_email`, emailing the customer-facing template (HTML + plain text) that embeds company branding, service facts, amount due, and hosted payment links. A manual "Send Invoice" button on the service form can resend this message if delivery fails.
4. **Payment Tracking** – Stripe webhooks are parsed in `_handle_payment_post_activation`, ensuring we create or update `account.payment` entries with the Stripe invoice ID, intent reference, and reconciliation against the Odoo invoice. Manual payments can be registered from the streamlined payment form without exposing unrelated accounting toggles.
5. **Lifecycle Updates** – Successful payments mark the service `active`, append metadata such as hosted invoice URLs, and queue admin alerts. Smart buttons on the service, invoice, and payment forms link the three records for quick navigation.

### Email Coverage

- **Customer invoice** – `mail_template_rollup_invoice_customer` thanks the user for the deployment, summarises the service profile, and links to the Stripe-hosted invoice plus the internal PDF.
- **Admin notification** – `mail_template_rollup_invoice_admin` notifies the billing team when deployments go live, including core metadata and reconciliation shortcuts.
- Both templates leverage company branding, have plain-text fallbacks, and can be resent manually from the service form.

### Metadata & Audit Trail

- `stripe.payment.log` keeps the raw Stripe payload for each deployment and automatically links to the service once metadata is enriched.
- The invoice and payment forms expose read-only fields for Stripe identifiers (`stripe_invoice_id`, `stripe_payment_intent_id`, `stripe_transaction_reference`) so operators can reconcile with Stripe dashboards without leaving Odoo.

### Operations Toolkit

- **Autopay toggles** – `action_disable_autopay` and `action_enable_autopay` keep the local flag in sync with Stripe collection preferences and timestamp every change.
- **Pause/Resume** – `action_pause_subscription` and `action_resume_subscription` orchestrate the Stripe subscription state and service status while logging the events under `metadata_json` for traceability.

The rollup billing pipeline therefore goes from payload validation → service creation → invoice generation → email delivery → payment reconciliation with no manual intervention, while still offering back-office controls for exceptional handling.

## Autopay Renewal Flow

1. **Stripe webhook (`invoice.payment_succeeded`)** – Stripe calls `/api/stripe/webhook` with the recurring invoice payload. The shared webhook controller logs the event and hands off to `_handle_rollup_invoice_payment` when rollup metadata is detected.
2. **Service reconciliation** – `_handle_rollup_invoice_payment` locates the correct `rollup.service` and calls `RollupService.process_stripe_invoice_payment`, which:
   - creates or updates the monthly `account.move` using `_create_invoice_from_amount`;
   - posts or updates the matching `account.payment` and reconciles it against the invoice;
   - stores Stripe period start/end, the autopay flag, and the next renewal date under `metadata_json` (`last_recurring_period_*`, `next_recurring_billing_date`).
3. **Status & notifications** – The service is set back to `active`, `next_billing_date` is recomputed from the metadata override, payment logs are linked, and `_send_invoice_email` dispatches the confirmation to the customer. Failed renewals travel through `process_failed_invoice_payment`, persisting the error reason and surfacing it in the chatter/logs.

### Fast-track testing

To verify autopay without waiting a full month you can use Stripe test clocks:

1. Create a clock and subscription in Stripe's test mode:
   ```bash
   stripe test_helpers test_clocks create --frozen_time "$(date -u +%Y-%m-%dT%H:%M:%SZ)"
   stripe test_helpers test_clocks start \
     --test_clock CLOCK_ID --frozen_time "$(date -u +%Y-%m-%dT%H:%M:%SZ)"
   stripe subscriptions create --customer CUS_ID --items '[{"price": "PRICE_ID"}]' --test_clock CLOCK_ID
   ```
2. Advance the clock to the next billing date:
   ```bash
   stripe test_helpers test_clocks advance --test_clock CLOCK_ID --frozen_time "$(date -u -d '+32 days' +%Y-%m-%dT%H:%M:%SZ)"
   ```
3. Stripe emits `invoice.payment_succeeded`; confirm the log entry on the Rollup Service smart button, the newly created invoice/payment, and the updated `metadata_json` fields (`next_recurring_billing_date`, `last_recurring_period_end`).

Alternatively, you can replay a webhook directly with the Stripe CLI once a service exists:

```bash
stripe trigger invoice.payment_succeeded \
  --add key=value \
  --override event.data.object.subscription=sub_xxx \
  --override event.data.object.customer=cus_xxx
```

The webhook logs in Odoo and the new invoice/payment records provide a full audit trail for each simulated renewal.

## Verification Checklist

- [ ] Create a subscription through the API deploy endpoint and ensure the returned payload contains the checkout session, service UUID, and deployment token while the service shows `subscription_status='pending_payment'` with a draft invoice.
- [ ] Register a manual payment from the invoice smart button in Odoo and confirm the service transitions to `subscription_status='active'`, deployment notifications are sent once, and the renewal/admin templates log their invoice IDs in `metadata_json`.
- [ ] Replay `invoice.payment_succeeded` from Stripe and verify the webhook reconciles the invoice, updates metadata, records the last sent mail timestamps, and leaves `_handle_invoice_paid` with nothing further to send.
- [ ] Trigger `invoice.will_be_due` and `invoice.overdue` events (or advance the cron) to see reminder, due, and suspension emails reuse the shared templates while the service `status` badge flips to `overdue`.
- [ ] Manually set `subscription_status` to `suspended`/`canceled` from the form and confirm chatter entries capture the reason and the service `status` mirrors the lifecycle for operators.

### Running automated autopay tests locally

Use the dedicated regression tests in `tests/test_rollup_service.py` to exercise the full autopay lifecycle without needing
external Stripe calls:

1. **Create a fresh database** (skip if you already have a disposable test DB):

   ```bash
   createdb rollup_autopay_test
   ```

2. **Execute the autopay test bundle** – the command below installs the `rollup_management` module, runs only the autopay
   scenarios (tagged by the file path), and stops when the tests finish:

   ```bash
   odoo-bin \
     -d rollup_autopay_test \
     --addons-path=Custom-odoo-addons,Standard-odoo-addons \
     --test-tags rollup_management.tests.test_rollup_service \
     --stop-after-init \
     -u rollup_management
   ```

3. **Review the results** – success prints `OK` to the console, while failures show the exact assertion that needs attention.
   The tests post multiple `invoice.payment_succeeded` payloads and assert that new invoices, payments, and metadata updates are
   created for each billing cycle.
