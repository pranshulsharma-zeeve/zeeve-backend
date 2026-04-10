# Module Map

This document maps custom modules, ownership boundaries, and practical extension guidance.

## 1) Module Dependency Perspective

### Foundation

- **`zeeve_base`**
  - Base domain/config module for protocols, server locations, notifications, and reporting utilities.
  - Many modules depend on it.

### Business Core

- **`subscription_management`**
  - Core subscription + billing + Stripe + validator metrics behavior.
  - Depends on `zeeve_base` and Odoo sales/accounting/mail.

- **`rollup_management`**
  - Rollup service lifecycle, deployment checkout, Stripe reconciliation.
  - Depends on `subscription_management` and `zeeve_base`.

### Access + Auth

- **`auth_module`**
  - Signup/login/oauth/token/verification/user management APIs.
  - Depends on `zeeve_base`.

- **`access_rights`**
  - Role/module/record access controls used by API controllers.

### Supporting Modules

- **`data_importer`**
  - CSV migration/import workflows (notably subscription/invoice paths).

- **`api_swagger_ui`**
  - OpenAPI generation + Swagger UI for custom API routes.

- **`website_subscription_management`**
  - Website-facing purchase/management layer for subscriptions.

---

## 2) Practical Module Breakdown

## `zeeve_base`

**Owns:**
- `protocol.master`, server/location/config masters, contact-us, notification models.
- report controller + report service utilities.

**Touch this module when:**
- adding shared protocol/network/server metadata,
- adding shared notification/reporting primitives,
- implementing cross-module reusable utilities.

---

## `subscription_management`

**Owns:**
- subscription entities (`subscription.subscription`, `subscription.node`, plans, reasons, discounts),
- billing queues/snapshots,
- Stripe payment logs/methods,
- main subscription API and Stripe webhook processing.

**Touch this module when:**
- changing subscription lifecycle/state,
- adding customer billing/payment logic,
- adding subscription REST endpoints.

---

## `rollup_management`

**Owns:**
- `rollup.service` and rollup catalogs (`rollup.type`, sequencer/DA/settlement layer),
- rollup API endpoints,
- rollup deployment and Stripe lifecycle orchestration.

**Touch this module when:**
- adding rollup deployment options,
- changing rollup billing rules,
- adding rollup-specific APIs.

---

## `auth_module`

**Owns:**
- custom auth flows (signup/login/oauth/password reset/email verification),
- JWT helpers and CORS/auth utility behavior,
- invitation and company-user management APIs.

**Touch this module when:**
- changing token or login behavior,
- adding identity/account endpoints,
- modifying CORS/auth gate behavior.

---

## `access_rights`

**Owns:**
- module/record access models,
- role-aware domain building logic (`AccessManager`).

**Touch this module when:**
- adding role/permission matrices,
- enforcing new per-module or per-record access rules.

---

## `data_importer`

**Owns:**
- CSV-driven import pipeline and row handlers,
- migration utility functions for subscriptions/invoices/rollups.

**Touch this module when:**
- extending migration schemas,
- adding new CSV import model handlers,
- improving import logging/retry semantics.

---

## `api_swagger_ui`

**Owns:**
- generated OpenAPI doc and swagger frontend endpoints.

**Touch this module when:**
- improving API discoverability/documentation rendering.

---

## `website_subscription_management`

**Owns:**
- website routes/templates/JS/CSS for subscription purchase experience.

**Touch this module when:**
- changing website checkout UX,
- adding website-side subscription self-service.

---

## 3) Extension Rules by Scenario

- **Add a new field on subscription behavior** → `subscription_management/models/*`.
- **Add a new rollup lifecycle status** → `rollup_management/models/*` + rollup utils/controller handling.
- **Add a new secure API endpoint** → owning module `controllers/*` + `auth_module` helper consistency + `access_rights` domain checks.
- **Add a periodic job** → owning module `data/*.xml` cron + idempotent handler in model/utils.
- **Add reporting metric** → `zeeve_base/utils/reports/*` and report controller DTO mapping.

---

## 4) Constraints for Module Work

1. Respect module ownership boundaries to avoid circular coupling.
2. Avoid business-heavy controller implementations; prefer utils/model methods.
3. Treat Webkul-derived modules as fragile for direct invasive edits.
4. Keep external API contracts and route payloads backward compatible.
5. Validate access domains for every data-returning endpoint.

---

## 5) “Do Not Modify” Zones (Without Design Review)

- Stripe webhook endpoint contract and event-dispatch behavior.
- Public UUID/reference fields relied upon by clients/integrations.
- Auth/JWT/CORS parameter names and semantics used in production clients.
- AccessManager domain rules without security regression testing.
