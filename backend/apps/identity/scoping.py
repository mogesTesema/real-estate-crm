"""The row-level visibility registry (architecture.md §2).

Function-level RBAC answers *"may this role use this feature?"*. This module answers *"which
records may this user see?"*, and §2 requires every list and detail endpoint in every module
to run through `apply_scope`.

**Why this is a registry and not a chain of `if resource == ...`.** `identity` sits near the
bottom of the import DAG (§1.2) and may not import `contacts`, `inventory`, `crm`,
`property_ops` or `finance`. It therefore cannot name their models or their anchor columns.
Each app registers its own resources from its `AppConfig.ready()` — the scoping rules live
next to the models they scope, and `identity` owns only the mechanism.

**Why a table per resource and not a shared "agency-wide" set.** An earlier sketch kept one
`_AGENCY_WIDE = {ALL, FINANCE_ALL, MARKETING_ALL}` frozenset and let every resource consult
it. That is wrong the moment a domain resource uses it: §2 grants `FINANCE_ALL` "finance
objects agency-wide; **non-finance modules only as granted by permissions**" and
`MARKETING_ALL` "campaign/source/landing objects agency-wide; **leads per permission/grant**".
Lumping them with `ALL` would hand every finance and marketing user every lead in the company.
So each resource states, per scope, exactly what that scope sees — and a scope a resource does
not mention sees **nothing**. Deny by default.

Reading a registration:

    register(
        "lead",
        model=Lead,
        entity_type=ScopedEntityType.LEAD,
        scopes={
            DataScope.OWN: owned_by("assigned_agent") | my_team("assigned_team"),
            ...
        },
    )

Each value is a `Builder`: a callable `(user, model) -> Q | UNFILTERED | None`, where `None`
means "this scope grants nothing here". Builders compose with `|`.
"""
from django.db.models import Q
from django.utils import timezone

# --- Sentinels ---------------------------------------------------------------------------


class _Unfiltered:
    """This scope grants every row.

    NOT representable as an empty ``Q()``. Django treats an empty Q as the identity element,
    so ``Q() | Q(branch_id=x)`` collapses to ``Q(branch_id=x)`` — a role granting everything
    would *narrow* a user's access when OR-ed with a narrower role instead of widening it. A
    predicate that grants everything has to short-circuit, not combine.
    """

    def __repr__(self):  # pragma: no cover - debugging aid
        return "<unfiltered>"


UNFILTERED = _Unfiltered()


# --- Builders ----------------------------------------------------------------------------


class Builder:
    """A per-scope predicate factory, composable with ``|``."""

    __slots__ = ("_build",)

    def __init__(self, build):
        self._build = build

    def __call__(self, user, model, scopes=None):
        """`scopes` is the caller's already-resolved data_scope set, threaded through so a
        `nested` arm re-entering the registry does not re-query the role table."""
        return self._build(user, model, scopes)

    def __or__(self, other):
        def combined(user, model, scopes=None):
            return _or(self(user, model, scopes), other(user, model, scopes))

        return Builder(combined)

    def __and__(self, other):
        """Narrow one grant by another — "your own deals, **and** only the closed ones"."""

        def combined(user, model, scopes=None):
            return _and(self(user, model, scopes), other(user, model, scopes))

        return Builder(combined)


def _or(left, right):
    """OR two predicate results, honouring both sentinels."""
    if left is UNFILTERED or right is UNFILTERED:
        return UNFILTERED
    if left is None:
        return right
    if right is None:
        return left
    return left | right


def _and(left, right):
    """AND two predicate results. Either side granting nothing makes the whole grant nothing."""
    if left is None or right is None:
        return None
    if left is UNFILTERED:
        return right
    if right is UNFILTERED:
        return left
    return left & right


#: Grants every row of the resource.
EVERYTHING = Builder(lambda user, model, scopes=None: UNFILTERED)

#: Grants no row. The default for any scope a resource does not mention; stated explicitly
#: where the silence would otherwise read as an oversight.
NOTHING = Builder(lambda user, model, scopes=None: None)


