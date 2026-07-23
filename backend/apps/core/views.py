"""Core API: identity, org hierarchy, custom-field definitions."""
from rest_framework import serializers
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from .mixins import BaseTenantViewSet
from .models import Branch, Company, FieldDefinition, Team


# --- Serializers ------------------------------------------------------------
class MeSerializer(serializers.Serializer):
    id = serializers.UUIDField()
    email = serializers.EmailField()
    full_name = serializers.CharField()
    role = serializers.CharField()
    tenant = serializers.UUIDField(source="tenant_id", allow_null=True)
    branch = serializers.UUIDField(source="branch_id", allow_null=True)
    team = serializers.UUIDField(source="team_id", allow_null=True)


class CompanySerializer(serializers.ModelSerializer):
    class Meta:
        model = Company
        fields = ["id", "name", "created_at"]


class BranchSerializer(serializers.ModelSerializer):
    class Meta:
        model = Branch
        fields = ["id", "company", "name", "created_at"]


class TeamSerializer(serializers.ModelSerializer):
    class Meta:
        model = Team
        fields = ["id", "branch", "name", "created_at"]


class FieldDefinitionSerializer(serializers.ModelSerializer):
    class Meta:
        model = FieldDefinition
        fields = [
            "id",
            "target_model",
            "key",
            "label",
            "field_type",
            "options",
            "required",
        ]


# --- Views ------------------------------------------------------------------
class MeView(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request):
        return Response(MeSerializer(request.user).data)


class CompanyViewSet(BaseTenantViewSet):
    queryset = Company.objects.all()
    serializer_class = CompanySerializer
    admin_write = True


class BranchViewSet(BaseTenantViewSet):
    queryset = Branch.objects.all()
    serializer_class = BranchSerializer
    admin_write = True
    filterset_fields = ["company"]


class TeamViewSet(BaseTenantViewSet):
    queryset = Team.objects.all()
    serializer_class = TeamSerializer
    admin_write = True
    filterset_fields = ["branch"]


class FieldDefinitionViewSet(BaseTenantViewSet):
    queryset = FieldDefinition.objects.all()
    serializer_class = FieldDefinitionSerializer
    admin_write = True
    filterset_fields = ["target_model"]
