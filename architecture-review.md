# Architecture Review: Real Estate CRM Data Model

**Reviewed document:** `architecture.md` (Real Estate Management System Database Schema & Architecture Specification)
**Review date:** Aug 10, 2026
**Reviewer scope:** Data-model and structural critique against `full-featured-web-based-real-estate-crm-srs.md`, `Real Estate CRM Complete User Role Definitions.md`, and `Real-Estate-CRM-Role-Based-Dashboard Designs.md`.

> **Important framing.** This system is a **single real estate company** internal operational monolith — **not** a SaaS / multi-company product. **Tenant** means a **rental tenant** (lease party), never a software customer account. SaaS-only concepts (company-partition `tenant_id`, subscription billing across customers, per-customer plan gating) are **out of scope** and are **not** defects. Access control is role- and ownership-based within the one company. All other functional and non-functional requirements still apply.

---

## 1. Executive summary

The proposed architecture is a **strong, professionally-designed foundation**. Its financial-integrity model, immutability rules, and domain modularity are well above the quality bar typically seen in first-draft CRM schemas. The core sales/leasing/finance spine is sound.

However, it is **not yet complete or fully consistent** enough to build against as-is. There are five **critical** issues (one architectural, four structural), a set of **medium** feature-coverage gaps, and minor consistency nits.

| Severity | Count | Headline |
| :--- | :--- | :--- |
| Critical | 5 | Row-level access scoping undefined; unit hierarchy ambiguous; dangling FKs; circular FK ordering; three listed apps have no tables |
| Medium | 8 | Custom-field registry, finance depth (expenses/statements/reconciliation), e-sign, checklists, screening, renewals, calendar model, geo search |
| Minor | 3 | `updated_by` inconsistency, single preferred-location, reference-code race safety |

Verdict: **Approve with required revisions.** The corrections are additive and do not require reworking the good parts. A corrected `architecture.md` v2 accompanies this report.

---

## 2. What is good (keep as-is)

These decisions are correct and should be preserved:

1. **Single-company simplification is right.** Dropping SaaS company-partitioning (`tenant_id` as customer account), multi-company filtering, and subscription tables removes real complexity and matches the actual deployment model. Good call.
2. **Domain-modular monolith.** The `apps/` split by bounded context (accounts, contacts, properties, leads, sales, leasing, finance, ...) matches SRS 5.6 (modular, enable/disable) and keeps migration/ownership boundaries clean.
3. **Money handling.** `DecimalField(max_digits=15, decimal_places=2)` everywhere, never floats. Correct for SRS 3.6.
4. **Time handling.** `TIMESTAMPTZ` (UTC) universally. Correct.
5. **UUID v4 PKs.** Good for distributed ID generation, non-enumerable API surfaces, and safe merges.
6. **Deletion discipline.** `on_delete=PROTECT` on financial/legal/historical FKs prevents accidental cascade wipes; soft-delete (`deleted_at`) + **partial unique indexes** (`WHERE deleted_at IS NULL`) on non-financial entities is exactly the right pattern.
7. **Financial immutability.** "Never update/delete posted payments, allocations, invoices, audit — use reversing entries" is the correct accounting stance and protects auditability (SRS 5.3, 3.17.3).
8. **Append-only audit at the DB role level.** Revoking UPDATE/DELETE on `audit_audit_event` at the database-role level (not just app level) is a genuinely strong control.
9. **Invoice / Payment / Allocation triad.** Modeling payments and invoices as many-to-many through `finance_payment_allocation` (with `UNIQUE(payment_id, invoice_id)` and `CHECK(allocated_amount > 0)`) correctly supports partial payments, overpayments, and one payment across several invoices. This is the right accounting shape (SRS 3.6.2/3.6.8).
10. **Cheque register.** The `finance_cheque` lifecycle (`HELD_IN_SAFE → DEPOSITED → CLEARED/BOUNCED/RETURNED`) models post-dated-cheque (PDC) rent collection — a real-world Gulf/ME requirement that the SRS only implies. Above-spec, good.
11. **Commission + split.** `finance_commission` plus `finance_commission_split` (recipient_type AGENT/BROKER/REFERRAL_EXTERNAL) directly serves SRS 3.6.4 (listing/selling split, referral cut, franchise fee) and 3.2.8 (referral commissions).
12. **Lease integrity.** `CHECK(end_date > start_date)` **and an exclusion constraint preventing overlapping active leases on a unit** is excellent — it makes double-leasing physically impossible at the DB level.
13. **Property-owner model.** `properties_property_owner` with `ownership_percentage` + `CHECK (>0 AND <=100)` and `is_primary_owner` supports fractional/co-ownership and mandates (SRS 3.3.9).
14. **Multi-role contacts.** `contacts_contact_role` as a child table (not an enum column) correctly lets one contact be buyer + past seller simultaneously (SRS 3.2.2), and `contacts_contact_relationship` covers household/company grouping (3.2.5).
15. **Consent tracking.** `contacts_consent` (channel, OPTED_IN/OUT, evidence, timestamps) satisfies SRS 5.5 and 3.9 marketing opt-in/out.
16. **Lead history.** Separate `leads_lead_assignment` and `leads_lead_status_history` give the audit/hand-off trail the role docs require for routing and manager oversight.
17. **Idempotent integration mapping.** `integrations_external_mapping` with `sync_hash` and dual uniqueness (local & external) is the right way to make re-syncs idempotent.

