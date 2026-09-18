"""Views for the inventory API (SRS §3.3).

Every mutation delegates to `apps.inventory.services`; no view touches the ORM to write.
Every queryset runs through `ScopedQuerysetMixin`, per architecture.md §2.
"""
from django.core.exceptions import PermissionDenied as DjangoPermissionDenied
from django.core.exceptions import ValidationError as DjangoValidationError
from django.db.models import Prefetch
from drf_spectacular.utils import extend_schema
from rest_framework import mixins, status, viewsets
from rest_framework.decorators import action
from rest_framework.exceptions import PermissionDenied, ValidationError
from rest_framework.parsers import FormParser, JSONParser, MultiPartParser
from rest_framework.response import Response

from apps.identity.selectors import ScopedQuerysetMixin

from .. import selectors, services
from ..models import PropertyType
from .serializers import (
    ListingSerializer,
    ListingWriteSerializer,
    MediaOrderSerializer,
    MediaSerializer,
    PropertyOwnerSerializer,
    PropertySearchSerializer,
    PropertySerializer,
    PropertyTypeSerializer,
    PropertyWriteSerializer,
    SetOwnersSerializer,
    StatusChangeSerializer,
    StatusHistorySerializer,
    UnitSerializer,
)


def _translate(exc):
    """Re-raise a service-layer Django exception as its DRF equivalent."""
    if isinstance(exc, DjangoPermissionDenied):
        raise PermissionDenied(str(exc)) from exc
    if isinstance(exc, DjangoValidationError):
        raise ValidationError(
            exc.message_dict if hasattr(exc, "message_dict") else exc.messages
        ) from exc
    raise exc


class PropertyTypeViewSet(mixins.ListModelMixin, viewsets.GenericViewSet):
    """Reference data. Not row-scoped: a property type is a picklist, not a record."""

    queryset = PropertyType.objects.all().order_by("name")
    serializer_class = PropertyTypeSerializer
    pagination_class = None


