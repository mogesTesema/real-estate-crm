"""How `finance` rows are scoped (architecture.md §2).

Registered from `FinanceConfig.ready()`. The pattern of this table: FINANCE_ALL and ALL see
everything (finance objects agency-wide is FINANCE_ALL's literal definition in §2); the
narrow scopes see money that hangs off records they already own; and the ledger, accounts
and reconciliations are visible to the finance function alone — a scope this table does not
mention sees NOTHING, and for `account_entry` that silence is most of the point.
"""
from apps.identity.scoping import (
    EVERYTHING,
    NOTHING,
    in_my_branch,
    in_my_team,
    managed_property,
    nested,
    owned_by,
    owned_by_any,
    portal_contact,
    register,
)


def register_resources():
    from apps.identity.models import Role

    from .models import (
        Account,
        AccountEntry,
        Cheque,
        Commission,
        Expense,
        InstallmentPlan,
        Invoice,
        OwnerStatement,
        Payment,
        Reconciliation,
    )

    scope = Role.DataScope

    finance_only = {scope.ALL: EVERYTHING, scope.FINANCE_ALL: EVERYTHING}

    register(
        "invoice",
        model=Invoice,
        entity_type=None,
        scopes={
            scope.ALL: EVERYTHING,
            scope.FINANCE_ALL: EVERYTHING,
            scope.BRANCH: in_my_branch("created_by"),
            scope.TEAM: in_my_team("created_by"),
            scope.OWN: owned_by("created_by") | nested("lease", "lease"),
            scope.MANAGED_PROPERTIES: nested("lease", "lease"),
            scope.MARKETING_ALL: NOTHING,
            # Keyed on the billed contact, NOT the lease: a guarantor is a lease party and
            # sees the lease, but the rent invoice is addressed to the tenant — §2's
            # isolation says the guarantor has no business reading someone else's bill.
            scope.PORTAL_OWN: portal_contact("contact_id"),
        },
    )

    register(
        "payment",
        model=Payment,
        entity_type=None,
        scopes={
            scope.ALL: EVERYTHING,
            scope.FINANCE_ALL: EVERYTHING,
            scope.BRANCH: in_my_branch("recorded_by"),
            scope.TEAM: in_my_team("recorded_by"),
            scope.OWN: owned_by("recorded_by"),
            scope.MANAGED_PROPERTIES: NOTHING,
            scope.MARKETING_ALL: NOTHING,
            scope.PORTAL_OWN: portal_contact("payer_id"),
        },
    )

    register(
        "cheque",
        model=Cheque,
        entity_type=None,
        scopes={
            scope.ALL: EVERYTHING,
            scope.FINANCE_ALL: EVERYTHING,
            scope.BRANCH: NOTHING,
            scope.TEAM: NOTHING,
            scope.OWN: nested("lease", "lease"),
            scope.MANAGED_PROPERTIES: nested("lease", "lease"),
            scope.MARKETING_ALL: NOTHING,
            scope.PORTAL_OWN: portal_contact("payer_id") | nested("lease", "lease"),
        },
    )

    register(
        "commission",
        model=Commission,
        entity_type=None,
        scopes={
            scope.ALL: EVERYTHING,
            scope.FINANCE_ALL: EVERYTHING,
            scope.BRANCH: in_my_branch("agent"),
            scope.TEAM: in_my_team("agent"),
            # An agent tracks their own commission status (SRS 3.6.5) — including
            # commissions they are merely a split recipient of, which is what the second
            # arm grants.
            scope.OWN: owned_by("agent") | owned_by_any("splits__recipient_user"),
            scope.MANAGED_PROPERTIES: NOTHING,
            scope.MARKETING_ALL: NOTHING,
            scope.PORTAL_OWN: NOTHING,
        },
    )

    register(
        "expense",
        model=Expense,
        entity_type=None,
        scopes={
            scope.ALL: EVERYTHING,
            scope.FINANCE_ALL: EVERYTHING,
            scope.BRANCH: NOTHING,
            scope.TEAM: NOTHING,
            scope.OWN: owned_by("created_by"),
            scope.MANAGED_PROPERTIES: managed_property("property"),
            scope.MARKETING_ALL: NOTHING,
            # Landlords see costs through their statement lines, never raw expenses.
            scope.PORTAL_OWN: NOTHING,
        },
    )

    register(
        "owner_statement",
        model=OwnerStatement,
        entity_type=None,
        scopes={
            scope.ALL: EVERYTHING,
            scope.FINANCE_ALL: EVERYTHING,
            scope.BRANCH: NOTHING,
            scope.TEAM: NOTHING,
            scope.OWN: NOTHING,
            scope.MANAGED_PROPERTIES: managed_property("property"),
            scope.MARKETING_ALL: NOTHING,
            # The landlord-portal statement read (SRS 3.5.6, 3.11.4).
            scope.PORTAL_OWN: portal_contact("owner_contact_id"),
        },
    )

    register(
        "installment_plan",
        model=InstallmentPlan,
        entity_type=None,
        scopes={
            scope.ALL: EVERYTHING,
            scope.FINANCE_ALL: EVERYTHING,
            scope.BRANCH: NOTHING,
            scope.TEAM: NOTHING,
            scope.OWN: owned_by("transaction__deal__owner"),
            scope.MANAGED_PROPERTIES: NOTHING,
            scope.MARKETING_ALL: NOTHING,
            scope.PORTAL_OWN: NOTHING,
        },
    )

    for name, model in (
        ("account", Account),
        ("account_entry", AccountEntry),
        ("reconciliation", Reconciliation),
    ):
        register(name, model=model, entity_type=None, scopes=dict(finance_only))
