"""Serializers for the inventory API.

Write serializers validate shape only; every mutation is delegated to `inventory.services`,
which is where the status state machine and the co-listing rules live so they hold from a
shell and an import too.
"""
from drf_spectacular.utils import extend_schema_field
from rest_framework import serializers

from apps.core.choices import ScopedEntityType
from apps.core.serializers import UserSummarySerializer
from apps.identity.field_access import FieldPermissionSerializerMixin

from ..models import (
    Building,
    Listing,
    Media,
    Project,
    Property,
    PropertyOwner,
    PropertyStatusHistory,
    PropertyType,
    Unit,
)


class PropertyTypeSerializer(serializers.ModelSerializer):
    class Meta:
        model = PropertyType
        fields = ("id", "name", "code", "category")


class ProjectSerializer(serializers.ModelSerializer):
    class Meta:
        model = Project
        fields = "__all__"
        read_only_fields = ("id", "created_at", "updated_at", "created_by", "updated_by")


class BuildingSerializer(serializers.ModelSerializer):
    class Meta:
        model = Building
        fields = "__all__"
        read_only_fields = ("id", "created_at", "updated_at", "created_by", "updated_by")


class MediaSerializer(serializers.ModelSerializer):
    class Meta:
        model = Media
        fields = (
            "id",
            "property",
            "unit",
            "listing",
            "file",
            "storage_key",
            "media_type",
            "caption",
            "sort_order",
            "is_primary",
            "created_at",
        )
        read_only_fields = ("id", "sort_order", "is_primary", "created_at")


class MediaOrderSerializer(serializers.Serializer):
    order = serializers.ListField(child=serializers.UUIDField())


class StatusHistorySerializer(serializers.ModelSerializer):
    changed_by = UserSummarySerializer(read_only=True)

    class Meta:
        model = PropertyStatusHistory
        fields = ("id", "from_status", "to_status", "changed_by", "reason", "changed_at")


class StatusChangeSerializer(serializers.Serializer):
    """A status move, with the reason SRS 3.3.4 wants on the history row."""

    status = serializers.CharField()
    reason = serializers.CharField(required=False, allow_blank=True)


class PropertyOwnerSerializer(serializers.ModelSerializer):
    contact_name = serializers.CharField(source="contact.__str__", read_only=True)

    class Meta:
        model = PropertyOwner
        fields = (
            "id",
            "contact",
            "contact_name",
            "ownership_percentage",
            "is_primary_owner",
            "start_date",
            "end_date",
            "commission_rate",
            "mandate_type",
            "mandate_expires_at",
        )
        read_only_fields = ("id",)


class SetOwnersSerializer(serializers.Serializer):
    owners = PropertyOwnerSerializer(many=True)


class UnitSerializer(serializers.ModelSerializer):
    class Meta:
        model = Unit
        fields = (
            "id",
            "property",
            "building",
            "unit_number",
            "floor_number",
            "unit_type",
            "bedrooms",
            "bathrooms",
            "built_area",
            "parking_spaces",
            "sale_price",
            "rent_amount",
            "amenities",
            "custom_data",
            "status",
            "created_at",
            "updated_at",
        )
        read_only_fields = ("id", "status", "created_at", "updated_at")


