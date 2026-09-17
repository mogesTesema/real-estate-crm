"""`collaboration` activity models (architecture.md §13).

The unified calendar/task record, and its mapping to external calendars.

**Invariant (§13, and repeated in §9 and §10):** specialised domain records — `crm.Viewing`
and `property_ops.Inspection` — own their domain data (feedback, GPS check-in, meter
readings) and MUST upsert a linked `Activity` through
`collaboration.services.upsert_activity_for_source`. UI and APIs must never create a
standalone Activity with `activity_type` VIEWING or INSPECTION. Two rows describing the same
appointment that can drift apart is exactly what the upsert path exists to prevent.
"""
import uuid

from django.conf import settings
from django.db import models


class Activity(models.Model):
    """A task, call, meeting, or reminder on the unified calendar.

    `recurrence_rule` is an RFC 5545 RRULE string (SRS 3.12.4). `source_type`/`source_id` are
    an untyped pointer back to the domain record that owns this activity, when there is one.
    """

    class ActivityType(models.TextChoices):
        TASK = "TASK", "Task"
        CALL = "CALL", "Call"
        MEETING = "MEETING", "Meeting"
        VIEWING = "VIEWING", "Viewing"
        FOLLOW_UP = "FOLLOW_UP", "Follow up"
        INSPECTION = "INSPECTION", "Inspection"
        REMINDER = "REMINDER", "Reminder"

    class SourceType(models.TextChoices):
        VIEWING = "VIEWING", "Viewing"
        INSPECTION = "INSPECTION", "Inspection"

    class Priority(models.TextChoices):
        LOW = "LOW", "Low"
        MEDIUM = "MEDIUM", "Medium"
        HIGH = "HIGH", "High"
        URGENT = "URGENT", "Urgent"

    class Status(models.TextChoices):
        OPEN = "OPEN", "Open"
        IN_PROGRESS = "IN_PROGRESS", "In progress"
        COMPLETED = "COMPLETED", "Completed"
        CANCELLED = "CANCELLED", "Cancelled"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    activity_type = models.CharField(max_length=20, choices=ActivityType.choices)
    subject = models.CharField(max_length=255)
    description = models.TextField(null=True, blank=True)
    assigned_to = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="activities"
    )
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="+"
    )
    start_at = models.DateTimeField(null=True, blank=True)
    due_at = models.DateTimeField(null=True, blank=True)
    completed_at = models.DateTimeField(null=True, blank=True)
    recurrence_rule = models.CharField(max_length=255, null=True, blank=True)
    reminder_minutes_before = models.IntegerField(null=True, blank=True)
    reminder_sent_at = models.DateTimeField(null=True, blank=True)
    priority = models.CharField(
        max_length=10, choices=Priority.choices, default=Priority.MEDIUM
    )
    status = models.CharField(max_length=20, choices=Status.choices, default=Status.OPEN)
    source_type = models.CharField(
        max_length=20, choices=SourceType.choices, null=True, blank=True
    )
    source_id = models.UUIDField(null=True, blank=True)
    contact = models.ForeignKey(
        "contacts.Contact",
        null=True,
        blank=True,
        on_delete=models.CASCADE,
        related_name="activities",
    )
    lead = models.ForeignKey(
        "crm.Lead", null=True, blank=True, on_delete=models.CASCADE, related_name="activities"
    )
    deal = models.ForeignKey(
        "crm.Deal", null=True, blank=True, on_delete=models.CASCADE, related_name="activities"
    )
    property = models.ForeignKey(
        "inventory.Property",
        null=True,
        blank=True,
        on_delete=models.CASCADE,
        related_name="activities",
    )
    lease = models.ForeignKey(
        "property_ops.Lease",
        null=True,
        blank=True,
        on_delete=models.CASCADE,
        related_name="activities",
    )

    class Meta:
        db_table = "collaboration_activity"
        constraints = [
            # source_type and source_id are two halves of one pointer; one without the other
            # is a dangling reference the upsert path can never match on.
            models.CheckConstraint(
                condition=(
                    models.Q(source_type__isnull=True, source_id__isnull=True)
                    | models.Q(source_type__isnull=False, source_id__isnull=False)
                ),
                name="collaboration_activity_source_paired",
            ),
            # One activity per domain source. This is what makes
            # upsert_activity_for_source an upsert rather than a race that can insert
            # duplicate calendar rows for the same viewing.
            models.UniqueConstraint(
                fields=["source_type", "source_id"],
                condition=models.Q(source_type__isnull=False),
                name="collaboration_activity_source_uniq",
            ),
        ]

    def __str__(self):
        return self.subject


class CalendarLink(models.Model):
    """Maps an activity to an external Google/Outlook/iCal event for two-way sync."""

    class Provider(models.TextChoices):
        GOOGLE = "GOOGLE", "Google"
        OUTLOOK = "OUTLOOK", "Outlook"
        ICAL = "ICAL", "iCal"

    class SyncDirection(models.TextChoices):
        INBOUND = "INBOUND", "Inbound"
        OUTBOUND = "OUTBOUND", "Outbound"
        BIDIRECTIONAL = "BIDIRECTIONAL", "Bidirectional"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="calendar_links"
    )
    provider = models.CharField(max_length=20, choices=Provider.choices)
    external_calendar_id = models.CharField(max_length=255, null=True, blank=True)
    activity = models.ForeignKey(
        Activity,
        null=True,
        blank=True,
        on_delete=models.CASCADE,
        related_name="calendar_links",
    )
    external_event_id = models.CharField(max_length=255, null=True, blank=True)
    sync_direction = models.CharField(max_length=20, choices=SyncDirection.choices)
    last_synced_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "collaboration_calendar_link"
        constraints = [
            # Partial: external_event_id is nullable (a calendar can be linked before any
            # event is mapped), and Postgres would otherwise treat every NULL as distinct
            # anyway — being explicit documents the intent.
            models.UniqueConstraint(
                fields=["provider", "external_event_id"],
                condition=models.Q(external_event_id__isnull=False),
                name="collaboration_calendar_event_uniq",
            )
        ]
