"""Routes owned by `finance`.

Mounted by `config/urls.py` under /api/v1/.
"""
from django.urls import include, path
from rest_framework.routers import DefaultRouter

from .views import (
    AccountEntryViewSet,
    AccountViewSet,
    ChequeViewSet,
    InvoiceViewSet,
    PaymentViewSet,
)

router = DefaultRouter()
router.register("accounts", AccountViewSet, basename="account")
router.register("account-entries", AccountEntryViewSet, basename="account-entry")
router.register("invoices", InvoiceViewSet, basename="invoice")
router.register("payments", PaymentViewSet, basename="payment")
router.register("cheques", ChequeViewSet, basename="cheque")

urlpatterns = [path("", include(router.urls))]
