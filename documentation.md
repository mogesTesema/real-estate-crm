# Real Estate CRM Backend: Daily Progress Log

Quick daily notes on what got done on the backend and where things stand. Frontend-only work
is tracked in the frontend repo's own log and omitted here.

**Current state:** the backend is being rebuilt against `architecture.md` v3.2, which
restructures it into nine apps under a strict import DAG. **There is no API right now** —
only `/healthz/` and Django admin. The current pass builds schema only (models, migrations,
constraints for ~90 tables); services and the HTTP API follow in a second pass.

Steps 1–2 of the roadmap are done (`core`, `identity`). The other seven apps are scaffolded
but empty. The previous working Phase-1 API is preserved at tag **`phase1-flat-layout`**.

The demo logins below no longer exist: `seed_demo` was part of the old layout and was removed
with it. A new seed command arrives with the API pass.

---

## Day 11: Tue, Sep 1, 2026; restructure to architecture.md v3.2

Replaced the flat seven-app layout with the nine DAG-ordered apps the spec requires, and got
the repo back to a state where `migrate` is meaningful.

**Why the rewrite, not an extension**
- Row visibility moved from Postgres RLS (session GUC + `tenant_id` partition on every table)
  to application-layer scoping via `identity.selectors.apply_scope`. The GUC machinery in
  `apps/core/tenancy.py` and the per-app `000N_rls.py` migrations had no place in the new
  design and were deleted rather than ported.
- "Tenant" was redefined to mean a *rental tenant* (lease party), so the SaaS partition column
  is gone from every table. The non-superuser `crm_app` role stays, but its job is now to make
  the four append-only tables genuinely immutable — a superuser ignores the `REVOKE`.

**What got done**
- Tagged the old implementation `phase1-flat-layout` and committed its removal as one
  checkpoint, so the domain logic (lead scoring/routing, contact merge, listing state machine,
  `move_stage`) stays retrievable for the services pass.
- Scaffolded architecture.md §1.2's mandatory public surface — `apps.py`, `services.py`,
  `selectors.py`, `tasks.py`, `api/` — across all nine apps, plus the normative `models/`
  packages for the three fat apps (`crm`, `property_ops`, `collaboration`).
- Wired **import-linter** contracts into CI. The §1.2 matrix is not a pure layering
  (`collaboration`/`platform` are satellites everyone calls, and `crm` ↔ `property_ops` is
  bidirectional by design), so the contracts encode the parts that are absolute: the
  `core → identity → contacts → inventory` spine, the ban on importing another app's `api/`,
  and the ban on satellites calling domain write services.
- Split extension bootstrap by trust level: `pg_trgm` and `btree_gist` are trusted, so a Django
  migration installs them and it works on Neon unattended; PostGIS is not, so it is
  provisioned as infrastructure in `postgres-init.sql`, a CI psql step, and the Neon dashboard.
- Unified the app database role on `crm_app` across compose, CI, settings, and `.env.example`.
  This was a real hazard: `db_policy._app_db_role()` reads the role name from settings at
  migrate time, so the three-way mismatch would have produced `REVOKE`s against a role nobody
  connects as — immutability tests passing vacuously.
- Rewrote `README.md`, `DEPLOY.md`, and this file, all three of which still described the old
  system as live.

---

## Day 10: Tue, Aug 11, 2026; seed depth

- Strengthened demo seeding for role walkthrough reliability in
  `backend/apps/core/management/commands/seed_demo.py`.

---

## Day 9: Mon, Aug 10, 2026; seed coverage for new roles

- Extended `seed_demo` with one user per missing role (super_admin, manager,
  property_manager, marketing, finance, portal) plus an `owner@demo.test` alias.

---

## Day 5: Tue, Aug 5, 2026; property/deal API fields

- Property geo `lat/lng/radius_km` filter; `Listing.co_listing_agent`; opportunity
  `days_in_stage`; activity `related_model` for deal tasks.

---

## Day 4: Mon, Aug 4, 2026; foundation shell, auth/leads APIs

- Forgot/reset password, MFA verify stub, `mfa_enabled` + `lead_routing_strategy` on `/me/`,
  `Notification` list/mark-read; `LeadSource` CRUD, lead assign action, routing strategy used
  in `capture_lead` / `route_lead`.

---

## Day 3 (evening): Mon, Aug 3, 2026; Phase 1 close-out

**Backend**
- Forgot/reset password + MFA verify stub; `mfa_enabled` + `lead_routing_strategy` on `/me/`.
- `LeadSource` CRUD; tenant routing strategy wired into `capture_lead` / `route_lead`.
- Opportunity `days_in_stage`; property geo `lat/lng/radius_km`; `Listing.co_listing_agent`;
  `Notification` list + mark-read.

**DoD**
- Backend `tests/test_phase1_closeout.py` (auth/MFA/sources/routing/geo/notifications) —
  green.

---

## Day 3 (continued): Mon, Aug 3, 2026; Phase 1 Wave A close-out

- `GET /users/`, `POST /leads/{id}/assign/`; custom_fields validator allows free-form when no
  FieldDefinitions (and system keys `image_url` / `media_order` / `cover_media_id`).

---

## Day 1: Thu, Jul 23, 2026; Backend build & infrastructure setup

Built the backend from scratch and got the first working slice of the MVP running locally.

**What got done**
- Set up the Django project with split settings (dev/prod/test), Docker Compose (Postgres,
  Redis, MinIO), Dockerfile, and CI pipeline.
- Built role-based access control and row-level scoping within the company (agents see their
  own records, managers see their branch, owners see company-wide).
- Built the user system with org hierarchy (Company → Branch → Team → User) and JWT auth.
- Built all the domain modules — contacts (with dedup and merge), properties (with listing
  status tracking), leads (capture → route → score → SLA alerts → convert), deals (pipeline
  stages with Kanban API), and an activity timeline.
- Wrote 21 backend tests covering RLS isolation, scoping, dedup, and state machines. All
  passing.

**Key decisions**
- Access control is role- and ownership-based inside one company (not SaaS multi-company
  partitioning).
- Deferred: Elasticsearch, public portal, Twilio/SendGrid, AI features.

---

## Day 2: Fri, Jul 24, 2026; DevOps & deployment

Got the backend deployed and accessible online. Fixed the CI pipeline that was failing due to
an RLS issue.

**CI/CD fix**
- Tests were failing in GitHub Actions because the Postgres Docker container makes its user a
  superuser, which bypasses RLS. Tried demoting the user but Postgres doesn't allow a
  bootstrap user to demote itself.
- Fixed it with a two-role setup: container starts with `postgres` as superuser, then a setup
  step creates a separate `crm` role without superuser privileges. Django connects as `crm`,
  RLS kicks in, tests pass.

**Deployed to production**
- Backend live on Render (`real-estate-crm-rfvc.onrender.com`). Configured env vars for Neon
  DB, CORS, and the frontend origin.
- Neon DB note: the default `neondb_owner` role has `BYPASSRLS`, so we created a dedicated
  `crm_app` role without it. Also rewrote the `-pooler` host to direct connections since
  session variables don't survive the transaction pooler.
- API docs available at `/api/docs/` (Swagger) and `/api/redoc/` (ReDoc).
- Seeded the database with demo data: agents, 22 contacts, 10 properties, 12 leads, 9 deals.

**Where things stand**
- Phase 1 MVP backend is almost done. Core API works. Next: finish remaining end-to-end
  flows, media uploads in production, and refine lead routing/scoring logic.
