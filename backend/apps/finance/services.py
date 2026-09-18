"""Public write API for `finance` (architecture.md §1.2, §11) — the sole money writer.

Every create/update/post of money goes through this module. No other app writes a finance
row; no signal creates one; the DAG's forbidden-patterns list names both. The one sanctioned
outbound write is stamping `property_ops.RentSchedule.invoice/status` when a period is
invoiced — the RentSchedule docstring grants exactly that.

**Ledger convention, stated once**: `finance_account_entry` is a cash register per account.
Money in = CREDIT, money out = DEBIT, balance = ΣCREDIT − ΣDEBIT. The table is append-only
at the database-role level — a posted entry is never edited; corrections are opposite
entries.

**The one invariant everything else leans on**: `Invoice.amount_paid` equals the sum of
allocations from POSTED payments, and `balance_due = total_amount − amount_paid` (a CHECK).
`_refresh_invoice` is the only code allowed to write those two fields, and it always writes
them together. A grep-guard test holds that exclusivity.
"""
import logging
from datetime import timedelta
from decimal import ROUND_HALF_UP, Decimal

from django.conf import settings as django_settings
from django.core.exceptions import ValidationError
from django.db import transaction
from django.db.models import Q, Sum
from django.utils import timezone

from apps.core.services import next_reference

from . import signals
from .models import (
    Account,
    AccountEntry,
    Cheque,
    Commission,
    CommissionPlan,
    CommissionSplit,
    InstallmentMilestone,
    InstallmentPlan,
    Invoice,
    InvoiceLine,
    Payment,
    PaymentAllocation,
)

logger = logging.getLogger(__name__)

TWO_PLACES = Decimal("0.01")


def _reject_unknown(fields, allowed, what):
    unknown = set(fields) - allowed
    if unknown:
        raise ValidationError(
            {name: f"This field cannot be set through the {what} service." for name in unknown}
        )


def _company_setting(key, default):
    """Config knobs live in `identity_company.settings` JSON (single-company system) with a
    Django-settings fallback — VAT rates and late-fee policy are operations decisions, not
    deployments."""
    from apps.identity.models import Company

    company = Company.objects.first()
    if company is not None and isinstance(company.settings, dict) and key in company.settings:
        return company.settings[key]
    return getattr(django_settings, f"FINANCE_{key.upper()}", default)


def _vat_rate() -> Decimal:
    return Decimal(str(_company_setting("vat_rate", "0")))


def _default_currency() -> str:
    from apps.identity.models import Company

    company = Company.objects.first()
    return company.default_currency if company else "AED"


# --- Ledger ---------------------------------------------------------------------------------


def _value_datetime(value_date):
    """Midday UTC on the value date — an aware timestamp for a DATE-grained business fact."""
    import datetime

    if isinstance(value_date, str):
        value_date = datetime.date.fromisoformat(value_date)
    return timezone.make_aware(
        datetime.datetime.combine(value_date, datetime.time(12, 0)), datetime.UTC
    )


def _post_entry(*, account, entry_type, amount, reference_type, reference_id,
                description=None, currency=None, posted_at=None):
    return AccountEntry.objects.create(
        account=account,
        entry_type=entry_type,
        amount=amount,
        currency=currency or account.currency,
        reference_type=reference_type,
        reference_id=reference_id,
        description=description,
        posted_at=posted_at or timezone.now(),
    )


@transaction.atomic
def create_account(*, actor, name, account_type, currency, account_number=None):
    return Account.objects.create(
        name=name, account_type=account_type, currency=currency,
        account_number=account_number,
    )


@transaction.atomic
def update_account(account, *, actor, **fields):
    _reject_unknown(fields, frozenset({"name", "account_number", "is_active"}), "account")
    for name, value in fields.items():
        setattr(account, name, value)
    account.save()
    return account


@transaction.atomic
def post_adjustment(*, actor, account, entry_type, amount, description,
                    reference_type=AccountEntry.ReferenceType.ADJUSTMENT, reference_id=None,
                    posted_on=None):
    """The only public door for a raw ledger entry — manual corrections, reconciliation
    adjustments. `posted_on` back-dates the entry's VALUE date: a correction for January's
    discrepancy must land in January, or the reconciliation it fixes can never see it."""
    if amount <= 0:
        raise ValidationError({"amount": "Ledger amounts are positive; direction is the type."})
    if not (description or "").strip():
        raise ValidationError({"description": "A manual ledger entry needs its reason."})
    return _post_entry(
        account=account, entry_type=entry_type, amount=amount,
        reference_type=reference_type, reference_id=reference_id, description=description,
        posted_at=_value_datetime(posted_on) if posted_on else None,
    )


# --- Invoices (SRS 3.6.2, 3.6.7) ------------------------------------------------------------


def _compute_lines(lines):
    """Line arithmetic, one place. `tax_rate=None` means "the company default VAT"
    (SRS 3.6.7); an explicit 0 means genuinely untaxed."""
    computed, subtotal, tax_total = [], Decimal("0"), Decimal("0")
    if not lines:
        raise ValidationError({"lines": "An invoice needs at least one line."})
    for line in lines:
        quantity = Decimal(str(line["quantity"]))
        unit_price = Decimal(str(line["unit_price"]))
        if quantity <= 0:
            raise ValidationError({"lines": "Line quantity must be positive."})
        rate = line.get("tax_rate")
        rate = _vat_rate() if rate is None else Decimal(str(rate))
        base = (quantity * unit_price).quantize(TWO_PLACES, ROUND_HALF_UP)
        tax = (base * rate / 100).quantize(TWO_PLACES, ROUND_HALF_UP)
        computed.append(
            {
                "description": line["description"],
                "quantity": quantity,
                "unit_price": unit_price,
                "tax_rate": rate,
                "tax_amount": tax,
                "line_total": base + tax,
                "property": line.get("property"),
            }
        )
        subtotal += base
        tax_total += tax
    return computed, subtotal, tax_total


@transaction.atomic
def create_invoice(*, actor, contact, invoice_type, issue_date, due_date, lines,
                   currency=None, deal=None, lease=None, transaction_obj=None,
                   discount_amount=Decimal("0"), notes=None, issue=False):
    computed, subtotal, tax_total = _compute_lines(lines)
    discount = Decimal(str(discount_amount))
    total = subtotal + tax_total - discount
    if total < 0:
        raise ValidationError({"discount_amount": "Discount exceeds the invoice value."})

    invoice = Invoice.objects.create(
        invoice_number=next_reference("invoice", prefix="INV"),
        contact=contact,
        deal=deal,
        lease=lease,
        transaction=transaction_obj,
        invoice_type=invoice_type,
        status=Invoice.Status.ISSUED if issue else Invoice.Status.DRAFT,
        issue_date=issue_date,
        due_date=due_date,
        currency=currency or _default_currency(),
        subtotal=subtotal,
        tax_amount=tax_total,
        discount_amount=discount,
        total_amount=total,
        amount_paid=Decimal("0"),
        balance_due=total,  # set with total in the same INSERT — the CHECK holds
        notes=notes,
        created_by=actor,
    )
    InvoiceLine.objects.bulk_create(
        [InvoiceLine(invoice=invoice, **line) for line in computed]
    )
    if issue:
        signals.invoice_issued.send_robust(sender=None, invoice=invoice, actor=actor)
    return invoice


@transaction.atomic
def update_draft_invoice(invoice, *, actor, lines=None, **fields):
    if invoice.status != Invoice.Status.DRAFT:
        raise ValidationError({"status": "Only a draft invoice is editable; void and reissue."})
    _reject_unknown(
        fields, frozenset({"issue_date", "due_date", "notes", "discount_amount"}), "invoice"
    )
    for name, value in fields.items():
        setattr(invoice, name, value)
    if lines is not None:
        computed, subtotal, tax_total = _compute_lines(lines)
        invoice.lines.all().delete()
        InvoiceLine.objects.bulk_create(
            [InvoiceLine(invoice=invoice, **line) for line in computed]
        )
        invoice.subtotal, invoice.tax_amount = subtotal, tax_total
    total = invoice.subtotal + invoice.tax_amount - invoice.discount_amount
    if total < 0:
        raise ValidationError({"discount_amount": "Discount exceeds the invoice value."})
    invoice.total_amount = total
    invoice.balance_due = total - invoice.amount_paid
    invoice.save()
    return invoice


