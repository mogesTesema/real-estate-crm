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
        payload = LeaseWriteSerializer(data=request.data, context={"request": request})
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
            instance=lease, data=request.data, partial=kwargs.pop("partial", False),
            context={"request": request},
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

    @action(detail=True, methods=["post"], url_path="propose-renewal")
    def propose_renewal(self, request, pk=None):
        """One-click renewal (SRS 3.5.5): defaults from the running lease, escalation math
        done service-side, tenant notified when a portal login exists."""
        from .serializers import ProposeRenewalSerializer, RenewalSerializer

        payload = ProposeRenewalSerializer(data=request.data)
        payload.is_valid(raise_exception=True)
        try:
            renewal = services.propose_renewal(
                self.get_object(), actor=request.user, **payload.validated_data
            )
        except Exception as exc:  # noqa: BLE001
            _translate(exc)
        return Response(RenewalSerializer(renewal).data, status=status.HTTP_201_CREATED)

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


# --- Phase C viewsets ------------------------------------------------------------------------


class InspectionViewSet(
    ScopedQuerysetMixin,
    mixins.ListModelMixin,
    mixins.RetrieveModelMixin,
    mixins.CreateModelMixin,
    viewsets.GenericViewSet,
):
    """Inspections (SRS 3.5.4) — the calendar twin of viewings: scheduling upserts the one
    INSPECTION activity, closing closes it."""

    scope_resource = "inspection"
    filterset_fields = ["lease", "status", "inspection_type", "performed_by"]
    ordering = ["-scheduled_date"]

    def get_unscoped_queryset(self):
        from ..models import Inspection

        return Inspection.objects.select_related("lease", "property", "performed_by")

    def get_serializer_class(self):
        from .serializers import (
            InspectionCompleteSerializer,
            InspectionRescheduleSerializer,
            InspectionSerializer,
            InspectionWriteSerializer,
        )

        return {
            "create": InspectionWriteSerializer,
            "reschedule": InspectionRescheduleSerializer,
            "complete": InspectionCompleteSerializer,
        }.get(self.action, InspectionSerializer)

    def create(self, request, *args, **kwargs):
        from django.shortcuts import get_object_or_404

        from apps.identity.selectors import visible_users

        from .. import selectors as po_selectors
        from .serializers import InspectionSerializer, InspectionWriteSerializer

        payload = InspectionWriteSerializer(data=request.data)
        payload.is_valid(raise_exception=True)
        data = dict(payload.validated_data)
        lease = get_object_or_404(
            po_selectors.visible_leases(request.user), pk=data.pop("lease")
        )
        performed_by = get_object_or_404(
            visible_users(request.user), pk=data.pop("performed_by")
        )
        try:
            inspection = services.schedule_inspection(
                actor=request.user, lease=lease, performed_by=performed_by, **data
            )
        except Exception as exc:  # noqa: BLE001 - narrowed by _translate
            _translate(exc)
        return Response(
            InspectionSerializer(inspection).data, status=status.HTTP_201_CREATED
        )

    @action(detail=True, methods=["post"])
    def reschedule(self, request, pk=None):
        from django.shortcuts import get_object_or_404

        from apps.identity.selectors import visible_users

        from .serializers import InspectionRescheduleSerializer, InspectionSerializer

        payload = InspectionRescheduleSerializer(data=request.data)
        payload.is_valid(raise_exception=True)
        data = dict(payload.validated_data)
        if data.get("performed_by"):
            data["performed_by"] = get_object_or_404(
                visible_users(request.user), pk=data["performed_by"]
            )
        try:
            inspection = services.reschedule_inspection(
                self.get_object(), actor=request.user, **data
            )
        except Exception as exc:  # noqa: BLE001
            _translate(exc)
        return Response(InspectionSerializer(inspection).data)

    @action(detail=True, methods=["post"])
    def complete(self, request, pk=None):
        from .serializers import InspectionCompleteSerializer, InspectionSerializer

        payload = InspectionCompleteSerializer(data=request.data)
        payload.is_valid(raise_exception=True)
        try:
            inspection = services.complete_inspection(
                self.get_object(), actor=request.user, **payload.validated_data
            )
        except Exception as exc:  # noqa: BLE001
            _translate(exc)
        return Response(InspectionSerializer(inspection).data)

    @action(detail=True, methods=["post"])
    def cancel(self, request, pk=None):
        from .serializers import InspectionSerializer

        try:
            inspection = services.cancel_inspection(
                self.get_object(), actor=request.user,
                reason=request.data.get("reason"),
            )
        except Exception as exc:  # noqa: BLE001
            _translate(exc)
        return Response(InspectionSerializer(inspection).data)


