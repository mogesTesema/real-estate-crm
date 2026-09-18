"""Views for the collaboration API.

Every mutation delegates to `apps.collaboration.services`; every queryset runs through the
scoping registry, per architecture.md §2.
"""
from django.core.exceptions import PermissionDenied as DjangoPermissionDenied
from django.core.exceptions import ValidationError as DjangoValidationError
from django.http import FileResponse, HttpResponseRedirect
from drf_spectacular.types import OpenApiTypes
from drf_spectacular.utils import OpenApiParameter, OpenApiResponse, extend_schema
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
    PublicDeclineSerializer,
    PublicSignSerializer,
    SendMessageSerializer,
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


# --- Phase D viewsets ------------------------------------------------------------------------

from django.shortcuts import get_object_or_404  # noqa: E402
from rest_framework.permissions import AllowAny  # noqa: E402

from apps.identity.permissions import IsStaff  # noqa: E402

from ..models import Document, Template  # noqa: E402


class DocumentViewSet(
    ScopedQuerysetMixin,
    mixins.ListModelMixin,
    mixins.RetrieveModelMixin,
    mixins.CreateModelMixin,
    mixins.UpdateModelMixin,
    mixins.DestroyModelMixin,
    viewsets.GenericViewSet,
):
    """The document repository (SRS 3.7.1). The scoping arm mirrors
    `document_access_level` — including the inversion where ALL-scope staff need a grant on
    confidential documents."""

    scope_resource = "document"
    filterset_fields = ["document_type", "is_confidential", "uploaded_by"]
    search_fields = ["title"]
    ordering = ["-created_at"]

    def get_unscoped_queryset(self):
        return selectors.live_documents()

    def get_serializer_class(self):
        from .serializers import (
            DocumentCreateSerializer,
            DocumentSerializer,
            GenerateDocumentSerializer,
            GrantSerializer,
            NewVersionSerializer,
        )

        return {
            "create": DocumentCreateSerializer,
            "new_version": NewVersionSerializer,
            "grants": GrantSerializer,
            "generate": GenerateDocumentSerializer,
        }.get(self.action, DocumentSerializer)

    def create(self, request, *args, **kwargs):
        from ..models import File
        from .serializers import DocumentCreateSerializer, DocumentSerializer

        payload = DocumentCreateSerializer(data=request.data)
        payload.is_valid(raise_exception=True)
        data = dict(payload.validated_data)
        file = get_object_or_404(File, pk=data.pop("file"))
        links = [dict(link) for link in data.pop("links")]
        try:
            document = services.create_document(
                actor=request.user, file=file, links=links, **data
            )
        except Exception as exc:  # noqa: BLE001 - narrowed by _translate
            _translate(exc)
        return Response(DocumentSerializer(document).data, status=status.HTTP_201_CREATED)

    def update(self, request, *args, **kwargs):
        from .serializers import DocumentSerializer

        allowed = {"title", "document_type", "status", "is_confidential"}
        fields = {k: v for k, v in request.data.items() if k in allowed}
        try:
            document = services.update_document(
                self.get_object(), actor=request.user, **fields
            )
        except Exception as exc:  # noqa: BLE001
            _translate(exc)
        return Response(DocumentSerializer(document).data)

    def destroy(self, request, *args, **kwargs):
        try:
            services.delete_document(self.get_object(), actor=request.user)
        except Exception as exc:  # noqa: BLE001
            _translate(exc)
        return Response(status=status.HTTP_204_NO_CONTENT)

    @action(detail=True, methods=["get"])
    def download(self, request, pk=None):
        """Gated document download — ≥DOWNLOAD level; audits the VIEW (SRS 3.7.5)."""
        from django.http import FileResponse, HttpResponseRedirect

        document = self.get_object()
        level = selectors.document_access_level(request.user, document)
        if level not in ("DOWNLOAD", "EDIT"):
            raise NotFound
        selectors._audit_document_view(request.user, document)
        target = services.files.download_target(document.file)
        if target["kind"] == "url":
            return HttpResponseRedirect(target["url"])
        return FileResponse(
            services.files.open_file(document.file),
            as_attachment=True,
            filename=document.file.original_name,
            content_type=document.file.mime_type or "application/octet-stream",
        )

    @action(detail=True, methods=["post"], url_path="new-version")
    def new_version(self, request, pk=None):
        from ..models import File
        from .serializers import DocumentSerializer, NewVersionSerializer

        payload = NewVersionSerializer(data=request.data)
        payload.is_valid(raise_exception=True)
        file = get_object_or_404(File, pk=payload.validated_data["file"])
        try:
            document = services.add_document_version(
                self.get_object(), actor=request.user, file=file
            )
        except Exception as exc:  # noqa: BLE001
            _translate(exc)
        return Response(DocumentSerializer(document).data)

    @action(detail=True, methods=["post"])
    def links(self, request, pk=None):
        from .serializers import DocumentLinkSerializer

        payload = DocumentLinkSerializer(data=request.data)
        payload.is_valid(raise_exception=True)
        try:
            link = services.link_document(
                self.get_object(), actor=request.user, **payload.validated_data
            )
        except Exception as exc:  # noqa: BLE001
            _translate(exc)
        return Response(DocumentLinkSerializer(link).data, status=status.HTTP_201_CREATED)

    @extend_schema(
        parameters=[OpenApiParameter("link_id", OpenApiTypes.UUID, OpenApiParameter.PATH)]
    )
    @action(detail=True, methods=["delete"], url_path=r"links/(?P<link_id>[^/.]+)")
    def unlink(self, request, pk=None, link_id=None):
        document = self.get_object()
        link = get_object_or_404(document.links, pk=link_id)
        try:
            services.unlink_document(link, actor=request.user)
        except Exception as exc:  # noqa: BLE001
            _translate(exc)
        return Response(status=status.HTTP_204_NO_CONTENT)

    @action(detail=True, methods=["post"])
    def grants(self, request, pk=None):
        from apps.identity.models import Role, User

        from .serializers import AccessGrantSerializer, GrantSerializer

        payload = GrantSerializer(data=request.data)
        payload.is_valid(raise_exception=True)
        data = payload.validated_data
        role = get_object_or_404(Role, pk=data["role"]) if data.get("role") else None
        user = get_object_or_404(User, pk=data["user"]) if data.get("user") else None
        try:
            grant = services.grant_access(
                self.get_object(), actor=request.user, role=role, user=user,
                access_level=data["access_level"],
            )
        except Exception as exc:  # noqa: BLE001
            _translate(exc)
        return Response(AccessGrantSerializer(grant).data, status=status.HTTP_201_CREATED)

    @extend_schema(
        parameters=[OpenApiParameter("grant_id", OpenApiTypes.UUID, OpenApiParameter.PATH)]
    )
    @action(detail=True, methods=["delete"], url_path=r"grants/(?P<grant_id>[^/.]+)")
    def revoke(self, request, pk=None, grant_id=None):
        grant = get_object_or_404(self.get_object().access_grants, pk=grant_id)
        try:
            services.revoke_access(grant, actor=request.user)
        except Exception as exc:  # noqa: BLE001
            _translate(exc)
        return Response(status=status.HTTP_204_NO_CONTENT)

    @action(detail=False, methods=["post"])
    def generate(self, request):
        """Merge-to-HTML document generation (SRS 3.6.2/3.7.2)."""
        from apps.contacts.selectors import live_contacts
        from apps.crm.selectors import visible_deals

        from .serializers import DocumentSerializer, GenerateDocumentSerializer

        payload = GenerateDocumentSerializer(data=request.data)
        payload.is_valid(raise_exception=True)
        data = dict(payload.validated_data)
        template = get_object_or_404(Template, pk=data.pop("template"))
        contact = (
            get_object_or_404(live_contacts(), pk=data.pop("contact"))
            if data.get("contact") else data.pop("contact", None)
        )
        deal = (
            get_object_or_404(visible_deals(request.user), pk=data.pop("deal"))
            if data.get("deal") else data.pop("deal", None)
        )
        links = [dict(link) for link in data.pop("links")]
        try:
            document = services.generate_document_from_template(
                actor=request.user, template=template, links=links,
                contact=contact, deal=deal, **data,
            )
        except Exception as exc:  # noqa: BLE001
            _translate(exc)
        return Response(DocumentSerializer(document).data, status=status.HTTP_201_CREATED)


