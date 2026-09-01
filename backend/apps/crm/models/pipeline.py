"""`crm` pipeline models (architecture.md §9).

Pipelines and their ordered stages. Deals live in `deals.py`; these are the configuration the
Kanban board renders from.
"""
import uuid

from django.db import models


class Pipeline(models.Model):
    """A named deal pipeline, one per business line."""

    class PipelineType(models.TextChoices):
        RESIDENTIAL_SALES = "RESIDENTIAL_SALES", "Residential sales"
        COMMERCIAL_SALES = "COMMERCIAL_SALES", "Commercial sales"
        LEASING = "LEASING", "Leasing"
        OFF_PLAN = "OFF_PLAN", "Off-plan"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    name = models.CharField(max_length=200)
    pipeline_type = models.CharField(max_length=30, choices=PipelineType.choices)
    is_default = models.BooleanField(default=False)
    is_active = models.BooleanField(default=True)

    class Meta:
        db_table = "crm_pipeline"

    def __str__(self):
        return self.name


class PipelineStage(models.Model):
    """An ordered stage with win/loss semantics.

    `is_won` / `is_lost` mark the terminal stages — the weighted-forecast and
    conversion-rate reports key off these rather than matching stage names.
    `required_fields` gates advancement: the service layer refuses to move a deal into a
    stage until the listed fields are populated (SRS 3.4.x).
    """

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    pipeline = models.ForeignKey(Pipeline, on_delete=models.CASCADE, related_name="stages")
    name = models.CharField(max_length=100)
    code = models.CharField(max_length=50)
    sort_order = models.IntegerField()
    probability = models.DecimalField(max_digits=5, decimal_places=2)
    is_won = models.BooleanField(default=False)
    is_lost = models.BooleanField(default=False)
    required_fields = models.JSONField(default=list, blank=True)

    class Meta:
        db_table = "crm_pipeline_stage"
        ordering = ["pipeline", "sort_order"]
        constraints = [
            # A stage cannot be both the win and the loss terminus.
            models.CheckConstraint(
                condition=~models.Q(is_won=True, is_lost=True),
                name="crm_pipeline_stage_not_both_terminal",
            ),
            models.UniqueConstraint(
                fields=["pipeline", "sort_order"], name="crm_pipeline_stage_order_uniq"
            ),
        ]

    def __str__(self):
        return f"{self.pipeline_id}:{self.code}"
