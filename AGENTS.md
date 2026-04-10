# AGENTS.md — Working Safely in `zeeve-backend`

This repository is an Odoo backend with custom modules layered on top of standard Odoo and vendor modules.
Follow these rules before making any change.

## Mandatory Safety Rules

1. **Always analyze existing modules before modifying anything.**
   - Read module `__manifest__.py`, models, controllers, and utils first.
   - Confirm ownership boundaries before editing.

2. **Prefer extending existing Odoo modules instead of rewriting.**
   - Add fields/method overrides in the owning module.
   - Reuse existing models/controllers/utilities wherever possible.

3. **Follow Odoo ORM conventions strictly.**
   - Use `_name` for new models and `_inherit` for extensions.
   - Use proper `fields.*` relationships and Odoo APIs (`search`, `create`, `write`, `read_group`, etc.).
   - Respect `sudo()` usage patterns and only use when required.

4. **Do not break existing business logic.**
   - Preserve lifecycle/state transitions, webhook contracts, and integration identifiers.
   - Keep backward compatibility for API payloads/routes unless explicitly requested.

5. **Keep changes minimal and consistent with current patterns.**
   - Small, focused edits.
   - Avoid broad refactors unless requested.
   - Match current naming/style and module organization.

6. **Identify the correct module before adding new logic.**
   - Do not place logic in unrelated modules.

7. **Reuse existing services and utilities where possible.**
   - Check `utils/` and existing model helper methods before introducing new helpers.

8. **Validate changes against Odoo workflows.**
   - Confirm behavior in model actions, API controllers, cron jobs, and accounting flows as relevant.

9. **Unit testing after development is required.**
   - Install dependencies from `Standard-odoo-addons/requirements.txt` if needed.
   - Run relevant tests for changed modules.
   - Prefer targeted module tests first, then broader suites when needed.

---

## Repository Structure (Practical)

- `Custom-odoo-addons/` → project custom modules (primary development area)
  - `zeeve_base` → shared base models/config/reports/notifications
  - `subscription_management` → core subscriptions, billing, Stripe, validator metrics
  - `rollup_management` → rollup deployment lifecycle and billing
  - `auth_module` → signup/login/oauth/JWT/invitations/user management APIs
  - `access_rights` → module/record access and role/domain logic
  - `data_importer` → CSV migration/import workflows
  - `api_swagger_ui` → OpenAPI/Swagger docs endpoints
  - `website_subscription_management` → website subscription UX layer
- `Standard-odoo-addons/` → vendor/standard addons and dependency requirements
- `docs/architecture/` → architecture documentation and developer references
- `docker-compose-*.yaml`, `Dockerfile` → runtime and build configuration

---

## Coding Conventions Observed in This Repo

- Odoo-first structure: `models/`, `controllers/`, `utils/`, `views/`, `data/`, `security/`, `report/`, `wizard/`.
- Controllers are mostly `type='http'` with JSON responses and preflight handling where needed.
- Business orchestration often lives in module `utils/` (service-like helpers).
- Models carry core business behavior (state transitions, compute methods, constraints).
- Integrations (Stripe, Vizion, OAuth/JWT) rely heavily on `ir.config_parameter` settings.
- Access filtering is enforced through `AccessManager` + role/domain checks.

When editing, preserve these patterns.

---

## Where to Add New Features

- **Subscription lifecycle/billing features**
  - Add to `Custom-odoo-addons/subscription_management/`.
- **Rollup-specific deployment or billing features**
  - Add to `Custom-odoo-addons/rollup_management/`.
- **Shared protocol/server/config/reporting foundations**
  - Add to `Custom-odoo-addons/zeeve_base/`.
- **Authentication/authorization/token/cors features**
  - Add to `Custom-odoo-addons/auth_module/` (and `access_rights` if permission logic changes).
- **Import/migration enhancements**
  - Add to `Custom-odoo-addons/data_importer/`.
- **API documentation surface changes**
  - Add to `Custom-odoo-addons/api_swagger_ui/`.
- **Website subscription UX changes**
  - Add to `Custom-odoo-addons/website_subscription_management/`.

---

## Where NOT to Make Changes (Without Explicit Approval)

- **Do not change Stripe webhook verification/contract flow casually**
  - Risk: billing regressions and security exposure.
- **Do not alter integration-facing IDs/semantics lightly**
  - `subscription_uuid`, `node_identifier`, `service_id`, Stripe mapping fields.
- **Do not perform invasive rewrites in Webkul-origin core logic without strong reason**
  - Prefer extension/override over replacement.
- **Do not bypass access/domain checks in APIs**
  - Preserve tenant and role-based visibility.
- **Do not move logic across module boundaries arbitrarily**
  - Keep domain ownership clear and maintainable.

---

## Change Workflow Checklist (Use Every Time)

1. Identify affected feature + owning module.
2. Read manifest + current model/controller/utils for that feature.
3. Reuse existing utilities and extension hooks.
4. Implement minimal change.
5. Validate related Odoo workflow end-to-end.
6. Install deps if needed: `pip install -r Standard-odoo-addons/requirements.txt`.
7. Run targeted tests for changed modules.
8. Summarize risks, touched files, and verification commands in final update.
