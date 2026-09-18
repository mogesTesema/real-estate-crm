"""Routes owned by `property_ops`.

Mounted by `config/urls.py` under /api/v1/.
"""
from django.urls import include, path
from rest_framework.routers import DefaultRouter

from .views import (
    ApplicationViewSet,
    DepositViewSet,
    InspectionViewSet,
    LeaseViewSet,
    MaintenanceRequestViewSet,
    RenewalViewSet,
    RentScheduleViewSet,
    VendorViewSet,
    WorkOrderViewSet,
)

router = DefaultRouter()
router.register("leases", LeaseViewSet, basename="lease")
router.register("rent-schedules", RentScheduleViewSet, basename="rent-schedule")
router.register("deposits", DepositViewSet, basename="deposit")
router.register("inspections", InspectionViewSet, basename="inspection")
router.register("applications", ApplicationViewSet, basename="application")
router.register("renewals", RenewalViewSet, basename="renewal")
router.register("vendors", VendorViewSet, basename="vendor")
router.register("maintenance-requests", MaintenanceRequestViewSet, basename="maintenance-request")
router.register("work-orders", WorkOrderViewSet, basename="work-order")

urlpatterns = [path("", include(router.urls))]
