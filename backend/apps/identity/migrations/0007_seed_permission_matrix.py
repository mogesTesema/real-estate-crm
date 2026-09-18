"""Seed the function-level permission matrix (SRS 3.17.1, architecture.md §4).

`identity_permission` / `identity_role_permission` shipped with the schema pass and sat
empty ever since — the scoping layer answered "which rows", the `data_scope` floor answered
"may portal clients write", and nothing answered "may THIS role use THIS feature". This
seeds the catalogue and one defensible default mapping per system role, taken from
`Real-Estate-CRM-Complete-User-Role-Definitions.md`.

**The behavior-preserving rule.** Any code that will guard an *existing* endpoint is granted
here to every role that can already reach that endpoint, so turning `HasPermission` on
changes nothing on day one — the matrix starts as a faithful description of current
behavior, and admins tighten it at runtime through the role-permission API. A seed that
silently revoked working access would be a regression wearing a security improvement's
clothes.

A data migration rather than a management command: it runs in every environment and every
test database, deterministically, and reverses cleanly.
"""
from django.db import migrations

STAFF = ("super_admin", "owner", "manager", "agent", "property_manager", "marketing", "finance")
MANAGEMENT = ("super_admin", "owner", "manager")
ADMINS = ("super_admin", "owner")

#: code -> (description, role codes granted)
MATRIX = {
    # contacts — existing surface; grants mirror today's reach (any staff).
    "contacts.view": ("Read the contact book (row-scoped).", STAFF),
    "contacts.manage": ("Create and edit contacts.", STAFF),
    "contacts.merge": ("Merge duplicate contacts.", STAFF),
    "contacts.import": ("Bulk-import contacts from CSV.", STAFF),
    "contacts.export": ("Export the visible contact book.", STAFF),
    # leads / deals — existing surface.
    "leads.view": ("Read leads (row-scoped).", STAFF),
    "leads.manage": ("Capture, edit, assign, convert leads.", STAFF),
    "leads.route_config": ("Edit routing rules.", ADMINS),
    "deals.view": ("Read deals (row-scoped).", STAFF),
    "deals.manage": ("Create, edit and move deals.", STAFF),
    # new surfaces (phases C-F cite these).
    "offers.manage": (
        "Submit, counter, accept, reject offers.",
        ("super_admin", "owner", "manager", "agent"),
    ),
    "transactions.manage": (
        "Move transactions through their lifecycle.",
        ("super_admin", "owner", "manager", "finance"),
    ),
    "checklists.manage": (
        "Create and complete closing checklists.",
        ("super_admin", "owner", "manager", "agent", "finance"),
    ),
    "leasing.manage": (
        "Create, activate, renew and terminate leases.",
        ("super_admin", "owner", "manager", "property_manager"),
    ),
    "maintenance.manage": (
        "Assign and progress maintenance and work orders.",
        ("super_admin", "owner", "manager", "property_manager"),
    ),
    "marketing.campaigns_manage": (
        "Create campaigns, steps and enrollments.",
        ("super_admin", "owner", "marketing"),
    ),
    "marketing.pages_manage": (
        "Create and publish landing pages.",
        ("super_admin", "owner", "marketing"),
    ),
    "marketing.alerts_manage": (
        "Manage saved-search alerts.",
        ("super_admin", "owner", "marketing", "agent"),
    ),
    "finance.record_payment": (
        "Record payments, allocations, cheques.",
        ("super_admin", "owner", "finance", "property_manager"),
    ),
    "finance.approve_commission": (
        "Approve and pay commissions.",
        ("super_admin", "owner", "finance"),
    ),
    "finance.manage": (
        "Issue invoices, expenses, statements, reconciliations.",
        ("super_admin", "owner", "finance", "property_manager"),
    ),
    "documents.manage": ("Upload documents, grant access, send envelopes.", STAFF),
    "reports.build": ("Build and save custom reports.", MANAGEMENT + ("finance", "marketing")),
    "reports.schedule": ("Schedule report delivery.", MANAGEMENT + ("finance", "marketing")),
    "integrations.manage": ("Connections, webhooks, sync.", ("super_admin", "owner")),
    # existing surfaces being brought under a code — all-staff by the preserving rule.
    "field_tracking.view_trails": ("Read GPS trails (scoped; supervisory reads audited).", STAFF),
    # admin
    "admin.users_manage": ("Register, deactivate, re-role users.", MANAGEMENT),
    "admin.roles_manage": ("Edit the permission matrix itself.", ("super_admin",)),
    "admin.field_permissions_manage": ("Edit field-level visibility rules.", ("super_admin",)),
    "admin.custom_fields_manage": ("Define custom fields and picklists.", ("super_admin", "owner")),
    "admin.audit_view": ("Read the audit log.", ADMINS),
}


def seed(apps, schema_editor):
    Permission = apps.get_model("identity", "Permission")
    Role = apps.get_model("identity", "Role")
    RolePermission = apps.get_model("identity", "RolePermission")

    roles = {role.code: role for role in Role.objects.all()}
    for code, (description, granted_to) in MATRIX.items():
        module, _, action = code.partition(".")
        permission, _ = Permission.objects.get_or_create(
            code=code,
            defaults={"module": module, "action": action, "description": description},
        )
        for role_code in granted_to:
            role = roles.get(role_code)
            if role is not None:
                RolePermission.objects.get_or_create(role=role, permission=permission)


def unseed(apps, schema_editor):
    Permission = apps.get_model("identity", "Permission")
    Permission.objects.filter(code__in=MATRIX).delete()  # cascades role_permissions


class Migration(migrations.Migration):
    dependencies = [("identity", "0006_recordshare_identity_share_lookup_idx")]
    operations = [migrations.RunPython(seed, unseed)]
