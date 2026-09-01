"""Migration-time DB policy helpers.

Two distinct mechanisms, both DB-level (not app-layer):

* ``append_only_operations`` — REVOKE UPDATE/DELETE on the DB role the app
  connects as, for tables architecture.md explicitly calls append-only
  (finance_account_entry, platform_audit_event, collaboration_signature_event,
  crm_agent_location_point). Only a real superuser can bypass a REVOKE, so
  this requires the app to connect as the non-superuser role already set up
  for RLS (see deploy/postgres-init.sql / CI's role-creation step).

* ``rls_operations`` — a generic ENABLE/FORCE RLS + policy helper, ported from
  the old tenant-scoped version but generalized (the policy predicate is now a
  parameter, not hardcoded to a tenant_id column). architecture.md describes
  Postgres RLS as "optional defense-in-depth" only — there is no per-table
  tenant partition in this schema (row visibility is an application-layer
  concern, apps.identity.selectors.apply_scope, built in a later pass). This
  helper has zero call sites in this foundation pass; it exists so a future
  pass can add a real RLS policy without re-deriving the RunSQL shape.
"""
from django.conf import settings
from django.db import migrations


def _app_db_role() -> str:
    """The Postgres role Django connects as, read at migrate-time (not a
    hardcoded literal) so each environment's actual role name is used."""
    return settings.DATABASES["default"]["USER"]


def append_only_operations(*tables: str) -> list[migrations.RunSQL]:
    """REVOKE UPDATE/DELETE on each table for the app's DB role; reversible."""
    role = _app_db_role()
    ops = []
    for table in tables:
        ops.append(
            migrations.RunSQL(
                sql=f'REVOKE UPDATE, DELETE ON TABLE "{table}" FROM "{role}";',
                reverse_sql=f'GRANT UPDATE, DELETE ON TABLE "{table}" TO "{role}";',
            )
        )
    return ops


def rls_operations(
    table: str,
    *,
    policy_name: str,
    using_sql: str,
    check_sql: str | None = None,
) -> list[migrations.RunSQL]:
    """Enable + FORCE row-level security on ``table`` with a single policy.

    ``using_sql``/``check_sql`` are raw SQL boolean expressions (e.g.
    referencing a session GUC via current_setting(...)). Scaffolding only —
    no call sites in this pass.
    """
    check_clause = f" WITH CHECK ({check_sql})" if check_sql else ""
    enable_sql = (
        f'ALTER TABLE "{table}" ENABLE ROW LEVEL SECURITY;\n'
        f'ALTER TABLE "{table}" FORCE ROW LEVEL SECURITY;\n'
        f'DROP POLICY IF EXISTS "{policy_name}" ON "{table}";\n'
        f'CREATE POLICY "{policy_name}" ON "{table}" USING ({using_sql}){check_clause};'
    )
    reverse_sql = (
        f'DROP POLICY IF EXISTS "{policy_name}" ON "{table}";\n'
        f'ALTER TABLE "{table}" NO FORCE ROW LEVEL SECURITY;\n'
        f'ALTER TABLE "{table}" DISABLE ROW LEVEL SECURITY;'
    )
    return [migrations.RunSQL(sql=enable_sql, reverse_sql=reverse_sql)]