class EsignEnvelopeViewSet(
    ScopedQuerysetMixin,
    mixins.ListModelMixin,
    mixins.RetrieveModelMixin,
    mixins.CreateModelMixin,
    viewsets.GenericViewSet,
):
    """Envelopes are staff tooling; signers use token URLs and never log in."""

    scope_resource = "esign_envelope"
    filterset_fields = ["status", "document", "provider"]
    ordering = ["-created_at"]

    def get_permissions(self):
        return [*super().get_permissions(), IsStaff()]

    def get_unscoped_queryset(self):
        from ..models import EsignEnvelope

        return EsignEnvelope.objects.select_related("document").prefetch_related(
            "signers", "signature_events"
        )

    def get_serializer_class(self):
        from .serializers import EsignEnvelopeCreateSerializer, EsignEnvelopeSerializer

        return {
            "create": EsignEnvelopeCreateSerializer,
        }.get(self.action, EsignEnvelopeSerializer)

    def create(self, request, *args, **kwargs):
        from apps.contacts.models import Contact
        from apps.identity.models import User

        from .serializers import EsignEnvelopeCreateSerializer, EsignEnvelopeSerializer

        payload = EsignEnvelopeCreateSerializer(data=request.data)
        payload.is_valid(raise_exception=True)
        data = dict(payload.validated_data)
        document = get_object_or_404(Document, pk=data.pop("document"))
        signers = []
        for entry in data.pop("signers"):
            entry = dict(entry)
            if entry.get("contact"):
                entry["contact"] = get_object_or_404(Contact, pk=entry["contact"])
            if entry.get("user"):
                entry["user"] = get_object_or_404(User, pk=entry["user"])
            signers.append(entry)
        try:
            envelope = services.create_envelope(
                actor=request.user, document=document, signers=signers, **data
            )
        except Exception as exc:  # noqa: BLE001
            _translate(exc)
        return Response(
            EsignEnvelopeSerializer(envelope).data, status=status.HTTP_201_CREATED
        )

    def _lifecycle(self, request, service, **kwargs):
        from .serializers import EsignEnvelopeSerializer

        try:
            envelope = service(self.get_object(), actor=request.user, **kwargs)
        except Exception as exc:  # noqa: BLE001
            _translate(exc)
        return Response(EsignEnvelopeSerializer(envelope).data)

    @action(detail=True, methods=["post"])
    def send(self, request, pk=None):
        return self._lifecycle(request, services.send_envelope)

    @action(detail=True, methods=["post"])
    def void(self, request, pk=None):
        return self._lifecycle(
            request, services.void_envelope, reason=request.data.get("reason", "")
        )

    @action(detail=True, methods=["post"])
    def remind(self, request, pk=None):
        return self._lifecycle(request, services.remind)


