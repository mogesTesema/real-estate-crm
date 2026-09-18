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


# --- Phase C viewsets ------------------------------------------------------------------------


class CommissionPlanViewSet(viewsets.ModelViewSet):
    """Commission plans are agency configuration (the RoutingRule precedent) — how everyone
    gets paid is not a row anybody owns."""

    def get_queryset(self):
        from ..models import CommissionPlan

        return CommissionPlan.objects.all().order_by("name")

    def get_serializer_class(self):
        from .serializers import CommissionPlanSerializer

        return CommissionPlanSerializer

    def get_permissions(self):
        return [*super().get_permissions(), IsAgencyAdmin()]


class CommissionViewSet(
    ScopedQuerysetMixin,
    mixins.ListModelMixin,
    mixins.RetrieveModelMixin,
    mixins.CreateModelMixin,
    viewsets.GenericViewSet,
):
    """Commissions (SRS 3.6.4/3.6.5). An agent sees their own — including ones they are
    merely split into — through scoping; approval and payout are finance actions."""

    scope_resource = "commission"
    filterset_fields = ["status", "agent", "transaction", "lease", "commission_plan"]
    ordering = ["-created_at"]

    def get_unscoped_queryset(self):
        from ..models import Commission

        return Commission.objects.select_related(
            "agent", "commission_plan", "transaction", "lease"
        ).prefetch_related("splits")

    def get_serializer_class(self):
        from .serializers import (
            CommissionCreateSerializer,
            CommissionInvoiceSerializer,
            CommissionSerializer,
            PayCommissionSerializer,
            ReasonSerializer,
            SetSplitsSerializer,
        )

        return {
            "create": CommissionCreateSerializer,
            "splits": SetSplitsSerializer,
            "reject": ReasonSerializer,
            "pay": PayCommissionSerializer,
            "invoice": CommissionInvoiceSerializer,
        }.get(self.action, CommissionSerializer)

    def create(self, request, *args, **kwargs):
        from apps.crm.models import Transaction
        from apps.identity.selectors import visible_users
        from apps.property_ops.selectors import visible_leases

        from ..models import CommissionPlan
        from .serializers import CommissionCreateSerializer, CommissionSerializer

        payload = CommissionCreateSerializer(data=request.data)
        payload.is_valid(raise_exception=True)
        data = dict(payload.validated_data)
        plan = (
            get_object_or_404(CommissionPlan, pk=data.pop("plan"))
            if data.get("plan") else data.pop("plan", None)
        )
        agent = (
            get_object_or_404(visible_users(request.user), pk=data.pop("agent"))
            if data.get("agent") else data.pop("agent", None)
        )
        try:
            if data.get("transaction"):
                txn = get_object_or_404(Transaction, pk=data.pop("transaction"))
                data.pop("lease", None)
                commission = services.create_commission_for_transaction(
                    actor=request.user, transaction_obj=txn, plan=plan, agent=agent,
                    deductions=data.get("deductions", 0),
                    net_amount=data.get("net_amount"),
                )
            else:
                lease = get_object_or_404(
                    visible_leases(request.user), pk=data.pop("lease")
                )
                data.pop("transaction", None)
                commission = services.create_commission_for_lease(
                    actor=request.user, lease=lease, plan=plan, agent=agent,
                    deductions=data.get("deductions", 0),
                )
        except Exception as exc:  # noqa: BLE001
            _translate(exc)
        return Response(
            CommissionSerializer(commission).data, status=status.HTTP_201_CREATED
        )

    @action(detail=True, methods=["post"])
    def splits(self, request, pk=None):
        from .serializers import CommissionSerializer, SetSplitsSerializer

        payload = SetSplitsSerializer(data=request.data)
        payload.is_valid(raise_exception=True)
        try:
            services.set_commission_splits(
                self.get_object(), payload.validated_data["splits"], actor=request.user
            )
        except Exception as exc:  # noqa: BLE001
            _translate(exc)
        return Response(CommissionSerializer(self.get_object()).data)

    def _lifecycle(self, request, service, **kwargs):
        from .serializers import CommissionSerializer

        try:
            commission = service(self.get_object(), actor=request.user, **kwargs)
        except Exception as exc:  # noqa: BLE001
            _translate(exc)
        return Response(CommissionSerializer(commission).data)

    @action(detail=True, methods=["post"])
    def submit(self, request, pk=None):
        return self._lifecycle(request, services.submit_commission)

    @action(detail=True, methods=["post"])
    def approve(self, request, pk=None):
        return self._lifecycle(request, services.approve_commission)

    @action(detail=True, methods=["post"])
    def reject(self, request, pk=None):
        from .serializers import ReasonSerializer

        payload = ReasonSerializer(data=request.data)
        payload.is_valid(raise_exception=True)
        return self._lifecycle(
            request, services.reject_commission, reason=payload.validated_data["reason"]
        )

    @action(detail=True, methods=["post"])
    def pay(self, request, pk=None):
        from .serializers import PayCommissionSerializer

        payload = PayCommissionSerializer(data=request.data)
        payload.is_valid(raise_exception=True)
        account = get_object_or_404(
            selectors.visible_accounts(request.user), pk=payload.validated_data["account"]
        )
        return self._lifecycle(request, services.pay_commission, account=account)

    @action(detail=True, methods=["post"])
    def invoice(self, request, pk=None):
        from apps.contacts.selectors import live_contacts

        from .serializers import CommissionInvoiceSerializer, InvoiceSerializer

        payload = CommissionInvoiceSerializer(data=request.data)
        payload.is_valid(raise_exception=True)
        contact = get_object_or_404(
            live_contacts(), pk=payload.validated_data["contact"]
        )
        try:
            invoice = services.create_commission_invoice(
                self.get_object(), actor=request.user, contact=contact,
                due_date=payload.validated_data["due_date"],
            )
        except Exception as exc:  # noqa: BLE001
            _translate(exc)
        return Response(InvoiceSerializer(invoice).data, status=status.HTTP_201_CREATED)


