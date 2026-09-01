# Deploy — Backend (Render + Neon)

Backend → **Render** (single free web service), database → **Neon**.

> **Current state.** The backend is mid-rebuild against `architecture.md` v3.2 and has no API
> layer yet — only `/healthz/` and Django admin. A deploy is therefore useful for validating
> that migrations apply cleanly against a real managed Postgres, not for serving the frontend.
> The frontend integration returns with the API pass. See the README's status table.

## Database prerequisites

Two things must be true of the Neon database before `migrate` will work.

**1. A dedicated non-superuser role.** Create **`crm_app`** and connect as it. Do **not** use
`neondb_owner`.

This is not about RLS (architecture.md §2 makes application-layer scoping the mandatory
mechanism and Postgres RLS merely optional defense-in-depth). It is about **immutability**:
`apps/core/db_policy.append_only_operations()` issues `REVOKE UPDATE, DELETE` against the
role Django connects as, for the four append-only tables —

| Table | architecture.md |
| :--- | :--- |
| `finance_account_entry` | §11 — "Never UPDATE/DELETE a posted entry — reverse with an opposite entry" |
| `platform_audit_event` | §15 — append-only audit trail |
| `collaboration_signature_event` | §12 — legal signature audit trail |
| `crm_agent_location_point` | §9 — append-only GPS breadcrumb trail |

A superuser (or a `BYPASSRLS`/owner-privileged role) ignores that revoke, which would silently
make all four tables mutable. The role name must also match `POSTGRES_USER` / the
`DATABASE_URL` user, because `db_policy` reads it from settings **at migrate time**.

**2. PostGIS.** Run `CREATE EXTENSION IF NOT EXISTS postgis;` once in the Neon SQL editor.
PostGIS is not a "trusted" extension, so `crm_app` cannot install it itself.

`pg_trgm` and `btree_gist` need no manual step — they are trusted extensions and are installed
by `apps/core/migrations/0002_extensions.py` as part of `migrate`.

## 1. Backend on Render

1. Push this repo to GitHub.
2. Render → **New → Blueprint**, pick the repo. It reads `render.yaml` and creates the
   `estatecrm-api` web service (Docker).
3. Set the two secret env vars (Dashboard → the service → Environment):
   - `DATABASE_URL` = the **crm_app** Neon URL, e.g.
     `postgresql://crm_app:PASSWORD@ep-...-pooler.<region>.aws.neon.tech/neondb?sslmode=require`.
     The `-pooler` host is rewritten to the direct host automatically: the pooler is a
     *transaction* pooler, which breaks anything needing a stable session — including the
     `SELECT ... FOR UPDATE` reference-code generator architecture.md §2 specifies.
   - `FRONTEND_ORIGIN` = the deployed frontend's URL, for CORS.
4. Deploy. `preDeployCommand` runs `migrate` + `collectstatic`.
5. Verify: `https://<service>.onrender.com/healthz/` returns `{"status": "ok"}`.

> Free-tier caveats: the service cold-starts (~30–50s) after inactivity, and free Neon may
> sleep — the first request in a while is slow.

## 2. Point the frontend at it

Not yet applicable — there are no `/api/v1/` routes to point at. Once the API pass lands, set
the frontend's API base URL to the Render URL and set `FRONTEND_ORIGIN` here to the frontend's
URL so CORS matches.

## 3. Background jobs

There is no Celery on the single-service plan, and the per-app `tasks.py` modules are still
empty. When the services pass fills them in, the scheduled work architecture.md §1.3 lists
(rent recurrence, CRM SLA sweeps, report snapshots, webhook delivery, notification dispatch,
MLS/portal sync) needs either a paid worker + beat, or an external cron hitting management
commands.

## Object storage

`collaboration_file` stores object metadata and S3 keys; binaries live in S3-compatible
storage. Set `AWS_STORAGE_BUCKET_NAME` plus the other `AWS_*` env vars to a Cloudflare R2 / S3
bucket to enable it. Uploads and signed-URL downloads (gated by the
`collaboration_access_grant` ACL, §12) arrive with the API pass.
