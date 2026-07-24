"""Root URL configuration."""
from django.contrib import admin
from django.urls import include, path
from drf_spectacular.views import (
    SpectacularAPIView,
    SpectacularRedocView,
    SpectacularSwaggerView,
)
from rest_framework_simplejwt.views import (
    TokenObtainPairView,
    TokenRefreshView,
)

api_v1 = [
    path("auth/token/", TokenObtainPairView.as_view(), name="token_obtain_pair"),
    path("auth/token/refresh/", TokenRefreshView.as_view(), name="token_refresh"),
    path("", include("apps.core.urls")),
    path("", include("apps.contacts.urls")),
    path("", include("apps.properties.urls")),
    path("", include("apps.leads.urls")),
    path("", include("apps.deals.urls")),
    path("", include("apps.activities.urls")),
]

urlpatterns = [
    path("admin/", admin.site.urls),
    path("api/v1/", include((api_v1, "api"), namespace="v1")),
    # OpenAPI schema + Swagger UI (drives frontend type generation).
    path("api/schema/", SpectacularAPIView.as_view(), name="schema"),
    path(
        "api/docs/",
        SpectacularSwaggerView.as_view(url_name="schema"),
        name="swagger-ui",
    ),
    path(
        "api/redoc/",
        SpectacularRedocView.as_view(url_name="schema"),
        name="redoc",
    ),
]