# --- Public e-sign (token URLs, no auth) -----------------------------------------------------


class PublicEsignBase(APIView):
    authentication_classes = []
    permission_classes = [AllowAny]
    throttle_scope = "esign_public"

    def _signer(self, token):
        signer = services.resolve_token(token)
        if signer is None:
            raise NotFound
        return signer

    @staticmethod
    def _client(request):
        return {
            "ip": request.META.get("HTTP_X_FORWARDED_FOR", "").split(",")[0].strip()
            or request.META.get("REMOTE_ADDR"),
            "user_agent": request.META.get("HTTP_USER_AGENT", "")[:500],
        }


class PublicEsignView(PublicEsignBase):
    """`GET /public/esign/{token}/` — what the signer sees. Records the VIEW."""

    @extend_schema(auth=[], responses={200: OpenApiResponse(description="Envelope state")})
    def get(self, request, token):
        signer = self._signer(token)
        client = self._client(request)
        services.record_view(signer, ip=client["ip"], user_agent=client["user_agent"])
        envelope = signer.envelope
        current = envelope.signers.filter(
            role__in=("SIGNER", "APPROVER", "WITNESS")
        ).exclude(status__in=("SIGNED", "DECLINED")).order_by("signing_order").first()
        return Response(
            {
                "subject": envelope.subject,
                "message": envelope.message,
                "document_title": envelope.document.title,
                "you": {"name": signer.name, "role": signer.role, "status": signer.status},
                "is_your_turn": bool(current and current.pk == signer.pk),
                "signers": [
                    {"name": s.name, "status": s.status, "order": s.signing_order}
                    for s in envelope.signers.all()
                ],
            }
        )


