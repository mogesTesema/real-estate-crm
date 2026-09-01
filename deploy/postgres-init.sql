-- Runs once at container init, as the bootstrap superuser.
--
-- Two jobs, both prerequisites for the schema architecture.md specifies:
--
-- 1. Create `crm_app`, the NON-superuser role Django connects as. This is load-bearing:
--    apps/core/db_policy.append_only_operations() REVOKEs UPDATE/DELETE from this role on
--    the four append-only tables (architecture.md §11 finance_account_entry, §15
--    platform_audit_event, §12 collaboration_signature_event, §9 crm_agent_location_point),
--    and only a non-superuser is actually bound by a REVOKE. The role name must match
--    POSTGRES_USER everywhere, because db_policy reads it from settings at migrate time.
--
-- 2. Install PostGIS into template1 so every database created afterwards inherits it —
--    including the `test_crm` database pytest creates. PostGIS is not a "trusted" extension,
--    so it cannot be installed later by crm_app; it has to happen here, as superuser.
--    pg_trgm and btree_gist ARE trusted, so they are installed by a Django migration
--    (apps/core/migrations/0002_extensions.py) which also covers managed hosts like Neon.

CREATE ROLE crm_app WITH LOGIN PASSWORD 'crm' NOSUPERUSER CREATEDB;

GRANT ALL ON SCHEMA public TO crm_app;
GRANT ALL PRIVILEGES ON DATABASE crm TO crm_app;

-- crm_app owns the objects it creates during migrate, so table-level REVOKEs apply to it.
ALTER DATABASE crm OWNER TO crm_app;

\connect template1
CREATE EXTENSION IF NOT EXISTS postgis;

\connect crm
CREATE EXTENSION IF NOT EXISTS postgis;
