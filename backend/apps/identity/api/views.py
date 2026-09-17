"""Views for the identity API.

Every mutation delegates to `apps.identity.services`; no view touches the ORM to write.
Every list runs through `apps.identity.selectors.apply_scope`, per architecture.md §2.
"""
from django.core.exceptions import PermissionDenied as DjangoPermissionDenied
from django.core.exceptions import ValidationError as DjangoValidationError
from drf_spectacular.utils import OpenApiResponse, extend_schema
from rest_framework import mixins, status, viewsets
from rest_framework.decorators import action
from rest_framework.exceptions import PermissionDenied, ValidationError
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.throttling import ScopedRateThrottle
from rest_framework.views import APIView
from rest_framework_simplejwt.views import TokenObtainPairView

from .. import services
from ..models import Role, User
from ..selectors import apply_scope, scopes_for
from .permissions import CanRegisterUsers, PasswordIsCurrent
from .serializers import (
    AssignRoleSerializer,
    ChangePasswordSerializer,
    MeSerializer,
    RegistrationSerializer,
    RoleSerializer,
    UserSerializer,
    UserSummarySerializer,
    UserUpdateSerializer,
)

#: Scopes narrow enough that the holder gets the summary serializer rather than full detail.
_SUMMARY_ONLY_SCOPES = {Role.DataScope.OWN, Role.DataScope.MANAGED_PROPERTIES}


def _translate(exc):
    """Re-raise a service-layer Django exception as its DRF equivalent.

    Services raise Django exceptions so they are usable from a shell or a management command;
    DRF only renders its own. Without this, a PermissionDenied from a service would surface
    as a 500 rather than a 403.
    """
    if isinstance(exc, DjangoPermissionDenied):
        raise PermissionDenied(str(exc)) from exc
    if isinstance(exc, DjangoValidationError):
        raise ValidationError(
            exc.message_dict if hasattr(exc, "message_dict") else exc.messages
        ) from exc
    raise exc


class ThrottledTokenObtainPairView(TokenObtainPairView):
    """JWT login, rate-limited.

    An unthrottled login endpoint is a brute-force target, and this one is reachable
    unauthenticated by definition.
    """

    throttle_classes = [ScopedRateThrottle]
    throttle_scope = "login"


class MeView(APIView):
    """The session endpoint: who am I, and what may I do?"""

    permission_classes = [IsAuthenticated]
    allow_stale_password = True  # reachable while a password change is pending

    @extend_schema(responses=MeSerializer)
    def get(self, request):
        return Response(MeSerializer(request.user).data)

    @extend_schema(request=UserUpdateSerializer, responses=MeSerializer)
    def patch(self, request):
        serializer = UserUpdateSerializer(
            request.user, data=request.data, partial=True
        )
        serializer.is_valid(raise_exception=True)
        serializer.save()
        return Response(MeSerializer(request.user).data)


class ChangePasswordView(APIView):
    """Change one's own password. Clears the forced-change flag."""

    permission_classes = [IsAuthenticated]
    allow_stale_password = True  # the whole point is to reach this while stale

    @extend_schema(
        request=ChangePasswordSerializer,
        responses={200: OpenApiResponse(description="Password changed.")},
    )
    def post(self, request):
        serializer = ChangePasswordSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        try:
            services.change_password(
                user=request.user,
                current_password=serializer.validated_data["current_password"],
                new_password=serializer.validated_data["new_password"],
            )
        except (DjangoPermissionDenied, DjangoValidationError) as exc:
            _translate(exc)
        return Response({"detail": "Password changed."})


