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


# --- Phase C ---------------------------------------------------------------------------------


class InspectionSerializer(serializers.ModelSerializer):
    performed_by = UserSummarySerializer(read_only=True)

    class Meta:
        from ..models import Inspection

        model = Inspection
        fields = "__all__"
        read_only_fields = ("id", "status", "completed_date", "property")


class InspectionWriteSerializer(serializers.Serializer):
    lease = serializers.UUIDField()
    inspection_type = serializers.CharField()
    scheduled_date = serializers.DateTimeField()
    performed_by = serializers.UUIDField()


class InspectionRescheduleSerializer(serializers.Serializer):
    scheduled_date = serializers.DateTimeField()
    performed_by = serializers.UUIDField(required=False)


class InspectionCompleteSerializer(serializers.Serializer):
    condition_summary = serializers.CharField(required=False, allow_blank=True)
    meter_readings = serializers.DictField(required=False)


class ApplicationSerializer(serializers.ModelSerializer):
    applicant = ContactSummarySerializer(source="applicant_contact", read_only=True)

    class Meta:
        from ..models import Application

        model = Application
        fields = "__all__"
        read_only_fields = (
            "id", "status", "decided_by", "decided_at", "screening_result",
            "created_at", "updated_at",
        )


class ApplicationWriteSerializer(serializers.ModelSerializer):
    class Meta:
        from ..models import Application

        model = Application
        fields = (
            "property", "unit", "applicant_contact", "assigned_to", "proposed_rent",
            "stated_income", "desired_move_in", "employment_status",
        )


class ScreeningSerializer(serializers.Serializer):
    background_check_status = serializers.CharField(required=False)
    credit_check_status = serializers.CharField(required=False)
    screening_result = serializers.DictField(required=False)


class DecisionSerializer(serializers.Serializer):
    decision = serializers.CharField()
    reason = serializers.CharField(required=False, allow_blank=True)


class ConvertApplicationSerializer(serializers.Serializer):
    end_date = serializers.DateField()
    start_date = serializers.DateField(required=False)
    rent_amount = serializers.DecimalField(max_digits=15, decimal_places=2, required=False)
    security_deposit = serializers.DecimalField(
        max_digits=15, decimal_places=2, required=False
    )
    landlord = serializers.UUIDField(required=False)


class RenewalSerializer(serializers.ModelSerializer):
    class Meta:
        from ..models import Renewal

        model = Renewal
        fields = "__all__"
        read_only_fields = (
            "id", "status", "new_lease", "current_rent", "decided_at", "notice_sent_at",
            "created_at", "updated_at",
        )


class ProposeRenewalSerializer(serializers.Serializer):
    escalation_type = serializers.CharField(required=False, default="NONE")
    escalation_value = serializers.DecimalField(
        max_digits=15, decimal_places=2, required=False, allow_null=True
    )
    proposed_start_date = serializers.DateField(required=False)
    proposed_end_date = serializers.DateField(required=False)
    proposed_rent = serializers.DecimalField(
        max_digits=15, decimal_places=2, required=False
    )
    send_notice = serializers.BooleanField(default=True)


class VendorSerializer(serializers.ModelSerializer):
    contact_name = serializers.CharField(source="contact.__str__", read_only=True)

    class Meta:
        from ..models import Vendor

        model = Vendor
        fields = ("id", "contact", "contact_name", "service_category", "license_number",
                  "rating", "is_active")
        read_only_fields = ("id", "rating")


class RateVendorSerializer(serializers.Serializer):
    rating = serializers.DecimalField(max_digits=3, decimal_places=2)


class MaintenanceRequestSerializer(serializers.ModelSerializer):
    reported_by = serializers.SerializerMethodField()

    class Meta:
        from ..models import MaintenanceRequest

        model = MaintenanceRequest
        fields = "__all__"
        read_only_fields = (
            "id", "status", "reported_by_contact", "reported_by_user", "actual_cost",
            "resolved_at", "requested_at",
        )

    def get_reported_by(self, obj) -> str:
        if obj.reported_by_user_id:
            return obj.reported_by_user.full_name
        if obj.reported_by_contact_id:
            return str(obj.reported_by_contact)
        return ""


class MaintenanceCreateSerializer(serializers.Serializer):
    property = serializers.UUIDField()
    unit = serializers.UUIDField(required=False, allow_null=True)
    lease = serializers.UUIDField(required=False, allow_null=True)
    title = serializers.CharField(max_length=200)
    description = serializers.CharField()
    priority = serializers.CharField(required=False, default="MEDIUM")


class MaintenanceAssignSerializer(serializers.Serializer):
    vendor = serializers.UUIDField(required=False, allow_null=True)
    assignee = serializers.UUIDField(required=False, allow_null=True)


class MaintenanceStatusSerializer(serializers.Serializer):
    status = serializers.CharField()
    note = serializers.CharField(required=False, allow_blank=True)


class WorkOrderSerializer(serializers.ModelSerializer):
    class Meta:
        from ..models import WorkOrder

        model = WorkOrder
        fields = "__all__"
        read_only_fields = (
            "id", "status", "quoted_amount", "approved_amount", "final_amount",
            "completed_at",
        )


class WorkOrderCreateSerializer(serializers.Serializer):
    maintenance_request = serializers.UUIDField()
    vendor = serializers.UUIDField()
    notes = serializers.CharField(required=False, allow_blank=True)
    scheduled_at = serializers.DateTimeField(required=False, allow_null=True)


class AmountSerializer(serializers.Serializer):
    amount = serializers.DecimalField(max_digits=15, decimal_places=2)


class WorkOrderCompleteSerializer(serializers.Serializer):
    final_amount = serializers.DecimalField(max_digits=15, decimal_places=2)
    bill_to_owner = serializers.BooleanField(default=False)
    account = serializers.UUIDField(required=False, allow_null=True)
    notes = serializers.CharField(required=False, allow_blank=True)


class ScheduleAtSerializer(serializers.Serializer):
    scheduled_at = serializers.DateTimeField()