class PublicEsignDocumentView(PublicEsignBase):
    """The document bytes, keyed by the same token — filesystem streams, S3 redirects."""

    @extend_schema(auth=[], responses={302: OpenApiResponse(description="Signed URL")})
    def get(self, request, token):
        from django.http import FileResponse, HttpResponseRedirect

        signer = self._signer(token)
        file = signer.envelope.document.file
        target = services.files.download_target(file)
        if target["kind"] == "url":
            return HttpResponseRedirect(target["url"])
        return FileResponse(
            services.files.open_file(file),
            as_attachment=True,
            filename=file.original_name,
            content_type=file.mime_type or "application/octet-stream",
        )


class PublicEsignSignView(PublicEsignBase):
    @extend_schema(
        auth=[],
        request=PublicSignSerializer,
        responses={200: OpenApiResponse(description="Signer + envelope status")},
    )
    def post(self, request, token):
        from .serializers import PublicSignSerializer

        signer = self._signer(token)
        payload = PublicSignSerializer(data=request.data)
        payload.is_valid(raise_exception=True)
        client = self._client(request)
        try:
            signer = services.sign(
                signer, ip=client["ip"], user_agent=client["user_agent"],
                **payload.validated_data,
            )
        except Exception as exc:  # noqa: BLE001
            _translate(exc)
        return Response(
            {"status": signer.status, "envelope_status": signer.envelope.status}
        )


class PublicEsignDeclineView(PublicEsignBase):
    @extend_schema(
        auth=[],
        request=PublicDeclineSerializer,
        responses={200: OpenApiResponse(description="Signer + envelope status")},
    )
    def post(self, request, token):
        from .serializers import PublicDeclineSerializer

        signer = self._signer(token)
        payload = PublicDeclineSerializer(data=request.data)
        payload.is_valid(raise_exception=True)
        client = self._client(request)
        try:
            signer = services.decline(
                signer, reason=payload.validated_data["reason"],
                ip=client["ip"], user_agent=client["user_agent"],
            )
        except Exception as exc:  # noqa: BLE001
            _translate(exc)
        return Response(
            {"status": signer.status, "envelope_status": signer.envelope.status}
        )


# --- Activities / calendar -------------------------------------------------------------------


