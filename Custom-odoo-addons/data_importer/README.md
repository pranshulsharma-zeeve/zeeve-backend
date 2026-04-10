# Data Importer

Utility helpers for migrating data into Odoo. The current focus of this package is importing legacy subscription records while keeping Odoo and Stripe in sync.

## Subscription CSV Import

- The importer expects Stripe customers and payment methods to already exist; it only links subscriptions.
- Each CSV row is handled by `SubscriptionUtils.handle_subscription_row` (`utils/subscription_utils.py:247`).
- Stripe API credentials are pulled from `ir.config_parameter` per row, so the script must run in an environment where those settings are configured.

### Line-by-Line Flow (`subscription_utils.py`)

- `utils/subscription_utils.py:11` `_retrieve_stripe_customer` fetches an existing Stripe customer when an ID is known.
- `utils/subscription_utils.py:23` `_search_stripe_customer_by_metadata` looks up Stripe customers by Zoho metadata.
- `utils/subscription_utils.py:45` `_search_stripe_customer_by_email` retrieves the first Stripe customer with a matching email.
- `utils/subscription_utils.py:62` `_ensure_odoo_customer` aligns the CSV row with an existing partner.
  - `utils/subscription_utils.py:69` searches partners by email, Stripe ID, then customer name.
  - `utils/subscription_utils.py:88` checks for a linked `res.users` account and logs if missing.
  - `utils/subscription_utils.py:96` refreshes partner email/name to match the CSV.
- `utils/subscription_utils.py:104` resolves the Stripe customer ID using partner data, the CSV customer ID, metadata search, or email search.
- `utils/subscription_utils.py:120` writes the Stripe ID back to the partner when found; otherwise the function returns `None`.
- `utils/subscription_utils.py:134` `_ensure_payment_method_ready` auto-selects a default payment method from attached cards and fails the row when none are present.
- `utils/subscription_utils.py:247` `handle_subscription_row` starts processing a CSV row, loads the Stripe customer, and returns a status dictionary used by the importer to log **success**, **partial**, **skipped**, or **error** outcomes.
- `utils/subscription_utils.py:268` ensures the Stripe customer has a default payment method (auto-selects one from attached cards, otherwise the row fails).
- `utils/subscription_utils.py:297` maps the Zoho item code to an Odoo subscription plan and Stripe price via `map_item_code_to_plan`.
- `utils/subscription_utils.py:362` builds the Odoo subscription payload, including duration, pricing, protocol, and network metadata sourced from the CSV.
- `utils/subscription_utils.py:222` `_compute_billing_cycle_anchor` transforms the CSV "Next Billing Date" into a Stripe-compatible timestamp, rolling it forward by the plan frequency when the provided date is already in the past.
- `utils/subscription_utils.py:389` prepares the Stripe subscription payload with the customer, price items, optional billing-cycle anchor, and a temporary trial (matching the next billing date) so migrated customers are not charged immediately.
- `utils/subscription_utils.py:202` `create_subscription` persists the subscription in Odoo and Stripe, returning whether Stripe creation succeeded.
  - `utils/subscription_utils.py:205` saves the Odoo subscription inside a database savepoint.
  - `utils/subscription_utils.py:211` assembles Stripe metadata mirroring the Odoo subscription context and the imported dates.
  - `utils/subscription_utils.py:229` ensures the Stripe customer ID is real and does not fall back to plain email.
  - `utils/subscription_utils.py:247` builds the `stripe.Subscription.create` payload.
  - `utils/subscription_utils.py:253` applies the billing-cycle anchor and disables proration when available.
- `utils/subscription_utils.py:259` creates the Stripe subscription when a customer and payment method are ready; otherwise logs why it was skipped and flags the result as partial.
- `utils/subscription_utils.py:280` stores the Stripe subscription ID back on the Odoo record when creation succeeds and reports the status to the caller.

## Invoice CSV Import

- Selecting the `account.move` model runs `InvoiceImportUtils.handle_invoice_row` (`utils/invoice_utils.py`) for each CSV line—there is a one-to-one relation between CSV rows and invoices.
- Only Odoo objects are touched. The importer links the invoice to an existing `subscription.subscription` (via node ID / subscription ID) or to a `rollup.service` (via service ID) and, when the CSV says the invoice is paid, it creates a matching `account.payment`.

### Line-by-Line Flow (`invoice_utils.py`)

- `handle_invoice_row` normalises the row, resolves the subscription/rollup using `CF.reference_id`, `CF.updatedValue` network IDs, or `Subscription ID`, and builds a single invoice line using the plan product tied to that subscription/rollup.
- The helper creates an `account.move` with the dates, currency, quantity, unit price, discount, and notes supplied by Zoho, then posts it unless Zoho flagged the invoice as draft/voided.
- `_create_payment_if_needed` checks `Balance` and `Invoice Status`; when the invoice is fully settled it auto-posts an inbound payment (manual method/bank journal) and reconciles it against the new invoice so the payment state matches Zoho.

### Operational Checklist

- Configure `stripe_secret_key` in Odoo (`ir.config_parameter`) before running imports.
- Ensure partners, Stripe customers, and payment methods already exist and are aligned via email or Zoho metadata.
- Check in Stripe that each customer has a default payment method (the importer will warn when it is missing).
- Run the importer with a limited CSV sample first to verify mapping and logging.
