from rest_framework import serializers

from apps.core.serializers import CustomFieldsValidationMixin

from .models import Contact, ContactRole


class ContactRoleSerializer(serializers.ModelSerializer):
    class Meta:
        model = ContactRole
        fields = ["id", "role", "is_active"]


class ContactSerializer(CustomFieldsValidationMixin, serializers.ModelSerializer):
    custom_fields_model_label = "contacts.Contact"
    roles = serializers.SerializerMethodField()

    class Meta:
        model = Contact
        fields = [
            "id",
            "kind",
            "full_name",
            "company_name",
            "email",
            "phone",
            "extra_emails",
            "extra_phones",
            "source",
            "tags",
            "location_preference",
            "budget_min",
            "budget_max",
            "is_vip",
            "assigned_agent",
            "referred_by",
            "roles",
            "custom_fields",
            "created_at",
            "updated_at",
        ]
        read_only_fields = ["created_at", "updated_at"]

    def get_roles(self, obj) -> list[str]:
        return obj.roles


class MergeSerializer(serializers.Serializer):
    duplicate_id = serializers.UUIDField()
