"""`crm` lead models (architecture.md §7).

Leads and inquiries, their assignment and status trails, multi-location preferences, and the
routing rules that assign them.

Two service-layer rules recorded here because the schema cannot enforce them:

* **De-duplication at capture** matches phone/email/name against contacts, *and additionally*
  flags a duplicate when the same identifiers target the same `target_property` with the same
  `lead_type` (SRS 3.1.3/3.1.10/3.1.11).
* **Routing rules are evaluated only inside `crm.services`** (`ingest_lead` / `assign_lead`) —
  never from a view or serializer. First matching active rule by ascending `priority` wins.
"""
import uuid

from django.conf import settings
from django.db import models

from apps.core.models import SoftDeleteModel

from .marketing import Campaign, LeadSource


class Lead(SoftDeleteModel):
    """An inquiry, before it becomes a deal.

    State machine: NEW -> CONTACTED -> QUALIFIED -> NURTURING -> CONVERTED | LOST.
    Transitions are recorded in `LeadStatusHistory` and driven by `crm.services`.
    """

    class LeadType(models.TextChoices):
        BUY = "BUY", "Buy"
        SELL = "SELL", "Sell"
        RENT_IN = "RENT_IN", "Rent in"
        RENT_OUT = "RENT_OUT", "Rent out"
        INVEST = "INVEST", "Invest"

    class Status(models.TextChoices):
        NEW = "NEW", "New"
        CONTACTED = "CONTACTED", "Contacted"
        QUALIFIED = "QUALIFIED", "Qualified"
        NURTURING = "NURTURING", "Nurturing"
        CONVERTED = "CONVERTED", "Converted"
        LOST = "LOST", "Lost"

    class Priority(models.TextChoices):
        LOW = "LOW", "Low"
        MEDIUM = "MEDIUM", "Medium"
        HIGH = "HIGH", "High"
        URGENT = "URGENT", "Urgent"

    contact = models.ForeignKey(
        "contacts.Contact", on_delete=models.PROTECT, related_name="leads"
    )
    lead_type = models.CharField(max_length=20, choices=LeadType.choices)
    status = models.CharField(max_length=20, choices=Status.choices, default=Status.NEW)
    source = models.ForeignKey(
        LeadSource, null=True, blank=True, on_delete=models.SET_NULL, related_name="leads"
    )
    campaign = models.ForeignKey(
        Campaign, null=True, blank=True, on_delete=models.SET_NULL, related_name="leads"
    )
    # A lead is assignable to an individual or to a team (round-robin within it).
    assigned_agent = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="assigned_leads",
    )
    assigned_team = models.ForeignKey(
        "identity.Team",
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="assigned_leads",
    )
    priority = models.CharField(
        max_length=10, choices=Priority.choices, default=Priority.MEDIUM
    )
    score = models.IntegerField(default=0)
    title = models.CharField(max_length=200)
    description = models.TextField(null=True, blank=True)
    budget_min = models.DecimalField(max_digits=15, decimal_places=2, null=True, blank=True)
    budget_max = models.DecimalField(max_digits=15, decimal_places=2, null=True, blank=True)
    preferred_property_type = models.CharField(max_length=50, null=True, blank=True)
    target_property = models.ForeignKey(
        "inventory.Property",
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="targeting_leads",
    )
    # Deprecated by the spec in favour of LeadLocationPreference, kept so existing single-value
    # captures and imports still have somewhere to land. Do not read it for matching.
    preferred_location = models.CharField(max_length=200, null=True, blank=True)
    preferred_bedrooms = models.IntegerField(null=True, blank=True)
    preferred_bathrooms = models.IntegerField(null=True, blank=True)
    financing_status = models.CharField(max_length=50, null=True, blank=True)
    expected_timeframe = models.CharField(max_length=50, null=True, blank=True)
    last_contacted_at = models.DateTimeField(null=True, blank=True)
    next_follow_up_at = models.DateTimeField(null=True, blank=True)
    converted_at = models.DateTimeField(null=True, blank=True)
    lost_reason = models.TextField(null=True, blank=True)
    custom_data = models.JSONField(default=dict, blank=True)

    class Meta:
        db_table = "crm_lead"

    def __str__(self):
        return self.title


class LeadLocationPreference(models.Model):
    """One of the areas a lead is targeting (SRS 3.1.7).

    Matching (SRS 3.3.8) intersects these with `inventory_property.geo_point`.
    """

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    lead = models.ForeignKey(Lead, on_delete=models.CASCADE, related_name="location_preferences")
    location = models.CharField(max_length=200)
    latitude = models.DecimalField(max_digits=9, decimal_places=6, null=True, blank=True)
    longitude = models.DecimalField(max_digits=9, decimal_places=6, null=True, blank=True)
    radius_km = models.DecimalField(max_digits=6, decimal_places=2, null=True, blank=True)

    class Meta:
        db_table = "crm_lead_location_preference"
        constraints = [
            models.UniqueConstraint(
                fields=["lead", "location"], name="crm_lead_location_uniq"
            )
        ]


class LeadAssignment(models.Model):
    """Reassignment trail — who handed the lead to whom, and why."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    lead = models.ForeignKey(Lead, on_delete=models.CASCADE, related_name="assignments")
    from_user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="+",
    )
    to_user = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="+"
    )
    assigned_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="+"
    )
    reason = models.TextField(null=True, blank=True)
    assigned_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "crm_lead_assignment"


class LeadStatusHistory(models.Model):
    """Status transition trail. `from_status` is null for the row recording creation."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    lead = models.ForeignKey(Lead, on_delete=models.CASCADE, related_name="status_history")
    from_status = models.CharField(max_length=20, null=True, blank=True)
    to_status = models.CharField(max_length=20)
    changed_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="+"
    )
    reason = models.TextField(null=True, blank=True)
    changed_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "crm_lead_status_history"


class LeadRoutingRule(models.Model):
    """Automated lead assignment (SRS 3.1.5).

    `criteria` matches inbound lead attributes — source, campaign, lead_type, location, score
    bands. The first active rule by ascending `priority` wins.
    """

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    name = models.CharField(max_length=200)
    priority = models.IntegerField()
    is_active = models.BooleanField(default=True)
    criteria = models.JSONField(default=dict, blank=True)
    assign_to_user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.CASCADE,
        related_name="routing_rules",
    )
    assign_to_team = models.ForeignKey(
        "identity.Team",
        null=True,
        blank=True,
        on_delete=models.CASCADE,
        related_name="routing_rules",
    )
    round_robin = models.BooleanField(default=False)
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="+",
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "crm_lead_routing_rule"
        constraints = [
            # "Exactly one of assign_to_user_id / assign_to_team_id should be set" — a rule
            # naming both has no defined meaning, and one naming neither can never assign.
            models.CheckConstraint(
                condition=(
                    models.Q(assign_to_user__isnull=False, assign_to_team__isnull=True)
                    | models.Q(assign_to_user__isnull=True, assign_to_team__isnull=False)
                ),
                name="crm_routing_rule_target_xor",
            )
        ]

    def __str__(self):
        return self.name
