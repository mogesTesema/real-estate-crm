from rest_framework.routers import DefaultRouter

from .views import LeadSourceViewSet, LeadViewSet

router = DefaultRouter()
router.register("leads", LeadViewSet, basename="lead")
router.register("lead-sources", LeadSourceViewSet, basename="lead-source")

urlpatterns = router.urls
