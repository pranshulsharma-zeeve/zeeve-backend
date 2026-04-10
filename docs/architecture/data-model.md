# Data Model Overview

This is a practical developer map of major entities, relationships, and data movement.

## 1) Core Entity Domains

## A. Subscription domain (`subscription_management`)

Primary entities:

- `subscription.subscription` (customer subscription aggregate)
- `subscription.node` (per-subscription infrastructure node)
- `subscription.plan` (plan/pricing setup)
- `subscription.discount` (discount definitions)
- `stripe.payment.log` (event/audit trail)
- `stripe.payment.method` (stored customer payment method references)
- queue/snapshot entities for validator performance & rewards

Typical links:

- subscription → many nodes
- subscription → many invoices/payments/payment logs
- node → many invoices
- partner/customer ← subscription ownership

---

## B. Rollup domain (`rollup_management`)

Primary entities:

- `rollup.service` (rollup deployment + billing aggregate)
- `rollup.node` (nodes created for service)
- `rollup.type` (catalog + base pricing)
- `rollup.sequencer`, `rollup.data.availability`, `rollup.settlement.layer` (infra taxonomy)

Typical links:

- rollup service → type/customer/company
- rollup service → many rollup nodes
- rollup service → many Stripe logs, invoices, payments

---

## C. Base/config domain (`zeeve_base`)

Primary entities:

- `protocol.master` (chain/protocol metadata)
- `server.location`
- `zeeve.network.type`
- `zeeve.config` / `zeeve.admin.channel`
- `zeeve.notification`

Purpose:

- shared reference/configuration data,
- notification channels,
- protocol metadata consumed by subscription/reporting logic.

---

## D. Access/auth domain

- `module.access`, `record.access` for operator permission scoping.
- `res.users` / `res.partner` extended by auth/subscription/base modules.
- invitation and token/session metadata managed through `auth_module` logic.

---

## E. Import/migration domain

- `data.importer` tracks import runs and logs.
- utility handlers map CSV rows into subscription/invoice-related models.

---

## 2) High-Level Relational Sketch

```text
res.partner (customer)
   ├── subscription.subscription
   │      ├── subscription.node
   │      │      └── account.move (invoice)
   │      ├── stripe.payment.log
   │      └── account.payment
   │
   └── rollup.service
          ├── rollup.node
          ├── stripe.payment.log
          └── account.move/account.payment

protocol.master ──┬── subscription.plan/subscription records
                  └── reporting and pricing utilities

server.location / zeeve.network.type
   ├── subscription.node
   └── rollup.service and rollup catalog entities
```

---

## 3) Data Flow by Business Operation

## Subscription checkout + renewal

1. API creates/updates subscription records and Stripe context.
2. Stripe returns events to webhook.
3. Webhook updates Stripe logs and subscription/accounting state.
4. Related invoices/payments are generated/reconciled.
5. Notification/email side effects execute.

## Rollup deploy + lifecycle

1. Deploy endpoint creates provisional `rollup.service`.
2. Checkout metadata ties Stripe session to service identifiers.
3. Webhook/service logic updates state and billing records.
4. Service metadata is used for reminders, overdue handling, and reconciliation.

## Reporting

1. Report services fetch ORM entities (nodes/snapshots/subscriptions).
2. Fetch external Vizion telemetry.
3. Aggregate to DTOs and return JSON.

---

## 4) ORM Usage Patterns in This Repo

- Strong usage of Odoo relation fields (`Many2one`, `One2many`, `Many2many`).
- Use of computed counters and read-group for aggregate counts.
- Use of SQL constraints for public identifier uniqueness.
- Chatter-enabled models (`mail.thread`/`mail.activity.mixin`) for auditability.
- Widespread `sudo()` in APIs/integrations for operational flows.

---

## 5) Extension Points (Data Model Focus)

When adding fields/relations:

1. Add fields in owning model module.
2. Expose/edit in XML views if needed.
3. Update serializers/API payloads in corresponding controller/service.
4. Add migration/import support if CSV pipelines depend on the model.
5. Add security ACLs/rules if new models/fields are sensitive.

Suggested placement:

- subscription fields: `subscription_management/models/*`
- rollup fields: `rollup_management/models/*`
- shared protocol/network/config fields: `zeeve_base/models/*`
- permission-scoped access models: `access_rights/models/*`

---

## 6) Constraints and Guardrails

1. **Identifier stability:** do not change semantics of `subscription_uuid`, `node_identifier`, `service_id` casually.
2. **Webhook correctness:** Stripe event mapping must remain idempotent and signature-verified.
3. **Tenant isolation:** data-returning APIs must preserve company/record domain filtering.
4. **Backwards compatibility:** API response contracts are consumed by frontend and external clients.
5. **Upgrade safety:** minimize invasive edits in Webkul-origin domain internals.

---

## 7) What Should Not Be Modified Without Broad Impact Review

- Existing cross-module foreign-key assumptions between subscription/rollup/accounting models.
- Route payload shapes for checkout/webhook/report endpoints.
- Global auth token payload semantics and CORS behavior.
- AccessManager domain logic that determines operator visibility.