class ActivityViewSet(
    ScopedQuerysetMixin,
    mixins.ListModelMixin,
    mixins.RetrieveModelMixin,
    mixins.CreateModelMixin,
    mixins.UpdateModelMixin,
    viewsets.GenericViewSet,
):
    """Tasks and the unified calendar (SRS §3.12). VIEWING/INSPECTION rows appear here but
    are created only by their domain records — §13's invariant, enforced in the service."""

    scope_resource = "activity"
    filterset_fields = ["activity_type", "status", "priority", "assigned_to",
                        "contact", "lead", "deal", "property", "lease"]
    ordering_fields = ["start_at", "due_at", "priority"]
    ordering = ["due_at"]

    def get_unscoped_queryset(self):
        from ..models import Activity

        return Activity.objects.select_related("assigned_to", "created_by")

    def get_serializer_class(self):
        from .serializers import ActivitySerializer, ActivityWriteSerializer

        if self.action in ("create", "update", "partial_update"):
            return ActivityWriteSerializer
        return ActivitySerializer

    def create(self, request, *args, **kwargs):
        from .serializers import ActivitySerializer, ActivityWriteSerializer

        payload = ActivityWriteSerializer(data=request.data)
        payload.is_valid(raise_exception=True)
        data = dict(payload.validated_data)
        try:
            activity = services.create_activity(
                actor=request.user,
                activity_type=data.pop("activity_type"),
                subject=data.pop("subject"),
                **data,
            )
        except Exception as exc:  # noqa: BLE001
            _translate(exc)
        return Response(ActivitySerializer(activity).data, status=status.HTTP_201_CREATED)

    def update(self, request, *args, **kwargs):
        from .serializers import ActivitySerializer, ActivityWriteSerializer

        payload = ActivityWriteSerializer(
            instance=self.get_object(), data=request.data,
            partial=kwargs.pop("partial", False),
        )
        payload.is_valid(raise_exception=True)
        try:
            activity = services.update_activity(
                self.get_object(), actor=request.user, **payload.validated_data
            )
        except Exception as exc:  # noqa: BLE001
            _translate(exc)
        return Response(ActivitySerializer(activity).data)

    def _move(self, request, new_status):
        from .serializers import ActivitySerializer

        try:
            activity, next_occurrence = services.change_activity_status(
                self.get_object(), new_status, actor=request.user
            )
        except Exception as exc:  # noqa: BLE001
            _translate(exc)
        body = ActivitySerializer(activity).data
        if next_occurrence is not None:
            body["next_occurrence"] = ActivitySerializer(next_occurrence).data
        return Response(body)

    @action(detail=True, methods=["post"])
    def start(self, request, pk=None):
        return self._move(request, "IN_PROGRESS")

    @action(detail=True, methods=["post"])
    def complete(self, request, pk=None):
        return self._move(request, "COMPLETED")

    @action(detail=True, methods=["post"])
    def cancel(self, request, pk=None):
        return self._move(request, "CANCELLED")

    @action(detail=True, methods=["post"])
    def reopen(self, request, pk=None):
        return self._move(request, "OPEN")

    @action(detail=False, methods=["get"], pagination_class=None)
    def calendar(self, request):
        """`?start=&end=` — the scoped calendar. Managers see their reports' diaries through
        the registry for free; `user=` filters within what scoping already granted."""
        import datetime

        from .serializers import ActivitySerializer

        start_raw, end_raw = request.query_params.get("start"), request.query_params.get("end")
        if not start_raw or not end_raw:
            raise ValidationError({"start": "Pass start and end (ISO dates)."})
        try:
            start = datetime.date.fromisoformat(start_raw)
            end = datetime.date.fromisoformat(end_raw)
        except ValueError:
            raise ValidationError({"start": "Dates are ISO YYYY-MM-DD."}) from None
        if (end - start).days > 100 or end < start:
            raise ValidationError({"end": "A calendar window is 1 to 100 days."})

        queryset = self.filter_queryset(self.get_queryset()).filter(
            start_at__date__lte=end,
        ).filter(
            models_q_calendar_overlap(start)
        )
        if request.query_params.get("user"):
            queryset = queryset.filter(assigned_to=request.query_params["user"])
        return Response(ActivitySerializer(queryset.order_by("start_at"), many=True).data)


def models_q_calendar_overlap(start):
    from django.db.models import Q

    return Q(start_at__date__gte=start) | Q(due_at__date__gte=start)


# --- Communications --------------------------------------------------------------------------