@transaction.atomic
def issue_invoice(invoice, *, actor):
    if invoice.status != Invoice.Status.DRAFT:
        raise ValidationError({"status": "Only a draft invoice can be issued."})
    invoice.status = Invoice.Status.ISSUED
    invoice.save(update_fields=["status", "updated_at"])
    signals.invoice_issued.send_robust(sender=None, invoice=invoice, actor=actor)
    return invoice


@transaction.atomic
def void_invoice(invoice, *, actor, reason, status=Invoice.Status.VOID):
    """Kill an invoice that has taken no money. Anything POSTED-allocated is history now —
    reverse the payment first; a voided invoice with cash against it is a books error."""
    if not (reason or "").strip():
        raise ValidationError({"reason": "Voiding a money document needs a reason."})
    if status not in (Invoice.Status.VOID, Invoice.Status.CANCELLED):
        raise ValidationError({"status": "An invoice is voided or cancelled, nothing else."})
    if invoice.status in (Invoice.Status.VOID, Invoice.Status.CANCELLED):
        return invoice
    if invoice.allocations.filter(payment__status=Payment.Status.POSTED).exists():
        raise ValidationError(
            {"status": "Payments are allocated to this invoice; reverse them first."}
        )

    from apps.property_ops.models import Lease, RentSchedule

    old_status = invoice.status
    # Detach dependents CHECK-safely: milestones drop to PENDING *before* losing the FK
    # (the INVOICED-requires-invoice CHECK), rent periods reopen or cancel with their lease.
    for milestone in invoice.installment_milestones.all():
        milestone.status = InstallmentMilestone.Status.PENDING
        milestone.invoice = None
        milestone.save(update_fields=["status", "invoice"])
    for schedule in invoice.rent_schedules.select_related("lease"):
        schedule.status = (
            RentSchedule.Status.CANCELLED
            if schedule.lease.status == Lease.Status.TERMINATED
            else RentSchedule.Status.SCHEDULED
        )
        schedule.invoice = None
        schedule.save(update_fields=["status", "invoice"])

    invoice.status = status
    invoice.save(update_fields=["status", "updated_at"])
    signals.invoice_voided.send_robust(
        sender=None, invoice=invoice, actor=actor, reason=reason, old_status=old_status
    )
    return invoice


def _refresh_invoice(invoice):
    """THE invariant-holder: amount_paid = Σ POSTED allocations; balance follows; status
    walks. The only code that writes these fields, always together, always one save."""
    paid = invoice.allocations.filter(
        payment__status=Payment.Status.POSTED
    ).aggregate(total=Sum("allocated_amount"))["total"] or Decimal("0")

    invoice.amount_paid = paid
    invoice.balance_due = invoice.total_amount - paid
    fields = ["amount_paid", "balance_due", "updated_at"]

    if invoice.status not in (
        Invoice.Status.DRAFT, Invoice.Status.VOID, Invoice.Status.CANCELLED
    ):
        if paid <= 0:
            overdue = invoice.due_date < timezone.localdate()
            invoice.status = Invoice.Status.OVERDUE if overdue else Invoice.Status.ISSUED
        elif paid < invoice.total_amount:
            invoice.status = Invoice.Status.PARTIALLY_PAID
        else:
            invoice.status = Invoice.Status.PAID
        fields.append("status")

    invoice.save(update_fields=fields)
    _sync_rent_schedule(invoice)
    _sync_milestones(invoice)
    return invoice


def _sync_rent_schedule(invoice):
    """The sanctioned cross-app write: rent periods mirror their invoice (model docstring:
    "`invoice` is written only by finance.services")."""
    from apps.property_ops.models import RentSchedule

    mirror = {
        Invoice.Status.PAID: RentSchedule.Status.PAID,
        Invoice.Status.PARTIALLY_PAID: RentSchedule.Status.PARTIALLY_PAID,
        Invoice.Status.ISSUED: RentSchedule.Status.INVOICED,
        Invoice.Status.OVERDUE: RentSchedule.Status.OVERDUE,
    }
    target = mirror.get(invoice.status)
    if target:
        invoice.rent_schedules.exclude(status=target).update(status=target)


def _sync_milestones(invoice):
    for milestone in invoice.installment_milestones.select_related("plan"):
        if invoice.status == Invoice.Status.PAID:
            if milestone.status != InstallmentMilestone.Status.PAID:
                milestone.status = InstallmentMilestone.Status.PAID
                milestone.paid_at = timezone.now()
                milestone.save(update_fields=["status", "paid_at"])
            plan = milestone.plan
            if not plan.milestones.exclude(
                status__in=(
                    InstallmentMilestone.Status.PAID, InstallmentMilestone.Status.WAIVED
                )
            ).exists():
                plan.status = InstallmentPlan.Status.COMPLETED
                plan.save(update_fields=["status", "updated_at"])
        elif milestone.status == InstallmentMilestone.Status.PAID:
            milestone.status = InstallmentMilestone.Status.INVOICED
            milestone.paid_at = None
            milestone.save(update_fields=["status", "paid_at"])


# --- Payments and allocation (SRS 3.6) ------------------------------------------------------


@transaction.atomic
def record_payment(*, actor, payer, account, amount, payment_method, payment_date,
                   currency=None, allocations=None, external_reference=None, notes=None,
                   _entry_reference=None):
    """Post money in. One CREDIT ledger entry; optional inline allocations; audited.

    `_entry_reference` is the cheque path's hook: a payment created by `clear_cheque` posts
    its cash-in referenced to the CHEQUE, which is what makes the PDC register reconcile
    against the ledger.
    """
    amount = Decimal(str(amount))
    if amount <= 0:
        raise ValidationError({"amount": "A payment must be positive."})

    payment = Payment.objects.create(
        payment_reference=next_reference("payment", prefix="PAY"),
        payer=payer,
        account=account,
        amount=amount,
        currency=currency or account.currency,
        payment_method=payment_method,
        payment_date=payment_date,
        external_reference=external_reference,
        notes=notes,
        recorded_by=actor,
        status=Payment.Status.POSTED,
    )
    reference_type, reference_id = _entry_reference or (
        AccountEntry.ReferenceType.PAYMENT, payment.pk
    )
    _post_entry(
        account=account,
        entry_type=AccountEntry.EntryType.CREDIT,
        amount=amount,
        currency=payment.currency,
        reference_type=reference_type,
        reference_id=reference_id,
        description=f"Payment {payment.payment_reference}",
        # The VALUE date, not the recording moment: a January rent recorded in February
        # belongs to January's reconciliation, or the bank statement never matches.
        posted_at=_value_datetime(payment_date),
    )
    if allocations:
        allocate_payment(payment, actor=actor, allocations=allocations)

    signals.payment_posted.send_robust(
        sender=None, payment=payment, actor=actor, allocations=allocations
    )
    return payment


