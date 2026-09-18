# Real Estate CRM — Backend

Django + DRF + PostgreSQL (PostGIS) + Celery API for a single-company real estate CRM.
`architecture.md` (v3.2) is the authoritative specification for the data model, the app
boundaries, and the import DAG; this README only describes where the build has got to.

**Scope:** one brokerage/agency (multi-branch / multi-team inside that company). **Not** a
multi-company SaaS product. In this system, **tenant** means a **rental tenant** (lease
party), never a software customer.

The frontend (React + TypeScript + Vite) lives in a separate repository.

## Status: schema complete; the identity API is live

An earlier Phase-1 backend shipped a working API over a flat seven-app layout. It is
preserved at tag **`phase1-flat-layout`** and has been replaced, because architecture.md v3.2
restructures the system around **nine apps under a strict import DAG** and changes two pieces
of doctrine:

- **Row visibility moved from Postgres RLS to the application layer.** The old design
  partitioned every table by a SaaS `tenant_id` and enforced it with RLS keyed on a session
  GUC. v3.2 replaces that with role-driven scoping through
  `identity.selectors.apply_scope` (§2). The non-superuser database role survives, but its job
  is now to make the **append-only** tables genuinely immutable.
- **"Tenant" was redefined** to mean a rental tenant, so the partition column is gone entirely.

The rebuild follows architecture.md's own Implementation Roadmap, which is complete for
all 98 tables. The API is now being built on top of it, app by app: **`identity` is done** —
registration, JWT auth, and user administration, together with
`identity.selectors.apply_scope`, the mandatory row-visibility layer every later module runs
through. The other eight apps still have no endpoints.

| Roadmap step | App | Tables | State |
| :--- | :--- | ---: | :--- |
| 1 | `core` — abstract bases, custom-field registry, sequences | 2 | done |
| 2 | `identity` — company, branches, teams, users, RBAC, portal | 11 | done |
| 3 | `contacts` — parties, roles, relationships, consent | 4 | done |
| 4 | `inventory` — projects, buildings, properties, units, listings | 9 | done |
| 5 | `crm` — marketing, leads, pipelines, deals, offers, transactions | 21 | done |
| 6 | `property_ops` — leases, screening, renewals, maintenance | 10 | done |
| 7 | `finance` — invoices, payments, commissions, statements | 16 | done |
| 8 | `collaboration` — documents, e-sign, activities, comms, notifications | 17 | done |
| 9 | `platform` — audit, integrations, reports | 8 | done |
| 10 | deferred foreign keys resolved | — | done |
| 11 | import-linter contracts in CI | — | done |

**The schema pass is complete: 98 tables, 256 specified foreign keys, all built and
verified against `architecture.md` by `tests/test_spec_coverage.py`.** Next is the services
pass — see *What is deliberately absent* below.

## The identity API

| | |
| :--- | :--- |
| `POST /api/v1/auth/token/` · `token/refresh/` | JWT login (throttled) |
| `GET` / `PATCH` `/api/v1/auth/me/` | session identity: roles, scopes, what you may grant |
| `POST /api/v1/auth/change-password/` | |
| `POST /api/v1/users/` | **register a staff member with their role** |
| `GET /api/v1/users/` · `{id}/` | scoped directory |
| `PATCH /api/v1/users/{id}/` · `DELETE` · `{id}/reactivate/` | |
| `POST` / `DELETE` `/api/v1/users/{id}/roles/` | grant / revoke |
| `GET /api/v1/roles/` | role catalogue |

**Who may register whom.** architecture.md fixes the chain in three places; the three roles
it never places go to the roles that already hold agency-wide scope:

| registrar | may create |
| :--- | :--- |
| `super_admin` | any staff role |
| `manager` | `owner` — in their own branch only |
| `owner` | `agent`, `property_manager`, `marketing`, `finance` |
| everyone else | nobody |

`portal` is refused: a portal profile must be tied to a contact with a completed contract,
and the import DAG forbids `identity` from reading `crm` or `property_ops` to verify one.
That flow belongs to the module that owns the contract.

**Credentials.** The registrar sets an initial password and the new user is confined to
`/auth/me/` and `/auth/change-password/` until they replace it — the secret was chosen by
someone else, so it must not unlock the CRM.

## Architecture in one paragraph

Nine Django apps in a fixed dependency order —
`core → identity → {contacts, inventory} → {crm, property_ops} → finance → {collaboration, platform}`.
Apps never talk over HTTP internally; they communicate through lazy FK string references,
a public `services.py` (the only module another app may import to *write*), a public
`selectors.py` (reads), and Celery tasks. An app's `api/` package is private to it. Money is
written **only** by `finance.services`. Four tables are append-only and have `UPDATE`/`DELETE`
revoked at the database-role level. `lint-imports` enforces the boundaries in CI.

