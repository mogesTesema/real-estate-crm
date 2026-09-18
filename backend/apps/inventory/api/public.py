"""Public, unauthenticated listing search (SRS 3.19.4 / the public website's feed).

The row filter IS the published set: `status=ACTIVE`, nothing else — deliberately not
`apply_scope`, because there is no user to scope for and ACTIVE is the marketing decision.
The serializer is a strict whitelist: no agents, no commission, no custom_data, no exact
address, coordinates rounded to ~100 m.
"""
from rest_framework import serializers
from rest_framework.generics import ListAPIView, RetrieveAPIView
from rest_framework.permissions import AllowAny
from rest_framework.throttling import ScopedRateThrottle

from ..models import Listing


def _public_listings():
    return (
        Listing.objects.filter(status=Listing.Status.ACTIVE, deleted_at__isnull=True)
        .select_related("property", "property__property_type")
        .order_by("-published_at", "-created_at")
    )


class PublicListingSerializer(serializers.Serializer):
    """Whitelist, not `exclude` — a new model field must opt IN to being public."""

    id = serializers.UUIDField(read_only=True)
    title = serializers.CharField(read_only=True)
    description = serializers.CharField(read_only=True, allow_null=True)
    listing_type = serializers.CharField(read_only=True)
    asking_price = serializers.DecimalField(
        max_digits=15, decimal_places=2, read_only=True, allow_null=True
    )
    rent_amount = serializers.DecimalField(
        max_digits=15, decimal_places=2, read_only=True, allow_null=True
    )
    available_from = serializers.DateField(read_only=True, allow_null=True)
    published_at = serializers.DateTimeField(read_only=True, allow_null=True)
    city = serializers.CharField(source="property.city", read_only=True)
    country = serializers.CharField(source="property.country", read_only=True)
    property_type = serializers.CharField(
        source="property.property_type.name", read_only=True
    )
    bedrooms = serializers.IntegerField(
        source="property.bedrooms", read_only=True, allow_null=True
    )
    bathrooms = serializers.IntegerField(
        source="property.bathrooms", read_only=True, allow_null=True
    )
    built_area = serializers.DecimalField(
        source="property.built_area", max_digits=12, decimal_places=2,
        read_only=True, allow_null=True,
    )
    amenities = serializers.JSONField(source="property.amenities", read_only=True)
    latitude = serializers.SerializerMethodField()
    longitude = serializers.SerializerMethodField()

    def get_latitude(self, obj) -> float | None:
        # ~100 m of blur: enough for a map pin, not enough for a door knock.
        return round(float(obj.property.latitude), 3) if obj.property.latitude else None

    def get_longitude(self, obj) -> float | None:
        return round(float(obj.property.longitude), 3) if obj.property.longitude else None


class PublicListingBase:
    authentication_classes = []
    permission_classes = [AllowAny]
    throttle_classes = [ScopedRateThrottle]
    throttle_scope = "public_listings"
    serializer_class = PublicListingSerializer


class PublicListingListView(PublicListingBase, ListAPIView):
    def get_queryset(self):
        params = self.request.query_params
        queryset = _public_listings()
        if params.get("listing_type"):
            queryset = queryset.filter(listing_type=params["listing_type"])
        if params.get("city"):
            queryset = queryset.filter(property__city__icontains=params["city"])
        if params.get("bedrooms"):
            try:
                queryset = queryset.filter(property__bedrooms__gte=int(params["bedrooms"]))
            except ValueError:
                pass
        for bound, lookup_sale, lookup_rent in (
            ("min_price", "asking_price__gte", "rent_amount__gte"),
            ("max_price", "asking_price__lte", "rent_amount__lte"),
        ):
            raw = params.get(bound)
            if raw:
                from django.db.models import Q

                try:
                    value = float(raw)
                except ValueError:
                    continue
                queryset = queryset.filter(
                    Q(**{lookup_sale: value}) | Q(**{lookup_rent: value})
                )
        if params.get("q"):
            from django.db.models import Q

            text = params["q"]
            queryset = queryset.filter(
                Q(title__icontains=text)
                | Q(description__icontains=text)
                | Q(property__city__icontains=text)
            )
        return queryset


class PublicListingDetailView(PublicListingBase, RetrieveAPIView):
    def get_queryset(self):
        return _public_listings()