class InstallmentPlanViewSet(
    ScopedQuerysetMixin,
    mixins.ListModelMixin,
    mixins.RetrieveModelMixin,
    mixins.CreateModelMixin,
    viewsets.GenericViewSet,
):
    scope_resource = "installment_plan"
    filterset_fields = ["status", "transaction"]
    ordering = ["-created_at"]

    def get_unscoped_queryset(self):
        from ..models import InstallmentPlan

        return InstallmentPlan.objects.select_related("transaction").prefetch_related(
            "milestones"
        )

    def get_serializer_class(self):
        from .serializers import (
            InstallmentPlanCreateSerializer,
            InstallmentPlanSerializer,
            MilestoneActionSerializer,
            ReasonSerializer,
        )

        return {
            "create": InstallmentPlanCreateSerializer,
            "invoice_milestone": MilestoneActionSerializer,
            "waive_milestone": MilestoneActionSerializer,
            "cancel": ReasonSerializer,
        }.get(self.action, InstallmentPlanSerializer)

    def create(self, request, *args, **kwargs):
        from apps.crm.models import Transaction

        from .serializers import InstallmentPlanCreateSerializer, InstallmentPlanSerializer

        payload = InstallmentPlanCreateSerializer(data=request.data)
        payload.is_valid(raise_exception=True)
        data = dict(payload.validated_data)
        txn = get_object_or_404(Transaction, pk=data.pop("transaction"))
        try:
            plan = services.create_installment_plan(
                actor=request.user, transaction_obj=txn,
                milestones=[dict(m) for m in data.pop("milestones")], **data,
            )
        except Exception as exc:  # noqa: BLE001
            _translate(exc)
        return Response(
            InstallmentPlanSerializer(plan).data, status=status.HTTP_201_CREATED
        )

    @action(detail=True, methods=["post"])
    def activate(self, request, pk=None):
        from .serializers import InstallmentPlanSerializer

        try:
            plan = services.activate_installment_plan(
                self.get_object(), actor=request.user
            )
        except Exception as exc:  # noqa: BLE001
            _translate(exc)
        return Response(InstallmentPlanSerializer(plan).data)

    @action(detail=True, methods=["post"])
    def cancel(self, request, pk=None):
        from .serializers import InstallmentPlanSerializer, ReasonSerializer

        payload = ReasonSerializer(data=request.data)
        payload.is_valid(raise_exception=True)
        try:
            plan = services.cancel_installment_plan(
                self.get_object(), actor=request.user,
                reason=payload.validated_data["reason"],
            )
        except Exception as exc:  # noqa: BLE001
            _translate(exc)
        return Response(InstallmentPlanSerializer(plan).data)

    @action(detail=True, methods=["post"], url_path="invoice-milestone")
    def invoice_milestone(self, request, pk=None):
        from apps.contacts.selectors import live_contacts

        from ..models import InstallmentMilestone
        from .serializers import InvoiceSerializer, MilestoneActionSerializer

        payload = MilestoneActionSerializer(data=request.data)
        payload.is_valid(raise_exception=True)
        data = payload.validated_data
        milestone = get_object_or_404(
            InstallmentMilestone, pk=data["milestone"], plan=self.get_object()
        )
        contact = (
            get_object_or_404(live_contacts(), pk=data["contact"])
            if data.get("contact") else None
        )
        try:
            invoice = services.invoice_milestone(
                milestone, actor=request.user, contact=contact,
                due_date=data.get("due_date"),
            )
        except Exception as exc:  # noqa: BLE001
            _translate(exc)
        return Response(InvoiceSerializer(invoice).data, status=status.HTTP_201_CREATED)

    @action(detail=True, methods=["post"], url_path="waive-milestone")
    def waive_milestone(self, request, pk=None):
        from ..models import InstallmentMilestone
        from .serializers import InstallmentPlanSerializer, MilestoneActionSerializer

        payload = MilestoneActionSerializer(data=request.data)
        payload.is_valid(raise_exception=True)
        milestone = get_object_or_404(
            InstallmentMilestone, pk=payload.validated_data["milestone"],
            plan=self.get_object(),
        )
        try:
            services.waive_milestone(
                milestone, actor=request.user,
                reason=payload.validated_data.get("reason", ""),
            )
        except Exception as exc:  # noqa: BLE001
            _translate(exc)
        return Response(InstallmentPlanSerializer(self.get_object()).data)


