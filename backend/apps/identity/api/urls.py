"""Routes owned by `identity`.

Mounted by `config/urls.py` under /api/v1/. Note that `config` sits outside import-linter's
`root_package = "apps"`, so the project wiring may import this module even though no *app*
may import another app's `api` package.
"""
from django.urls import include, path
from rest_framework.routers import DefaultRouter
from rest_framework_simplejwt.views import TokenRefreshView

from .views import (
    ChangePasswordView,
    CustomFieldViewSet,
    FieldPermissionViewSet,
    ForgotPasswordView,
    LoginView,
    LogoutView,
    MeView,
    PermissionViewSet,
    PortalUserViewSet,
    ResetPasswordView,
    RolePermissionViewSet,
    RoleViewSet,
    UserViewSet,
)

router = DefaultRouter()
router.register("users", UserViewSet, basename="user")
router.register("portal-users", PortalUserViewSet, basename="portal-user")
router.register("roles", RoleViewSet, basename="role")
router.register("permissions", PermissionViewSet, basename="permission")
router.register("role-permissions", RolePermissionViewSet, basename="role-permission")
router.register("field-permissions", FieldPermissionViewSet, basename="field-permission")
router.register("custom-fields", CustomFieldViewSet, basename="custom-field")

auth_patterns = [
    path("token/", LoginView.as_view(), name="token_obtain_pair"),
    path("token/refresh/", TokenRefreshView.as_view(), name="token_refresh"),
    path("logout/", LogoutView.as_view(), name="logout"),
    path("me/", MeView.as_view(), name="me"),
    path("change-password/", ChangePasswordView.as_view(), name="change_password"),
    path("forgot-password/", ForgotPasswordView.as_view(), name="forgot_password"),
    path("reset-password/", ResetPasswordView.as_view(), name="reset_password"),
]

urlpatterns = [
    path("auth/", include(auth_patterns)),
    path("", include(router.urls)),
]
