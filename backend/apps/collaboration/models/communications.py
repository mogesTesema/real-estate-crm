"""`collaboration` communication models (architecture.md §17).

Templates, omni-channel threads, logged messages, call logs, internal notes.

The SRS calls "data loss from manual entry" a primary problem the system exists to solve —
these tables are where every call, message, and note lands so a contact's history is complete
rather than scattered across inboxes.
"""
import uuid

from django.conf import settings
from django.db import models


class Template(models.Model):
    """A reusable email/SMS/WhatsApp template (SRS 3.9.2).

    Referenced by `Message` and by `crm.CampaignStep` drip steps — the crm FK is the one
    §6 flags as deferred until this table exists.
    """

    class Channel(models.TextChoices):
        EMAIL = "EMAIL", "Email"
        SMS = "SMS", "SMS"
        WHATSAPP = "WHATSAPP", "WhatsApp"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    name = models.CharField(max_length=200)
    channel = models.CharField(max_length=20, choices=Channel.choices)
    subject = models.CharField(max_length=255, null=True, blank=True)
    body = models.TextField()
    variables = models.JSONField(default=dict, blank=True)
    is_active = models.BooleanField(default=True)
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="+"
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "collaboration_template"

    def __str__(self):
        return self.name


class Thread(models.Model):
    """An omni-channel conversation, grouped against a CRM record.

    `channel` may be MIXED — a conversation that started as email and continued over WhatsApp
    is still one thread. `last_message_at` is the inbox ordering key.
    """

    class Channel(models.TextChoices):
        EMAIL = "EMAIL", "Email"
        SMS = "SMS", "SMS"
        WHATSAPP = "WHATSAPP", "WhatsApp"
        CALL = "CALL", "Call"
        MIXED = "MIXED", "Mixed"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    subject = models.CharField(max_length=255, null=True, blank=True)
    channel = models.CharField(max_length=20, choices=Channel.choices)
    contact = models.ForeignKey(
        "contacts.Contact",
        null=True,
        blank=True,
        on_delete=models.CASCADE,
        related_name="threads",
    )
    lead = models.ForeignKey(
        "crm.Lead", null=True, blank=True, on_delete=models.CASCADE, related_name="threads"
    )
    deal = models.ForeignKey(
        "crm.Deal", null=True, blank=True, on_delete=models.CASCADE, related_name="threads"
    )
    property = models.ForeignKey(
        "inventory.Property",
        null=True,
        blank=True,
        on_delete=models.CASCADE,
        related_name="threads",
    )
    assigned_to = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="assigned_threads",
    )
    last_message_at = models.DateTimeField(null=True, blank=True)
    is_closed = models.BooleanField(default=False)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "collaboration_thread"


class Message(models.Model):
    """A single logged message, inbound or outbound (SRS 3.9.1).

    Conceptually immutable: what was sent is what was sent. Status advances
    (QUEUED -> SENT -> DELIVERED -> READ, or FAILED) but the body does not change.
    `provider_message_id` is what delivery callbacks and de-duplication key on.
    Attachments link through `DocumentLink.message`.
    """

    class Channel(models.TextChoices):
        EMAIL = "EMAIL", "Email"
        SMS = "SMS", "SMS"
        WHATSAPP = "WHATSAPP", "WhatsApp"

    class Direction(models.TextChoices):
        INBOUND = "INBOUND", "Inbound"
        OUTBOUND = "OUTBOUND", "Outbound"

    class Status(models.TextChoices):
        QUEUED = "QUEUED", "Queued"
        SENT = "SENT", "Sent"
        DELIVERED = "DELIVERED", "Delivered"
        READ = "READ", "Read"
        FAILED = "FAILED", "Failed"
        RECEIVED = "RECEIVED", "Received"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    thread = models.ForeignKey(Thread, on_delete=models.CASCADE, related_name="messages")
    channel = models.CharField(max_length=20, choices=Channel.choices)
    direction = models.CharField(max_length=20, choices=Direction.choices)
    from_address = models.CharField(max_length=255, null=True, blank=True)
    to_address = models.CharField(max_length=255, null=True, blank=True)
    sender_user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="sent_messages",
    )
    contact = models.ForeignKey(
        "contacts.Contact",
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="messages",
    )
    template = models.ForeignKey(
        Template, null=True, blank=True, on_delete=models.SET_NULL, related_name="messages"
    )
    subject = models.CharField(max_length=255, null=True, blank=True)
    body = models.TextField()
    status = models.CharField(max_length=20, choices=Status.choices)
    provider_message_id = models.CharField(max_length=255, null=True, blank=True)
    sent_at = models.DateTimeField(null=True, blank=True)
    delivered_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "collaboration_message"
        constraints = [
            # Provider ids are the de-duplication key for inbound webhooks, which retry.
            models.UniqueConstraint(
                fields=["channel", "provider_message_id"],
                condition=models.Q(provider_message_id__isnull=False),
                name="collaboration_message_provider_uniq",
            )
        ]


