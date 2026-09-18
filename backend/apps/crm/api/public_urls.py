"""Public routes owned by `crm`, mounted by `config/urls.py` under /api/public/."""
from django.urls import path

from .public import (
    PublicInquiryView,
    PublicLandingPageSubmitView,
    PublicLandingPageView,
)

urlpatterns = [
    path("inquiries/", PublicInquiryView.as_view(), name="public-inquiry"),
    path("pages/<slug:slug>/", PublicLandingPageView.as_view(), name="public-page"),
    path(
        "pages/<slug:slug>/submit/",
        PublicLandingPageSubmitView.as_view(),
        name="public-page-submit",
    ),
]
