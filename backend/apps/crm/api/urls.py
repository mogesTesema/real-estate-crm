"""Routes owned by `crm`.

Mounted by `config/urls.py` under /api/v1/.
"""
from django.urls import include, path
from rest_framework.routers import DefaultRouter

from .views import (
    CampaignViewSet,
    ClosingChecklistViewSet,
    DealPropertyViewSet,
    DealViewSet,
    FieldSessionViewSet,
    LandingPageViewSet,
    LeadMatchesView,
    LeadSourceViewSet,
    LeadViewSet,
    ListingMatchingLeadsView,
    OfferViewSet,
    PipelineViewSet,
    RoutingRuleViewSet,
    SavedSearchAlertViewSet,
    TransactionViewSet,
    ViewingViewSet,
)

router = DefaultRouter()
router.register("leads", LeadViewSet, basename="lead")
router.register("deals", DealViewSet, basename="deal")
router.register("deal-properties", DealPropertyViewSet, basename="deal-property")
router.register("lead-sources", LeadSourceViewSet, basename="lead-source")
router.register("routing-rules", RoutingRuleViewSet, basename="routing-rule")
router.register("pipelines", PipelineViewSet, basename="pipeline")
router.register("viewings", ViewingViewSet, basename="viewing")
router.register("field-sessions", FieldSessionViewSet, basename="field-session")
router.register("offers", OfferViewSet, basename="offer")
router.register("transactions", TransactionViewSet, basename="transaction")
router.register("closing-checklists", ClosingChecklistViewSet, basename="closing-checklist")
router.register("campaigns", CampaignViewSet, basename="campaign")
router.register("landing-pages", LandingPageViewSet, basename="landing-page")
router.register("saved-search-alerts", SavedSearchAlertViewSet, basename="saved-search-alert")

urlpatterns = [
    path("leads/<uuid:pk>/matches/", LeadMatchesView.as_view(), name="lead-matches"),
    path(
        "listings/<uuid:pk>/matching-leads/",
        ListingMatchingLeadsView.as_view(),
        name="listing-matching-leads",
    ),
    path("", include(router.urls)),
]
