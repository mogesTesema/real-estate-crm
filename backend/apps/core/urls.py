from django.urls import include, path
from rest_framework.routers import DefaultRouter

from . import views

router = DefaultRouter()
router.register("companies", views.CompanyViewSet, basename="company")
router.register("branches", views.BranchViewSet, basename="branch")
router.register("teams", views.TeamViewSet, basename="team")
router.register("field-definitions", views.FieldDefinitionViewSet, basename="fielddef")

urlpatterns = [
    path("me/", views.MeView.as_view(), name="me"),
    path("", include(router.urls)),
]
