# ADR: Unified Subscription Architecture Across `subscription_management` and `rollup_management`

- **Status:** Proposed
- **Date:** 2026-04-10
- **Authors:** Backend/Odoo Team
- **Issue:** Parent architecture unification (Child 1)
- **Scope:** Documentation-only decision record (no runtime behavior change)

---

## 1) Context

Current behavior spans both modules:

- `subscription_management` is the generic subscription and billing core. It owns `subscription.subscription`, generic subscription states, Stripe identifiers, and payment/invoice/log relationships.
- `subscription_management` also contains the Stripe webhook entrypoint and already imports rollup utilities, which means cross-module billing orchestration already exists.
- `rollup_management` depends on `subscription_management` and owns rollup deployment/service lifecycle in `rollup.service`, including rollup-specific operational and billing status fields.
- `rollup_management` extends `stripe.payment.log` to correlate Stripe events with `rollup.service`, reusing the shared payment log path rather than creating a duplicate model.

This confirms the system is already partially unified by behavior, but ownership boundaries are implicit.

---

## 2) Problem Statement

Although functional, the current split has unclear boundaries for:

1. Stripe webhook event ownership/routing,
2. billing-state and lifecycle-state semantics,
3. invoice/payment reconciliation contracts,
4. shared versus rollup-specific responsibilities.

This raises change risk for future child tasks unless a clear contract is agreed first.

---

## 3) Decision

Adopt an **incremental unified subscription contract** with explicit ownership boundaries and no big-bang rewrite.

### 3.1 Shared primitives (canonical contracts)

1. **Billing Event Processing Contract**
   - Normalize incoming Stripe events into a stable internal contract.
   - Keep idempotency and event correlation as first-class requirements.

2. **Status Transition Contract**
   - Define mappings between:
     - `subscription.subscription.state` (generic subscription lifecycle),
     - `rollup.service.subscription_status` (rollup billing state),
     - `rollup.service.status` (rollup technical/operational lifecycle).

3. **Payment Log Write Contract**
   - Keep `stripe.payment.log.create_log_entry(...)` as canonical persistence path.
   - Continue module-level enrichment through Odoo `_inherit` extension.

### 3.2 Ownership boundaries

#### Shared contract layer (cross-module)

- Stripe metadata key dictionary,
- billing event normalization,
- idempotent payment-log write conventions,
- cross-domain status mapping policy.

#### `subscription_management` retains

- Generic subscription model ownership,
- generic subscription API ownership,
- webhook ingress + orchestration responsibility.

#### `rollup_management` retains

- Rollup domain entities (`rollup.service`, `rollup.node`, rollup type taxonomy),
- rollup deploy orchestration and rollup APIs,
- rollup-specific operational lifecycle and artifacts.

---

## 4) Responsibility Matrix

| Capability | Shared Contract | `subscription_management` | `rollup_management` |
|---|---:|---:|---:|
| Stripe webhook normalization/routing policy | ✅ | ✅ (entrypoint/orchestration) | ➖ |
| Generic subscription domain lifecycle | ✅ (semantics) | ✅ (source of truth) | ➖ |
| Rollup deployment orchestration | ➖ | ➖ | ✅ |
| Rollup operational lifecycle | ➖ | ➖ | ✅ |
| Payment log persistence path | ✅ | ✅ (base model) | ✅ (enrichment via `_inherit`) |
| Stripe metadata dictionary compatibility | ✅ | ✅ | ✅ |
| Invoice reconciliation conventions | ✅ | ✅ | ✅ |

---

## 5) Phased Rollout Plan (No Big Bang)

### Phase 0 — Discovery + contract freeze (this child)

- Produce ADR and shared-vs-specific responsibility matrix.
- Define target contract without changing runtime behavior.

**Rollback checkpoint:** N/A (documentation-only).

### Phase 1 — Event contract alignment (behavior-preserving)

- Route Stripe event processing through documented contract.
- Preserve existing webhook endpoint and payload compatibility.

**Rollback checkpoint:** retain legacy event routing path behind a controlled switchback.

### Phase 2 — Status harmonization

- Align status transition behavior using agreed mapping table.
- Keep both modules’ current fields, but enforce consistent transitions.

**Rollback checkpoint:** disable harmonization path and keep legacy independent transitions.

### Phase 3 — Compatibility hardening and deprecation

- Keep fallback metadata lookups during transition window.
- Add observability for mismatch detection before deprecating old paths.

**Rollback checkpoint:** re-enable compatibility fallbacks for event linkage and metadata parsing.

---

## 6) What Stays vs What Moves

### Remains in `rollup_management`

- Deployment lifecycle orchestration,
- rollup-specific API/controller behavior,
- `rollup.service` + `rollup.node` domain ownership,
- operational statuses and artifact handling.

### Moves toward unified subscription model (contract-first)

- Billing event normalization semantics,
- status mapping and state transition policy,
- payment-log write contract,
- metadata key standards for correlation/reconciliation.

This is intentionally **contract-level movement first**, not immediate model consolidation.

---

## 7) Migration & Compatibility Risks

1. **Webhook double-processing risk** during transition.
2. **State divergence risk** between generic and rollup billing states.
3. **Metadata correlation risk** (`deployment_token`, `rollup_service_id`, Stripe IDs).
4. **Invoice reconciliation ambiguity** across generic vs rollup paths.
5. **Access-domain regression risk** if shared handling bypasses existing access checks.

### Risk mitigations

- Additive changes first; avoid destructive field or route changes.
- Preserve integration-facing identifiers and semantics.
- Maintain compatibility metadata lookups during migration window.
- Phase-gated rollout with explicit rollback checkpoints.

---

## 8) Acceptance Mapping (Child 1)

- ✅ Architecture note (this ADR) with current/target decision framing.
- ✅ Shared vs rollup-specific responsibility matrix.
- ✅ Ordered phase plan with rollback checkpoints.
- ✅ Risks including webhook and invoice reconciliation compatibility constraints.

---

## 9) Out of Scope

- No runtime behavior changes.
- No route or payload contract changes.
- No schema migrations in this child task.
