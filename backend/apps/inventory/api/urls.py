"""Routes owned by `inventory`.

Mounted by `config/urls.py` under /api/v1/.
"""
from django.urls import include, path
from rest_framework.routers import DefaultRouter

from .views import (
    ListingViewSet,
    MediaViewSet,
    PropertyTypeViewSet,
    PropertyViewSet,
    UnitViewSet,
)

router = DefaultRouter()
router.register("properties", PropertyViewSet, basename="property")
router.register("units", UnitViewSet, basename="unit")
router.register("listings", ListingViewSet, basename="listing")
router.register("media", MediaViewSet, basename="media")
router.register("property-types", PropertyTypeViewSet, basename="property-type")

urlpatterns = [path("", include(router.urls))]