---

## 3. Critical problems (must fix)

### C1. Row-level access scoping is undefined — the biggest gap
**What.** The schema models **function-level** RBAC only: `accounts_role`, `accounts_permission (module, action)`, `accounts_role_permission`. There is **no model or documented strategy for record-level (row) visibility**.

**Why it matters.** Nearly every role in `Real Estate CRM Complete User Role Definitions.md` is defined by *data scope*, not just feature access:
- Sales Agent — "Access Level: **Personal only**" (own leads/contacts/deals).
- Branch/Team Manager — "**Branch-wide**".
- Broker/Owner — "**Agency-wide**".
- Property Manager — "**Managed properties only**" ("every property must have a dedicated Property Manager").
- Portal User — "**Limited, own data only**".

SRS 3.17.2 explicitly requires "field-level and record-level (row) security". The dashboards in `Real-Estate-CRM-Role-Based-Dashboard Designs.md` (agency vs branch vs personal KPIs) are meaningless without an enforced scoping layer. Without this, an agent could read every lead in the company.

**Fix (in v2).**
- Document a **scoping doctrine**: ownership is derived from existing fields — `assigned_agent_id` (leads/contacts/listings), `owner_id` (deals), `managed_by_id`/`property_manager_id` (properties/leases), and `accounts_portal_profile.contact_id` (portal). Branch/team scope is inferred by joining owner → `accounts_user.branch_id`/`team_id`.
- Add an application-level **QuerySet scoping service/mixin** (documented, since this is a schema doc) that every list endpoint runs through, keyed on the requester's role + org position.
- Add `accounts_record_share` for explicit cross-user grants (share a lead/deal with a colleague) and `accounts_field_permission` for field-level masking (e.g., hide commission fields from agents).
- Note: optional Postgres RLS keyed on session user/role GUC may be used as defense-in-depth; the **application scoping layer is mandatory**. There is no SaaS multi-company tenancy partition.

### C2. Unit / property hierarchy is ambiguous
**What.** `architecture.md` §3 core diagram says "Property ├── Project / Building / **Unit**" and SRS 3.3.6 requires Project → Building/Phase → **Unit** with unit-level availability. But `properties_property` has `project_id` and `building_id` and **no unit concept** — no self `parent_id`, no `node_type`. Meanwhile `leasing_lease.unit_id` and `maintenance_request.unit_id` are FKs **back to `properties_property`**, and each of those tables *also* has a `property_id` FK to the same table. So "a unit" is silently "another property row," and the twin `property_id` + `unit_id` references are undefined in meaning.

**Why it matters.** Off-plan/multi-unit developments (a first-class SRS use case) need addressable units with their own availability, price, and lease. Ambiguity here corrupts leasing, maintenance, availability, and inventory-aging reports.

**Fix (in v2).** Introduce an explicit **`properties_unit`** table (FK to `properties_building`/`properties_property`) with its own status/attributes, and repoint `leasing_lease.unit_id`, `maintenance_request.unit_id`, and the lease exclusion constraint at `properties_unit`. (Alternative considered: self-referential `parent_id` + `node_type` on `properties_property`; rejected for clarity — the current implementation already suffered from `node_type` overloading.)

