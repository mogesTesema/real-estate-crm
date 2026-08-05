from django.contrib.contenttypes.models import ContentType
from rest_framework import serializers

from .models import Activity


class ActivitySerializer(serializers.ModelSerializer):
    related_type = serializers.SerializerMethodField()
    related_model = serializers.CharField(write_only=True, required=False)

    class Meta:
        model = Activity
        fields = [
            "id",
            "activity_type",
            "body",
            "actor",
            "content_type",
            "object_id",
            "related_type",
            "related_model",
            "due_at",
            "completed_at",
            "at",
        ]
        read_only_fields = ["at", "related_type", "actor"]

    def get_related_type(self, obj) -> str | None:
        return obj.content_type.model if obj.content_type_id else None

    def create(self, validated_data):
        related_model = validated_data.pop("related_model", None)
        if related_model and validated_data.get("object_id"):
            model_map = {
                "opportunity": ("deals", "opportunity"),
                "contact": ("contacts", "contact"),
                "property": ("properties", "property"),
                "lead": ("leads", "lead"),
            }
            app_label, model = model_map.get(related_model, ("deals", related_model))
            validated_data["content_type"] = ContentType.objects.get(
                app_label=app_label, model=model
            )
        return super().create(validated_data)
