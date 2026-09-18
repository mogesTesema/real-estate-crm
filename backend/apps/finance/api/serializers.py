"""Serializers for the finance API.

Shape only — the allocation algorithm, the ledger postings and every status walk live in
`finance.services`, which is the sole money writer (§11).
"""
from drf_spectacular.utils import extend_schema_field
from rest_framework import serializers

from ..models import Account, AccountEntry, Cheque, Invoice, InvoiceLine, Payment


class AccountSerializer(serializers.ModelSerializer):
    class Meta:
        model = Account
        fields = ("id", "name", "account_type", "currency", "account_number", "is_active")
        read_only_fields = ("id",)


class AccountEntrySerializer(serializers.ModelSerializer):
    class Meta:
        model = AccountEntry
        fields = (
            "id", "account", "entry_type", "amount", "currency", "reference_type",
            "reference_id", "description", "posted_at", "created_at",
        )
        read_only_fields = fields


class AdjustmentSerializer(serializers.Serializer):
    entry_type = serializers.ChoiceField(choices=AccountEntry.EntryType.choices)
    amount = serializers.DecimalField(max_digits=15, decimal_places=2)
    description = serializers.CharField()


class InvoiceLineSerializer(serializers.ModelSerializer):
    class Meta:
        model = InvoiceLine
        fields = (
            "id", "description", "quantity", "unit_price", "tax_rate", "tax_amount",
            "line_total", "property",
        )
        read_only_fields = ("id", "tax_amount", "line_total")


class InvoiceLineInputSerializer(serializers.Serializer):
    description = serializers.CharField()
    quantity = serializers.DecimalField(max_digits=12, decimal_places=2)
    unit_price = serializers.DecimalField(max_digits=15, decimal_places=2)
    tax_rate = serializers.DecimalField(
        max_digits=7, decimal_places=4, required=False, allow_null=True,
        help_text="Omit for the company default VAT; 0 for genuinely untaxed.",
    )
    property = serializers.UUIDField(required=False, allow_null=True)


class InvoiceSerializer(serializers.ModelSerializer):
    lines = InvoiceLineSerializer(many=True, read_only=True)
    contact_name = serializers.CharField(source="contact.__str__", read_only=True)

    class Meta:
        model = Invoice
        fields = "__all__"
        read_only_fields = (
            "id", "invoice_number", "status", "subtotal", "tax_amount", "total_amount",
            "amount_paid", "balance_due", "created_at", "updated_at",
        )


class InvoiceCreateSerializer(serializers.Serializer):
    contact = serializers.UUIDField()
    invoice_type = serializers.ChoiceField(choices=Invoice.InvoiceType.choices)
    issue_date = serializers.DateField()
    due_date = serializers.DateField()
    lines = InvoiceLineInputSerializer(many=True)
    currency = serializers.CharField(required=False, max_length=3)
    lease = serializers.UUIDField(required=False, allow_null=True)
    deal = serializers.UUIDField(required=False, allow_null=True)
    discount_amount = serializers.DecimalField(
        max_digits=15, decimal_places=2, required=False, default=0
    )
    notes = serializers.CharField(required=False, allow_blank=True)
    issue = serializers.BooleanField(default=False)


class VoidSerializer(serializers.Serializer):
    reason = serializers.CharField()


class AllocationInputSerializer(serializers.Serializer):
    invoice = serializers.UUIDField()
    amount = serializers.DecimalField(max_digits=15, decimal_places=2)


class PaymentSerializer(serializers.ModelSerializer):
    payer_name = serializers.CharField(source="payer.__str__", read_only=True)
    allocations = serializers.SerializerMethodField()

    class Meta:
        model = Payment
        fields = "__all__"
        read_only_fields = ("id", "payment_reference", "status", "created_at")

    @extend_schema_field(serializers.ListField(child=serializers.DictField()))
    def get_allocations(self, obj):
        return [
            {
                "invoice": str(row.invoice_id),
                "invoice_number": row.invoice.invoice_number,
                "allocated_amount": str(row.allocated_amount),
            }
            for row in obj.allocations.select_related("invoice")
        ]


class PaymentCreateSerializer(serializers.Serializer):
    payer = serializers.UUIDField()
    account = serializers.UUIDField()
    amount = serializers.DecimalField(max_digits=15, decimal_places=2)
    payment_method = serializers.ChoiceField(choices=Payment.PaymentMethod.choices)
    payment_date = serializers.DateField()
    currency = serializers.CharField(required=False, max_length=3)
    allocations = AllocationInputSerializer(many=True, required=False)
    external_reference = serializers.CharField(required=False, allow_blank=True)
    notes = serializers.CharField(required=False, allow_blank=True)