@transaction.atomic
def allocate_payment(payment, *, actor, allocations):
    """Apply a payment to invoices. `allocations` = [{"invoice": ..., "amount": delta}].

    Amounts are DELTAS, and a repeat allocation to the same invoice adjusts the existing
    row — UNIQUE(payment, invoice) means top-ups never stack. No ledger entry: the cash
    entered the account when the payment posted; allocation only says which bill it answers.
    """
    payment = Payment.objects.select_for_update().get(pk=payment.pk)
    if payment.status != Payment.Status.POSTED:
        raise ValidationError({"payment": "A reversed payment allocates nothing."})

    resolved = []
    for entry in allocations:
        invoice = entry["invoice"]
        if not isinstance(invoice, Invoice):
            invoice = Invoice.objects.get(pk=invoice)
        delta = Decimal(str(entry["amount"]))
        if delta <= 0:
            raise ValidationError({"allocations": "Allocation amounts must be positive."})
        resolved.append((invoice, delta))

    # Lock invoices in a stable order — two clerks allocating overlapping sets must queue,
    # not deadlock.
    ids = [invoice.pk for invoice, _ in resolved]
    locked = {
        inv.pk: inv
        for inv in Invoice.objects.select_for_update()
        .filter(pk__in=ids)
        .order_by("invoice_number")
    }

    already = payment.allocations.aggregate(total=Sum("allocated_amount"))[
        "total"
    ] or Decimal("0")
    incoming = sum(delta for _, delta in resolved)
    if already + incoming > payment.amount:
        raise ValidationError(
            {"allocations": "Allocations would exceed the payment amount."}
        )

    rows = []
    for invoice, delta in resolved:
        invoice = locked[invoice.pk]
        if invoice.status not in (
            Invoice.Status.ISSUED, Invoice.Status.PARTIALLY_PAID, Invoice.Status.OVERDUE
        ):
            raise ValidationError(
                {"allocations": f"{invoice.invoice_number} is not open for payment."}
            )
        if invoice.currency != payment.currency:
            raise ValidationError(
                {"allocations": f"{invoice.invoice_number} is billed in {invoice.currency}; "
                                f"this payment is {payment.currency}."}
            )
        if delta > invoice.balance_due:
            raise ValidationError(
                {"allocations": f"{invoice.invoice_number} has only "
                                f"{invoice.balance_due} outstanding."}
            )
        allocation, created = PaymentAllocation.objects.get_or_create(
            payment=payment, invoice=invoice, defaults={"allocated_amount": delta}
        )
        if not created:
            allocation.allocated_amount += delta
            allocation.save(update_fields=["allocated_amount"])
        rows.append(allocation)
        _refresh_invoice(invoice)
        _notify_rent_payment(invoice, payment, actor)
    return rows


def _notify_rent_payment(invoice, payment, actor):
    if invoice.invoice_type == Invoice.InvoiceType.RENT and invoice.lease_id:
        from apps.collaboration import services as collaboration_services

        collaboration_services.notify(
            recipient=invoice.lease.property_manager,
            type="PAYMENT_RECEIVED",
            title=f"Rent received: {invoice.invoice_number}",
            body=f"{payment.amount} {payment.currency} from {payment.payer}",
            entity_type="PAYMENT",
            entity_id=payment.pk,
            actor=actor,
        )


@transaction.atomic
def reverse_payment(payment, *, actor, reason, refund=False):
    """Take money back out — never by editing. The compensating DEBIT is the correction;
    the allocation rows are kept as history and excluded from every sum by the payment's
    status; the invoices regress to whatever the remaining money says."""
    if not (reason or "").strip():
        raise ValidationError({"reason": "Reversing money needs a reason."})
    payment = Payment.objects.select_for_update().get(pk=payment.pk)
    if payment.status != Payment.Status.POSTED:
        raise ValidationError({"status": "Only a posted payment can be reversed."})

    payment.status = Payment.Status.REFUNDED if refund else Payment.Status.REVERSED
    payment.save(update_fields=["status"])
    _post_entry(
        account=payment.account,
        entry_type=AccountEntry.EntryType.DEBIT,
        amount=payment.amount,
        currency=payment.currency,
        reference_type=AccountEntry.ReferenceType.PAYMENT,
        reference_id=payment.pk,
        description=f"Reversal of {payment.payment_reference}: {reason}",
    )
    for allocation in payment.allocations.select_related("invoice"):
        _refresh_invoice(allocation.invoice)

    signals.payment_reversed.send_robust(
        sender=None, payment=payment, actor=actor, reason=reason, refund=refund
    )
    return payment


# --- Cheques — the PDC register (§11) -------------------------------------------------------

CHEQUE_TRANSITIONS = {
    Cheque.Status.HELD_IN_SAFE: {
        Cheque.Status.DEPOSITED, Cheque.Status.RETURNED, Cheque.Status.REPLACED
    },
    Cheque.Status.DEPOSITED: {Cheque.Status.CLEARED, Cheque.Status.BOUNCED},
    # A bounced cheque can be re-presented, swapped for a new one, or handed back.
    Cheque.Status.BOUNCED: {
        Cheque.Status.DEPOSITED, Cheque.Status.REPLACED, Cheque.Status.RETURNED
    },
    Cheque.Status.CLEARED: set(),
    Cheque.Status.REPLACED: set(),
    Cheque.Status.RETURNED: set(),
}


def _cheque_move(cheque, new_status):
    allowed = CHEQUE_TRANSITIONS.get(cheque.status, set())
    if new_status not in allowed:
        raise ValidationError(
            {"status": f"Cannot move a cheque from {cheque.status} to {new_status}."
                       + (f" Allowed: {sorted(allowed)}." if allowed else " It is settled.")}
        )
    cheque.status = new_status


@transaction.atomic
def register_cheque(*, actor, payer, drawer_name, bank_name, cheque_number, amount,
                    cheque_date, lease=None):
    amount = Decimal(str(amount))
    if amount <= 0:
        raise ValidationError({"amount": "A cheque must be positive."})
    return Cheque.objects.create(
        payer=payer, drawer_name=drawer_name, bank_name=bank_name,
        cheque_number=cheque_number, amount=amount, cheque_date=cheque_date, lease=lease,
    )


@transaction.atomic
def deposit_cheque(cheque, *, actor, account):
    _cheque_move(cheque, Cheque.Status.DEPOSITED)
    cheque.deposit_account = account
    cheque.save(update_fields=["status", "deposit_account", "updated_at"])
    return cheque


@transaction.atomic
def clear_cheque(cheque, *, actor, allocations=None, cleared_at=None):
    """The moment paper becomes money: CLEARED creates the Payment, whose ledger entry is
    CHEQUE-referenced. With no explicit allocations and a lease on the cheque, it pays the
    lease's open rent oldest-due-first — the thing a PDC exists to do."""
    _cheque_move(cheque, Cheque.Status.CLEARED)
    cleared_at = cleared_at or timezone.now()
    cheque.cleared_at = cleared_at
    cheque.save(update_fields=["status", "cleared_at", "updated_at"])

    if allocations is None and cheque.lease_id:
        allocations = _oldest_first_allocations(cheque.lease, cheque.amount)

    payment = record_payment(
        actor=actor,
        payer=cheque.payer,
        account=cheque.deposit_account,
        amount=cheque.amount,
        payment_method=Payment.PaymentMethod.CHEQUE,
        payment_date=cleared_at.date(),
        external_reference=cheque.cheque_number,
        allocations=allocations,
        _entry_reference=(AccountEntry.ReferenceType.CHEQUE, cheque.pk),
    )
    return cheque, payment


def _oldest_first_allocations(lease, budget):
    open_invoices = (
        Invoice.objects.filter(
            lease=lease,
            invoice_type=Invoice.InvoiceType.RENT,
            status__in=(
                Invoice.Status.ISSUED, Invoice.Status.PARTIALLY_PAID, Invoice.Status.OVERDUE
            ),
        )
        .order_by("due_date")
    )
    allocations, remaining = [], Decimal(budget)
    for invoice in open_invoices:
        if remaining <= 0:
            break
        slice_amount = min(remaining, invoice.balance_due)
        if slice_amount > 0:
            allocations.append({"invoice": invoice, "amount": slice_amount})
            remaining -= slice_amount
    return allocations


@transaction.atomic
def bounce_cheque(cheque, *, actor, reason, bounced_at=None):
    """No payment existed yet — the payment is only born at clearing — so there is nothing
    to reverse; the rent it was meant to cover simply stays unpaid and the sweep marks it
    OVERDUE. A clawback *after* clearing is `reverse_payment`, not a cheque transition."""
    if not (reason or "").strip():
        raise ValidationError({"reason": "A bounce needs the bank's reason."})
    _cheque_move(cheque, Cheque.Status.BOUNCED)
    cheque.bounced_at = bounced_at or timezone.now()
    cheque.bounce_reason = reason
    cheque.save(update_fields=["status", "bounced_at", "bounce_reason", "updated_at"])

    signals.cheque_bounced.send_robust(sender=None, cheque=cheque, actor=actor, reason=reason)
    if cheque.lease_id:
        from apps.collaboration import services as collaboration_services

        collaboration_services.notify(
            recipient=cheque.lease.property_manager,
            type="SYSTEM",
            title=f"Cheque bounced: {cheque.cheque_number}",
            body=reason,
            entity_type="CHEQUE",
            entity_id=cheque.pk,
            actor=actor,
        )
    return cheque