class PropertyViewSet(
    ScopedQuerysetMixin,
    mixins.ListModelMixin,
    mixins.RetrieveModelMixin,
    mixins.CreateModelMixin,
    mixins.UpdateModelMixin,
    mixins.DestroyModelMixin,
    viewsets.GenericViewSet,
):
    """The property database (SRS 3.3.1–3.3.10)."""

    scope_resource = "property"
    filterset_fields = ["status", "property_type", "city", "managed_by", "project", "building"]
    search_fields = ["title", "address_line_1", "city"]
    ordering_fields = ["created_at", "updated_at", "title", "city", "bedrooms", "built_area"]
    ordering = ["-created_at"]

    def get_unscoped_queryset(self):
        return (
            selectors.live_properties()
            .select_related("property_type", "managed_by", "project", "building")
            .prefetch_related(
                # Filtered, not a bare "media": prefetch_related does not inherit the
                # queryset's soft-delete filter, so a deleted row could still be served as
                # the cover image.
                Prefetch("media", queryset=selectors.live_media().order_by("sort_order")),
                "owners__contact",
            )
        )

    def get_serializer_class(self):
        if self.action in ("create", "update", "partial_update"):
            return PropertyWriteSerializer
        if self.action == "status":
            return StatusChangeSerializer
        if self.action == "owners":
            return SetOwnersSerializer
        if self.action == "units":
            return UnitSerializer
        if self.action == "media":
            return MediaSerializer
        return PropertySerializer

    @extend_schema(request=PropertyWriteSerializer, responses={201: PropertySerializer})
    def create(self, request, *args, **kwargs):
        payload = PropertyWriteSerializer(data=request.data, context={"request": request})
        payload.is_valid(raise_exception=True)
        try:
            prop = services.create_property(actor=request.user, **payload.validated_data)
        except Exception as exc:  # noqa: BLE001 - narrowed by _translate
            _translate(exc)
        return Response(PropertySerializer(prop).data, status=status.HTTP_201_CREATED)

    @extend_schema(request=PropertyWriteSerializer, responses={200: PropertySerializer})
    def update(self, request, *args, **kwargs):
        prop = self.get_object()
        payload = PropertyWriteSerializer(
            instance=prop, data=request.data, partial=kwargs.pop("partial", False),
            context={"request": request},
        )
        payload.is_valid(raise_exception=True)
        try:
            prop = services.update_property(prop, actor=request.user, **payload.validated_data)
        except Exception as exc:  # noqa: BLE001
            _translate(exc)
        return Response(PropertySerializer(prop).data)

    def destroy(self, request, *args, **kwargs):
        try:
            services.delete_property(self.get_object(), actor=request.user)
        except Exception as exc:  # noqa: BLE001
            _translate(exc)
        return Response(status=status.HTTP_204_NO_CONTENT)

    @extend_schema(request=StatusChangeSerializer, responses={200: PropertySerializer})
    @action(detail=True, methods=["post"])
    def status(self, request, pk=None):
        """Move availability status, writing the history row (SRS 3.3.4).

        A separate endpoint rather than a PATCH field, so the transition table and the history
        write cannot be bypassed by a client that simply sets `status`.
        """
        prop = self.get_object()
        payload = StatusChangeSerializer(data=request.data)
        payload.is_valid(raise_exception=True)
        try:
            prop = services.change_status(
                prop,
                payload.validated_data["status"],
                actor=request.user,
                reason=payload.validated_data.get("reason"),
            )
        except Exception as exc:  # noqa: BLE001
            _translate(exc)
        return Response(PropertySerializer(prop).data)

    @extend_schema(responses={200: StatusHistorySerializer(many=True)})
    @action(detail=True, methods=["get"], url_path="status-history")
    def status_history(self, request, pk=None):
        rows = self.get_object().status_history.select_related("changed_by").order_by("-changed_at")
        return Response(StatusHistorySerializer(rows, many=True).data)

    @extend_schema(
        request=SetOwnersSerializer, responses={200: PropertyOwnerSerializer(many=True)}
    )
    @action(detail=True, methods=["get", "put"])
    def owners(self, request, pk=None):
        """Owner records with commission and mandate terms (SRS 3.3.9)."""
        prop = self.get_object()
        if request.method == "GET":
            return Response(
                PropertyOwnerSerializer(
                    prop.owners.select_related("contact"), many=True
                ).data
            )
        payload = SetOwnersSerializer(data=request.data)
        payload.is_valid(raise_exception=True)
        try:
            rows = services.set_owners(
                prop, payload.validated_data["owners"], actor=request.user
            )
        except Exception as exc:  # noqa: BLE001
            _translate(exc)
        return Response(PropertyOwnerSerializer(rows, many=True).data)

    @extend_schema(request=UnitSerializer, responses={201: UnitSerializer})
    @action(detail=True, methods=["get", "post"])
    def units(self, request, pk=None):
        """Addressable units inside the property (SRS 3.3.6)."""
        prop = self.get_object()
        if request.method == "GET":
            return Response(
                UnitSerializer(
                    selectors.live_units().filter(property=prop).order_by("unit_number"),
                    many=True,
                ).data
            )
        payload = UnitSerializer(data={**request.data, "property": str(prop.pk)})
        payload.is_valid(raise_exception=True)
        data = dict(payload.validated_data)
        data["property"] = prop
        try:
            unit = services.create_unit(actor=request.user, **data)
        except Exception as exc:  # noqa: BLE001
            _translate(exc)
        return Response(UnitSerializer(unit).data, status=status.HTTP_201_CREATED)

    @extend_schema(
        parameters=[PropertySearchSerializer],
        responses={200: PropertySerializer(many=True)},
    )
    @action(detail=False, methods=["get"])
    def search(self, request):
        """Advanced search: text, price, beds, amenities, and a PostGIS radius (SRS 3.3.7).

        Composes with row scoping — the search runs over `get_queryset()`, so it can never
        widen what the caller may see.
        """
        query = PropertySearchSerializer(data=request.query_params)
        query.is_valid(raise_exception=True)
        params = dict(query.validated_data)
        amenities = params.pop("amenities", "")
        results = selectors.search_properties(
            self.get_queryset(),
            amenities=[a.strip() for a in amenities.split(",") if a.strip()],
            **params,
        )
        page = self.paginate_queryset(results)
        return self.get_paginated_response(PropertySerializer(page, many=True).data)


