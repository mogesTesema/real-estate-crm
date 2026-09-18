"""Routes owned by `finance`.

Mounted by `config/urls.py` under /api/v1/.
"""
from django.urls import include, path
from rest_framework.routers import DefaultRouter

from .views import (
    AccountEntryViewSet,
    AccountViewSet,
    ChequeViewSet,
    CommissionPlanViewSet,
    CommissionViewSet,
    ExpenseViewSet,
    InstallmentPlanViewSet,
    InvoiceViewSet,
    OwnerStatementViewSet,
    PaymentViewSet,
    ReconciliationViewSet,
)

router = DefaultRouter()
router.register("accounts", AccountViewSet, basename="account")
router.register("account-entries", AccountEntryViewSet, basename="account-entry")
router.register("invoices", InvoiceViewSet, basename="invoice")
router.register("payments", PaymentViewSet, basename="payment")
router.register("cheques", ChequeViewSet, basename="cheque")
router.register("commission-plans", CommissionPlanViewSet, basename="commission-plan")
router.register("commissions", CommissionViewSet, basename="commission")
router.register("installment-plans", InstallmentPlanViewSet, basename="installment-plan")
router.register("expenses", ExpenseViewSet, basename="expense")
router.register("owner-statements", OwnerStatementViewSet, basename="owner-statement")
router.register("reconciliations", ReconciliationViewSet, basename="reconciliation")

urlpatterns = [path("", include(router.urls))]
