"""Views for the identity API.

Every mutation delegates to `apps.identity.services`; no view touches the ORM to write.
Every list runs through `apps.identity.selectors.apply_scope`, per architecture.md §2.
"""
from django.core.exceptions import PermissionDenied as DjangoPermissionDenied
from django.core.exceptions import ValidationError as DjangoValidationError
from drf_spectacular.utils import OpenApiResponse, extend_schema
from rest_framework import mixins, status, viewsets
from rest_framework.decorators import action
from rest_framework.exceptions import AuthenticationFailed, PermissionDenied, ValidationError
from rest_framework.permissions import AllowAny, IsAuthenticated
from rest_framework.response import Response
from rest_framework.throttling import ScopedRateThrottle
from rest_framework.views import APIView
from rest_framework_simplejwt.exceptions import TokenError
from rest_framework_simplejwt.tokens import RefreshToken
from rest_framework_simplejwt.views import TokenObtainPairView

from .. import services, signals
from ..models import PortalProfile, Role, User
from ..selectors import ScopedQuerysetMixin, scopes_for
from .permissions import CanInvitePortalUsers, CanRegisterUsers, PasswordIsCurrent
from .serializers import (
    AssignRoleSerializer,
    ChangePasswordSerializer,
    ForgotPasswordSerializer,
    LoginSerializer,
    LogoutSerializer,
    MeSerializer,
    PortalAccessSerializer,
    PortalProfileSerializer,
    RegistrationSerializer,
    ResetPasswordSerializer,
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


def _client_meta(request) -> dict:
    """The forensic context SRS 5.3 wants on a login record."""
    forwarded = request.META.get("HTTP_X_FORWARDED_FOR", "")
    return {
        "ip_address": (forwarded.split(",")[0].strip() or None)
        if forwarded
        else request.META.get("REMOTE_ADDR"),
        "user_agent": request.META.get("HTTP_USER_AGENT", "")[:1000] or None,
    }


class LoginView(TokenObtainPairView):
    """JWT login: rate-limited, portal-aware, and audited.

    Three things happen here that stock `TokenObtainPairView` does not do:

    * **Throttling** — this endpoint is reachable unauthenticated and checks passwords, so
      unthrottled it is a brute-force target.
    * **Portal eligibility** — SRS 3.11.2 grants a portal client access only while they hold
      a completed contract. Checking at login (not just at invitation) means access ends when
      the contract does, without having to deactivate the account.
    * **Audit** — SRS 5.3 names login and failed login explicitly.
    """

    serializer_class = LoginSerializer
    throttle_classes = [ScopedRateThrottle]
    throttle_scope = "login"

    def post(self, request, *args, **kwargs):
        meta = _client_meta(request)
        try:
            response = super().post(request, *args, **kwargs)
        except AuthenticationFailed:
            signals.user_login_failed.send(
                sender=None, email=request.data.get("email", ""), **meta
            )
            raise

        user = getattr(self.request, "_authenticated_user", None)
        if user is not None:
            signals.user_logged_in.send(sender=None, user=user, **meta)
        return response


class LogoutView(APIView):
    """Revoke a refresh token.

    Note the limit, which is inherent to JWT rather than to this implementation: an access
    token already issued stays valid until it expires. `ACCESS_TOKEN_LIFETIME` is kept short
    for that reason. This endpoint stops the *refresh* token being exchanged for new ones,
    which is what ends a session.
    """

    permission_classes = [IsAuthenticated]
    allow_stale_password = True

    @extend_schema(
        request=LogoutSerializer,
        responses={205: OpenApiResponse(description="Refresh token revoked.")},
    )
    def post(self, request):
        serializer = LogoutSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        try:
            RefreshToken(serializer.validated_data["refresh"]).blacklist()
        except TokenError as exc:
            raise ValidationError({"refresh": "That token is invalid or already revoked."}) from exc
        return Response(status=status.HTTP_205_RESET_CONTENT)


class ForgotPasswordView(APIView):
    """Start a password reset.

    **Always returns 200**, whether or not the address exists. An endpoint that distinguishes
    the two is an account-enumeration oracle, and this one is public by necessity.
    """

    permission_classes = [AllowAny]
    authentication_classes: list = []
    throttle_classes = [ScopedRateThrottle]
    throttle_scope = "password_reset"

    @extend_schema(
        request=ForgotPasswordSerializer,
        responses={
            200: OpenApiResponse(description="Sent, if the account exists.")
        },
    )
    def post(self, request):
        serializer = ForgotPasswordSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        services.send_password_reset(email=serializer.validated_data["email"])
        return Response(
            {"detail": "If that email matches an account, reset instructions have been sent."}
        )


class ResetPasswordView(APIView):
    """Complete a password reset with the emailed token."""

    permission_classes = [AllowAny]
    authentication_classes: list = []
    throttle_classes = [ScopedRateThrottle]
    throttle_scope = "password_reset"

    @extend_schema(
        request=ResetPasswordSerializer,
        responses={200: OpenApiResponse(description="Password set.")},
    )
    def post(self, request):
        serializer = ResetPasswordSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        try:
            services.reset_password(**serializer.validated_data)
        except (DjangoPermissionDenied, DjangoValidationError) as exc:
            _translate(exc)
        return Response({"detail": "Password set. You can now sign in."})


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
    ScopedQuerysetMixin,
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

    scope_resource = "user"

    def get_unscoped_queryset(self):
        return (
            User.objects.filter(deleted_at__isnull=True)
            # A portal client is not a colleague. They hold a login but belong to no branch
            # and appear in no staff directory — SRS 3.11.6 makes their isolation a hard
            # rule, and listing them here would leak clients to every staff member.
            .exclude(user_roles__role__code=Role.PORTAL)
            .select_related("branch", "branch__company", "team")
            .prefetch_related("user_roles__role")
        )

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

        On DELETE, `role_code` may come from the body **or** the query string. A body on a
        DELETE is legal but awkward — some HTTP clients and intermediaries drop it silently —
        so `?role_code=finance` is offered as the more portable form.
        """
        user = self.get_object()
        data = request.data if request.data else {}
        if request.method == "DELETE" and not data.get("role_code"):
            data = {"role_code": request.query_params.get("role_code", "")}

        serializer = AssignRoleSerializer(data=data)
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


class PortalUserViewSet(
    mixins.ListModelMixin,
    mixins.RetrieveModelMixin,
    mixins.CreateModelMixin,
    mixins.DestroyModelMixin,
    viewsets.GenericViewSet,
):
    """Portal clients — buyers, sellers, rental tenants and landlords.

    Separate from `UserViewSet` because a client is not staff: they are created from a
    contact plus a verified contract rather than from a role grant (SRS 3.11.2), they never
    appear in the staff directory, and `destroy` suspends their access rather than
    deactivating an employee account.
    """

    permission_classes = [IsAuthenticated, PasswordIsCurrent, CanInvitePortalUsers]
    serializer_class = PortalProfileSerializer
    filterset_fields = ["portal_type", "eligibility_status"]
    ordering = ["-id"]

    def get_queryset(self):
        qs = PortalProfile.objects.select_related("user").filter(
            user__deleted_at__isnull=True
        )
        # SRS 3.11.6: no portal user may view another client's data. And staff see only the
        # client types they are responsible for — an agent's remit is buyers and sellers, so
        # a branch's rental tenants are not theirs to browse.
        user = getattr(self.request, "user", None)
        if user is None or not user.is_authenticated:
            # Only reachable during schema generation — the permission classes stop real
            # anonymous requests. AnonymousUser has no pk, so filtering on it would raise.
            return qs.none()
        if user.is_superuser:
            return qs

        invitable = services.invitable_portal_types(user)
        if invitable:
            return qs.filter(portal_type__in=invitable)
        # Not staff: a client sees themselves and nobody else.
        return qs.filter(user=user)

    def get_serializer_class(self):
        if self.action == "create":
            return PortalAccessSerializer
        return PortalProfileSerializer

    @extend_schema(request=PortalAccessSerializer, responses={201: PortalProfileSerializer})
    def create(self, request, *args, **kwargs):
        serializer = PortalAccessSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        try:
            user = services.grant_portal_access(
                actor=request.user, **serializer.validated_data
            )
        except (DjangoPermissionDenied, DjangoValidationError) as exc:
            _translate(exc)
        return Response(
            PortalProfileSerializer(user.portal_profile).data,
            status=status.HTTP_201_CREATED,
        )

    @extend_schema(responses={204: OpenApiResponse(description="Portal access suspended.")})
    def destroy(self, request, *args, **kwargs):
        profile = self.get_object()
        try:
            services.revoke_portal_access(
                actor=request.user,
                portal_profile=profile,
                reason=request.data.get("reason", "") if request.data else "",
            )
        except (DjangoPermissionDenied, DjangoValidationError) as exc:
            _translate(exc)
        return Response(status=status.HTTP_204_NO_CONTENT)


class RoleViewSet(mixins.ListModelMixin, mixins.RetrieveModelMixin, viewsets.GenericViewSet):
    """The role catalogue, so a client can render the options it may grant."""

    queryset = Role.objects.all().order_by("code")
    serializer_class = RoleSerializer
    permission_classes = [IsAuthenticated, PasswordIsCurrent]
