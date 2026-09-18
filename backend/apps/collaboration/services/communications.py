"""Communications (SRS §3.9): threads, messages, call logs, internal notes, templates.

Every outbound message goes through a gateway (email real, SMS/WhatsApp mocked) and every
inbound one through `record_inbound_message`, which platform's webhook receiver calls.
Messages are conceptually immutable — status advances, the body never changes.
"""
import logging

from django.core.exceptions import ValidationError
from django.db import IntegrityError, transaction
from django.utils import timezone

from ..gateways import get_gateway
from ..merge import build_merge_context, render
from ..models import CallLog, InternalNote, Message, Template, Thread

logger = logging.getLogger(__name__)


@transaction.atomic
def send_message(*, actor, channel, contact=None, thread=None, to_address=None,
                 template=None, subject=None, body=None):
    """Compose and send. Template XOR inline body; the QUEUED row is committed, then the
    gateway is tried, so a delivery failure leaves the message on the record as FAILED — SRS
    3.9's "complete history" means the ones that did not send, too."""
    if bool(template) == bool(body):
        raise ValidationError({"body": "Provide exactly one of a template or a body."})
    if template is not None:
        if template.channel != channel:
            raise ValidationError({"template": "That template is for a different channel."})
        if not template.is_active:
            raise ValidationError({"template": "That template is inactive."})

    if thread is None and contact is None:
        raise ValidationError({"contact": "A message needs a contact or a thread."})
    if thread is None:
        thread = _resolve_thread(contact, channel, subject, actor)

    to_address = to_address or _address_for(thread.contact or contact, channel)
    if not to_address:
        raise ValidationError({"to_address": "No address on file for this channel."})

    context = build_merge_context(
        contact=thread.contact or contact, agent=actor,
        deal=thread.deal, lead=thread.lead, property=thread.property,
    )
    rendered_subject = render(subject, context) if subject else (
        render(template.subject, context) if template and template.subject else None
    )
    rendered_body = render(body if body is not None else template.body, context)

    message = Message.objects.create(
        thread=thread,
        channel=channel,
        direction=Message.Direction.OUTBOUND,
        from_address=None,
        to_address=to_address,
        sender_user=actor,
        contact=thread.contact or contact,
        template=template,
        subject=rendered_subject,
        body=rendered_body,
        status=Message.Status.QUEUED,
    )
    _deliver(message, rendered_subject, rendered_body)
    _touch_thread(thread, channel)
    return message


def _deliver(message, subject, body):
    try:
        result = get_gateway(message.channel).send(
            to_address=message.to_address, subject=subject, body=body
        )
        message.status = (
            Message.Status.SENT if result.status == "SENT" else Message.Status.FAILED
        )
        message.provider_message_id = result.provider_message_id
        if result.status == "SENT":
            message.sent_at = timezone.now()
    except Exception:  # noqa: BLE001 - a gateway failure is a FAILED message, not a crash
        logger.exception("Message send failed on %s", message.channel)
        message.status = Message.Status.FAILED
    message.save(update_fields=["status", "provider_message_id", "sent_at"])


def _resolve_thread(contact, channel, subject, actor):
    thread = (
        Thread.objects.filter(contact=contact, is_closed=False)
        .filter(channel__in=[channel, Thread.Channel.MIXED])
        .order_by("-last_message_at")
        .first()
    )
    if thread is None:
        thread = Thread.objects.create(
            channel=channel, contact=contact, subject=subject, assigned_to=actor,
        )
    return thread


def _touch_thread(thread, channel):
    fields = ["last_message_at"]
    thread.last_message_at = timezone.now()
    if thread.channel != channel and thread.channel != Thread.Channel.MIXED:
        thread.channel = Thread.Channel.MIXED
        fields.append("channel")
    thread.save(update_fields=fields)


def _address_for(contact, channel):
    if contact is None:
        return None
    if channel == "EMAIL":
        return contact.email
    return contact.phone


