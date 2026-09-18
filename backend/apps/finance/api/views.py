"""Views for the finance API (SRS §3.6, §11).

Every mutation delegates to `apps.finance.services` — the sole money writer. There is no
write endpoint for the ledger at all: `account-entries` is read-only by construction, and
manual corrections go through the accounts' `adjustments` action, which is the one public
door `post_adjustment` opens.
"""
from django.core.exceptions import PermissionDenied as DjangoPermissionDenied
from django.core.exceptions import ValidationError as DjangoValidationError
from django.shortcuts import get_object_or_404
from drf_spectacular.utils import OpenApiResponse, extend_schema
from rest_framework import mixins, status, viewsets
from rest_framework.decorators import action
from rest_framework.exceptions import PermissionDenied, ValidationError
from rest_framework.response import Response

from apps.identity.permissions import IsAgencyAdmin
from apps.identity.selectors import ScopedQuerysetMixin

from .. import selectors, services
from .serializers import (
    AccountEntrySerializer,
    AccountSerializer,
    AdjustmentSerializer,
    AllocateSerializer,
    ChequeBounceSerializer,
    ChequeClearSerializer,
    ChequeCreateSerializer,
    ChequeDepositSerializer,
    ChequeReplaceSerializer,
    ChequeSerializer,
    InvoiceCreateSerializer,
    InvoiceSerializer,
    PaymentCreateSerializer,
    PaymentSerializer,
    ReverseSerializer,
    VoidSerializer,
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


class AccountViewSet(
    ScopedQuerysetMixin,
    mixins.ListModelMixin,
    mixins.RetrieveModelMixin,
    mixins.CreateModelMixin,
    mixins.UpdateModelMixin,
    viewsets.GenericViewSet,
):
    """Bank/cash accounts. Creating one is agency configuration; reading them is already
    limited to the finance function by scoping (ALL/FINANCE_ALL, everyone else NOTHING)."""

    scope_resource = "account"
    serializer_class = AccountSerializer

    def get_permissions(self):
        if self.action in ("create", "update", "partial_update"):
            return [*super().get_permissions(), IsAgencyAdmin()]
        return super().get_permissions()

    def get_unscoped_queryset(self):
        from ..models import Account

        return Account.objects.all()

    def perform_create(self, serializer):
        account = services.create_account(actor=self.request.user, **serializer.validated_data)
        serializer.instance = account

    def perform_update(self, serializer):
        serializer.instance = services.update_account(
            serializer.instance, actor=self.request.user, **serializer.validated_data
        )

    @extend_schema(responses={200: OpenApiResponse(description='{"balance": "..."} ')})
    @action(detail=True, methods=["get"])
    def balance(self, request, pk=None):
        return Response({"balance": str(selectors.account_balance(self.get_object()))})

    @extend_schema(request=AdjustmentSerializer, responses={201: AccountEntrySerializer})
    @action(detail=True, methods=["post"])
    def adjustments(self, request, pk=None):
        payload = AdjustmentSerializer(data=request.data)
        payload.is_valid(raise_exception=True)
        try:
            entry = services.post_adjustment(
                actor=request.user, account=self.get_object(), **payload.validated_data
            )
        except Exception as exc:  # noqa: BLE001 - narrowed by _translate
            _translate(exc)
        return Response(AccountEntrySerializer(entry).data, status=status.HTTP_201_CREATED)


class AccountEntryViewSet(
    ScopedQuerysetMixin, mixins.ListModelMixin, mixins.RetrieveModelMixin,
    viewsets.GenericViewSet,
):
    """The append-only ledger. Read-only is not a policy here, it is physics: UPDATE and
    DELETE are revoked from the application role at the database."""

    scope_resource = "account_entry"
    serializer_class = AccountEntrySerializer
    filterset_fields = ["account", "entry_type", "reference_type"]
    ordering_fields = ["posted_at"]
    ordering = ["-posted_at"]

    def get_unscoped_queryset(self):
        from ..models import AccountEntry

        return AccountEntry.objects.select_related("account")


class InvoiceViewSet(
    ScopedQuerysetMixin,
    mixins.ListModelMixin,
    mixins.RetrieveModelMixin,
    mixins.CreateModelMixin,
    viewsets.GenericViewSet,
):
    scope_resource = "invoice"
    filterset_fields = ["status", "invoice_type", "contact", "lease", "deal"]
    search_fields = ["invoice_number"]
    ordering_fields = ["issue_date", "due_date", "total_amount", "balance_due"]
    ordering = ["-issue_date"]

    def get_unscoped_queryset(self):
        return selectors.live_invoices()

    def get_serializer_class(self):
        if self.action == "create":
            return InvoiceCreateSerializer
        if self.action == "void":
            return VoidSerializer
        return InvoiceSerializer

    @extend_schema(request=InvoiceCreateSerializer, responses={201: InvoiceSerializer})
    def create(self, request, *args, **kwargs):
        from apps.contacts.selectors import live_contacts
        from apps.property_ops.selectors import visible_leases

        payload = InvoiceCreateSerializer(data=request.data)
        payload.is_valid(raise_exception=True)
        data = dict(payload.validated_data)
        contact = get_object_or_404(live_contacts(), pk=data.pop("contact"))
        lease = (
            get_object_or_404(visible_leases(request.user), pk=data.pop("lease"))
            if data.get("lease")
            else data.pop("lease", None)
        )
        deal = None
        if data.get("deal"):
            from apps.crm.selectors import visible_deals

            deal = get_object_or_404(visible_deals(request.user), pk=data.pop("deal"))
        else:
            data.pop("deal", None)
        lines = [dict(line) for line in data.pop("lines")]
        for line in lines:
            if line.get("property"):
                from apps.inventory.models import Property

                line["property"] = get_object_or_404(Property, pk=line["property"])
        try:
            invoice = services.create_invoice(
                actor=request.user, contact=contact, lease=lease, deal=deal,
                lines=lines, **data,
            )
        except Exception as exc:  # noqa: BLE001
            _translate(exc)
        return Response(InvoiceSerializer(invoice).data, status=status.HTTP_201_CREATED)

    @extend_schema(request=None, responses={200: InvoiceSerializer})
    @action(detail=True, methods=["post"])
    def issue(self, request, pk=None):
        try:
            invoice = services.issue_invoice(self.get_object(), actor=request.user)
        except Exception as exc:  # noqa: BLE001
            _translate(exc)
        return Response(InvoiceSerializer(invoice).data)

    @extend_schema(request=VoidSerializer, responses={200: InvoiceSerializer})
    @action(detail=True, methods=["post"])
    def void(self, request, pk=None):
        payload = VoidSerializer(data=request.data)
        payload.is_valid(raise_exception=True)
        try:
            invoice = services.void_invoice(
                self.get_object(), actor=request.user,
                reason=payload.validated_data["reason"],
            )
        except Exception as exc:  # noqa: BLE001
            _translate(exc)
        return Response(InvoiceSerializer(invoice).data)


class PaymentViewSet(
    ScopedQuerysetMixin,
    mixins.ListModelMixin,
    mixins.RetrieveModelMixin,
    mixins.CreateModelMixin,
    viewsets.GenericViewSet,
):
    scope_resource = "payment"
    filterset_fields = ["status", "payment_method", "payer", "account"]
    search_fields = ["payment_reference", "external_reference"]
    ordering_fields = ["payment_date", "amount"]
    ordering = ["-payment_date"]

    def get_unscoped_queryset(self):
        return selectors.live_payments()

    def get_serializer_class(self):
        if self.action == "create":
            return PaymentCreateSerializer
        if self.action == "allocate":
            return AllocateSerializer
        if self.action == "reverse":
            return ReverseSerializer
        return PaymentSerializer

    @extend_schema(request=PaymentCreateSerializer, responses={201: PaymentSerializer})
    def create(self, request, *args, **kwargs):
        from apps.contacts.selectors import live_contacts

        payload = PaymentCreateSerializer(data=request.data)
        payload.is_valid(raise_exception=True)
        data = dict(payload.validated_data)
        payer = get_object_or_404(live_contacts(), pk=data.pop("payer"))
        account = get_object_or_404(
            selectors.visible_accounts(request.user), pk=data.pop("account")
        )
        allocations = [
            {"invoice": row["invoice"], "amount": row["amount"]}
            for row in data.pop("allocations", [])
        ] or None
        try:
            payment = services.record_payment(
                actor=request.user, payer=payer, account=account,
                allocations=allocations, **data,
            )
        except Exception as exc:  # noqa: BLE001
            _translate(exc)
        return Response(PaymentSerializer(payment).data, status=status.HTTP_201_CREATED)

    @extend_schema(request=AllocateSerializer, responses={200: PaymentSerializer})
    @action(detail=True, methods=["post"])
    def allocate(self, request, pk=None):
        payload = AllocateSerializer(data=request.data)
        payload.is_valid(raise_exception=True)
        try:
            services.allocate_payment(
                self.get_object(), actor=request.user,
                allocations=payload.validated_data["allocations"],
            )
        except Exception as exc:  # noqa: BLE001
            _translate(exc)
        return Response(PaymentSerializer(self.get_object()).data)

    @extend_schema(request=ReverseSerializer, responses={200: PaymentSerializer})
    @action(detail=True, methods=["post"])
    def reverse(self, request, pk=None):
        payload = ReverseSerializer(data=request.data)
        payload.is_valid(raise_exception=True)
        try:
            payment = services.reverse_payment(
                self.get_object(), actor=request.user, **payload.validated_data
            )
        except Exception as exc:  # noqa: BLE001
            _translate(exc)
        return Response(PaymentSerializer(payment).data)


class ChequeViewSet(
    ScopedQuerysetMixin,
    mixins.ListModelMixin,
    mixins.RetrieveModelMixin,
    mixins.CreateModelMixin,
    viewsets.GenericViewSet,
):
    scope_resource = "cheque"
    filterset_fields = ["status", "payer", "lease"]
    search_fields = ["cheque_number", "drawer_name"]
    ordering_fields = ["cheque_date"]
    ordering = ["cheque_date"]

    def get_unscoped_queryset(self):
        from ..models import Cheque as ChequeModel

        return ChequeModel.objects.select_related("payer", "lease", "deposit_account")

    def get_serializer_class(self):
        return {
            "create": ChequeCreateSerializer,
            "deposit": ChequeDepositSerializer,
            "clear": ChequeClearSerializer,
            "bounce": ChequeBounceSerializer,
            "replace": ChequeReplaceSerializer,
        }.get(self.action, ChequeSerializer)

    @extend_schema(request=ChequeCreateSerializer, responses={201: ChequeSerializer})
    def create(self, request, *args, **kwargs):
        from apps.contacts.selectors import live_contacts
        from apps.property_ops.selectors import visible_leases

        payload = ChequeCreateSerializer(data=request.data)
        payload.is_valid(raise_exception=True)
        data = dict(payload.validated_data)
        payer = get_object_or_404(live_contacts(), pk=data.pop("payer"))
        lease = (
            get_object_or_404(visible_leases(request.user), pk=data.pop("lease"))
            if data.get("lease")
            else data.pop("lease", None)
        )
        try:
            cheque = services.register_cheque(
                actor=request.user, payer=payer, lease=lease, **data
            )
        except Exception as exc:  # noqa: BLE001
            _translate(exc)
        return Response(ChequeSerializer(cheque).data, status=status.HTTP_201_CREATED)

    @extend_schema(request=ChequeDepositSerializer, responses={200: ChequeSerializer})
    @action(detail=True, methods=["post"])
    def deposit(self, request, pk=None):
        payload = ChequeDepositSerializer(data=request.data)
        payload.is_valid(raise_exception=True)
        account = get_object_or_404(
            selectors.visible_accounts(request.user), pk=payload.validated_data["account"]
        )
        try:
            cheque = services.deposit_cheque(
                self.get_object(), actor=request.user, account=account
            )
        except Exception as exc:  # noqa: BLE001
            _translate(exc)
        return Response(ChequeSerializer(cheque).data)

    @extend_schema(request=ChequeClearSerializer, responses={200: ChequeSerializer})
    @action(detail=True, methods=["post"])
    def clear(self, request, pk=None):
        payload = ChequeClearSerializer(data=request.data)
        payload.is_valid(raise_exception=True)
        allocations = payload.validated_data.get("allocations") or None
        if allocations:
            allocations = [
                {"invoice": row["invoice"], "amount": row["amount"]} for row in allocations
            ]
        try:
            cheque, _payment = services.clear_cheque(
                self.get_object(), actor=request.user, allocations=allocations
            )
        except Exception as exc:  # noqa: BLE001
            _translate(exc)
        return Response(ChequeSerializer(cheque).data)

    @extend_schema(request=ChequeBounceSerializer, responses={200: ChequeSerializer})
    @action(detail=True, methods=["post"])
    def bounce(self, request, pk=None):
        payload = ChequeBounceSerializer(data=request.data)
        payload.is_valid(raise_exception=True)
        try:
            cheque = services.bounce_cheque(
                self.get_object(), actor=request.user,
                reason=payload.validated_data["reason"],
            )
        except Exception as exc:  # noqa: BLE001
            _translate(exc)
        return Response(ChequeSerializer(cheque).data)

    @extend_schema(request=ChequeReplaceSerializer, responses={200: ChequeSerializer})
    @action(detail=True, methods=["post"])
    def replace(self, request, pk=None):
        payload = ChequeReplaceSerializer(data=request.data)
        payload.is_valid(raise_exception=True)
        try:
            _old, replacement = services.replace_cheque(
                self.get_object(), actor=request.user, **payload.validated_data
            )
        except Exception as exc:  # noqa: BLE001
            _translate(exc)
        return Response(ChequeSerializer(replacement).data)

    @extend_schema(request=None, responses={200: ChequeSerializer})
    @action(detail=True, methods=["post"], url_path="return")
    def return_cheque(self, request, pk=None):
        try:
            cheque = services.return_cheque(self.get_object(), actor=request.user)
        except Exception as exc:  # noqa: BLE001
            _translate(exc)
        return Response(ChequeSerializer(cheque).data)
