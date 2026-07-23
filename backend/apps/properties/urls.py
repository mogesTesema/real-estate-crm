from rest_framework.routers import DefaultRouter

from .views import ListingViewSet, PropertyViewSet

router = DefaultRouter()
router.register("properties", PropertyViewSet, basename="property")
router.register("listings", ListingViewSet, basename="listing")

urlpatterns = router.urls
