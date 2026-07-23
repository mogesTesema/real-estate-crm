"""
Tenant context + database binding for Row-Level Security (plan §2.1).

Two layers of isolation, both real:
  1. App layer  — a contextvar holds the "current tenant"; TenantManager filters by it.
  2. DB layer   — the same tenant id is pushed to the Postgres session GUC
                  ``app.current_tenant``; FORCE'd RLS policies key off it, so a query
                  that forgets to filter still cannot see another tenant's rows.

The binding is session-scoped (``set_config(..., is_local=false)``) and cleared on the way
out of each request, so it requires a real per-connection session — i.e. a DIRECT Postgres
connection, NOT a transaction-pooling endpoint (settings.base rewrites a Neon ``-pooler``
host to its direct form for exactly this reason).

The app role Django connects with MUST be a non-superuser (superusers bypass RLS). On Neon
the ``neondb_owner`` role is a non-superuser owner, so FORCE RLS is enforced.
"""
import contextlib
import contextvars

from django.db import connection

# contextvars are safe under async and thread pools alike.
_current_tenant: contextvars.ContextVar = contextvars.ContextVar(
    "current_tenant", default=None
)


def get_current_tenant():
    """Return the current tenant id (UUID) or None."""
    return _current_tenant.get()


def _bind_db(tenant_id) -> None:
    """Push the tenant id onto the Postgres session so RLS policies see it."""
    with connection.cursor() as cursor:
        if tenant_id is None:
            cursor.execute("RESET app.current_tenant;")
        else:
            cursor.execute(
                "SELECT set_config('app.current_tenant', %s, false);", [str(tenant_id)]
            )


def set_current_tenant(tenant_id) -> contextvars.Token:
    """Set the tenant for both layers. Returns a token to restore later."""
    token = _current_tenant.set(tenant_id)
    _bind_db(tenant_id)
    return token


def clear_current_tenant(token: contextvars.Token | None = None) -> None:
    if token is not None:
        _current_tenant.reset(token)
    else:
        _current_tenant.set(None)
    _bind_db(None)


@contextlib.contextmanager
def tenant_context(tenant_id):
    """`with tenant_context(t): ...` — used by seeds, management commands, and tests."""
    token = set_current_tenant(tenant_id)
    try:
        yield
    finally:
        clear_current_tenant(token)
