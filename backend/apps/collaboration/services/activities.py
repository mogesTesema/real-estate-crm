"""Activities and the unified calendar (architecture.md §13, SRS 3.12).

`upsert_activity_for_source` is the required orchestration §1.2 lists: a domain record that
appears on the calendar — a viewing, an inspection — owns exactly one activity, and §13
forbids a standalone VIEWING activity. Domain services call this; nothing creates a
source-backed activity any other way.
"""
import logging

from django.core.exceptions import ValidationError
from django.db import transaction
from django.utils import timezone

from ..models import Activity

logger = logging.getLogger(__name__)


@transaction.atomic
def upsert_activity_for_source(
    *,
    source_type,
    source_id,
    activity_type,
    subject,
    assigned_to,
    actor,
    start_at=None,
    due_at=None,
    status=None,
    contact=None,
    lead=None,
    deal=None,
    property=None,
    lease=None,
    description=None,
):
    """Create or update the one activity that mirrors a domain record on the calendar.

    A genuine upsert, not a get-then-create: `collaboration_activity` carries a partial unique
    index on `(source_type, source_id)`, so two concurrent reschedules of the same viewing
    cannot both insert. `update_or_create` maps onto that index directly.

    **Why domain apps call this rather than writing the row.** §13 forbids a standalone
    VIEWING activity — an activity whose `source_type` is set but whose source no longer
    exists, or a viewing with two calendar entries, is a diary that disagrees with the CRM.
    Keeping one function means rescheduling updates rather than duplicating, and it means the
    rule holds for inspections in a later pass without being re-implemented.
    """
    if source_type not in set(Activity.SourceType.values):
        raise ValidationError({"source_type": f"Unknown activity source {source_type!r}."})
    if source_id is None:
        raise ValidationError({"source_id": "A source-backed activity needs its source id."})

    activity, created = Activity.objects.update_or_create(
        source_type=source_type,
        source_id=source_id,
        defaults={
            "activity_type": activity_type,
            "subject": subject,
            "description": description,
            "assigned_to": assigned_to,
            "created_by": actor,
            "start_at": start_at,
            "due_at": due_at,
            "status": status or Activity.Status.OPEN,
            "contact": contact,
            "lead": lead,
            "deal": deal,
            "property": property,
            "lease": lease,
        },
    )
    return activity, created


@transaction.atomic
def close_activity_for_source(*, source_type, source_id, status, completed_at=None):
    """Mark the mirrored activity done or cancelled when its source reaches a final state.

    Silent when there is no activity: an older record created before this orchestration
    existed has nothing to close, and refusing would block the domain action for the sake of
    a calendar row.
    """
    activity = Activity.objects.filter(
        source_type=source_type, source_id=source_id
    ).first()
    if activity is None:
        return None
    activity.status = status
    activity.completed_at = completed_at
    activity.save(update_fields=["status", "completed_at"])
    return activity


def create_task_for_stage_move(*, deal, subject, actor):
    """The mandatory next action (SRS 3.4.4) as a real task (SRS 3.4.5).

    Called by `crm.services.move_stage`. Deliberately minimal until the activities pass
    lands the full task API: an OPEN TASK on the deal owner's list, never raising — a
    failed task write must not fail a stage move.
    """
    import logging

    logger = logging.getLogger(__name__)
    try:
        with transaction.atomic():
            return Activity.objects.create(
                activity_type=Activity.ActivityType.TASK,
                subject=subject[:255],
                assigned_to=deal.owner,
                created_by=actor,
                deal=deal,
                contact=deal.primary_contact,
                status=Activity.Status.OPEN,
            )
    except Exception:  # noqa: BLE001 - courtesy artefact, never blocks the move
        logger.exception("Stage-move task creation failed for deal=%s", deal.pk)
        return None


