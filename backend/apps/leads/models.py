"""
Lead & Inquiry (plan §3, SRS §3.1).

Lead is deliberately distinct from Contact and Opportunity: it is the raw inbound
signal + stated criteria, which may be junk or a duplicate. On qualification it
converts to an Opportunity, carrying its criteria across.
"""
from django.conf import settings
from django.db import models

from apps.core.models import CustomFieldsMixin, TenantAwareModel


class Lead(CustomFieldsMixin, TenantAwareModel):
    class Type(models.TextChoices):
        BUY = "buy", "Buy"
        SELL = "sell", "Sell"
        RENT_OUT = "rent_out", "Rent Out"
        RENT_IN = "rent_in", "Rent In"
        INVESTMENT = "investment", "Investment"

    class Status(models.TextChoices):
        NEW = "new", "New"
        CONTACTED = "contacted", "Contacted"
        QUALIFIED = "qualified", "Qualified"
        CONVERTED = "converted", "Converted"
        LOST = "lost", "Lost"

    # Raw captured identity (used for dedupe before a Contact is linked).
    name = models.CharField(max_length=255, blank=True)
    email = models.EmailField(blank=True)
    phone = models.CharField(max_length=32, blank=True)

    contact = models.ForeignKey(
        "contacts.Contact",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="leads",
    )

    lead_type = models.CharField(max_length=16, choices=Type.choices)
    source = models.CharField(max_length=64, blank=True)
    status = models.CharField(
        max_length=16, choices=Status.choices, default=Status.NEW
    )
    score = models.PositiveSmallIntegerField(default=0)

    # Stated criteria (SRS §3.1.7)
    budget_min = models.DecimalField(
        max_digits=14, decimal_places=2, null=True, blank=True
    )
    budget_max = models.DecimalField(
        max_digits=14, decimal_places=2, null=True, blank=True
    )
    preferred_location = models.CharField(max_length=255, blank=True)
    bedrooms = models.PositiveSmallIntegerField(null=True, blank=True)
    timeline = models.CharField(max_length=64, blank=True)
    notes = models.TextField(blank=True)

    assigned_agent = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="leads",
    )

    # SLA tracking (SRS §3.1.9)
    sla_due_at = models.DateTimeField(null=True, blank=True)
    sla_breached = models.BooleanField(default=False)
    first_response_at = models.DateTimeField(null=True, blank=True)
    acknowledged = models.BooleanField(default=False)

    converted_opportunity = models.ForeignKey(
        "deals.Opportunity",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="source_lead",
    )

    is_possible_duplicate = models.BooleanField(default=False)

    class Meta:
        indexes = [
            models.Index(fields=["tenant", "status"]),
            models.Index(fields=["tenant", "assigned_agent"]),
        ]

    def __str__(self):
        return f"{self.name or self.email or self.phone} ({self.lead_type})"