### C3. Dangling FKs to undefined tables
**What.** Two FKs reference tables that are **never defined** in the document:
- `finance_commission.commission_plan_id → finance_commission_plan` — `finance_commission_plan` is undefined.
- `integrations_external_mapping.connection_id → integrations_connection` — `integrations_connection` is undefined.

**Why it matters.** Commission calculation (SRS 3.6.4) needs the plan definition (flat/tiered/split rules). Integration mapping is meaningless without the connection it belongs to. As written, the schema will not build.

**Fix (in v2).** Define `finance_commission_plan` (rule_type, config JSONB, rates) and `integrations_connection` (provider, credentials ref, status), plus `integrations_webhook` and `integrations_sync_log`.

### C4. Circular FK / migration-ordering hazard
**What.** `accounts_user.avatar_file_id → documents_file` and `organization_company.logo_file_id → documents_file`, while `documents_file.uploaded_by → accounts_user`. The Implementation Roadmap builds `accounts` (step 1) **before** `documents` (step 6).

**Why it matters.** You cannot create `accounts_user` with a non-null-capable FK to a table that does not exist yet, and the two tables reference each other. Naive migration order fails.

**Fix (in v2).** Make `avatar_file_id`/`logo_file_id` **nullable** and add them in a **later** migration (after `documents_file` exists), or use Django `swappable`/deferred FK. Document the ordering explicitly in the roadmap.

### C5. Three listed apps have no schema
**What.** §1 lists `communications/`, `notifications/`, and `reports/` as apps, but **no tables are defined** for them anywhere in the document. Only `activities_activity` exists.

**Why it matters.** These are core SRS features, not nice-to-haves:
- Communications (SRS 3.9): omni-channel logging (call/SMS/WhatsApp/email) and **internal team chat** (3.9.6). The SRS calls "data loss from manual entry" a primary problem the system must solve.
- Notifications (SRS 3.18): in-app/email/push, **per-user channel preferences + quiet hours**.
- Reports (SRS 3.13.3): **saved/custom reports and scheduled delivery**.

**Fix (in v2).** Add full schema for all three (see §5 additions in v2).

---

## 4. Medium problems (should fix)

### M1. No custom-field registry / inconsistent extensibility
SRS 2.5 and 3.17.4 require custom fields/objects and picklists **without migrations**. Only `contacts_contact.custom_data JSONB` exists; `properties_property`, `leads_lead`, and `sales_deal` have **no** `custom_data`. There is no field-definition/registry table for admin-defined fields and picklists.
**Fix:** add `custom_data JSONB` to all core entities and a `core_custom_field` definition table (entity_type, key, label, data_type, choices, is_required).

### M2. Finance depth gaps
- **No `finance_commission_plan`** (also a dangling FK, C3).
- **No `finance_expense`** — yet §3 says "Property ├── Expense," SRS 3.14.4 links maintenance cost to owner statements, and Finance role tracks expenses/budgets.
- **No owner/landlord statement** — SRS 3.5.6 requires rent-collected / fees / net-remittance statements; PM and Landlord-portal dashboards depend on it.
- **No reconciliation model** — Finance role explicitly reconciles bank/trust/clearing accounts.
- **Derived-column risk:** `finance_invoice.balance_due`/`amount_paid` are stored with `CHECK(balance_due = total_amount - amount_paid)`, but nothing ties `amount_paid` to `SUM(allocations)`. This must be service-maintained inside `transaction.atomic()` (ideally a DB trigger) or the numbers drift.
- **Multi-currency:** invoices/payments/deals each carry `currency VARCHAR` but there is no FX-rate capture for cross-currency reporting (SRS 3.15.3). For a single-currency org this is fine; document the assumption.
**Fix:** define the missing tables; add the split-sum and allocation invariants as documented rules.

### M3. E-signature under-modeled
SRS 3.7.3/3.7.5 require signer tracking, completion status, and an **immutable signature-event trail**. Only `documents_document.status` exists.
**Fix:** add `documents_esign_envelope`, `documents_esign_signer`, `documents_signature_event`.

### M4. Missing structured workflows
- **Closing checklists** (SRS 3.6.6) — no model; `activities_activity` is too generic for per-transaction-type checklists.
- **Tenant screening** (SRS 3.5.2) — no application/screening model (PM role lists it).
- **Lease renewal + escalation** (SRS 3.5.1/3.5.5) — `leasing_lease.status` has `RENEWED` but no `renewed_from_lease_id` linkage or escalation schedule.
- **Counter-offer chain** (SRS 3.6.1) — `sales_offer.status` has `COUNTERED` but no `parent_offer_id` to follow the negotiation.
**Fix:** add `sales_closing_checklist(+_item)`, `leasing_application`, `leasing_renewal`, and `sales_offer.parent_offer_id`.

