"""
Unified activity timeline (SRS §3.2.4, plan §3).

One polymorphic table for every interaction — call, email, sms, note, meeting,
viewing, stage change — attachable to a Contact, Property, or Opportunity via a
generic relation. The single timeline is the feature agents judge the product on.
"""
from django.conf import settings
from django.contrib.contenttypes.fields import GenericForeignKey
from django.contrib.contenttypes.models import ContentType
from django.db import models

from apps.core.models import TenantAwareModel


class Activity(TenantAwareModel):
    class Type(models.TextChoices):
        CALL = "call", "Call"
        EMAIL = "email", "Email"
        SMS = "sms", "SMS"
        WHATSAPP = "whatsapp", "WhatsApp"
        NOTE = "note", "Note"
        MEETING = "meeting", "Meeting"
        VIEWING = "viewing", "Property Viewing"
        STAGE_CHANGE = "stage_change", "Stage Change"
        TASK = "task", "Task"

    activity_type = models.CharField(max_length=16, choices=Type.choices)
    body = models.TextField(blank=True)
    actor = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="activities",
    )

    # Generic link to the related record (Contact / Property / Opportunity / ...).
    content_type = models.ForeignKey(
        ContentType, on_delete=models.CASCADE, null=True, related_name="+"
    )
    object_id = models.CharField(max_length=64, null=True, blank=True)
    related = GenericForeignKey("content_type", "object_id")

    # Task/scheduling fields (SRS §3.12)
    due_at = models.DateTimeField(null=True, blank=True)
    completed_at = models.DateTimeField(null=True, blank=True)

    at = models.DateTimeField(auto_now_add=True)

    class Meta:
        verbose_name_plural = "activities"
        ordering = ["-at"]
        indexes = [models.Index(fields=["content_type", "object_id"])]

    def __str__(self):
        return f"{self.activity_type} @ {self.at:%Y-%m-%d}"