class CallLog(models.Model):
    """Call activity, including click-to-call outcomes (SRS 3.9.1)."""

    class Direction(models.TextChoices):
        INBOUND = "INBOUND", "Inbound"
        OUTBOUND = "OUTBOUND", "Outbound"
        MISSED = "MISSED", "Missed"

    class Outcome(models.TextChoices):
        CONNECTED = "CONNECTED", "Connected"
        NO_ANSWER = "NO_ANSWER", "No answer"
        VOICEMAIL = "VOICEMAIL", "Voicemail"
        BUSY = "BUSY", "Busy"
        FAILED = "FAILED", "Failed"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    thread = models.ForeignKey(
        Thread, null=True, blank=True, on_delete=models.CASCADE, related_name="call_logs"
    )
    contact = models.ForeignKey(
        "contacts.Contact",
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="call_logs",
    )
    lead = models.ForeignKey(
        "crm.Lead", null=True, blank=True, on_delete=models.SET_NULL, related_name="call_logs"
    )
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="call_logs"
    )
    direction = models.CharField(max_length=20, choices=Direction.choices)
    phone_number = models.CharField(max_length=30)
    started_at = models.DateTimeField()
    duration_seconds = models.IntegerField(null=True, blank=True)
    outcome = models.CharField(max_length=20, choices=Outcome.choices)
    recording_file = models.ForeignKey(
        "collaboration.File",
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="call_recordings",
    )
    notes = models.TextField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "collaboration_call_log"


class InternalNote(models.Model):
    """Internal team note with @mentions on any record (SRS 3.9.6).

    Internal-only: unlike `Message`, this is never sent to an external party. `mentions`
    drives MENTION notifications.
    """

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    author = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="internal_notes"
    )
    body = models.TextField()
    mentions = models.JSONField(default=list, blank=True)
    contact = models.ForeignKey(
        "contacts.Contact",
        null=True,
        blank=True,
        on_delete=models.CASCADE,
        related_name="internal_notes",
    )
    lead = models.ForeignKey(
        "crm.Lead",
        null=True,
        blank=True,
        on_delete=models.CASCADE,
        related_name="internal_notes",
    )
    deal = models.ForeignKey(
        "crm.Deal",
        null=True,
        blank=True,
        on_delete=models.CASCADE,
        related_name="internal_notes",
    )
    property = models.ForeignKey(
        "inventory.Property",
        null=True,
        blank=True,
        on_delete=models.CASCADE,
        related_name="internal_notes",
    )
    lease = models.ForeignKey(
        "property_ops.Lease",
        null=True,
        blank=True,
        on_delete=models.CASCADE,
        related_name="internal_notes",
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "collaboration_internal_note"
        constraints = [
            models.CheckConstraint(
                condition=(
                    models.Q(contact__isnull=False)
                    | models.Q(lead__isnull=False)
                    | models.Q(deal__isnull=False)
                    | models.Q(property__isnull=False)
                    | models.Q(lease__isnull=False)
                ),
                name="collaboration_internal_note_has_target",
            )
        ]
