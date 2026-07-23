"""Enable Row-Level Security on core tenant tables (plan §2.1)."""
from django.db import migrations

from apps.core.rls import rls_operations


class Migration(migrations.Migration):
    dependencies = [("core", "0001_initial")]

    # Tenant, User, and AuditLog are intentionally NOT under RLS:
    # Tenant is the isolation root, User must be resolvable pre-auth, and AuditLog
    # must always be writable for system actions.
    operations = rls_operations(
        "core_company",
        "core_branch",
        "core_team",
        "core_fielddefinition",
    )