### M5. Calendar / activity model overlap
`sales_viewing`, `leasing_inspection`, and `activities_activity` (types `VIEWING`, `INSPECTION`) overlap without a stated relationship, and there is no `recurrence_rule` (SRS 3.12.4 recurring tasks) or external-calendar mapping (3.12.1 Google/Outlook sync).
**Fix:** document that specialized records (viewing/inspection) own domain data and create a linked `activities_activity` for the calendar; add `recurrence_rule` + reminder fields and `activities_calendar_link`.

### M6. Geo search not addressed
`latitude`/`longitude` are plain `DECIMAL` with no spatial index. SRS 3.3.7 needs radius/map-drawn search and 5.1 needs 1s search over 1M records.
**Fix:** add a **PostGIS `geography(Point)`** column + GiST index on properties (and contacts where useful); document a Postgres full-text/`pg_trgm` strategy for name/address search (Elasticsearch intentionally dropped for a single-company scale).

### M7. Document access control too coarse
SRS 3.7.4 wants role-based document access (confidential financials visible only to Finance/Management). Only an `is_confidential` boolean exists.
**Fix:** add `documents_access_grant` (document_id, role_id/user_id, access_level).

### M8. Marketing depth (Phase 2/4, but listed)
`marketing_campaign(+_metric)` exist, but drip **sequences** (3.8.1), **landing pages/microsites** (3.8.2), and **saved-search alerts** (3.8.4) have no tables.
**Fix:** add `marketing_campaign_step`, `marketing_landing_page`, `marketing_saved_search_alert` (can be marked Phase-2 build).

---

## 5. Minor / consistency nits

- **`updated_by` inconsistency:** present on `contacts_contact`, absent on `leads_lead` and several others. Standardize `created_by`/`updated_by` on all mutable business entities.
- **Single preferred location:** `leads_lead.preferred_location VARCHAR` vs SRS 3.1.7 "preferred location(**s**)". Use a child table or JSON array.
- **Reference-code race safety:** `reference_code`/`invoice_number`/`payment_reference` need a concurrency-safe generator (DB sequence or `SELECT ... FOR UPDATE` on a counter), otherwise parallel creates collide. Document the mechanism.

---

## 6. Fix plan summary

All fixes are **additive** and captured in the corrected `architecture.md` v2:

1. Add the row-level scoping doctrine + `accounts_record_share` + `accounts_field_permission` (C1).
2. Add `properties_unit`; repoint lease/maintenance/exclusion constraint (C2).
3. Define `finance_commission_plan` and `integrations_connection`/`_webhook`/`_sync_log` (C3).
4. Make avatar/logo FKs nullable + deferred; fix roadmap order (C4).
5. Add `communications`, `notifications`, `reports` schema (C5).
6. Add `core_custom_field` + `custom_data` everywhere (M1); finance `expense`/`owner_statement`/`reconciliation` + invariants (M2); e-sign tables (M3); checklists/screening/renewal/counter-offer (M4); calendar recurrence + link (M5); PostGIS geo (M6); document ACL (M7); marketing depth (M8).
7. Standardize audit columns, multi-location, and reference-code generation (minor).

See the **traceability matrix** in the Appendix for requirement-by-requirement coverage.

---

## Appendix A: Requirements traceability matrix

Status legend: **OK** = already covered; **v2** = added/fixed in corrected architecture; **defer** = intentionally later phase (table stubbed or noted).

