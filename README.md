# Real Estate CRM — Backend

Django + DRF + PostgreSQL (PostGIS) API for a single-company real estate CRM.
`architecture.md` (v3.4) is the authoritative specification for the data model, the app
boundaries, and the import DAG; this README describes where the build has got to and how to
run it.

**Scope:** one brokerage/agency (multi-branch / multi-team inside that company). **Not** a
multi-company SaaS product. In this system, **tenant** means a **rental tenant** (lease
party), never a software customer.

The frontend (React + TypeScript + Vite) lives in a separate repository.

## Status: all nine apps operational end to end

The full SRS is implemented — 101 tables, 994 tests, 15/15 import-linter contracts, a clean
OpenAPI schema, and a 17-step live-server walkthrough that exercises the whole story from a
public inquiry to a paid invoice and a signed document. The entire Phase-2–4 build added
exactly **one** table to the v3.3 schema (`crm_campaign_enrollment`, for per-lead drip
state); everything else landed on tables that already existed.

| App | What it does |
| :--- | :--- |
| `core` | abstract bases, custom-field registry, `next_reference` sequences |
| `identity` | org hierarchy, JWT auth, RBAC + runtime permission matrix, field-level permissions, portal invitations |
| `contacts` | parties with roles, E.164/email normalisation, dedupe probe, merge, CSV import/export |
| `inventory` | projects → properties → units, listings with a status machine, PostGIS radius search, media |
| `crm` | lead capture→dedupe→score→route→SLA→convert, Kanban pipeline, offers/counter-offers, checklist-gated transactions, matching engine, drip campaigns, landing pages, saved-search alerts, GPS field tracking, AI insights |
| `property_ops` | lease lifecycle over a GiST no-overlap exclusion, rent schedules, deposits, applications/screening, renewals, inspections, maintenance → vendor work orders |
| `finance` | invoices, payments with delta allocations, PDC cheques, marginal tiered commissions with splits, installment plans, collection-basis owner statements, reconciliation, append-only ledger |
| `collaboration` | documents with grants and in-place versions, built-in token e-sign, recurring tasks + calendar, threads/messages/call logs/notes, templates, notifications with quiet hours |
| `platform` | audit trail, portal sync adapters, HMAC-signed webhooks in and out, saved reports + schedules, dashboard snapshots |

Money is written only by `finance.services`; §1.2's orchestrations are real
(`mark_deal_won` → transaction + commission; `activate_lease` → rent schedule → invoices;
`complete_work_order` → billable expense on the owner statement).

## API surface

Swagger at `/api/docs/` is the authoritative reference (zero-warning `drf-spectacular`
schema). Roughly:

- **`/api/v1/`** — everything above, per app: `auth/*`, `users`, `contacts`, `properties`,
  `listings`, `leads` (+`/matches/`, `/ai-insights/`), `deals` (+board, `/move/`), `offers`,
  `transactions`, `closing-checklists`, `campaigns`, `leases` (+`/activate/`,
  `/rent-schedule/`), `maintenance-requests`, `work-orders`, `invoices`, `payments`,
  `cheques`, `commissions`, `owner-statements`, `documents`, `esign/envelopes`,
  `activities` (+`/calendar/`), `threads`, `messages`, `notifications`, `dashboard`
  (+`?as_of=` snapshots), `saved-reports`, `connections`, `webhooks`, and the admin
  surfaces `permissions` / `role-permissions` / `field-permissions` / `custom-fields`.
- **`/api/public/`** — unauthenticated, rate-limited, for the public website: `listings/`
  (ACTIVE rows on a strict whitelist card), `inquiries/` (honeypot, enumeration-proof 202s,
  full capture pipeline), `pages/{slug}/` (+`/submit/`), `chat/qualify/`,
  `esign/{token}/` signing pages, `webhooks/{id}/` (HMAC-verified inbound).

Every external service sits behind a settings-selected mock adapter — SMS/WhatsApp/push
gateways, portal syndication, lead feeds, the AI provider, webhook transport.
`third-part-needed.md` documents each seam and the real provider that replaces it.

## Run it

```bash
docker compose up -d db redis minio backend
docker compose exec backend python manage.py migrate
docker compose exec backend python manage.py bootstrap_e2e   # prints a super-admin login
```

`bootstrap_e2e` is idempotent: a company, branch, team, one super-admin
(`e2e-admin@walkthrough.test` / `e2e-walkthrough-pass-1`) and the property type the
walkthrough needs. `createsuperuser` works too (it attaches the `super_admin` role).
File uploads need the `crm-media` bucket in MinIO once — create it at
http://localhost:9001 (minioadmin/minioadmin), or the walkthrough's first upload error
tells you.

- Swagger: http://localhost:8000/api/docs/ · ReDoc: `/api/redoc/`
- Health: http://localhost:8000/healthz/ · Django admin: `/admin/`

