"""Views for the contacts API (SRS §3.2).

Every mutation delegates to `apps.contacts.services`; no view touches the ORM to write. Every
queryset runs through `ScopedQuerysetMixin`, per architecture.md §2.
"""
import csv

from django.core.exceptions import PermissionDenied as DjangoPermissionDenied
from django.core.exceptions import ValidationError as DjangoValidationError
from django.http import StreamingHttpResponse
from drf_spectacular.utils import OpenApiParameter, OpenApiResponse, extend_schema
from rest_framework import mixins, status, viewsets
from rest_framework.decorators import action
from rest_framework.exceptions import PermissionDenied, ValidationError
from rest_framework.parsers import FormParser, JSONParser, MultiPartParser
from rest_framework.response import Response

from apps.identity.permissions import IsStaff
from apps.identity.selectors import ScopedQuerysetMixin

from .. import selectors, services
from ..models import Contact
from .serializers import (
    ConsentSerializer,
    ContactSerializer,
    ContactWriteSerializer,
    DuplicateMatchSerializer,
    DuplicateQuerySerializer,
    ImportSerializer,
    MergeSerializer,
    RelationshipSerializer,
    SetRolesSerializer,
)


def _translate(exc):
    """Re-raise a service-layer Django exception as its DRF equivalent.

    Services raise Django exceptions so they are usable from a shell or a management command;
    DRF only renders its own. Without this a ValidationError from a service surfaces as a 500.
    """
    if isinstance(exc, DjangoPermissionDenied):
        raise PermissionDenied(str(exc)) from exc
    if isinstance(exc, DjangoValidationError):
        raise ValidationError(
            exc.message_dict if hasattr(exc, "message_dict") else exc.messages
        ) from exc
    raise exc


class _Echo:
    """A file-like object whose write() returns the line, for StreamingHttpResponse."""

    def write(self, value):
        return value