class ExpenseViewSet(
    ScopedQuerysetMixin,
    mixins.ListModelMixin,
    mixins.RetrieveModelMixin,
    mixins.CreateModelMixin,
    viewsets.GenericViewSet,
):
    scope_resource = "expense"
    filterset_fields = ["status", "category", "property", "lease", "is_billable_to_owner"]
    ordering = ["-expense_date"]

    def get_unscoped_queryset(self):
        from ..models import Expense

        return Expense.objects.select_related("property", "lease", "vendor_contact")

    def get_serializer_class(self):
        from .serializers import ExpenseCreateSerializer, ExpenseSerializer, ReasonSerializer

        return {
            "create": ExpenseCreateSerializer,
            "void": ReasonSerializer,
        }.get(self.action, ExpenseSerializer)

    def create(self, request, *args, **kwargs):
        from apps.contacts.selectors import live_contacts
        from apps.inventory.selectors import visible_properties
        from apps.property_ops.selectors import visible_leases

        from .serializers import ExpenseCreateSerializer, ExpenseSerializer

        payload = ExpenseCreateSerializer(data=request.data)
        payload.is_valid(raise_exception=True)
        data = dict(payload.validated_data)
        for key, resolver in (
            ("property", lambda pk: get_object_or_404(visible_properties(request.user), pk=pk)),
            ("lease", lambda pk: get_object_or_404(visible_leases(request.user), pk=pk)),
            ("vendor_contact", lambda pk: get_object_or_404(live_contacts(), pk=pk)),
            ("account",
             lambda pk: get_object_or_404(selectors.visible_accounts(request.user), pk=pk)),
        ):
            if data.get(key):
                data[key] = resolver(data[key])
            else:
                data.pop(key, None)
        try:
            expense = services.record_expense(actor=request.user, **data)
        except Exception as exc:  # noqa: BLE001
            _translate(exc)
        return Response(ExpenseSerializer(expense).data, status=status.HTTP_201_CREATED)

    @action(detail=True, methods=["post"])
    def approve(self, request, pk=None):
        from .serializers import ExpenseSerializer

        try:
            expense = services.approve_expense(self.get_object(), actor=request.user)
        except Exception as exc:  # noqa: BLE001
            _translate(exc)
        return Response(ExpenseSerializer(expense).data)

    @action(detail=True, methods=["post"])
    def pay(self, request, pk=None):
        from .serializers import ExpenseSerializer

        account = None
        if request.data.get("account"):
            account = get_object_or_404(
                selectors.visible_accounts(request.user), pk=request.data["account"]
            )
        try:
            expense = services.pay_expense(
                self.get_object(), actor=request.user, account=account
            )
        except Exception as exc:  # noqa: BLE001
            _translate(exc)
        return Response(ExpenseSerializer(expense).data)

    @action(detail=True, methods=["post"])
    def void(self, request, pk=None):
        from .serializers import ExpenseSerializer, ReasonSerializer

        payload = ReasonSerializer(data=request.data)
        payload.is_valid(raise_exception=True)
        try:
            expense = services.void_expense(
                self.get_object(), actor=request.user,
                reason=payload.validated_data["reason"],
            )
        except Exception as exc:  # noqa: BLE001
            _translate(exc)
        return Response(ExpenseSerializer(expense).data)