| SRS ref | Requirement | Table(s) | Status |
| :--- | :--- | :--- | :--- |
| 3.1.1–3.1.11 | Lead capture, dedup, routing, scoring, SLA, auto-ack | `leads_lead`, `leads_lead_assignment`, `leads_lead_status_history`, `marketing_lead_source` | OK (SLA/auto-ack are runtime rules) |
| 3.2.1–3.2.8 | 360 contact, multi-role, tags/custom, merge, referral | `contacts_contact(+_role,+_relationship,+_consent)` | OK; `custom_data` broadened v2 |
| 3.3.1–3.3.10 | Listings, attributes, media, status history, co-list, Project→Building→Unit, geo search, matching, owner, viewings | `properties_project/building/property/unit/property_owner/listing`, `sales_viewing` | Unit + geo **v2**; status-history table **v2** |
| 3.4.1–3.4.7 | Kanban pipelines, weighted forecast, mandatory reason, stage automation | `sales_pipeline(+_stage)`, `sales_deal` | OK; stage automation runtime |
| 3.5.1–3.5.7 | Lease lifecycle, screening, recurring invoicing, deposits, renewals, statements | `leasing_lease/_party/_rent_schedule/_deposit/_inspection`, `leasing_application`, `leasing_renewal`, `finance_owner_statement` | Screening/renewal/statement **v2** |
| 3.6.1–3.6.9 | Offers, invoices, commission+splits, checklists, tax, late fees, export | `sales_offer/_transaction`, `finance_invoice(+_line)/payment/allocation/commission(+_split)/commission_plan`, `sales_closing_checklist` | commission_plan/checklist/counter-offer **v2** |
| 3.7.1–3.7.5 | Repository, templates, e-sign, doc RBAC, audit trail | `documents_file/document/document_link/esign_envelope/esign_signer/signature_event/access_grant` | e-sign + ACL **v2** |
| 3.8.1–3.8.6 | Drip, landing pages, bulk send, saved-search alerts, ad ROI, CMA | `marketing_campaign(+_metric,+_step)`, `marketing_landing_page`, `marketing_saved_search_alert` | steps/pages/alerts **v2**; CMA defer |
| 3.9.1–3.9.6 | Call/SMS/WhatsApp/email logging, templates, chatbot, internal chat | `comms_thread/message/call_log/internal_note` | **v2** (whole module) |
| 3.10.1–3.10.4 | MLS/IDX in/out, reconcile, error alerts | `integrations_connection/external_mapping/sync_log/webhook` | connection/log/webhook **v2** |
| 3.11.1–3.11.4 | Public site, buyer/seller/tenant/landlord portals, maintenance form | `accounts_portal_profile`, `maintenance_request`, listings | OK; public site is app layer / defer |
| 3.12.1–3.12.4 | Calendar sync, auto tasks, next-best-action, recurring tasks | `activities_activity(+recurrence)`, `activities_calendar_link` | recurrence/link **v2** |
| 3.13.1–3.13.4 | Role dashboards, standard + custom reports, export | `reports_saved_report/dashboard_snapshot/schedule` | **v2** |
| 3.14.1–3.14.4 | Maintenance requests, work orders, vendors, cost→owner | `maintenance_request/work_order/vendor`, `finance_expense`, `finance_owner_statement` | expense/statement **v2** |
| 3.15.1–3.15.4 | Company→Branch→Team→Agent, visibility, multi-currency, split fees | `organization_company/branch/team`, scoping doctrine, `finance_commission_split` | scoping **v2**; multi-currency FX defer |
| 3.16.1–3.16.4 | Mobile responsive, native app, offline, GPS check-in | `sales_viewing.check_in_*` | OK (client-side; GPS fields present) |
| 3.17.1–3.17.5 | RBAC, field/row security, audit, custom fields, SSO | `accounts_role/permission/role_permission/record_share/field_permission`, `core_custom_field`, `audit_audit_event` | row/field/custom **v2**; SSO app layer |
| 3.18.1–3.18.3 | In-app/email/push, prefs+quiet hours, client notices | `notifications_notification/preference/dispatch_log` | **v2** |
| 3.19.1–3.19.4 | REST API, webhooks, connectors, sandbox | `integrations_connection/webhook/sync_log` | webhook/connection **v2**; sandbox ops |
| 3.20.1–3.20.4 | AI scoring, next-best-action, drafting, chatbot | `leads_lead.score`, comms | defer (Phase 4; fields present) |

## Appendix B: Self-check results (post-v2)

- **FK integrity:** all FK targets defined (commission_plan, integrations_connection, properties_unit added). PASS.
- **No non-nullable circular FK:** avatar/logo FKs nullable + late migration. PASS.
- **Every listed app has ≥1 table:** communications, notifications, reports now defined. PASS.
- **Scoping fields on every scoped entity:** leads/contacts/listings (`assigned_agent_id`), deals (`owner_id`), properties (`managed_by_id`), leases (`property_manager_id`), portal (`portal_profile.contact_id`). PASS.
