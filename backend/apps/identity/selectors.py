"""Public reads / scoped querysets for `identity` (architecture.md §1.2, §2).

Other apps read `identity` rows through this module. They must never import
`apps.identity.api` — that package is the HTTP surface and is private to this app.

`apply_scope`, the mandatory row-level visibility layer, is re-exported here because §2 names
`identity.selectors.apply_scope` normatively. Its machinery lives in `identity.scoping`, which
also hosts the registry each app writes its own resource rules into from `AppConfig.ready()`.
"""
from django.db.models import Q

from .models import Role, User
from .scoping import (  # noqa: F401 - re-exported as §2's normative import path
    EVERYTHING,
    REGISTRY,
    UNFILTERED,
    ScopedQuerysetMixin,
    apply_scope,
    register,
    scopes_for,
    where,
)

DataScope = Role.DataScope


def _same_branch_or_self(user):
    """A user with no branch would otherwise match every other branchless user."""
    return Q(branch_id=user.branch_id) if user.branch_id else Q(pk=user.pk)


def _same_team_or_self(user):
    return Q(team_id=user.team_id) if user.team_id else Q(pk=user.pk)


def register_resources():
    """`identity`'s own scope tables. Called from `IdentityConfig.ready()`.

    The **people directory** is deliberate policy, not a reading of §2 — that section's table
    governs domain records and says nothing about listing people. A directory exists so
    colleagues can be picked as assignees, so `OWN` resolves to the requester's own branch
    rather than to themselves alone. Row breadth and column breadth are separate decisions:
    the API pairs this with a summary serializer, so an agent sees who their colleagues are
    without seeing their personal details (see api/serializers.py).

    This is also the one resource where FINANCE_ALL and MARKETING_ALL read as agency-wide.
    They are module-wide scopes, but the people holding them are agency-level staff and a
    directory is not a domain record. No domain resource may copy that reasoning.
    """
    register(
        "user",
        model=User,
        entity_type=None,  # a colleague is not a shareable record
        scopes={
            DataScope.ALL: EVERYTHING,
            DataScope.FINANCE_ALL: EVERYTHING,
            DataScope.MARKETING_ALL: EVERYTHING,
            DataScope.BRANCH: where(_same_branch_or_self),
            DataScope.TEAM: where(_same_team_or_self),
            DataScope.OWN: where(_same_branch_or_self),
            DataScope.MANAGED_PROPERTIES: where(_same_branch_or_self),
            DataScope.PORTAL_OWN: where(lambda user: Q(pk=user.pk)),
        },
    )


def visible_users(user):
    """The user directory `user` may see: active, not soft-deleted, scoped."""
    qs = User.objects.filter(is_active=True, deleted_at__isnull=True)
    return apply_scope(qs, user, "user")
