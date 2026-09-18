"""Views for the platform API (SRS §3.13, §5.3).

Read-only. Every figure is computed over an `apply_scope`d queryset reached through each
domain app's `selectors` module, so a dashboard cannot become the one place row visibility
does not apply.
"""
import csv

from django.http import StreamingHttpResponse
from drf_spectacular.utils import OpenApiResponse, extend_schema
from rest_framework import mixins, viewsets
from rest_framework.response import Response
from rest_framework.views import APIView

from .. import selectors
from ..models import AuditEvent
from .serializers import (
    AuditEventSerializer,
    ReportIndexSerializer,
    ReportQuerySerializer,
    WindowSerializer,
    report_index,
)


class _Echo:
    def write(self, value):
        return value


class DashboardView(APIView):
    """Role-aware KPIs (SRS 3.13.1).

    One code path for every role. The figures are the same questions for everyone — how many
    leads, how many converted, what is the pipeline worth — and the *scope* is what differs,
    which `apply_scope` already decides from the caller's `data_scope`. Branching on role here
    would be a second place for visibility rules to live, and the two would drift.
    """

    @extend_schema(
        parameters=[WindowSerializer],
        responses={200: OpenApiResponse(description="KPIs, scoped to the caller")},
    )
    def get(self, request):
        window = WindowSerializer(data=request.query_params)
        window.is_valid(raise_exception=True)
        return Response(
            selectors.dashboard(request.user, days=window.validated_data["days"])
        )


class ReportIndexView(APIView):
    """What reports exist. A separate class rather than a nullable `name` on the one below:
    two routes served by one method share an operationId, and drf-spectacular resolves the
    collision with a numeral suffix that moves whenever the routes are reordered — which
    renames a method in every regenerated client."""

    @extend_schema(responses={200: ReportIndexSerializer(many=True)})
    def get(self, request):
        return Response(ReportIndexSerializer(report_index(), many=True).data)


class ReportView(APIView):
    """Named reports (SRS 3.13.2), with CSV export (SRS 3.13.4)."""

    @extend_schema(
        parameters=[ReportQuerySerializer],
        responses={200: OpenApiResponse(description="Report rows, or text/csv")},
    )
    def get(self, request, name):
        query = ReportQuerySerializer(data=request.query_params)
        query.is_valid(raise_exception=True)
        try:
            data = selectors.run_report(
                name, request.user, days=query.validated_data["days"]
            )
        except KeyError:
            from rest_framework.exceptions import NotFound

            raise NotFound(f"No such report: {name}") from None

        if query.validated_data["export"] == "csv":
            return self._csv(name, data)
        return Response(data)

    def _csv(self, name, data):
        """Stream the report as CSV.

        Only a list-shaped report exports: a nested report (inventory aging is buckets plus a
        sample) has no single row shape, and flattening it silently would produce a file that
        does not say what the reader thinks it says.
        """
        from rest_framework.exceptions import ValidationError

        if not isinstance(data, list):
            raise ValidationError(
                {"export": f"The {name} report is not a flat table; request it as JSON."}
            )
        writer = csv.writer(_Echo())
        header = list(data[0].keys()) if data else []

        def rows():
            if header:
                yield writer.writerow(header)
            for row in data:
                yield writer.writerow([row.get(key, "") for key in header])

        response = StreamingHttpResponse(rows(), content_type="text/csv")
        response["Content-Disposition"] = f'attachment; filename="{name}.csv"'
        return response


class AuditEventViewSet(mixins.ListModelMixin, viewsets.GenericViewSet):
    """The audit trail (SRS 5.3, 3.17.3).

    Not row-scoped through `apply_scope`: an audit log is a governance surface, and filtering
    it by the reader's ordinary data scope would let the person who did something choose not
    to be in it. Who may open it at all is a function-level permission question — and until
    that permission set lands, this is restricted to `data_scope = ALL`.
    """

    serializer_class = AuditEventSerializer
    queryset = AuditEvent.objects.select_related("actor_user").order_by("-created_at")
    filterset_fields = ["action", "entity_type", "actor_user"]
    ordering_fields = ["created_at"]
    ordering = ["-created_at"]

    def get_queryset(self):
        from apps.identity.models import Role
        from apps.identity.selectors import scopes_for

        if Role.DataScope.ALL not in scopes_for(self.request.user):
            return AuditEvent.objects.none()
        return super().get_queryset()
