"""
Sales/leasing pipeline (plan §3, SRS §3.4).

Opportunity = a qualified deal with money attached. Multiple pipelines can run in
parallel (residential sales, commercial leasing, off-plan). Stage moves are logged
with a mandatory reason + next action.
"""
import builtins

from django.conf import settings
from django.db import models

from apps.core.models import CustomFieldsMixin, TenantAwareModel


class Pipeline(TenantAwareModel):
    class BusinessLine(models.TextChoices):
        SALES = "sales", "Sales"
        LEASING = "leasing", "Leasing"
        OFF_PLAN = "off_plan", "Off-Plan"

    name = models.CharField(max_length=128)
    business_line = models.CharField(
        max_length=16, choices=BusinessLine.choices, default=BusinessLine.SALES
    )
    is_default = models.BooleanField(default=False)

    def __str__(self):
        return self.name


class Stage(TenantAwareModel):
    pipeline = models.ForeignKey(
        Pipeline, on_delete=models.CASCADE, related_name="stages"
    )
    name = models.CharField(max_length=128)
    order = models.PositiveSmallIntegerField(default=0)
    probability = models.PositiveSmallIntegerField(default=0)  # 0-100
    is_won = models.BooleanField(default=False)
    is_lost = models.BooleanField(default=False)

    class Meta:
        ordering = ["order"]

    def __str__(self):
        return f"{self.pipeline.name}:{self.name}"


class Opportunity(CustomFieldsMixin, TenantAwareModel):
    class Status(models.TextChoices):
        OPEN = "open", "Open"
        WON = "won", "Won"
        LOST = "lost", "Lost"

    title = models.CharField(max_length=255)
    pipeline = models.ForeignKey(
        Pipeline, on_delete=models.PROTECT, related_name="opportunities"
    )
    stage = models.ForeignKey(
        Stage, on_delete=models.PROTECT, related_name="opportunities"
    )
    status = models.CharField(
        max_length=8, choices=Status.choices, default=Status.OPEN
    )

    contact = models.ForeignKey(
        "contacts.Contact", on_delete=models.PROTECT, related_name="opportunities"
    )
    property = models.ForeignKey(
        "properties.Property",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="opportunities",
    )

    value = models.DecimalField(max_digits=14, decimal_places=2, default=0)
    currency = models.CharField(max_length=3, default="USD")
    expected_close = models.DateField(null=True, blank=True)
    probability = models.PositiveSmallIntegerField(default=0)
    lost_reason = models.CharField(max_length=255, blank=True)

    assigned_agent = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="opportunities",
    )

    class Meta:
        verbose_name_plural = "opportunities"
        indexes = [models.Index(fields=["tenant", "pipeline", "stage"])]

    def __str__(self):
        return self.title

    # `property` (the FK below) shadows the builtin in this namespace.
    @builtins.property
    def weighted_value(self):
        return (self.value or 0) * (self.probability or 0) / 100


class OpportunityStageHistory(TenantAwareModel):
    """Mandatory reason + next action on every move (SRS §3.4.4)."""

    opportunity = models.ForeignKey(
        Opportunity, on_delete=models.CASCADE, related_name="stage_history"
    )
    from_stage = models.ForeignKey(
        Stage, on_delete=models.SET_NULL, null=True, related_name="+"
    )
    to_stage = models.ForeignKey(
        Stage, on_delete=models.SET_NULL, null=True, related_name="+"
    )
    changed_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, related_name="+"
    )
    reason = models.CharField(max_length=255)
    next_action = models.CharField(max_length=255, blank=True)
    at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-at"]
