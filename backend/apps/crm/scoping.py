"""How `crm` rows are scoped (architecture.md §2).

Registered from `CrmConfig.ready()`.

Anchors §2 names here: `assigned_agent_id` on a lead — joined by `assigned_team_id`, because a
lead may sit in a team pool before any individual picks it up — and `owner_id` on a deal.
"""
from apps.identity.scoping import (
    EVERYTHING,
    NOTHING,
    in_my_branch,
    in_my_team,
    managed_property,
    managed_property_any,
    my_team,
    nested,
    owned_by,
    portal_contact,
    register,
    where,
)


def register_resources():
    from django.db.models import Q

    from apps.core.choices import ScopedEntityType
    from apps.identity.models import Role

    from .models import AgentFieldSession, Deal, DealProperty, Lead, Viewing

    scope = Role.DataScope

    register(
        "lead",
        model=Lead,
        entity_type=ScopedEntityType.LEAD,
        scopes={
            scope.ALL: EVERYTHING,
            # `assigned_team` is a Team and `assigned_agent` a User; both carry `branch_id`,
            # so one suffix serves both.
            scope.BRANCH: in_my_branch("assigned_agent", "assigned_team"),
            scope.TEAM: in_my_team("assigned_agent") | my_team("assigned_team"),
            # The team arm is load-bearing, not a nicety. Routing (SRS 3.1.5) may assign a
            # lead to a team for round-robin, leaving `assigned_agent_id` null; an OWN
            # predicate written only against `assigned_agent` would hide the entire unclaimed
            # pool from the agents meant to work it.
            scope.OWN: owned_by("assigned_agent") | my_team("assigned_team"),
            scope.MANAGED_PROPERTIES: managed_property("target_property"),
            # §2 is explicit that MARKETING_ALL covers "campaign/source/landing objects
            # agency-wide; **leads per permission/grant**" — so marketing gets no blanket row
            # grant over leads. What marketing is given deliberately arrives through
            # identity_record_share, which apply_scope unions in regardless of role.
            scope.MARKETING_ALL: NOTHING,
            scope.FINANCE_ALL: NOTHING,
            # A portal client has a completed contract; an open inquiry is not portal data.
            scope.PORTAL_OWN: NOTHING,
        },
    )

    register(
        "deal",
        model=Deal,
        entity_type=ScopedEntityType.DEAL,
        scopes={
            scope.ALL: EVERYTHING,
            scope.BRANCH: in_my_branch("owner"),
            scope.TEAM: in_my_team("owner"),
            scope.OWN: owned_by("owner"),
            scope.MANAGED_PROPERTIES: managed_property_any("deal_properties__property"),
            # A deal is where the money originates — commissions and transactions hang off it
            # (§11) — so it is a finance object in the sense §2 grants agency-wide.
            scope.FINANCE_ALL: EVERYTHING,
            scope.MARKETING_ALL: NOTHING,
            # §2's portal path: "Buyer/Seller: own **closed** deals/offers/documents linked to
            # the contact". An open negotiation is not portal data; a completed one is.
            scope.PORTAL_OWN: portal_contact("primary_contact_id")
            & where(lambda user: Q(status=Deal.Status.WON)),
        },
    )

    # A deal-property link is only ever read or removed through its deal, so it inherits.
    register(
        "deal_property",
        model=DealProperty,
        entity_type=None,
        scopes={s: nested("deal", "deal") for s in scope.values},
    )

    from .models import ClosingChecklist, Offer, Transaction

    # An offer is a chapter of its deal's negotiation — it inherits the deal's visibility
    # wholesale, including the portal's closed-deals-only rule (§2: "own closed
    # deals/offers/documents").
    register(
        "offer",
        model=Offer,
        entity_type=None,
        scopes={s: nested("deal", "deal") for s in scope.values},
    )

    # `Transaction.deal` is nullable (imports, direct sales), so deal inheritance alone
    # would hide deal-less transactions from everyone. FINANCE_ALL and ALL read them all;
    # everyone else reaches a transaction through its deal.
    register(
        "transaction",
        model=Transaction,
        entity_type=None,
        scopes={
            scope.ALL: EVERYTHING,
            scope.FINANCE_ALL: EVERYTHING,
            **{
                s: nested("deal", "deal")
                for s in (scope.BRANCH, scope.TEAM, scope.OWN, scope.MANAGED_PROPERTIES)
            },
            scope.MARKETING_ALL: NOTHING,
            scope.PORTAL_OWN: nested("deal", "deal"),
        },
    )

    # A checklist may hang off a deal or a transaction; either parent's visibility grants
    # it. Portal clients have no business in closing paperwork.
    register(
        "closing_checklist",
        model=ClosingChecklist,
        entity_type=None,
        scopes={
            **{
                s: nested("deal", "deal") | nested("transaction", "transaction")
                for s in (
                    scope.ALL, scope.BRANCH, scope.TEAM, scope.OWN,
                    scope.MANAGED_PROPERTIES, scope.FINANCE_ALL,
                )
            },
            scope.MARKETING_ALL: NOTHING,
            scope.PORTAL_OWN: NOTHING,
        },
    )

    register(
        "viewing",
        model=Viewing,
        entity_type=None,
        scopes={
            scope.ALL: EVERYTHING,
            scope.BRANCH: in_my_branch("agent"),
            scope.TEAM: in_my_team("agent"),
            scope.OWN: owned_by("agent"),
            scope.MANAGED_PROPERTIES: managed_property("property"),
            scope.FINANCE_ALL: NOTHING,
            scope.MARKETING_ALL: NOTHING,
            # A portal client sees the appointments booked in their own name.
            scope.PORTAL_OWN: portal_contact("contact_id"),
        },
    )

    # GPS field tracking (SRS 3.16.5–3.16.7). Read by the tracked agent and by whoever
    # supervises them; every supervisory read is separately audited at the endpoint (3.16.7),
    # because row visibility alone is not the control this requirement asks for.
    register(
        "field_session",
        model=AgentFieldSession,
        entity_type=None,
        scopes={
            scope.ALL: EVERYTHING,
            scope.BRANCH: in_my_branch("agent"),
            scope.TEAM: in_my_team("agent"),
            scope.OWN: owned_by("agent"),
            scope.MANAGED_PROPERTIES: NOTHING,
            scope.FINANCE_ALL: NOTHING,
            scope.MARKETING_ALL: NOTHING,
            scope.PORTAL_OWN: NOTHING,
        },
    )

    from .models import SavedSearchAlert

    # An alert belongs to the contact it serves; staff reach it through the contact's
    # visibility, and marketing sees them all (§2: marketing objects agency-wide).
    register(
        "saved_search_alert",
        model=SavedSearchAlert,
        entity_type=None,
        scopes={
            **{s_: nested("contact", "contact") for s_ in scope.values},
            scope.MARKETING_ALL: EVERYTHING,
        },
    )
