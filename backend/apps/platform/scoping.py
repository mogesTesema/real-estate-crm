"""How `platform` rows are scoped (architecture.md §2). Registered from
`PlatformConfig.ready()`.

Only `saved_report` registers: audit events / connections / webhooks / sync logs are
admin surfaces guarded by permission classes, and snapshots have their own access rule in
`selectors.snapshot_for`.
"""
from django.db.models import Q

from apps.identity.scoping import NOTHING, owned_by, register, where


def _shared_with(user):
    """The visibility half of the SavedReport doctrine: who may OPEN the definition.
    Execution separately re-scopes rows through the caller."""
    predicate = Q(visibility="ORG")
    if user.branch_id:
        predicate |= Q(visibility="BRANCH", owner__branch_id=user.branch_id)
    if user.team_id:
        predicate |= Q(visibility="TEAM", owner__team_id=user.team_id)
    return predicate


def register_resources():
    from apps.identity.models import Role

    from .models import SavedReport

    scope = Role.DataScope

    register(
        "saved_report",
        model=SavedReport,
        entity_type=None,
        scopes={
            **{
                s: owned_by("owner") | where(_shared_with)
                for s in (
                    scope.ALL, scope.BRANCH, scope.TEAM, scope.OWN,
                    scope.MANAGED_PROPERTIES, scope.FINANCE_ALL, scope.MARKETING_ALL,
                )
            },
            scope.PORTAL_OWN: NOTHING,
        },
    )
