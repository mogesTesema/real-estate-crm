"""`inventory` models (architecture.md §8).

Projects, buildings, properties, units, listings, media.

Two structural points the spec is emphatic about:

* **Units are a real table, not "another property row."** `Unit` exists so off-plan and
  multi-unit developments get addressable inventory with their own status, price, and lease.
  `property_ops.Lease.unit` and `property_ops.MaintenanceRequest.unit` point at `Unit`, never
  at `Property`. A single-unit property may have zero units and be leased at property level.
* **Every property has a Property Manager.** `Property.managed_by` is required (SRS 3.3.10),
  and `MANAGED_PROPERTIES` row scoping derives from it — leases, maintenance, and owner
  statements all inherit their visibility through this column.
"""
import uuid

from django.conf import settings
from django.contrib.gis.db import models as gis_models
from django.contrib.postgres.indexes import GinIndex
from django.db import models

from apps.core.models import BaseModel, SoftDeleteModel


class PropertyType(models.Model):
    """Typology lookup shared by properties and units."""

    class Category(models.TextChoices):
        RESIDENTIAL = "RESIDENTIAL", "Residential"
        COMMERCIAL = "COMMERCIAL", "Commercial"
        LAND = "LAND", "Land"
        INDUSTRIAL = "INDUSTRIAL", "Industrial"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    name = models.CharField(max_length=100)
    code = models.CharField(max_length=30, unique=True)
    category = models.CharField(max_length=20, choices=Category.choices)
    is_active = models.BooleanField(default=True)

    class Meta:
        db_table = "inventory_property_type"

    def __str__(self):
        return self.code


class Project(BaseModel):
    """A development. Groups buildings and off-plan inventory."""

    class Status(models.TextChoices):
        PLANNED = "PLANNED", "Planned"
        UNDER_CONSTRUCTION = "UNDER_CONSTRUCTION", "Under construction"
        COMPLETED = "COMPLETED", "Completed"
        ARCHIVED = "ARCHIVED", "Archived"

    name = models.CharField(max_length=200)
    developer = models.ForeignKey(
        "contacts.Contact",
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="developed_projects",
    )
    description = models.TextField(null=True, blank=True)
    location = models.CharField(max_length=200, null=True, blank=True)
    start_date = models.DateField(null=True, blank=True)
    expected_completion_date = models.DateField(null=True, blank=True)
    actual_completion_date = models.DateField(null=True, blank=True)
    status = models.CharField(max_length=20, choices=Status.choices)

    class Meta:
        db_table = "inventory_project"

    def __str__(self):
        return self.name