class AllocateSerializer(serializers.Serializer):
    allocations = AllocationInputSerializer(many=True)


class ReverseSerializer(serializers.Serializer):
    reason = serializers.CharField()
    refund = serializers.BooleanField(default=False)


class ChequeSerializer(serializers.ModelSerializer):
    class Meta:
        model = Cheque
        fields = "__all__"
        read_only_fields = ("id", "status", "cleared_at", "bounced_at", "created_at")


class ChequeCreateSerializer(serializers.Serializer):
    payer = serializers.UUIDField()
    drawer_name = serializers.CharField()
    bank_name = serializers.CharField()
    cheque_number = serializers.CharField()
    amount = serializers.DecimalField(max_digits=15, decimal_places=2)
    cheque_date = serializers.DateField()
    lease = serializers.UUIDField(required=False, allow_null=True)


class ChequeDepositSerializer(serializers.Serializer):
    account = serializers.UUIDField()


class ChequeClearSerializer(serializers.Serializer):
    allocations = AllocationInputSerializer(many=True, required=False)


class ChequeBounceSerializer(serializers.Serializer):
    reason = serializers.CharField()


class ChequeReplaceSerializer(serializers.Serializer):
    cheque_number = serializers.CharField()
    amount = serializers.DecimalField(max_digits=15, decimal_places=2)
    cheque_date = serializers.DateField()


# --- Phase C ---------------------------------------------------------------------------------


class CommissionPlanSerializer(serializers.ModelSerializer):
    class Meta:
        from ..models import CommissionPlan

        model = CommissionPlan
        fields = "__all__"
        read_only_fields = ("id", "created_at", "updated_at")


class CommissionSplitSerializer(serializers.Serializer):
    recipient_type = serializers.CharField()
    recipient_user = serializers.UUIDField(required=False, allow_null=True)
    recipient_contact = serializers.UUIDField(required=False, allow_null=True)
    percentage = serializers.DecimalField(max_digits=7, decimal_places=4)


class CommissionSerializer(serializers.ModelSerializer):
    splits = serializers.SerializerMethodField()

    class Meta:
        from ..models import Commission

        model = Commission
        fields = "__all__"
        read_only_fields = (
            "id", "status", "gross_commission", "tax_amount", "net_commission",
            "approved_by", "approved_at", "paid_at", "created_at", "updated_at",
        )

    @extend_schema_field(serializers.ListField(child=serializers.DictField()))
    def get_splits(self, obj):
        return [
            {
                "recipient_type": split.recipient_type,
                "recipient_user": (
                    str(split.recipient_user_id) if split.recipient_user_id else None
                ),
                "recipient_contact": (
                    str(split.recipient_contact_id) if split.recipient_contact_id else None
                ),
                "percentage": str(split.percentage),
                "amount": str(split.amount),
            }
            for split in obj.splits.all()
        ]


class CommissionCreateSerializer(serializers.Serializer):
    """Exactly one of `transaction`/`lease` — the model's XOR, surfaced."""

    transaction = serializers.UUIDField(required=False, allow_null=True)
    lease = serializers.UUIDField(required=False, allow_null=True)
    plan = serializers.UUIDField(required=False, allow_null=True)
    agent = serializers.UUIDField(required=False, allow_null=True)
    deductions = serializers.DecimalField(
        max_digits=15, decimal_places=2, required=False, default=0
    )
    net_amount = serializers.DecimalField(
        max_digits=15, decimal_places=2, required=False, allow_null=True
    )

    def validate(self, attrs):
        if bool(attrs.get("transaction")) == bool(attrs.get("lease")):
            raise serializers.ValidationError(
                "A commission comes from exactly one of a transaction or a lease."
            )
        return attrs


class SetSplitsSerializer(serializers.Serializer):
    splits = CommissionSplitSerializer(many=True)


class ReasonSerializer(serializers.Serializer):
    reason = serializers.CharField()


class PayCommissionSerializer(serializers.Serializer):
    account = serializers.UUIDField()


class CommissionInvoiceSerializer(serializers.Serializer):
    contact = serializers.UUIDField()
    due_date = serializers.DateField()


