"""Documents: repository, access control, linking, versions (SRS §3.7).

One access function rules everything — `selectors.document_access_level` — and the writes
here enforce the same answer, so the scoping arm, the download gate and the mutation rules
cannot drift apart.
"""
import logging

from django.core.exceptions import ValidationError
from django.db import transaction

from ..models import AccessGrant, Document, DocumentLink, EsignEnvelope
from .files import store_file

logger = logging.getLogger(__name__)

_LINK_TARGETS = ("contact", "property", "deal", "lease", "transaction", "thread", "message")

_LIVE_ENVELOPES = (EsignEnvelope.Status.SENT, EsignEnvelope.Status.PARTIALLY_SIGNED)


def _require_edit(user, document):
    from .. import selectors

    if selectors.document_access_level(user, document) != "EDIT":
        raise ValidationError(
            {"detail": "You need edit access to this document."}
        )


def _clean_links(links):
    cleaned = []
    if not links:
        raise ValidationError(
            {"links": "A document must be linked to at least one record — a repository "
                      "entry reachable from nothing belongs to nothing."}
        )
    for link in links:
        targets = {key: link[key] for key in _LINK_TARGETS if link.get(key) is not None}
        if not targets:
            raise ValidationError({"links": "Each link needs a target record."})
        cleaned.append(
            {**targets, "relationship_type": link.get("relationship_type") or "ATTACHMENT"}
        )
    return cleaned


@transaction.atomic
def create_document(*, actor, file, title, document_type, status="ACTIVE",
                    is_confidential=False, links):
    if document_type not in set(Document.DocumentType.values):
        raise ValidationError({"document_type": "Unknown document type."})
    # Attaching someone else's raw upload would let any staff member publish files they
    # cannot even download. ALL-scope staff excepted — they administer the repository.
    from apps.identity.models import Role
    from apps.identity.scoping import scopes_for

    if file.uploaded_by_id != actor.pk and Role.DataScope.ALL not in scopes_for(actor):
        raise ValidationError({"file": "You can only attach files you uploaded."})

    cleaned = _clean_links(links)
    document = Document.objects.create(
        file=file, title=title, document_type=document_type, status=status,
        is_confidential=is_confidential, uploaded_by=actor,
    )
    for link in cleaned:
        DocumentLink.objects.create(document=document, **link)

    _audit(actor, "CREATE", document, new={"title": title, "confidential": is_confidential})
    return document


@transaction.atomic
def add_document_version(document, *, actor, file):
    """Same row, version+1 — the version history IS the audit trail (no version table, on
    purpose: EXPECTED_TABLES is exact and the integer + append-only audit satisfies SRS
    3.7.1's "version"). Blocked while a signature is live: you cannot swap the paper under
    someone's pen."""
    _require_edit(actor, document)
    if document.esign_envelopes.filter(status__in=_LIVE_ENVELOPES).exists():
        raise ValidationError(
            {"file": "An envelope is out for signing on this document; void it first."}
        )
    old_file_id, old_version = document.file_id, document.version
    document.file = file
    document.version += 1
    document.save(update_fields=["file", "version", "updated_at"])
    _audit(
        actor, "UPDATE", document,
        old={"file_id": str(old_file_id), "version": old_version},
        new={"file_id": str(file.pk), "version": document.version},
    )
    return document


@transaction.atomic
def update_document(document, *, actor, **fields):
    _require_edit(actor, document)
    allowed = frozenset({"title", "document_type", "status", "is_confidential"})
    unknown = set(fields) - allowed
    if unknown:
        raise ValidationError(
            {name: "This field cannot be set through the document service."
             for name in unknown}
        )
    for name, value in fields.items():
        setattr(document, name, value)
    document.save()
    return document


@transaction.atomic
def link_document(document, *, actor, **link):
    _require_edit(actor, document)
    cleaned = _clean_links([link])[0]
    return DocumentLink.objects.create(document=document, **cleaned)


@transaction.atomic
def unlink_document(link, *, actor):
    _require_edit(actor, link.document)
    if link.document.links.count() <= 1:
        raise ValidationError(
            {"detail": "A document keeps at least one link; delete the document instead."}
        )
    link.delete()


@transaction.atomic
def grant_access(document, *, actor, role=None, user=None, access_level):
    """SRS 3.7.4 — role- or user-level document access. Exactly one grantee (tightening the
    DB's at-least-one CHECK: a grant naming both is ambiguous about intent)."""
    _require_edit(actor, document)
    if bool(role) == bool(user):
        raise ValidationError({"detail": "Grant to exactly one of a role or a user."})
    if access_level not in set(AccessGrant.AccessLevel.values):
        raise ValidationError({"access_level": "Unknown access level."})
    grant, _ = AccessGrant.objects.update_or_create(
        document=document, role=role, user=user,
        defaults={"access_level": access_level, "granted_by": actor},
    )
    _audit(actor, "UPDATE", document,
           new={"granted": access_level,
                "role": role.code if role else None,
                "user": str(user.pk) if user else None})
    return grant


@transaction.atomic
def revoke_access(grant, *, actor):
    _require_edit(actor, grant.document)
    _audit(actor, "UPDATE", grant.document,
           old={"revoked": grant.access_level,
                "role": grant.role.code if grant.role_id else None,
                "user": str(grant.user_id) if grant.user_id else None})
    grant.delete()


@transaction.atomic
def delete_document(document, *, actor):
    from django.utils import timezone

    _require_edit(actor, document)
    if document.esign_envelopes.filter(status__in=_LIVE_ENVELOPES).exists():
        raise ValidationError(
            {"detail": "An envelope is live on this document; void it first."}
        )
    document.deleted_at = timezone.now()
    document.save(update_fields=["deleted_at"])
    _audit(actor, "DELETE", document, old={"title": document.title})
    return document


@transaction.atomic
def generate_document_from_template(*, actor, template, title, document_type, links,
                                    contact=None, lead=None, deal=None, property=None):
    """Merge-to-HTML document generation (SRS 3.6.2, 3.7.2). PDF is a documented later step
    (weasyprint, third-part-needed.md §20); HTML is honest and renderable today."""
    from django.core.files.base import ContentFile

    from ..merge import build_merge_context, render

    context = build_merge_context(
        contact=contact, lead=lead, deal=deal, property=property, agent=actor
    )
    body = render(template.body, context)
    html = (
        "<!doctype html><html><head><meta charset='utf-8'>"
        f"<title>{title}</title></head><body>{body}</body></html>"
    )
    upload = ContentFile(html.encode(), name=f"{title[:60]}.html")
    upload.content_type = "text/html"
    file = store_file(actor=actor, uploaded_file=upload)
    return create_document(
        actor=actor, file=file, title=title, document_type=document_type, links=links
    )


def _audit(actor, action, document, old=None, new=None):
    from apps.platform.services import record_event

    record_event(
        action=action, entity_type="DOCUMENT", entity_id=document.pk, actor=actor,
        old_values=old, new_values=new,
    )
