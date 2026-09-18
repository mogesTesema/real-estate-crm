"""How `contacts` rows are scoped (architecture.md §2).

Registered from `ContactsConfig.ready()`. `identity` cannot write this table itself — the
import DAG (§1.2) forbids it from naming this app's models — so the rules live next to the
models they guard.

The anchor §2 names for a contact is `assigned_agent_id`.
"""
from apps.identity.scoping import (
    EVERYTHING,
    in_my_branch,
    in_my_team,
    managed_property_any,
    owned_by,
    portal_contact,
    register,
)


def register_resources():
    from apps.core.choices import ScopedEntityType
    from apps.identity.models import Role

    from .models import Contact

    scope = Role.DataScope

    register(
        "contact",
        model=Contact,
        entity_type=ScopedEntityType.CONTACT,
        scopes={
            scope.ALL: EVERYTHING,
            scope.BRANCH: in_my_branch("assigned_agent"),
            scope.TEAM: in_my_team("assigned_agent"),
            scope.OWN: owned_by("assigned_agent"),
            # A property manager deals directly with the owners and rental tenants of the
            # properties they manage, so those contacts are in scope — but only those.
            scope.MANAGED_PROPERTIES: managed_property_any(
                "owned_properties__property", "tenancies__property"
            ),
            # Finance invoices and marketing campaigns both address the whole contact base;
            # neither can do its job from a slice of it. Whether either may reach the contact
            # endpoint at all stays a function-level permission question (§2: "non-finance
            # modules only as granted by permissions"), which is the control that applies
            # here — not a row filter.
            scope.FINANCE_ALL: EVERYTHING,
            scope.MARKETING_ALL: EVERYTHING,
            # Portal-client isolation: a portal user is exactly one contact, their own.
            scope.PORTAL_OWN: portal_contact("pk"),
        },
    )