@transaction.atomic
def replace_cheque(cheque, *, actor, **new_cheque_fields):
    _cheque_move(cheque, Cheque.Status.REPLACED)
    cheque.save(update_fields=["status", "updated_at"])
    replacement = register_cheque(
        actor=actor,
        payer=new_cheque_fields.pop("payer", cheque.payer),
        lease=new_cheque_fields.pop("lease", cheque.lease),
        drawer_name=new_cheque_fields.pop("drawer_name", cheque.drawer_name),
        bank_name=new_cheque_fields.pop("bank_name", cheque.bank_name),
        **new_cheque_fields,
    )
    return cheque, replacement


@transaction.atomic
def return_cheque(cheque, *, actor, reason=None):
    _cheque_move(cheque, Cheque.Status.RETURNED)
    cheque.save(update_fields=["status", "updated_at"])
    return cheque


# --- Rent invoicing (SRS 3.5.3) — the activate_lease ↔ recurring-command split --------------


def generate_rent_schedule_invoices(*, actor=None, lease=None, as_of=None, horizon_days=None):
    """Invoice every SCHEDULED rent period due within the horizon. Idempotent by
    construction: the schedule row itself is the claim — `invoice IS NULL` under a row lock —
    so overlapping runs cannot double-invoice (§1.3: "finance tasks keyed by business
    reference codes").

    `created_by` falls back to the lease's property manager: the recurring command has no
    human actor, and Invoice.created_by is NOT NULL by design.
    """
    from apps.property_ops.models import Lease, RentSchedule

    as_of = as_of or timezone.localdate()
    horizon = horizon_days if horizon_days is not None else int(
        _company_setting("rent_invoice_horizon_days", 14)
    )
    cutoff = as_of + timedelta(days=horizon)

    invoiceable_statuses = (
        Lease.Status.ACTIVE, Lease.Status.EXPIRING, Lease.Status.RENEWED
    )
    created = []
    with transaction.atomic():
        due = (
            RentSchedule.objects.select_for_update(of=("self",))
            .filter(
                status=RentSchedule.Status.SCHEDULED,
                invoice__isnull=True,
                due_date__lte=cutoff,
                lease__status__in=invoiceable_statuses,
            )
            .select_related(
                "lease", "lease__tenant", "lease__property", "lease__property_manager"
            )
        )
        if lease is not None:
            due = due.filter(lease=lease)

        for row in due:
            row_lease = row.lease
            vat_applies = (
                row_lease.lease_type == Lease.LeaseType.COMMERCIAL
                and bool(_company_setting("vat_on_rent", False))
            )
            invoice = create_invoice(
                actor=actor or row_lease.property_manager,
                contact=row_lease.tenant,
                invoice_type=Invoice.InvoiceType.RENT,
                lease=row_lease,
                issue_date=as_of,
                due_date=row.due_date,
                lines=[
                    {
                        "description": (
                            f"Rent {row.period_start} – {row.period_end} "
                            f"({row_lease.reference_code})"
                        ),
                        "quantity": 1,
                        "unit_price": row.amount + row.late_fee_amount,
                        "tax_rate": None if vat_applies else Decimal("0"),
                        "property": row_lease.property,
                    }
                ],
                issue=True,
            )
            row.invoice = invoice
            row.status = RentSchedule.Status.INVOICED
            row.save(update_fields=["invoice", "status"])
            created.append(invoice)
    return created


def sweep_overdue(*, as_of=None):
    """Everything past due, in one idempotent pass: invoices → OVERDUE, rent periods →
    OVERDUE with a **one-shot** late fee (the `late_fee_amount == 0` guard is the
    idempotency — a fee that compounds on every sweep run is not a policy, it's a bug),
    milestones → OVERDUE. The fee lands on the period's NEXT invoice; an issued invoice is
    never mutated."""
    from apps.property_ops.models import RentSchedule

    as_of = as_of or timezone.localdate()
    late_fee = _company_setting("late_fee", {}) or {}
    grace = int(late_fee.get("grace_days", 0))
    counts = {"invoices": 0, "rent_periods": 0, "milestones": 0}

    with transaction.atomic():
        counts["invoices"] = Invoice.objects.filter(
            status__in=(Invoice.Status.ISSUED, Invoice.Status.PARTIALLY_PAID),
            due_date__lt=as_of,
        ).update(status=Invoice.Status.OVERDUE)

        overdue_rows = RentSchedule.objects.select_for_update().filter(
            status__in=(RentSchedule.Status.INVOICED, RentSchedule.Status.PARTIALLY_PAID),
            due_date__lt=as_of - timedelta(days=grace),
        )
        for row in overdue_rows:
            fields = ["status"]
            row.status = RentSchedule.Status.OVERDUE
            if late_fee and row.late_fee_amount == 0:
                mode, value = late_fee.get("mode"), Decimal(str(late_fee.get("value", 0)))
                if mode == "FIXED":
                    row.late_fee_amount = value
                elif mode == "PERCENT":
                    row.late_fee_amount = (row.amount * value / 100).quantize(
                        TWO_PLACES, ROUND_HALF_UP
                    )
                fields.append("late_fee_amount")
            row.save(update_fields=fields)
            counts["rent_periods"] += 1
            _notify_overdue(row)

        counts["milestones"] = InstallmentMilestone.objects.filter(
            status__in=(
                InstallmentMilestone.Status.PENDING, InstallmentMilestone.Status.INVOICED
            ),
            due_date__lt=as_of,
        ).update(status=InstallmentMilestone.Status.OVERDUE)
    return counts


def _notify_overdue(row):
    from apps.collaboration import services as collaboration_services

    collaboration_services.notify(
        recipient=row.lease.property_manager,
        type="SYSTEM",
        title=f"Rent overdue: {row.lease.reference_code}",
        body=f"Period {row.period_start} – {row.period_end}, due {row.due_date}.",
        entity_type="RENT_SCHEDULE",
        entity_id=row.pk,
    )


# --- Expenses (SRS 3.14.4) ------------------------------------------------------------------

EXPENSE_CATEGORIES = frozenset(
    {"MAINTENANCE", "UTILITIES", "MANAGEMENT", "TAX", "INSURANCE", "MARKETING",
     "PAYROLL", "OTHER"}
)


@transaction.atomic
def record_expense(*, actor, category, description, amount, expense_date, currency=None,
                   property=None, unit=None, lease=None, maintenance_request=None,
                   vendor_contact=None, account=None, tax_amount=Decimal("0"),
                   is_billable_to_owner=False, document=None, status="DRAFT"):
    """The §1.2 entry point `property_ops.complete_work_order` hands costs to. `status` may
    arrive APPROVED from that path — completing an approved work order IS the approval act;
    making the PM re-approve the same number in a second screen is process theatre."""
    from .models import Expense

    amount = Decimal(str(amount))
    if amount < 0:
        raise ValidationError({"amount": "An expense cannot be negative."})
    if category not in EXPENSE_CATEGORIES:
        raise ValidationError({"category": f"Unknown expense category {category!r}."})
    if status not in ("DRAFT", "APPROVED"):
        raise ValidationError({"status": "An expense is recorded DRAFT or pre-APPROVED."})

    expense = Expense.objects.create(
        expense_number=next_reference("expense", prefix="EXP"),
        category=category,
        description=description,
        amount=amount,
        tax_amount=Decimal(str(tax_amount)),
        currency=currency or _default_currency(),
        expense_date=expense_date,
        property=property,
        unit=unit,
        lease=lease,
        maintenance_request=maintenance_request,
        vendor_contact=vendor_contact,
        account=account,
        is_billable_to_owner=is_billable_to_owner,
        document=document,
        status=status,
        approved_by=actor if status == "APPROVED" else None,
        created_by=actor,
        updated_by=actor,
    )
    return expense


@transaction.atomic
def approve_expense(expense, *, actor):
    from .models import Expense

    if expense.status != Expense.Status.DRAFT:
        raise ValidationError({"status": "Only a draft expense needs approval."})
    expense.status = Expense.Status.APPROVED
    expense.approved_by = actor
    expense.updated_by = actor
    expense.save(update_fields=["status", "approved_by", "updated_by", "updated_at"])
    return expense


