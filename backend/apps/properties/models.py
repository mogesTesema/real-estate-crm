"""
Property vs Listing (plan §3).

Property  = the physical asset (durable facts: address, geo, size, beds/baths).
Listing   = a marketing instance of a property (price, status, agent, mandate).
Splitting them makes relisting, price history, and "who sold this unit" trivial.
"""
from django.conf import settings
from django.db import models

from apps.core.models import CustomFieldsMixin, TenantAwareModel


class Property(CustomFieldsMixin, TenantAwareModel):
    class Category(models.TextChoices):
        RESIDENTIAL = "residential", "Residential"
        COMMERCIAL = "commercial", "Commercial"
        INDUSTRIAL = "industrial", "Industrial"
        LAND = "land", "Land"

    class NodeType(models.TextChoices):
        PROPERTY = "property", "Property / Unit"
        PROJECT = "project", "Project / Development"
        BUILDING = "building", "Building / Phase"

    node_type = models.CharField(
        max_length=16, choices=NodeType.choices, default=NodeType.PROPERTY
    )
    # Self-referential tree for Project -> Building/Phase -> Unit (SRS §3.3.6).
    parent = models.ForeignKey(
        "self", on_delete=models.CASCADE, null=True, blank=True, related_name="children"
    )

    category = models.CharField(max_length=16, choices=Category.choices)
    title = models.CharField(max_length=255)
    description = models.TextField(blank=True)

    address_line = models.CharField(max_length=255, blank=True)
    city = models.CharField(max_length=128, blank=True, db_index=True)
    region = models.CharField(max_length=128, blank=True)
    country = models.CharField(max_length=64, blank=True)
    postal_code = models.CharField(max_length=32, blank=True)
    latitude = models.DecimalField(
        max_digits=9, decimal_places=6, null=True, blank=True
    )
    longitude = models.DecimalField(
        max_digits=9, decimal_places=6, null=True, blank=True
    )

    bedrooms = models.PositiveSmallIntegerField(null=True, blank=True)
    bathrooms = models.PositiveSmallIntegerField(null=True, blank=True)
    area_built = models.DecimalField(
        max_digits=10, decimal_places=2, null=True, blank=True
    )
    area_carpet = models.DecimalField(
        max_digits=10, decimal_places=2, null=True, blank=True
    )
    year_built = models.PositiveSmallIntegerField(null=True, blank=True)
    floor = models.CharField(max_length=16, blank=True)
    unit_number = models.CharField(max_length=32, blank=True)
    parking = models.PositiveSmallIntegerField(null=True, blank=True)
    amenities = models.JSONField(default=list, blank=True)

    owner = models.ForeignKey(
        "contacts.Contact",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="owned_properties",
    )

    class Meta:
        verbose_name_plural = "properties"
        indexes = [models.Index(fields=["tenant", "city"])]

    def __str__(self):
        return self.title


class ListingStatus(models.TextChoices):
    DRAFT = "draft", "Draft"
    ACTIVE = "active", "Active"
    UNDER_OFFER = "under_offer", "Under Offer"
    RESERVED = "reserved", "Reserved"
    SOLD = "sold", "Sold"
    RENTED = "rented", "Rented"
    OFF_MARKET = "off_market", "Off-Market"
    EXPIRED = "expired", "Expired"


class Listing(CustomFieldsMixin, TenantAwareModel):
    class ListingType(models.TextChoices):
        SALE = "sale", "For Sale"
        RENT = "rent", "For Rent"

    class Mandate(models.TextChoices):
        EXCLUSIVE = "exclusive", "Exclusive"
        OPEN = "open", "Open / Shared"

    property = models.ForeignKey(
        Property, on_delete=models.CASCADE, related_name="listings"
    )
    listing_type = models.CharField(max_length=8, choices=ListingType.choices)
    status = models.CharField(
        max_length=16, choices=ListingStatus.choices, default=ListingStatus.DRAFT
    )
    mandate = models.CharField(
        max_length=16, choices=Mandate.choices, default=Mandate.EXCLUSIVE
    )
    price = models.DecimalField(max_digits=14, decimal_places=2)
    currency = models.CharField(max_length=3, default="USD")

    listing_agent = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="listings",
    )
    listed_at = models.DateTimeField(null=True, blank=True)
    expires_at = models.DateTimeField(null=True, blank=True)

    def __str__(self):
        return f"{self.property.title} ({self.get_status_display()})"


class ListingStatusHistory(TenantAwareModel):
    """Immutable trail of status changes (SRS §3.3.4)."""

    listing = models.ForeignKey(
        Listing, on_delete=models.CASCADE, related_name="status_history"
    )
    from_status = models.CharField(max_length=16, blank=True)
    to_status = models.CharField(max_length=16)
    changed_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, related_name="+"
    )
    reason = models.CharField(max_length=255, blank=True)
    at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-at"]


class ListingMedia(TenantAwareModel):
    class Kind(models.TextChoices):
        PHOTO = "photo", "Photo"
        FLOORPLAN = "floorplan", "Floor Plan"
        BROCHURE = "brochure", "Brochure / PDF"
        TOUR = "tour", "Virtual Tour URL"

    listing = models.ForeignKey(
        Listing, on_delete=models.CASCADE, related_name="media"
    )
    kind = models.CharField(max_length=16, choices=Kind.choices, default=Kind.PHOTO)
    file = models.FileField(upload_to="listings/%Y/%m/", null=True, blank=True)
    url = models.URLField(blank=True)  # for embedded tours
    caption = models.CharField(max_length=255, blank=True)
