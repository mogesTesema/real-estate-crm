from rest_framework import serializers

from .models import Activity


class ActivitySerializer(serializers.ModelSerializer):
    related_type = serializers.SerializerMethodField()

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
            "due_at",
            "completed_at",
            "at",
        ]
        read_only_fields = ["at", "related_type"]

    def get_related_type(self, obj) -> str | None:
        return obj.content_type.model if obj.content_type_id else None