@transaction.atomic
def pay_expense(expense, *, actor, account=None):
    """Money out: one DEBIT. The enum has no EXPENSE reference type, so the entry is an
    ADJUSTMENT whose description carries the expense number — documented, not hidden."""
    from .models import Expense

    if expense.status != Expense.Status.APPROVED:
        raise ValidationError({"status": "Approve the expense before paying it."})
    account = account or expense.account
    if account is None:
        raise ValidationError({"account": "Say which account the money leaves."})
    expense.status = Expense.Status.PAID
    expense.account = account
    expense.updated_by = actor
    expense.save(update_fields=["status", "account", "updated_by", "updated_at"])
    _post_entry(
        account=account,
        entry_type=AccountEntry.EntryType.DEBIT,
        amount=expense.amount + expense.tax_amount,
        currency=expense.currency,
        reference_type=AccountEntry.ReferenceType.ADJUSTMENT,
        reference_id=expense.pk,
        description=f"Expense {expense.expense_number}: {expense.description[:100]}",
    )
    return expense


@transaction.atomic
def void_expense(expense, *, actor, reason):
    from .models import Expense

    if not (reason or "").strip():
        raise ValidationError({"reason": "Voiding an expense needs a reason."})
    # Re-read: the caller's instance may predate a statement run that attached this expense,
    # and a void must be judged against the current truth, not a snapshot.
    expense.refresh_from_db(fields=["owner_statement", "status"])
    if expense.owner_statement_id:
        raise ValidationError(
            {"status": "This expense is on an owner statement; regenerate the draft first."}
        )
    if expense.status == Expense.Status.PAID:
        raise ValidationError(
            {"status": "A paid expense is money out; correct it with an adjustment."}
        )
    expense.status = Expense.Status.VOID
    expense.updated_by = actor
    expense.save(update_fields=["status", "updated_by", "updated_at"])
    return expense


# --- Commissions (SRS 3.6.4, 3.6.5, 3.6.7, 3.15.6) ------------------------------------------

COMMISSION_TRANSITIONS = {
    Commission.Status.CALCULATED: {
        Commission.Status.PENDING_APPROVAL, Commission.Status.REJECTED,
    },
    Commission.Status.PENDING_APPROVAL: {
        Commission.Status.APPROVED, Commission.Status.REJECTED,
    },
    Commission.Status.APPROVED: {Commission.Status.PAID},
    Commission.Status.REJECTED: set(),
    Commission.Status.PAID: set(),
}


def _commission_base(plan, *, transaction_obj=None, lease=None, net_amount=None) -> Decimal:
    if plan.base == CommissionPlan.Base.NET_AMOUNT:
        if net_amount is None:
            raise ValidationError(
                {"net_amount": "This plan calculates on net; provide the net amount."}
            )
        return Decimal(str(net_amount))
    if plan.base == CommissionPlan.Base.RENT_PERIODS:
        if lease is None:
            raise ValidationError({"lease": "RENT_PERIODS plans apply to leases."})
        periods = Decimal(str((plan.config or {}).get("periods", 1)))
        return (lease.rent_amount * periods).quantize(TWO_PLACES, ROUND_HALF_UP)
    # GROSS_AMOUNT
    if transaction_obj is not None:
        return transaction_obj.gross_amount
    if lease is not None:
        total = lease.rent_schedules.aggregate(total=Sum("amount"))["total"]
        return total if total is not None else lease.rent_amount
    raise ValidationError({"plan": "Nothing to calculate the base from."})


def _apply_plan(plan, base: Decimal) -> Decimal:
    """The gross commission a plan yields on a base. TIERED is **marginal** — each band's
    rate applies to the slice of the base inside that band, like income tax, because a cliff
    where one extra dirham of price cuts the whole commission is how agents learn to game
    the price."""
    if plan.plan_type == CommissionPlan.PlanType.FLAT_PERCENT:
        if plan.rate is None:
            raise ValidationError({"plan": "A FLAT_PERCENT plan needs a rate."})
        return (base * plan.rate / 100).quantize(TWO_PLACES, ROUND_HALF_UP)
    if plan.plan_type == CommissionPlan.PlanType.FIXED_AMOUNT:
        if plan.rate is None:
            raise ValidationError({"plan": "A FIXED_AMOUNT plan needs its amount in `rate`."})
        return plan.rate.quantize(TWO_PLACES)
    if plan.plan_type == CommissionPlan.PlanType.TIERED:
        return _apply_tiers(plan, base)
    if plan.plan_type == CommissionPlan.PlanType.SPLIT:
        rate = Decimal(str((plan.config or {}).get("rate", "0")))
        return (base * rate / 100).quantize(TWO_PLACES, ROUND_HALF_UP)
    raise ValidationError({"plan": f"Unknown plan type {plan.plan_type!r}."})


def _apply_tiers(plan, base: Decimal) -> Decimal:
    tiers = (plan.config or {}).get("tiers")
    if not tiers:
        raise ValidationError({"plan": "A TIERED plan needs config['tiers']."})
    gross, floor = Decimal("0"), Decimal("0")
    previous_cap = None
    for index, tier in enumerate(tiers):
        cap = tier.get("up_to")
        rate = Decimal(str(tier["rate"]))
        if cap is None:
            if index != len(tiers) - 1:
                raise ValidationError(
                    {"plan": "Only the last tier may be open-ended (up_to null)."}
                )
            slice_amount = max(base - floor, Decimal("0"))
        else:
            cap = Decimal(str(cap))
            if previous_cap is not None and cap <= previous_cap:
                raise ValidationError({"plan": "Tier caps must ascend."})
            previous_cap = cap
            slice_amount = max(min(base, cap) - floor, Decimal("0"))
            floor = cap
        gross += slice_amount * rate / 100
        if cap is not None and base <= cap:
            break
    else:
        if tiers[-1].get("up_to") is not None and base > previous_cap:
            raise ValidationError(
                {"plan": "The base exceeds the last tier; add an open-ended terminator."}
            )
    return gross.quantize(TWO_PLACES, ROUND_HALF_UP)


def _resolve_plan(plan, applies):
    if plan is not None:
        return plan
    candidates = list(
        CommissionPlan.objects.filter(is_active=True, applies_to__in=[applies, "BOTH"])[:2]
    )
    if len(candidates) != 1:
        raise ValidationError(
            {"plan": "No single default commission plan; name one explicitly."}
        )
    return candidates[0]


@transaction.atomic
def create_commission_for_transaction(*, actor, transaction_obj, plan=None, agent=None,
                                      deductions=Decimal("0"), net_amount=None, splits=None):
    """§1.2's named entry point for sale commissions."""
    plan = _resolve_plan(plan, "SALE")
    if agent is None:
        agent = transaction_obj.deal.owner if transaction_obj.deal_id else None
    if agent is None:
        raise ValidationError(
            {"agent": "This transaction has no deal; name the earning agent."}
        )
    base = _commission_base(plan, transaction_obj=transaction_obj, net_amount=net_amount)
    return _create_commission(
        actor=actor, plan=plan, agent=agent, base=base,
        transaction_obj=transaction_obj, lease=None,
        deductions=Decimal(str(deductions)), splits=splits,
    )


@transaction.atomic
def create_commission_for_lease(*, actor, lease, plan=None, agent=None,
                                deductions=Decimal("0"), splits=None):
    """§1.2: letting commission straight off the lease — never a fake crm transaction."""
    plan = _resolve_plan(plan, "RENTAL")
    if agent is None:
        agent = (
            lease.transaction.deal.owner
            if lease.transaction_id and lease.transaction.deal_id
            else lease.property_manager
        )
    base = _commission_base(plan, lease=lease)
    return _create_commission(
        actor=actor, plan=plan, agent=agent, base=base,
        transaction_obj=None, lease=lease,
        deductions=Decimal(str(deductions)), splits=splits,
    )


