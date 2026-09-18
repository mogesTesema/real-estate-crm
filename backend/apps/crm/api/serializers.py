"""Serializers for the crm API.

Write serializers validate shape only; the lead engine and the deal state machine live in
`crm.services`, so the same rules hold from a shell, a management command and an import.
"""
from drf_spectacular.utils import extend_schema_field
from rest_framework import serializers

from apps.core.serializers import UserSummarySerializer

from ..models import (
    Deal,
    DealProperty,
    DealStageHistory,
    Lead,
    LeadAssignment,
    LeadLocationPreference,
    LeadRoutingRule,
    LeadSource,
    LeadStatusHistory,
    Pipeline,
    PipelineStage,
)


class LeadSourceSerializer(serializers.ModelSerializer):
    class Meta:
        model = LeadSource
        fields = ("id", "name", "source_type", "description", "is_active")


class ContactSummarySerializer(serializers.Serializer):
    """The contact on a lead or deal card.

    Hand-written rather than imported from `contacts.api.serializers`: §1.2 makes every app's
    `api` package private and `lint-imports` enforces it.
    """

    id = serializers.UUIDField(read_only=True)
    display_name = serializers.CharField(source="__str__", read_only=True)
    email = serializers.CharField(read_only=True)
    phone = serializers.CharField(read_only=True)


class LeadLocationSerializer(serializers.ModelSerializer):
    class Meta:
        model = LeadLocationPreference
        fields = ("id", "location", "latitude", "longitude", "radius_km")
        read_only_fields = ("id",)


class LeadStatusHistorySerializer(serializers.ModelSerializer):
    changed_by = UserSummarySerializer(read_only=True)

    class Meta:
        model = LeadStatusHistory
        fields = ("id", "from_status", "to_status", "changed_by", "reason", "changed_at")


class LeadAssignmentSerializer(serializers.ModelSerializer):
    from_user = UserSummarySerializer(read_only=True)
    to_user = UserSummarySerializer(read_only=True)
    assigned_by = UserSummarySerializer(read_only=True)

    class Meta:
        model = LeadAssignment
        fields = ("id", "from_user", "to_user", "assigned_by", "reason", "assigned_at")


class LeadSerializer(serializers.ModelSerializer):
    contact = ContactSummarySerializer(read_only=True)
    assigned_agent = UserSummarySerializer(read_only=True)
    source = LeadSourceSerializer(read_only=True)
    location_preferences = LeadLocationSerializer(many=True, read_only=True)
    sla_state = serializers.SerializerMethodField()

    class Meta:
        model = Lead
        exclude = ("deleted_at",)
        read_only_fields = (
            "id", "status", "score", "sla_due_at", "sla_breached", "first_response_at",
            "acknowledged", "is_possible_duplicate", "converted_at", "created_at", "updated_at",
        )

    @extend_schema_field(serializers.CharField())
    def get_sla_state(self, obj):
        """One word the UI can colour a row by, instead of recomputing the rule client-side."""
        if obj.first_response_at:
            return "ANSWERED"
        if obj.sla_breached:
            return "BREACHED"
        return "PENDING"


class ContactInputSerializer(serializers.Serializer):
    """Contact details arriving with a lead, for a person who may not be in the CRM yet."""

    first_name = serializers.CharField(required=False, allow_blank=True)
    last_name = serializers.CharField(required=False, allow_blank=True)
    company_name = serializers.CharField(required=False, allow_blank=True)
    email = serializers.CharField(required=False, allow_blank=True)
    phone = serializers.CharField(required=False, allow_blank=True)
    national_id = serializers.CharField(required=False, allow_blank=True)
    contact_type = serializers.CharField(required=False, allow_blank=True)


