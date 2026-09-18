"""Views for the platform API (SRS §3.13, §5.3).

Read-only. Every figure is computed over an `apply_scope`d queryset reached through each
domain app's `selectors` module, so a dashboard cannot become the one place row visibility
does not apply.
"""
import csv

from django.http import StreamingHttpResponse
from drf_spectacular.utils import OpenApiResponse, extend_schema
from rest_framework import mixins, viewsets
from rest_framework.decorators import action
from rest_framework.response import Response
from rest_framework.settings import api_settings
from rest_framework.views import APIView

from apps.identity.permissions import IsStaff

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

    permission_classes = [*api_settings.DEFAULT_PERMISSION_CLASSES, IsStaff]

    @extend_schema(
        parameters=[WindowSerializer],
        responses={200: OpenApiResponse(description="KPIs, scoped to the caller")},
    )
    def get(self, request):
        as_of = request.query_params.get("as_of")
        if as_of:
            # Historical mode (SRS 3.13.1): read the pre-computed snapshot instead of
            # recomputing over live rows. Access follows the caller's scope: ALL any,
            # BRANCH its own branch + ORG, TEAM its own team + ORG.
            import datetime

            from rest_framework.exceptions import NotFound
            from rest_framework.exceptions import ValidationError as DRFValidationError

            try:
                as_of_date = datetime.date.fromisoformat(as_of)
            except ValueError:
                raise DRFValidationError({"as_of": "Use YYYY-MM-DD."}) from None
            snapshot = selectors.snapshot_for(
                request.user,
                as_of=as_of_date,
                scope_type=request.query_params.get("scope_type", "ORG"),
                scope_id=request.query_params.get("scope_id") or None,
            )
            if snapshot is None:
                raise NotFound("No snapshot for that day and scope.")
            return Response(
                {"as_of": str(snapshot.as_of_date), "scope_type": snapshot.scope_type,
                 "scope_id": snapshot.scope_id, **snapshot.data}
            )
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

    permission_classes = [*api_settings.DEFAULT_PERMISSION_CLASSES, IsStaff]

    @extend_schema(responses={200: ReportIndexSerializer(many=True)})
    def get(self, request):
        return Response(ReportIndexSerializer(report_index(), many=True).data)


class ReportView(APIView):
    """Named reports (SRS 3.13.2), with CSV export (SRS 3.13.4)."""

    permission_classes = [*api_settings.DEFAULT_PERMISSION_CLASSES, IsStaff]

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


# --- Integration admin (SRS §3.10, 3.19.2) --------------------------------------------------
#
# AgencyAdminOnly, reads included: connections and webhooks carry delivery URLs, HMAC
# secrets, credential references and raw payload snapshots, and the role definitions give
# Integration Management to the Super Admin alone.


class _AgencyAdminViewSet(viewsets.GenericViewSet):
    def get_permissions(self):
        from apps.identity.permissions import AgencyAdminOnly

        return [*super().get_permissions(), AgencyAdminOnly()]


class ConnectionViewSet(
    mixins.ListModelMixin, mixins.RetrieveModelMixin, mixins.CreateModelMixin,
    mixins.UpdateModelMixin, _AgencyAdminViewSet,
):
    filterset_fields = ["provider", "direction", "status"]
    ordering = ["name"]

    def get_queryset(self):
        from ..models import Connection

        return Connection.objects.all()

    def get_serializer_class(self):
        from .serializers import ConnectionSerializer

        return ConnectionSerializer

    def perform_create(self, serializer):
        serializer.save(created_by=self.request.user)

    @action(detail=True, methods=["post"])
    def sync(self, request, pk=None):
        """Run one sync now — the admin's 'try it' button."""
        from ..sync import run_connection_sync
        from .serializers import SyncLogSerializer

        log = run_connection_sync(self.get_object())
        return Response(SyncLogSerializer(log).data)


class WebhookViewSet(
    mixins.ListModelMixin, mixins.RetrieveModelMixin, mixins.CreateModelMixin,
    mixins.UpdateModelMixin, mixins.DestroyModelMixin, _AgencyAdminViewSet,
):
    filterset_fields = ["direction", "event_type", "is_active", "connection"]
    ordering = ["event_type"]

    def get_queryset(self):
        from ..models import Webhook

        return Webhook.objects.select_related("connection")

    def get_serializer_class(self):
        from .serializers import WebhookSerializer

        return WebhookSerializer

    def perform_create(self, serializer):
        from ..webhooks import _webhook_connection

        if serializer.validated_data.get("connection") is None:
            serializer.save(connection=_webhook_connection())
        else:
            serializer.save()


