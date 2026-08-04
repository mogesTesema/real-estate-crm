"""Core API: identity, org hierarchy, custom-field definitions, auth helpers."""
import re

from django.conf import settings
from django.contrib.auth.tokens import PasswordResetTokenGenerator
from django.utils.encoding import force_bytes, force_str
from django.utils.http import urlsafe_base64_decode, urlsafe_base64_encode
from rest_framework import serializers, status, viewsets
from rest_framework.decorators import action
from rest_framework.permissions import AllowAny, IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.integrations import get_email_provider

from .mixins import BaseTenantViewSet
from .models import Branch, Company, FieldDefinition, Notification, Team, Tenant, User
from .permissions import IsAuthenticatedInTenant, RolePermission
from .tenancy import set_current_tenant

_password_reset_tokens = PasswordResetTokenGenerator()


# --- Serializers ------------------------------------------------------------
class MeSerializer(serializers.Serializer):
    id = serializers.UUIDField()
    email = serializers.EmailField()
    full_name = serializers.CharField()
    role = serializers.CharField()
    tenant = serializers.UUIDField(source="tenant_id", allow_null=True)
    branch = serializers.UUIDField(source="branch_id", allow_null=True)
    team = serializers.UUIDField(source="team_id", allow_null=True)
    mfa_enabled = serializers.BooleanField()
    lead_routing_strategy = serializers.SerializerMethodField()

    def get_lead_routing_strategy(self, obj) -> str | None:
        if obj.tenant_id is None:
            return None
        return getattr(obj.tenant, "lead_routing_strategy", None)


class MeUpdateSerializer(serializers.Serializer):
    full_name = serializers.CharField(required=False, allow_blank=True, max_length=255)
    mfa_enabled = serializers.BooleanField(required=False)
    password = serializers.CharField(required=False, write_only=True, min_length=8)
    current_password = serializers.CharField(required=False, write_only=True)
    lead_routing_strategy = serializers.ChoiceField(
        choices=Tenant.LeadRoutingStrategy.choices,
        required=False,
    )


class UserSerializer(serializers.ModelSerializer):
    class Meta:
        model = User
        fields = ["id", "email", "full_name", "role", "branch", "team", "is_active"]
        read_only_fields = fields


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


class NotificationSerializer(serializers.ModelSerializer):
    class Meta:
        model = Notification
        fields = ["id", "title", "body", "link", "is_read", "created_at"]
        read_only_fields = ["id", "title", "body", "link", "created_at"]


class ForgotPasswordSerializer(serializers.Serializer):
    email = serializers.EmailField()


class ResetPasswordSerializer(serializers.Serializer):
    uid = serializers.CharField()
    token = serializers.CharField()
    password = serializers.CharField(min_length=8)


class MfaVerifySerializer(serializers.Serializer):
    code = serializers.CharField(min_length=6, max_length=6)


# --- Views ------------------------------------------------------------------
class MeView(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request):
        user = request.user
        if user.tenant_id:
            # Ensure tenant strategy is available without an extra round-trip.
            _ = user.tenant
        return Response(MeSerializer(user).data)

    def patch(self, request):
        ser = MeUpdateSerializer(data=request.data, partial=True)
        ser.is_valid(raise_exception=True)
        data = ser.validated_data
        user = request.user
        update_fields: list[str] = []

        if "full_name" in data:
            user.full_name = data["full_name"]
            update_fields.append("full_name")
        if "mfa_enabled" in data:
            user.mfa_enabled = data["mfa_enabled"]
            update_fields.append("mfa_enabled")
        if "password" in data:
            current = data.get("current_password") or ""
            if not user.check_password(current):
                return Response(
                    {"current_password": ["Current password is incorrect."]},
                    status=status.HTTP_400_BAD_REQUEST,
                )
            user.set_password(data["password"])
            update_fields.append("password")
        if update_fields:
            user.save(update_fields=update_fields)

        strategy = data.get("lead_routing_strategy")
        if strategy and user.tenant_id:
            if user.role not in (
                "super_admin",
                "owner",
                "manager",
            ) and not user.is_superuser:
                return Response(
                    {"lead_routing_strategy": ["Only managers and owners can change routing."]},
                    status=status.HTTP_403_FORBIDDEN,
                )
            Tenant.objects.filter(pk=user.tenant_id).update(
                lead_routing_strategy=strategy
            )
            user.tenant.refresh_from_db()

        return Response(MeSerializer(user).data)