class ApplicationViewSet(
    ScopedQuerysetMixin,
    mixins.ListModelMixin,
    mixins.RetrieveModelMixin,
    mixins.CreateModelMixin,
    viewsets.GenericViewSet,
):
    """Tenant screening (SRS 3.5.2): submit → screen → decide → convert to a draft lease."""

    scope_resource = "application"
    filterset_fields = ["property", "status", "assigned_to"]
    ordering = ["-created_at"]

    def get_unscoped_queryset(self):
        from ..models import Application

        return Application.objects.select_related(
            "property", "unit", "applicant_contact", "assigned_to"
        )

    def get_serializer_class(self):
        from .serializers import (
            ApplicationSerializer,
            ApplicationWriteSerializer,
            ConvertApplicationSerializer,
            DecisionSerializer,
            ScreeningSerializer,
        )

        return {
            "create": ApplicationWriteSerializer,
            "screening": ScreeningSerializer,
            "decide": DecisionSerializer,
            "convert": ConvertApplicationSerializer,
        }.get(self.action, ApplicationSerializer)

    def create(self, request, *args, **kwargs):
        from .serializers import ApplicationSerializer, ApplicationWriteSerializer

        payload = ApplicationWriteSerializer(data=request.data)
        payload.is_valid(raise_exception=True)
        data = dict(payload.validated_data)
        try:
            application = services.submit_application(
                actor=request.user,
                property=data.pop("property"),
                applicant_contact=data.pop("applicant_contact"),
                **data,
            )
        except Exception as exc:  # noqa: BLE001
            _translate(exc)
        return Response(
            ApplicationSerializer(application).data, status=status.HTTP_201_CREATED
        )

    @action(detail=True, methods=["post"])
    def screening(self, request, pk=None):
        from .serializers import ApplicationSerializer, ScreeningSerializer

        payload = ScreeningSerializer(data=request.data)
        payload.is_valid(raise_exception=True)
        try:
            application = services.update_screening(
                self.get_object(), actor=request.user, **payload.validated_data
            )
        except Exception as exc:  # noqa: BLE001
            _translate(exc)
        return Response(ApplicationSerializer(application).data)

    @action(detail=True, methods=["post"])
    def decide(self, request, pk=None):
        from .serializers import ApplicationSerializer, DecisionSerializer

        payload = DecisionSerializer(data=request.data)
        payload.is_valid(raise_exception=True)
        try:
            application = services.decide_application(
                self.get_object(), payload.validated_data["decision"],
                actor=request.user, reason=payload.validated_data.get("reason"),
            )
        except Exception as exc:  # noqa: BLE001
            _translate(exc)
        return Response(ApplicationSerializer(application).data)

    @action(detail=True, methods=["post"])
    def convert(self, request, pk=None):
        from django.shortcuts import get_object_or_404

        from apps.contacts.selectors import live_contacts

        from .serializers import ConvertApplicationSerializer, LeaseSerializer

        payload = ConvertApplicationSerializer(data=request.data)
        payload.is_valid(raise_exception=True)
        data = dict(payload.validated_data)
        if data.get("landlord"):
            data["landlord"] = get_object_or_404(live_contacts(), pk=data["landlord"])
        try:
            lease = services.convert_application(
                self.get_object(), actor=request.user, **data
            )
        except Exception as exc:  # noqa: BLE001
            _translate(exc)
        return Response(LeaseSerializer(lease).data, status=status.HTTP_201_CREATED)


