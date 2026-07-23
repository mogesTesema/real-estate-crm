from rest_framework.routers import DefaultRouter

from .views import LeadViewSet

router = DefaultRouter()
router.register("leads", LeadViewSet, basename="lead")

urlpatterns = router.urls