class ExternalMappingViewSet(
    mixins.ListModelMixin, mixins.RetrieveModelMixin, _AgencyAdminViewSet
):
    filterset_fields = ["connection", "entity_type"]
    ordering = ["-last_synced_at"]

    def get_queryset(self):
        from ..models import ExternalMapping

        return ExternalMapping.objects.select_related("connection")

    def get_serializer_class(self):
        from .serializers import ExternalMappingSerializer

        return ExternalMappingSerializer


class SyncLogViewSet(
    mixins.ListModelMixin, mixins.RetrieveModelMixin, _AgencyAdminViewSet
):
    filterset_fields = ["connection", "status", "direction", "entity_type"]
    ordering = ["-started_at"]

    def get_queryset(self):
        from ..models import SyncLog

        return SyncLog.objects.select_related("connection")

    def get_serializer_class(self):
        from .serializers import SyncLogSerializer

        return SyncLogSerializer


# --- Saved reports and schedules (SRS 3.13.3) -----------------------------------------------


class SavedReportViewSet(viewsets.ModelViewSet):
    """Visibility gates the DEFINITION (scoping registry); execution re-scopes rows through
    the caller — both halves of the SavedReport model doctrine."""

    filterset_fields = ["report_type", "visibility", "owner"]
    search_fields = ["name"]
    ordering = ["name"]

    def get_permissions(self):
        return [*super().get_permissions(), IsStaff()]

    def get_queryset(self):
        from apps.identity.selectors import apply_scope

        from ..models import SavedReport

        return apply_scope(
            SavedReport.objects.select_related("owner"), self.request.user, "saved_report"
        )

    def get_serializer_class(self):
        from .serializers import SavedReportSerializer

        return SavedReportSerializer

    def perform_create(self, serializer):
        serializer.save(owner=self.request.user)

    def perform_update(self, serializer):
        if serializer.instance.owner_id != self.request.user.pk and not (
            self.request.user.is_superuser
        ):
            from rest_framework.exceptions import PermissionDenied

            raise PermissionDenied("Only the owner edits a report definition.")
        serializer.save()

    def perform_destroy(self, instance):
        if instance.owner_id != self.request.user.pk and not self.request.user.is_superuser:
            from rest_framework.exceptions import PermissionDenied

            raise PermissionDenied("Only the owner deletes a report.")
        instance.delete()

    @action(detail=True, methods=["post"])
    def run(self, request, pk=None):
        """Execute now, as the CALLER — an ORG-shared report run by an agent returns the
        agent's rows. Audited EXPORT."""
        from ..reporting import execute_and_audit

        return Response(execute_and_audit(self.get_object(), request.user))

    @action(detail=True, methods=["get"], url_path="export")
    def export(self, request, pk=None):
        """CSV download of a tabular report."""
        from ..reporting import execute_and_audit

        result = execute_and_audit(self.get_object(), request.user)
        if "rows" not in result:
            from rest_framework.exceptions import ValidationError as DRFValidationError

            raise DRFValidationError(
                {"detail": "Analytic reports export from their own endpoints."}
            )
        writer = csv.writer(_Echo())
        lines = [writer.writerow(result["columns"])]
        lines += [
            writer.writerow([row.get(name) for name in result["columns"]])
            for row in result["rows"]
        ]
        response = StreamingHttpResponse(lines, content_type="text/csv")
        response["Content-Disposition"] = 'attachment; filename="report.csv"'
        return response


class ReportScheduleViewSet(viewsets.ModelViewSet):
    filterset_fields = ["saved_report", "frequency", "is_active"]
    ordering = ["next_run_at"]

    def get_permissions(self):
        return [*super().get_permissions(), IsStaff()]

    def get_queryset(self):
        """A schedule is visible with its report; only creators and admins see theirs
        listed (recipients are the creator's responsibility)."""
        from ..models import ReportSchedule

        if getattr(self, "swagger_fake_view", False):
            return ReportSchedule.objects.none()
        user = self.request.user
        queryset = ReportSchedule.objects.select_related("saved_report", "created_by")
        if user.is_superuser:
            return queryset
        from apps.identity.models import Role
        from apps.identity.scoping import scopes_for

        if Role.DataScope.ALL in scopes_for(user):
            return queryset
        return queryset.filter(created_by=user)

    def get_serializer_class(self):
        from .serializers import ReportScheduleSerializer

        return ReportScheduleSerializer

    def perform_create(self, serializer):
        from apps.identity.selectors import apply_scope

        from ..models import SavedReport

        report = serializer.validated_data["saved_report"]
        if not apply_scope(
            SavedReport.objects.filter(pk=report.pk), self.request.user, "saved_report"
        ).exists():
            from rest_framework.exceptions import PermissionDenied

            raise PermissionDenied("You cannot schedule a report you cannot open.")
        serializer.save(created_by=self.request.user)