@transaction.atomic
def record_inbound_message(*, channel, from_address, body, subject=None,
                           provider_message_id=None, to_address=None, occurred_at=None):
    """The function platform's webhook receiver calls (platform → collaboration.services is
    a legal edge). Deduplicated on the partial-unique (channel, provider_message_id) — inbound
    webhooks retry, and a retried delivery must not become a second logged message."""

    if provider_message_id:
        existing = Message.objects.filter(
            channel=channel, provider_message_id=provider_message_id
        ).first()
        if existing:
            return existing

    contact = _match_contact(channel, from_address)
    thread = None
    if contact is not None:
        thread = (
            Thread.objects.filter(contact=contact, is_closed=False)
            .filter(channel__in=[channel, Thread.Channel.MIXED])
            .order_by("-last_message_at")
            .first()
        )
        if thread is None:
            thread = Thread.objects.create(channel=channel, contact=contact)
    if thread is None:
        thread = Thread.objects.create(channel=channel)

    try:
        with transaction.atomic():
            message = Message.objects.create(
                thread=thread,
                channel=channel,
                direction=Message.Direction.INBOUND,
                from_address=from_address,
                to_address=to_address,
                contact=contact,
                subject=subject,
                body=body,
                status=Message.Status.RECEIVED,
                provider_message_id=provider_message_id,
                sent_at=occurred_at or timezone.now(),
            )
    except IntegrityError:
        # Lost a race on the dedup index — the other writer's row is authoritative.
        return Message.objects.get(
            channel=channel, provider_message_id=provider_message_id
        )

    _touch_thread(thread, channel)
    if thread.assigned_to_id:
        from .notify import notify

        notify(
            recipient=thread.assigned_to,
            type="SYSTEM",
            title=f"New {channel.lower()} message",
            body=body[:200] if body else None,
            entity_type="THREAD",
            entity_id=thread.pk,
        )
    return message


def _match_contact(channel, address):
    from apps.contacts.models import Contact
    from apps.contacts.normalization import normalize_email, normalize_phone

    if channel == "EMAIL":
        return Contact.objects.filter(
            deleted_at__isnull=True, email=normalize_email(address)
        ).first()
    return Contact.objects.filter(
        deleted_at__isnull=True, phone=normalize_phone(address)
    ).first()


def update_message_status(*, channel, provider_message_id, status, timestamp=None):
    """Delivery/read callbacks. Monotonic (a DELIVERED can't regress to SENT); a no-op on an
    unknown id, because a provider retrying a callback for a message we never logged is not
    an error."""
    order = {"QUEUED": 0, "SENT": 1, "DELIVERED": 2, "READ": 3, "FAILED": 1}
    message = Message.objects.filter(
        channel=channel, provider_message_id=provider_message_id
    ).first()
    if message is None:
        return None
    if order.get(status, -1) <= order.get(message.status, 0) and status != "FAILED":
        return message
    message.status = status
    if status == "DELIVERED":
        message.delivered_at = timestamp or timezone.now()
    message.save(update_fields=["status", "delivered_at"])
    return message


# --- Threads --------------------------------------------------------------------------------


@transaction.atomic
def assign_thread(thread, *, actor, assignee):
    thread.assigned_to = assignee
    thread.save(update_fields=["assigned_to", "updated_at"])
    return thread


@transaction.atomic
def close_thread(thread, *, actor):
    thread.is_closed = True
    thread.save(update_fields=["is_closed", "updated_at"])
    return thread


@transaction.atomic
def reopen_thread(thread, *, actor):
    thread.is_closed = False
    thread.save(update_fields=["is_closed", "updated_at"])
    return thread


# --- Call logs (SRS 3.9.1) ------------------------------------------------------------------


@transaction.atomic
def log_call(*, actor, direction, phone_number, started_at, outcome, contact=None,
             lead=None, thread=None, duration_seconds=None, recording_file=None, notes=None):
    """Log a call outcome. Click-to-call is a `tel:` link on the frontend; this records what
    happened. `user` is the caller — required, because an unattributed call log is noise."""
    return CallLog.objects.create(
        user=actor, direction=direction, phone_number=phone_number, started_at=started_at,
        outcome=outcome, contact=contact, lead=lead, thread=thread,
        duration_seconds=duration_seconds, recording_file=recording_file, notes=notes,
    )


