"""`crm` marketing models (architecture.md §6).

Lead sources, campaigns and their metrics, drip steps, landing pages, saved-search alerts.

Campaign steps, landing pages, and saved-search alerts are flagged Phase-2 in the spec: the
tables exist now so the schema is complete and FKs resolve, but nothing drives them until the
marketing services land.
"""
import uuid

from django.conf import settings
from django.db import models

from apps.core.models import BaseModel


class LeadSource(models.Model):
    """Where a lead came from. Referenced by contacts, leads, and landing pages."""

    class SourceType(models.TextChoices):
        WEBSITE = "WEBSITE", "Website"
        FACEBOOK = "FACEBOOK", "Facebook"
        INSTAGRAM = "INSTAGRAM", "Instagram"
        WHATSAPP = "WHATSAPP", "WhatsApp"
        PHONE = "PHONE", "Phone"
        WALK_IN = "WALK_IN", "Walk-in"
        REFERRAL = "REFERRAL", "Referral"
        PROPERTY_PORTAL = "PROPERTY_PORTAL", "Property portal"
        CAMPAIGN = "CAMPAIGN", "Campaign"
        MANUAL = "MANUAL", "Manual"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    name = models.CharField(max_length=100)
    source_type = models.CharField(max_length=20, choices=SourceType.choices)
    description = models.TextField(null=True, blank=True)
    is_active = models.BooleanField(default=True)

    class Meta:
        db_table = "crm_lead_source"

    def __str__(self):
        return self.name


class Campaign(models.Model):
    """Marketing campaign header.

    `campaign_type` and `status` are free-text in the spec (§6 lists no enum for either), so
    they stay CharFields here rather than inventing choices the spec did not sanction.
    """

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    name = models.CharField(max_length=200)
    campaign_type = models.CharField(max_length=50)
    description = models.TextField(null=True, blank=True)
    budget = models.DecimalField(max_digits=15, decimal_places=2, null=True, blank=True)
    start_date = models.DateField()
    end_date = models.DateField(null=True, blank=True)
    status = models.CharField(max_length=50)
    owner = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="owned_campaigns"
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "crm_campaign"

    def __str__(self):
        return self.name


class CampaignMetric(models.Model):
    """Daily rollup per campaign, feeding ad-spend ROI reporting (SRS 3.8.5)."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    campaign = models.ForeignKey(Campaign, on_delete=models.CASCADE, related_name="metrics")
    metric_date = models.DateField()
    impressions = models.IntegerField(default=0)
    clicks = models.IntegerField(default=0)
    leads_generated = models.IntegerField(default=0)
    conversions = models.IntegerField(default=0)
    cost = models.DecimalField(max_digits=15, decimal_places=2, default=0)
    revenue = models.DecimalField(max_digits=15, decimal_places=2, default=0)

    class Meta:
        db_table = "crm_campaign_metric"
        constraints = [
            # Not in the spec's constraint list, but a daily rollup is by definition one row
            # per campaign per day: without this, a re-run of the metrics job silently
            # double-counts spend and conversions.
            models.UniqueConstraint(
                fields=["campaign", "metric_date"], name="crm_campaign_metric_day_uniq"
            )
        ]


class CampaignStep(models.Model):
    """One step of a drip/nurture sequence (SRS 3.8.1). Phase-2 build."""

    class Channel(models.TextChoices):
        EMAIL = "EMAIL", "Email"
        SMS = "SMS", "SMS"
        WHATSAPP = "WHATSAPP", "WhatsApp"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    campaign = models.ForeignKey(Campaign, on_delete=models.CASCADE, related_name="steps")
    step_order = models.IntegerField()
    channel = models.CharField(max_length=20, choices=Channel.choices)
    delay_days = models.IntegerField(default=0)
    # Added by crm/0003 once collaboration exists — §6's deferred-FK note.
    template = models.ForeignKey(
        "collaboration.Template",
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="campaign_steps",
    )
    subject = models.CharField(max_length=200, null=True, blank=True)
    body = models.TextField(null=True, blank=True)
    is_active = models.BooleanField(default=True)

    class Meta:
        db_table = "crm_campaign_step"
        constraints = [
            models.UniqueConstraint(
                fields=["campaign", "step_order"], name="crm_campaign_step_order_uniq"
            )
        ]


class LandingPage(BaseModel):
    """Microsite / landing page with a lead-capture form (SRS 3.8.2). Phase-2 build."""

    campaign = models.ForeignKey(
        Campaign, null=True, blank=True, on_delete=models.SET_NULL, related_name="landing_pages"
    )
    slug = models.SlugField(max_length=200, unique=True)
    title = models.CharField(max_length=200)
    content = models.JSONField(default=dict, blank=True)
    form_config = models.JSONField(default=dict, blank=True)
    lead_source = models.ForeignKey(
        LeadSource, null=True, blank=True, on_delete=models.SET_NULL, related_name="landing_pages"
    )
    is_published = models.BooleanField(default=False)
    views = models.IntegerField(default=0)
    submissions = models.IntegerField(default=0)

    class Meta:
        db_table = "crm_landing_page"

    def __str__(self):
        return self.slug


class SavedSearchAlert(models.Model):
    """Notifies a contact when matching listings appear (SRS 3.8.4). Phase-2 build.

    `last_run_at` is what the scheduler advances; `criteria` is the saved query, intersected
    with property geo the same way lead location preferences are.
    """

    class Frequency(models.TextChoices):
        INSTANT = "INSTANT", "Instant"
        DAILY = "DAILY", "Daily"
        WEEKLY = "WEEKLY", "Weekly"

    class Channel(models.TextChoices):
        EMAIL = "EMAIL", "Email"
        SMS = "SMS", "SMS"
        WHATSAPP = "WHATSAPP", "WhatsApp"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    contact = models.ForeignKey(
        "contacts.Contact", on_delete=models.CASCADE, related_name="saved_search_alerts"
    )
    name = models.CharField(max_length=200)
    criteria = models.JSONField(default=dict, blank=True)
    frequency = models.CharField(max_length=20, choices=Frequency.choices)
    channel = models.CharField(max_length=20, choices=Channel.choices)
    is_active = models.BooleanField(default=True)
    last_run_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "crm_saved_search_alert"
