"""`collaboration` notification models (architecture.md §18).

In-app feed, per-user channel preferences with quiet hours, and a per-channel dispatch log.

The dispatch log records **suppression** as an outcome (SUPPRESSED_QUIET_HOURS,
SUPPRESSED_PREFERENCE) rather than silently dropping the send. "Why didn't I get notified?"
is answerable only if the decision not to notify was written down.
"""
import uuid

from django.conf import settings
from django.db import models


class Notification(models.Model):
    """One item in a user's in-app feed (SRS 3.18.1)."""

    class Type(models.TextChoices):
        LEAD_ASSIGNED = "LEAD_ASSIGNED", "Lead assigned"
        TASK_DUE = "TASK_DUE", "Task due"
        VIEWING_SCHEDULED = "VIEWING_SCHEDULED", "Viewing scheduled"
        PAYMENT_RECEIVED = "PAYMENT_RECEIVED", "Payment received"
        COMMISSION_APPROVED = "COMMISSION_APPROVED", "Commission approved"
        LEASE_EXPIRING = "LEASE_EXPIRING", "Lease expiring"
        MAINTENANCE_UPDATE = "MAINTENANCE_UPDATE", "Maintenance update"
        MENTION = "MENTION", "Mention"
        SYSTEM = "SYSTEM", "System"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    recipient = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="notifications"
    )
    type = models.CharField(max_length=30, choices=Type.choices)
    title = models.CharField(max_length=255)
    body = models.TextField(null=True, blank=True)
    entity_type = models.CharField(max_length=30, null=True, blank=True)
    entity_id = models.UUIDField(null=True, blank=True)
    is_read = models.BooleanField(default=False)
    read_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "collaboration_notification"
        indexes = [
            # The feed query: this user's unread notifications, newest first.
            models.Index(
                fields=["recipient", "is_read", "-created_at"],
                name="collab_notif_feed_idx",
            )
        ]

    def __str__(self):
        return self.title


class NotificationPreference(models.Model):
    """Per-user, per-type channel preferences and quiet hours (SRS 3.18.2).

    `timezone` is per-user because quiet hours are meaningless without one — 22:00 means a
    different instant for an agent in Dubai than one in London.
    """

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="notification_preferences",
    )
    notification_type = models.CharField(max_length=30, choices=Notification.Type.choices)
    in_app_enabled = models.BooleanField(default=True)
    email_enabled = models.BooleanField(default=True)
    sms_enabled = models.BooleanField(default=False)
    push_enabled = models.BooleanField(default=True)
    quiet_hours_start = models.TimeField(null=True, blank=True)
    quiet_hours_end = models.TimeField(null=True, blank=True)
    timezone = models.CharField(max_length=64, null=True, blank=True)

    class Meta:
        db_table = "collaboration_notification_preference"
        constraints = [
            models.UniqueConstraint(
                fields=["user", "notification_type"],
                name="collaboration_notification_pref_uniq",
            ),
            # Quiet hours are a pair. One end without the other defines no window, and the
            # dispatcher would have to guess.
            models.CheckConstraint(
                condition=(
                    models.Q(quiet_hours_start__isnull=True, quiet_hours_end__isnull=True)
                    | models.Q(quiet_hours_start__isnull=False, quiet_hours_end__isnull=False)
                ),
                name="collaboration_notification_quiet_hours_paired",
            ),
        ]


class NotificationDispatchLog(models.Model):
    """Per-channel delivery audit, including deliberate suppression.

    `notification` is nullable because some sends are channel-only and never produce an
    in-app feed item.
    """

    class Channel(models.TextChoices):
        IN_APP = "IN_APP", "In-app"
        EMAIL = "EMAIL", "Email"
        SMS = "SMS", "SMS"
        PUSH = "PUSH", "Push"

    class Status(models.TextChoices):
        QUEUED = "QUEUED", "Queued"
        SENT = "SENT", "Sent"
        DELIVERED = "DELIVERED", "Delivered"
        FAILED = "FAILED", "Failed"
        SUPPRESSED_QUIET_HOURS = "SUPPRESSED_QUIET_HOURS", "Suppressed (quiet hours)"
        SUPPRESSED_PREFERENCE = "SUPPRESSED_PREFERENCE", "Suppressed (preference)"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    notification = models.ForeignKey(
        Notification,
        null=True,
        blank=True,
        on_delete=models.CASCADE,
        related_name="dispatch_logs",
    )
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="dispatch_logs"
    )
    channel = models.CharField(max_length=20, choices=Channel.choices)
    status = models.CharField(max_length=30, choices=Status.choices)
    provider_reference = models.CharField(max_length=255, null=True, blank=True)
    error_detail = models.TextField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "collaboration_notification_dispatch_log"
