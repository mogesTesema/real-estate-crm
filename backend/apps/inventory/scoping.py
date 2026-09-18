"""How `inventory` rows are scoped (architecture.md §2).

Registered from `InventoryConfig.ready()`.

Anchors §2 names here: `managed_by_id` on a property (mandatory — SRS 3.3.10), and
`assigned_agent_id` on a listing, joined in v3.3 by `co_listing_agent_id` (SRS 3.3.5).
"""
from apps.identity.scoping import (
    EVERYTHING,
    in_my_branch,
    in_my_team,
    managed_property,
    nested,
    owned_by,
    owned_by_any,
    portal_contact_any,
    register,
)


def register_resources():
    from apps.core.choices import ScopedEntityType
    from apps.identity.models import Role

    from .models import Listing, Media, Property, Unit

    scope = Role.DataScope

    register(
        "property",
        model=Property,
        entity_type=ScopedEntityType.PROPERTY,
        scopes={
            scope.ALL: EVERYTHING,
            scope.BRANCH: in_my_branch("managed_by"),
            scope.TEAM: in_my_team("managed_by"),
            # An agent's hold on a property is the listing they carry on it. Without the
            # listing arm an agent could not open the property behind their own listing.
            scope.OWN: owned_by("managed_by")
            | owned_by_any("listings__assigned_agent", "listings__co_listing_agent"),
            scope.MANAGED_PROPERTIES: owned_by("managed_by"),
            scope.FINANCE_ALL: EVERYTHING,
            scope.MARKETING_ALL: EVERYTHING,
            # A landlord on the portal sees the properties they own. A rental tenant reaches
            # the property they occupy through their lease, which is `property_ops`' table to
            # scope — not this one.
            scope.PORTAL_OWN: portal_contact_any("owners__contact"),
        },
    )

    register(
        "listing",
        model=Listing,
        entity_type=ScopedEntityType.LISTING,
        scopes={
            scope.ALL: EVERYTHING,
            scope.BRANCH: in_my_branch("assigned_agent", "co_listing_agent"),
            scope.TEAM: in_my_team("assigned_agent", "co_listing_agent"),
            # Both agents on a co-listed mandate hold it (SRS 3.3.5, v3.3).
            scope.OWN: owned_by("assigned_agent", "co_listing_agent"),
            scope.MANAGED_PROPERTIES: managed_property("property"),
            scope.FINANCE_ALL: EVERYTHING,
            scope.MARKETING_ALL: EVERYTHING,
            scope.PORTAL_OWN: portal_contact_any("property__owners__contact"),
        },
    )

    # Units inherit their property's visibility — §2's "child records inherit scope from
    # their parent". They get a registration rather than being read only through the property
    # endpoint because a unit has its own detail page and its own status machine.
    register(
        "unit",
        model=Unit,
        entity_type=None,
        scopes={s: nested("property", "property") for s in scope.values},
    )

    # Media has three nullable parents —
    # property, unit, listing — so it cannot inherit through a single path, and it needs its
    # own endpoint because uploads and re-ordering happen against the gallery directly.
    register(
        "media",
        model=Media,
        entity_type=None,
        scopes={
            s: nested("property", "property")
            | nested("unit__property", "property")
            | nested("listing", "listing")
            for s in scope.values
        },
    )
