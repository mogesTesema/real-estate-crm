"""
Contact model — the gravitational center of the CRM (plan §3).

One Contact holds a *set* of roles (buyer/seller/tenant/landlord/investor/vendor)
so a person can be a past seller AND a current buyer simultaneously (SRS §3.2.2).
NOT separate Buyer/Seller/... tables.
"""
from django.conf import settings
from django.db import models

from apps.core.models import CustomFieldsMixin, TenantAwareModel


def normalize_phone(value: str | None) -> str:
    return "".join(ch for ch in (value or "") if ch.isdigit())


def normalize_email(value: str | None) -> str:
    return (value or "").strip().lower()


class Contact(CustomFieldsMixin, TenantAwareModel):
    class Kind(models.TextChoices):
        PERSON = "person", "Person"
        COMPANY = "company", "Company / Organization"

    kind = models.CharField(max_length=16, choices=Kind.choices, default=Kind.PERSON)
    full_name = models.CharField(max_length=255)
    company_name = models.CharField(max_length=255, blank=True)

    # Normalized primaries power fast dedupe/search; extras hold the rest.
    email = models.EmailField(blank=True, db_index=True)
    phone = models.CharField(max_length=32, blank=True, db_index=True)
    extra_emails = models.JSONField(default=list, blank=True)
    extra_phones = models.JSONField(default=list, blank=True)

    source = models.CharField(max_length=64, blank=True)
    tags = models.JSONField(default=list, blank=True)
    # Segmentation (SRS §3.2.3)
    location_preference = models.CharField(max_length=255, blank=True)
    budget_min = models.DecimalField(
        max_digits=14, decimal_places=2, null=True, blank=True
    )
    budget_max = models.DecimalField(
        max_digits=14, decimal_places=2, null=True, blank=True
    )
    is_vip = models.BooleanField(default=False)

    assigned_agent = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="contacts",
    )

    # Referral source linkage (SRS §3.2.8)
    referred_by = models.ForeignKey(
        "self",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="referrals",
    )

    class Meta:
        indexes = [
            models.Index(fields=["tenant", "email"]),
            models.Index(fields=["tenant", "phone"]),
        ]

    def save(self, *args, **kwargs):
        self.email = normalize_email(self.email)
        self.phone = normalize_phone(self.phone) or self.phone
        super().save(*args, **kwargs)

    def __str__(self):
        return self.full_name or self.company_name or str(self.id)

    @property
    def roles(self):
        return list(self.contact_roles.filter(is_active=True).values_list("role", flat=True))


class ContactRole(TenantAwareModel):
    class RoleType(models.TextChoices):
        BUYER = "buyer", "Buyer"
        SELLER = "seller", "Seller"
        TENANT = "tenant", "Tenant"
        LANDLORD = "landlord", "Landlord"
        INVESTOR = "investor", "Investor"
        VENDOR = "vendor", "Vendor"

    contact = models.ForeignKey(
        Contact, on_delete=models.CASCADE, related_name="contact_roles"
    )
    role = models.CharField(max_length=16, choices=RoleType.choices)
    is_active = models.BooleanField(default=True)

    class Meta:
        unique_together = ("contact", "role")

    def __str__(self):
        return f"{self.contact_id}:{self.role}"