class RenewalViewSet(
    ScopedQuerysetMixin, mixins.ListModelMixin, mixins.RetrieveModelMixin,
    mixins.UpdateModelMixin, viewsets.GenericViewSet,
):
    scope_resource = "renewal"
    filterset_fields = ["original_lease", "status"]
    ordering = ["-created_at"]

    def get_unscoped_queryset(self):
        from ..models import Renewal

        return Renewal.objects.select_related("original_lease", "new_lease")

    def get_serializer_class(self):
        from .serializers import RenewalSerializer

        return RenewalSerializer

    def update(self, request, *args, **kwargs):
        from .serializers import RenewalSerializer

        allowed = {"proposed_rent", "proposed_start_date", "proposed_end_date"}
        fields = {k: v for k, v in request.data.items() if k in allowed}
        try:
            renewal = services.update_renewal(
                self.get_object(), actor=request.user, **fields
            )
        except Exception as exc:  # noqa: BLE001
            _translate(exc)
        return Response(RenewalSerializer(renewal).data)

    @action(detail=True, methods=["post"])
    def accept(self, request, pk=None):
        from .serializers import LeaseSerializer

        try:
            successor = services.accept_renewal(self.get_object(), actor=request.user)
        except Exception as exc:  # noqa: BLE001
            _translate(exc)
        return Response(LeaseSerializer(successor).data, status=status.HTTP_201_CREATED)

    @action(detail=True, methods=["post"])
    def decline(self, request, pk=None):
        from .serializers import RenewalSerializer

        try:
            renewal = services.decline_renewal(
                self.get_object(), actor=request.user, reason=request.data.get("reason")
            )
        except Exception as exc:  # noqa: BLE001
            _translate(exc)
        return Response(RenewalSerializer(renewal).data)


class VendorViewSet(viewsets.ModelViewSet):
    """Vendors are agency reference data (the LeadSource precedent): unscoped, staff-only."""

    def get_queryset(self):
        from ..models import Vendor

        return Vendor.objects.select_related("contact").order_by("service_category")

    def get_serializer_class(self):
        from .serializers import VendorSerializer

        return VendorSerializer

    def get_permissions(self):
        from apps.identity.permissions import IsStaff

        return [*super().get_permissions(), IsStaff()]

    def perform_create(self, serializer):
        vendor = services.create_vendor(
            actor=self.request.user, **serializer.validated_data
        )
        serializer.instance = vendor

    def perform_update(self, serializer):
        data = dict(serializer.validated_data)
        data.pop("contact", None)  # a vendor profile never moves between contacts
        serializer.instance = services.update_vendor(
            serializer.instance, actor=self.request.user, **data
        )

    def perform_destroy(self, instance):
        services.update_vendor(instance, actor=self.request.user, is_active=False)

    @action(detail=True, methods=["post"])
    def rate(self, request, pk=None):
        from .serializers import RateVendorSerializer, VendorSerializer

        payload = RateVendorSerializer(data=request.data)
        payload.is_valid(raise_exception=True)
        try:
            vendor = services.rate_vendor(
                self.get_object(), actor=request.user,
                rating=payload.validated_data["rating"],
            )
        except Exception as exc:  # noqa: BLE001
            _translate(exc)
        return Response(VendorSerializer(vendor).data)