def _create_commission(*, actor, plan, agent, base, transaction_obj, lease, deductions,
                       splits):
    gross = _apply_plan(plan, base)
    tax = (gross * _vat_rate() / 100).quantize(TWO_PLACES, ROUND_HALF_UP)
    net = gross - deductions
    if net < 0:
        raise ValidationError({"deductions": "Deductions exceed the gross commission."})

    commission = Commission.objects.create(
        transaction=transaction_obj,
        lease=lease,
        agent=agent,
        commission_plan=plan,
        gross_commission=gross,
        tax_amount=tax,
        deductions=deductions,
        net_commission=net,
        status=Commission.Status.CALCULATED,
    )
    if splits is None and plan.plan_type == CommissionPlan.PlanType.SPLIT:
        splits = _default_splits(plan, agent)
    if splits:
        set_commission_splits(commission, splits, actor=actor)
    return commission


def _default_splits(plan, primary_agent):
    """SPLIT plans carry their distribution in config — including the head-office share as
    a NAMED split row, not a deduction, so the sum-to-100 rule stays whole (SRS 3.15.6)."""
    rows = []
    for entry in (plan.config or {}).get("splits", []):
        row = {
            "recipient_type": entry["recipient_type"],
            "percentage": entry["percentage"],
        }
        if entry.get("source") == "primary_agent":
            row["recipient_user"] = primary_agent
        elif entry.get("recipient_user_id"):
            row["recipient_user"] = entry["recipient_user_id"]
        elif entry.get("recipient_contact_id"):
            row["recipient_contact"] = entry["recipient_contact_id"]
        rows.append(row)
    return rows


@transaction.atomic
def set_commission_splits(commission, splits, *, actor):
    """Replace the split set. Typing enforced here (AGENT/BROKER → a user,
    REFERRAL_EXTERNAL → a contact); amounts derived from percentages with the rounding
    residue folded into the largest split so Σ amounts equals the net exactly."""
    from apps.contacts.models import Contact
    from apps.identity.models import User

    if commission.status not in (
        Commission.Status.CALCULATED, Commission.Status.PENDING_APPROVAL
    ):
        raise ValidationError({"status": "Splits are frozen once the commission is decided."})

    resolved = []
    for entry in splits:
        recipient_type = entry["recipient_type"]
        user = entry.get("recipient_user")
        contact = entry.get("recipient_contact")
        if user is not None and not isinstance(user, User):
            user = User.objects.get(pk=user)
        if contact is not None and not isinstance(contact, Contact):
            contact = Contact.objects.get(pk=contact)
        if recipient_type in ("AGENT", "BROKER"):
            if user is None or contact is not None:
                raise ValidationError(
                    {"splits": f"{recipient_type} splits go to a user, not a contact."}
                )
        elif recipient_type == "REFERRAL_EXTERNAL":
            if contact is None or user is not None:
                raise ValidationError(
                    {"splits": "REFERRAL_EXTERNAL splits go to a contact, not a user."}
                )
        else:
            raise ValidationError({"splits": f"Unknown recipient type {recipient_type!r}."})
        resolved.append(
            {"recipient_type": recipient_type, "recipient_user": user,
             "recipient_contact": contact,
             "percentage": Decimal(str(entry["percentage"]))}
        )

    commission.splits.all().delete()
    rows, allocated = [], Decimal("0")
    for entry in resolved:
        amount = (commission.net_commission * entry["percentage"] / 100).quantize(
            TWO_PLACES, ROUND_HALF_UP
        )
        allocated += amount
        rows.append(CommissionSplit(commission=commission, amount=amount, **entry))
    if rows:
        residue = commission.net_commission - allocated
        if residue and sum(r.percentage for r in rows) == 100:
            largest = max(rows, key=lambda r: r.amount)
            largest.amount += residue
    CommissionSplit.objects.bulk_create(rows)
    return rows


@transaction.atomic
def submit_commission(commission, *, actor):
    _commission_move(commission, Commission.Status.PENDING_APPROVAL)
    commission.save(update_fields=["status", "updated_at"])
    return commission


def _commission_move(commission, new_status):
    allowed = COMMISSION_TRANSITIONS.get(commission.status, set())
    if new_status not in allowed:
        raise ValidationError(
            {"status": f"Cannot move a commission from {commission.status} to {new_status}."}
        )
    commission.status = new_status


@transaction.atomic
def approve_commission(commission, *, actor):
    """THE approval-time invariant (§11): splits, when present, must sum to exactly 100% or
    to the net amount; no splits means an implicit 100% to the earning agent."""
    _commission_move(commission, Commission.Status.APPROVED)
    splits = list(commission.splits.all())
    if splits:
        pct = sum(s.percentage for s in splits)
        amounts = sum(s.amount for s in splits)
        if pct != Decimal("100") and amounts != commission.net_commission:
            raise ValidationError(
                {"splits": (
                    f"Splits must sum to 100% or to the net commission; "
                    f"got {pct}% / {amounts}."
                )}
            )
    commission.approved_by = actor
    commission.approved_at = timezone.now()
    commission.save(update_fields=["status", "approved_by", "approved_at", "updated_at"])

    signals.commission_approved.send_robust(sender=None, commission=commission, actor=actor)
    _notify_commission(commission, actor)
    return commission


def _notify_commission(commission, actor):
    from apps.collaboration import services as collaboration_services

    recipients = {commission.agent}
    recipients.update(
        split.recipient_user
        for split in commission.splits.all()
        if split.recipient_user is not None
    )
    for recipient in recipients:
        collaboration_services.notify(
            recipient=recipient,
            type="COMMISSION_APPROVED",
            title=f"Commission approved: {commission.net_commission}",
            entity_type="COMMISSION",
            entity_id=commission.pk,
            actor=actor,
        )


@transaction.atomic
def reject_commission(commission, *, actor, reason):
    if not (reason or "").strip():
        raise ValidationError({"reason": "Rejecting a commission needs a reason."})
    _commission_move(commission, Commission.Status.REJECTED)
    commission.save(update_fields=["status", "updated_at"])
    return commission


@transaction.atomic
def pay_commission(commission, *, actor, account, paid_at=None):
    _commission_move(commission, Commission.Status.PAID)
    commission.paid_at = paid_at or timezone.now()
    commission.save(update_fields=["status", "paid_at", "updated_at"])
    _post_entry(
        account=account,
        entry_type=AccountEntry.EntryType.DEBIT,
        amount=commission.net_commission,
        reference_type=AccountEntry.ReferenceType.COMMISSION,
        reference_id=commission.pk,
        description=f"Commission payout to {commission.agent.full_name}",
    )
    signals.commission_paid.send_robust(
        sender=None, commission=commission, actor=actor, account=account
    )
    return commission


@transaction.atomic
def create_commission_invoice(commission, *, actor, contact, due_date):
    """Bill the client for the commission (SRS 3.6.2/3.6.7) — VAT rides on top as computed
    at calculation time."""
    return create_invoice(
        actor=actor,
        contact=contact,
        invoice_type=Invoice.InvoiceType.COMMISSION,
        transaction_obj=commission.transaction,
        lease=commission.lease,
        issue_date=timezone.localdate(),
        due_date=due_date,
        lines=[
            {
                "description": "Brokerage commission",
                "quantity": 1,
                "unit_price": commission.gross_commission,
                "tax_rate": (
                    (commission.tax_amount / commission.gross_commission * 100)
                    if commission.gross_commission
                    else Decimal("0")
                ),
            }
        ],
        issue=True,
    )


# --- Installment plans (SRS 3.6.3, 3.6.8) ---------------------------------------------------


@transaction.atomic
def create_installment_plan(*, actor, transaction_obj, name, currency, total_amount,
                            milestones):
    """§11: created only through this service. Percentage milestones resolve to amounts
    immediately (residue folded into the last row); Σ == total is enforced on ACTIVATE, not
    here, so a plan can be drafted incrementally."""
    total = Decimal(str(total_amount))
    if total <= 0:
        raise ValidationError({"total_amount": "A plan must be worth something."})
    plan = InstallmentPlan.objects.create(
        transaction=transaction_obj, name=name, currency=currency, total_amount=total,
        created_by=actor, updated_by=actor,
    )
    _write_milestones(plan, milestones)
    return plan