class Building(models.Model):
    """A building, optionally inside a project — standalone buildings are allowed."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    project = models.ForeignKey(
        Project, null=True, blank=True, on_delete=models.PROTECT, related_name="buildings"
    )
    name = models.CharField(max_length=200)
    code = models.CharField(max_length=50)
    number_of_floors = models.IntegerField(null=True, blank=True)
    description = models.TextField(null=True, blank=True)

    class Meta:
        db_table = "inventory_building"

    def __str__(self):
        return self.name


class Property(SoftDeleteModel):
    """The physical asset.

    `geo_point` is the searchable geography column (GiST-indexed) and drives radius /
    map-drawn / nearest search; `latitude`/`longitude` are kept alongside it as the
    human-readable pair. Keeping both is the spec's call (§8) — the decimals are what forms
    and imports carry, the geography is what queries use.
    """

    class Status(models.TextChoices):
        DRAFT = "DRAFT", "Draft"
        AVAILABLE = "AVAILABLE", "Available"
        OCCUPIED = "OCCUPIED", "Occupied"
        UNDER_MAINTENANCE = "UNDER_MAINTENANCE", "Under maintenance"
        SOLD = "SOLD", "Sold"
        ARCHIVED = "ARCHIVED", "Archived"

    property_type = models.ForeignKey(
        PropertyType, on_delete=models.PROTECT, related_name="properties"
    )
    project = models.ForeignKey(
        Project, null=True, blank=True, on_delete=models.PROTECT, related_name="properties"
    )
    building = models.ForeignKey(
        Building, null=True, blank=True, on_delete=models.PROTECT, related_name="properties"
    )
    is_multi_unit = models.BooleanField(default=False)
    title = models.CharField(max_length=200)
    description = models.TextField(null=True, blank=True)
    address_line_1 = models.CharField(max_length=200)
    address_line_2 = models.CharField(max_length=200, null=True, blank=True)
    city = models.CharField(max_length=100)
    state = models.CharField(max_length=100, null=True, blank=True)
    country = models.CharField(max_length=100)
    postal_code = models.CharField(max_length=20, null=True, blank=True)
    latitude = models.DecimalField(max_digits=9, decimal_places=6, null=True, blank=True)
    longitude = models.DecimalField(max_digits=9, decimal_places=6, null=True, blank=True)
    geo_point = gis_models.PointField(geography=True, srid=4326, null=True, blank=True)
    land_area = models.DecimalField(max_digits=12, decimal_places=2, null=True, blank=True)
    built_area = models.DecimalField(max_digits=12, decimal_places=2, null=True, blank=True)
    bedrooms = models.IntegerField(null=True, blank=True)
    bathrooms = models.IntegerField(null=True, blank=True)
    parking_spaces = models.IntegerField(null=True, blank=True)
    year_built = models.IntegerField(null=True, blank=True)
    floor_number = models.IntegerField(null=True, blank=True)
    total_floors = models.IntegerField(null=True, blank=True)
    amenities = models.JSONField(default=dict, blank=True)
    custom_data = models.JSONField(default=dict, blank=True)
    status = models.CharField(max_length=20, choices=Status.choices)
    # Required, not optional: SRS 3.3.10 mandates a Property Manager per property, and the
    # MANAGED_PROPERTIES data scope resolves through this column.
    managed_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="managed_properties"
    )

    class Meta:
        db_table = "inventory_property"
        indexes = [
            # GIN, not a plain Index: a btree over JSONB cannot serve the containment (@>)
            # and key-exists (?) operators that amenity and custom-field filters use, so a
            # models.Index here would build silently and never be chosen by the planner.
            GinIndex(fields=["amenities"], name="inventory_property_amen_gin"),
            GinIndex(fields=["custom_data"], name="inventory_property_cust_gin"),
        ]
        # No explicit index for geo_point: PointField defaults to spatial_index=True and
        # already emits the GiST index that serves radius / map-drawn / nearest search
        # (SRS 3.3.7, and the 1s-over-1M target in 5.1). Declaring one here as well just
        # builds the same index twice.

    def __str__(self):
        return self.title


class Unit(SoftDeleteModel):
    """An addressable unit inside a property (SRS 3.3.6).

    Leases and maintenance requests reference this table, not `Property`.
    """

    class Status(models.TextChoices):
        AVAILABLE = "AVAILABLE", "Available"
        RESERVED = "RESERVED", "Reserved"
        SOLD = "SOLD", "Sold"
        OCCUPIED = "OCCUPIED", "Occupied"
        UNDER_MAINTENANCE = "UNDER_MAINTENANCE", "Under maintenance"
        OFF_MARKET = "OFF_MARKET", "Off market"

    property = models.ForeignKey(Property, on_delete=models.PROTECT, related_name="units")
    building = models.ForeignKey(
        Building, null=True, blank=True, on_delete=models.PROTECT, related_name="units"
    )
    unit_number = models.CharField(max_length=50)
    floor_number = models.IntegerField(null=True, blank=True)
    unit_type = models.ForeignKey(
        PropertyType, null=True, blank=True, on_delete=models.PROTECT, related_name="units"
    )
    bedrooms = models.IntegerField(null=True, blank=True)
    bathrooms = models.IntegerField(null=True, blank=True)
    built_area = models.DecimalField(max_digits=12, decimal_places=2, null=True, blank=True)
    parking_spaces = models.IntegerField(null=True, blank=True)
    sale_price = models.DecimalField(max_digits=15, decimal_places=2, null=True, blank=True)
    rent_amount = models.DecimalField(max_digits=15, decimal_places=2, null=True, blank=True)
    amenities = models.JSONField(default=dict, blank=True)
    custom_data = models.JSONField(default=dict, blank=True)
    status = models.CharField(max_length=20, choices=Status.choices)

    class Meta:
        db_table = "inventory_unit"
        constraints = [
            # Partial, so retiring a unit frees its number for reuse (§2's soft-delete rule).
            models.UniqueConstraint(
                fields=["property", "unit_number"],
                condition=models.Q(deleted_at__isnull=True),
                name="inventory_unit_number_uniq",
            )
        ]

    def __str__(self):
        return f"{self.property_id}/{self.unit_number}"


class PropertyStatusHistory(models.Model):
    """Availability trail for a property *or* a unit (SRS 3.3.4)."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    property = models.ForeignKey(
        Property, null=True, blank=True, on_delete=models.CASCADE, related_name="status_history"
    )
    unit = models.ForeignKey(
        Unit, null=True, blank=True, on_delete=models.CASCADE, related_name="status_history"
    )
    from_status = models.CharField(max_length=20, null=True, blank=True)
    to_status = models.CharField(max_length=20)
    changed_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="+"
    )
    reason = models.TextField(null=True, blank=True)
    changed_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "inventory_property_status_history"
        constraints = [
            # "Exactly one of property_id/unit_id is set" — XOR, not merely "at least one".
            models.CheckConstraint(
                condition=(
                    models.Q(property__isnull=False, unit__isnull=True)
                    | models.Q(property__isnull=True, unit__isnull=False)
                ),
                name="inventory_status_history_target_xor",
            )
        ]


