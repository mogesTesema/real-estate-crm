"""Serializers for the collaboration API.

Grows per aggregate. Phase A ships files + notifications; documents, e-sign, activities and
communications land in their own pass.
"""
from rest_framework import serializers

from ..models import File, Notification, NotificationPreference


class FileSerializer(serializers.ModelSerializer):
    """What a client learns about a stored file.

    `storage_key` is deliberately absent: it is an internal pointer into the bucket, and
    handing it out invites clients to build URLs that bypass the access gate. Bytes come
    back only through the download endpoint.
    """

    class Meta:
        model = File
        fields = (
            "id",
            "original_name",
            "mime_type",
            "size_bytes",
            "checksum",
            "is_encrypted",
            "created_at",
        )
        read_only_fields = fields


class FileUploadSerializer(serializers.Serializer):
    file = serializers.FileField()
    is_encrypted = serializers.BooleanField(default=False)


class NotificationSerializer(serializers.ModelSerializer):
    class Meta:
        model = Notification
        fields = (
            "id",
            "type",
            "title",
            "body",
            "entity_type",
            "entity_id",
            "is_read",
            "read_at",
            "created_at",
        )
        read_only_fields = fields


class NotificationPreferenceSerializer(serializers.ModelSerializer):
    class Meta:
        model = NotificationPreference
        fields = (
            "notification_type",
            "in_app_enabled",
            "email_enabled",
            "sms_enabled",
            "push_enabled",
            "quiet_hours_start",
            "quiet_hours_end",
            "timezone",
        )


class NotificationPreferenceWriteSerializer(serializers.Serializer):
    in_app_enabled = serializers.BooleanField(required=False)
    email_enabled = serializers.BooleanField(required=False)
    sms_enabled = serializers.BooleanField(required=False)
    push_enabled = serializers.BooleanField(required=False)
    quiet_hours_start = serializers.TimeField(required=False, allow_null=True)
    quiet_hours_end = serializers.TimeField(required=False, allow_null=True)
    timezone = serializers.CharField(required=False, allow_null=True, allow_blank=True)
