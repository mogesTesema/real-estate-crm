from rest_framework.routers import DefaultRouter

from .views import OpportunityViewSet, PipelineViewSet, StageViewSet

router = DefaultRouter()
router.register("pipelines", PipelineViewSet, basename="pipeline")
router.register("stages", StageViewSet, basename="stage")
router.register("opportunities", OpportunityViewSet, basename="opportunity")

urlpatterns = router.urls
