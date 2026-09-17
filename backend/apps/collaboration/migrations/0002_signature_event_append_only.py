"""Make `collaboration_signature_event` append-only at the database-role level.

architecture.md §12: "Append-only immutable signature/audit trail (SRS 3.7.3/3.7.5).
UPDATE/DELETE revoked at DB role level (same policy as platform_audit_event)."

This is the evidence that a specific person, at a specific IP and user agent, saw and signed
a specific document. An editable signature trail proves nothing, so the guarantee has to sit
below the application: REVOKE binds the role Django connects as, and works only because that
role is deliberately not a superuser (see deploy/postgres-init.sql and DEPLOY.md).
"""
from django.db import migrations

from apps.core.db_policy import append_only_operations


class Migration(migrations.Migration):
    dependencies = [
        ("collaboration", "0001_initial"),
    ]

    operations = append_only_operations("collaboration_signature_event")
