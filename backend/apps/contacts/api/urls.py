"""Routes owned by `contacts`.

Mounted by `config/urls.py` under /api/v1/. `config` sits outside import-linter's
`root_package = "apps"`, so the project wiring may import this module even though no *app* may
import another app's `api` package (architecture.md §1.2).
"""
from django.urls import include, path
from rest_framework.routers import DefaultRouter

from .views import ContactViewSet

router = DefaultRouter()
router.register("contacts", ContactViewSet, basename="contact")

urlpatterns = [path("", include(router.urls))]
