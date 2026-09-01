"""Base model classes shared across every app (architecture.md §2).

Not every model in this project uses ``BaseModel``: only subclass it when the
table's spec field list carries all four audit columns (created_at, updated_at,
created_by, updated_by). Many reference/junction/config tables have leaner,
bespoke field lists and should declare their fields directly instead.
"""
import uuid

from django.conf import settings
from django.db import models

from .choices import ScopedEntityType


class BaseModel(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="+",
    )
    updated_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="+",
    )

    class Meta:
        abstract = True


class SoftDeleteModel(BaseModel):
    deleted_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        abstract = True


class AppendOnlyModel(models.Model):
    """For rows that are truly never updated (spec: "immutable/append-only
    records... carry only creation metadata"). No updated_at/updated_by —
    carrying them here would misleadingly imply mutability.

    Used by exactly the 4 models that also get a DB-role REVOKE UPDATE/DELETE
    immutability migration (see apps/core/db_policy.py):
    finance.AccountEntry, platform.AuditEvent, collaboration.SignatureEvent,
    crm.AgentLocationPoint.
    """

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    created_at = models.DateTimeField(auto_now_add=True)
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="+",
    )

    class Meta:
        abstract = True


class CustomField(models.Model):
    """Registry of admin-defined custom fields/picklists (architecture.md §2,
    SRS 2.5 / 3.17.4). Entities carry a ``custom_data`` JSONField validated
    against this registry — enforcement is a future services-layer concern,
    this pass only creates the registry table.
    """

    class DataType(models.TextChoices):
        TEXT = "TEXT", "Text"
        NUMBER = "NUMBER", "Number"
        DATE = "DATE", "Date"
        BOOLEAN = "BOOLEAN", "Boolean"
        SINGLE_SELECT = "SINGLE_SELECT", "Single select"
        MULTI_SELECT = "MULTI_SELECT", "Multi select"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    entity_type = models.CharField(max_length=20, choices=ScopedEntityType.choices)
    key = models.CharField(max_length=100)
    label = models.CharField(max_length=200)
    data_type = models.CharField(max_length=20, choices=DataType.choices)
    choices = models.JSONField(null=True, blank=True)
    is_required = models.BooleanField(default=False)
    is_active = models.BooleanField(default=True)
    sort_order = models.IntegerField(default=0)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "core_custom_field"
        constraints = [
            models.UniqueConstraint(
                fields=["entity_type", "key"], name="core_custom_field_entity_key_uniq"
            )
        ]

    def __str__(self):
        return f"{self.entity_type}.{self.key}"


class Sequence(models.Model):
    """Concurrency-safe human-readable reference-code counter (architecture.md
    §2). The ``SELECT ... FOR UPDATE`` increment logic is a services-layer
    concern — this pass only creates the table.
    """

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    key = models.CharField(max_length=50, unique=True)
    prefix = models.CharField(max_length=20, null=True, blank=True)
    current_value = models.BigIntegerField(default=0)
    padding = models.IntegerField(default=6)

    class Meta:
        db_table = "core_sequence"

    def __str__(self):
        return self.key
