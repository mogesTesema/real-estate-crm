# Real Estate CRM — Backend

Django + DRF + PostgreSQL (PostGIS) + Celery API for a single-company real estate CRM.
`architecture.md` (v3.2) is the authoritative specification for the data model, the app
boundaries, and the import DAG; this README only describes where the build has got to.

**Scope:** one brokerage/agency (multi-branch / multi-team inside that company). **Not** a
multi-company SaaS product. In this system, **tenant** means a **rental tenant** (lease
party), never a software customer.

The frontend (React + TypeScript + Vite) lives in a separate repository.

## Status: rebuilding against architecture.md v3.2

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

The rebuild follows architecture.md's own Implementation Roadmap. **This pass builds schema
only** — models, migrations, and constraints for all ~90 tables. Services, selectors, and the
HTTP API are a deliberate second pass, so **there are no API endpoints yet** beyond
`/healthz/` and Django admin.

| Roadmap step | App | Tables | State |
| :--- | :--- | ---: | :--- |
| 1 | `core` — abstract bases, custom-field registry, sequences | 2 | done |
| 2 | `identity` — company, branches, teams, users, RBAC, portal | 10 | done |
| 3 | `contacts` — parties, roles, relationships, consent | 4 | pending |
| 4 | `inventory` — projects, buildings, properties, units, listings | 9 | pending |
| 5 | `crm` — marketing, leads, pipelines, deals, offers, transactions | 21 | pending |
| 6 | `property_ops` — leases, screening, renewals, maintenance | 10 | pending |
| 7 | `finance` — invoices, payments, commissions, statements | 16 | pending |
| 8 | `collaboration` — documents, e-sign, activities, comms, notifications | 17 | pending |
| 9 | `platform` — audit, integrations, reports | 8 | pending |
| 11 | import-linter contracts in CI | — | done |

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

- Health check: http://localhost:8000/healthz/
- Django admin: http://localhost:8000/admin/ (create a user with `createsuperuser`)

There is no demo seed command and no Swagger UI at present; both return with the API pass.

Running without Docker: copy `backend/.env.example` to `backend/.env`, point it at a local
Postgres+PostGIS instance and Redis, then `pip install -r backend/requirements-dev.txt` and
`python backend/manage.py runserver`.

## Test & lint

```bash
docker compose run --rm backend pytest        # schema, DB constraints, append-only, geo
docker compose run --rm backend ruff check .
docker compose run --rm backend lint-imports  # architecture.md §1.2 import DAG
```

The suite is deliberately schema-level while the API is absent: it asserts that every
model's `db_table` matches the spec's logical name, that each database-level invariant
actually rejects bad rows (overlapping leases, unbalanced invoices, malformed commissions),
that the append-only tables reject `UPDATE`/`DELETE` as the app role, and that PostGIS radius
queries work.

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
