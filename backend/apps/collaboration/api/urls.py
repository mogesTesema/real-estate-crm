"""Routes owned by `collaboration`.

Mounted by `config/urls.py` under /api/v1/.
"""
from django.urls import include, path
from rest_framework.routers import DefaultRouter

from .views import (
    ActivityViewSet,
    CallLogViewSet,
    DocumentViewSet,
    EsignEnvelopeViewSet,
    FileDownloadView,
    FileUploadView,
    InternalNoteViewSet,
    MessageComposeView,
    MessageViewSet,
    NotificationPreferenceDetailView,
    NotificationPreferenceView,
    NotificationViewSet,
    PublicEsignDeclineView,
    PublicEsignDocumentView,
    PublicEsignSignView,
    PublicEsignView,
    TemplateViewSet,
    ThreadViewSet,
)

router = DefaultRouter()
router.register("notifications", NotificationViewSet, basename="notification")
router.register("documents", DocumentViewSet, basename="document")
router.register("esign/envelopes", EsignEnvelopeViewSet, basename="esign-envelope")
router.register("activities", ActivityViewSet, basename="activity")
router.register("threads", ThreadViewSet, basename="thread")
router.register("comm-messages", MessageViewSet, basename="message")
router.register("call-logs", CallLogViewSet, basename="call-log")
router.register("internal-notes", InternalNoteViewSet, basename="internal-note")
router.register("templates", TemplateViewSet, basename="template")

urlpatterns = [
    path("messages/", MessageComposeView.as_view(), name="message-compose"),
    path("public/esign/<str:token>/", PublicEsignView.as_view(), name="esign-public"),
    path(
        "public/esign/<str:token>/document/",
        PublicEsignDocumentView.as_view(),
        name="esign-public-document",
    ),
    path(
        "public/esign/<str:token>/sign/",
        PublicEsignSignView.as_view(),
        name="esign-public-sign",
    ),
    path(
        "public/esign/<str:token>/decline/",
        PublicEsignDeclineView.as_view(),
        name="esign-public-decline",
    ),
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