class UserViewSet(
    mixins.ListModelMixin,
    mixins.RetrieveModelMixin,
    mixins.CreateModelMixin,
    mixins.UpdateModelMixin,
    mixins.DestroyModelMixin,
    viewsets.GenericViewSet,
):
    """Register and administer people.

    `create` is registration. `destroy` deactivates — it never deletes, since roughly thirty
    PROTECT foreign keys point at User.

    Deactivated users stay in the list: this is a management surface, and hiding them would
    make deactivation irreversible over the API and invisible to an audit. Clients wanting
    only active people pass `?is_active=true`; other *apps* wanting an assignee list call
    `selectors.visible_users`, which filters to active by construction.
    """

    permission_classes = [IsAuthenticated, PasswordIsCurrent, CanRegisterUsers]
    filterset_fields = ["branch", "team", "is_active"]
    search_fields = ["first_name", "last_name", "email"]
    ordering_fields = ["first_name", "last_name", "created_at"]
    ordering = ["first_name", "last_name"]

    def get_queryset(self):
        qs = User.objects.filter(deleted_at__isnull=True).select_related(
            "branch", "branch__company", "team"
        ).prefetch_related("user_roles__role")
        return apply_scope(qs, self.request.user, "user")

    def get_serializer_class(self):
        if self.action == "create":
            return RegistrationSerializer
        if self.action in ("update", "partial_update"):
            return UserUpdateSerializer
        if self.action == "roles":
            return AssignRoleSerializer
        # Narrow scopes see the directory, not the full record. An anonymous user reaches
        # here only during schema generation — the permission classes stop real requests —
        # so describe the full shape rather than the narrow one.
        user = getattr(self.request, "user", None)
        if user is None or not user.is_authenticated or user.is_superuser:
            return UserSerializer
        scopes = scopes_for(user)
        if scopes and scopes.issubset(_SUMMARY_ONLY_SCOPES):
            return UserSummarySerializer
        return UserSerializer

    @extend_schema(request=RegistrationSerializer, responses={201: UserSerializer})
    def create(self, request, *args, **kwargs):
        serializer = RegistrationSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = dict(serializer.validated_data)
        try:
            user = services.register_user(
                actor=request.user,
                email=data.pop("email"),
                first_name=data.pop("first_name"),
                last_name=data.pop("last_name"),
                password=data.pop("password"),
                role_code=data.pop("role_code"),
                branch=data.pop("branch", None),
                team=data.pop("team", None),
                **data,
            )
        except (DjangoPermissionDenied, DjangoValidationError) as exc:
            _translate(exc)
        return Response(
            UserSerializer(user).data, status=status.HTTP_201_CREATED
        )

    @extend_schema(responses={204: OpenApiResponse(description="User deactivated.")})
    def destroy(self, request, *args, **kwargs):
        user = self.get_object()
        try:
            services.deactivate_user(actor=request.user, user=user)
        except (DjangoPermissionDenied, DjangoValidationError) as exc:
            _translate(exc)
        return Response(status=status.HTTP_204_NO_CONTENT)

    @extend_schema(
        request=None,
        responses={200: UserSerializer},
    )
    @action(detail=True, methods=["post"])
    def reactivate(self, request, pk=None):
        """Restore a deactivated account."""
        user = self.get_object()
        try:
            services.reactivate_user(actor=request.user, user=user)
        except (DjangoPermissionDenied, DjangoValidationError) as exc:
            _translate(exc)
        user.refresh_from_db()
        return Response(UserSerializer(user).data)

    @extend_schema(
        request=AssignRoleSerializer,
        responses={200: UserSerializer, 204: OpenApiResponse(description="Role revoked.")},
    )
    @action(detail=True, methods=["post", "delete"])
    def roles(self, request, pk=None):
        """Grant (POST) or revoke (DELETE) a role.

        Both run the same authority check as registration, so this cannot be used to escalate
        around it.
        """
        user = self.get_object()
        serializer = AssignRoleSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        role_code = serializer.validated_data["role_code"]

        try:
            if request.method == "DELETE":
                services.revoke_role(actor=request.user, user=user, role_code=role_code)
                return Response(status=status.HTTP_204_NO_CONTENT)
            services.assign_role(actor=request.user, user=user, role_code=role_code)
        except (DjangoPermissionDenied, DjangoValidationError) as exc:
            _translate(exc)
        user.refresh_from_db()
        return Response(UserSerializer(user).data)


class RoleViewSet(mixins.ListModelMixin, mixins.RetrieveModelMixin, viewsets.GenericViewSet):
    """The role catalogue, so a client can render the options it may grant."""

    queryset = Role.objects.all().order_by("code")
    serializer_class = RoleSerializer
    permission_classes = [IsAuthenticated, PasswordIsCurrent]
