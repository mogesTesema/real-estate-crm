-- Runs once at container init (as the bootstrap superuser).
-- Creates the NON-superuser role Django connects as, so RLS is actually enforced
-- (superusers bypass RLS entirely — see implimentation-plan.md §2.1).
CREATE ROLE crm_app WITH LOGIN PASSWORD 'crm' NOSUPERUSER CREATEDB;

GRANT ALL ON SCHEMA public TO crm_app;
GRANT ALL PRIVILEGES ON DATABASE crm TO crm_app;

-- crm_app owns the objects it creates during migrate; ownership + FORCE RLS
-- means the app role is itself subject to the isolation policies.
ALTER DATABASE crm OWNER TO crm_app;
