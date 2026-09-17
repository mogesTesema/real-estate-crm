"""Immutability of the append-only tables (architecture.md §9, §11, §12, §15).

Four tables must be impossible to rewrite, and the mechanism is a database-role REVOKE rather
than app-layer discipline: `apps/core/db_policy.append_only_operations()` revokes UPDATE and
DELETE from the role Django connects as.

That only works because the role is deliberately NOT a superuser — a superuser ignores REVOKE.
So these tests are also the canary for the deployment mistake of pointing DATABASE_URL at a
privileged role: if someone does, the REVOKEs become no-ops and these tests fail loudly
instead of the immutability silently evaporating.

Tables are added here as their phases land.
"""
import pytest
from django.db import ProgrammingError, connection, transaction

APPEND_ONLY_TABLES = [
    "crm_agent_location_point",
    "finance_account_entry",
    "collaboration_signature_event",
    "platform_audit_event",
]


def test_the_app_role_is_not_a_superuser(db):
    """The premise every other test in this file rests on."""
    with connection.cursor() as cur:
        cur.execute("SELECT usesuper FROM pg_user WHERE usename = current_user")
        row = cur.fetchone()
    assert row is not None, "current_user has no pg_user row"
    assert row[0] is False, (
        "The app database role is a superuser, so the append-only REVOKEs are ignored and "
        "the immutable tables are silently mutable. Connect as a non-superuser (crm_app)."
    )


@pytest.mark.parametrize("table", APPEND_ONLY_TABLES)
@pytest.mark.parametrize("statement", ["UPDATE {} SET id = id", "DELETE FROM {}"])
def test_append_only_tables_reject_mutation(db, table, statement):
    """Even with no rows present, the privilege check fires at planning time."""
    with pytest.raises(ProgrammingError) as exc, transaction.atomic():
        with connection.cursor() as cur:
            cur.execute(statement.format(table))
    assert "permission denied" in str(exc.value).lower()


@pytest.mark.parametrize("table", APPEND_ONLY_TABLES)
def test_append_only_tables_still_accept_inserts(db, table):
    """Append-only means append — INSERT must remain granted."""
    with connection.cursor() as cur:
        cur.execute(
            "SELECT has_table_privilege(current_user, %s, 'INSERT'), "
            "has_table_privilege(current_user, %s, 'UPDATE'), "
            "has_table_privilege(current_user, %s, 'DELETE')",
            [table, table, table],
        )
        can_insert, can_update, can_delete = cur.fetchone()
    assert can_insert, f"{table} must still accept inserts"
    assert not can_update, f"{table} must not be updatable"
    assert not can_delete, f"{table} must not be deletable"