# --- Internal notes and @mentions (SRS 3.9.6) -----------------------------------------------


@transaction.atomic
def create_note(*, actor, body, mentions=None, contact=None, lead=None, deal=None,
                property=None, lease=None):
    targets = {"contact": contact, "lead": lead, "deal": deal, "property": property,
               "lease": lease}
    if not any(targets.values()):
        raise ValidationError({"detail": "A note must be attached to at least one record."})
    mention_ids = _validate_mentions(mentions)
    note = InternalNote.objects.create(
        author=actor, body=body, mentions=mention_ids, **targets
    )
    _fan_out_mentions(note, mention_ids, previous=set(), actor=actor)
    return note


@transaction.atomic
def update_note(note, *, actor, body=None, mentions=None):
    if note.author_id != actor.pk:
        raise ValidationError({"detail": "Only the author edits a note."})
    previous = set(note.mentions or [])
    if body is not None:
        note.body = body
    if mentions is not None:
        note.mentions = _validate_mentions(mentions)
    note.save(update_fields=["body", "mentions", "updated_at"])
    if mentions is not None:
        _fan_out_mentions(note, note.mentions, previous=previous, actor=actor)
    return note


def _validate_mentions(mentions):
    from apps.identity.models import User

    if not mentions:
        return []
    ids = [str(m) for m in mentions]
    valid = set(
        str(pk) for pk in User.objects.filter(
            pk__in=ids, is_active=True
        ).values_list("pk", flat=True)
    )
    unknown = set(ids) - valid
    if unknown:
        raise ValidationError({"mentions": f"Unknown or inactive users: {sorted(unknown)}"})
    return list(valid)


def _fan_out_mentions(note, mention_ids, *, previous, actor):
    """Notify newly-mentioned users only (an edit that keeps a mention does not re-ping),
    and never the author mentioning themselves."""
    from apps.identity.models import User

    from .notify import notify

    new_ids = set(mention_ids) - previous - {str(actor.pk)}
    for user in User.objects.filter(pk__in=new_ids):
        notify(
            recipient=user,
            type="MENTION",
            title=f"{actor.full_name} mentioned you",
            body=(note.body or "")[:200],
            entity_type="INTERNAL_NOTE",
            entity_id=note.pk,
            actor=actor,
        )


# --- Templates (SRS 3.9.4) ------------------------------------------------------------------


@transaction.atomic
def create_template(*, actor, name, channel, body, subject=None, variables=None):
    _validate_template_variables(body, variables)
    return Template.objects.create(
        name=name, channel=channel, body=body, subject=subject,
        variables=variables or {}, created_by=actor,
    )


@transaction.atomic
def update_template(template, *, actor, **fields):
    allowed = frozenset({"name", "channel", "body", "subject", "variables", "is_active"})
    unknown = set(fields) - allowed
    if unknown:
        raise ValidationError({name: "Not a template field." for name in unknown})
    if "body" in fields or "variables" in fields:
        _validate_template_variables(
            fields.get("body", template.body), fields.get("variables", template.variables)
        )
    for name, value in fields.items():
        setattr(template, name, value)
    template.save()
    return template


def _validate_template_variables(body, variables):
    """Declared variables must be real merge fields — a template promising
    `{{ contact.middle_name }}` would fail loudly at send time, so catch it at authoring."""
    from ..merge import declared_variables

    vocabulary = _full_vocabulary()
    used = declared_variables(body)
    unknown = used - vocabulary
    if unknown:
        raise ValidationError(
            {"body": f"Unknown merge variables: {sorted(unknown)}"}
        )


def _full_vocabulary():
    return {
        "today", "company.name",
        "contact.first_name", "contact.last_name", "contact.full_name",
        "contact.email", "contact.phone",
        "agent.name", "agent.email", "agent.phone",
        "lead.title", "deal.reference", "deal.title",
        "property.title", "property.city",
    }


def render_template_preview(template, *, contact=None, deal=None):
    context = build_merge_context(contact=contact, deal=deal)
    return {
        "subject": render(template.subject, context) if template.subject else None,
        "body": render(template.body, context),
    }