def _write_milestones(plan, milestones):
    rows, allocated = [], Decimal("0")
    for order, entry in enumerate(milestones):
        if entry.get("amount") is not None:
            amount = Decimal(str(entry["amount"]))
        elif entry.get("percentage") is not None:
            amount = (
                plan.total_amount * Decimal(str(entry["percentage"])) / 100
            ).quantize(TWO_PLACES, ROUND_HALF_UP)
        else:
            raise ValidationError({"milestones": "Each milestone needs an amount or a %."})
        allocated += amount
        rows.append(
            InstallmentMilestone(
                plan=plan, label=entry["label"], due_date=entry["due_date"],
                amount=amount, percentage=entry.get("percentage"),
                sort_order=entry.get("sort_order", order),
            )
        )
    if rows and all(entry.get("percentage") is not None for entry in milestones):
        residue = plan.total_amount - allocated
        if residue:
            rows[-1].amount += residue
    InstallmentMilestone.objects.bulk_create(rows)
    return rows


@transaction.atomic
def update_installment_plan(plan, *, actor, milestones=None, **fields):
    if plan.status != InstallmentPlan.Status.DRAFT:
        raise ValidationError({"status": "Only a draft plan is editable."})
    _reject_unknown(fields, frozenset({"name", "total_amount"}), "installment plan")
    for name, value in fields.items():
        setattr(plan, name, value if name != "total_amount" else Decimal(str(value)))
    plan.updated_by = actor
    plan.save()
    if milestones is not None:
        plan.milestones.all().delete()
        _write_milestones(plan, milestones)
    return plan


@transaction.atomic
def activate_installment_plan(plan, *, actor):
    """§11's activation invariant: the milestones must account for every dirham of the plan."""
    if plan.status != InstallmentPlan.Status.DRAFT:
        raise ValidationError({"status": "Only a draft plan activates."})
    total = plan.milestones.aggregate(total=Sum("amount"))["total"] or Decimal("0")
    if total != plan.total_amount:
        raise ValidationError(
            {"milestones": f"Milestones sum to {total}, plan is {plan.total_amount}."}
        )
    plan.status = InstallmentPlan.Status.ACTIVE
    plan.updated_by = actor
    plan.save(update_fields=["status", "updated_by", "updated_at"])
    return plan


@transaction.atomic
def invoice_milestone(milestone, *, actor, contact=None, due_date=None):
    """Bill one milestone. FK and INVOICED status land in the same save — the CHECK demands
    an invoiced milestone carry its invoice."""
    plan = milestone.plan
    if plan.status != InstallmentPlan.Status.ACTIVE:
        raise ValidationError({"status": "Activate the plan before invoicing milestones."})
    if milestone.status not in (
        InstallmentMilestone.Status.PENDING, InstallmentMilestone.Status.OVERDUE
    ):
        raise ValidationError({"status": "This milestone is already invoiced or settled."})
    if contact is None:
        deal = plan.transaction.deal
        contact = deal.primary_contact if deal else None
    if contact is None:
        raise ValidationError({"contact": "No deal on the transaction; name the payer."})

    invoice = create_invoice(
        actor=actor,
        contact=contact,
        invoice_type=Invoice.InvoiceType.SALE,
        transaction_obj=plan.transaction,
        issue_date=timezone.localdate(),
        due_date=due_date or milestone.due_date,
        lines=[
            {"description": f"{plan.name} — {milestone.label}", "quantity": 1,
             "unit_price": milestone.amount, "tax_rate": 0}
        ],
        issue=True,
    )
    milestone.invoice = invoice
    milestone.status = InstallmentMilestone.Status.INVOICED
    milestone.save(update_fields=["invoice", "status"])
    return invoice


@transaction.atomic
def waive_milestone(milestone, *, actor, reason):
    if not (reason or "").strip():
        raise ValidationError({"reason": "Waiving money needs a reason."})
    if milestone.status not in (
        InstallmentMilestone.Status.PENDING, InstallmentMilestone.Status.OVERDUE
    ):
        raise ValidationError({"status": "Only an uninvoiced milestone can be waived."})
    milestone.status = InstallmentMilestone.Status.WAIVED
    milestone.save(update_fields=["status"])
    return milestone


@transaction.atomic
def cancel_installment_plan(plan, *, actor, reason):
    if not (reason or "").strip():
        raise ValidationError({"reason": "Cancelling a plan needs a reason."})
    if plan.status not in (InstallmentPlan.Status.DRAFT, InstallmentPlan.Status.ACTIVE):
        raise ValidationError({"status": "This plan is already settled."})
    unpaid = plan.milestones.filter(
        status=InstallmentMilestone.Status.INVOICED,
        invoice__balance_due__gt=0,
    )
    if unpaid.exists():
        raise ValidationError(
            {"milestones": "Invoiced milestones are outstanding; void their invoices first."}
        )
    plan.status = InstallmentPlan.Status.CANCELLED
    plan.updated_by = actor
    plan.save(update_fields=["status", "updated_by", "updated_at"])
    return plan


# --- Owner statements (SRS 3.5.6, 3.14.4) ---------------------------------------------------


@transaction.atomic
def generate_owner_statement(*, actor, owner_contact, period_start, period_end,
                             property=None, currency=None):
    """What the landlord is owed for a period, on a **collection basis** — it is "gross rent
    COLLECTED", so the lines come from POSTED payment allocations dated in the period, not
    from invoices issued in it.

    Idempotency: an overlapping non-DRAFT statement refuses; an overlapping DRAFT is
    regenerated in place (lines deleted, expenses detached, recomputed), which is what lets
    the month-end run repeat safely.
    """
    import datetime

    from apps.property_ops.models import Lease

    from .models import Expense, OwnerStatement, OwnerStatementLine, PaymentAllocation

    if isinstance(period_start, str):
        period_start = datetime.date.fromisoformat(period_start)
    if isinstance(period_end, str):
        period_end = datetime.date.fromisoformat(period_end)
    if period_end <= period_start:
        raise ValidationError({"period_end": "A period must end after it starts."})

    overlapping = OwnerStatement.objects.filter(
        owner_contact=owner_contact,
        period_start__lte=period_end,
        period_end__gte=period_start,
    )
    if property is not None:
        overlapping = overlapping.filter(property=property)
    settled = overlapping.exclude(status=OwnerStatement.Status.DRAFT).first()
    if settled is not None:
        raise ValidationError(
            {"period_start": f"Statement {settled.statement_number} already covers this "
                             f"period and is {settled.status}."}
        )
    statement = overlapping.filter(status=OwnerStatement.Status.DRAFT).first()
    if statement is not None:
        statement.lines.all().delete()
        Expense.objects.filter(owner_statement=statement).update(owner_statement=None)
        statement.period_start, statement.period_end = period_start, period_end
    else:
        statement = OwnerStatement(
            statement_number=next_reference("owner_statement", prefix="STMT"),
            owner_contact=owner_contact,
            property=property,
            period_start=period_start,
            period_end=period_end,
            currency=currency or _default_currency(),
            net_payable=Decimal("0"),
            created_by=actor,
            updated_by=actor,
        )

    leases = Lease.objects.filter(landlord=owner_contact)
    if property is not None:
        leases = leases.filter(property=property)
    lease_ids = list(leases.values_list("pk", flat=True))

    lines = []

    # RENT collected: allocations of POSTED payments, dated by payment inside the period.
    collected_by_lease = {}
    allocations = (
        PaymentAllocation.objects.filter(
            payment__status=Payment.Status.POSTED,
            payment__payment_date__gte=period_start,
            payment__payment_date__lte=period_end,
            invoice__lease_id__in=lease_ids,
            invoice__invoice_type=Invoice.InvoiceType.RENT,
        )
        .select_related("payment", "invoice")
    )
    gross = Decimal("0")
    for allocation in allocations:
        gross += allocation.allocated_amount
        lease_id = allocation.invoice.lease_id
        collected_by_lease[lease_id] = (
            collected_by_lease.get(lease_id, Decimal("0")) + allocation.allocated_amount
        )
        lines.append(
            OwnerStatementLine(
                statement=statement,
                line_type=OwnerStatementLine.LineType.RENT,
                description=f"Rent collected — {allocation.invoice.invoice_number}",
                amount=allocation.allocated_amount,
                reference_type="PAYMENT",
                reference_id=allocation.payment_id,
                occurred_on=allocation.payment.payment_date,
            )
        )

    # Management fees: PERCENT of what was collected per lease (default), or FIXED per
    # collected period.
    fee_mode = _company_setting("management_fee_mode", "PERCENT")
    fees = Decimal("0")
    for lease in leases.filter(management_fee__isnull=False):
        collected = collected_by_lease.get(lease.pk, Decimal("0"))
        if collected <= 0:
            continue
        if fee_mode == "FIXED":
            periods_paid = (
                allocations.filter(invoice__lease=lease)
                .values("invoice").distinct().count()
            )
            fee = (lease.management_fee * periods_paid).quantize(TWO_PLACES)
        else:
            fee = (collected * lease.management_fee / 100).quantize(
                TWO_PLACES, ROUND_HALF_UP
            )
        if fee > 0:
            fees += fee
            lines.append(
                OwnerStatementLine(
                    statement=statement,
                    line_type=OwnerStatementLine.LineType.MANAGEMENT_FEE,
                    description=f"Management fee — {lease.reference_code}",
                    amount=fee,
                    reference_type="LEASE",
                    reference_id=lease.pk,
                )
            )

    # Billable expenses in the period, not yet on any statement. Attaching them to this
    # statement is the double-billing guard across regenerations and future periods.
    expense_filter = Q(lease_id__in=lease_ids)
    if property is not None:
        expense_filter |= Q(property=property)
    else:
        expense_filter |= Q(property__owners__contact=owner_contact)
    billable = (
        Expense.objects.filter(
            expense_filter,
            is_billable_to_owner=True,
            status__in=(Expense.Status.APPROVED, Expense.Status.PAID),
            expense_date__gte=period_start,
            expense_date__lte=period_end,
            owner_statement__isnull=True,
        )
        .distinct()
    )
    expenses_total = Decimal("0")
    statement.save()  # need the pk before attaching
    for expense in billable:
        expenses_total += expense.amount + expense.tax_amount
        expense.owner_statement = statement
        expense.save(update_fields=["owner_statement"])
        lines.append(
            OwnerStatementLine(
                statement=statement,
                line_type=OwnerStatementLine.LineType.EXPENSE,
                description=f"{expense.expense_number}: {expense.description[:120]}",
                amount=expense.amount + expense.tax_amount,
                reference_type="EXPENSE",
                reference_id=expense.pk,
                occurred_on=expense.expense_date,
            )
        )

    statement.gross_rent_collected = gross
    statement.management_fees = fees
    statement.expenses_total = expenses_total
    statement.other_deductions = Decimal("0")
    statement.net_payable = gross - fees - expenses_total
    statement.updated_by = actor
    statement.save()
    OwnerStatementLine.objects.bulk_create(lines)
    return statement


