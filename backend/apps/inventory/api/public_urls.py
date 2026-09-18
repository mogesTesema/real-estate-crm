"""Public routes owned by `inventory`, mounted by `config/urls.py` under /api/public/."""
from django.urls import path

from .public import PublicListingDetailView, PublicListingListView

urlpatterns = [
    path("listings/", PublicListingListView.as_view(), name="public-listing-list"),
    path(
        "listings/<uuid:pk>/",
        PublicListingDetailView.as_view(),
        name="public-listing-detail",
    ),
]
