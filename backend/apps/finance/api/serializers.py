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
