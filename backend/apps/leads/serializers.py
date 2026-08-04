from rest_framework import serializers

from apps.core.serializers import CustomFieldsValidationMixin

from .models import Lead, LeadSource


class LeadSourceSerializer(serializers.ModelSerializer):
    class Meta:
        model = LeadSource
        fields = ["id", "key", "label", "is_active", "weight", "created_at"]
        read_only_fields = ["created_at"]


class LeadSerializer(CustomFieldsValidationMixin, serializers.ModelSerializer):
    custom_fields_model_label = "leads.Lead"

    class Meta:
        model = Lead
        fields = [
            "id",
            "name",
            "email",
            "phone",
            "contact",
            "lead_type",
            "source",
            "status",
            "score",
            "budget_min",
            "budget_max",
            "preferred_location",
            "bedrooms",
            "timeline",
            "notes",
            "assigned_agent",
            "sla_due_at",
            "sla_breached",
            "first_response_at",
            "acknowledged",
            "converted_opportunity",
            "is_possible_duplicate",
            "custom_fields",
            "created_at",
        ]
        # These are set by the capture/convert engine, not by the client.
        read_only_fields = [
            "contact",
            "score",
            "status",
            "assigned_agent",
            "sla_due_at",
            "sla_breached",
            "first_response_at",
            "acknowledged",
            "converted_opportunity",
            "is_possible_duplicate",
            "created_at",
        ]


class CaptureLeadSerializer(serializers.Serializer):
    name = serializers.CharField(required=False, allow_blank=True)
    email = serializers.EmailField(required=False, allow_blank=True)
    phone = serializers.CharField(required=False, allow_blank=True)
    lead_type = serializers.ChoiceField(choices=Lead.Type.choices)
    source = serializers.CharField(required=False, allow_blank=True)
    budget_min = serializers.DecimalField(
        max_digits=14, decimal_places=2, required=False, allow_null=True
    )
    budget_max = serializers.DecimalField(
        max_digits=14, decimal_places=2, required=False, allow_null=True
    )
    preferred_location = serializers.CharField(required=False, allow_blank=True)
    bedrooms = serializers.IntegerField(required=False, allow_null=True)
    timeline = serializers.CharField(required=False, allow_blank=True)
    notes = serializers.CharField(required=False, allow_blank=True)


class ConvertLeadSerializer(serializers.Serializer):
    pipeline = serializers.UUIDField()
    stage = serializers.UUIDField()


class AssignLeadSerializer(serializers.Serializer):
    assigned_agent = serializers.UUIDField()