class ContactViewSet(
    ScopedQuerysetMixin,
    mixins.ListModelMixin,
    mixins.RetrieveModelMixin,
    mixins.CreateModelMixin,
    mixins.UpdateModelMixin,
    mixins.DestroyModelMixin,
    viewsets.GenericViewSet,
):
    """The centralised contact database (SRS 3.2).

    `destroy` soft-deletes: leads, deals and leases PROTECT this row, and the history they
    carry is the reason the record is worth keeping after the relationship ends.
    """

    scope_resource = "contact"
    parser_classes = [JSONParser, FormParser, MultiPartParser]
    #: Actions whose *reads* are staff tools rather than records. `duplicates` reports the
    #: existence of contacts outside the caller's scope by design — safe between colleagues,
    #: and a customer enumerating the company's contact book otherwise. `export` streams the
    #: book in bulk. Writes are already closed to clients by the default StaffWrite.
    STAFF_ONLY_ACTIONS = ("duplicates", "export", "import_csv", "merge")
    filterset_fields = ["contact_type", "is_active", "assigned_agent", "city", "country"]
    search_fields = ["first_name", "last_name", "company_name", "email", "phone"]
    # Declared explicitly. With OrderingFilter enabled globally and no `ordering_fields`, DRF
    # accepts any model field — including ones the serializer never exposes, which turns the
    # list endpoint into an enumeration oracle for them.
    ordering_fields = ["created_at", "updated_at", "first_name", "last_name", "company_name"]
    ordering = ["-created_at"]

    def get_permissions(self):
        if self.action in self.STAFF_ONLY_ACTIONS:
            return [*super().get_permissions(), IsStaff()]
        return super().get_permissions()

    def get_unscoped_queryset(self):
        return (
            selectors.live_contacts()
            .select_related("assigned_agent", "default_source")
            .prefetch_related("roles", "consents")
        )

    def get_serializer_class(self):
        if self.action in ("create", "update", "partial_update"):
            return ContactWriteSerializer
        if self.action == "roles":
            return SetRolesSerializer
        if self.action == "merge":
            return MergeSerializer
        if self.action == "consents":
            return ConsentSerializer
        if self.action == "relationships":
            return RelationshipSerializer
        if self.action == "import_csv":
            return ImportSerializer
        return ContactSerializer

    @extend_schema(
        request=ContactWriteSerializer,
        responses={201: ContactSerializer},
    )
    def create(self, request, *args, **kwargs):
        payload = ContactWriteSerializer(data=request.data, context={"request": request})
        payload.is_valid(raise_exception=True)
        data = dict(payload.validated_data)
        roles = data.pop("roles", ())
        try:
            contact = services.create_contact(actor=request.user, roles=roles, **data)
        except Exception as exc:  # noqa: BLE001 - narrowed by _translate
            _translate(exc)
        return Response(
            ContactSerializer(contact).data, status=status.HTTP_201_CREATED
        )

    @extend_schema(request=ContactWriteSerializer, responses={200: ContactSerializer})
    def update(self, request, *args, **kwargs):
        contact = self.get_object()
        payload = ContactWriteSerializer(
            instance=contact, data=request.data, partial=kwargs.pop("partial", False),
            context={"request": request},
        )
        payload.is_valid(raise_exception=True)
        data = dict(payload.validated_data)
        roles = data.pop("roles", None)
        try:
            contact = services.update_contact(
                contact, actor=request.user, roles=roles, **data
            )
        except Exception as exc:  # noqa: BLE001
            _translate(exc)
        return Response(ContactSerializer(contact).data)

    def destroy(self, request, *args, **kwargs):
        services.delete_contact(self.get_object(), actor=request.user)
        return Response(status=status.HTTP_204_NO_CONTENT)

    @extend_schema(request=SetRolesSerializer, responses={200: ContactSerializer})
    @action(detail=True, methods=["put"])
    def roles(self, request, pk=None):
        """Replace the contact's role set (SRS 3.2.2)."""
        contact = self.get_object()
        payload = SetRolesSerializer(data=request.data)
        payload.is_valid(raise_exception=True)
        try:
            services.set_roles(contact, payload.validated_data["roles"], actor=request.user)
        except Exception as exc:  # noqa: BLE001
            _translate(exc)
        contact.refresh_from_db()
        return Response(ContactSerializer(contact).data)

    @extend_schema(
        request=ConsentSerializer,
        responses={201: ConsentSerializer, 200: ConsentSerializer(many=True)},
    )
    @action(detail=True, methods=["get", "post"])
    def consents(self, request, pk=None):
        """Per-channel marketing consent with an evidence trail (SRS 5.5)."""
        contact = self.get_object()
        if request.method == "GET":
            return Response(
                ConsentSerializer(contact.consents.all(), many=True).data
            )
        payload = ConsentSerializer(data=request.data)
        payload.is_valid(raise_exception=True)
        try:
            consent = services.record_consent(
                contact=contact, actor=request.user, **payload.validated_data
            )
        except Exception as exc:  # noqa: BLE001
            _translate(exc)
        return Response(ConsentSerializer(consent).data, status=status.HTTP_201_CREATED)

    @extend_schema(
        request=RelationshipSerializer,
        responses={201: RelationshipSerializer, 200: RelationshipSerializer(many=True)},
    )
    @action(detail=True, methods=["get", "post"])
    def relationships(self, request, pk=None):
        """Household, company representative, referral source (SRS 3.2.5)."""
        contact = self.get_object()
        if request.method == "GET":
            return Response(
                RelationshipSerializer(
                    contact.relationships_from.select_related("to_contact"), many=True
                ).data
            )
        payload = RelationshipSerializer(data=request.data)
        payload.is_valid(raise_exception=True)
        data = payload.validated_data
        # The other end must be a contact the caller may see — otherwise linking is a way to
        # confirm that a given id exists.
        target = self._require_visible(data["to_contact"].pk)
        try:
            link = services.add_relationship(
                from_contact=contact,
                to_contact=target,
                relationship_type=data["relationship_type"],
                notes=data.get("notes"),
                actor=request.user,
            )
        except Exception as exc:  # noqa: BLE001
            _translate(exc)
        return Response(RelationshipSerializer(link).data, status=status.HTTP_201_CREATED)

    @extend_schema(request=MergeSerializer, responses={200: ContactSerializer})
    @action(detail=True, methods=["post"])
    def merge(self, request, pk=None):
        """Fold another contact into this one (SRS 3.2.7). This record survives."""
        survivor = self.get_object()
        payload = MergeSerializer(data=request.data)
        payload.is_valid(raise_exception=True)
        # Both sides must be visible to the caller: merging is destructive, and doing it to a
        # record you cannot see is not something to allow on a guessed id.
        duplicate = self._require_visible(payload.validated_data["duplicate_id"])
        try:
            survivor = services.merge_contacts(
                survivor=survivor, duplicate=duplicate, actor=request.user
            )
        except Exception as exc:  # noqa: BLE001
            _translate(exc)
        return Response(ContactSerializer(survivor).data)

    def _require_visible(self, contact_id):
        try:
            return self.get_queryset().get(pk=contact_id)
        except Contact.DoesNotExist:
            raise ValidationError(
                {"detail": "No such contact, or it is outside your access."}
            ) from None

    @extend_schema(
        parameters=[DuplicateQuerySerializer],
        responses={200: DuplicateMatchSerializer(many=True)},
    )
    @action(detail=False, methods=["get"])
    def duplicates(self, request):
        """Candidate duplicates for a contact about to be created (SRS 3.1.3, 3.1.10).

        Searches the whole book, not the caller's slice — a scoped lookup would hide the
        record another agent already owns and create the duplicate this exists to prevent —
        but reveals a match outside the caller's access only as its existence and its owner.
        """
        query = DuplicateQuerySerializer(data=request.query_params)
        query.is_valid(raise_exception=True)
        matches = selectors.find_duplicates(request.user, **query.validated_data)
        return Response(matches)

    @extend_schema(
        request=ImportSerializer,
        responses={200: OpenApiResponse(description="Dry-run or commit report")},
    )
    @action(detail=False, methods=["post"], url_path="import", parser_classes=[MultiPartParser])
    def import_csv(self, request):
        """Upload → mapping → dry-run report → commit (SRS 3.2.6).

        Posting without `commit=true` classifies every row and writes nothing, so a mis-mapped
        column is found before five thousand rows land rather than after.
        """
        payload = ImportSerializer(data=request.data)
        payload.is_valid(raise_exception=True)
        upload = payload.validated_data["file"]
        try:
            headers, rows = services.read_csv(upload)
            mapping = payload.validated_data.get("mapping") or services.suggest_mapping(headers)
            report = services.import_contacts(
                rows,
                mapping=mapping,
                actor=request.user,
                roles=payload.validated_data.get("roles", ()),
                commit=payload.validated_data["commit"],
                filename=getattr(upload, "name", ""),
            )
        except Exception as exc:  # noqa: BLE001
            _translate(exc)
        return Response({"headers": headers, "mapping": mapping, **report})

    @extend_schema(
        parameters=[
            OpenApiParameter(
                "search", str, description="Same free-text filter as the list endpoint."
            )
        ],
        responses={200: OpenApiResponse(description="text/csv")},
    )
    @action(detail=False, methods=["get"])
    def export(self, request):
        from apps.identity.permissions import HasPermission

        # SRS 5.3 names export a privileged operation; the matrix code is seeded to every
        # staff role today (behavior-preserving) and admins tighten it at runtime.
        if not HasPermission("contacts.export")().has_permission(request, self):
            self.permission_denied(request, message="contacts.export required")
        """Stream the caller's visible contacts as CSV (SRS 3.2.6, 3.13.4).

        Exactly the rows `GET /contacts/` would return with the same filters — an export that
        ignored scoping would be the widest data leak in the product. SRS 5.3 names export a
        sensitive action, so the row count and the exporter are audited.
        """
        queryset = self.filter_queryset(self.get_queryset())
        writer = csv.writer(_Echo())
        response = StreamingHttpResponse(
            (writer.writerow(row) for row in services.export_rows(queryset, actor=request.user)),
            content_type="text/csv",
        )
        response["Content-Disposition"] = 'attachment; filename="contacts.csv"'
        return response
