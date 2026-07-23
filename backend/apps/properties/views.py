from rest_framework import status as http_status
from rest_framework.decorators import action
from rest_framework.parsers import FormParser, MultiPartParser
from rest_framework.response import Response

from apps.core.mixins import BaseTenantViewSet

from .models import Listing, Property
from .serializers import (
    ListingMediaSerializer,
    ListingSerializer,
    PropertySerializer,
    TransitionSerializer,
)
from .services import IllegalTransition, change_listing_status


class PropertyViewSet(BaseTenantViewSet):
    queryset = Property.objects.all().order_by("-created_at")
    serializer_class = PropertySerializer
    search_fields = ["title", "address_line", "city", "unit_number"]
    filterset_fields = ["category", "node_type", "city", "bedrooms", "parent"]
    # Shared inventory: no per-agent ownership scoping (tenant isolation still applies).
    scope_owner_field = None


class ListingViewSet(BaseTenantViewSet):
    queryset = Listing.objects.all().order_by("-created_at")
    serializer_class = ListingSerializer
    search_fields = ["property__title", "property__city"]
    filterset_fields = ["listing_type", "status", "mandate", "listing_agent", "property"]
    scope_owner_field = "listing_agent"
    scope_branch_field = "listing_agent__branch"

    @action(detail=True, methods=["post"])
    def transition(self, request, pk=None):
        """Move the listing to a new status through the state machine."""
        listing = self.get_object()
        ser = TransitionSerializer(data=request.data)
        ser.is_valid(raise_exception=True)
        try:
            change_listing_status(
                listing=listing,
                to_status=ser.validated_data["to_status"],
                user=request.user,
                reason=ser.validated_data.get("reason", ""),
            )
        except IllegalTransition as exc:
            return Response(
                {"detail": str(exc)}, status=http_status.HTTP_400_BAD_REQUEST
            )
        return Response(ListingSerializer(listing).data)

    @action(
        detail=True,
        methods=["post"],
        parser_classes=[MultiPartParser, FormParser],
    )
    def media(self, request, pk=None):
        """Upload a photo/floorplan/brochure to object storage (SRS §3.3.3)."""
        listing = self.get_object()
        ser = ListingMediaSerializer(
            data={**request.data.dict(), "listing": str(listing.id)}
        )
        ser.is_valid(raise_exception=True)
        ser.save(tenant_id=listing.tenant_id)
        return Response(ser.data, status=http_status.HTTP_201_CREATED)
