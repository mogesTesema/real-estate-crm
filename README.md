# Real Estate CRM — Backend

Django + DRF + PostgreSQL (PostGIS) + Celery API for a single-company real estate CRM
(Phase 1 — MVP Foundation). See `architecture.md` for the data model and domain design.

**Scope:** one brokerage/agency (multi-branch / multi-team inside that company). **Not** a
multi-company SaaS product. In this system, **tenant** means a **rental tenant** (lease
party), not a software customer.

The frontend (React + TypeScript + Vite) lives in a separate repository and talks to this
API over HTTP.

## What's built (Phase 0 + Phase 1)

- **Role-based access + row-level scoping** — `own / branch / all` visibility per role within
  the company, built into a base viewset every module inherits, enforced with Postgres
  Row-Level Security.
- **Custom fields** — JSONB + `FieldDefinition` metadata, no migrations for ad-hoc company
  fields.
- **Audit log**, **soft delete**, **integration adapters** (console SMS/email stubs).
- **Contacts** (roles-as-a-set, dedupe + merge), **Properties/Listings** (split, status
  state machine + history, media upload), **Leads** (capture → dedupe → route → score →
  SLA sweep → acknowledge → convert), **Pipeline** (Opportunities, Kanban board,
  mandatory-reason stage moves), **unified activity timeline**.

Finance, marketing, leases, and portal are not backend apps yet — they currently exist only
as an in-browser demo-data layer on the frontend.

## Run it

```bash
# Postgres+PostGIS, Redis, MinIO, API, Celery worker/beat
docker compose up -d
docker compose exec backend python manage.py seed_demo   # demo company users + data
```

- API:      http://localhost:8000/api/v1/
- API docs: http://localhost:8000/api/docs/  (Swagger)
- ReDoc:    http://localhost:8000/api/redoc/
- Demo login: `admin@demo.test` / `demo12345`

Running without Docker: copy `backend/.env.example` to `backend/.env`, point it at a local
Postgres+PostGIS instance and Redis, then `pip install -r backend/requirements-dev.txt` and
`python backend/manage.py runserver`.

## Test & lint

```bash
docker compose run --rm backend pytest        # scoping, services, domain tests
docker compose run --rm backend ruff check .
```

## Layout

```
backend/   Django project (config/) + domain apps (apps/core, contacts, properties,
           leads, deals, activities, integrations)
deploy/    postgres-init.sql and related deploy helpers
```

## Deploy

See `DEPLOY.md` for deploying this service to Render against a Neon Postgres database.
