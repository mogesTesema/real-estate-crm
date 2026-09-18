"""Views for the collaboration API.

Every mutation delegates to `apps.collaboration.services`; every queryset runs through the
scoping registry, per architecture.md §2.
"""
from django.core.exceptions import PermissionDenied as DjangoPermissionDenied
from django.core.exceptions import ValidationError as DjangoValidationError
from django.http import FileResponse, HttpResponseRedirect
from drf_spectacular.utils import OpenApiResponse, extend_schema
from rest_framework import mixins, status, viewsets
from rest_framework.decorators import action
from rest_framework.exceptions import NotFound, PermissionDenied, ValidationError
from rest_framework.parsers import MultiPartParser
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.identity.selectors import ScopedQuerysetMixin

from .. import selectors, services
from ..models import File, Notification
from .serializers import (
    FileSerializer,
    FileUploadSerializer,
    NotificationPreferenceSerializer,
    NotificationPreferenceWriteSerializer,
    NotificationSerializer,
)


def _translate(exc):
    """Re-raise a service-layer Django exception as its DRF equivalent."""
    if isinstance(exc, DjangoPermissionDenied):
        raise PermissionDenied(str(exc)) from exc
    if isinstance(exc, DjangoValidationError):
        raise ValidationError(
            exc.message_dict if hasattr(exc, "message_dict") else exc.messages
        ) from exc
    raise exc


class FileUploadView(APIView):
    """`POST /files/` — the one way bytes enter the system (SRS 3.3.3, 3.7.1)."""

    parser_classes = [MultiPartParser]
    throttle_scope = "file_upload"

    @extend_schema(
        request={"multipart/form-data": FileUploadSerializer},
        responses={201: FileSerializer},
    )
    def post(self, request):
        payload = FileUploadSerializer(data=request.data)
        payload.is_valid(raise_exception=True)
        try:
            file = services.store_file(
                actor=request.user,
                uploaded_file=payload.validated_data["file"],
                is_encrypted=payload.validated_data["is_encrypted"],
            )
        except Exception as exc:  # noqa: BLE001 - narrowed by _translate
            _translate(exc)
        return Response(FileSerializer(file).data, status=status.HTTP_201_CREATED)


class FileDownloadView(APIView):
    """`GET /files/{id}/download/` — gated bytes.

    Denial is a 404, not a 403: confirming that a file id exists is itself a disclosure.
    S3-backed storage answers with a redirect to a short-lived signed URL; filesystem
    storage streams.
    """

    @extend_schema(responses={302: OpenApiResponse(description="Signed URL redirect")})
    def get(self, request, pk):
        file = File.objects.filter(pk=pk).first()
        if file is None or not selectors.user_can_download_file(request.user, file):
            raise NotFound
        target = services.files.download_target(file)
        if target["kind"] == "url":
            return HttpResponseRedirect(target["url"])
        return FileResponse(
            services.files.open_file(file),
            as_attachment=True,
            filename=file.original_name,
            content_type=file.mime_type or "application/octet-stream",
        )


class NotificationViewSet(
    ScopedQuerysetMixin, mixins.ListModelMixin, mixins.RetrieveModelMixin, viewsets.GenericViewSet
):
    """A user's own feed (SRS 3.18.1). Marking read is self-service, so portal clients —
    who receive lease and maintenance notifications — may write here too."""

    scope_resource = "notification"
    serializer_class = NotificationSerializer
    portal_writable = True
    filterset_fields = ["is_read", "type"]
    ordering = ["-created_at"]

    def get_unscoped_queryset(self):
        return Notification.objects.all()

    @extend_schema(responses={200: OpenApiResponse(description='{"unread": n}')})
    @action(detail=False, methods=["get"], url_path="unread-count")
    def unread_count(self, request):
        return Response({"unread": selectors.unread_count(request.user)})

    @extend_schema(request=None, responses={200: NotificationSerializer})
    @action(detail=True, methods=["post"], url_path="mark-read")
    def mark_read(self, request, pk=None):
        try:
            notification = services.mark_read(self.get_object(), actor=request.user)
        except Exception as exc:  # noqa: BLE001
            _translate(exc)
        return Response(NotificationSerializer(notification).data)

    @extend_schema(request=None, responses={200: OpenApiResponse(description='{"marked": n}')})
    @action(detail=False, methods=["post"], url_path="mark-all-read")
    def mark_all_read(self, request):
        return Response({"marked": services.mark_all_read(actor=request.user)})


class NotificationPreferenceView(APIView):
    """Per-type channel preferences and quiet hours (SRS 3.18.2). Self-service."""

    portal_writable = True

    @extend_schema(responses={200: NotificationPreferenceSerializer(many=True)})
    def get(self, request):
        from ..models import NotificationPreference

        existing = {
            p.notification_type: p
            for p in NotificationPreference.objects.filter(user=request.user)
        }
        # Materialize defaults for missing types without writing rows: the answer to "what
        # are my settings?" must not depend on whether you ever changed them.
        rows = [
            existing.get(value)
            or NotificationPreference(user=request.user, notification_type=value)
            for value in Notification.Type.values
        ]
        return Response(NotificationPreferenceSerializer(rows, many=True).data)


class NotificationPreferenceDetailView(APIView):
    portal_writable = True

    @extend_schema(
        request=NotificationPreferenceWriteSerializer,
        responses={200: NotificationPreferenceSerializer},
    )
    def put(self, request, notification_type):
        payload = NotificationPreferenceWriteSerializer(data=request.data)
        payload.is_valid(raise_exception=True)
        try:
            preference = services.set_notification_preference(
                actor=request.user,
                notification_type=notification_type,
                **payload.validated_data,
            )
        except Exception as exc:  # noqa: BLE001
            _translate(exc)
        return Response(NotificationPreferenceSerializer(preference).data)