class UnitViewSet(
    ScopedQuerysetMixin,
    mixins.ListModelMixin,
    mixins.RetrieveModelMixin,
    mixins.UpdateModelMixin,
    viewsets.GenericViewSet,
):
    """Units, scoped through their property — §2's "child records inherit scope"."""

    scope_resource = "unit"
    serializer_class = UnitSerializer
    filterset_fields = ["property", "status", "bedrooms"]
    ordering_fields = ["unit_number", "created_at", "sale_price", "rent_amount"]
    ordering = ["unit_number"]

    def get_unscoped_queryset(self):
        return selectors.live_units().select_related("property", "unit_type")

    def update(self, request, *args, **kwargs):
        unit = self.get_object()
        payload = UnitSerializer(
            instance=unit, data=request.data, partial=kwargs.pop("partial", False)
        )
        payload.is_valid(raise_exception=True)
        data = dict(payload.validated_data)
        data.pop("property", None)  # a unit does not move between properties
        try:
            unit = services.update_unit(unit, actor=request.user, **data)
        except Exception as exc:  # noqa: BLE001
            _translate(exc)
        return Response(UnitSerializer(unit).data)

    @extend_schema(request=StatusChangeSerializer, responses={200: UnitSerializer})
    @action(detail=True, methods=["post"])
    def status(self, request, pk=None):
        unit = self.get_object()
        payload = StatusChangeSerializer(data=request.data)
        payload.is_valid(raise_exception=True)
        try:
            unit = services.change_status(
                unit,
                payload.validated_data["status"],
                actor=request.user,
                reason=payload.validated_data.get("reason"),
            )
        except Exception as exc:  # noqa: BLE001
            _translate(exc)
        return Response(UnitSerializer(unit).data)


