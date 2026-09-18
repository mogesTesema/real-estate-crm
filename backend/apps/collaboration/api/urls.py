"""Routes owned by `collaboration`.

Mounted by `config/urls.py` under /api/v1/.
"""
from django.urls import include, path
from rest_framework.routers import DefaultRouter

from .views import (
    FileDownloadView,
    FileUploadView,
    NotificationPreferenceDetailView,
    NotificationPreferenceView,
    NotificationViewSet,
)

router = DefaultRouter()
router.register("notifications", NotificationViewSet, basename="notification")

urlpatterns = [
    path("files/", FileUploadView.as_view(), name="file-upload"),
    path("files/<uuid:pk>/download/", FileDownloadView.as_view(), name="file-download"),
    path(
        "notification-preferences/",
        NotificationPreferenceView.as_view(),
        name="notification-preferences",
    ),
    path(
        "notification-preferences/<str:notification_type>/",
        NotificationPreferenceDetailView.as_view(),
        name="notification-preference",
    ),
    path("", include(router.urls)),
]
