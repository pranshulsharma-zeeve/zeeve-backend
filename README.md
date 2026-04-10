# Zeeve Backend (Odoo)

Odoo-based backend for Zeeve subscription and rollup operations.

This repository contains custom modules for authentication, subscription billing, rollup lifecycle management, reporting, access controls, and data import workflows on top of Odoo 18.

## Project Overview

- **Platform:** Odoo 18 + PostgreSQL
- **Main concerns:**
  - Customer authentication and account APIs
  - Subscription lifecycle + Stripe billing
  - Rollup deployment and payment orchestration
  - Reporting and notifications
  - Access/role enforcement and data import tooling

Runtime is containerized via `docker-compose` with three services:

- `frontend`
- `backend` (Odoo)
- `db` (PostgreSQL 15)

---

## Module Structure (Custom Addons)

All custom modules live in `Custom-odoo-addons/`.

- `zeeve_base` — shared base models, config, protocol/server metadata, reports, notifications
- `subscription_management` — core subscription domain, Stripe billing/webhooks, validator metrics
- `rollup_management` — rollup service lifecycle, checkout/deployment/payment flows
- `auth_module` — signup/login/oauth/jwt, invitations, user/company management APIs
- `access_rights` — module and record-level access controls
- `data_importer` — CSV import/migration workflows
- `api_swagger_ui` — OpenAPI JSON + Swagger UI
- `website_subscription_management` — website subscription experience

---

## How to Run the Project

## Prerequisites

- Docker + Docker Compose
- Environment files used by compose:
  - backend env file
  - frontend env file

## Local (dev compose)

```bash
docker compose -f docker-compose-dev.yaml up -d
```

Backend starts with addon paths including:

- Odoo core addons
- `/mnt/custom-addons`
- `/mnt/standard-addons/addons`

Stop services:

```bash
docker compose -f docker-compose-dev.yaml down
```

## Build image manually

```bash
docker build -t zeeve-backend:local .
```

---

## How to Add New Modules / Features

## Add a new feature in existing modules

1. Identify the owning module first (subscription, rollup, auth, base, etc.).
2. Prefer extending existing models/controllers/utils instead of rewriting flows.
3. Keep API changes backward compatible unless explicitly planned.
4. Add/update security access, views, and cron/data XML as needed.

## Add a new custom module

1. Create a new module under `Custom-odoo-addons/<module_name>/`.
2. Add required Odoo structure (`__manifest__.py`, `__init__.py`, models/controllers/views/security/data as needed).
3. Declare dependencies correctly in `__manifest__.py`.
4. Include module in deployment image via existing custom addons copy path.
5. Install/update module in Odoo.

---

## Development Guidelines

- Analyze existing logic before modifying anything.
- Follow Odoo ORM conventions (`_name`, `_inherit`, relation fields, compute methods, constraints).
- Keep changes small and consistent with current module patterns.
- Reuse existing helpers/services in `utils/` where possible.
- Do not break Stripe webhook contracts or integration-facing IDs.
- Preserve access/domain checks in API endpoints.
- Run targeted tests after changes.

### Testing Notes

Install backend Python dependencies (if running tests outside container):

```bash
pip install -r Standard-odoo-addons/requirements.txt
```

Run targeted module tests in Odoo test mode (example):

```bash
odoo-bin \
  -d <test_db> \
  --addons-path=Custom-odoo-addons,Standard-odoo-addons/addons \
  --test-tags <module_name>.tests \
  --stop-after-init \
  -u <module_name>
```

---

## Related Docs

- `AGENTS.md` — mandatory safety rules for contributors/Codex
- `docs/architecture/current-system.md`
- `docs/architecture/module-map.md`
- `docs/architecture/data-model.md`
