"""Routes owned by `platform`.

Mounted by `config/urls.py` under /api/v1/.
"""
from django.urls import include, path
from rest_framework.routers import DefaultRouter

from .views import (
    AuditEventViewSet,
    ConnectionViewSet,
    DashboardView,
    ExternalMappingViewSet,
    ReportIndexView,
    ReportScheduleViewSet,
    ReportView,
    SavedReportViewSet,
    SyncLogViewSet,
    WebhookViewSet,
)

router = DefaultRouter()
router.register("audit-events", AuditEventViewSet, basename="audit-event")
router.register("connections", ConnectionViewSet, basename="connection")
router.register("webhooks", WebhookViewSet, basename="webhook")
router.register("external-mappings", ExternalMappingViewSet, basename="external-mapping")
router.register("sync-logs", SyncLogViewSet, basename="sync-log")
router.register("saved-reports", SavedReportViewSet, basename="saved-report")
router.register("report-schedules", ReportScheduleViewSet, basename="report-schedule")

urlpatterns = [
    path("dashboard/", DashboardView.as_view(), name="dashboard"),
    path("reports/", ReportIndexView.as_view(), name="report-index"),
    path("reports/<str:name>/", ReportView.as_view(), name="report"),
    path("", include(router.urls)),
]