@transaction.atomic
def add_statement_adjustment(statement, *, actor, description, amount,
                             line_type="DEDUCTION", occurred_on=None):
    from .models import OwnerStatement, OwnerStatementLine

    if statement.status != OwnerStatement.Status.DRAFT:
        raise ValidationError({"status": "Only a draft statement takes adjustments."})
    amount = Decimal(str(amount))
    OwnerStatementLine.objects.create(
        statement=statement, line_type=line_type, description=description,
        amount=amount, occurred_on=occurred_on,
    )
    statement.other_deductions += amount
    statement.net_payable -= amount
    statement.updated_by = actor
    statement.save(
        update_fields=["other_deductions", "net_payable", "updated_by", "updated_at"]
    )
    return statement


@transaction.atomic
def issue_owner_statement(statement, *, actor):
    from .models import OwnerStatement

    if statement.status != OwnerStatement.Status.DRAFT:
        raise ValidationError({"status": "Only a draft statement can be issued."})
    statement.status = OwnerStatement.Status.ISSUED
    statement.issued_at = timezone.now()
    statement.updated_by = actor
    statement.save(update_fields=["status", "issued_at", "updated_by", "updated_at"])

    signals.statement_issued.send_robust(sender=None, statement=statement, actor=actor)
    _notify_landlord(statement, actor)
    return statement


def _notify_landlord(statement, actor):
    from apps.collaboration import services as collaboration_services
    from apps.identity.models import PortalProfile

    profile = PortalProfile.objects.filter(
        contact=statement.owner_contact,
        eligibility_status=PortalProfile.EligibilityStatus.ACTIVE,
    ).first()
    if profile:
        collaboration_services.notify(
            recipient=profile.user,
            type="SYSTEM",
            title=f"Owner statement {statement.statement_number}",
            body=f"Net payable {statement.net_payable} {statement.currency}.",
            entity_type="OWNER_STATEMENT",
            entity_id=statement.pk,
            actor=actor,
        )


@transaction.atomic
def mark_statement_paid(statement, *, actor, account, paid_at=None):
    from .models import OwnerStatement

    if statement.status != OwnerStatement.Status.ISSUED:
        raise ValidationError({"status": "Issue the statement before paying it."})
    statement.status = OwnerStatement.Status.PAID
    statement.paid_at = paid_at or timezone.now()
    statement.updated_by = actor
    statement.save(update_fields=["status", "paid_at", "updated_by", "updated_at"])
    if statement.net_payable > 0:
        _post_entry(
            account=account,
            entry_type=AccountEntry.EntryType.DEBIT,
            amount=statement.net_payable,
            currency=statement.currency,
            reference_type=AccountEntry.ReferenceType.ADJUSTMENT,
            reference_id=statement.pk,
            description=f"Owner payout {statement.statement_number}",
        )
    return statement


# --- Reconciliation (§11) -------------------------------------------------------------------


@transaction.atomic
def start_reconciliation(*, actor, account, period_start, period_end,
                         opening_balance, closing_balance_statement):
    from .models import Reconciliation

    opening = Decimal(str(opening_balance))
    stated = Decimal(str(closing_balance_statement))
    system = opening + _period_movement(account, period_start, period_end)
    return Reconciliation.objects.create(
        account=account,
        period_start=period_start,
        period_end=period_end,
        opening_balance=opening,
        closing_balance_statement=stated,
        closing_balance_system=system,
        difference=stated - system,  # set together with system — the CHECK holds
        status=Reconciliation.Status.OPEN,
    )


def _period_movement(account, period_start, period_end) -> Decimal:
    entries = AccountEntry.objects.filter(
        account=account,
        posted_at__date__gte=period_start,
        posted_at__date__lte=period_end,
    )
    sums = {row["entry_type"]: row["total"]
            for row in entries.values("entry_type").annotate(total=Sum("amount"))}
    return (sums.get("CREDIT") or Decimal("0")) - (sums.get("DEBIT") or Decimal("0"))


@transaction.atomic
def refresh_reconciliation(recon, *, actor):
    from .models import Reconciliation

    if recon.status not in (
        Reconciliation.Status.OPEN, Reconciliation.Status.IN_PROGRESS,
        Reconciliation.Status.DISCREPANCY,
    ):
        raise ValidationError({"status": "This reconciliation is settled."})
    system = recon.opening_balance + _period_movement(
        recon.account, recon.period_start, recon.period_end
    )
    recon.closing_balance_system = system
    recon.difference = recon.closing_balance_statement - system
    recon.status = Reconciliation.Status.IN_PROGRESS
    recon.save(
        update_fields=["closing_balance_system", "difference", "status", "updated_at"]
    )
    return recon


@transaction.atomic
def complete_reconciliation(recon, *, actor, notes=None):
    """Difference zero → RECONCILED (the CHECK enforces it too); anything else →
    DISCREPANCY, corrected via `post_adjustment(reference_type=RECONCILIATION)` then
    refresh + complete again."""
    from .models import Reconciliation

    refresh_reconciliation(recon, actor=actor)
    if notes:
        recon.notes = notes
    if recon.difference == 0:
        recon.status = Reconciliation.Status.RECONCILED
        recon.reconciled_by = actor
        recon.reconciled_at = timezone.now()
        recon.save(
            update_fields=["status", "reconciled_by", "reconciled_at", "notes", "updated_at"]
        )
    else:
        recon.status = Reconciliation.Status.DISCREPANCY
        recon.save(update_fields=["status", "notes", "updated_at"])
    return recon