class PropertyOwner(models.Model):
    """Fractional / co-ownership over time (SRS 3.3.9).

    Percentages are validated per row; the spec does not require the shares on a property to
    sum to 100, since ownership periods overlap and change over time.
    """

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    property = models.ForeignKey(Property, on_delete=models.PROTECT, related_name="owners")
    contact = models.ForeignKey(
        "contacts.Contact", on_delete=models.PROTECT, related_name="owned_properties"
    )
    ownership_percentage = models.DecimalField(max_digits=5, decimal_places=2)
    is_primary_owner = models.BooleanField(default=False)
    start_date = models.DateField()
    end_date = models.DateField(null=True, blank=True)

    class Meta:
        db_table = "inventory_property_owner"
        constraints = [
            models.CheckConstraint(
                condition=models.Q(ownership_percentage__gt=0)
                & models.Q(ownership_percentage__lte=100),
                name="inventory_property_owner_pct_range",
            )
        ]


class Listing(SoftDeleteModel):
    """The marketed offer over a property or one of its units."""

    class ListingType(models.TextChoices):
        SALE = "SALE", "Sale"
        RENT = "RENT", "Rent"
        SALE_AND_RENT = "SALE_AND_RENT", "Sale and rent"

    class Status(models.TextChoices):
        DRAFT = "DRAFT", "Draft"
        ACTIVE = "ACTIVE", "Active"
        UNDER_OFFER = "UNDER_OFFER", "Under offer"
        RESERVED = "RESERVED", "Reserved"
        SOLD = "SOLD", "Sold"
        RENTED = "RENTED", "Rented"
        OFF_MARKET = "OFF_MARKET", "Off market"
        EXPIRED = "EXPIRED", "Expired"

    property = models.ForeignKey(Property, on_delete=models.PROTECT, related_name="listings")
    unit = models.ForeignKey(
        Unit, null=True, blank=True, on_delete=models.PROTECT, related_name="listings"
    )
    reference_code = models.CharField(max_length=50)
    listing_type = models.CharField(max_length=20, choices=ListingType.choices)
    title = models.CharField(max_length=200)
    description = models.TextField(null=True, blank=True)
    asking_price = models.DecimalField(max_digits=15, decimal_places=2, null=True, blank=True)
    rent_amount = models.DecimalField(max_digits=15, decimal_places=2, null=True, blank=True)
    security_deposit = models.DecimalField(max_digits=15, decimal_places=2, null=True, blank=True)
    commission_rate = models.DecimalField(max_digits=5, decimal_places=2, null=True, blank=True)
    available_from = models.DateField(null=True, blank=True)
    published_at = models.DateTimeField(null=True, blank=True)
    expires_at = models.DateTimeField(null=True, blank=True)
    status = models.CharField(max_length=20, choices=Status.choices)
    is_exclusive = models.BooleanField(default=False)
    assigned_agent = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="assigned_listings",
    )
    custom_data = models.JSONField(default=dict, blank=True)

    class Meta:
        db_table = "inventory_listing"
        constraints = [
            models.UniqueConstraint(
                fields=["reference_code"],
                condition=models.Q(deleted_at__isnull=True),
                name="inventory_listing_reference_uniq",
            )
        ]

    def __str__(self):
        return self.reference_code


class Media(SoftDeleteModel):
    """Gallery / presentation metadata for a property, unit, or listing.

    Inventory owns the presentation metadata; the binary lives in object storage. Until the
    collaboration app exists, `storage_key` carries the object reference on its own; the
    `file` FK to `collaboration.File` and the widened "file or storage_key" check arrive in
    inventory/0002. Listing carousels read this table — not `collaboration.DocumentLink`.
    """

    class MediaType(models.TextChoices):
        PHOTO = "PHOTO", "Photo"
        FLOOR_PLAN = "FLOOR_PLAN", "Floor plan"
        VIDEO = "VIDEO", "Video"
        DOCUMENT = "DOCUMENT", "Document"
        VIRTUAL_TOUR = "VIRTUAL_TOUR", "Virtual tour"

    property = models.ForeignKey(
        Property, null=True, blank=True, on_delete=models.CASCADE, related_name="media"
    )
    unit = models.ForeignKey(
        Unit, null=True, blank=True, on_delete=models.CASCADE, related_name="media"
    )
    listing = models.ForeignKey(
        Listing, null=True, blank=True, on_delete=models.CASCADE, related_name="media"
    )
    # Added by inventory/0002 once collaboration exists. Prefer `file` over `storage_key`
    # for new rows; storage_key remains for media captured before collaboration was built.
    file = models.ForeignKey(
        "collaboration.File",
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="inventory_media",
    )
    storage_key = models.CharField(max_length=500, null=True, blank=True)
    media_type = models.CharField(max_length=20, choices=MediaType.choices)
    caption = models.CharField(max_length=200, null=True, blank=True)
    sort_order = models.IntegerField(default=0)
    is_primary = models.BooleanField(default=False)

    class Meta:
        db_table = "inventory_media"
        constraints = [
            models.CheckConstraint(
                condition=(
                    models.Q(property__isnull=False)
                    | models.Q(unit__isnull=False)
                    | models.Q(listing__isnull=False)
                ),
                name="inventory_media_has_target",
            ),
            # The spec's full form, now that `file` exists: a media row must point at a
            # blob one way or the other.
            models.CheckConstraint(
                condition=models.Q(file__isnull=False) | models.Q(storage_key__isnull=False),
                name="inventory_media_has_blob_ref",
            ),
        ]