class ThreadViewSet(
    ScopedQuerysetMixin, mixins.ListModelMixin, mixins.RetrieveModelMixin,
    viewsets.GenericViewSet,
):
    scope_resource = "thread"
    filterset_fields = ["channel", "contact", "lead", "deal", "property", "assigned_to",
                        "is_closed"]
    ordering = ["-last_message_at"]

    def get_unscoped_queryset(self):
        from ..models import Thread

        return Thread.objects.select_related("contact", "assigned_to")

    def get_serializer_class(self):
        from .serializers import SendMessageSerializer, ThreadSerializer

        if self.action == "messages":
            return SendMessageSerializer
        return ThreadSerializer

    @action(detail=True, methods=["get", "post"])
    def messages(self, request, pk=None):
        from .serializers import MessageSerializer

        thread = self.get_object()
        if request.method == "GET":
            return Response(
                MessageSerializer(
                    thread.messages.order_by("created_at"), many=True
                ).data
            )
        return _send_message(request, thread=thread)

    @action(detail=True, methods=["post"])
    def assign(self, request, pk=None):
        from apps.identity.selectors import visible_users

        from .serializers import ThreadSerializer

        assignee = get_object_or_404(
            visible_users(request.user), pk=request.data.get("assignee")
        )
        thread = services.assign_thread(
            self.get_object(), actor=request.user, assignee=assignee
        )
        return Response(ThreadSerializer(thread).data)

    @action(detail=True, methods=["post"])
    def close(self, request, pk=None):
        from .serializers import ThreadSerializer

        return Response(
            ThreadSerializer(
                services.close_thread(self.get_object(), actor=request.user)
            ).data
        )

    @action(detail=True, methods=["post"])
    def reopen(self, request, pk=None):
        from .serializers import ThreadSerializer

        return Response(
            ThreadSerializer(
                services.reopen_thread(self.get_object(), actor=request.user)
            ).data
        )


def _send_message(request, thread=None):
    from apps.contacts.selectors import live_contacts

    from ..models import Template as TemplateModel
    from .serializers import MessageSerializer, SendMessageSerializer

    payload = SendMessageSerializer(data=request.data)
    payload.is_valid(raise_exception=True)
    data = dict(payload.validated_data)
    contact = (
        get_object_or_404(live_contacts(), pk=data.pop("contact"))
        if data.get("contact") else data.pop("contact", None)
    )
    template = (
        get_object_or_404(TemplateModel, pk=data.pop("template"))
        if data.get("template") else data.pop("template", None)
    )
    data.pop("thread", None)
    try:
        message = services.send_message(
            actor=request.user, contact=contact, thread=thread, template=template, **data
        )
    except Exception as exc:  # noqa: BLE001
        _translate(exc)
    return Response(MessageSerializer(message).data, status=status.HTTP_201_CREATED)


class MessageComposeView(APIView):
    """`POST /messages/` — threadless compose; the service resolves or creates the thread."""

    @extend_schema(
        request=SendMessageSerializer,
        responses={201: OpenApiResponse(description="Message")},
    )
    def post(self, request):
        return _send_message(request)


class MessageViewSet(
    ScopedQuerysetMixin, mixins.ListModelMixin, mixins.RetrieveModelMixin,
    viewsets.GenericViewSet,
):
    scope_resource = "message"
    filterset_fields = ["thread", "channel", "direction", "status", "contact"]
    ordering = ["-created_at"]

    def get_unscoped_queryset(self):
        from ..models import Message

        return Message.objects.select_related("thread", "sender_user", "contact")

    def get_serializer_class(self):
        from .serializers import MessageSerializer

        return MessageSerializer


