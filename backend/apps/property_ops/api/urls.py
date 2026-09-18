"""Routes owned by `property_ops`.

Mounted by `config/urls.py` under /api/v1/.
"""
from django.urls import include, path
from rest_framework.routers import DefaultRouter

from .views import DepositViewSet, LeaseViewSet, RentScheduleViewSet

router = DefaultRouter()
router.register("leases", LeaseViewSet, basename="lease")
router.register("rent-schedules", RentScheduleViewSet, basename="rent-schedule")
router.register("deposits", DepositViewSet, basename="deposit")

urlpatterns = [path("", include(router.urls))]
