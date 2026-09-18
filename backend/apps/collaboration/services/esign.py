"""Built-in e-signature (SRS 3.7.3, 3.7.5) — envelope → signer token → click-to-sign →
immutable events + document hash.

The signer token is `django.core.signing.dumps({"s": signer_id})`: unguessable (HMAC over
SECRET_KEY), stateless (no column to store or leak), expirable (max_age), and revocable by
state — every public request re-checks the envelope status, so voiding an envelope kills all
its outstanding tokens at once. A DocuSign adapter can replace `provider=INTERNAL` later
without touching the event trail.
"""
import logging

from django.conf import settings
from django.core import signing
from django.core.exceptions import ValidationError
from django.db import transaction
from django.utils import timezone

from ..models import EsignEnvelope, EsignSigner, SignatureEvent
from .files import file_sha256

logger = logging.getLogger(__name__)

_SALT = "collaboration.esign.signer"
_NON_CC_ROLES = ("SIGNER", "APPROVER", "WITNESS")

ENVELOPE_TRANSITIONS = {
    EsignEnvelope.Status.DRAFT: {EsignEnvelope.Status.SENT, EsignEnvelope.Status.VOIDED},
    EsignEnvelope.Status.SENT: {
        EsignEnvelope.Status.PARTIALLY_SIGNED, EsignEnvelope.Status.COMPLETED,
        EsignEnvelope.Status.DECLINED, EsignEnvelope.Status.VOIDED,
        EsignEnvelope.Status.EXPIRED,
    },
    EsignEnvelope.Status.PARTIALLY_SIGNED: {
        EsignEnvelope.Status.COMPLETED, EsignEnvelope.Status.DECLINED,
        EsignEnvelope.Status.VOIDED, EsignEnvelope.Status.EXPIRED,
    },
    EsignEnvelope.Status.COMPLETED: set(),
    EsignEnvelope.Status.DECLINED: set(),
    EsignEnvelope.Status.VOIDED: set(),
    EsignEnvelope.Status.EXPIRED: set(),
}


def sign_token(signer):
    return signing.dumps({"s": str(signer.pk)}, salt=_SALT)


def resolve_token(token):
    """A live signer for a valid, unexpired token whose envelope is still out for signing.
    Any failure — tampered token, expired, voided envelope — is None, and the public views
    turn None into a 404."""
    max_age = getattr(settings, "ESIGN_TOKEN_MAX_AGE_DAYS", 30) * 86400
    try:
        payload = signing.loads(token, salt=_SALT, max_age=max_age)
    except signing.BadSignature:
        return None
    signer = (
        EsignSigner.objects.filter(pk=payload["s"])
        .select_related("envelope", "envelope__document")
        .first()
    )
    if signer is None:
        return None
    envelope = signer.envelope
    if envelope.status not in (
        EsignEnvelope.Status.SENT, EsignEnvelope.Status.PARTIALLY_SIGNED
    ):
        return None
    if envelope.expires_at and envelope.expires_at < timezone.now():
        return None
    return signer


def _event(envelope, event_type, *, signer=None, actor=None, ip=None, ua=None, metadata=None):
    return SignatureEvent.objects.create(
        envelope=envelope, signer=signer, event_type=event_type,
        ip_address=ip, user_agent=ua, metadata=metadata or {},
        occurred_at=timezone.now(), created_by=actor,
    )


def _move_envelope(envelope, new_status):
    allowed = ENVELOPE_TRANSITIONS.get(envelope.status, set())
    if new_status not in allowed:
        raise ValidationError(
            {"status": f"Cannot move an envelope from {envelope.status} to {new_status}."}
        )
    envelope.status = new_status


@transaction.atomic
def create_envelope(*, actor, document, subject, message=None, expires_at=None,
                    provider=EsignEnvelope.Provider.INTERNAL, signers):
    from .. import selectors

    if selectors.document_access_level(actor, document) != "EDIT":
        raise ValidationError({"document": "You need edit access to send this for signing."})
    if not signers:
        raise ValidationError({"signers": "An envelope needs at least one signer."})

    envelope = EsignEnvelope.objects.create(
        document=document, subject=subject, message=message, expires_at=expires_at,
        provider=provider, status=EsignEnvelope.Status.DRAFT, created_by=actor,
    )
    for entry in signers:
        contact, user = entry.get("contact"), entry.get("user")
        if bool(contact) == bool(user):
            raise ValidationError(
                {"signers": "Each signer is exactly one of a contact or a user."}
            )
        name = entry.get("name") or (str(contact) if contact else user.full_name)
        email = entry.get("email") or (contact.email if contact else user.email)
        if not email:
            raise ValidationError({"signers": f"No email for signer {name}."})
        EsignSigner.objects.create(
            envelope=envelope, contact=contact, user=user, name=name, email=email,
            signing_order=entry.get("signing_order", 1),
            role=entry.get("role", EsignSigner.Role.SIGNER),
        )
    _event(envelope, SignatureEvent.EventType.CREATED, actor=actor,
           metadata={"signer_count": len(signers)})
    return envelope


@transaction.atomic
def send_envelope(envelope, *, actor):
    """DRAFT → SENT. Sequential signing: only the lowest-order unsigned non-CC signer is
    emailed. Their token URL rides in the email and is never stored or logged."""
    _move_envelope(envelope, EsignEnvelope.Status.SENT)
    envelope.sent_at = timezone.now()
    envelope.save(update_fields=["status", "sent_at", "updated_at"])
    _dispatch_next(envelope)
    return envelope


def _current_signer(envelope):
    return (
        envelope.signers.filter(role__in=_NON_CC_ROLES)
        .exclude(status__in=(EsignSigner.Status.SIGNED, EsignSigner.Status.DECLINED))
        .order_by("signing_order")
        .first()
    )


