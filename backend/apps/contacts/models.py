"""`contacts` models (architecture.md §5).

Buyers, sellers, rental tenants, landlords, vendors, consent, relationships.

One design point worth restating from the spec: a contact carries a *set* of roles in a child
table rather than a `role` column, so the same person can be a buyer today and a past seller
at the same time (SRS 3.2.2). Do not collapse `ContactRole` back into an enum field.
"""
import uuid

from django.conf import settings
from django.db import models

from apps.core.models import SoftDeleteModel


class Contact(SoftDeleteModel):
    """A party the company deals with — person or company.

    Deduplication (spec §5 note) matches on normalized `email`, E.164-normalized `phone`, and
    `national_id`, with pg_trgm similarity over name/address for fuzzy suggestions. The
    normalization and the merge that repoints child FKs are `contacts.services` concerns; the
    exact-match keys are indexed here because that lookup runs on every lead capture.
    """

    class ContactType(models.TextChoices):
        PERSON = "PERSON", "Person"
        COMPANY = "COMPANY", "Company"

    contact_type = models.CharField(max_length=10, choices=ContactType.choices)
    first_name = models.CharField(max_length=150, null=True, blank=True)
    middle_name = models.CharField(max_length=150, null=True, blank=True)
    last_name = models.CharField(max_length=150, null=True, blank=True)
    company_name = models.CharField(max_length=200, null=True, blank=True)
    email = models.EmailField(null=True, blank=True, db_index=True)
    secondary_email = models.EmailField(null=True, blank=True)
    phone = models.CharField(max_length=30, null=True, blank=True, db_index=True)
    secondary_phone = models.CharField(max_length=30, null=True, blank=True)
    date_of_birth = models.DateField(null=True, blank=True)
    national_id = models.CharField(max_length=100, null=True, blank=True, db_index=True)
    tax_number = models.CharField(max_length=100, null=True, blank=True)
    preferred_language = models.CharField(max_length=20, null=True, blank=True)
    preferred_contact_method = models.CharField(max_length=20, null=True, blank=True)
    address_line_1 = models.CharField(max_length=200, null=True, blank=True)
    address_line_2 = models.CharField(max_length=200, null=True, blank=True)
    city = models.CharField(max_length=100, null=True, blank=True)
    state = models.CharField(max_length=100, null=True, blank=True)
    country = models.CharField(max_length=100, null=True, blank=True)
    postal_code = models.CharField(max_length=20, null=True, blank=True)
    # Plain decimals, not PostGIS: §2 puts the geography column on inventory_property, where
    # radius/map search actually runs. A contact's coordinates are descriptive only.
    latitude = models.DecimalField(max_digits=9, decimal_places=6, null=True, blank=True)
    longitude = models.DecimalField(max_digits=9, decimal_places=6, null=True, blank=True)
    assigned_agent = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="assigned_contacts",
    )
    # Added by contacts/0002 rather than 0001: crm is built after contacts, so the target did
    # not exist when this table was created.
    default_source = models.ForeignKey(
        "crm.LeadSource",
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="default_for_contacts",
    )
    notes = models.TextField(null=True, blank=True)
    custom_data = models.JSONField(default=dict, blank=True)
    is_active = models.BooleanField(default=True)

    class Meta:
        db_table = "contacts_contact"

    def __str__(self):
        if self.contact_type == self.ContactType.COMPANY:
            return self.company_name or str(self.id)
        return " ".join(filter(None, [self.first_name, self.last_name])) or str(self.id)


class ContactRole(models.Model):
    """One of the hats a contact wears. A contact may hold several at once."""

    class Role(models.TextChoices):
        BUYER = "BUYER", "Buyer"
        SELLER = "SELLER", "Seller"
        TENANT = "TENANT", "Tenant"
        LANDLORD = "LANDLORD", "Landlord"
        INVESTOR = "INVESTOR", "Investor"
        VENDOR = "VENDOR", "Vendor"
        DEVELOPER = "DEVELOPER", "Developer"
        REFERRAL_PARTNER = "REFERRAL_PARTNER", "Referral partner"
        LEGAL_REPRESENTATIVE = "LEGAL_REPRESENTATIVE", "Legal representative"
        OTHER = "OTHER", "Other"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    contact = models.ForeignKey(Contact, on_delete=models.CASCADE, related_name="roles")
    role = models.CharField(max_length=25, choices=Role.choices)

    class Meta:
        db_table = "contacts_contact_role"
        constraints = [
            models.UniqueConstraint(
                fields=["contact", "role"], name="contacts_contact_role_uniq"
            )
        ]

    def __str__(self):
        return f"{self.contact_id}:{self.role}"


class ContactRelationship(models.Model):
    """Directed contact-to-contact edge — household, company rep, referral source (SRS 3.2.5)."""

    class RelationshipType(models.TextChoices):
        SPOUSE = "SPOUSE", "Spouse"
        FAMILY_MEMBER = "FAMILY_MEMBER", "Family member"
        COMPANY_REPRESENTATIVE = "COMPANY_REPRESENTATIVE", "Company representative"
        LEGAL_REPRESENTATIVE = "LEGAL_REPRESENTATIVE", "Legal representative"
        REFERRAL_SOURCE = "REFERRAL_SOURCE", "Referral source"
        PROPERTY_OWNER = "PROPERTY_OWNER", "Property owner"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    from_contact = models.ForeignKey(
        Contact, on_delete=models.CASCADE, related_name="relationships_from"
    )
    to_contact = models.ForeignKey(
        Contact, on_delete=models.CASCADE, related_name="relationships_to"
    )
    relationship_type = models.CharField(max_length=30, choices=RelationshipType.choices)
    notes = models.TextField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "contacts_contact_relationship"


class Consent(models.Model):
    """Per-channel marketing/communication consent with an evidence trail (SRS 5.5, 3.9).

    Not soft-deleted and not edited in place conceptually: withdrawing consent sets
    `withdrawn_at` and flips `status`, leaving the original grant visible.
    """

    class Channel(models.TextChoices):
        EMAIL = "EMAIL", "Email"
        SMS = "SMS", "SMS"
        WHATSAPP = "WHATSAPP", "WhatsApp"
        PHONE = "PHONE", "Phone"
        MARKETING = "MARKETING", "Marketing"

    class Status(models.TextChoices):
        OPTED_IN = "OPTED_IN", "Opted in"
        OPTED_OUT = "OPTED_OUT", "Opted out"
        PENDING = "PENDING", "Pending"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    contact = models.ForeignKey(Contact, on_delete=models.CASCADE, related_name="consents")
    channel = models.CharField(max_length=20, choices=Channel.choices)
    status = models.CharField(max_length=20, choices=Status.choices)
    source = models.CharField(max_length=100)
    consented_at = models.DateTimeField(null=True, blank=True)
    withdrawn_at = models.DateTimeField(null=True, blank=True)
    evidence = models.TextField(null=True, blank=True)

    class Meta:
        db_table = "contacts_consent"

    def __str__(self):
        return f"{self.contact_id}:{self.channel}={self.status}"
