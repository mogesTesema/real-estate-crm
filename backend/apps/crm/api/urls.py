"""Routes owned by `crm`.

Mounted by `config/urls.py` under /api/v1/.
"""
from django.urls import include, path
from rest_framework.routers import DefaultRouter

from .views import (
    DealPropertyViewSet,
    DealViewSet,
    LeadSourceViewSet,
    LeadViewSet,
    PipelineViewSet,
    RoutingRuleViewSet,
)

router = DefaultRouter()
router.register("leads", LeadViewSet, basename="lead")
router.register("deals", DealViewSet, basename="deal")
router.register("deal-properties", DealPropertyViewSet, basename="deal-property")
router.register("lead-sources", LeadSourceViewSet, basename="lead-source")
router.register("routing-rules", RoutingRuleViewSet, basename="routing-rule")
router.register("pipelines", PipelineViewSet, basename="pipeline")

urlpatterns = [path("", include(router.urls))]
