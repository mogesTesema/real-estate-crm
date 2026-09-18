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
    nested,
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


def register_phase_b_resources():
    """Leasing/maintenance children and their own anchors. Split from `register_resources`
    only for readability; both run from `PropertyOpsConfig.ready()`."""
    from apps.identity.models import Role

    from .models import (
        Application,
        Deposit,
        Inspection,
        MaintenanceRequest,
        Renewal,
        RentSchedule,
        WorkOrder,
    )

    scope = Role.DataScope

    # Children of the lease inherit exactly the lease's grant — including FINANCE_ALL
    # (rent invoicing runs off schedules) and PORTAL_OWN (the tenant's own rent history,
    # SRS 3.11.5).
    register(
        "rent_schedule",
        model=RentSchedule,
        entity_type=None,
        scopes={s: nested("lease", "lease") for s in scope.values},
    )
    register(
        "deposit",
        model=Deposit,
        entity_type=None,
        scopes={s: nested("lease", "lease") for s in scope.values},
    )
    register(
        "renewal",
        model=Renewal,
        entity_type=None,
        scopes={s: nested("original_lease", "lease") for s in scope.values},
    )

    register(
        "inspection",
        model=Inspection,
        entity_type=None,
        scopes={
            scope.ALL: EVERYTHING,
            scope.BRANCH: in_my_branch("performed_by"),
            scope.TEAM: in_my_team("performed_by"),
            scope.OWN: owned_by("performed_by") | nested("lease", "lease"),
            scope.MANAGED_PROPERTIES: managed_property("property"),
            scope.FINANCE_ALL: NOTHING,
            scope.MARKETING_ALL: NOTHING,
            scope.PORTAL_OWN: nested("lease", "lease"),
        },
    )

    register(
        "application",
        model=Application,
        entity_type=None,
        scopes={
            scope.ALL: EVERYTHING,
            scope.BRANCH: in_my_branch("assigned_to", "created_by"),
            scope.TEAM: in_my_team("assigned_to", "created_by"),
            scope.OWN: owned_by("assigned_to", "created_by"),
            scope.MANAGED_PROPERTIES: managed_property("property"),
            scope.FINANCE_ALL: NOTHING,
            scope.MARKETING_ALL: NOTHING,
            # An applicant has no portal login — the portal requires a completed contract
            # (SRS 3.11.2), and an application is by definition pre-contract.
            scope.PORTAL_OWN: NOTHING,
        },
    )

    register(
        "maintenance_request",
        model=MaintenanceRequest,
        entity_type=None,
        scopes={
            scope.ALL: EVERYTHING,
            scope.BRANCH: in_my_branch("assigned_to"),
            scope.TEAM: in_my_team("assigned_to"),
            scope.OWN: owned_by("assigned_to", "reported_by_user"),
            scope.MANAGED_PROPERTIES: managed_property("property"),
            scope.FINANCE_ALL: NOTHING,
            scope.MARKETING_ALL: NOTHING,
            # The reporter always sees their own request — even when the lease link was
            # omitted at capture — and the lease arm additionally shows a landlord the
            # issues on their property (SRS 3.14.1).
            scope.PORTAL_OWN: portal_contact("reported_by_contact_id")
            | nested("lease", "lease"),
        },
    )

    # Vendor quotes and internal pricing are not portal data: the tenant sees their
    # request's status, never the commercials.
    register(
        "work_order",
        model=WorkOrder,
        entity_type=None,
        scopes={
            **{
                s: nested("maintenance_request", "maintenance_request")
                for s in scope.values
            },
            scope.FINANCE_ALL: EVERYTHING,
            scope.PORTAL_OWN: NOTHING,
        },
    )
