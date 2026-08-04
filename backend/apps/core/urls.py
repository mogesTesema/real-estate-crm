from django.urls import include, path
from rest_framework.routers import DefaultRouter

from . import views

router = DefaultRouter()
router.register("companies", views.CompanyViewSet, basename="company")
router.register("branches", views.BranchViewSet, basename="branch")
router.register("teams", views.TeamViewSet, basename="team")
router.register("field-definitions", views.FieldDefinitionViewSet, basename="fielddef")
router.register("users", views.UserViewSet, basename="user")
router.register("notifications", views.NotificationViewSet, basename="notification")

urlpatterns = [
    path("me/", views.MeView.as_view(), name="me"),
    path("auth/forgot-password/", views.ForgotPasswordView.as_view(), name="forgot-password"),
    path("auth/reset-password/", views.ResetPasswordView.as_view(), name="reset-password"),
    path("auth/mfa/verify/", views.MfaVerifyView.as_view(), name="mfa-verify"),
    path("", include(router.urls)),
]
