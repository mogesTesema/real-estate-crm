"""Make `crm_agent_location_point` append-only at the database-role level.

architecture.md §9: "Append-only location trail for an active field session." This is employee
location data, so a rewritable trail is worse than no trail — it would look authoritative while
being editable by anything with app credentials.

REVOKE binds the role Django connects as. It is only meaningful because that role is
deliberately NOT a superuser (a superuser ignores REVOKE entirely) — see deploy/postgres-init.sql
and DEPLOY.md. `tests/test_append_only.py` proves the revoke is in force rather than assuming it.

Retention is a Super Admin policy applied out of band by a privileged role, not something the
app can do row by row.
"""
from django.db import migrations

from apps.core.db_policy import append_only_operations


class Migration(migrations.Migration):
    dependencies = [
        ("crm", "0001_initial"),
    ]

    operations = append_only_operations("crm_agent_location_point")
