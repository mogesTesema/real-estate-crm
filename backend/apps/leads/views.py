from django.shortcuts import get_object_or_404
from django.utils import timezone
from rest_framework import status as http_status
from rest_framework.decorators import action
from rest_framework.response import Response

from apps.core.mixins import BaseTenantViewSet
from apps.deals.models import Pipeline, Stage
from apps.deals.serializers import OpportunitySerializer

from apps.core.models import Role, User

from .models import Lead, LeadSource
from .serializers import (
    AssignLeadSerializer,
    CaptureLeadSerializer,
    ConvertLeadSerializer,
    LeadSerializer,
    LeadSourceSerializer,
)
from .services import capture_lead, convert_lead


class LeadSourceViewSet(BaseTenantViewSet):
    queryset = LeadSource.objects.all().order_by("label")
    serializer_class = LeadSourceSerializer
    search_fields = ["key", "label"]
    filterset_fields = ["is_active"]
    admin_write = True
    admin_roles = {
        Role.SUPER_ADMIN,
        Role.OWNER,
        Role.MANAGER,
        Role.MARKETING,
    }
    scope_owner_field = None


class LeadViewSet(BaseTenantViewSet):
    queryset = Lead.objects.all().order_by("-created_at")
    serializer_class = LeadSerializer
    search_fields = ["name", "email", "phone", "preferred_location"]
    filterset_fields = ["lead_type", "status", "source", "assigned_agent", "sla_breached"]
    scope_owner_field = "assigned_agent"
    scope_branch_field = "assigned_agent__branch"

    def create(self, request, *args, **kwargs):
        """Capture a lead through the engine (dedupe/route/score/SLA/ack)."""
        ser = CaptureLeadSerializer(data=request.data)
        ser.is_valid(raise_exception=True)
        lead = capture_lead(
            tenant_id=request.user.tenant_id,
            data=ser.validated_data,
            actor=request.user,
        )
        return Response(
            LeadSerializer(lead).data, status=http_status.HTTP_201_CREATED
        )

    @action(detail=True, methods=["post"])
    def respond(self, request, pk=None):
        """Record first response — stops the SLA clock (SRS §3.1.9)."""
        lead = self.get_object()
        if lead.first_response_at is None:
            lead.first_response_at = timezone.now()
            lead.status = Lead.Status.CONTACTED
            lead.save(update_fields=["first_response_at", "status", "updated_at"])
        return Response(LeadSerializer(lead).data)

    @action(detail=True, methods=["post"])
    def convert(self, request, pk=None):
        """Convert a qualified lead into an opportunity (SRS §3.1.8)."""
        lead = self.get_object()
        ser = ConvertLeadSerializer(data=request.data)
        ser.is_valid(raise_exception=True)
        pipeline = get_object_or_404(
            Pipeline.objects, pk=ser.validated_data["pipeline"]
        )
        stage = get_object_or_404(Stage.objects, pk=ser.validated_data["stage"])
        opp = convert_lead(
            lead=lead, pipeline=pipeline, stage=stage, actor=request.user
        )
        return Response(
            OpportunitySerializer(opp).data, status=http_status.HTTP_201_CREATED
        )

    @action(detail=True, methods=["post"])
    def assign(self, request, pk=None):
        """Manually assign / reassign a lead to an agent (SRS §3.1.5)."""
        lead = self.get_object()
        ser = AssignLeadSerializer(data=request.data)
        ser.is_valid(raise_exception=True)
        agent = get_object_or_404(
            User.objects.filter(tenant_id=lead.tenant_id, is_active=True),
            pk=ser.validated_data["assigned_agent"],
        )
        lead.assigned_agent = agent
        lead.save(update_fields=["assigned_agent", "updated_at"])
        from apps.core.models import Notification

        Notification.objects.create(
            tenant_id=lead.tenant_id,
            user_id=agent.id,
            title="Lead reassigned",
            body=f"{lead.name or lead.email or 'Lead'} assigned to you",
            link=f"/leads?id={lead.id}",
        )
        return Response(LeadSerializer(lead).data)
