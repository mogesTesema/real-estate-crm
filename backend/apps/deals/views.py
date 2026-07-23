from django.shortcuts import get_object_or_404
from rest_framework import status as http_status
from rest_framework.decorators import action
from rest_framework.response import Response

from apps.core.mixins import BaseTenantViewSet

from .models import Opportunity, Pipeline, Stage
from .serializers import (
    MoveStageSerializer,
    OpportunitySerializer,
    PipelineSerializer,
    StageSerializer,
)
from .services import MissingReason, move_stage


class PipelineViewSet(BaseTenantViewSet):
    queryset = Pipeline.objects.all().order_by("name")
    serializer_class = PipelineSerializer
    admin_write = True
    scope_owner_field = None


class StageViewSet(BaseTenantViewSet):
    queryset = Stage.objects.all()
    serializer_class = StageSerializer
    admin_write = True
    filterset_fields = ["pipeline"]
    scope_owner_field = None


class OpportunityViewSet(BaseTenantViewSet):
    queryset = Opportunity.objects.all().order_by("-created_at")
    serializer_class = OpportunitySerializer
    search_fields = ["title", "contact__full_name"]
    filterset_fields = ["pipeline", "stage", "status", "assigned_agent", "contact"]
    scope_owner_field = "assigned_agent"
    scope_branch_field = "assigned_agent__branch"

    @action(detail=True, methods=["post"])
    def move_stage(self, request, pk=None):
        """Drag-to-stage endpoint: enforces reason + writes history (SRS §3.4.4)."""
        opp = self.get_object()
        ser = MoveStageSerializer(data=request.data)
        ser.is_valid(raise_exception=True)
        to_stage = get_object_or_404(
            Stage.objects, pk=ser.validated_data["to_stage"]
        )
        try:
            move_stage(
                opportunity=opp,
                to_stage=to_stage,
                user=request.user,
                reason=ser.validated_data["reason"],
                next_action=ser.validated_data.get("next_action", ""),
            )
        except (MissingReason, ValueError) as exc:
            return Response(
                {"detail": str(exc)}, status=http_status.HTTP_400_BAD_REQUEST
            )
        return Response(OpportunitySerializer(opp).data)

    @action(detail=False, methods=["get"])
    def board(self, request):
        """Kanban view: stages of a pipeline, each with its opportunities."""
        pipeline_id = request.query_params.get("pipeline")
        stages = Stage.objects.all()
        if pipeline_id:
            stages = stages.filter(pipeline_id=pipeline_id)
        opps = self.filter_queryset(self.get_queryset())
        columns = []
        for stage in stages.order_by("order"):
            stage_opps = [o for o in opps if o.stage_id == stage.id]
            columns.append(
                {
                    "stage": StageSerializer(stage).data,
                    "opportunities": OpportunitySerializer(stage_opps, many=True).data,
                }
            )
        return Response({"columns": columns})