def _dispatch_next(envelope):
    signer = _current_signer(envelope)
    if signer is None:
        return
    _email_signer(envelope, signer)
    if signer.status == EsignSigner.Status.PENDING:
        signer.status = EsignSigner.Status.SENT
        signer.save(update_fields=["status"])
    _event(envelope, SignatureEvent.EventType.SENT, signer=signer,
           metadata={"email": signer.email})


def _email_signer(envelope, signer):
    from ..gateways import get_gateway

    template = getattr(settings, "ESIGN_PUBLIC_URL_TEMPLATE", "/public/esign/{token}/")
    url = template.format(token=sign_token(signer))
    get_gateway("EMAIL").send(
        to_address=signer.email,
        subject=f"Please sign: {envelope.subject}",
        body=(
            f"{envelope.message or ''}\n\n"
            f"Review and sign the document here:\n{url}\n"
        ),
    )


@transaction.atomic
def record_view(signer, *, ip=None, user_agent=None):
    """A signer opened the document. Only the first view flips SENT→VIEWED, but EVERY view
    appends an event — SRS 3.7.5 wants each access on the record."""
    if signer.status == EsignSigner.Status.SENT:
        signer.status = EsignSigner.Status.VIEWED
        signer.save(update_fields=["status"])
    _event(signer.envelope, SignatureEvent.EventType.VIEWED, signer=signer,
           ip=ip, ua=user_agent)
    return signer


@transaction.atomic
def sign(signer, *, typed_name, consent, ip=None, user_agent=None):
    """The signature. Turn-checked (no earlier non-CC signer still pending), consent
    required, name required. The SIGNED event carries the document sha256 — the fingerprint
    of what this person actually signed."""
    envelope = EsignEnvelope.objects.select_for_update().get(pk=signer.envelope_id)
    signer = EsignSigner.objects.select_for_update().get(pk=signer.pk)
    if envelope.status not in (
        EsignEnvelope.Status.SENT, EsignEnvelope.Status.PARTIALLY_SIGNED
    ):
        raise ValidationError({"status": "This envelope is no longer open for signing."})
    if signer.status in (EsignSigner.Status.SIGNED, EsignSigner.Status.DECLINED):
        raise ValidationError({"status": "You have already responded."})
    ahead = (
        envelope.signers.filter(
            role__in=_NON_CC_ROLES, signing_order__lt=signer.signing_order
        )
        .exclude(status=EsignSigner.Status.SIGNED)
        .exists()
    )
    if ahead:
        raise ValidationError({"signing_order": "It is not your turn to sign yet."})
    if consent is not True:
        raise ValidationError({"consent": "You must consent to sign electronically."})
    if not (typed_name or "").strip():
        raise ValidationError({"typed_name": "Type your name to sign."})

    signer.status = EsignSigner.Status.SIGNED
    signer.signed_at = timezone.now()
    signer.save(update_fields=["status", "signed_at"])
    doc_hash = file_sha256(envelope.document.file)
    _event(
        envelope, SignatureEvent.EventType.SIGNED, signer=signer, ip=ip, ua=user_agent,
        metadata={"typed_name": typed_name, "consent": True, "document_sha256": doc_hash},
    )

    remaining = _current_signer(envelope)
    if remaining is None:
        envelope.status = EsignEnvelope.Status.COMPLETED
        envelope.completed_at = timezone.now()
        envelope.save(update_fields=["status", "completed_at", "updated_at"])
        _event(envelope, SignatureEvent.EventType.COMPLETED,
               metadata={"document_sha256": doc_hash})
        _notify_creator(envelope, "completed")
        from apps.platform.services import emit_webhook_event

        emit_webhook_event(
            "document.signed",
            {
                "envelope_id": str(envelope.pk),
                "document_id": str(envelope.document_id),
                "document_sha256": doc_hash,
            },
        )
    else:
        if envelope.status == EsignEnvelope.Status.SENT:
            envelope.status = EsignEnvelope.Status.PARTIALLY_SIGNED
            envelope.save(update_fields=["status", "updated_at"])
        _dispatch_next(envelope)
    return signer


@transaction.atomic
def decline(signer, *, reason, ip=None, user_agent=None):
    envelope = signer.envelope
    signer.status = EsignSigner.Status.DECLINED
    signer.save(update_fields=["status"])
    _event(envelope, SignatureEvent.EventType.DECLINED, signer=signer, ip=ip, ua=user_agent,
           metadata={"reason": reason})
    if envelope.status in (
        EsignEnvelope.Status.SENT, EsignEnvelope.Status.PARTIALLY_SIGNED
    ):
        envelope.status = EsignEnvelope.Status.DECLINED
        envelope.save(update_fields=["status", "updated_at"])
    _notify_creator(envelope, "declined")
    return signer


@transaction.atomic
def void_envelope(envelope, *, actor, reason):
    _move_envelope(envelope, EsignEnvelope.Status.VOIDED)
    envelope.save(update_fields=["status", "updated_at"])
    _event(envelope, SignatureEvent.EventType.VOIDED, actor=actor,
           metadata={"reason": reason})
    return envelope


@transaction.atomic
def remind(envelope, *, actor=None):
    signer = _current_signer(envelope)
    if signer is None:
        raise ValidationError({"status": "No one is pending on this envelope."})
    _email_signer(envelope, signer)
    _event(envelope, SignatureEvent.EventType.REMINDER_SENT, signer=signer, actor=actor)
    return envelope


def _notify_creator(envelope, what):
    from .notify import notify

    notify(
        recipient=envelope.created_by,
        type="SYSTEM",
        title=f"Envelope {what}: {envelope.subject}",
        entity_type="ESIGN_ENVELOPE",
        entity_id=envelope.pk,
    )
