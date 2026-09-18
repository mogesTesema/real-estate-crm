"""Serializers for the platform API.

Read-only: everything here is computed from domain selectors, so there is nothing to validate
on the way in beyond the query parameters.
"""
from rest_framework import serializers

from ..models import AuditEvent
from ..selectors import DEFAULT_WINDOW_DAYS, REPORTS


class WindowSerializer(serializers.Serializer):
    """The reporting window.

    A dashboard without one reports the company's whole history, where a bad month is
    invisible against three good years. Capped at a year because these are live aggregates
    with no pre-computed snapshot behind them yet.
    """

    days = serializers.IntegerField(
        required=False, min_value=1, max_value=365, default=DEFAULT_WINDOW_DAYS
    )


class ReportQuerySerializer(WindowSerializer):
    # Named `export`, not `format`: DRF reserves `format` for content negotiation, so
    # `?format=csv` is read as a request for a renderer named csv and 404s before the view
    # ever runs.
    export = serializers.ChoiceField(choices=["json", "csv"], default="json")


class AuditEventSerializer(serializers.ModelSerializer):
    actor_email = serializers.CharField(source="actor_user.email", read_only=True)

    class Meta:
        model = AuditEvent
        fields = (
            "id", "action", "entity_type", "entity_id", "actor_user", "actor_email",
            "old_values", "new_values", "ip_address", "user_agent", "created_at",
        )


class ReportIndexSerializer(serializers.Serializer):
    name = serializers.CharField()
    accepts_window = serializers.BooleanField()


def report_index():
    return [
        {"name": name, "accepts_window": takes_window}
        for name, (_, takes_window) in sorted(REPORTS.items())
    ]


# --- Integration admin (SRS §3.10, 3.19.2) --------------------------------------------------

from rest_framework import serializers  # noqa: E402

from ..models import Connection, ExternalMapping, SyncLog, Webhook  # noqa: E402


class ConnectionSerializer(serializers.ModelSerializer):
    """`credentials_ref` is a secrets-manager pointer, never a secret — safe for the admins
    this surface is restricted to."""

    class Meta:
        model = Connection
        fields = (
            "id", "name", "provider", "direction", "auth_type", "credentials_ref",
            "config", "status", "last_sync_at", "last_error", "created_at", "updated_at",
        )
        read_only_fields = ("id", "last_sync_at", "last_error", "created_at", "updated_at")


class WebhookSerializer(serializers.ModelSerializer):
    #: Write-only: the HMAC secret goes in, it never comes back out.
    secret = serializers.CharField(
        write_only=True, required=False, allow_null=True, allow_blank=True
    )

    class Meta:
        model = Webhook
        fields = (
            "id", "connection", "direction", "event_type", "target_url", "secret",
            "is_active", "created_at", "updated_at",
        )
        read_only_fields = ("id", "created_at", "updated_at")


class ExternalMappingSerializer(serializers.ModelSerializer):
    class Meta:
        model = ExternalMapping
        fields = (
            "id", "connection", "entity_type", "local_id", "external_id",
            "last_synced_at", "sync_hash",
        )
        read_only_fields = fields


class SyncLogSerializer(serializers.ModelSerializer):
    class Meta:
        model = SyncLog
        fields = (
            "id", "connection", "entity_type", "direction", "status",
            "records_processed", "records_failed", "started_at", "finished_at",
            "error_detail", "payload_snapshot",
        )
        read_only_fields = fields


class SavedReportSerializer(serializers.ModelSerializer):
    class Meta:
        from ..models import SavedReport

        model = SavedReport
        fields = (
            "id", "name", "report_type", "definition", "visibility", "owner",
            "created_at", "updated_at",
        )
        read_only_fields = ("id", "owner", "created_at", "updated_at")

    def validate_definition(self, value):
        from django.core.exceptions import ValidationError as DjangoValidationError

        from ..reporting import validate_definition

        try:
            validate_definition(value)
        except DjangoValidationError as exc:
            detail = exc.message_dict if hasattr(exc, "message_dict") else exc.messages
            raise serializers.ValidationError(detail) from exc
        return value


class ReportScheduleSerializer(serializers.ModelSerializer):
    class Meta:
        from ..models import ReportSchedule

        model = ReportSchedule
        fields = (
            "id", "saved_report", "frequency", "next_run_at", "recipients",
            "export_format", "is_active", "last_run_at", "created_at",
        )
        read_only_fields = ("id", "last_run_at", "created_at")

    def validate_export_format(self, value):
        if value != "CSV":
            # PDF needs weasyprint, XLSX needs openpyxl — both documented in
            # third-part-needed.md, neither installed. Refusing beats a 2 AM crash.
            raise serializers.ValidationError(
                "Only CSV is available today; PDF/XLSX arrive with their libraries."
            )
        return value
