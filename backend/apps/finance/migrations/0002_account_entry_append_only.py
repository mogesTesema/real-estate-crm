"""Make `finance_account_entry` append-only at the database-role level.

architecture.md §11: "Append-only. Never UPDATE/DELETE a posted entry — reverse with an
opposite entry." This is the ledger. A correction is a new, opposite entry, so the record of
what was believed at the time survives; silently editing a posted entry destroys exactly the
evidence an audit exists to examine.

REVOKE binds the role Django connects as, and is only meaningful because that role is
deliberately not a superuser — see deploy/postgres-init.sql and DEPLOY.md.
tests/test_append_only.py proves the revoke is in force rather than assuming it.
"""
from django.db import migrations

from apps.core.db_policy import append_only_operations


class Migration(migrations.Migration):
    dependencies = [
        ("finance", "0001_initial"),
    ]

    operations = append_only_operations("finance_account_entry")
