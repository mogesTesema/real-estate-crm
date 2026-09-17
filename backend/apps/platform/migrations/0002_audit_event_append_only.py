"""Make `platform_audit_event` append-only at the database-role level.

architecture.md §15: "Append-only log table. UPDATE and DELETE permissions revoked at
database role level." §1.2 calls this out as a genuinely strong control, and the reason is
simple: an audit log that the application can rewrite records only what someone was willing
to leave behind.

REVOKE binds the role Django connects as, and only bites because that role is deliberately
not a superuser — see deploy/postgres-init.sql and DEPLOY.md.
"""
from django.db import migrations

from apps.core.db_policy import append_only_operations


class Migration(migrations.Migration):
    dependencies = [
        ("platform", "0001_initial"),
    ]

    operations = append_only_operations("platform_audit_event")