## Run it

```bash
docker compose up -d          # Postgres+PostGIS, Redis, MinIO, API, Celery worker/beat
docker compose exec backend python manage.py migrate
```

- API docs: http://localhost:8000/api/docs/ (Swagger) · http://localhost:8000/api/redoc/
- Health check: http://localhost:8000/healthz/
- Django admin: http://localhost:8000/admin/

**Bootstrap.** `createsuperuser` now attaches the `super_admin` role, so the first
administrator is usable immediately:

```bash
docker compose exec backend python manage.py createsuperuser
```

Create a Company and Branch in Django admin (there is no branches API yet), then register
everyone else through `POST /api/v1/users/`. There is no demo seed command.

Running without Docker: copy `backend/.env.example` to `backend/.env`, point it at a local
Postgres+PostGIS instance and Redis, then `pip install -r backend/requirements-dev.txt` and
`python backend/manage.py runserver`.

## Test & lint

```bash
docker compose run --rm backend pytest        # schema, DB constraints, append-only, geo
docker compose run --rm backend ruff check .
docker compose run --rm backend lint-imports  # architecture.md §1.2 import DAG
```

The suite is deliberately schema-level while the API is absent. It asserts that:

- every model's `db_table` matches the spec's logical name, and no migration is unwritten;
- **the built schema covers `architecture.md` in full** — every table and all 256 declared
  foreign keys, checked by parsing the spec itself (`tests/test_spec_coverage.py`);
- each database-level invariant actually rejects bad rows: overlapping leases, a `LOST` deal
  with no reason, a commission hanging off both a transaction and a lease, a reconciliation
  claiming to balance while showing a difference;
- the four append-only tables reject `UPDATE`/`DELETE` as the app role — and that the app
  role is not a superuser, since a superuser would make that check pass vacuously;
- PostGIS radius search and JSONB containment queries work end to end.

## What is deliberately absent

Recorded so it is not mistaken for oversight. All of it belongs to the services pass:

- **Domain endpoints.** `apply_scope` exists and is enforced for `user`, but every other
  resource is unregistered — deliberately, since it raises rather than silently returning an
  unfiltered queryset. Each module registers its anchors alongside its own endpoints.
- **Every domain `services.py` body**, including the orchestrations §1.2 mandates:
  `mark_deal_won` → `create_lease_from_deal` → `generate_rent_schedule_invoices`;
  `upsert_activity_for_source` for viewings and inspections.
- **Audit.** §1.3 wants `platform.services.record_event` on every sensitive mutation, but the
  import DAG forbids `identity → platform`, so the five most audit-worthy events (register,
  grant, revoke, deactivate, login) currently write no audit row. Resolving this needs either
  an inverted dependency (identity emits a signal, platform receives it) or an amendment to
  the §1.2 matrix — a decision, not an oversight.
- **Token revocation.** `token_blacklist` is not installed and rotation is off, so
  `is_active=False` is the only revocation mechanism; an access token stays valid for up to
  its 60-minute lifetime.
- **Two invariants that a CHECK cannot hold**, both flagged in their model docstrings:
  `Invoice.amount_paid` must equal the sum of its non-reversed allocations (the spec
  recommends a trigger), and commission split percentages must total 100 — that one needs
  sibling rows, so it is enforced on approval.
- Reference-code generation through `core_sequence` with `SELECT … FOR UPDATE`.
- The API layer, JWT routes, Swagger, and a seed command.
- Porting domain logic from tag `phase1-flat-layout`: contact dedupe/merge, lead
  scoring/routing/capture/convert with the SLA sweep, the listing status state machine, deal
  `move_stage`, and the Kanban board.

## A note on the database role

The app connects as **`crm_app`**, deliberately **not** a superuser. `apps/core/db_policy.py`
revokes `UPDATE`/`DELETE` on `finance_account_entry`, `platform_audit_event`,
`collaboration_signature_event`, and `crm_agent_location_point` from exactly this role — and a
superuser would ignore the revoke, making those tables mutable and the immutability tests
pass vacuously. The role name must match across `docker-compose.yml`,
`deploy/postgres-init.sql`, CI, and `POSTGRES_USER`, because `db_policy` reads it from
settings at migrate time.

## Layout

```
architecture.md         The specification. Read the owning section before touching an app.
architecture-review.md  The review that produced v3.2's corrections.
backend/                Django project (config/) + the nine apps under apps/
deploy/                 postgres-init.sql (app role + PostGIS bootstrap)
```

## Deploy

See `DEPLOY.md`.