def create_task_for_lease_expiry(lease):
    """A renewal follow-up task on the property manager's list (SRS 3.5.5, 3.12.2).

    Same never-raises contract as the stage-move task: a sweep must not die on one row.
    """
    import logging

    logger = logging.getLogger(__name__)
    try:
        with transaction.atomic():
            existing = Activity.objects.filter(
                activity_type=Activity.ActivityType.TASK,
                lease=lease,
                status__in=(Activity.Status.OPEN, Activity.Status.IN_PROGRESS),
                subject__startswith="Lease renewal follow-up",
            ).exists()
            if existing:
                return None
            return Activity.objects.create(
                activity_type=Activity.ActivityType.TASK,
                subject=f"Lease renewal follow-up: {lease.reference_code}",
                assigned_to=lease.property_manager,
                created_by=lease.property_manager,
                lease=lease,
                contact=lease.tenant,
                status=Activity.Status.OPEN,
            )
    except Exception:  # noqa: BLE001
        logger.exception("Lease-expiry task creation failed for lease=%s", lease.pk)
        return None


# --- Tasks and the calendar (SRS §3.12) ------------------------------------------------------

ACTIVITY_TRANSITIONS = {
    Activity.Status.OPEN: {
        Activity.Status.IN_PROGRESS, Activity.Status.COMPLETED, Activity.Status.CANCELLED,
    },
    Activity.Status.IN_PROGRESS: {
        Activity.Status.OPEN, Activity.Status.COMPLETED, Activity.Status.CANCELLED,
    },
    Activity.Status.COMPLETED: set(),
    Activity.Status.CANCELLED: set(),
}

#: Types a user may create directly. VIEWING and INSPECTION exist only as mirrors of their
#: domain records through the upsert path — §13's invariant.
USER_CREATABLE_TYPES = frozenset(set(Activity.ActivityType.values) - {"VIEWING", "INSPECTION"})


def _validate_rrule(rule, dtstart):
    from dateutil.rrule import rrulestr

    try:
        rrulestr(rule, dtstart=dtstart)
    except (ValueError, TypeError) as exc:
        raise ValidationError(
            {"recurrence_rule": f"Not a valid RFC 5545 RRULE: {exc}"}
        ) from exc


ACTIVITY_WRITABLE = frozenset(
    {"activity_type", "subject", "description", "assigned_to", "start_at", "due_at",
     "recurrence_rule", "reminder_minutes_before", "priority",
     "contact", "lead", "deal", "property", "lease"}
)


@transaction.atomic
def create_activity(*, actor, activity_type, subject, assigned_to=None, **fields):
    """A task, call, meeting or reminder (SRS 3.12.1/3.12.4)."""
    unknown = set(fields) - (ACTIVITY_WRITABLE - {"activity_type", "subject", "assigned_to"})
    if unknown:
        raise ValidationError(
            {name: "This field cannot be set through the activity service."
             for name in unknown}
        )
    if activity_type not in USER_CREATABLE_TYPES:
        raise ValidationError(
            {"activity_type": (
                "VIEWING and INSPECTION activities are created by their domain records "
                "(schedule the viewing/inspection instead)."
            )}
        )
    start_at, due_at = fields.get("start_at"), fields.get("due_at")
    if start_at and due_at and due_at < start_at:
        raise ValidationError({"due_at": "A task cannot be due before it starts."})
    if fields.get("recurrence_rule"):
        _validate_rrule(fields["recurrence_rule"], start_at or due_at or timezone.now())

    return Activity.objects.create(
        activity_type=activity_type,
        subject=subject,
        assigned_to=assigned_to or actor,
        created_by=actor,
        **fields,
    )


@transaction.atomic
def update_activity(activity, *, actor, **fields):
    if activity.source_type:
        raise ValidationError(
            {"detail": "Reschedule the viewing/inspection, not its calendar mirror."}
        )
    unknown = set(fields) - ACTIVITY_WRITABLE
    if unknown:
        raise ValidationError(
            {name: "This field cannot be set through the activity service."
             for name in unknown}
        )
    if fields.get("activity_type") and fields["activity_type"] not in USER_CREATABLE_TYPES:
        raise ValidationError({"activity_type": "Not a user-editable type."})
    if fields.get("recurrence_rule"):
        _validate_rrule(
            fields["recurrence_rule"],
            fields.get("start_at") or activity.start_at or timezone.now(),
        )
    for name, value in fields.items():
        setattr(activity, name, value)
    activity.save()
    return activity


