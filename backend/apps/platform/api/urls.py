"""Routes owned by `platform`.

Mounted by `config/urls.py` under /api/v1/.
"""
from django.urls import include, path
from rest_framework.routers import DefaultRouter

from .views import AuditEventViewSet, DashboardView, ReportIndexView, ReportView

router = DefaultRouter()
router.register("audit-events", AuditEventViewSet, basename="audit-event")

urlpatterns = [
    path("dashboard/", DashboardView.as_view(), name="dashboard"),
    path("reports/", ReportIndexView.as_view(), name="report-index"),
    path("reports/<str:name>/", ReportView.as_view(), name="report"),
    path("", include(router.urls)),
]