class ForgotPasswordView(APIView):
    """Always 200 — do not reveal whether the email exists."""

    permission_classes = [AllowAny]
    authentication_classes = []

    def post(self, request):
        ser = ForgotPasswordSerializer(data=request.data)
        ser.is_valid(raise_exception=True)
        email = ser.validated_data["email"].lower()
        user = User.objects.filter(email__iexact=email, is_active=True).first()
        if user:
            uid = urlsafe_base64_encode(force_bytes(user.pk))
            token = _password_reset_tokens.make_token(user)
            origin = getattr(settings, "FRONTEND_ORIGIN", None) or "http://localhost:5173"
            reset_url = f"{origin.rstrip('/')}/reset-password?uid={uid}&token={token}"
            get_email_provider().send(
                to=user.email,
                subject="Reset your password",
                body=f"Use this link to reset your password:\n{reset_url}\n",
            )
        return Response({"detail": "If that email exists, a reset link was sent."})


class ResetPasswordView(APIView):
    permission_classes = [AllowAny]
    authentication_classes = []

    def post(self, request):
        ser = ResetPasswordSerializer(data=request.data)
        ser.is_valid(raise_exception=True)
        try:
            uid = force_str(urlsafe_base64_decode(ser.validated_data["uid"]))
            user = User.objects.get(pk=uid)
        except (User.DoesNotExist, ValueError, TypeError, OverflowError):
            return Response(
                {"detail": "Invalid or expired reset link."},
                status=status.HTTP_400_BAD_REQUEST,
            )
        if not _password_reset_tokens.check_token(user, ser.validated_data["token"]):
            return Response(
                {"detail": "Invalid or expired reset link."},
                status=status.HTTP_400_BAD_REQUEST,
            )
        user.set_password(ser.validated_data["password"])
        user.save(update_fields=["password"])
        return Response({"detail": "Password updated."})


class MfaVerifyView(APIView):
    """Stub MFA: accept any 6-digit code when the user has MFA enabled."""

    permission_classes = [IsAuthenticated]

    def post(self, request):
        ser = MfaVerifySerializer(data=request.data)
        ser.is_valid(raise_exception=True)
        code = ser.validated_data["code"]
        if not re.fullmatch(r"\d{6}", code):
            return Response(
                {"code": ["Enter a 6-digit code."]},
                status=status.HTTP_400_BAD_REQUEST,
            )
        if not request.user.mfa_enabled:
            return Response({"detail": "MFA is not enabled for this account."})
        return Response({"detail": "MFA verified.", "mfa_verified": True})


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


class UserViewSet(viewsets.ReadOnlyModelViewSet):
    """Tenant directory for assignee pickers (SRS §3.1.5 / lead assign)."""

    serializer_class = UserSerializer
    permission_classes = [IsAuthenticatedInTenant, RolePermission]
    search_fields = ["full_name", "email"]
    filterset_fields = ["role", "branch", "team", "is_active"]

    def initial(self, request, *args, **kwargs):
        super().initial(request, *args, **kwargs)
        tenant_id = getattr(request.user, "tenant_id", None)
        if tenant_id is not None:
            set_current_tenant(tenant_id)

    def get_queryset(self):
        user = self.request.user
        qs = User.objects.filter(is_active=True).order_by("full_name", "email")
        if user.is_superuser:
            return qs
        return qs.filter(tenant_id=user.tenant_id)


class NotificationViewSet(viewsets.ModelViewSet):
    """List + mark-read for the current user's notifications."""

    serializer_class = NotificationSerializer
    permission_classes = [IsAuthenticatedInTenant]
    http_method_names = ["get", "post", "head", "options"]

    def initial(self, request, *args, **kwargs):
        super().initial(request, *args, **kwargs)
        tenant_id = getattr(request.user, "tenant_id", None)
        if tenant_id is not None:
            set_current_tenant(tenant_id)

    def get_queryset(self):
        return Notification.objects.filter(user=self.request.user).order_by(
            "-created_at"
        )

    def create(self, request, *args, **kwargs):
        return Response(status=status.HTTP_405_METHOD_NOT_ALLOWED)

    @action(detail=True, methods=["post"], url_path="mark-read")
    def mark_read(self, request, pk=None):
        notif = self.get_object()
        if not notif.is_read:
            notif.is_read = True
            notif.save(update_fields=["is_read", "updated_at"])
        return Response(NotificationSerializer(notif).data)

    @action(detail=False, methods=["post"], url_path="mark-all-read")
    def mark_all_read(self, request):
        updated = self.get_queryset().filter(is_read=False).update(is_read=True)
        return Response({"updated": updated})