class LeadCaptureSerializer(serializers.Serializer):
    """`POST /leads/` — the one way a lead enters the system.

    Either `contact` (an existing id) or `contact_details`. Everything SRS 3.1 guarantees —
    de-duplication, scoring, routing, the SLA clock, the acknowledgment — happens behind this,
    which is why there is no second create path.
    """

    contact = serializers.UUIDField(required=False)
    contact_details = ContactInputSerializer(required=False)
    lead_type = serializers.ChoiceField(choices=Lead.LeadType.choices)
    title = serializers.CharField(max_length=200)
    description = serializers.CharField(required=False, allow_blank=True)
    source = serializers.UUIDField(required=False, allow_null=True)
    campaign = serializers.UUIDField(required=False, allow_null=True)
    target_property = serializers.UUIDField(required=False, allow_null=True)
    priority = serializers.ChoiceField(choices=Lead.Priority.choices, required=False)
    budget_min = serializers.DecimalField(max_digits=15, decimal_places=2, required=False)
    budget_max = serializers.DecimalField(max_digits=15, decimal_places=2, required=False)
    preferred_property_type = serializers.CharField(required=False, allow_blank=True)
    preferred_bedrooms = serializers.IntegerField(required=False)
    preferred_bathrooms = serializers.IntegerField(required=False)
    financing_status = serializers.CharField(required=False, allow_blank=True)
    expected_timeframe = serializers.CharField(required=False, allow_blank=True)
    locations = LeadLocationSerializer(many=True, required=False)
    custom_data = serializers.DictField(required=False)

    def validate(self, attrs):
        if not attrs.get("contact") and not attrs.get("contact_details"):
            raise serializers.ValidationError(
                "Pass either `contact` (an existing contact id) or `contact_details`."
            )
        return attrs


class LeadUpdateSerializer(serializers.ModelSerializer):
    class Meta:
        model = Lead
        fields = (
            "title", "description", "priority", "budget_min", "budget_max",
            "preferred_property_type", "target_property", "preferred_location",
            "preferred_bedrooms", "preferred_bathrooms", "financing_status",
            "expected_timeframe", "next_follow_up_at", "custom_data",
        )


class LeadStatusSerializer(serializers.Serializer):
    status = serializers.ChoiceField(choices=Lead.Status.choices)
    reason = serializers.CharField(required=False, allow_blank=True)


class LeadAssignSerializer(serializers.Serializer):
    """Exactly one of the two — a person, or a team pool."""

    to_user = serializers.UUIDField(required=False)
    to_team = serializers.UUIDField(required=False)
    reason = serializers.CharField(required=False, allow_blank=True)

    def validate(self, attrs):
        if bool(attrs.get("to_user")) == bool(attrs.get("to_team")):
            raise serializers.ValidationError("Assign to exactly one of to_user or to_team.")
        return attrs


class LeadRespondSerializer(serializers.Serializer):
    note = serializers.CharField(required=False, allow_blank=True)


class LeadConvertSerializer(serializers.Serializer):
    pipeline = serializers.UUIDField(required=False)
    stage = serializers.UUIDField(required=False)
    owner = serializers.UUIDField(required=False)
    title = serializers.CharField(required=False)
    estimated_value = serializers.DecimalField(
        max_digits=15, decimal_places=2, required=False
    )
    currency = serializers.CharField(required=False)
    expected_close_date = serializers.DateField(required=False)


class RoutingRuleSerializer(serializers.ModelSerializer):
    class Meta:
        model = LeadRoutingRule
        fields = (
            "id", "name", "priority", "is_active", "criteria",
            "assign_to_user", "assign_to_team", "round_robin", "created_at",
        )
        read_only_fields = ("id", "created_at")


class PipelineStageSerializer(serializers.ModelSerializer):
    class Meta:
        model = PipelineStage
        fields = (
            "id", "name", "code", "sort_order", "probability",
            "is_won", "is_lost", "required_fields",
        )


class PipelineSerializer(serializers.ModelSerializer):
    stages = PipelineStageSerializer(many=True, read_only=True)

    class Meta:
        model = Pipeline
        fields = ("id", "name", "pipeline_type", "is_default", "is_active", "stages")


