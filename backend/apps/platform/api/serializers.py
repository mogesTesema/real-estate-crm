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
