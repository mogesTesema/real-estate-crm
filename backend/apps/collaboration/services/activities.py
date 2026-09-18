"""Activities and the unified calendar (architecture.md §13, SRS 3.12).

`upsert_activity_for_source` is the required orchestration §1.2 lists: a domain record that
appears on the calendar — a viewing, an inspection — owns exactly one activity, and §13
forbids a standalone VIEWING activity. Domain services call this; nothing creates a
source-backed activity any other way.
"""
import logging

from django.core.exceptions import ValidationError
from django.db import transaction

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