class MaintenanceRequestViewSet(
    ScopedQuerysetMixin,
    mixins.ListModelMixin,
    mixins.RetrieveModelMixin,
    mixins.CreateModelMixin,
    viewsets.GenericViewSet,
):
    """Maintenance requests (SRS 3.14.1–3.14.2).

    THE portal-create carve-out: `portal_writable = True` lets a rental tenant through the
    StaffWrite default for **create only** — `get_permissions` closes every other write to
    staff — and `create_maintenance_request` then forces the reporter identity and verifies
    the tenancy. Three layers, because this is the one place a client writes a domain row.
    """

    scope_resource = "maintenance_request"
    portal_writable = True
    filterset_fields = ["property", "lease", "status", "priority", "assigned_vendor",
                        "assigned_to"]
    ordering = ["-requested_at"]

    def get_permissions(self):
        from apps.identity.permissions import IsStaff

        if self.action in ("list", "retrieve", "create"):
            return super().get_permissions()
        return [*super().get_permissions(), IsStaff()]

    def get_unscoped_queryset(self):
        from ..models import MaintenanceRequest

        return MaintenanceRequest.objects.select_related(
            "property", "unit", "lease", "assigned_vendor", "assigned_to",
            "reported_by_contact", "reported_by_user",
        )

    def get_serializer_class(self):
        from .serializers import (
            MaintenanceAssignSerializer,
            MaintenanceCreateSerializer,
            MaintenanceRequestSerializer,
            MaintenanceStatusSerializer,
        )

        return {
            "create": MaintenanceCreateSerializer,
            "assign": MaintenanceAssignSerializer,
            "status": MaintenanceStatusSerializer,
        }.get(self.action, MaintenanceRequestSerializer)

    def create(self, request, *args, **kwargs):
        from django.shortcuts import get_object_or_404

        from apps.inventory.models import Property, Unit

        from .. import selectors as po_selectors
        from .serializers import MaintenanceCreateSerializer, MaintenanceRequestSerializer

        payload = MaintenanceCreateSerializer(data=request.data)
        payload.is_valid(raise_exception=True)
        data = dict(payload.validated_data)
        # Resolved unscoped on purpose: a tenant's property is not in their *staff* scope,
        # and the service's tenancy guard is the control that matters here.
        prop = get_object_or_404(
            Property.objects.filter(deleted_at__isnull=True), pk=data.pop("property")
        )
        unit = (
            get_object_or_404(Unit, pk=data.pop("unit")) if data.get("unit")
            else data.pop("unit", None)
        )
        lease = (
            get_object_or_404(po_selectors.live_leases(), pk=data.pop("lease"))
            if data.get("lease")
            else data.pop("lease", None)
        )
        try:
            request_obj = services.create_maintenance_request(
                actor=request.user, property=prop, unit=unit, lease=lease, **data
            )
        except Exception as exc:  # noqa: BLE001
            _translate(exc)
        return Response(
            MaintenanceRequestSerializer(request_obj).data, status=status.HTTP_201_CREATED
        )

    @action(detail=True, methods=["post"])
    def assign(self, request, pk=None):
        from django.shortcuts import get_object_or_404

        from apps.identity.selectors import visible_users

        from ..models import Vendor
        from .serializers import MaintenanceAssignSerializer, MaintenanceRequestSerializer

        payload = MaintenanceAssignSerializer(data=request.data)
        payload.is_valid(raise_exception=True)
        data = payload.validated_data
        vendor = (
            get_object_or_404(Vendor, pk=data["vendor"]) if data.get("vendor") else None
        )
        assignee = (
            get_object_or_404(visible_users(request.user), pk=data["assignee"])
            if data.get("assignee")
            else None
        )
        try:
            request_obj = services.assign_maintenance(
                self.get_object(), actor=request.user, vendor=vendor, assignee=assignee
            )
        except Exception as exc:  # noqa: BLE001
            _translate(exc)
        return Response(MaintenanceRequestSerializer(request_obj).data)

    @action(detail=True, methods=["post"])
    def status(self, request, pk=None):
        from .serializers import MaintenanceRequestSerializer, MaintenanceStatusSerializer

        payload = MaintenanceStatusSerializer(data=request.data)
        payload.is_valid(raise_exception=True)
        try:
            request_obj = services.change_maintenance_status(
                self.get_object(), payload.validated_data["status"],
                actor=request.user, note=payload.validated_data.get("note"),
            )
        except Exception as exc:  # noqa: BLE001
            _translate(exc)
        return Response(MaintenanceRequestSerializer(request_obj).data)