class PropertySerializer(FieldPermissionSerializerMixin, serializers.ModelSerializer):
    field_permission_entity = ScopedEntityType.PROPERTY

    managed_by = UserSummarySerializer(read_only=True)
    owners = PropertyOwnerSerializer(many=True, read_only=True)
    primary_media = serializers.SerializerMethodField()
    distance_km = serializers.SerializerMethodField()

    class Meta:
        model = Property
        exclude = ("geo_point", "deleted_at")
        read_only_fields = ("id", "status", "created_at", "updated_at")

    @extend_schema_field(MediaSerializer(allow_null=True))
    def get_primary_media(self, obj):
        # Reads the prefetched set rather than querying, so a 25-row page stays at one query
        # for the gallery instead of twenty-five.
        #
        # The `deleted_at` check is in Python on purpose. The viewset prefetches a filtered
        # queryset, but a serializer is also used on a bare instance — from another view, a
        # management command, a test — where `obj.media.all()` is unfiltered and a
        # soft-deleted row would be served as the cover image. Calling `.filter()` here would
        # be correct and would silently discard the prefetch on every row of every page.
        cover = next(
            (m for m in obj.media.all() if m.is_primary and m.deleted_at is None), None
        )
        return MediaSerializer(cover).data if cover else None

    @extend_schema_field(serializers.FloatField(allow_null=True))
    def get_distance_km(self, obj):
        """Present only on a geographic search.

        The old serializer computed a distance and then never exposed it, so a map view had
        no way to sort or label by nearest — the work was done and thrown away.
        """
        distance = getattr(obj, "distance", None)
        return round(distance.km, 3) if distance is not None else None


class PropertyWriteSerializer(FieldPermissionSerializerMixin, serializers.ModelSerializer):
    field_permission_entity = ScopedEntityType.PROPERTY

    class Meta:
        model = Property
        fields = (
            "property_type",
            "project",
            "building",
            "is_multi_unit",
            "title",
            "description",
            "address_line_1",
            "address_line_2",
            "city",
            "state",
            "country",
            "postal_code",
            "latitude",
            "longitude",
            "land_area",
            "built_area",
            "bedrooms",
            "bathrooms",
            "parking_spaces",
            "year_built",
            "floor_number",
            "total_floors",
            "amenities",
            "custom_data",
            "managed_by",
        )
        extra_kwargs = {
            # SRS 3.3.10 makes this mandatory, and MANAGED_PROPERTIES resolves through it.
            "managed_by": {"required": True, "allow_null": False},
        }


class ListingSerializer(FieldPermissionSerializerMixin, serializers.ModelSerializer):
    field_permission_entity = ScopedEntityType.LISTING

    assigned_agent = UserSummarySerializer(read_only=True)
    co_listing_agent = UserSummarySerializer(read_only=True)
    property_title = serializers.CharField(source="property.title", read_only=True)

    class Meta:
        model = Listing
        exclude = ("deleted_at",)
        read_only_fields = ("id", "reference_code", "status", "published_at", "created_at")


class ListingWriteSerializer(FieldPermissionSerializerMixin, serializers.ModelSerializer):
    field_permission_entity = ScopedEntityType.LISTING

    class Meta:
        model = Listing
        fields = (
            "property",
            "unit",
            "listing_type",
            "title",
            "description",
            "asking_price",
            "rent_amount",
            "security_deposit",
            "commission_rate",
            "available_from",
            "expires_at",
            "is_exclusive",
            "assigned_agent",
            "co_listing_agent",
            "custom_data",
        )


class PropertySearchSerializer(serializers.Serializer):
    """Query parameters for `GET /properties/search/` (SRS 3.3.7)."""

    text = serializers.CharField(required=False, allow_blank=True)
    latitude = serializers.FloatField(required=False)
    longitude = serializers.FloatField(required=False)
    radius_km = serializers.FloatField(required=False)
    min_price = serializers.DecimalField(max_digits=15, decimal_places=2, required=False)
    max_price = serializers.DecimalField(max_digits=15, decimal_places=2, required=False)
    bedrooms = serializers.IntegerField(required=False)
    bathrooms = serializers.IntegerField(required=False)
    amenities = serializers.CharField(
        required=False, help_text="Comma-separated amenity keys that must all be present."
    )

    def validate(self, attrs):
        lat, lon = attrs.get("latitude"), attrs.get("longitude")
        if (lat is None) != (lon is None):
            raise serializers.ValidationError(
                "latitude and longitude must be given together."
            )
        if attrs.get("radius_km") and lat is None:
            raise serializers.ValidationError(
                {"radius_km": "A radius needs a latitude and longitude to measure from."}
            )
        return attrs
