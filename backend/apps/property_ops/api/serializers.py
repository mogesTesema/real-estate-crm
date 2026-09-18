"""Serializers for the property_ops API.

Write serializers validate shape only; the lease state machine, the overlap guard and the
deposit arithmetic all live in `property_ops.services`.
"""
from rest_framework import serializers

from apps.core.serializers import ContactSummarySerializer, UserSummarySerializer

from ..models import Deposit, Lease, LeaseParty, RentSchedule


class LeasePartySerializer(serializers.ModelSerializer):
    contact_name = serializers.CharField(source="contact.__str__", read_only=True)

    class Meta:
        model = LeaseParty
        fields = ("id", "contact", "contact_name", "party_type", "share_percentage")
        read_only_fields = ("id",)


class RentScheduleSerializer(serializers.ModelSerializer):
    invoice_number = serializers.CharField(source="invoice.invoice_number", read_only=True)
    invoice_status = serializers.CharField(source="invoice.status", read_only=True)
    balance_due = serializers.DecimalField(
        source="invoice.balance_due", max_digits=15, decimal_places=2, read_only=True
    )

    class Meta:
        model = RentSchedule
        fields = (
            "id", "lease", "period_start", "period_end", "due_date", "amount",
            "late_fee_amount", "status", "invoice", "invoice_number", "invoice_status",
            "balance_due",
        )
        read_only_fields = fields


class DepositSerializer(serializers.ModelSerializer):
    class Meta:
        model = Deposit
        fields = (
            "id", "lease", "amount", "held_amount", "refunded_amount", "deducted_amount",
            "status", "received_date", "refunded_date", "notes",
        )
        read_only_fields = fields


class DepositMovementSerializer(serializers.Serializer):
    amount = serializers.DecimalField(max_digits=15, decimal_places=2, required=False)
    reason = serializers.CharField(required=False, allow_blank=True)
    notes = serializers.CharField(required=False, allow_blank=True)


class LeaseSerializer(serializers.ModelSerializer):
    tenant = ContactSummarySerializer(read_only=True)
    landlord = ContactSummarySerializer(read_only=True)
    property_manager = UserSummarySerializer(read_only=True)
    property_title = serializers.CharField(source="property.title", read_only=True)
    parties = LeasePartySerializer(many=True, read_only=True)

    class Meta:
        model = Lease
        fields = "__all__"
        read_only_fields = ("id", "reference_code", "status", "created_at", "updated_at")


class LeaseWriteSerializer(serializers.ModelSerializer):
    parties = LeasePartySerializer(many=True, required=False)

    class Meta:
        model = Lease
        fields = (
            "property", "unit", "tenant", "landlord", "property_manager", "lease_type",
            "start_date", "end_date", "rent_amount", "billing_frequency",
            "security_deposit", "management_fee", "notice_period_days", "terms", "parties",
        )


class LeaseStatusSerializer(serializers.Serializer):
    status = serializers.CharField()
    reason = serializers.CharField(required=False, allow_blank=True)


class LeaseActivateSerializer(serializers.Serializer):
    invoice_horizon_days = serializers.IntegerField(required=False, min_value=0)


class LeaseTerminateSerializer(serializers.Serializer):
    reason = serializers.CharField(required=False, allow_blank=True)


class WaiveSerializer(serializers.Serializer):
    reason = serializers.CharField()