class WorkOrderViewSet(
    ScopedQuerysetMixin,
    mixins.ListModelMixin,
    mixins.RetrieveModelMixin,
    mixins.CreateModelMixin,
    viewsets.GenericViewSet,
):
    scope_resource = "work_order"
    filterset_fields = ["maintenance_request", "vendor", "status"]
    ordering = ["-scheduled_at"]

    def get_unscoped_queryset(self):
        from ..models import WorkOrder

        return WorkOrder.objects.select_related("maintenance_request", "vendor")

    def get_serializer_class(self):
        from .serializers import (
            AmountSerializer,
            ScheduleAtSerializer,
            WorkOrderCompleteSerializer,
            WorkOrderCreateSerializer,
            WorkOrderSerializer,
        )

        return {
            "create": WorkOrderCreateSerializer,
            "quote": AmountSerializer,
            "approve": AmountSerializer,
            "schedule": ScheduleAtSerializer,
            "complete": WorkOrderCompleteSerializer,
        }.get(self.action, WorkOrderSerializer)

    def create(self, request, *args, **kwargs):
        from django.shortcuts import get_object_or_404

        from apps.identity.selectors import apply_scope

        from ..models import MaintenanceRequest, Vendor
        from .serializers import WorkOrderCreateSerializer, WorkOrderSerializer

        payload = WorkOrderCreateSerializer(data=request.data)
        payload.is_valid(raise_exception=True)
        data = dict(payload.validated_data)
        maintenance_request = get_object_or_404(
            apply_scope(
                MaintenanceRequest.objects.all(), request.user, "maintenance_request"
            ),
            pk=data.pop("maintenance_request"),
        )
        vendor = get_object_or_404(Vendor, pk=data.pop("vendor"))
        try:
            work_order = services.create_work_order(
                actor=request.user, maintenance_request=maintenance_request,
                vendor=vendor, **data,
            )
        except Exception as exc:  # noqa: BLE001
            _translate(exc)
        return Response(
            WorkOrderSerializer(work_order).data, status=status.HTTP_201_CREATED
        )

    def _simple(self, request, service, serializer_cls, remap=None):
        from .serializers import WorkOrderSerializer

        payload = serializer_cls(data=request.data)
        payload.is_valid(raise_exception=True)
        data = dict(payload.validated_data)
        if remap:
            data = {remap.get(k, k): v for k, v in data.items()}
        try:
            result = service(self.get_object(), actor=request.user, **data)
        except Exception as exc:  # noqa: BLE001
            _translate(exc)
        work_order = result[0] if isinstance(result, tuple) else result
        return Response(WorkOrderSerializer(work_order).data)

    @action(detail=True, methods=["post"])
    def quote(self, request, pk=None):
        from .serializers import AmountSerializer

        return self._simple(request, services.quote_work_order, AmountSerializer,
                            remap={"amount": "quoted_amount"})

    @action(detail=True, methods=["post"])
    def approve(self, request, pk=None):
        from .serializers import AmountSerializer

        return self._simple(request, services.approve_work_order, AmountSerializer,
                            remap={"amount": "approved_amount"})

    @action(detail=True, methods=["post"])
    def schedule(self, request, pk=None):
        from .serializers import ScheduleAtSerializer

        return self._simple(request, services.schedule_work_order, ScheduleAtSerializer)

    @action(detail=True, methods=["post"])
    def start(self, request, pk=None):
        try:
            work_order = services.start_work_order(self.get_object(), actor=request.user)
        except Exception as exc:  # noqa: BLE001
            _translate(exc)
        from .serializers import WorkOrderSerializer

        return Response(WorkOrderSerializer(work_order).data)

    @action(detail=True, methods=["post"])
    def complete(self, request, pk=None):
        from django.shortcuts import get_object_or_404

        from apps.finance.selectors import visible_accounts

        from .serializers import WorkOrderCompleteSerializer, WorkOrderSerializer

        payload = WorkOrderCompleteSerializer(data=request.data)
        payload.is_valid(raise_exception=True)
        data = dict(payload.validated_data)
        if data.get("account"):
            data["account"] = get_object_or_404(
                visible_accounts(request.user), pk=data["account"]
            )
        else:
            data.pop("account", None)
        try:
            work_order, _expense = services.complete_work_order(
                self.get_object(), actor=request.user, **data
            )
        except Exception as exc:  # noqa: BLE001
            _translate(exc)
        return Response(WorkOrderSerializer(work_order).data)

    @action(detail=True, methods=["post"])
    def cancel(self, request, pk=None):
        from .serializers import WorkOrderSerializer

        try:
            work_order = services.cancel_work_order(
                self.get_object(), actor=request.user, reason=request.data.get("reason")
            )
        except Exception as exc:  # noqa: BLE001
            _translate(exc)
        return Response(WorkOrderSerializer(work_order).data)
