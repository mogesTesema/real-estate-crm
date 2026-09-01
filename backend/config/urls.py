"""Root URL configuration.

Foundation pass: no API layer yet (see apps/*/models.py). Only admin + a
trivial healthcheck are wired here; JWT/schema endpoints return once the
identity app's services/api layer exists in a later pass.
"""
from django.contrib import admin
from django.http import JsonResponse
from django.urls import path


def healthz(request):
    return JsonResponse({"status": "ok"})


urlpatterns = [
    path("admin/", admin.site.urls),
    path("healthz/", healthz, name="healthz"),
]
