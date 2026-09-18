"""Public reads / scoped querysets for `finance` (architecture.md §1.2).

Other apps read `finance` rows through this module. They must never import
`apps.finance.api` — that package is the HTTP surface and is private to this app.
"""
from decimal import Decimal

from django.db.models import Sum

from apps.identity.selectors import apply_scope

from .models import Account, AccountEntry, Invoice, Payment


def live_invoices():
    return Invoice.objects.select_related("contact", "lease", "deal").prefetch_related("lines")


def visible_invoices(user):
    return apply_scope(live_invoices(), user, "invoice")


def live_payments():
    return Payment.objects.select_related("payer", "account", "recorded_by")


def visible_payments(user):
    return apply_scope(live_payments(), user, "payment")


def visible_accounts(user):
    return apply_scope(Account.objects.filter(is_active=True), user, "account")


def account_balance(account, *, as_of=None) -> Decimal:
    """Σ CREDIT − Σ DEBIT — the ledger convention, computed nowhere else."""
    entries = AccountEntry.objects.filter(account=account)
    if as_of is not None:
        entries = entries.filter(posted_at__lte=as_of)
    sums = entries.values("entry_type").annotate(total=Sum("amount"))
    by_type = {row["entry_type"]: row["total"] for row in sums}
    return (by_type.get("CREDIT") or Decimal("0")) - (by_type.get("DEBIT") or Decimal("0"))


def unallocated_amount(payment) -> Decimal:
    if payment.status != Payment.Status.POSTED:
        return Decimal("0")
    allocated = payment.allocations.aggregate(total=Sum("allocated_amount"))[
        "total"
    ] or Decimal("0")
    return payment.amount - allocated


def open_rent_invoices(lease):
    """Oldest due first — the cheque auto-allocation order and the portal's arrears view."""
    return (
        Invoice.objects.filter(
            lease=lease,
            invoice_type=Invoice.InvoiceType.RENT,
            status__in=(
                Invoice.Status.ISSUED, Invoice.Status.PARTIALLY_PAID, Invoice.Status.OVERDUE
            ),
        )
        .order_by("due_date")
    )
