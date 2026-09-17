"""Public reads / scoped querysets for `identity` (architecture.md §1.2, §2).

Other apps read `identity` rows through this module. They must never import
`apps.identity.api` — that package is the HTTP surface and is private to this app.

This module owns **`apply_scope`**, the mandatory row-level visibility layer. Function-level
RBAC answers "may this role use this feature?"; `apply_scope` answers "which records may this
user see?", and §2 requires every list and detail endpoint in every module to run through it.
"""
from django.db.models import Q

from .models import Role, User

# --- The scope registry -----------------------------------------------------------------
#
# A resource name maps to a function returning the Q predicate that one `data_scope` grants
# over that resource. Keeping this a registry rather than a chain of `if resource == ...`
# means a new module registers its anchors next to its endpoints instead of editing a
# growing conditional here.
#
# Only "user" is registered today. Domain resources (lead, deal, property, listing, lease)
# are added as their endpoints land — registering them now would be guesswork, since none of
# those apps has an API and the anchors would go untested.

#: Scopes that see the whole company for any resource.
_AGENCY_WIDE = frozenset(
    {
        Role.DataScope.ALL,
        Role.DataScope.FINANCE_ALL,
        Role.DataScope.MARKETING_ALL,
    }
)


class _Unfiltered:
    """Sentinel: this scope grants everything.

    NOT representable as an empty ``Q()``. Django treats an empty Q as the identity element,
    so ``Q() | Q(branch_id=x)`` collapses to ``Q(branch_id=x)`` — meaning a role granting
    everything would *narrow* a user's access when OR-ed with a narrower role instead of
    widening it. A predicate that grants everything has to short-circuit, not combine.
    """

    def __repr__(self):  # pragma: no cover - debugging aid
        return "<unfiltered>"


UNFILTERED = _Unfiltered()


def _user_predicate(scope: str, user: User) -> Q | None:
    """Which people may a requester holding `scope` see?

    Deliberate policy, not a reading of §2 — that section's table governs domain records and
    says nothing about listing people. A directory exists so colleagues can be picked as
    assignees, so OWN resolves to *the requester's own branch* rather than to themselves
    alone. Row breadth and column breadth are separate decisions: the API pairs this with a
    summary serializer, so an agent sees who their colleagues are without seeing their
    personal details (see api/serializers.py).

    FINANCE_ALL and MARKETING_ALL are module-wide rather than org-position-wide, but the
    people holding them are agency-level staff, so for a directory they read as agency-wide.

    Returns UNFILTERED when the scope grants everything and None when it grants nothing, so
    the caller can tell "every row", "no rows" and "these rows" apart.
    """
    if scope in _AGENCY_WIDE:
        return UNFILTERED
    if scope == Role.DataScope.BRANCH:
        # A user with no branch would otherwise match every other branchless user.
        return Q(branch_id=user.branch_id) if user.branch_id else Q(pk=user.pk)
    if scope == Role.DataScope.TEAM:
        return Q(team_id=user.team_id) if user.team_id else Q(pk=user.pk)
    if scope in (Role.DataScope.OWN, Role.DataScope.MANAGED_PROPERTIES):
        return Q(branch_id=user.branch_id) if user.branch_id else Q(pk=user.pk)
    if scope == Role.DataScope.PORTAL_OWN:
        return Q(pk=user.pk)
    return None


#: resource name -> callable(scope, user) -> Q | None
SCOPE_PREDICATES = {
    "user": _user_predicate,
}


def scopes_for(user) -> set[str]:
    """The `data_scope` values a user holds, via their roles.

    Returns an empty set for an anonymous user rather than querying — `AnonymousUser` has no
    primary key, so the filter below would raise "'AnonymousUser' is not a valid UUID".
    """
    if not user or not user.is_authenticated:
        return set()
    return set(
        Role.objects.filter(user_roles__user=user).values_list("data_scope", flat=True)
    )


def apply_scope(qs, user, resource: str):
    """Restrict `qs` to the rows `user` may see of `resource` (architecture.md §2).

    Multiple roles are **OR-ed, not ranked**. `identity_user_role` has no `is_primary` flag,
    so a user holding several roles has no single scope; each role contributes a predicate
    and the union is what they may see. This deliberately avoids inventing a total ordering
    over `data_scope` — FINANCE_ALL and BRANCH are not comparable, one being module-wide and
    the other org-position-wide, so "the broadest role wins" has no sound definition.

    Raises KeyError for an unregistered resource. That is intentional: a silent fallback to
    the unfiltered queryset is how scoping layers quietly stop scoping.
    """
    if not user or not user.is_authenticated:
        return qs.none()
    # Django's own superuser flag is an escape hatch for operators; it predates the role
    # system and is what `createsuperuser` grants before any role exists.
    if user.is_superuser:
        return qs

    predicate_for = SCOPE_PREDICATES[resource]

    predicates = []
    for scope in scopes_for(user):
        predicate = predicate_for(scope, user)
        if predicate is UNFILTERED:
            # Short-circuit rather than combine: see the note on UNFILTERED. A role granting
            # everything must not be narrowed by holding a second, narrower role.
            return qs
        if predicate is None:
            continue
        predicates.append(predicate)

    if not predicates:
        return qs.none()

    combined = predicates[0]
    for predicate in predicates[1:]:
        combined |= predicate
    # OR-ing predicates that traverse a relation (record shares, once registered) yields one
    # row per match, so the union is de-duplicated.
    return qs.filter(combined).distinct()


def visible_users(user):
    """The user directory `user` may see: active, not soft-deleted, scoped."""
    qs = User.objects.filter(is_active=True, deleted_at__isnull=True)
    return apply_scope(qs, user, "user")
