"""Public routes owned by `platform`, mounted by `config/urls.py` under /api/public/."""
from django.urls import path

from .public import InboundWebhookView

urlpatterns = [
    path("webhooks/<uuid:pk>/", InboundWebhookView.as_view(), name="public-webhook"),
]
