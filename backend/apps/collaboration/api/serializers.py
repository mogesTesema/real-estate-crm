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


# --- Phase D ---------------------------------------------------------------------------------

from apps.core.serializers import ContactSummarySerializer, UserSummarySerializer  # noqa: E402

from ..models import (  # noqa: E402
    AccessGrant,
    Activity,
    CallLog,
    Document,
    DocumentLink,
    EsignEnvelope,
    EsignSigner,
    InternalNote,
    Message,
    SignatureEvent,
    Template,
    Thread,
)


class DocumentLinkSerializer(serializers.ModelSerializer):
    #: Optional at the API too — the service defaults it to ATTACHMENT.
    relationship_type = serializers.CharField(required=False, allow_blank=True)

    class Meta:
        model = DocumentLink
        fields = (
            "id", "contact", "property", "deal", "lease", "transaction", "thread",
            "message", "relationship_type",
        )
        read_only_fields = ("id",)


class AccessGrantSerializer(serializers.ModelSerializer):
    class Meta:
        model = AccessGrant
        fields = ("id", "role", "user", "access_level", "granted_by", "created_at")
        read_only_fields = ("id", "granted_by", "created_at")


class DocumentSerializer(serializers.ModelSerializer):
    file = FileSerializer(read_only=True)
    uploaded_by = UserSummarySerializer(read_only=True)
    links = DocumentLinkSerializer(many=True, read_only=True)
    access_grants = AccessGrantSerializer(many=True, read_only=True)

    class Meta:
        model = Document
        exclude = ("deleted_at",)
        read_only_fields = ("id", "version", "created_at", "updated_at")


class DocumentCreateSerializer(serializers.Serializer):
    file = serializers.UUIDField()
    title = serializers.CharField(max_length=255)
    document_type = serializers.ChoiceField(choices=Document.DocumentType.choices)
    is_confidential = serializers.BooleanField(default=False)
    links = DocumentLinkSerializer(many=True)


class NewVersionSerializer(serializers.Serializer):
    file = serializers.UUIDField()


class GrantSerializer(serializers.Serializer):
    role = serializers.UUIDField(required=False, allow_null=True)
    user = serializers.UUIDField(required=False, allow_null=True)
    access_level = serializers.ChoiceField(choices=AccessGrant.AccessLevel.choices)


class GenerateDocumentSerializer(serializers.Serializer):
    template = serializers.UUIDField()
    title = serializers.CharField(max_length=255)
    document_type = serializers.ChoiceField(choices=Document.DocumentType.choices)
    links = DocumentLinkSerializer(many=True)
    contact = serializers.UUIDField(required=False, allow_null=True)
    deal = serializers.UUIDField(required=False, allow_null=True)


class SignatureEventSerializer(serializers.ModelSerializer):
    class Meta:
        model = SignatureEvent
        fields = (
            "id", "event_type", "signer", "ip_address", "user_agent", "metadata",
            "occurred_at",
        )


class EsignSignerSerializer(serializers.ModelSerializer):
    class Meta:
        model = EsignSigner
        fields = (
            "id", "contact", "user", "name", "email", "signing_order", "role", "status",
            "signed_at",
        )


class EsignSignerInputSerializer(serializers.Serializer):
    contact = serializers.UUIDField(required=False, allow_null=True)
    user = serializers.UUIDField(required=False, allow_null=True)
    name = serializers.CharField(required=False, allow_blank=True)
    email = serializers.EmailField(required=False, allow_blank=True)
    signing_order = serializers.IntegerField(default=1, min_value=1)
    role = serializers.ChoiceField(choices=EsignSigner.Role.choices, default="SIGNER")


class EsignEnvelopeSerializer(serializers.ModelSerializer):
    signers = EsignSignerSerializer(many=True, read_only=True)
    signature_events = SignatureEventSerializer(many=True, read_only=True)

    class Meta:
        model = EsignEnvelope
        fields = "__all__"
        read_only_fields = ("id", "status", "sent_at", "completed_at", "created_at")


class EsignEnvelopeCreateSerializer(serializers.Serializer):
    document = serializers.UUIDField()
    subject = serializers.CharField(max_length=255)
    message = serializers.CharField(required=False, allow_blank=True)
    expires_at = serializers.DateTimeField(required=False, allow_null=True)
    signers = EsignSignerInputSerializer(many=True)


class PublicSignSerializer(serializers.Serializer):
    typed_name = serializers.CharField(max_length=200)
    consent = serializers.BooleanField()


class PublicDeclineSerializer(serializers.Serializer):
    reason = serializers.CharField()


class ActivitySerializer(serializers.ModelSerializer):
    assigned_to = UserSummarySerializer(read_only=True)

    class Meta:
        model = Activity
        fields = "__all__"
        read_only_fields = (
            "id", "status", "completed_at", "reminder_sent_at", "source_type", "source_id",
        )


class _ActiveUsers:
    """Lazy queryset holder: resolving at import would race app loading."""

    def all(self):
        from apps.identity.models import User

        return User.objects.filter(is_active=True)

    def __iter__(self):
        return iter(self.all())

    def get(self, **kwargs):
        return self.all().get(**kwargs)

    @property
    def model(self):  # spectacular introspects queryset.model for the schema
        from apps.identity.models import User

        return User


class ActivityWriteSerializer(serializers.ModelSerializer):
    #: Optional — the service defaults an unassigned task to its creator.
    assigned_to = serializers.PrimaryKeyRelatedField(
        queryset=_ActiveUsers(), required=False, allow_null=True
    )

    class Meta:
        model = Activity
        fields = (
            "activity_type", "subject", "description", "assigned_to", "start_at", "due_at",
            "recurrence_rule", "reminder_minutes_before", "priority",
            "contact", "lead", "deal", "property", "lease",
        )


class ThreadSerializer(serializers.ModelSerializer):
    assigned_to = UserSummarySerializer(read_only=True)
    contact = ContactSummarySerializer(read_only=True)

    class Meta:
        model = Thread
        fields = "__all__"
        read_only_fields = ("id", "channel", "last_message_at", "is_closed", "created_at")


class MessageSerializer(serializers.ModelSerializer):
    sender_user = UserSummarySerializer(read_only=True)

    class Meta:
        model = Message
        fields = "__all__"
        read_only_fields = fields = tuple(
            f.name for f in Message._meta.fields
        )  # messages are immutable


class SendMessageSerializer(serializers.Serializer):
    channel = serializers.ChoiceField(choices=Message.Channel.choices)
    contact = serializers.UUIDField(required=False, allow_null=True)
    thread = serializers.UUIDField(required=False, allow_null=True)
    to_address = serializers.CharField(required=False, allow_blank=True)
    template = serializers.UUIDField(required=False, allow_null=True)
    subject = serializers.CharField(required=False, allow_blank=True)
    body = serializers.CharField(required=False, allow_blank=True)


class CallLogSerializer(serializers.ModelSerializer):
    user = UserSummarySerializer(read_only=True)

    class Meta:
        model = CallLog
        fields = "__all__"
        read_only_fields = ("id", "user", "created_at")


class InternalNoteSerializer(serializers.ModelSerializer):
    author = UserSummarySerializer(read_only=True)

    class Meta:
        model = InternalNote
        fields = "__all__"
        read_only_fields = ("id", "author", "created_at", "updated_at")


class TemplateSerializer(serializers.ModelSerializer):
    class Meta:
        model = Template
        fields = "__all__"
        read_only_fields = ("id", "created_by", "created_at", "updated_at")