def equals(*paths, value):
    """`path == value(user)` for each path, OR-ed.

    `value` returning None yields no predicate rather than `path IS NULL` — a user with no
    branch must not match every branchless record.

    Every path must be **single-valued** (a forward FK chain). Multi-valued paths belong in
    `any_of`, which subqueries instead of joining; see the note on `apply_scope`.
    """

    def build(user, model, scopes=None):
        resolved = value(user)
        if resolved is None:
            return None
        predicate = None
        for path in paths:
            predicate = _or(predicate, Q(**{path: resolved}))
        return predicate

    return Builder(build)


def any_of(*paths, value):
    """As `equals`, but for paths that traverse a **reverse or multi-valued** relation.

    Rendered as `pk IN (SELECT ...)` rather than a JOIN. A JOIN on a to-many path multiplies
    the outer rows, which forces `.distinct()` on every scoped queryset — and `DISTINCT`
    against the `COUNT(*)` that pagination issues is a well-known cliff. Keeping the outer
    query single-table is what lets `apply_scope` stay free of `.distinct()`.
    """

    def build(user, model, scopes=None):
        resolved = value(user)
        if resolved is None:
            return None
        inner = None
        for path in paths:
            inner = inner | Q(**{path: resolved}) if inner is not None else Q(**{path: resolved})
        return Q(pk__in=model._default_manager.filter(inner).values("pk"))

    return Builder(build)


def where(fn):
    """Escape hatch for a predicate that no combinator expresses. `fn(user) -> Q | None`."""
    return Builder(lambda user, model, scopes=None: fn(user))


# --- Named anchors (the vocabulary §2's doctrine is written in) --------------------------


def owned_by(*paths):
    """Anchor FKs to `identity_user` that equal the requester — §2's `OWN`."""
    return equals(*paths, value=lambda user: user.pk)


def owned_by_any(*paths):
    """`owned_by` across a to-many path."""
    return any_of(*paths, value=lambda user: user.pk)


def my_team(*paths):
    """Anchor FKs to `identity_team` that equal the requester's team.

    This is what makes an unclaimed **team-pool** record visible. A lead assigned to a team
    for round-robin has `assigned_agent_id IS NULL`; an `OWN` predicate written only against
    `assigned_agent` would hide the entire pool from the very agents meant to work it.
    """
    return equals(*paths, value=lambda user: user.team_id)


def in_my_team(*paths):
    """Anchor FKs whose target (a user or a team) sits in the requester's team."""
    return equals(*[f"{path}__team_id" for path in paths], value=lambda user: user.team_id)


def in_my_branch(*paths):
    """Anchor FKs whose target (a user or a team) sits in the requester's branch."""
    return equals(*[f"{path}__branch_id" for path in paths], value=lambda user: user.branch_id)


def managed_property(*paths):
    """Records hanging off a property the requester manages — §2's `MANAGED_PROPERTIES`.

    `paths` point at `inventory_property`; the `managed_by` hop is added here.
    """
    return equals(*[f"{path}__managed_by_id" for path in paths], value=lambda user: user.pk)


def managed_property_any(*paths):
    """`managed_property` across a to-many path."""
    return any_of(*[f"{path}__managed_by_id" for path in paths], value=lambda user: user.pk)


def portal_contact(*paths):
    """Anchor FKs to `contacts_contact` that equal the portal user's own contact.

    This is the **rental-tenant / portal-client isolation** boundary (§2, Architecture
    Principles): one portal client never sees another's data. `portal_contact_id` returns None
    unless the profile is ACTIVE, so an ineligible portal user — one without a completed
    contract (SRS 3.11.2) — matches nothing rather than everything.
    """
    return equals(*paths, value=portal_contact_id)


def portal_contact_any(*paths):
    """`portal_contact` across a to-many path."""
    return any_of(*paths, value=portal_contact_id)


