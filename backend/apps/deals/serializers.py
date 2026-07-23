from rest_framework import serializers

from apps.core.serializers import CustomFieldsValidationMixin

from .models import Opportunity, OpportunityStageHistory, Pipeline, Stage


class StageSerializer(serializers.ModelSerializer):
    class Meta:
        model = Stage
        fields = ["id", "pipeline", "name", "order", "probability", "is_won", "is_lost"]


class PipelineSerializer(serializers.ModelSerializer):
    stages = StageSerializer(many=True, read_only=True)

    class Meta:
        model = Pipeline
        fields = ["id", "name", "business_line", "is_default", "stages"]


class OpportunityStageHistorySerializer(serializers.ModelSerializer):
    class Meta:
        model = OpportunityStageHistory
        fields = [
            "id",
            "from_stage",
            "to_stage",
            "changed_by",
            "reason",
            "next_action",
            "at",
        ]


class OpportunitySerializer(CustomFieldsValidationMixin, serializers.ModelSerializer):
    custom_fields_model_label = "deals.Opportunity"
    weighted_value = serializers.ReadOnlyField()
    stage_history = OpportunityStageHistorySerializer(many=True, read_only=True)

    class Meta:
        model = Opportunity
        fields = [
            "id",
            "title",
            "pipeline",
            "stage",
            "status",
            "contact",
            "property",
            "value",
            "currency",
            "expected_close",
            "probability",
            "lost_reason",
            "assigned_agent",
            "weighted_value",
            "stage_history",
            "custom_fields",
            "created_at",
        ]
        # Stage moves go through /move_stage so history + reason are enforced.
        read_only_fields = ["status", "lost_reason", "created_at"]


class MoveStageSerializer(serializers.Serializer):
    to_stage = serializers.UUIDField()
    reason = serializers.CharField()
    next_action = serializers.CharField(required=False, allow_blank=True)
