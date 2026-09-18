"""Views for the property_ops API (SRS §3.5).

Every mutation delegates to `apps.property_ops.services`; every queryset runs through the
scoping registry, per architecture.md §2. Phase B ships leases, rent schedules and
deposits; inspections, applications, renewals and maintenance arrive with phase C.
"""
from django.core.exceptions import PermissionDenied as DjangoPermissionDenied
from django.core.exceptions import ValidationError as DjangoValidationError
from drf_spectacular.utils import extend_schema
from rest_framework import mixins, status, viewsets
from rest_framework.decorators import action
from rest_framework.exceptions import PermissionDenied, ValidationError
from rest_framework.response import Response

from apps.identity.selectors import ScopedQuerysetMixin

from .. import selectors, services
from .serializers import (
    DepositMovementSerializer,
    DepositSerializer,
    LeaseActivateSerializer,
    LeasePartySerializer,
    LeaseSerializer,
    LeaseStatusSerializer,
    LeaseTerminateSerializer,
    LeaseWriteSerializer,
    RentScheduleSerializer,
    WaiveSerializer,
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


class LeaseViewSet(
    ScopedQuerysetMixin,
    mixins.ListModelMixin,
    mixins.RetrieveModelMixin,
    mixins.CreateModelMixin,
    mixins.UpdateModelMixin,
    viewsets.GenericViewSet,
):
    """Leases (SRS 3.5.1). A portal tenant or landlord reads their own lease through the
    same endpoint — the scoping registry's PORTAL_OWN arm decides, not a separate portal
    app (§1.3 forbids inventing one)."""

    scope_resource = "lease"
    filterset_fields = ["status", "lease_type", "property", "tenant", "landlord",
                        "property_manager"]
    search_fields = ["reference_code"]
    ordering_fields = ["start_date", "end_date", "created_at", "rent_amount"]
    ordering = ["-created_at"]

    def get_unscoped_queryset(self):
        return selectors.live_leases()

    def get_serializer_class(self):
        if self.action in ("create", "update", "partial_update"):
            return LeaseWriteSerializer
        if self.action == "status":
            return LeaseStatusSerializer
        if self.action == "activate":
            return LeaseActivateSerializer
        if self.action == "terminate":
            return LeaseTerminateSerializer
        if self.action == "parties":
            return LeasePartySerializer
        return LeaseSerializer

    @extend_schema(request=LeaseWriteSerializer, responses={201: LeaseSerializer})
    def create(self, request, *args, **kwargs):
        payload = LeaseWriteSerializer(data=request.data)
        payload.is_valid(raise_exception=True)
        data = dict(payload.validated_data)
        parties = data.pop("parties", None)
        try:
            lease = services.create_lease(actor=request.user, parties=parties, **data)
        except Exception as exc:  # noqa: BLE001 - narrowed by _translate
            _translate(exc)
        return Response(LeaseSerializer(lease).data, status=status.HTTP_201_CREATED)

    @extend_schema(request=LeaseWriteSerializer, responses={200: LeaseSerializer})
    def update(self, request, *args, **kwargs):
        lease = self.get_object()
        payload = LeaseWriteSerializer(
            instance=lease, data=request.data, partial=kwargs.pop("partial", False)
        )
        payload.is_valid(raise_exception=True)
        data = dict(payload.validated_data)
        data.pop("parties", None)
        try:
            lease = services.update_lease(lease, actor=request.user, **data)
        except Exception as exc:  # noqa: BLE001
            _translate(exc)
        return Response(LeaseSerializer(lease).data)

    @extend_schema(request=LeaseStatusSerializer, responses={200: LeaseSerializer})
    @action(detail=True, methods=["post"])
    def status(self, request, pk=None):
        lease = self.get_object()
        payload = LeaseStatusSerializer(data=request.data)
        payload.is_valid(raise_exception=True)
        try:
            lease = services.change_lease_status(
                lease, payload.validated_data["status"], actor=request.user,
                reason=payload.validated_data.get("reason"),
            )
        except Exception as exc:  # noqa: BLE001
            _translate(exc)
        return Response(LeaseSerializer(lease).data)

    @extend_schema(request=LeaseActivateSerializer, responses={200: LeaseSerializer})
    @action(detail=True, methods=["post"])
    def activate(self, request, pk=None):
        """§1.2's orchestration: occupy, schedule, expect the deposit, invoice what's due."""
        lease = self.get_object()
        payload = LeaseActivateSerializer(data=request.data)
        payload.is_valid(raise_exception=True)
        try:
            lease = services.activate_lease(
                lease, actor=request.user,
                invoice_horizon_days=payload.validated_data.get("invoice_horizon_days"),
            )
        except Exception as exc:  # noqa: BLE001
            _translate(exc)
        return Response(LeaseSerializer(lease).data)

    @extend_schema(request=LeaseTerminateSerializer, responses={200: LeaseSerializer})
    @action(detail=True, methods=["post"])
    def terminate(self, request, pk=None):
        lease = self.get_object()
        payload = LeaseTerminateSerializer(data=request.data)
        payload.is_valid(raise_exception=True)
        try:
            lease = services.terminate_lease(
                lease, actor=request.user, reason=payload.validated_data.get("reason")
            )
        except Exception as exc:  # noqa: BLE001
            _translate(exc)
        return Response(LeaseSerializer(lease).data)

    @extend_schema(request=LeasePartySerializer(many=True),
                   responses={200: LeasePartySerializer(many=True)})
    @action(detail=True, methods=["put"])
    def parties(self, request, pk=None):
        lease = self.get_object()
        payload = LeasePartySerializer(data=request.data, many=True)
        payload.is_valid(raise_exception=True)
        try:
            rows = services.set_lease_parties(
                lease, payload.validated_data, actor=request.user
            )
        except Exception as exc:  # noqa: BLE001
            _translate(exc)
        return Response(LeasePartySerializer(rows, many=True).data)

    @extend_schema(request=DepositMovementSerializer, responses={200: DepositSerializer})
    @action(detail=True, methods=["post"], url_path="receive-deposit")
    def receive_deposit(self, request, pk=None):
        """Stamp the deposit as actually received (SRS 3.5.4) — receipt is a separate fact
        from the contractual expectation, and the row may not exist yet."""
        lease = self.get_object()
        payload = DepositMovementSerializer(data=request.data)
        payload.is_valid(raise_exception=True)
        try:
            deposit = services.record_deposit_received(
                lease, actor=request.user,
                amount=payload.validated_data.get("amount"),
                notes=payload.validated_data.get("notes"),
            )
        except Exception as exc:  # noqa: BLE001
            _translate(exc)
        return Response(DepositSerializer(deposit).data)

    @extend_schema(responses={200: RentScheduleSerializer(many=True)})
    @action(detail=True, methods=["get"], url_path="rent-schedule")
    def rent_schedule(self, request, pk=None):
        """The lease ledger — periods, invoices, balances. This is the portal tenant's
        "rent payment history" (SRS 3.11.5) as much as the PM's collection view."""
        lease = self.get_object()
        rows = lease.rent_schedules.select_related("invoice").order_by("period_start")
        return Response(RentScheduleSerializer(rows, many=True).data)


class RentScheduleViewSet(
    ScopedQuerysetMixin, mixins.ListModelMixin, mixins.RetrieveModelMixin,
    viewsets.GenericViewSet,
):
    scope_resource = "rent_schedule"
    serializer_class = RentScheduleSerializer
    filterset_fields = ["lease", "status"]
    ordering_fields = ["due_date", "period_start"]
    ordering = ["due_date"]

    def get_unscoped_queryset(self):
        return selectors.live_rent_schedules()

    @extend_schema(request=WaiveSerializer, responses={200: RentScheduleSerializer})
    @action(detail=True, methods=["post"])
    def waive(self, request, pk=None):
        payload = WaiveSerializer(data=request.data)
        payload.is_valid(raise_exception=True)
        try:
            row = services.waive_rent_period(
                self.get_object(), actor=request.user,
                reason=payload.validated_data["reason"],
            )
        except Exception as exc:  # noqa: BLE001
            _translate(exc)
        return Response(RentScheduleSerializer(row).data)


class DepositViewSet(
    ScopedQuerysetMixin, mixins.ListModelMixin, mixins.RetrieveModelMixin,
    viewsets.GenericViewSet,
):
    scope_resource = "deposit"
    serializer_class = DepositSerializer
    filterset_fields = ["lease", "status"]

    def get_unscoped_queryset(self):
        return selectors.live_deposits()

    def _movement(self, request, service, **extra):
        payload = DepositMovementSerializer(data=request.data)
        payload.is_valid(raise_exception=True)
        data = {**payload.validated_data, **extra}
        try:
            deposit = service(self.get_object(), actor=request.user, **data)
        except Exception as exc:  # noqa: BLE001
            _translate(exc)
        return Response(DepositSerializer(deposit).data)

    @extend_schema(request=DepositMovementSerializer, responses={200: DepositSerializer})
    @action(detail=True, methods=["post"])
    def refund(self, request, pk=None):
        return self._movement(request, services.refund_deposit)

    @extend_schema(request=DepositMovementSerializer, responses={200: DepositSerializer})
    @action(detail=True, methods=["post"])
    def deduct(self, request, pk=None):
        return self._movement(request, services.deduct_from_deposit)

    @extend_schema(request=DepositMovementSerializer, responses={200: DepositSerializer})
    @action(detail=True, methods=["post"])
    def forfeit(self, request, pk=None):
        payload = DepositMovementSerializer(data=request.data)
        payload.is_valid(raise_exception=True)
        try:
            deposit = services.forfeit_deposit(
                self.get_object(), actor=request.user,
                reason=payload.validated_data.get("reason", ""),
            )
        except Exception as exc:  # noqa: BLE001
            _translate(exc)
        return Response(DepositSerializer(deposit).data)