class CallLogViewSet(
    ScopedQuerysetMixin,
    mixins.ListModelMixin,
    mixins.RetrieveModelMixin,
    mixins.CreateModelMixin,
    viewsets.GenericViewSet,
):
    scope_resource = "call_log"
    filterset_fields = ["direction", "outcome", "contact", "lead"]
    ordering = ["-started_at"]

    def get_unscoped_queryset(self):
        from ..models import CallLog

        return CallLog.objects.select_related("user", "contact", "lead", "recording_file")

    def get_serializer_class(self):
        from .serializers import CallLogSerializer

        return CallLogSerializer

    def create(self, request, *args, **kwargs):
        from ..models import File
        from .serializers import CallLogSerializer

        data = {
            key: request.data.get(key)
            for key in ("direction", "phone_number", "started_at", "outcome",
                        "duration_seconds", "notes")
            if request.data.get(key) is not None
        }
        from apps.contacts.selectors import live_contacts
        from apps.crm.selectors import live_leads

        for key, queryset in (
            ("contact", live_contacts()),
            ("lead", live_leads()),
            ("recording_file", File.objects.all()),
        ):
            if request.data.get(key):
                data[key] = get_object_or_404(queryset, pk=request.data[key])
        try:
            call = services.log_call(actor=request.user, **data)
        except Exception as exc:  # noqa: BLE001
            _translate(exc)
        return Response(CallLogSerializer(call).data, status=status.HTTP_201_CREATED)


class InternalNoteViewSet(
    ScopedQuerysetMixin,
    mixins.ListModelMixin,
    mixins.RetrieveModelMixin,
    mixins.CreateModelMixin,
    mixins.UpdateModelMixin,
    viewsets.GenericViewSet,
):
    """Internal by definition — IsStaff keeps every portal read out."""

    scope_resource = "internal_note"
    filterset_fields = ["contact", "lead", "deal", "property", "lease", "author"]
    ordering = ["-created_at"]

    def get_permissions(self):
        return [*super().get_permissions(), IsStaff()]

    def get_unscoped_queryset(self):
        from ..models import InternalNote

        return InternalNote.objects.select_related("author")

    def get_serializer_class(self):
        from .serializers import InternalNoteSerializer

        return InternalNoteSerializer

    def create(self, request, *args, **kwargs):
        from .serializers import InternalNoteSerializer

        serializer = InternalNoteSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = dict(serializer.validated_data)
        try:
            note = services.create_note(actor=request.user, **data)
        except Exception as exc:  # noqa: BLE001
            _translate(exc)
        return Response(InternalNoteSerializer(note).data, status=status.HTTP_201_CREATED)

    def update(self, request, *args, **kwargs):
        from .serializers import InternalNoteSerializer

        try:
            note = services.update_note(
                self.get_object(), actor=request.user,
                body=request.data.get("body"), mentions=request.data.get("mentions"),
            )
        except Exception as exc:  # noqa: BLE001
            _translate(exc)
        return Response(InternalNoteSerializer(note).data)


class TemplateViewSet(viewsets.ModelViewSet):
    """Templates are configuration (the RoutingRule precedent) — staff read, staff write."""

    filterset_fields = ["channel", "is_active"]

    def get_permissions(self):
        return [*super().get_permissions(), IsStaff()]

    def get_queryset(self):
        return Template.objects.all().order_by("name")

    def get_serializer_class(self):
        from .serializers import TemplateSerializer

        return TemplateSerializer

    def perform_create(self, serializer):
        serializer.instance = services.create_template(
            actor=self.request.user, **serializer.validated_data
        )

    def perform_update(self, serializer):
        serializer.instance = services.update_template(
            serializer.instance, actor=self.request.user, **serializer.validated_data
        )

    def perform_destroy(self, instance):
        services.update_template(instance, actor=self.request.user, is_active=False)

    @action(detail=True, methods=["post"])
    def preview(self, request, pk=None):
        from apps.contacts.selectors import live_contacts

        contact = None
        if request.data.get("contact"):
            contact = get_object_or_404(live_contacts(), pk=request.data["contact"])
        try:
            rendered = services.render_template_preview(self.get_object(), contact=contact)
        except Exception as exc:  # noqa: BLE001
            _translate(exc)
        return Response(rendered)
