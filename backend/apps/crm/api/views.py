"""Views for the crm API (SRS §3.1, §3.4).

Every mutation delegates to `apps.crm.services`; no view touches the ORM to write. Every
queryset runs through `ScopedQuerysetMixin`, per architecture.md §2.
"""
from django.core.exceptions import PermissionDenied as DjangoPermissionDenied
from django.core.exceptions import ValidationError as DjangoValidationError
from django.shortcuts import get_object_or_404
from drf_spectacular.utils import OpenApiParameter, extend_schema
from rest_framework import mixins, status, viewsets
from rest_framework.decorators import action
from rest_framework.exceptions import PermissionDenied, ValidationError
from rest_framework.response import Response
from rest_framework.settings import api_settings

from apps.identity.permissions import IsAgencyAdmin
from apps.identity.selectors import ScopedQuerysetMixin

from .. import selectors, services
from ..models import (
    Deal,
    DealProperty,
    LeadRoutingRule,
    LeadSource,
    Pipeline,
    PipelineStage,
)
from .serializers import (
    BoardQuerySerializer,
    BoardSerializer,
    DealPropertySerializer,
    DealSerializer,
    DealStageHistorySerializer,
    DealUpdateSerializer,
    DealWriteSerializer,
    LeadAssignmentSerializer,
    LeadAssignSerializer,
    LeadCaptureSerializer,
    LeadConvertSerializer,
    LeadRespondSerializer,
    LeadSerializer,
    LeadSourceSerializer,
    LeadStatusHistorySerializer,
    LeadStatusSerializer,
    LeadUpdateSerializer,
    LinkPropertySerializer,
    MoveStageSerializer,
    PipelineSerializer,
    RoutingRuleSerializer,
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


class LeadSourceViewSet(mixins.ListModelMixin, viewsets.GenericViewSet):
    """Reference data — a source is a picklist entry, not a scoped record."""

    queryset = LeadSource.objects.filter(is_active=True).order_by("name")
    serializer_class = LeadSourceSerializer
    pagination_class = None


class PipelineViewSet(mixins.ListModelMixin, mixins.RetrieveModelMixin, viewsets.GenericViewSet):
    """Pipelines and their stages (SRS 3.4.1, 3.4.2) — configuration, not rows."""

    queryset = Pipeline.objects.filter(is_active=True).prefetch_related("stages")
    serializer_class = PipelineSerializer
    pagination_class = None


class RoutingRuleViewSet(viewsets.ModelViewSet):
    """Automated assignment rules (SRS 3.1.5).

    Not row-scoped — a rule is agency configuration. Who may *edit* one is a function-level
    permission question, which the default permission classes answer.
    """

    queryset = LeadRoutingRule.objects.all().order_by("priority", "created_at")
    serializer_class = RoutingRuleSerializer
    # A routing rule is not a row with an owner, so row scoping has nothing to say about it.
    # Without this gate any authenticated account — including a portal client — could write a
    # priority-0 catch-all sending every inbound lead to itself.
    permission_classes = [*api_settings.DEFAULT_PERMISSION_CLASSES, IsAgencyAdmin]
    filterset_fields = ["is_active", "assign_to_user", "assign_to_team"]
    ordering_fields = ["priority", "created_at", "name"]
    ordering = ["priority"]

    def perform_create(self, serializer):
        serializer.save(created_by=self.request.user)


class LeadViewSet(
    ScopedQuerysetMixin,
    mixins.ListModelMixin,
    mixins.RetrieveModelMixin,
    mixins.UpdateModelMixin,
    viewsets.GenericViewSet,
):
    """Leads and inquiries (SRS §3.1).

    `create` is `capture_lead` — de-duplication, scoring, routing, the SLA clock and the
    acknowledgment all happen behind it. There is deliberately no second create path.
    """

    scope_resource = "lead"
    filterset_fields = [
        "status", "lead_type", "priority", "source", "campaign",
        "assigned_agent", "assigned_team", "sla_breached", "is_possible_duplicate",
    ]
    search_fields = ["title", "description", "contact__first_name", "contact__last_name"]
    ordering_fields = ["created_at", "updated_at", "score", "sla_due_at", "next_follow_up_at"]
    ordering = ["-created_at"]

    def get_unscoped_queryset(self):
        return (
            selectors.live_leads()
            .select_related("contact", "assigned_agent", "source", "campaign", "target_property")
            .prefetch_related("location_preferences")
        )

    def get_serializer_class(self):
        if self.action == "create":
            return LeadCaptureSerializer
        if self.action in ("update", "partial_update"):
            return LeadUpdateSerializer
        if self.action == "status":
            return LeadStatusSerializer
        if self.action == "assign":
            return LeadAssignSerializer
        if self.action == "respond":
            return LeadRespondSerializer
        if self.action == "convert":
            return LeadConvertSerializer
        return LeadSerializer

    @extend_schema(request=LeadCaptureSerializer, responses={201: LeadSerializer})
    def create(self, request, *args, **kwargs):
        payload = LeadCaptureSerializer(data=request.data)
        payload.is_valid(raise_exception=True)
        data = dict(payload.validated_data)

        contact = None
        if data.pop("contact", None):
            from apps.contacts.selectors import live_contacts

            contact = get_object_or_404(live_contacts(), pk=payload.validated_data["contact"])
        contact_data = data.pop("contact_details", None)
        locations = data.pop("locations", ())

        for key in ("source", "campaign", "target_property"):
            if data.get(key):
                data[key] = self._resolve(key, data[key])

        try:
            lead = services.capture_lead(
                actor=request.user,
                contact=contact,
                contact_data=contact_data,
                locations=locations,
                **data,
            )
        except Exception as exc:  # noqa: BLE001 - narrowed by _translate
            _translate(exc)
        return Response(LeadSerializer(lead).data, status=status.HTTP_201_CREATED)

    def _resolve(self, key, value):
        """Turn an id from the payload into the object, refusing ones the caller cannot see.

        Sources and campaigns are agency configuration and not row-scoped. A target property
        is a record — resolving it unscoped would let a caller confirm any property id exists
        by watching whether the lead capture succeeds.
        """
        from apps.crm.models import Campaign
        from apps.inventory.selectors import visible_properties

        if key == "source":
            return get_object_or_404(LeadSource, pk=value)
        if key == "campaign":
            return get_object_or_404(Campaign, pk=value)
        return get_object_or_404(visible_properties(self.request.user), pk=value)

    @extend_schema(request=LeadUpdateSerializer, responses={200: LeadSerializer})
    def update(self, request, *args, **kwargs):
        lead = self.get_object()
        payload = LeadUpdateSerializer(
            instance=lead, data=request.data, partial=kwargs.pop("partial", False)
        )
        payload.is_valid(raise_exception=True)
        try:
            lead = services.update_lead(lead, actor=request.user, **payload.validated_data)
        except Exception as exc:  # noqa: BLE001 - narrowed by _translate
            _translate(exc)
        return Response(LeadSerializer(lead).data)

    @extend_schema(request=LeadStatusSerializer, responses={200: LeadSerializer})
    @action(detail=True, methods=["post"])
    def status(self, request, pk=None):
        lead = self.get_object()
        payload = LeadStatusSerializer(data=request.data)
        payload.is_valid(raise_exception=True)
        try:
            lead = services.change_lead_status(
                lead,
                payload.validated_data["status"],
                actor=request.user,
                reason=payload.validated_data.get("reason"),
            )
        except Exception as exc:  # noqa: BLE001
            _translate(exc)
        return Response(LeadSerializer(lead).data)

    @extend_schema(request=LeadAssignSerializer, responses={200: LeadSerializer})
    @action(detail=True, methods=["post"])
    def assign(self, request, pk=None):
        """Hand a lead to a person or to a team pool (SRS 3.1.5)."""
        from apps.identity.models import Team
        from apps.identity.selectors import visible_users

        lead = self.get_object()
        payload = LeadAssignSerializer(data=request.data)
        payload.is_valid(raise_exception=True)
        data = payload.validated_data
        to_user = (
            get_object_or_404(visible_users(request.user), pk=data["to_user"])
            if data.get("to_user")
            else None
        )
        to_team = get_object_or_404(Team, pk=data["to_team"]) if data.get("to_team") else None
        try:
            lead = services.assign_lead(
                lead, to_user=to_user, to_team=to_team, actor=request.user,
                reason=data.get("reason"),
            )
        except Exception as exc:  # noqa: BLE001
            _translate(exc)
        return Response(LeadSerializer(lead).data)

    @extend_schema(request=LeadRespondSerializer, responses={200: LeadSerializer})
    @action(detail=True, methods=["post"])
    def respond(self, request, pk=None):
        """Stop the SLA clock (SRS 3.1.9).

        Only the first response counts — the requirement measures time-to-first-contact, so a
        later call must not reset it and hide an original breach.
        """
        lead = self.get_object()
        payload = LeadRespondSerializer(data=request.data)
        payload.is_valid(raise_exception=True)
        lead = services.record_first_response(
            lead, actor=request.user, note=payload.validated_data.get("note")
        )
        return Response(LeadSerializer(lead).data)

    @extend_schema(request=LeadConvertSerializer, responses={201: DealSerializer})
    @action(detail=True, methods=["post"])
    def convert(self, request, pk=None):
        """Turn a qualified lead into a deal (SRS 3.1.8). Idempotent."""
        lead = self.get_object()
        payload = LeadConvertSerializer(data=request.data)
        payload.is_valid(raise_exception=True)
        data = dict(payload.validated_data)

        pipeline = (
            get_object_or_404(Pipeline, pk=data.pop("pipeline"))
            if data.get("pipeline")
            else Pipeline.objects.filter(is_default=True, is_active=True).first()
        )
        if pipeline is None:
            raise ValidationError({"pipeline": "No default pipeline is configured."})
        stage = (
            get_object_or_404(PipelineStage, pk=data.pop("stage")) if data.get("stage") else None
        )
        owner = None
        if data.get("owner"):
            from apps.identity.selectors import visible_users

            owner = get_object_or_404(visible_users(request.user), pk=data.pop("owner"))
        data.pop("owner", None)

        try:
            deal = services.convert_lead(
                lead, actor=request.user, pipeline=pipeline, stage=stage, owner=owner, **data
            )
        except Exception as exc:  # noqa: BLE001
            _translate(exc)
        return Response(DealSerializer(deal).data, status=status.HTTP_201_CREATED)

    @extend_schema(responses={200: LeadStatusHistorySerializer(many=True)})
    @action(detail=True, methods=["get"])
    def history(self, request, pk=None):
        lead = self.get_object()
        return Response(
            {
                "status_history": LeadStatusHistorySerializer(
                    lead.status_history.select_related("changed_by").order_by("-changed_at"),
                    many=True,
                ).data,
                "assignments": LeadAssignmentSerializer(
                    lead.assignments.select_related(
                        "from_user", "to_user", "assigned_by"
                    ).order_by("-assigned_at"),
                    many=True,
                ).data,
            }
        )

    @extend_schema(responses={200: LeadSerializer(many=True)})
    @action(detail=False, methods=["get"])
    def overdue(self, request):
        """Leads past their SLA with no response (SRS 3.1.9) — the follow-up work queue."""
        page = self.paginate_queryset(
            self.filter_queryset(self.get_queryset()).filter(
                sla_breached=True, first_response_at__isnull=True
            )
        )
        return self.get_paginated_response(LeadSerializer(page, many=True).data)


class DealViewSet(
    ScopedQuerysetMixin,
    mixins.ListModelMixin,
    mixins.RetrieveModelMixin,
    mixins.CreateModelMixin,
    mixins.UpdateModelMixin,
    viewsets.GenericViewSet,
):
    """The sales pipeline (SRS §3.4)."""

    scope_resource = "deal"
    filterset_fields = ["status", "pipeline", "stage", "owner", "deal_type", "currency"]
    search_fields = ["title", "reference_code"]
    ordering_fields = [
        "created_at", "updated_at", "estimated_value", "expected_close_date", "probability",
    ]
    ordering = ["-created_at"]

    def get_unscoped_queryset(self):
        return (
            selectors.live_deals()
            .select_related("stage", "pipeline", "owner", "primary_contact", "lead")
            .prefetch_related("deal_properties__property")
        )

    def get_serializer_class(self):
        if self.action == "create":
            return DealWriteSerializer
        if self.action in ("update", "partial_update"):
            return DealUpdateSerializer
        if self.action == "move":
            return MoveStageSerializer
        if self.action == "properties":
            return LinkPropertySerializer
        return DealSerializer

    @extend_schema(request=DealWriteSerializer, responses={201: DealSerializer})
    def create(self, request, *args, **kwargs):
        payload = DealWriteSerializer(data=request.data)
        payload.is_valid(raise_exception=True)
        data = dict(payload.validated_data)
        pipeline = data.pop("pipeline")
        stage = data.pop("stage", None)
        try:
            deal = services.create_deal(
                actor=request.user, pipeline=pipeline, stage=stage, **data
            )
        except Exception as exc:  # noqa: BLE001
            _translate(exc)
        return Response(DealSerializer(deal).data, status=status.HTTP_201_CREATED)

    @extend_schema(request=DealUpdateSerializer, responses={200: DealSerializer})
    def update(self, request, *args, **kwargs):
        deal = self.get_object()
        payload = DealUpdateSerializer(
            instance=deal, data=request.data, partial=kwargs.pop("partial", False)
        )
        payload.is_valid(raise_exception=True)
        try:
            deal = services.update_deal(deal, actor=request.user, **payload.validated_data)
        except Exception as exc:  # noqa: BLE001
            _translate(exc)
        return Response(DealSerializer(deal).data)

    @extend_schema(request=MoveStageSerializer, responses={200: DealSerializer})
    @action(detail=True, methods=["post"])
    def move(self, request, pk=None):
        """Move a deal between stages (SRS 3.4.4).

        A separate endpoint, not a PATCH field: the reason is mandatory and the history row is
        part of the move, and neither survives a client that simply sets `stage`.
        """
        deal = self.get_object()
        payload = MoveStageSerializer(data=request.data)
        payload.is_valid(raise_exception=True)
        stage = get_object_or_404(PipelineStage, pk=payload.validated_data["stage"])
        try:
            deal = services.move_stage(
                deal,
                stage,
                actor=request.user,
                reason=payload.validated_data["reason"],
                next_action=payload.validated_data.get("next_action"),
            )
        except Exception as exc:  # noqa: BLE001
            _translate(exc)
        return Response(DealSerializer(deal).data)

    @extend_schema(responses={200: DealStageHistorySerializer(many=True)})
    @action(detail=True, methods=["get"], url_path="stage-history")
    def stage_history(self, request, pk=None):
        rows = (
            self.get_object()
            .stage_history.select_related("changed_by", "from_stage", "to_stage")
            .order_by("-changed_at")
        )
        return Response(DealStageHistorySerializer(rows, many=True).data)

    @extend_schema(
        request=LinkPropertySerializer, responses={201: DealPropertySerializer}
    )
    @action(detail=True, methods=["get", "post"])
    def properties(self, request, pk=None):
        """The properties an opportunity is about (SRS 3.4.7)."""
        from apps.inventory.models import Unit
        from apps.inventory.selectors import visible_properties

        deal = self.get_object()
        if request.method == "GET":
            return Response(
                DealPropertySerializer(
                    deal.deal_properties.select_related("property", "unit"), many=True
                ).data
            )
        payload = LinkPropertySerializer(data=request.data)
        payload.is_valid(raise_exception=True)
        data = payload.validated_data
        prop = get_object_or_404(visible_properties(request.user), pk=data["property"])
        unit = get_object_or_404(Unit, pk=data["unit"]) if data.get("unit") else None
        try:
            link = services.link_property(
                deal, property=prop, unit=unit, is_primary=data["is_primary"],
                actor=request.user,
            )
        except Exception as exc:  # noqa: BLE001
            _translate(exc)
        return Response(DealPropertySerializer(link).data, status=status.HTTP_201_CREATED)

    @action(detail=True, methods=["post"], url_path="create-lease")
    def create_lease(self, request, pk=None):
        """§1.2's letting hand-off: a won RENTAL deal becomes a DRAFT lease with the property
        manager's explicit commercial terms — never derived from the deal's estimate."""
        from apps.property_ops import services as property_ops_services

        deal = self.get_object()
        try:
            lease = property_ops_services.create_lease_from_deal(
                deal=deal,
                actor=request.user,
                transaction=deal.transactions.exclude(status="CANCELLED").first(),
                start_date=request.data.get("start_date"),
                end_date=request.data.get("end_date"),
                rent_amount=request.data.get("rent_amount"),
                billing_frequency=request.data.get("billing_frequency", "MONTHLY"),
                security_deposit=request.data.get("security_deposit", 0),
            )
        except Exception as exc:  # noqa: BLE001
            _translate(exc)
        return Response(
            {"lease_id": str(lease.pk), "reference_code": lease.reference_code,
             "status": lease.status},
            status=status.HTTP_201_CREATED,
        )

    @extend_schema(
        parameters=[
            OpenApiParameter("pipeline", str, description="Pipeline id; defaults to the default."),
            OpenApiParameter("owner", str, description="Filter to one owner."),
            OpenApiParameter("deal_type", str),
        ],
        responses={200: BoardSerializer},
    )
    @action(detail=False, methods=["get"])
    def board(self, request):
        """The Kanban board (SRS 3.4.6).

        A *view* of the deals the caller may already see — it composes with row scoping and
        the same filters as the list, never a second way to reach a deal.
        """
        # Validated, not passed through. `?owner=not-a-uuid` reached Django's UUID field as a
        # raw string and raised a bare ValidationError that DRF does not render — a 500 on a
        # malformed query string, from any authenticated user.
        query = BoardQuerySerializer(data=request.query_params)
        query.is_valid(raise_exception=True)
        params = query.validated_data

        pipeline = (
            get_object_or_404(Pipeline, pk=params["pipeline"]) if params.get("pipeline") else None
        )
        filters = {
            key: params[key]
            for key in ("owner", "deal_type", "currency")
            if params.get(key)
        }
        data = selectors.board(request.user, pipeline=pipeline, filters=filters)
        forecast = selectors.weighted_pipeline(
            selectors.visible_deals(request.user).filter(status=Deal.Status.OPEN)
        )
        return Response(BoardSerializer({**data, "weighted_forecast": forecast}).data)


class DealPropertyViewSet(
    ScopedQuerysetMixin, mixins.DestroyModelMixin, viewsets.GenericViewSet
):
    """Unlink a property from a deal. Scoped through the deal it hangs off."""

    scope_resource = "deal_property"
    serializer_class = DealPropertySerializer

    def get_unscoped_queryset(self):
        return DealProperty.objects.select_related("deal", "property")

    def destroy(self, request, *args, **kwargs):
        # Through the service, not DestroyModelMixin's instance.delete(). Every other write
        # in this module goes through `crm.services`, and the one that does not is the one
        # that will still be calling the ORM directly when unlinking grows a side effect.
        services.unlink_property(self.get_object(), actor=request.user)
        return Response(status=status.HTTP_204_NO_CONTENT)


class ViewingViewSet(
    ScopedQuerysetMixin,
    mixins.ListModelMixin,
    mixins.RetrieveModelMixin,
    mixins.CreateModelMixin,
    viewsets.GenericViewSet,
):
    """Property viewings (SRS 3.3.11).

    Scheduling puts the appointment on the unified calendar through
    `collaboration.services.upsert_activity_for_source` — §1.2 lists that as a required
    orchestration, and §13 forbids a standalone VIEWING activity.
    """

    scope_resource = "viewing"
    filterset_fields = ["status", "agent", "property", "deal", "lead"]
    ordering_fields = ["scheduled_start", "created_at"]
    ordering = ["-scheduled_start"]

    def get_unscoped_queryset(self):
        from ..models import Viewing

        return Viewing.objects.select_related("property", "agent", "contact", "deal", "lead")

    def get_serializer_class(self):
        from .serializers import (
            CheckInSerializer,
            ViewingCompleteSerializer,
            ViewingRescheduleSerializer,
            ViewingScheduleSerializer,
            ViewingSerializer,
        )

        return {
            "create": ViewingScheduleSerializer,
            "reschedule": ViewingRescheduleSerializer,
            "complete": ViewingCompleteSerializer,
            "check_in": CheckInSerializer,
        }.get(self.action, ViewingSerializer)

    def create(self, request, *args, **kwargs):
        from apps.contacts.selectors import live_contacts
        from apps.identity.selectors import visible_users
        from apps.inventory.selectors import visible_properties

        from .serializers import ViewingScheduleSerializer, ViewingSerializer

        payload = ViewingScheduleSerializer(data=request.data)
        payload.is_valid(raise_exception=True)
        data = dict(payload.validated_data)

        prop = get_object_or_404(visible_properties(request.user), pk=data.pop("property"))
        contact = get_object_or_404(live_contacts(), pk=data.pop("contact"))
        agent = (
            get_object_or_404(visible_users(request.user), pk=data.pop("agent"))
            if data.get("agent")
            else request.user
        )
        data.pop("agent", None)
        for key, queryset in (
            ("lead", selectors.live_leads()),
            ("deal", selectors.live_deals()),
        ):
            if data.get(key):
                data[key] = get_object_or_404(queryset, pk=data[key])

        try:
            viewing = services.schedule_viewing(
                actor=request.user, property=prop, contact=contact, agent=agent, **data
            )
        except Exception as exc:  # noqa: BLE001 - narrowed by _translate
            _translate(exc)
        return Response(ViewingSerializer(viewing).data, status=status.HTTP_201_CREATED)

    @action(detail=True, methods=["post"])
    def reschedule(self, request, pk=None):
        """Move an appointment. The calendar entry is updated, never duplicated."""
        from .serializers import ViewingRescheduleSerializer, ViewingSerializer

        viewing = self.get_object()
        payload = ViewingRescheduleSerializer(data=request.data)
        payload.is_valid(raise_exception=True)
        try:
            viewing = services.reschedule_viewing(
                viewing, actor=request.user, **payload.validated_data
            )
        except Exception as exc:  # noqa: BLE001
            _translate(exc)
        return Response(ViewingSerializer(viewing).data)

    @action(detail=True, methods=["post"])
    def complete(self, request, pk=None):
        """Close the appointment out with the client's feedback (SRS 3.3.11)."""
        from .serializers import ViewingCompleteSerializer, ViewingSerializer

        viewing = self.get_object()
        payload = ViewingCompleteSerializer(data=request.data)
        payload.is_valid(raise_exception=True)
        try:
            viewing = services.complete_viewing(
                viewing, actor=request.user, **payload.validated_data
            )
        except Exception as exc:  # noqa: BLE001
            _translate(exc)
        return Response(ViewingSerializer(viewing).data)

    @action(detail=True, methods=["post"], url_path="check-in")
    def check_in(self, request, pk=None):
        """Stamp the agent's arrival, with coordinates when the device offered them (3.16.4)."""
        from .serializers import CheckInSerializer, ViewingSerializer

        viewing = self.get_object()
        payload = CheckInSerializer(data=request.data)
        payload.is_valid(raise_exception=True)
        try:
            viewing = services.check_in_to_viewing(
                viewing, actor=request.user, **payload.validated_data
            )
        except Exception as exc:  # noqa: BLE001
            _translate(exc)
        return Response(ViewingSerializer(viewing).data)


class FieldSessionViewSet(
    ScopedQuerysetMixin,
    mixins.ListModelMixin,
    mixins.RetrieveModelMixin,
    mixins.CreateModelMixin,
    viewsets.GenericViewSet,
):
    """GPS-tracked field visits (SRS 3.16.5–3.16.7).

    This is location data about an employee. Three controls apply and all three are needed:
    row scoping decides whose sessions are visible, the trail is append-only at the database
    role, and every *supervisory* read of a trail writes an audit row.
    """

    scope_resource = "field_session"
    filterset_fields = ["status", "agent", "session_type"]
    ordering_fields = ["started_at", "ended_at"]
    ordering = ["-started_at"]

    def get_unscoped_queryset(self):
        from ..models import AgentFieldSession

        return AgentFieldSession.objects.select_related("agent").prefetch_related(
            "location_points"
        )

    def get_serializer_class(self):
        from .serializers import (
            AppendPointsSerializer,
            EndFieldSessionSerializer,
            FieldSessionSerializer,
            StartFieldSessionSerializer,
        )

        return {
            "create": StartFieldSessionSerializer,
            "end": EndFieldSessionSerializer,
            "points": AppendPointsSerializer,
        }.get(self.action, FieldSessionSerializer)

    def create(self, request, *args, **kwargs):
        from .serializers import FieldSessionSerializer, StartFieldSessionSerializer

        payload = StartFieldSessionSerializer(data=request.data)
        payload.is_valid(raise_exception=True)
        data = dict(payload.validated_data)
        for key, queryset in (
            ("lead", selectors.live_leads()),
            ("deal", selectors.live_deals()),
            ("viewing", selectors.visible_viewings(request.user)),
        ):
            if data.get(key):
                data[key] = get_object_or_404(queryset, pk=data[key])
            else:
                data.pop(key, None)
        if data.get("property"):
            from apps.inventory.selectors import visible_properties

            data["property"] = get_object_or_404(
                visible_properties(request.user), pk=data["property"]
            )
        else:
            data.pop("property", None)

        try:
            session = services.start_field_session(actor=request.user, **data)
        except Exception as exc:  # noqa: BLE001
            _translate(exc)
        return Response(FieldSessionSerializer(session).data, status=status.HTTP_201_CREATED)

    @action(detail=True, methods=["post"])
    def end(self, request, pk=None):
        from .serializers import EndFieldSessionSerializer, FieldSessionSerializer

        session = self.get_object()
        payload = EndFieldSessionSerializer(data=request.data)
        payload.is_valid(raise_exception=True)
        session = services.end_field_session(
            session, actor=request.user, **payload.validated_data
        )
        return Response(FieldSessionSerializer(session).data)

    @extend_schema(methods=["GET"], responses={200: None})
    @action(detail=True, methods=["get", "post"])
    def points(self, request, pk=None):
        """Append to, or read, the location trail.

        A GET by anyone other than the tracked agent writes a `platform_audit_event` — SRS
        3.16.7's "role-restricted **and audited**". Row scoping is the restriction; without
        the audit the requirement is half met.
        """
        from .serializers import AppendPointsSerializer, LocationPointReadSerializer

        session = self.get_object()
        if request.method == "GET":
            trail = services.read_location_trail(session, actor=request.user)
            return Response(LocationPointReadSerializer(trail, many=True).data)

        payload = AppendPointsSerializer(data=request.data)
        payload.is_valid(raise_exception=True)
        try:
            rows = services.record_location_points(
                session, actor=request.user, points=payload.validated_data["points"]
            )
        except Exception as exc:  # noqa: BLE001
            _translate(exc)
        return Response(
            {"recorded": len(rows)}, status=status.HTTP_201_CREATED
        )