class ListingViewSet(
    ScopedQuerysetMixin,
    mixins.ListModelMixin,
    mixins.RetrieveModelMixin,
    mixins.CreateModelMixin,
    mixins.UpdateModelMixin,
    viewsets.GenericViewSet,
):
    """Listings — exclusive, open, and co-listed (SRS 3.3.1, 3.3.5)."""

    scope_resource = "listing"
    parser_classes = [JSONParser, FormParser, MultiPartParser]
    filterset_fields = ["status", "listing_type", "property", "assigned_agent", "is_exclusive"]
    search_fields = ["title", "reference_code"]
    ordering_fields = ["created_at", "published_at", "asking_price", "rent_amount", "title"]
    ordering = ["-created_at"]

    def get_unscoped_queryset(self):
        return selectors.live_listings().select_related(
            "property", "unit", "assigned_agent", "co_listing_agent"
        )

    def get_serializer_class(self):
        if self.action in ("create", "update", "partial_update"):
            return ListingWriteSerializer
        if self.action == "status":
            return StatusChangeSerializer
        if self.action == "media":
            return MediaSerializer
        if self.action == "reorder_media":
            return MediaOrderSerializer
        return ListingSerializer

    @extend_schema(request=ListingWriteSerializer, responses={201: ListingSerializer})
    def create(self, request, *args, **kwargs):
        payload = ListingWriteSerializer(data=request.data, context={"request": request})
        payload.is_valid(raise_exception=True)
        try:
            listing = services.create_listing(actor=request.user, **payload.validated_data)
        except Exception as exc:  # noqa: BLE001
            _translate(exc)
        return Response(ListingSerializer(listing).data, status=status.HTTP_201_CREATED)

    @extend_schema(request=ListingWriteSerializer, responses={200: ListingSerializer})
    def update(self, request, *args, **kwargs):
        listing = self.get_object()
        payload = ListingWriteSerializer(
            instance=listing, data=request.data, partial=kwargs.pop("partial", False),
            context={"request": request},
        )
        payload.is_valid(raise_exception=True)
        try:
            listing = services.update_listing(
                listing, actor=request.user, **payload.validated_data
            )
        except Exception as exc:  # noqa: BLE001
            _translate(exc)
        return Response(ListingSerializer(listing).data)

    @extend_schema(request=StatusChangeSerializer, responses={200: ListingSerializer})
    @action(detail=True, methods=["post"])
    def status(self, request, pk=None):
        listing = self.get_object()
        payload = StatusChangeSerializer(data=request.data)
        payload.is_valid(raise_exception=True)
        try:
            listing = services.change_listing_status(
                listing,
                payload.validated_data["status"],
                actor=request.user,
                reason=payload.validated_data.get("reason"),
            )
        except Exception as exc:  # noqa: BLE001
            _translate(exc)
        return Response(ListingSerializer(listing).data)

    @extend_schema(request=MediaSerializer, responses={201: MediaSerializer})
    @action(detail=True, methods=["get", "post"])
    def media(self, request, pk=None):
        """The listing gallery (SRS 3.3.3).

        `request.data` is used directly, never `request.data.dict()` — the old implementation
        called that and raised on any JSON body, so the endpoint worked only from a form.
        """
        listing = self.get_object()
        if request.method == "GET":
            return Response(
                MediaSerializer(selectors.gallery(listing), many=True).data
            )
        payload = MediaSerializer(data=request.data)
        payload.is_valid(raise_exception=True)
        data = dict(payload.validated_data)
        data.pop("property", None)
        data.pop("unit", None)
        data["listing"] = listing
        wants_cover = request.data.get("is_primary") in (True, "true", "True", "1", 1)
        try:
            media = services.add_media(actor=request.user, is_primary=wants_cover, **data)
        except Exception as exc:  # noqa: BLE001
            _translate(exc)
        return Response(MediaSerializer(media).data, status=status.HTTP_201_CREATED)

    @extend_schema(request=MediaOrderSerializer, responses={200: MediaSerializer(many=True)})
    @action(detail=True, methods=["post"], url_path="media/reorder")
    def reorder_media(self, request, pk=None):
        listing = self.get_object()
        payload = MediaOrderSerializer(data=request.data)
        payload.is_valid(raise_exception=True)
        try:
            services.reorder_media(
                {"listing": listing}, payload.validated_data["order"], actor=request.user
            )
        except Exception as exc:  # noqa: BLE001
            _translate(exc)
        return Response(MediaSerializer(selectors.gallery(listing), many=True).data)


class MediaViewSet(
    ScopedQuerysetMixin,
    mixins.RetrieveModelMixin,
    mixins.DestroyModelMixin,
    viewsets.GenericViewSet,
):
    """Individual media rows. Scoped through whichever parent they hang off."""

    scope_resource = "media"
    serializer_class = MediaSerializer

    def get_unscoped_queryset(self):
        return selectors.live_media()

    def destroy(self, request, *args, **kwargs):
        services.delete_media(self.get_object(), actor=request.user)
        return Response(status=status.HTTP_204_NO_CONTENT)

    @extend_schema(request=None, responses={200: MediaSerializer})
    @action(detail=True, methods=["post"], url_path="set-primary")
    def set_primary(self, request, pk=None):
        media = services.set_primary_media(self.get_object(), actor=request.user)
        return Response(MediaSerializer(media).data)