The whole system in one command, against the live server:

```bash
python backend/scripts/e2e_walkthrough.py     # 17 steps, stdlib only, stops on first failure
```

The `worker`/`beat` compose services exist but are unused: recurring work is **idempotent
management commands** instead of Celery (the deployment target blocks background workers).
Point cron / Render jobs at whichever cadence suits:

`sweep_sla`, `generate_rent_invoices`, `sweep_overdue`, `sweep_lease_expiry`,
`sweep_reminders`, `sweep_esign`, `sweep_offers`, `run_drip`, `run_saved_search_alerts`,
`run_sync`, `retry_webhooks`, `run_report_schedules`, `build_dashboard_snapshots`.

Each is proven run-twice-changes-nothing by its tests, so overlapping or repeated runs are
safe.

Running without Docker: copy `backend/.env.example` to `backend/.env`, point it at local
Postgres+PostGIS, Redis and an S3-compatible store, then
`pip install -r backend/requirements-dev.txt` and `python backend/manage.py runserver`.

## Test & lint

```bash
docker compose run --rm backend pytest                                        # 994 tests
docker compose run --rm backend ruff check .
docker compose run --rm backend lint-imports                                  # 15/15 kept
docker compose run --rm backend python manage.py makemigrations --check --dry-run
docker compose run --rm backend python manage.py spectacular --file /dev/null # 0 warnings
```

The suite covers the schema against the spec (`tests/test_spec_coverage.py` parses
`architecture.md` itself), the database invariants, append-only enforcement as the
non-superuser role, every service state machine, row scoping per resource (including
portal-client and wrong-branch refusals), the money arithmetic, and every management
command run twice.

## Architecture in one paragraph

Nine Django apps in a fixed dependency order —
`core → identity → {contacts, inventory} → {crm, property_ops} → finance → {collaboration, platform}`.
Apps never talk over HTTP internally; they communicate through lazy FK string references,
a public `services.py` (the only module another app may import to *write*) and a public
`selectors.py` (reads). An app's `api/` package is private to it. Satellites
(`collaboration`, `platform`) never call domain write services — inbound events reach a
domain through signals it defines (`inbound_lead_received`, `verify_portal_eligibility`),
and domain events reach `platform`'s audit/webhook fan-out the same way. Row visibility is
`identity.selectors.apply_scope`, a per-resource registry each app fills in
`AppConfig.ready()`; an unregistered resource raises rather than leaking. Four tables are
append-only with `UPDATE`/`DELETE` revoked at the database-role level. `lint-imports`
enforces all of it in CI.

## Who may register whom

`super_admin` creates any staff role; `manager` creates `owner` (own branch only); `owner`
creates `agent`; nobody else registers anyone. The registrar sets an initial password and
the new user is confined to `/auth/me/` and `/auth/change-password/` until they replace it.

Portal clients (buyer, seller, rental tenant, landlord) get a login **only** against a
completed contract — verified through signals, never asserted, and re-checked at every
login. `agent` invites buyers/sellers, `property_manager` invites tenants/landlords,
management invites anyone, `marketing`/`finance` nobody. Isolation runs both ways: clients
see only their own records, and never appear in the staff directory.

## Mocked or not built

- **Every external provider is a mock adapter** behind a settings key — see
  `third-part-needed.md` for the 20 integrations, their env vars, and cut-over steps.
  Email is real (Django mail; console backend in dev).
- **AI** is the deterministic rule-based provider (`AI_PROVIDER=mock`); the Claude API
  provider is documented, not wired.
- **PDF/XLSX export** are refused with a clear message until `weasyprint`/`openpyxl` are
  added; CSV is real.
- **MFA and SSO** have no schema support (optional per SRS 5.3 / 3.17.5).

## A note on the database role

The app connects as **`crm_app`**, deliberately **not** a superuser.
`apps/core/db_policy.py` revokes `UPDATE`/`DELETE` on `finance_account_entry`,
`platform_audit_event`, `collaboration_signature_event`, and `crm_agent_location_point`
from exactly this role — a superuser would ignore the revoke and make the immutability
tests pass vacuously. The role name must match across `docker-compose.yml`,
`deploy/postgres-init.sql`, CI, and `POSTGRES_USER`, because `db_policy` reads it from
settings at migrate time.

## Layout

```
architecture.md         The specification (v3.4). Read the owning section before touching an app.
architecture-review.md  The review that produced v3.2's corrections.
third-part-needed.md    Every mocked integration: seam, real provider, env vars, cut-over.
documentation.md        Daily progress log.
backend/                Django project (config/) + the nine apps under apps/
backend/scripts/        e2e_walkthrough.py — the live-server smoke story
deploy/                 postgres-init.sql (app role + PostGIS bootstrap)
```

## Deploy

See `DEPLOY.md`.
