"""How `collaboration` rows are scoped (architecture.md §2).

Registered from `CollaborationConfig.ready()`.

`document` is the odd one out: its arm mirrors `selectors.document_access_level` — including
the inversion where ALL-scope staff do NOT see confidential documents without a grant. The
arm and the function are kept adjacent in review because they must answer identically.
"""
from django.db.models import Q

from apps.identity.scoping import (
    EVERYTHING,
    NOTHING,
    in_my_branch,
    in_my_team,
    nested,
    owned_by,
    register,
    where,
)


def _grants_arm():
    def build(user):
        from .models import AccessGrant

        return Q(
            pk__in=AccessGrant.objects.filter(
                Q(user=user) | Q(role__user_roles__user=user)
            ).values("document_id")
        )

    return where(build)


def _linked_visible(resource, link_field):
    """Documents linked to a record the caller can see — the repository-per-record read
    (SRS 3.7.1), subqueried so the outer query stays single-table."""

    from apps.identity.scoping import UNFILTERED, Builder, _predicate_for

    def build(user, model, scopes=None):
        from .models import DocumentLink

        predicate = _predicate_for(user, resource, scopes=scopes)
        if predicate is None:
            return None
        links = DocumentLink.objects.filter(**{f"{link_field}__isnull": False})
        if predicate is not UNFILTERED:
            parent_model = DocumentLink._meta.get_field(link_field).related_model
            links = links.filter(
                **{f"{link_field}__in":
                   parent_model._default_manager.filter(predicate).values("pk")}
            )
        return Q(pk__in=links.values("document_id"))

    return Builder(build)


def register_resources():
    from apps.identity.models import Role

    from .models import Document, EsignEnvelope, Notification

    scope = Role.DataScope

    register(
        "notification",
        model=Notification,
        entity_type=None,
        scopes={s: owned_by("recipient") for s in scope.values},
    )

    non_confidential = where(lambda user: Q(is_confidential=False))
    mine_or_granted = owned_by("uploaded_by") | _grants_arm()
    linked = (
        _linked_visible("contact", "contact")
        | _linked_visible("property", "property")
        | _linked_visible("deal", "deal")
        | _linked_visible("lease", "lease")
    )

    register(
        "document",
        model=Document,
        entity_type=None,
        scopes={
            # ALL is NOT EVERYTHING here: confidential documents need a grant even from the
            # top — "confidential" the whole management chain reads by default is a label,
            # not a control (SRS 3.7.4).
            scope.ALL: non_confidential | mine_or_granted,
            **{
                s: mine_or_granted | (non_confidential & linked)
                for s in (
                    scope.BRANCH, scope.TEAM, scope.OWN, scope.MANAGED_PROPERTIES,
                    scope.FINANCE_ALL, scope.MARKETING_ALL,
                )
            },
            scope.PORTAL_OWN: non_confidential & linked,
        },
    )

    from .models import Activity, CallLog, InternalNote, Message, Thread

    register(
        "activity",
        model=Activity,
        entity_type=None,
        scopes={
            scope.ALL: EVERYTHING,
            scope.BRANCH: in_my_branch("assigned_to"),
            scope.TEAM: in_my_team("assigned_to"),
            scope.OWN: owned_by("assigned_to", "created_by"),
            scope.MANAGED_PROPERTIES: owned_by("assigned_to", "created_by"),
            scope.FINANCE_ALL: owned_by("assigned_to", "created_by"),
            scope.MARKETING_ALL: owned_by("assigned_to", "created_by"),
            scope.PORTAL_OWN: NOTHING,
        },
    )

    register(
        "thread",
        model=Thread,
        entity_type=None,
        scopes={
            scope.ALL: EVERYTHING,
            scope.BRANCH: in_my_branch("assigned_to")
            | nested("lead", "lead") | nested("deal", "deal"),
            scope.TEAM: in_my_team("assigned_to")
            | nested("lead", "lead") | nested("deal", "deal"),
            scope.OWN: owned_by("assigned_to")
            | nested("contact", "contact") | nested("lead", "lead")
            | nested("deal", "deal") | nested("property", "property"),
            scope.MANAGED_PROPERTIES: nested("property", "property"),
            scope.FINANCE_ALL: NOTHING,
            scope.MARKETING_ALL: NOTHING,
            scope.PORTAL_OWN: NOTHING,
        },
    )

    register(
        "message",
        model=Message,
        entity_type=None,
        scopes={s: nested("thread", "thread") for s in scope.values},
    )

    register(
        "call_log",
        model=CallLog,
        entity_type=None,
        scopes={
            scope.ALL: EVERYTHING,
            scope.BRANCH: in_my_branch("user"),
            scope.TEAM: in_my_team("user"),
            scope.OWN: owned_by("user"),
            scope.MANAGED_PROPERTIES: owned_by("user"),
            scope.FINANCE_ALL: NOTHING,
            scope.MARKETING_ALL: NOTHING,
            scope.PORTAL_OWN: NOTHING,
        },
    )

    # Internal notes are internal by definition — the portal never reads them.
    mentioned = where(
        lambda user: Q(mentions__contains=[str(user.pk)])
    )
    register(
        "internal_note",
        model=InternalNote,
        entity_type=None,
        scopes={
            scope.ALL: EVERYTHING,
            **{
                s: owned_by("author") | mentioned
                | nested("contact", "contact") | nested("lead", "lead")
                | nested("deal", "deal") | nested("property", "property")
                | nested("lease", "lease")
                for s in (
                    scope.BRANCH, scope.TEAM, scope.OWN, scope.MANAGED_PROPERTIES,
                    scope.FINANCE_ALL, scope.MARKETING_ALL,
                )
            },
            scope.PORTAL_OWN: NOTHING,
        },
    )

    register(
        "esign_envelope",
        model=EsignEnvelope,
        entity_type=None,
        scopes={
            scope.ALL: EVERYTHING,
            **{
                s: owned_by("created_by") | nested("document", "document")
                for s in (
                    scope.BRANCH, scope.TEAM, scope.OWN, scope.MANAGED_PROPERTIES,
                    scope.FINANCE_ALL, scope.MARKETING_ALL,
                )
            },
            # Signers use token URLs; the portal never lists envelopes.
            scope.PORTAL_OWN: NOTHING,
        },
    )