def nested(path, resource):
    """A child record is visible exactly when its parent is — §2's "child records inherit
    scope from their parent".

    Registered child resources are the exception, not the rule: most children are read
    through their parent's endpoint and never reach `apply_scope` on their own. Register one
    only when it has its own endpoint (`media`, whose parent may be a property, a unit or a
    listing, is the case that forced this).
    """

    def build(user, model, scopes=None):
        predicate = _predicate_for(user, resource, scopes=scopes)
        if predicate is None:
            return None
        parent = REGISTRY[resource].model
        # A soft-deleted parent is not a visible parent. `_default_manager` on a
        # SoftDeleteModel is unfiltered, so without this a property's gallery and its units
        # outlived the property being archived — visible children of an invisible parent.
        live = parent._default_manager.all()
        soft_deleted = any(f.name == "deleted_at" for f in parent._meta.get_fields())
        if soft_deleted:
            live = live.filter(deleted_at__isnull=True)
        if predicate is UNFILTERED:
            # Every parent is visible — but a null parent link is not a visible parent, which
            # matters for a child like `inventory_media` whose three parent columns are each
            # nullable.
            return (
                Q(**{f"{path}__in": live.values("pk")})
                if soft_deleted
                else Q(**{f"{path}__isnull": False})
            )
        return Q(**{f"{path}__in": live.filter(predicate).values("pk")})

    return Builder(build)


def portal_contact_id(user):
    """The contact a portal user *is*, or None when they may not use the portal at all."""
    from .models import PortalProfile

    profile = getattr(user, "portal_profile", None)
    if profile is None:
        return None
    if profile.eligibility_status != PortalProfile.EligibilityStatus.ACTIVE:
        return None
    return profile.contact_id


# --- The registry ------------------------------------------------------------------------


class ResourceScope:
    """One resource's visibility table."""

    __slots__ = ("resource", "model", "entity_type", "scopes")

    def __init__(self, resource, model, entity_type, scopes):
        self.resource = resource
        self.model = model
        self.entity_type = entity_type
        self.scopes = scopes


#: resource name -> ResourceScope
REGISTRY: dict[str, ResourceScope] = {}


def register(resource, *, model, scopes, entity_type=None, replace=False):
    """Declare how `resource` is scoped. Called from each app's `AppConfig.ready()`.

    `entity_type` is the `core.choices.ScopedEntityType` member that `identity_record_share`
    uses for this resource; pass it so explicit shares are unioned in (§2). Leave it None for
    a resource that cannot be shared.

    Registering the same name twice raises unless `replace=True`. Two apps silently claiming
    one resource name is how the wrong anchor ends up guarding an endpoint.
    """
    if resource in REGISTRY and not replace:
        raise ValueError(
            f"scope resource {resource!r} is already registered by "
            f"{REGISTRY[resource].model._meta.label}"
        )
    unknown = set(scopes) - _valid_scopes()
    if unknown:
        raise ValueError(f"{resource!r}: unknown data_scope values {sorted(unknown)}")
    REGISTRY[resource] = ResourceScope(resource, model, entity_type, dict(scopes))


def _valid_scopes():
    from .models import Role

    return set(Role.DataScope.values)


def scopes_for(user) -> set[str]:
    """The `data_scope` values a user holds, via their roles.

    Returns an empty set for an anonymous user rather than querying — `AnonymousUser` has no
    primary key, so the filter below would raise "'AnonymousUser' is not a valid UUID".
    """
    from .models import Role

    if not user or not user.is_authenticated:
        return set()
    return set(
        Role.objects.filter(user_roles__user=user).values_list("data_scope", flat=True)
    )


def shared_entity_ids(user, entity_type):
    """Ids of `entity_type` records explicitly shared with `user` and not expired (§2).

    Materialised rather than left as a correlated subquery: the lookup is a single indexed
    read on `(shared_with_user, entity_type)` returning a handful of rows, and an `IN (...)`
    of literals keeps the outer plan simple. `access_level` is deliberately ignored here —
    both VIEW and EDIT grant *visibility*; EDIT versus VIEW is a permission question the
    write path answers, not a row-visibility one.
    """
    from .models import RecordShare

    now = timezone.now()
    return list(
        RecordShare.objects.filter(shared_with_user=user, entity_type=entity_type)
        .filter(Q(expires_at__isnull=True) | Q(expires_at__gt=now))
        .values_list("entity_id", flat=True)
    )


