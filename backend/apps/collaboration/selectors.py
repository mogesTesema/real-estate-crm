"""Public reads / scoped querysets for `collaboration` (architecture.md §1.2).

Other apps read `collaboration` rows through this module. They must never import
`apps.collaboration.api` — that package is the HTTP surface and is private to this app.
"""
from .models import Activity


def activity_for_source(source_type, source_id):
    """The one activity mirroring a domain record, or None."""
    return Activity.objects.filter(source_type=source_type, source_id=source_id).first()


def calendar_for(user, *, start=None, end=None):
    """A user's own calendar. Not `apply_scope`d: an activity is assigned *to* someone, and
    "my calendar" is the assignment, not a row-visibility question. A manager reading a
    colleague's diary is a separate endpoint with its own scoping, which arrives with the
    activities pass."""
    qs = Activity.objects.filter(assigned_to=user)
    if start:
        qs = qs.filter(start_at__gte=start)
    if end:
        qs = qs.filter(start_at__lte=end)
    return qs.order_by("start_at")


def my_notifications(user):
    """A user's own feed, newest first. The scoping registry enforces the same rule for
    every caller; this is the convenience spelling."""
    from apps.identity.selectors import apply_scope

    from .models import Notification

    return apply_scope(
        Notification.objects.all(), user, "notification"
    ).order_by("-created_at")


def unread_count(user) -> int:
    return my_notifications(user).filter(is_read=False).count()


def user_can_download_file(user, file) -> bool:
    """May `user` receive this file's bytes?

    A `File` row has no owner-anchor of its own beyond the uploader — its meaning comes from
    whatever wraps it. So access is: you uploaded it, or something you are allowed to see
    carries it. Checked against the wrapping aggregates that exist so far (inventory media);
    documents and call recordings extend this in their own pass.
    """
    from apps.identity.selectors import apply_scope

    if file.uploaded_by_id == user.pk:
        return True

    # A document wrapping the file speaks for it — including the VIEW-vs-DOWNLOAD split and
    # the confidential inversion (SRS 3.7.4/3.7.5).
    for document in file.documents.filter(deleted_at__isnull=True):
        if document_access_level(user, document) in ("DOWNLOAD", "EDIT"):
            _audit_document_view(user, document)
            return True

    from apps.inventory.models import Media

    if apply_scope(
        Media.objects.filter(file=file, deleted_at__isnull=True), user, "media"
    ).exists():
        return True

    from apps.identity.models import Role
    from apps.identity.scoping import scopes_for

    return Role.DataScope.ALL in scopes_for(user)


def document_access_level(user, document):
    """The one answer to "may this user touch this document?" — None / VIEW / DOWNLOAD /
    EDIT. Used by the scoping arm, the download gate, and every write service, so the three
    cannot drift.

    The rule set (SRS 3.7.4), in precedence order:
    * uploader → EDIT, always — confidentiality restricts *others*.
    * explicit grants (user- or role-level) → the granted level, max across roles.
    * ALL-scope staff → EDIT on non-confidential, **grants-only on confidential** — the one
      place the usual ALL rule inverts, because "confidential" that the whole management
      chain can read by default is a label, not a control.
    * other staff → DOWNLOAD on non-confidential documents linked to a record they can see.
    * portal clients → DOWNLOAD on non-confidential documents linked to their own
      contact/lease/won-deal (§2's "own documents").
    """
    from django.db.models import Q

    from apps.identity.models import Role
    from apps.identity.scoping import portal_contact_id, scopes_for

    if document.uploaded_by_id == user.pk:
        return "EDIT"

    grants = document.access_grants.filter(
        Q(user=user) | Q(role__user_roles__user=user)
    ).values_list("access_level", flat=True)
    if grants:
        order = {"VIEW": 1, "DOWNLOAD": 2, "EDIT": 3}
        return max(grants, key=lambda level: order[level])

    if document.is_confidential:
        return None

    scopes = scopes_for(user)
    if Role.DataScope.ALL in scopes or user.is_superuser:
        return "EDIT"

    portal_cid = portal_contact_id(user)
    if portal_cid is not None:
        return "DOWNLOAD" if _document_linked_to_portal_contact(document, portal_cid) else None

    return "DOWNLOAD" if _document_linked_to_visible_record(document, user) else None


def _document_linked_to_portal_contact(document, contact_id) -> bool:
    from django.db.models import Q

    return document.links.filter(
        Q(contact_id=contact_id)
        | Q(lease__tenant_id=contact_id)
        | Q(lease__landlord_id=contact_id)
        | Q(lease__parties__contact_id=contact_id)
        | Q(deal__primary_contact_id=contact_id, deal__status="WON")
    ).exists()


def _document_linked_to_visible_record(document, user) -> bool:
    from apps.identity.selectors import apply_scope

    for link in document.links.all():
        for field, resource in (
            ("contact", "contact"), ("property", "property"), ("deal", "deal"),
            ("lease", "lease"),
        ):
            target_id = getattr(link, f"{field}_id")
            if target_id is None:
                continue
            model = link._meta.get_field(field).related_model
            if apply_scope(
                model._default_manager.filter(pk=target_id), user, resource
            ).exists():
                return True
    return False


def live_documents():
    from .models import Document

    return Document.objects.filter(deleted_at__isnull=True).select_related(
        "file", "uploaded_by"
    ).prefetch_related("links", "access_grants")


def _audit_document_view(user, document):
    """SRS 3.7.5 — the immutable trail of document VIEWS. Every gated download of a
    document-wrapped file writes one."""
    from apps.platform.services import record_event

    record_event(
        action="VIEW", entity_type="DOCUMENT", entity_id=document.pk, actor=user,
        new_values={"file_id": str(document.file_id), "version": document.version},
    )
