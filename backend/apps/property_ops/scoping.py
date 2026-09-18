"""How `property_ops` rows are scoped (architecture.md §2).

Registered from `PropertyOpsConfig.ready()`.

The anchor §2 names here is `property_manager_id` on a lease. The `PORTAL_OWN` row below is
the **rental-tenant isolation** guarantee the Architecture Principles open with: one rental
tenant never sees another's lease.
"""
from apps.identity.scoping import (
    EVERYTHING,
    NOTHING,
    in_my_branch,
    in_my_team,
    managed_property,
    owned_by,
    portal_contact,
    portal_contact_any,
    register,
)


def register_resources():
    from apps.core.choices import ScopedEntityType
    from apps.identity.models import Role

    from .models import Lease

    scope = Role.DataScope

    register(
        "lease",
        model=Lease,
        entity_type=ScopedEntityType.LEASE,
        scopes={
            scope.ALL: EVERYTHING,
            scope.BRANCH: in_my_branch("property_manager"),
            scope.TEAM: in_my_team("property_manager"),
            scope.OWN: owned_by("property_manager"),
            # Either hook: managing the lease, or managing the property it sits on.
            scope.MANAGED_PROPERTIES: owned_by("property_manager")
            | managed_property("property"),
            # Rent invoicing and owner statements run off leases (§11).
            scope.FINANCE_ALL: EVERYTHING,
            scope.MARKETING_ALL: NOTHING,
            # Tenant, landlord, or any additional party on the lease — co-tenant, guarantor.
            # Nothing else. A portal user without an ACTIVE profile matches none of these.
            scope.PORTAL_OWN: portal_contact("tenant_id", "landlord_id")
            | portal_contact_any("parties__contact"),
        },
    )