def _predicate_for(user, resource, *, scopes=None):
    """The combined Q for `user` over `resource` — UNFILTERED for everything, None for nothing.

    Multiple roles are **OR-ed, not ranked**. `identity_user_role` has no `is_primary` flag,
    so a user holding several roles has no single scope; each role contributes a predicate and
    the union is what they may see. This deliberately avoids inventing a total ordering over
    `data_scope` — FINANCE_ALL and BRANCH are not comparable, one being module-wide and the
    other org-position-wide, so "the broadest role wins" has no sound definition.
    """
    spec = REGISTRY[resource]

    # Resolved once and threaded through. `nested` re-enters this function per arm, so
    # `media` — three arms, each reaching a parent resource — issued four identical role
    # lookups for one list request.
    if scopes is None:
        scopes = scopes_for(user)

    predicate = None
    for scope in scopes:
        builder = spec.scopes.get(scope)
        if builder is None:
            continue  # deny by default: a scope the resource does not mention sees nothing
        predicate = _or(predicate, builder(user, spec.model, scopes))
        if predicate is UNFILTERED:
            return UNFILTERED

    # Unioned *outside* the role loop, and unconditionally: an explicit share is a grant in
    # its own right. A user whose roles grant nothing on this resource still sees what was
    # deliberately shared with them, which is the entire point of identity_record_share.
    if spec.entity_type is not None:
        ids = shared_entity_ids(user, spec.entity_type)
        if ids:
            predicate = _or(predicate, Q(pk__in=ids))

    return predicate


def apply_scope(qs, user, resource: str):
    """Restrict `qs` to the rows `user` may see of `resource` (architecture.md §2).

    Raises KeyError for an unregistered resource. That is intentional: a silent fallback to
    the unfiltered queryset is how scoping layers quietly stop scoping.

    No `.distinct()`. Every registered predicate is single-table by construction — to-many
    anchors go through `any_of` / `nested`, which subquery — so the filter cannot multiply
    rows. `tests/test_scoping.py` holds that invariant with a test rather than paying for a
    blanket DISTINCT on every list query and its pagination COUNT.
    """
    if not user or not user.is_authenticated:
        return qs.none()
    # Django's own superuser flag is an escape hatch for operators; it predates the role
    # system and is what `createsuperuser` grants before any role exists.
    if user.is_superuser:
        return qs

    predicate = _predicate_for(user, resource)
    if predicate is UNFILTERED:
        return qs
    if predicate is None:
        return qs.none()
    return qs.filter(predicate)


# --- The endpoint-side guard -------------------------------------------------------------


class ScopedQuerysetMixin:
    """Runs a view's queryset through `apply_scope`. Set `scope_resource` on the view.

    §2 requires *every* list and detail endpoint in every module to scope. The failure mode
    that matters is not a wrong anchor — that shows up in tests — but an endpoint that never
    scopes at all, which looks completely normal and returns the whole table. Two guards:

    * an unset `scope_resource` raises at request time rather than defaulting to anything;
    * a subclass that overrides `get_queryset` raises **at import time**. Overriding it is the
      obvious way to add `select_related` and is exactly how a view silently stops scoping,
      because the subclass method wins the MRO and the mixin never runs. Shape the queryset in
      `get_unscoped_queryset` instead, which is called from inside the scoped path.

    This lives here, next to `apply_scope`, and not in `identity/api/`: §1.2 makes every app's
    `api` package private, so a mixin other apps' views must subclass cannot live in one.
    """

    #: The registered resource name this view serves. Required.
    scope_resource: str | None = None

    def __init_subclass__(cls, **kwargs):
        super().__init_subclass__(**kwargs)
        if "get_queryset" in cls.__dict__:
            raise TypeError(
                f"{cls.__name__} overrides get_queryset() while using ScopedQuerysetMixin, "
                f"which bypasses row scoping entirely (architecture.md §2). Override "
                f"get_unscoped_queryset() instead."
            )

    def get_unscoped_queryset(self):
        """The queryset before scoping — override to add `select_related` and base filters."""
        return super().get_queryset()

    def get_queryset(self):
        from django.core.exceptions import ImproperlyConfigured

        resource = getattr(self, "scope_resource", None)
        if not resource:
            raise ImproperlyConfigured(
                f"{type(self).__name__} uses ScopedQuerysetMixin but sets no "
                f"`scope_resource`. architecture.md §2 requires every endpoint to declare "
                f"which resource's visibility rules apply; there is no safe default."
            )
        return apply_scope(self.get_unscoped_queryset(), self.request.user, resource)
