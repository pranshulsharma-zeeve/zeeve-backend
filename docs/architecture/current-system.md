# Current System Architecture

## 1) Runtime Topology

This backend is an **Odoo 18** deployment with PostgreSQL and a separate frontend service.

- **frontend** container (external web app)
- **backend** container (Odoo)
- **db** container (PostgreSQL 15)

Key backend startup characteristics:

- Odoo runs with worker + gevent configuration.
- Addons are loaded from:
  1. core Odoo addons,
  2. `Custom-odoo-addons`,
  3. `Standard-odoo-addons/addons`.

The system is therefore a layered Odoo stack: **core + vendor + project-custom**.

---

## 2) Logical Architecture

## Layers used in this codebase

1. **HTTP Controller Layer**
   - API endpoints (`/api/v1/*`, `/api/v2/*`, webhook endpoints, reports, docs UI).
   - Handles request parsing, preflight, auth checks, and response shaping.

2. **Domain Model Layer (Odoo ORM)**
   - Business entities in `models/` with `_name` / `_inherit`.
   - Implements validations, state transitions, computed fields, and business actions.

3. **Service/Utility Layer**
   - Domain orchestration in `utils/` (e.g., Stripe lifecycle, reporting aggregation, migration helpers).
   - Shared helper logic for controllers and models.

4. **Data & View Layer**
   - XML for security, menus, views, scheduled jobs (cron), email templates, reports.

5. **External Integration Layer**
   - Stripe (checkout/subscriptions/webhooks)
   - Vizion APIs (metrics/reporting)
   - OAuth/JWT based auth flows and external account provisioning

---

## 3) Core Request/Data Flows

## A. Subscription purchase/billing flow

1. Client calls subscription/checkout API (v1 or v2 controllers).
2. Controller authenticates user and validates payload.
3. Service/model logic creates Odoo subscription records and Stripe checkout/subscription context.
4. Stripe webhook (`/api/stripe/webhook`) receives event and updates:
   - subscription state,
   - invoices/payments,
   - Stripe logs.
5. Emails/notifications are triggered via templates/helper functions.

## B. Rollup deployment flow

1. Client calls rollup deploy endpoint.
2. `rollup_management` creates provisional rollup service record.
3. Stripe checkout is started and metadata linked to rollup service.
4. Webhook events transition lifecycle (`pending_payment` → `active` / overdue / suspended).
5. Accounting artifacts (invoice/payment/logs) remain linked to rollup service.

## C. Reporting flow

1. Client calls `/api/v1/reports/*` endpoints.
2. Controller performs auth and parameter validation.
3. Report services aggregate:
   - Odoo ORM data (subscriptions/nodes/snapshots),
   - external Vizion data,
   - helper/scoring transformations.
4. Response DTO is serialized and returned as API JSON.

---

## 4) Security Model (Practical View)

- The codebase uses mixed route auth modes (`user`, `public`, `none`) and often enforces authentication manually via auth helpers.
- `access_rights` module adds role-based and record-based visibility rules via `AccessManager`.
- Stripe webhook is signature-validated and processed in elevated context for backend automation.

**Developer implication:** every new endpoint must be explicit about:

- auth mode,
- CORS/preflight behavior,
- tenant/record domain filtering,
- use of `sudo()`.

---

## 5) Extension Points (Where to Add Features)

- **Domain fields/state logic**: add in the owning module’s `models/`.
- **New API operations**: add to owning module `controllers/`, keep auth + access checks consistent.
- **Cross-cutting business orchestration**: add/extend module `utils/` services.
- **Back-office UX**: add XML in `views/` + menu/security declarations.
- **Scheduled automation**: add cron XML and idempotent model/utils handlers.

Rule of thumb:

- Subscription capabilities → `subscription_management`
- Rollup capabilities → `rollup_management`
- Shared protocol/server/user infra → `zeeve_base`
- Auth/session/token → `auth_module`
- Access governance → `access_rights`
- Data migration/import → `data_importer`

---

## 6) Constraints & Risks

1. **Tight external dependencies**: Stripe/Vizion availability impacts critical paths.
2. **Large controller files**: business logic can leak into controllers; maintain service boundaries when adding code.
3. **Mixed legacy/vendor customization**: `subscription_management` and website companion are Webkul-based and heavily customized, raising upgrade conflict risk.
4. **Security sensitivity**: endpoints with `auth='public'/'none'` rely on explicit manual checks.
5. **Data contract fragility**: IDs like `subscription_uuid`, `node_identifier`, `service_id` are integration-facing and should remain stable.

---

## 7) What to Avoid Modifying Without a Migration Plan

- Stripe webhook verification and event routing contracts.
- Public integration identifiers and uniqueness constraints.
- Core auth/JWT parameter semantics used by frontend and third-party clients.
- Vendor baseline behavior in Webkul-origin modules unless wrapped carefully.