class MilestoneInputSerializer(serializers.Serializer):
    label = serializers.CharField()
    due_date = serializers.DateField()
    amount = serializers.DecimalField(
        max_digits=15, decimal_places=2, required=False, allow_null=True
    )
    percentage = serializers.DecimalField(
        max_digits=7, decimal_places=4, required=False, allow_null=True
    )
    sort_order = serializers.IntegerField(required=False)


class InstallmentPlanSerializer(serializers.ModelSerializer):
    milestones = serializers.SerializerMethodField()

    class Meta:
        from ..models import InstallmentPlan

        model = InstallmentPlan
        fields = "__all__"
        read_only_fields = ("id", "status", "created_at", "updated_at")

    @extend_schema_field(serializers.ListField(child=serializers.DictField()))
    def get_milestones(self, obj):
        return [
            {
                "id": str(m.pk), "label": m.label, "due_date": str(m.due_date),
                "amount": str(m.amount), "status": m.status,
                "invoice": str(m.invoice_id) if m.invoice_id else None,
            }
            for m in obj.milestones.all()
        ]


class InstallmentPlanCreateSerializer(serializers.Serializer):
    transaction = serializers.UUIDField()
    name = serializers.CharField()
    currency = serializers.CharField(max_length=3)
    total_amount = serializers.DecimalField(max_digits=15, decimal_places=2)
    milestones = MilestoneInputSerializer(many=True)


class MilestoneActionSerializer(serializers.Serializer):
    milestone = serializers.UUIDField()
    contact = serializers.UUIDField(required=False, allow_null=True)
    due_date = serializers.DateField(required=False, allow_null=True)
    reason = serializers.CharField(required=False, allow_blank=True)


class ExpenseSerializer(serializers.ModelSerializer):
    class Meta:
        from ..models import Expense

        model = Expense
        fields = "__all__"
        read_only_fields = (
            "id", "expense_number", "status", "approved_by", "owner_statement",
            "created_at", "updated_at",
        )


class ExpenseCreateSerializer(serializers.Serializer):
    category = serializers.CharField()
    description = serializers.CharField()
    amount = serializers.DecimalField(max_digits=15, decimal_places=2)
    expense_date = serializers.DateField()
    tax_amount = serializers.DecimalField(
        max_digits=15, decimal_places=2, required=False, default=0
    )
    property = serializers.UUIDField(required=False, allow_null=True)
    lease = serializers.UUIDField(required=False, allow_null=True)
    vendor_contact = serializers.UUIDField(required=False, allow_null=True)
    account = serializers.UUIDField(required=False, allow_null=True)
    is_billable_to_owner = serializers.BooleanField(default=False)


class OwnerStatementSerializer(serializers.ModelSerializer):
    lines = serializers.SerializerMethodField()

    class Meta:
        from ..models import OwnerStatement

        model = OwnerStatement
        fields = "__all__"
        read_only_fields = (
            "id", "statement_number", "status", "gross_rent_collected",
            "management_fees", "expenses_total", "other_deductions", "net_payable",
            "issued_at", "paid_at", "created_at", "updated_at",
        )

    @extend_schema_field(serializers.ListField(child=serializers.DictField()))
    def get_lines(self, obj):
        return [
            {
                "line_type": line.line_type, "description": line.description,
                "amount": str(line.amount), "occurred_on": str(line.occurred_on or ""),
                "reference_type": line.reference_type,
            }
            for line in obj.lines.all()
        ]


class StatementGenerateSerializer(serializers.Serializer):
    owner_contact = serializers.UUIDField()
    period_start = serializers.DateField()
    period_end = serializers.DateField()
    property = serializers.UUIDField(required=False, allow_null=True)


class StatementAdjustmentSerializer(serializers.Serializer):
    description = serializers.CharField()
    amount = serializers.DecimalField(max_digits=15, decimal_places=2)
    line_type = serializers.CharField(required=False, default="DEDUCTION")


class ReconciliationSerializer(serializers.ModelSerializer):
    class Meta:
        from ..models import Reconciliation

        model = Reconciliation
        fields = "__all__"
        read_only_fields = (
            "id", "status", "closing_balance_system", "difference",
            "reconciled_by", "reconciled_at", "created_at", "updated_at",
        )


class ReconciliationCreateSerializer(serializers.Serializer):
    account = serializers.UUIDField()
    period_start = serializers.DateField()
    period_end = serializers.DateField()
    opening_balance = serializers.DecimalField(max_digits=15, decimal_places=2)
    closing_balance_statement = serializers.DecimalField(max_digits=15, decimal_places=2)
