"""The notification engine (architecture.md §18, SRS 3.18) — `collaboration.services.notify`.

§1.2 lists "domain service → `collaboration.services.notify_*`" as a required orchestration,
and until now it did not exist: SRS 3.1.9 says leads past SLA are "flagged **and alerted**",
and only the flag was real. Every alert in the system now comes through this one function.

**Never raises.** The `record_event` doctrine applies with more force here: a lease
activation must not roll back because an SMTP server is down, and a caller's transaction
must not be poisoned by a failure inside a courtesy. Every database write runs in its own
savepoint; every gateway send is wrapped; the worst outcome of total failure is a logged
exception and a missing notification — never a failed business action.

**Suppression is an outcome, not an absence.** When a preference or quiet hours block a
channel, a `NotificationDispatchLog` row records *that decision* — "why didn't I get
notified?" is answerable only if the decision not to notify was written down (the model's
own doctrine).
"""
import logging
from zoneinfo import ZoneInfo

from django.db import transaction
from django.utils import timezone

from ..gateways import get_gateway
from ..models import Notification, NotificationDispatchLog, NotificationPreference

logger = logging.getLogger(__name__)

_CHANNELS = (
    # (dispatch-log channel, preference flag, gateway channel, address resolver)
    ("EMAIL", "email_enabled", "EMAIL", lambda user: user.email),
    ("SMS", "sms_enabled", "SMS", lambda user: user.phone),
    ("PUSH", "push_enabled", "PUSH", lambda user: str(user.pk)),
)


def notify(*, recipient, type, title, body=None, entity_type=None, entity_id=None, actor=None):
    """Create the in-app feed item and dispatch per-channel, honouring preferences.

    Returns the feed `Notification`, or None (inactive recipient, in-app disabled, or an
    unexpected failure — logged, never raised).
    """
    try:
        return _notify(
            recipient=recipient, type=type, title=title, body=body,
            entity_type=entity_type, entity_id=entity_id, actor=actor,
        )
    except Exception:  # noqa: BLE001 - see the module docstring
        logger.exception("notify() failed for recipient=%s type=%s", recipient, type)
        return None


def _notify(*, recipient, type, title, body, entity_type, entity_id, actor):
    if recipient is None or not recipient.is_active:
        return None
    if type not in set(Notification.Type.values):
        logger.error("notify(): unknown notification type %r", type)
        return None

    preference = NotificationPreference.objects.filter(
        user=recipient, notification_type=type
    ).first()
    quiet = _in_quiet_hours(preference)

    notification = None
    # IN_APP first, and never suppressed by quiet hours: the feed is where a suppressed
    # email's content survives — quiet hours silence the phone, not the record.
    if preference is None or preference.in_app_enabled:
        with transaction.atomic():
            notification = Notification.objects.create(
                recipient=recipient, type=type, title=title, body=body,
                entity_type=entity_type,
                entity_id=entity_id,
            )
            NotificationDispatchLog.objects.create(
                notification=notification, user=recipient,
                channel=NotificationDispatchLog.Channel.IN_APP,
                status=NotificationDispatchLog.Status.SENT,
            )
    else:
        _log(recipient, None, "IN_APP", NotificationDispatchLog.Status.SUPPRESSED_PREFERENCE)

    for channel, flag, gateway_channel, address_of in _CHANNELS:
        enabled = getattr(preference, flag) if preference is not None else _default_flag(flag)
        if not enabled:
            _log(recipient, notification, channel,
                 NotificationDispatchLog.Status.SUPPRESSED_PREFERENCE)
            continue
        if quiet:
            _log(recipient, notification, channel,
                 NotificationDispatchLog.Status.SUPPRESSED_QUIET_HOURS)
            continue
        address = address_of(recipient)
        if not address:
            _log(recipient, notification, channel, NotificationDispatchLog.Status.FAILED,
                 error="No address on file for this channel.")
            continue
        # Per-channel isolation: a misconfigured SMS gateway must not stop the email, and
        # a gateway that *raises* (config error, import error) is a FAILED dispatch, not a
        # dead loop.
        try:
            result = get_gateway(gateway_channel).send(
                to_address=address, subject=title, body=body or title
            )
            status, reference, error = (
                NotificationDispatchLog.Status.SENT
                if result.status == "SENT"
                else NotificationDispatchLog.Status.FAILED,
                result.provider_message_id,
                result.error,
            )
        except Exception as exc:  # noqa: BLE001
            logger.exception("Gateway %s failed for user=%s", gateway_channel, recipient.pk)
            status, reference, error = (
                NotificationDispatchLog.Status.FAILED, None, str(exc)[:500]
            )
        _log(recipient, notification, channel, status,
             provider_reference=reference, error=error)
    return notification