@transaction.atomic
def change_activity_status(activity, new_status, *, actor):
    """Progress a task. Completing a recurring one materializes the next occurrence — no
    scheduler needed: the completion IS the tick (SRS 3.12.4)."""
    if new_status not in ACTIVITY_TRANSITIONS:
        raise ValidationError({"status": f"Unknown activity status {new_status!r}."})
    allowed = ACTIVITY_TRANSITIONS.get(activity.status, set())
    if new_status == activity.status:
        return activity, None
    if new_status not in allowed:
        raise ValidationError(
            {"status": f"Cannot move a task from {activity.status} to {new_status}."}
        )
    activity.status = new_status
    fields = ["status"]
    if new_status == Activity.Status.COMPLETED:
        activity.completed_at = timezone.now()
        fields.append("completed_at")
    activity.save(update_fields=fields)

    next_occurrence = None
    if (
        new_status == Activity.Status.COMPLETED
        and activity.recurrence_rule
        and activity.source_type is None
    ):
        next_occurrence = _materialize_next_occurrence(activity)
    return activity, next_occurrence


def _materialize_next_occurrence(activity):
    """Clone the task at its next RRULE date. A cancelled recurring task ends the chain —
    documented behaviour, not an accident."""
    from dateutil.rrule import rrulestr

    anchor = activity.start_at or activity.due_at
    if anchor is None:
        return None
    try:
        rule = rrulestr(activity.recurrence_rule, dtstart=anchor)
        next_start = rule.after(anchor)
    except (ValueError, TypeError):
        logger.exception("Bad stored RRULE on activity %s", activity.pk)
        return None
    if next_start is None:
        return None
    shift = next_start - anchor
    return Activity.objects.create(
        activity_type=activity.activity_type,
        subject=activity.subject,
        description=activity.description,
        assigned_to=activity.assigned_to,
        created_by=activity.created_by,
        start_at=(activity.start_at + shift) if activity.start_at else None,
        due_at=(activity.due_at + shift) if activity.due_at else None,
        recurrence_rule=activity.recurrence_rule,
        reminder_minutes_before=activity.reminder_minutes_before,
        priority=activity.priority,
        contact=activity.contact,
        lead=activity.lead,
        deal=activity.deal,
        property=activity.property,
        lease=activity.lease,
        status=Activity.Status.OPEN,
    )


def sweep_activity_reminders(now=None):
    """Send due task reminders (SRS 3.12.2). `reminder_sent_at` is the idempotency stamp —
    two overlapping sweeps send at most one reminder per task."""
    from datetime import timedelta

    from django.db.models import F
    from django.db.models.functions import Coalesce

    from .notify import notify

    now = now or timezone.now()
    due = (
        Activity.objects.filter(
            status__in=(Activity.Status.OPEN, Activity.Status.IN_PROGRESS),
            reminder_minutes_before__isnull=False,
            reminder_sent_at__isnull=True,
        )
        .annotate(anchor=Coalesce(F("start_at"), F("due_at")))
        .filter(anchor__isnull=False)
        .select_related("assigned_to")
    )
    sent = []
    for activity in due:
        fire_at = activity.anchor - timedelta(minutes=activity.reminder_minutes_before)
        if fire_at > now:
            continue
        notify(
            recipient=activity.assigned_to,
            type="TASK_DUE",
            title=f"Due: {activity.subject}",
            body=f"Scheduled for {activity.anchor:%Y-%m-%d %H:%M}.",
            entity_type="ACTIVITY",
            entity_id=activity.pk,
        )
        activity.reminder_sent_at = now
        activity.save(update_fields=["reminder_sent_at"])
        sent.append(activity.pk)
    return sent