class DealPropertySerializer(serializers.ModelSerializer):
    property_title = serializers.CharField(source="property.title", read_only=True)

    class Meta:
        model = DealProperty
        fields = ("id", "property", "property_title", "unit", "is_primary", "created_at")
        read_only_fields = ("id", "created_at")


class DealStageHistorySerializer(serializers.ModelSerializer):
    changed_by = UserSummarySerializer(read_only=True)
    from_stage_name = serializers.CharField(source="from_stage.name", read_only=True)
    to_stage_name = serializers.CharField(source="to_stage.name", read_only=True)

    class Meta:
        model = DealStageHistory
        fields = (
            "id", "from_stage", "from_stage_name", "to_stage", "to_stage_name",
            "changed_by", "reason", "next_action", "changed_at",
        )


class DealSerializer(serializers.ModelSerializer):
    owner = UserSummarySerializer(read_only=True)
    primary_contact = ContactSummarySerializer(read_only=True)
    stage = PipelineStageSerializer(read_only=True)
    deal_properties = DealPropertySerializer(many=True, read_only=True)
    days_in_stage = serializers.SerializerMethodField()

    class Meta:
        model = Deal
        exclude = ("deleted_at",)
        read_only_fields = (
            "id", "reference_code", "status", "stage_entered_at", "actual_close_date",
            "lost_reason", "created_at", "updated_at",
        )

    @extend_schema_field(serializers.IntegerField(allow_null=True))
    def get_days_in_stage(self, obj):
        """A subtraction against `stage_entered_at` (v3.3), not a scan of the stage history.

        The old board issued one history query per card to answer this.
        """
        from django.utils import timezone

        if getattr(obj, "days_in_stage", None) is not None:
            return obj.days_in_stage
        return (timezone.now() - obj.stage_entered_at).days if obj.stage_entered_at else None


class DealWriteSerializer(serializers.ModelSerializer):
    class Meta:
        model = Deal
        fields = (
            "lead", "primary_contact", "pipeline", "stage", "owner", "title", "deal_type",
            "estimated_value", "currency", "expected_close_date", "next_action",
            "next_action_due_at", "custom_data",
        )


class DealUpdateSerializer(serializers.ModelSerializer):
    class Meta:
        model = Deal
        fields = (
            "primary_contact", "owner", "title", "deal_type", "estimated_value", "currency",
            "expected_close_date", "next_action", "next_action_due_at", "custom_data",
        )


class MoveStageSerializer(serializers.Serializer):
    """SRS 3.4.4 — the reason is mandatory, and `allow_blank=False` is the point."""

    stage = serializers.UUIDField()
    reason = serializers.CharField(allow_blank=False)
    next_action = serializers.CharField(required=False, allow_blank=True)


class LinkPropertySerializer(serializers.Serializer):
    property = serializers.UUIDField()
    unit = serializers.UUIDField(required=False, allow_null=True)
    is_primary = serializers.BooleanField(default=False)


class BoardQuerySerializer(serializers.Serializer):
    """Query parameters for the Kanban board.

    Typed rather than forwarded: a raw `?owner=not-a-uuid` reaches Django's UUID field and
    raises a bare `django.core.exceptions.ValidationError`, which DRF does not render — a 500
    from any authenticated user with a typo.
    """

    pipeline = serializers.UUIDField(required=False)
    owner = serializers.UUIDField(required=False)
    deal_type = serializers.CharField(required=False, max_length=50)
    currency = serializers.CharField(required=False, max_length=3)


class BoardStageSerializer(serializers.Serializer):
    stage = PipelineStageSerializer()
    deals = DealSerializer(many=True)
    count = serializers.IntegerField()
    value = serializers.DecimalField(max_digits=18, decimal_places=2)


class BoardSerializer(serializers.Serializer):
    pipeline = PipelineSerializer(allow_null=True)
    stages = BoardStageSerializer(many=True)
    weighted_forecast = serializers.DecimalField(max_digits=18, decimal_places=2)


# --- viewings (SRS 3.3.11) ---------------------------------------------------------------


