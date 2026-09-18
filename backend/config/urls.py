"""Root URL configuration.

Each app owns its own routes under `apps/<app>/api/urls.py` and this module mounts them.
`config` is deliberately outside import-linter's `root_package = "apps"`, which is what makes
the project able to import `apps.identity.api.urls` while no *app* may import another app's
`api` package (architecture.md §1.2).

`identity`, `contacts`, `inventory`, `crm` and `platform` are mounted; `property_ops`,
`finance` and the rest of `collaboration` arrive with their own passes.
"""
from django.contrib import admin
from django.http import JsonResponse
from django.urls import include, path
from drf_spectacular.views import (
    SpectacularAPIView,
    SpectacularRedocView,
    SpectacularSwaggerView,
)


def healthz(request):
    return JsonResponse({"status": "ok"})


api_v1 = [
    path("", include("apps.identity.api.urls")),
    path("", include("apps.contacts.api.urls")),
    path("", include("apps.inventory.api.urls")),
    path("", include("apps.crm.api.urls")),
    path("", include("apps.platform.api.urls")),
    path("", include("apps.collaboration.api.urls")),
    path("", include("apps.property_ops.api.urls")),
    path("", include("apps.finance.api.urls")),
]

# Unauthenticated, rate-limited endpoints for the public website: published-listing
# search, web-form lead capture, landing pages. E-sign token URLs stay under /api/v1/
# (they carry their own capability token).
api_public = [
    path("", include("apps.inventory.api.public_urls")),
    path("", include("apps.crm.api.public_urls")),
    path("", include("apps.platform.api.public_urls")),
]

urlpatterns = [
    path("admin/", admin.site.urls),
    path("healthz/", healthz, name="healthz"),
    path("api/v1/", include((api_v1, "api"), namespace="v1")),
    path("api/public/", include((api_public, "api-public"), namespace="public")),
    path("api/schema/", SpectacularAPIView.as_view(), name="schema"),
    path(
        "api/docs/",
        SpectacularSwaggerView.as_view(url_name="schema"),
        name="swagger-ui",
    ),
    path("api/redoc/", SpectacularRedocView.as_view(url_name="schema"), name="redoc"),
]