class OwnerStatementViewSet(
    ScopedQuerysetMixin,
    mixins.ListModelMixin,
    mixins.RetrieveModelMixin,
    mixins.CreateModelMixin,
    viewsets.GenericViewSet,
):
    """Owner statements (SRS 3.5.6). A landlord reads their own through the portal arm of
    the scoping registry; generation, adjustment and payout are finance actions."""

    scope_resource = "owner_statement"
    filterset_fields = ["owner_contact", "property", "status"]
    ordering = ["-period_start"]

    def get_unscoped_queryset(self):
        from ..models import OwnerStatement

        return OwnerStatement.objects.select_related(
            "owner_contact", "property"
        ).prefetch_related("lines")

    def get_serializer_class(self):
        from .serializers import (
            OwnerStatementSerializer,
            StatementAdjustmentSerializer,
            StatementGenerateSerializer,
        )

        return {
            "create": StatementGenerateSerializer,
            "adjustments": StatementAdjustmentSerializer,
        }.get(self.action, OwnerStatementSerializer)

    def create(self, request, *args, **kwargs):
        from apps.contacts.selectors import live_contacts
        from apps.inventory.selectors import visible_properties

        from .serializers import OwnerStatementSerializer, StatementGenerateSerializer

        payload = StatementGenerateSerializer(data=request.data)
        payload.is_valid(raise_exception=True)
        data = dict(payload.validated_data)
        owner = get_object_or_404(live_contacts(), pk=data.pop("owner_contact"))
        prop = (
            get_object_or_404(visible_properties(request.user), pk=data.pop("property"))
            if data.get("property") else data.pop("property", None)
        )
        try:
            statement = services.generate_owner_statement(
                actor=request.user, owner_contact=owner, property=prop, **data
            )
        except Exception as exc:  # noqa: BLE001
            _translate(exc)
        return Response(
            OwnerStatementSerializer(statement).data, status=status.HTTP_201_CREATED
        )

    @action(detail=True, methods=["post"])
    def adjustments(self, request, pk=None):
        from .serializers import OwnerStatementSerializer, StatementAdjustmentSerializer

        payload = StatementAdjustmentSerializer(data=request.data)
        payload.is_valid(raise_exception=True)
        try:
            statement = services.add_statement_adjustment(
                self.get_object(), actor=request.user, **payload.validated_data
            )
        except Exception as exc:  # noqa: BLE001
            _translate(exc)
        return Response(OwnerStatementSerializer(statement).data)

    @action(detail=True, methods=["post"])
    def issue(self, request, pk=None):
        from .serializers import OwnerStatementSerializer

        try:
            statement = services.issue_owner_statement(
                self.get_object(), actor=request.user
            )
        except Exception as exc:  # noqa: BLE001
            _translate(exc)
        return Response(OwnerStatementSerializer(statement).data)

    @action(detail=True, methods=["post"], url_path="mark-paid")
    def mark_paid(self, request, pk=None):
        from .serializers import OwnerStatementSerializer

        account = get_object_or_404(
            selectors.visible_accounts(request.user), pk=request.data.get("account")
        )
        try:
            statement = services.mark_statement_paid(
                self.get_object(), actor=request.user, account=account
            )
        except Exception as exc:  # noqa: BLE001
            _translate(exc)
        return Response(OwnerStatementSerializer(statement).data)


class ReconciliationViewSet(
    ScopedQuerysetMixin,
    mixins.ListModelMixin,
    mixins.RetrieveModelMixin,
    mixins.CreateModelMixin,
    viewsets.GenericViewSet,
):
    scope_resource = "reconciliation"
    filterset_fields = ["account", "status"]
    ordering = ["-period_start"]

    def get_unscoped_queryset(self):
        from ..models import Reconciliation

        return Reconciliation.objects.select_related("account", "reconciled_by")

    def get_serializer_class(self):
        from .serializers import ReconciliationCreateSerializer, ReconciliationSerializer

        return {
            "create": ReconciliationCreateSerializer,
        }.get(self.action, ReconciliationSerializer)

    def create(self, request, *args, **kwargs):
        from .serializers import ReconciliationCreateSerializer, ReconciliationSerializer

        payload = ReconciliationCreateSerializer(data=request.data)
        payload.is_valid(raise_exception=True)
        data = dict(payload.validated_data)
        account = get_object_or_404(
            selectors.visible_accounts(request.user), pk=data.pop("account")
        )
        try:
            recon = services.start_reconciliation(
                actor=request.user, account=account, **data
            )
        except Exception as exc:  # noqa: BLE001
            _translate(exc)
        return Response(
            ReconciliationSerializer(recon).data, status=status.HTTP_201_CREATED
        )

    @action(detail=True, methods=["post"])
    def refresh(self, request, pk=None):
        from .serializers import ReconciliationSerializer

        try:
            recon = services.refresh_reconciliation(self.get_object(), actor=request.user)
        except Exception as exc:  # noqa: BLE001
            _translate(exc)
        return Response(ReconciliationSerializer(recon).data)

    @action(detail=True, methods=["post"])
    def complete(self, request, pk=None):
        from .serializers import ReconciliationSerializer

        try:
            recon = services.complete_reconciliation(
                self.get_object(), actor=request.user, notes=request.data.get("notes")
            )
        except Exception as exc:  # noqa: BLE001
            _translate(exc)
        return Response(ReconciliationSerializer(recon).data)
