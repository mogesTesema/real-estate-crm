"""
Row-Level Security helpers (plan §2.1).

Each app ships a migration that calls ``rls_operations(*tables)`` to FORCE an
isolation policy on its tenant tables. The policy keys off the ``app.current_tenant``
GUC set by tenancy.set_current_tenant(). With the app connecting as a NON-superuser
role, FORCE RLS means even a query that forgets ``WHERE tenant_id = ...`` returns
nothing for other tenants — and INSERTs without a bound tenant are rejected.
"""
from django.db import migrations

POLICY = "tenant_isolation"


def _forward(table: str) -> str:
    return f"""
    ALTER TABLE {table} ENABLE ROW LEVEL SECURITY;
    ALTER TABLE {table} FORCE ROW LEVEL SECURITY;
    DROP POLICY IF EXISTS {POLICY} ON {table};
    CREATE POLICY {POLICY} ON {table}
        USING (tenant_id::text = current_setting('app.current_tenant', true))
        WITH CHECK (tenant_id::text = current_setting('app.current_tenant', true));
    """


def _reverse(table: str) -> str:
    return f"""
    DROP POLICY IF EXISTS {POLICY} ON {table};
    ALTER TABLE {table} NO FORCE ROW LEVEL SECURITY;
    ALTER TABLE {table} DISABLE ROW LEVEL SECURITY;
    """


def rls_operations(*tables: str) -> list:
    """Return a list of RunSQL operations enabling RLS on the given tables."""
    return [
        migrations.RunSQL(sql=_forward(t), reverse_sql=_reverse(t)) for t in tables
    ]
