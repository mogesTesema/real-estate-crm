"""Seed the eight canonical system roles (architecture.md §4).

§4 calls these a "System seed" and maps each `code` to a `data_scope`. The mapping is not
cosmetic: `identity.selectors.apply_scope` keys off `data_scope` precisely so that adding a
custom role never requires a hardcoded role-name switch in query code (§2). Seeding them in a
migration means every environment — dev, CI, and production — starts with the same scopes.

The `code` values are the product/UI canonical snake_case role keys and align with the
frontend's `RoleKey`. Registration hierarchy (SRS 3.15):
Super Admin -> Branch/Team Manager -> Broker/Agency Owner -> Sales/Leasing Agent.
"""
from django.db import migrations

# (code, display name, data_scope, description)
SYSTEM_ROLES = [
    (
        "super_admin",
        "Super Admin",
        "ALL",
        "Highest authority: agency-wide data plus system governance (users, security, config).",
    ),
    (
        "owner",
        "Broker / Agency Owner",
        "ALL",
        "Agency-wide visibility. Registered under a manager.",
    ),
    (
        "manager",
        "Branch / Team Manager",
        "BRANCH",
        "Branch-wide visibility. A team-scoped manager uses data_scope TEAM instead.",
    ),
    (
        "agent",
        "Sales / Leasing Agent",
        "OWN",
        "Own records only, plus anything explicitly shared. Registered under an owner.",
    ),
    (
        "property_manager",
        "Property Manager",
        "MANAGED_PROPERTIES",
        "Properties where managed_by is this user, and their child leases/maintenance/statements.",
    ),
    (
        "marketing",
        "Marketing",
        "MARKETING_ALL",
        "Campaigns, sources, and landing pages agency-wide; leads per permission grant.",
    ),
    (
        "finance",
        "Finance",
        "FINANCE_ALL",
        "Finance objects agency-wide; other modules only as granted, usually read-only.",
    ),
    (
        "portal",
        "Portal User",
        "PORTAL_OWN",
        "Own data only, via identity_portal_profile. Granted only after a completed contract.",
    ),
]


def seed_roles(apps, schema_editor):
    Role = apps.get_model("identity", "Role")
    for code, name, data_scope, description in SYSTEM_ROLES:
        Role.objects.update_or_create(
            code=code,
            defaults={
                "name": name,
                "data_scope": data_scope,
                "description": description,
                "is_system_role": True,
            },
        )


def unseed_roles(apps, schema_editor):
    Role = apps.get_model("identity", "Role")
    Role.objects.filter(code__in=[code for code, *_ in SYSTEM_ROLES], is_system_role=True).delete()


class Migration(migrations.Migration):
    dependencies = [
        ("identity", "0001_initial"),
    ]

    operations = [
        migrations.RunPython(seed_roles, unseed_roles),
    ]