class ViewingSerializer(serializers.ModelSerializer):
    agent = UserSummarySerializer(read_only=True)
    contact = ContactSummarySerializer(read_only=True)
    property_title = serializers.CharField(source="property.title", read_only=True)

    class Meta:
        from ..models import Viewing

        model = Viewing
        fields = "__all__"
        read_only_fields = (
            "id", "status", "check_in_at", "check_out_at", "check_in_latitude",
            "check_in_longitude", "feedback", "rating", "created_at", "updated_at",
        )


class ViewingScheduleSerializer(serializers.Serializer):
    property = serializers.UUIDField()
    contact = serializers.UUIDField()
    agent = serializers.UUIDField(required=False)
    lead = serializers.UUIDField(required=False, allow_null=True)
    deal = serializers.UUIDField(required=False, allow_null=True)
    scheduled_start = serializers.DateTimeField()
    scheduled_end = serializers.DateTimeField()
    location = serializers.CharField(required=False, allow_blank=True)


class ViewingRescheduleSerializer(serializers.Serializer):
    scheduled_start = serializers.DateTimeField()
    scheduled_end = serializers.DateTimeField()
    location = serializers.CharField(required=False, allow_blank=True)


class ViewingCompleteSerializer(serializers.Serializer):
    """SRS 3.3.11 — "record viewing feedback and ratings". Captured on the call that closes
    the appointment, rather than left to a separate step nobody takes."""

    status = serializers.CharField(required=False)
    feedback = serializers.CharField(required=False, allow_blank=True)
    rating = serializers.IntegerField(required=False, min_value=1, max_value=5)


class CheckInSerializer(serializers.Serializer):
    latitude = serializers.DecimalField(max_digits=9, decimal_places=6, required=False)
    longitude = serializers.DecimalField(max_digits=9, decimal_places=6, required=False)


# --- GPS field tracking (SRS 3.16.5–3.16.7) -----------------------------------------------


class LocationPointSerializer(serializers.Serializer):
    recorded_at = serializers.DateTimeField(required=False)
    latitude = serializers.DecimalField(max_digits=9, decimal_places=6)
    longitude = serializers.DecimalField(max_digits=9, decimal_places=6)
    accuracy_m = serializers.DecimalField(max_digits=8, decimal_places=2, required=False)


class LocationPointReadSerializer(serializers.Serializer):
    id = serializers.UUIDField(read_only=True)
    recorded_at = serializers.DateTimeField(read_only=True)
    latitude = serializers.DecimalField(max_digits=9, decimal_places=6, read_only=True)
    longitude = serializers.DecimalField(max_digits=9, decimal_places=6, read_only=True)
    accuracy_m = serializers.DecimalField(max_digits=8, decimal_places=2, read_only=True)


class FieldSessionSerializer(serializers.ModelSerializer):
    agent = UserSummarySerializer(read_only=True)
    point_count = serializers.SerializerMethodField()

    class Meta:
        from ..models import AgentFieldSession

        model = AgentFieldSession
        fields = "__all__"
        read_only_fields = ("id", "status", "ended_at", "created_at", "updated_at")

    @extend_schema_field(serializers.IntegerField())
    def get_point_count(self, obj):
        """The count, not the points. A list of sessions must not stream every breadcrumb of
        every employee's day — the trail itself needs the audited detail endpoint."""
        return obj.location_points.count()


class StartFieldSessionSerializer(serializers.Serializer):
    session_type = serializers.CharField()
    gps_enabled = serializers.BooleanField(default=False)
    gps_required = serializers.BooleanField(default=True)
    lead = serializers.UUIDField(required=False, allow_null=True)
    deal = serializers.UUIDField(required=False, allow_null=True)
    property = serializers.UUIDField(required=False, allow_null=True)
    viewing = serializers.UUIDField(required=False, allow_null=True)
    notes = serializers.CharField(required=False, allow_blank=True)


class EndFieldSessionSerializer(serializers.Serializer):
    notes = serializers.CharField(required=False, allow_blank=True)


class AppendPointsSerializer(serializers.Serializer):
    points = LocationPointSerializer(many=True)