def _default_flag(flag: str) -> bool:
    """Model defaults when no preference row exists: in-app/email/push on, SMS off."""
    return {"email_enabled": True, "sms_enabled": False, "push_enabled": True}[flag]


def _log(user, notification, channel, status, *, provider_reference=None, error=None):
    try:
        with transaction.atomic():
            NotificationDispatchLog.objects.create(
                notification=notification, user=user, channel=channel, status=status,
                provider_reference=provider_reference or "",
                error_detail=error or "",
            )
    except Exception:  # noqa: BLE001 - the log must never take down the send loop
        logger.exception("Dispatch log write failed for user=%s channel=%s", user.pk, channel)


def _in_quiet_hours(preference, now=None) -> bool:
    """Inside the user's quiet window? Requires start+end (paired CHECK) *and* a timezone —
    a wall-clock window without a timezone is meaningless, so its absence means no window.

    Windows may cross midnight: 22:00–07:00 means `t >= start or t < end`.
    """
    if (
        preference is None
        or preference.quiet_hours_start is None
        or preference.quiet_hours_end is None
        or not preference.timezone
    ):
        return False
    try:
        local = (now or timezone.now()).astimezone(ZoneInfo(preference.timezone)).time()
    except Exception:  # noqa: BLE001 - a bad tz string must not block notifications
        logger.warning("Invalid timezone %r on preference %s", preference.timezone, preference.pk)
        return False
    start, end = preference.quiet_hours_start, preference.quiet_hours_end
    if start <= end:
        return start <= local < end
    return local >= start or local < end


# --- Feed and preferences (self-service reads/writes) --------------------------------------


def mark_read(notification, *, actor):
    """Recipient-only. Reading someone else's feed item is not a thing."""
    from django.core.exceptions import ValidationError

    if notification.recipient_id != actor.pk:
        raise ValidationError("You can only mark your own notifications read.")
    if not notification.is_read:
        notification.is_read = True
        notification.read_at = timezone.now()
        notification.save(update_fields=["is_read", "read_at"])
    return notification


def mark_all_read(*, actor) -> int:
    return Notification.objects.filter(recipient=actor, is_read=False).update(
        is_read=True, read_at=timezone.now()
    )


def set_notification_preference(*, actor, notification_type, **flags):
    """Upsert one (user, type) preference row. Validates tz + quiet-hour pairing before the
    CHECK does, so the user gets a field-keyed 400 instead of an IntegrityError."""
    from django.core.exceptions import ValidationError

    if notification_type not in set(Notification.Type.values):
        raise ValidationError({"notification_type": "Unknown notification type."})

    allowed = {
        "in_app_enabled", "email_enabled", "sms_enabled", "push_enabled",
        "quiet_hours_start", "quiet_hours_end", "timezone",
    }
    unknown = set(flags) - allowed
    if unknown:
        raise ValidationError({name: "Not a preference field." for name in unknown})

    start, end = flags.get("quiet_hours_start"), flags.get("quiet_hours_end")
    if (start is None) != (end is None):
        raise ValidationError(
            {"quiet_hours_end": "Quiet hours need both a start and an end."}
        )
    if start is not None and not flags.get("timezone"):
        raise ValidationError(
            {"timezone": "Quiet hours are wall-clock times; say which clock."}
        )
    if flags.get("timezone"):
        try:
            ZoneInfo(flags["timezone"])
        except Exception:  # noqa: BLE001
            raise ValidationError({"timezone": "Unknown timezone."}) from None

    preference, _ = NotificationPreference.objects.update_or_create(
        user=actor, notification_type=notification_type, defaults=flags
    )
    return preference
